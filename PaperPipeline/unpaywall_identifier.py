"""Conservative DOI recovery through Unpaywall's official title search.

Unpaywall search scores are deliberately ignored.  Each distinct known title
(the primary title and any alternate titles) is searched.  A DOI is accepted
only when the returned DOI object has one of those exactly equal normalized
titles and a publication year within one year of the input paper.  If more
than one distinct valid DOI meets those conditions, the result is ambiguous.

The configured contact email is sent only as the API's required query
parameter.  It is never included in cache files or public resolution records.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import HttpClientError, HttpFetch, SafeHttpClient
from PaperPipeline.identifier_resolver import (
    normalize_title,
    paper_title_queries,
)
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import load_json_object, safe_source_stem, write_json_atomic


UNPAYWALL_SEARCH_URL = "https://api.unpaywall.org/v2/search"
UNPAYWALL_MAX_RESULTS = 50
CACHE_SCHEMA_VERSION = "1.1"
_RESOLVER_NAME = "unpaywall-title"
_CACHEABLE_STATUSES = frozenset(
    {"resolved", "missing_metadata", "no_match", "ambiguous"}
)
_STATUS_REASONS: Mapping[str, frozenset[str]] = {
    "resolved": frozenset({"exact_title_and_year_match"}),
    "missing_metadata": frozenset(
        {
            "missing_title",
            "missing_publication_year",
            "missing_title_and_publication_year",
        }
    ),
    "no_match": frozenset(
        {"title_mismatch", "year_mismatch", "missing_or_invalid_doi"}
    ),
    "ambiguous": frozenset({"multiple_distinct_dois"}),
}


class UnpaywallHttp(Protocol):
    """Small HTTP seam implemented by :class:`SafeHttpClient`."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """GET a bounded response from a previously validated public URL."""


@dataclass(frozen=True, slots=True)
class UnpaywallIdentifierConfig:
    """Runtime and per-paper cache settings."""

    email: str = field(repr=False)
    cache_dir: Path | None = None
    max_response_bytes: int = 8 * 1024 * 1024
    positive_ttl_seconds: int = 30 * 24 * 60 * 60
    negative_ttl_seconds: int = 24 * 60 * 60
    force_refresh: bool = False


@dataclass(frozen=True, slots=True)
class UnpaywallOaLocation:
    """Safe OA location metadata retained from a matching DOI object."""

    doi: str
    url_for_pdf: str | None = None
    url_for_landing_page: str | None = None
    license: str | None = None
    version: str | None = None
    host_type: str | None = None

    @property
    def candidate(self) -> ResolverCandidate | None:
        """Return a downloader candidate when a direct PDF URL is available."""

        if self.url_for_pdf is None:
            return None
        return ResolverCandidate(
            url=self.url_for_pdf,
            resolver=_RESOLVER_NAME,
            landing_url=self.url_for_landing_page,
            license=self.license,
            version=self.version,
            is_open_access=True,
            verified_doi=self.doi,
        )


@dataclass(frozen=True, slots=True)
class UnpaywallIdentifierResolution:
    """One auditable, secret-free title resolution decision."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = _RESOLVER_NAME
    doi_original: str | None = None
    matched_title: str | None = None
    matched_year: int | None = None
    oa_location: UnpaywallOaLocation | None = None
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
        return self.status in {"resolved", "already_present"} and bool(self.paper.doi)

    @property
    def candidate(self) -> ResolverCandidate | None:
        return self.oa_location.candidate if self.oa_location is not None else None


@dataclass(frozen=True, slots=True)
class _MatchedItem:
    doi_original: str
    doi: str
    title: str
    year: int
    oa_location: UnpaywallOaLocation | None


