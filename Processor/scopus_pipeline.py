from __future__ import annotations

import concurrent.futures
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from Collector.scopus import ScopusAbstractDetail, ScopusCollector
from Core.deduplicator import deduplicate_papers
from Core.normalizer import NormalizedPaper, normalize_scopus_papers
from Core.query_builder import ScopusQuery, build_scopus_queries
from Exporter.export_excl import export_papers_to_excel
from Processor.summarizer import summarize_papers
from Storage.db import DatabaseManager
from config import Settings


@dataclass
class ScopusPipelineResult:
    raw_count: int
    normalized_count: int
    deduplicated_count: int
    incremental_count: int
    relevant_count: int
    stored_count: int
    export_path: Path | None


def _latest_timestamp(paper: NormalizedPaper) -> str | None:
    return paper.updated_at or paper.registered_at


def _parse_subject_area_codes(value: str | None) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None

    result: set[str] = set()
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                code = str(item.get("code", "")).strip()
                if code:
                    result.add(code)
            elif isinstance(item, str):
                code = item.strip()
                if code:
                    result.add(code)
        return result

    for token in value.replace("|", ";").split(";"):
        code = token.strip()
        if code:
            result.add(code)
    return result


def _normalize_subject_code_set(values: object) -> set[str]:
    if not isinstance(values, list):
        return set()
    result: set[str] = set()
    for item in values:
        if isinstance(item, (str, int)):
            code = str(item).strip()
            if code:
                result.add(code)
    return result


def _load_scopus_subject_code_allowlist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if isinstance(payload, list):
        return _normalize_subject_code_set(payload)
    raise ValueError(f"SCOPUS_SUBJECT_CODE_ALLOWLIST_PATH must be a JSON array: {path}")


