"""Conservative discovery of public KoreaScience article and PDF links.

KoreaScience exposes a server-rendered advanced-search page and article pages
with standard ``citation_*`` metadata.  This resolver uses only those public
HTML pages.  It does not log in, accept cookies, solve challenges, enumerate
article identifiers, or fetch PDF bytes.

Discovery and PDF acquisition are deliberately separate.  The resolver can
locate and audit an explicit ``citation_pdf_url`` while
``pdf_permission_granted`` is false, but it returns ``permission_required``
and no :class:`~PaperPipeline.records.ResolverCandidate`.  The switch may be
enabled only after the operator has explicit permission to automate PDF
retrieval; the common downloader can then consume the returned candidate.

Matching fails closed:

* a known DOI must match the article's ``citation_doi`` exactly;
* without a DOI, a known title and publication year must both match exactly;
* journal and printed-page metadata must also agree when both sides expose it;
* only bounded, first-page search results are followed; and
* every URL is restricted to the canonical HTTPS host and the exact
  ``/search/advanced.page`` or ``/article/JAKO...`` path shapes.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    PerHostRateLimiter,
    SafeHttpClient,
    redact_url,
)
from PaperPipeline.identifier_resolver import normalize_title, paper_title_queries
from PaperPipeline.records import PaperCandidate, ResolverCandidate


KOREASCIENCE_ORIGIN = "https://koreascience.kr"
KOREASCIENCE_HOST = "koreascience.kr"
KOREASCIENCE_SEARCH_PATH = "/search/advanced.page"
KOREASCIENCE_SEARCH_URL = f"{KOREASCIENCE_ORIGIN}{KOREASCIENCE_SEARCH_PATH}"
KOREASCIENCE_RESOLVER_NAME = "koreascience-public-html"
KOREASCIENCE_USER_AGENT = (
    "Forensics-Paper-KoreaScience-Discovery/1.0 "
    "(bounded public metadata resolver)"
)

_ARTICLE_ID_RE = re.compile(r"\AJAKO[0-9]{15,24}\Z")
_ARTICLE_PAGE_RE = re.compile(
    r"\A/article/(?P<article>JAKO[0-9]{15,24})[.]page\Z"
)
_ARTICLE_PDF_RE = re.compile(
    r"\A/article/(?P<article>JAKO[0-9]{15,24})[.]pdf\Z",
    re.IGNORECASE,
)
_JOURNAL_PAGE_RE = re.compile(r"\A/journal/[A-Za-z0-9_-]{1,64}[.]page\Z")
_YEAR_RE = re.compile(r"(?<![0-9])(?P<year>(?:19|20)[0-9]{2})(?![0-9])")
_INTERNAL_JOURNAL_ID_RE = re.compile(r"\A[A-Z]{2,12}[0-9]{5,}\Z")
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SEARCH_QUERY_KEYS = frozenset(
    {
        "pageNo",
        "pageSize",
        "keywords",
        "jnl",
        "pubYear",
        "page",
        "doi",
        "contentType",
    }
)


class KoreaScienceHttp(Protocol):
    """The one bounded anonymous operation used for HTML discovery."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Fetch one public search or article HTML page."""


