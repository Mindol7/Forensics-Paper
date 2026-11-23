from __future__ import annotations

import io
import stat
import warnings
import zipfile
from collections import defaultdict, deque
from dataclasses import dataclass, replace

import pytest

import PaperPipeline.spo_resolver as spo
from PaperPipeline.http_client import HttpFetch, RedirectLimitError
from PaperPipeline.records import PaperCandidate
from PaperPipeline.spo_resolver import (
    SPO_AUDITED_RESOLVABLE_COUNT,
    SPO_AUDITED_SELECTED_COUNT,
    SPO_AUDITED_UNAVAILABLE_SOURCE_IDS,
    SPO_LIST_URL,
    SpoPublicPdfResolver,
    SpoResolverConfig,
)


DOI = "10.23026/crclps.2022..76.004"


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    requested_url: str | None = None
    final_url: str | None = None
    redirects: tuple[str, ...] = ()
    headers: dict[str, str] | None = None


class FakeHttp:
    def __init__(
        self,
        responses: dict[str, list[Reply | BaseException]] | None = None,
    ) -> None:
        self.responses = {
            url: deque(values)
            for url, values in (responses or {}).items()
        }
        self.calls: list[tuple[str, int, dict[str, str] | None]] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        self.calls.append((url, max_bytes, headers))
        if url not in self.responses or not self.responses[url]:
            raise AssertionError(f"unexpected HTTP call: {url}")
        value = self.responses[url].popleft()
        if isinstance(value, BaseException):
            raise value
        response_headers = {
            "Content-Type": value.content_type,
            **(value.headers or {}),
        }
        return HttpFetch(
            requested_url=value.requested_url or url,
            final_url=value.final_url or url,
            status_code=value.status,
            headers=response_headers,
            body=value.body,
            redirects=value.redirects,
        )


def _paper(
    *,
    source_id: str = "ART003000001",
    journal: str = "법과학의 신동향",
    issue: str = "6",
    year: int = 2022,
    doi: str | None = "10.23026/forensics.2022.6.001",
    title: str | None = "Exact Forensic Paper Title",
    authors: tuple[str, ...] = ("Alice Example(University)",),
    first_page: str | None = "117",
    final_page: str | None = "118",
    institution: str = "대검찰청",
) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id=source_id,
        title=title,
        doi_original=doi,
        doi=doi,
        landing_url=f"https://www.kci.go.kr/article/{source_id}",
        abstract=None,
        publication_year=year,
        first_page=first_page,
        final_page=final_page,
        journal_id=journal,
        institution_id=institution,
        issue_id=issue,
        authors=authors,
    )


def _view_url(bc_idx: str) -> str:
    return (
        "https://www.spo.go.kr/site/spo/ex/board/View.do?"
        f"bcIdx={bc_idx}&cbIdx=1303"
    )


def _attachment_url(bc_idx: str, token: str) -> str:
    return (
        "https://www.spo.go.kr/common/board/Download.do?"
        f"bcIdx={bc_idx}&cbIdx=1303&streFileNm={token}"
    )


def _list_page(*posts: tuple[str, str], page_hrefs: tuple[str, ...] = ()) -> bytes:
    values = [
        (
            f"""<a href="javascript:doBbsContentView('{bc_idx}');">
                  <span><b class="title">{title}</b></span>
                  <span class="desc">untrusted description 제99호 1999</span>
                </a>"""
        )
        for bc_idx, title in posts
    ]
    values.extend(f'<a href="{href}">page</a>' for href in page_hrefs)
    return ("<!doctype html><html><body>" + "".join(values) + "</body></html>").encode()


def _view_page(
    bc_idx: str,
    title: str,
    *,
    attachments: tuple[tuple[str, str], ...] = (),
    other_anchors: tuple[str, ...] = (),
) -> bytes:
    anchors = [
        (
            f'<a class="pdfFile" href="/common/board/Download.do?'
            f'bcIdx={bc_idx}&amp;cbIdx=1303&amp;streFileNm={token}">'
            f"<span>{label}</span></a>"
        )
        for token, label in attachments
    ]
    anchors.extend(other_anchors)
    return f"""
    <!doctype html><html><body>
      <h2 class="title">정보자료</h2>
      <H4 class="blind">{title}</H4>
      <span class="title">{title}</span>
      {''.join(anchors)}
    </body></html>
    """.encode()


