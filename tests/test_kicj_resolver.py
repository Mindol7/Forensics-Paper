from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace

import pytest

from PaperPipeline.http_client import HttpFetch, RetryableHttpError
from PaperPipeline.kicj_resolver import (
    KICJ_ARCHIVE_URL,
    KICJ_RESOLVER_NAME,
    KICJ_ROBOTS_URL,
    KicjPublicPdfResolver,
    KicjResolverConfig,
)
from PaperPipeline.records import PaperCandidate


TITLE = "디지털 증거 압수수색에서 피의자의 참여권 보장 법리에 대한 비판적 고찰"
ENGLISH_TITLE = (
    "A Critical Look at the Jurisprudence of the Suspect's Right to "
    "Participate in a Seizure of Digital Evidence"
)
DETAIL_URL = (
    "https://www.kicj.re.kr/board.es"
    "?mid=a10103010000&bid=0003&act=view&list_no=13698"
)
DETAIL_PAGE_2_URL = DETAIL_URL + "&nPage=2"
PAGE_2_URL = (
    "https://www.kicj.re.kr/board.es"
    "?mid=a10103010000&bid=0003&nPage=2"
)
PDF_URL = (
    "https://www.kicj.re.kr/boardDownload.es"
    "?bid=0003&list_no=13698&seq=7"
)


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    requested_url: str | None = None
    final_url: str | None = None
    redirects: tuple[str, ...] = ()


class FakeHttp:
    def __init__(self, replies: list[Reply | BaseException]) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[str, int, dict[str, str] | None]] = []

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> HttpFetch:
        self.calls.append((url, max_bytes, headers))
        value = self.replies.popleft()
        if isinstance(value, BaseException):
            raise value
        return HttpFetch(
            requested_url=value.requested_url or url,
            final_url=value.final_url or url,
            status_code=value.status,
            headers={"Content-Type": value.content_type},
            body=value.body,
            redirects=value.redirects,
        )


def _paper(**changes: object) -> PaperCandidate:
    values: dict[str, object] = {
        "database_id": 240,
        "source": "kci",
        "source_id": "ART003007915",
        "title": TITLE,
        "doi_original": None,
        "doi": None,
        "landing_url": "https://www.kci.go.kr/article/ART003007915",
        "abstract": None,
        "publication_year": 2023,
        "first_page": "193",
        "final_page": "224",
        "alternate_titles": (ENGLISH_TITLE,),
        "journal_id": "형사정책연구",
        "institution_id": "한국형사법무정책연구원",
        "issue_id": "34_3",
        # KCI sometimes projects only the first author.  The official row may
        # contain additional authors, but the projected author must be exact.
        "authors": ("이관희(고려대학교)",),
    }
    values.update(changes)
    return PaperCandidate(**values)  # type: ignore[arg-type]


def _robots(*lines: str) -> Reply:
    body = "\n".join(lines or ("User-agent: *", "Allow: /")).encode()
    return Reply(body=body, content_type="text/plain; charset=utf-8")


def _archive_page(
    *,
    issue_label: str | None = "형사정책연구 2023년 제135호",
    detail_href: str = (
        "/board.es?mid=a10103010000&bid=0003&act=view&list_no=13698"
    ),
    pagination: str = "",
) -> Reply:
    issue = (
        ""
        if issue_label is None
        else f'<a class="label" href="{detail_href}">{issue_label}</a>'
    )
    return Reply(
        body=(
            "<!doctype html><html><body>"
            f"{issue}{pagination}"
            "</body></html>"
        ).encode()
    )