class UnpaywallTitleResolver:
    """Recover a missing DOI with Unpaywall's official ``/v2/search`` API."""

    def __init__(
        self,
        config: UnpaywallIdentifierConfig,
        *,
        http: UnpaywallHttp | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._email = _validate_email(config.email)
        if config.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if config.positive_ttl_seconds < 0:
            raise ValueError("positive_ttl_seconds must be non-negative")
        if config.negative_ttl_seconds < 0:
            raise ValueError("negative_ttl_seconds must be non-negative")
        self.http: UnpaywallHttp = http or SafeHttpClient()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve(self, paper: PaperCandidate) -> UnpaywallIdentifierResolution:
        """Return a decision and a DOI-populated copy only on an exact match."""

        if paper.doi:
            return UnpaywallIdentifierResolution(
                status="already_present",
                reason="paper_already_has_doi",
                paper=paper,
                doi_original=paper.doi_original,
            )

        title_queries = paper_title_queries(paper)
        normalized_titles = tuple(
            normalized_title for _, normalized_title in title_queries
        )
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
            resolution = UnpaywallIdentifierResolution(
                status="missing_metadata",
                reason="missing_" + "_and_".join(missing),
                paper=paper,
            )
            self._store_cache(
                paper,
                normalized_titles=normalized_titles,
                publication_year=publication_year,
                resolution=resolution,
            )
            return resolution

        assert publication_year is not None
        all_items: list[Mapping[str, Any]] = []
        http_status: int | None = None
        for query_title, _ in title_queries:
            url = f"{UNPAYWALL_SEARCH_URL}?{urlencode({
                'query': query_title,
                'email': self._email,
            })}"
            try:
                fetch = self.http.get_bytes(
                    url,
                    max_bytes=self.config.max_response_bytes,
                    headers={"Accept": "application/json"},
                )
            except HttpClientError as exc:
                return UnpaywallIdentifierResolution(
                    status="request_error",
                    reason="unpaywall_request_failed",
                    paper=paper,
                    error_code=getattr(exc, "code", "http_error"),
                )

            http_status = fetch.status_code
            if not 200 <= fetch.status_code < 300:
                return UnpaywallIdentifierResolution(
                    status="http_error",
                    reason="unpaywall_http_error",
                    paper=paper,
                    http_status=fetch.status_code,
                )

            try:
                payload = json.loads(fetch.body)
            except (UnicodeError, json.JSONDecodeError):
                return UnpaywallIdentifierResolution(
                    status="invalid_response",
                    reason="invalid_unpaywall_json",
                    paper=paper,
                    http_status=fetch.status_code,
                )
            items = _unpaywall_items(payload)
            if items is None:
                return UnpaywallIdentifierResolution(
                    status="invalid_response",
                    reason="invalid_unpaywall_payload",
                    paper=paper,
                    http_status=fetch.status_code,
                )
            all_items.extend(items)

        assert http_status is not None

        resolution = _match_items(
            paper,
            all_items,
            normalized_titles=normalized_titles,
            publication_year=publication_year,
            http_status=http_status,
        )
        self._store_cache(
            paper,
            normalized_titles=normalized_titles,
            publication_year=publication_year,
            resolution=resolution,
        )
        return resolution

    def cache_path_for(self, paper: PaperCandidate) -> Path | None:
        """Return this paper's cache path, or ``None`` when caching is disabled."""

        if self.config.cache_dir is None:
            return None
        return (
            Path(self.config.cache_dir)
            / f"{safe_source_stem(paper.source_id)}.unpaywall-doi.json"
        )

    def _load_cache(
        self,
        paper: PaperCandidate,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int | None,
    ) -> UnpaywallIdentifierResolution | None:
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
        inputs = value.get("input")
        if not isinstance(inputs, dict):
            return None
        if inputs.get("normalized_titles") != list(normalized_titles):
            return None
        if inputs.get("publication_year") != publication_year:
            return None
        expires_at = _parse_timestamp(value.get("expires_at"))
        if expires_at is None or expires_at <= _aware_utc(self._clock()):
            return None

        raw_result = value.get("result")
        if not isinstance(raw_result, dict):
            return None
        status = raw_result.get("status")
        reason = raw_result.get("reason")
        if (
            not isinstance(status, str)
            or not isinstance(reason, str)
            or status not in _CACHEABLE_STATUSES
            or reason not in _STATUS_REASONS[status]
        ):
            return None

        resolved_paper = paper
        doi_original = _optional_string(raw_result.get("doi_original"))
        oa_location: UnpaywallOaLocation | None = None
        if status == "resolved":
            cached_doi = _canonical_doi(raw_result.get("doi"))
            if cached_doi is None:
                return None
            if doi_original is not None:
                original_doi = _canonical_doi(doi_original)
                if (
                    original_doi is None
                    or original_doi.casefold() != cached_doi.casefold()
                ):
                    return None
            oa_location = _oa_location_from_cache(
                raw_result.get("oa_location"),
                cached_doi,
            )
            if raw_result.get("oa_location") is not None and oa_location is None:
                return None
            matched_title = _optional_string(raw_result.get("matched_title"))
            matched_year = _paper_year(raw_result.get("matched_year"))
            if (
                normalize_title(matched_title) not in normalized_titles
                or matched_year is None
                or publication_year is None
                or abs(matched_year - publication_year) > 1
            ):
                return None
            resolution_record = _paper_resolution_record(
                reason=reason,
                matched_title=matched_title,
                matched_year=matched_year,
                oa_location=oa_location,
                cache_hit=True,
            )
            resolved_paper = replace(
                paper,
                doi_original=doi_original or cached_doi,
                doi=cached_doi,
                doi_source=_RESOLVER_NAME,
                identifier_resolution=resolution_record,
            )
        elif (
            doi_original is not None
            or raw_result.get("doi") is not None
            or raw_result.get("oa_location") is not None
        ):
            return None

        returned_items = _bounded_count(
            raw_result.get("returned_items"),
            maximum=UNPAYWALL_MAX_RESULTS * len(normalized_titles),
        )
        exact_title_matches = _bounded_count(
            raw_result.get("exact_title_matches"),
            maximum=returned_items,
        )
        title_year_matches = _bounded_count(
            raw_result.get("title_year_matches"),
            maximum=exact_title_matches,
        )
        if (
            returned_items is None
            or exact_title_matches is None
            or title_year_matches is None
        ):
            return None

        return UnpaywallIdentifierResolution(
            status=status,
            reason=reason,
            paper=resolved_paper,
            doi_original=doi_original,
            matched_title=_optional_string(raw_result.get("matched_title")),
            matched_year=_paper_year(raw_result.get("matched_year")),
            oa_location=oa_location,
            returned_items=returned_items,
            exact_title_matches=exact_title_matches,
            title_year_matches=title_year_matches,
            cache_hit=True,
            http_status=_optional_int(raw_result.get("http_status")),
        )

    def _store_cache(
        self,
        paper: PaperCandidate,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int | None,
        resolution: UnpaywallIdentifierResolution,
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
                "oa_location": _serialize_oa_location(resolution.oa_location),
                "returned_items": resolution.returned_items,
                "exact_title_matches": resolution.exact_title_matches,
                "title_year_matches": resolution.title_year_matches,
                "http_status": resolution.http_status,
            },
            "cached_at": _format_timestamp(now),
            "expires_at": _format_timestamp(now + timedelta(seconds=ttl_seconds)),
        }
        try:
            write_json_atomic(path, value)
        except OSError:
            # Resolution remains useful when the optional cache is unwritable.
            return


