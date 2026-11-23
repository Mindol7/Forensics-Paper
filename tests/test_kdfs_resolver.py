from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlencode

import pytest

import PaperPipeline.kdfs_resolver as kdfs
from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.kdfs_resolver import (
    KDFS_DETAIL_PATH,
    KDFS_DOWNLOAD_PATH,
    KDFS_HOST,
    KDFS_INTERNAL_USE_NOTICE,
    KDFS_ORIGIN,
    KDFS_RESOLVER_NAME,
    KDFS_SEARCH_URL,
    KdfsJamsAuthorizedPdfResolver,
    KdfsResolverConfig,
)
from PaperPipeline.records import PaperCandidate


TITLE = "얼굴 탐지 기술을 활용한 딥페이크 영상의 저작권 침해 여부 탐지"
ENGLISH_TITLE = (
    "Detection of Copyright Infringement in Deepfake Video using Face Detector"
)
DOI = "10.22798/kdfs.2022.16.1.1"
ARTICLE_ID = "SJ0000000546"
FILE_ID = "KCI_FI003362664"


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    requested_url: str | None = None
    final_url: str | None = None
    redirects: tuple[str, ...] = ()
    extra_headers: Mapping[str, str] | None = None


class FakeHttp:
    def __init__(
        self,
        *,
        gets: list[Reply | BaseException],
        posts: list[Reply | BaseException],
    ) -> None:
        self.gets = deque(gets)
        self.posts = deque(posts)
        self.calls: list[
            tuple[
                str,
                str,
                Mapping[str, str] | None,
                int,
                Mapping[str, str] | None,
            ]
        ] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        self.calls.append(("GET", url, None, max_bytes, headers))
        return self._fetch(self.gets.popleft(), url)

    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        self.calls.append(("POST", url, dict(data), max_bytes, headers))
        return self._fetch(self.posts.popleft(), url)

    @staticmethod
    def _fetch(value: Reply | BaseException, url: str) -> HttpFetch:
        if isinstance(value, BaseException):
            raise value
        headers = {"Content-Type": value.content_type}
        headers.update(value.extra_headers or {})
        return HttpFetch(
            requested_url=value.requested_url or url,
            final_url=value.final_url or url,
            status_code=value.status,
            headers=headers,
            body=value.body,
            redirects=value.redirects,
        )


def _paper(**changes: object) -> PaperCandidate:
    values: dict[str, object] = {
        "database_id": 1,
        "source": "kci",
        "source_id": "ART002830207",
        "title": TITLE,
        "doi_original": f"http://dx.doi.org/{DOI.upper()}",
        "doi": DOI,
        "landing_url": (
            "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
            "ciSereArtiView.kci?artiId=ART002830207"
        ),
        "abstract": None,
        "publication_year": 2022,
        "first_page": "1",
        "final_page": "12",
        "alternate_titles": (ENGLISH_TITLE,),
        "journal_id": "디지털포렌식연구",
        "institution_id": "한국디지털포렌식학회",
        "issue_id": "16_1",
        "authors": (
            "김동엽 (고려대학교 정보보안학과)",
            "이상진 (고려대학교)",
        ),
    }
    values.update(changes)
    return PaperCandidate(**values)  # type: ignore[arg-type]


def _detail_url(
    *,
    article_id: str = ARTICLE_ID,
    count: str = "1",
) -> str:
    return (
        f"{KDFS_ORIGIN}{KDFS_DETAIL_PATH}?"
        + urlencode(
            {
                "soceId": "INS000009412",
                "artiId": article_id,
                "sereId": "SER000000001",
                "submCnt": count,
            }
        )
    )


def _download_url(file_id: str = FILE_ID) -> str:
    return (
        f"{KDFS_ORIGIN}{KDFS_DOWNLOAD_PATH}?"
        + urlencode({"orteFileId": file_id})
    )


