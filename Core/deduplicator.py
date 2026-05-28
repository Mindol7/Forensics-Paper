# deduplicator.py #

"""
    - 중복 논문 제거
    - 같은 논문 식별 -> 정보 풍부한 버전 유지 -> 최종 논문 리스트 반환
"""

from __future__ import annotations

from typing import Iterable
import re

from Core.normalizer import NormalizedPaper

# 제목 정규화
def _title_key(title: str | None) -> str | None:
    if not title:
        return None
    cleaned = re.sub(r"[^\w가-힣]+", "", title).casefold()
    return cleaned or None

# 우선순위 기반 return
def _paper_key(paper: NormalizedPaper) -> tuple[str, str]:
    if paper.doi:
        return ("doi", paper.doi.casefold())
    if paper.source_id:
        return ("source_id", f"{paper.source}:{paper.source_id}".casefold())
    return ("title", _title_key(paper.title) or "")

# 논문 정보 풍부도 점수 계산 (정보가 많은 논문이 유지되도록)
def _richness_score(paper: NormalizedPaper) -> int:
    fields = [
        paper.title,
        paper.title_kor,
        paper.title_eng,
        paper.doi,
        paper.abstract,
        paper.url,
        paper.uci,
        paper.journal_id,
        paper.institution_id,
        paper.issue_id,
        paper.issn,
        paper.eissn,
        paper.registered_at,
        paper.updated_at,
        paper.summary,
    ]
    score = sum(1 for field in fields if field)
    score += len(paper.keywords)
    score += len(paper.matched_queries)
    score += len(paper.matched_keyword)
    return score

# 높은 점수의 데이터 선택
def _merge_papers(base: NormalizedPaper, other: NormalizedPaper) -> NormalizedPaper:
    if _richness_score(other) > _richness_score(base):
        base, other = other, base

    for attr in (
        "title",
        "title_kor",
        "title_eng",
        "title_other",
        "doi",
        "uci",
        "url",
        "abstract",
        "abstract_kor",
        "abstract_eng",
        "abstract_other",
        "journal_id",
        "institution_id",
        "issue_id",
        "first_page",
        "final_page",
        "page_count",
        "issn",
        "eissn",
        "subject_code",
        "is_fulltext",
        "registered_at",
        "updated_at",
        "publication_year",
        "summary",
    ):
        if getattr(base, attr) in (None, "", []):
            setattr(base, attr, getattr(other, attr))

    seen_keywords = {keyword.casefold() for keyword in base.keywords}
    for keyword in other.keywords:
        if keyword.casefold() not in seen_keywords:
            base.keywords.append(keyword)
            seen_keywords.add(keyword.casefold())

    seen_queries = {query.casefold() for query in base.matched_queries}
    for query in other.matched_queries:
        if query.casefold() not in seen_queries:
            base.matched_queries.append(query)
            seen_queries.add(query.casefold())

    seen_matched_keywords = {keyword.casefold() for keyword in base.matched_keyword}
    for keyword in other.matched_keyword:
        if keyword.casefold() not in seen_matched_keywords:
            base.matched_keyword.append(keyword)
            seen_matched_keywords.add(keyword.casefold())

    seen_reasons = {reason.casefold() for reason in base.relevance_reasons}
    for reason in other.relevance_reasons:
        if reason.casefold() not in seen_reasons:
            base.relevance_reasons.append(reason)
            seen_reasons.add(reason.casefold())

    if base.relevance_score is None or (other.relevance_score is not None and other.relevance_score > base.relevance_score):
        base.relevance_score = other.relevance_score
        base.is_relevant = other.is_relevant

    if not base.raw_payload and other.raw_payload:
        base.raw_payload = other.raw_payload

    return base

# 중복 제거
def deduplicate_papers(papers: Iterable[NormalizedPaper]) -> list[NormalizedPaper]:
    deduped: dict[tuple[str, str], NormalizedPaper] = {}
    for paper in papers:
        key = _paper_key(paper)
        if key not in deduped:
            deduped[key] = paper
        else:
            deduped[key] = _merge_papers(deduped[key], paper)
    return list(deduped.values())
