"""Conservative DOI recovery from OpenAlex title search results.

Only OpenAlex's official Works search endpoint is queried.  Every distinct
normalized database title (the primary title plus alternate titles) is
searched.  A DOI is accepted when, and only when, a returned title is
identical to one of those known titles, the publication year is within one
year of the database value, and all qualifying results across every search
contain a single distinct valid DOI.  OpenAlex's ranking is never treated as
identity evidence.

Validated ``best_oa_location`` and ``locations`` metadata belonging to the
accepted work is retained for a later PDF download attempt.  The API key is
used only in the request URL; it is excluded from dataclass representations,
resolution records, and cache files.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import HttpClientError, SafeHttpClient
from PaperPipeline.identifier_resolver import normalize_title
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.storage import load_json_object, safe_source_stem, write_json_atomic


OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_MAX_RESULTS = 50
CACHE_SCHEMA_VERSION = "1.2"
_RESOLVER_NAME = "openalex-title"
_CACHEABLE_STATUSES = frozenset(
    {"resolved", "missing_metadata", "no_match", "ambiguous"}
)


@dataclass(frozen=True, slots=True)
class OpenAlexIdentifierConfig:
    """Runtime and per-paper cache settings for OpenAlex DOI recovery."""

    api_key: str | None = field(default=None, repr=False)
    cache_dir: Path | None = None
    max_response_bytes: int = 4 * 1024 * 1024
    positive_ttl_seconds: int = 30 * 24 * 60 * 60
    negative_ttl_seconds: int = 24 * 60 * 60
    force_refresh: bool = False


@dataclass(frozen=True, slots=True)
class OpenAlexLocation:
    """Curated metadata from one OpenAlex work location.

    A location may contain only a landing page.  ``as_resolver_candidate``
    intentionally returns ``None`` in that case because a landing page is not
    a direct PDF candidate.
    """

    pdf_url: str | None
    landing_page_url: str | None
    license: str | None = None
    version: str | None = None
    is_open_access: bool | None = None
    is_best: bool = False
    source_id: str | None = None
    source_name: str | None = None

    def as_resolver_candidate(self, doi: str) -> ResolverCandidate | None:
        """Convert a direct-PDF location to the downloader's common shape."""

        if self.pdf_url is None:
            return None
        return ResolverCandidate(
            url=self.pdf_url,
            resolver=_RESOLVER_NAME,
            landing_url=self.landing_page_url,
            license=self.license,
            version=self.version,
            is_open_access=self.is_open_access,
            verified_doi=doi,
        )


