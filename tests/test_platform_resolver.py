from __future__ import annotations

import io
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest
from pypdf import PdfWriter

from PaperPipeline.downloader import (
    DownloadConfig,
    PdfDownloader,
    _migrate_dbpia_identifier_manifest,
    download_artifact_is_current,
    sanitize_dbpia_download_manifest,
)
from PaperPipeline.http_client import HttpFetch
from PaperPipeline.platform_resolver import (
    AuthorizedPlatformPdfResolver,
    DBPIA_LAYOUT_VERIFIED_PROVENANCE,
    DBPIA_RESOLVER_VERIFIED_PROVENANCE,
    KISS_RESOLVER_VERIFIED_PROVENANCE,
    PlatformResolverConfig,
    _contains_expected_doi,
    _extract_dois,
    _kiss_pdf_bibliographic_provenance,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import ArtifactPaths, write_json_atomic


TITLE = "디지털 포렌식 증거수집 기법 연구"
ENGLISH_TITLE = "A Study of Digital Forensic Evidence Collection"
DOI = "10.1234/forensics.2025.1"
DOI_URL = f"https://doi.org/{DOI}"
ASCII_TITLE = "Electronic Information Warrant System Study"
KISS_TITLE = "Electronic Information Seizure Legality Requirements"
LONG_TITLE = (
    "대리전문가 증언의 허용성과 대면권의 한계 : "
    "미연방대법원 Smith v. Arizona 판결(2024)과 시사점"
)


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    final_url: str | None = None
    requested_url: str | None = None
    content_type: str = "text/html; charset=utf-8"
    redirects: tuple[str, ...] = ()


class FakeHttp:
    def __init__(
        self,
        *,
        gets: list[Reply],
        posts: list[Reply] | None = None,
    ) -> None:
        self.gets = deque(gets)
        self.posts = deque(posts or [])
        self.get_calls: list[
            tuple[str, int, int | None, dict[str, str] | None]
        ] = []
        self.post_calls: list[
            tuple[str, dict[str, str], int, dict[str, str] | None]
        ] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        self.get_calls.append((url, max_bytes, max_html_bytes, headers))
        return _fetch(self.gets.popleft(), requested_url=url)

    def post_form_bytes(
        self,
        url: str,
        *,
        data: dict[str, str],
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        self.post_calls.append((url, data, max_bytes, headers))
        return _fetch(self.posts.popleft(), requested_url=url)


def _fetch(reply: Reply, *, requested_url: str) -> HttpFetch:
    return HttpFetch(
        requested_url=reply.requested_url or requested_url,
        final_url=reply.final_url or requested_url,
        status_code=reply.status,
        headers={"Content-Type": reply.content_type},
        body=reply.body,
        redirects=reply.redirects,
    )


def _paper(
    *,
    doi: str | None = DOI,
    title: str | None = TITLE,
    alternate_titles: tuple[str, ...] = (ENGLISH_TITLE,),
    year: int | None = 2025,
    authors: tuple[str, ...] = (),
    journal_id: str | None = None,
    institution_id: str | None = None,
    issue_id: str | None = None,
    first_page: str | None = "1",
    final_page: str | None = "10",
) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id="ART003999999",
        title=title,
        doi_original=doi,
        doi=doi,
        landing_url=None,
        abstract=None,
        publication_year=year,
        first_page=first_page,
        final_page=final_page,
        alternate_titles=alternate_titles,
        authors=authors,
        journal_id=journal_id,
        institution_id=institution_id,
        issue_id=issue_id,
    )


def _pdf(*, title: str = TITLE) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _pdf_escape(value: str) -> bytes:
    return (
        value.encode("latin-1")
        .replace(b"\\", b"\\\\")
        .replace(b"(", b"\\(")
        .replace(b")", b"\\)")
    )


def _text_pdf(page_texts: list[str]) -> bytes:
    font_id = 3 + 2 * len(page_texts)
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_ids: list[int] = []
    for index, text in enumerate(page_texts):
        page_id = 3 + 2 * index
        content_id = page_id + 1
        page_ids.append(page_id)
        stream = (
            b"BT\n/F1 12 Tf\n72 720 Td\n("
            + _pdf_escape(text)
            + b") Tj\nET\n"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode()
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"endstream"
        )
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    ).encode()
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (font_id + 1)
    for object_id in range(1, font_id + 1):
        offsets[object_id] = len(pdf)
        pdf.extend(f"{object_id} 0 obj\n".encode())
        pdf.extend(objects[object_id])
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {font_id + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for object_id in range(1, font_id + 1):
        pdf.extend(f"{offsets[object_id]:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer << /Size {font_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(pdf)


def _landing_html(
    body: str,
    *,
    title: str = TITLE,
    doi: str | None = DOI,
    publication_date: str | None = "2025/07",
    extra_meta: str = "",
) -> bytes:
    doi_meta = (
        f'<meta name="citation_doi" content="{doi}">'
        if doi is not None
        else ""
    )
    date_meta = (
        f'<meta name="citation_publication_date" content="{publication_date}">'
        if publication_date is not None
        else ""
    )
    return (
        "<!doctype html><html><head>"
        f'<meta name="citation_title" content="{title}">'
        f"{doi_meta}"
        f"{date_meta}"
        f"{extra_meta}"
        f"</head><body>{body}</body></html>"
    ).encode()


def _dbpia_bibliographic_meta(
    *,
    publisher: str = "Korea Digital Forensics Society",
    journal: str = "Digital Forensics Research",
    volume: str = "18",
    issue: str = "1",
    first_page: str = "11",
    final_page: str = "23",
    authors: tuple[str, ...] = ("Alice Example", "Bob Example"),
) -> str:
    return (
        f'<meta name="publisher" content="{publisher}">'
        f'<meta name="citation_journal_title" content="{journal}">'
        f'<meta name="citation_volume" content="{volume}">'
        f'<meta name="citation_issue" content="{issue}">'
        f'<meta name="citation_firstpage" content="{first_page}">'
        f'<meta name="citation_lastpage" content="{final_page}">'
        + "".join(
            f'<meta name="citation_author" content="{author}">'
            for author in authors
        )
    )


def _resolver(http: FakeHttp) -> AuthorizedPlatformPdfResolver:
    return AuthorizedPlatformPdfResolver(
        PlatformResolverConfig(permission_granted=True),
        http=http,
    )


def _dbpia_http(
    *,
    landing_title: str,
    pdf_title: str,
    filename: str,
    publication_date: str | None = "2025/07",
    auth_node: str = "NODE12345678",
    doi: str = DOI,
    pdf_content: bytes | None = None,
    landing_url: str | None = None,
) -> tuple[FakeHttp, str, str]:
    resolved_landing_url = landing_url or (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE12345678"
    )
    one_use = (
        "https://download.dbpia.co.kr/File/Download?"
        "key=secret-one-use-value"
    )
    cdn_url = (
        "https://downloadcf.dbpia.co.kr/1/23/45/1234567.pdf?"
        + urlencode({"jwt": "secret-jwt", "filename": filename})
    )
    landing = _landing_html(
        """
        <button onclick="onClickDownloadNode(
          'NODE12345678','downloadNodeButton_detail', false
        )">다운로드</button>
        """,
        title=landing_title,
        doi=doi,
        publication_date=publication_date,
    )
    auth = json.dumps(
        {
            "nodeId": auth_node,
            "showAlert_YN": "N",
            "resultCode": "",
            "link": one_use,
            "cnt": "0",
        }
    ).encode()
    return (
        FakeHttp(
            gets=[
                Reply(
                    landing,
                    final_url=resolved_landing_url,
                    redirects=(resolved_landing_url,),
                ),
                Reply(
                    pdf_content or _pdf(title=pdf_title),
                    final_url=cdn_url,
                    requested_url=one_use,
                    redirects=(cdn_url,),
                    content_type="application/pdf",
                ),
            ],
            posts=[
                Reply(
                    auth,
                    final_url=(
                        "https://www.dbpia.co.kr/download/downloadData"
                    ),
                    content_type="application/json",
                ),
            ],
        ),
        one_use,
        cdn_url,
    )


def _kiss_http(
    *,
    landing_title: str,
    pdf_content: bytes,
    doi: str = DOI,
    publication_date: str = "2025/07",
) -> FakeHttp:
    landing_url = "https://kiss.kstudy.com/Detail/Ar?key=landing-key"
    pdf_url = (
        "https://kisspdf.kstudy.com/File/Down?"
        "keyn=7r000306&code=4652011827580063102"
    )
    return FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    "<button onclick=\"pdfDown('4138758');\">"
                    "다운로드</button>",
                    title=landing_title,
                    doi=doi,
                    publication_date=publication_date,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                pdf_content,
                final_url=pdf_url,
                requested_url=pdf_url,
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "resultCode": "Success",
                        "code": "4652011827580063102",
                        "down": "7r000306",
                    }
                ).encode(),
                content_type="application/json",
            )
        ],
    )


