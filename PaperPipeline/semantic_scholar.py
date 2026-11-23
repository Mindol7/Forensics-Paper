"""Conservative Semantic Scholar lookup for public PDF metadata.

Only Semantic Scholar's official Academic Graph batch endpoint is queried.
An ``openAccessPdf`` URL is exposed to the downloader only after the response
DOI, normalized title, and publication year all match the input paper.  This
module deliberately performs no fuzzy matching and never treats a merely
present URL as evidence of open access.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Iterator, Mapping
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import (
    HttpFetch,
    PerHostRateLimiter,
    ResponseTooLargeError,
    RetryableHttpError,
    parse_retry_after,
)
from PaperPipeline.identifier_resolver import normalize_title
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import load_json_object, write_json_atomic


SEMANTIC_SCHOLAR_MAX_BATCH = 500
SEMANTIC_SCHOLAR_BATCH_URL = (
    "https://api.semanticscholar.org/graph/v1/paper/batch"
    "?fields=title,year,externalIds,isOpenAccess,openAccessPdf"
)
CACHE_SCHEMA_VERSION = "1.1"
_RESOLVER_NAME = "semantic-scholar"
_CACHEABLE_STATUSES = frozenset(
    {
        "resolved",
        "invalid_doi",
        "missing_metadata",
        "not_found",
        "identity_mismatch",
        "not_open_access",
        "missing_pdf_url",
        "invalid_pdf_metadata",
    }
)


class SemanticScholarHttp(Protocol):
    """Small HTTP seam used by the batch resolver and its tests."""

    def post_json_bytes(
        self,
        url: str,
        *,
        json_body: Mapping[str, object],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """POST a JSON object and return a bounded response body."""


@dataclass(frozen=True, slots=True)
class SemanticScholarConfig:
    """Runtime and cache settings for :class:`SemanticScholarBatchResolver`."""

    cache_path: Path | None = None
    batch_size: int = SEMANTIC_SCHOLAR_MAX_BATCH
    max_response_bytes: int = 16 * 1024 * 1024
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    # New API keys start at 1 request/second across all endpoints.  Stay
    # deliberately below that ceiling.
    requests_per_second: float = 0.045
    retries: int = 0
    retry_backoff_seconds: float = 2.0
    # ``None`` honors the complete server-provided cooldown.  A caller may
    # set an explicit cap when a bounded job runtime is more important.
    max_retry_after_seconds: float | None = None
    # Anonymous access remains the default.  Callers that already have an
    # official Semantic Scholar key may opt in without changing transports.
    api_key: str | None = field(default=None, repr=False)
    positive_ttl_seconds: int = 30 * 24 * 60 * 60
    negative_ttl_seconds: int = 24 * 60 * 60
    force_refresh: bool = False


@dataclass(frozen=True, slots=True)
class SemanticScholarOpenAccessPdf:
    """Validated metadata retained from ``openAccessPdf``."""

    doi: str
    url: str
    title: str
    year: int
    status: str | None = None
    license: str | None = None
    disclaimer: str | None = None

    def as_resolver_candidate(self) -> ResolverCandidate:
        """Convert this record to the downloader's common candidate shape."""

        return ResolverCandidate(
            url=self.url,
            resolver=_RESOLVER_NAME,
            license=self.license,
            version=self.status,
            is_open_access=True,
            verified_doi=self.doi,
        )


@dataclass(frozen=True, slots=True)
class SemanticScholarResolution:
    """One auditable decision for one input paper."""

    source_id: str
    doi: str | None
    status: str
    reason: str
    pdf: SemanticScholarOpenAccessPdf | None = None
    cache_hit: bool = False
    http_status: int | None = None
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.pdf is not None

    @property
    def candidate(self) -> ResolverCandidate | None:
        return self.pdf.as_resolver_candidate() if self.pdf is not None else None