def _search_row(
    *,
    title: str = TITLE,
    article_id: str = ARTICLE_ID,
    count: str = "1",
    journal: str = "디지털포렌식연구",
    year: int = 2022,
    volume: int = 16,
    issue: int = 1,
    first_page: int = 1,
    final_page: int = 12,
    author: str = "김동엽 (고려대학교 정보보안학과)",
    control: str = "",
) -> str:
    return f"""
      <div class="col-12">
        <div class="box">
          <a class="tit"
             href="javascript:fncGoList(
               'INS000009412','{article_id}','SER000000001','{count}'
             );">
            1. <em>{title}</em>
          </a>
          <div class="info-2">
            <p><em>저자</em><span>{author} || 이상진 (고려대학교)</span></p>
            <p><em>학술지</em><span>{journal}
              {volume} 권 {issue} 호 pp.{first_page}~{final_page}
            </span></p>
            <p><em>발행년월</em><span>{year}.03</span></p>
          </div>
          {control}
        </div>
      </div>
    """


def _search_reply(*rows: str, **reply_changes: object) -> Reply:
    return Reply(
        body=(
            "<!doctype html><html><body>"
            '<div class="shadow-box-1">'
            + "".join(rows)
            + "</div></body></html>"
        ).encode(),
        **reply_changes,
    )


def _detail_reply(
    *,
    title: str = TITLE,
    journal: str = "디지털포렌식연구",
    volume: int = 16,
    issue: int = 1,
    first_page: int = 1,
    final_page: int = 12,
    author: str = "김동엽 (고려대학교 정보보안학과)",
    control: str = (
        f"<a class=\"download\" onclick=\"fncFileIframe('{FILE_ID}')\">"
        "원문 PDF</a>"
    ),
    template: str = (
        '<script>function fncFileIframe(orteFileId){'
        'ifrmFile.location.href="/co/download/popup/poDownload.kci?'
        'orteFileId="+orteFileId;}</script>'
    ),
    **reply_changes: object,
) -> Reply:
    return Reply(
        body=f"""
          <!doctype html><html><body>
            {template}
            <div class="infos">
              <p class="txt1">{title}</p>
              <p class="txt3"><em>학술지명:</em><span>{journal}</span></p>
              <p class="txt3"><em>발행정보:</em>
                <span>{volume}권 {issue}호 pp.{first_page} ~ {final_page}</span>
              </p>
              <p class="txt3"><em>저자:</em><span>{author}</span></p>
            </div>
            <div class="subbtn_align">{control}</div>
          </body></html>
        """.encode(),
        **reply_changes,
    )


def _pdf_reply(**changes: object) -> Reply:
    return Reply(
        body=b"%PDF-1.7\nfixture-kdfs-pdf",
        content_type="application/pdf",
        extra_headers={
            "Content-Disposition": "attachment; filename=paper.pdf",
            "Set-Cookie": "JSESSIONID=must-not-carry; Secure; HttpOnly",
        },
        **changes,
    )


def _pdf_document(
    *,
    doi: str = DOI,
    title: str = TITLE,
    journal: str = "디지털포렌식연구",
    year: int = 2022,
    volume: int = 16,
    issue: int = 1,
    author: str = "김동엽",
    first_page: int = 1,
    final_page: int = 12,
    page_count: int = 12,
) -> kdfs._PdfDocument:
    first = (
        f"{journal} {year} 제{volume}권 제{issue}호 "
        f"pp.{first_page}~{final_page} {title} {author} DOI: {doi}"
    )
    return kdfs._PdfDocument(
        page_count=page_count,
        first_pages=(first, "둘째 페이지", "셋째 페이지"),
        last_page=f"마지막 페이지 {final_page}",
        metadata_text=title,
    )


def _resolver(
    http: FakeHttp,
    **changes: object,
) -> KdfsJamsAuthorizedPdfResolver:
    return KdfsJamsAuthorizedPdfResolver(
        KdfsResolverConfig(permission_granted=True, **changes),
        http=http,  # type: ignore[arg-type]
    )


def test_permission_guard_makes_no_network_request() -> None:
    http = FakeHttp(gets=[], posts=[])
    resolver = KdfsJamsAuthorizedPdfResolver(http=http)  # type: ignore[arg-type]

    result = resolver.resolve(_paper())

    assert result.status == "permission_required"
    assert result.reason == "kdfs_automation_permission_not_enabled"
    assert not http.calls


