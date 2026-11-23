# collect_builder.py #
"""
    - KCI Open API(articleSearch) 검색용 쿼리 생성
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol, TypeVar


KCI_REG_DATE_MIN = "19000101"
KCI_REG_DATE_MAX = "20991231"
KCI_MAX_RESULTS_PER_QUERY = 10_000


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
class KciQuerySlice:
    """One KCI query window proven to fit under the API result cap."""

    query: KciQuery
    total_count: int
    label: str


class KciCountProbe(Protocol):
    """Small dependency boundary used while preparing capped KCI queries."""

    def probe_total(self, query: KciQuery) -> int: ...


PaperT = TypeVar("PaperT")


def build_article_detail_targets(
    papers: Iterable[PaperT],
) -> dict[str, list[PaperT]]:
    """Group normalized papers by the exact KCI ART id for ``articleDetail``.

    The normalizer retains the authoritative KCI id in ``raw_payload.arti_id``.
    Grouping here ensures each detail record is requested once even when the
    same article arrived through several search terms.
    """

    targets: dict[str, list[PaperT]] = {}
    for paper in papers:
        payload = getattr(paper, "raw_payload", None)
        article_id = payload.get("arti_id") if isinstance(payload, dict) else None
        if isinstance(article_id, str) and article_id.strip():
            targets.setdefault(article_id.strip(), []).append(paper)
    return targets


def describe_kci_query_window(query: KciQuery) -> str:
    parts: list[str] = []
    if query.date_from or query.date_to:
        parts.append(f"pub={query.date_from or '*'}~{query.date_to or '*'}")
    if query.reg_date_from or query.reg_date_to:
        parts.append(f"reg={query.reg_date_from or '*'}~{query.reg_date_to or '*'}")
    return " ".join(parts)


def _iter_yyyymm_range(date_from: str, date_to: str) -> list[str]:
    start_year, start_month = int(date_from[:4]), int(date_from[4:6])
    end_year, end_month = int(date_to[:4]), int(date_to[4:6])
    months: list[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _split_yyyymmdd_range(
    date_from: str,
    date_to: str,
) -> tuple[tuple[str, str], tuple[str, str]]:
    start = datetime.strptime(date_from, "%Y%m%d").date()
    end = datetime.strptime(date_to, "%Y%m%d").date()
    if start >= end:
        raise ValueError(
            f"Cannot split one-day KCI regDate range: {date_from}~{date_to}"
        )
    midpoint = start + timedelta(days=(end - start).days // 2)
    right_start = midpoint + timedelta(days=1)
    return (
        (start.strftime("%Y%m%d"), midpoint.strftime("%Y%m%d")),
        (right_start.strftime("%Y%m%d"), end.strftime("%Y%m%d")),
    )


def _split_kci_query_by_reg_date(
    probe: KciCountProbe,
    query: KciQuery,
    *,
    label: str,
    max_results: int,
    on_split: Callable[[str], None] | None,
) -> list[KciQuerySlice]:
    reg_date_from = query.reg_date_from or KCI_REG_DATE_MIN
    reg_date_to = query.reg_date_to or KCI_REG_DATE_MAX
    bounded_query = replace(
        query,
        reg_date_from=reg_date_from,
        reg_date_to=reg_date_to,
    )
    total_count = probe.probe_total(bounded_query)
    if total_count == 0:
        return []
    if total_count <= max_results:
        return [
            KciQuerySlice(
                query=bounded_query,
                total_count=total_count,
                label=describe_kci_query_window(bounded_query),
            )
        ]
    if reg_date_from == reg_date_to:
        raise RuntimeError(
            "KCI query still exceeds the result cap for a single registration "
            f"day: {label} {describe_kci_query_window(bounded_query)} "
            f"total={total_count}"
        )

    if on_split is not None:
        on_split(
            "KCI oversized slice split | "
            f"{label} {describe_kci_query_window(bounded_query)} total={total_count}"
        )
    slices: list[KciQuerySlice] = []
    for child_from, child_to in _split_yyyymmdd_range(reg_date_from, reg_date_to):
        child = replace(
            bounded_query,
            reg_date_from=child_from,
            reg_date_to=child_to,
        )
        slices.extend(
            _split_kci_query_by_reg_date(
                probe,
                child,
                label=label,
                max_results=max_results,
                on_split=on_split,
            )
        )
    return slices


def build_kci_query_slices(
    probe: KciCountProbe,
    query: KciQuery,
    *,
    label: str = "",
    max_results: int = KCI_MAX_RESULTS_PER_QUERY,
    on_split: Callable[[str], None] | None = None,
) -> list[KciQuerySlice]:
    """Prepare complete KCI query slices without crossing the API cap."""

    if max_results < 1:
        raise ValueError("max_results must be at least 1")
    total_count = probe.probe_total(query)
    if total_count == 0:
        return []
    if total_count <= max_results:
        return [
            KciQuerySlice(
                query=query,
                total_count=total_count,
                label=describe_kci_query_window(query),
            )
        ]

    if query.date_from and query.date_to and query.date_from != query.date_to:
        if on_split is not None:
            on_split(
                "KCI oversized query split by publication month | "
                f"{label} {describe_kci_query_window(query)} total={total_count}"
            )
        slices: list[KciQuerySlice] = []
        for month in _iter_yyyymm_range(query.date_from, query.date_to):
            month_query = replace(
                query,
                date_from=month,
                date_to=month,
                reg_date_from=None,
                reg_date_to=None,
            )
            slices.extend(
                build_kci_query_slices(
                    probe,
                    month_query,
                    label=label,
                    max_results=max_results,
                    on_split=on_split,
                )
            )
        return slices

    return _split_kci_query_by_reg_date(
        probe,
        query,
        label=label,
        max_results=max_results,
        on_split=on_split,
    )


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
