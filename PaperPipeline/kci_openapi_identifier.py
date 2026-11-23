"""Conservative DOI recovery from KCI's official ``articleDetail`` API.

The KCI article identifier is already an authoritative lookup key, but a DOI
is still accepted only after the returned record independently agrees with the
database candidate's ART id, at least one complete known title, and publication
year. Matching is equality-only apart from a terminal subtitle marker; there
is no fuzzy, prefix, edit-distance, or year-window fallback.

The API key is carried in a redacting query-parameter mapping at the HTTP
boundary.  It is never appended to the endpoint passed around by the resolver,
included in a result/audit record, or written to the optional cache.
"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    ResponseTooLargeError,
    RetryableHttpError,
)
from PaperPipeline.identifier_resolver import (
    IdentifierResolution,
    normalize_title,
    paper_title_queries,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import load_json_object, write_json_atomic


KCI_OPENAPI_DETAIL_URL = (
    "https://open.kci.go.kr/po/openapi/openApiSearch.kci"
)
KCI_OPENAPI_API_CODE = "articleDetail"
KCI_IDENTIFIER_CACHE_SCHEMA_VERSION = "1.3"
DEFAULT_KCI_IDENTIFIER_CACHE_DIR = Path(
    "Analyzer/Manifest/.identifier-cache-kci"
)

_RESOLVER_NAME = "kci-openapi-detail"
_ART_ID = re.compile(r"ART[0-9]{9}\Z")
_TRAILING_TITLE_DELIMITER_RE = re.compile(
    r"(?:\s+[-‐‑‒–—―:;])+\Z"
)
_FORBIDDEN_XML_DECLARATION = re.compile(
    br"<!\s*(?:DOCTYPE|ENTITY)\b",
    flags=re.IGNORECASE,
)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_CACHEABLE_STATUSES = frozenset(
    {"resolved", "no_match", "identity_mismatch", "ambiguous"}
)
_SUCCESS_MESSAGES = frozenset(
    {"정상 처리되었습니다.", "ok", "success"}
)
_NO_DATA_MESSAGES = frozenset(
    {"검색 조건이 없습니다", "검색 조건이 없습니다.", "no data"}
)


class KciOpenApiHttp(Protocol):
    """Small HTTP seam used by :class:`KciOpenApiIdentifierResolver`."""

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """GET ``url`` with separately supplied query parameters."""


class _RedactedQueryParams(Mapping[str, str]):
    """A mapping whose string representations never reveal secret values."""

    __slots__ = ("_values", "_sensitive")

    def __init__(
        self,
        values: Mapping[str, str],
        *,
        sensitive: frozenset[str],
    ) -> None:
        self._values = dict(values)
        self._sensitive = sensitive

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        rendered = {
            key: "<redacted>" if key in self._sensitive else value
            for key, value in self._values.items()
        }
        return f"{type(self).__name__}({rendered!r})"

    __str__ = __repr__


class KciOpenApiHttpClient:
    """Minimal fixed-origin HTTPS transport for KCI article-detail requests.

    Redirects are deliberately not followed: forwarding a query-string API
    key to a new URL would be unsafe.  Returned fetch metadata contains the
    fixed, query-free endpoint and a small allowlist of response headers.
    """

    def __init__(
        self,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        user_agent: str = "Forensics-Paper/1.0 (KCI article identifier resolver)",
        request_gate: Callable[[str], None] | None = None,
    ) -> None:
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if _has_control(user_agent):
            raise ValueError("user_agent must contain no control characters")
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.user_agent = user_agent
        self._request_gate = request_gate

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        if url != KCI_OPENAPI_DETAIL_URL or not _is_exact_endpoint(url):
            raise HttpClientError("KCI Open API endpoint is not allowlisted")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if set(params) != {"apiCode", "key", "id"}:
            raise HttpClientError("unexpected KCI Open API parameters")
        if params["apiCode"] != KCI_OPENAPI_API_CODE:
            raise HttpClientError("unexpected KCI Open API code")
        if _ART_ID.fullmatch(params["id"]) is None:
            raise HttpClientError("invalid KCI article id")
        if not params["key"] or any(_has_control(value) for value in params.values()):
            raise HttpClientError("invalid KCI Open API parameters")

        parsed = urlsplit(url)
        request_target = parsed.path + "?" + urlencode(
            (
                ("apiCode", params["apiCode"]),
                ("key", params["key"]),
                ("id", params["id"]),
            )
        )
        accept = (headers or {}).get("Accept", "application/xml, text/xml")
        if _has_control(accept):
            raise HttpClientError("invalid Accept header")
        request_headers = {
            "Accept": accept,
            "User-Agent": self.user_agent,
            "Connection": "close",
        }

        connection: http.client.HTTPSConnection | None = None
        try:
            if self._request_gate is not None:
                # The limiter sees only the fixed endpoint.  In particular it
                # never receives the query string containing the API key.
                self._request_gate(KCI_OPENAPI_DETAIL_URL)
            connection = http.client.HTTPSConnection(
                parsed.hostname,
                port=443,
                timeout=self.connect_timeout,
                context=ssl.create_default_context(),
            )
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self.read_timeout)
            connection.request("GET", request_target, headers=request_headers)
            response = connection.getresponse()
            content_length = _content_length(response.getheader("Content-Length"))
            if content_length is not None and content_length > max_bytes:
                raise ResponseTooLargeError(
                    f"declared response exceeds {max_bytes} bytes"
                )
            body = bytearray()
            while True:
                chunk = response.read(min(64 * 1024, max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLargeError(
                        f"decoded response exceeds {max_bytes} bytes"
                    )
            safe_headers = {
                name: value
                for name in ("Content-Type", "Content-Length", "Retry-After")
                if (value := response.getheader(name)) is not None
            }
            return HttpFetch(
                requested_url=KCI_OPENAPI_DETAIL_URL,
                final_url=KCI_OPENAPI_DETAIL_URL,
                status_code=int(response.status),
                headers=safe_headers,
                body=bytes(body),
                redirects=(),
            )
        except ResponseTooLargeError:
            raise
        except (
            OSError,
            socket.timeout,
            TimeoutError,
            http.client.HTTPException,
        ):
            # Do not interpolate the underlying exception: some HTTP stacks
            # include request targets (and therefore query credentials).
            raise RetryableHttpError("KCI Open API request failed") from None
        finally:
            if connection is not None:
                connection.close()


@dataclass(frozen=True, slots=True)
class KciOpenApiIdentifierConfig:
    """Credentials, bounds, and cache policy for KCI DOI recovery."""

    api_key: str | None = field(default=None, repr=False)
    cache_dir: Path | None = DEFAULT_KCI_IDENTIFIER_CACHE_DIR
    max_response_bytes: int = 2 * 1024 * 1024
    positive_ttl_seconds: int = 90 * 24 * 60 * 60
    negative_ttl_seconds: int = 7 * 24 * 60 * 60
    force_refresh: bool = False
    max_xml_elements: int = 20_000
    max_xml_depth: int = 64


class KciOpenApiIdentifierResolver:
    """Recover a DOI from the official KCI record for one exact ART id."""

    def __init__(
        self,
        config: KciOpenApiIdentifierConfig | None = None,
        *,
        http: KciOpenApiHttp | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or KciOpenApiIdentifierConfig()
        self._api_key = _validated_api_key(self.config.api_key)
        if self.config.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if self.config.positive_ttl_seconds < 0:
            raise ValueError("positive_ttl_seconds must be non-negative")
        if self.config.negative_ttl_seconds < 0:
            raise ValueError("negative_ttl_seconds must be non-negative")
        if self.config.max_xml_elements < 1:
            raise ValueError("max_xml_elements must be positive")
        if self.config.max_xml_depth < 1:
            raise ValueError("max_xml_depth must be positive")
        self.http = http or KciOpenApiHttpClient()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve(self, paper: PaperCandidate) -> IdentifierResolution:
        """Return an auditable decision without mutating ``paper``."""

        if paper.doi:
            return self._result(
                status="already_present",
                reason="paper_already_has_doi",
                paper=paper,
                doi_original=paper.doi_original,
            )
        if _ART_ID.fullmatch(paper.source_id) is None:
            return self._result(
                status="invalid_input",
                reason="invalid_kci_article_id",
                paper=paper,
                error_code="invalid_source_id",
            )

        title_entries = paper_title_queries(paper)
        normalized_titles = tuple(value for _, value in title_entries)
        publication_year = _paper_year(paper.publication_year)
        cached = (
            None
            if self.config.force_refresh
            else self._load_cache(
                paper,
                normalized_titles=normalized_titles,
                publication_year=publication_year,
            )
        )
        if cached is not None:
            return cached

        missing: list[str] = []
        if not normalized_titles:
            missing.append("title")
        if publication_year is None:
            missing.append("publication_year")
        if missing:
            return self._result(
                status="missing_metadata",
                reason="missing_" + "_and_".join(missing),
                paper=paper,
            )
        if self._api_key is None:
            return self._result(
                status="configuration_error",
                reason="missing_kci_openapi_key",
                paper=paper,
            )

        params = _RedactedQueryParams(
            {
                "apiCode": KCI_OPENAPI_API_CODE,
                "key": self._api_key,
                "id": paper.source_id,
            },
            sensitive=frozenset({"key"}),
        )
        try:
            fetch = self.http.get_bytes(
                KCI_OPENAPI_DETAIL_URL,
                params=params,
                max_bytes=self.config.max_response_bytes,
                headers={"Accept": "application/xml, text/xml"},
            )
        except RetryableHttpError:
            return self._result(
                status="retryable_error",
                reason="kci_openapi_request_retryable",
                paper=paper,
                error_code="retryable_error",
            )
        except ResponseTooLargeError:
            return self._result(
                status="invalid_response",
                reason="kci_openapi_response_too_large",
                paper=paper,
                error_code="too_large",
            )
        except HttpClientError as exc:
            return self._result(
                status="request_error",
                reason="kci_openapi_request_failed",
                paper=paper,
                error_code=getattr(exc, "code", "http_error"),
            )

        if not _is_exact_fetch(fetch):
            return self._result(
                status="invalid_response",
                reason="kci_openapi_redirect_rejected",
                paper=paper,
                http_status=fetch.status_code,
                error_code="redirect_not_allowed",
            )
        if 300 <= fetch.status_code < 400:
            return self._result(
                status="invalid_response",
                reason="kci_openapi_redirect_rejected",
                paper=paper,
                http_status=fetch.status_code,
                error_code="redirect_not_allowed",
            )
        if fetch.status_code in _RETRYABLE_HTTP_STATUSES:
            return self._result(
                status="retryable_error",
                reason=(
                    "kci_openapi_rate_limited"
                    if fetch.status_code == 429
                    else "kci_openapi_retryable_http_error"
                ),
                paper=paper,
                http_status=fetch.status_code,
                error_code="retryable_error",
            )
        if fetch.status_code in {401, 403}:
            return self._result(
                status="configuration_error",
                reason="kci_openapi_access_denied",
                paper=paper,
                http_status=fetch.status_code,
                error_code="access_denied",
            )
        if fetch.status_code == 404:
            resolution = self._result(
                status="no_match",
                reason="kci_article_not_found",
                paper=paper,
                http_status=fetch.status_code,
            )
            self._store_cache(
                paper,
                normalized_titles=normalized_titles,
                publication_year=publication_year,
                resolution=resolution,
            )
            return resolution
        if not 200 <= fetch.status_code < 300:
            return self._result(
                status="http_error",
                reason="kci_openapi_http_error",
                paper=paper,
                http_status=fetch.status_code,
                error_code="http_error",
            )
        if len(fetch.body) > self.config.max_response_bytes:
            return self._result(
                status="invalid_response",
                reason="kci_openapi_response_too_large",
                paper=paper,
                http_status=fetch.status_code,
                error_code="too_large",
            )
        if not _is_xml_content_type(fetch.content_type):
            return self._result(
                status="invalid_response",
                reason="invalid_kci_openapi_content_type",
                paper=paper,
                http_status=fetch.status_code,
                error_code="invalid_content_type",
            )

        try:
            root = _safe_parse_xml(
                fetch.body,
                max_elements=self.config.max_xml_elements,
                max_depth=self.config.max_xml_depth,
            )
        except ValueError as exc:
            return self._result(
                status="invalid_response",
                reason=str(exc),
                paper=paper,
                http_status=fetch.status_code,
                error_code="invalid_xml",
            )

        try:
            records, response_message_state = _validated_api_envelope(root)
        except ValueError as exc:
            return self._result(
                status="invalid_response",
                reason=str(exc),
                paper=paper,
                http_status=fetch.status_code,
                error_code="invalid_payload",
            )
        if not records:
            if response_message_state in {None, "no_data"}:
                resolution = self._result(
                    status="no_match",
                    reason="kci_article_not_found",
                    paper=paper,
                    http_status=fetch.status_code,
                )
                self._store_cache(
                    paper,
                    normalized_titles=normalized_titles,
                    publication_year=publication_year,
                    resolution=resolution,
                )
                return resolution
            return self._result(
                status="invalid_response",
                reason="invalid_kci_openapi_payload",
                paper=paper,
                http_status=fetch.status_code,
                error_code="missing_record",
            )
        if len(records) != 1:
            return self._result(
                status="ambiguous",
                reason="multiple_kci_article_records",
                paper=paper,
                returned_items=len(records),
                http_status=fetch.status_code,
                error_code="ambiguous_response",
            )

        resolution = self._match_record(
            paper,
            records[0],
            normalized_titles=normalized_titles,
            publication_year=publication_year,
            http_status=fetch.status_code,
        )
        self._store_cache(
            paper,
            normalized_titles=normalized_titles,
            publication_year=publication_year,
            resolution=resolution,
        )
        return resolution

    def cache_path_for(self, paper: PaperCandidate) -> Path | None:
        """Return the secret-free per-ART-id cache path."""

        if self.config.cache_dir is None or _ART_ID.fullmatch(paper.source_id) is None:
            return None
        return Path(self.config.cache_dir) / f"{paper.source_id}.json"

    def _match_record(
        self,
        paper: PaperCandidate,
        record: ElementTree.Element,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int,
        http_status: int,
    ) -> IdentifierResolution:
        article_infos = _direct_children(record, "articleInfo")
        journal_infos = _direct_children(record, "journalInfo")
        common = {
            "paper": paper,
            "returned_items": 1,
            "http_status": http_status,
        }
        if len(article_infos) != 1 or len(journal_infos) != 1:
            return self._result(
                status="invalid_response",
                reason="invalid_kci_article_record",
                error_code="invalid_payload",
                **common,
            )

        article_info = article_infos[0]
        journal_info = journal_infos[0]
        article_id = (article_info.get("article-id") or "").strip()
        if article_id != paper.source_id:
            return self._result(
                status="identity_mismatch",
                reason="article_id_mismatch",
                **common,
            )

        returned_title_entries = [
            (_element_text(element), _normalize_element_text(element))
            for element in _elements_named(article_info, "article-title")
        ]
        returned_title_entries = [
            (original, normalized)
            for original, normalized in returned_title_entries
            if original is not None and normalized is not None
        ]
        known_title_identities = frozenset(
            identity
            for title in normalized_titles
            if (identity := _title_identity(title)) is not None
        )
        matching_titles = [
            original
            for original, normalized in returned_title_entries
            if _title_identity(normalized) in known_title_identities
        ]
        if not matching_titles:
            return self._result(
                status="identity_mismatch",
                reason="title_mismatch",
                **common,
            )
        matched_title = matching_titles[0]

        years = {
            year
            for element in _elements_named(journal_info, "pub-year")
            if (year := _paper_year(_element_text(element))) is not None
        }
        if years != {publication_year}:
            return self._result(
                status="identity_mismatch",
                reason="publication_year_mismatch",
                matched_title=matched_title,
                exact_title_matches=1,
                **common,
            )

        returned_authors = _article_authors(article_info)
        if returned_authors is None:
            return self._result(
                status="invalid_response",
                reason="invalid_kci_article_authors",
                matched_title=matched_title,
                matched_year=publication_year,
                exact_title_matches=1,
                title_year_matches=1,
                error_code="invalid_payload",
                **common,
            )
        effective_paper = (
            replace(paper, authors=returned_authors)
            if not paper.authors and returned_authors
            else paper
        )
        common["paper"] = effective_paper
        common["matched_authors"] = returned_authors

        doi_values = [
            value
            for element in _direct_children(article_info, "doi")
            if (value := _element_text(element)) is not None
        ]
        normalized_dois: dict[str, tuple[str, str]] = {}
        invalid_doi = False
        for doi_original in doi_values:
            try:
                doi = normalize_doi(doi_original)
            except InvalidDoiError:
                invalid_doi = True
                continue
            if doi is None:
                invalid_doi = True
                continue
            normalized_dois.setdefault(doi.casefold(), (doi_original, doi))
        if not doi_values:
            return self._result(
                status="no_match",
                reason="missing_doi",
                matched_title=matched_title,
                matched_year=publication_year,
                exact_title_matches=1,
                title_year_matches=1,
                **common,
            )
        if invalid_doi:
            return self._result(
                status="identity_mismatch",
                reason="invalid_doi",
                matched_title=matched_title,
                matched_year=publication_year,
                exact_title_matches=1,
                title_year_matches=1,
                **common,
            )
        if len(normalized_dois) != 1:
            return self._result(
                status="ambiguous",
                reason="multiple_distinct_dois",
                matched_title=matched_title,
                matched_year=publication_year,
                exact_title_matches=1,
                title_year_matches=1,
                **common,
            )

        doi_original, doi = next(iter(normalized_dois.values()))
        reason = "exact_article_id_title_and_year_match"
        resolved_paper = replace(
            effective_paper,
            doi_original=doi_original,
            doi=doi,
            doi_source=_RESOLVER_NAME,
            identifier_resolution=_audit_mapping(
                reason=reason,
                matched_title=matched_title,
                matched_year=publication_year,
                cache_hit=False,
            ),
        )
        return self._result(
            status="resolved",
            reason=reason,
            paper=resolved_paper,
            doi_original=doi_original,
            matched_title=matched_title,
            matched_year=publication_year,
            returned_items=1,
            exact_title_matches=1,
            title_year_matches=1,
            http_status=http_status,
        )

    def _load_cache(
        self,
        paper: PaperCandidate,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int | None,
    ) -> IdentifierResolution | None:
        path = self.cache_path_for(paper)
        if path is None:
            return None
        value = load_json_object(path)
        if (
            not value
            or value.get("schema_version")
            != KCI_IDENTIFIER_CACHE_SCHEMA_VERSION
        ):
            return None
        if value.get("resolver") != _RESOLVER_NAME:
            return None
        inputs = value.get("input")
        if not isinstance(inputs, dict) or inputs != {
            "source_id": paper.source_id,
            "normalized_titles": list(normalized_titles),
            "publication_year": publication_year,
        }:
            return None
        expires_at = _parse_timestamp(value.get("expires_at"))
        if expires_at is None or expires_at <= _aware_utc(self._clock()):
            return None

        cached = value.get("result")
        if not isinstance(cached, dict):
            return None
        status = cached.get("status")
        reason = cached.get("reason")
        if status not in _CACHEABLE_STATUSES or not isinstance(reason, str):
            return None
        matched_title = _optional_string(cached.get("matched_title"))
        matched_year = _paper_year(cached.get("matched_year"))
        matched_authors = _cached_authors(cached.get("matched_authors"))
        if matched_authors is None:
            return None
        resolved_paper = (
            replace(paper, authors=matched_authors)
            if not paper.authors and matched_authors
            else paper
        )
        doi_original = _optional_string(cached.get("doi_original"))
        if status == "resolved":
            cached_doi = _optional_string(cached.get("doi"))
            try:
                doi = normalize_doi(cached_doi)
            except InvalidDoiError:
                return None
            if (
                doi is None
                or doi_original is None
                or matched_title is None
                or matched_year != publication_year
                or _title_identity(matched_title)
                not in {
                    identity
                    for title in normalized_titles
                    if (identity := _title_identity(title)) is not None
                }
            ):
                return None
            try:
                original_doi = normalize_doi(doi_original)
            except InvalidDoiError:
                return None
            if original_doi is None or original_doi.casefold() != doi.casefold():
                return None
            resolved_paper = replace(
                resolved_paper,
                doi_original=doi_original,
                doi=doi,
                doi_source=_RESOLVER_NAME,
                identifier_resolution=_audit_mapping(
                    reason=reason,
                    matched_title=matched_title,
                    matched_year=matched_year,
                    cache_hit=True,
                ),
            )

        return self._result(
            status=status,
            reason=reason,
            paper=resolved_paper,
            doi_original=doi_original,
            matched_title=matched_title,
            matched_year=matched_year,
            matched_authors=matched_authors,
            returned_items=_nonnegative_int(cached.get("returned_items")),
            exact_title_matches=_nonnegative_int(
                cached.get("exact_title_matches")
            ),
            title_year_matches=_nonnegative_int(
                cached.get("title_year_matches")
            ),
            cache_hit=True,
            http_status=_optional_int(cached.get("http_status")),
        )

    def _store_cache(
        self,
        paper: PaperCandidate,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int | None,
        resolution: IdentifierResolution,
    ) -> None:
        path = self.cache_path_for(paper)
        if path is None or resolution.status not in _CACHEABLE_STATUSES:
            return
        ttl = (
            self.config.positive_ttl_seconds
            if resolution.status == "resolved"
            else self.config.negative_ttl_seconds
        )
        if ttl == 0:
            return
        now = _aware_utc(self._clock())
        value = {
            "schema_version": KCI_IDENTIFIER_CACHE_SCHEMA_VERSION,
            "resolver": _RESOLVER_NAME,
            "input": {
                "source_id": paper.source_id,
                "normalized_titles": list(normalized_titles),
                "publication_year": publication_year,
            },
            "result": {
                "status": resolution.status,
                "reason": resolution.reason,
                "doi_original": resolution.doi_original,
                "doi": resolution.doi,
                "matched_title": resolution.matched_title,
                "matched_year": resolution.matched_year,
                "matched_authors": list(resolution.matched_authors),
                "returned_items": resolution.returned_items,
                "exact_title_matches": resolution.exact_title_matches,
                "title_year_matches": resolution.title_year_matches,
                "http_status": resolution.http_status,
            },
            "cached_at": _format_timestamp(now),
            "expires_at": _format_timestamp(now + timedelta(seconds=ttl)),
        }
        try:
            write_json_atomic(path, value)
        except OSError:
            return

    @staticmethod
    def _result(
        *,
        status: str,
        reason: str,
        paper: PaperCandidate,
        **values: Any,
    ) -> IdentifierResolution:
        return IdentifierResolution(
            status=status,
            reason=reason,
            paper=paper,
            resolver=_RESOLVER_NAME,
            **values,
        )


def _validated_api_key(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("api_key must be a string or None")
    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > 4096
        or any(character.isspace() or _has_control(character) for character in stripped)
    ):
        raise ValueError(
            "api_key must be nonempty and contain no whitespace or controls"
        )
    return stripped


def _safe_parse_xml(
    body: bytes,
    *,
    max_elements: int,
    max_depth: int,
) -> ElementTree.Element:
    if not body:
        raise ValueError("empty_kci_openapi_xml")
    if b"\x00" in body or _FORBIDDEN_XML_DECLARATION.search(body):
        raise ValueError("unsafe_kci_openapi_xml")
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, RecursionError):
        raise ValueError("invalid_kci_openapi_xml") from None

    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > max_elements or depth > max_depth:
            raise ValueError("complex_kci_openapi_xml")
        stack.extend((child, depth + 1) for child in element)
    return root


def _validated_api_envelope(
    root: ElementTree.Element,
) -> tuple[list[ElementTree.Element], str | None]:
    if _local_name(root.tag) != "MetaData":
        raise ValueError("invalid_kci_openapi_envelope")
    output_nodes = _direct_children(root, "outputData")
    if len(output_nodes) != 1:
        raise ValueError("invalid_kci_openapi_envelope")
    output = output_nodes[0]
    result_nodes = _direct_children(output, "result")
    if len(result_nodes) != 1:
        raise ValueError("invalid_kci_openapi_envelope")
    result = result_nodes[0]

    codes = [
        text
        for element in _direct_children(result, "resultCode")
        if (text := _element_text(element)) is not None
    ]
    if len(codes) > 1 or (
        codes and re.fullmatch(r"0+", codes[0]) is None
    ):
        raise ValueError("kci_openapi_reported_error")

    message_state = _api_message_state(result)
    if message_state == "error":
        raise ValueError("kci_openapi_reported_error")

    totals = [
        text
        for element in _direct_children(result, "total")
        if (text := _element_text(element)) is not None
    ]
    if len(totals) != 1 or re.fullmatch(r"[0-9]+", totals[0]) is None:
        raise ValueError("invalid_kci_openapi_envelope")
    total = int(totals[0])
    records = _direct_children(output, "record")
    if total != len(records):
        raise ValueError("invalid_kci_openapi_envelope")
    if message_state == "no_data" and records:
        raise ValueError("kci_openapi_reported_error")
    return records, message_state


def _api_message_state(result: ElementTree.Element) -> str | None:
    messages = [
        text
        for name in ("resultMsg", "message")
        for element in _direct_children(result, name)
        if (text := _element_text(element)) is not None
    ]
    if not messages:
        return None
    normalized = {message.casefold() for message in messages}
    if normalized <= {message.casefold() for message in _SUCCESS_MESSAGES}:
        return "success"
    if normalized & {message.casefold() for message in _NO_DATA_MESSAGES}:
        return "no_data"
    return "error"


def _elements_named(
    root: ElementTree.Element,
    name: str,
) -> list[ElementTree.Element]:
    return [
        element
        for element in root.iter()
        if _local_name(element.tag) == name
    ]


def _direct_children(
    root: ElementTree.Element,
    name: str,
) -> list[ElementTree.Element]:
    return [child for child in root if _local_name(child.tag) == name]


def _local_name(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _element_text(element: ElementTree.Element) -> str | None:
    value = "".join(element.itertext())
    normalized = " ".join(value.split())
    return normalized or None


def _article_authors(
    article_info: ElementTree.Element,
) -> tuple[str, ...] | None:
    """Read KCI's direct Korean author names without guessing affiliations."""

    groups = _direct_children(article_info, "author-group")
    if not groups:
        return ()
    if len(groups) != 1:
        return None
    values: list[str] = []
    seen: set[str] = set()
    for author in _direct_children(groups[0], "author"):
        names = [
            value
            for element in _direct_children(author, "name")
            if (value := _element_text(element)) is not None
        ]
        if len(names) != 1:
            return None
        normalized = normalize_title(names[0])
        if normalized is None:
            return None
        key = "".join(
            character.casefold()
            for character in normalized
            if character.isalnum()
        )
        if not key or key in seen:
            return None
        seen.add(key)
        values.append(names[0])
    return tuple(values) if values else None


