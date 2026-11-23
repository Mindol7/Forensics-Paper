from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import pytest

from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.jiis_resolver import (
    JIIS_RESOLVER_NAME,
    JIIS_SEARCH_URL,
    JiisPublicSearchResolver,
    JiisResolverConfig,
)
from PaperPipeline.records import PaperCandidate


TITLE = "Vision Transformer-based Deepfake Detection Using Multiscale Features"
KOREAN_TITLE = "멀티스케일 특성을 활용한 비전 트랜스포머 기반 딥페이크 탐지에 관한 연구"
DETAIL_URL = "https://jiisonline.org/index.php?mnu=archive&archiveId=1035"


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    final_url: str | None = None
    requested_url: str | None = None
    redirects: tuple[str, ...] = ()


class FakeHttp:
    def __init__(
        self,
        *,
        posts: list[Reply | BaseException],
        gets: list[Reply | BaseException] | None = None,
    ) -> None:
        self.posts = deque(posts)
        self.gets = deque(gets or [])
        self.post_calls: list[
            tuple[str, dict[str, str], int, dict[str, str] | None]
        ] = []
        self.get_calls: list[
            tuple[str, int, dict[str, str] | None]
        ] = []

    def post_form_bytes(
        self,
        url: str,
        *,
        data: dict[str, str],
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        self.post_calls.append((url, data, max_bytes, headers))
        value = self.posts.popleft()
        if isinstance(value, BaseException):
            raise value
        return _fetch(value, requested_url=url)

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        self.get_calls.append((url, max_bytes, headers))
        value = self.gets.popleft()
        if isinstance(value, BaseException):
            raise value
        return _fetch(value, requested_url=url)


def _fetch(reply: Reply, *, requested_url: str) -> HttpFetch:
    return HttpFetch(
        requested_url=reply.requested_url or requested_url,
        final_url=reply.final_url or requested_url,
        status_code=reply.status,
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=reply.body,
        redirects=reply.redirects,
    )


def _paper(
    *,
    title: str | None = KOREAN_TITLE,
    alternate_titles: tuple[str, ...] = (TITLE,),
    doi: str | None = "10.13088/jiis.2024.30.2.275",
    year: int | None = 2024,
    first_page: str | None = "275",
    final_page: str | None = "285",
) -> PaperCandidate:
    return PaperCandidate(
        database_id=1,
        source="kci",
        source_id="ART003095258",
        title=title,
        doi_original=doi,
        doi=doi,
        landing_url="https://www.kci.go.kr/article/ART003095258",
        abstract="abstract",
        publication_year=year,
        first_page=first_page,
        final_page=final_page,
        alternate_titles=alternate_titles,
    )


def _search_result(
    *,
    title: str = TITLE,
    href: str | None = (
        "/index.php?mnu=archive&archiveId=1035&PHPSESSID=search-secret"
    ),
    volume: str = "30",
    issue: str = "2",
    first_page: str = "275",
    final_page: str = "285",
) -> str:
    if href is None:
        title_html = f"<b>{title}</b>"
    else:
        title_html = f'<a href="{href}"><b>{title}</b></a>'
    return f"""
    <tr><td class="dot1 tit">{title_html}</td></tr>
    <tr><td class="text_small2">
      <div>Vol. {volume}, No. {issue}, Page: {first_page} ~ {final_page}</div>
    </td></tr>
    """


def _search_page(*results: str) -> bytes:
    return (
        "<!doctype html><html><body><table>"
        + "".join(results)
        + "</table></body></html>"
    ).encode()


def _detail_page(
    *,
    title: str = TITLE,
    year: str = "2024",
    volume: str = "30",
    issue: str = "2",
    first_page: str = "275",
    final_page: str = "285",
    anchors: str | None = None,
) -> bytes:
    if anchors is None:
        anchors = """
        <a href="/files/DLA/deepfake.pdf?PHPSESSID=detail-secret"
           target="_blank">
          <img title="deepfake.pdf 다운로드" alt="">
          <strong>Full-text Download</strong>
        </a>
        """
    return f"""
    <!doctype html>
    <html><body>
      <div id="printArea">
        <table>
          <tr><td class="text_achive">{title}</td></tr>
          <tr><td>{anchors}</td></tr>
          <tr><td>
            <div>Vol. {volume}, No. {issue},
                 Page: {first_page} ~ {final_page}</div>
            <div>{year}. Cite this article.</div>
          </td></tr>
        </table>
      </div>
      <div>Unrelated archive years: 2023 2024 2025</div>
    </body></html>
    """.encode()


def _one_title_paper(**changes: object) -> PaperCandidate:
    values = {
        "title": TITLE,
        "alternate_titles": (),
    }
    values.update(changes)
    return _paper(**values)  # type: ignore[arg-type]


def test_resolves_exact_official_result_without_trusting_detail_as_doi_evidence() -> None:
    normalized_variant = (
        "  VISION   TRANSFORMER-BASED DEEPFAKE DETECTION "
        "USING MULTISCALE FEATURES  "
    )
    search = Reply(body=_search_page(_search_result(title=normalized_variant)))
    http = FakeHttp(
        posts=[search, search],
        gets=[Reply(body=_detail_page())],
    )
    resolver = JiisPublicSearchResolver(
        JiisResolverConfig(
            max_search_response_bytes=123_456,
            max_detail_response_bytes=234_567,
        ),
        http=http,  # type: ignore[arg-type]
    )

    result = resolver.resolve(_paper())

    assert result.status == "resolved"
    assert result.resolved is True
    assert result.reason == "exact_official_title_bibliography_and_pdf_link"
    assert result.query_count == 2
    assert result.queried_titles == (KOREAN_TITLE, TITLE)
    assert result.detail_url == DETAIL_URL
    assert result.candidate is not None
    assert result.candidate.url == "https://jiisonline.org/files/DLA/deepfake.pdf"
    assert result.candidate.landing_url == DETAIL_URL
    assert result.candidate.resolver == JIIS_RESOLVER_NAME
    assert result.candidate.is_open_access is True
    assert result.candidate.verified_doi is None

    assert [call[0] for call in http.post_calls] == [
        JIIS_SEARCH_URL,
        JIIS_SEARCH_URL,
    ]
    assert [call[1] for call in http.post_calls] == [
        {"kw": KOREAN_TITLE},
        {"kw": TITLE},
    ]
    assert all(call[2] == 123_456 for call in http.post_calls)
    assert all(
        set((call[3] or {}).keys()) == {"Accept"}
        for call in http.post_calls
    )
    assert http.get_calls == [
        (
            DETAIL_URL,
            234_567,
            {"Accept": "text/html,application/xhtml+xml"},
        )
    ]
    assert "PHPSESSID" not in result.candidate.url
    assert "PHPSESSID" not in result.detail_url


@pytest.mark.parametrize(
    ("doi", "reason"),
    [
        (None, "missing_doi"),
        ("10.9999/not-jiis", "unsupported_doi_prefix"),
        ("10.13088/jiis.not-encoded", "unsupported_jiis_doi_format"),
    ],
)
def test_missing_or_non_jiis_doi_is_not_applicable_without_network(
    doi: str | None,
    reason: str,
) -> None:
    http = FakeHttp(posts=[])

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper(doi=doi))

    assert result.status == "not_applicable"
    assert result.reason == reason
    assert not http.post_calls
    assert not http.get_calls