def _text_pdf(page_texts: list[str]) -> bytes:
    """Small standards-compliant PDF using ASCII/Latin-1 test text."""

    def escaped(value: str) -> bytes:
        return (
            value.encode("latin-1")
            .replace(b"\\", b"\\\\")
            .replace(b"(", b"\\(")
            .replace(b")", b"\\)")
        )

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
            + escaped(text)
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
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
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
            f"trailer\n<< /Size {font_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(pdf)


def _zip(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return output.getvalue()


def _enabled(
    http: FakeHttp,
    **changes: object,
) -> SpoPublicPdfResolver:
    values: dict[str, object] = {"permission_granted": True}
    values.update(changes)
    return SpoPublicPdfResolver(
        SpoResolverConfig(**values),  # type: ignore[arg-type]
        http=http,
    )


def _success_http(
    *,
    doi: str = "10.23026/forensics.2022.6.001",
    token: str = "server-file.pdf",
    body: bytes | None = None,
) -> tuple[FakeHttp, str]:
    bc_idx = "1032969"
    title = "「법과학의 신동향」 통권 제6호(2022. 11.)"
    attachment = _attachment_url(bc_idx, token)
    return (
        FakeHttp(
            {
                SPO_LIST_URL: [Reply(_list_page((bc_idx, title)))],
                _view_url(bc_idx): [
                    Reply(
                        _view_page(
                            bc_idx,
                            title,
                            attachments=((token, "filename-only hint"),),
                        )
                    )
                ],
                attachment: [
                    Reply(
                        body or _text_pdf([f"DOI: {doi}"]),
                        content_type="application/x-msdownload",
                        headers={
                            "Set-Cookie": "WMONID=must-not-be-stored",
                            "X-Test": "retained",
                        },
                    )
                ],
            }
        ),
        attachment,
    )


def test_permission_is_required_by_default_without_any_network_request() -> None:
    http = FakeHttp()
    result = SpoPublicPdfResolver(
        SpoResolverConfig(),
        http=http,
    ).resolve(_paper())

    assert result.status == "permission_required"
    assert result.reason == "spo_automated_copying_not_enabled"
    assert result.error_code == "permission_required"
    assert not http.calls


@pytest.mark.parametrize(
    ("paper", "reason"),
    [
        (_paper(institution="다른 기관"), "institution_not_spo"),
        (_paper(journal="다른 학술지"), "journal_not_supported_by_spo_resolver"),
        (_paper(issue="not-an-issue"), "missing_or_invalid_issue"),
    ],
)
def test_ineligible_rows_never_reach_the_board(
    paper: PaperCandidate,
    reason: str,
) -> None:
    http = FakeHttp()
    result = _enabled(http).resolve(paper)

    assert result.reason == reason
    assert not http.calls


def test_exact_doi_pdf_resolves_and_cookie_headers_are_discarded() -> None:
    http, attachment = _success_http()
    result = _enabled(http).resolve(_paper())

    assert result.resolved
    assert result.status == "resolved"
    assert result.reason == "official_spo_pdf_exact_content_identity"
    assert result.attachment_url == attachment
    assert result.archive_entry is None
    assert result.matched_doi == "10.23026/forensics.2022.6.001"
    assert result.candidate is not None
    assert result.candidate.url == attachment
    assert result.candidate.verified_doi == result.matched_doi
    assert result.candidate.verified_source_id is None
    assert result.candidate.is_open_access is None
    assert result.fetch is not None
    assert "Set-Cookie" not in result.fetch.headers
    assert result.fetch.headers["X-Test"] == "retained"
    assert all(
        not any(key.casefold() == "cookie" for key in (headers or {}))
        for _url, _limit, headers in http.calls
    )


def test_attachment_filename_is_never_used_as_paper_identity() -> None:
    http, _attachment = _success_http(
        body=_text_pdf(["DOI: 10.23026/some.other.paper"])
    )
    # The HTML label can claim the selected title; only PDF content matters.
    result = _enabled(http).resolve(_paper(title="filename-only hint"))

    assert result.status == "not_found"
    assert result.reason == "exact_pdf_identity_not_found"


def test_safe_non_pdf_post_is_skipped_before_later_publication_post() -> None:
    call_id = "1031864"
    publication_id = "1033946"
    title = "<형사법의 신동향> 통권 제77호(2022. 겨울.)"
    hwp_token = "application-form.hwp"
    pdf_token = "published-paper.pdf"
    hwp_url = _attachment_url(call_id, hwp_token)
    pdf_url = _attachment_url(publication_id, pdf_token)
    doi = "10.23026/crclps.2022..77.002"
    http = FakeHttp(
        {
            SPO_LIST_URL: [
                Reply(
                    _list_page(
                        (call_id, f"{title} 원고 모집"),
                        (publication_id, f"{title} 3-1"),
                    )
                )
            ],
            _view_url(call_id): [
                Reply(
                    _view_page(
                        call_id,
                        f"{title} 원고 모집",
                        attachments=((hwp_token, "투고 신청서.hwp"),),
                    )
                )
            ],
            _view_url(publication_id): [
                Reply(
                    _view_page(
                        publication_id,
                        f"{title} 3-1",
                        attachments=((pdf_token, "게재 논문.pdf"),),
                    )
                )
            ],
            pdf_url: [
                Reply(
                    _text_pdf([f"DOI: {doi}"]),
                    content_type="application/pdf",
                )
            ],
        }
    )

    result = _enabled(http).resolve(
        _paper(
            journal="형사법의 신동향",
            issue="77",
            year=2022,
            doi=doi,
        )
    )

    assert result.resolved
    assert result.post_url == _view_url(publication_id)
    assert result.attachment_url == pdf_url
    assert hwp_url not in {url for url, _limit, _headers in http.calls}
    assert _view_url(call_id) in {url for url, _limit, _headers in http.calls}
    assert _view_url(publication_id) in {
        url for url, _limit, _headers in http.calls
    }


def test_doi_pdf_without_a_stamped_doi_uses_exact_multifactor_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, _attachment = _success_http(body=b"%PDF-1.7\nplaceholder")
    document = spo._PdfDocument(
        pages=(
            "117 Exact Forensic Paper Title Alice Example",
            "118 법과학의 신동향 통권 제6호(2022. 11.)",
        ),
        metadata_text="",
    )
    monkeypatch.setattr(spo, "_read_pdf_document", lambda *_args, **_kwargs: document)

    result = _enabled(http).resolve(_paper())

    assert result.resolved
    assert result.matched_title == "Exact Forensic Paper Title"
    assert result.matched_doi is None
    assert result.candidate is not None
    assert result.candidate.verified_doi is None


def test_doi_multifactor_fallback_rejects_any_conflicting_pdf_doi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, _attachment = _success_http(body=b"%PDF-1.7\nplaceholder")
    document = spo._PdfDocument(
        pages=(
            "117 Exact Forensic Paper Title Alice Example "
            "DOI: 10.23026/a.different.paper",
            "118 법과학의 신동향 통권 제6호(2022. 11.)",
        ),
        metadata_text="",
    )
    monkeypatch.setattr(spo, "_read_pdf_document", lambda *_args, **_kwargs: document)

    result = _enabled(http).resolve(_paper())

    assert result.status == "not_found"
    assert result.reason == "exact_pdf_identity_not_found"


def test_doi_less_pdf_requires_all_exact_content_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, _attachment = _success_http(body=b"%PDF-1.7\nplaceholder")
    document = spo._PdfDocument(
        pages=(
            "117 Exact Forensic Paper Title Alice Example",
            "118 법과학의 신동향 통권 제6호(2022. 11.)",
        ),
        metadata_text="",
    )
    monkeypatch.setattr(spo, "_read_pdf_document", lambda *_args, **_kwargs: document)

    result = _enabled(http).resolve(_paper(doi=None))

    assert result.resolved
    assert result.matched_title == "Exact Forensic Paper Title"
    assert result.matched_doi is None
    assert result.candidate is not None
    assert result.candidate.verified_doi is None


@pytest.mark.parametrize(
    "document",
    [
        # Fuzzy/partial title is not accepted.
        spo._PdfDocument(
            pages=(
                "117 Exact Forensic Paper Alice Example",
                "118 법과학의 신동향 통권 제6호(2022)",
            ),
            metadata_text="",
        ),
        # Wrong first author.
        spo._PdfDocument(
            pages=(
                "117 Exact Forensic Paper Title Bob Example",
                "118 법과학의 신동향 통권 제6호(2022)",
            ),
            metadata_text="",
        ),
        # Wrong printed final page.
        spo._PdfDocument(
            pages=(
                "117 Exact Forensic Paper Title Alice Example",
                "119 법과학의 신동향 통권 제6호(2022)",
            ),
            metadata_text="",
        ),
        # Journal/issue/year not jointly present.
        spo._PdfDocument(
            pages=(
                "117 Exact Forensic Paper Title Alice Example",
                "118 법과학의 신동향 통권 제7호(2022)",
            ),
            metadata_text="",
        ),
    ],
)
def test_doi_less_identity_is_all_fields_not_fuzzy(
    monkeypatch: pytest.MonkeyPatch,
    document: spo._PdfDocument,
) -> None:
    http, _attachment = _success_http(body=b"%PDF-1.7\nplaceholder")
    monkeypatch.setattr(spo, "_read_pdf_document", lambda *_args, **_kwargs: document)

    result = _enabled(http).resolve(_paper(doi=None))

    assert result.status == "not_found"
    assert result.reason == "exact_pdf_identity_not_found"


@pytest.mark.parametrize(
    (
        "source_id",
        "journal",
        "issue",
        "year",
        "doi",
        "database_first",
        "database_final",
        "official_first",
        "official_final",
        "official_page_count",
    ),
    [
        (
            "ART003030161",
            "형사법의 신동향",
            "81",
            2023,
            "10.23026/crclps.2023..81.003",
            "65",
            "98",
            65,
            96,
            32,
        ),
        (
            "ART003210490",
            "법과학의 신동향",
            "10",
            2024,
            None,
            "74",
            "111",
            76,
            111,
            33,
        ),
    ],
)
def test_audited_source_id_page_corrections_are_exact_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
    journal: str,
    issue: str,
    year: int,
    doi: str | None,
    database_first: str,
    database_final: str,
    official_first: int,
    official_final: int,
    official_page_count: int,
) -> None:
    bc_idx = "1067000"
    post_title = f"「{journal}」 통권 제{issue}호({year}. 10.)"
    token = "official-paper.pdf"
    attachment = _attachment_url(bc_idx, token)
    http = FakeHttp(
        {
            SPO_LIST_URL: [Reply(_list_page((bc_idx, post_title)))],
            _view_url(bc_idx): [
                Reply(
                    _view_page(
                        bc_idx,
                        post_title,
                        attachments=((token, "official-paper.pdf"),),
                    )
                )
            ],
            attachment: [
                Reply(
                    b"%PDF-1.7\nplaceholder",
                    content_type="application/pdf",
                )
            ],
        }
    )
    pages = ["middle"] * official_page_count
    pages[0] = (
        f"{official_first} Exact Forensic Paper Title Alice Example "
        f"{journal} 통권 제{issue}호({year}. 10.)"
    )
    pages[-1] = str(official_final)
    document = spo._PdfDocument(pages=tuple(pages), metadata_text="")
    monkeypatch.setattr(spo, "_read_pdf_document", lambda *_args, **_kwargs: document)
    paper = _paper(
        source_id=source_id,
        journal=journal,
        issue=issue,
        year=year,
        doi=doi,
        first_page=database_first,
        final_page=database_final,
    )

    result = _enabled(http).resolve(paper)

    assert result.resolved
    assert result.matched_title == "Exact Forensic Paper Title"
    assert result.matched_doi is None
    correction_attempts = [
        attempt
        for attempt in result.attempts
        if attempt.stage == "spo-page-identity-correction"
    ]
    assert len(correction_attempts) == 1
    assert correction_attempts[0].status == "applied"
    assert f"source_id={source_id}" in (correction_attempts[0].detail or "")
    assert (
        f"db_pages={database_first}-{database_final}"
        in (correction_attempts[0].detail or "")
    )

    expected, error = spo._expected_identity(paper)
    assert error is None
    assert expected is not None
    assert expected.first_page == official_first
    assert expected.final_page == official_final
    assert expected.page_count == official_page_count
    assert expected.page_correction is not None

    # The same metadata and PDF must not inherit the correction under any
    # other source id.
    unpinned, error = spo._expected_identity(
        replace(paper, source_id="ART009999999")
    )
    assert error is None
    assert unpinned is not None
    assert unpinned.page_correction is None
    assert (
        spo._match_pdf_identity(
            document,
            unpinned,
            corrected_post=False,
        )
        is None
    )


