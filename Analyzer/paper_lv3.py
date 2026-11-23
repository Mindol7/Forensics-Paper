"""Validate reviewed Level-3 paper classifications and export an Excel report.

This module intentionally does not guess a paper's classification from title
keywords.  Semantic decisions live in auditable per-paper JSON manifests; this
code validates those decisions against the bilingual Type-1 taxonomy, the
current 527-paper corpus, and source metadata before publishing a workbook.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pypdf import PdfReader

ANALYZER_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from PaperPipeline.paper_files import (
    PAPER_FILE_INDEX_NAME,
    PaperFileIndexError,
    PaperFileRecord,
    load_paper_file_index,
)

from Analyzer.technique_ids import canonical_technique_id, legacy_technique_id


DEFAULT_DATABASE = REPOSITORY_ROOT / "kci_pipeline.db"
DEFAULT_TAXONOMY = (
    ANALYZER_DIR / "LV" / "Techniques" / "dfir-technology-classification-(type1).md"
)
DEFAULT_INTELLIGENCE = ANALYZER_DIR / "Intelligence" / "keyword_intelligence.md"
DEFAULT_TECHNIQUE_TAXONOMY = ANALYZER_DIR / "LV" / "Techniques" / "tech_taxonomy_v5.2.json"
# Hashes of earlier revisions of the technique taxonomy that named the same 79
# techniques (``tech_taxonomy.json``: three-letter stage codes, then the
# four-letter Korean revision replaced by v5.2 on 2026-10-04); manifests pinned
# to them stay valid.
SUPERSEDED_TECHNIQUE_TAXONOMY_SHA256 = (
    "3a041d344adfb7e361dc8e284fabcd868837955679abeab7cd8feb33c1e04ed7",
    "8450dbcbb6f92c4afa6817908faba42d59ba1ada3d15c2544eb034829da811af",
)
DEFAULT_TECHNIQUE_MANIFEST = (
    REPOSITORY_ROOT / "web" / "DF-Insight" / "data" / "manifest.json"
)
DEFAULT_ANALYSIS_DIR = ANALYZER_DIR / "Manifest" / "Analysis"
DEFAULT_PAPERS_DIR = ANALYZER_DIR / "Paper"
DEFAULT_STRUCTURE_KO_DIR = ANALYZER_DIR / "Structure" / "kor"
DEFAULT_STRUCTURE_EN_DIR = ANALYZER_DIR / "Structure" / "eng"
DEFAULT_STRUCTURE_INDEX = ANALYZER_DIR / "Structure" / "INDEX.md"
DEFAULT_CLASSIFICATION_DIR = ANALYZER_DIR / "Manifest" / "Lv3"
DEFAULT_PAPER_FILE_INDEX = ANALYZER_DIR / "Manifest" / PAPER_FILE_INDEX_NAME
DEFAULT_OUTPUT = (
    ANALYZER_DIR / "Intelligence" / "kci_paper_level3_classification.xlsx"
)

EXPECTED_LEVEL_1_COUNT = 5
EXPECTED_LEVEL_2_COUNT = 17
EXPECTED_LEVEL_3_COUNT = 70
# Size of the corpus reviewed by the original interactive Codex session.  The
# exporter no longer requires it: the corpus is whatever has been analyzed, and
# indexed PDFs still awaiting analysis are reported as pending instead.
LEGACY_CORPUS_SIZE = 527
EXPECTED_CORPUS_SIZE = LEGACY_CORPUS_SIZE
ANALYSIS_SCHEMA_PREFIX = "2."
MAX_MAPPINGS_PER_PAPER = 3
MIN_CONFIDENCE = 0.80

KOREAN_MARKER = "디지털포렌식 기술분류체계 (Korean version)"
ENGLISH_MARKER = "디지털포렌식 기술분류체계 (English version)"

# The Korean table contains a copy/paste block that is absent from the English
# table and repeated later under the correct examination branch.  Matching the
# bilingual tables requires excluding these two impossible collection paths.
MISPLACED_KOREAN_PATHS = {
    (
        "디지털 증거 수집 및 보존",
        "네트워크 및 클라우드 데이터 수집",
        "애플리케이션 레벨 메모리 조사",
    ),
    (
        "디지털 증거 수집 및 보존",
        "네트워크 및 클라우드 데이터 수집",
        "파일 저장 포맷 조사",
    ),
}

VALID_RELEVANCE = {"관련", "부분 관련", "비관련"}
# Every paper already judged related or partially related must be assigned to
# at least one canonical Level-3 path.  Only non-forensic papers may remain
# without a mapping.
VALID_STATUSES = {"classified", "non_forensic"}
MULTI_VALUE_SEPARATOR = " | "
PATH_SEPARATOR = " > "

MANIFEST_KEYS = {
    "schema_version",
    "source_id",
    "paper_index",
    "title",
    "forensic_relevance",
    "research_type",
    "classification_status",
    "taxonomy_sha256",
    "intelligence_sha256",
    "pdf_sha256",
    "structure_sha256",
    "mappings",
    "not_applicable_reason",
    "review",
}
MAPPING_KEYS = {
    "rank",
    "role",
    "path_id",
    "confidence",
    "rationale",
    "evidence_id",
    "evidence_pages",
    "evidence_support",
}
REVIEW_KEYS = {"status", "reviewed_on", "reviewer"}

PAPER_COLUMNS = [
    "paper_index",
    "source",
    "source_id",
    "title",
    "title_kor",
    "title_eng",
    "doi",
    "url",
    "forensic_relevance",
    "research_type",
    "classification_status",
    "primary_path_id",
    "primary_level_1",
    "primary_level_2",
    "primary_level_3",
    "level_3_count",
    "all_path_ids",
    "all_level_1",
    "all_level_2",
    "all_level_3",
    "classification_paths",
    "classification_rationales",
    "not_applicable_reason",
    "scope_evidence_id",
    "scope_evidence_pages",
    "scope_evidence_support",
    "abstract",
    "keywords",
    "authors",
    "journal_id",
    "institution_id",
    "issue_id",
    "subject_code",
    "registered_at",
    "publication_year",
    "pdf_sha256",
    "structure_sha256",
    "classification_manifest_sha256",
]

DETAIL_COLUMNS = [
    "paper_index",
    "source_id",
    "title",
    "doi",
    "rank",
    "role",
    "path_id",
    "confidence",
    "level_1",
    "level_2",
    "level_3",
    "level_1_en",
    "level_2_en",
    "level_3_en",
    "rationale",
    "evidence_id",
    "evidence_pages",
    "evidence_support",
    "review_status",
    "forensic_relevance",
    "research_type",
    "journal_id",
    "publication_year",
]

PREFERRED_WIDTHS = {
    "paper_index": 12,
    "source": 10,
    "source_id": 18,
    "title": 52,
    "title_kor": 52,
    "title_eng": 52,
    "doi": 30,
    "url": 40,
    "forensic_relevance": 16,
    "research_type": 22,
    "classification_status": 23,
    "primary_path_id": 19,
    "primary_level_1": 28,
    "primary_level_2": 38,
    "primary_level_3": 42,
    "level_3_count": 14,
    "all_path_ids": 38,
    "all_level_1": 38,
    "all_level_2": 54,
    "all_level_3": 64,
    "classification_paths": 85,
    "classification_rationales": 85,
    "not_applicable_reason": 75,
    "scope_evidence_id": 14,
    "scope_evidence_pages": 24,
    "scope_evidence_support": 90,
    "abstract": 80,
    "keywords": 45,
    "authors": 45,
    "journal_id": 28,
    "institution_id": 30,
    "issue_id": 16,
    "subject_code": 24,
    "registered_at": 16,
    "publication_year": 14,
    "pdf_sha256": 68,
    "structure_sha256": 68,
    "classification_manifest_sha256": 68,
    "rank": 10,
    "role": 12,
    "path_id": 19,
    "confidence": 12,
    "level_1": 30,
    "level_2": 44,
    "level_3": 48,
    "level_1_en": 42,
    "level_2_en": 52,
    "level_3_en": 58,
    "rationale": 80,
    "evidence_id": 14,
    "evidence_pages": 22,
    "evidence_support": 90,
    "review_status": 16,
    "count_primary": 15,
    "count_total": 15,
    "source_line_ko": 16,
    "source_line_en": 16,
    "check": 38,
    "value": 90,
}

WRAPPED_COLUMNS = {
    "title",
    "title_kor",
    "title_eng",
    "url",
    "all_path_ids",
    "all_level_1",
    "all_level_2",
    "all_level_3",
    "classification_paths",
    "classification_rationales",
    "not_applicable_reason",
    "scope_evidence_support",
    "abstract",
    "keywords",
    "authors",
    "level_1",
    "level_2",
    "level_3",
    "level_1_en",
    "level_2_en",
    "level_3_en",
    "rationale",
    "evidence_support",
    "value",
}


class PaperLevel3Error(RuntimeError):
    """Raised when classification input cannot be trusted or exported safely."""


@dataclass(frozen=True, slots=True)
class Level3Path:
    path_id: str
    level_1: str
    level_2: str
    level_3: str
    level_1_en: str
    level_2_en: str
    level_3_en: str
    source_line_ko: int
    source_line_en: int

    @property
    def display_path(self) -> str:
        return PATH_SEPARATOR.join((self.level_1, self.level_2, self.level_3))


@dataclass(frozen=True, slots=True)
class Type1Taxonomy:
    """One or more Level-3 vocabularies a classification may cite.

    ``sha256`` identifies the primary (Type-1 Markdown) vocabulary.  A combined
    taxonomy additionally accepts the canonical ``tech_taxonomy.json`` family:
    ``accepted_sha256`` lists every vocabulary hash a manifest may pin, and
    ``family_by_path`` says which hash each ``path_id`` belongs to, so a
    manifest cannot mix identifiers from one family with the hash of another.
    """

    paths: tuple[Level3Path, ...]
    sha256: str
    source_path: Path
    excluded_misplaced_path_count: int
    accepted_sha256: frozenset[str] = frozenset()
    family_by_path: Mapping[str, str] = field(default_factory=dict)
    family_labels: Mapping[str, str] = field(default_factory=dict)
    # Earlier revisions of a member vocabulary (same techniques, earlier stage
    # codes): pinned hash -> the hash of the current revision of that family.
    superseded_sha256: Mapping[str, str] = field(default_factory=dict)

    @property
    def by_id(self) -> Mapping[str, Level3Path]:
        return {path.path_id: path for path in self.paths}

    def accepts(self, taxonomy_sha256: Any) -> bool:
        if isinstance(taxonomy_sha256, str) and taxonomy_sha256 in self.superseded_sha256:
            return True
        if self.accepted_sha256:
            return taxonomy_sha256 in self.accepted_sha256
        return taxonomy_sha256 == self.sha256

    def current_family(self, taxonomy_sha256: str) -> str:
        """The family a pinned hash belongs to today (superseded pins resolve forward)."""

        return self.superseded_sha256.get(taxonomy_sha256, taxonomy_sha256)

    def family_of(self, path_id: str) -> str:
        return self.family_by_path.get(path_id, self.sha256)


@dataclass(frozen=True, slots=True)
class IntelligencePaper:
    index: int
    source_id: str
    title: str
    verdict: str


@dataclass(frozen=True, slots=True)
class MappingDecision:
    rank: int
    role: str
    path_id: str
    confidence: float
    rationale: str
    evidence_id: str
    evidence_pages: str
    evidence_support: str


@dataclass(frozen=True, slots=True)
class PaperDecision:
    source_id: str
    paper_index: int
    forensic_relevance: str
    research_type: str
    classification_status: str
    mappings: tuple[MappingDecision, ...]
    not_applicable_reason: str | None
    review_status: str
    evidence_id: str
    evidence_pages: str
    evidence_support: str
    pdf_sha256: str
    structure_sha256: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class StructureEvidence:
    """Immutable evidence extracted from one reviewed Korean structure document."""

    source_id: str
    title: str
    evidence_id: str
    evidence_pages: str
    evidence_support: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    paper_count: int
    classified_count: int
    unclassified_count: int
    mapping_count: int
    taxonomy_path_count: int
    legacy_count: int = 0
    analysis_count: int = 0
    pending_source_ids: tuple[str, ...] = ()


def _duplicate_key_guard(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PaperLevel3Error(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PaperLevel3Error(f"unable to read JSON: {path}: {exc}") from exc
    try:
        value = json.loads(text, object_pairs_hook=_duplicate_key_guard)
    except json.JSONDecodeError as exc:
        raise PaperLevel3Error(
            f"invalid JSON: {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise PaperLevel3Error(f"JSON root must be an object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PaperLevel3Error(f"unable to hash file: {path}: {exc}") from exc
    return digest.hexdigest()


def _parse_language_rows(
    lines: Sequence[str], *, language: str
) -> list[tuple[str, str, str, int]]:
    marker = KOREAN_MARKER if language == "ko" else ENGLISH_MARKER
    other_marker = ENGLISH_MARKER if language == "ko" else None
    active = False
    level_1: str | None = None
    level_2: str | None = None
    rows: list[tuple[str, str, str, int]] = []

    for line_number, line in enumerate(lines, start=1):
        if marker in line:
            active = True
            continue
        if active and other_marker and other_marker in line:
            break
        if not active or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5:
            continue
        if cells[0].lower() == "level-1" or all(
            not cell or set(cell) <= {":", "-"} for cell in cells
        ):
            continue
        current_level_1, current_level_2, current_level_3, _, _ = cells
        if current_level_1:
            level_1 = current_level_1
            level_2 = None
        if current_level_2:
            level_2 = current_level_2
        if current_level_3:
            if not level_1 or not level_2:
                raise PaperLevel3Error(
                    f"orphan Level-3 row in taxonomy at line {line_number}"
                )
            rows.append((level_1, level_2, current_level_3, line_number))
    return rows


def load_type1_taxonomy(path: str | Path = DEFAULT_TAXONOMY) -> Type1Taxonomy:
    """Parse and reconcile the Korean/English Level-3 Markdown tables."""

    source_path = Path(path)
    try:
        raw = source_path.read_bytes()
        text = raw.decode("utf-8-sig")
    except OSError as exc:
        raise PaperLevel3Error(f"unable to read taxonomy: {source_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise PaperLevel3Error(f"taxonomy is not UTF-8: {source_path}") from exc

    lines = text.splitlines()
    korean_raw = _parse_language_rows(lines, language="ko")
    english = _parse_language_rows(lines, language="en")
    misplaced = [row for row in korean_raw if row[:3] in MISPLACED_KOREAN_PATHS]
    korean = [row for row in korean_raw if row[:3] not in MISPLACED_KOREAN_PATHS]

    misplaced_counts = Counter(row[:3] for row in misplaced)
    expected_misplaced_counts = Counter(MISPLACED_KOREAN_PATHS)
    if misplaced and misplaced_counts != expected_misplaced_counts:
        raise PaperLevel3Error(
            "the known duplicated Korean taxonomy block changed; review is required"
        )
    if len(korean) != EXPECTED_LEVEL_3_COUNT or len(english) != EXPECTED_LEVEL_3_COUNT:
        raise PaperLevel3Error(
            "canonical taxonomy must contain exactly "
            f"{EXPECTED_LEVEL_3_COUNT} bilingual Level-3 paths; "
            f"got ko={len(korean)}, en={len(english)}"
        )

    paths: list[Level3Path] = []
    previous_level_1: str | None = None
    previous_level_2: str | None = None
    previous_level_1_en: str | None = None
    previous_level_2_en: str | None = None
    level_1_number = 0
    level_2_number = 0
    level_3_number = 0
    level_2_paths: set[tuple[str, str]] = set()

    for ko, en in zip(korean, english, strict=True):
        level_1, level_2, level_3, line_ko = ko
        level_1_en, level_2_en, level_3_en, line_en = en
        ko_new_level_1 = level_1 != previous_level_1
        en_new_level_1 = level_1_en != previous_level_1_en
        ko_new_level_2 = ko_new_level_1 or level_2 != previous_level_2
        en_new_level_2 = en_new_level_1 or level_2_en != previous_level_2_en
        if ko_new_level_1 != en_new_level_1 or ko_new_level_2 != en_new_level_2:
            raise PaperLevel3Error(
                "Korean and English taxonomy hierarchy diverges at "
                f"ko line {line_ko}, en line {line_en}"
            )
        if ko_new_level_1:
            level_1_number += 1
            level_2_number = 0
        if ko_new_level_2:
            level_2_number += 1
            level_3_number = 0
        level_3_number += 1
        path_id = (
            f"DFIR-{level_1_number:02d}-{level_2_number:02d}-{level_3_number:02d}"
        )
        paths.append(
            Level3Path(
                path_id=path_id,
                level_1=level_1,
                level_2=level_2,
                level_3=level_3,
                level_1_en=level_1_en,
                level_2_en=level_2_en,
                level_3_en=level_3_en,
                source_line_ko=line_ko,
                source_line_en=line_en,
            )
        )
        level_2_paths.add((level_1, level_2))
        previous_level_1, previous_level_2 = level_1, level_2
        previous_level_1_en, previous_level_2_en = level_1_en, level_2_en

    if len({path.level_1 for path in paths}) != EXPECTED_LEVEL_1_COUNT:
        raise PaperLevel3Error("unexpected number of canonical Level-1 nodes")
    if len(level_2_paths) != EXPECTED_LEVEL_2_COUNT:
        raise PaperLevel3Error("unexpected number of canonical Level-2 paths")
    if len({path.path_id for path in paths}) != len(paths):
        raise PaperLevel3Error("duplicate canonical Level-3 path_id")
    if len({path.display_path for path in paths}) != len(paths):
        raise PaperLevel3Error("duplicate canonical Korean Level-3 path")
    if (
        len(
            {
                (path.level_1_en, path.level_2_en, path.level_3_en)
                for path in paths
            }
        )
        != len(paths)
    ):
        raise PaperLevel3Error("duplicate canonical English Level-3 path")

    sha256 = hashlib.sha256(raw).hexdigest()
    return Type1Taxonomy(
        paths=tuple(paths),
        sha256=sha256,
        source_path=source_path,
        excluded_misplaced_path_count=len(misplaced),
        accepted_sha256=frozenset({sha256}),
        family_by_path={path.path_id: sha256 for path in paths},
        family_labels={sha256: "type1-markdown"},
    )


def load_technique_taxonomy(
    taxonomy_path: str | Path = DEFAULT_TECHNIQUE_TAXONOMY,
    manifest_path: str | Path = DEFAULT_TECHNIQUE_MANIFEST,
) -> Type1Taxonomy:
    """Load the technique taxonomy (v5.2) as a Level-3 vocabulary.

    ``tech_taxonomy_v5.2.json`` carries the ids and English titles; the Korean
    titles (Level 1-3) come from ``tech_titles_ko.json`` beside it.  The DF-Insight
    manifest is no longer needed (``manifest_path`` is kept for callers).
    """

    from Analyzer.technique_taxonomy import TaxonomyError, load_taxonomy

    del manifest_path
    source_path = Path(taxonomy_path)
    try:
        taxonomy = load_taxonomy(source_path)
    except TaxonomyError as exc:
        raise PaperLevel3Error(f"{source_path}: {exc}") from exc
    paths = [
        Level3Path(
            path_id=item.id,
            level_1=item.level_1_ko,
            level_2=item.level_2_ko,
            level_3=item.title_ko,
            level_1_en=item.level_1_en,
            level_2_en=item.level_2_en,
            level_3_en=item.title_en,
            source_line_ko=0,
            source_line_en=0,
        )
        for item in taxonomy.techniques.values()
    ]
    if not paths:
        raise PaperLevel3Error(f"technique taxonomy has no Level-3 paths: {source_path}")
    sha256 = taxonomy.sha256
    return Type1Taxonomy(
        paths=tuple(paths),
        sha256=sha256,
        source_path=source_path,
        excluded_misplaced_path_count=0,
        accepted_sha256=frozenset({sha256}),
        family_by_path={path.path_id: sha256 for path in paths},
        family_labels={sha256: source_path.name},
        superseded_sha256={old: sha256 for old in SUPERSEDED_TECHNIQUE_TAXONOMY_SHA256 if old != sha256},
    )


def combine_taxonomies(primary: Type1Taxonomy, *others: Type1Taxonomy) -> Type1Taxonomy:
    """Union of vocabularies; manifests may pin any member family."""

    paths = list(primary.paths)
    family_by_path = dict(primary.family_by_path or {p.path_id: primary.sha256 for p in primary.paths})
    accepted = set(primary.accepted_sha256 or {primary.sha256})
    labels = dict(primary.family_labels)
    superseded = dict(primary.superseded_sha256)
    for other in others:
        for path in other.paths:
            if path.path_id in family_by_path:
                raise PaperLevel3Error(f"path_id {path.path_id} exists in two taxonomies")
            family_by_path[path.path_id] = other.family_of(path.path_id)
            paths.append(path)
        accepted |= set(other.accepted_sha256 or {other.sha256})
        labels.update(other.family_labels)
        superseded.update(other.superseded_sha256)
    return Type1Taxonomy(
        paths=tuple(paths),
        sha256=primary.sha256,
        source_path=primary.source_path,
        excluded_misplaced_path_count=primary.excluded_misplaced_path_count,
        accepted_sha256=frozenset(accepted),
        family_by_path=family_by_path,
        family_labels=labels,
        superseded_sha256=superseded,
    )


def load_intelligence_papers(
    path: str | Path = DEFAULT_INTELLIGENCE,
) -> tuple[tuple[IntelligencePaper, ...], str]:
    source_path = Path(path)
    try:
        raw = source_path.read_bytes()
        text = raw.decode("utf-8")
    except OSError as exc:
        raise PaperLevel3Error(f"unable to read intelligence: {source_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise PaperLevel3Error(f"intelligence is not UTF-8: {source_path}") from exc

    matches = list(
        re.finditer(
            r"(?ms)^## (?P<index>\d+)\. (?P<source_id>ART\d+)\s*$"
            r"(?P<body>.*?)(?=^## \d+\. ART\d+\s*$|\Z)",
            text,
        )
    )
    papers: list[IntelligencePaper] = []
    for match in matches:
        body = match.group("body")
        title_match = re.search(r"(?m)^### \[(.*?)\]\([^\n]+\)\s*$", body)
        verdict_match = re.search(r"(?m)^- \*\*판정:\*\*\s*(.+?)\s*$", body)
        if not title_match or not verdict_match:
            raise PaperLevel3Error(
                f"incomplete intelligence record: {match.group('source_id')}"
            )
        verdict = verdict_match.group(1).strip()
        if verdict not in VALID_RELEVANCE:
            raise PaperLevel3Error(
                f"unknown forensic relevance for {match.group('source_id')}: {verdict}"
            )
        papers.append(
            IntelligencePaper(
                index=int(match.group("index")),
                source_id=match.group("source_id"),
                title=title_match.group(1).strip(),
                verdict=verdict,
            )
        )

    if not papers:
        raise PaperLevel3Error("intelligence contains no paper records")
    if [paper.index for paper in papers] != list(range(1, len(papers) + 1)):
        raise PaperLevel3Error("intelligence paper indexes are not contiguous")
    identifiers = [paper.source_id for paper in papers]
    if len(identifiers) != len(set(identifiers)):
        raise PaperLevel3Error("duplicate source_id in intelligence")
    return tuple(papers), hashlib.sha256(raw).hexdigest()


def load_analysis_papers(
    directory: str | Path = DEFAULT_ANALYSIS_DIR,
) -> tuple[tuple[IntelligencePaper, ...], dict[str, str]]:
    """Read the Claude analysis records as intelligence papers.

    Each record replaces the legacy whole-file ``intelligence_sha256`` with the
    hash of its own keyword section, so one paper's re-analysis never
    invalidates the other manifests.
    """

    from Analyzer.evidence_writer import keyword_section_sha256

    analysis_dir = Path(directory)
    papers: list[IntelligencePaper] = []
    digests: dict[str, str] = {}
    if not analysis_dir.is_dir():
        return (), digests
    for path in sorted(analysis_dir.glob("ART*.json")):
        record = _load_json(path)
        if record.get("dry_run"):
            continue
        schema = str(record.get("schema_version", ""))
        if not schema.startswith(ANALYSIS_SCHEMA_PREFIX):
            raise PaperLevel3Error(f"{path}: unsupported analysis schema {schema!r}")
        source_id = record.get("source_id")
        if source_id != path.stem:
            raise PaperLevel3Error(f"{path}: analysis record source_id mismatch")
        index = record.get("paper_index")
        if type(index) is not int or index < 1:
            raise PaperLevel3Error(f"{source_id}: analysis record lacks a paper_index")
        title = _require_text(record.get("title"), "title", source_id)
        relevance = record.get("relevance")
        verdict = relevance.get("verdict") if isinstance(relevance, dict) else None
        if verdict not in VALID_RELEVANCE:
            raise PaperLevel3Error(f"{source_id}: analysis record has no valid verdict")
        papers.append(IntelligencePaper(index=index, source_id=source_id, title=title, verdict=verdict))
        digests[source_id] = keyword_section_sha256(record)
    return tuple(papers), digests


def assemble_corpus(
    legacy_papers: Sequence[IntelligencePaper],
    legacy_sha256: str | None,
    analysis_papers: Sequence[IntelligencePaper],
    analysis_sha256: Mapping[str, str],
    indexed_source_ids: Sequence[str],
) -> tuple[tuple[IntelligencePaper, ...], dict[str, str], tuple[str, ...]]:
    """Merge the legacy intelligence corpus with per-paper analysis records.

    Returns the corpus ordered by ``paper_index``, the ``intelligence_sha256``
    each manifest must pin, and the indexed PDFs that have no analysis yet
    (``pending``).  A re-analyzed legacy paper follows its analysis record.
    """

    by_id: dict[str, IntelligencePaper] = {}
    expected: dict[str, str] = {}
    if legacy_papers:
        if legacy_sha256 is None:
            raise PaperLevel3Error("legacy intelligence papers require the file digest")
        for paper in legacy_papers:
            by_id[paper.source_id] = paper
            expected[paper.source_id] = legacy_sha256
    for paper in analysis_papers:
        by_id[paper.source_id] = paper
        expected[paper.source_id] = analysis_sha256[paper.source_id]
    indexed = list(dict.fromkeys(indexed_source_ids))
    if indexed:
        missing_pdf = sorted(set(by_id) - set(indexed))
        if missing_pdf:
            raise PaperLevel3Error(
                f"analyzed papers are absent from the PDF index: {missing_pdf[:10]}"
            )
    pending = tuple(source_id for source_id in indexed if source_id not in by_id)
    papers = tuple(sorted(by_id.values(), key=lambda paper: paper.index))
    if not papers:
        raise PaperLevel3Error("no analyzed papers are available")
    indexes = [paper.index for paper in papers]
    if indexes != list(range(1, len(papers) + 1)):
        duplicates = sorted({value for value in indexes if indexes.count(value) > 1})
        raise PaperLevel3Error(
            f"paper indexes are not contiguous: duplicates={duplicates[:10]}, "
            f"count={len(papers)}, max={indexes[-1] if indexes else 0}"
        )
    return papers, expected, pending


def load_structure_index(path: str | Path) -> dict[str, tuple[str, str]]:
    source_path = Path(path)
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PaperLevel3Error(f"unable to read structure index: {source_path}: {exc}") from exc
    result: dict[str, tuple[str, str]] = {}
    for match in re.finditer(
        r"(?m)^\| (ART\d+) \| ([^|]+?) \| ([^|]+?) \|", text
    ):
        source_id, verdict, research_type = (
            match.group(1),
            match.group(2).strip(),
            match.group(3).strip(),
        )
        if source_id in result:
            raise PaperLevel3Error(f"duplicate source_id in structure index: {source_id}")
        result[source_id] = (verdict, research_type)
    if not result:
        raise PaperLevel3Error("structure index contains no paper records")
    return result


def load_structure_evidence(
    directory: str | Path,
    papers: Sequence[IntelligencePaper],
) -> dict[str, StructureEvidence]:
    """Load the exact E-02 scope evidence used for every semantic decision.

    The structure documents were produced from the PDFs and already bind their
    summary and page range to E-02.  Requiring manifests to repeat those exact
    values makes a stale or hand-edited classification fail closed.
    """

    structure_dir = Path(directory)
    result: dict[str, StructureEvidence] = {}
    for paper in papers:
        path = structure_dir / f"{paper.source_id}.md"
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except OSError as exc:
            raise PaperLevel3Error(f"unable to read structure document: {path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise PaperLevel3Error(f"structure document is not UTF-8: {path}") from exc

        identifier = re.search(r'(?m)^identifier:\s*"(ART\d+)"\s*$', text)
        heading = re.search(r"(?m)^# (.+?)\s*$", text)
        validation = re.search(r"(?m)^\| 검증 수준 \| (.+?) \|\s*$", text)
        summary = re.search(
            r"(?m)^- \[(?:OBS|PROP|LIT)\] (.+?) \[E-02\]\s*$", text
        )
        if (
            identifier is None
            or identifier.group(1) != paper.source_id
            or heading is None
            or validation is None
            or summary is None
        ):
            raise PaperLevel3Error(
                f"{paper.source_id}: structure document lacks canonical E-02 evidence"
            )
        pages = re.search(
            r"근거 페이지:\s*(PDF pp?\.\s*.*?)(?=\.\s)", validation.group(1)
        )
        if pages is None:
            raise PaperLevel3Error(
                f"{paper.source_id}: unable to parse E-02 PDF page range"
            )
        result[paper.source_id] = StructureEvidence(
            source_id=paper.source_id,
            title=heading.group(1).strip(),
            evidence_id="E-02",
            evidence_pages=pages.group(1).strip(),
            evidence_support=summary.group(1).strip(),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    return result


def _require_text(value: Any, field: str, source_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperLevel3Error(f"{source_id}: {field} must be non-empty text")
    return value.strip()


def _require_explanation(value: Any, field: str, source_id: str) -> str:
    text = _require_text(value, field, source_id)
    if len(text) < 20:
        raise PaperLevel3Error(
            f"{source_id}: {field} is too short to be an auditable explanation"
        )
    return text


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], *, field: str, source_id: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise PaperLevel3Error(
            f"{source_id}: {field} keys differ: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def load_classifications(
    directory: str | Path,
    *,
    papers: Sequence[IntelligencePaper],
    intelligence_sha256: str | Mapping[str, str],
    taxonomy: Type1Taxonomy,
    structure_index: Mapping[str, tuple[str, str]],
    structure_evidence: Mapping[str, StructureEvidence],
    papers_dir: str | Path,
    paper_files: Mapping[str, PaperFileRecord] | None = None,
) -> dict[str, PaperDecision]:
    classification_dir = Path(directory)
    files = sorted(classification_dir.glob("ART*.json"))
    expected_ids = {paper.source_id for paper in papers}
    actual_ids = {file.stem for file in files}
    if actual_ids != expected_ids:
        raise PaperLevel3Error(
            "classification manifest set differs from intelligence: "
            f"missing={sorted(expected_ids - actual_ids)[:10]}, "
            f"extra={sorted(actual_ids - expected_ids)[:10]}"
        )

    paper_by_id = {paper.source_id: paper for paper in papers}
    decisions: dict[str, PaperDecision] = {}
    valid_paths = taxonomy.by_id
    paper_root = Path(papers_dir)
    indexed_files = paper_files or {}
    for file in files:
        value = _load_json(file)
        _require_exact_keys(value, MANIFEST_KEYS, field="manifest", source_id=file.stem)
        source_id = _require_text(value.get("source_id"), "source_id", file.stem)
        if source_id != file.stem or source_id not in expected_ids:
            raise PaperLevel3Error(f"manifest filename/source_id mismatch: {file}")
        if value.get("schema_version") != "1.0":
            raise PaperLevel3Error(f"{source_id}: unsupported schema_version")
        manifest_taxonomy = value.get("taxonomy_sha256")
        if not taxonomy.accepts(manifest_taxonomy):
            raise PaperLevel3Error(f"{source_id}: stale taxonomy_sha256")
        manifest_taxonomy = taxonomy.current_family(manifest_taxonomy)
        expected_intelligence = (
            intelligence_sha256.get(source_id)
            if isinstance(intelligence_sha256, Mapping)
            else intelligence_sha256
        )
        if expected_intelligence is None or value.get("intelligence_sha256") != expected_intelligence:
            raise PaperLevel3Error(f"{source_id}: stale intelligence_sha256")

        paper = paper_by_id[source_id]
        if value.get("title") != paper.title:
            raise PaperLevel3Error(f"{source_id}: title mismatch")
        paper_index = value.get("paper_index")
        if type(paper_index) is not int or paper_index != paper.index:
            raise PaperLevel3Error(f"{source_id}: paper_index mismatch")
        forensic_relevance = _require_text(
            value.get("forensic_relevance"), "forensic_relevance", source_id
        )
        if forensic_relevance != paper.verdict:
            raise PaperLevel3Error(f"{source_id}: forensic relevance mismatch")
        indexed = structure_index.get(source_id)
        if indexed is None or indexed[0] != forensic_relevance:
            raise PaperLevel3Error(f"{source_id}: structure index verdict mismatch")
        research_type = _require_text(value.get("research_type"), "research_type", source_id)
        if research_type != indexed[1]:
            raise PaperLevel3Error(f"{source_id}: research_type mismatch")
        status = _require_text(
            value.get("classification_status"), "classification_status", source_id
        )
        if status not in VALID_STATUSES:
            raise PaperLevel3Error(f"{source_id}: unknown classification_status {status!r}")
        review = value.get("review")
        if not isinstance(review, dict) or review.get("status") != "approved":
            raise PaperLevel3Error(f"{source_id}: classification is not approved")
        _require_exact_keys(review, REVIEW_KEYS, field="review", source_id=source_id)
        reviewer = review.get("reviewer")
        reviewed_on = review.get("reviewed_on")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise PaperLevel3Error(f"{source_id}: review reviewer is missing")
        try:
            date.fromisoformat(reviewed_on)
        except (TypeError, ValueError) as exc:
            raise PaperLevel3Error(f"{source_id}: invalid reviewed_on date") from exc

        evidence = structure_evidence.get(source_id)
        if evidence is None:
            raise PaperLevel3Error(f"{source_id}: structure evidence is missing")
        pdf_filename = (
            indexed_files[source_id].filename
            if source_id in indexed_files
            else f"{source_id}.pdf"
        )
        pdf_path = paper_root / pdf_filename
        pdf_sha256 = _sha256_file(pdf_path)
        if value.get("pdf_sha256") != pdf_sha256:
            raise PaperLevel3Error(f"{source_id}: stale pdf_sha256")
        if value.get("structure_sha256") != evidence.sha256:
            raise PaperLevel3Error(f"{source_id}: stale structure_sha256")

        raw_mappings = value.get("mappings")
        if not isinstance(raw_mappings, list):
            raise PaperLevel3Error(f"{source_id}: mappings must be an array")
        if len(raw_mappings) > MAX_MAPPINGS_PER_PAPER:
            raise PaperLevel3Error(f"{source_id}: too many Level-3 mappings")
        mappings: list[MappingDecision] = []
        seen_path_ids: set[str] = set()
        for expected_rank, raw_mapping in enumerate(raw_mappings, start=1):
            if not isinstance(raw_mapping, dict):
                raise PaperLevel3Error(f"{source_id}: mapping must be an object")
            _require_exact_keys(
                raw_mapping, MAPPING_KEYS, field=f"mapping[{expected_rank}]", source_id=source_id
            )
            rank = raw_mapping.get("rank")
            role = raw_mapping.get("role")
            path_id = raw_mapping.get("path_id")
            if isinstance(path_id, str) and path_id not in valid_paths:
                # Evidence written under the earlier stage codes (EXM-… -> EXAM-…).
                path_id = canonical_technique_id(path_id)
            confidence = raw_mapping.get("confidence")
            if (
                type(rank) is not int
                or rank != expected_rank
                or role != ("primary" if rank == 1 else "secondary")
            ):
                raise PaperLevel3Error(f"{source_id}: invalid mapping rank/role")
            if path_id not in valid_paths or path_id in seen_path_ids:
                raise PaperLevel3Error(f"{source_id}: unknown or duplicate path_id {path_id!r}")
            if taxonomy.family_of(path_id) != manifest_taxonomy:
                raise PaperLevel3Error(
                    f"{source_id}: path_id {path_id!r} belongs to a different taxonomy "
                    "than the pinned taxonomy_sha256"
                )
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not MIN_CONFIDENCE <= float(confidence) <= 1.0
            ):
                raise PaperLevel3Error(f"{source_id}: invalid mapping confidence")
            seen_path_ids.add(path_id)
            mappings.append(
                MappingDecision(
                    rank=rank,
                    role=role,
                    path_id=path_id,
                    confidence=float(confidence),
                    rationale=_require_explanation(
                        raw_mapping.get("rationale"), "rationale", source_id
                    ),
                    evidence_id=_require_text(raw_mapping.get("evidence_id"), "evidence_id", source_id),
                    evidence_pages=_require_text(raw_mapping.get("evidence_pages"), "evidence_pages", source_id),
                    evidence_support=_require_text(raw_mapping.get("evidence_support"), "evidence_support", source_id),
                )
            )
            mapping = mappings[-1]
            if (
                mapping.evidence_id != evidence.evidence_id
                or mapping.evidence_pages != evidence.evidence_pages
                or mapping.evidence_support != evidence.evidence_support
            ):
                raise PaperLevel3Error(
                    f"{source_id}: mapping evidence differs from the hashed structure document"
                )

        reason_value = value.get("not_applicable_reason")
        not_applicable_reason = (
            reason_value.strip() if isinstance(reason_value, str) and reason_value.strip() else None
        )
        if not_applicable_reason is not None and len(not_applicable_reason) < 20:
            raise PaperLevel3Error(
                f"{source_id}: not_applicable_reason is too short to be auditable"
            )
        if mappings:
            if status != "classified" or not_applicable_reason is not None:
                raise PaperLevel3Error(f"{source_id}: invalid classified status/reason combination")
        else:
            if forensic_relevance != "비관련":
                raise PaperLevel3Error(
                    f"{source_id}: related or partially related paper requires "
                    "at least one Level-3 mapping"
                )
            if status != "non_forensic" or not_applicable_reason is None:
                raise PaperLevel3Error(f"{source_id}: invalid unclassified status/reason combination")
        if forensic_relevance == "비관련" and mappings:
            raise PaperLevel3Error(f"{source_id}: non-forensic paper has a Level-3 mapping")

        decisions[source_id] = PaperDecision(
            source_id=source_id,
            paper_index=paper_index,
            forensic_relevance=forensic_relevance,
            research_type=research_type,
            classification_status=status,
            mappings=tuple(mappings),
            not_applicable_reason=not_applicable_reason,
            review_status="approved",
            evidence_id=evidence.evidence_id,
            evidence_pages=evidence.evidence_pages,
            evidence_support=evidence.evidence_support,
            pdf_sha256=pdf_sha256,
            structure_sha256=evidence.sha256,
            manifest_sha256=_sha256_file(file),
        )
    return decisions


def _json_list(value: Any, *, field: str, source_id: str) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise PaperLevel3Error(f"{source_id}: invalid DB JSON field {field}") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise PaperLevel3Error(f"{source_id}: DB field {field} must be a string array")
    return decoded


def load_metadata(database: str | Path, source_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    database_path = Path(database)
    if not database_path.is_file() or database_path.stat().st_size == 0:
        raise PaperLevel3Error(f"database is missing or empty: {database_path}")
    placeholders = ",".join("?" for _ in source_ids)
    query = f"""
        SELECT source, source_id, title, title_kor, title_eng, doi, url, abstract,
               keywords_json, authors_json, journal_id, institution_id, issue_id,
               subject_code, registered_at, publication_year
        FROM kci_papers WHERE source_id IN ({placeholders})
    """
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(query, tuple(source_ids)).fetchall()
    except sqlite3.Error as exc:
        raise PaperLevel3Error(f"unable to load metadata: {database_path}: {exc}") from exc
    finally:
        connection.close()
    metadata: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        source_id = record["source_id"]
        if source_id in metadata:
            raise PaperLevel3Error(f"duplicate DB metadata row: {source_id}")
        record["keywords"] = _json_list(
            record.pop("keywords_json"), field="keywords_json", source_id=source_id
        )
        record["authors"] = _json_list(
            record.pop("authors_json"), field="authors_json", source_id=source_id
        )
        metadata[source_id] = record
    expected = set(source_ids)
    if set(metadata) != expected:
        raise PaperLevel3Error(
            f"DB metadata set mismatch: missing={sorted(expected - set(metadata))[:10]}"
        )
    return metadata


def validate_corpus_files(
    papers: Sequence[IntelligencePaper],
    *,
    papers_dir: str | Path,
    structure_ko_dir: str | Path,
    structure_en_dir: str | Path,
    expected_size: int | None = None,
    paper_files: Mapping[str, PaperFileRecord] | None = None,
) -> None:
    expected = {paper.source_id for paper in papers}
    if not expected:
        raise PaperLevel3Error("the corpus contains no papers")
    if expected_size is not None and len(expected) != expected_size:
        raise PaperLevel3Error(
            f"expected {expected_size} intelligence papers, found {len(expected)}"
        )
    indexed_files = paper_files or {}
    if indexed_files:
        if not expected.issubset(indexed_files):
            raise PaperLevel3Error(
                "paper-file index source_id set mismatch: "
                f"missing={sorted(expected - set(indexed_files))[:10]}, "
                "the index may contain additional incrementally collected papers"
            )
        expected_pdf_files = {
            indexed_files[source_id].filename for source_id in expected
        }
        actual_pdf_files = {
            path.name for path in Path(papers_dir).glob("*.pdf")
        }
        indexed_pdf_files = {
            record.filename for record in indexed_files.values()
        }
        if not expected_pdf_files.issubset(actual_pdf_files):
            raise PaperLevel3Error(
                "PDF filename set mismatch: "
                f"missing={sorted(expected_pdf_files - actual_pdf_files)[:10]}, "
                "additional indexed PDFs are permitted"
            )
        unindexed_pdf_files = actual_pdf_files - indexed_pdf_files
        if unindexed_pdf_files:
            raise PaperLevel3Error(
                "unindexed PDF file found: "
                f"extra={sorted(unindexed_pdf_files)[:10]}"
            )
    sets = {
        "Korean structure": {path.stem for path in Path(structure_ko_dir).glob("*.md")},
        "English structure": {path.stem for path in Path(structure_en_dir).glob("*.md")},
    }
    if not indexed_files:
        sets["PDF"] = {path.stem for path in Path(papers_dir).glob("*.pdf")}
    for label, actual in sets.items():
        if actual != expected:
            raise PaperLevel3Error(
                f"{label} source_id set mismatch: "
                f"missing={sorted(expected - actual)[:10]}, extra={sorted(actual - expected)[:10]}"
            )
    for paper in papers:
        pdf_filename = (
            indexed_files[paper.source_id].filename
            if paper.source_id in indexed_files
            else f"{paper.source_id}.pdf"
        )
        pdf = Path(papers_dir) / pdf_filename
        try:
            with pdf.open("rb") as stream:
                magic = stream.read(5)
        except OSError as exc:
            raise PaperLevel3Error(f"unable to read PDF: {pdf}: {exc}") from exc
        if magic != b"%PDF-":
            raise PaperLevel3Error(f"invalid PDF signature: {pdf}")
        pypdf_logger = logging.getLogger("pypdf")
        previous_level = pypdf_logger.level
        try:
            pypdf_logger.setLevel(logging.ERROR)
            reader = PdfReader(pdf, strict=False)
            if len(reader.pages) < 1:
                raise PaperLevel3Error(f"PDF contains no pages: {pdf}")
        except PaperLevel3Error:
            raise
        except Exception as exc:
            raise PaperLevel3Error(f"PDF cannot be parsed: {pdf}: {exc}") from exc
        finally:
            pypdf_logger.setLevel(previous_level)

    structure_requirements = {
        "Korean structure": (Path(structure_ko_dir), "## 0. 논문 범위", "## 4. 조사"),
        "English structure": (
            Path(structure_en_dir),
            "## 0. Paper Scope",
            "## 4. Examination",
        ),
    }
    for label, (directory, scope_heading, examination_heading) in structure_requirements.items():
        for paper in papers:
            path = directory / f"{paper.source_id}.md"
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except OSError as exc:
                raise PaperLevel3Error(f"unable to read {label}: {path}: {exc}") from exc
            except UnicodeDecodeError as exc:
                raise PaperLevel3Error(f"{label} is not UTF-8: {path}") from exc
            identifier = re.search(r'(?m)^identifier:\s*"(ART\d+)"\s*$', text)
            if (
                not raw
                or identifier is None
                or identifier.group(1) != paper.source_id
                or "[E-01]" not in text
                or "[E-02]" not in text
                or scope_heading not in text
                or examination_heading not in text
            ):
                raise PaperLevel3Error(
                    f"{label} lacks the canonical structure/evidence anchors: {path}"
                )


def _stable_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _digest_named_hashes(values: Iterable[tuple[str, str]]) -> str:
    """Hash an ordered identifier-to-hash manifest without filesystem metadata."""

    payload = "".join(f"{name}\t{digest}\n" for name, digest in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _paper_row(
    paper: IntelligencePaper,
    decision: PaperDecision,
    metadata: Mapping[str, Any],
    taxonomy: Type1Taxonomy,
) -> dict[str, Any]:
    paths = [taxonomy.by_id[mapping.path_id] for mapping in decision.mappings]
    primary = paths[0] if paths else None
    rationales = [
        f"[{mapping.path_id}] {mapping.rationale}" for mapping in decision.mappings
    ]
    return {
        "paper_index": paper.index,
        "source": metadata.get("source"),
        "source_id": paper.source_id,
        "title": paper.title,
        "title_kor": metadata.get("title_kor"),
        "title_eng": metadata.get("title_eng"),
        "doi": metadata.get("doi"),
        "url": metadata.get("url"),
        "forensic_relevance": decision.forensic_relevance,
        "research_type": decision.research_type,
        "classification_status": decision.classification_status,
        "primary_path_id": primary.path_id if primary else None,
        "primary_level_1": primary.level_1 if primary else None,
        "primary_level_2": primary.level_2 if primary else None,
        "primary_level_3": primary.level_3 if primary else None,
        "level_3_count": len(paths),
        "all_path_ids": MULTI_VALUE_SEPARATOR.join(path.path_id for path in paths) or None,
        "all_level_1": MULTI_VALUE_SEPARATOR.join(
            _stable_unique(path.level_1 for path in paths)
        ) or None,
        "all_level_2": MULTI_VALUE_SEPARATOR.join(
            _stable_unique(path.level_2 for path in paths)
        ) or None,
        "all_level_3": MULTI_VALUE_SEPARATOR.join(path.level_3 for path in paths) or None,
        "classification_paths": "\n".join(path.display_path for path in paths) or None,
        "classification_rationales": "\n".join(rationales) or None,
        "not_applicable_reason": decision.not_applicable_reason,
        "scope_evidence_id": decision.evidence_id,
        "scope_evidence_pages": decision.evidence_pages,
        "scope_evidence_support": decision.evidence_support,
        "abstract": metadata.get("abstract"),
        "keywords": "; ".join(metadata.get("keywords") or []) or None,
        "authors": "; ".join(metadata.get("authors") or []) or None,
        "journal_id": metadata.get("journal_id"),
        "institution_id": metadata.get("institution_id"),
        "issue_id": metadata.get("issue_id"),
        "subject_code": metadata.get("subject_code"),
        "registered_at": metadata.get("registered_at"),
        "publication_year": metadata.get("publication_year"),
        "pdf_sha256": decision.pdf_sha256,
        "structure_sha256": decision.structure_sha256,
        "classification_manifest_sha256": decision.manifest_sha256,
    }


def _detail_rows(
    papers: Sequence[IntelligencePaper],
    decisions: Mapping[str, PaperDecision],
    metadata: Mapping[str, Mapping[str, Any]],
    taxonomy: Type1Taxonomy,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for paper in papers:
        decision = decisions[paper.source_id]
        paper_metadata = metadata[paper.source_id]
        for mapping in decision.mappings:
            path = taxonomy.by_id[mapping.path_id]
            rows.append(
                {
                    "paper_index": paper.index,
                    "source_id": paper.source_id,
                    "title": paper.title,
                    "doi": paper_metadata.get("doi"),
                    "rank": mapping.rank,
                    "role": mapping.role,
                    "path_id": mapping.path_id,
                    "confidence": mapping.confidence,
                    "level_1": path.level_1,
                    "level_2": path.level_2,
                    "level_3": path.level_3,
                    "level_1_en": path.level_1_en,
                    "level_2_en": path.level_2_en,
                    "level_3_en": path.level_3_en,
                    "rationale": mapping.rationale,
                    "evidence_id": mapping.evidence_id,
                    "evidence_pages": mapping.evidence_pages,
                    "evidence_support": mapping.evidence_support,
                    "review_status": decision.review_status,
                    "forensic_relevance": decision.forensic_relevance,
                    "research_type": decision.research_type,
                    "journal_id": paper_metadata.get("journal_id"),
                    "publication_year": paper_metadata.get("publication_year"),
                }
            )
    return rows


def _set_text_cell(cell, value: Any) -> None:
    if isinstance(value, str) and len(value) > 32_767:
        raise PaperLevel3Error(
            f"Excel cell {cell.coordinate} exceeds the 32,767-character limit"
        )
    cell.value = value
    if isinstance(value, str):
        # Prevent titles or evidence beginning with '=', '+', '-', or '@' from
        # becoming formulas when the workbook is opened.
        cell.data_type = "s"


def _write_rows(worksheet, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    for column_index, header in enumerate(columns, start=1):
        _set_text_cell(worksheet.cell(row=1, column=column_index), header)
    for row_index, row in enumerate(rows, start=2):
        for column_index, header in enumerate(columns, start=1):
            _set_text_cell(worksheet.cell(row=row_index, column=column_index), row.get(header))


def _style_worksheet(worksheet) -> None:
    worksheet.freeze_panes = "A2"
    worksheet.sheet_view.showGridLines = False
    worksheet.sheet_view.zoomScale = 85
    if worksheet.max_row >= 1 and worksheet.max_column >= 1:
        worksheet.auto_filter.ref = (
            f"A1:{get_column_letter(worksheet.max_column)}{worksheet.max_row}"
        )
    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="D9E2F3")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    headers = {cell.column: str(cell.value or "") for cell in worksheet[1]}
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
        cell.border = border
    worksheet.row_dimensions[1].height = 26
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            header = headers.get(cell.column, "")
            cell.alignment = Alignment(
                vertical="top", wrap_text=header in WRAPPED_COLUMNS
            )
            cell.border = border
    for column_index, header in headers.items():
        worksheet.column_dimensions[get_column_letter(column_index)].width = (
            PREFERRED_WIDTHS.get(header, min(max(len(header) + 3, 12), 40))
        )


def build_workbook(
    *,
    papers: Sequence[IntelligencePaper],
    decisions: Mapping[str, PaperDecision],
    metadata: Mapping[str, Mapping[str, Any]],
    taxonomy: Type1Taxonomy,
    intelligence_sha256: str,
    pending_source_ids: Sequence[str] = (),
) -> Workbook:
    workbook = Workbook()
    paper_sheet = workbook.active
    paper_sheet.title = "논문별 분류"
    paper_rows = [
        _paper_row(paper, decisions[paper.source_id], metadata[paper.source_id], taxonomy)
        for paper in papers
    ]
    _write_rows(paper_sheet, PAPER_COLUMNS, paper_rows)

    detail_rows = _detail_rows(papers, decisions, metadata, taxonomy)
    detail_sheet = workbook.create_sheet("분류 상세")
    _write_rows(detail_sheet, DETAIL_COLUMNS, detail_rows)

    primary_counts = Counter(
        decision.mappings[0].path_id
        for decision in decisions.values()
        if decision.mappings
    )
    total_counts = Counter(
        mapping.path_id
        for decision in decisions.values()
        for mapping in decision.mappings
    )
    summary_columns = [
        "path_id",
        "level_1",
        "level_2",
        "level_3",
        "count_primary",
        "count_total",
    ]
    summary_rows = [
        {
            "path_id": path.path_id,
            "level_1": path.level_1,
            "level_2": path.level_2,
            "level_3": path.level_3,
            "count_primary": primary_counts[path.path_id],
            "count_total": total_counts[path.path_id],
        }
        for path in taxonomy.paths
    ]
    summary_sheet = workbook.create_sheet("분류 요약")
    _write_rows(summary_sheet, summary_columns, summary_rows)

    unclassified_rows = [
        row for row in paper_rows if row["classification_status"] != "classified"
    ]
    unclassified_sheet = workbook.create_sheet("미분류 검토")
    _write_rows(unclassified_sheet, PAPER_COLUMNS, unclassified_rows)

    taxonomy_columns = [
        "path_id",
        "level_1",
        "level_2",
        "level_3",
        "level_1_en",
        "level_2_en",
        "level_3_en",
        "source_line_ko",
        "source_line_en",
    ]
    taxonomy_rows = [
        {
            "path_id": path.path_id,
            "level_1": path.level_1,
            "level_2": path.level_2,
            "level_3": path.level_3,
            "level_1_en": path.level_1_en,
            "level_2_en": path.level_2_en,
            "level_3_en": path.level_3_en,
            "source_line_ko": path.source_line_ko,
            "source_line_en": path.source_line_en,
        }
        for path in taxonomy.paths
    ]
    taxonomy_sheet = workbook.create_sheet("분류체계")
    _write_rows(taxonomy_sheet, taxonomy_columns, taxonomy_rows)

    status_counts = Counter(decision.classification_status for decision in decisions.values())
    ordered_decisions = [decisions[paper.source_id] for paper in papers]
    pdf_set_sha256 = _digest_named_hashes(
        (decision.source_id, decision.pdf_sha256) for decision in ordered_decisions
    )
    structure_set_sha256 = _digest_named_hashes(
        (decision.source_id, decision.structure_sha256) for decision in ordered_decisions
    )
    manifest_set_sha256 = _digest_named_hashes(
        (decision.source_id, decision.manifest_sha256) for decision in ordered_decisions
    )
    verification_rows = [
        {"check": "generated_on", "value": date.today().isoformat()},
        {"check": "paper_count", "value": len(papers)},
        {"check": "unique_source_id_count", "value": len({p.source_id for p in papers})},
        {"check": "classified_count", "value": status_counts["classified"]},
        {
            "check": "related_or_partial_unclassified_count",
            "value": sum(
                1
                for decision in decisions.values()
                if decision.forensic_relevance != "비관련" and not decision.mappings
            ),
        },
        {"check": "non_forensic_count", "value": status_counts["non_forensic"]},
        {"check": "mapping_count", "value": len(detail_rows)},
        {"check": "taxonomy_level_1_count", "value": len({path.level_1 for path in taxonomy.paths})},
        {"check": "taxonomy_level_2_count", "value": len({(path.level_1, path.level_2) for path in taxonomy.paths})},
        {"check": "taxonomy_level_3_count", "value": len(taxonomy.paths)},
        {"check": "taxonomy_sha256", "value": taxonomy.sha256},
        *(
            {"check": f"taxonomy_sha256[{label}]", "value": digest}
            for digest, label in sorted(taxonomy.family_labels.items(), key=lambda item: item[1])
        ),
        {"check": "intelligence_sha256", "value": intelligence_sha256},
        {"check": "pending_analysis_count", "value": len(pending_source_ids)},
        {"check": "pending_source_ids", "value": ", ".join(pending_source_ids)},
        {"check": "pdf_set_sha256", "value": pdf_set_sha256},
        {"check": "structure_set_sha256", "value": structure_set_sha256},
        {"check": "classification_manifest_set_sha256", "value": manifest_set_sha256},
        {"check": "manifest_count", "value": len(decisions)},
        {"check": "unknown_path_count", "value": 0},
        {"check": "stale_manifest_count", "value": 0},
        {"check": "needs_review_count", "value": 0},
        {
            "check": "classification_policy",
            "value": (
                "논문의 직접적·실질적 기여만 최대 3개 Level-3 경로에 배정하고, "
                "배경·도구·부수 언급은 배정 근거로 사용하지 않음. 법·판례·정책 "
                "연구는 구현 여부가 아니라 직접 규율·검토한 증거 대상과 생애주기 "
                "행위를 기준으로 가장 구체적인 경로에 배정"
            ),
        },
        {
            "check": "unclassified_policy",
            "value": (
                "비관련 논문만 non_forensic으로 미분류하며, 관련·부분 관련 논문은 "
                "연구 대상과 행위를 기준으로 최소 1개 canonical Level-3 경로에 배정"
            ),
        },
        {
            "check": "canonical_correction",
            "value": (
                "한국어 표에서 네트워크 및 클라우드 데이터 수집 아래 잘못 복제된 "
                f"Level-3 {taxonomy.excluded_misplaced_path_count}개를 영어 표와 "
                "교차검증하여 제외"
                if taxonomy.excluded_misplaced_path_count
                else "한국어·영어 표가 70개 canonical Level-3 경로로 일치"
            ),
        },
    ]
    verification_sheet = workbook.create_sheet("검증")
    _write_rows(verification_sheet, ["check", "value"], verification_rows)

    for worksheet in workbook.worksheets:
        _style_worksheet(worksheet)
    workbook.active = 0
    return workbook


def _atomic_save(workbook: Workbook, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = destination.stat().st_mode & 0o777 if destination.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".xlsx", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        try:
            workbook.save(temporary)
        finally:
            workbook.close()
        verification = load_workbook(temporary, read_only=True, data_only=True)
        verification.close()
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def export_level3(
    *,
    database: str | Path = DEFAULT_DATABASE,
    taxonomy_path: str | Path = DEFAULT_TAXONOMY,
    intelligence_path: str | Path = DEFAULT_INTELLIGENCE,
    papers_dir: str | Path = DEFAULT_PAPERS_DIR,
    structure_ko_dir: str | Path = DEFAULT_STRUCTURE_KO_DIR,
    structure_en_dir: str | Path = DEFAULT_STRUCTURE_EN_DIR,
    structure_index_path: str | Path = DEFAULT_STRUCTURE_INDEX,
    classification_dir: str | Path = DEFAULT_CLASSIFICATION_DIR,
    paper_file_index_path: str | Path | None = DEFAULT_PAPER_FILE_INDEX,
    output_path: str | Path = DEFAULT_OUTPUT,
    expected_corpus_size: int | None = None,
    technique_taxonomy_path: str | Path | None = DEFAULT_TECHNIQUE_TAXONOMY,
    technique_manifest_path: str | Path = DEFAULT_TECHNIQUE_MANIFEST,
    analysis_dir: str | Path | None = DEFAULT_ANALYSIS_DIR,
    strict: bool = False,
) -> ExportResult:
    """Validate every analyzed paper's Level-3 decision and export the workbook.

    The corpus is the legacy intelligence list plus the Claude analysis
    records, restricted to indexed PDFs.  Indexed PDFs without an analysis are
    reported as pending (``strict`` turns them into an error).
    """

    taxonomy = load_type1_taxonomy(taxonomy_path)
    if technique_taxonomy_path is not None and Path(technique_taxonomy_path).is_file():
        taxonomy = combine_taxonomies(
            taxonomy, load_technique_taxonomy(technique_taxonomy_path, technique_manifest_path)
        )
    legacy_papers: tuple[IntelligencePaper, ...] = ()
    legacy_sha256: str | None = None
    if intelligence_path is not None and Path(intelligence_path).is_file():
        legacy_papers, legacy_sha256 = load_intelligence_papers(intelligence_path)
    analysis_papers, analysis_sha256 = (
        load_analysis_papers(analysis_dir) if analysis_dir is not None else ((), {})
    )
    try:
        paper_files = (
            load_paper_file_index(paper_file_index_path)
            if paper_file_index_path is not None
            else {}
        )
    except PaperFileIndexError as exc:
        raise PaperLevel3Error(str(exc)) from exc
    papers, intelligence_by_paper, pending = assemble_corpus(
        legacy_papers,
        legacy_sha256,
        analysis_papers,
        analysis_sha256,
        list(paper_files),
    )
    if strict and pending:
        raise PaperLevel3Error(
            f"{len(pending)} indexed PDFs are still awaiting analysis: {list(pending[:10])}"
        )
    intelligence_sha256 = _digest_named_hashes(
        (paper.source_id, intelligence_by_paper[paper.source_id]) for paper in papers
    )
    validate_corpus_files(
        papers,
        papers_dir=papers_dir,
        structure_ko_dir=structure_ko_dir,
        structure_en_dir=structure_en_dir,
        expected_size=expected_corpus_size,
        paper_files=paper_files,
    )
    structure_index = load_structure_index(structure_index_path)
    expected_ids = {paper.source_id for paper in papers}
    if set(structure_index) != expected_ids:
        raise PaperLevel3Error("structure index source_id set differs from intelligence")
    structure_evidence = load_structure_evidence(structure_ko_dir, papers)
    decisions = load_classifications(
        classification_dir,
        papers=papers,
        intelligence_sha256=intelligence_by_paper,
        taxonomy=taxonomy,
        structure_index=structure_index,
        structure_evidence=structure_evidence,
        papers_dir=papers_dir,
        paper_files=paper_files,
    )
    metadata = load_metadata(database, [paper.source_id for paper in papers])
    workbook = build_workbook(
        papers=papers,
        decisions=decisions,
        metadata=metadata,
        taxonomy=taxonomy,
        intelligence_sha256=intelligence_sha256,
        pending_source_ids=pending,
    )
    destination = Path(output_path)
    _atomic_save(workbook, destination)
    classified_count = sum(
        decision.classification_status == "classified" for decision in decisions.values()
    )
    mapping_count = sum(len(decision.mappings) for decision in decisions.values())
    return ExportResult(
        path=destination,
        paper_count=len(papers),
        classified_count=classified_count,
        unclassified_count=len(papers) - classified_count,
        mapping_count=mapping_count,
        taxonomy_path_count=len(taxonomy.paths),
        legacy_count=sum(1 for paper in papers if paper.source_id not in analysis_sha256),
        analysis_count=sum(1 for paper in papers if paper.source_id in analysis_sha256),
        pending_source_ids=pending,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate reviewed paper Level-3 decisions and export XLSX."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--intelligence", type=Path, default=DEFAULT_INTELLIGENCE)
    parser.add_argument("--papers-dir", type=Path, default=DEFAULT_PAPERS_DIR)
    parser.add_argument("--structure-ko-dir", type=Path, default=DEFAULT_STRUCTURE_KO_DIR)
    parser.add_argument("--structure-en-dir", type=Path, default=DEFAULT_STRUCTURE_EN_DIR)
    parser.add_argument("--structure-index", type=Path, default=DEFAULT_STRUCTURE_INDEX)
    parser.add_argument("--classification-dir", type=Path, default=DEFAULT_CLASSIFICATION_DIR)
    parser.add_argument(
        "--paper-file-index",
        type=Path,
        default=DEFAULT_PAPER_FILE_INDEX,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--expected-corpus-size",
        type=int,
        default=None,
        help="Require exactly this many analyzed papers (default: no fixed size).",
    )
    parser.add_argument("--technique-taxonomy", type=Path, default=DEFAULT_TECHNIQUE_TAXONOMY)
    parser.add_argument("--technique-manifest", type=Path, default=DEFAULT_TECHNIQUE_MANIFEST)
    parser.add_argument("--analysis-dir", type=Path, default=DEFAULT_ANALYSIS_DIR)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when an indexed PDF has no analysis instead of reporting it as pending.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = export_level3(
            database=args.database,
            taxonomy_path=args.taxonomy,
            intelligence_path=args.intelligence,
            papers_dir=args.papers_dir,
            structure_ko_dir=args.structure_ko_dir,
            structure_en_dir=args.structure_en_dir,
            structure_index_path=args.structure_index,
            classification_dir=args.classification_dir,
            paper_file_index_path=args.paper_file_index,
            output_path=args.output,
            expected_corpus_size=args.expected_corpus_size,
            technique_taxonomy_path=args.technique_taxonomy,
            technique_manifest_path=args.technique_manifest,
            analysis_dir=args.analysis_dir,
            strict=args.strict,
        )
    except PaperLevel3Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(result.path),
                "paper_count": result.paper_count,
                "classified_count": result.classified_count,
                "unclassified_count": result.unclassified_count,
                "mapping_count": result.mapping_count,
                "taxonomy_path_count": result.taxonomy_path_count,
                "legacy_count": result.legacy_count,
                "analysis_count": result.analysis_count,
                "pending_count": len(result.pending_source_ids),
                "pending_source_ids": list(result.pending_source_ids),
                "errors": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