def _earticle_search_html(
    *results: tuple[str, str, int, tuple[str, ...]],
    page_title: str = "검색결과 - earticle",
    include_container: bool = True,
) -> bytes:
    rows: list[str] = []
    for article_path, title, year, authors in results:
        author_links = ", ".join(
            (
                '<a href="/Search/Result?sf=3&amp;q='
                f'{author}"><span>{author}</span></a>'
            )
            for author in authors
        )
        rows.append(
            '<div class="art-summary">'
            '<div class="info">'
            '<p class="title">'
            f'<a href="{article_path}"><span>{title}</span></a>'
            "</p>"
            f'<p class="author">{author_links}</p>'
            '<p class="pubInfo">'
            f'<span class="dt" title="발행일:{year}.12">{year}.12</span>'
            "</p>"
            "</div>"
            "</div>"
        )
    container = (
        f'<div class="search_result"><div class="list">{"".join(rows)}</div></div>'
        if include_container
        else "".join(rows)
    )
    return (
        "<!doctype html><html><head>"
        f"<title>{page_title}</title>"
        f"</head><body>{container}</body></html>"
    ).encode()


def test_disabled_by_default_without_network() -> None:
    http = FakeHttp(gets=[])

    result = AuthorizedPlatformPdfResolver(http=http).resolve(_paper())

    assert result.status == "permission_required"
    assert result.reason == "authorized_platform_collection_disabled"
    assert not http.get_calls
    assert not http.post_calls


def test_requires_title_and_year_for_doi_less_discovery_without_network() -> None:
    http = FakeHttp(gets=[])
    resolver = _resolver(http)

    missing_title = resolver.resolve(
        _paper(title=None, alternate_titles=()),
    )
    missing_year = resolver.resolve(_paper(doi=None, year=None))

    assert missing_title.reason == "missing_known_title"
    assert missing_year.reason == "missing_earticle_search_year"
    assert not http.get_calls


def test_dbpia_official_flow_returns_already_validated_pdf() -> None:
    landing_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE12345678"
    )
    one_use = (
        "https://download.dbpia.co.kr/File/Download?"
        "key=secret-one-use-value"
    )
    cdn_url = (
        "https://downloadcf.dbpia.co.kr/1/23/45/1234567.pdf?"
        "filename=paper.pdf&jwt=secret-jwt"
    )
    landing = _landing_html(
        """
        <button onclick="onClickDownloadNode(
          'NODE12345678','downloadNodeButton_detail', false
        )">다운로드</button>
        """
    )
    auth = json.dumps(
        {
            "nodeId": "NODE12345678",
            "showAlert_YN": "N",
            "resultCode": "",
            "link": one_use,
            "cnt": "0",
        }
    ).encode()
    http = FakeHttp(
        gets=[
            Reply(
                landing,
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                _pdf(),
                final_url=cdn_url,
                requested_url=one_use,
                redirects=(cdn_url,),
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                auth,
                final_url=(
                    "https://www.dbpia.co.kr/download/downloadData"
                ),
                content_type="application/json",
            ),
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.resolved
    assert result.platform == "dbpia"
    assert result.page_count == 1
    assert result.identity_status == "metadata_title_match"
    assert result.candidate is not None
    assert result.candidate.url == one_use
    assert result.candidate.verified_doi == DOI
    assert result.fetch is not None
    assert result.fetch.body.startswith(b"%PDF-")
    assert http.post_calls[0][1] == {
        "nodeId": "NODE12345678",
        "depth": "Article",
        "shape": "download",
    }
    # One-use keys and JWTs are never copied into audit attempts.
    assert all(
        "secret-" not in attempt.url
        and "secret-" not in (attempt.final_url or "")
        for attempt in result.attempts
    )


def test_dbpia_official_numeric_article_path_uses_single_advertised_node() -> None:
    legacy_landing = (
        "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215?"
        "Page=1&Seq=37"
    )
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=ASCII_TITLE,
        pdf_title=ASCII_TITLE,
        filename=f"{ASCII_TITLE}.pdf",
        landing_url=legacy_landing,
        pdf_content=_text_pdf([f"{ASCII_TITLE}\nDOI: {DOI}"]),
    )

    result = _resolver(http).resolve(
        _paper(title=ASCII_TITLE, alternate_titles=()),
        landing_url=legacy_landing,
    )

    assert result.resolved
    assert result.landing_url == legacy_landing
    assert result.identity_status == "doi_match"
    assert "dbpia_legacy_article_path_exact" in result.identity_provenance
    assert (
        "dbpia_unique_download_button_node_exact"
        in result.identity_provenance
    )
    assert "dbpia_landing_node_exact" not in result.identity_provenance
    assert http.post_calls[0][1]["nodeId"] == "NODE12345678"


def test_dbpia_official_node_path_is_followed_without_url_rewrite() -> None:
    node_landing = (
        "https://www.dbpia.co.kr/Journal/ArticleDetail/NODE12345678?"
        "Page=1&Seq=37"
    )
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=TITLE,
        pdf_title=TITLE,
        filename="paper.pdf",
        landing_url=node_landing,
    )

    result = _resolver(http).resolve(_paper(), landing_url=node_landing)

    assert result.resolved
    assert result.landing_url == node_landing
    assert http.get_calls[0][0] == node_landing
    assert http.post_calls[0][1]["nodeId"] == "NODE12345678"


def test_dbpia_numeric_article_path_rejects_unknown_context_before_auth() -> None:
    landing_url = (
        "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215?"
        "redirect=https%3A%2F%2Fevil.example"
    )
    http = FakeHttp(
        gets=[Reply(_landing_html(""), final_url=landing_url)],
    )

    result = _resolver(http).resolve(_paper(), landing_url=landing_url)

    assert result.status == "unsupported"
    assert result.reason == "unsupported_platform_landing"
    assert not http.post_calls


def test_dbpia_numeric_article_path_rejects_multiple_advertised_nodes() -> None:
    legacy_landing = (
        "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215"
    )
    landing = _landing_html(
        "<button onclick=\"onClickDownloadNode('NODE12345678')\"></button>"
        "<button onclick=\"onClickDownloadNode('NODE12345679')\"></button>"
    )
    http = FakeHttp(gets=[Reply(landing, final_url=legacy_landing)])

    result = _resolver(http).resolve(
        _paper(alternate_titles=()),
        landing_url=legacy_landing,
    )

    assert result.status == "identity_mismatch"
    assert result.reason == "dbpia_legacy_download_node_not_unique"
    assert not http.post_calls


