from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest

from PaperPipeline.http_client import HttpFetch
from PaperPipeline.koreascience_resolver import (
    KOREASCIENCE_RESOLVER_NAME,
    KoreaScienceResolver,
    KoreaScienceResolverConfig,
)
from PaperPipeline.records import PaperCandidate


TITLE_KO = "MITRE ATT&CK 모델을 이용한 사이버 공격 그룹 분류"
TITLE_EN = "Classification of Cyber Attack Groups Using the MITRE ATT&CK Model"
DOI = "10.1234/KOREA.2022.23.6.1"
ARTICLE_ID = "JAKO202209059036672"
ARTICLE_URL = f"https://koreascience.kr/article/{ARTICLE_ID}.page?lang=ko"
PDF_URL = f"https://koreascience.kr/article/{ARTICLE_ID}.pdf"


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    requested_url: str | None = None
    final_url: str | None = None
    redirects: tuple[str, ...] = ()


class FakeHttp:
    def __init__(self, replies: list[Reply]) -> None:
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
        reply = self.replies.popleft()
        return HttpFetch(
            requested_url=reply.requested_url or url,
            final_url=reply.final_url or url,
            status_code=reply.status,
            headers={"Content-Type": reply.content_type},
            body=reply.body,
            redirects=reply.redirects,
        )


def _paper(**changes: object) -> PaperCandidate:
    values: dict[str, object] = {
        "database_id": 1,
        "source": "kci",
        "source_id": "ART002917601",
        "title": TITLE_KO,
        "alternate_titles": (TITLE_EN,),
        "doi_original": DOI,
        "doi": DOI,
        "landing_url": "https://www.kci.go.kr/article/ART002917601",
        "abstract": None,
        "publication_year": 2022,
        "first_page": "1",
        "final_page": "13",
        "journal_id": "인터넷정보학회논문지",
        "institution_id": "한국인터넷정보학회",
        "issue_id": "23_6",
        "authors": ("홍길동",),
    }
    values.update(changes)
    return PaperCandidate(**values)  # type: ignore[arg-type]


def _search_page(*article_urls: str) -> Reply:
    anchors = "".join(
        f'<a class="article" href="{url}">result</a>'
        for url in article_urls
    )
    return Reply(f"<!doctype html><html><body>{anchors}</body></html>".encode())


def _article_page(
    *,
    article_id: str = ARTICLE_ID,
    title_ko: str = TITLE_KO,
    title_en: str = TITLE_EN,
    doi: str | None = DOI,
    year: str = "2022",
    journal_ko: str = "인터넷정보학회논문지",
    journal_en: str = "Journal of Internet Computing and Services",
    first_page: str = "1",
    final_page: str = "13",
    pdf_url: str | None = None,
) -> Reply:
    if pdf_url is None:
        pdf_url = f"https://koreascience.kr:443/article/{article_id}.pdf"
    doi_meta = "" if doi is None else (
        f'<meta name="citation_doi" content="{doi}">'
    )
    pdf_meta = "" if pdf_url == "" else (
        f'<meta name="citation_pdf_url" content="{pdf_url}">'
    )
    return Reply(
        f"""
        <!doctype html>
        <html>
          <head>
            <meta name="citation_title" content="{title_en}">
            <meta name="citation_date" content="{year}">
            <meta name="citation_journal_title" content="{journal_en}">
            <meta name="citation_firstpage" content="{first_page}">
            <meta name="citation_lastpage" content="{final_page}">
            {doi_meta}
            {pdf_meta}
          </head>
          <body>
            <a class="article-title-txt" href="/journal/ABCD.page?lang=ko">
              {journal_ko}<small>({journal_en})</small>
            </a>
            <h1 class="h2 article-title" title="{title_ko}">{title_ko}</h1>
            <a class="pdf" href="/article/{article_id}.pdf">PDF 다운로드</a>
          </body>
        </html>
        """.encode(),
        final_url=(
            f"https://koreascience.kr/article/{article_id}.page?lang=ko"
        ),
    )


def _resolver(
    http: FakeHttp,
    **changes: object,
) -> KoreaScienceResolver:
    return KoreaScienceResolver(
        KoreaScienceResolverConfig(**changes),  # type: ignore[arg-type]
        http=http,
    )


