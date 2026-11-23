"""Validate reviewed PDF document-type decisions and export ``type_list.xlsx``.

The six subtype codes are loaded from :mod:`Analyzer/Type/paper_type.md`; they
are never inferred from a hard-coded replacement taxonomy.  Classification is
also deliberately separated from keyword screening.  A corpus-bound review
manifest records which exact PDF hashes were reviewed and contains explicit
decisions for every boundary candidate.  All remaining papers can be assigned
to the residual ``ar`` leaf only after the manifest attests that the full
corpus was audited for ``di``, ``cp``, ``dp``, ``re`` and ``sh`` first.

The exporter cross-checks the 527 title-named PDFs against their download
manifests, the SQLite metadata, and the existing PDF-grounded Structure/Lv3
evidence.  It then writes an atomic, formula-free workbook with per-paper
reasoning and provenance sheets.

Run from any directory::

    python3 Analyzer/Type/paper_type.py
    python3 -m Analyzer.Type.paper_type
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from pypdf import PdfReader


TYPE_DIR = Path(__file__).resolve().parent
ANALYZER_DIR = TYPE_DIR.parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from PaperPipeline.db_source import normalize_doi
from PaperPipeline.paper_files import (
    PAPER_FILE_INDEX_NAME,
    PaperFileRecord,
    load_paper_file_index,
)
from Util.logger import ProgressLogger


DEFAULT_DATABASE = REPOSITORY_ROOT / "kci_pipeline.db"
DEFAULT_TAXONOMY = TYPE_DIR / "paper_type.md"
DEFAULT_REVIEW = TYPE_DIR / "type_review.json"
DEFAULT_OUTPUT = TYPE_DIR / "type_list.xlsx"
DEFAULT_PAPER_INDEX = ANALYZER_DIR / "Manifest" / PAPER_FILE_INDEX_NAME
DEFAULT_PAPERS_DIR = ANALYZER_DIR / "Paper"
DEFAULT_DOWNLOAD_MANIFEST_DIR = ANALYZER_DIR / "Manifest"
DEFAULT_STRUCTURE_DIR = ANALYZER_DIR / "Structure" / "kor"
DEFAULT_LV3_DIR = ANALYZER_DIR / "Manifest" / "Lv3"
DEFAULT_DECISION_DIR = ANALYZER_DIR / "Manifest" / "Type"
LEGACY_REVIEW_RECORD_NAME = "_legacy_review.json"

# Size of the corpus reviewed by the original monolithic review manifest.  The
# exporter itself no longer assumes a fixed size: every indexed PDF with a
# Structure document and Level-3 manifest is part of the corpus, and indexed
# PDFs still awaiting analysis are reported as pending.
LEGACY_CORPUS_SIZE = 527
EXPECTED_CORPUS_SIZE = LEGACY_CORPUS_SIZE
REVIEW_SCHEMA_VERSION = "1.0"
DECISION_SCHEMA_VERSION = "1.0"
REPORT_SCHEMA_VERSION = "1.1"
DECISION_KEYS = {
    "schema_version",
    "source_id",
    "subtype",
    "confidence",
    "boundary_candidate",
    "rationale",
    "boundary_reason",
    "pdf_sha256",
    "structure_sha256",
    "lv3_manifest_sha256",
    "taxonomy_sha256",
    "review",
}
DEFAULT_DECISION_POLICY = (
    "현재 PDF 자체의 공식 형식(di, cp)을 먼저 판정하고, 데이터셋이 주기여인 dp, "
    "기존 지식 종합이 주기여인 re/sh를 차례로 판정한 뒤에만 잔여 leaf ar를 적용한다."
)
VALID_SUBTYPES = ("ar", "re", "cp", "sh", "dp", "di")
ARTIFACT_PRIORITY = ("di", "cp", "dp", "re/sh", "ar")
RESEARCH_TYPE_LABELS = {
    "ACQUISITION_METHOD": "수집·복구 방법론",
    "DETECTION_METHOD": "탐지·분류 방법론",
    "EDUCATION_TRAINING": "교육·훈련 연구",
    "EMPIRICAL_ARTIFACT": "실증 아티팩트 분석",
    "LAW_CASE_POLICY": "법·판례·정책 논증",
    "NON_FORENSIC": "비포렌식 연구",
    "REVIEW_SURVEY": "문헌·동향 검토",
    "TOOL_SYSTEM": "도구·시스템 개발",
}
STRUCTURE_RESEARCH_TYPES = {
    "수집·복구 방법론": "ACQUISITION_METHOD",
    "탐지·분류 방법론": "DETECTION_METHOD",
    "교육·훈련": "EDUCATION_TRAINING",
    "실증 아티팩트 분석": "EMPIRICAL_ARTIFACT",
    "법·판례·정책": "LAW_CASE_POLICY",
    "포렌식 비관련": "NON_FORENSIC",
    "문헌고찰·동향": "REVIEW_SURVEY",
    "도구·시스템 개발": "TOOL_SYSTEM",
}

_ART_ID_RE = re.compile(r"ART[0-9]{9}\Z")
_TAXONOMY_ROW_RE = re.compile(
    r"^\|\s*([a-z]{2})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$"
)
_STRUCTURE_FIELD_RE = re.compile(
    r"^\|\s*(연구 유형|포렌식 관련성)\s*\|\s*(.*?)\s*\|\s*$", re.MULTILINE
)
_STRUCTURE_SUMMARY_RE = re.compile(
    r"^### 핵심 요약\s*$\s*^- \[[A-Z]+\]\s*(.*?)\s*\[(E-[0-9]+)\]\s*$",
    re.MULTILINE,
)
_STRUCTURE_EVIDENCE_RE = re.compile(
    r"^\|\s*(E-[0-9]+)\s*\|\s*([A-Z]+)\s*\|\s*(PDF\s+p{1,2}\.[^|]+)\s*\|\s*(.*?)\s*\|\s*$",
    re.MULTILINE,
)
_INVALID_EXCEL_XML_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]"
)

MAIN_COLUMNS = [
    "paper_index",
    "database_id",
    "source",
    "source_id",
    "title",
    "title_kor",
    "title_eng",
    "doi",
    "journal_id",
    "institution_id",
    "publication_year",
    "subtype",
    "subtype_des",
    "subtype_description_ko",
    "classification_status",
    "confidence",
    "boundary_candidate",
    "artifact_form",
    "primary_purpose",
    "primary_contribution",
    "classification_rationale",
    "boundary_reason",
    "evidence_source",
    "evidence_pages",
    "evidence_summary",
    "research_type",
    "forensic_relevance",
    "pdf_filename",
    "pdf_page_count",
    "pdf_sha256",
    "text_extractable",
    "text_char_count",
    "text_sha256",
    "structure_sha256",
    "lv3_manifest_sha256",
    "taxonomy_sha256",
    "review_manifest_sha256",
    "review_status",
    "reviewed_on",
    "reviewer",
]

PREFERRED_WIDTHS = {
    "paper_index": 12,
    "database_id": 13,
    "source": 10,
    "source_id": 18,
    "title": 58,
    "title_kor": 58,
    "title_eng": 58,
    "doi": 32,
    "journal_id": 30,
    "institution_id": 32,
    "publication_year": 14,
    "subtype": 10,
    "subtype_des": 20,
    "subtype_description_ko": 24,
    "classification_status": 18,
    "confidence": 12,
    "boundary_candidate": 18,
    "artifact_form": 24,
    "primary_purpose": 75,
    "primary_contribution": 75,
    "classification_rationale": 88,
    "boundary_reason": 88,
    "evidence_source": 25,
    "evidence_pages": 28,
    "evidence_summary": 95,
    "research_type": 24,
    "forensic_relevance": 16,
    "pdf_filename": 62,
    "pdf_page_count": 15,
    "pdf_sha256": 68,
    "text_extractable": 16,
    "text_char_count": 16,
    "text_sha256": 68,
    "structure_sha256": 68,
    "lv3_manifest_sha256": 68,
    "taxonomy_sha256": 68,
    "review_manifest_sha256": 68,
    "review_status": 16,
    "reviewed_on": 16,
    "reviewer": 42,
}


class PaperTypeError(RuntimeError):
    """Raised when corpus, decision, provenance, or workbook checks fail."""


@dataclass(frozen=True, slots=True)
class TaxonomyRow:
    subtype: str
    subtype_des: str
    description_ko: str


@dataclass(frozen=True, slots=True)
class StructureEvidence:
    research_type: str
    forensic_relevance: str
    summary: str
    evidence_id: str
    evidence_pages: str
    evidence_support: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PdfEvidence:
    page_count: int
    sha256: str
    text_extractable: bool
    text_char_count: int
    text_sha256: str


@dataclass(frozen=True, slots=True)
class CorpusPaper:
    paper_index: int
    file_record: PaperFileRecord
    metadata: Mapping[str, Any]
    structure: StructureEvidence
    pdf: PdfEvidence
    lv3_sha256: str


@dataclass(frozen=True, slots=True)
class TypeDecision:
    """One paper's reviewed document-type decision, bound to its evidence."""

    source_id: str
    subtype: str
    confidence: float
    boundary_candidate: bool
    rationale: str
    boundary_reason: str
    review_status: str
    reviewed_on: str
    reviewer: str
    file_sha256: str

    @property
    def as_mapping(self) -> dict[str, Any]:
        return {
            "subtype": self.subtype,
            "confidence": self.confidence,
            "boundary_candidate": self.boundary_candidate,
            "rationale": self.rationale,
            "boundary_reason": self.boundary_reason,
        }