@pytest.mark.parametrize(
    ("change", "value", "reason"),
    [
        ("first_page", "276", "doi_and_paper_first_page_mismatch"),
        ("year", 2023, "doi_and_paper_year_mismatch"),
    ],
)
def test_doi_disagreement_with_db_identity_stops_before_search(
    change: str,
    value: object,
    reason: str,
) -> None:
    http = FakeHttp(posts=[])

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper(**{change: value}))

    assert result.status == "identity_mismatch"
    assert result.reason == reason
    assert not http.post_calls


def test_fuzzy_title_is_never_accepted() -> None:
    http = FakeHttp(
        posts=[
            Reply(
                body=_search_page(
                    _search_result(title=TITLE + ": An Extended Study")
                )
            )
        ]
    )

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "no_match"
    assert result.reason == "title_mismatch"
    assert not http.get_calls


@pytest.mark.parametrize(
    ("search_change", "reason"),
    [
        ({"volume": "31"}, "search_bibliography_mismatch"),
        ({"issue": "3"}, "search_bibliography_mismatch"),
        ({"first_page": "276"}, "search_bibliography_mismatch"),
        ({"final_page": "286"}, "search_bibliography_mismatch"),
    ],
)
def test_search_bibliography_must_match_all_doi_and_db_page_keys(
    search_change: dict[str, str],
    reason: str,
) -> None:
    http = FakeHttp(
        posts=[
            Reply(body=_search_page(_search_result(**search_change)))
        ]
    )

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "no_match"
    assert result.reason == reason
    assert not http.get_calls


@pytest.mark.parametrize(
    ("detail_change", "reason"),
    [
        ({"title": "A Different Exact Title"}, "detail_title_mismatch"),
        ({"year": "2023"}, "detail_year_mismatch"),
        ({"volume": "31"}, "detail_bibliography_mismatch"),
        ({"issue": "3"}, "detail_bibliography_mismatch"),
        ({"first_page": "276"}, "detail_bibliography_mismatch"),
        ({"final_page": "286"}, "detail_bibliography_mismatch"),
    ],
)
def test_detail_identity_mismatches_are_rejected(
    detail_change: dict[str, str],
    reason: str,
) -> None:
    http = FakeHttp(
        posts=[Reply(body=_search_page(_search_result()))],
        gets=[Reply(body=_detail_page(**detail_change))],
    )

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "no_match"
    assert result.reason == reason
    assert result.candidate is None