def test_dbpia_numeric_article_path_allows_repeated_same_node_controls() -> None:
    legacy_landing = (
        "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215"
    )
    one_use = "https://download.dbpia.co.kr/File/Download?key=one-use"
    cdn_url = "https://downloadcf.dbpia.co.kr/1/23/45/1234567.pdf"
    repeated_button = (
        "<button onclick=\"onClickDownloadNode('NODE12345678')\"></button>"
    )
    landing = _landing_html(repeated_button + repeated_button)
    auth = json.dumps(
        {
            "nodeId": "NODE12345678",
            "showAlert_YN": "N",
            "resultCode": "",
            "link": one_use,
        }
    ).encode()
    http = FakeHttp(
        gets=[
            Reply(landing, final_url=legacy_landing),
            Reply(
                _pdf(),
                requested_url=one_use,
                final_url=cdn_url,
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                auth,
                final_url="https://www.dbpia.co.kr/download/downloadData",
                content_type="application/json",
            )
        ],
    )

    result = _resolver(http).resolve(_paper(), landing_url=legacy_landing)

    assert result.resolved
    assert http.post_calls[0][1]["nodeId"] == "NODE12345678"


@pytest.mark.parametrize(
    ("viewer_path", "viewer_query"),
    [
        ("/pdf/pdfView.do", "nodeId=NODE11750870&width=1920"),
        ("/pdf/pdfAiChatView.do", "width=1920&nodeId=NODE11750870"),
    ],
)
@pytest.mark.parametrize(
    ("paper_journal", "landing_journal"),
    [
        ("Digital Forensics Research", "Digital Forensics Research"),
        ("SER000003109", "디지털포렌식연구"),
    ],
)
def test_dbpia_viewer_node_binds_blank_landing_doi_by_bibliography(
    viewer_path: str,
    viewer_query: str,
    paper_journal: str,
    landing_journal: str,
) -> None:
    viewer_url = f"https://www.dbpia.co.kr{viewer_path}?{viewer_query}"
    landing_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE11750870"
    )
    one_use = (
        "https://download.dbpia.co.kr/File/Download?"
        "key=secret-one-use-value"
    )
    cdn_url = (
        "https://downloadcf.dbpia.co.kr/1/23/45/7813730.pdf?"
        "jwt=secret-jwt"
    )
    paper = _paper(
        title="Android Wallet ArtifactAnalysis Research",
        alternate_titles=(),
        year=2024,
        authors=("Alice Example (University)", "Bob Example (Lab)"),
        journal_id=paper_journal,
        institution_id="Korea Digital Forensics Society",
        issue_id="18_1",
        first_page="11",
        final_page="23",
    )
    landing = _landing_html(
        "<button onclick=\"onClickDownloadNode("
        "'NODE11750870','downloadNodeButton_detail',false)\">"
        "다운로드</button>",
        title="Android Wallet Artifact Analysis Research",
        doi=None,
        publication_date="2024/03",
        extra_meta=_dbpia_bibliographic_meta(journal=landing_journal),
    )
    auth = json.dumps(
        {
            "nodeId": "NODE11750870",
            "showAlert_YN": "N",
            "resultCode": "",
            "link": one_use,
        }
    ).encode()
    http = FakeHttp(
        gets=[
            Reply(landing, final_url=landing_url),
            Reply(
                _text_pdf(
                    [
                        "Android Wallet Artifact Analysis Research\n"
                        f"DOI: {DOI}"
                    ]
                ),
                requested_url=one_use,
                final_url=cdn_url,
                redirects=(cdn_url,),
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                auth,
                final_url="https://www.dbpia.co.kr/download/downloadData",
                content_type="application/json",
            )
        ],
    )

    result = _resolver(http).resolve(paper, landing_url=viewer_url)

    assert result.resolved
    assert result.identity_status == "doi_match"
    assert result.page_count == 1
    assert result.matched_doi == DOI
    assert "dbpia_landing_publisher_exact" in result.identity_provenance
    assert "dbpia_landing_journal_exact" in result.identity_provenance
    assert "dbpia_landing_volume_issue_exact" in result.identity_provenance
    assert "dbpia_landing_page_range_exact" in result.identity_provenance
    assert "dbpia_landing_authors_exact" in result.identity_provenance
    assert "dbpia_viewer_node_exact" in result.identity_provenance
    assert (
        "dbpia_viewer_canonical_article_landing"
        in result.identity_provenance
    )
    assert result.landing_url == landing_url
    assert result.reason == "authorized_official_ui_and_pdf_identity_match"
    assert http.get_calls[0][0] == landing_url


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_reason"),
    [
        ("year", "2023/03", "platform_landing_year_mismatch"),
        (
            "publisher",
            "Different Publisher",
            "dbpia_landing_bibliography_mismatch",
        ),
        (
            "journal",
            "Different Journal",
            "dbpia_landing_bibliography_mismatch",
        ),
        ("volume", "17", "dbpia_landing_bibliography_mismatch"),
        ("volume", "unknown", "dbpia_landing_bibliography_mismatch"),
        ("issue", "2", "dbpia_landing_bibliography_mismatch"),
        ("first_page", "12", "dbpia_landing_bibliography_mismatch"),
        ("final_page", "24", "dbpia_landing_bibliography_mismatch"),
        (
            "authors",
            ("Alice Example", "Mallory Example"),
            "dbpia_landing_bibliography_mismatch",
        ),
    ],
)
def test_dbpia_blank_landing_doi_rejects_each_bibliographic_mismatch_before_auth(
    field: str,
    bad_value: str | tuple[str, ...],
    expected_reason: str,
) -> None:
    landing_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE11750870"
    )
    paper = _paper(
        title="Android Wallet ArtifactAnalysis Research",
        alternate_titles=(),
        year=2024,
        authors=("Alice Example", "Bob Example"),
        journal_id="Digital Forensics Research",
        institution_id="Korea Digital Forensics Society",
        issue_id="18_1",
        first_page="11",
        final_page="23",
    )
    meta_values: dict[str, str | tuple[str, ...]] = {
        "publisher": "Korea Digital Forensics Society",
        "journal": "Digital Forensics Research",
        "volume": "18",
        "issue": "1",
        "first_page": "11",
        "final_page": "23",
        "authors": ("Alice Example", "Bob Example"),
    }
    publication_date = "2024/03"
    if field == "year":
        assert isinstance(bad_value, str)
        publication_date = bad_value
    else:
        meta_values[field] = bad_value
    landing = _landing_html(
        "<button onclick=\"onClickDownloadNode("
        "'NODE11750870','downloadNodeButton_detail',false)\"></button>",
        title="Android Wallet Artifact Analysis Research",
        doi=None,
        publication_date=publication_date,
        extra_meta=_dbpia_bibliographic_meta(
            publisher=str(meta_values["publisher"]),
            journal=str(meta_values["journal"]),
            volume=str(meta_values["volume"]),
            issue=str(meta_values["issue"]),
            first_page=str(meta_values["first_page"]),
            final_page=str(meta_values["final_page"]),
            authors=(
                meta_values["authors"]
                if isinstance(meta_values["authors"], tuple)
                else (str(meta_values["authors"]),)
            ),
        ),
    )
    http = FakeHttp(gets=[Reply(landing, final_url=landing_url)])

    result = _resolver(http).resolve(paper, landing_url=landing_url)

    assert result.status == "identity_mismatch"
    assert result.reason == expected_reason
    assert not http.post_calls


