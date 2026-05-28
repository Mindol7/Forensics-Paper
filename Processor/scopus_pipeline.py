from __future__ import annotations

import concurrent.futures
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from Collector.scopus import ScopusAbstractDetail, ScopusCollector
from Core.deduplicator import deduplicate_papers
from Core.normalizer import NormalizedPaper, normalize_scopus_papers
from Core.query_builder import ScopusKeywordSpec, ScopusQuery, build_scopus_queries
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


def _load_search_keywords(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Keyword filter file must be a JSON object: {path}")

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

    return _normalize_keywords(payload.get("include_any"))


def _dedupe_keyword_terms(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
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
    return tuple(result)


def _load_scopus_search_keywords(path: Path) -> ScopusKeywordSpec:
    if not path.exists():
        return ScopusKeywordSpec(mode_terms={"direct": ()}, anchor_modes={}, exclude_terms=())
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Scopus keyword file must be a JSON object: {path}")

    search_config = payload.get("search_config", {})
    anchor_modes_raw = search_config.get("anchor_modes", {}) if isinstance(search_config, dict) else {}
    if not isinstance(anchor_modes_raw, dict):
        raise ValueError(f"search_config.anchor_modes must be a JSON object: {path}")

    anchor_modes: dict[str, tuple[str, ...]] = {}
    for mode, anchors in anchor_modes_raw.items():
        if not isinstance(mode, str):
            continue
        normalized_anchors = _dedupe_keyword_terms(anchors)
        if normalized_anchors:
            anchor_modes[mode] = normalized_anchors

    collected_terms: dict[str, list[str]] = {"direct": []}
    for mode in anchor_modes:
        collected_terms[mode] = []
    searchable_modes = set(collected_terms)

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in searchable_modes and isinstance(value, list):
                    collected_terms[key].extend(value_item for value_item in value if isinstance(value_item, str))
                    continue
                _walk(value)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)

    reserved_top_level = {"search_config", "exclude"}
    for top_key, top_value in payload.items():
        if top_key in reserved_top_level:
            continue
        _walk(top_value)

    mode_terms = {
        mode: _dedupe_keyword_terms(values)
        for mode, values in collected_terms.items()
    }
    mode_terms.setdefault("direct", ())
    exclude_terms = _dedupe_keyword_terms(payload.get("exclude"))

    return ScopusKeywordSpec(
        mode_terms=mode_terms,
        anchor_modes=anchor_modes,
        exclude_terms=exclude_terms,
    )


def _append_direct_keyword_terms(
    keyword_spec: ScopusKeywordSpec,
    extra_terms: list[str] | None,
) -> ScopusKeywordSpec:
    if not extra_terms:
        return keyword_spec
    direct_terms = list(keyword_spec.mode_terms.get("direct", ()))
    direct_terms.extend(extra_terms)
    mode_terms = dict(keyword_spec.mode_terms)
    mode_terms["direct"] = _dedupe_keyword_terms(direct_terms)
    return ScopusKeywordSpec(
        mode_terms=mode_terms,
        anchor_modes=keyword_spec.anchor_modes,
        exclude_terms=keyword_spec.exclude_terms,
    )


def _count_scopus_positive_keyword_terms(keyword_spec: ScopusKeywordSpec) -> int:
    return sum(len(terms) for terms in keyword_spec.mode_terms.values())


def _format_source_title(value: str) -> str:
    escaped = value.replace('"', '\\"')
    return f'EXACTSRCTITLE("{escaped}")'


def _format_issn_query(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("-", "").strip()
    if not normalized:
        return None
    return f"ISSN({normalized})"


def _load_scopus_source_clauses(path: Path, *, chunk_size: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file) or {}
    journals = payload.get("journals", []) if isinstance(payload, dict) else []
    if not isinstance(journals, list):
        raise ValueError(f"Scopus journal filter file must contain a journals list: {path}")

    source_terms: list[str] = []
    seen: set[str] = set()
    for journal in journals:
        if not isinstance(journal, dict) or not journal.get("enabled", True):
            continue
        name = journal.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        bits = [_format_source_title(name.strip())]
        for key in ("issn", "e_issn"):
            issn_query = _format_issn_query(journal.get(key))
            if issn_query:
                bits.append(issn_query)
        source_expr = f"({' OR '.join(bits)})"
        dedupe_key = source_expr.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        source_terms.append(source_expr)

    result: list[str] = []
    effective_chunk_size = max(1, chunk_size)
    for index in range(0, len(source_terms), effective_chunk_size):
        chunk = source_terms[index : index + effective_chunk_size]
        result.append(f"({' OR '.join(chunk)})")
    return result


def _all_positive_keyword_terms(keyword_spec: ScopusKeywordSpec) -> tuple[str, ...]:
    terms: list[str] = []
    for mode_terms in keyword_spec.mode_terms.values():
        terms.extend(mode_terms)
    return _dedupe_keyword_terms(terms)


def _term_to_regex(term: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in term.casefold().split()]
    if not parts:
        return re.compile(r"a\Ab")
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(parts) + r"(?!\w)")


def _annotate_matched_keywords(papers: list[NormalizedPaper], keyword_spec: ScopusKeywordSpec) -> None:
    compiled_terms = [(term, _term_to_regex(term)) for term in _all_positive_keyword_terms(keyword_spec)]
    for paper in papers:
        text = " ".join(
            value
            for value in [
                paper.title or "",
                paper.abstract or "",
                " ".join(paper.keywords),
            ]
            if value
        ).casefold()
        matched: list[str] = []
        seen: set[str] = set()
        for term, pattern in compiled_terms:
            if not pattern.search(text):
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            matched.append(term)
        paper.matched_keyword = matched


def load_keyword_filter_terms(path: Path) -> list[str]:
    """Load include-any keyword terms used to build search queries."""
    return _load_search_keywords(path)


def _build_scopus_collector(
    *,
    settings: Settings,
    page_size: int | None,
) -> ScopusCollector:
    if not settings.scopus_api_keys:
        raise ValueError("SCOPUS_API_KEY or SCOPUS_FALLBACK_API_KEYS is not set")
    return ScopusCollector(
        api_key=settings.scopus_api_key,
        api_keys=settings.scopus_api_keys,
        api_root=settings.scopus_api_root,
        timeout=settings.request_timeout,
        page_size=page_size or settings.scopus_page_size,
        requests_per_second=settings.scopus_requests_per_second,
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


def _scopus_abstract_identifiers(paper: NormalizedPaper) -> tuple[str | None, str | None]:
    source_id = (paper.source_id or "").strip()
    scopus_id = source_id if source_id.isdigit() else None
    doi = (paper.doi or "").strip() or None
    return scopus_id, doi


def _parallel_enrich_scopus_abstract(
    *,
    settings: Settings,
    papers: list[NormalizedPaper],
    max_workers: int,
) -> tuple[int, int]:
    abstract_targets = [
        paper
        for paper in papers
        if any(_scopus_abstract_identifiers(paper))
    ]
    skipped = len(papers) - len(abstract_targets)
    if not abstract_targets:
        return 0, skipped

    worker_count = max(1, max_workers)
    if worker_count == 1:
        succeeded = 0
        for paper in abstract_targets:
            collector = _build_scopus_collector(settings=settings, page_size=None)
            scopus_id, doi = _scopus_abstract_identifiers(paper)
            detail = collector.fetch_abstract_detail(scopus_id=scopus_id, doi=doi)
            if detail:
                _merge_scopus_abstract_detail(paper, detail)
                succeeded += 1
        return succeeded, len(papers) - succeeded

    local_state = threading.local()

    def _worker(paper: NormalizedPaper) -> tuple[NormalizedPaper, ScopusAbstractDetail | None]:
        if not hasattr(local_state, "collector"):
            local_state.collector = _build_scopus_collector(settings=settings, page_size=None)
        scopus_id, doi = _scopus_abstract_identifiers(paper)
        detail = local_state.collector.fetch_abstract_detail(scopus_id=scopus_id, doi=doi)
        return paper, detail

    succeeded = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_worker, paper) for paper in abstract_targets]
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
    skip_abstract: bool,
    log: Callable[[str, str], None],
) -> ScopusPipelineResult:
    scopus_collector = _build_scopus_collector(settings=settings, page_size=page_size)
    keyword_spec = _load_scopus_search_keywords(settings.scopus_keyword_filter_keywords_path)
    keyword_spec = _append_direct_keyword_terms(keyword_spec, extra_terms)
    source_clauses = _load_scopus_source_clauses(
        settings.scopus_journal_filter_path,
        chunk_size=settings.scopus_source_chunk_size,
    )
    scopus_queries = build_scopus_queries(keyword_spec=keyword_spec, source_clauses=source_clauses)
    allowed_subject_codes = _load_scopus_subject_code_allowlist(settings.scopus_subject_code_allowlist_path)
    if allowed_subject_codes:
        log("3", f"Scopus subject-code filter ENABLED | allow={len(allowed_subject_codes)}")
    else:
        log("3", "Scopus subject-code filter DISABLED | allowlist empty")
    positive_keyword_count = _count_scopus_positive_keyword_terms(keyword_spec)
    if positive_keyword_count:
        mode_counts = " ".join(
            f"{mode}={len(terms)}"
            for mode, terms in keyword_spec.mode_terms.items()
            if terms
        )
        log(
            "3",
            f"Search keyword query ENABLED | keyword_terms={positive_keyword_count} "
            f"exclude_terms={len(keyword_spec.exclude_terms)} {mode_counts}",
        )
    else:
        log("3", "Search keyword query DISABLED | keyword list empty")
    if source_clauses:
        log("3", f"Scopus source filter ENABLED | chunks={len(source_clauses)} chunk_size={settings.scopus_source_chunk_size}")
    else:
        log("3", "Scopus source filter DISABLED | source list empty")
    if skip_abstract:
        log("6", "Abstract enrichment DISABLED | search results will be stored directly")
    state_scopus = None if force_full else db.get_state(settings.state_key_scopus_last_success_at)
    known_source_ids = db.all_scopus_source_ids()
    abstract_seen_source_ids: set[str] = set()

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
        _annotate_matched_keywords(deduplicated_batch, keyword_spec)
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
                if not paper_existing and paper.source_id not in abstract_seen_source_ids:
                    new_scopus_papers.append(paper)
                    abstract_seen_source_ids.add(paper.source_id)
                continue

            latest = _latest_timestamp(paper)
            if not paper_existing:
                incremental_batch.append(paper)
                if paper.source_id not in abstract_seen_source_ids:
                    new_scopus_papers.append(paper)
                    abstract_seen_source_ids.add(paper.source_id)
                continue

            if latest and latest > state_scopus:
                incremental_batch.append(paper)

        incremental_count += len(incremental_batch)
        log("5", f"Incremental filtering END | query_index={index}/{total_queries} selected={len(incremental_batch)}")

        if scopus_collector and new_scopus_papers and not skip_abstract:
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
            _annotate_matched_keywords(incremental_batch, keyword_spec)

        if allowed_subject_codes and not skip_abstract:
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
        elif allowed_subject_codes and skip_abstract:
            log("6", f"Subject-code filter SKIPPED | query_index={index}/{total_queries} reason=abstract disabled")

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