def test_exact_official_control_resolves_and_validates_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(), _pdf_reply()],
    )
    monkeypatch.setattr(
        kdfs,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(),
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "resolved"
    assert result.resolved is True
    assert result.reason == "official_kdfs_jams_exact_pdf_identity"
    assert result.resolver == KDFS_RESOLVER_NAME
    assert result.matched_title == TITLE
    assert result.jams_article_id == ARTICLE_ID
    assert result.detail_url == _detail_url()
    assert result.download_url == _download_url()
    assert result.pdf_bytes == b"%PDF-1.7\nfixture-kdfs-pdf"
    assert result.bytes_received == len(result.pdf_bytes or b"")
    assert result.usage_notice == KDFS_INTERNAL_USE_NOTICE
    assert result.candidate is not None
    assert result.candidate.url == _download_url()
    assert result.candidate.landing_url == _detail_url()
    assert result.candidate.is_open_access is None
    assert result.candidate.verified_doi == DOI
    assert result.fetch is not None
    assert not any(
        key.casefold() == "set-cookie" for key in result.fetch.headers
    )

    assert [call[0] for call in http.calls] == ["POST", "GET", "GET"]
    search = http.calls[0]
    assert search[1] == KDFS_SEARCH_URL
    assert search[2] == {
        "startPg": "1",
        "itemTypeCd": "13",
        "fieldList": "title_idx",
        "keywordList": TITLE,
        "operationList": "AND",
        "pubiStYr": "",
        "pubiEndYr": "",
    }
    assert http.calls[1][1] == _detail_url()
    assert http.calls[2][1] == _download_url()
    assert http.calls[2][4]["Referer"] == _detail_url()
    for _method, _url, _data, _limit, headers in http.calls:
        assert headers is not None
        assert not any(
            name.casefold()
            in {"authorization", "cookie", "proxy-authorization"}
            for name in headers
        )


def test_explicit_direct_download_anchor_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = f'<a href="{_download_url()}">PDF 다운로드</a>'
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[
            _detail_reply(control=control, template=""),
            _pdf_reply(),
        ],
    )
    monkeypatch.setattr(
        kdfs,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(),
    )

    result = _resolver(http).resolve(_paper())

    assert result.resolved is True
    assert result.download_url == _download_url()