@pytest.mark.parametrize(
    "viewer_url",
    [
        "http://www.dbpia.co.kr/pdf/pdfView.do?nodeId=NODE11750870",
        "https://evil.example/pdf/pdfView.do?nodeId=NODE11750870",
        "https://user@www.dbpia.co.kr/pdf/pdfView.do?nodeId=NODE11750870",
        "https://www.dbpia.co.kr:444/pdf/pdfView.do?nodeId=NODE11750870",
        "https://www.dbpia.co.kr/pdf/pdfView.do?nodeId=NODE11750870#page=1",
        "https://www.dbpia.co.kr/pdf/pdfView.do?nodeId=node11750870",
        (
            "https://www.dbpia.co.kr/pdf/pdfView.do?"
            "nodeId=NODE11750870&nodeId=NODE11750871"
        ),
        (
            "https://www.dbpia.co.kr/pdf/pdfView.do?"
            "nodeId=NODE11750870&width=1920&extra=1"
        ),
        (
            "https://www.dbpia.co.kr/pdf/pdfAiChatView.do?"
            "nodeId=NODE11750870&width=full"
        ),
    ],
)
def test_dbpia_viewer_url_validation_fails_closed_without_network(
    viewer_url: str,
) -> None:
    http = FakeHttp(gets=[])

    result = _resolver(http).resolve(_paper(), landing_url=viewer_url)

    assert result.status == "invalid_response"
    assert result.reason == "invalid_dbpia_viewer_url"
    assert result.error_code == "unsafe_url"
    assert not http.get_calls
    assert not http.post_calls


def test_dbpia_rejects_landing_redirect_through_unapproved_host() -> None:
    landing_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE12345678"
    )
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(""),
                final_url=landing_url,
                redirects=(
                    "https://tracking.example/article",
                    landing_url,
                ),
            ),
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "platform_landing_redirect_outside_allowlist"
    assert not http.post_calls


def test_dbpia_rejects_wrong_doi_before_authorization() -> None:
    landing_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE12345678"
    )
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    "<button onclick=\"onClickDownloadNode("
                    "'NODE12345678','x',false)\"></button>",
                    doi="10.9999/different",
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "platform_landing_doi_mismatch"
    assert not http.post_calls


def test_dbpia_rejects_authorization_link_outside_allowlist() -> None:
    landing_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE12345678"
    )
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    "<button onclick=\"onClickDownloadNode("
                    "'NODE12345678','x',false)\"></button>"
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "nodeId": "NODE12345678",
                        "showAlert_YN": "N",
                        "resultCode": "",
                        "link": "https://evil.example/paper.pdf",
                    }
                ).encode(),
                content_type="application/json",
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "unsafe_dbpia_download_url"
    assert len(http.get_calls) == 1


def test_earticle_success_requires_exact_hidden_id_and_success_decision() -> None:
    landing_url = "https://www.earticle.net/Article/A461112"
    auth_token = "opaque-authorized-token"
    pdf_url = (
        "https://file1.earticle.net/PDF/Download?"
        "key=opaque-authorized-token"
    )
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    """
                    <input id="d_artType" value="A">
                    <input id="d_id" value="461112">
                    <button class="btn btn-download">다운로드</button>
                    """,
                    doi=None,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                _pdf(),
                final_url=pdf_url,
                requested_url=pdf_url,
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "auth": auth_token,
                        "count": 1,
                        "result": [
                            {"art": "A461112", "result": "SUCCESS"}
                        ],
                    }
                ).encode(),
                content_type="text/html; charset=utf-8",
            ),
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.resolved
    assert result.platform == "earticle"
    assert result.candidate is not None
    assert result.candidate.url == pdf_url
    assert result.candidate.verified_doi is None
    assert http.post_calls[0][1] == {"keys": "A461112"}


def test_earticle_doi_less_exact_title_discovery_uses_existing_pdf_flow() -> None:
    search_url = (
        "https://www.earticle.net/Search/Result?"
        + urlencode({"sf": "2", "q": TITLE})
    )
    landing_url = "https://www.earticle.net/Article/A461112"
    pdf_url = "https://file1.earticle.net/PDF/Download?key=valid-token"
    http = FakeHttp(
        gets=[
            Reply(
                _earticle_search_html(
                    (
                        "/Article/A461112",
                        "디지털 <strong>포렌식</strong> 증거수집 기법 연구",
                        2025,
                        ("홍길동",),
                    )
                ),
                final_url=search_url,
            ),
            Reply(
                _landing_html(
                    """
                    <input id="d_artType" value="A">
                    <input id="d_id" value="461112">
                    <button class="btn-download">다운로드</button>
                    """,
                    doi=None,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                _pdf(),
                final_url=pdf_url,
                requested_url=pdf_url,
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "auth": "valid-token",
                        "count": 1,
                        "result": [
                            {"art": "A461112", "result": "SUCCESS"}
                        ],
                    }
                ).encode(),
                content_type="application/json",
            )
        ],
    )

    result = _resolver(http).resolve(
        _paper(doi=None, authors=("홍길동(연구소)",)),
    )

    assert result.resolved
    assert result.platform == "earticle"
    assert result.reason == (
        "authorized_earticle_exact_title_discovery_and_pdf_identity_match"
    )
    assert result.identity_status == "metadata_title_match"
    assert result.landing_url == landing_url
    assert {
        "earticle_search_request_exact",
        "earticle_search_title_normalized_exact",
        "earticle_search_year_exact",
        "earticle_search_authors_exact",
        "earticle_search_single_article_exact",
    }.issubset(result.identity_provenance)
    assert http.get_calls[0][0] == search_url
    assert len(http.get_calls) == 3
    assert http.post_calls[0][1] == {"keys": "A461112"}


def test_earticle_discovery_rejects_ambiguous_exact_results() -> None:
    search_url = (
        "https://www.earticle.net/Search/Result?"
        + urlencode({"sf": "2", "q": TITLE})
    )
    http = FakeHttp(
        gets=[
            Reply(
                _earticle_search_html(
                    ("/Article/A111111", TITLE, 2025, ("홍길동",)),
                    ("/Article/A222222", TITLE, 2025, ("홍길동",)),
                ),
                final_url=search_url,
            )
        ],
    )

    result = _resolver(http).resolve(
        _paper(doi=None, authors=("홍길동(연구소)",)),
    )

    assert result.status == "ambiguous"
    assert result.reason == "ambiguous_earticle_exact_title_results"
    assert len(http.get_calls) == 1
    assert not http.post_calls


def test_earticle_discovery_rejects_year_mismatch() -> None:
    search_url = (
        "https://www.earticle.net/Search/Result?"
        + urlencode({"sf": "2", "q": TITLE})
    )
    http = FakeHttp(
        gets=[
            Reply(
                _earticle_search_html(
                    ("/Article/A461112", TITLE, 2024, ("홍길동",)),
                ),
                final_url=search_url,
            )
        ],
    )

    result = _resolver(http).resolve(_paper(doi=None))

    assert result.status == "identity_mismatch"
    assert result.reason == "earticle_search_year_mismatch"
    assert len(http.get_calls) == 1


def test_earticle_discovery_rejects_known_author_mismatch() -> None:
    search_url = (
        "https://www.earticle.net/Search/Result?"
        + urlencode({"sf": "2", "q": TITLE})
    )
    http = FakeHttp(
        gets=[
            Reply(
                _earticle_search_html(
                    ("/Article/A461112", TITLE, 2025, ("다른저자",)),
                ),
                final_url=search_url,
            )
        ],
    )

    result = _resolver(http).resolve(
        _paper(doi=None, authors=("홍길동(연구소)",)),
    )

    assert result.status == "identity_mismatch"
    assert result.reason == "earticle_search_author_mismatch"
    assert len(http.get_calls) == 1


def test_earticle_discovery_rejects_redirect_and_login_pages() -> None:
    search_url = (
        "https://www.earticle.net/Search/Result?"
        + urlencode({"sf": "2", "q": TITLE})
    )
    login_url = "https://www.earticle.net/Account/PersonLogin"
    redirected_http = FakeHttp(
        gets=[
            Reply(
                "<!doctype html><title>로그인</title>".encode(),
                final_url=login_url,
                redirects=(login_url,),
            )
        ],
    )
    login_http = FakeHttp(
        gets=[
            Reply(
                (
                    "<!doctype html><html><head><title>로그인</title>"
                    "</head><body></body></html>"
                ).encode(),
                final_url=search_url,
            )
        ],
    )

    redirected = _resolver(redirected_http).resolve(_paper(doi=None))
    login = _resolver(login_http).resolve(_paper(doi=None))

    assert redirected.status == "invalid_response"
    assert redirected.reason == "earticle_search_unexpected_redirect"
    assert login.status == "access_denied"
    assert login.reason == "earticle_search_returned_login_page"