class SemanticScholarIndex(Mapping[str, SemanticScholarResolution]):
    """Read-only source-id index with DOI-oriented PDF lookup helpers."""

    def __init__(
        self,
        results: Mapping[str, SemanticScholarResolution],
        *,
        input_fingerprint: str,
    ) -> None:
        copied = dict(results)
        self._results: Mapping[str, SemanticScholarResolution] = MappingProxyType(
            copied
        )
        pdfs: dict[str, SemanticScholarOpenAccessPdf] = {}
        for resolution in copied.values():
            if resolution.pdf is not None:
                pdfs.setdefault(resolution.pdf.doi.casefold(), resolution.pdf)
        self._pdfs_by_doi: Mapping[str, SemanticScholarOpenAccessPdf] = (
            MappingProxyType(pdfs)
        )
        self.input_fingerprint = input_fingerprint

    def __getitem__(self, source_id: str) -> SemanticScholarResolution:
        return self._results[source_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._results)

    def __len__(self) -> int:
        return len(self._results)

    @property
    def results(self) -> Mapping[str, SemanticScholarResolution]:
        return self._results

    @property
    def open_access_pdfs(self) -> Mapping[str, SemanticScholarOpenAccessPdf]:
        """Resolved PDFs keyed by a case-folded canonical DOI."""

        return self._pdfs_by_doi

    def pdf_for_doi(self, doi: str | None) -> SemanticScholarOpenAccessPdf | None:
        """Return validated PDF metadata for ``doi`` when present."""

        try:
            canonical = normalize_doi(doi)
        except InvalidDoiError:
            return None
        return (
            self._pdfs_by_doi.get(canonical.casefold())
            if canonical is not None
            else None
        )

    def candidate_for(
        self, paper_or_source_id: PaperCandidate | str
    ) -> ResolverCandidate | None:
        """Return the common downloader candidate for one indexed paper."""

        source_id = (
            paper_or_source_id.source_id
            if isinstance(paper_or_source_id, PaperCandidate)
            else paper_or_source_id
        )
        resolution = self._results.get(source_id)
        return resolution.candidate if resolution is not None else None


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    resolution: SemanticScholarResolution
    cached_at: datetime
    expires_at: datetime