def _detail_page(
    *,
    issue_label: str = "형사정책연구 2023년 제135호",
    title: str = TITLE,
    authors: str = "이관희 · 이상진",
    download_anchors: str = (
        '<a class="btn_line" '
        'href="/boardDownload.es?bid=0003&amp;list_no=13698&amp;seq=7">'
        "다운로드<i></i></a>"
    ),
) -> Reply:
    return Reply(
        body=f"""
        <!doctype html>
        <html>
          <head><title>{issue_label} | KICJ</title></head>
          <body>
            <table>
              <caption>{issue_label}에 대한 제목,저자,다운로드 테이블</caption>
              <tbody>
                <tr>
                  <td class="pre txt-left">{title}</td>
                  <td class="pre">{authors}</td>
                  <td>{download_anchors}</td>
                </tr>
                <tr>
                  <td>(통합본) {issue_label}</td>
                  <td>KICJ</td>
                  <td>
                    <a href="/boardDownload.es?bid=0003&amp;list_no=13698&amp;seq=13">
                      다운로드
                    </a>
                  </td>
                </tr>
              </tbody>
            </table>
          </body>
        </html>
        """.encode()
    )


def _resolver(http: FakeHttp, **config: int) -> KicjPublicPdfResolver:
    return KicjPublicPdfResolver(
        KicjResolverConfig(**config),
        http=http,  # type: ignore[arg-type]
    )


def test_resolves_only_explicit_official_issue_row_and_keeps_doi_unverified() -> None:
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(),
        ]
    )
    resolver = _resolver(
        http,
        max_robots_bytes=12_345,
        max_archive_response_bytes=234_567,
        max_detail_response_bytes=345_678,
        max_archive_pages=2,
    )

    result = resolver.resolve(_paper())

    assert result.status == "resolved"
    assert result.resolved is True
    assert result.reason == "official_kicj_issue_exact_identity"
    assert result.detail_url == DETAIL_URL
    assert result.matched_title == TITLE
    assert result.matched_authors == ("이관희", "이상진")
    assert result.archive_pages_checked == 1
    assert result.exact_title_matches == 1
    assert result.author_matches == 1
    assert result.expected_year == 2023
    assert result.expected_volume == 34
    assert result.expected_issue == 3
    assert result.expected_serial_issue == 135
    assert result.expected_first_page == 193
    assert result.expected_final_page == 224
    assert result.candidate is not None
    assert result.candidate.url == PDF_URL
    assert result.candidate.landing_url == DETAIL_URL
    assert result.candidate.resolver == KICJ_RESOLVER_NAME
    assert result.candidate.is_open_access is True
    # The issue HTML has no DOI field beside the per-article link.
    assert result.candidate.verified_doi is None

    assert http.calls == [
        (
            KICJ_ROBOTS_URL,
            12_345,
            {"Accept": "text/plain;q=1.0"},
        ),
        (
            KICJ_ARCHIVE_URL,
            234_567,
            {"Accept": "text/html,application/xhtml+xml;q=0.9"},
        ),
        (
            DETAIL_URL,
            345_678,
            {"Accept": "text/html,application/xhtml+xml;q=0.9"},
        ),
    ]
    for _url, _limit, headers in http.calls:
        assert headers is not None
        assert {key.casefold() for key in headers} == {"accept"}


def test_doi_encoded_year_issue_and_first_page_are_combined_with_page_identity() -> None:
    title = "클라우드 압수ㆍ수색의 집행기술 적법성"
    paper = _paper(
        database_id=459,
        source_id="ART003294967",
        title=title,
        alternate_titles=("A Review of the Legality of Cloud Search and Seizure",),
        doi_original="http://dx.doi.org/10.36889/KCR.2025.12.31.4.1",
        doi="10.36889/KCR.2025.12.31.4.1",
        publication_year=2025,
        first_page="1",
        final_page="24",
        issue_id="36_4",
        authors=(
            "고상협(성균관대학교)",
            "이경주(성균관대학교)",
            "김기범(성균관대학교)",
        ),
    )
    issue_label = "형사정책연구 제36권 제4호(통권 제144호, 2025년 겨울호)"
    http = FakeHttp(
        [
            _robots(),
            _archive_page(issue_label=issue_label),
            _detail_page(
                issue_label=issue_label,
                title=title,
                authors="고상협 · 이경주 · 김기범",
                download_anchors=(
                    '<a href="/boardDownload.es?bid=0003&amp;'
                    'list_no=13698&amp;seq=1">다운로드</a>'
                ),
            ),
        ]
    )

    result = _resolver(http).resolve(paper)

    assert result.resolved
    assert result.doi == "10.36889/KCR.2025.12.31.4.1"
    assert result.expected_serial_issue == 144
    assert result.candidate is not None
    assert result.candidate.url.endswith(
        "boardDownload.es?bid=0003&list_no=13698&seq=1"
    )


