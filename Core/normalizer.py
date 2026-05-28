# normalizer.py #
"""
    - KCI에서 수집한 '원본 논문 데이터'를 표준화된 구조로 변환하는 모듈
    - 다양한 형식 raw 데이터 -> 정리된 NormalizedPaper 객체 변환 -> 이후 필터링/저장/분석에 사용.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
import re
from typing import Any, Iterable

from Collector.kci import KciRawPaper
from Collector.scopus import ScopusRawPaper


@dataclass
class NormalizedPaper:
    source: str
    source_id: str
    title: str | None
    title_kor: str | None
    title_eng: str | None
    title_other: str | None
    doi: str | None
    uci: str | None
    url: str | None
    abstract: str | None
    abstract_kor: str | None
    abstract_eng: str | None
    abstract_other: str | None
    keywords: list[str] = field(default_factory=list)
    keyword_text_kor: str | None = None
    keyword_text_eng: str | None = None
    keyword_text_other: str | None = None
    authors: list[str] = field(default_factory=list)
    journal_id: str | None = None
    institution_id: str | None = None
    issue_id: str | None = None
    first_page: str | None = None
    final_page: str | None = None
    page_count: int | None = None
    issn: str | None = None
    eissn: str | None = None
    subject_code: str | None = None
    is_fulltext: bool | None = None
    registered_at: str | None = None
    updated_at: str | None = None
    publication_year: int | None = None
    matched_queries: list[str] = field(default_factory=list)
    matched_keyword: list[str] = field(default_factory=list)
    relevance_score: float | None = None
    relevance_reasons: list[str] = field(default_factory=list)
    is_relevant: bool | None = None
    summary: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

# 키워드 문자열 -> 리스트 변환
_SPLIT_RE = re.compile(r"\s*(?:;|\||,|\n|\t)\s*")


def split_keywords(*values: str | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        for token in _SPLIT_RE.split(value):
            cleaned = token.strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(cleaned)
    return result

# 날짜 처리 (다양한 날짜 포맷 -> ISO 표준 변환)
def normalize_kci_datetime(value: str | None) -> str | None:
    if not value:
        return None

    text = value.strip()
    formats = ["%Y%m%d%H%M%S", "%Y-%m-%d", "%Y%m%d"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                return parsed.date().isoformat()
            if fmt == "%Y%m%d":
                return parsed.date().isoformat()
            return parsed.isoformat()
        except ValueError:
            continue
    return text


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _pick_first(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def normalize_kci_paper(raw_paper: KciRawPaper) -> NormalizedPaper:
    registered_at = normalize_kci_datetime(raw_paper.registered_at_raw)
    updated_at = normalize_kci_datetime(raw_paper.updated_at_raw)
    keywords = split_keywords(raw_paper.keyword_kor, raw_paper.keyword_eng, raw_paper.keyword_fola)

    return NormalizedPaper(
        source="kci",
        source_id=raw_paper.arti_id or raw_paper.uci or raw_paper.doi or raw_paper.title_kor or raw_paper.title_eng or "unknown",
        title=_pick_first(raw_paper.title_kor, raw_paper.title_eng, raw_paper.title_fola),
        title_kor=raw_paper.title_kor,
        title_eng=raw_paper.title_eng,
        title_other=raw_paper.title_fola,
        doi=raw_paper.doi,
        uci=raw_paper.uci,
        url=raw_paper.url,
        abstract=_pick_first(raw_paper.abstract_kor, raw_paper.abstract_eng, raw_paper.abstract_fola),
        abstract_kor=raw_paper.abstract_kor,
        abstract_eng=raw_paper.abstract_eng,
        abstract_other=raw_paper.abstract_fola,
        keywords=keywords,
        keyword_text_kor=raw_paper.keyword_kor,
        keyword_text_eng=raw_paper.keyword_eng,
        keyword_text_other=raw_paper.keyword_fola,
        authors=[],
        journal_id=raw_paper.sere_id,
        institution_id=raw_paper.insi_id,
        issue_id=raw_paper.vol_isse_id,
        first_page=raw_paper.first_page,
        final_page=raw_paper.final_page,
        page_count=_to_int(raw_paper.total_page_count),
        issn=raw_paper.issn,
        eissn=raw_paper.eissn,
        subject_code=raw_paper.subject_code,
        is_fulltext=True if raw_paper.is_fulltext == "Y" else False if raw_paper.is_fulltext == "N" else None,
        registered_at=registered_at,
        updated_at=updated_at,
        # openApiM310List does not expose an explicit publication date/year.
        # Keep this field empty instead of inferring from registration metadata.
        publication_year=None,
        matched_queries=[raw_paper.matched_query] if raw_paper.matched_query else [],
        raw_payload=raw_paper.raw_item,
    )


def normalize_kci_papers(raw_papers: Iterable[KciRawPaper]) -> list[NormalizedPaper]:
    return [normalize_kci_paper(raw_paper) for raw_paper in raw_papers]


def normalize_scopus_datetime(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    formats = ["%Y-%m-%d", "%Y-%m", "%Y"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                return parsed.date().isoformat()
            if fmt == "%Y-%m":
                return f"{parsed.year:04d}-{parsed.month:02d}"
            return f"{parsed.year:04d}"
        except ValueError:
            continue
    return text


def _extract_year_from_date(date_text: str | None) -> int | None:
    if not date_text:
        return None
    match = re.match(r"^(\d{4})", date_text)
    if not match:
        return None
    return int(match.group(1))


def _compose_scopus_issue_id(volume: str | None, issue_identifier: str | None) -> str | None:
    if volume and issue_identifier:
        return f"{volume}_{issue_identifier}"
    if volume:
        return volume
    if issue_identifier:
        return issue_identifier
    return None


def normalize_scopus_paper(raw_paper: ScopusRawPaper) -> NormalizedPaper:
    registered_at = normalize_scopus_datetime(raw_paper.cover_date)

    return NormalizedPaper(
        source="scopus",
        source_id=raw_paper.scopus_id or raw_paper.eid or raw_paper.doi or raw_paper.title or "unknown",
        title=raw_paper.title,
        title_kor=None,
        title_eng=raw_paper.title,
        title_other=None,
        doi=raw_paper.doi,
        uci=None,
        url=raw_paper.url,
        abstract=raw_paper.abstract,
        abstract_kor=None,
        abstract_eng=raw_paper.abstract,
        abstract_other=None,
        keywords=split_keywords(*raw_paper.keywords),
        keyword_text_kor=None,
        keyword_text_eng="; ".join(raw_paper.keywords) if raw_paper.keywords else None,
        keyword_text_other=None,
        authors=list(raw_paper.author_names),
        journal_id=raw_paper.publication_name,
        institution_id=None,
        issue_id=_compose_scopus_issue_id(raw_paper.volume, raw_paper.issue_identifier),
        first_page=None,
        final_page=None,
        page_count=None,
        issn=raw_paper.issn,
        eissn=raw_paper.eissn,
        subject_code=None,
        is_fulltext=None,
        registered_at=registered_at,
        updated_at=None,
        publication_year=_extract_year_from_date(raw_paper.cover_date),
        matched_queries=[raw_paper.matched_query] if raw_paper.matched_query else [],
        raw_payload=raw_paper.raw_item,
    )


def normalize_scopus_papers(raw_papers: Iterable[ScopusRawPaper]) -> list[NormalizedPaper]:
    return [normalize_scopus_paper(raw_paper) for raw_paper in raw_papers]
