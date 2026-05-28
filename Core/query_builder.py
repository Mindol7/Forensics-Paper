# query_builder.py #
"""
    - KCI 논문 검색용 쿼리들을 자동 생성하는 모듈
    - 여러 키워드 기반, 다양한 조건 조합, 중복 없는 검색 쿼리 리스트 생성
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping


@dataclass(frozen=True)
class KciQuery:
    """Single KCI paper-search query.

    KCI openApiM310List documents artiNm, sereId, and insiId as relevant inputs
    for paper lookup, with artiNm functioning as the practical entry point for
    title-based discovery in the current MVP.
    """

    arti_nm: str # 논문 제목 기반 검색 키워드
    sere_id: str | None = None # 학술지 ID
    insi_id: str | None = None # 기관 ID
    pubi_yr: int | None = None


@dataclass(frozen=True)
class ScopusQuery:
    """Single Scopus search query."""

    query: str
    view: str | None = None
    sort: str | None = None
    date: str | None = None
    subj: str | None = None


@dataclass(frozen=True)
class ScopusKeywordSpec:
    """Structured Scopus keyword query configuration."""

    mode_terms: Mapping[str, tuple[str, ...]]
    anchor_modes: Mapping[str, tuple[str, ...]]
    exclude_terms: tuple[str, ...] = ()

# 기본 검색 키워드: 추후 확장 가능
DEFAULT_KCI_TITLE_TERMS: tuple[str, ...] = (
    "디지털 포렌식",
    "디지털포렌식",
    "digital forensic",
    "digital forensics",
    "mobile forensic",
    "모바일 포렌식",
    "memory forensic",
    "메모리 포렌식",
    "network forensic",
    "네트워크 포렌식",
    "disk forensic",
    "dfir",
)

# 문자열 공백 제거, 빈 문자열 처리
def _normalize_term(term: str) -> str:
    return " ".join(term.strip().split())

# 다양한 조건 조합 -> 중복 없는 KCI 검색 쿼리 리스트 생성
def build_kci_queries(
    extra_terms: Iterable[str] | None = None, # 사용자 정의 키워드
    sere_ids: Iterable[str] | None = None,
    insi_ids: Iterable[str] | None = None,
    target_years: Iterable[int] | None = None,
) -> list[KciQuery]:
    """Create deduplicated KCI query objects.

    Why this approach:
    - KCI currently exposes title-oriented search inputs rather than rich boolean
      fielded search, so controlled term expansion is more robust than a complex
      query DSL for the first KCI-only version.
    """
    base_terms = [_normalize_term(term) for term in DEFAULT_KCI_TITLE_TERMS if _normalize_term(term)]

    if extra_terms:
        for term in extra_terms:
            normalized = _normalize_term(term)
            if normalized and normalized not in base_terms:
                base_terms.append(normalized)

    years = tuple(target_years) if target_years is not None else get_scopus_target_years()
    queries: list[KciQuery] = []
    for year in years:
        queries.extend(KciQuery(arti_nm=term, pubi_yr=year) for term in base_terms)

    if sere_ids:
        for sere_id in sere_ids:
            for term in base_terms:
                for year in years:
                    queries.append(KciQuery(arti_nm=term, sere_id=sere_id, pubi_yr=year))

    if insi_ids:
        for insi_id in insi_ids:
            for term in base_terms:
                for year in years:
                    queries.append(KciQuery(arti_nm=term, insi_id=insi_id, pubi_yr=year))

    deduped: list[KciQuery] = []
    seen: set[tuple[str, str | None, str | None, int | None]] = set()
    for query in queries:
        key = (query.arti_nm.casefold(), query.sere_id, query.insi_id, query.pubi_yr)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)

    return deduped


SCOPUS_INCLUDE_SUBJECTS: tuple[str, ...] = ("COMP", "SOCI", "DECI", "MULT", "ENGI", "PSYC")
SCOPUS_EXCLUDE_SUBJECTS: tuple[str, ...] = ("AGRI", "ENVI")
SCOPUS_INCLUDE_DOCTYPES: tuple[str, ...] = ("ar", "re", "cp", "sh", "dp")


def get_scopus_target_years(*, current_year: int | None = None) -> tuple[int, ...]:
    year = current_year if current_year is not None else datetime.now().year
    start_year = 2022
    end_year = year + 1
    return tuple(range(start_year, end_year + 1))


def _build_scopus_base_query_for_year(year: int) -> str:
    doctype_clause = " OR ".join(f"DOCTYPE({doctype})" for doctype in SCOPUS_INCLUDE_DOCTYPES)
    return f"({doctype_clause})"


def _build_scopus_exclude_subject_clause() -> str:
    return " ".join(f"AND NOT SUBJAREA({subject})" for subject in SCOPUS_EXCLUDE_SUBJECTS)


def _dedupe_terms(terms: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = _normalize_term(term)
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _format_scopus_keyword_term(term: str) -> str:
    normalized = _normalize_term(term)
    if any(char.isspace() for char in normalized) or "-" in normalized:
        escaped = normalized.replace('"', '\\"')
        return f'"{escaped}"'
    return normalized


def _join_keyword_terms(terms: Iterable[str]) -> str:
    segments: list[str] = []
    for term in _dedupe_terms(terms):
        formatted = _format_scopus_keyword_term(term)
        if not formatted:
            continue
        segments.append(formatted)
    return " OR ".join(segments)


def _build_scopus_positive_keyword_clause(keyword_spec: ScopusKeywordSpec) -> str:
    positive_clauses: list[str] = []

    direct_terms = keyword_spec.mode_terms.get("direct", ())
    direct_clause = _join_keyword_terms(direct_terms)
    if direct_clause:
        positive_clauses.append(f"TITLE-ABS-KEY({direct_clause})")

    for mode, anchors in keyword_spec.anchor_modes.items():
        term_clause = _join_keyword_terms(keyword_spec.mode_terms.get(mode, ()))
        anchor_clause = _join_keyword_terms(anchors)
        if not term_clause or not anchor_clause:
            continue
        positive_clauses.append(f"TITLE-ABS-KEY(({term_clause}) AND ({anchor_clause}))")

    if not positive_clauses:
        return ""

    return f"({' OR '.join(positive_clauses)})"


def _build_scopus_exclude_keyword_clause(keyword_spec: ScopusKeywordSpec) -> str:
    exclude_clause = _join_keyword_terms(keyword_spec.exclude_terms)
    if exclude_clause:
        return f"AND NOT (TITLE-ABS-KEY({exclude_clause}))"
    return ""


def _keyword_spec_from_flat_terms(keyword_terms: Iterable[str] | None) -> ScopusKeywordSpec:
    return ScopusKeywordSpec(
        mode_terms={"direct": tuple(_dedupe_terms(keyword_terms or ()))},
        anchor_modes={},
        exclude_terms=(),
    )


def build_scopus_queries(
    keyword_terms: Iterable[str] | None = None,
    *,
    keyword_spec: ScopusKeywordSpec | None = None,
    source_clauses: Iterable[str] | None = None,
    target_years: Iterable[int] | None = None,
) -> list[ScopusQuery]:
    """Create Scopus query objects for each target year."""
    queries: list[ScopusQuery] = []
    years = tuple(target_years) if target_years is not None else get_scopus_target_years()
    effective_keyword_spec = keyword_spec or _keyword_spec_from_flat_terms(keyword_terms)
    keyword_clause = _build_scopus_positive_keyword_clause(effective_keyword_spec)
    exclude_keyword_clause = _build_scopus_exclude_keyword_clause(effective_keyword_spec)
    source_clause_values = tuple(source_clauses or ())
    if not source_clause_values:
        source_clause_values = ("",)
    subj = ",".join(SCOPUS_INCLUDE_SUBJECTS)

    for year in years:
        base_query = _build_scopus_base_query_for_year(year)
        exclude_subject_clause = _build_scopus_exclude_subject_clause()
        common_tail = " ".join(part for part in (exclude_keyword_clause, exclude_subject_clause) if part)
        for source_clause in source_clause_values:
            positive_parts = [base_query]
            if keyword_clause:
                positive_parts.append(keyword_clause)
            if source_clause:
                positive_parts.append(source_clause)
            query_text = " AND ".join(positive_parts)
            if common_tail:
                query_text = f"{query_text} {common_tail}"
            queries.append(ScopusQuery(query=query_text, date=str(year), subj=subj))

    deduped: list[ScopusQuery] = []
    seen: set[tuple[str, str | None, str | None, str | None, str | None]] = set()
    for query in queries:
        key = (
            query.query.casefold(),
            query.view,
            query.sort,
            query.date,
            query.subj,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)
    return deduped