def test_archive_issue_label_accepts_official_season_format_without_year_suffix() -> None:
    issue_label = '"형사정책연구" 제34권 제3호(통권 제135호, 2023·가을호)'
    http = FakeHttp(
        [
            _robots(),
            _archive_page(issue_label=issue_label),
            _detail_page(issue_label=issue_label),
        ]
    )

    result = _resolver(http).resolve(_paper())

    assert result.resolved
    assert result.expected_serial_issue == 135


def test_follows_only_an_explicit_official_pagination_anchor() -> None:
    pagination = (
        '<a href="/board.es?mid=a10103010000&amp;bid=0003&amp;nPage=2">'
        "2</a>"
    )
    detail_href = (
        "/board.es?mid=a10103010000&bid=0003&act=view"
        "&list_no=13698&nPage=2"
    )
    http = FakeHttp(
        [
            _robots(),
            _archive_page(issue_label=None, pagination=pagination),
            _archive_page(detail_href=detail_href),
            _detail_page(),
        ]
    )

    result = _resolver(http, max_archive_pages=2).resolve(_paper())

    assert result.resolved
    assert result.archive_pages_checked == 2
    assert result.detail_url == DETAIL_PAGE_2_URL
    assert [call[0] for call in http.calls] == [
        KICJ_ROBOTS_URL,
        KICJ_ARCHIVE_URL,
        PAGE_2_URL,
        DETAIL_PAGE_2_URL,
    ]


def test_void_elements_in_earlier_anchors_do_not_swallow_archive_links() -> None:
    pagination = """
      <a href="/cover"><img src="/cover.png" alt="cover"></a>
      <a href="/board.es?mid=a10103010000&amp;bid=0003&amp;nPage=2">2</a>
    """
    http = FakeHttp(
        [
            _robots(),
            _archive_page(issue_label=None, pagination=pagination),
            _archive_page(),
            _detail_page(),
        ]
    )

    result = _resolver(http, max_archive_pages=2).resolve(_paper())

    assert result.resolved
    assert result.archive_pages_checked == 2
    assert [call[0] for call in http.calls][2] == PAGE_2_URL


def test_does_not_guess_a_second_archive_page_when_no_link_exposes_it() -> None:
    http = FakeHttp([_robots(), _archive_page(issue_label=None)])

    result = _resolver(http, max_archive_pages=4).resolve(_paper())

    assert result.status == "no_match"
    assert result.reason == "issue_not_in_public_kicj_archive"
    assert [call[0] for call in http.calls] == [
        KICJ_ROBOTS_URL,
        KICJ_ARCHIVE_URL,
    ]


@pytest.mark.parametrize(
    ("robots", "reason"),
    [
        (
            _robots("User-agent: *", "Allow: /", "Disallow: /board.es"),
            "kicj_archive_disallowed_by_robots",
        ),
        (
            _robots("User-agent: *", "Disallow:"),
            "kicj_robots_missing_explicit_allow",
        ),
        (
            _robots(
                "User-agent: *",
                "Allow: /",
                "User-agent: Forensics-Paper-KICJ-Resolver",
                "Allow: /",
                "Disallow: /board.es",
            ),
            "kicj_archive_disallowed_by_robots",
        ),
    ],
)
def test_robots_policy_is_explicit_and_fail_closed(
    robots: Reply,
    reason: str,
) -> None:
    http = FakeHttp([robots])

    result = _resolver(http).resolve(_paper())

    assert result.status == "robots_disallowed"
    assert result.reason == reason
    assert result.error_code == "robots_disallowed"
    assert [call[0] for call in http.calls] == [KICJ_ROBOTS_URL]