@pytest.mark.parametrize(
    "title",
    [
        f"{TITLE}\n추가",
        "가" * 301,
    ],
)
def test_earticle_discovery_rejects_unsafe_search_titles_without_network(
    title: str,
) -> None:
    http = FakeHttp(gets=[])

    result = _resolver(http).resolve(
        _paper(doi=None, title=title, alternate_titles=()),
    )

    assert result.status == "missing_metadata"
    assert result.reason == "invalid_earticle_search_title"
    assert not http.get_calls


def test_earticle_denies_purchase_required_result() -> None:
    landing_url = "https://www.earticle.net/Article/A461112"
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    """
                    <input id="d_artType" value="A">
                    <input id="d_id" value="461112">
                    <button class="btn-download">다운로드</button>
                    """,
                    doi=None,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            )
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "auth": "",
                        "count": 0,
                        "result": [
                            {"art": "A461112", "result": "ErrBuy"}
                        ],
                    }
                ).encode()
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "access_denied"
    assert result.reason == "earticle_authorization_errbuy"
    assert len(http.get_calls) == 1


def test_kiss_official_ajax_flow_and_sensitive_query_redaction() -> None:
    landing_url = "https://kiss.kstudy.com/Detail/Ar?key=landing-secret"
    pdf_url = (
        "https://kisspdf.kstudy.com/File/Down?"
        "keyn=7r000306&code=4652011827580063102"
    )
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    "<button onclick=\"pdfDown('4138758');\">다운로드</button>",
                    doi=None,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                _pdf(),
                final_url=pdf_url,
                requested_url=pdf_url,
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "resultCode": "Success",
                        "code": "4652011827580063102",
                        "down": "7r000306",
                    }
                ).encode(),
                content_type="application/json",
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.resolved
    assert result.platform == "kiss"
    post_url, post_data, _limit, post_headers = http.post_calls[0]
    assert post_url.endswith("/Down/Download?keyn=4138758")
    assert post_data == {}
    assert post_headers is not None
    assert post_headers["X-Requested-With"] == "XMLHttpRequest"
    assert post_headers["Content-Type"].startswith("application/json")
    assert post_headers["Referer"] == landing_url
    assert all(
        "landing-secret" not in attempt.url
        and "landing-secret" not in (attempt.final_url or "")
        for attempt in result.attempts
    )


def test_kiss_rejects_multiple_pdf_identifiers() -> None:
    landing_url = "https://kiss.kstudy.com/Detail/Ar?key=landing-secret"
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    """
                    <button onclick="pdfDown('111');"></button>
                    <button onclick="pdfDown('222');"></button>
                    """,
                    doi=None,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "multiple_kiss_pdf_identifiers"
    assert not http.post_calls


def test_kiss_unprinted_doi_accepts_complete_bibliographic_evidence() -> None:
    first_page = (
        "Electronic Information Seizure\n"
        "1)Legality Requirements\n"
        "Law Journal Vol.57 (2025) pp.217~236"
    )
    http = _kiss_http(
        landing_title=KISS_TITLE,
        pdf_content=_text_pdf([first_page, *(["body"] * 19)]),
    )
    paper = _paper(
        title=KISS_TITLE,
        alternate_titles=(),
        journal_id="Law Journal",
        issue_id="57",
        first_page="217",
        final_page="236",
    )

    result = _resolver(http).resolve(paper)

    assert result.resolved
    assert result.reason == "authorized_official_ui_resolver_verified_doi"
    assert result.identity_status == "resolver_verified_doi"
    assert result.resolver_verified_doi
    assert result.matched_doi == DOI
    assert KISS_RESOLVER_VERIFIED_PROVENANCE.issubset(
        result.identity_provenance
    )


def test_kiss_bibliographic_fallback_rejects_conflicting_doi() -> None:
    first_page = (
        "Electronic Information Seizure\n"
        "1)Legality Requirements\n"
        "Law Journal Vol.57 (2025) pp.217~236\n"
        "DOI: 10.9999/conflicting.1"
    )
    http = _kiss_http(
        landing_title=KISS_TITLE,
        pdf_content=_text_pdf([first_page, *(["body"] * 19)]),
    )

    result = _resolver(http).resolve(
        _paper(
            title=KISS_TITLE,
            alternate_titles=(),
            journal_id="Law Journal",
            issue_id="57",
            first_page="217",
            final_page="236",
        )
    )

    assert result.status == "invalid_pdf"
    assert result.reason == "platform PDF contains a different DOI"


def test_kiss_bibliographic_fallback_requires_exact_page_range() -> None:
    first_page = (
        "Electronic Information Seizure\n"
        "1)Legality Requirements\n"
        "Law Journal Vol.57 (2025) pp.217~235"
    )
    http = _kiss_http(
        landing_title=KISS_TITLE,
        pdf_content=_text_pdf([first_page, *(["body"] * 19)]),
    )

    result = _resolver(http).resolve(
        _paper(
            title=KISS_TITLE,
            alternate_titles=(),
            journal_id="Law Journal",
            issue_id="57",
            first_page="217",
            final_page="236",
        )
    )

    assert result.status == "invalid_pdf"
    assert result.reason == (
        "platform PDF DOI/title identity could not be confirmed"
    )


def test_kiss_korean_first_page_footnote_title_bibliography() -> None:
    korean_title = (
        "임의제출에 의한 전자정보 압수의 적법성 요건으로서의 "
        "참여권 보장 - 대법원 2022. 1. 13. 선고 2016도9596 판결 -"
    )
    paper = _paper(
        title=korean_title,
        alternate_titles=(),
        journal_id="영남법학",
        issue_id="57",
        first_page="217",
        final_page="236",
    )
    first_page = (
        "임의제출에의한 전자정보압수의\n"
        "1)적법성 요건으로서의참여권 보장*\n"
        "- 대법원 2022. 1. 13. 선고 2016도9596 판결 -\n"
        "영남대학교 법학연구소 ｢영남법학｣ 제57호 "
        "(2023.12.) 217~236면."
    )

    matched_title, provenance = _kiss_pdf_bibliographic_provenance(
        first_page,
        page_count=20,
        paper=paper,
        known_titles=((korean_title, korean_title),),
    )

    assert matched_title == korean_title
    assert {
        "kiss_pdf_title_exact",
        "kiss_pdf_journal_exact",
        "kiss_pdf_issue_exact",
        "kiss_pdf_page_range_exact",
        "kiss_pdf_page_count_exact",
    }.issubset(provenance)


def test_kyobo_fails_closed_when_page_only_has_javascript_pdf_button() -> None:
    landing_url = (
        "https://scholar.kyobobook.co.kr/article/detail/4010071506079"
    )
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    '<a href="javascript:void(0)" class="pdf">원문보기</a>'
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "access_denied"
    assert result.reason == "kyobo_requires_authenticated_file_authorization"
    assert not http.post_calls