def _match_items(
    paper: PaperCandidate,
    items: list[Mapping[str, Any]],
    *,
    normalized_titles: tuple[str, ...],
    publication_year: int,
    http_status: int,
) -> UnpaywallIdentifierResolution:
    exact_title_matches = 0
    title_year_matches = 0
    matches: list[_MatchedItem] = []
    known_titles = frozenset(normalized_titles)

    for item in items:
        title = _optional_string(item.get("title"))
        if title is None or normalize_title(title) not in known_titles:
            continue
        exact_title_matches += 1

        year = _paper_year(item.get("year"))
        if year is None or abs(year - publication_year) > 1:
            continue
        title_year_matches += 1

        doi_original = _optional_string(item.get("doi"))
        doi = _canonical_doi(doi_original)
        if doi is None or doi_original is None:
            continue
        matches.append(
            _MatchedItem(
                doi_original=doi_original,
                doi=doi,
                title=title,
                year=year,
                oa_location=_extract_oa_location(item, doi),
            )
        )

    unique_matches: dict[str, _MatchedItem] = {}
    for match in matches:
        key = match.doi.casefold()
        existing = unique_matches.get(key)
        if existing is None or _oa_location_rank(
            match.oa_location
        ) > _oa_location_rank(existing.oa_location):
            unique_matches[key] = match

    common = {
        "paper": paper,
        "returned_items": len(items),
        "exact_title_matches": exact_title_matches,
        "title_year_matches": title_year_matches,
        "http_status": http_status,
    }
    if len(unique_matches) > 1:
        return UnpaywallIdentifierResolution(
            status="ambiguous",
            reason="multiple_distinct_dois",
            **common,
        )
    if not unique_matches:
        if exact_title_matches == 0:
            reason = "title_mismatch"
        elif title_year_matches == 0:
            reason = "year_mismatch"
        else:
            reason = "missing_or_invalid_doi"
        return UnpaywallIdentifierResolution(
            status="no_match",
            reason=reason,
            **common,
        )

    match = next(iter(unique_matches.values()))
    resolution_record = _paper_resolution_record(
        reason="exact_title_and_year_match",
        matched_title=match.title,
        matched_year=match.year,
        oa_location=match.oa_location,
        cache_hit=False,
    )
    resolved_paper = replace(
        paper,
        doi_original=match.doi_original,
        doi=match.doi,
        doi_source=_RESOLVER_NAME,
        identifier_resolution=resolution_record,
    )
    return UnpaywallIdentifierResolution(
        status="resolved",
        reason="exact_title_and_year_match",
        paper=resolved_paper,
        doi_original=match.doi_original,
        matched_title=match.title,
        matched_year=match.year,
        oa_location=match.oa_location,
        returned_items=len(items),
        exact_title_matches=exact_title_matches,
        title_year_matches=title_year_matches,
        http_status=http_status,
    )


