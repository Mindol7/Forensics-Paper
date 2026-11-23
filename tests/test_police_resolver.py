from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote

import pytest

import PaperPipeline.police_resolver as police
from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.police_resolver import (
    POLICE_AUDITED_ABSENT_SOURCE_IDS,
    POLICE_AUDITED_RESOLVABLE_SOURCE_IDS,
    POLICE_BOARD_URL,
    POLICE_DOWNLOAD_URL,
    POLICE_INTERNAL_USE_NOTICE,
    POLICE_RESOLVER_NAME,
    POLICE_ROBOTS_URL,
    PoliceResolverConfig,
    PoliceUniversityPublicPdfResolver,
)
from PaperPipeline.records import PaperCandidate


@dataclass(frozen=True)
class PaperFixture:
    source_id: str
    title: str
    english_title: str
    issue_id: str
    first_page: str
    final_page: str
    authors: tuple[str, ...]


KNOWN = (
    PaperFixture(
        "ART003198151",
        "영상처리 디지털포렌식 기술의 혁신 분석",
        "Analysing Innovations in Image Processing Digital Forensics",
        "11_1",
        "5",
        "31",
        ("심지연(연세대학교)", "나승균(연세대학교)", "이봉규(연세대학교)"),
    ),
    PaperFixture(
        "ART003237353",
        "불법 딥페이크 영상 제작 사건의 디지털증거 확보와 압수 절차 정립에 관한 연구",
        (
            "A Study on the Collection of Digital Evidence and the "
            "Establishment of Seizure Procedures in Illegal Deepfake "
            "Video Creation Cases"
        ),
        "11_2",
        "5",
        "26",
        ("이진호(경찰청)", "이상진(고려대학교)"),
    ),
    PaperFixture(
        "ART003237379",
        "AI 특허 분석을 통한 수사기술 진화 전략",
        (
            "AI Patent Network Analysis for the Advancement of "
            "Investigative Technologies"
        ),
        "11_2",
        "93",
        "114",
        ("나승균(연세대학교)", "김민제(연세대학교)", "이기헌(연세대학교)"),
    ),
    PaperFixture(
        "ART003237411",
        "발화 패턴 기반 오픈 채팅 참여자 네트워크 분석",
        (
            "Utterance Pattern-Based Network Analysis of Participants in "
            "Open Chat Rooms"
        ),
        "11_2",
        "229",
        "253",
        ("김채린(고려대학교)", "이상진(고려대학교)"),
    ),
    PaperFixture(
        "ART003286669",
        (
            "외국에서 수집한 디지털증거의 증거능력 연구 - 국가안보사건 "
            "판례와 캄보디아 사건의 시사점을 중심으로 -"
        ),
        (
            "A Study on the Admissibility of Digital Evidence Collected "
            "Abroad - Focusing on National Security Case Precedents and "
            "Implications from the Cambodia Case -"
        ),
        "11_3",
        "87",
        "108",
        ("고상협(성균관대학교)", "김기범(성균관대학교)"),
    ),
    PaperFixture(
        "ART003286695",
        "한국·영국 디지털포렌식 품질관리 비교 및 발전 방향",
        (
            "A Comparative Study on Digital Forensic Quality Management "
            "Systems in Korea and the United Kingdom"
        ),
        "11_3",
        "109",
        "129",
        (
            "윤성조(경찰대학, 성균관대학교 디지털포렌식학과 석사과정)",
            "문병훈(성균관대학교 과학수사학과)",
            "김기범(성균관대학교)",
        ),
    ),
    PaperFixture(
        "ART003286806",
        "수정된 위드마크 공식으로 산출한 혈중알코올농도의 증명력에 관한 연구",
        (
            "A Study on the Evidentiary Value of Blood Alcohol "
            "Concentration Estimated by the Modified Widmark Formula"
        ),
        "11_3",
        "309",
        "337",
        (
            "이지연(경찰대학 치안정책연구소, 순천향대학교 대학원)",
            "김태희(순천향대학교 법과학대학원 석사과정)",
            "신윤지(순천향대학교 법과학대학원 석사과정)",
            "유제설(순천향대학교)",
        ),
    ),
)