def test_discovers_exact_doi_but_default_gate_withholds_download_candidate() -> None:
    http = FakeHttp(
        [
            _search_page(f"/article/{ARTICLE_ID}.page?lang=ko"),
            _article_page(),
        ]
    )
    resolver = _resolver(
        http,
        max_search_response_bytes=111_111,
        max_article_response_bytes=222_222,
    )

    result = resolver.resolve(_paper())

    assert result.status == "permission_required"
    assert result.reason == (
        "koreascience_pdf_automation_permission_not_confirmed"
    )
    assert result.discovered is True
    assert result.resolved is False
    assert result.candidate is None
    assert result.article_url == ARTICLE_URL
    assert result.pdf_url == PDF_URL
    assert result.article_id == ARTICLE_ID
    assert result.match_basis == "doi_exact"
    assert result.matched_doi == DOI
    assert result.exact_matches == 1
    # The resolver fetches only HTML.  It never requests PDF bytes itself.
    assert len(http.calls) == 2
    assert all(not urlsplit(call[0]).path.endswith(".pdf") for call in http.calls)

    search_url, search_limit, search_headers = http.calls[0]
    query = parse_qs(urlsplit(search_url).query)
    assert urlsplit(search_url).path == "/search/advanced.page"
    assert query == {
        "pageNo": ["1"],
        "pageSize": ["20"],
        "contentType": ["JOURNAL"],
        "doi": [DOI],
    }
    assert search_limit == 111_111
    assert search_headers == {
        "Accept": "text/html,application/xhtml+xml;q=0.9"
    }
    assert http.calls[1] == (
        ARTICLE_URL,
        222_222,
        {"Accept": "text/html,application/xhtml+xml;q=0.9"},
    )


def test_explicit_permission_releases_candidate_without_fetching_pdf() -> None:
    http = FakeHttp(
        [
            _search_page(f"/article/{ARTICLE_ID}.page?lang=ko"),
            _article_page(),
        ]
    )

    result = _resolver(http, pdf_permission_granted=True).resolve(_paper())

    assert result.resolved is True
    assert result.reason == "exact_koreascience_article_and_explicit_pdf"
    assert result.candidate is not None
    assert result.candidate.url == PDF_URL
    assert result.candidate.landing_url == ARTICLE_URL
    assert result.candidate.resolver == KOREASCIENCE_RESOLVER_NAME
    assert result.candidate.verified_doi == DOI
    # Public visibility is not treated as a licence assertion.
    assert result.candidate.is_open_access is None
    assert len(http.calls) == 2


def test_missing_doi_uses_exact_title_year_journal_and_pages() -> None:
    http = FakeHttp(
        [
            _search_page(f"/article/{ARTICLE_ID}.page?lang=ko"),
            _article_page(doi=None),
        ]
    )
    paper = _paper(doi=None, doi_original=None, alternate_titles=())

    result = _resolver(http, pdf_permission_granted=True).resolve(paper)

    assert result.resolved is True
    assert result.match_basis == "title_year_exact"
    assert result.matched_title == TITLE_KO
    assert result.matched_year == 2022
    assert result.matched_journal == "인터넷정보학회논문지"
    assert result.matched_first_page == "1"
    assert result.matched_final_page == "13"
    assert result.candidate is not None
    assert result.candidate.verified_doi is None

    query = parse_qs(urlsplit(http.calls[0][0]).query)
    assert query == {
        "pageNo": ["1"],
        "pageSize": ["20"],
        "contentType": ["JOURNAL"],
        "keywords": [TITLE_KO],
        "pubYear": ["2022"],
        "jnl": ["인터넷정보학회논문지"],
        "page": ["1-13"],
    }


def test_empty_doi_search_falls_back_to_exact_title_metadata() -> None:
    http = FakeHttp(
        [
            _search_page(),
            _search_page(f"/article/{ARTICLE_ID}.page?lang=ko"),
            _article_page(doi=None),
        ]
    )

    result = _resolver(
        http,
        pdf_permission_granted=True,
        max_search_queries=2,
    ).resolve(_paper())

    assert result.resolved is True
    assert result.match_basis == "title_year_exact"
    assert result.candidate is not None
    assert result.candidate.verified_doi is None
    assert len(http.calls) == 3
    first_query = parse_qs(urlsplit(http.calls[0][0]).query)
    second_query = parse_qs(urlsplit(http.calls[1][0]).query)
    assert first_query["doi"] == [DOI]
    assert "keywords" not in first_query
    assert second_query["keywords"] == [TITLE_KO]
    assert "doi" not in second_query


@pytest.mark.parametrize(
    ("paper_changes", "article_changes"),
    [
        ({}, {"doi": "10.1234/OTHER.ARTICLE"}),
        (
            {"doi": None, "doi_original": None, "alternate_titles": ()},
            {"doi": None, "title_ko": "다른 논문", "title_en": "Other paper"},
        ),
        (
            {"doi": None, "doi_original": None, "alternate_titles": ()},
            {"doi": None, "year": "2021"},
        ),
        (
            {"doi": None, "doi_original": None, "alternate_titles": ()},
            {
                "doi": None,
                "journal_ko": "다른 학술지",
                "journal_en": "Other Journal",
            },
        ),
        (
            {"doi": None, "doi_original": None, "alternate_titles": ()},
            {"doi": None, "first_page": "2"},
        ),
        (
            {"doi": None, "doi_original": None, "alternate_titles": ()},
            {"doi": None, "final_page": "14"},
        ),
    ],
)
def test_identity_mismatches_fail_closed(
    paper_changes: dict[str, object],
    article_changes: dict[str, object],
) -> None:
    http = FakeHttp(
        [
            _search_page(f"/article/{ARTICLE_ID}.page?lang=ko"),
            _article_page(**article_changes),  # type: ignore[arg-type]
        ]
    )

    result = _resolver(http, pdf_permission_granted=True).resolve(
        _paper(**paper_changes)
    )

    assert result.status == "no_match"
    assert result.candidate is None