def _load_scopus_third_filter_keywords(path: Path) -> tuple[list[str], list[str]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"SCOPUS_THIRD_FILTER_KEYWORDS_PATH must be a JSON object: {path}")

    def _normalize_keywords(values: object) -> list[str]:
        if isinstance(values, dict):
            flattened: list[str] = []
            for topic_values in values.values():
                if isinstance(topic_values, list):
                    flattened.extend(topic_values)
            values = flattened
        if not isinstance(values, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            keyword = " ".join(value.strip().split())
            if not keyword:
                continue
            key = keyword.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(keyword)
        return result

    include_any = _normalize_keywords(payload.get("include_any"))
    exclude_any = _normalize_keywords(payload.get("exclude_any"))
    return include_any, exclude_any


def _apply_third_keyword_filter(
    papers: list[NormalizedPaper],
    *,
    include_any: list[str],
    exclude_any: list[str],
) -> tuple[list[NormalizedPaper], int]:
    if not include_any and not exclude_any:
        return papers, 0

    lowered_include = [keyword.casefold() for keyword in include_any]
    lowered_exclude = [keyword.casefold() for keyword in exclude_any]
    filtered: list[NormalizedPaper] = []
    dropped = 0

    for paper in papers:
        text = " ".join(
            token
            for token in [
                paper.title or "",
                paper.abstract or "",
                " ".join(paper.keywords),
            ]
            if token
        ).casefold()

        if lowered_exclude and any(keyword in text for keyword in lowered_exclude):
            dropped += 1
            continue
        if lowered_include and not any(keyword in text for keyword in lowered_include):
            dropped += 1
            continue

        filtered.append(paper)

    return filtered, dropped


def _build_scopus_collector(
    *,
    settings: Settings,
    page_size: int | None,
) -> ScopusCollector:
    if not settings.scopus_api_key:
        raise ValueError("SCOPUS_API_KEY is not set")
    return ScopusCollector(
        api_key=settings.scopus_api_key,
        api_root=settings.scopus_api_root,
        timeout=settings.request_timeout,
        page_size=page_size or settings.scopus_page_size,
    )


def _collect_scopus_papers(
    *,
    collector: ScopusCollector,
    settings: Settings,
    queries: list[ScopusQuery],
    max_pages: int | None,
    existing_source_ids: set[str],
) -> tuple[list[NormalizedPaper], int]:
    raw_papers = collector.collect_many(
        queries,
        max_pages=max_pages or settings.scopus_max_pages,
        existing_source_ids=existing_source_ids,
        overlap_count=20,
        exclude_erratum=True,
    )
    return normalize_scopus_papers(raw_papers), len(raw_papers)


def _merge_scopus_abstract_detail(
    paper: NormalizedPaper,
    detail: ScopusAbstractDetail,
) -> None:
    def _load_subject_areas(value: str | None) -> list[dict[str, str]]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            normalized: list[dict[str, str]] = []
            for item in parsed:
                if isinstance(item, dict):
                    code = str(item.get("code", "")).strip()
                    if not code:
                        continue
                    payload: dict[str, str] = {"code": code}
                    abbrev_raw = item.get("abbrev")
                    name_raw = item.get("name")
                    abbrev = str(abbrev_raw).strip() if abbrev_raw is not None else ""
                    name = str(name_raw).strip() if name_raw is not None else ""
                    if abbrev:
                        payload["abbrev"] = abbrev
                    if name:
                        payload["name"] = name
                    normalized.append(payload)
                elif isinstance(item, str):
                    code = item.strip()
                    if code:
                        normalized.append({"code": code})
            if normalized:
                return normalized
        return [{"code": token.strip()} for token in value.replace("|", ";").split(";") if token.strip()]

    if not paper.title and detail.title:
        paper.title = detail.title
        paper.title_eng = detail.title
    if not paper.abstract and detail.abstract:
        paper.abstract = detail.abstract
        paper.abstract_eng = detail.abstract
    if not paper.url and detail.url:
        paper.url = detail.url
    if not paper.registered_at and detail.cover_date:
        paper.registered_at = detail.cover_date
    if not paper.journal_id and detail.publication_name:
        paper.journal_id = detail.publication_name
    if detail.publisher:
        paper.institution_id = detail.publisher
    if not paper.issn and detail.issn:
        paper.issn = detail.issn
    if not paper.eissn and detail.eissn:
        paper.eissn = detail.eissn

    if detail.author_names:
        seen_authors = {author.casefold() for author in paper.authors}
        for author in detail.author_names:
            key = author.casefold()
            if key in seen_authors:
                continue
            seen_authors.add(key)
            paper.authors.append(author)

    if detail.keywords:
        seen_keywords = {keyword.casefold() for keyword in paper.keywords}
        for keyword in detail.keywords:
            key = keyword.casefold()
            if key in seen_keywords:
                continue
            seen_keywords.add(key)
            paper.keywords.append(keyword)

    if detail.subject_areas:
        merged_subject_areas = _load_subject_areas(paper.subject_code)
        seen_subject_codes = {item.get("code", "") for item in merged_subject_areas if item.get("code")}
        for subject_area in detail.subject_areas:
            subject_code = subject_area.get("code", "").strip()
            if not subject_code or subject_code in seen_subject_codes:
                continue
            seen_subject_codes.add(subject_code)
            merged_subject_areas.append(subject_area)
        paper.subject_code = json.dumps(merged_subject_areas, ensure_ascii=False) if merged_subject_areas else None

    if detail.raw_payload:
        paper.raw_payload = detail.raw_payload


def _parallel_enrich_scopus_abstract(
    *,
    settings: Settings,
    papers: list[NormalizedPaper],
    max_workers: int,
) -> tuple[int, int]:
    if not papers:
        return 0, 0

    worker_count = max(1, max_workers)
    if worker_count == 1:
        succeeded = 0
        for paper in papers:
            collector = _build_scopus_collector(settings=settings, page_size=None)
            detail = collector.fetch_abstract_detail(scopus_id=paper.source_id, doi=paper.doi)
            if detail:
                _merge_scopus_abstract_detail(paper, detail)
                succeeded += 1
        return succeeded, len(papers) - succeeded

    local_state = threading.local()

    def _worker(paper: NormalizedPaper) -> tuple[NormalizedPaper, ScopusAbstractDetail | None]:
        if not hasattr(local_state, "collector"):
            local_state.collector = _build_scopus_collector(settings=settings, page_size=None)
        detail = local_state.collector.fetch_abstract_detail(scopus_id=paper.source_id, doi=paper.doi)
        return paper, detail

    succeeded = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_worker, paper) for paper in papers]
        for future in concurrent.futures.as_completed(futures):
            paper, detail = future.result()
            if not detail:
                continue
            _merge_scopus_abstract_detail(paper, detail)
            succeeded += 1
    return succeeded, len(papers) - succeeded