ABSENT = (
    PaperFixture(
        "ART003330239",
        "클라우드 압수수색의 유형화와 절차적 권리 보장을 위한 집행지침",
        (
            "A Typology of Cloud Search and Seizure and Enforcement "
            "Guidelines to Protect Procedural Rights"
        ),
        "12_1",
        "53",
        "80",
        ("한상혁(경찰수사연수원)", "양홍석(경찰수사연수원)", "김기범(성균관대학교)"),
    ),
    PaperFixture(
        "ART003330395",
        (
            "AI 기반 3D 현장 재구성 기술의 형사사법적 활용 - "
            "3D Gaussian Splatting(3DGS)을 중심으로 -"
        ),
        (
            "Criminal Justice Applications of AI-Based 3D Crime Scene "
            "Reconstruction - Focusing on 3D Gaussian Splatting (3DGS) -"
        ),
        "12_1",
        "283",
        "306",
        ("조동호(경찰청)", "김한별(경찰대학)"),
    ),
    PaperFixture(
        "ART003330400",
        (
            "메신저 대화 트리아지를 위한 화자 스크리닝 연구 - "
            "머신러닝과 딥러닝 임베딩 기반 접근 -"
        ),
        (
            "Speaker Screening for Messenger Conversation Triage - "
            "Machine Learning and Deep Learning Embedding-Based Approaches -"
        ),
        "12_1",
        "307",
        "326",
        ("고상협(성균관대학교)", "김기범(성균관대학교)"),
    ),
)


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


def _paper(
    fixture: PaperFixture = KNOWN[0],
    **changes: object,
) -> PaperCandidate:
    values: dict[str, object] = {
        "database_id": 1,
        "source": "kci",
        "source_id": fixture.source_id,
        "title": fixture.title,
        "doi_original": None,
        "doi": None,
        "landing_url": (
            "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
            f"ciSereArtiView.kci?artiId={fixture.source_id}"
        ),
        "abstract": None,
        "publication_year": 2025 if fixture.issue_id.startswith("11_") else 2026,
        "first_page": fixture.first_page,
        "final_page": fixture.final_page,
        "alternate_titles": (fixture.english_title,),
        "journal_id": (
            "SER000005144"
            if fixture.source_id == "ART003330239"
            else "범죄수사학연구"
        ),
        "institution_id": "범죄수사연구원",
        "issue_id": fixture.issue_id,
        "authors": fixture.authors,
    }
    values.update(changes)
    return PaperCandidate(**values)  # type: ignore[arg-type]


def _robots(*lines: str) -> Reply:
    return Reply(
        body="\n".join(
            lines
            or (
                "User-agent : Googlebot",
                "Disallow: */boardList.do",
                "",
                "User-agent: *",
                "Disallow: /police/police/knpu/traf/reserved_end.do",
                "Allow: /",
            )
        ).encode(),
        content_type="text/plain; charset=utf-8",
    )


def _board_form(
    *,
    action: str = (
        "/police/police/master/208/boardList.do?mdex=police175"
    ),
    page_index: str = "",
) -> Reply:
    return Reply(
        body=f"""
        <!doctype html>
        <html><body>
          <form id="searchForm" name="searchForm" method="post"
                action="{action}">
            <select name="searchKey">
              <option value="T">제목</option>
              <option value="C">내용</option>
              <option value="TC">제목+내용</option>
            </select>
            <input type="text" name="searchKeyword" value="">
            <input type="hidden" name="type" value="">
            <input type="hidden" name="pageIndex" value="{page_index}">
          </form>
        </body></html>
        """.encode(),
    )


def _article_filename(fixture: PaperFixture) -> str:
    volume, issue = fixture.issue_id.split("_", 1)
    return (
        f"『제{volume}권 제{issue}호(통권 제24호) "
        f"4. {fixture.title}.pdf"
    )


def _result_page(
    fixture: PaperFixture | None = KNOWN[0],
    *,
    path: str = "/pds/",
    filename: str | None = None,
    row_text: str | None = None,
    duplicate_article_seq: str | None = None,
    final_url: str | None = None,
    redirects: tuple[str, ...] = (),
) -> Reply:
    if fixture is None:
        rows = "<tr><td>검색 결과가 없습니다.</td></tr>"
    else:
        filename = filename or _article_filename(fixture)
        rows = _row(
            filename=filename,
            row_text=row_text,
            path=path,
            article_seq="900001",
        )
        if duplicate_article_seq is not None:
            rows += _row(
                filename=filename,
                row_text=row_text,
                path=path,
                article_seq=duplicate_article_seq,
            )
    return Reply(
        body=(
            "<!doctype html><html><body><table>"
            f"{rows}"
            "</table></body></html>"
        ).encode(),
        final_url=final_url,
        redirects=redirects,
    )