def test_kyobo_accepts_only_one_literal_official_pdf_url() -> None:
    landing_url = (
        "https://scholar.kyobobook.co.kr/article/detail/4010071506079"
    )
    pdf_url = "https://contents.kyobobook.co.kr/articles/4010071506079.pdf"
    html = (
        "<!doctype html><html><head>"
        f'<meta name="citation_title" content="{TITLE}">'
        f'<meta name="citation_doi" content="{DOI}">'
        f'<meta name="citation_pdf_url" content="{pdf_url}">'
        "</head></html>"
    ).encode()
    http = FakeHttp(
        gets=[
            Reply(
                html,
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                _pdf(),
                final_url=pdf_url,
                requested_url=pdf_url,
                content_type="application/pdf",
            ),
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.resolved
    assert result.platform == "kyobo"
    assert result.candidate is not None
    assert result.candidate.url == pdf_url


def test_pdf_must_match_expected_title_or_doi() -> None:
    landing_url = "https://www.earticle.net/Article/A461112"
    pdf_url = "https://file1.earticle.net/PDF/Download?key=valid-token"
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    """
                    <input id="d_artType" value="A">
                    <input id="d_id" value="461112">
                    <button class="btn-download">다운로드</button>
                    """,
                    doi=None,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                _pdf(title="A completely unrelated paper"),
                final_url=pdf_url,
                requested_url=pdf_url,
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "auth": "valid-token",
                        "count": 1,
                        "result": [
                            {"art": "A461112", "result": "SUCCESS"}
                        ],
                    }
                ).encode()
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_pdf"
    assert result.reason == "platform PDF DOI/title identity could not be confirmed"


def test_html_download_response_is_never_accepted_as_pdf() -> None:
    landing_url = "https://www.earticle.net/Article/A461112"
    pdf_url = "https://file1.earticle.net/PDF/Download?key=valid-token"
    http = FakeHttp(
        gets=[
            Reply(
                _landing_html(
                    """
                    <input id="d_artType" value="A">
                    <input id="d_id" value="461112">
                    <button class="btn-download">다운로드</button>
                    """,
                    doi=None,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                b"<!doctype html><title>login</title>",
                final_url=pdf_url,
                requested_url=pdf_url,
                content_type="text/html",
            ),
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "auth": "valid-token",
                        "count": 1,
                        "result": [
                            {"art": "A461112", "result": "SUCCESS"}
                        ],
                    }
                ).encode()
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_pdf"
    assert result.reason == "platform response does not contain a PDF header"


@pytest.mark.parametrize(
    "text",
    [
        f"https://doi.org/{DOI}(사단법인) 발행",
        f"{DOI.upper()} 논문",
        f"{DOI}4차 산업혁명",
    ],
)
def test_expected_doi_match_precedes_greedy_extraction(text: str) -> None:
    assert _contains_expected_doi(text, DOI)
    assert _extract_dois(text, expected_doi=DOI) == {DOI}


@pytest.mark.parametrize(
    "text",
    [
        f"x{DOI}",
        f"{DOI}9",
        f"{DOI}suffix",
        f"{DOI}/continuation",
    ],
)
def test_expected_doi_rejects_ascii_continuations(text: str) -> None:
    assert not _contains_expected_doi(text, DOI)


def test_generic_doi_extraction_stops_at_korean_and_parenthesis() -> None:
    assert _extract_dois(f"{DOI}(사단법인)") == {DOI}
    assert _extract_dois(f"{DOI}논문제목") == {DOI}
    assert _extract_dois(f"{DOI}4차", expected_doi=DOI) == {DOI}


@pytest.mark.parametrize(
    ("expected_doi", "observed_doi", "year"),
    [
        (
            "10.30833/LTPR.2023.02.11.1.495",
            "10.30833/LTPR.2023.02.11.1. \n495",
            2023,
        ),
        (
            "10.29305/tj.2025.6.208.26",
            "10.29305/tj.2025.6.208 .26",
            2025,
        ),
    ],
)
def test_layout_spaced_expected_doi_requires_exact_pdf_title(
    expected_doi: str,
    observed_doi: str,
    year: int,
) -> None:
    content = _text_pdf([f"{ASCII_TITLE}\nDOI: {observed_doi}"])
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=ASCII_TITLE,
        pdf_title="",
        filename="unrelated.pdf",
        publication_date=f"{year}/01",
        doi=expected_doi,
        pdf_content=content,
    )

    result = _resolver(http).resolve(
        _paper(
            doi=expected_doi,
            title=ASCII_TITLE,
            alternate_titles=(),
            year=year,
        )
    )

    assert _extract_dois(
        f"DOI: {observed_doi}",
        expected_doi=expected_doi,
    ) == {expected_doi}
    assert result.resolved
    assert result.identity_status == "resolver_verified_doi_layout"
    assert result.resolver_verified_doi_layout
    assert result.matched_doi == expected_doi
    assert DBPIA_LAYOUT_VERIFIED_PROVENANCE.issubset(
        result.identity_provenance
    )


def test_layout_spaced_doi_rejects_a_conflicting_valid_doi() -> None:
    expected_doi = "10.29305/tj.2025.6.208.26"
    content = _text_pdf(
        [
            f"{ASCII_TITLE}\n"
            "DOI: 10.29305/tj.2025.6.208 .26\n"
            "DOI: 10.9999/conflicting.1"
        ]
    )
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=ASCII_TITLE,
        pdf_title="",
        filename="unrelated.pdf",
        doi=expected_doi,
        pdf_content=content,
    )

    result = _resolver(http).resolve(
        _paper(
            doi=expected_doi,
            title=ASCII_TITLE,
            alternate_titles=(),
        )
    )

    assert result.status == "invalid_pdf"
    assert result.reason == "platform PDF contains a different DOI"


def test_layout_spaced_doi_without_exact_title_is_not_sufficient() -> None:
    expected_doi = "10.29305/tj.2025.6.208.26"
    content = _text_pdf(
        ["Unrelated Article\nDOI: 10.29305/tj.2025.6.208 .26"]
    )
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=ASCII_TITLE,
        pdf_title="",
        filename="unrelated.pdf",
        doi=expected_doi,
        pdf_content=content,
    )

    result = _resolver(http).resolve(
        _paper(
            doi=expected_doi,
            title=ASCII_TITLE,
            alternate_titles=(),
        )
    )

    assert result.status == "invalid_pdf"
    assert result.reason == (
        "platform PDF DOI/title identity could not be confirmed"
    )


def test_compact_landing_title_requires_exact_doi_and_year() -> None:
    landing_title = "디지털 포렌식 증거수집 기법: 연구"
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=landing_title,
        pdf_title=TITLE,
        filename=f"{TITLE}.pdf",
    )

    result = _resolver(http).resolve(_paper())

    assert result.resolved
    assert result.identity_status == "metadata_title_match"
    assert "landing_title_compact_exact" in result.identity_provenance
    assert "landing_title_normalized_exact" not in result.identity_provenance
    assert "landing_doi_exact" in result.identity_provenance
    assert "landing_year_exact" in result.identity_provenance


def test_compact_landing_title_without_exact_year_fails_before_auth() -> None:
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title="디지털 포렌식 증거수집 기법: 연구",
        pdf_title=TITLE,
        filename=f"{TITLE}.pdf",
        publication_date=None,
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "platform_landing_title_mismatch"
    assert not http.post_calls
    assert len(http.get_calls) == 1


def test_dbpia_unreadable_pdf_accepts_complete_official_evidence_chain() -> None:
    filename_prefix = (
        "대리전문가 증언의 허용성과 대면권의 한계  "
        "미연방대법원 Smith v. Arizona 판결"
    )
    http, one_use, _cdn_url = _dbpia_http(
        landing_title=LONG_TITLE,
        pdf_title="DBPIA-NURIMEDIA",
        filename=filename_prefix,
    )

    result = _resolver(http).resolve(
        _paper(title=LONG_TITLE, alternate_titles=()),
    )

    assert result.resolved
    assert result.reason == "authorized_official_ui_resolver_verified_doi"
    assert result.identity_status == "resolver_verified_doi"
    assert result.resolver_verified_doi
    assert result.matched_doi == DOI
    assert result.candidate is not None
    assert result.candidate.url == one_use
    assert result.candidate.verified_doi == DOI
    assert DBPIA_RESOLVER_VERIFIED_PROVENANCE.issubset(
        result.identity_provenance
    )