def test_anonymous_detail_without_file_control_stops_permission_safely() -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[
            _detail_reply(
                control="<!-- 로그인 사용자만 파일 다운로드 가능 -->",
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "permission_required"
    assert result.reason == "kdfs_anonymous_download_not_advertised"
    assert result.jams_article_id == ARTICLE_ID
    assert [call[0] for call in http.calls] == ["POST", "GET"]
    assert not http.gets


def test_file_id_is_never_derived_from_article_id_or_script_template() -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(control="")],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "permission_required"
    assert result.download_url is None
    assert all(KDFS_DOWNLOAD_PATH not in call[1] for call in http.calls)


def test_inline_search_highlights_do_not_insert_spaces_inside_korean_words() -> None:
    row = _search_row().replace(
        f"<em>{TITLE}</em>",
        (
            "<em>얼굴</em> 탐지 기술을 활용한 "
            "<em>딥</em><em>페이크</em> 영상의 저작권 침해 여부 탐지"
        ),
    )
    http = FakeHttp(
        posts=[_search_reply(row)],
        gets=[_detail_reply(control="")],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "permission_required"
    assert result.matched_title == TITLE
    assert [call[0] for call in http.calls] == ["POST", "GET"]


def test_download_action_requires_page_advertised_template() -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(template="")],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "unsafe_kdfs_download_control"
    assert len(http.calls) == 2


def test_foreign_download_anchor_is_rejected() -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[
            _detail_reply(
                control=(
                    '<a href="https://evil.example/co/download/popup/'
                    'poDownload.kci?orteFileId=KCI_FI003362664">'
                    "PDF</a>"
                )
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "unsafe_kdfs_download_control"
    assert len(http.calls) == 2


def test_multiple_distinct_explicit_file_controls_are_ambiguous() -> None:
    controls = (
        f"<a onclick=\"fncFileIframe('{FILE_ID}')\">PDF 1</a>"
        "<a onclick=\"fncFileIframe('KCI_FI003362665')\">PDF 2</a>"
    )
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(control=controls)],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_kdfs_download_controls"
    assert len(http.calls) == 2


def test_exact_title_with_wrong_search_bibliography_fails_closed() -> None:
    http = FakeHttp(
        posts=[
            _search_reply(
                _search_row(issue=2),
            )
        ],
        gets=[],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "kdfs_search_bibliographic_identity_mismatch"
    assert len(http.calls) == 1


def test_search_metadata_is_never_borrowed_from_another_row() -> None:
    bad = _search_row(journal="다른학술지")
    unrelated = _search_row(
        title="전혀 다른 논문",
        article_id="SJ0000000999",
    )
    http = FakeHttp(
        posts=[_search_reply(bad, unrelated)],
        gets=[],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert len(http.calls) == 1


def test_multiple_exact_article_ids_are_ambiguous() -> None:
    http = FakeHttp(
        posts=[
            _search_reply(
                _search_row(),
                _search_row(article_id="SJ0000000547"),
            )
        ],
        gets=[],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_exact_kdfs_article_matches"


def test_no_exact_title_match_returns_not_found() -> None:
    http = FakeHttp(
        posts=[
            _search_reply(
                _search_row(title="전혀 다른 논문"),
            ),
            _search_reply(),
        ],
        gets=[],
    )

    result = _resolver(http, max_title_queries=2).resolve(_paper())

    assert result.status == "not_found"
    assert result.reason == "exact_kdfs_article_not_found"
    assert [call[2]["keywordList"] for call in http.calls] == [
        TITLE,
        ENGLISH_TITLE,
    ]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"institution_id": "다른 기관"}, "unsupported_kdfs_institution"),
        ({"journal_id": "다른 학술지"}, "unsupported_kdfs_journal"),
        ({"source_id": "../ART1"}, "invalid_kci_source_id"),
        ({"issue_id": "16"}, "incomplete_kdfs_bibliographic_identity"),
        ({"publication_year": None}, "incomplete_kdfs_bibliographic_identity"),
        ({"first_page": "x"}, "incomplete_kdfs_bibliographic_identity"),
        ({"authors": ()}, "incomplete_kdfs_bibliographic_identity"),
        (
            {"doi": "10.22798/kdfs.2022.16.2.1"},
            "kdfs_doi_bibliographic_identity_mismatch",
        ),
    ],
)
def test_eligibility_is_exact_and_requires_complete_identity(
    changes: Mapping[str, object],
    reason: str,
) -> None:
    http = FakeHttp(gets=[], posts=[])

    result = _resolver(http).resolve(_paper(**changes))

    assert result.resolved is False
    assert result.reason == reason
    assert not http.calls


def test_historic_kdfs_journal_identifier_is_an_exact_alias() -> None:
    paper = _paper(
        journal_id="SER000003109",
        doi=None,
        doi_original=None,
    )
    http = FakeHttp(posts=[_search_reply()], gets=[])

    result = _resolver(http, max_title_queries=1).resolve(paper)

    assert result.status == "not_found"
    assert len(http.calls) == 1


def test_doi_suffix_page_is_not_mistaken_for_an_identity_guarantee() -> None:
    # The official ART003226378 row starts at page 153 while its publisher DOI
    # ends in 157.  DOI, title, and printed page range are verified separately.
    paper = _paper(
        source_id="ART003226378",
        title="안드로이드 환경에서의 네이버 MYBOX 애플리케이션 아티팩트 분석",
        alternate_titles=(
            "Artifact Analysis of the Naver MYBOX Application on Android",
        ),
        doi="10.22798/kdfs.2025.19.3.157",
        doi_original="http://dx.doi.org/10.22798/KDFS.2025.19.3.157",
        publication_year=2025,
        issue_id="19_3",
        first_page="153",
        final_page="184",
        authors=(
            "안관우(아주대학교 사이버보안학과)",
            "손태식(아주대학교)",
        ),
    )
    http = FakeHttp(posts=[_search_reply()], gets=[])

    result = _resolver(http, max_title_queries=1).resolve(paper)

    assert result.status == "not_found"
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    ("detail_changes", "reason"),
    [
        ({"title": "전혀 다른 제목"}, "kdfs_detail_title_mismatch"),
        ({"journal": "다른학술지"}, "kdfs_detail_journal_mismatch"),
        ({"issue": 2}, "kdfs_detail_volume_issue_mismatch"),
        ({"final_page": 13}, "kdfs_detail_page_range_mismatch"),
        ({"author": "다른저자"}, "kdfs_detail_author_mismatch"),
    ],
)
def test_detail_page_independently_rechecks_identity(
    detail_changes: Mapping[str, object],
    reason: str,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(**detail_changes)],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == reason
    assert result.pdf_bytes is None
    assert len(http.calls) == 2


