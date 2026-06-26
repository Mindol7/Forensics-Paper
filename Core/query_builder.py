# query_builder.py #
"""
    - KCI Open API(articleSearch) / Scopus 검색용 쿼리 생성
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class KciQuery:
    """Single KCI Open API articleSearch query.

    The articleSearch endpoint accepts these parameters (others are ignored or
    optional). At least one of `title` or `keyword` must be non-empty — the
    `abstract` parameter is documented but verified to be silently ignored by
    the server. `date_from`/`date_to` use the YYYYMM (발행년월) format.
    `reg_date_from`/`reg_date_to` use YYYYMMDD (등록일, 8자리) — useful for
    sub-dividing publication-month queries that exceed the 10,000-result cap.
    """

    title: str = ""
    keyword: str = ""
    date_from: str | None = None      # YYYYMM (publication month)
    date_to: str | None = None        # YYYYMM
    reg_date_from: str | None = None  # YYYYMMDD (registration date)
    reg_date_to: str | None = None    # YYYYMMDD


@dataclass(frozen=True)
class ScopusQuery:
    """Single Scopus search query."""

    query: str
    view: str | None = None
    sort: str | None = None


def _normalize_term(term: str) -> str:
    return " ".join(term.strip().split())


def get_kci_target_years(*, span: int = 5, current_year: int | None = None) -> tuple[int, ...]:
    """Return the most recent `span` years inclusive of the current year."""
    year = current_year if current_year is not None else datetime.now().year
    start_year = year - (span - 1)
    return tuple(range(start_year, year + 1))


def get_kci_monthly_ranges(
    *,
    span_years: int = 5,
    start_year: int | None = None,
    end_year: int | None = None,
    current_year: int | None = None,
    current_month: int | None = None,
) -> list[tuple[str, str]]:
    """Return list of (dateFrom, dateTo) YYYYMM pairs covering each month in the span.

    Each tuple is a single-month window so the per-window result count stays
    bounded for paginated fetches.
    """
    now = datetime.now()
    actual_current_year = current_year if current_year is not None else now.year
    actual_current_month = current_month if current_month is not None else now.month
    requested_end_year = end_year if end_year is not None else actual_current_year
    if requested_end_year >= actual_current_year:
        year = actual_current_year
        month = actual_current_month
    else:
        year = requested_end_year
        month = 12
    first_year = start_year if start_year is not None else year - (span_years - 1)
    if first_year > year:
        raise ValueError("start_year must be less than or equal to end_year/current_year")

    ranges: list[tuple[str, str]] = []
    y, m = first_year, 1
    while (y, m) <= (year, month):
        ym = f"{y:04d}{m:02d}"
        ranges.append((ym, ym))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return ranges


SCOPUS_INCLUDE_SUBJECTS: tuple[str, ...] = ("COMP", "SOCI", "DECI", "MULT", "ENGI", "PSYC")


def get_scopus_target_years(*, current_year: int | None = None) -> tuple[int, ...]:
    year = current_year if current_year is not None else datetime.now().year
    start_year = year - 5
    end_year = year + 1
    return tuple(range(start_year, end_year + 1))


def _build_scopus_base_query_for_year(year: int) -> str:
    include_subjects = " or ".join(SCOPUS_INCLUDE_SUBJECTS)
    return f"PUBYEAR = {year} AND SUBJAREA({include_subjects})"


def _to_scopus_proximity_term(term: str) -> str:
    words = term.split()
    if len(words) <= 1:
        return words[0] if words else ""
    return " W/16 ".join(words)


def _build_title_abs_key_clause(
    terms: Iterable[str],
    *,
    raw_clauses: Iterable[str] | None = None,
) -> str:
    normalized_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = _normalize_term(term)
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized_terms.append(normalized)

    segments: list[str] = []
    for term in normalized_terms:
        proximity_term = _to_scopus_proximity_term(term)
        if not proximity_term:
            continue
        segments.append(f"({proximity_term})")

    for clause in raw_clauses or []:
        normalized = " ".join(clause.strip().split())
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        segments.append(f"({normalized})")

    if not segments:
        return ""
    return " OR ".join(segments)


def build_scopus_queries(
    keyword_terms: Iterable[str] | None = None,
    keyword_clauses: Iterable[str] | None = None,
    *,
    target_years: Iterable[int] | None = None,
) -> list[ScopusQuery]:
    """Create Scopus query objects for each target year."""
    queries: list[ScopusQuery] = []
    years = tuple(target_years) if target_years is not None else get_scopus_target_years()
    title_abs_key_clause = _build_title_abs_key_clause(
        keyword_terms or [],
        raw_clauses=keyword_clauses,
    )

    for year in years:
        base_query = _build_scopus_base_query_for_year(year)
        if title_abs_key_clause:
            queries.append(ScopusQuery(query=f"TITLE-ABS-KEY({title_abs_key_clause}) AND {base_query}"))
        else:
            queries.append(ScopusQuery(query=base_query))

    deduped: list[ScopusQuery] = []
    seen: set[str] = set()
    for query in queries:
        key = query.query.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)
    return deduped
