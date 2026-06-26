#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from Collector.kci import KciCollector, KciRawPaper, MAX_RESULTS_PER_QUERY
from Core.deduplicator import deduplicate_papers
from Core.normalizer import NormalizedPaper, normalize_kci_papers
from Core.query_builder import KciQuery, get_kci_monthly_ranges
from Exporter.export_excl import export_papers_to_excel
from Processor.filter import (
    MATCH_FIELD_ORDER,
    KeywordFilterConfig,
    apply_keyword_filter,
    exclude_matches_text,
    keyword_matches_text,
    load_keyword_filter_configs,
)
from Processor.scopus_pipeline import run_scopus_pipeline_yearly
from Processor.summarizer import summarize_papers
from Storage.db import DatabaseManager
from config import Settings, load_settings


@dataclass
class PipelineResult:
    raw_count: int
    normalized_count: int
    deduplicated_count: int
    relevant_count: int
    stored_count: int
    export_path: Path | None


@dataclass(frozen=True)
class KciQuerySlice:
    query: KciQuery
    total_count: int
    label: str


@dataclass
class KciCollectOutcome:
    """Result of one collection unit (a Path-1 term or a Path-2 month).

    A unit that exhausts its retries records the failed slice label in
    `failures` and is skipped, instead of raising. The KCI pipeline only
    persists to the DB after *all* collection finishes, so letting one failed
    month abort the run would discard several hours of already-collected data.
    Skipping keeps everything else; failed units are reported at the end so the
    user can re-run just those (e.g. `--sweep-month YYYYMM`).
    """

    papers: list[KciRawPaper] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


SourceType = Literal["kci", "scopus", "all"]
KCI_FORENSIC_SOCIETY_NAMES = ("한국디지털포렌식학회",)
KCI_FORENSIC_SOCIETY_JOURNALS = ("디지털포렌식연구",)
KCI_SOCIETY_COLLECTION_REASON = "제공된 키워드셋은 아니지만, 한국디지털포렌식 학회 논문이기에 수집해옴"
KCI_SOCIETY_COLLECTION_CATEGORY = "한국디지털포렌식학회"
KCI_SOCIETY_COLLECTION_KEYWORD = "학회 수집"
KCI_MIN_REQUEST_TIMEOUT = 30
KCI_SAFE_RATE_LIMIT_RPS = 8.0
KCI_SAFE_MAX_WORKERS = 4
KCI_SAFE_SWEEP_MAX_WORKERS = 1
KCI_REG_DATE_MIN = "19000101"
KCI_REG_DATE_MAX = "20991231"
# pipeline_state keys used to resume an interrupted KCI run. Each sweep month is
# processed and stored as it completes, then recorded here so `--resume` can skip
# already-finished work after a crash/disconnect mid-run.
KCI_SWEEP_PROGRESS_STATE_KEY = "kci:sweep_progress"
KCI_PATH1_DONE_STATE_KEY = "kci:path1_done"
ORG_LEGAL_PREFIXES = ("사단법인", "재단법인", "공익사단법인", "공익재단법인")
ORG_PAREN_PREFIXES = ("구.", "구 ", "전.", "전 ", "현.", "현 ")


def _log(step: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{step}] {message}", flush=True)


def _effective_kci_rate_limit_rps(settings: Settings) -> float | None:
    if not settings.kci_rate_limit_rps or settings.kci_rate_limit_rps <= 0:
        return None
    return min(settings.kci_rate_limit_rps, KCI_SAFE_RATE_LIMIT_RPS)


def _effective_kci_timeout(settings: Settings) -> int:
    return max(settings.request_timeout, KCI_MIN_REQUEST_TIMEOUT)


def _effective_worker_count(requested: int, *, cap: int) -> int:
    return min(max(1, requested), cap)


def _build_kci_collector(settings: Settings, *, page_size: int | None) -> KciCollector:
    if not settings.kci_open_api_key:
        raise ValueError("KCI_OPEN_API_KEY is not set")
    return KciCollector(
        api_key=settings.kci_open_api_key,
        base_url=settings.kci_open_api_url,
        timeout=_effective_kci_timeout(settings),
        page_size=page_size or settings.kci_page_size,
        rate_limit_rps=_effective_kci_rate_limit_rps(settings),
    )