@pytest.mark.parametrize(
    ("pdf_changes", "reason"),
    [
        ({"page_count": 2}, "kdfs_pdf_page_count_mismatch"),
        ({"title": "전혀 다른 제목"}, "kdfs_pdf_title_mismatch"),
        ({"doi": "10.22798/kdfs.2022.16.1.99"}, "kdfs_pdf_doi_mismatch"),
        ({"journal": "다른학술지"}, "kdfs_pdf_journal_mismatch"),
        ({"year": 2021}, "kdfs_pdf_year_mismatch"),
        ({"issue": 2}, "kdfs_pdf_volume_issue_mismatch"),
        ({"author": "다른저자"}, "kdfs_pdf_author_mismatch"),
        ({"first_page": 2}, "kdfs_pdf_first_page_mismatch"),
        ({"final_page": 11}, "kdfs_pdf_final_page_mismatch"),
    ],
)
def test_pdf_content_must_match_every_identity_dimension(
    pdf_changes: Mapping[str, object],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(), _pdf_reply()],
    )
    monkeypatch.setattr(
        kdfs,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(
            **pdf_changes
        ),
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == reason


def test_pdf_may_omit_doi_when_complete_bibliographic_identity_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(), _pdf_reply()],
    )
    document = _pdf_document()
    first = document.first_pages[0].replace(f" DOI: {DOI}", "")
    monkeypatch.setattr(
        kdfs,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: kdfs._PdfDocument(
            page_count=document.page_count,
            first_pages=(first, *document.first_pages[1:]),
            last_page=document.last_page,
            metadata_text=document.metadata_text,
        ),
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "resolved"


def test_pdf_may_omit_combined_page_range_when_terminal_page_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(), _pdf_reply()],
    )
    document = _pdf_document()
    first = (
        document.first_pages[0].replace("pp.1~12 ", "")
        + " ISO 27001-2013"
    )
    monkeypatch.setattr(
        kdfs,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: kdfs._PdfDocument(
            page_count=document.page_count,
            first_pages=(first, *document.first_pages[1:]),
            last_page=document.last_page,
            metadata_text=document.metadata_text,
        ),
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "resolved"


def test_pdf_page_range_check_ignores_later_body_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(), _pdf_reply()],
    )
    document = _pdf_document()
    first = document.first_pages[0].replace("pp.1~12 ", "")
    monkeypatch.setattr(
        kdfs,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: kdfs._PdfDocument(
            page_count=document.page_count,
            first_pages=(first, "citation pp.200-220", document.first_pages[2]),
            last_page=document.last_page,
            metadata_text=document.metadata_text,
        ),
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "resolved"


def test_exact_pdf_doi_allows_unextractable_terminal_page_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(), _pdf_reply()],
    )
    document = _pdf_document()
    monkeypatch.setattr(
        kdfs,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: kdfs._PdfDocument(
            page_count=document.page_count,
            first_pages=document.first_pages,
            last_page="terminal page number is not extractable",
            metadata_text=document.metadata_text,
        ),
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "resolved"


def test_doi_less_row_uses_all_other_exact_pdf_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(), _pdf_reply()],
    )
    monkeypatch.setattr(
        kdfs,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(doi=""),
    )

    result = _resolver(http).resolve(
        _paper(doi=None, doi_original=None)
    )

    assert result.resolved is True
    assert result.expected_doi is None
    assert result.candidate is not None
    assert result.candidate.verified_doi is None


def test_invalid_pdf_is_not_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[_detail_reply(), _pdf_reply()],
    )

    def reject(*args: object, **kwargs: object) -> kdfs._PdfDocument:
        raise ValueError("invalid")

    monkeypatch.setattr(kdfs, "_read_pdf_document", reject)

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_pdf"
    assert result.reason == "kdfs_attachment_is_not_a_safe_parseable_pdf"
    assert result.pdf_bytes is None