@dataclass(frozen=True, slots=True)
class KoreaScienceResolverConfig:
    """Resource limits and the explicit PDF-automation authority switch."""

    pdf_permission_granted: bool = False
    requests_per_second: float = 0.2
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    http_retries: int = 2
    max_search_response_bytes: int = 2 * 1024 * 1024
    max_article_response_bytes: int = 2 * 1024 * 1024
    search_page_size: int = 20
    max_search_queries: int = 3
    max_article_pages: int = 20
    request_gate: Callable[[str], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class KoreaScienceAttempt:
    """One redacted and auditable discovery request."""

    stage: str
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class KoreaScienceResolution:
    """One exact-identity KoreaScience discovery decision."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = KOREASCIENCE_RESOLVER_NAME
    candidate: ResolverCandidate | None = None
    article_url: str | None = None
    pdf_url: str | None = None
    article_id: str | None = None
    match_basis: str | None = None
    matched_title: str | None = None
    matched_doi: str | None = None
    matched_year: int | None = None
    matched_journal: str | None = None
    matched_first_page: str | None = None
    matched_final_page: str | None = None
    search_queries: tuple[str, ...] = ()
    search_pages_checked: int = 0
    article_pages_checked: int = 0
    exact_matches: int = 0
    attempts: tuple[KoreaScienceAttempt, ...] = ()
    http_status: int | None = None
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.candidate is not None

    @property
    def discovered(self) -> bool:
        """Whether an exact article and explicit safe PDF link were found."""

        return (
            self.status in {"resolved", "permission_required"}
            and self.article_url is not None
            and self.pdf_url is not None
            and self.exact_matches == 1
        )


@dataclass(frozen=True, slots=True)
class _ExpectedIdentity:
    titles: tuple[tuple[str, str], ...]
    doi: str | None
    year: int | None
    journal: str | None
    first_page: str | None
    final_page: str | None


@dataclass(frozen=True, slots=True)
class _ArticleMetadata:
    article_url: str
    article_id: str
    pdf_url: str
    titles: tuple[str, ...]
    doi: str | None
    year: int | None
    journals: tuple[str, ...]
    first_page: str | None
    final_page: str | None


@dataclass(frozen=True, slots=True)
class _ExactMatch:
    metadata: _ArticleMetadata
    match_basis: str
    matched_title: str | None
    matched_journal: str | None


@dataclass(frozen=True, slots=True)
class _Failure:
    status: str
    reason: str
    error_code: str
    http_status: int | None = None


class KoreaScienceResolver:
    """Discover one exact KoreaScience record without downloading its PDF."""

    def __init__(
        self,
        config: KoreaScienceResolverConfig | None = None,
        *,
        http: KoreaScienceHttp | None = None,
    ) -> None:
        self.config = config or KoreaScienceResolverConfig()
        _validate_config(self.config)
        if http is None:
            request_gate = self.config.request_gate
            if request_gate is None:
                request_gate = PerHostRateLimiter(
                    self.config.requests_per_second
                ).wait
            http = SafeHttpClient(
                user_agent=KOREASCIENCE_USER_AGENT,
                connect_timeout=self.config.connect_timeout,
                read_timeout=self.config.read_timeout,
                retries=self.config.http_retries,
                request_gate=request_gate,
            )
        self.http = http

    def resolve(self, paper: PaperCandidate) -> KoreaScienceResolution:
        """Search public HTML and return an exact, permission-gated candidate."""

        identity, failure = _expected_identity(paper)
        if failure is not None:
            return KoreaScienceResolution(
                status=failure.status,
                reason=failure.reason,
                paper=paper,
                error_code=failure.error_code,
            )
        assert identity is not None

        search_queries = _search_urls(
            identity,
            page_size=self.config.search_page_size,
            max_queries=self.config.max_search_queries,
        )
        attempts: list[KoreaScienceAttempt] = []
        article_urls: list[str] = []
        seen_articles: set[str] = set()
        search_pages_checked = 0
        last_http_status: int | None = None

        for query_index, search_url in enumerate(search_queries):
            fetch, request_failure = self._fetch_html(
                stage="koreascience-search",
                url=search_url,
                max_bytes=self.config.max_search_response_bytes,
                attempts=attempts,
            )
            if request_failure is not None:
                return self._failure_resolution(
                    paper,
                    request_failure,
                    attempts=attempts,
                    search_queries=search_queries,
                    search_pages_checked=search_pages_checked,
                    article_pages_checked=0,
                )
            assert fetch is not None
            search_pages_checked += 1
            last_http_status = fetch.status_code

            document = _decode_html(fetch.body)
            if document is None:
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="invalid_response",
                        reason="invalid_koreascience_search_html",
                        error_code="invalid_html",
                        http_status=fetch.status_code,
                    ),
                    attempts=attempts,
                    search_queries=search_queries,
                    search_pages_checked=search_pages_checked,
                    article_pages_checked=0,
                )
            if _is_challenge_page(document):
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="access_controlled",
                        reason="koreascience_search_challenge_present",
                        error_code="access_controlled",
                        http_status=fetch.status_code,
                    ),
                    attempts=attempts,
                    search_queries=search_queries,
                    search_pages_checked=search_pages_checked,
                    article_pages_checked=0,
                )

            parser = _SearchPageParser(base_url=fetch.final_url)
            try:
                parser.feed(document)
                parser.close()
            except (AssertionError, ValueError):
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="invalid_response",
                        reason="invalid_koreascience_search_html",
                        error_code="invalid_html",
                        http_status=fetch.status_code,
                    ),
                    attempts=attempts,
                    search_queries=search_queries,
                    search_pages_checked=search_pages_checked,
                    article_pages_checked=0,
                )
            for article_url in parser.article_urls:
                if article_url in seen_articles:
                    continue
                seen_articles.add(article_url)
                article_urls.append(article_url)
                if len(article_urls) >= self.config.max_article_pages:
                    break
            if len(article_urls) >= self.config.max_article_pages:
                break
            # A DOI query is the preferred identity path.  If KoreaScience
            # returned at least one official article for it, inspect only
            # those bounded results instead of issuing broader title queries.
            if (
                query_index == 0
                and identity.doi is not None
                and parser.article_urls
            ):
                break

        matches: dict[str, _ExactMatch] = {}
        article_pages_checked = 0
        for article_url in article_urls:
            fetch, request_failure = self._fetch_html(
                stage="koreascience-article",
                url=article_url,
                max_bytes=self.config.max_article_response_bytes,
                attempts=attempts,
            )
            if request_failure is not None:
                return self._failure_resolution(
                    paper,
                    request_failure,
                    attempts=attempts,
                    search_queries=search_queries,
                    search_pages_checked=search_pages_checked,
                    article_pages_checked=article_pages_checked,
                )
            assert fetch is not None
            article_pages_checked += 1
            last_http_status = fetch.status_code

            document = _decode_html(fetch.body)
            if document is None:
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="invalid_response",
                        reason="invalid_koreascience_article_html",
                        error_code="invalid_html",
                        http_status=fetch.status_code,
                    ),
                    attempts=attempts,
                    search_queries=search_queries,
                    search_pages_checked=search_pages_checked,
                    article_pages_checked=article_pages_checked,
                )
            if _is_challenge_page(document):
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="access_controlled",
                        reason="koreascience_article_challenge_present",
                        error_code="access_controlled",
                        http_status=fetch.status_code,
                    ),
                    attempts=attempts,
                    search_queries=search_queries,
                    search_pages_checked=search_pages_checked,
                    article_pages_checked=article_pages_checked,
                )

            metadata, metadata_failure = _parse_article_page(
                document,
                article_url=fetch.final_url,
            )
            if metadata_failure is not None:
                if metadata_failure.status == "not_found":
                    continue
                # A malformed exact official article page is not silently
                # skipped: doing so could convert conflicting metadata into a
                # false "no match" result.
                return self._failure_resolution(
                    paper,
                    metadata_failure,
                    attempts=attempts,
                    search_queries=search_queries,
                    search_pages_checked=search_pages_checked,
                    article_pages_checked=article_pages_checked,
                )
            assert metadata is not None
            match = _match_article(identity, metadata)
            if match is not None:
                matches.setdefault(metadata.article_id, match)

        doi_matches = {
            article_id: match
            for article_id, match in matches.items()
            if match.match_basis == "doi_exact"
        }
        preferred_matches = doi_matches or matches
        common = {
            "paper": paper,
            "search_queries": search_queries,
            "search_pages_checked": search_pages_checked,
            "article_pages_checked": article_pages_checked,
            "exact_matches": len(preferred_matches),
            "attempts": tuple(attempts),
            "http_status": last_http_status,
        }
        if not preferred_matches:
            return KoreaScienceResolution(
                status="no_match",
                reason="no_exact_koreascience_article",
                **common,
            )
        if len(preferred_matches) != 1:
            return KoreaScienceResolution(
                status="ambiguous",
                reason="multiple_exact_koreascience_articles",
                error_code="ambiguous",
                **common,
            )

        match = next(iter(preferred_matches.values()))
        metadata = match.metadata
        identity_fields = {
            "article_url": metadata.article_url,
            "pdf_url": metadata.pdf_url,
            "article_id": metadata.article_id,
            "match_basis": match.match_basis,
            "matched_title": match.matched_title,
            "matched_doi": metadata.doi,
            "matched_year": metadata.year,
            "matched_journal": match.matched_journal,
            "matched_first_page": metadata.first_page,
            "matched_final_page": metadata.final_page,
        }
        if not self.config.pdf_permission_granted:
            return KoreaScienceResolution(
                status="permission_required",
                reason="koreascience_pdf_automation_permission_not_confirmed",
                error_code="permission_required",
                **identity_fields,
                **common,
            )

        candidate = ResolverCandidate(
            url=metadata.pdf_url,
            resolver=KOREASCIENCE_RESOLVER_NAME,
            landing_url=metadata.article_url,
            is_open_access=None,
            verified_doi=(
                metadata.doi if match.match_basis == "doi_exact" else None
            ),
        )
        return KoreaScienceResolution(
            status="resolved",
            reason="exact_koreascience_article_and_explicit_pdf",
            candidate=candidate,
            **identity_fields,
            **common,
        )

    def _fetch_html(
        self,
        *,
        stage: str,
        url: str,
        max_bytes: int,
        attempts: list[KoreaScienceAttempt],
    ) -> tuple[HttpFetch | None, _Failure | None]:
        try:
            fetch = self.http.get_bytes(
                url,
                max_bytes=max_bytes,
                headers={
                    "Accept": "text/html,application/xhtml+xml;q=0.9",
                },
            )
        except HttpClientError as exc:
            error_code = getattr(exc, "code", "http_error")
            attempts.append(
                KoreaScienceAttempt(
                    stage=stage,
                    url=redact_url(url),
                    status="request_error",
                    error_code=error_code,
                )
            )
            return None, _Failure(
                status="request_error",
                reason=f"{stage.replace('-', '_')}_request_failed",
                error_code=error_code,
            )

        url_is_valid = (
            _same_allowed_resource(url, fetch.requested_url)
            and _same_allowed_resource(url, fetch.final_url)
            and all(_allowed_resource_url(value) for value in fetch.redirects)
        )
        status = (
            "success"
            if 200 <= fetch.status_code < 300 and url_is_valid
            else "http_error"
            if not 200 <= fetch.status_code < 300
            else "unsafe_redirect"
        )
        attempts.append(
            KoreaScienceAttempt(
                stage=stage,
                url=redact_url(url),
                status=status,
                http_status=fetch.status_code,
                final_url=redact_url(fetch.final_url),
                error_code=None if status == "success" else status,
            )
        )
        if not url_is_valid:
            return None, _Failure(
                status="invalid_response",
                reason="koreascience_response_url_outside_allowlist",
                error_code="unsafe_redirect",
                http_status=fetch.status_code,
            )
        if not 200 <= fetch.status_code < 300:
            if fetch.status_code in {401, 403}:
                return None, _Failure(
                    status="access_controlled",
                    reason="koreascience_html_access_denied",
                    error_code="access_controlled",
                    http_status=fetch.status_code,
                )
            return None, _Failure(
                status="http_error",
                reason="koreascience_html_http_error",
                error_code=(
                    "rate_limited"
                    if fetch.status_code == 429
                    else "retryable_error"
                    if fetch.status_code in _RETRYABLE_STATUSES
                    else "http_error"
                ),
                http_status=fetch.status_code,
            )
        if fetch.content_type not in {"text/html", "application/xhtml+xml"}:
            return None, _Failure(
                status="invalid_response",
                reason="koreascience_response_is_not_html",
                error_code="invalid_content_type",
                http_status=fetch.status_code,
            )
        return fetch, None

    @staticmethod
    def _failure_resolution(
        paper: PaperCandidate,
        failure: _Failure,
        *,
        attempts: list[KoreaScienceAttempt],
        search_queries: tuple[str, ...],
        search_pages_checked: int,
        article_pages_checked: int,
    ) -> KoreaScienceResolution:
        return KoreaScienceResolution(
            status=failure.status,
            reason=failure.reason,
            paper=paper,
            search_queries=search_queries,
            search_pages_checked=search_pages_checked,
            article_pages_checked=article_pages_checked,
            attempts=tuple(attempts),
            http_status=failure.http_status,
            error_code=failure.error_code,
        )


class _SearchPageParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.article_urls: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        href = _attribute(attrs, "href")
        if href is None:
            return
        article_url = _safe_article_url(urljoin(self.base_url, href))
        if article_url is None or article_url in self._seen:
            return
        self._seen.add(article_url)
        self.article_urls.append(article_url)


class _ArticlePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, list[str]] = {}
        self.article_titles: list[str] = []
        self.pdf_hrefs: list[str] = []
        self.journal_labels: list[str] = []
        self._title_depth = 0
        self._journal_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        if self._title_depth:
            self._title_depth += 1
        if self._journal_depth:
            self._journal_depth += 1

        if lowered == "meta":
            name = (_attribute(attrs, "name") or "").strip().casefold()
            content = _attribute(attrs, "content")
            if name.startswith("citation_") and content is not None:
                value = _clean_text(content)
                if value:
                    self.metadata.setdefault(name, []).append(value)
            return

        if lowered == "h1":
            classes = {
                token.casefold()
                for token in (_attribute(attrs, "class") or "").split()
            }
            if "article-title" in classes:
                self._title_depth = 1
                title = _clean_text(_attribute(attrs, "title"))
                if title:
                    self.article_titles.append(title)

        if lowered != "a":
            return
        href = _attribute(attrs, "href")
        if href is None:
            return
        if ".pdf" in href.casefold():
            self.pdf_hrefs.append(href)
        path = urlsplit(urljoin(KOREASCIENCE_ORIGIN, href)).path
        if _JOURNAL_PAGE_RE.fullmatch(path):
            self._journal_depth = 1

    def handle_endtag(self, _tag: str) -> None:
        if self._title_depth:
            self._title_depth -= 1
        if self._journal_depth:
            self._journal_depth -= 1

    def handle_data(self, data: str) -> None:
        value = _clean_text(data)
        if not value:
            return
        if self._title_depth:
            self.article_titles.append(value)
        if self._journal_depth:
            self.journal_labels.append(value.strip("() "))


def _validate_config(config: KoreaScienceResolverConfig) -> None:
    positive_float_fields = {
        "requests_per_second": config.requests_per_second,
        "connect_timeout": config.connect_timeout,
        "read_timeout": config.read_timeout,
    }
    for name, value in positive_float_fields.items():
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    positive_integer_fields = {
        "max_search_response_bytes": config.max_search_response_bytes,
        "max_article_response_bytes": config.max_article_response_bytes,
        "search_page_size": config.search_page_size,
        "max_search_queries": config.max_search_queries,
        "max_article_pages": config.max_article_pages,
    }
    for name, value in positive_integer_fields.items():
        if value < 1:
            raise ValueError(f"{name} must be positive")
    if config.search_page_size > 50:
        raise ValueError("search_page_size must not exceed 50")
    if config.max_search_queries > 10:
        raise ValueError("max_search_queries must not exceed 10")
    if config.max_article_pages > 50:
        raise ValueError("max_article_pages must not exceed 50")
    if config.http_retries < 0:
        raise ValueError("http_retries must be non-negative")


def _expected_identity(
    paper: PaperCandidate,
) -> tuple[_ExpectedIdentity | None, _Failure | None]:
    if paper.source.casefold() != "kci":
        return None, _Failure(
            status="not_applicable",
            reason="not_a_kci_record",
            error_code="not_applicable",
        )

    doi: str | None = None
    if paper.doi:
        try:
            doi = normalize_doi(paper.doi)
        except InvalidDoiError:
            return None, _Failure(
                status="invalid_metadata",
                reason="invalid_paper_doi",
                error_code="invalid_doi",
            )
    titles = paper_title_queries(paper)
    year = _year(paper.publication_year)
    if doi is None and not titles:
        return None, _Failure(
            status="missing_metadata",
            reason="missing_doi_and_title",
            error_code="missing_metadata",
        )
    if doi is None and year is None:
        return None, _Failure(
            status="missing_metadata",
            reason="missing_doi_and_publication_year",
            error_code="missing_metadata",
        )

    journal = _paper_journal(paper.journal_id)
    return (
        _ExpectedIdentity(
            titles=titles,
            doi=doi,
            year=year,
            journal=journal,
            first_page=_normalize_page(paper.first_page),
            final_page=_normalize_page(paper.final_page),
        ),
        None,
    )


def _search_urls(
    identity: _ExpectedIdentity,
    *,
    page_size: int,
    max_queries: int,
) -> tuple[str, ...]:
    queries: list[dict[str, str]] = []
    common = {
        "pageNo": "1",
        "pageSize": str(page_size),
        "contentType": "JOURNAL",
    }
    if identity.doi is not None:
        queries.append({**common, "doi": identity.doi})
    if identity.year is not None:
        for title, _normalized in identity.titles:
            query = {
                **common,
                "keywords": title,
                "pubYear": str(identity.year),
            }
            if identity.journal is not None:
                query["jnl"] = identity.journal
            page_query = _page_query(identity.first_page, identity.final_page)
            if page_query is not None:
                query["page"] = page_query
            queries.append(query)

    return tuple(
        f"{KOREASCIENCE_SEARCH_URL}?{urlencode(query)}"
        for query in queries[:max_queries]
    )


def _parse_article_page(
    document: str,
    *,
    article_url: str,
) -> tuple[_ArticleMetadata | None, _Failure | None]:
    safe_article_url = _safe_article_url(article_url)
    if safe_article_url is None:
        return None, _Failure(
            status="invalid_response",
            reason="invalid_koreascience_article_url",
            error_code="unsafe_url",
        )
    article_id = _article_id(safe_article_url)
    assert article_id is not None

    parser = _ArticlePageParser()
    try:
        parser.feed(document)
        parser.close()
    except (AssertionError, ValueError):
        return None, _Failure(
            status="invalid_response",
            reason="invalid_koreascience_article_html",
            error_code="invalid_html",
        )

    doi_values: dict[str, str] = {}
    for value in parser.metadata.get("citation_doi", []):
        try:
            doi = normalize_doi(value)
        except InvalidDoiError:
            return None, _Failure(
                status="invalid_response",
                reason="invalid_koreascience_citation_doi",
                error_code="invalid_doi",
            )
        if doi is not None:
            doi_values.setdefault(doi.casefold(), doi)
    if len(doi_values) > 1:
        return None, _Failure(
            status="invalid_response",
            reason="conflicting_koreascience_citation_dois",
            error_code="conflicting_metadata",
        )
    doi = next(iter(doi_values.values()), None)

    citation_pdf_values = parser.metadata.get("citation_pdf_url", [])
    safe_meta_pdfs: dict[str, str] = {}
    for value in citation_pdf_values:
        pdf_url = _safe_pdf_url(urljoin(safe_article_url, value))
        if pdf_url is None or _pdf_article_id(pdf_url) != article_id:
            return None, _Failure(
                status="invalid_response",
                reason="unsafe_koreascience_citation_pdf_url",
                error_code="unsafe_url",
            )
        safe_meta_pdfs.setdefault(pdf_url, pdf_url)

    safe_anchor_pdfs: dict[str, str] = {}
    for href in parser.pdf_hrefs:
        pdf_url = _safe_pdf_url(urljoin(safe_article_url, href))
        if pdf_url is None or _pdf_article_id(pdf_url) != article_id:
            continue
        safe_anchor_pdfs.setdefault(pdf_url, pdf_url)

    pdf_values = safe_meta_pdfs or safe_anchor_pdfs
    if not pdf_values:
        return None, _Failure(
            status="not_found",
            reason="koreascience_article_has_no_explicit_pdf",
            error_code="pdf_not_found",
        )
    if len(pdf_values) != 1:
        return None, _Failure(
            status="invalid_response",
            reason="multiple_koreascience_pdf_urls",
            error_code="ambiguous",
        )
    pdf_url = next(iter(pdf_values.values()))

    titles = _unique_clean(
        [
            *parser.metadata.get("citation_title", []),
            *parser.article_titles,
        ]
    )
    journals = _unique_clean(
        [
            *parser.metadata.get("citation_journal_title", []),
            *parser.journal_labels,
        ]
    )
    year = _metadata_year(parser.metadata.get("citation_date", []))
    first_page, first_failure = _single_page(
        parser.metadata.get("citation_firstpage", [])
    )
    final_page, final_failure = _single_page(
        parser.metadata.get("citation_lastpage", [])
    )
    if first_failure or final_failure:
        return None, _Failure(
            status="invalid_response",
            reason="conflicting_koreascience_page_metadata",
            error_code="conflicting_metadata",
        )

    return (
        _ArticleMetadata(
            article_url=safe_article_url,
            article_id=article_id,
            pdf_url=pdf_url,
            titles=titles,
            doi=doi,
            year=year,
            journals=journals,
            first_page=first_page,
            final_page=final_page,
        ),
        None,
    )


def _match_article(
    identity: _ExpectedIdentity,
    metadata: _ArticleMetadata,
) -> _ExactMatch | None:
    normalized_article_titles = {
        normalized
        for value in metadata.titles
        if (normalized := normalize_title(value)) is not None
    }
    matched_title = next(
        (
            title
            for title, normalized in identity.titles
            if normalized in normalized_article_titles
        ),
        None,
    )

    if identity.doi is not None and metadata.doi is not None:
        if metadata.doi.casefold() != identity.doi.casefold():
            return None
        if (
            identity.year is not None
            and metadata.year is not None
            and metadata.year != identity.year
        ):
            return None
        match_basis = "doi_exact"
    else:
        if matched_title is None:
            return None
        if (
            identity.year is None
            or metadata.year is None
            or metadata.year != identity.year
        ):
            return None
        match_basis = "title_year_exact"

    normalized_journals = {
        normalized
        for value in metadata.journals
        if (normalized := _normalize_journal(value)) is not None
    }
    matched_journal = None
    if identity.journal is not None and normalized_journals:
        expected_journal = _normalize_journal(identity.journal)
        if expected_journal not in normalized_journals:
            return None
        matched_journal = identity.journal

    if (
        identity.first_page is not None
        and metadata.first_page is not None
        and identity.first_page != metadata.first_page
    ):
        return None
    if (
        identity.final_page is not None
        and metadata.final_page is not None
        and identity.final_page != metadata.final_page
    ):
        return None
    return _ExactMatch(
        metadata=metadata,
        match_basis=match_basis,
        matched_title=matched_title,
        matched_journal=matched_journal,
    )


def _safe_article_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if not _canonical_origin(parsed):
        return None
    if _ARTICLE_PAGE_RE.fullmatch(parsed.path) is None:
        return None
    if parsed.fragment:
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key != "lang" or item not in {"ko", "en"} for key, item in query):
        return None
    if len(query) > 1:
        return None
    canonical_query = urlencode(query) if query else ""
    return urlunsplit(
        ("https", KOREASCIENCE_HOST, parsed.path, canonical_query, "")
    )


def _safe_pdf_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if not _canonical_origin(parsed):
        return None
    if _ARTICLE_PDF_RE.fullmatch(parsed.path) is None:
        return None
    if parsed.query or parsed.fragment:
        return None
    return urlunsplit(("https", KOREASCIENCE_HOST, parsed.path, "", ""))


def _allowed_resource_url(value: str) -> bool:
    return (
        _safe_search_url(value) is not None
        or _safe_article_url(value) is not None
    )


def _same_allowed_resource(expected: str, observed: str) -> bool:
    expected_search = _safe_search_url(expected)
    if expected_search is not None:
        return _safe_search_url(observed) == expected_search
    expected_article = _safe_article_url(expected)
    if expected_article is not None:
        observed_article = _safe_article_url(observed)
        return (
            observed_article is not None
            and _article_id(observed_article) == _article_id(expected_article)
        )
    return False


def _safe_search_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if (
        not _canonical_origin(parsed)
        or parsed.path != KOREASCIENCE_SEARCH_PATH
        or parsed.fragment
    ):
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    keys = [key for key, _item in query]
    if (
        not query
        or any(key not in _SEARCH_QUERY_KEYS for key in keys)
        or len(keys) != len(set(keys))
    ):
        return None
    values = dict(query)
    if values.get("pageNo") != "1":
        return None
    if values.get("contentType") != "JOURNAL":
        return None
    try:
        page_size = int(values.get("pageSize", ""))
    except ValueError:
        return None
    if not 1 <= page_size <= 50:
        return None
    return urlunsplit(
        ("https", KOREASCIENCE_HOST, parsed.path, urlencode(query), "")
    )


def _canonical_origin(parsed) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold().rstrip(".")
        == KOREASCIENCE_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _article_id(article_url: str) -> str | None:
    match = _ARTICLE_PAGE_RE.fullmatch(urlsplit(article_url).path)
    return match.group("article") if match is not None else None


def _pdf_article_id(pdf_url: str) -> str | None:
    match = _ARTICLE_PDF_RE.fullmatch(urlsplit(pdf_url).path)
    return match.group("article") if match is not None else None


def _decode_html(body: bytes) -> str | None:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_challenge_page(document: str) -> bool:
    lowered = document.casefold()
    return any(
        marker in lowered
        for marker in (
            "g-recaptcha",
            "hcaptcha",
            "cf-chl-",
            "captcha-container",
        )
    )


def _attribute(
    attrs: list[tuple[str, str | None]],
    name: str,
) -> str | None:
    wanted = name.casefold()
    return next(
        (
            value
            for key, value in attrs
            if key.casefold() == wanted and value is not None
        ),
        None,
    )


def _clean_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = unicodedata.normalize("NFKC", html.unescape(value))
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _unique_clean(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value)
        if cleaned is None:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return tuple(result)


def _year(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1900 <= value <= 2100:
        return value
    if isinstance(value, str):
        match = _YEAR_RE.search(value)
        if match is not None:
            return int(match.group("year"))
    return None


def _metadata_year(values: list[str]) -> int | None:
    years = {year for value in values if (year := _year(value)) is not None}
    return next(iter(years)) if len(years) == 1 else None


def _paper_journal(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    if cleaned is None or _INTERNAL_JOURNAL_ID_RE.fullmatch(cleaned):
        return None
    return cleaned


def _normalize_journal(value: str | None) -> str | None:
    normalized = normalize_title(value)
    if normalized is None:
        return None
    return normalized.strip("() ")


def _normalize_page(value: object) -> str | None:
    cleaned = _clean_text(value) if isinstance(value, str) else None
    if cleaned is None:
        return None
    if cleaned.isdecimal():
        return str(int(cleaned))
    return cleaned.casefold()


def _single_page(values: list[str]) -> tuple[str | None, bool]:
    pages = {
        page for value in values if (page := _normalize_page(value)) is not None
    }
    if len(pages) > 1:
        return None, True
    return next(iter(pages), None), False


def _page_query(
    first_page: str | None,
    final_page: str | None,
) -> str | None:
    if first_page is not None and final_page is not None:
        return (
            first_page
            if first_page == final_page
            else f"{first_page}-{final_page}"
        )
    return first_page or final_page
