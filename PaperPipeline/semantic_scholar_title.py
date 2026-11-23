"""Conservative DOI recovery through Semantic Scholar title matching.

This module uses only Semantic Scholar's official ``paper/search/match``
endpoint.  Every distinct primary/alternate title is queried, but ranking is
never accepted as identity evidence: a response must have a title exactly
equal after the project's title normalization, a publication year within one
year, and a valid DOI.  A DOI is adopted only when all conclusive matches agree
on one distinct DOI.

The API key is sent only in the ``x-api-key`` request header.  Callers must
inject one externally shared ``request_gate`` so title searches and other
Semantic Scholar endpoints can obey the key's cumulative rate limit.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    RetryableHttpError,
    SafeHttpClient,
    parse_retry_after,
)
from PaperPipeline.identifier_resolver import normalize_title
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import load_json_object, safe_source_stem, write_json_atomic


SEMANTIC_SCHOLAR_MATCH_URL = (
    "https://api.semanticscholar.org/graph/v1/paper/search/match"
)
SEMANTIC_SCHOLAR_MATCH_FIELDS = (
    "title,year,externalIds,isOpenAccess,openAccessPdf"
)
CACHE_SCHEMA_VERSION = "2.0"
_RESOLVER_NAME = "semantic-scholar-title"
_MATCH_ENVELOPE_KEYS = frozenset({"data"})
_MAX_MATCH_ITEMS = 1
_CACHEABLE_STATUSES = frozenset(
    {"resolved", "missing_metadata", "no_match", "ambiguous"}
)
_STATUS_REASONS: Mapping[str, frozenset[str]] = {
    "resolved": frozenset({"exact_title_year_single_doi"}),
    "missing_metadata": frozenset(
        {
            "missing_title",
            "missing_publication_year",
            "missing_title_and_publication_year",
        }
    ),
    "no_match": frozenset(
        {
            "title_mismatch",
            "year_mismatch",
            "missing_or_invalid_doi",
            "paper_not_found",
        }
    ),
    "ambiguous": frozenset(
        {
            "multiple_distinct_dois",
            "multiple_match_items",
        }
    ),
}


class SemanticScholarTitleHttp(Protocol):
    """Bounded GET seam implemented by :class:`SafeHttpClient`."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Return one response without following redirects."""


@dataclass(frozen=True, slots=True)
class SemanticScholarTitleConfig:
    """Credentials, cache, and response bounds for title matching."""

    api_key: str = field(repr=False)
    cache_dir: Path | None = None
    max_response_bytes: int = 2 * 1024 * 1024
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    retries: int = 0
    retry_backoff_seconds: float = 5.0
    max_retry_after_seconds: float | None = None
    positive_ttl_seconds: int = 30 * 24 * 60 * 60
    negative_ttl_seconds: int = 24 * 60 * 60
    force_refresh: bool = False


@dataclass(frozen=True, slots=True)
class SemanticScholarTitleOpenAccessPdf:
    """OA PDF metadata bound to an exact title/year/DOI match."""

    doi: str
    url: str
    title: str
    year: int
    status: str | None = None
    license: str | None = None
    disclaimer: str | None = None

    def as_resolver_candidate(self) -> ResolverCandidate:
        return ResolverCandidate(
            url=self.url,
            resolver=_RESOLVER_NAME,
            license=self.license,
            version=self.status,
            is_open_access=True,
            verified_doi=self.doi,
        )