def _iter_yyyymm_range(date_from: str, date_to: str) -> list[str]:
    start_year, start_month = int(date_from[:4]), int(date_from[4:6])
    end_year, end_month = int(date_to[:4]), int(date_to[4:6])
    months: list[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}{month:02d}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return months


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _format_yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def _split_yyyymmdd_range(date_from: str, date_to: str) -> tuple[tuple[str, str], tuple[str, str]]:
    start = _parse_yyyymmdd(date_from)
    end = _parse_yyyymmdd(date_to)
    if start >= end:
        raise ValueError(f"Cannot split one-day KCI regDate range: {date_from}~{date_to}")
    midpoint = start + timedelta(days=(end - start).days // 2)
    right_start = midpoint + timedelta(days=1)
    return (
        (_format_yyyymmdd(start), _format_yyyymmdd(midpoint)),
        (_format_yyyymmdd(right_start), _format_yyyymmdd(end)),
    )


def _describe_kci_query_window(query: KciQuery) -> str:
    parts: list[str] = []
    if query.date_from or query.date_to:
        parts.append(f"pub={query.date_from or '*'}~{query.date_to or '*'}")
    if query.reg_date_from or query.reg_date_to:
        parts.append(f"reg={query.reg_date_from or '*'}~{query.reg_date_to or '*'}")
    return " ".join(parts)


def _split_kci_query_by_reg_date(
    collector: KciCollector,
    query: KciQuery,
    *,
    label: str,
    log_step: str,
) -> list[KciQuerySlice]:
    reg_date_from = query.reg_date_from or KCI_REG_DATE_MIN
    reg_date_to = query.reg_date_to or KCI_REG_DATE_MAX
    bounded_query = replace(
        query,
        reg_date_from=reg_date_from,
        reg_date_to=reg_date_to,
    )
    total_count = collector.probe_total(bounded_query)
    if total_count == 0:
        return []
    if total_count <= MAX_RESULTS_PER_QUERY:
        return [
            KciQuerySlice(
                query=bounded_query,
                total_count=total_count,
                label=_describe_kci_query_window(bounded_query),
            )
        ]

    if reg_date_from == reg_date_to:
        raise RuntimeError(
            "KCI query still exceeds the 10,000-result API cap for a single registration day: "
            f"{label} {_describe_kci_query_window(bounded_query)} total={total_count}. "
            "Refusing to continue because this would silently miss abstract matches."
        )

    left_range, right_range = _split_yyyymmdd_range(reg_date_from, reg_date_to)
    _log(
        log_step,
        "KCI oversized slice split | "
        f"{label} {_describe_kci_query_window(bounded_query)} total={total_count}",
    )
    slices: list[KciQuerySlice] = []
    for child_from, child_to in (left_range, right_range):
        child_query = replace(
            bounded_query,
            reg_date_from=child_from,
            reg_date_to=child_to,
        )
        slices.extend(_split_kci_query_by_reg_date(collector, child_query, label=label, log_step=log_step))
    return slices


def _build_kci_query_slices(
    collector: KciCollector,
    query: KciQuery,
    *,
    label: str,
    log_step: str,
) -> list[KciQuerySlice]:
    total_count = collector.probe_total(query)
    if total_count == 0:
        return []
    if total_count <= MAX_RESULTS_PER_QUERY:
        return [
            KciQuerySlice(
                query=query,
                total_count=total_count,
                label=_describe_kci_query_window(query),
            )
        ]

    if query.date_from and query.date_to and query.date_from != query.date_to:
        _log(
            log_step,
            "KCI oversized query split by publication month | "
            f"{label} {_describe_kci_query_window(query)} total={total_count}",
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
            slices.extend(_build_kci_query_slices(collector, month_query, label=label, log_step=log_step))
        return slices

    return _split_kci_query_by_reg_date(collector, query, label=label, log_step=log_step)


def _collect_by_field(
    collector: KciCollector,
    field_name: Literal["title", "keyword"],
    keyword: str,
    *,
    date_from: str,
    date_to: str,
    max_pages: int | None,
    idx: int,
    total: int,
) -> KciCollectOutcome:
    prefix = f"  [{field_name} {idx}/{total}] {keyword!r}"
    _log("3a", f"{prefix} START")
    query = (
        KciQuery(title=keyword, date_from=date_from, date_to=date_to)
        if field_name == "title"
        else KciQuery(keyword=keyword, date_from=date_from, date_to=date_to)
    )
    papers: list[KciRawPaper] = []
    failures: list[str] = []
    try:
        slices = _build_kci_query_slices(collector, query, label=prefix, log_step="3a")
    except Exception as exc:
        _log("3a", f"{prefix} ERROR while probing slices: {exc} (skipping unit)")
        return KciCollectOutcome(papers=papers, failures=[f"{prefix} (probe)"])

    if not slices:
        _log("3a", f"{prefix} END | fetched=0")
        return KciCollectOutcome()
    if len(slices) > 1:
        _log("3a", f"{prefix} split={len(slices)} slices")
    for slice_idx, query_slice in enumerate(slices, start=1):
        slice_prefix = prefix
        if len(slices) > 1:
            slice_prefix = f"{prefix} slice {slice_idx}/{len(slices)} {query_slice.label}"
        try:
            papers.extend(
                collector.collect(
                    query_slice.query,
                    max_pages=max_pages,
                    on_page=lambda msg, _p=slice_prefix: _log("3a", f"{_p} {msg}"),
                )
            )
        except Exception as exc:
            _log(
                "3a",
                f"{slice_prefix} ERROR: {exc} "
                f"(skipping slice, keeping {len(papers)} collected so far)",
            )
            failures.append(slice_prefix)
    _log("3a", f"{prefix} END | fetched={len(papers)} failed_slices={len(failures)}")
    return KciCollectOutcome(papers=papers, failures=failures)


def _is_kci_society_raw_paper(paper: KciRawPaper) -> bool:
    publisher = (paper.publisher_name or "").casefold()
    journal = (paper.journal_name or "").casefold()
    return any(name.casefold() in publisher for name in KCI_FORENSIC_SOCIETY_NAMES) or any(
        journal_name.casefold() in journal for journal_name in KCI_FORENSIC_SOCIETY_JOURNALS
    )


def _is_kci_society_paper(paper: NormalizedPaper) -> bool:
    publisher = (paper.institution_id or "").casefold()
    journal = (paper.journal_id or "").casefold()
    return any(name.casefold() in publisher for name in KCI_FORENSIC_SOCIETY_NAMES) or any(
        journal_name.casefold() in journal for journal_name in KCI_FORENSIC_SOCIETY_JOURNALS
    )


def _load_blacklist(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    terms: list[str] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            cleaned = " ".join(line.strip().split())
            if not cleaned or cleaned.startswith("#"):
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(cleaned)
    return tuple(terms)


def _simplify_org_name(value: str) -> str:
    text = value.casefold()
    for prefix in ORG_LEGAL_PREFIXES:
        text = text.replace(prefix.casefold(), "")
    text = text.replace("(사)", "").replace("（사）", "")
    text = text.replace("(재)", "").replace("（재）", "")
    return "".join(ch for ch in text if ch.isalnum() or "가" <= ch <= "힣")


def _org_name_variants(value: str) -> set[str]:
    variants: set[str] = set()
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        return variants

    candidates = {cleaned}
    candidates.add(" ".join(re.sub(r"\([^)]*\)", " ", cleaned).split()))
    for match in re.finditer(r"\(([^)]*)\)", cleaned):
        inner = " ".join(match.group(1).split())
        for prefix in ORG_PAREN_PREFIXES:
            if inner.casefold().startswith(prefix.casefold()):
                inner = inner[len(prefix):].strip()
                break
        if inner:
            candidates.add(inner)

    for candidate in candidates:
        simplified = _simplify_org_name(candidate)
        if simplified:
            variants.add(simplified)
    return variants


def _matches_blacklist_terms(text_parts: list[str | None], blacklist_terms: tuple[str, ...]) -> bool:
    haystack = " ".join(part or "" for part in text_parts).casefold()
    if not haystack.strip() or not blacklist_terms:
        return False
    haystack_no_space = "".join(haystack.split())
    haystack_variants: set[str] = set()
    for part in text_parts:
        if part:
            haystack_variants.update(_org_name_variants(part))

    for term in blacklist_terms:
        if keyword_matches_text(term.casefold(), haystack, haystack_no_space):
            return True
        for term_variant in _org_name_variants(term):
            for haystack_variant in haystack_variants:
                if term_variant in haystack_variant or haystack_variant in term_variant:
                    return True
    return False


def _is_blacklisted_raw_paper(paper: KciRawPaper, blacklist_terms: tuple[str, ...]) -> bool:
    if _is_kci_society_raw_paper(paper):
        return False
    return _matches_blacklist_terms(
        [paper.publisher_name, paper.journal_name],
        blacklist_terms,
    )


def _is_blacklisted_kci_paper(paper: NormalizedPaper, blacklist_terms: tuple[str, ...]) -> bool:
    if _is_kci_society_paper(paper):
        return False
    return _matches_blacklist_terms(
        [paper.institution_id, paper.journal_id],
        blacklist_terms,
    )


def _filter_blacklisted_kci_papers(
    papers: list[NormalizedPaper],
    blacklist_terms: tuple[str, ...],
) -> list[NormalizedPaper]:
    if not blacklist_terms:
        return papers
    return [paper for paper in papers if not _is_blacklisted_kci_paper(paper, blacklist_terms)]


def _matches_terms(text_parts: list[str | None], terms: list[str]) -> bool:
    """Path-2 candidate filter using the layered keyword variants (V0/V1/V3)."""
    haystack = " ".join(part or "" for part in text_parts).casefold()
    if not haystack.strip() or not terms:
        return False
    haystack_no_space = "".join(haystack.split())
    return any(keyword_matches_text(term, haystack, haystack_no_space) for term in terms)


def _matches_exclude_terms(text_parts: list[str | None], terms: list[str]) -> bool:
    """Exclude check for the sweep candidate filter — whole-word matching only.

    Mirrors [[apply_keyword_filter]]: short exclude terms ('pcr', 'snp', ...)
    must not false-match as substrings (e.g. 'pcr' inside 'appcredential') and
    silently drop legitimate papers at the candidate stage.
    """
    haystack = " ".join(part or "" for part in text_parts).casefold()
    if not haystack.strip() or not terms:
        return False
    return any(exclude_matches_text(term, haystack) for term in terms)


def _filter_sweep_candidates(
    papers: list[KciRawPaper],
    config: KeywordFilterConfig,
    blacklist_terms: tuple[str, ...],
) -> list[KciRawPaper]:
    """Apply the standard sweep candidate filter (society fast-path, blacklist, include/exclude)."""
    include_terms = [t.casefold() for t in config.include_any]
    exclude_terms = [t.casefold() for t in config.exclude_any]
    kept: list[KciRawPaper] = []
    for paper in papers:
        if _is_kci_society_raw_paper(paper):
            paper.matched_query = KCI_FORENSIC_SOCIETY_NAMES[0]
            paper.matched_field = "journal"
            kept.append(paper)
            continue
        if _is_blacklisted_raw_paper(paper, blacklist_terms):
            continue
        searchable_parts = [
            paper.title_original or "",
            paper.title_english or "",
            paper.title_foreign or "",
            " ".join(paper.keywords),
            paper.abstract_original or "",
            paper.abstract_english or "",
        ]
        if _matches_exclude_terms(searchable_parts, exclude_terms):
            continue
        if _matches_terms(searchable_parts, include_terms):
            kept.append(paper)
    return kept


def _sweep_month(
    collector: KciCollector,
    year_month: tuple[str, str],
    config: KeywordFilterConfig,
    blacklist_terms: tuple[str, ...],
    *,
    max_pages: int | None,
    idx: int,
    total: int,
) -> KciCollectOutcome:
    """Broad monthly sweep — `title=*` query per month with client-side keyword filter.

    KCI caps each query at 10,000 results. Oversized months are split by
    registration date until every slice fits under the cap, so abstract-only
    matches are not silently truncated.

    A slice (or the whole month, if probing fails) that exhausts its retries is
    skipped and recorded, not raised — see [[KciCollectOutcome]]. Pages already
    filtered before the failure are kept.
    """
    date_from, date_to = year_month
    prefix = f"  [sweep {idx}/{total}] {date_from}"
    _log("3b", f"{prefix} START")
    kept: list[KciRawPaper] = []
    failures: list[str] = []
    fetched_count = 0
    query = KciQuery(title="*", date_from=date_from, date_to=date_to)
    try:
        slices = _build_kci_query_slices(collector, query, label=prefix, log_step="3b")
    except Exception as exc:
        _log("3b", f"{prefix} ERROR while probing slices: {exc} (skipping month)")
        return KciCollectOutcome(papers=kept, failures=[f"{prefix} (probe)"])

    if len(slices) > 1:
        _log("3b", f"{prefix} split={len(slices)} slices total={sum(s.total_count for s in slices)}")
    for slice_idx, query_slice in enumerate(slices, start=1):
        slice_prefix = prefix
        if len(slices) > 1:
            slice_prefix = f"{prefix} slice {slice_idx}/{len(slices)} {query_slice.label}"
        try:
            for page_items, _total_count in collector.iter_pages(
                query_slice.query,
                max_pages=max_pages,
                on_page=lambda msg, _p=slice_prefix: _log("3b", f"{_p} {msg}"),
            ):
                fetched_count += len(page_items)
                kept.extend(_filter_sweep_candidates(page_items, config, blacklist_terms))
        except Exception as exc:
            _log(
                "3b",
                f"{slice_prefix} ERROR after fetched={fetched_count} "
                f"kept_after_filter={len(kept)}: {exc} (skipping slice)",
            )
            failures.append(slice_prefix)
    _log(
        "3b",
        f"{prefix} END | fetched={fetched_count} "
        f"kept_after_filter={len(kept)} failed_slices={len(failures)}",
    )
    return KciCollectOutcome(papers=kept, failures=failures)


def _enrich_kci_relevant_keywords(
    collector: KciCollector,
    papers: list[NormalizedPaper],
    *,
    max_workers: int,
) -> tuple[int, int]:
    """Fetch articleDetail for the (already small) relevant set to fill in the
    keyword list.

    KCI's articleSearch response includes title, abstract, journal/publisher,
    year and DOI but omits keywords, so enrichment is no longer needed for
    filtering — only to enrich the keyword list of papers we already decided to
    keep. Enriching every candidate up front (tens of thousands of serial
    articleDetail calls + their abstracts held in memory) is what stalled the
    run for hours; here we only touch the relevant subset.
    """
    id_to_papers: dict[str, list[NormalizedPaper]] = {}
    for paper in papers:
        article_id = paper.raw_payload.get("arti_id") if paper.raw_payload else None
        if article_id:
            id_to_papers.setdefault(article_id, []).append(paper)
    article_ids = sorted(id_to_papers)
    if not article_ids:
        return 0, 0

    details: dict[str, KciRawPaper | None] = {}
    failed = 0

    def _fetch(article_id: str) -> tuple[str, KciRawPaper | None]:
        try:
            return article_id, collector.fetch_article_detail(article_id)
        except Exception as exc:
            _log("7b", f"  [detail] {article_id} ERROR: {exc}")
            return article_id, None

    worker_count = max(1, max_workers)
    if worker_count == 1:
        for article_id in article_ids:
            key, detail = _fetch(article_id)
            details[key] = detail
            if detail is None:
                failed += 1
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_fetch, article_id) for article_id in article_ids]
            for future in as_completed(futures):
                key, detail = future.result()
                details[key] = detail
                if detail is None:
                    failed += 1

    for article_id, paper_list in id_to_papers.items():
        detail = details.get(article_id)
        if not detail or not detail.keywords:
            continue
        for paper in paper_list:
            seen = {keyword.casefold() for keyword in paper.keywords}
            for keyword in detail.keywords:
                if keyword.casefold() not in seen:
                    paper.keywords.append(keyword)
                    seen.add(keyword.casefold())
            # Record the article's authoritative keyword list. Pre-enrichment
            # keywords are search-term seeds (articleSearch returns no keywords),
            # so this is the only source of the real keyword field for storage.
            paper.raw_payload["_real_keywords"] = list(detail.keywords)

    return len(article_ids) - failed, failed


def _filter_kci_year_range(
    papers: list[NormalizedPaper],
    *,
    start_year: int,
    end_year: int,
) -> list[NormalizedPaper]:
    return [
        paper
        for paper in papers
        if paper.publication_year is not None and start_year <= paper.publication_year <= end_year
    ]


def _set_kci_classification(
    paper: NormalizedPaper,
    *,
    categories: list[str],
    matched_keywords: list[str],
    reasons: list[str] | None = None,
) -> None:
    paper.categories = [value for value in categories if value and value != "*"]
    paper.matched_keywords = [value for value in matched_keywords if value and value != "*"]
    if reasons is not None:
        paper.relevance_reasons = [value for value in reasons if value and value != "*"]


def _load_done_months(db: DatabaseManager) -> set[str]:
    raw = db.get_state(KCI_SWEEP_PROGRESS_STATE_KEY)
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    return {str(item) for item in data} if isinstance(data, list) else set()


def _save_done_months(db: DatabaseManager, done: set[str]) -> None:
    db.set_state(KCI_SWEEP_PROGRESS_STATE_KEY, json.dumps(sorted(done)))


def _clear_kci_checkpoint(db: DatabaseManager) -> None:
    db.set_state(KCI_SWEEP_PROGRESS_STATE_KEY, "[]")
    db.set_state(KCI_PATH1_DONE_STATE_KEY, "")


def _classify_kci_relevance(
    papers: list[NormalizedPaper],
    keyword_config: KeywordFilterConfig,
    breakdown: dict[str, int],
) -> list[NormalizedPaper]:
    """Flag relevance by exact local matching (title -> keyword -> abstract),
    plus the Korean Digital Forensics Society journal/publisher fast-path."""
    relevant: list[NormalizedPaper] = []
    for paper in papers:
        apply_keyword_filter(paper, keyword_config)
        if paper.is_relevant:
            if not paper.categories or not paper.matched_keywords:
                continue
            relevant.append(paper)
            for reason in paper.relevance_reasons:
                if reason.startswith("matched_in:"):
                    field_name = reason.split(":", 1)[1]
                    if field_name in breakdown:
                        breakdown[field_name] += 1
                    break
            continue

        if _is_kci_society_paper(paper):
            paper.is_relevant = True
            paper.relevance_score = 1.0
            _set_kci_classification(
                paper,
                categories=[KCI_SOCIETY_COLLECTION_CATEGORY],
                matched_keywords=[KCI_SOCIETY_COLLECTION_KEYWORD],
                reasons=[KCI_SOCIETY_COLLECTION_REASON],
            )
            breakdown["journal"] += 1
            relevant.append(paper)
    return relevant


@dataclass
class KciBatchResult:
    summarized: list[NormalizedPaper]
    raw_count: int
    normalized_count: int
    deduplicated_count: int
    stored_count: int


def _process_and_store_kci_batch(
    *,
    collector: KciCollector,
    db: DatabaseManager,
    raw_papers: list[KciRawPaper],
    keyword_config: KeywordFilterConfig,
    blacklist_terms: tuple[str, ...],
    start_year: int,
    end_year: int,
    enrich_workers: int,
    breakdown: dict[str, int],
    log_step: str,
    label: str,
) -> KciBatchResult:
    """Enrich -> normalize -> dedupe -> year/blacklist filter -> classify ->
    summarize -> upsert one batch, persisting it immediately.

    Storing each batch as it is collected bounds the blast radius of a mid-run
    crash (WSL OOM, SIGHUP, ...) to the in-progress batch instead of the whole
    multi-hour run. Cross-batch dedup is handled by the source_id-keyed DB
    upsert, so re-collecting an already-stored paper just updates its row.
    """
    raw_count = len(raw_papers)
    if not raw_papers:
        return KciBatchResult([], 0, 0, 0, 0)

    normalized = normalize_kci_papers(raw_papers)
    # articleSearch omits keywords. For Path-1 `keyword=K` hits, KCI already
    # matched K against the paper's keyword index server-side, so seed K into the
    # keyword field. This lets the classifier confirm keyword-field matches
    # WITHOUT enriching every candidate (the abstract/title are already inline).
    for paper in normalized:
        if paper.keywords:
            continue
        if (paper.raw_payload or {}).get("matched_field") != "keyword":
            continue
        seeds = [term for term in paper.matched_queries if term and term != "*"]
        if seeds:
            paper.keywords = seeds

    deduplicated = deduplicate_papers(normalized)
    deduplicated = _filter_kci_year_range(deduplicated, start_year=start_year, end_year=end_year)
    deduplicated = _filter_blacklisted_kci_papers(deduplicated, blacklist_terms)

    # Pass 1 — select who is relevant (and thus worth enriching). Title /
    # abstract / journal / year are present in the search response, so the strict
    # (anchor-aware) filter runs without a single articleDetail call. Enriching
    # the full candidate set first (often 100k+ broad keyword hits, mostly
    # anchor-less noise) is what froze the run for hours and exhausted memory.
    prelim_breakdown = {field_name: 0 for field_name in breakdown}
    candidates = _classify_kci_relevance(deduplicated, keyword_config, prelim_breakdown)

    # Enrich only the selected candidates to fill their author keyword list,
    # which articleSearch omits.
    detail_ok, detail_failed = _enrich_kci_relevant_keywords(
        collector, candidates, max_workers=enrich_workers
    )

    # Pass 2 — re-classify now that enrichment filled the author keywords. A paper
    # whose taxonomy term lives only in <keyword-group> (common for 디지털포렌식학회
    # papers arriving via the sweep with no keywords yet) now gets its real
    # category instead of the society fallback. This is the authoritative pass.
    relevant = _classify_kci_relevance(candidates, keyword_config, breakdown)

    # Storage hygiene: replace the keyword field with the article's real keyword
    # list from enrichment, dropping the search-term seeds we injected only to
    # drive matching (KCI's articleSearch never returns keywords, so any
    # pre-enrichment keyword is a seed, not a real author keyword). What matched
    # is preserved separately in matched_keywords.
    for paper in relevant:
        real_keywords = paper.raw_payload.pop("_real_keywords", None)
        paper.keywords = list(real_keywords) if real_keywords else []

    summarized = summarize_papers(relevant)
    stored = db.upsert_papers(summarized)
    _log(
        log_step,
        f"{label} batch stored | raw={raw_count} normalized={len(normalized)} "
        f"dedup={len(deduplicated)} relevant={len(relevant)} "
        f"enriched_ok={detail_ok} enriched_fail={detail_failed} upserted={stored}",
    )
    return KciBatchResult(
        summarized=summarized,
        raw_count=raw_count,
        normalized_count=len(normalized),
        deduplicated_count=len(deduplicated),
        stored_count=stored,
    )


def _run_kci_pipeline(
    *,
    settings: Settings,
    db: DatabaseManager,
    max_pages: int | None,
    page_size: int | None,
    skip_export: bool,
    sweep_months_override: list[tuple[str, str]] | None,
    resume: bool = False,
) -> PipelineResult:
    collector = _build_kci_collector(settings, page_size=page_size)
    effective_max_pages = max_pages or settings.kci_max_pages

    keyword_config = load_keyword_filter_configs(
        [
            settings.kci_keyword_filter_keywords_path,
            settings.kci_keyword_filter_english_keywords_path,
        ]
    )
    blacklist_terms = _load_blacklist(settings.kci_blacklist_path)
    search_terms = list(keyword_config.include_any)
    _log(
        "1",
        f"Keyword config loaded | rules={len(keyword_config.rules)} "
        f"terms={len(search_terms)} "
        f"categories={len(keyword_config.categories)} "
        f"exclude={len(keyword_config.exclude_any)} "
        f"blacklist={len(blacklist_terms)} "
        f"sources={settings.kci_keyword_filter_keywords_path},"
        f"{settings.kci_keyword_filter_english_keywords_path}",
    )
    if not keyword_config.rules:
        raise ValueError(
            f"No keyword rules in {settings.kci_keyword_filter_keywords_path}"
        )

    monthly_ranges = sweep_months_override or get_kci_monthly_ranges(
        span_years=settings.kci_recent_years,
        start_year=settings.kci_start_year,
        end_year=settings.kci_end_year,
    )
    date_from, date_to = monthly_ranges[0][0], monthly_ranges[-1][1]
    _log(
        "2",
        f"Target months: {len(monthly_ranges)} months "
        f"({date_from} ~ {date_to})",
    )

    requested_max_workers = max(1, settings.kci_max_workers)
    requested_sweep_workers = max(1, settings.kci_sweep_max_workers)
    max_workers = _effective_worker_count(requested_max_workers, cap=KCI_SAFE_MAX_WORKERS)
    sweep_workers = _effective_worker_count(
        requested_sweep_workers,
        cap=KCI_SAFE_SWEEP_MAX_WORKERS,
    )
    effective_rate_limit = _effective_kci_rate_limit_rps(settings)
    _log(
        "2b",
        "KCI pacing | "
        f"timeout={_effective_kci_timeout(settings)}"
        f"{'' if _effective_kci_timeout(settings) == settings.request_timeout else f' (requested {settings.request_timeout})'} "
        f"rate_limit_rps={effective_rate_limit or 'off'}"
        f"{'' if effective_rate_limit == settings.kci_rate_limit_rps else f' (requested {settings.kci_rate_limit_rps})'} "
        f"workers={max_workers}"
        f"{'' if max_workers == requested_max_workers else f' (requested {requested_max_workers})'} "
        f"sweep_workers={sweep_workers}"
        f"{'' if sweep_workers == requested_sweep_workers else f' (requested {requested_sweep_workers})'}",
    )

    sweep_enabled = settings.kci_enable_sweep or bool(sweep_months_override)
    title_search_enabled = not sweep_enabled

    enrich_workers = max_workers
    breakdown: dict[str, int] = {f: 0 for f in MATCH_FIELD_ORDER}
    breakdown["journal"] = 0
    collect_failures: list[str] = []
    export_rows: list[NormalizedPaper] = []
    raw_count = 0
    normalized_count = 0
    deduplicated_count = 0
    stored_count = 0

    # ---------- Checkpoint / resume ----------
    # Each batch (Path 1, and every sweep month) is stored to the DB the moment
    # it finishes, recording progress in pipeline_state. A fresh run clears the
    # checkpoint; `--resume` continues where an interrupted run left off so a
    # crash/disconnect mid-run does not force re-collecting everything.
    # A targeted `--sweep-month` backfill is an isolated operation: it must not
    # read or overwrite the full-run checkpoint, so checkpointing is disabled
    # whenever sweep months are overridden.
    checkpoint_enabled = sweep_months_override is None
    done_months: set[str] = set()
    path1_done = False
    if checkpoint_enabled and resume:
        done_months = _load_done_months(db)
        path1_done = bool(db.get_state(KCI_PATH1_DONE_STATE_KEY))
        _log(
            "2c",
            f"Resume ENABLED | path1_done={path1_done} sweep_months_done={len(done_months)}",
        )
    elif checkpoint_enabled:
        _clear_kci_checkpoint(db)
    elif resume:
        _log("2c", "Resume IGNORED | --sweep-month runs are isolated from the checkpoint")

    # ---------- Path 1: per-keyword server search ----------
    # `keyword=K` is restricted to `direct` taxonomy terms. Broad `anchored`
    # terms (ai, app, cloud, ...) match tens of thousands of non-forensic papers
    # in KCI's keyword index — all rejected later for lacking the forensic
    # anchor — so searching them collected ~100k noise papers and stalled the
    # batch classification. Their genuine matches are still caught by the sweep
    # (title/abstract) + post-enrichment re-classification. `title=K` (fast mode
    # only) keeps the full term set; the sweep inspects title text client-side
    # so title search is skipped whenever the sweep is enabled.
    keyword_search_terms = list(keyword_config.direct_keywords)
    if path1_done:
        _log("3a", "Path 1 SKIP | already completed in a prior run (--resume)")
    else:
        _log(
            "3a",
            "Path 1 START | "
            f"keyword_terms={len(keyword_search_terms)} (direct-only, of {len(search_terms)}) "
            f"workers={max_workers} title_search={'on' if title_search_enabled else 'off'}",
        )
        title_papers: list[KciRawPaper] = []
        keyword_papers: list[KciRawPaper] = []
        path1_failures: list[str] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            if title_search_enabled:
                futures.update(
                    {
                        executor.submit(
                            _collect_by_field,
                            collector,
                            "title",
                            keyword,
                            date_from=date_from,
                            date_to=date_to,
                            max_pages=effective_max_pages,
                            idx=idx,
                            total=len(search_terms),
                        ): "title"
                        for idx, keyword in enumerate(search_terms, start=1)
                    }
                )
            futures.update({
                executor.submit(
                    _collect_by_field,
                    collector,
                    "keyword",
                    keyword,
                    date_from=date_from,
                    date_to=date_to,
                    max_pages=effective_max_pages,
                    idx=idx,
                    total=len(keyword_search_terms),
                ): "keyword"
                for idx, keyword in enumerate(keyword_search_terms, start=1)
            })
            for future in as_completed(futures):
                # pop + del so each completed Future's cached result is released
                # once copied out, instead of being pinned in `futures` until the
                # loop ends (same retention that OOM-killed the sweep loop).
                field = futures.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    _log("3a", f"  [{field}] worker crashed unexpectedly: {exc}")
                    path1_failures.append(f"path1 {field} worker: {exc}")
                    del future
                    continue
                if field == "title":
                    title_papers.extend(outcome.papers)
                else:
                    keyword_papers.extend(outcome.papers)
                path1_failures.extend(outcome.failures)
                del outcome, future
        _log(
            "3a",
            f"Path 1 collected | title={len(title_papers)} keyword={len(keyword_papers)} "
            f"failed_units={len(path1_failures)}",
        )
        batch = _process_and_store_kci_batch(
            collector=collector,
            db=db,
            raw_papers=title_papers + keyword_papers,
            keyword_config=keyword_config,
            blacklist_terms=blacklist_terms,
            start_year=settings.kci_start_year,
            end_year=settings.kci_end_year,
            enrich_workers=enrich_workers,
            breakdown=breakdown,
            log_step="3a",
            label="Path 1",
        )
        export_rows.extend(batch.summarized)
        raw_count += batch.raw_count
        normalized_count += batch.normalized_count
        deduplicated_count += batch.deduplicated_count
        stored_count += batch.stored_count
        collect_failures.extend(path1_failures)
        # Only mark done when nothing failed, so --resume retries Path 1 if it
        # was partially throttled out.
        if checkpoint_enabled and not path1_failures:
            db.set_state(KCI_PATH1_DONE_STATE_KEY, "1")

    # ---------- Path 2: monthly broad sweep + client-side title+abstract filter ----------
    if sweep_enabled:
        pending = [
            (idx, ym)
            for idx, ym in enumerate(monthly_ranges, start=1)
            if ym[0] not in done_months
        ]
        resumed_skip = len(monthly_ranges) - len(pending)
        _log(
            "3b",
            f"Path 2 START | months={len(monthly_ranges)} pending={len(pending)} "
            f"resumed_skip={resumed_skip} workers={sweep_workers}",
        )
        with ThreadPoolExecutor(max_workers=sweep_workers) as executor:
            futures = {
                executor.submit(
                    _sweep_month,
                    collector,
                    ym,
                    keyword_config,
                    blacklist_terms,
                    max_pages=effective_max_pages,
                    idx=idx,
                    total=len(monthly_ranges),
                ): ym
                for idx, ym in pending
            }
            for future in as_completed(futures):
                # pop (not index): a completed Future caches its .result() — the
                # month's ~15k raw papers (abstracts inline). Keeping every month
                # in `futures` until the loop ends accumulated all 54 months in
                # memory and OOM-killed the process. Popping + del frees each
                # month before the (sweep_workers=1, minutes-long) wait for the
                # next one.
                ym = futures.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    _log("3b", f"  [sweep {ym[0]}] worker crashed unexpectedly: {exc}")
                    collect_failures.append(f"sweep {ym[0]} worker: {exc}")
                    del future
                    continue
                collect_failures.extend(outcome.failures)
                batch = _process_and_store_kci_batch(
                    collector=collector,
                    db=db,
                    raw_papers=outcome.papers,
                    keyword_config=keyword_config,
                    blacklist_terms=blacklist_terms,
                    start_year=settings.kci_start_year,
                    end_year=settings.kci_end_year,
                    enrich_workers=enrich_workers,
                    breakdown=breakdown,
                    log_step="3b",
                    label=f"sweep {ym[0]}",
                )
                export_rows.extend(batch.summarized)
                raw_count += batch.raw_count
                normalized_count += batch.normalized_count
                deduplicated_count += batch.deduplicated_count
                stored_count += batch.stored_count
                # Checkpoint only fully-collected months, so a month with a
                # failed slice is retried (not skipped) on the next --resume.
                if checkpoint_enabled and not outcome.failures:
                    done_months.add(ym[0])
                    _save_done_months(db, done_months)
                del outcome, batch, future
        _log("3b", f"Path 2 END | failed_units={len(collect_failures)}")
    else:
        _log(
            "3b",
            "Path 2 SKIP | KCI_ENABLE_SWEEP=false "
            "(fast title/keyword server search only; abstract-only matches require explicit sweep)",
        )

    # ---------- One-time DB hygiene (stale rows from prior runs) ----------
    cleaned_db_count = db.delete_kci_papers_outside_year_range(
        start_year=settings.kci_start_year,
        end_year=settings.kci_end_year,
    )
    cleaned_invalid_evidence_count = db.delete_kci_papers_with_invalid_match_evidence()
    breakdown_str = " ".join(f"{f}={c}" for f, c in breakdown.items())
    _log(
        "6",
        f"Relevance breakdown | relevant={len(export_rows)} ({breakdown_str}) "
        f"cleaned_db={cleaned_db_count} cleaned_invalid_evidence={cleaned_invalid_evidence_count}",
    )

    # ---------- Export ----------
    export_path: Path | None = None
    if not skip_export:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = settings.export_dir / f"kci_papers_{timestamp}.xlsx"
        export_papers_to_excel(export_rows, export_path)
        _log("9", f"Export END | path={export_path} rows={len(export_rows)}")

    db.set_state(settings.state_key_last_success_at, datetime.now(timezone.utc).isoformat())
    total_db_count = db.count_papers()

    # Clear the resume checkpoint only on a clean run, so a run that skipped any
    # failed unit keeps its checkpoint for a follow-up `--resume`.
    if checkpoint_enabled and not collect_failures:
        _clear_kci_checkpoint(db)

    _log(
        "10",
        "Final summary | "
        f"raw={raw_count} normalized={normalized_count} dedup={deduplicated_count} "
        f"relevant={len(export_rows)} stored={stored_count} db_total={total_db_count} "
        f"export={export_path} collect_failures={len(collect_failures)}",
    )

    if collect_failures:
        _log(
            "10b",
            f"WARNING: {len(collect_failures)} collection unit(s) failed and were "
            "SKIPPED. Everything collected was already stored; re-run with "
            "`--resume` to retry only the unfinished/failed units:",
        )
        for label in collect_failures:
            _log("10b", f"  FAILED: {label.strip()}")

    _log("11", "Pipeline END")

    return PipelineResult(
        raw_count=raw_count,
        normalized_count=normalized_count,
        deduplicated_count=deduplicated_count,
        relevant_count=len(export_rows),
        stored_count=stored_count,
        export_path=export_path,
    )


def run_pipeline(
    *,
    settings: Settings | None = None,
    source: SourceType = "kci",
    extra_terms: list[str] | None = None,
    max_pages: int | None = None,
    page_size: int | None = None,
    force_full: bool = False,
    skip_export: bool = False,
    keep_irrelevant: bool = False,
    enable_sweep: bool | None = None,
    sweep_months_override: list[tuple[str, str]] | None = None,
    resume: bool = False,
) -> PipelineResult:
    _log("0", "Pipeline START")

    _log("1", "Load settings START")
    if settings is None:
        settings = load_settings(validate=(source in ("kci", "all")))
    if enable_sweep is not None:
        settings = replace(settings, kci_enable_sweep=enable_sweep)
    _log("1", "Load settings END")

    _log("2", "Database init START")
    db = DatabaseManager(settings.database_url)
    db.create_tables()
    _log("2", "Database init END")

    if source == "kci":
        return _run_kci_pipeline(
            settings=settings,
            db=db,
            max_pages=max_pages,
            page_size=page_size,
            skip_export=skip_export,
            sweep_months_override=sweep_months_override,
            resume=resume,
        )

    if source == "scopus":
        scopus_result = run_scopus_pipeline_yearly(
            settings=settings,
            db=db,
            extra_terms=extra_terms,
            max_pages=max_pages,
            page_size=page_size,
            force_full=force_full,
            skip_export=skip_export,
            log=_log,
        )
        return PipelineResult(
            raw_count=scopus_result.raw_count,
            normalized_count=scopus_result.normalized_count,
            deduplicated_count=scopus_result.deduplicated_count,
            relevant_count=scopus_result.relevant_count,
            stored_count=scopus_result.stored_count,
            export_path=scopus_result.export_path,
        )

    kci_result = _run_kci_pipeline(
        settings=settings,
        db=db,
        max_pages=max_pages,
        page_size=page_size,
        skip_export=skip_export,
        sweep_months_override=sweep_months_override,
        resume=resume,
    )
    scopus_result = run_scopus_pipeline_yearly(
        settings=settings,
        db=db,
        extra_terms=extra_terms,
        max_pages=max_pages,
        page_size=page_size,
        force_full=force_full,
        skip_export=skip_export,
        log=_log,
    )
    return PipelineResult(
        raw_count=kci_result.raw_count + scopus_result.raw_count,
        normalized_count=kci_result.normalized_count + scopus_result.normalized_count,
        deduplicated_count=kci_result.deduplicated_count + scopus_result.deduplicated_count,
        relevant_count=kci_result.relevant_count + scopus_result.relevant_count,
        stored_count=kci_result.stored_count + scopus_result.stored_count,
        export_path=scopus_result.export_path or kci_result.export_path,
    )


def _parse_month_arg(value: str) -> tuple[str, str]:
    """Accept YYYYMM, return single-month range tuple."""
    if len(value) != 6 or not value.isdigit():
        raise argparse.ArgumentTypeError(f"--sweep-month must be YYYYMM (got {value!r})")
    return (value, value)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the KCI-first paper collection pipeline.")
    parser.add_argument(
        "--source",
        choices=["kci", "scopus", "all"],
        default="kci",
        help="Data source to collect from.",
    )
    parser.add_argument("--extra-term", action="append", default=[], help="Additional Scopus search term.")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages per query.")
    parser.add_argument("--page-size", type=int, default=None, help="Items per page request.")
    parser.add_argument("--force-full", action="store_true", help="Ignore stored incremental state (Scopus).")
    parser.add_argument("--skip-export", action="store_true", help="Skip Excel export.")
    sweep_group = parser.add_mutually_exclusive_group()
    sweep_group.add_argument(
        "--enable-sweep",
        action="store_true",
        help="Enable slow KCI monthly title='*' sweep for abstract-only matches.",
    )
    sweep_group.add_argument(
        "--disable-sweep",
        action="store_true",
        help="Disable KCI monthly title='*' sweep and use fast title/keyword search.",
    )
    parser.add_argument(
        "--sweep-month",
        type=_parse_month_arg,
        action="append",
        default=None,
        help="Limit path-2 sweep to specific YYYYMM month(s). Repeatable; implies sweep for those months.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted KCI run: skip Path 1 / sweep months already "
        "stored in a prior crashed run instead of re-collecting them.",
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    enable_sweep = None
    if args.enable_sweep:
        enable_sweep = True
    elif args.disable_sweep:
        enable_sweep = False
    run_pipeline(
        source=args.source,
        extra_terms=args.extra_term,
        max_pages=args.max_pages,
        page_size=args.page_size,
        force_full=args.force_full,
        skip_export=args.skip_export,
        enable_sweep=enable_sweep,
        sweep_months_override=args.sweep_month,
        resume=args.resume,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