def _normalize_element_text(element: ElementTree.Element) -> str | None:
    return _normalize_text(_element_text(element))


def _normalize_text(value: str | None) -> str | None:
    return normalize_title(value)


def _title_identity(value: str | None) -> str | None:
    """Normalize only a terminal, whitespace-delimited subtitle marker.

    KCI records occasionally retain a closing ``" -"`` that the source DB
    projection trims, and some alternate-language fields are truncated.  The
    exact ART id, publication year, and at least one complete known title must
    still agree; no fuzzy, prefix, or edit-distance matching is used.
    """

    normalized = _normalize_text(value)
    if normalized is None:
        return None
    identity = _TRAILING_TITLE_DELIMITER_RE.sub("", normalized).strip()
    return identity or None


def _paper_year(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 9999 else None


def _is_xml_content_type(content_type: str) -> bool:
    return not content_type or content_type in {
        "application/xml",
        "text/xml",
        "application/octet-stream",
    } or content_type.endswith("+xml")


def _is_exact_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "open.kci.go.kr"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/po/openapi/openApiSearch.kci"
        and not parsed.query
        and not parsed.fragment
    )


def _is_exact_fetch(fetch: HttpFetch) -> bool:
    return (
        fetch.requested_url == KCI_OPENAPI_DETAIL_URL
        and fetch.final_url == KCI_OPENAPI_DETAIL_URL
        and not fetch.redirects
        and _is_exact_endpoint(fetch.final_url)
    )


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _audit_mapping(
    *,
    reason: str,
    matched_title: str,
    matched_year: int,
    cache_hit: bool,
) -> dict[str, Any]:
    return {
        "resolver": _RESOLVER_NAME,
        "status": "resolved",
        "reason": reason,
        "matched_title": matched_title,
        "matched_year": matched_year,
        "cache_hit": cache_hit,
    }


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _cached_authors(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    authors: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return None
        normalized = normalize_title(item)
        if normalized is None:
            return None
        key = "".join(
            character.casefold()
            for character in normalized
            if character.isalnum()
        )
        if not key or key in seen:
            return None
        seen.add(key)
        authors.append(item)
    return tuple(authors)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nonnegative_int(value: Any) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed >= 0 else 0
