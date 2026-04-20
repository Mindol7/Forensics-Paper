#!/usr/bin/env python3

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from Collector.kci import KciCollector
from Core.deduplicator import deduplicate_papers
from Core.normalizer import NormalizedPaper, normalize_kci_papers
from Core.query_builder import build_kci_queries
from Exporter.export_excl import export_papers_to_excel
from Processor.filter import filter_digital_forensics_papers
from Processor.scopus_pipeline import run_scopus_pipeline_yearly
from Processor.summarizer import summarize_papers
from Storage.db import DatabaseManager
from config import Settings, load_settings


@dataclass
class PipelineResult:
    raw_count: int
    normalized_count: int
    deduplicated_count: int
    incremental_count: int
    relevant_count: int
    stored_count: int
    export_path: Path | None


SourceType = Literal["kci", "scopus", "all"]


def _log(step: str, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{step}] {message}")


def _latest_timestamp(paper: NormalizedPaper) -> str | None:
    return paper.updated_at or paper.registered_at


def _filter_incremental(
    papers: list[NormalizedPaper],
    *,
    last_success_at: str | None,
    existing_source_ids: set[str],
) -> list[NormalizedPaper]:
    if not last_success_at:
        return list(papers)

    result: list[NormalizedPaper] = []
    for paper in papers:
        latest = _latest_timestamp(paper)
        if paper.source_id not in existing_source_ids:
            result.append(paper)
            continue
        if latest and latest > last_success_at:
            result.append(paper)
    return result


def _collect_kci_papers(
    *,
    settings: Settings,
    extra_terms: list[str] | None,
    max_pages: int | None,
    page_size: int | None,
) -> tuple[list[NormalizedPaper], int]:
    if not settings.kci_service_key:
        raise ValueError("KCI_SERVICE_KEY (or KCI_API_KEY) is not set")

    queries = build_kci_queries(extra_terms=(list(settings.extra_query_terms) + (extra_terms or [])))
    collector = KciCollector(
        service_key=settings.kci_service_key,
        base_url=settings.kci_base_url,
        timeout=settings.request_timeout,
        page_size=page_size or settings.kci_page_size,
    )
    raw_papers = collector.collect_many(queries, max_pages=max_pages or settings.kci_max_pages)
    return normalize_kci_papers(raw_papers), len(raw_papers)


def _run_kci_pipeline(
    *,
    settings: Settings,
    db: DatabaseManager,
    extra_terms: list[str] | None,
    max_pages: int | None,
    page_size: int | None,
    force_full: bool,
    skip_export: bool,
    keep_irrelevant: bool,
) -> PipelineResult:
    _log("3", "KCI collect+normalize START")
    normalized, raw_count = _collect_kci_papers(
        settings=settings,
        extra_terms=extra_terms,
        max_pages=max_pages,
        page_size=page_size,
    )
    _log("3", f"KCI collect+normalize END | fetched={raw_count} normalized={len(normalized)}")

    _log("4", "Deduplication START")
    deduplicated = deduplicate_papers(normalized)
    deduplicated_removed = len(normalized) - len(deduplicated)
    _log("4", f"Deduplication END | removed={deduplicated_removed} remaining={len(deduplicated)}")

    last_success_at = None if force_full else db.get_state(settings.state_key_last_success_at)
    existing_source_ids = db.existing_source_ids([paper.source_id for paper in deduplicated])
    _log("5", "Incremental filtering START")
    incremental = _filter_incremental(
        deduplicated,
        last_success_at=last_success_at,
        existing_source_ids=existing_source_ids,
    )
    _log("5", f"Incremental filtering END | selected={len(incremental)}")

    filtered = filter_digital_forensics_papers(
        incremental,
        keep_irrelevant=keep_irrelevant,
    )

    _log("7", "Summarization START")
    summarized = summarize_papers(filtered)
    _log("7", f"Summarization END | output={len(summarized)}")

    _log("8", f"DB upsert START | kci={len(summarized)} scopus=0")
    stored_count = db.upsert_papers(summarized)
    _log("8", f"DB upsert END | attempted={stored_count}")

    export_path: Path | None = None
    if not skip_export:
        _log("9", "Export START")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = settings.export_dir / f"kci_papers_{timestamp}.xlsx"
        export_papers_to_excel(summarized, export_path)
        _log("9", f"Export END | path={export_path}")

    db.set_state(settings.state_key_last_success_at, datetime.now(timezone.utc).isoformat())
    total_db_count = db.count_papers()

    _log(
        "10",
        "Final summary | "
        f"initial_read={raw_count}, "
        f"deduplicated_removed={deduplicated_removed}, "
        f"incremental_stored={stored_count}, "
        f"db_total={total_db_count}, "
        f"export={export_path}",
    )
    _log("11", "Pipeline END")

    return PipelineResult(
        raw_count=raw_count,
        normalized_count=len(normalized),
        deduplicated_count=len(deduplicated),
        incremental_count=len(incremental),
        relevant_count=len(summarized),
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
) -> PipelineResult:
    _log("0", "Pipeline START")

    _log("1", "Load settings START")
    if settings is None:
        settings = load_settings(validate=(source == "kci"))
    _log("1", "Load settings END")

    _log("2", "Database init START")
    db = DatabaseManager(settings.database_url)
    db.create_tables()
    _log("2", "Database init END")

    if source == "kci":
        return _run_kci_pipeline(
            settings=settings,
            db=db,
            extra_terms=extra_terms,
            max_pages=max_pages,
            page_size=page_size,
            force_full=force_full,
            skip_export=skip_export,
            keep_irrelevant=keep_irrelevant,
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
            incremental_count=scopus_result.incremental_count,
            relevant_count=scopus_result.relevant_count,
            stored_count=scopus_result.stored_count,
            export_path=scopus_result.export_path,
        )

    kci_result = _run_kci_pipeline(
        settings=settings,
        db=db,
        extra_terms=extra_terms,
        max_pages=max_pages,
        page_size=page_size,
        force_full=force_full,
        skip_export=skip_export,
        keep_irrelevant=keep_irrelevant,
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
        incremental_count=kci_result.incremental_count + scopus_result.incremental_count,
        relevant_count=kci_result.relevant_count + scopus_result.relevant_count,
        stored_count=kci_result.stored_count + scopus_result.stored_count,
        export_path=scopus_result.export_path or kci_result.export_path,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the KCI-first paper collection pipeline.")
    parser.add_argument(
        "--source",
        choices=["kci", "scopus", "all"],
        default="kci",
        help="Data source to collect from.",
    )
    parser.add_argument("--extra-term", action="append", default=[], help="Additional title search term.")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages per query.")
    parser.add_argument("--page-size", type=int, default=None, help="Items per page request.")
    parser.add_argument("--force-full", action="store_true", help="Ignore stored incremental state.")
    parser.add_argument("--skip-export", action="store_true", help="Skip Excel export.")
    parser.add_argument("--keep-irrelevant", action="store_true", help="KCI only: keep low-scoring papers too.")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    run_pipeline(
        source=args.source,
        extra_terms=args.extra_term,
        max_pages=args.max_pages,
        page_size=args.page_size,
        force_full=args.force_full,
        skip_export=args.skip_export,
        keep_irrelevant=args.keep_irrelevant,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