def run_scopus_pipeline_yearly(
    *,
    settings: Settings,
    db: DatabaseManager,
    extra_terms: list[str] | None,
    max_pages: int | None,
    page_size: int | None,
    force_full: bool,
    skip_export: bool,
    log: Callable[[str, str], None],
) -> ScopusPipelineResult:
    scopus_collector = _build_scopus_collector(settings=settings, page_size=page_size)
    query_terms = list(settings.extra_query_terms) + (extra_terms or [])
    scopus_queries = build_scopus_queries(extra_terms=query_terms)
    allowed_subject_codes = _load_scopus_subject_code_allowlist(settings.scopus_subject_code_allowlist_path)
    third_include_any, third_exclude_any = _load_scopus_third_filter_keywords(settings.scopus_third_filter_keywords_path)
    if allowed_subject_codes:
        log("3", f"Scopus subject-code filter ENABLED | allow={len(allowed_subject_codes)}")
    else:
        log("3", "Scopus subject-code filter DISABLED | allowlist empty")
    if third_include_any or third_exclude_any:
        log(
            "3",
            f"Third keyword filter ENABLED | include_any={len(third_include_any)} exclude_any={len(third_exclude_any)}",
        )
    else:
        log("3", "Third keyword filter DISABLED | keyword lists empty")
    state_scopus = None if force_full else db.get_state(settings.state_key_scopus_last_success_at)
    known_source_ids = db.all_scopus_source_ids()

    raw_count = 0
    normalized_count = 0
    deduplicated_count = 0
    incremental_count = 0
    relevant_count = 0
    stored_count = 0
    deduplicated_removed = 0
    export_rows: list[NormalizedPaper] = []

    total_queries = len(scopus_queries)
    for index, scopus_query in enumerate(scopus_queries, start=1):
        log("3", f"Scopus collect START | query_index={index}/{total_queries}")
        normalized_batch, fetched_count = _collect_scopus_papers(
            collector=scopus_collector,
            settings=settings,
            queries=[scopus_query],
            max_pages=max_pages,
            existing_source_ids=known_source_ids,
        )
        raw_count += fetched_count
        normalized_count += len(normalized_batch)
        log(
            "3",
            f"Scopus collect END | query_index={index}/{total_queries} "
            f"fetched={fetched_count} normalized={len(normalized_batch)}",
        )

        if not normalized_batch:
            continue

        log("4", f"Deduplication START | query_index={index}/{total_queries}")
        deduplicated_batch = deduplicate_papers(normalized_batch)
        deduplicated_count += len(deduplicated_batch)
        removed_count = len(normalized_batch) - len(deduplicated_batch)
        deduplicated_removed += removed_count
        log(
            "4",
            f"Deduplication END | query_index={index}/{total_queries} "
            f"removed={removed_count} remaining={len(deduplicated_batch)}",
        )

        existing_scopus_source_ids = db.existing_scopus_source_ids([paper.source_id for paper in deduplicated_batch])
        incremental_batch: list[NormalizedPaper] = []
        new_scopus_papers: list[NormalizedPaper] = []

        for paper in deduplicated_batch:
            paper_existing = paper.source_id in existing_scopus_source_ids
            if not state_scopus:
                incremental_batch.append(paper)
                if not paper_existing:
                    new_scopus_papers.append(paper)
                continue

            latest = _latest_timestamp(paper)
            if not paper_existing:
                incremental_batch.append(paper)
                new_scopus_papers.append(paper)
                continue

            if latest and latest > state_scopus:
                incremental_batch.append(paper)

        incremental_count += len(incremental_batch)
        log("5", f"Incremental filtering END | query_index={index}/{total_queries} selected={len(incremental_batch)}")

        if scopus_collector and new_scopus_papers:
            log("6", f"Abstract enrichment START | query_index={index}/{total_queries} targets={len(new_scopus_papers)}")
            succeeded, failed = _parallel_enrich_scopus_abstract(
                settings=settings,
                papers=new_scopus_papers,
                max_workers=settings.scopus_abstract_max_workers,
            )
            log(
                "6",
                f"Abstract enrichment END | query_index={index}/{total_queries} "
                f"succeeded={succeeded} failed={failed}",
            )

        if allowed_subject_codes:
            filtered_by_subject: list[NormalizedPaper] = []
            dropped_by_subject = 0
            for paper in incremental_batch:
                parsed_codes = _parse_subject_area_codes(paper.subject_code)
                if parsed_codes and parsed_codes.intersection(allowed_subject_codes):
                    filtered_by_subject.append(paper)
                    continue
                dropped_by_subject += 1
            incremental_batch = filtered_by_subject
            log(
                "6",
                f"Subject-code filter END | query_index={index}/{total_queries} "
                f"kept={len(incremental_batch)} dropped={dropped_by_subject}",
            )

        third_filtered_batch, dropped_by_third = _apply_third_keyword_filter(
            incremental_batch,
            include_any=third_include_any,
            exclude_any=third_exclude_any,
        )
        incremental_batch = third_filtered_batch
        log(
            "6",
            f"Third keyword filter END | query_index={index}/{total_queries} "
            f"kept={len(incremental_batch)} dropped={dropped_by_third}",
        )

        summarized = summarize_papers(incremental_batch)
        relevant_count += len(summarized)
        export_rows.extend(summarized)
        stored_count += db.upsert_scopus_papers(summarized)
        known_source_ids.update(paper.source_id for paper in deduplicated_batch if paper.source_id)
        log("8", f"DB upsert END | query_index={index}/{total_queries} stored={len(summarized)}")

    export_path: Path | None = None
    if not skip_export:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = settings.export_dir / f"scopus_papers_{timestamp}.xlsx"
        export_papers_to_excel(export_rows, export_path)
        log("9", f"Export END | path={export_path}")

    now_iso = datetime.now(timezone.utc).isoformat()
    db.set_state(settings.state_key_scopus_last_success_at, now_iso)
    total_db_count = db.count_scopus_papers()

    log(
        "10",
        "Final summary | "
        f"initial_read={raw_count}, "
        f"deduplicated_removed={deduplicated_removed}, "
        f"incremental_stored={stored_count}, "
        f"db_total={total_db_count}, "
        f"export={export_path}",
    )
    log("11", "Pipeline END")
    return ScopusPipelineResult(
        raw_count=raw_count,
        normalized_count=normalized_count,
        deduplicated_count=deduplicated_count,
        incremental_count=incremental_count,
        relevant_count=relevant_count,
        stored_count=stored_count,
        export_path=export_path,
    )
