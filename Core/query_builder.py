# query_builder.py #
"""
    - KCI 논문 검색용 쿼리들을 자동 생성하는 모듈
    - 여러 키워드 기반, 다양한 조건 조합, 중복 없는 검색 쿼리 리스트 생성
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


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


def _build_title_abs_key_clause(terms: Iterable[str]) -> str:
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

    if not segments:
        return ""
    return " OR ".join(segments)


def build_scopus_queries(
    keyword_terms: Iterable[str] | None = None,
    *,
    target_years: Iterable[int] | None = None,
) -> list[ScopusQuery]:
    """Create Scopus query objects for each target year."""
    queries: list[ScopusQuery] = []
    years = tuple(target_years) if target_years is not None else get_scopus_target_years()
    title_abs_key_clause = _build_title_abs_key_clause(keyword_terms or [])

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