def test_html_login_response_is_access_denied_before_pdf_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[
            _detail_reply(),
            Reply(body=b"<html><body>login</body></html>"),
        ],
    )
    inspected = False

    def inspect(*args: object, **kwargs: object) -> kdfs._PdfDocument:
        nonlocal inspected
        inspected = True
        return _pdf_document()

    monkeypatch.setattr(kdfs, "_read_pdf_document", inspect)

    result = _resolver(http).resolve(_paper())

    assert result.status == "access_denied"
    assert result.reason == "kdfs_download_returned_html_or_login"
    assert inspected is False


def test_redirected_detail_is_rejected_even_on_official_host() -> None:
    redirected = f"{KDFS_ORIGIN}/co/main/jmMain.kci"
    http = FakeHttp(
        posts=[_search_reply(_search_row())],
        gets=[
            _detail_reply(
                final_url=redirected,
                redirects=(redirected,),
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "kdfs_response_url_or_redirect_mismatch"
    assert result.error_code == "unsafe_redirect"
    assert len(http.calls) == 2


def test_foreign_search_response_is_rejected() -> None:
    http = FakeHttp(
        posts=[
            _search_reply(
                _search_row(),
                final_url="https://evil.example/results",
            )
        ],
        gets=[],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "kdfs_response_url_or_redirect_mismatch"
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [
        (429, "retryable_error"),
        (503, "retryable_error"),
        (403, "access_denied"),
        (404, "not_found"),
        (418, "invalid_response"),
    ],
)
def test_http_statuses_have_stable_outcomes(
    status: int,
    expected_status: str,
) -> None:
    http = FakeHttp(
        posts=[Reply(body=b"", status=status)],
        gets=[],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == expected_status
    assert result.http_status == status
    assert len(http.calls) == 1


def test_retryable_exception_is_auditable() -> None:
    http = FakeHttp(
        posts=[RetryableHttpError("temporary")],
        gets=[],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "retryable_error"
    assert result.error_code == "retryable_error"
    assert result.attempts[0].stage == "kdfs-title-search"


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"requests_per_second": 0}, "requests_per_second"),
        ({"requests_per_second": 1.1}, "requests_per_second"),
        ({"connect_timeout": 0}, "connect_timeout"),
        ({"read_timeout": 0}, "read_timeout"),
        ({"http_retries": -1}, "http_retries"),
        ({"max_search_response_bytes": 0}, "max_search_response_bytes"),
        ({"max_detail_response_bytes": 0}, "max_detail_response_bytes"),
        ({"max_pdf_bytes": 0}, "max_pdf_bytes"),
        ({"max_pdf_pages": 0}, "max_pdf_pages"),
        ({"max_pdf_text_chars": 0}, "max_pdf_text_chars"),
        ({"max_title_queries": 0}, "max_title_queries"),
    ],
)
def test_invalid_configuration_fails_fast(
    changes: Mapping[str, object],
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        KdfsJamsAuthorizedPdfResolver(
            KdfsResolverConfig(**changes),  # type: ignore[arg-type]
        )


def test_official_url_validator_rejects_credentials_ports_and_duplicates() -> None:
    assert kdfs._is_exact_official_url(KDFS_SEARCH_URL, kind="search")
    assert kdfs._is_exact_official_url(_detail_url(), kind="detail")
    assert kdfs._is_exact_official_url(_download_url(), kind="download")
    assert not kdfs._is_exact_official_url(
        f"https://user@{KDFS_HOST}{KDFS_SEARCH_URL.removeprefix(KDFS_ORIGIN)}",
        kind="search",
    )
    assert not kdfs._is_exact_official_url(
        f"https://{KDFS_HOST}:444{KDFS_DOWNLOAD_PATH}?"
        f"orteFileId={FILE_ID}",
        kind="download",
    )
    assert not kdfs._is_exact_official_url(
        _download_url() + f"&orteFileId={FILE_ID}",
        kind="download",
    )
    assert not kdfs._is_exact_official_url(
        f"https://{KDFS_HOST}.evil.example{KDFS_DOWNLOAD_PATH}?"
        f"orteFileId={FILE_ID}",
        kind="download",
    )