def _row(
    *,
    filename: str,
    row_text: str | None = None,
    path: str,
    article_seq: str,
) -> str:
    return f"""
      <tr>
        <td>{row_text or filename}</td>
        <td>
          <a class="fileDown" href="#">
            <img src="/images/file.gif" alt="첨부파일">
          </a>
          <input type="hidden" class="fnm" value="fixture-900001.pdf">
          <input type="hidden" class="orgFnm" value="{filename}">
          <input type="hidden" class="path" value="{path}">
          <input type="hidden" class="articleSeq" value="{article_seq}">
        </td>
      </tr>
    """


def _pdf_reply() -> Reply:
    return Reply(
        body=b"%PDF-1.7\nfixture-pdf",
        content_type="application/octet-stream",
        extra_headers={
            "Content-Disposition": "attachment; filename=article.pdf",
            "Set-Cookie": "JSESSIONID=must-not-escape; Path=/; Secure",
        },
    )


def _pdf_document(
    fixture: PaperFixture,
    *,
    author_override: str | None = None,
) -> police._PdfDocument:
    volume, issue = (int(value) for value in fixture.issue_id.split("_"))
    authors = [
        police._author_name(author)
        for author in fixture.authors
    ]
    if author_override is not None:
        authors[0] = author_override
    first = " ".join(
        (
            "범죄수사학연구",
            str(2025 if volume == 11 else 2026),
            f"제{volume}권 제{issue}호",
            fixture.title,
            *(author for author in authors if author),
            fixture.first_page,
        )
    )
    return police._PdfDocument(
        page_count=int(fixture.final_page) - int(fixture.first_page) + 1,
        first_pages=(first, "둘째 페이지", "셋째 페이지"),
        last_page=fixture.final_page,
    )


def _resolver(http: FakeHttp, **changes: object) -> PoliceUniversityPublicPdfResolver:
    return PoliceUniversityPublicPdfResolver(
        PoliceResolverConfig(**changes),
        http=http,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("fixture", KNOWN, ids=lambda item: item.source_id)
def test_all_seven_audited_rows_resolve_from_exact_same_row_form_values(
    fixture: PaperFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[_result_page(fixture), _pdf_reply()],
    )
    monkeypatch.setattr(
        police,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(fixture),
    )

    result = _resolver(http).resolve(_paper(fixture))

    assert result.status == "resolved"
    assert result.resolved is True
    assert result.reason == "official_police_board_exact_pdf_identity"
    assert result.resolver == POLICE_RESOLVER_NAME
    assert result.matched_title == fixture.title
    assert result.matched_authors == tuple(
        police._author_name(author) for author in fixture.authors
    )
    assert result.pdf_bytes == b"%PDF-1.7\nfixture-pdf"
    assert result.usage_notice == POLICE_INTERNAL_USE_NOTICE
    assert result.candidate is not None
    assert result.candidate.url == POLICE_DOWNLOAD_URL
    assert result.candidate.landing_url == POLICE_BOARD_URL
    assert result.candidate.license is None
    assert result.candidate.verified_doi is None
    assert result.candidate.verified_source_id is None
    assert result.fetch is not None
    assert not any(
        key.casefold() == "set-cookie"
        for key in result.fetch.headers
    )

    search_call = http.calls[2]
    assert search_call[:2] == ("POST", POLICE_BOARD_URL)
    assert search_call[2] == {
        "searchKey": "T",
        "searchKeyword": fixture.title,
        "type": "",
        "pageIndex": "",
    }
    download_call = http.calls[3]
    filename = _article_filename(fixture)
    assert download_call[:2] == ("POST", POLICE_DOWNLOAD_URL)
    assert download_call[2] == {
        "fnm": "fixture-900001.pdf",
        "orgFnm": quote(filename, safe="-_.!~*'()"),
        "path": "/pds/",
        "articleSeq": "900001",
    }
    for _method, _url, _data, _limit, headers in http.calls:
        assert headers is not None
        assert not any(
            key.casefold() in {"cookie", "authorization", "proxy-authorization"}
            for key in headers
        )


@pytest.mark.parametrize("fixture", ABSENT, ids=lambda item: item.source_id)
def test_three_audited_unpublished_rows_cleanly_return_not_found(
    fixture: PaperFixture,
) -> None:
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[_result_page(None)],
    )

    result = _resolver(http, max_title_queries=1).resolve(_paper(fixture))

    assert result.status == "not_found"
    assert result.reason == "exact_police_article_not_found"
    assert result.resolved is False
    assert [call[0] for call in http.calls] == ["GET", "GET", "POST"]
    assert not http.posts


