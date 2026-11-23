"""Memory-only DBpia article discovery through the official search Open API.

The API is used only to identify an exact DBpia article landing page.  It does
not provide a PDF or grant permission to automate DBpia's PDF user interface.
Raw responses and derived NODE mappings are deliberately never cached here.

The default catalog remains the Korean Digital Forensics Society (KDFS)
catalog.  Callers may explicitly configure another exact journal/publisher
pair; catalog searches and bibliographic matching remain bound to that pair.
"""

from __future__ import annotations

import html
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import requests

from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    ResponseTooLargeError,
    RetryableHttpError,
    parse_retry_after,
)
from PaperPipeline.records import PaperCandidate


DBPIA_OPENAPI_URL = "https://api.dbpia.co.kr/v2/search/search.xml"
DBPIA_KDFS_JOURNAL = "디지털포렌식연구"
DBPIA_KDFS_PUBLISHER = "한국디지털포렌식학회"
# Two older KCI rows retain KCI's stable series identifier instead of the
# display name.  Treat only this exact, observed identifier as the same journal.
KCI_KDFS_JOURNAL_IDENTIFIERS = frozenset({"ser000003109"})
DBPIA_MIN_YEAR = 2020
DBPIA_MAX_YEAR = 2026

_NODE_RE = re.compile(r"NODE[0-9]{5,20}")
_LEGACY_ARTICLE_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
_ARTICLE_PATH_RE = re.compile(
    r"/journal/articledetail/(?P<article>NODE[0-9]{5,20}|[1-9][0-9]{0,19})",
    flags=re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(20[0-9]{2})")
_PAGE_RANGE_RE = re.compile(
    r"\s*([0-9]+)\s*(?:-|~|–|—|−)\s*([0-9]+)"
    r"(?:\s*\(\s*[0-9]+\s*(?:pages?|쪽)?\s*\))?\s*",
    flags=re.IGNORECASE,
)
_ISSUE_RE = re.compile(r"\s*([0-9]+)\s*[_./-]\s*([0-9]+)\s*")
_API_ISSUE_PATTERNS = (
    re.compile(r"\s*([0-9]+)\s*\(\s*([0-9]+)\s*\)\s*"),
    re.compile(r"\s*제?\s*([0-9]+)\s*권\s*제?\s*([0-9]+)\s*호\s*"),
    re.compile(
        r"\s*(?:vol\.?\s*)?([0-9]+)\s*"
        r"(?:[,;/_-]\s*|no\.?\s*)([0-9]+)\s*",
        flags=re.IGNORECASE,
    ),
)
_API_ISSUE_SEARCH_PATTERNS = (
    re.compile(r"제?\s*([0-9]+)\s*권\s*제?\s*([0-9]+)\s*호"),
    re.compile(
        r"vol\.?\s*([0-9]+)\s*(?:[,;/_-]\s*)?no\.?\s*([0-9]+)",
        flags=re.IGNORECASE,
    ),
)
_XML_CONTENT_TYPES = frozenset(
    {
        "application/xml",
        "application/rss+xml",
        "text/xml",
    }
)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_EXPECTED_PARAMETER_NAMES = frozenset(
    {
        "key",
        "target",
        "searchbook",
        "searchpublisher",
        "itype",
        "pyear",
        "pyear_start",
        "pyear_end",
        "pagecount",
        "pagenumber",
    }
)
_DOCUMENTED_LINK_CONTEXT_NAMES = frozenset(
    {
        "Collection",
        "Multimedia",
        "Page",
        "PageSize",
        "SearchAll",
        "Seq",
        "Sort",
        "SortType",
        "isIdentifyAuthor",
        "q",
        "searchWord",
    }
)
_MAX_SEARCH_SCOPE_FIELD_LENGTH = 255


class DbpiaOpenApiHttp(Protocol):
    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch: ...


class _RedactedParams(dict[str, str]):
    """A requests-compatible mapping whose representation hides the key."""

    def __repr__(self) -> str:
        return repr(
            {
                name: "[REDACTED]" if name == "key" else value
                for name, value in self.items()
            }
        )

    __str__ = __repr__


class DbpiaOpenApiHttpClient:
    """Bounded HTTPS client for the one fixed DBpia search endpoint."""

    def __init__(
        self,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        retries: int = 2,
        session: requests.Session | None = None,
        request_gate: Callable[[str], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        search_journal: str = DBPIA_KDFS_JOURNAL,
        search_publisher: str = DBPIA_KDFS_PUBLISHER,
    ) -> None:
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if retries < 0:
            raise ValueError("retries must be non-negative")
        self.timeout = (connect_timeout, read_timeout)
        self.retries = retries
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._request_gate = request_gate
        self._sleep = sleeper
        self.search_journal = _validate_search_scope_field(
            search_journal,
            field_name="journal",
        )
        self.search_publisher = _validate_search_scope_field(
            search_publisher,
            field_name="publisher",
        )

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        self._validate_request(url, params, max_bytes)
        request_headers = {
            "User-Agent": "Forensics-Paper/1.0 (official DBpia metadata API)",
            "Accept-Encoding": "gzip, deflate",
            **{
                name: value
                for name, value in (headers or {}).items()
                if name.casefold()
                not in {"authorization", "cookie", "proxy-authorization"}
            },
        }

        response: requests.Response | None = None
        for attempt in range(self.retries + 1):
            if self._request_gate is not None:
                # The query-free endpoint is the only value exposed to the
                # limiter.  In particular, the API key is never logged there.
                self._request_gate(DBPIA_OPENAPI_URL)
            try:
                response = self.session.get(
                    DBPIA_OPENAPI_URL,
                    params=params,
                    headers=request_headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                    verify=True,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt >= self.retries:
                    raise RetryableHttpError(
                        "DBpia Open API request failed"
                    ) from None
                self._sleep(min(8.0, float(2 ** (attempt + 1))))
                continue

            status = int(response.status_code)
            if status not in _RETRYABLE_HTTP_STATUSES or attempt >= self.retries:
                break
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            response.close()
            response = None
            self._sleep(
                min(120.0, max(0.0, retry_after))
                if retry_after is not None
                else min(8.0, float(2 ** (attempt + 1)))
            )

        if response is None:  # defensive; every loop path returns or assigns
            raise RetryableHttpError("DBpia Open API request failed")
        try:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    content_length = int(declared)
                except ValueError:
                    content_length = None
                if content_length is not None and content_length > max_bytes:
                    raise ResponseTooLargeError(
                        "DBpia Open API response exceeds the configured limit"
                    )
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLargeError(
                        "DBpia Open API response exceeds the configured limit"
                    )
            # requests' prepared URL contains the query key.  Never copy it
            # into our result object; this endpoint does not follow redirects.
            return HttpFetch(
                requested_url=DBPIA_OPENAPI_URL,
                final_url=DBPIA_OPENAPI_URL,
                status_code=int(response.status_code),
                headers=dict(response.headers),
                body=bytes(body),
                redirects=(),
            )
        finally:
            response.close()

    def _validate_request(
        self,
        url: str,
        params: Mapping[str, str],
        max_bytes: int,
    ) -> None:
        if url != DBPIA_OPENAPI_URL or max_bytes < 1:
            raise HttpClientError("invalid DBpia Open API request")
        if set(params) != _EXPECTED_PARAMETER_NAMES:
            raise HttpClientError("invalid DBpia Open API parameters")
        key = params.get("key", "")
        if (
            not key
            or not key.isascii()
            or any(character.isspace() or ord(character) < 32 for character in key)
        ):
            raise HttpClientError("invalid DBpia Open API key")
        if (
            params.get("target") != "se_adv"
            or params.get("searchbook") != self.search_journal
            or params.get("searchpublisher") != self.search_publisher
            or params.get("itype") != "1"
            or params.get("pyear") != "3"
        ):
            raise HttpClientError("invalid DBpia Open API search scope")
        try:
            start_year = int(params["pyear_start"])
            end_year = int(params["pyear_end"])
            page_size = int(params["pagecount"])
            page_number = int(params["pagenumber"])
        except (KeyError, ValueError):
            raise HttpClientError("invalid DBpia Open API pagination") from None
        if not (
            DBPIA_MIN_YEAR <= start_year <= end_year <= DBPIA_MAX_YEAR
            and 1 <= page_size <= 100
            and 1 <= page_number <= 10_000
        ):
            raise HttpClientError("invalid DBpia Open API bounds")


@dataclass(frozen=True, slots=True)
class DbpiaOpenApiConfig:
    api_key: str | None = field(repr=False)
    journal: str = DBPIA_KDFS_JOURNAL
    publisher: str = DBPIA_KDFS_PUBLISHER
    minimum_year: int = DBPIA_MIN_YEAR
    maximum_year: int = DBPIA_MAX_YEAR
    # DBpia documents 20 as the default page size.  Keeping that conservative
    # value avoids relying on an undocumented maximum while still requiring
    # explicit, auditable pagination parameters.
    page_size: int = 20
    max_pages: int = 25
    max_response_bytes: int = 4 * 1024 * 1024
    max_xml_depth: int = 24
    max_xml_elements: int = 20_000

    def __post_init__(self) -> None:
        _validate_search_scope_field(self.journal, field_name="journal")
        _validate_search_scope_field(self.publisher, field_name="publisher")
        if not (
            DBPIA_MIN_YEAR
            <= self.minimum_year
            <= self.maximum_year
            <= DBPIA_MAX_YEAR
        ):
            raise ValueError("DBpia catalog year range is out of bounds")
        if not 1 <= self.page_size <= 100:
            raise ValueError("DBpia page_size must be between 1 and 100")
        if not 1 <= self.max_pages <= 25:
            raise ValueError("DBpia max_pages must be between 1 and 25")
        if (
            self.max_response_bytes < 1
            or not 2 <= self.max_xml_depth <= 64
            or not 100 <= self.max_xml_elements <= 100_000
        ):
            raise ValueError("invalid DBpia response bounds")


@dataclass(frozen=True, slots=True)
class DbpiaOpenApiResolution:
    status: str
    reason: str
    landing_url: str | None = field(default=None, repr=False)
    node_id: str | None = field(default=None, repr=False)
    matched_title: str | None = None
    matched_year: int | None = None
    returned_items: int = 0
    exact_matches: int = 0
    pages_fetched: int = 0
    http_status: int | None = None
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return (
            self.status == "resolved"
            and self.landing_url is not None
        )


@dataclass(frozen=True, slots=True)
class DbpiaOpenApiBatchResult:
    resolutions: Mapping[str, DbpiaOpenApiResolution]
    pages_fetched: int
    returned_items: int
    # True means the provider/key/transport response made additional scopes
    # unsafe or pointless in this run.  Callers must stop cross-scope queries.
    stop_provider: bool = False


@dataclass(frozen=True, slots=True)
class _CatalogArticle:
    article_key: str
    node_id: str | None
    landing_url: str = field(repr=False)
    title: str
    year: int | None
    journal: str | None
    publisher: str | None
    volume: str | None
    issue: str | None
    first_page: str | None
    final_page: str | None
    authors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CatalogFailure(Exception):
    status: str
    reason: str
    http_status: int | None = None
    error_code: str | None = None
    stop_batch: bool = False


@dataclass(frozen=True, slots=True)
class _CatalogLink:
    article_key: str
    node_id: str | None
    landing_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _SkippedCatalogItem:
    identity_key: tuple[str, str, str]
    fingerprint: tuple[tuple[str, tuple[tuple[str, str], ...], str], ...]


class DbpiaOpenApiResolver:
    """Resolve selected papers against one exact-scope catalog per year."""

    def __init__(
        self,
        config: DbpiaOpenApiConfig,
        *,
        http: DbpiaOpenApiHttp,
    ) -> None:
        if isinstance(http, DbpiaOpenApiHttpClient) and (
            http.search_journal != config.journal
            or http.search_publisher != config.publisher
        ):
            raise ValueError(
                "DBpia Open API HTTP client scope must match resolver config"
            )
        self.config = config
        self.http = http

    def resolve(
        self,
        papers: Sequence[PaperCandidate],
    ) -> DbpiaOpenApiBatchResult:
        papers_by_source: dict[str, PaperCandidate] = {}
        resolutions: dict[str, DbpiaOpenApiResolution] = {}
        eligible: list[PaperCandidate] = []
        for paper in papers:
            if paper.source_id in papers_by_source:
                raise ValueError(f"duplicate source_id: {paper.source_id}")
            papers_by_source[paper.source_id] = paper
            missing_reason = _paper_metadata_problem(paper, self.config)
            if missing_reason is not None:
                status = (
                    "unsupported"
                    if missing_reason == "dbpia_year_out_of_scope"
                    else "missing_metadata"
                )
                resolutions[paper.source_id] = DbpiaOpenApiResolution(
                    status=status,
                    reason=missing_reason,
                )
                continue
            eligible.append(paper)

        if not eligible:
            return DbpiaOpenApiBatchResult(resolutions, 0, 0)
        if not _valid_api_key(self.config.api_key):
            failure = DbpiaOpenApiResolution(
                status="configuration_error",
                reason="missing_or_invalid_dbpia_openapi_key",
                error_code="configuration_error",
            )
            for paper in eligible:
                resolutions[paper.source_id] = failure
            return DbpiaOpenApiBatchResult(
                resolutions,
                0,
                0,
                stop_provider=True,
            )

        eligible_by_year: dict[int, list[PaperCandidate]] = {}
        for paper in eligible:
            # _paper_metadata_problem already proved this is a bounded int.
            year = int(paper.publication_year)  # type: ignore[arg-type]
            eligible_by_year.setdefault(year, []).append(paper)

        total_pages = 0
        total_items = 0
        stop_provider = False
        ordered_years = sorted(eligible_by_year)
        for year_index, year in enumerate(ordered_years):
            year_papers = eligible_by_year[year]
            try:
                articles, pages_fetched, http_status = self._load_catalog(
                    start_year=year,
                    end_year=year,
                )
            except _CatalogFailure as failure:
                resolution = DbpiaOpenApiResolution(
                    status=failure.status,
                    reason=failure.reason,
                    http_status=failure.http_status,
                    error_code=failure.error_code,
                )
                for paper in year_papers:
                    resolutions[paper.source_id] = resolution
                if failure.stop_batch:
                    stop_provider = True
                    # Key/IP/quota/provider failures are not made safer by
                    # querying more years.  Item and pagination defects are
                    # year-local, so those do not suppress later catalogs.
                    for deferred_year in ordered_years[year_index + 1 :]:
                        for paper in eligible_by_year[deferred_year]:
                            resolutions[paper.source_id] = resolution
                    break
                continue

            total_pages += pages_fetched
            total_items += len(articles)
            for paper in year_papers:
                resolutions[paper.source_id] = _match_catalog_article(
                    paper,
                    articles,
                    config=self.config,
                    pages_fetched=pages_fetched,
                    http_status=http_status,
                )
        return DbpiaOpenApiBatchResult(
            resolutions=resolutions,
            pages_fetched=total_pages,
            returned_items=total_items,
            stop_provider=stop_provider,
        )

    def _load_catalog(
        self,
        *,
        start_year: int,
        end_year: int,
    ) -> tuple[list[_CatalogArticle], int, int]:
        articles: list[_CatalogArticle] = []
        by_article: dict[str, _CatalogArticle] = {}
        skipped_by_identity: dict[
            tuple[str, str, str],
            _SkippedCatalogItem,
        ] = {}
        total_count: int | None = None
        raw_count = 0
        last_status = 200

        for page_number in range(1, self.config.max_pages + 1):
            params = _RedactedParams(
                {
                    "key": self.config.api_key or "",
                    "target": "se_adv",
                    "searchbook": self.config.journal,
                    "searchpublisher": self.config.publisher,
                    "itype": "1",
                    "pyear": "3",
                    "pyear_start": str(start_year),
                    "pyear_end": str(end_year),
                    "pagecount": str(self.config.page_size),
                    "pagenumber": str(page_number),
                }
            )
            try:
                fetch = self.http.get_bytes(
                    DBPIA_OPENAPI_URL,
                    params=params,
                    max_bytes=self.config.max_response_bytes,
                    headers={"Accept": "application/xml, text/xml"},
                )
            except ResponseTooLargeError:
                raise _CatalogFailure(
                    "invalid_response",
                    "dbpia_openapi_response_too_large",
                    error_code="too_large",
                ) from None
            except RetryableHttpError:
                raise _CatalogFailure(
                    "retryable_error",
                    "dbpia_openapi_request_failed",
                    error_code="retryable_error",
                    stop_batch=True,
                ) from None
            except HttpClientError:
                raise _CatalogFailure(
                    "request_error",
                    "dbpia_openapi_request_rejected",
                    error_code="request_error",
                    stop_batch=True,
                ) from None

            last_status = fetch.status_code
            _validate_fetch(fetch)
            (
                page_articles,
                page_skipped_items,
                reported_total,
                page_item_count,
            ) = _parse_catalog_page(
                fetch.body,
                max_depth=self.config.max_xml_depth,
                max_elements=self.config.max_xml_elements,
                fallback_year=start_year if start_year == end_year else None,
            )
            if page_item_count > self.config.page_size:
                raise _CatalogFailure(
                    "invalid_response",
                    "dbpia_openapi_page_size_exceeded",
                    fetch.status_code,
                    "invalid_xml",
                )
            if total_count is None:
                total_count = reported_total
                if total_count > self.config.page_size * self.config.max_pages:
                    raise _CatalogFailure(
                        "invalid_response",
                        "dbpia_openapi_catalog_too_large",
                        fetch.status_code,
                        "too_large",
                    )
            elif reported_total != total_count:
                raise _CatalogFailure(
                    "invalid_response",
                    "dbpia_openapi_total_changed",
                    fetch.status_code,
                    "invalid_xml",
                )

            raw_count += page_item_count
            for skipped in page_skipped_items:
                previous_skipped = skipped_by_identity.get(
                    skipped.identity_key
                )
                if previous_skipped is not None:
                    raise _CatalogFailure(
                        "invalid_response",
                        "duplicate_dbpia_nonarticle_catalog_item"
                        if previous_skipped == skipped
                        else "conflicting_dbpia_nonarticle_metadata",
                        fetch.status_code,
                        "invalid_xml",
                    )
                skipped_by_identity[skipped.identity_key] = skipped
            for article in page_articles:
                previous = by_article.get(article.article_key)
                if previous is not None:
                    raise _CatalogFailure(
                        "invalid_response",
                        "duplicate_dbpia_article_across_pages"
                        if previous == article
                        else "conflicting_dbpia_article_metadata",
                        fetch.status_code,
                        "invalid_xml",
                    )
                by_article[article.article_key] = article
                articles.append(article)

            if total_count is not None and raw_count > total_count:
                raise _CatalogFailure(
                    "invalid_response",
                    "inconsistent_dbpia_openapi_totalcount",
                    fetch.status_code,
                    "invalid_xml",
                )
            if raw_count >= (total_count or 0):
                return articles, page_number, last_status
            if page_item_count == 0:
                raise _CatalogFailure(
                    "invalid_response",
                    "incomplete_dbpia_openapi_catalog",
                    fetch.status_code,
                    "invalid_xml",
                )

        raise _CatalogFailure(
            "invalid_response",
            "incomplete_dbpia_openapi_catalog",
            last_status,
            "too_large",
        )


def _valid_api_key(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value.isascii()
        and not any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    )


def _validate_fetch(fetch: HttpFetch) -> None:
    if (
        fetch.requested_url != DBPIA_OPENAPI_URL
        or fetch.final_url != DBPIA_OPENAPI_URL
        or fetch.redirects
    ):
        raise _CatalogFailure(
            "invalid_response",
            "dbpia_openapi_redirect_rejected",
            fetch.status_code,
            "unsafe_url",
            True,
        )
    if fetch.status_code in {401, 403}:
        raise _CatalogFailure(
            "configuration_error",
            "dbpia_openapi_access_denied",
            fetch.status_code,
            "access_denied",
            True,
        )
    if fetch.status_code == 429:
        raise _CatalogFailure(
            "retryable_error",
            "dbpia_openapi_rate_limited",
            fetch.status_code,
            "retryable_error",
            True,
        )
    if fetch.status_code in _RETRYABLE_HTTP_STATUSES:
        raise _CatalogFailure(
            "retryable_error",
            "dbpia_openapi_temporary_failure",
            fetch.status_code,
            "retryable_error",
            True,
        )
    if fetch.status_code != 200:
        raise _CatalogFailure(
            "configuration_error" if fetch.status_code == 400 else "request_error",
            "dbpia_openapi_request_rejected",
            fetch.status_code,
            "access_denied" if fetch.status_code == 400 else "request_error",
            True,
        )
    content_type = next(
        (
            value.split(";", 1)[0].strip().casefold()
            for name, value in fetch.headers.items()
            if name.casefold() == "content-type"
        ),
        "",
    )
    if content_type not in _XML_CONTENT_TYPES:
        raise _CatalogFailure(
            "invalid_response",
            "invalid_dbpia_openapi_content_type",
            fetch.status_code,
            "invalid_content_type",
            True,
        )


def _parse_catalog_page(
    body: bytes,
    *,
    max_depth: int,
    max_elements: int,
    fallback_year: int | None = None,
) -> tuple[list[_CatalogArticle], list[_SkippedCatalogItem], int, int]:
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise _CatalogFailure(
            "invalid_response",
            "unsafe_dbpia_openapi_xml",
            error_code="invalid_xml",
        )
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        raise _CatalogFailure(
            "invalid_response",
            "invalid_dbpia_openapi_xml",
            error_code="invalid_xml",
        ) from None
    if _xml_depth(root) > max_depth or sum(1 for _element in root.iter()) > max_elements:
        raise _CatalogFailure(
            "invalid_response",
            "complex_dbpia_openapi_xml",
            error_code="invalid_xml",
        )

    api_error_code: str | None = None
    for element in root.iter():
        name = _local_name(element.tag)
        value = _element_text(element)
        if name not in {"error", "errorcode", "code"} or value is None:
            continue
        match = re.search(r"\bE[0-9]{4}\b", value, flags=re.IGNORECASE)
        if match is not None:
            api_error_code = match.group(0).upper()
            break
    if api_error_code == "E0016":
        return [], [], 0, 0
    if api_error_code in {"E0000", "E0015", "E0017", "E0018"}:
        raise _CatalogFailure(
            "retryable_error",
            "dbpia_openapi_rate_limited"
            if api_error_code == "E0017"
            else "dbpia_openapi_temporary_failure",
            error_code="retryable_error",
            stop_batch=True,
        )
    if api_error_code is not None:
        raise _CatalogFailure(
            "configuration_error",
            "dbpia_openapi_error_response",
            error_code="access_denied",
            stop_batch=True,
        )

    total_values = [
        _element_text(element)
        for element in root.iter()
        if _local_name(element.tag) == "totalcount"
    ]
    if len(total_values) != 1 or total_values[0] is None:
        raise _CatalogFailure(
            "invalid_response",
            "missing_dbpia_openapi_totalcount",
            error_code="invalid_xml",
        )
    try:
        total_count = int(total_values[0])
    except ValueError:
        raise _CatalogFailure(
            "invalid_response",
            "invalid_dbpia_openapi_totalcount",
            error_code="invalid_xml",
        ) from None
    if total_count < 0:
        raise _CatalogFailure(
            "invalid_response",
            "invalid_dbpia_openapi_totalcount",
            error_code="invalid_xml",
        )

    item_elements = [
        element
        for element in root.iter()
        if _local_name(element.tag) == "item"
    ]
    articles: list[_CatalogArticle] = []
    skipped_items: list[_SkippedCatalogItem] = []
    for element in item_elements:
        content_type = _child_text(element, "ctype")
        if content_type is not None and content_type.casefold() != "article":
            skipped_items.append(
                _skipped_catalog_item(element, content_type=content_type)
            )
            continue
        articles.append(_parse_item(element, fallback_year=fallback_year))
    if total_count == 0 and item_elements:
        raise _CatalogFailure(
            "invalid_response",
            "inconsistent_dbpia_openapi_totalcount",
            error_code="invalid_xml",
        )
    return articles, skipped_items, total_count, len(item_elements)


def _parse_item(
    item: ET.Element,
    *,
    fallback_year: int | None,
) -> _CatalogArticle:
    raw_id = item.attrib.get("id", "").strip()
    link = _child_text(item, "link_url")
    catalog_link = _catalog_link(link)
    raw_article_key = _recognizable_article_key(raw_id)
    if catalog_link is None or (
        raw_article_key is not None
        and raw_article_key.partition(":")[0]
        == catalog_link.article_key.partition(":")[0]
        and raw_article_key != catalog_link.article_key
    ):
        raise _CatalogFailure(
            "invalid_response",
            "invalid_dbpia_article_identity",
            error_code="invalid_xml",
        )
    title = _child_text(item, "title")
    if not title:
        raise _CatalogFailure(
            "invalid_response",
            "missing_dbpia_article_title",
            error_code="invalid_xml",
        )

    issue_element = _child(item, "issue")
    yymm = _child_text(issue_element, "yymm") if issue_element is not None else None
    year_match = _YEAR_RE.search(unicodedata.normalize("NFKC", yymm or ""))
    api_issue = _api_issue_identity(issue_element)
    pages = _child_text(item, "pages")
    page_match = _PAGE_RANGE_RE.fullmatch(
        unicodedata.normalize("NFKC", pages or "")
    )
    authors_element = _child(item, "authors")
    authors: list[str] = []
    if authors_element is not None:
        for author in _children(authors_element, "author"):
            name = _child_text(author, "name")
            if name:
                authors.append(name)

    publisher_element = _child(item, "publisher")
    publication_element = _child(item, "publication")
    return _CatalogArticle(
        article_key=catalog_link.article_key,
        node_id=catalog_link.node_id,
        landing_url=catalog_link.landing_url,
        title=title,
        year=int(year_match.group(1)) if year_match else fallback_year,
        journal=(
            _child_text(publication_element, "name")
            if publication_element is not None
            else None
        ),
        publisher=(
            _child_text(publisher_element, "name")
            if publisher_element is not None
            else None
        ),
        volume=api_issue[0] if api_issue is not None else None,
        issue=api_issue[1] if api_issue is not None else None,
        first_page=page_match.group(1) if page_match else None,
        final_page=page_match.group(2) if page_match else None,
        authors=tuple(authors),
    )


def _skipped_catalog_item(
    item: ET.Element,
    *,
    content_type: str,
) -> _SkippedCatalogItem:
    """Build an in-memory overlap identity for a non-article result.

    Non-article rows still count toward DBpia's ``totalcount``.  Detecting
    their repetition is necessary; otherwise an overlapping page can make the
    raw count look complete while a later article was never observed.
    """

    normalized_type = content_type.strip().casefold()
    raw_id = item.attrib.get("id", "").strip()
    link = _child_text(item, "link_url") or ""
    title = _child_text(item, "title") or ""
    canonical_link = _canonical_nonarticle_link(link) if link else None
    if raw_id:
        anchor_kind, anchor = "id", raw_id
    elif canonical_link:
        anchor_kind = "link_title"
        anchor = f"{canonical_link}\0{_title_key(title) or ''}"
    elif title:
        anchor_kind, anchor = "title", _title_key(title) or title
    else:
        raise _CatalogFailure(
            "invalid_response",
            "missing_dbpia_nonarticle_identity",
            error_code="invalid_xml",
        )
    fingerprint = tuple(
        (
            _local_name(element.tag),
            tuple(sorted((str(name), str(value)) for name, value in element.attrib.items())),
            (
                canonical_link
                if _local_name(element.tag) == "link_url" and canonical_link
                else " ".join((element.text or "").split())
            ),
        )
        for element in item.iter()
    )
    return _SkippedCatalogItem(
        identity_key=(normalized_type, anchor_kind, anchor),
        fingerprint=fingerprint,
    )


def _canonical_nonarticle_link(value: str) -> str | None:
    """Remove only documented search context for overlap comparison."""

    if len(value) > 4_096 or any(ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "www.dbpia.co.kr"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/")
        or (parsed.query and not _discardable_link_query(parsed.query))
    ):
        return None
    normalized_path = unicodedata.normalize("NFKC", parsed.path).casefold()
    return f"https://www.dbpia.co.kr{normalized_path}"


def _match_catalog_article(
    paper: PaperCandidate,
    articles: Sequence[_CatalogArticle],
    *,
    config: DbpiaOpenApiConfig,
    pages_fetched: int,
    http_status: int,
) -> DbpiaOpenApiResolution:
    matches_by_article = {
        article.article_key: article
        for article in articles
        if _article_matches_paper(article, paper, config)
    }
    if len(matches_by_article) == 1:
        article = next(iter(matches_by_article.values()))
        return DbpiaOpenApiResolution(
            status="resolved",
            reason="exact_dbpia_bibliographic_match",
            landing_url=article.landing_url,
            node_id=article.node_id,
            matched_title=article.title,
            matched_year=article.year,
            returned_items=len(articles),
            exact_matches=1,
            pages_fetched=pages_fetched,
            http_status=http_status,
        )
    if matches_by_article:
        return DbpiaOpenApiResolution(
            status="ambiguous",
            reason="multiple_exact_dbpia_articles",
            returned_items=len(articles),
            exact_matches=len(matches_by_article),
            pages_fetched=pages_fetched,
            http_status=http_status,
        )
    return DbpiaOpenApiResolution(
        status="not_found",
        reason="no_exact_dbpia_article",
        returned_items=len(articles),
        pages_fetched=pages_fetched,
        http_status=http_status,
    )


def _paper_metadata_problem(
    paper: PaperCandidate,
    config: DbpiaOpenApiConfig,
) -> str | None:
    if not paper.title and not paper.alternate_titles:
        return "missing_dbpia_title"
    if paper.publication_year is None:
        return "missing_dbpia_publication_year"
    if not config.minimum_year <= paper.publication_year <= config.maximum_year:
        return "dbpia_year_out_of_scope"
    if not _paper_journal_matches_scope(paper.journal_id, config):
        return "missing_or_mismatched_dbpia_journal"
    if _field_key(paper.institution_id) != _field_key(config.publisher):
        return "missing_or_mismatched_dbpia_publisher"
    if _expected_issue(paper.issue_id) is None:
        return "missing_or_invalid_dbpia_issue"
    if _page_key(paper.first_page) is None or _page_key(paper.final_page) is None:
        return "missing_or_invalid_dbpia_pages"
    if not _author_keys(paper.authors):
        return "missing_dbpia_authors"
    return None


def _article_matches_paper(
    article: _CatalogArticle,
    paper: PaperCandidate,
    config: DbpiaOpenApiConfig,
) -> bool:
    expected_titles = {
        key
        for value in (paper.title, *paper.alternate_titles)
        if (key := _title_key(value)) is not None
    }
    expected_issue = _expected_issue(paper.issue_id)
    return bool(
        _title_key(article.title) in expected_titles
        and article.year == paper.publication_year
        and _field_key(article.journal) == _field_key(config.journal)
        and _paper_journal_matches_scope(paper.journal_id, config)
        and _field_key(article.publisher) == _field_key(paper.institution_id)
        == _field_key(config.publisher)
        and expected_issue is not None
        and (_number_key(article.volume), _number_key(article.issue))
        == expected_issue
        and _page_key(article.first_page) == _page_key(paper.first_page)
        and _page_key(article.final_page) == _page_key(paper.final_page)
        and _author_keys(article.authors) == _author_keys(paper.authors)
    )


def _catalog_link(value: str | None) -> _CatalogLink | None:
    """Return a validated HTTPS landing identity from one API link.

    DBpia's published Search API examples use a numeric legacy article path,
    while newer responses can use ``NODE`` identifiers.  Numeric IDs are not
    transformed into guessed NODEs.  A numeric link's documented search
    context is followed exactly in memory but hidden from reprs and never
    persisted.  Unknown, duplicate, or blank query fields fail closed.
    """

    if not value:
        return None
    if len(value) > 4_096 or any(ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "www.dbpia.co.kr"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    path_match = _ARTICLE_PATH_RE.fullmatch(parsed.path)
    if path_match is not None and _discardable_link_query(parsed.query):
        article_id = path_match.group("article")
        node_id = article_id.upper()
        if _NODE_RE.fullmatch(node_id):
            return _CatalogLink(
                article_key=f"node:{node_id}",
                node_id=node_id,
                landing_url=value,
            )
        if _LEGACY_ARTICLE_ID_RE.fullmatch(article_id):
            return _CatalogLink(
                article_key=f"legacy:{article_id}",
                node_id=None,
                # Follow the official detail link exactly as supplied.  Its
                # allowlisted search context remains memory-only and is never
                # copied to logs, reprs, caches, or manifests.
                landing_url=value,
            )
        return None
    if parsed.path.casefold() != "/journal/articledetail":
        return None
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if len(pairs) != 1 or pairs[0][0] != "nodeId":
        return None
    node_id = pairs[0][1]
    if _NODE_RE.fullmatch(node_id) is None:
        return None
    return _CatalogLink(
        article_key=f"node:{node_id}",
        node_id=node_id,
        landing_url=value,
    )


def _recognizable_article_key(value: str) -> str | None:
    node_id = value.upper()
    if _NODE_RE.fullmatch(node_id):
        return f"node:{node_id}"
    if _LEGACY_ARTICLE_ID_RE.fullmatch(value):
        return f"legacy:{value}"
    return None


def _discardable_link_query(query: str) -> bool:
    """Accept only documented search-context fields, then discard them."""

    if not query:
        return True
    folded = query.casefold()
    if not (
        len(query) <= 2_048
        and all(ord(character) >= 32 for character in query)
        and "%0d" not in folded
        and "%0a" not in folded
    ):
        return False
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return False
    names = [name for name, _value in pairs]
    return bool(
        1 <= len(pairs) <= len(_DOCUMENTED_LINK_CONTEXT_NAMES)
        and len(set(names)) == len(names)
        and set(names).issubset(_DOCUMENTED_LINK_CONTEXT_NAMES)
        and all(
            value
            and len(value) <= 1_024
            and all(ord(character) >= 32 and ord(character) != 127 for character in value)
            for _name, value in pairs
        )
    )


def _title_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    compact = "".join(character for character in normalized if character.isalnum())
    return compact or None


def _field_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    normalized = " ".join(normalized.split())
    return normalized or None


def _paper_journal_matches_scope(
    value: str | None,
    config: DbpiaOpenApiConfig,
) -> bool:
    key = _field_key(value)
    if key == _field_key(config.journal):
        return True
    # KCI retained this opaque series identifier in two historical KDFS rows.
    # It is the only audited alias.  Never infer that an arbitrary SER value
    # names the configured journal, even when the publisher happens to match.
    return bool(
        _field_key(config.journal) == _field_key(DBPIA_KDFS_JOURNAL)
        and _field_key(config.publisher) == _field_key(DBPIA_KDFS_PUBLISHER)
        and key in KCI_KDFS_JOURNAL_IDENTIFIERS
    )


def _validate_search_scope_field(value: object, *, field_name: str) -> str:
    """Validate, but never normalize or infer, one exact API search field."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_SEARCH_SCOPE_FIELD_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid DBpia search {field_name}")
    return value


def _author_keys(values: Sequence[str]) -> tuple[str, ...]:
    keys: list[str] = []
    for value in values:
        normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
        normalized = re.sub(r"\s*[\(\[\{（].*\Z", "", normalized).strip()
        key = "".join(character for character in normalized if character.isalnum())
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def _api_issue_identity(
    issue_element: ET.Element | None,
) -> tuple[str, str] | None:
    if issue_element is None:
        return None
    for field_name in ("num", "name"):
        value = _child_text(issue_element, field_name)
        if value is None:
            continue
        value = unicodedata.normalize("NFKC", value)
        for pattern in _API_ISSUE_PATTERNS:
            match = pattern.fullmatch(value)
            if match is not None:
                return _number_key(match.group(1)), _number_key(match.group(2))
        embedded = {
            (_number_key(match.group(1)), _number_key(match.group(2)))
            for pattern in _API_ISSUE_SEARCH_PATTERNS
            for match in pattern.finditer(value)
        }
        if len(embedded) == 1:
            return next(iter(embedded))
    # Compatibility with a small number of legacy responses that exposed
    # separate fields despite the current API documenting ``num``/``name``.
    volume = _child_text(issue_element, "volume")
    number = _child_text(issue_element, "number")
    if _number_key(volume) is not None and _number_key(number) is not None:
        return _number_key(volume), _number_key(number)
    return None


def _expected_issue(value: str | None) -> tuple[str, str] | None:
    match = _ISSUE_RE.fullmatch(value or "")
    if match is None:
        return None
    return _number_key(match.group(1)), _number_key(match.group(2))


def _number_key(value: object) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"\s*[0-9]+\s*", value) is None:
        return None
    return str(int(value))


def _page_key(value: object) -> str | None:
    return _number_key(value)


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].casefold()


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    expected = name.casefold()
    return [child for child in list(element) if _local_name(child.tag) == expected]


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    matches = _children(element, name)
    return matches[0] if len(matches) == 1 else None


def _element_text(element: ET.Element) -> str | None:
    value = " ".join("".join(element.itertext()).split())
    return html.unescape(value) if value else None


def _child_text(element: ET.Element | None, name: str) -> str | None:
    child = _child(element, name)
    return _element_text(child) if child is not None else None


def _xml_depth(root: ET.Element) -> int:
    maximum = 1
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        maximum = max(maximum, depth)
        stack.extend((child, depth + 1) for child in list(element))
    return maximum
