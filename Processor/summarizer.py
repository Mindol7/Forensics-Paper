# summarizer.py #

from __future__ import annotations

import re
from typing import Iterable

from Core.normalizer import NormalizedPaper


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|(?<=다\.)\s+")


def summarize_paper(paper: NormalizedPaper, *, max_sentences: int = 2) -> NormalizedPaper:
    """Deterministic fallback summarizer.

    Why this exists:
    - the project architecture expects a summarization stage
    - the KCI-only MVP should still run without depending on an external LLM API
    """
    if paper.summary:
        return paper

    if paper.abstract:
        sentences = [segment.strip() for segment in _SENTENCE_SPLIT_RE.split(paper.abstract) if segment.strip()]
        summary = " ".join(sentences[:max_sentences]).strip()
        paper.summary = summary or paper.abstract[:300]
        return paper

    keywords = ", ".join(paper.keywords[:5]) if paper.keywords else "키워드 없음"
    title = paper.title or "제목 없음"
    paper.summary = f"{title} | keywords: {keywords}"
    return paper


def summarize_papers(papers: Iterable[NormalizedPaper], *, max_sentences: int = 2) -> list[NormalizedPaper]:
    return [summarize_paper(paper, max_sentences=max_sentences) for paper in papers]
