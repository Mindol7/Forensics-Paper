"""KCI metadata collection and corpus workflows.

This module owns the KCI application service that used to live in the project
entry point.  The root :mod:`main` module now only composes this service with
the PDF collection and report-generation phases.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from Collector.kci import KciCollector, KciRawPaper
from Core.collect_builder import (
    KciQuery,
    KciQuerySlice,
    build_article_detail_targets,
    build_kci_query_slices,
    get_kci_monthly_ranges,
)
from Core.deduplicator import deduplicate_papers
from Core.normalizer import NormalizedPaper, normalize_kci_papers
from Exporter.report_generator import export_papers_to_excel
from Processor.filter import (
    MATCH_FIELD_ORDER,
    KeywordFilterConfig,
    classify_kci_relevance as _classify_kci_relevance,
    filter_blacklisted_kci_papers as _filter_blacklisted_kci_papers,
    filter_kci_sweep_candidates as _filter_sweep_candidates,
    filter_kci_year_range as _filter_kci_year_range,
    load_kci_blacklist as _load_blacklist,
    load_keyword_filter_configs,
)
from Storage.db import DatabaseManager
from Util.logger import log
from config import Settings, load_settings


@dataclass
class PipelineResult:
    raw_count: int
    normalized_count: int
    deduplicated_count: int
    relevant_count: int
    stored_count: int
    export_path: Path | None
    # ``source_id`` of every ``kci_papers`` row this run appended.  Filled by
    # the corpus match so a downstream orchestrator can hand exactly the new
    # papers to the PDF and analysis stages; empty for other modes.
    new_source_ids: tuple[str, ...] = ()
    # Additive full re-match only: previously selected papers that the current
    # blacklist/keyword rules would no longer select.  They are kept (their
    # PDFs and analyses exist) and reported for the operator to review.
    stale_source_ids: tuple[str, ...] = ()
    # Additive full re-match only: previously selected papers whose publication
    # year lies outside the configured window (kept as well, reported apart).
    outside_year_window_source_ids: tuple[str, ...] = ()


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


KCI_MIN_REQUEST_TIMEOUT = 30
KCI_SAFE_RATE_LIMIT_RPS = 8.0
KCI_SAFE_MAX_WORKERS = 4
KCI_SAFE_SWEEP_MAX_WORKERS = 1
# pipeline_state keys used to resume an interrupted KCI run. Each sweep month is
# processed and stored as it completes, then recorded here so `--resume` can skip
# already-finished work after a crash/disconnect mid-run.
KCI_SWEEP_PROGRESS_STATE_KEY = "kci:sweep_progress"
KCI_PATH1_DONE_STATE_KEY = "kci:path1_done"
# Cumulative full-corpus build progress (--build-corpus). The corpus is
# append-only and idempotent (skip already-stored article ids), so this is just
# an optimization to skip months already fully fetched across repeated runs.
KCI_CORPUS_PROGRESS_STATE_KEY = "kci:corpus_progress"
# Highest corpus id already classified into kci_papers. Incremental match only
# processes ids beyond this and appends; a full match resets it to the corpus max.
KCI_MATCH_MAX_ID_STATE_KEY = "kci:match_max_id"
# The most recent months are never marked "done" in the corpus build, so each
# monthly run re-checks them for late-registered papers (idempotent skip-existing
# means only genuinely new papers are fetched).
KCI_CORPUS_RECENT_RECHECK_MONTHS = 2


_log = log


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


def _build_kci_query_slices(
    collector: KciCollector,
    query: KciQuery,
    *,
    label: str,
    log_step: str,
) -> list[KciQuerySlice]:
    return build_kci_query_slices(
        collector,
        query,
        label=label,
        on_split=lambda message: _log(log_step, message),
    )


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
            slice_prefix = f"{prefix} 구간 {slice_idx}/{len(slices)}({query_slice.label})"
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
            slice_prefix = f"{prefix} 구간 {slice_idx}/{len(slices)}({query_slice.label})"
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
    log_label: str = "",
    log_every: int = 500,
    log_interval_seconds: float = 15.0,
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
    id_to_papers = build_article_detail_targets(papers)
    article_ids = sorted(id_to_papers)
    if not article_ids:
        return 0, 0

    details: dict[str, KciRawPaper | None] = {}
    failed = 0
    total = len(article_ids)
    done = 0
    last_progress_at = time.monotonic()

    def _note_progress() -> None:
        nonlocal last_progress_at
        # The detail-fetch loop is otherwise silent for minutes (thousands of
        # serial calls), which looks frozen. Emit a heartbeat so corpus builds
        # show steady progress.
        now = time.monotonic()
        count_due = log_every > 0 and done % log_every == 0
        time_due = (
            log_interval_seconds > 0
            and now - last_progress_at >= log_interval_seconds
        )
        if log_label and (count_due or time_due or done == total):
            _log("3c", f"{log_label} 상세 정보(키워드·초록) 보강 {done:,}/{total:,}건 ({done / total:.0%})")
            last_progress_at = now

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
            done += 1
            _note_progress()
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_fetch, article_id) for article_id in article_ids]
            for future in as_completed(futures):
                key, detail = future.result()
                details[key] = detail
                if detail is None:
                    failed += 1
                done += 1
                _note_progress()

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


def _load_done_months(db: DatabaseManager, key: str = KCI_SWEEP_PROGRESS_STATE_KEY) -> set[str]:
    raw = db.get_state(key)
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    return {str(item) for item in data} if isinstance(data, list) else set()


def _save_done_months(
    db: DatabaseManager, done: set[str], key: str = KCI_SWEEP_PROGRESS_STATE_KEY
) -> None:
    db.set_state(key, json.dumps(sorted(done)))


def load_corpus_done_months(db: DatabaseManager) -> set[str]:
    """Return the ``YYYYMM`` months the cumulative corpus build has completed.

    The two most recent months are never marked done (they are re-checked for
    late registrations), so the largest value here is the last month that no
    longer needs a fetch.  The end-to-end pipeline derives its target months
    from this checkpoint instead of the run date alone.
    """

    return _load_done_months(db, key=KCI_CORPUS_PROGRESS_STATE_KEY)


def _clear_kci_checkpoint(db: DatabaseManager) -> None:
    db.set_state(KCI_SWEEP_PROGRESS_STATE_KEY, "[]")
    db.set_state(KCI_PATH1_DONE_STATE_KEY, "")


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

    # Analyzer/intelligencer is intentionally not implemented yet.  Preserve
    # the current metadata columns without generating a synthetic summary.
    summarized = relevant
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
            "SKIPPED. Everything collected was already stored; re-run the metadata "
            "stage (`python3 main.py -all`) to retry only the unfinished/failed units:",
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


# ---------- Full-corpus mode: collect everything, then match offline ----------


def _collect_all_month_papers(
    collector: KciCollector,
    year_month: tuple[str, str],
    *,
    max_pages: int | None,
    idx: int,
    total: int,
) -> KciCollectOutcome:
    """Fetch EVERY paper for one month (no keyword filter) for corpus building.

    Like `_sweep_month` but keeps all papers instead of only keyword candidates —
    the corpus must hold the full national output so it can be re-matched offline.
    A failed slice is skipped (graceful) so one bad slice doesn't lose the month.
    """
    date_from, date_to = year_month
    prefix = f"  논문 정보 수집 [{idx}/{total}번째 달] {date_from[:4]}-{date_from[4:6]}"
    _log("C1", f"{prefix} 시작")
    papers: list[KciRawPaper] = []
    failures: list[str] = []
    query = KciQuery(title="*", date_from=date_from, date_to=date_to)
    try:
        slices = _build_kci_query_slices(collector, query, label=prefix, log_step="C1")
    except Exception as exc:
        _log("C1", f"{prefix} 오류 — 건수 확인 실패: {exc} (이 달은 건너뛰고 다음 실행에서 다시 시도)")
        return KciCollectOutcome(papers=papers, failures=[f"{prefix} (probe)"])

    if len(slices) > 1:
        _log("C1", f"{prefix} 건수가 많아 {len(slices)}개 구간으로 나눠 받습니다 (총 {sum(s.total_count for s in slices):,}건)")
    for slice_idx, query_slice in enumerate(slices, start=1):
        slice_prefix = prefix
        if len(slices) > 1:
            slice_prefix = f"{prefix} 구간 {slice_idx}/{len(slices)}({query_slice.label})"
        try:
            for page_items, _total_count in collector.iter_pages(
                query_slice.query,
                max_pages=max_pages,
                on_page=lambda msg, _p=slice_prefix: _log("C1", f"{_p} {msg}"),
            ):
                papers.extend(page_items)
        except Exception as exc:
            _log("C1", f"{slice_prefix} 오류 — {len(papers):,}건 받은 뒤 중단: {exc} (이 구간은 다음 실행에서 다시 시도)")
            failures.append(slice_prefix)
    _log("C1", f"{prefix} 완료 — 받은 논문 정보 {len(papers):,}건 · 실패 구간 {len(failures)}개")
    return KciCollectOutcome(papers=papers, failures=failures)


def _build_kci_corpus(
    *,
    settings: Settings,
    db: DatabaseManager,
    max_pages: int | None,
    page_size: int | None,
    sweep_months_override: list[tuple[str, str]] | None,
) -> PipelineResult:
    """Phase 1 — fetch the full 5-year corpus (with author keywords), no matching.

    Per month: enumerate every paper via `title=*` sweep, then articleDetail-enrich
    the ones not already stored, and upsert into `kci_corpus`. Cumulative and
    idempotent (skips already-stored article ids and already-finished months), so
    it is meant to be re-run repeatedly until the whole span is collected.
    """
    collector = _build_kci_collector(settings, page_size=page_size)
    effective_max_pages = max_pages or settings.kci_max_pages
    enrich_workers = _effective_worker_count(max(1, settings.kci_max_workers), cap=KCI_SAFE_MAX_WORKERS)

    monthly_ranges = sweep_months_override or get_kci_monthly_ranges(
        span_years=settings.kci_recent_years,
        start_year=settings.kci_start_year,
        end_year=settings.kci_end_year,
    )
    date_from, date_to = monthly_ranges[0][0], monthly_ranges[-1][1]

    done_months = _load_done_months(db, key=KCI_CORPUS_PROGRESS_STATE_KEY)
    # The most recent months keep getting new papers registered (KCI indexing
    # lag), so always re-check them even if a prior run marked them done.
    recent_months = {ym[0] for ym in monthly_ranges[-KCI_CORPUS_RECENT_RECHECK_MONTHS:]}
    pending = [
        (idx, ym)
        for idx, ym in enumerate(monthly_ranges, start=1)
        if ym[0] not in done_months or ym[0] in recent_months
    ]
    _log(
        "C0",
        f"KCI 논문 정보 수집 시작 — 대상 기간 {date_from}~{date_to}({len(monthly_ranges)}개월) 중 "
        f"이번에 받을 달 {len(pending)}개 · 이미 받은 달 {len(done_months)}개 · "
        f"지연 등록 재확인 {', '.join(sorted(recent_months)) or '없음'} · "
        f"현재 저장된 논문 정보 {db.count_corpus():,}건",
    )

    fetched_total = 0
    stored_total = 0
    failures: list[str] = []
    for idx, ym in pending:
        outcome = _collect_all_month_papers(
            collector, ym, max_pages=effective_max_pages, idx=idx, total=len(monthly_ranges)
        )
        failures.extend(outcome.failures)
        fetched_total += len(outcome.papers)

        if outcome.papers:
            normalized = normalize_kci_papers(outcome.papers)
            unique: dict[str, NormalizedPaper] = {}
            for paper in normalized:
                if paper.source_id and paper.source_id not in unique:
                    unique[paper.source_id] = paper
            existing = db.existing_corpus_ids(list(unique))
            new_papers = [paper for sid, paper in unique.items() if sid not in existing]
            detail_ok, detail_failed = _enrich_kci_relevant_keywords(
                collector, new_papers, max_workers=enrich_workers, log_label=f"  [{ym[0][:4]}-{ym[0][4:6]}]"
            )
            for paper in new_papers:
                paper.raw_payload.pop("_real_keywords", None)
            stored = db.upsert_corpus_papers(new_papers)
            stored_total += stored
            _log(
                "C2",
                f"{ym[0][:4]}-{ym[0][4:6]} 저장 완료 — 받은 {len(outcome.papers):,}건 중 새 논문 정보 {len(new_papers):,}건 저장 "
                f"(이미 있던 {len(unique) - len(new_papers):,}건 제외, 상세 보강 성공 {detail_ok:,}·실패 {detail_failed:,}) · "
                f"누적 {db.count_corpus():,}건",
            )

        # Mark a month done only when fully fetched (no slice failures, no page
        # cap) AND it is not a recent month (those are re-checked every run for
        # late registrations, so never permanently skipped).
        if not outcome.failures and effective_max_pages is None and ym[0] not in recent_months:
            done_months.add(ym[0])
            _save_done_months(db, done_months, key=KCI_CORPUS_PROGRESS_STATE_KEY)

    corpus_total = db.count_corpus()
    months_done = len(done_months)
    _log(
        "C9",
        f"KCI 논문 정보 수집 완료 — 이번에 받은 {fetched_total:,}건 중 새로 저장 {stored_total:,}건 · "
        f"누적 {corpus_total:,}건 · 수집 완료된 달 {months_done}/{len(monthly_ranges)} · 실패 구간 {len(failures)}개",
    )
    if months_done < len(monthly_ranges):
        _log(
            "C9",
            f"아직 받지 못한 달이 {len(monthly_ranges) - months_done}개 남았습니다 — "
            "`python3 main.py -all`을 다시 실행하면 남은 달부터 이어서 받습니다.",
        )
    if failures:
        _log("C9", f"WARNING: {len(failures)} unit(s) failed this run (those months will retry next run):")
        for label in failures:
            _log("C9", f"  FAILED: {label.strip()}")

    return PipelineResult(
        raw_count=fetched_total,
        normalized_count=fetched_total,
        deduplicated_count=corpus_total,
        relevant_count=0,
        stored_count=stored_total,
        export_path=None,
    )


def _match_corpus_segment(
    database_url: str,
    keyword_paths: list[Path],
    blacklist_path: Path,
    start_year: int,
    end_year: int,
    id_lo: int,
    id_hi: int,
) -> tuple[list[NormalizedPaper], dict[str, int], int, int, int, list[str]]:
    """Worker (runs in its own process): classify one corpus id-segment.

    Opens its own SQLite connection (concurrent readers are fine), streams its
    id range, applies year guard -> blacklist -> keyword match, and returns only
    the relevant papers (small) + breakdown/counters. Reading only — the main
    process does all writing after every worker finishes, so there is no
    read/write lock contention.
    """
    db = DatabaseManager(database_url)
    keyword_config = load_keyword_filter_configs(keyword_paths)
    blacklist_terms = _load_blacklist(blacklist_path)
    breakdown: dict[str, int] = {field_name: 0 for field_name in MATCH_FIELD_ORDER}
    breakdown["journal"] = 0
    relevant: list[NormalizedPaper] = []
    processed = 0
    dropped_year = 0
    dropped_blacklist = 0
    dropped_year_ids: list[str] = []
    buffer: list[NormalizedPaper] = []

    def _flush(papers: list[NormalizedPaper]) -> None:
        nonlocal dropped_year, dropped_blacklist
        if not papers:
            return
        in_range = _filter_kci_year_range(papers, start_year=start_year, end_year=end_year)
        dropped_year += len(papers) - len(in_range)
        if len(in_range) != len(papers):
            kept_ids = {paper.source_id for paper in in_range}
            dropped_year_ids.extend(paper.source_id for paper in papers if paper.source_id not in kept_ids)
        kept = _filter_blacklisted_kci_papers(in_range, blacklist_terms)
        dropped_blacklist += len(in_range) - len(kept)
        relevant.extend(_classify_kci_relevance(kept, keyword_config, breakdown))

    for paper in db.iter_corpus_papers(chunk_size=2000, id_min=id_lo, id_max=id_hi):
        processed += 1
        buffer.append(paper)
        if len(buffer) >= 2000:
            _flush(buffer)
            buffer = []
    _flush(buffer)
    return relevant, breakdown, processed, dropped_year, dropped_blacklist, dropped_year_ids


EXCLUDED_PAPERS_PATH = Path(__file__).resolve().parents[1] / "Processor" / "refs" / "excluded_papers.json"


def _load_excluded_source_ids(path: Path | None = None) -> frozenset[str]:
    """``source_id`` of papers removed by review (see ``Collector/paper_removal.py``)."""

    path = path or EXCLUDED_PAPERS_PATH
    if not path.is_file():
        return frozenset()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return frozenset(entry["source_id"] for entry in payload.get("papers", []) if entry.get("source_id"))


def _match_kci_corpus(
    *,
    settings: Settings,
    db: DatabaseManager,
    skip_export: bool,
    incremental: bool = False,
    additive: bool = False,
) -> PipelineResult:
    """Phase 2 — classify the stored corpus offline (no network), in parallel.

    Full mode (default): clear `kci_papers` and re-classify the entire corpus —
    use after changing the keyword set/blacklist.
    Incremental mode (`incremental=True`, the monthly path): classify only corpus ids beyond the
    `kci:match_max_id` checkpoint and APPEND to `kci_papers` — the fast monthly
    path (keyword-set changes are NOT applied retroactively).
    Additive mode (`additive=True`): re-classify the entire corpus with the
    current rules but never remove a previously selected paper — downstream
    artifacts (PDFs, analyses, profiles) already depend on those rows.  Papers
    the new rules would drop are reported in ``stale_source_ids`` instead.
    All modes regenerate the accumulated Excel (`kci_papers_accumulated.xlsx`)
    from the full `kci_papers` table.
    """
    if incremental and additive:
        raise ValueError("incremental and additive match modes are mutually exclusive")
    keyword_config = load_keyword_filter_configs(
        [
            settings.kci_keyword_filter_keywords_path,
            settings.kci_keyword_filter_english_keywords_path,
        ]
    )
    blacklist_terms = _load_blacklist(settings.kci_blacklist_path)
    if not keyword_config.rules:
        raise ValueError(f"No keyword rules in {settings.kci_keyword_filter_keywords_path}")
    corpus_total = db.count_corpus()
    _log(
        "M0",
        f"블랙리스트·키워드 필터링 시작 — 저장된 논문 정보 {corpus_total:,}건 · 키워드 규칙 {len(keyword_config.rules)}개 · "
        f"제외어 {len(keyword_config.exclude_any)}개 · 블랙리스트 {len(blacklist_terms)}개 · "
        f"대상 연도 {settings.kci_start_year}~{settings.kci_end_year}",
    )
    if corpus_total == 0:
        raise ValueError("kci_corpus is empty — collect metadata first (`python3 main.py -all`)")

    corpus_low, corpus_high = db.corpus_id_bounds()
    # Rows appended by this run are the ones after the current maximum id;
    # remember it so the hygiene pass and the new-paper report stay scoped.
    papers_before_max_id = db.max_paper_id()
    existing_source_ids = set(db.kci_paper_source_ids()) if additive else set()
    if incremental:
        # Only classify corpus rows added since the last match; append (don't
        # clear). match_max_id marks the last classified corpus id.
        last_matched = int(db.get_state(KCI_MATCH_MAX_ID_STATE_KEY) or 0)
        low, high = last_matched + 1, corpus_high
        _log("M0", f"새로 받은 논문 정보만 필터링합니다 (저장 번호 {last_matched + 1:,}~{corpus_high:,}, 기존 선별 논문에 추가)")
    elif additive:
        low, high = corpus_low, corpus_high
        _log(
            "M0",
            f"전체 논문 정보에 규칙을 다시 적용합니다 — 이미 선별된 {len(existing_source_ids):,}편은 그대로 유지, "
            f"저장 번호 {low:,}~{high:,}",
        )
    else:
        # Full re-match: kci_papers is fully derived from the corpus, so clear it
        # first (a tighter keyword set otherwise leaves old false positives).
        cleared = db.clear_kci_papers()
        low, high = corpus_low, corpus_high
        _log("M0", f"선별 논문 {cleared:,}편을 비우고 전체 논문 정보에서 다시 선별합니다 (저장 번호 {low:,}~{high:,})")

    workers = max(1, min(os.cpu_count() or 1, 8))
    segments: list[tuple[int, int]] = []
    if low <= high:
        span = high - low + 1
        segment_size = max(1, (span + workers - 1) // workers)
        start = low
        while start <= high:
            segments.append((start, min(start + segment_size - 1, high)))
            start += segment_size
    _log("M0", f"필터링을 {len(segments)}개 구간으로 나눠 {workers}개 작업으로 동시에 처리합니다")

    keyword_paths = [
        settings.kci_keyword_filter_keywords_path,
        settings.kci_keyword_filter_english_keywords_path,
    ]
    breakdown: dict[str, int] = {f: 0 for f in MATCH_FIELD_ORDER}
    breakdown["journal"] = 0
    all_relevant: list[NormalizedPaper] = []
    processed = 0
    dropped_year = 0
    dropped_blacklist = 0
    dropped_year_ids: set[str] = set()

    # Workers only READ (own SQLite connection); the main process writes only
    # after every worker has finished, so there is no read/write lock contention.
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _match_corpus_segment,
                settings.database_url,
                keyword_paths,
                settings.kci_blacklist_path,
                settings.kci_start_year,
                settings.kci_end_year,
                seg_lo,
                seg_hi,
            )
            for seg_lo, seg_hi in segments
        ]
        completed = 0
        for future in as_completed(futures):
            (
                seg_relevant, seg_breakdown, seg_processed, seg_dropped_year, seg_dropped_bl,
                seg_dropped_year_ids,
            ) = future.result()
            all_relevant.extend(seg_relevant)
            dropped_year_ids.update(seg_dropped_year_ids)
            for field_name, value in seg_breakdown.items():
                breakdown[field_name] = breakdown.get(field_name, 0) + value
            processed += seg_processed
            dropped_year += seg_dropped_year
            dropped_blacklist += seg_dropped_bl
            completed += 1
            _log(
                "M1",
                f"필터링 진행 {completed}/{len(segments)}구간 ({completed / len(segments):.0%}) — "
                f"검사한 논문 정보 {processed:,}건 · 지금까지 고른 관련 논문 {len(all_relevant):,}편",
            )

    # Papers a person reviewed out of the collection are never selected again.
    excluded_ids = _load_excluded_source_ids()
    excluded_hits = [paper.source_id for paper in all_relevant if paper.source_id in excluded_ids]
    if excluded_hits:
        all_relevant = [paper for paper in all_relevant if paper.source_id not in excluded_ids]
        _log("M1", f"검토로 제외된 논문 {len(excluded_hits)}편은 다시 선별하지 않습니다 (Processor/refs/excluded_papers.json)")

    # Intelligence/summary enrichment belongs to the future Analyzer stage.
    # Workers finish in any order; keep the stored order deterministic.
    summarized = sorted(all_relevant, key=lambda paper: paper.source_id)
    if additive:
        # Rows selected by earlier runs carry enrichment (summary, keywords,
        # raw payload) that the corpus copy lacks: refresh only their match
        # evidence and insert the genuinely new papers.
        kept_rows = [paper for paper in summarized if paper.source_id in existing_source_ids]
        new_rows = [paper for paper in summarized if paper.source_id not in existing_source_ids]
        stored_total = db.upsert_papers(new_rows) + db.update_paper_match_evidence(kept_rows)
    else:
        stored_total = db.upsert_papers(summarized)
    export_rows: list[NormalizedPaper] = summarized

    # The year window is a guard for what this run adds.  In incremental mode
    # it must not reach rows that earlier runs (or the operator) accepted and
    # that downstream artifacts already depend on.
    cleaned_db_count = db.delete_kci_papers_outside_year_range(
        start_year=settings.kci_start_year,
        end_year=settings.kci_end_year,
        min_id=papers_before_max_id + 1 if (incremental or additive) else None,
    )
    cleaned_invalid_count = db.delete_kci_papers_with_invalid_match_evidence()
    new_source_ids = tuple(db.kci_paper_source_ids(min_id=papers_before_max_id + 1))
    stale_source_ids: tuple[str, ...] = ()
    outside_year_window: tuple[str, ...] = ()
    if additive:
        relevant_ids = {paper.source_id for paper in summarized}
        outside_year_window = tuple(sorted(existing_source_ids & dropped_year_ids))
        stale_source_ids = tuple(sorted(existing_source_ids - relevant_ids - set(outside_year_window)))
        if outside_year_window:
            _log(
                "M2",
                f"{len(outside_year_window)} previously stored papers lie outside "
                f"{settings.kci_start_year}-{settings.kci_end_year} (kept): "
                f"{', '.join(outside_year_window[:10])}",
            )
        if stale_source_ids:
            _log(
                "M2",
                f"Rules no longer select {len(stale_source_ids)} previously stored papers "
                f"(kept): {', '.join(stale_source_ids[:10])}",
            )
    breakdown_str = " ".join(f"{f}={c}" for f, c in breakdown.items())
    _log(
        "M2",
        f"필터링 결과 — 관련 논문 {len(export_rows):,}편 (일치 위치: {breakdown_str}) · "
        f"연도 범위 밖 제외 {dropped_year:,}건 · 블랙리스트 제외 {dropped_blacklist:,}건",
    )

    # Advance the incremental baseline to the corpus max so the next
    # --incremental run only processes rows added after this one (a full
    # re-match resets it here too).
    db.set_state(KCI_MATCH_MAX_ID_STATE_KEY, str(corpus_high))

    # Regenerate the accumulated Excel from the full kci_papers table (which
    # accumulates via incremental append) so it always reflects everything to
    # date, plus a timestamped snapshot.
    total_relevant = db.count_papers()
    export_path: Path | None = None
    if not skip_export:
        accumulated = list(db.iter_kci_papers())
        accumulated_path = settings.export_dir / "kci_papers_accumulated.xlsx"
        export_papers_to_excel(accumulated, accumulated_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = settings.export_dir / f"kci_papers_{timestamp}.xlsx"
        export_papers_to_excel(accumulated, export_path)
        _log(
            "M3",
            f"Export END | accumulated={accumulated_path} snapshot={export_path} "
            f"rows={len(accumulated)}",
        )

    db.set_state(settings.state_key_last_success_at, datetime.now(timezone.utc).isoformat())
    _log(
        "M9",
        f"필터링 완료({'새 논문만' if incremental else '전체 재적용·기존 유지' if additive else '전체 다시 선별'}) — "
        f"검사 {processed:,}건 · 이번에 새로 선별된 논문 {len(new_source_ids):,}편 · 선별 논문 누계 {total_relevant:,}편"
        + (f" · Excel: {export_path}" if export_path else ""),
    )
    return PipelineResult(
        raw_count=processed,
        normalized_count=processed,
        deduplicated_count=processed,
        relevant_count=len(export_rows),
        stored_count=stored_total,
        export_path=export_path,
        new_source_ids=new_source_ids,
        stale_source_ids=stale_source_ids,
        outside_year_window_source_ids=outside_year_window,
    )


def _reenrich_kci_corpus(*, settings: Settings, db: DatabaseManager) -> PipelineResult:
    """Backfill keywords for corpus rows stored without them.

    A network outage (or any articleDetail failure) during `--build-corpus`
    stores those papers keyword-less, and the month is marked done so a normal
    re-run skips them. This re-fetches every keyword-less corpus row's detail and
    fills the recoverable ones; genuinely keyword-less articles stay empty. Safe
    to re-run; run it AFTER the build finishes (not concurrently — SQLite write
    contention).
    """
    collector = _build_kci_collector(settings, page_size=None)
    workers = _effective_worker_count(max(1, settings.kci_max_workers), cap=KCI_SAFE_MAX_WORKERS)
    ids = db.keywordless_corpus_ids()
    total = len(ids)
    _log("R0", f"Re-enrich corpus START | keyword-less rows={total} workers={workers}")
    if total == 0:
        _log("R9", "Re-enrich corpus END | nothing to do")
        return PipelineResult(0, 0, 0, 0, 0, None)

    def _fetch(article_id: str) -> tuple[str, KciRawPaper | None]:
        try:
            return article_id, collector.fetch_article_detail(article_id)
        except Exception as exc:
            _log("R", f"  [detail] {article_id} ERROR: {exc}")
            return article_id, None

    recovered = 0
    still_empty = 0
    failed = 0
    done = 0
    pending: dict[str, list[str]] = {}

    def _flush() -> None:
        nonlocal pending
        if pending:
            db.set_corpus_keywords(pending)
            pending = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fetch, article_id) for article_id in ids]
        for future in as_completed(futures):
            article_id, detail = future.result()
            done += 1
            if detail is None:
                failed += 1
            elif detail.keywords:
                pending[article_id] = list(detail.keywords)
                recovered += 1
            else:
                still_empty += 1
            if len(pending) >= 1000:
                _flush()
            if done % 500 == 0:
                _log(
                    "R1",
                    f"re-enrich progress | {done}/{total} recovered={recovered} "
                    f"still_empty={still_empty} failed={failed}",
                )
    _flush()

    _log(
        "R9",
        f"Re-enrich corpus END | checked={total} recovered={recovered} "
        f"still_empty={still_empty} failed={failed} corpus_total={db.count_corpus()}",
    )
    return PipelineResult(
        raw_count=total,
        normalized_count=total,
        deduplicated_count=db.count_corpus(),
        relevant_count=recovered,
        stored_count=recovered,
        export_path=None,
    )


def run_kci_metadata_pipeline(
    *,
    settings: Settings | None = None,
    db: DatabaseManager | None = None,
    max_pages: int | None = None,
    page_size: int | None = None,
    skip_export: bool = False,
    enable_sweep: bool | None = None,
    sweep_months_override: list[tuple[str, str]] | None = None,
    resume: bool = False,
    build_corpus: bool = False,
    match_corpus: bool = False,
    match_incremental: bool = False,
    match_additive: bool = False,
    reenrich_corpus: bool = False,
) -> PipelineResult:
    """Run one KCI metadata workflow while preserving the existing modes.

    The default mode is the five-year initial collection configured by
    :class:`config.Settings`.  Corpus build/match flags remain explicit so the
    monthly scheduler can compose an idempotent build followed by an
    incremental offline match.
    """

    _log("0", "KCI metadata pipeline START")
    if settings is None:
        settings = load_settings(validate=True)
    if enable_sweep is not None:
        settings = replace(settings, kci_enable_sweep=enable_sweep)

    if db is None:
        _log("2", "Database init START")
        db = DatabaseManager(settings.database_url)
        db.create_tables()
        _log("2", "Database init END")

    if build_corpus or match_corpus or reenrich_corpus:
        result = PipelineResult(0, 0, 0, 0, 0, None)
        if build_corpus:
            result = _build_kci_corpus(
                settings=settings,
                db=db,
                max_pages=max_pages,
                page_size=page_size,
                sweep_months_override=sweep_months_override,
            )
        if reenrich_corpus:
            result = _reenrich_kci_corpus(settings=settings, db=db)
        if match_corpus:
            result = _match_kci_corpus(
                settings=settings,
                db=db,
                skip_export=skip_export,
                incremental=match_incremental,
                additive=match_additive,
            )
        _log("11", "KCI metadata pipeline END")
        return result

    return _run_kci_pipeline(
        settings=settings,
        db=db,
        max_pages=max_pages,
        page_size=page_size,
        skip_export=skip_export,
        sweep_months_override=sweep_months_override,
        resume=resume,
    )


__all__ = [
    "KciBatchResult",
    "KciCollectOutcome",
    "KciQuerySlice",
    "PipelineResult",
    "run_kci_metadata_pipeline",
    "load_corpus_done_months",
]