@dataclass(frozen=True, slots=True)
class OpenAlexIdentifierResolution:
    """One auditable OpenAlex title-resolution decision."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = _RESOLVER_NAME
    doi_original: str | None = None
    matched_title: str | None = None
    matched_year: int | None = None
    locations: tuple[OpenAlexLocation, ...] = ()
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
    def candidates(self) -> tuple[ResolverCandidate, ...]:
        """Return deduplicated direct-PDF candidates, best location first."""

        if not self.doi:
            return ()
        candidates: list[ResolverCandidate] = []
        seen_urls: set[str] = set()
        for location in self.locations:
            candidate = location.as_resolver_candidate(self.doi)
            if candidate is None or candidate.url in seen_urls:
                continue
            seen_urls.add(candidate.url)
            candidates.append(candidate)
        return tuple(candidates)

    @property
    def pdf_candidates(self) -> tuple[ResolverCandidate, ...]:
        """Explicit alias for callers interested only in direct PDFs."""

        return self.candidates


@dataclass(frozen=True, slots=True)
class _DoiMatch:
    doi_original: str
    doi: str
    title: str
    year: int
    locations: tuple[OpenAlexLocation, ...]


class OpenAlexTitleResolver:
    """Recover a missing DOI and public-location metadata through OpenAlex."""

    def __init__(
        self,
        config: OpenAlexIdentifierConfig | None = None,
        *,
        http: SafeHttpClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or OpenAlexIdentifierConfig()
        if self.config.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if self.config.positive_ttl_seconds < 0:
            raise ValueError("positive_ttl_seconds must be non-negative")
        if self.config.negative_ttl_seconds < 0:
            raise ValueError("negative_ttl_seconds must be non-negative")
        if self.config.api_key is not None:
            if not isinstance(self.config.api_key, str):
                raise ValueError("api_key must be a string")
            stripped_key = self.config.api_key.strip()
            if not stripped_key or any(
                ord(character) < 32 or ord(character) == 127
                for character in stripped_key
            ):
                raise ValueError("api_key must be nonempty and contain no controls")
        self.http = http or SafeHttpClient()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve(self, paper: PaperCandidate) -> OpenAlexIdentifierResolution:
        """Return a decision and a DOI-populated immutable paper on success."""

        if paper.doi:
            return OpenAlexIdentifierResolution(
                status="already_present",
                reason="paper_already_has_doi",
                paper=paper,
                doi_original=paper.doi_original,
            )

        known_titles = _known_titles(paper)
        normalized_titles = tuple(
            normalized_title for _, normalized_title in known_titles
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
        if not known_titles:
            missing.append("title")
        if publication_year is None:
            missing.append("publication_year")
        if missing:
            resolution = OpenAlexIdentifierResolution(
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

        api_key = (self.config.api_key or "").strip()
        if not api_key:
            return OpenAlexIdentifierResolution(
                status="configuration_error",
                reason="missing_openalex_api_key",
                paper=paper,
            )

        assert publication_year is not None
        all_items: list[Mapping[str, Any]] = []
        http_status: int | None = None
        for query_title, _ in known_titles:
            query = urlencode(
                {
                    "search": query_title,
                    "per-page": str(OPENALEX_MAX_RESULTS),
                    "api_key": api_key,
                }
            )
            url = f"{OPENALEX_WORKS_URL}?{query}"
            try:
                fetch = self.http.get_bytes(
                    url,
                    max_bytes=self.config.max_response_bytes,
                    headers={"Accept": "application/json"},
                )
            except HttpClientError as exc:
                return OpenAlexIdentifierResolution(
                    status="request_error",
                    reason="openalex_request_failed",
                    paper=paper,
                    error_code=getattr(exc, "code", "http_error"),
                )

            if not 200 <= fetch.status_code < 300:
                return OpenAlexIdentifierResolution(
                    status="http_error",
                    reason="openalex_http_error",
                    paper=paper,
                    http_status=fetch.status_code,
                )

            try:
                payload = json.loads(fetch.body)
            except (UnicodeError, json.JSONDecodeError):
                return OpenAlexIdentifierResolution(
                    status="invalid_response",
                    reason="invalid_openalex_json",
                    paper=paper,
                    http_status=fetch.status_code,
                )
            items = _openalex_items(payload)
            if items is None:
                return OpenAlexIdentifierResolution(
                    status="invalid_response",
                    reason="invalid_openalex_payload",
                    paper=paper,
                    http_status=fetch.status_code,
                )
            all_items.extend(items)
            http_status = fetch.status_code

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
        """Return the isolated cache path for ``paper`` when caching is enabled."""

        if self.config.cache_dir is None:
            return None
        return (
            Path(self.config.cache_dir)
            / f"{safe_source_stem(paper.source_id)}.openalex-doi.json"
        )

    def _load_cache(
        self,
        paper: PaperCandidate,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int | None,
    ) -> OpenAlexIdentifierResolution | None:
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

        raw = value.get("result")
        if not isinstance(raw, dict):
            return None
        status = raw.get("status")
        reason = raw.get("reason")
        if status not in _CACHEABLE_STATUSES or not isinstance(reason, str):
            return None

        doi_original = _optional_string(raw.get("doi_original"))
        matched_title = _optional_string(raw.get("matched_title"))
        matched_year = _paper_year(raw.get("matched_year"))
        locations: tuple[OpenAlexLocation, ...] = ()
        resolved_paper = paper
        if status == "resolved":
            cached_doi = _optional_string(raw.get("doi"))
            try:
                canonical_doi = normalize_doi(cached_doi)
            except InvalidDoiError:
                return None
            if (
                canonical_doi is None
                or matched_title is None
                or normalize_title(matched_title) not in normalized_titles
                or matched_year is None
                or publication_year is None
                or abs(matched_year - publication_year) > 1
            ):
                return None
            locations = _locations_from_cache(raw.get("locations"))
            if locations is None:
                return None
            resolved_paper = _resolved_paper(
                paper,
                doi_original=doi_original or cached_doi,
                doi=canonical_doi,
                matched_title=matched_title,
                matched_year=matched_year,
                cache_hit=True,
            )

        return OpenAlexIdentifierResolution(
            status=status,
            reason=reason,
            paper=resolved_paper,
            doi_original=doi_original,
            matched_title=matched_title,
            matched_year=matched_year,
            locations=locations,
            returned_items=_nonnegative_int(raw.get("returned_items")),
            exact_title_matches=_nonnegative_int(raw.get("exact_title_matches")),
            title_year_matches=_nonnegative_int(raw.get("title_year_matches")),
            cache_hit=True,
            http_status=_optional_int(raw.get("http_status")),
        )

    def _store_cache(
        self,
        paper: PaperCandidate,
        *,
        normalized_titles: tuple[str, ...],
        publication_year: int | None,
        resolution: OpenAlexIdentifierResolution,
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
                "locations": [
                    _serialize_location(location)
                    for location in resolution.locations
                ],
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
            # DOI recovery remains usable if the optional cache is unwritable.
            return


def _known_titles(paper: PaperCandidate) -> tuple[tuple[str, str], ...]:
    """Return query text and normalized identity for each distinct known title."""

    titles: list[tuple[str, str]] = []
    seen_normalized: set[str] = set()
    for value in (paper.title, *paper.alternate_titles):
        if not isinstance(value, str):
            continue
        query_title = value.strip()
        normalized_title = normalize_title(query_title)
        if normalized_title is None or normalized_title in seen_normalized:
            continue
        seen_normalized.add(normalized_title)
        titles.append((query_title, normalized_title))
    return tuple(titles)


def _match_items(
    paper: PaperCandidate,
    items: list[Mapping[str, Any]],
    *,
    normalized_titles: tuple[str, ...],
    publication_year: int,
    http_status: int,
) -> OpenAlexIdentifierResolution:
    exact_title_matches = 0
    title_year_matches = 0
    matches_by_doi: dict[str, list[_DoiMatch]] = {}

    for item in items:
        matching_title = next(
            (
                title
                for title in _item_titles(item)
                if normalize_title(title) in normalized_titles
            ),
            None,
        )
        if matching_title is None:
            continue
        exact_title_matches += 1

        year = _paper_year(item.get("publication_year"))
        if year is None or abs(year - publication_year) > 1:
            continue
        title_year_matches += 1

        distinct_item_dois: dict[str, tuple[str, str]] = {}
        for doi_original in _item_doi_values(item):
            try:
                doi = normalize_doi(doi_original)
            except InvalidDoiError:
                doi = None
            if doi is not None:
                distinct_item_dois.setdefault(
                    doi.casefold(),
                    (doi_original, doi),
                )
        locations = _item_locations(item)
        for doi_key, (doi_original, doi) in distinct_item_dois.items():
            matches_by_doi.setdefault(doi_key, []).append(
                _DoiMatch(
                    doi_original=doi_original,
                    doi=doi,
                    title=matching_title,
                    year=year,
                    locations=locations,
                )
            )

    common = {
        "paper": paper,
        "returned_items": len(items),
        "exact_title_matches": exact_title_matches,
        "title_year_matches": title_year_matches,
        "http_status": http_status,
    }
    if len(matches_by_doi) > 1:
        return OpenAlexIdentifierResolution(
            status="ambiguous",
            reason="multiple_distinct_dois",
            **common,
        )
    if not matches_by_doi:
        if exact_title_matches == 0:
            reason = "title_mismatch"
        elif title_year_matches == 0:
            reason = "year_mismatch"
        else:
            reason = "missing_or_invalid_doi"
        return OpenAlexIdentifierResolution(
            status="no_match",
            reason=reason,
            **common,
        )

    doi_matches = next(iter(matches_by_doi.values()))
    match = doi_matches[0]
    locations = _merge_locations(
        location
        for doi_match in doi_matches
        for location in doi_match.locations
    )
    resolved_paper = _resolved_paper(
        paper,
        doi_original=match.doi_original,
        doi=match.doi,
        matched_title=match.title,
        matched_year=match.year,
        cache_hit=False,
    )
    return OpenAlexIdentifierResolution(
        status="resolved",
        reason="exact_title_and_year_match",
        paper=resolved_paper,
        doi_original=match.doi_original,
        matched_title=match.title,
        matched_year=match.year,
        locations=locations,
        returned_items=len(items),
        exact_title_matches=exact_title_matches,
        title_year_matches=title_year_matches,
        http_status=http_status,
    )


def _resolved_paper(
    paper: PaperCandidate,
    *,
    doi_original: str,
    doi: str,
    matched_title: str,
    matched_year: int,
    cache_hit: bool,
) -> PaperCandidate:
    return replace(
        paper,
        doi_original=doi_original,
        doi=doi,
        doi_source=_RESOLVER_NAME,
        identifier_resolution={
            "resolver": _RESOLVER_NAME,
            "status": "resolved",
            "reason": "exact_title_and_year_match",
            "matched_title": matched_title,
            "matched_year": matched_year,
            "cache_hit": cache_hit,
        },
    )


def _openalex_items(value: Any) -> list[Mapping[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    items = value.get("results")
    if not isinstance(items, list):
        return None
    return [
        item
        for item in items[:OPENALEX_MAX_RESULTS]
        if isinstance(item, dict)
    ]


def _item_titles(item: Mapping[str, Any]) -> tuple[str, ...]:
    titles: list[str] = []
    for key in ("display_name", "title"):
        value = _optional_string(item.get(key))
        if value is not None and value not in titles:
            titles.append(value)
    return tuple(titles)


def _item_doi_values(item: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    direct = _optional_string(item.get("doi"))
    if direct is not None:
        values.append(direct)
    ids = item.get("ids")
    nested = _optional_string(ids.get("doi")) if isinstance(ids, dict) else None
    if nested is not None and nested not in values:
        values.append(nested)
    return tuple(values)


def _item_locations(item: Mapping[str, Any]) -> tuple[OpenAlexLocation, ...]:
    values: list[OpenAlexLocation] = []
    best = item.get("best_oa_location")
    if isinstance(best, dict):
        parsed = _parse_location(best, is_best=True)
        if parsed is not None:
            values.append(parsed)
    raw_locations = item.get("locations")
    if isinstance(raw_locations, list):
        for raw in raw_locations:
            if not isinstance(raw, dict):
                continue
            parsed = _parse_location(raw, is_best=False)
            if parsed is not None:
                values.append(parsed)
    return _merge_locations(values)


def _parse_location(
    value: Mapping[str, Any],
    *,
    is_best: bool,
) -> OpenAlexLocation | None:
    pdf_url = _validated_https_url(value.get("pdf_url"))
    landing_page_url = _validated_https_url(value.get("landing_page_url"))
    if pdf_url is None and landing_page_url is None:
        return None
    source = value.get("source")
    source_id = (
        _validated_https_url(source.get("id"))
        if isinstance(source, dict)
        else None
    )
    source_name = (
        _optional_string(source.get("display_name"))
        if isinstance(source, dict)
        else None
    )
    raw_is_oa = value.get("is_oa")
    is_open_access = raw_is_oa if isinstance(raw_is_oa, bool) else None
    return OpenAlexLocation(
        pdf_url=pdf_url,
        landing_page_url=landing_page_url,
        license=_optional_string(value.get("license")),
        version=_optional_string(value.get("version")),
        is_open_access=is_open_access,
        is_best=is_best,
        source_id=source_id,
        source_name=source_name,
    )


def _merge_locations(
    locations: Any,
) -> tuple[OpenAlexLocation, ...]:
    merged: dict[tuple[str | None, str | None], OpenAlexLocation] = {}
    for location in locations:
        if not isinstance(location, OpenAlexLocation):
            continue
        key = (location.pdf_url, location.landing_page_url)
        previous = merged.get(key)
        if previous is None:
            merged[key] = location
            continue
        merged[key] = OpenAlexLocation(
            pdf_url=previous.pdf_url,
            landing_page_url=previous.landing_page_url,
            license=previous.license or location.license,
            version=previous.version or location.version,
            is_open_access=(
                True
                if previous.is_open_access is True
                or location.is_open_access is True
                else previous.is_open_access
                if previous.is_open_access is not None
                else location.is_open_access
            ),
            is_best=previous.is_best or location.is_best,
            source_id=previous.source_id or location.source_id,
            source_name=previous.source_name or location.source_name,
        )
    return tuple(
        sorted(
            merged.values(),
            key=lambda location: (
                not location.is_best,
                location.pdf_url is None,
            ),
        )
    )


def _locations_from_cache(value: Any) -> tuple[OpenAlexLocation, ...] | None:
    if not isinstance(value, list):
        return None
    locations: list[OpenAlexLocation] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        parsed = _location_from_cache(raw)
        if parsed is None:
            return None
        locations.append(parsed)
    return _merge_locations(locations)


def _location_from_cache(value: Mapping[str, Any]) -> OpenAlexLocation | None:
    pdf_url = _validated_https_url(value.get("pdf_url"))
    landing_page_url = _validated_https_url(value.get("landing_page_url"))
    if pdf_url is None and landing_page_url is None:
        return None
    raw_is_oa = value.get("is_open_access")
    if raw_is_oa is not None and not isinstance(raw_is_oa, bool):
        return None
    raw_is_best = value.get("is_best")
    if not isinstance(raw_is_best, bool):
        return None
    source_id_raw = value.get("source_id")
    source_id = (
        None
        if source_id_raw is None
        else _validated_https_url(source_id_raw)
    )
    if source_id_raw is not None and source_id is None:
        return None
    return OpenAlexLocation(
        pdf_url=pdf_url,
        landing_page_url=landing_page_url,
        license=_optional_string(value.get("license")),
        version=_optional_string(value.get("version")),
        is_open_access=raw_is_oa,
        is_best=raw_is_best,
        source_id=source_id,
        source_name=_optional_string(value.get("source_name")),
    )


def _serialize_location(location: OpenAlexLocation) -> dict[str, Any]:
    return {
        "pdf_url": location.pdf_url,
        "landing_page_url": location.landing_page_url,
        "license": location.license,
        "version": location.version,
        "is_open_access": location.is_open_access,
        "is_best": location.is_best,
        "source_id": location.source_id,
        "source_name": location.source_name,
    }


def _validated_https_url(value: Any) -> str | None:
    url = _optional_string(value)
    if (
        url is None
        or len(url) > 4096
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        return None
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    return url


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


def _nonnegative_int(value: Any) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed >= 0 else 0


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