def test_filename_without_issue_uses_exact_volume_issue_from_same_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = KNOWN[0]
    filename = f"1. 심지연 외_{fixture.title}.pdf"
    row_text = f"제11권 제1호 {filename}"
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[
            _result_page(
                fixture,
                filename=filename,
                row_text=row_text,
            ),
            _pdf_reply(),
        ],
    )
    monkeypatch.setattr(
        police,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(fixture),
    )

    result = _resolver(http).resolve(_paper(fixture))

    assert result.status == "resolved"
    assert result.matched_title == fixture.title
    assert result.attachment_filename == filename


def test_filename_without_issue_rejects_conflicting_same_row_issue() -> None:
    fixture = KNOWN[0]
    filename = f"1. 심지연 외_{fixture.title}.pdf"
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[
            _result_page(
                fixture,
                filename=filename,
                row_text=f"제11권 제2호 {filename}",
            )
        ],
    )

    result = _resolver(http).resolve(_paper(fixture))

    assert result.status == "identity_mismatch"
    assert result.reason == "police_attachment_volume_issue_mismatch"
    assert len(http.calls) == 3


def test_filename_without_issue_never_borrows_issue_from_another_row() -> None:
    fixture = KNOWN[0]
    filename = f"1. 심지연 외_{fixture.title}.pdf"
    search_page = Reply(
        body=(
            "<!doctype html><html><body><table>"
            + _row(
                filename=filename,
                row_text=filename,
                path="/pds/",
                article_seq="900001",
            )
            + "<tr><td>제11권 제1호</td></tr>"
            + "</table></body></html>"
        ).encode(),
    )
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[search_page],
    )

    result = _resolver(http, max_title_queries=1).resolve(_paper(fixture))

    assert result.status == "not_found"
    assert result.reason == "exact_police_article_not_found"
    assert len(http.calls) == 3


def test_filename_and_same_row_issue_conflict_fails_closed() -> None:
    fixture = KNOWN[0]
    filename = _article_filename(fixture)
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[
            _result_page(
                fixture,
                filename=filename,
                row_text=f"제11권 제2호 {fixture.title}",
            )
        ],
    )

    result = _resolver(http).resolve(_paper(fixture))

    assert result.status == "identity_mismatch"
    assert result.reason == "police_attachment_volume_issue_mismatch"
    assert len(http.calls) == 3


def test_explicit_subtitle_prefix_is_search_fallback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = KNOWN[4]
    main_title = "외국에서 수집한 디지털증거의 증거능력 연구"
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[
            _result_page(None),
            _result_page(fixture),
            _pdf_reply(),
        ],
    )
    monkeypatch.setattr(
        police,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(fixture),
    )

    result = _resolver(http).resolve(_paper(fixture))

    assert result.status == "resolved"
    assert result.matched_title == fixture.title
    search_calls = [
        call
        for call in http.calls
        if call[0] == "POST" and call[1] == POLICE_BOARD_URL
    ]
    assert [call[2]["searchKeyword"] for call in search_calls] == [
        fixture.title,
        main_title,
    ]


def test_subtitle_prefix_search_does_not_relax_attachment_title_match() -> None:
    fixture = KNOWN[4]
    unrelated_filename = (
        "『제11권 제3호(통권 제24호) 9. 전혀 관련 없는 논문 제목.pdf"
    )
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[
            _result_page(None),
            _result_page(
                fixture,
                filename=unrelated_filename,
                row_text=f"제11권 제3호 {unrelated_filename}",
            ),
        ],
    )

    result = _resolver(http, max_title_queries=2).resolve(_paper(fixture))

    assert result.status == "not_found"
    assert result.reason == "exact_police_article_not_found"
    assert [call[0] for call in http.calls] == [
        "GET",
        "GET",
        "POST",
        "POST",
    ]


def test_audit_constants_cover_the_ten_selected_db_rows() -> None:
    assert POLICE_AUDITED_RESOLVABLE_SOURCE_IDS == {
        fixture.source_id for fixture in KNOWN
    }
    assert POLICE_AUDITED_ABSENT_SOURCE_IDS == {
        fixture.source_id for fixture in ABSENT
    }
    assert not (
        POLICE_AUDITED_RESOLVABLE_SOURCE_IDS
        & POLICE_AUDITED_ABSENT_SOURCE_IDS
    )


def test_googlebot_only_disallow_does_not_apply_to_our_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = KNOWN[0]
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[_result_page(fixture), _pdf_reply()],
    )
    monkeypatch.setattr(
        police,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(fixture),
    )

    assert _resolver(http).resolve(_paper(fixture)).resolved is True