@pytest.mark.parametrize(
    "pdf_url",
    [
        "https://evil.example/article/JAKO202209059036672.pdf",
        "https://koreascience.kr/files/JAKO202209059036672.pdf",
        "https://koreascience.kr/article/JAKO202209059036673.pdf",
        (
            "https://koreascience.kr/article/JAKO202209059036672.pdf"
            "?sig=secret"
        ),
        "http://koreascience.kr/article/JAKO202209059036672.pdf",
    ],
)
def test_rejects_unsafe_or_unbound_citation_pdf_url(pdf_url: str) -> None:
    http = FakeHttp(
        [
            _search_page(f"/article/{ARTICLE_ID}.page?lang=ko"),
            _article_page(pdf_url=pdf_url),
        ]
    )

    result = _resolver(http, pdf_permission_granted=True).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "unsafe_koreascience_citation_pdf_url"
    assert result.candidate is None


def test_ignores_external_search_links_and_never_follows_them() -> None:
    http = FakeHttp(
        [
            _search_page(
                "https://evil.example/article/JAKO202209059036672.page",
                "/login.page?next=/article/JAKO202209059036672.page",
                "/article/not-an-id.page",
            )
        ]
    )

    result = _resolver(
        http,
        pdf_permission_granted=True,
        max_search_queries=1,
    ).resolve(_paper())

    assert result.status == "no_match"
    assert result.article_pages_checked == 0
    assert len(http.calls) == 1


def test_rejects_same_host_redirect_to_a_different_resource() -> None:
    search = _search_page()
    search_url_placeholder = "https://koreascience.kr/search/advanced.page"
    search = Reply(
        body=search.body,
        requested_url=search_url_placeholder,
        final_url="https://koreascience.kr/login.page",
        redirects=("https://koreascience.kr/login.page",),
    )
    http = FakeHttp([search])

    result = _resolver(http).resolve(_paper())

    assert result.status == "invalid_response"
    assert result.reason == "koreascience_response_url_outside_allowlist"
    assert result.article_pages_checked == 0


def test_multiple_exact_article_pages_are_ambiguous() -> None:
    second_id = "JAKO202209059036673"
    http = FakeHttp(
        [
            _search_page(
                f"/article/{ARTICLE_ID}.page?lang=ko",
                f"/article/{second_id}.page?lang=ko",
            ),
            _article_page(),
            _article_page(article_id=second_id),
        ]
    )

    result = _resolver(http, pdf_permission_granted=True).resolve(_paper())

    assert result.status == "ambiguous"
    assert result.reason == "multiple_exact_koreascience_articles"
    assert result.exact_matches == 2
    assert result.candidate is None


def test_article_traversal_is_hard_bounded() -> None:
    ids = (
        "JAKO202209059036670",
        "JAKO202209059036671",
        "JAKO202209059036672",
    )
    http = FakeHttp(
        [
            _search_page(*(f"/article/{value}.page?lang=ko" for value in ids)),
            _article_page(
                article_id=ids[0],
                doi="10.1234/OTHER.ZERO",
            ),
            _article_page(
                article_id=ids[1],
                doi="10.1234/OTHER.ONE",
            ),
        ]
    )

    result = _resolver(
        http,
        pdf_permission_granted=True,
        max_article_pages=2,
    ).resolve(_paper())

    assert result.status == "no_match"
    assert result.article_pages_checked == 2
    assert len(http.calls) == 3


def test_challenge_and_rate_limit_stop_without_bypass_attempts() -> None:
    challenge_http = FakeHttp(
        [Reply(b'<div class="g-recaptcha"></div>')]
    )
    challenge = _resolver(challenge_http).resolve(_paper())
    assert challenge.status == "access_controlled"
    assert challenge.reason == "koreascience_search_challenge_present"
    assert len(challenge_http.calls) == 1

    limited_http = FakeHttp([Reply(b"slow down", status=429)])
    limited = _resolver(limited_http).resolve(_paper())
    assert limited.status == "http_error"
    assert limited.error_code == "rate_limited"
    assert len(limited_http.calls) == 1


def test_missing_identity_non_kci_and_config_bounds_need_no_network() -> None:
    no_calls = FakeHttp([])
    missing = _resolver(no_calls).resolve(
        _paper(
            doi=None,
            doi_original=None,
            title=None,
            alternate_titles=(),
        )
    )
    assert missing.status == "missing_metadata"
    assert no_calls.calls == []

    other_source = _resolver(no_calls).resolve(_paper(source="other"))
    assert other_source.status == "not_applicable"
    assert no_calls.calls == []

    with pytest.raises(ValueError, match="search_page_size"):
        KoreaScienceResolverConfig(search_page_size=51)
        KoreaScienceResolver(
            KoreaScienceResolverConfig(search_page_size=51),
            http=no_calls,
        )