def test_distinct_exact_bibliographic_detail_urls_are_ambiguous() -> None:
    page = _search_page(
        _search_result(),
        _search_result(
            href="/index.php?mnu=archive&archiveId=9999",
        ),
    )
    http = FakeHttp(posts=[Reply(body=page)])

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_jiis_detail_urls"
    assert not http.get_calls


def test_distinct_explicit_pdf_urls_are_ambiguous() -> None:
    anchors = """
      <a href="/files/DLA/first.pdf">PDF Download</a>
      <a href="/files/DLA/second.pdf">PDF Download</a>
    """
    http = FakeHttp(
        posts=[Reply(body=_search_page(_search_result()))],
        gets=[Reply(body=_detail_page(anchors=anchors))],
    )

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_jiis_pdf_urls"


@pytest.mark.parametrize(
    "href",
    [
        "https://attacker.example/index.php?mnu=archive&archiveId=1035",
        "http://jiisonline.org/index.php?mnu=archive&archiveId=1035",
        "https://jiisonline.org/not-index.php?mnu=archive&archiveId=1035",
        "https://jiisonline.org/index.php?mnu=archive&archiveId=1035&token=x",
    ],
)
def test_unsafe_detail_host_path_or_query_is_rejected(href: str) -> None:
    http = FakeHttp(
        posts=[
            Reply(body=_search_page(_search_result(href=href)))
        ]
    )

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "invalid_response"
    assert result.error_code == "unsafe_url"
    assert not http.get_calls


@pytest.mark.parametrize(
    "href",
    [
        "https://attacker.example/files/DLA/paper.pdf",
        "http://jiisonline.org/files/DLA/paper.pdf",
        "/files/OTHER/paper.pdf",
        "/files/DLA/nested/paper.pdf",
        "/files/DLA/%2e%2e/paper.pdf",
        "/files/DLA/paper.pdf?token=secret",
    ],
)
def test_unsafe_pdf_host_path_or_query_is_rejected(href: str) -> None:
    anchors = f'<a href="{href}">PDF Download</a>'
    http = FakeHttp(
        posts=[Reply(body=_search_page(_search_result()))],
        gets=[Reply(body=_detail_page(anchors=anchors))],
    )

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "invalid_response"
    assert result.error_code == "unsafe_url"
    assert result.candidate is None


@pytest.mark.parametrize(
    "href",
    [
        None,
        "/index.php?mnu=archive",
        "/index.php?archiveId=1035",
        "/index.php?mnu=archive&archiveId=",
    ],
)
def test_missing_archive_key_or_link_is_rejected(href: str | None) -> None:
    http = FakeHttp(
        posts=[Reply(body=_search_page(_search_result(href=href)))]
    )

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "invalid_response"
    assert result.reason == "missing_jiis_detail_key"
    assert not http.get_calls


def test_unmarked_pdf_link_is_not_followed() -> None:
    anchors = '<a href="/files/DLA/paper.pdf">Read article</a>'
    http = FakeHttp(
        posts=[Reply(body=_search_page(_search_result()))],
        gets=[Reply(body=_detail_page(anchors=anchors))],
    )

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "no_match"
    assert result.reason == "missing_explicit_jiis_pdf_download"
    assert result.candidate is None


def test_redirected_response_and_oversized_injected_response_are_rejected() -> None:
    redirected = FakeHttp(
        posts=[
            Reply(
                body=_search_page(_search_result()),
                final_url="https://www.jiisonline.org/index.php?mnu=search",
                redirects=("https://www.jiisonline.org/index.php?mnu=search",),
            )
        ]
    )
    redirected_result = JiisPublicSearchResolver(
        http=redirected,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    oversized = FakeHttp(
        posts=[Reply(body=b"x" * 11)]
    )
    oversized_result = JiisPublicSearchResolver(
        JiisResolverConfig(
            max_search_response_bytes=10,
            max_detail_response_bytes=10,
        ),
        http=oversized,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert redirected_result.status == "invalid_response"
    assert redirected_result.error_code == "redirect_not_allowed"
    assert oversized_result.status == "invalid_response"
    assert oversized_result.error_code == "too_large"


def test_transport_error_is_auditable() -> None:
    http = FakeHttp(posts=[RetryableHttpError("temporary failure")])

    result = JiisPublicSearchResolver(
        http=http,  # type: ignore[arg-type]
    ).resolve(_one_title_paper())

    assert result.status == "request_error"
    assert result.reason == "jiis_search_request_failed"
    assert result.error_code == "retryable_error"