def test_page_correction_envelope_change_fails_closed() -> None:
    paper = _paper(
        source_id="ART003030161",
        journal="형사법의 신동향",
        issue="81",
        year=2023,
        doi="10.23026/crclps.2023..81.003",
        first_page="65",
        final_page="99",
    )

    expected, error = spo._expected_identity(paper)

    assert error is None
    assert expected is not None
    assert expected.page_correction is None
    assert expected.first_page == 65
    assert expected.final_page == 99
    assert expected.page_count == 35


def test_zip_entry_is_safely_extracted_and_returned_as_pdf() -> None:
    bc_idx = "1080251"
    title = "「법과학의 신동향」 통권 제11호(2025. 5.)"
    token = "server-bundle.zip"
    archive_url = _attachment_url(bc_idx, token)
    doi = "10.23026/forensics.2025.11.001"
    archive_body = _zip(
        [
            ("cover.pdf", _text_pdf(["not the selected DOI"])),
            ("papers/selected.pdf", _text_pdf([f"DOI: {doi}"])),
        ]
    )
    http = FakeHttp(
        {
            SPO_LIST_URL: [Reply(_list_page((bc_idx, title)))],
            _view_url(bc_idx): [
                Reply(
                    _view_page(
                        bc_idx,
                        title,
                        attachments=((token, "bundle.zip"),),
                        other_anchors=(
                            '<a href="https://www.kci.go.kr/article/ARTX">KCI</a>',
                        ),
                    )
                )
            ],
            archive_url: [
                Reply(archive_body, content_type="application/octet-stream")
            ],
        }
    )

    result = _enabled(http).resolve(
        _paper(issue="11", year=2025, doi=doi)
    )

    assert result.resolved
    assert result.archive_entry == "papers/selected.pdf"
    assert result.fetch is not None
    assert result.fetch.content_type == "application/pdf"
    assert result.fetch.headers["X-SPO-Archive-Entry"] == "papers/selected.pdf"
    assert result.pdf_bytes == _text_pdf([f"DOI: {doi}"])
    assert result.skipped_kci_links == 1
    assert all("kci.go.kr" not in url for url, _limit, _headers in http.calls)