@dataclass(frozen=True, slots=True)
class CorpusLoad:
    papers: tuple[CorpusPaper, ...]
    db_count: int
    corpus_sha256: str
    pending_source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TypeExportResult:
    output: Path
    counts: Counter[str]
    paper_count: int
    pending_source_ids: tuple[str, ...]
    migrated_count: int


def _duplicate_key_guard(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PaperTypeError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_json_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_key_guard)
    except FileNotFoundError:
        raise PaperTypeError(f"required JSON is missing: {path}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperTypeError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PaperTypeError(f"JSON root must be an object: {path}")
    return value, _sha256_bytes(raw)


def _load_json(path: Path) -> dict[str, Any]:
    return _load_json_snapshot(path)[0]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_text(value: Any, *, limit: int = 32_767) -> str:
    if value is None:
        return ""
    text = _INVALID_EXCEL_XML_RE.sub("", str(value))
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text[:limit]


def load_taxonomy(path: str | Path = DEFAULT_TAXONOMY) -> tuple[dict[str, TaxonomyRow], str]:
    source = Path(path)
    try:
        raw = source.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PaperTypeError(f"unable to read taxonomy: {source}: {exc}") from exc
    rows: dict[str, TaxonomyRow] = {}
    for line in text.splitlines():
        match = _TAXONOMY_ROW_RE.match(line)
        if not match:
            continue
        subtype, subtype_des, description = (item.strip() for item in match.groups())
        if subtype in rows:
            raise PaperTypeError(f"duplicate taxonomy subtype: {subtype}")
        rows[subtype] = TaxonomyRow(subtype, subtype_des, description)
    if tuple(rows) != VALID_SUBTYPES:
        raise PaperTypeError(
            f"taxonomy must contain exactly {VALID_SUBTYPES} in source order; got {tuple(rows)}"
        )
    return rows, _sha256_bytes(raw)


def load_metadata(database: str | Path, source_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(kci_papers)")}
        required = {
            "id",
            "source",
            "source_id",
            "title",
            "title_kor",
            "title_eng",
            "doi",
            "journal_id",
            "institution_id",
            "publication_year",
        }
        if not required <= columns:
            raise PaperTypeError(f"kci_papers is missing columns: {sorted(required - columns)}")
        rows = {
            row["source_id"]: dict(row)
            for row in connection.execute(
                "SELECT id, source, source_id, title, title_kor, title_eng, doi, "
                "journal_id, institution_id, publication_year FROM kci_papers"
            )
            if row["source_id"] in set(source_ids)
        }
        db_count = connection.execute("SELECT COUNT(*) FROM kci_papers").fetchone()[0]
    finally:
        connection.close()
    if set(rows) != set(source_ids):
        missing = sorted(set(source_ids) - set(rows))
        raise PaperTypeError(f"PDF corpus IDs missing from kci_papers: {missing}")
    for source_id, row in rows.items():
        if row["source"] != "kci" or not _ART_ID_RE.fullmatch(source_id):
            raise PaperTypeError(f"invalid KCI identity: {source_id}")
    rows["__db_count__"] = {"count": db_count}
    return rows


def load_structure(
    path: Path, *, pdf_page_count: int | None = None
) -> StructureEvidence:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PaperTypeError(f"unable to read Structure evidence: {path}: {exc}") from exc
    field_matches = _STRUCTURE_FIELD_RE.findall(text)
    summary_match = _STRUCTURE_SUMMARY_RE.search(text)
    evidence_matches = _STRUCTURE_EVIDENCE_RE.findall(text)
    if (
        len(field_matches) != 2
        or len({key for key, _value in field_matches}) != 2
        or not summary_match
        or not evidence_matches
    ):
        raise PaperTypeError(f"incomplete Structure evidence: {path}")
    fields: dict[str, str] = {}
    referenced_ids: set[str] = {summary_match.group(2)}
    for key, raw_value in field_matches:
        match = re.fullmatch(r"(.*?)\s*\[(E-[0-9]+)\]\s*", raw_value)
        if not match or not match.group(1).strip():
            raise PaperTypeError(f"unreferenced Structure field: {path}: {key}")
        fields[key] = match.group(1).strip()
        referenced_ids.add(match.group(2))
    evidence_ids = [item[0] for item in evidence_matches]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise PaperTypeError(f"duplicate Structure evidence ID: {path}")
    if not referenced_ids <= set(evidence_ids):
        raise PaperTypeError(
            f"Structure references missing evidence IDs: {path}: "
            f"{sorted(referenced_ids - set(evidence_ids))}"
        )
    if pdf_page_count is not None:
        for evidence_id, _status, pages, _support in evidence_matches:
            page_numbers = [int(value) for value in re.findall(r"[0-9]+", pages)]
            if (
                not page_numbers
                or min(page_numbers) < 1
                or max(page_numbers) > pdf_page_count
            ):
                raise PaperTypeError(
                    f"Structure evidence page is outside PDF bounds: {path}: "
                    f"{evidence_id}={pages!r}, PDF pages={pdf_page_count}"
                )
    # Prefer the principal E-02 scope evidence; fall back only when a legacy
    # document used a different evidence number.
    selected = next((item for item in evidence_matches if item[0] == "E-02"), evidence_matches[-1])
    evidence_id, _status, pages, support = selected
    research_type = STRUCTURE_RESEARCH_TYPES.get(fields["연구 유형"])
    if research_type is None:
        raise PaperTypeError(
            f"unknown Structure research type {fields['연구 유형']!r}: {path}"
        )
    if fields["포렌식 관련성"] not in {"관련", "부분 관련", "비관련"}:
        raise PaperTypeError(
            f"unknown Structure forensic relevance "
            f"{fields['포렌식 관련성']!r}: {path}"
        )
    return StructureEvidence(
        research_type=research_type,
        forensic_relevance=fields["포렌식 관련성"],
        summary=" ".join(summary_match.group(1).split()),
        evidence_id=evidence_id,
        evidence_pages=" ".join(pages.split()),
        evidence_support=" ".join(support.split()),
        sha256=_sha256_bytes(raw),
    )


PDF_EVIDENCE_CACHE_NAME = ".pdf-evidence-cache.json"


def _load_pdf_evidence_cache(path: Path) -> dict[str, PdfEvidence]:
    """Read the sha256 -> extracted-text evidence cache; corrupt files are ignored."""

    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {}
    entries = payload.get("entries")
    result: dict[str, PdfEvidence] = {}
    if not isinstance(entries, dict):
        return result
    for sha256, value in entries.items():
        try:
            evidence = PdfEvidence(
                page_count=int(value["page_count"]),
                sha256=str(sha256),
                text_extractable=bool(value["text_extractable"]),
                text_char_count=int(value["text_char_count"]),
                text_sha256=str(value["text_sha256"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if re.fullmatch(r"[0-9a-f]{64}", sha256) and re.fullmatch(r"[0-9a-f]{64}", evidence.text_sha256):
            result[sha256] = evidence
    return result


def _save_pdf_evidence_cache(path: Path, entries: Mapping[str, PdfEvidence]) -> None:
    payload = {
        "schema_version": 1,
        "entries": {
            sha256: {
                "page_count": evidence.page_count,
                "text_extractable": evidence.text_extractable,
                "text_char_count": evidence.text_char_count,
                "text_sha256": evidence.text_sha256,
            }
            for sha256, evidence in sorted(entries.items())
        },
    }
    _write_json_atomic(path, payload)


def _extract_pdf_evidence(
    path: Path, *, cache: dict[str, PdfEvidence] | None = None
) -> PdfEvidence:
    """Hash and text-profile one PDF.

    Text extraction dominates the exporter's run time, so results are cached by
    PDF SHA-256 when ``cache`` is given: a byte-identical PDF is never
    re-extracted, while any change to the file misses the cache by construction.
    """

    if path.is_symlink() or not path.is_file():
        raise PaperTypeError(f"PDF must be a regular non-symlink file: {path}")
    with path.open("rb") as stream:
        signature = stream.read(5)
    if signature != b"%PDF-":
        raise PaperTypeError(f"invalid PDF signature: {path}")
    pdf_sha256 = _sha256_file(path)
    if cache is not None and pdf_sha256 in cache:
        return cache[pdf_sha256]
    try:
        # ``strict=False`` matches the project's canonical text extractor and
        # tolerates harmless publisher syntax deviations.  Identity and page
        # integrity are enforced independently by SHA-256 and manifest counts.
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise PaperTypeError(f"encrypted PDF is unsupported: {path}")
        page_texts: list[str] = []
        for page in reader.pages:
            try:
                page_texts.append(page.extract_text() or "")
            except Exception as exc:  # A page-level failure must stay visible.
                raise PaperTypeError(f"PDF text extraction failed: {path}: {exc}") from exc
    except PaperTypeError:
        raise
    except Exception as exc:
        raise PaperTypeError(f"unable to parse PDF: {path}: {exc}") from exc
    normalized = "\n\f\n".join(" ".join(value.split()) for value in page_texts)
    char_count = sum(len("".join(value.split())) for value in page_texts)
    evidence = PdfEvidence(
        page_count=len(page_texts),
        sha256=pdf_sha256,
        text_extractable=char_count >= 500,
        text_char_count=char_count,
        text_sha256=_sha256_bytes(normalized.encode("utf-8")),
    )
    if cache is not None:
        cache[pdf_sha256] = evidence
    return evidence


def _validate_download_manifest(
    path: Path,
    record: PaperFileRecord,
    pdf: PdfEvidence,
) -> None:
    value = _load_json(path)
    paper = value.get("paper")
    document = value.get("pdf")
    if not isinstance(paper, dict) or not isinstance(document, dict):
        raise PaperTypeError(f"malformed download manifest: {path}")
    if paper.get("source_id") != record.source_id:
        raise PaperTypeError(f"download manifest identity mismatch: {path}")
    if not isinstance(paper.get("title"), str) or not paper["title"].strip():
        raise PaperTypeError(f"download manifest has no provenance title: {path}")
    if value.get("status") != "downloaded":
        raise PaperTypeError(f"download manifest is not downloaded: {path}")
    if document.get("path") != record.filename:
        raise PaperTypeError(f"download manifest filename mismatch: {path}")
    if document.get("sha256") != pdf.sha256:
        raise PaperTypeError(f"download manifest PDF hash mismatch: {path}")
    if document.get("page_count") != pdf.page_count:
        raise PaperTypeError(f"download manifest page-count mismatch: {path}")


def _validate_lv3_manifest(
    value: Mapping[str, Any],
    *,
    path: Path,
    record: PaperFileRecord,
    paper_index: int | None,
    pdf: PdfEvidence,
    structure: StructureEvidence,
) -> int:
    """Validate the Level-3 manifest and return its ``paper_index``.

    ``paper_index`` pins an expected value; ``None`` accepts the manifest's own
    positive integer, which the corpus loader then checks for contiguity.
    """

    expected_keys = {
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
    if set(value) != expected_keys or value.get("schema_version") != "1.0":
        raise PaperTypeError(f"unexpected Lv3 evidence schema: {path}")
    expected_status = (
        "non_forensic"
        if structure.forensic_relevance == "비관련"
        else "classified"
    )
    manifest_index = value.get("paper_index")
    if type(manifest_index) is not int or manifest_index < 1:
        raise PaperTypeError(f"Lv3 evidence has no positive paper_index: {path}")
    if (
        value.get("source_id") != record.source_id
        or (paper_index is not None and manifest_index != paper_index)
        or value.get("title") != record.title
        or value.get("pdf_sha256") != pdf.sha256
        or value.get("structure_sha256") != structure.sha256
        or value.get("research_type") != structure.research_type
        or value.get("forensic_relevance") != structure.forensic_relevance
        or value.get("classification_status") != expected_status
    ):
        raise PaperTypeError(f"stale or inconsistent Lv3 evidence: {path}")
    for hash_field in ("taxonomy_sha256", "intelligence_sha256"):
        if not isinstance(value.get(hash_field), str) or not re.fullmatch(
            r"[0-9a-f]{64}", value[hash_field]
        ):
            raise PaperTypeError(f"invalid Lv3 {hash_field}: {path}")
    mappings = value.get("mappings")
    if not isinstance(mappings, list):
        raise PaperTypeError(f"Lv3 mappings must be a list: {path}")
    if expected_status == "classified" and not mappings:
        raise PaperTypeError(f"classified Lv3 evidence has no mapping: {path}")
    if expected_status == "non_forensic" and (
        mappings
        or not isinstance(value.get("not_applicable_reason"), str)
        or not value["not_applicable_reason"].strip()
    ):
        raise PaperTypeError(f"invalid non-forensic Lv3 evidence: {path}")
    review = value.get("review")
    if not isinstance(review, dict) or set(review) != {
        "status",
        "reviewed_on",
        "reviewer",
    }:
        raise PaperTypeError(f"invalid Lv3 review block: {path}")
    try:
        reviewed_on = date.fromisoformat(str(review["reviewed_on"]))
    except ValueError:
        raise PaperTypeError(f"invalid Lv3 review date: {path}") from None
    if (
        review["status"] != "approved"
        or reviewed_on > date.today()
        or not isinstance(review["reviewer"], str)
        or not review["reviewer"].strip()
    ):
        raise PaperTypeError(f"unapproved Lv3 evidence: {path}")
    return manifest_index


def _corpus_digest(papers: Sequence[CorpusPaper]) -> str:
    return _sha256_bytes(
        "\n".join(
            f"{paper.paper_index}\0{paper.file_record.source_id}\0{paper.pdf.sha256}"
            f"\0{paper.structure.sha256}\0{paper.lv3_sha256}"
            for paper in papers
        ).encode("utf-8")
    )


def load_corpus(
    *,
    database: Path,
    paper_index: Path,
    papers_dir: Path,
    download_manifest_dir: Path,
    structure_dir: Path,
    lv3_dir: Path,
    progress: ProgressLogger | None = None,
    strict: bool = False,
) -> CorpusLoad:
    """Load every indexed PDF that carries complete Structure and Level-3 evidence.

    Indexed PDFs whose Korean Structure document or Level-3 manifest does not
    exist yet are *pending analysis*: they are listed in the result instead of
    failing the export, unless ``strict`` is set.  Any evidence file that does
    exist must validate; a half-written or stale one still fails closed.
    """

    records_by_id = load_paper_file_index(paper_index)
    records = list(records_by_id.values())
    if not records:
        raise PaperTypeError("the PDF index contains no papers")
    pending: list[str] = []
    analyzed: list[PaperFileRecord] = []
    for record in records:
        has_structure = (structure_dir / f"{record.source_id}.md").is_file()
        has_lv3 = (lv3_dir / f"{record.source_id}.json").is_file()
        if has_structure and has_lv3:
            analyzed.append(record)
        elif has_structure or has_lv3:
            raise PaperTypeError(
                f"incomplete evidence for {record.source_id}: "
                f"structure={has_structure}, lv3={has_lv3}"
            )
        else:
            pending.append(record.source_id)
    if strict and pending:
        raise PaperTypeError(
            f"{len(pending)} indexed PDFs have no analysis yet: {pending[:10]}"
        )
    if not analyzed:
        raise PaperTypeError("no indexed PDF carries Structure and Level-3 evidence")
    records = analyzed
    source_ids = [record.source_id for record in records]
    metadata = load_metadata(database, source_ids)
    db_count = int(metadata.pop("__db_count__")["count"])
    papers: list[CorpusPaper] = []
    cache_path = download_manifest_dir / PDF_EVIDENCE_CACHE_NAME
    evidence_cache = _load_pdf_evidence_cache(cache_path)
    cache_size_before = len(evidence_cache)
    for position, record in enumerate(records, start=1):
        row = metadata[record.source_id]
        # The database was refreshed after the title-based PDF rename.  Some
        # rows now contain spacing, subtitle, or Korean/English representative
        # title changes.  Source ID is the identity key; an exact title join
        # would incorrectly lose valid papers.  The immutable file index title
        # remains the canonical label for the corresponding PDF.
        pdf_path = papers_dir / record.filename
        pdf = _extract_pdf_evidence(pdf_path, cache=evidence_cache)
        _validate_download_manifest(
            download_manifest_dir / f"{record.source_id}.download.json", record, pdf
        )
        structure = load_structure(
            structure_dir / f"{record.source_id}.md",
            pdf_page_count=pdf.page_count,
        )
        lv3_path = lv3_dir / f"{record.source_id}.json"
        lv3, lv3_sha256 = _load_json_snapshot(lv3_path)
        paper_index_value = _validate_lv3_manifest(
            lv3,
            path=lv3_path,
            record=record,
            paper_index=None,
            pdf=pdf,
            structure=structure,
        )
        papers.append(
            CorpusPaper(
                paper_index=paper_index_value,
                file_record=record,
                metadata=row,
                structure=structure,
                pdf=pdf,
                lv3_sha256=lv3_sha256,
            )
        )
        if progress and (
            position == 1
            or position % 25 == 0
            or position == len(records)
        ):
            progress.emit(
                "PAPER-TYPE",
                "PDF·manifest·Structure 근거 검증",
                current=position,
                total=len(records),
                counters={
                    "text": sum(item.pdf.text_extractable for item in papers),
                    "image_only": sum(not item.pdf.text_extractable for item in papers),
                },
            )
    if len(evidence_cache) != cache_size_before:
        try:
            _save_pdf_evidence_cache(cache_path, evidence_cache)
        except OSError:
            pass  # the cache is an optimization only
    papers.sort(key=lambda paper: paper.paper_index)
    indexes = [paper.paper_index for paper in papers]
    # Analyzer/numbering.py keeps the numbers sorted and without gaps; between a removal and the
    # next renumbering the sequence has gaps, so here it must only be unique and positive.
    if len(set(indexes)) != len(indexes) or any(value < 1 for value in indexes):
        raise PaperTypeError(
            "Lv3 paper_index values are not unique across the analyzed corpus: "
            f"count={len(papers)}, max={indexes[-1] if indexes else 0}, "
            f"duplicates={sorted({value for value in indexes if indexes.count(value) > 1})[:10]}"
        )
    if len({paper.pdf.sha256 for paper in papers}) != len(papers):
        raise PaperTypeError("duplicate PDF content hashes exist in the corpus")
    return CorpusLoad(
        papers=tuple(papers),
        db_count=db_count,
        corpus_sha256=_corpus_digest(papers),
        pending_source_ids=tuple(pending),
    )


def _require_text(value: Any, field: str, source_id: str = "review") -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperTypeError(f"{source_id}: {field} must be non-empty text")
    return " ".join(value.split())


def load_review_manifest(
    path: Path,
    *,
    taxonomy_sha256: str,
    corpus_sha256: str,
    source_ids: Sequence[str],
    taxonomy: Mapping[str, TaxonomyRow],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_key_guard)
    required = {
        "schema_version",
        "taxonomy_sha256",
        "corpus_sha256",
        "reviewed_count",
        "paper_decisions",
        "audit",
        "review",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PaperTypeError(f"unexpected review-manifest schema: {path}")
    if value["schema_version"] != REVIEW_SCHEMA_VERSION:
        raise PaperTypeError(f"unsupported review-manifest version: {path}")
    if value["taxonomy_sha256"] != taxonomy_sha256:
        raise PaperTypeError("review manifest was made for a different taxonomy")
    if value["corpus_sha256"] != corpus_sha256:
        raise PaperTypeError("review manifest was made for different PDF/Structure hashes")
    if type(value["reviewed_count"]) is not int or value["reviewed_count"] != len(source_ids):
        raise PaperTypeError("reviewed_count must exactly match the corpus")
    audit = value["audit"]
    audit_keys = {
        "full_corpus_reviewed",
        "artifact_identity_priority",
        "decision_policy",
        "candidate_discovery",
        "cross_review",
        "image_only_review",
        "boundary_candidate_source_ids",
        "zero_result_subtypes",
    }
    if not isinstance(audit, dict) or set(audit) != audit_keys:
        raise PaperTypeError("invalid full-corpus audit block")
    if audit.get("full_corpus_reviewed") is not True:
        raise PaperTypeError("review manifest must attest a full-corpus review")
    if audit.get("artifact_identity_priority") != list(ARTIFACT_PRIORITY):
        raise PaperTypeError("review manifest uses a different decision priority")
    review = value["review"]
    if not isinstance(review, dict) or set(review) != {"status", "reviewed_on", "reviewer"}:
        raise PaperTypeError("invalid review approval block")
    try:
        reviewed_on = date.fromisoformat(str(review["reviewed_on"]))
    except ValueError:
        raise PaperTypeError("review manifest has an invalid calendar date") from None
    if review["status"] != "approved" or reviewed_on > date.today():
        raise PaperTypeError("review manifest is not approved or is future-dated")
    _require_text(review["reviewer"], "reviewer")
    for field in (
        "decision_policy",
        "candidate_discovery",
        "cross_review",
        "image_only_review",
    ):
        _require_text(audit[field], f"audit.{field}")
    zero_result_subtypes = audit["zero_result_subtypes"]
    if (
        not isinstance(zero_result_subtypes, list)
        or len(set(zero_result_subtypes)) != len(zero_result_subtypes)
        or any(code not in taxonomy for code in zero_result_subtypes)
    ):
        raise PaperTypeError("audit.zero_result_subtypes is invalid")
    decisions = value["paper_decisions"]
    if not isinstance(decisions, dict) or list(decisions) != list(source_ids):
        raise PaperTypeError(
            "paper_decisions must contain every source ID exactly once in corpus order"
        )
    boundary_ids = audit.get("boundary_candidate_source_ids")
    if (
        not isinstance(boundary_ids, list)
        or len(set(boundary_ids)) != len(boundary_ids)
        or any(source_id not in source_ids for source_id in boundary_ids)
    ):
        raise PaperTypeError("audit.boundary_candidate_source_ids is invalid")
    decision_keys = {
        "subtype",
        "confidence",
        "boundary_candidate",
        "rationale",
        "boundary_reason",
    }
    for source_id, decision in decisions.items():
        if not isinstance(decision, dict) or set(decision) != decision_keys:
            raise PaperTypeError(f"{source_id}: malformed paper decision")
        subtype = decision["subtype"]
        if subtype not in taxonomy:
            raise PaperTypeError(f"{source_id}: unknown subtype {subtype!r}")
        confidence = decision["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.8 <= float(confidence) <= 1.0:
            raise PaperTypeError(f"{source_id}: invalid confidence")
        if type(decision["boundary_candidate"]) is not bool:
            raise PaperTypeError(f"{source_id}: boundary_candidate must be boolean")
        for key in decision_keys - {"subtype", "confidence", "boundary_candidate"}:
            _require_text(decision[key], key, source_id)
    flagged = [
        source_id
        for source_id in source_ids
        if decisions[source_id]["boundary_candidate"] is True
    ]
    if boundary_ids != flagged:
        raise PaperTypeError(
            "audit.boundary_candidate_source_ids must match per-paper flags in corpus order"
        )
    if any(
        decision["subtype"] != "ar" and decision["boundary_candidate"] is not True
        for decision in decisions.values()
    ):
        raise PaperTypeError("every non-ar decision must be flagged for boundary review")
    actual_zero = [
        code
        for code in taxonomy
        if not any(decision["subtype"] == code for decision in decisions.values())
    ]
    if zero_result_subtypes != actual_zero:
        raise PaperTypeError("audit.zero_result_subtypes does not match decisions")
    return decisions, audit, review, _sha256_bytes(raw)


def _decision_from_mapping(
    value: Mapping[str, Any],
    *,
    source_id: str,
    taxonomy: Mapping[str, TaxonomyRow],
    file_sha256: str,
    path: Path | str,
) -> TypeDecision:
    subtype = value.get("subtype")
    if subtype not in taxonomy:
        raise PaperTypeError(f"{source_id}: unknown subtype {subtype!r} ({path})")
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.8 <= float(confidence) <= 1.0
    ):
        raise PaperTypeError(f"{source_id}: invalid confidence ({path})")
    boundary = value.get("boundary_candidate")
    if type(boundary) is not bool:
        raise PaperTypeError(f"{source_id}: boundary_candidate must be boolean ({path})")
    if subtype != "ar" and boundary is not True:
        raise PaperTypeError(f"{source_id}: non-ar decision must be flagged for boundary review")
    review = value.get("review")
    if not isinstance(review, dict) or set(review) != {"status", "reviewed_on", "reviewer"}:
        raise PaperTypeError(f"{source_id}: invalid review block ({path})")
    try:
        reviewed_on = date.fromisoformat(str(review["reviewed_on"]))
    except ValueError:
        raise PaperTypeError(f"{source_id}: invalid reviewed_on date ({path})") from None
    if review["status"] != "approved" or reviewed_on > date.today():
        raise PaperTypeError(f"{source_id}: decision is not approved or is future-dated")
    return TypeDecision(
        source_id=source_id,
        subtype=str(subtype),
        confidence=float(confidence),
        boundary_candidate=boundary,
        rationale=_require_text(value.get("rationale"), "rationale", source_id),
        boundary_reason=_require_text(value.get("boundary_reason"), "boundary_reason", source_id),
        review_status="approved",
        reviewed_on=reviewed_on.isoformat(),
        reviewer=_require_text(review.get("reviewer"), "reviewer", source_id),
        file_sha256=file_sha256,
    )


def load_type_decisions(
    decision_dir: Path,
    papers: Sequence[CorpusPaper],
    *,
    taxonomy: Mapping[str, TaxonomyRow],
    taxonomy_sha256: str,
) -> dict[str, TypeDecision]:
    """Load one reviewed decision file per corpus paper, bound to its evidence.

    Each ``Analyzer/Manifest/Type/<ART>.json`` pins the PDF, Korean Structure
    document, Level-3 manifest and type taxonomy hashes it was decided against,
    so a decision silently outliving a changed input fails closed exactly like
    the former monolithic manifest did for the whole corpus.
    """

    decisions: dict[str, TypeDecision] = {}
    for paper in papers:
        source_id = paper.file_record.source_id
        path = decision_dir / f"{source_id}.json"
        if not path.is_file():
            raise PaperTypeError(f"type decision is missing: {path}")
        value, file_sha256 = _load_json_snapshot(path)
        if set(value) != DECISION_KEYS:
            raise PaperTypeError(f"unexpected type-decision schema: {path}")
        if value.get("schema_version") != DECISION_SCHEMA_VERSION:
            raise PaperTypeError(f"unsupported type-decision version: {path}")
        if value.get("source_id") != source_id:
            raise PaperTypeError(f"type decision identity mismatch: {path}")
        if value.get("taxonomy_sha256") != taxonomy_sha256:
            raise PaperTypeError(f"{source_id}: decision was made for a different type taxonomy")
        if (
            value.get("pdf_sha256") != paper.pdf.sha256
            or value.get("structure_sha256") != paper.structure.sha256
            or value.get("lv3_manifest_sha256") != paper.lv3_sha256
        ):
            raise PaperTypeError(
                f"{source_id}: decision was made for different PDF/Structure/Lv3 evidence"
            )
        decisions[source_id] = _decision_from_mapping(
            value, source_id=source_id, taxonomy=taxonomy, file_sha256=file_sha256, path=path
        )
    return decisions


def migrate_review_manifest(
    review_path: Path,
    papers: Sequence[CorpusPaper],
    decision_dir: Path,
    *,
    taxonomy: Mapping[str, TaxonomyRow],
    taxonomy_sha256: str,
) -> int:
    """Split the monolithic review manifest into per-paper decision files.

    The legacy manifest is validated exactly as before against the papers it
    reviewed (its own ``paper_decisions`` order and the current evidence
    hashes), then each decision is written once with those hashes pinned.  The
    audit narrative and approval block are preserved next to the decisions.
    Existing decision files are never overwritten.  Returns the number written.
    """

    raw = json.loads(review_path.read_text(encoding="utf-8"), object_pairs_hook=_duplicate_key_guard)
    reviewed_ids = list(raw.get("paper_decisions", {})) if isinstance(raw, dict) else []
    by_id = {paper.file_record.source_id: paper for paper in papers}
    missing = [source_id for source_id in reviewed_ids if source_id not in by_id]
    if missing:
        raise PaperTypeError(
            f"legacy review manifest covers papers absent from the corpus: {missing[:10]}"
        )
    reviewed = [by_id[source_id] for source_id in reviewed_ids]
    decisions, audit, review, review_sha256 = load_review_manifest(
        review_path,
        taxonomy_sha256=taxonomy_sha256,
        corpus_sha256=_corpus_digest(reviewed),
        source_ids=reviewed_ids,
        taxonomy=taxonomy,
    )
    decision_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for paper in reviewed:
        source_id = paper.file_record.source_id
        target = decision_dir / f"{source_id}.json"
        if target.exists():
            continue
        decision = decisions[source_id]
        payload = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "source_id": source_id,
            "subtype": decision["subtype"],
            "confidence": decision["confidence"],
            "boundary_candidate": decision["boundary_candidate"],
            "rationale": decision["rationale"],
            "boundary_reason": decision["boundary_reason"],
            "pdf_sha256": paper.pdf.sha256,
            "structure_sha256": paper.structure.sha256,
            "lv3_manifest_sha256": paper.lv3_sha256,
            "taxonomy_sha256": taxonomy_sha256,
            "review": dict(review),
        }
        _write_json_atomic(target, payload)
        written += 1
    legacy_record = decision_dir / LEGACY_REVIEW_RECORD_NAME
    if not legacy_record.exists():
        _write_json_atomic(
            legacy_record,
            {
                "schema_version": DECISION_SCHEMA_VERSION,
                "source": review_path.name,
                "review_manifest_sha256": review_sha256,
                "corpus_sha256": raw.get("corpus_sha256"),
                "reviewed_count": len(reviewed_ids),
                "reviewed_source_ids": reviewed_ids,
                "audit": audit,
                "review": review,
                "migrated_on": date.today().isoformat(),
            },
        )
    return written


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".json", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_legacy_review_record(decision_dir: Path) -> dict[str, Any] | None:
    path = decision_dir / LEGACY_REVIEW_RECORD_NAME
    if not path.is_file():
        return None
    value = _load_json(path)
    if not isinstance(value.get("audit"), dict) or not isinstance(value.get("review"), dict):
        raise PaperTypeError(f"invalid legacy review record: {path}")
    return value


def build_audit(
    papers: Sequence[CorpusPaper],
    decisions: Mapping[str, TypeDecision],
    *,
    taxonomy: Mapping[str, TaxonomyRow],
    pending_source_ids: Sequence[str],
    legacy_record: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Describe how the current corpus was reviewed, mixing legacy and automated."""

    legacy_ids = set(legacy_record.get("reviewed_source_ids", [])) if legacy_record else set()
    legacy_audit = legacy_record.get("audit", {}) if legacy_record else {}
    ordered_ids = [paper.file_record.source_id for paper in papers]
    legacy_count = sum(source_id in legacy_ids for source_id in ordered_ids)
    automated = [
        decisions[source_id].reviewer
        for source_id in ordered_ids
        if source_id not in legacy_ids
    ]
    reviewers = sorted(set(automated))
    automated_note = (
        f"{len(automated)}편은 {', '.join(reviewers)}가 PDF 원문·Structure·Lv3 근거로 "
        "동일한 우선순위 규칙을 적용해 자동 판정했다."
        if automated
        else "자동 판정 논문 없음."
    )
    boundary_ids = [
        source_id for source_id in ordered_ids if decisions[source_id].boundary_candidate
    ]
    zero_result = [
        code for code in taxonomy
        if not any(decision.subtype == code for decision in decisions.values())
    ]
    return {
        "full_corpus_reviewed": not pending_source_ids,
        "artifact_identity_priority": list(ARTIFACT_PRIORITY),
        "decision_policy": legacy_audit.get("decision_policy") or DEFAULT_DECISION_POLICY,
        "candidate_discovery": " ".join(
            part for part in (
                f"[legacy {legacy_count}편] {legacy_audit.get('candidate_discovery', '')}".strip()
                if legacy_count else "",
                f"[automated] {automated_note}",
            ) if part
        ),
        "cross_review": " ".join(
            part for part in (
                f"[legacy] {legacy_audit.get('cross_review', '')}".strip() if legacy_count else "",
                "[automated] 자동 판정은 re/sh·비-ar 결정과 REVIEW_SURVEY·이미지형 PDF를 "
                "boundary 후보로 강제 표시하며 사람 교차검토 대상으로 남긴다.",
            ) if part
        ),
        "image_only_review": " ".join(
            part for part in (
                f"[legacy] {legacy_audit.get('image_only_review', '')}".strip() if legacy_count else "",
                "[automated] 텍스트 계층이 없는 PDF는 boundary 후보로 표시된다.",
            ) if part
        ),
        "boundary_candidate_source_ids": boundary_ids,
        "zero_result_subtypes": zero_result,
        "legacy_reviewed_count": legacy_count,
        "automated_reviewed_count": len(automated),
        "pending_analysis_count": len(pending_source_ids),
        "pending_source_ids": list(pending_source_ids),
    }


def _normalize_doi(value: Any) -> str:
    if not value:
        return ""
    try:
        return normalize_doi(str(value)) or ""
    except Exception:
        return str(value).strip()


def build_rows(
    papers: Sequence[CorpusPaper],
    *,
    taxonomy: Mapping[str, TaxonomyRow],
    taxonomy_sha256: str,
    decisions: Mapping[str, TypeDecision | Mapping[str, Any]],
    review: Mapping[str, Any] | None = None,
    review_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Build the main-sheet rows.

    ``decisions`` holds per-paper :class:`TypeDecision` objects, each carrying
    its own review block and file hash.  Plain mappings (the legacy monolithic
    manifest shape) are still accepted together with ``review`` and
    ``review_sha256`` for that shared approval.
    """

    rows: list[dict[str, Any]] = []
    for paper in papers:
        source_id = paper.file_record.source_id
        decision = decisions[source_id]
        if isinstance(decision, TypeDecision):
            explicit: Mapping[str, Any] = decision.as_mapping
            paper_review = {
                "status": decision.review_status,
                "reviewed_on": decision.reviewed_on,
                "reviewer": decision.reviewer,
            }
            paper_review_sha256 = decision.file_sha256
        else:
            if review is None or review_sha256 is None:
                raise PaperTypeError("legacy decisions require a shared review block")
            explicit = decision
            paper_review = review
            paper_review_sha256 = review_sha256
        subtype = str(explicit["subtype"])
        confidence = float(explicit["confidence"])
        boundary_candidate = bool(explicit["boundary_candidate"])
        artifact_form = "정규 학술지 논문"
        primary_purpose = paper.structure.summary
        if subtype == "re":
            primary_contribution = "기존 문헌·판례·기술 지식의 종합·비교와 연구 공백 도출"
        elif subtype == "sh":
            primary_contribution = "제한된 범위의 짧은 문헌·기술 동향 종합"
        elif subtype == "dp":
            primary_contribution = "재사용 가능한 데이터셋의 구조·품질·접근 방법 설명"
        elif subtype == "cp":
            primary_contribution = "학술대회 발표 또는 proceedings 산출물"
            artifact_form = "학술대회 산출물"
        elif subtype == "di":
            primary_contribution = "학위 수여를 위해 제출된 연구 산출물"
            artifact_form = "학위논문"
        else:
            type_label = RESEARCH_TYPE_LABELS.get(
                paper.structure.research_type, paper.structure.research_type
            )
            primary_contribution = f"{type_label}에 해당하는 독자적 학술 기여"
        rationale = str(explicit["rationale"])
        boundary_reason = str(explicit["boundary_reason"])
        evidence_pages = paper.structure.evidence_pages
        evidence_summary = paper.structure.evidence_support
        evidence_source = "PDF 원문 + 명시적 경계심사"
        taxon = taxonomy[subtype]
        meta = paper.metadata
        rows.append(
            {
                "paper_index": paper.paper_index,
                "database_id": meta.get("id"),
                "source": meta.get("source"),
                "source_id": source_id,
                "title": paper.file_record.title,
                "title_kor": meta.get("title_kor"),
                "title_eng": meta.get("title_eng"),
                "doi": _normalize_doi(meta.get("doi")),
                "journal_id": meta.get("journal_id"),
                "institution_id": meta.get("institution_id"),
                "publication_year": meta.get("publication_year"),
                "subtype": subtype,
                "subtype_des": taxon.subtype_des,
                "subtype_description_ko": taxon.description_ko,
                "classification_status": "classified",
                "confidence": confidence,
                "boundary_candidate": "Y" if boundary_candidate else "N",
                "artifact_form": artifact_form,
                "primary_purpose": primary_purpose,
                "primary_contribution": primary_contribution,
                "classification_rationale": rationale,
                "boundary_reason": boundary_reason,
                "evidence_source": evidence_source,
                "evidence_pages": evidence_pages,
                "evidence_summary": evidence_summary,
                "research_type": paper.structure.research_type,
                "forensic_relevance": paper.structure.forensic_relevance,
                "pdf_filename": paper.file_record.filename,
                "pdf_page_count": paper.pdf.page_count,
                "pdf_sha256": paper.pdf.sha256,
                "text_extractable": "Y" if paper.pdf.text_extractable else "N",
                "text_char_count": paper.pdf.text_char_count,
                "text_sha256": paper.pdf.text_sha256,
                "structure_sha256": paper.structure.sha256,
                "lv3_manifest_sha256": paper.lv3_sha256,
                "taxonomy_sha256": taxonomy_sha256,
                "review_manifest_sha256": paper_review_sha256,
                "review_status": paper_review["status"],
                "reviewed_on": paper_review["reviewed_on"],
                "reviewer": paper_review["reviewer"],
            }
        )
    return rows


def _append_rows(worksheet, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    worksheet.append(list(columns))
    for row in rows:
        worksheet.append([_clean_text(row.get(column)) if isinstance(row.get(column), str) else row.get(column) for column in columns])


def _style_sheet(worksheet, *, freeze: str = "A2", table_name: str | None = None) -> None:
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="D9E2F3")
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=thin)
    worksheet.freeze_panes = freeze
    worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.sheet_view.showGridLines = False
    for column_index, cell in enumerate(worksheet[1], start=1):
        width = PREFERRED_WIDTHS.get(str(cell.value), 28)
        worksheet.column_dimensions[get_column_letter(column_index)].width = width
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    if table_name and worksheet.max_row >= 2:
        table = Table(displayName=table_name, ref=worksheet.dimensions)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        worksheet.add_table(table)


def build_workbook(
    rows: Sequence[Mapping[str, Any]],
    *,
    taxonomy: Mapping[str, TaxonomyRow],
    taxonomy_sha256: str,
    review_sha256: str,
    corpus_sha256: str,
    db_count: int,
    review_manifest: Mapping[str, Any],
) -> Workbook:
    workbook = Workbook()
    main = workbook.active
    main.title = "논문 유형 목록"
    _append_rows(main, MAIN_COLUMNS, rows)
    _style_sheet(main, table_name="PaperTypeList")

    counts = Counter(str(row["subtype"]) for row in rows)
    summary = workbook.create_sheet("유형 요약")
    summary_columns = ["subtype", "subtype_des", "설명", "논문 수", "비율"]
    summary_rows = [
        {
            "subtype": code,
            "subtype_des": taxonomy[code].subtype_des,
            "설명": taxonomy[code].description_ko,
            "논문 수": counts[code],
            "비율": counts[code] / len(rows),
        }
        for code in VALID_SUBTYPES
    ]
    _append_rows(summary, summary_columns, summary_rows)
    for cell in summary["E"][1:]:
        cell.number_format = "0.00%"
    _style_sheet(summary, table_name="PaperTypeSummary")

    taxonomy_sheet = workbook.create_sheet("분류 기준")
    taxonomy_columns = ["우선순위", "subtype", "subtype_des", "설명", "판정 기준"]
    criteria = {
        "di": "현재 PDF 자체가 학위 수여를 위해 제출된 학위논문",
        "cp": "현재 PDF 자체가 proceedings·학술대회 발표 논문 또는 행사 소개글",
        "dp": "재사용 가능한 데이터셋의 설명·구조·품질·접근법이 주된 기여",
        "re": "기존 문헌·지식의 포괄적 또는 체계적 종합이 주된 기여",
        "sh": "명시적 mini/short survey 등 범위와 종합 깊이가 제한된 짧은 리뷰",
        "ar": "앞의 다섯 유형을 배제한 일반 원저·논증·방법·도구·사례 연구",
    }
    priority = {"di": 1, "cp": 2, "dp": 3, "re": 4, "sh": 4, "ar": 5}
    taxonomy_rows = [
        {
            "우선순위": priority[code],
            "subtype": code,
            "subtype_des": taxonomy[code].subtype_des,
            "설명": taxonomy[code].description_ko,
            "판정 기준": criteria[code],
        }
        for code in ("di", "cp", "dp", "re", "sh", "ar")
    ]
    _append_rows(taxonomy_sheet, taxonomy_columns, taxonomy_rows)
    _style_sheet(taxonomy_sheet, table_name="PaperTypeTaxonomy")

    validation = workbook.create_sheet("검증")
    validation_columns = ["검증 항목", "결과", "값"]
    validation_rows = [
        {"검증 항목": "PDF 인덱스 행 수", "결과": "PASS", "값": len(rows)},
        {"검증 항목": "분류 완료 행 수", "결과": "PASS", "값": sum(row["classification_status"] == "classified" for row in rows)},
        {"검증 항목": "source_id 유일성", "결과": "PASS", "값": len({row["source_id"] for row in rows})},
        {"검증 항목": "PDF SHA-256 유일성", "결과": "PASS", "값": len({row["pdf_sha256"] for row in rows})},
        {"검증 항목": "텍스트 추출 가능 PDF", "결과": "PASS", "값": sum(row["text_extractable"] == "Y" for row in rows)},
        {"검증 항목": "이미지형/저텍스트 PDF", "결과": "REVIEWED", "값": sum(row["text_extractable"] == "N" for row in rows)},
        {"검증 항목": "DB 전체 행 수", "결과": "INFO", "값": db_count},
        {"검증 항목": "PDF 없는 DB 행 수", "결과": "INFO", "값": db_count - len(rows)},
        {"검증 항목": "taxonomy_sha256", "결과": "PASS", "값": taxonomy_sha256},
        {"검증 항목": "corpus_sha256", "결과": "PASS", "값": corpus_sha256},
        {"검증 항목": "review_manifest_sha256", "결과": "PASS", "값": review_sha256},
        {"검증 항목": "report_schema_version", "결과": "PASS", "값": REPORT_SCHEMA_VERSION},
        {"검증 항목": "생성일", "결과": "INFO", "값": date.today().isoformat()},
    ]
    _append_rows(validation, validation_columns, validation_rows)
    _style_sheet(validation, table_name="PaperTypeValidation")

    audit = workbook.create_sheet("심사 기록")
    audit_columns = ["항목", "값"]
    audit_rows = [
        {"항목": "full_corpus_reviewed", "값": review_manifest.get("full_corpus_reviewed")},
        {"항목": "artifact_identity_priority", "값": " > ".join(review_manifest.get("artifact_identity_priority", []))},
        {"항목": "decision_policy", "값": review_manifest.get("decision_policy", "")},
        {"항목": "candidate_discovery", "값": review_manifest.get("candidate_discovery", "")},
        {"항목": "cross_review", "값": review_manifest.get("cross_review", "")},
        {"항목": "image_only_review", "값": review_manifest.get("image_only_review", "")},
        {
            "항목": "boundary_candidate_count",
            "값": len(review_manifest.get("boundary_candidate_source_ids", [])),
        },
        {
            "항목": "zero_result_subtypes",
            "값": ", ".join(review_manifest.get("zero_result_subtypes", [])),
        },
    ]
    for key in ("legacy_reviewed_count", "automated_reviewed_count", "pending_analysis_count"):
        if key in review_manifest:
            audit_rows.append({"항목": key, "값": review_manifest[key]})
    if review_manifest.get("pending_source_ids"):
        audit_rows.append(
            {"항목": "pending_source_ids", "값": ", ".join(review_manifest["pending_source_ids"])}
        )
    _append_rows(audit, audit_columns, audit_rows)
    _style_sheet(audit, table_name="PaperTypeAudit")
    return workbook


def _workbook_content_digest(workbook: Workbook) -> str:
    def canonical_cell_value(value: Any) -> tuple[str, str]:
        # Excel/openpyxl stores an explicitly written empty string as an empty
        # cell.  Treat both representations identically so that the digest
        # measures workbook content instead of an in-memory serialization
        # detail.
        if value is None or value == "":
            return ("none", "")
        if isinstance(value, bool):
            return ("bool", "1" if value else "0")
        if isinstance(value, (date, datetime)):
            return (type(value).__name__, value.isoformat())
        if isinstance(value, (int, float)):
            # XLSX stores both Python ints and floats as Excel numeric cells;
            # integer-valued floats can therefore reopen as ``int``.  Match
            # openpyxl's 16-significant-digit XLSX serialization so harmless
            # binary-float round trips do not look like corruption.
            number = float(value)
            if not math.isfinite(number):
                return ("none", "")
            return ("number", format(number, ".16g"))
        return ("str", str(value))

    digest = hashlib.sha256()
    for worksheet in workbook.worksheets:
        digest.update(f"S\0{worksheet.title}\0{worksheet.max_row}\0{worksheet.max_column}\n".encode("utf-8"))
        for row in worksheet.iter_rows():
            for cell in row:
                value_type, value = canonical_cell_value(cell.value)
                digest.update(
                    f"{cell.coordinate}\0{value_type}\0{value}\n".encode("utf-8")
                )
        for table in sorted(
            worksheet.tables.values(), key=lambda value: value.displayName
        ):
            digest.update(
                f"T\0{table.displayName}\0{table.ref}\n".encode("utf-8")
            )
        digest.update(f"F\0{worksheet.freeze_panes!s}\n".encode("utf-8"))
    return digest.hexdigest()


def _validate_workbook(
    path: Path,
    expected_rows: Sequence[Mapping[str, Any]],
    expected_content_digest: str,
) -> None:
    workbook = load_workbook(path, read_only=False, data_only=False)
    try:
        expected_sheets = ["논문 유형 목록", "유형 요약", "분류 기준", "검증", "심사 기록"]
        if workbook.sheetnames != expected_sheets:
            raise PaperTypeError(f"workbook sheet mismatch: {workbook.sheetnames}")
        if _workbook_content_digest(workbook) != expected_content_digest:
            raise PaperTypeError("workbook content changed during save or was corrupted")
        worksheet = workbook["논문 유형 목록"]
        headers = [cell.value for cell in worksheet[1]]
        if headers != MAIN_COLUMNS or worksheet.max_row != len(expected_rows) + 1:
            raise PaperTypeError("workbook main-sheet dimensions mismatch")
        source_column = headers.index("source_id") + 1
        subtype_column = headers.index("subtype") + 1
        actual_ids = [worksheet.cell(row, source_column).value for row in range(2, worksheet.max_row + 1)]
        actual_subtypes = [worksheet.cell(row, subtype_column).value for row in range(2, worksheet.max_row + 1)]
        if actual_ids != [row["source_id"] for row in expected_rows]:
            raise PaperTypeError("workbook source-id order mismatch")
        if actual_subtypes != [row["subtype"] for row in expected_rows]:
            raise PaperTypeError("workbook subtype values mismatch")
        formulas = 0
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formulas += 1
        if formulas:
            raise PaperTypeError(f"workbook contains {formulas} formula cells")
    finally:
        workbook.close()


def _atomic_save(workbook: Workbook, destination: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp.xlsx", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        expected_content_digest = _workbook_content_digest(workbook)
        workbook.save(temporary)
        _validate_workbook(temporary, rows, expected_content_digest)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def export_type_list(
    *,
    database: Path = DEFAULT_DATABASE,
    taxonomy_path: Path = DEFAULT_TAXONOMY,
    review_path: Path = DEFAULT_REVIEW,
    output: Path = DEFAULT_OUTPUT,
    paper_index: Path = DEFAULT_PAPER_INDEX,
    papers_dir: Path = DEFAULT_PAPERS_DIR,
    download_manifest_dir: Path = DEFAULT_DOWNLOAD_MANIFEST_DIR,
    structure_dir: Path = DEFAULT_STRUCTURE_DIR,
    lv3_dir: Path = DEFAULT_LV3_DIR,
    decision_dir: Path = DEFAULT_DECISION_DIR,
    progress: ProgressLogger | None = None,
    strict: bool = False,
    migrate_legacy_review: bool = True,
) -> TypeExportResult:
    """Validate the analyzed corpus, load per-paper decisions, export the list.

    When the per-paper decision directory lacks papers that the legacy
    monolithic ``review_path`` manifest covers, that manifest is validated and
    split into decision files first (``migrate_legacy_review``).
    """

    progress = progress or ProgressLogger()
    progress.emit("PAPER-TYPE", "분류체계와 분석 완료 논문 원문 검증 시작")
    taxonomy, taxonomy_sha256 = load_taxonomy(taxonomy_path)
    loaded = load_corpus(
        database=database,
        paper_index=paper_index,
        papers_dir=papers_dir,
        download_manifest_dir=download_manifest_dir,
        structure_dir=structure_dir,
        lv3_dir=lv3_dir,
        progress=progress,
        strict=strict,
    )
    papers = list(loaded.papers)
    corpus_sha256 = loaded.corpus_sha256
    migrated = 0
    undecided = [
        paper for paper in papers
        if not (decision_dir / f"{paper.file_record.source_id}.json").is_file()
    ]
    if undecided and migrate_legacy_review and Path(review_path).is_file():
        migrated = migrate_review_manifest(
            Path(review_path),
            papers,
            Path(decision_dir),
            taxonomy=taxonomy,
            taxonomy_sha256=taxonomy_sha256,
        )
        progress.emit("PAPER-TYPE", f"레거시 심사 manifest를 논문별 판정 {migrated}건으로 분할")
    decisions = load_type_decisions(
        Path(decision_dir), papers, taxonomy=taxonomy, taxonomy_sha256=taxonomy_sha256
    )
    audit = build_audit(
        papers,
        decisions,
        taxonomy=taxonomy,
        pending_source_ids=loaded.pending_source_ids,
        legacy_record=load_legacy_review_record(Path(decision_dir)),
    )
    decisions_sha256 = _sha256_bytes(
        "\n".join(
            f"{paper.file_record.source_id}\0{decisions[paper.file_record.source_id].file_sha256}"
            for paper in papers
        ).encode("utf-8")
    )
    mandatory_boundary_ids = {
        paper.file_record.source_id
        for paper in papers
        if paper.structure.research_type == "REVIEW_SURVEY"
        or not paper.pdf.text_extractable
    }
    reviewed_boundary_ids = {
        source_id
        for source_id, decision in decisions.items()
        if decision.boundary_candidate is True
    }
    if not mandatory_boundary_ids <= reviewed_boundary_ids:
        raise PaperTypeError(
            "every REVIEW_SURVEY or image-only PDF requires an explicit boundary decision: "
            f"{sorted(mandatory_boundary_ids - reviewed_boundary_ids)}"
        )
    rows = build_rows(
        papers,
        taxonomy=taxonomy,
        taxonomy_sha256=taxonomy_sha256,
        decisions=decisions,
    )
    if len(rows) != len(papers) or len({row["source_id"] for row in rows}) != len(rows):
        raise PaperTypeError("final classification coverage or uniqueness check failed")
    if any(row["subtype"] not in taxonomy for row in rows):
        raise PaperTypeError("final classification contains an unknown subtype")
    workbook = build_workbook(
        rows,
        taxonomy=taxonomy,
        taxonomy_sha256=taxonomy_sha256,
        review_sha256=decisions_sha256,
        corpus_sha256=corpus_sha256,
        db_count=loaded.db_count,
        review_manifest=audit,
    )
    _atomic_save(workbook, output, rows)
    progress.emit(
        "PAPER-TYPE",
        "Excel 저장 및 재개봉 검증 완료",
        current=len(rows),
        total=len(rows),
        counters={"pending": len(loaded.pending_source_ids)},
    )
    return TypeExportResult(
        output=Path(output),
        counts=Counter(str(row["subtype"]) for row in rows),
        paper_count=len(rows),
        pending_source_ids=loaded.pending_source_ids,
        migrated_count=migrated,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--paper-index", type=Path, default=DEFAULT_PAPER_INDEX)
    parser.add_argument("--papers-dir", type=Path, default=DEFAULT_PAPERS_DIR)
    parser.add_argument("--download-manifest-dir", type=Path, default=DEFAULT_DOWNLOAD_MANIFEST_DIR)
    parser.add_argument("--structure-dir", type=Path, default=DEFAULT_STRUCTURE_DIR)
    parser.add_argument("--lv3-dir", type=Path, default=DEFAULT_LV3_DIR)
    parser.add_argument("--decision-dir", type=Path, default=DEFAULT_DECISION_DIR)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when an indexed PDF has no analysis instead of reporting it as pending.",
    )
    parser.add_argument(
        "--no-migrate-legacy-review",
        action="store_true",
        help="Do not split the legacy monolithic review manifest into per-paper decisions.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    arguments = build_parser().parse_args(argv)
    try:
        result = export_type_list(
            database=arguments.database,
            taxonomy_path=arguments.taxonomy,
            review_path=arguments.review,
            output=arguments.output,
            paper_index=arguments.paper_index,
            papers_dir=arguments.papers_dir,
            download_manifest_dir=arguments.download_manifest_dir,
            structure_dir=arguments.structure_dir,
            lv3_dir=arguments.lv3_dir,
            decision_dir=arguments.decision_dir,
            strict=arguments.strict,
            migrate_legacy_review=not arguments.no_migrate_legacy_review,
        )
    except (PaperTypeError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    counts = result.counts
    rendered = ", ".join(f"{code}={counts.get(code, 0)}" for code in VALID_SUBTYPES)
    print(
        f"[DONE] {arguments.output} papers={result.paper_count} ({rendered}) "
        f"pending={len(result.pending_source_ids)} migrated={result.migrated_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