@pytest.mark.parametrize(
    ("changes", "status", "reason"),
    [
        (
            {"journal_id": "형사정책"},
            "not_applicable",
            "unsupported_kicj_journal",
        ),
        (
            {"institution_id": "다른 기관"},
            "not_applicable",
            "unsupported_kicj_institution",
        ),
        (
            {"title": None, "alternate_titles": ()},
            "missing_metadata",
            "missing_kicj_title",
        ),
        (
            {"publication_year": None},
            "missing_metadata",
            "missing_or_invalid_kicj_year",
        ),
        (
            {"issue_id": "34-3"},
            "missing_metadata",
            "missing_or_invalid_kicj_issue_id",
        ),
        (
            {"publication_year": 2022},
            "identity_mismatch",
            "kicj_year_volume_mismatch",
        ),
        (
            {"first_page": None},
            "missing_metadata",
            "missing_or_invalid_kicj_pages",
        ),
        (
            {"first_page": "225", "final_page": "224"},
            "identity_mismatch",
            "invalid_kicj_page_range",
        ),
        (
            {"authors": ()},
            "missing_metadata",
            "missing_kicj_authors",
        ),
    ],
)
def test_ineligible_or_incomplete_papers_never_reach_the_network(
    changes: dict[str, object],
    status: str,
    reason: str,
) -> None:
    http = FakeHttp([])

    result = _resolver(http).resolve(_paper(**changes))

    assert result.status == status
    assert result.reason == reason
    assert not http.calls


@pytest.mark.parametrize(
    ("doi", "reason"),
    [
        ("not-a-doi", "invalid_kicj_doi"),
        ("10.5555/not-kicj", "unsupported_kicj_doi_format"),
        ("10.36889/KCR.2024.9.30.3.193", "kicj_doi_year_mismatch"),
        ("10.36889/KCR.2023.9.30.4.193", "kicj_doi_issue_mismatch"),
        ("10.36889/KCR.2023.2.31.3.193", "invalid_kicj_doi_date"),
    ],
)
def test_doi_disagreement_stops_before_archive_lookup(
    doi: str,
    reason: str,
) -> None:
    http = FakeHttp([])

    result = _resolver(http).resolve(
        _paper(doi=doi, doi_original=doi)
    )

    assert result.status == "identity_mismatch"
    assert result.reason == reason
    assert not http.calls


def test_doi_page_component_does_not_override_official_article_identity() -> None:
    """KCI page projections can disagree with the official DOI/PDF."""

    doi = "10.36889/KCR.2023.9.30.3.194"
    http = FakeHttp([_robots(), _archive_page(), _detail_page()])

    result = _resolver(http).resolve(
        _paper(doi=doi, doi_original=doi)
    )

    assert result.status == "resolved"
    assert result.doi == doi
    assert result.expected_first_page == 193
    assert result.candidate is not None


def test_title_comparison_ignores_only_unicode_whitespace_not_punctuation() -> None:
    whitespace_variant = (
        "디지털 증거 압수수색에서 피의자의 참여권 보장 법리에 대한 "
        "비판적   고찰"
    )
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(title=whitespace_variant),
        ]
    )
    assert _resolver(http).resolve(_paper()).resolved

    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(title=TITLE + "!"),
        ]
    )
    result = _resolver(http).resolve(_paper())
    assert result.status == "no_match"
    assert result.reason == "kicj_exact_title_not_found"


def test_official_main_title_only_row_is_verified_against_full_pdf_later() -> None:
    full_title = "전자정보의 압수수색과 피의자의 참여권 -대법원 입장의 비판적 수용-"
    main_title = "전자정보의 압수수색과 피의자의 참여권"
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(title=main_title),
        ]
    )

    result = _resolver(http).resolve(_paper(title=full_title))

    assert result.resolved
    assert result.matched_title == main_title


def test_official_title_prefix_requires_an_explicit_subtitle_delimiter() -> None:
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(title=TITLE[:-2]),
        ]
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "no_match"
    assert result.reason == "kicj_exact_title_not_found"


