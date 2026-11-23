"""Conservative DOI recovery from Crossref title metadata.

The resolver deliberately performs no fuzzy matching.  Each distinct known
title (the primary title and any alternate titles) is searched.  A DOI is
accepted only when a Crossref result has one of those same normalized titles
and a publication year within one year of the database value.  More than one
distinct DOI satisfying those conditions is treated as ambiguous.
"""

from __future__ import annotations

import html
import json
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import HttpClientError, SafeHttpClient
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import load_json_object, safe_source_stem, write_json_atomic


CROSSREF_WORKS_URL = "https://api.crossref.org/works"
CROSSREF_MAX_RESULTS = 50
CACHE_SCHEMA_VERSION = "1.2"
_CACHEABLE_STATUSES = frozenset(
    {"resolved", "missing_metadata", "no_match", "ambiguous"}
)


@dataclass(frozen=True, slots=True)
class IdentifierResolverConfig:
    """Runtime and cache settings for :class:`CrossrefTitleResolver`."""

    cache_dir: Path | None = None
    contact_email: str | None = None
    max_response_bytes: int = 2 * 1024 * 1024
    positive_ttl_seconds: int = 30 * 24 * 60 * 60
    negative_ttl_seconds: int = 24 * 60 * 60
    force_refresh: bool = False


@dataclass(frozen=True, slots=True)
class IdentifierResolution:
    """One auditable resolution decision.

    ``paper`` is a new :class:`PaperCandidate` with DOI fields populated only
    when ``status == "resolved"``.  The input candidate is never mutated.
    """

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = "crossref-title"
    doi_original: str | None = None
    matched_title: str | None = None
    matched_year: int | None = None
    matched_authors: tuple[str, ...] = ()
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


@dataclass(frozen=True, slots=True)
class _MatchedItem:
    doi_original: str
    doi: str
    title: str
    year: int