def _unpaywall_items(value: Any) -> list[Mapping[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    results = value.get("results")
    if not isinstance(results, list):
        return None
    items: list[Mapping[str, Any]] = []
    for result in results[:UNPAYWALL_MAX_RESULTS]:
        if not isinstance(result, dict):
            continue
        response = result.get("response")
        if isinstance(response, dict):
            items.append(response)
    return items


def _extract_oa_location(
    item: Mapping[str, Any],
    doi: str,
) -> UnpaywallOaLocation | None:
    if item.get("is_oa") is not True:
        return None

    raw_locations: list[Mapping[str, Any]] = []
    best = item.get("best_oa_location")
    if isinstance(best, dict):
        raw_locations.append(best)
    locations = item.get("oa_locations")
    if isinstance(locations, list):
        raw_locations.extend(
            location for location in locations if isinstance(location, dict)
        )

    landing_only: UnpaywallOaLocation | None = None
    seen: set[tuple[str | None, str | None]] = set()
    for raw in raw_locations:
        pdf_url = _safe_web_url(raw.get("url_for_pdf"))
        landing_url = _safe_web_url(raw.get("url_for_landing_page"))
        identity = (pdf_url, landing_url)
        if identity in seen or identity == (None, None):
            continue
        seen.add(identity)
        location = UnpaywallOaLocation(
            doi=doi,
            url_for_pdf=pdf_url,
            url_for_landing_page=landing_url,
            license=_bounded_string(raw.get("license")),
            version=_bounded_string(raw.get("version")),
            host_type=_bounded_string(raw.get("host_type")),
        )
        if pdf_url is not None:
            return location
        if landing_only is None:
            landing_only = location
    return landing_only


def _oa_location_from_cache(
    value: Any,
    doi: str,
) -> UnpaywallOaLocation | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    cached_doi = _canonical_doi(value.get("doi"))
    if cached_doi is None or cached_doi.casefold() != doi.casefold():
        return None
    pdf_url = _safe_web_url(value.get("url_for_pdf"))
    landing_url = _safe_web_url(value.get("url_for_landing_page"))
    if (value.get("url_for_pdf") is not None and pdf_url is None) or (
        value.get("url_for_landing_page") is not None and landing_url is None
    ):
        return None
    if pdf_url is None and landing_url is None:
        return None
    return UnpaywallOaLocation(
        doi=doi,
        url_for_pdf=pdf_url,
        url_for_landing_page=landing_url,
        license=_bounded_string(value.get("license")),
        version=_bounded_string(value.get("version")),
        host_type=_bounded_string(value.get("host_type")),
    )


def _serialize_oa_location(
    location: UnpaywallOaLocation | None,
) -> dict[str, Any] | None:
    if location is None:
        return None
    return {
        "doi": location.doi,
        "url_for_pdf": location.url_for_pdf,
        "url_for_landing_page": location.url_for_landing_page,
        "license": location.license,
        "version": location.version,
        "host_type": location.host_type,
    }


def _oa_location_rank(location: UnpaywallOaLocation | None) -> int:
    if location is None:
        return 0
    return 2 if location.url_for_pdf is not None else 1


def _paper_resolution_record(
    *,
    reason: str,
    matched_title: str | None,
    matched_year: int | None,
    oa_location: UnpaywallOaLocation | None,
    cache_hit: bool,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "resolver": _RESOLVER_NAME,
        "status": "resolved",
        "reason": reason,
        "matched_title": matched_title,
        "matched_year": matched_year,
        "cache_hit": cache_hit,
    }
    if oa_location is not None:
        value["oa_location"] = _serialize_oa_location(oa_location)
    return value


def _validate_email(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Unpaywall email must be a string")
    email = value.strip()
    if (
        not email
        or len(email) > 320
        or email.count("@") != 1
        or email.startswith("@")
        or email.endswith("@")
        or any(character.isspace() for character in email)
        or any(ord(character) < 32 or ord(character) == 127 for character in email)
    ):
        raise ValueError("Unpaywall email must be a valid contact email")
    return email


def _safe_web_url(value: Any) -> str | None:
    url = _optional_string(value)
    if url is None or len(url) > 8192:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return url
    return url if address.is_global else None


def _canonical_doi(value: Any) -> str | None:
    raw = _optional_string(value)
    try:
        return normalize_doi(raw)
    except InvalidDoiError:
        return None


def _paper_year(value: Any) -> int | None:
    year = _optional_int(value)
    return year if year is not None and 1000 <= year <= 9999 else None


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _bounded_string(value: Any, *, max_length: int = 512) -> str | None:
    text = _optional_string(value)
    if text is None or len(text) > max_length:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        return None
    return text


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bounded_count(value: Any, *, maximum: int) -> int | None:
    parsed = _optional_int(value)
    return parsed if parsed is not None and 0 <= parsed <= maximum else None


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