def test_korean_middle_dot_glyphs_are_title_equivalent() -> None:
    title_with_ararea = "클라우드 압수ㆍ수색의 집행기술 적법성"
    title_with_middle_dot = "클라우드 압수·수색의 집행기술 적법성"
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(title=title_with_middle_dot),
        ]
    )

    result = _resolver(http).resolve(_paper(title=title_with_ararea))

    assert result.resolved
    assert result.matched_title == title_with_middle_dot


def test_projected_authors_must_be_an_ordered_exact_subset_of_official_authors() -> None:
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(authors="다른사람 · 이상진"),
        ]
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "kicj_author_mismatch"
    assert result.candidate is None


@pytest.mark.parametrize(
    "download_anchors",
    [
        (
            '<a href="https://evil.example/boardDownload.es?'
            'bid=0003&amp;list_no=13698&amp;seq=7">다운로드</a>'
        ),
        (
            '<a href="/boardDownload.es?bid=0003&amp;'
            'list_no=99999&amp;seq=7">다운로드</a>'
        ),
        (
            '<a href="/boardDownload.es?bid=0003&amp;'
            'list_no=13698&amp;seq=7&amp;token=secret">다운로드</a>'
        ),
        '<a href="javascript:download(7)">다운로드</a>',
    ],
)
def test_unsafe_or_unadvertised_download_url_is_rejected(
    download_anchors: str,
) -> None:
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(download_anchors=download_anchors),
        ]
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "unsafe_kicj_pdf_link"
    assert result.error_code == "unsafe_url"
    assert result.candidate is None
    assert all("evil.example" not in call[0] for call in http.calls)


def test_multiple_distinct_download_links_are_ambiguous() -> None:
    anchors = """
      <a href="/boardDownload.es?bid=0003&amp;list_no=13698&amp;seq=7">
        다운로드
      </a>
      <a href="/boardDownload.es?bid=0003&amp;list_no=13698&amp;seq=8">
        다운로드
      </a>
    """
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(download_anchors=anchors),
        ]
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_kicj_pdf_links"
    assert result.candidate is None


def test_issue_identity_must_be_repeated_by_the_detail_page() -> None:
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(issue_label="형사정책연구 2023년 제134호"),
        ]
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "identity_mismatch"
    assert result.reason == "kicj_detail_issue_identity_mismatch"


def test_redirect_or_changed_final_url_is_rejected_before_parsing() -> None:
    http = FakeHttp(
        [
            _robots(),
            Reply(
                body=_archive_page().body,
                final_url="https://evil.example/archive",
                redirects=("https://evil.example/archive",),
            ),
        ]
    )

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "kicj-archive-1_redirect_or_url_change_rejected"
    assert result.error_code == "redirect_not_allowed"


def test_bounded_response_and_transport_errors_are_auditable() -> None:
    oversized = _archive_page()
    http = FakeHttp(
        [
            _robots(),
            oversized,
        ]
    )
    result = _resolver(
        http,
        max_archive_response_bytes=len(oversized.body) - 1,
    ).resolve(_paper())
    assert result.status == "invalid_response"
    assert result.error_code == "too_large"

    http = FakeHttp(
        [
            _robots(),
            RetryableHttpError("temporary"),
        ]
    )
    result = _resolver(http).resolve(_paper())
    assert result.status == "request_error"
    assert result.reason == "kicj-archive-1_request_failed"
    assert result.error_code == "retryable_error"


def test_valid_robots_policy_is_cached_without_retaining_http_cookies() -> None:
    http = FakeHttp(
        [
            _robots(),
            _archive_page(),
            _detail_page(),
            _archive_page(),
            _detail_page(),
        ]
    )
    resolver = _resolver(http)

    assert resolver.resolve(_paper()).resolved
    assert resolver.resolve(replace(_paper(), database_id=241)).resolved

    assert [call[0] for call in http.calls].count(KICJ_ROBOTS_URL) == 1
    assert len(http.calls) == 5
    assert all(
        "cookie" not in {key.casefold() for key in (headers or {})}
        for _url, _limit, headers in http.calls
    )