class SemanticScholarBatchResolver:
    """Resolve DOI papers through the anonymous Academic Graph batch API."""

    def __init__(
        self,
        config: SemanticScholarConfig | None = None,
        *,
        http: SemanticScholarHttp | None = None,
        request_gate: Callable[[str], None] | None = None,
        defer_gate: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or SemanticScholarConfig()
        if not 1 <= self.config.batch_size <= SEMANTIC_SCHOLAR_MAX_BATCH:
            raise ValueError(
                f"batch_size must be between 1 and {SEMANTIC_SCHOLAR_MAX_BATCH}"
            )
        if self.config.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if self.config.connect_timeout <= 0 or self.config.read_timeout <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if not 0 < self.config.requests_per_second < 1:
            raise ValueError(
                "requests_per_second must be positive and below 1"
            )
        if self.config.retries < 0:
            raise ValueError("retries must be non-negative")
        if self.config.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        if (
            self.config.max_retry_after_seconds is not None
            and self.config.max_retry_after_seconds < 0
        ):
            raise ValueError("max_retry_after_seconds must be non-negative")
        if self.config.api_key is not None and (
            not self.config.api_key.strip()
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.config.api_key
            )
        ):
            raise ValueError("api_key must be nonempty and contain no controls")
        if self.config.positive_ttl_seconds < 0:
            raise ValueError("positive_ttl_seconds must be non-negative")
        if self.config.negative_ttl_seconds < 0:
            raise ValueError("negative_ttl_seconds must be non-negative")
        if http is None:
            effective_request_gate = request_gate or PerHostRateLimiter(
                self.config.requests_per_second
            ).wait
            self.http = _RequestsBatchHttp(
                connect_timeout=self.config.connect_timeout,
                read_timeout=self.config.read_timeout,
                request_gate=effective_request_gate,
            )
        else:
            self.http = http
        self._defer_gate = defer_gate
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper

    def resolve(
        self, papers: Iterable[PaperCandidate]
    ) -> SemanticScholarIndex:
        """Return a decision for every input paper.

        The endpoint receives at most ``batch_size`` unique DOI identifiers in
        any POST.  Duplicate source IDs are rejected because they cannot be
        represented unambiguously by the returned source-id mapping.
        """

        paper_list = tuple(papers)
        by_source_id: dict[str, PaperCandidate] = {}
        for paper in paper_list:
            if paper.source_id in by_source_id:
                raise ValueError(f"duplicate source_id: {paper.source_id!r}")
            by_source_id[paper.source_id] = paper

        inputs = _input_descriptors(paper_list)
        fingerprint = _input_fingerprint(inputs)
        now = _aware_utc(self._clock())
        cached_entries = (
            {}
            if self.config.force_refresh
            else self._load_cache(
                papers=by_source_id,
                inputs=inputs,
                fingerprint=fingerprint,
                now=now,
            )
        )
        results: dict[str, SemanticScholarResolution] = {
            source_id: entry.resolution
            for source_id, entry in cached_entries.items()
        }
        entries_to_store = dict(cached_entries)
        papers_by_doi: dict[str, list[tuple[PaperCandidate, str]]] = {}
        made_new_decision = False

        for paper in paper_list:
            if paper.source_id in results:
                continue
            canonical_doi, invalid_doi = _paper_doi(paper)
            if invalid_doi:
                resolution = SemanticScholarResolution(
                    source_id=paper.source_id,
                    doi=None,
                    status="invalid_doi",
                    reason="invalid_input_doi",
                )
                results[paper.source_id] = resolution
                made_new_decision = True
                self._add_cache_entry(entries_to_store, resolution, now)
                continue

            missing: list[str] = []
            if canonical_doi is None:
                missing.append("doi")
            if not _known_normalized_titles(paper):
                missing.append("title")
            if _paper_year(paper.publication_year) is None:
                missing.append("publication_year")
            if missing:
                resolution = SemanticScholarResolution(
                    source_id=paper.source_id,
                    doi=canonical_doi,
                    status="missing_metadata",
                    reason="missing_" + "_and_".join(missing),
                )
                results[paper.source_id] = resolution
                made_new_decision = True
                self._add_cache_entry(entries_to_store, resolution, now)
                continue

            assert canonical_doi is not None
            papers_by_doi.setdefault(canonical_doi.casefold(), []).append(
                (paper, canonical_doi)
            )

        doi_groups = list(papers_by_doi.values())
        for offset in range(0, len(doi_groups), self.config.batch_size):
            chunk = doi_groups[offset : offset + self.config.batch_size]
            new_results = self._resolve_chunk(chunk)
            made_new_decision = True
            for resolution in new_results:
                results[resolution.source_id] = resolution
                self._add_cache_entry(entries_to_store, resolution, now)

        # Preserve the caller's iteration order in the public index and cache.
        ordered_results = {
            paper.source_id: results[paper.source_id] for paper in paper_list
        }
        if made_new_decision:
            self._store_cache(
                inputs=inputs,
                fingerprint=fingerprint,
                entries=entries_to_store,
                now=now,
            )
        return SemanticScholarIndex(
            ordered_results,
            input_fingerprint=fingerprint,
        )

    def resolve_many(
        self, papers: Iterable[PaperCandidate]
    ) -> SemanticScholarIndex:
        """Alias for :meth:`resolve` for call sites that prefer an explicit name."""

        return self.resolve(papers)

    def _resolve_chunk(
        self,
        groups: list[list[tuple[PaperCandidate, str]]],
    ) -> list[SemanticScholarResolution]:
        requested_dois = [group[0][1] for group in groups]
        body = {"ids": [f"DOI:{doi}" for doi in requested_dois]}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.config.api_key is not None:
            headers["x-api-key"] = self.config.api_key.strip()
        fetch: HttpFetch | None = None
        request_error: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                response = self.http.post_json_bytes(
                    SEMANTIC_SCHOLAR_BATCH_URL,
                    json_body=body,
                    max_bytes=self.config.max_response_bytes,
                    headers=headers,
                )
            except Exception as exc:
                if (
                    isinstance(exc, RetryableHttpError)
                    and attempt < self.config.retries
                ):
                    delay = self._retry_delay(None, attempt)
                    if self._defer_gate is not None:
                        self._defer_gate(delay)
                    self._sleep(delay)
                    continue
                request_error = exc
                break
            if (
                response.status_code in {408, 425, 429, 500, 502, 503, 504}
                and attempt < self.config.retries
            ):
                delay = self._retry_delay(response, attempt)
                if self._defer_gate is not None:
                    self._defer_gate(delay)
                self._sleep(delay)
                continue
            fetch = response
            break

        if request_error is not None:
            if (
                isinstance(request_error, RetryableHttpError)
                and self._defer_gate is not None
            ):
                self._defer_gate(
                    self._retry_delay(None, self.config.retries)
                )
            return [
                _error_resolution(
                    paper,
                    canonical_doi,
                    status="request_error",
                    reason="semantic_scholar_request_failed",
                    error_code=getattr(
                        request_error,
                        "code",
                        request_error.__class__.__name__.casefold(),
                    ),
                )
                for group in groups
                for paper, canonical_doi in group
            ]
        assert fetch is not None

        if not 200 <= fetch.status_code < 300:
            if (
                fetch.status_code
                in {408, 425, 429, 500, 502, 503, 504}
                and self._defer_gate is not None
            ):
                self._defer_gate(
                    self._retry_delay(fetch, self.config.retries)
                )
            return [
                _error_resolution(
                    paper,
                    canonical_doi,
                    status="http_error",
                    reason="semantic_scholar_http_error",
                    http_status=fetch.status_code,
                )
                for group in groups
                for paper, canonical_doi in group
            ]
        try:
            payload = json.loads(fetch.body)
        except (UnicodeError, json.JSONDecodeError):
            return [
                _error_resolution(
                    paper,
                    canonical_doi,
                    status="invalid_response",
                    reason="invalid_semantic_scholar_json",
                    http_status=fetch.status_code,
                )
                for group in groups
                for paper, canonical_doi in group
            ]
        if (
            not isinstance(payload, list)
            or len(payload) != len(groups)
            or any(item is not None and not isinstance(item, dict) for item in payload)
        ):
            return [
                _error_resolution(
                    paper,
                    canonical_doi,
                    status="invalid_response",
                    reason="invalid_semantic_scholar_payload",
                    http_status=fetch.status_code,
                )
                for group in groups
                for paper, canonical_doi in group
            ]

        resolutions: list[SemanticScholarResolution] = []
        for group, item in zip(groups, payload, strict=True):
            for paper, canonical_doi in group:
                resolutions.append(
                    _match_response(
                        paper,
                        canonical_doi=canonical_doi,
                        item=item,
                        http_status=fetch.status_code,
                    )
                )
        return resolutions

    def _retry_delay(
        self,
        fetch: HttpFetch | None,
        attempt: int,
    ) -> float:
        if fetch is not None:
            for name, value in fetch.headers.items():
                if name.casefold() != "retry-after":
                    continue
                seconds = parse_retry_after(value)
                if seconds is not None and seconds >= 0:
                    maximum = self.config.max_retry_after_seconds
                    return seconds if maximum is None else min(seconds, maximum)
                break
        return self.config.retry_backoff_seconds * (2**attempt)

    def _add_cache_entry(
        self,
        entries: dict[str, _CacheEntry],
        resolution: SemanticScholarResolution,
        now: datetime,
    ) -> None:
        if resolution.status not in _CACHEABLE_STATUSES:
            entries.pop(resolution.source_id, None)
            return
        ttl = (
            self.config.positive_ttl_seconds
            if resolution.status == "resolved"
            else self.config.negative_ttl_seconds
        )
        if ttl == 0:
            entries.pop(resolution.source_id, None)
            return
        entries[resolution.source_id] = _CacheEntry(
            resolution=resolution,
            cached_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )

    def _load_cache(
        self,
        *,
        papers: Mapping[str, PaperCandidate],
        inputs: list[dict[str, Any]],
        fingerprint: str,
        now: datetime,
    ) -> dict[str, _CacheEntry]:
        path = self.config.cache_path
        if path is None:
            return {}
        value = load_json_object(Path(path))
        if not value or value.get("schema_version") != CACHE_SCHEMA_VERSION:
            return {}
        if value.get("resolver") != _RESOLVER_NAME:
            return {}
        if value.get("input_fingerprint") != fingerprint:
            return {}
        if value.get("inputs") != inputs:
            return {}
        raw_results = value.get("results")
        if not isinstance(raw_results, dict):
            return {}

        loaded: dict[str, _CacheEntry] = {}
        for source_id, paper in papers.items():
            raw_entry = raw_results.get(source_id)
            if not isinstance(raw_entry, dict):
                continue
            cached_at = _parse_timestamp(raw_entry.get("cached_at"))
            expires_at = _parse_timestamp(raw_entry.get("expires_at"))
            raw_resolution = raw_entry.get("result")
            if (
                cached_at is None
                or expires_at is None
                or expires_at <= now
                or not isinstance(raw_resolution, dict)
            ):
                continue
            resolution = _resolution_from_cache(paper, raw_resolution)
            if resolution is None:
                continue
            loaded[source_id] = _CacheEntry(
                resolution=resolution,
                cached_at=cached_at,
                expires_at=expires_at,
            )
        return loaded

    def _store_cache(
        self,
        *,
        inputs: list[dict[str, Any]],
        fingerprint: str,
        entries: Mapping[str, _CacheEntry],
        now: datetime,
    ) -> None:
        path = self.config.cache_path
        if path is None:
            return
        results: dict[str, Any] = {}
        for descriptor in inputs:
            source_id = descriptor["source_id"]
            entry = entries.get(source_id)
            if entry is None:
                continue
            results[source_id] = {
                "cached_at": _format_timestamp(entry.cached_at),
                "expires_at": _format_timestamp(entry.expires_at),
                "result": _serialize_resolution(entry.resolution),
            }
        value = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "resolver": _RESOLVER_NAME,
            "input_fingerprint": fingerprint,
            "inputs": inputs,
            "cached_at": _format_timestamp(now),
            "results": results,
        }
        try:
            write_json_atomic(Path(path), value)
        except OSError:
            # Metadata resolution remains useful when the optional cache is
            # temporarily unwritable.
            return