def test_dbpia_unreadable_pdf_rejects_unrelated_filename() -> None:
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=LONG_TITLE,
        pdf_title="DBPIA-NURIMEDIA",
        filename="completely unrelated document.pdf",
    )

    result = _resolver(http).resolve(
        _paper(title=LONG_TITLE, alternate_titles=()),
    )

    assert result.status == "invalid_pdf"
    assert result.reason == (
        "platform PDF DOI/title identity could not be confirmed"
    )
    assert not result.resolver_verified_doi


def test_dbpia_unreadable_pdf_requires_same_authorized_node() -> None:
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=LONG_TITLE,
        pdf_title="DBPIA-NURIMEDIA",
        filename=LONG_TITLE,
        auth_node="NODE87654321",
    )

    result = _resolver(http).resolve(
        _paper(title=LONG_TITLE, alternate_titles=()),
    )

    assert result.status == "identity_mismatch"
    assert result.reason == "dbpia_authorization_node_mismatch"
    assert len(http.get_calls) == 1


def test_dbpia_unreadable_pdf_requires_exact_landing_year() -> None:
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=LONG_TITLE,
        pdf_title="DBPIA-NURIMEDIA",
        filename=LONG_TITLE,
        publication_date=None,
    )

    result = _resolver(http).resolve(
        _paper(title=LONG_TITLE, alternate_titles=()),
    )

    assert result.status == "invalid_pdf"
    assert result.reason == (
        "platform PDF DOI/title identity could not be confirmed"
    )
    assert not result.resolver_verified_doi


def test_downloader_persists_and_reuses_dbpia_resolver_evidence(
    tmp_path: Path,
) -> None:
    filename_prefix = (
        "대리전문가 증언의 허용성과 대면권의 한계  "
        "미연방대법원 Smith v. Arizona 판결"
    )
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=LONG_TITLE,
        pdf_title="DBPIA-NURIMEDIA",
        filename=filename_prefix,
    )
    paper = _paper(title=LONG_TITLE, alternate_titles=())
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(enable_platform_download=True),
        http=http,
    )

    manifest = downloader.download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"].endswith(":dbpia")
    assert manifest["resolver"]["landing_url"] is None
    assert manifest["resolver"]["download_url"] is None
    assert manifest["pdf"]["identity_status"] == "resolver_doi_match"
    assert manifest["pdf"]["matched_doi"] == DOI
    summary = next(
        attempt
        for attempt in manifest["attempts"]
        if attempt.get("resolver") == "authorized-platform-public-ui"
        and attempt.get("resolution_status") == "resolved"
    )
    assert summary["identity_status"] == "resolver_verified_doi"
    assert DBPIA_RESOLVER_VERIFIED_PROVENANCE.issubset(
        summary["identity_provenance"]
    )
    assert download_artifact_is_current(paper, paths, manifest)

    reused = downloader.download(paper, paths)

    assert reused["status"] == "downloaded"
    assert reused["_runtime"]["action"] == "reused"


@pytest.mark.parametrize(
    "api_landing",
    [
        (
            "https://www.dbpia.co.kr/journal/articleDetail?"
            "nodeId=NODE12345678"
        ),
        "https://www.dbpia.co.kr/Journal/ArticleDetail/3209215",
    ],
)
def test_dbpia_openapi_landing_is_consumed_without_manifest_id_persistence(
    tmp_path: Path,
    api_landing: str,
) -> None:
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=LONG_TITLE,
        pdf_title=LONG_TITLE,
        filename=f"{LONG_TITLE}.pdf",
        landing_url=api_landing,
    )
    paper = _paper(title=LONG_TITLE, alternate_titles=())
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(
            enable_platform_download=True,
            platform_landing_urls={
                paper.source_id: api_landing
            },
            ephemeral_platform_landing_source_ids=frozenset(
                {paper.source_id}
            ),
        ),
        http=http,
    )

    manifest = downloader.download(paper, paths)
    serialized = json.dumps(manifest, ensure_ascii=False)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["landing_url"] is None
    assert any(
        attempt.get("landing_source") == "dbpia_openapi_memory"
        for attempt in manifest["attempts"]
    )
    assert "NODE12345678" not in serialized
    assert "3209215" not in serialized
    assert "nodeId=" not in serialized
    assert "articledetail" not in serialized.casefold()
    assert download_artifact_is_current(paper, paths, manifest)


def test_reused_marked_legacy_manifest_is_scrubbed_during_upgrade(
    tmp_path: Path,
) -> None:
    api_landing = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE12345678"
    )
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=LONG_TITLE,
        pdf_title=LONG_TITLE,
        filename=f"{LONG_TITLE}.pdf",
    )
    paper = _paper(title=LONG_TITLE, alternate_titles=())
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(
            enable_platform_download=True,
            platform_landing_urls={paper.source_id: api_landing},
            ephemeral_platform_landing_source_ids=frozenset(
                {paper.source_id}
            ),
        ),
        http=http,
    )
    manifest = downloader.download(paper, paths)
    for attempt in manifest["attempts"]:
        attempt.pop("landing_source", None)
    manifest["resolver"]["landing_url"] = api_landing
    manifest["resolver"]["download_url"] = (
        "https://downloadcf.dbpia.co.kr/1/23/45/1234567.pdf?"
        "jwt=secret-signed-token"
    )
    manifest["attempts"].append(
        {
            "resolver": "authorized-platform-public-ui:platform-landing",
            "status": "fetched",
            "url": api_landing,
            "final_url": api_landing,
            "message": "legacy NODE12345678",
        }
    )
    write_json_atomic(paths.download_manifest, manifest)

    reused = downloader.download(paper, paths)
    persisted = paths.download_manifest.read_text(encoding="utf-8")

    assert reused["_runtime"]["action"] == "reused"
    assert "NODE12345678" not in persisted
    assert "nodeId=" not in persisted
    assert "articleDetail" not in persisted
    assert "downloadcf.dbpia.co.kr" not in persisted
    assert "secret-signed-token" not in persisted
    assert reused["resolver"]["landing_url"] is None
    assert reused["resolver"]["download_url"] is None


def test_failed_dbpia_openapi_landing_does_not_persist_node(
    tmp_path: Path,
) -> None:
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=LONG_TITLE,
        pdf_title=LONG_TITLE,
        filename=f"{LONG_TITLE}.pdf",
        doi="",
        auth_node="NODE12345679",
    )
    paper = _paper(doi=None, title=LONG_TITLE, alternate_titles=())
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(
            enable_platform_download=True,
            platform_landing_urls={
                paper.source_id: (
                    "https://www.dbpia.co.kr/journal/articleDetail?"
                    "nodeId=NODE12345678"
                )
            },
            ephemeral_platform_landing_source_ids=frozenset(
                {paper.source_id}
            ),
        ),
        http=http,
    )

    manifest = downloader.download(paper, paths)
    serialized = json.dumps(manifest, ensure_ascii=False)

    assert manifest["status"] != "downloaded"
    assert "NODE12345678" not in serialized
    assert "NODE12345679" not in serialized
    assert "nodeId=" not in serialized
    assert "articleDetail" not in serialized


def test_explicit_dbpia_initial_http_failure_does_not_persist_node(
    tmp_path: Path,
) -> None:
    landing_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE12345678"
    )
    paper = _paper(doi=None, title=LONG_TITLE, alternate_titles=())
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    http = FakeHttp(
        gets=[
            Reply(
                b"temporarily unavailable",
                status=503,
                final_url=landing_url,
            )
        ]
    )
    downloader = PdfDownloader(
        DownloadConfig(
            enable_platform_download=True,
            platform_landing_urls={paper.source_id: landing_url},
        ),
        http=http,
    )

    manifest = downloader.download(paper, paths)
    serialized = json.dumps(manifest, ensure_ascii=False)

    assert manifest["status"] != "downloaded"
    assert http.get_calls[0][0] == landing_url
    assert "NODE12345678" not in serialized
    assert "nodeId=" not in serialized
    assert "articleDetail" not in serialized