@dataclass(frozen=True, slots=True)
class SemanticScholarTitleResolution:
    """One auditable decision for one DOI-less paper."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = _RESOLVER_NAME
    doi_original: str | None = None
    matched_title: str | None = None
    matched_year: int | None = None
    pdf: SemanticScholarTitleOpenAccessPdf | None = None
    queried_titles: tuple[str, ...] = ()
    query_count: int = 0
    returned_items: int = 0
    exact_title_matches: int = 0
    title_year_matches: int = 0
    cache_hit: bool = False
    http_status: int | None = None
    error_code: str | None = None

    @property
    def doi(self) -> str | None:
        return self.paper.doi

    @property
    def resolved(self) -> bool:
        return self.status in {"resolved", "already_present"} and bool(
            self.paper.doi
        )

    @property
    def candidate(self) -> ResolverCandidate | None:
        return self.pdf.as_resolver_candidate() if self.pdf is not None else None


@dataclass(frozen=True, slots=True)
class _MatchedItem:
    doi_original: str
    doi: str
    title: str
    year: int
    pdf: SemanticScholarTitleOpenAccessPdf | None


@dataclass(frozen=True, slots=True)
class _QueryFailure:
    status: str
    reason: str
    http_status: int | None = None
    error_code: str | None = None


class SemanticScholarTitleResolver:
    """Recover one DOI using exact matches across all known paper titles."""

    def __init__(
        self,
        config: SemanticScholarTitleConfig,
        *,
        request_gate: Callable[[str], None],
        defer_gate: Callable[[float], None] | None = None,
        http: SemanticScholarTitleHttp | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not callable(request_gate):
            raise ValueError("request_gate must be callable")
        self.config = config
        self._api_key = _validated_api_key(config.api_key)
        if config.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if config.connect_timeout <= 0 or config.read_timeout <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if config.retries < 0:
            raise ValueError("retries must be non-negative")
        if config.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        if (
            config.max_retry_after_seconds is not None
            and config.max_retry_after_seconds < 0
        ):
            raise ValueError("max_retry_after_seconds must be non-negative")
        if config.positive_ttl_seconds < 0:
            raise ValueError("positive_ttl_seconds must be non-negative")
        if config.negative_ttl_seconds < 0:
            raise ValueError("negative_ttl_seconds must be non-negative")
        self._request_gate = request_gate
        self._defer_gate = defer_gate
        # Retries and redirects are deliberately disabled here.  A caller-wide
        # gate must account for every Semantic Scholar request, and the match
        # API must never redirect the x-api-key header to another endpoint.
        self.http: SemanticScholarTitleHttp = http or SafeHttpClient(
            connect_timeout=config.connect_timeout,
            read_timeout=config.read_timeout,
            max_redirects=0,
            retries=0,
            allow_http=False,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper

    def resolve(self, paper: PaperCandidate) -> SemanticScholarTitleResolution:
        if paper.doi:
            return SemanticScholarTitleResolution(
                status="already_present",
                reason="paper_already_has_doi",
                paper=paper,
                doi_original=paper.doi_original,
            )

        title_entries = _known_title_entries(paper)
        queried_titles = tuple(title for title, _normalized in title_entries)
        normalized_titles = tuple(
            sorted(normalized for _title, normalized in title_entries)
        )
        publication_year = _paper_year(paper.publication_year)
        input_fingerprint = _input_fingerprint(
            normalized_titles,
            publication_year,
        )
        cached = (
            None
            if self.config.force_refresh
            else self._load_cache(
                paper,
                normalized_titles=normalized_titles,
                publication_year=publication_year,
                input_fingerprint=input_fingerprint,
                queried_titles=queried_titles,
            )
        )
        if cached is not None:
            return cached

        missing: list[str] = []
        if not title_entries:
            missing.append("title")
        if publication_year is None:
            missing.append("publication_year")
        if missing:
            resolution = SemanticScholarTitleResolution(
                status="missing_metadata",
                reason="missing_" + "_and_".join(missing),
                paper=paper,
                queried_titles=queried_titles,
            )
            self._store_cache(
                paper,
                normalized_titles=normalized_titles,
                publication_year=publication_year,
                input_fingerprint=input_fingerprint,
                resolution=resolution,
            )
            return resolution

        assert publication_year is not None
        known_normalized = frozenset(normalized_titles)
        matches: list[_MatchedItem] = []
        failures: list[_QueryFailure] = []
        query_count = 0
        returned_items = 0
        exact_title_matches = 0
        title_year_matches = 0

        for query_title, _query_normalized in title_entries:
            url = _match_url(query_title)
            if not _is_official_match_url(url):
                # This is unreachable for the constant URL builder, but makes
                # the host/path restriction explicit and testable.
                failures.append(
                    _QueryFailure(
                        status="invalid_response",
                        reason="invalid_semantic_scholar_match_url",
                        error_code="unsafe_url",
                    )
                )
                break
            fetch, failure, request_count, stop_queries = (
                self._fetch_query_with_retries(url)
            )
            query_count += request_count
            if failure is not None:
                failures.append(failure)
                if stop_queries:
                    break
                continue
            assert fetch is not None
            if fetch.status_code == 404:
                continue

            try:
                payload = json.loads(fetch.body)
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                failures.append(
                    _QueryFailure(
                        status="invalid_response",
                        reason="invalid_semantic_scholar_title_json",
                        http_status=fetch.status_code,
                        error_code="invalid_json",
                    )
                )
                continue

            item, envelope_failure = _match_item_from_envelope(
                payload,
                http_status=fetch.status_code,
            )
            if envelope_failure is not None:
                failures.append(envelope_failure)
                continue
            if item is None:
                continue

            returned_items += 1
            response_title = _optional_string(item.get("title"))
            response_normalized = normalize_title(response_title)
            if (
                response_title is None
                or response_normalized not in known_normalized
            ):
                continue
            exact_title_matches += 1

            response_year = _paper_year(item.get("year"))
            if (
                response_year is None
                or abs(response_year - publication_year) > 1
            ):
                continue
            title_year_matches += 1

            external_ids = item.get("externalIds")
            raw_doi = (
                external_ids.get("DOI")
                if isinstance(external_ids, Mapping)
                else None
            )
            doi_original = _optional_string(raw_doi)
            try:
                doi = normalize_doi(doi_original)
            except InvalidDoiError:
                doi = None
            if doi is None or doi_original is None:
                continue

            matches.append(
                _MatchedItem(
                    doi_original=doi_original,
                    doi=doi,
                    title=response_title,
                    year=response_year,
                    pdf=_oa_pdf_from_item(
                        item,
                        doi=doi,
                        title=response_title,
                        year=response_year,
                    ),
                )
            )

        distinct_dois = {match.doi.casefold() for match in matches}
        common: dict[str, Any] = {
            "paper": paper,
            "queried_titles": queried_titles,
            "query_count": query_count,
            "returned_items": returned_items,
            "exact_title_matches": exact_title_matches,
            "title_year_matches": title_year_matches,
        }

        if len(distinct_dois) > 1:
            resolution = SemanticScholarTitleResolution(
                status="ambiguous",
                reason="multiple_distinct_dois",
                **common,
            )
        elif failures:
            failure = _preferred_failure(failures)
            resolution = SemanticScholarTitleResolution(
                status=failure.status,
                reason=failure.reason,
                http_status=failure.http_status,
                error_code=failure.error_code,
                **common,
            )
        elif not distinct_dois:
            if returned_items == 0:
                reason = "paper_not_found"
            elif exact_title_matches == 0:
                reason = "title_mismatch"
            elif title_year_matches == 0:
                reason = "year_mismatch"
            else:
                reason = "missing_or_invalid_doi"
            resolution = SemanticScholarTitleResolution(
                status="no_match",
                reason=reason,
                **common,
            )
        else:
            matches_for_doi = [
                match
                for match in matches
                if match.doi.casefold() in distinct_dois
            ]
            selected = min(
                matches_for_doi,
                key=lambda match: (
                    match.pdf is None,
                    abs(match.year - publication_year),
                ),
            )
            identifier_resolution = {
                "resolver": _RESOLVER_NAME,
                "status": "resolved",
                "reason": "exact_title_year_single_doi",
                "matched_title": selected.title,
                "matched_year": selected.year,
                "known_titles_fingerprint": input_fingerprint,
                "cache_hit": False,
            }
            resolved_paper = replace(
                paper,
                doi_original=selected.doi_original,
                doi=selected.doi,
                doi_source=_RESOLVER_NAME,
                identifier_resolution=identifier_resolution,
            )
            resolution = SemanticScholarTitleResolution(
                status="resolved",
                reason="exact_title_year_single_doi",
                paper=resolved_paper,
                doi_original=selected.doi_original,
                matched_title=selected.title,
                matched_year=selected.year,
                pdf=selected.pdf,
                queried_titles=queried_titles,
                query_count=query_count,
                returned_items=returned_items,
                exact_title_matches=exact_title_matches,
                title_year_matches=title_year_matches,
                http_status=200,
            )

        self._store_cache(
            paper,
            normalized_titles=normalized_titles,
            publication_year=publication_year,
            input_fingerprint=input_fingerprint,
            resolution=resolution,
        )
        return resolution

    def cache_path_for(self, paper: PaperCandidate) -> Path | None:
        if self.config.cache_dir is None:
            return None
        return (
            Path(self.config.cache_dir)
            / f"{safe_source_stem(paper.source_id)}.semantic-scholar-title.json"
        )

    def _load_cache(
        self,
        paper: PaperCandidate,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int | None,
        input_fingerprint: str,
        queried_titles: tuple[str, ...],
    ) -> SemanticScholarTitleResolution | None:
        path = self.cache_path_for(paper)
        if path is None:
            return None
        value = load_json_object(path)
        if not value or value.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        if value.get("resolver") != _RESOLVER_NAME:
            return None
        if value.get("source_id") != paper.source_id:
            return None
        raw_input = value.get("input")
        if not isinstance(raw_input, dict):
            return None
        if raw_input.get("known_titles_fingerprint") != input_fingerprint:
            return None
        if raw_input.get("normalized_known_titles") != list(normalized_titles):
            return None
        if raw_input.get("publication_year") != publication_year:
            return None
        expires_at = _parse_timestamp(value.get("expires_at"))
        if expires_at is None or expires_at <= _aware_utc(self._clock()):
            return None

        raw = value.get("result")
        if not isinstance(raw, dict):
            return None
        status = raw.get("status")
        reason = raw.get("reason")
        if (
            status not in _CACHEABLE_STATUSES
            or not isinstance(reason, str)
            or reason not in _STATUS_REASONS[status]
        ):
            return None

        counts = _cache_counts(raw, maximum=len(normalized_titles))
        if counts is None:
            return None
        query_count, returned_items, exact_title_matches, title_year_matches = counts
        resolved_paper = paper
        doi_original = _optional_string(raw.get("doi_original"))
        matched_title = _optional_string(raw.get("matched_title"))
        matched_year = _paper_year(raw.get("matched_year"))
        pdf: SemanticScholarTitleOpenAccessPdf | None = None

        if status == "resolved":
            try:
                doi = normalize_doi(raw.get("doi"))
                original_doi = normalize_doi(doi_original)
            except InvalidDoiError:
                return None
            if (
                doi is None
                or original_doi is None
                or doi_original is None
                or original_doi.casefold() != doi.casefold()
                or normalize_title(matched_title) not in normalized_titles
                or matched_year is None
                or publication_year is None
                or abs(matched_year - publication_year) > 1
            ):
                return None
            raw_pdf = raw.get("open_access_pdf")
            if raw_pdf is not None:
                pdf = _pdf_from_cache(
                    raw_pdf,
                    expected_doi=doi,
                    normalized_titles=frozenset(normalized_titles),
                    publication_year=publication_year,
                )
                if pdf is None:
                    return None
            resolved_paper = replace(
                paper,
                doi_original=doi_original,
                doi=doi,
                doi_source=_RESOLVER_NAME,
                identifier_resolution={
                    "resolver": _RESOLVER_NAME,
                    "status": "resolved",
                    "reason": reason,
                    "matched_title": matched_title,
                    "matched_year": matched_year,
                    "known_titles_fingerprint": input_fingerprint,
                    "cache_hit": True,
                },
            )
        elif any(
            raw.get(key) is not None
            for key in (
                "doi_original",
                "doi",
                "matched_title",
                "matched_year",
                "open_access_pdf",
            )
        ):
            return None

        return SemanticScholarTitleResolution(
            status=status,
            reason=reason,
            paper=resolved_paper,
            doi_original=doi_original,
            matched_title=matched_title,
            matched_year=matched_year,
            pdf=pdf,
            queried_titles=queried_titles,
            query_count=query_count,
            returned_items=returned_items,
            exact_title_matches=exact_title_matches,
            title_year_matches=title_year_matches,
            cache_hit=True,
            http_status=200 if status == "resolved" else None,
        )

    def _fetch_query_with_retries(
        self,
        url: str,
    ) -> tuple[HttpFetch | None, _QueryFailure | None, int, bool]:
        """Fetch one title query, accounting for every retry at the shared gate."""

        retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
        terminal_statuses = {
            300,
            301,
            302,
            303,
            307,
            308,
            401,
            403,
        }
        request_count = 0
        for attempt in range(self.config.retries + 1):
            try:
                request_count += 1
                self._request_gate(url)
                fetch = self.http.get_bytes(
                    url,
                    max_bytes=self.config.max_response_bytes,
                    headers={
                        "Accept": "application/json",
                        "x-api-key": self._api_key,
                    },
                )
            except HttpClientError as exc:
                failure = _QueryFailure(
                    status="request_error",
                    reason="semantic_scholar_title_request_failed",
                    error_code=getattr(exc, "code", "http_error"),
                )
                if not isinstance(exc, RetryableHttpError):
                    return None, failure, request_count, False
                delay = self._retry_delay(None, attempt)
                if attempt < self.config.retries:
                    self._wait_before_retry(delay)
                    continue
                self._defer_future_requests(delay)
                return None, failure, request_count, True

            transport_failure = _validate_fetch_identity(fetch, url)
            if transport_failure is not None:
                return None, transport_failure, request_count, True
            if fetch.status_code in retryable_statuses:
                delay = self._retry_delay(fetch, attempt)
                if attempt < self.config.retries:
                    self._wait_before_retry(delay)
                    continue
                self._defer_future_requests(delay)
                return (
                    None,
                    _http_failure(fetch.status_code),
                    request_count,
                    True,
                )
            if not 200 <= fetch.status_code < 300 and fetch.status_code != 404:
                return (
                    None,
                    _http_failure(fetch.status_code),
                    request_count,
                    fetch.status_code in terminal_statuses,
                )
            return fetch, None, request_count, False

        raise AssertionError("bounded retry loop did not return")

    def _wait_before_retry(self, delay: float) -> None:
        if self._defer_gate is not None:
            # The next request_gate call observes this shared cooldown, so
            # every process using the same key remains under Retry-After.
            self._defer_gate(delay)
        elif delay:
            self._sleep(delay)

    def _defer_future_requests(self, delay: float) -> None:
        if self._defer_gate is not None:
            self._defer_gate(delay)

    def _retry_delay(
        self,
        fetch: HttpFetch | None,
        attempt: int,
    ) -> float:
        retry_after: str | None = None
        if fetch is not None:
            for name, value in fetch.headers.items():
                if name.casefold() == "retry-after":
                    retry_after = value
                    break
        parsed = parse_retry_after(retry_after)
        if parsed is None or parsed < 0:
            return self.config.retry_backoff_seconds * (2**attempt)
        maximum = self.config.max_retry_after_seconds
        return parsed if maximum is None else min(parsed, maximum)

    def _store_cache(
        self,
        paper: PaperCandidate,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int | None,
        input_fingerprint: str,
        resolution: SemanticScholarTitleResolution,
    ) -> None:
        path = self.cache_path_for(paper)
        if path is None or resolution.status not in _CACHEABLE_STATUSES:
            return
        ttl_seconds = (
            self.config.positive_ttl_seconds
            if resolution.status == "resolved"
            else self.config.negative_ttl_seconds
        )
        if ttl_seconds == 0:
            return
        now = _aware_utc(self._clock())
        value = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "resolver": _RESOLVER_NAME,
            "source_id": paper.source_id,
            "input": {
                "known_titles_fingerprint": input_fingerprint,
                "normalized_known_titles": list(normalized_titles),
                "publication_year": publication_year,
            },
            "result": {
                "status": resolution.status,
                "reason": resolution.reason,
                "doi_original": resolution.doi_original,
                "doi": resolution.doi,
                "matched_title": resolution.matched_title,
                "matched_year": resolution.matched_year,
                "open_access_pdf": _serialize_pdf(resolution.pdf),
                "query_count": resolution.query_count,
                "returned_items": resolution.returned_items,
                "exact_title_matches": resolution.exact_title_matches,
                "title_year_matches": resolution.title_year_matches,
            },
            "cached_at": _format_timestamp(now),
            "expires_at": _format_timestamp(
                now + timedelta(seconds=ttl_seconds)
            ),
        }
        try:
            write_json_atomic(path, value)
        except OSError:
            return


def _known_title_entries(paper: PaperCandidate) -> tuple[tuple[str, str], ...]:
    values = (paper.title, *paper.alternate_titles)
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        title = _optional_string(value)
        normalized = normalize_title(title)
        if title is None or normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        entries.append((title, normalized))
    return tuple(entries)


def _input_fingerprint(
    normalized_titles: tuple[str, ...],
    publication_year: int | None,
) -> str:
    serialized = json.dumps(
        {
            "normalized_known_titles": list(normalized_titles),
            "publication_year": publication_year,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _match_url(title: str) -> str:
    return (
        f"{SEMANTIC_SCHOLAR_MATCH_URL}?"
        + urlencode(
            {
                "query": title,
                "fields": SEMANTIC_SCHOLAR_MATCH_FIELDS,
            }
        )
    )


def _is_official_match_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    expected = urlsplit(SEMANTIC_SCHOLAR_MATCH_URL)
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == "api.semanticscholar.org"
        and parsed.path == expected.path
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _validate_fetch_identity(
    fetch: HttpFetch,
    requested_url: str,
) -> _QueryFailure | None:
    if (
        fetch.requested_url != requested_url
        or fetch.final_url != requested_url
        or fetch.redirects
        or not _is_official_match_url(fetch.final_url)
    ):
        return _QueryFailure(
            status="invalid_response",
            reason="semantic_scholar_redirect_rejected",
            http_status=fetch.status_code,
            error_code="redirect_not_allowed",
        )
    return None


def _match_item_from_envelope(
    payload: Any,
    *,
    http_status: int,
) -> tuple[Mapping[str, Any] | None, _QueryFailure | None]:
    """Extract the sole paper from the official search/match envelope.

    The current endpoint contract is exactly ``{"data": [...]}``.  A flat
    paper object is intentionally not accepted: treating it as a paper was the
    source of false ``no_match`` decisions when the API returned its documented
    envelope.  The match endpoint may return zero or one paper; more than one
    is an invalid/ambiguous response and is never used for DOI adoption.
    """

    if (
        not isinstance(payload, dict)
        or frozenset(payload) != _MATCH_ENVELOPE_KEYS
    ):
        return None, _QueryFailure(
            status="invalid_response",
            reason="invalid_semantic_scholar_title_payload",
            http_status=http_status,
            error_code="invalid_payload",
        )

    data = payload.get("data")
    if not isinstance(data, list):
        return None, _QueryFailure(
            status="invalid_response",
            reason="invalid_semantic_scholar_title_payload",
            http_status=http_status,
            error_code="invalid_payload",
        )
    if len(data) > _MAX_MATCH_ITEMS:
        return None, _QueryFailure(
            status="ambiguous",
            reason="multiple_match_items",
            http_status=http_status,
            error_code="multiple_match_items",
        )
    if not data:
        return None, None

    item = data[0]
    if not isinstance(item, dict):
        return None, _QueryFailure(
            status="invalid_response",
            reason="invalid_semantic_scholar_title_payload",
            http_status=http_status,
            error_code="invalid_payload",
        )
    return item, None


def _http_failure(status_code: int) -> _QueryFailure:
    if status_code == 429:
        reason = "semantic_scholar_rate_limited"
        error_code = "rate_limited"
    elif 500 <= status_code <= 599:
        reason = "semantic_scholar_server_error"
        error_code = "server_error"
    elif 300 <= status_code <= 399:
        reason = "semantic_scholar_redirect_rejected"
        error_code = "redirect_not_allowed"
    elif status_code in {401, 403}:
        reason = "semantic_scholar_authentication_failed"
        error_code = "authentication_failed"
    else:
        reason = "semantic_scholar_title_http_error"
        error_code = "http_error"
    return _QueryFailure(
        status="http_error",
        reason=reason,
        http_status=status_code,
        error_code=error_code,
    )


def _preferred_failure(failures: list[_QueryFailure]) -> _QueryFailure:
    # Identity ambiguity must dominate transient or malformed sibling queries:
    # otherwise a later provider's single DOI could be adopted despite the
    # match endpoint having returned multiple candidates.
    rank = {
        "ambiguous": -1,
        "request_error": 0,
        "http_error": 1,
        "invalid_response": 2,
    }
    return min(
        failures,
        key=lambda failure: (
            rank.get(failure.status, 9),
            failure.http_status or 0,
        ),
    )


def _oa_pdf_from_item(
    item: Mapping[str, Any],
    *,
    doi: str,
    title: str,
    year: int,
) -> SemanticScholarTitleOpenAccessPdf | None:
    if item.get("isOpenAccess") is not True:
        return None
    raw = item.get("openAccessPdf")
    if not isinstance(raw, Mapping):
        return None
    url = _validated_https_url(raw.get("url"))
    if url is None:
        return None
    metadata = _pdf_metadata(raw)
    if metadata is None:
        return None
    status, license_value, disclaimer = metadata
    return SemanticScholarTitleOpenAccessPdf(
        doi=doi,
        url=url,
        title=title,
        year=year,
        status=status,
        license=license_value,
        disclaimer=disclaimer,
    )


def _pdf_metadata(
    value: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None] | None:
    result: list[str | None] = []
    for key in ("status", "license", "disclaimer"):
        raw = value.get(key)
        if raw is not None and not isinstance(raw, str):
            return None
        result.append(_optional_string(raw))
    return result[0], result[1], result[2]


def _serialize_pdf(
    pdf: SemanticScholarTitleOpenAccessPdf | None,
) -> dict[str, Any] | None:
    if pdf is None:
        return None
    return {
        "doi": pdf.doi,
        "url": pdf.url,
        "title": pdf.title,
        "year": pdf.year,
        "status": pdf.status,
        "license": pdf.license,
        "disclaimer": pdf.disclaimer,
    }


def _pdf_from_cache(
    value: Any,
    *,
    expected_doi: str,
    normalized_titles: frozenset[str],
    publication_year: int,
) -> SemanticScholarTitleOpenAccessPdf | None:
    if not isinstance(value, dict):
        return None
    try:
        doi = normalize_doi(value.get("doi"))
    except InvalidDoiError:
        return None
    title = _optional_string(value.get("title"))
    year = _paper_year(value.get("year"))
    url = _validated_https_url(value.get("url"))
    metadata = _pdf_metadata(value)
    if (
        doi is None
        or doi.casefold() != expected_doi.casefold()
        or title is None
        or normalize_title(title) not in normalized_titles
        or year is None
        or abs(year - publication_year) > 1
        or url is None
        or metadata is None
    ):
        return None
    status, license_value, disclaimer = metadata
    return SemanticScholarTitleOpenAccessPdf(
        doi=doi,
        url=url,
        title=title,
        year=year,
        status=status,
        license=license_value,
        disclaimer=disclaimer,
    )


def _cache_counts(
    value: Mapping[str, Any],
    *,
    maximum: int,
) -> tuple[int, int, int, int] | None:
    query_count = _bounded_count(value.get("query_count"), maximum=maximum)
    returned_items = _bounded_count(
        value.get("returned_items"),
        maximum=maximum,
    )
    exact_title_matches = _bounded_count(
        value.get("exact_title_matches"),
        maximum=returned_items if returned_items is not None else 0,
    )
    title_year_matches = _bounded_count(
        value.get("title_year_matches"),
        maximum=exact_title_matches if exact_title_matches is not None else 0,
    )
    if None in (
        query_count,
        returned_items,
        exact_title_matches,
        title_year_matches,
    ):
        return None
    return (
        query_count,
        returned_items,
        exact_title_matches,
        title_year_matches,
    )


def _bounded_count(value: Any, *, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= maximum else None


def _validated_api_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("api_key must be a string")
    stripped = value.strip()
    if (
        not stripped
        or not stripped.isascii()
        or any(character.isspace() or ord(character) < 32 for character in stripped)
    ):
        raise ValueError(
            "api_key must be nonempty ASCII without whitespace or controls"
        )
    return stripped


def _validated_https_url(value: Any) -> str | None:
    url = _optional_string(value)
    if url is None or any(
        character.isspace() or ord(character) < 32 for character in url
    ):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    return (
        url
        if (
            parsed.scheme.casefold() == "https"
            and bool(parsed.hostname)
            and port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
        else None
    )


def _paper_year(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1000 <= value <= 9999 else None


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)