def test_wildcard_robots_download_disallow_fails_closed_before_board_get() -> None:
    http = FakeHttp(
        gets=[
            _robots(
                "User-agent: *",
                "Allow: /police/police/master/208/boardList.do",
                "Disallow: /police/common/fileDown.do",
            )
        ],
        posts=[],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "robots_disallowed"
    assert result.reason == "police_download_disallowed_by_robots"
    assert len(http.calls) == 1


def test_search_form_must_be_exact_fixed_https_post() -> None:
    http = FakeHttp(
        gets=[
            _robots(),
            _board_form(action="https://evil.example/search"),
        ],
        posts=[],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "exact_police_search_form_not_found"
    assert not http.posts


def test_foreign_or_redirected_search_response_is_rejected_without_download() -> None:
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[
            _result_page(
                KNOWN[0],
                final_url="https://evil.example/result",
                redirects=("https://evil.example/result",),
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "police_response_url_or_redirect_mismatch"
    assert result.error_code == "unsafe_redirect"
    assert len(http.calls) == 3


def test_same_row_values_reject_unsafe_path_before_download() -> None:
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[_result_page(KNOWN[0], path="../../private/")],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "police_attachment_form_values_unsafe"
    assert result.error_code == "unsafe_form_value"
    assert len(http.calls) == 3


def test_two_distinct_exact_same_title_attachments_are_ambiguous() -> None:
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[
            _result_page(
                KNOWN[0],
                duplicate_article_seq="900002",
            )
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "multiple_exact_police_attachments"
    assert result.error_code == "ambiguous_match"
    assert len(http.calls) == 3


def test_pdf_author_identity_mismatch_is_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = KNOWN[0]
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[_result_page(fixture), _pdf_reply()],
    )
    monkeypatch.setattr(
        police,
        "_read_pdf_document",
        lambda body, *, max_pages, max_text_chars: _pdf_document(
            fixture,
            author_override="다른저자",
        ),
    )

    result = _resolver(http).resolve(_paper(fixture))

    assert result.status == "identity_mismatch"
    assert result.reason == "police_pdf_author_mismatch"
    assert result.fetch is None


def test_non_pdf_magic_is_rejected_even_with_pdf_mime() -> None:
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[
            _result_page(KNOWN[0]),
            Reply(
                body=b"<html>not a pdf</html>",
                content_type="application/pdf",
            ),
        ],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_pdf"
    assert result.reason == "police_attachment_is_not_a_safe_parseable_pdf"


def test_retryable_search_failure_remains_retryable() -> None:
    http = FakeHttp(
        gets=[_robots(), _board_form()],
        posts=[RetryableHttpError("temporary")],
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "retryable_error"
    assert result.error_code == "retryable_error"


def test_unsupported_paper_never_touches_network() -> None:
    http = FakeHttp(gets=[], posts=[])

    result = _resolver(http).resolve(
        _paper(institution_id="다른 기관")
    )

    assert result.status == "unsupported"
    assert result.reason == "institution_not_police_research_institute"
    assert http.calls == []


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"requests_per_second": 0}, "requests_per_second"),
        ({"max_pdf_bytes": 0}, "max_pdf_bytes"),
        ({"max_title_queries": True}, "max_title_queries"),
    ],
)
def test_config_bounds_are_validated(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PoliceUniversityPublicPdfResolver(
            PoliceResolverConfig(**changes)  # type: ignore[arg-type]
        )


def test_production_transport_builds_fresh_redirect_free_clients_with_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, object]] = []
    requested: list[str] = []

    class DummyClient:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

        def get_bytes(
            self,
            url: str,
            *,
            max_bytes: int,
            headers: Mapping[str, str] | None = None,
        ) -> HttpFetch:
            requested.append(url)
            return HttpFetch(url, url, 200, {}, b"ok", ())

    monkeypatch.setattr(police, "SafeHttpClient", DummyClient)
    gate = lambda url: None
    transport = police._StatelessPoliceHttp(
        connect_timeout=3,
        read_timeout=4,
        request_gate=gate,
    )

    transport.get_bytes(POLICE_ROBOTS_URL, max_bytes=10)
    transport.get_bytes(POLICE_BOARD_URL, max_bytes=20)

    assert requested == [POLICE_ROBOTS_URL, POLICE_BOARD_URL]
    assert len(built) == 2
    assert all(item["max_redirects"] == 0 for item in built)
    assert all(item["retries"] == 0 for item in built)
    assert all(item["allow_http"] is False for item in built)
    assert all(item["request_gate"] is gate for item in built)