def test_pre_marker_failed_dbpia_manifest_is_scrubbed_immediately(
    tmp_path: Path,
) -> None:
    landing_url = (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        "nodeId=NODE12345678"
    )
    paths = ArtifactPaths.for_source(tmp_path, "ART003071074")
    paths.root.mkdir(parents=True, exist_ok=True)
    legacy = {
        "schema_version": "1.1",
        "status": "retryable_error",
        "resolver": None,
        "attempts": [
            {
                "resolver": "authorized-platform-public-ui:platform-landing",
                "status": "failed",
                "url": landing_url,
                "final_url": landing_url,
                "message": "failed NODE12345678",
            }
        ],
        "updated_at": "2025-01-01T00:00:00Z",
    }
    write_json_atomic(paths.download_manifest, legacy)

    migrated = _migrate_dbpia_identifier_manifest(paths, legacy)
    serialized = paths.download_manifest.read_text(encoding="utf-8")

    assert "NODE12345678" not in serialized
    assert "nodeId=" not in serialized
    assert "articleDetail" not in serialized
    assert migrated["updated_at"] != "2025-01-01T00:00:00Z"


def test_manifest_sanitizer_does_not_create_lock_without_manifest(
    tmp_path: Path,
) -> None:
    paths = ArtifactPaths.for_source(
        tmp_path / "paper",
        "ART0001",
        manifest_root=tmp_path / "manifest",
    )

    assert sanitize_dbpia_download_manifest(paths) is False
    assert not paths.lock.exists()
    assert not paths.root.exists()
    assert not paths.state_root.exists()


def test_downloader_second_validation_prefers_platform_matched_title(
    tmp_path: Path,
) -> None:
    catalog_title = "Legacy catalog title for a different wording"
    published_title = "Authoritative Published Digital Forensics Title"
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=published_title,
        pdf_title=published_title,
        filename=f"{published_title}.pdf",
    )
    paper = _paper(
        title=catalog_title,
        alternate_titles=(published_title,),
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(enable_platform_download=True),
        http=http,
    )

    manifest = downloader.download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["pdf"]["identity_status"] == "metadata_title_match"
    summary = next(
        attempt
        for attempt in manifest["attempts"]
        if attempt.get("resolver") == "authorized-platform-public-ui"
        and attempt.get("status") == "downloaded"
    )
    assert summary["matched_title"] == published_title


def test_downloader_persists_dbpia_layout_doi_only_with_full_provenance(
    tmp_path: Path,
) -> None:
    expected_doi = "10.29305/tj.2025.6.208.26"
    content = _text_pdf(
        [f"{ASCII_TITLE}\nDOI: 10.29305/tj.2025.6.208 .26"]
    )
    http, _one_use, _cdn_url = _dbpia_http(
        landing_title=ASCII_TITLE,
        pdf_title="",
        filename="unrelated.pdf",
        doi=expected_doi,
        pdf_content=content,
    )
    paper = _paper(
        doi=expected_doi,
        title=ASCII_TITLE,
        alternate_titles=(),
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(enable_platform_download=True),
        http=http,
    )

    manifest = downloader.download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"].endswith(":dbpia")
    assert manifest["pdf"]["identity_status"] == (
        "resolver_doi_layout_title_match"
    )
    assert manifest["pdf"]["matched_doi"] == expected_doi
    summary = next(
        attempt
        for attempt in manifest["attempts"]
        if attempt.get("resolver") == "authorized-platform-public-ui"
        and attempt.get("resolution_status") == "resolved"
    )
    assert summary["identity_status"] == "resolver_verified_doi_layout"
    assert DBPIA_LAYOUT_VERIFIED_PROVENANCE.issubset(
        summary["identity_provenance"]
    )
    assert download_artifact_is_current(paper, paths, manifest)

    tampered = json.loads(json.dumps(manifest))
    tampered_summary = next(
        attempt
        for attempt in tampered["attempts"]
        if attempt.get("resolver") == "authorized-platform-public-ui"
        and attempt.get("resolution_status") == "resolved"
    )
    tampered_summary["identity_provenance"].remove("pdf_title_exact")
    assert not download_artifact_is_current(paper, paths, tampered)

    reused = downloader.download(paper, paths)

    assert reused["status"] == "downloaded"
    assert reused["_runtime"]["action"] == "reused"


def test_downloader_runs_earticle_discovery_before_doi_less_terminal(
    tmp_path: Path,
) -> None:
    search_url = (
        "https://www.earticle.net/Search/Result?"
        + urlencode({"sf": "2", "q": TITLE})
    )
    landing_url = "https://www.earticle.net/Article/A461112"
    pdf_url = "https://file1.earticle.net/PDF/Download?key=valid-token"
    http = FakeHttp(
        gets=[
            Reply(
                _earticle_search_html(
                    ("/Article/A461112", TITLE, 2025, ("홍길동",)),
                ),
                final_url=search_url,
            ),
            Reply(
                _landing_html(
                    """
                    <input id="d_artType" value="A">
                    <input id="d_id" value="461112">
                    <button class="btn-download">다운로드</button>
                    """,
                    doi=None,
                ),
                final_url=landing_url,
                redirects=(landing_url,),
            ),
            Reply(
                _pdf(),
                final_url=pdf_url,
                requested_url=pdf_url,
                content_type="application/pdf",
            ),
        ],
        posts=[
            Reply(
                json.dumps(
                    {
                        "auth": "valid-token",
                        "count": 1,
                        "result": [
                            {"art": "A461112", "result": "SUCCESS"}
                        ],
                    }
                ).encode(),
                content_type="application/json",
            )
        ],
    )
    paper = _paper(doi=None, authors=("홍길동(연구소)",))
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)

    manifest = PdfDownloader(
        DownloadConfig(enable_platform_download=True),
        http=http,
    ).download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"].endswith(":earticle")
    assert manifest["pdf"]["identity_status"] == "metadata_title_match"
    assert download_artifact_is_current(paper, paths, manifest)
    assert len(http.get_calls) == 3


def test_downloader_persists_and_reuses_kiss_resolver_evidence(
    tmp_path: Path,
) -> None:
    first_page = (
        "Electronic Information Seizure\n"
        "1)Legality Requirements\n"
        "Law Journal Vol.57 (2025) pp.217~236"
    )
    http = _kiss_http(
        landing_title=KISS_TITLE,
        pdf_content=_text_pdf([first_page, *(["body"] * 19)]),
    )
    paper = _paper(
        title=KISS_TITLE,
        alternate_titles=(),
        journal_id="Law Journal",
        issue_id="57",
        first_page="217",
        final_page="236",
    )
    paths = ArtifactPaths.for_source(tmp_path, paper.source_id)
    downloader = PdfDownloader(
        DownloadConfig(enable_platform_download=True),
        http=http,
    )

    manifest = downloader.download(paper, paths)

    assert manifest["status"] == "downloaded"
    assert manifest["resolver"]["name"].endswith(":kiss")
    assert manifest["pdf"]["identity_status"] == "resolver_doi_match"
    summary = next(
        attempt
        for attempt in manifest["attempts"]
        if attempt.get("resolver") == "authorized-platform-public-ui"
        and attempt.get("resolution_status") == "resolved"
    )
    assert KISS_RESOLVER_VERIFIED_PROVENANCE.issubset(
        summary["identity_provenance"]
    )
    assert download_artifact_is_current(paper, paths, manifest)

    reused = downloader.download(paper, paths)

    assert reused["status"] == "downloaded"
    assert reused["_runtime"]["action"] == "reused"
