"""Normalize collector records into the established database column model."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol


class KciPaperInput(Protocol):
    """Collector-neutral projection required by the KCI normalizer."""

    matched_query: str
    matched_field: str
    arti_id: str | None
    journal_id: str | None
    journal_name: str | None
    publisher_name: str | None
    pub_year: str | None
    pub_mon: str | None
    volume: str | None
    issue: str | None
    article_categories: str | None
    article_regularity: str | None
    title_original: str | None
    title_foreign: str | None
    title_english: str | None
    abstract_original: str | None
    abstract_english: str | None
    keywords: list[str]
    authors: list[str]
    doi: str | None
    uci: str | None
    url: str | None
    first_page: str | None
    last_page: str | None
    issn: str | None
    eissn: str | None
    citation_count_kci: str | None
    citation_count_wos: str | None
    is_open_access: str | None
    verified: str | None


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


def _contains_hangul(value: str) -> bool:
    return bool(re.search(r"[가-힣]", value))


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
    categories: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    matched_queries: list[str] = field(default_factory=list)
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
def _compose_pub_date(pub_year: str | None, pub_mon: str | None) -> str | None:
    if not pub_year:
        return None
    if pub_mon:
        return f"{pub_year}-{pub_mon.zfill(2)}"
    return pub_year


def normalize_kci_paper(raw_paper: KciPaperInput) -> NormalizedPaper:
    title = _pick_first(raw_paper.title_original, raw_paper.title_english, raw_paper.title_foreign)
    abstract = _pick_first(raw_paper.abstract_original, raw_paper.abstract_english)
    registered_at = _compose_pub_date(raw_paper.pub_year, raw_paper.pub_mon)
    publication_year = _to_int(raw_paper.pub_year)
    keywords = split_keywords(*raw_paper.keywords)
    keywords_kor = [keyword for keyword in keywords if _contains_hangul(keyword)]
    keywords_eng = [keyword for keyword in keywords if not _contains_hangul(keyword)]

    return NormalizedPaper(
        source="kci",
        source_id=raw_paper.arti_id or raw_paper.doi or raw_paper.uci or title or "unknown",
        title=title,
        title_kor=raw_paper.title_original,
        title_eng=raw_paper.title_english,
        title_other=raw_paper.title_foreign,
        doi=raw_paper.doi,
        uci=raw_paper.uci,
        url=raw_paper.url,
        abstract=abstract,
        abstract_kor=raw_paper.abstract_original,
        abstract_eng=raw_paper.abstract_english,
        abstract_other=None,
        keywords=keywords,
        keyword_text_kor="; ".join(keywords_kor) if keywords_kor else None,
        keyword_text_eng="; ".join(keywords_eng) if keywords_eng else None,
        keyword_text_other=None,
        authors=list(raw_paper.authors),
        journal_id=raw_paper.journal_id or raw_paper.journal_name,
        institution_id=raw_paper.publisher_name,
        issue_id=f"{raw_paper.volume}_{raw_paper.issue}" if raw_paper.volume and raw_paper.issue else (raw_paper.volume or raw_paper.issue),
        first_page=raw_paper.first_page,
        final_page=raw_paper.last_page,
        page_count=None,
        issn=raw_paper.issn,
        eissn=raw_paper.eissn,
        subject_code=raw_paper.article_categories,
        is_fulltext=True if raw_paper.is_open_access == "Y" else False if raw_paper.is_open_access == "N" else None,
        registered_at=registered_at,
        updated_at=None,
        publication_year=publication_year,
        matched_queries=[raw_paper.matched_query] if raw_paper.matched_query else [],
        raw_payload={
            "arti_id": raw_paper.arti_id,
            "matched_field": raw_paper.matched_field,
            "verified": raw_paper.verified,
            "citation_count_kci": raw_paper.citation_count_kci,
            "citation_count_wos": raw_paper.citation_count_wos,
            "article_regularity": raw_paper.article_regularity,
        },
    )


def normalize_kci_papers(
    raw_papers: Iterable[KciPaperInput],
) -> list[NormalizedPaper]:
    return [normalize_kci_paper(raw_paper) for raw_paper in raw_papers]
