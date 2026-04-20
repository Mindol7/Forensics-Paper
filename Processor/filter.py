# filter.py #

from __future__ import annotations

from typing import Iterable

from Core.normalizer import NormalizedPaper


STRONG_TERMS = {
    "디지털 포렌식": 4.0,
    "디지털포렌식": 4.0,
    "digital forensic": 4.0,
    "digital forensics": 4.0,
    "mobile forensic": 3.5,
    "모바일 포렌식": 3.5,
    "memory forensic": 3.5,
    "메모리 포렌식": 3.5,
    "network forensic": 3.0,
    "네트워크 포렌식": 3.0,
    "disk forensic": 3.0,
    "dfir": 3.0,
    "incident response": 2.5,
    "artifact": 1.0,
    "forensic artifact": 2.0,
    "malware forensic": 2.0,
    "malware analysis": 1.5,
}

WEAK_TERMS = {
    "포렌식": 1.5,
    "forensic": 1.5,
    "사이버": 0.5,
    "cyber": 0.5,
    "로그": 0.5,
    "log": 0.5,
    "증거": 0.5,
    "evidence": 0.5,
}

EXCLUDE_TERMS = {
    "법의학",
    "forensic psychiatry",
    "forensic medicine",
    "forensic nursing",
    "legal medicine",
}

# 점수 계산
def _score_text(text: str | None) -> tuple[float, list[str]]:
    if not text:
        return 0.0, []
    lowered = text.casefold()
    score = 0.0
    reasons: list[str] = []

    for term in EXCLUDE_TERMS:
        if term.casefold() in lowered:
            score -= 3.0
            reasons.append(f"exclude:{term}")

    for term, weight in STRONG_TERMS.items():
        if term.casefold() in lowered:
            score += weight
            reasons.append(f"strong:{term}")

    for term, weight in WEAK_TERMS.items():
        if term.casefold() in lowered:
            score += weight
            reasons.append(f"weak:{term}")

    return score, reasons

# 최종 점수 판단
def apply_rule_based_filter(paper: NormalizedPaper, *, min_score: float = 3.0) -> NormalizedPaper:
    title_score, title_reasons = _score_text(paper.title)
    abstract_score, abstract_reasons = _score_text(paper.abstract)
    keywords_score, keywords_reasons = _score_text(" ".join(paper.keywords))

    score = title_score * 1.5 + abstract_score + keywords_score * 1.2
    reasons = title_reasons + abstract_reasons + keywords_reasons

    paper.relevance_score = round(score, 2)
    paper.relevance_reasons = reasons
    paper.is_relevant = score >= min_score
    return paper


def filter_digital_forensics_papers(
    papers: Iterable[NormalizedPaper],
    *, # *이후의 인자들은 반드시 키워드 인자로만 전달해야함
    min_score: float = 3.0,
    keep_irrelevant: bool = False,
) -> list[NormalizedPaper]:
    result: list[NormalizedPaper] = []
    for paper in papers:
        enriched = apply_rule_based_filter(paper, min_score=min_score)
        if keep_irrelevant or enriched.is_relevant:
            result.append(enriched)
    return result