def test_multiple_content_identical_doi_matches_are_ambiguous() -> None:
    bc_idx = "1032969"
    title = "「법과학의 신동향」 통권 제6호(2022. 11.)"
    tokens = ("first.pdf", "second.pdf")
    body = _text_pdf(["DOI: 10.23026/forensics.2022.6.001"])
    responses: dict[str, list[Reply | BaseException]] = {
        SPO_LIST_URL: [Reply(_list_page((bc_idx, title)))],
        _view_url(bc_idx): [
            Reply(
                _view_page(
                    bc_idx,
                    title,
                    attachments=tuple((token, token) for token in tokens),
                )
            )
        ],
    }
    for token in tokens:
        responses[_attachment_url(bc_idx, token)] = [
            Reply(body, content_type="application/pdf")
        ]

    result = _enabled(FakeHttp(responses)).resolve(_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_exact_spo_pdf_matches"


def test_kci_only_view_is_reported_but_never_followed() -> None:
    bc_idx = "1107696"
    title = "「법과학의 신동향」 통권 제13호(2026. 5.)"
    http = FakeHttp(
        {
            SPO_LIST_URL: [Reply(_list_page((bc_idx, title)))],
            _view_url(bc_idx): [
                Reply(
                    _view_page(
                        bc_idx,
                        title,
                        other_anchors=(
                            '<a href="https://www.kci.go.kr/article/ART003331345">'
                            "KCI 원문</a>",
                        ),
                    )
                )
            ],
        }
    )

    result = _enabled(http).resolve(
        _paper(
            source_id="ART003331345",
            issue="13",
            year=2026,
            doi=None,
        )
    )

    assert result.status == "not_found"
    assert result.reason == "no_official_spo_attachment"
    assert result.skipped_kci_links == 1
    assert len(http.calls) == 2


def test_list_requires_exact_journal_issue_and_year() -> None:
    unrelated = "「법과학의 신동향」 통권 제7호(2022. 11.)"
    http = FakeHttp(
        {SPO_LIST_URL: [Reply(_list_page(("999", unrelated)))]}
    )

    result = _enabled(http).resolve(_paper(issue="6", year=2022))

    assert result.status == "not_found"
    assert result.reason == "exact_spo_issue_post_not_found"
    assert len(http.calls) == 1


def test_documented_issue_76_title_typo_is_reached_only_by_explicit_neighbor() -> None:
    exact_id = "1031681"
    typo_id = "1031680"
    exact_title = "<형사법의 신동향> 통권 76호(2022. 가을) 2-2"
    typo_title = "<형사법의 신동향> 통권 75호(2022. 가을) 2-1"
    exact_token = "part-two.pdf"
    typo_token = "part-one.pdf"
    exact_url = _attachment_url(exact_id, exact_token)
    typo_url = _attachment_url(typo_id, typo_token)
    http = FakeHttp(
        {
            SPO_LIST_URL: [
                Reply(
                    _list_page(
                        (exact_id, exact_title),
                        (typo_id, typo_title),
                    )
                )
            ],
            _view_url(exact_id): [
                Reply(
                    _view_page(
                        exact_id,
                        exact_title,
                        attachments=((exact_token, "part 2"),),
                        other_anchors=(
                            '<a href="javascript:doPrevNext(\'1303\',\'1031680\');">'
                            f"{typo_title}</a>",
                        ),
                    )
                )
            ],
            _view_url(typo_id): [
                Reply(
                    _view_page(
                        typo_id,
                        typo_title,
                        attachments=((typo_token, "part 1"),),
                    )
                )
            ],
            exact_url: [
                Reply(
                    _text_pdf(["DOI: 10.23026/crclps.2022..76.999"]),
                    content_type="application/pdf",
                )
            ],
            typo_url: [
                Reply(
                    _text_pdf([f"DOI: {DOI}"]),
                    content_type="application/pdf",
                )
            ],
        }
    )

    result = _enabled(http).resolve(
        _paper(
            journal="형사법의 신동향",
            issue="76",
            year=2022,
            doi=DOI,
        )
    )

    assert result.resolved
    assert result.post_url == _view_url(typo_id)
    assert result.attachment_url == typo_url
    assert result.matched_doi == DOI


def test_mislabeled_76_part_is_not_used_without_the_explicit_neighbor_edge() -> None:
    typo_id = "1031680"
    typo_title = "<형사법의 신동향> 통권 75호(2022. 가을) 2-1"
    http = FakeHttp(
        {SPO_LIST_URL: [Reply(_list_page((typo_id, typo_title)))]}
    )

    result = _enabled(http).resolve(
        _paper(
            journal="형사법의 신동향",
            issue="76",
            year=2022,
            doi=DOI,
        )
    )

    assert result.status == "not_found"
    assert len(http.calls) == 1


def test_index_and_view_discovery_are_cached_but_pdf_is_revalidated() -> None:
    bc_idx = "1032969"
    title = "「법과학의 신동향」 통권 제6호(2022. 11.)"
    token = "paper.pdf"
    attachment = _attachment_url(bc_idx, token)
    pdf = _text_pdf(["DOI: 10.23026/forensics.2022.6.001"])
    page_two_url = (
        "https://www.spo.go.kr/site/spo/ex/board/List.do?"
        "cbIdx=1303&pageIndex=2"
    )
    http = FakeHttp(
        {
            SPO_LIST_URL: [
                Reply(
                    _list_page(
                        (bc_idx, title),
                        page_hrefs=("?pageIndex=2",),
                    )
                )
            ],
            page_two_url: [Reply(_list_page())],
            _view_url(bc_idx): [
                Reply(
                    _view_page(
                        bc_idx,
                        title,
                        attachments=((token, token),),
                    )
                )
            ],
            attachment: [
                Reply(pdf, content_type="application/pdf"),
                Reply(pdf, content_type="application/pdf"),
            ],
        }
    )
    resolver = _enabled(http)

    first = resolver.resolve(_paper())
    second = resolver.resolve(_paper())

    assert first.resolved and second.resolved
    counts = defaultdict(int)
    for url, _limit, _headers in http.calls:
        counts[url] += 1
    assert counts[SPO_LIST_URL] == 1
    assert counts[page_two_url] == 1
    assert counts[_view_url(bc_idx)] == 1
    assert counts[attachment] == 2
    assert any(attempt.status == "cache_hit" for attempt in second.attempts)


@pytest.mark.parametrize(
    "href",
    [
        (
            "https://evil.example/common/board/Download.do?"
            "bcIdx=1032969&cbIdx=1303&streFileNm=paper.pdf"
        ),
        (
            "/common/board/Download.do?"
            "bcIdx=OTHER&cbIdx=1303&streFileNm=paper.pdf"
        ),
        (
            "/common/board/Download.do?"
            "bcIdx=1032969&cbIdx=1303&streFileNm=../paper.pdf"
        ),
        (
            "/common/board/Download.do?"
            "bcIdx=1032969&cbIdx=1303&streFileNm=paper.pdf&extra=1"
        ),
    ],
)
def test_unsafe_or_mismatched_attachment_links_fail_closed(href: str) -> None:
    bc_idx = "1032969"
    title = "「법과학의 신동향」 통권 제6호(2022. 11.)"
    http = FakeHttp(
        {
            SPO_LIST_URL: [Reply(_list_page((bc_idx, title)))],
            _view_url(bc_idx): [
                Reply(
                    _view_page(
                        bc_idx,
                        title,
                        other_anchors=(f'<a href="{href}">PDF</a>',),
                    )
                )
            ],
        }
    )

    result = _enabled(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.error_code == "unsafe_url"
    assert len(http.calls) == 2


def test_redirect_or_url_change_is_rejected() -> None:
    http = FakeHttp(
        {
            SPO_LIST_URL: [
                Reply(
                    b"",
                    status=302,
                    final_url="https://evil.example/collect",
                    redirects=("https://evil.example/collect",),
                )
            ]
        }
    )

    result = _enabled(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.error_code == "redirect_not_allowed"


def test_transport_redirect_exception_is_audited() -> None:
    http = FakeHttp({SPO_LIST_URL: [RedirectLimitError("redirect")]})

    result = _enabled(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "spo_redirect_rejected"


def test_archive_rejects_traversal_symlink_duplicate_and_invalid_pdf() -> None:
    config = SpoResolverConfig(permission_granted=True)
    symlink = zipfile.ZipInfo("link.pdf")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    unsafe_archives = [
        _zip([("../escape.pdf", b"%PDF-1.7\n")]),
        _zip([(symlink, b"%PDF-1.7\n")]),
        _zip([("fake.pdf", b"not a PDF")]),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        unsafe_archives.append(
            _zip(
                [
                    ("duplicate.pdf", b"%PDF-1.7\n"),
                    ("DUPLICATE.PDF", b"%PDF-1.7\n"),
                ]
            )
        )

    for archive in unsafe_archives:
        with pytest.raises(spo._ArchiveSafetyError):
            spo._safe_zip_pdf_entries(archive, config)


def test_archive_enforces_entry_total_and_compression_ratio_limits() -> None:
    pdf = b"%PDF-1.7\n" + b"A" * 100
    two_entries = _zip([("one.pdf", pdf), ("two.pdf", pdf)])
    compressed_bomb = _zip([("large.pdf", b"%PDF-1.7\n" + b"0" * 10_000)])

    with pytest.raises(spo._ArchiveSafetyError, match="entry-count"):
        spo._safe_zip_pdf_entries(
            two_entries,
            SpoResolverConfig(permission_granted=True, max_zip_entries=1),
        )
    with pytest.raises(spo._ArchiveSafetyError, match="expanded-byte"):
        spo._safe_zip_pdf_entries(
            two_entries,
            SpoResolverConfig(
                permission_granted=True,
                max_zip_uncompressed_bytes=100,
            ),
        )
    with pytest.raises(spo._ArchiveSafetyError, match="compression-ratio"):
        spo._safe_zip_pdf_entries(
            compressed_bomb,
            SpoResolverConfig(
                permission_granted=True,
                max_zip_compression_ratio=2,
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"requests_per_second": 1.1},
        {"requests_per_second": 0},
        {"max_list_pages": 0},
        {"max_zip_entries": True},
        {"max_zip_compression_ratio": float("inf")},
        {"permission_granted": 1},
    ],
)
def test_config_limits_fail_fast(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SpoPublicPdfResolver(
            SpoResolverConfig(**changes),  # type: ignore[arg-type]
            http=FakeHttp(),
        )


def test_audited_current_db_coverage_is_explicitly_47_of_50() -> None:
    assert SPO_AUDITED_SELECTED_COUNT == 50
    assert SPO_AUDITED_RESOLVABLE_COUNT == 47
    assert SPO_AUDITED_UNAVAILABLE_SOURCE_IDS == {
        "ART003331345",
        "ART003331352",
        "ART003188894",
    }