class _RequestsBatchHttp:
    """Minimal anonymous JSON POST client constrained to the official endpoint."""

    def __init__(
        self,
        *,
        connect_timeout: float,
        read_timeout: float,
        session: requests.Session | None = None,
        request_gate: Callable[[str], None] | None = None,
    ) -> None:
        self.timeout = (connect_timeout, read_timeout)
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._request_gate = request_gate

    def post_json_bytes(
        self,
        url: str,
        *,
        json_body: Mapping[str, object],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        if url != SEMANTIC_SCHOLAR_BATCH_URL:
            raise ValueError("Semantic Scholar HTTP client only accepts its batch URL")
        request_headers = {
            "User-Agent": "Forensics-Paper/1.0 (public scholarly PDF resolver)",
            **dict(headers or {}),
        }
        if self._request_gate is not None:
            self._request_gate(url)
        try:
            response = self.session.post(
                url,
                json=dict(json_body),
                headers=request_headers,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
                verify=True,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RetryableHttpError(str(exc)) from exc
        try:
            content_length = _content_length(response.headers.get("Content-Length"))
            if content_length is not None and content_length > max_bytes:
                raise ResponseTooLargeError(
                    f"declared Content-Length exceeds {max_bytes} bytes"
                )
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLargeError(
                        f"decoded response exceeds {max_bytes} bytes"
                    )
            return HttpFetch(
                requested_url=url,
                final_url=url,
                status_code=int(response.status_code),
                headers=dict(response.headers),
                body=bytes(body),
                redirects=(),
            )
        finally:
            response.close()


def _match_response(
    paper: PaperCandidate,
    *,
    canonical_doi: str,
    item: Mapping[str, Any] | None,
    http_status: int,
) -> SemanticScholarResolution:
    common = {
        "source_id": paper.source_id,
        "doi": canonical_doi,
        "http_status": http_status,
    }
    if item is None:
        return SemanticScholarResolution(
            status="not_found",
            reason="doi_not_found",
            **common,
        )

    external_ids = item.get("externalIds")
    external_doi = (
        external_ids.get("DOI") if isinstance(external_ids, dict) else None
    )
    try:
        response_doi = normalize_doi(external_doi)
    except InvalidDoiError:
        response_doi = None
    if response_doi is None or response_doi.casefold() != canonical_doi.casefold():
        return SemanticScholarResolution(
            status="identity_mismatch",
            reason="external_doi_mismatch",
            **common,
        )

    response_title = _optional_string(item.get("title"))
    if (
        response_title is None
        or normalize_title(response_title) not in _known_normalized_titles(paper)
    ):
        return SemanticScholarResolution(
            status="identity_mismatch",
            reason="title_mismatch",
            **common,
        )
    response_year = _paper_year(item.get("year"))
    paper_year = _paper_year(paper.publication_year)
    if (
        response_year is None
        or paper_year is None
        or abs(response_year - paper_year) > 1
    ):
        return SemanticScholarResolution(
            status="identity_mismatch",
            reason="year_mismatch",
            **common,
        )

    if item.get("isOpenAccess") is not True:
        return SemanticScholarResolution(
            status="not_open_access",
            reason="is_open_access_not_true",
            **common,
        )
    raw_pdf = item.get("openAccessPdf")
    if not isinstance(raw_pdf, dict):
        return SemanticScholarResolution(
            status="missing_pdf_url",
            reason="open_access_pdf_missing",
            **common,
        )
    url = _optional_string(raw_pdf.get("url"))
    if url is None or not _is_valid_https_url(url):
        return SemanticScholarResolution(
            status="missing_pdf_url",
            reason="open_access_pdf_url_not_https",
            **common,
        )
    metadata = _pdf_metadata_text(raw_pdf)
    if metadata is None:
        return SemanticScholarResolution(
            status="invalid_pdf_metadata",
            reason="invalid_open_access_pdf_metadata",
            **common,
        )
    status, license_value, disclaimer = metadata
    pdf = SemanticScholarOpenAccessPdf(
        doi=response_doi,
        url=url,
        title=response_title,
        year=response_year,
        status=status,
        license=license_value,
        disclaimer=disclaimer,
    )
    return SemanticScholarResolution(
        status="resolved",
        reason="exact_doi_title_year_open_access_match",
        pdf=pdf,
        **common,
    )


def _resolution_from_cache(
    paper: PaperCandidate,
    raw: Mapping[str, Any],
) -> SemanticScholarResolution | None:
    status = raw.get("status")
    reason = raw.get("reason")
    if status not in _CACHEABLE_STATUSES or not isinstance(reason, str):
        return None
    canonical_doi, invalid_doi = _paper_doi(paper)
    raw_doi = _optional_string(raw.get("doi"))
    if raw_doi is not None:
        try:
            cached_doi = normalize_doi(raw_doi)
        except InvalidDoiError:
            return None
    else:
        cached_doi = None
    if (cached_doi or "").casefold() != (canonical_doi or "").casefold():
        return None
    if invalid_doi != (status == "invalid_doi"):
        return None

    http_status = _optional_int(raw.get("http_status"))
    if status != "resolved":
        return SemanticScholarResolution(
            source_id=paper.source_id,
            doi=canonical_doi,
            status=status,
            reason=reason,
            cache_hit=True,
            http_status=http_status,
        )

    raw_pdf = raw.get("open_access_pdf")
    if canonical_doi is None or not isinstance(raw_pdf, dict):
        return None
    external_doi = _optional_string(raw_pdf.get("doi"))
    try:
        pdf_doi = normalize_doi(external_doi)
    except InvalidDoiError:
        return None
    title = _optional_string(raw_pdf.get("title"))
    year = _paper_year(raw_pdf.get("year"))
    url = _optional_string(raw_pdf.get("url"))
    if (
        pdf_doi is None
        or pdf_doi.casefold() != canonical_doi.casefold()
        or title is None
        or normalize_title(title) not in _known_normalized_titles(paper)
        or year is None
        or _paper_year(paper.publication_year) is None
        or abs(year - int(paper.publication_year)) > 1
        or url is None
        or not _is_valid_https_url(url)
    ):
        return None
    metadata = _pdf_metadata_text(raw_pdf)
    if metadata is None:
        return None
    status_value, license_value, disclaimer = metadata
    pdf = SemanticScholarOpenAccessPdf(
        doi=pdf_doi,
        url=url,
        title=title,
        year=year,
        status=status_value,
        license=license_value,
        disclaimer=disclaimer,
    )
    return SemanticScholarResolution(
        source_id=paper.source_id,
        doi=canonical_doi,
        status="resolved",
        reason=reason,
        pdf=pdf,
        cache_hit=True,
        http_status=http_status,
    )


def _serialize_resolution(resolution: SemanticScholarResolution) -> dict[str, Any]:
    value: dict[str, Any] = {
        "status": resolution.status,
        "reason": resolution.reason,
        "doi": resolution.doi,
        "http_status": resolution.http_status,
    }
    if resolution.pdf is not None:
        value["open_access_pdf"] = {
            "doi": resolution.pdf.doi,
            "url": resolution.pdf.url,
            "title": resolution.pdf.title,
            "year": resolution.pdf.year,
            "status": resolution.pdf.status,
            "license": resolution.pdf.license,
            "disclaimer": resolution.pdf.disclaimer,
        }
    return value


def _error_resolution(
    paper: PaperCandidate,
    canonical_doi: str,
    *,
    status: str,
    reason: str,
    http_status: int | None = None,
    error_code: str | None = None,
) -> SemanticScholarResolution:
    return SemanticScholarResolution(
        source_id=paper.source_id,
        doi=canonical_doi,
        status=status,
        reason=reason,
        http_status=http_status,
        error_code=error_code,
    )


def _input_descriptors(
    papers: Iterable[PaperCandidate],
) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for paper in papers:
        canonical_doi, invalid = _paper_doi(paper)
        doi_value: str | None
        if invalid:
            doi_value = f"invalid:{paper.doi!s}"
        else:
            doi_value = canonical_doi.casefold() if canonical_doi else None
        descriptors.append(
            {
                "source_id": paper.source_id,
                "doi": doi_value,
                "normalized_titles": list(_known_normalized_titles(paper)),
                "publication_year": _paper_year(paper.publication_year),
            }
        )
    return sorted(
        descriptors,
        key=lambda item: (
            item["source_id"],
            item["doi"] or "",
            "\u0000".join(item["normalized_titles"]),
            item["publication_year"] or 0,
        ),
    )


def _input_fingerprint(inputs: list[dict[str, Any]]) -> str:
    serialized = json.dumps(
        inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _paper_doi(paper: PaperCandidate) -> tuple[str | None, bool]:
    try:
        return normalize_doi(paper.doi), False
    except InvalidDoiError:
        return None, True


def _known_normalized_titles(paper: PaperCandidate) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for title in (paper.title, *paper.alternate_titles):
        normalized = normalize_title(title)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        values.append(normalized)
    return tuple(values)


def _pdf_metadata_text(
    value: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None] | None:
    fields: list[str | None] = []
    for key in ("status", "license", "disclaimer"):
        raw = value.get(key)
        if raw is not None and not isinstance(raw, str):
            return None
        fields.append(_optional_string(raw))
    return fields[0], fields[1], fields[2]


def _is_valid_https_url(value: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
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


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)