class CrossrefTitleResolver:
    """Recover missing DOI values through Crossref's official works endpoint."""

    def __init__(
        self,
        config: IdentifierResolverConfig | None = None,
        *,
        http: SafeHttpClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or IdentifierResolverConfig()
        if self.config.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if self.config.positive_ttl_seconds < 0:
            raise ValueError("positive_ttl_seconds must be non-negative")
        if self.config.negative_ttl_seconds < 0:
            raise ValueError("negative_ttl_seconds must be non-negative")
        self.http = http or SafeHttpClient()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve(self, paper: PaperCandidate) -> IdentifierResolution:
        """Return a decision and, on success, a DOI-populated paper copy."""

        if paper.doi:
            return IdentifierResolution(
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
            resolution = IdentifierResolution(
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
        contact_email = (self.config.contact_email or "").strip()
        all_items: list[Mapping[str, Any]] = []
        http_status: int | None = None
        for query_title, _ in title_queries:
            query: dict[str, str] = {
                "query.title": query_title,
                "rows": str(CROSSREF_MAX_RESULTS),
            }
            if contact_email:
                query["mailto"] = contact_email
            url = f"{CROSSREF_WORKS_URL}?{urlencode(query)}"

            try:
                fetch = self.http.get_bytes(
                    url,
                    max_bytes=self.config.max_response_bytes,
                    headers={"Accept": "application/json"},
                )
            except HttpClientError as exc:
                return IdentifierResolution(
                    status="request_error",
                    reason="crossref_request_failed",
                    paper=paper,
                    error_code=getattr(exc, "code", "http_error"),
                )

            http_status = fetch.status_code
            if not 200 <= fetch.status_code < 300:
                return IdentifierResolution(
                    status="http_error",
                    reason="crossref_http_error",
                    paper=paper,
                    http_status=fetch.status_code,
                )

            try:
                payload = json.loads(fetch.body)
            except (UnicodeError, json.JSONDecodeError):
                return IdentifierResolution(
                    status="invalid_response",
                    reason="invalid_crossref_json",
                    paper=paper,
                    http_status=fetch.status_code,
                )
            items = _crossref_items(payload)
            if items is None:
                return IdentifierResolution(
                    status="invalid_response",
                    reason="invalid_crossref_payload",
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
            / f"{safe_source_stem(paper.source_id)}.crossref-doi.json"
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
        if not value or value.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        if value.get("resolver") != "crossref-title":
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

        cached_result = value.get("result")
        if not isinstance(cached_result, dict):
            return None
        status = cached_result.get("status")
        reason = cached_result.get("reason")
        if status not in _CACHEABLE_STATUSES or not isinstance(reason, str):
            return None

        resolved_paper = paper
        doi_original = _optional_string(cached_result.get("doi_original"))
        if status == "resolved":
            cached_doi = _optional_string(cached_result.get("doi"))
            try:
                canonical_doi = normalize_doi(cached_doi)
            except InvalidDoiError:
                return None
            if canonical_doi is None:
                return None
            matched_title = _optional_string(cached_result.get("matched_title"))
            matched_year = _paper_year(cached_result.get("matched_year"))
            if (
                normalize_title(matched_title) not in normalized_titles
                or matched_year is None
                or publication_year is None
                or abs(matched_year - publication_year) > 1
            ):
                return None
            resolved_paper = replace(
                paper,
                doi_original=doi_original or cached_doi,
                doi=canonical_doi,
                doi_source="crossref-title",
                identifier_resolution={
                    "resolver": "crossref-title",
                    "status": "resolved",
                    "reason": reason,
                    "matched_title": matched_title,
                    "matched_year": matched_year,
                    "cache_hit": True,
                },
            )

        return IdentifierResolution(
            status=status,
            reason=reason,
            paper=resolved_paper,
            doi_original=doi_original,
            matched_title=_optional_string(cached_result.get("matched_title")),
            matched_year=_optional_int(cached_result.get("matched_year")),
            returned_items=_nonnegative_int(cached_result.get("returned_items")),
            exact_title_matches=_nonnegative_int(
                cached_result.get("exact_title_matches")
            ),
            title_year_matches=_nonnegative_int(
                cached_result.get("title_year_matches")
            ),
            cache_hit=True,
            http_status=_optional_int(cached_result.get("http_status")),
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
            "resolver": "crossref-title",
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
            # Identifier recovery must not fail merely because its optional
            # cache directory is temporarily unwritable.
            return


def normalize_title(value: str | None) -> str | None:
    """Normalize a title for conservative, equality-only comparison."""

    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    normalized = " ".join(normalized.split())
    return normalized or None


def paper_title_queries(paper: PaperCandidate) -> tuple[tuple[str, str], ...]:
    """Return unique ``(query title, normalized title)`` pairs in source order."""

    queries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in (paper.title, *paper.alternate_titles):
        normalized = normalize_title(value)
        if normalized is None or normalized in seen or not isinstance(value, str):
            continue
        seen.add(normalized)
        queries.append((value.strip(), normalized))
    return tuple(queries)


def _match_items(
    paper: PaperCandidate,
    items: list[Mapping[str, Any]],
    *,
    normalized_titles: tuple[str, ...],
    publication_year: int,
    http_status: int,
) -> IdentifierResolution:
    exact_title_matches = 0
    title_year_matches = 0
    matches: list[_MatchedItem] = []
    known_titles = frozenset(normalized_titles)

    for item in items:
        matching_title = next(
            (
                title
                for title in _item_titles(item)
                if normalize_title(title) in known_titles
            ),
            None,
        )
        if matching_title is None:
            continue
        exact_title_matches += 1

        matching_years = [
            year
            for year in _item_years(item)
            if abs(year - publication_year) <= 1
        ]
        if not matching_years:
            continue
        title_year_matches += 1
        matched_year = min(
            matching_years,
            key=lambda year: (abs(year - publication_year), year),
        )

        doi_original = _optional_string(item.get("DOI"))
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
                title=matching_title,
                year=matched_year,
            )
        )

    unique_matches: dict[str, _MatchedItem] = {}
    for match in matches:
        unique_matches.setdefault(match.doi.casefold(), match)

    common = {
        "paper": paper,
        "returned_items": len(items),
        "exact_title_matches": exact_title_matches,
        "title_year_matches": title_year_matches,
        "http_status": http_status,
    }
    if len(unique_matches) > 1:
        return IdentifierResolution(
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
        return IdentifierResolution(status="no_match", reason=reason, **common)

    match = next(iter(unique_matches.values()))
    resolved_paper = replace(
        paper,
        doi_original=match.doi_original,
        doi=match.doi,
        doi_source="crossref-title",
        identifier_resolution={
            "resolver": "crossref-title",
            "status": "resolved",
            "reason": "exact_title_and_year_match",
            "matched_title": match.title,
            "matched_year": match.year,
            "cache_hit": False,
        },
    )
    return IdentifierResolution(
        status="resolved",
        reason="exact_title_and_year_match",
        paper=resolved_paper,
        doi_original=match.doi_original,
        matched_title=match.title,
        matched_year=match.year,
        returned_items=len(items),
        exact_title_matches=exact_title_matches,
        title_year_matches=title_year_matches,
        http_status=http_status,
    )


def _crossref_items(value: Any) -> list[Mapping[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    message = value.get("message")
    if not isinstance(message, dict):
        return None
    items = message.get("items")
    if not isinstance(items, list):
        return None
    return [
        item
        for item in items[:CROSSREF_MAX_RESULTS]
        if isinstance(item, dict)
    ]


def _item_titles(item: Mapping[str, Any]) -> tuple[str, ...]:
    value = item.get("title")
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list):
        values = tuple(part for part in value if isinstance(part, str))
    else:
        values = ()
    return tuple(title for title in values if title.strip())


def _item_years(item: Mapping[str, Any]) -> tuple[int, ...]:
    years: set[int] = set()
    for key in ("published", "published-print", "published-online", "issued"):
        value = item.get(key)
        if not isinstance(value, dict):
            continue
        date_parts = value.get("date-parts")
        if not isinstance(date_parts, list):
            continue
        for parts in date_parts:
            if not isinstance(parts, list) or not parts:
                continue
            year = _optional_int(parts[0])
            if year is not None and 1000 <= year <= 9999:
                years.add(year)
    return tuple(sorted(years))


def _paper_year(value: Any) -> int | None:
    year = _optional_int(value)
    return year if year is not None and 1000 <= year <= 9999 else None


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
