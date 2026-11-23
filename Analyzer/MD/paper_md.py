"""Generate one Korean and one English Markdown record for every indexed PDF.

The field-level source contract follows the project workbooks named by the
user, with base bibliographic metadata loaded from ``kci_pipeline.db``.  Joins
use KCI ``source_id`` whenever it exists.  The keyword workbook has no source
ID, so it is joined by normalized DOI first and normalized title second, with
a mandatory one-to-one coverage check.  Missing or unconfirmed evidence is
rendered as the literal ``N/A``; it is never guessed.

References come from the KCI ``articleDetail`` responses that the reference
stage keeps verbatim (``Analyzer/Ref/api-response/referenceInfo/<ART>.xml``,
indexed by ``Analyzer/Ref/api-response/manifest.json``).  The ``REF`` cell is
the value KCI delivered: a ``<![CDATA[...]]>`` citation string as is, or the
structured fields joined into one citation.

Run from any directory::

    python3 Analyzer/MD/paper_md.py
    python3 -m Analyzer.MD.paper_md
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openpyxl import load_workbook


MODULE_DIR = Path(__file__).resolve().parent
ANALYZER_DIR = MODULE_DIR.parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from PaperPipeline.db_source import normalize_doi
from PaperPipeline.paper_files import load_paper_file_index

from Analyzer.Ref.ref_collect import (
    InvalidReferenceResponse,
    parse_reference_info_xml,
)
from Analyzer.tag_taxonomy import default_tag_taxonomy


KOREAN_TEMPLATE = MODULE_DIR / "reference-paper-profile.template.kor.md"
ENGLISH_TEMPLATE = MODULE_DIR / "reference-paper-profile.template.eng.md"
PAPER_INDEX = ANALYZER_DIR / "Manifest" / "paper_files.json"
PAPER_DIRECTORY = ANALYZER_DIR / "Paper"
KEYWORD_WORKBOOK = (
    ANALYZER_DIR
    / "Intelligence"
    / "kci_keyword_intelligence_with_app_category.xlsx"
)
OA_WORKBOOK = ANALYZER_DIR / "OA" / "oa_list.xlsx"
TYPE_WORKBOOK = ANALYZER_DIR / "Type" / "type_list.xlsx"
SPECIAL_WORKBOOK = ANALYZER_DIR / "Special-Issue" / "special_list.xlsx"
REFERENCE_XML_DIR = ANALYZER_DIR / "Ref" / "api-response" / "referenceInfo"
REFERENCE_MANIFEST = ANALYZER_DIR / "Ref" / "api-response" / "manifest.json"
LEVEL3_WORKBOOK = (
    ANALYZER_DIR / "Intelligence" / "kci_paper_level3_classification.xlsx"
)
TECHNIQUE_TAXONOMY = ANALYZER_DIR / "LV" / "Techniques" / "tech_taxonomy_v5.2.json"
# Tags and the "4. 대상 애플리케이션" table (with Level-5 ids) come from the Jev AI results
# (chapter III '논문 분류'), prepared by the jev stage (``Analyzer/JevAI/jev_fields.py``).
# The earlier Level-5 stage's ``Analyzer/Application/targets.json`` is no longer read here.
TARGETS_JSON = ANALYZER_DIR / "JevAI" / "targets.json"
LEVEL5_REGISTRY = REPOSITORY_ROOT / "web" / "DF-Insight" / "data" / "techniques"
TARGET_REGISTRY = REPOSITORY_ROOT / "web" / "DF-Insight" / "data" / "targets_md"
DATABASE = REPOSITORY_ROOT / "kci_pipeline.db"
DEFAULT_OUTPUT = ANALYZER_DIR / "Paper" / "MD"
PROFILE_INDEX = ANALYZER_DIR / "Manifest" / "paper_profiles.json"
LANGUAGE_FILES = {"kor": "ko.md", "eng": "en.md"}

# Size of the originally reviewed corpus.  Generation no longer assumes it: the
# corpus is every paper in the type workbook, and indexed PDFs that are still
# awaiting analysis are reported as pending.
LEGACY_PAPER_COUNT = 527
EXPECTED_PAPER_COUNT = LEGACY_PAPER_COUNT
MISSING = "N/A"
DEFAULT_KOREAN_AUTHOR = "조민혁"
DEFAULT_ENGLISH_AUTHOR = "Jo Minhyuk"
SOURCE_ID_RE = re.compile(r"ART[0-9]{9}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

# Public record identity: KCI source IDs are ordered ascending and numbered from
# one on the first export. Persisted identities never change when new papers
# are inserted. Existing REP-DP numeric suffixes are retained at seven digits.
RECORD_ID_PREFIX = "KNOW-DOMP-"
RECORD_ID_DIGITS = 7
RECORD_ID_RE = re.compile(r"KNOW-DOMP-[0-9]{7}\Z")
LEVEL3_ID_RE = re.compile(r"[A-Z]{3,4}-[0-9]{2}-[0-9]{2}\Z")
TARGET_ID_RE = re.compile(r"(?:TGT-[0-9]{4}|TAGT-[0-9]{7})\Z")  # DF-Insight renamed its target ids in 2026-09
# DF-Insight writes the last segment with 2 digits since 2026-10 (3 before); a profile only
# carries ids that are current DF-Insight Level-5 nodes (``_level5_registry_ids``).
LEVEL5_ID_RE = re.compile(r"[A-Z]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2,3}\Z")
STAGE_CODES = {
    "PRE": "PREP", "IDC": "IDEC", "PCP": "PRCP", "COL": "COLL",
    "PSP": "PSCP", "EXM": "EXAM", "ANL": "ANYS", "RPT": "REPT",
    "CMN": "CMNF", "CVG": "CVGF",
}

EXPECTED_TEMPLATE_SHA256 = {
    # 2026-09-28: '발행일 / Published' row (publication month, YYYY-MM); the
    # reference table became ``| ID | REF |`` whose cell is the KCI value.
    # 2026-10-04: the "4. 대상 애플리케이션" example shows a Level-5 id (one row per id).
    # 2026-10-14: DF-Insight's templates as of main 860143294, where one
    # '출판 날짜 / Publication Date | YYYY. MM.' row replaces '출판 연도' and '발행일'.
    # The classification comments keep the v5.2 titles (DF-Insight renamed 78 of
    # them on 2026-10-05; adopting those is a separate taxonomy change).
    KOREAN_TEMPLATE: "5a6d46f0fef85003555f577e22b219f0507597af42c49f91bca8a54ff19f9838",
    ENGLISH_TEMPLATE: "f1407b61d56cc9ec8fe501ba24f70fad9a7806a53d5a14c15842f795974500d9",
}

# Tag vocabulary of the reference-paper-profile templates, read from the
# user-maintained ``Analyzer/Tags/tags_taxonomy.json``.  Each axis is closed: a
# value the workbooks record but this vocabulary cannot express is reported as
# unmapped rather than guessed into an invented tag.  Adding a tag or a member
# value is a change to that JSON file (and to the templates' ``tags`` block).
_TAG_TAXONOMY = default_tag_taxonomy()
ALLOWED_TAGS = _TAG_TAXONOMY.allowed_tags
# The DEV axis covers only PC and mobile, so the workbook's other recorded
# devices (server, drone, IoT, vehicle, camera, ...) have no tag.
DEVICE_TAGS = dict(_TAG_TAXONOMY.device_tags)
# The FS axis names storage families rather than individual file systems, so a
# recorded file system maps to the family it belongs to (case-folded keys).
FILESYSTEM_TAGS = dict(_TAG_TAXONOMY.filesystem_tags)
OS_TAGS = dict(_TAG_TAXONOMY.os_tags)
# An APP tag always comes from the category of a named application, so a paper
# that carries one also lists the concrete application name.
APPLICATION_TAGS = dict(_TAG_TAXONOMY.application_tags)
CLOUD_TAGS = dict(_TAG_TAXONOMY.cloud_tags)
# ``CLOUD-SaaS`` exists only with a service flavour.  The workbooks record the
# delivery model and the application category in separate columns, so a recorded
# SaaS deployment takes its flavour from the paper's own application category.
SAAS_MARKER = "SaaS"
SAAS_TAGS = dict(_TAG_TAXONOMY.saas_tags)

DATABASE_FIELDS = {
    "id",
    "source",
    "source_id",
    "title",
    "title_kor",
    "title_eng",
    "doi",
    "url",
    "abstract_kor",
    "abstract_eng",
    "keywords_json",
    "authors_json",
    "journal_id",
    "institution_id",
    "issue_id",
    "publication_year",
}


class PaperMarkdownError(RuntimeError):
    """Raised when a source contract or generated document is invalid."""


@dataclass(frozen=True, slots=True)
class TypeRecord:
    paper_index: int
    source_id: str
    title: str
    title_kor: str
    title_eng: str
    doi: str | None
    # ``None``: the paper has no type decision yet (analysis pending); its profile shows ``N/A``.
    subtype: str | None
    subtype_des: str | None
    pdf_filename: str
    pdf_sha256: str


@dataclass(frozen=True, slots=True)
class KeywordRecord:
    doi: str | None
    title: str
    target_device: str | None
    target_os: str | None
    target_app: str | None
    target_app_category: str | None
    filesystem: str | None
    cloud: str | None


@dataclass(frozen=True, slots=True)
class Technique:
    rank: int
    level_2: str
    level_3: str
    level_2_en: str
    level_3_en: str


@dataclass(frozen=True, slots=True)
class PaperData:
    record_id: str
    type_record: TypeRecord
    keyword: KeywordRecord
    metadata: Mapping[str, Any]
    # ``None`` when the Special-Issue collector has not confirmed the paper yet.
    is_special_issue: bool | None
    oa_classification: str
    reference_rows: tuple[Mapping[str, Any], ...]
    techniques: tuple[Technique, ...]
    # ``KNOW-DOMP-`` id of every cited work that is itself part of this corpus,
    # keyed by ``reference_order``; references outside the corpus are absent.
    reference_record_ids: Mapping[int, str]
    # Level-3 technique identifiers in the Level result's own rank (Jev: highest probability first).
    classification: tuple[str, ...]
    # Section-4 rows ``(Level-5 id or None, Korean name, English name)``: one row per
    # Level-5 id (the node's title per language), one blank-id row per application
    # without a Level-5 node.  Source: ``TARGETS_JSON`` (Jev AI).
    target_applications: tuple[tuple[str | None, str, str], ...]
    # Front-matter tags (template tag ids, in ``ALLOWED_TAGS`` order).
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CorpusData:
    papers: tuple[PaperData, ...]
    metadata_coverage: int
    oa_unconfirmed: int
    special_issue_count: int
    reference_unavailable_count: int
    non_forensic_count: int
    special_unconfirmed: int = 0
    # Indexed PDFs that get no profile yet: no type decision (analysis pending) and no
    # final classification either.
    pending_source_ids: tuple[str, ...] = ()
    # Papers in ``papers`` without a type decision yet: their classification is final, so
    # they have a profile whose type cell is ``N/A`` until the analysis stage has run.
    untyped_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationSummary:
    paper_count: int
    markdown_count: int
    metadata_coverage: int
    oa_unconfirmed: int
    special_issue_count: int
    reference_unavailable_count: int
    non_forensic_count: int
    tag_counts: Mapping[str, int]
    unmapped_values: Mapping[str, tuple[str, ...]]
    unmapped_paper_counts: Mapping[str, int]
    special_unconfirmed: int = 0
    pending_source_ids: tuple[str, ...] = ()
    untyped_source_ids: tuple[str, ...] = ()


def _require_headers(
    path: Path, sheet_name: str, headers: Sequence[Any], required: Iterable[str]
) -> dict[str, int]:
    if any(value is None or not isinstance(value, str) or not value.strip() for value in headers):
        raise PaperMarkdownError(f"blank/non-text header: {path}: {sheet_name}")
    if len(set(headers)) != len(headers):
        raise PaperMarkdownError(f"duplicate headers: {path}: {sheet_name}")
    index = {str(value): position for position, value in enumerate(headers)}
    missing = set(required) - set(index)
    if missing:
        raise PaperMarkdownError(
            f"missing columns in {path}:{sheet_name}: {sorted(missing)}"
        )
    return index


def _read_sheet(
    path: Path, sheet_name: str, required: Iterable[str]
) -> list[dict[str, Any]]:
    try:
        workbook = load_workbook(path, read_only=True, data_only=False)
    except (OSError, ValueError) as exc:
        raise PaperMarkdownError(f"unable to open workbook {path}: {exc}") from exc
    try:
        if sheet_name not in workbook.sheetnames:
            raise PaperMarkdownError(f"missing sheet {sheet_name!r}: {path}")
        worksheet = workbook[sheet_name]
        iterator = worksheet.iter_rows()
        try:
            header_cells = next(iterator)
        except StopIteration:
            raise PaperMarkdownError(f"empty sheet: {path}: {sheet_name}") from None
        headers = [cell.value for cell in header_cells]
        _require_headers(path, sheet_name, headers, required)
        rows: list[dict[str, Any]] = []
        for row_number, cells in enumerate(iterator, start=2):
            if any(cell.data_type == "f" for cell in cells):
                raise PaperMarkdownError(
                    f"formula cell is not accepted: {path}:{sheet_name}:{row_number}"
                )
            values = [cell.value for cell in cells]
            if not any(value is not None for value in values):
                continue
            rows.append(dict(zip(headers, values, strict=True)))
        return rows
    finally:
        workbook.close()


def _optional_text(value: Any, *, preserve_lines: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    if preserve_lines:
        lines = [" ".join(line.split()) for line in text.split("\n")]
        return "\n".join(lines).strip()
    return " ".join(text.split())


def _required_text(value: Any, field: str, source: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise PaperMarkdownError(f"{source}: {field} must be non-empty")
    return text


def _doi(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return normalize_doi(text) or None
    except Exception as exc:
        raise PaperMarkdownError(f"invalid DOI {text!r}: {exc}") from exc


def _doi_key(value: str | None) -> str:
    return value.casefold() if value else ""


def _title_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _optional_text(value) or "").casefold()
    return "".join(character for character in text if character.isalnum())


def _split_values(value: str | None, delimiter: str = ",") -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        part.strip()
        for part in value.split(delimiter)
        if part is not None and part.strip()
    )


def _index_unique(
    rows: Sequence[Mapping[str, Any]], field: str, source: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        key = _required_text(row.get(field), field, source)
        if key in result:
            raise PaperMarkdownError(f"duplicate {field} {key}: {source}")
        result[key] = row
    return result


def validate_templates(taxonomy: TechniqueTaxonomy | None = None) -> None:
    expected_headings = {
        KOREAN_TEMPLATE: (
            "## 1. 기본 정보",
            "## 2. 초록",
            "## 3. 키워드",
            "## 4. 대상 애플리케이션",
            "## 5. 참고문헌",
        ),
        ENGLISH_TEMPLATE: (
            "## 1. Basic Information",
            "## 2. Abstract",
            "## 3. Keywords",
            "## 4. Target Applications",
            "## 5. References",
        ),
    }
    taxonomy = taxonomy or load_technique_taxonomy()
    tag_sets: list[tuple[str, ...]] = []
    classification_sets: list[tuple[str, ...]] = []
    for path, headings in expected_headings.items():
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PaperMarkdownError(f"unable to read template {path}: {exc}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        expected_digest = EXPECTED_TEMPLATE_SHA256[path]
        if digest != expected_digest:
            raise PaperMarkdownError(
                f"template content changed without a renderer update: {path}; "
                f"expected_sha256={expected_digest}, actual_sha256={digest}"
            )
        normalized_headings = tuple(
            line.strip() for line in text.splitlines() if line.startswith("## ")
        )
        if normalized_headings != headings:
            raise PaperMarkdownError(
                f"template heading contract changed: {path}: {normalized_headings}"
            )
        front_matter = text.split("---", 2)
        if len(front_matter) != 3:
            raise PaperMarkdownError(f"template has invalid front matter: {path}")
        # Both blocks are YAML sequences whose entries carry a trailing
        # ``# label`` comment; the vocabulary is the entry, not the label.
        classification_block, _, tag_block = front_matter[1].partition("\ntags:")
        if not tag_block:
            raise PaperMarkdownError(f"template has no tags block: {path}")
        if "classification:" not in classification_block:
            raise PaperMarkdownError(f"template has no classification block: {path}")
        classification_sets.append(_template_sequence(classification_block))
        tag_sets.append(_template_sequence(tag_block))
    if any(tags != ALLOWED_TAGS for tags in tag_sets):
        raise PaperMarkdownError(
            "template tag vocabulary changed; update the explicit evidence mapping first"
        )
    expected_classification = taxonomy.identifiers
    for values in classification_sets:
        if values != expected_classification:
            missing = sorted(set(expected_classification) - set(values))
            unexpected = sorted(set(values) - set(expected_classification))
            raise PaperMarkdownError(
                "template classification vocabulary disagrees with "
                f"{TECHNIQUE_TAXONOMY}: missing={missing}, unexpected={unexpected}, "
                f"same_order={values == expected_classification}"
            )


def _template_sequence(block: str) -> tuple[str, ...]:
    """Sequence entries of a template front-matter block, comments stripped."""

    return tuple(
        match.group(1)
        for match in re.finditer(
            r"^\s+-\s+([^#\s]\S*)\s*(?:#.*)?$", block, re.MULTILINE
        )
    )


def _load_types() -> tuple[TypeRecord, ...]:
    return _load_type_corpus()[0]


def _load_type_corpus() -> tuple[tuple[TypeRecord, ...], tuple[str, ...]]:
    """Return the classified papers in ``paper_index`` order plus pending ids.

    The type workbook defines the corpus.  Every row must name an indexed PDF
    whose bytes still hash to the recorded digest; indexed PDFs absent from the
    workbook are papers whose analysis has not run yet and are returned as
    pending rather than treated as an error.
    """

    required = {
        "paper_index",
        "source_id",
        "title",
        "title_kor",
        "title_eng",
        "doi",
        "subtype",
        "subtype_des",
        "classification_status",
        "review_status",
        "pdf_filename",
        "pdf_sha256",
    }
    rows = _read_sheet(TYPE_WORKBOOK, "논문 유형 목록", required)
    records_by_id = load_paper_file_index(PAPER_INDEX)
    if not rows or not records_by_id:
        raise PaperMarkdownError(
            f"empty type/PDF corpus: type={len(rows)}, PDF={len(records_by_id)}"
        )
    rows_by_id = _index_unique(rows, "source_id", "type_list")
    unexpected = sorted(set(rows_by_id) - set(records_by_id))
    if unexpected:
        raise PaperMarkdownError(
            f"type rows without an indexed PDF: {unexpected[:10]}"
        )
    pending = tuple(
        source_id for source_id in records_by_id if source_id not in rows_by_id
    )
    indexed_pdf_paths = {
        PAPER_DIRECTORY / record.filename for record in records_by_id.values()
    }
    actual_pdf_paths = set(PAPER_DIRECTORY.glob("*.pdf"))
    unexpected_pdfs = actual_pdf_paths - indexed_pdf_paths
    if unexpected_pdfs:
        raise PaperMarkdownError(
            f"PDF file set mismatch: unindexed PDFs={len(unexpected_pdfs)}"
        )
    # A classified paper must have its PDF; an index entry without a PDF can
    # only be a reservation of a collection run that is in progress or was
    # interrupted, and such a paper is pending anyway.
    missing_corpus_pdfs = sorted(
        source_id
        for source_id in rows_by_id
        if not (PAPER_DIRECTORY / records_by_id[source_id].filename).is_file()
    )
    if missing_corpus_pdfs:
        raise PaperMarkdownError(
            f"PDF file set mismatch: classified papers without a PDF={missing_corpus_pdfs[:10]}"
        )
    indexed_rows = sorted(
        rows_by_id.values(),
        key=lambda row: row.get("paper_index") if type(row.get("paper_index")) is int else 0,
    )
    result: list[TypeRecord] = []
    seen_indexes: set[int] = set()
    for row_number, row in enumerate(indexed_rows, start=1):
        source_id = _required_text(row.get("source_id"), "source_id", "type_list")
        file_record = records_by_id[source_id]
        # Analyzer/numbering.py keeps the numbers sorted and without gaps, but between a removal
        # and the next renumbering gaps exist; a number must only be a unique positive integer here.
        expected_index = row.get("paper_index")
        if type(expected_index) is not int or expected_index < 1 or expected_index in seen_indexes:
            raise PaperMarkdownError(f"type/PDF paper_index is not a unique positive integer at row {row_number}: {source_id}")
        seen_indexes.add(expected_index)
        if (
            not SOURCE_ID_RE.fullmatch(source_id)
            or row.get("classification_status") != "classified"
            or row.get("review_status") != "approved"
        ):
            raise PaperMarkdownError(
                f"type/PDF identity or approval mismatch at row {expected_index}: {source_id}"
            )
        title = _required_text(row.get("title"), "title", source_id)
        if _title_key(title) != _title_key(file_record.title):
            raise PaperMarkdownError(f"type/PDF title mismatch: {source_id}")
        raw_pdf_filename = row.get("pdf_filename")
        if not isinstance(raw_pdf_filename, str) or not raw_pdf_filename:
            raise PaperMarkdownError(f"{source_id}: pdf_filename must be non-empty")
        pdf_filename = raw_pdf_filename
        pdf_sha256 = _required_text(row.get("pdf_sha256"), "pdf_sha256", source_id)
        pdf_path = PAPER_DIRECTORY / file_record.filename
        if (
            pdf_filename != file_record.filename
            or not SHA256_RE.fullmatch(pdf_sha256)
            or pdf_path.is_symlink()
            or not pdf_path.is_file()
        ):
            raise PaperMarkdownError(f"type/PDF file identity mismatch: {source_id}")
        digest = hashlib.sha256()
        try:
            with pdf_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise PaperMarkdownError(f"unable to hash PDF {pdf_path}: {exc}") from exc
        if digest.hexdigest() != pdf_sha256:
            raise PaperMarkdownError(f"type/PDF SHA-256 mismatch: {source_id}")
        result.append(
            TypeRecord(
                paper_index=expected_index,
                source_id=source_id,
                title=title,
                title_kor=_required_text(row.get("title_kor"), "title_kor", source_id),
                title_eng=_required_text(row.get("title_eng"), "title_eng", source_id),
                doi=_doi(row.get("doi")),
                subtype=_required_text(row.get("subtype"), "subtype", source_id),
                subtype_des=_required_text(
                    row.get("subtype_des"), "subtype_des", source_id
                ),
                pdf_filename=pdf_filename,
                pdf_sha256=pdf_sha256,
            )
        )
    return tuple(result), pending


KEYWORD_COLUMNS = {
    "doi",
    "title",
    "target_device",
    "target_os",
    "target_app",
    "target_app_category",
    "filesystem",
    "cloud",
}


def _keyword_record(row: Mapping[str, Any], source_id: str) -> KeywordRecord:
    return KeywordRecord(
        doi=_doi(row.get("doi")),
        title=_required_text(row.get("title"), "title", source_id),
        target_device=_optional_text(row.get("target_device")),
        target_os=_optional_text(row.get("target_os")),
        target_app=_optional_text(row.get("target_app")),
        target_app_category=_optional_text(row.get("target_app_category")),
        filesystem=_optional_text(row.get("filesystem")),
        cloud=_optional_text(row.get("cloud")),
    )


def _load_keywords(
    types: Sequence[TypeRecord], *, workbook_path: Path = KEYWORD_WORKBOOK
) -> dict[str, KeywordRecord]:
    """Join keyword rows to the corpus.

    Workbooks regenerated by ``Analyzer.keyword_workbook`` carry a ``source_id``
    column and join by identity; the original reviewed workbook has no such
    column and falls back to the one-to-one DOI/title join.
    """

    rows = _read_sheet(workbook_path, "keyword_intelligence", KEYWORD_COLUMNS)
    if rows and "source_id" in rows[0]:
        return _join_keywords_by_source_id(types, rows)
    return _join_keywords_by_doi_title(types, workbook_path=workbook_path, rows=rows)


def _join_keywords_by_source_id(
    types: Sequence[TypeRecord], rows: Sequence[Mapping[str, Any]]
) -> dict[str, KeywordRecord]:
    indexed = _index_unique(rows, "source_id", "keyword_intelligence")
    result: dict[str, KeywordRecord] = {}
    for paper in types:
        row = indexed.get(paper.source_id)
        if row is None:
            raise PaperMarkdownError(f"keyword row is missing: {paper.source_id}")
        result[paper.source_id] = _keyword_record(row, paper.source_id)
    return result


def _join_keywords_by_doi_title(
    types: Sequence[TypeRecord],
    *,
    workbook_path: Path = KEYWORD_WORKBOOK,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, KeywordRecord]:
    if rows is None:
        rows = _read_sheet(workbook_path, "keyword_intelligence", KEYWORD_COLUMNS)
    if len(rows) != len(types):
        raise PaperMarkdownError(
            f"keyword/type row-count mismatch: {len(rows)} != {len(types)}"
        )
    by_doi: dict[str, list[int]] = defaultdict(list)
    by_title: dict[str, list[int]] = defaultdict(list)
    for row_index, row in enumerate(rows):
        doi = _doi(row.get("doi"))
        title_key = _title_key(row.get("title"))
        if doi:
            by_doi[_doi_key(doi)].append(row_index)
        if not title_key:
            raise PaperMarkdownError(f"keyword row {row_index + 2} has no title")
        by_title[title_key].append(row_index)
    used: set[int] = set()
    result: dict[str, KeywordRecord] = {}
    for paper in types:
        candidates: list[int] = []
        paper_doi_key = _doi_key(paper.doi)
        if paper_doi_key and len(by_doi.get(paper_doi_key, [])) == 1:
            candidates = by_doi[paper_doi_key]
        if not candidates:
            candidates = by_title.get(_title_key(paper.title), [])
        candidates = [index for index in candidates if index not in used]
        if len(candidates) != 1:
            raise PaperMarkdownError(
                f"keyword join is not one-to-one for {paper.source_id}: {candidates}"
            )
        row_index = candidates[0]
        row = rows[row_index]
        if _title_key(row.get("title")) != _title_key(paper.title):
            raise PaperMarkdownError(f"keyword title mismatch: {paper.source_id}")
        used.add(row_index)
        result[paper.source_id] = _keyword_record(row, paper.source_id)
    if len(used) != len(rows):
        raise PaperMarkdownError("unused keyword rows remain after one-to-one join")
    return result


def tags_for_keyword(record: KeywordRecord) -> tuple[str, ...]:
    selected: set[str] = set()
    for value in _split_values(record.target_device):
        tag = DEVICE_TAGS.get(value)
        if tag:
            selected.add(tag)
    for value in _split_values(record.filesystem):
        tag = FILESYSTEM_TAGS.get(value.casefold())
        if tag:
            selected.add(tag)
    for value in _split_values(record.target_os):
        tag = OS_TAGS.get(value)
        if tag:
            selected.add(tag)
    categories = _split_values(record.target_app_category, "|")
    for value in categories:
        tag = APPLICATION_TAGS.get(value)
        if tag:
            selected.add(tag)
    for value in _split_values(record.cloud):
        tag = CLOUD_TAGS.get(value)
        if tag:
            selected.add(tag)
        elif value == SAAS_MARKER:
            for category in categories:
                flavour = SAAS_TAGS.get(category)
                if flavour:
                    selected.add(flavour)
    if not selected <= set(ALLOWED_TAGS):
        raise PaperMarkdownError(f"unsupported generated tags: {sorted(selected)}")
    return tuple(tag for tag in ALLOWED_TAGS if tag in selected)


# Level-3 titles that the classification workbook still records under the
# previous ``DFIR-…`` taxonomy wording.  Each pair below is the same technique
# under a renamed heading, verified against ``tech_taxonomy.json``.
RENAMED_LEVEL3_TITLES = {
    "안티포렌식 행위 지표 식별": "안티-포렌식 행위 지표 식별",
    "익명 네트워크 및 다크웹 조사": "익명 네트워크 및 다크웹 데이터 조사",
    "포렌식 보고서 생성": "포렌식 보고서 작성",
    "논리적 포렌식 데이터 우선순위 분류": "포렌식 데이터 우선순위 분류",
    "연관 콘텐츠 검색": "연관 콘텐츠 분석",
}
# ``증거 생애주기 관리`` was a catch-all under the previous taxonomy's evidence
# preservation branch.  The current taxonomy splits that branch into four
# specific activities and keeps no equivalent, so the classification is dropped
# rather than reassigned by guesswork; papers left with nothing render ``[]``.
WITHDRAWN_LEVEL3_TITLES = ("증거 생애주기 관리",)


@dataclass(frozen=True, slots=True)
class TechniqueTaxonomy:
    """Template identities, cross-checked by title with the existing taxonomy."""

    title_kor: Mapping[str, str]
    title_eng: Mapping[str, str]
    id_by_title: Mapping[str, str]

    @property
    def identifiers(self) -> tuple[str, ...]:
        return tuple(self.title_kor)


def load_technique_taxonomy(
    path: Path = TECHNIQUE_TAXONOMY,
) -> TechniqueTaxonomy:
    """The v5.2 technique ids, cross-checked with the bilingual templates.

    ``tech_taxonomy_v5.2.json`` is English only: its Level-3 ids and titles must
    equal the English template's classification labels, and the Korean labels
    (the ``ko.md`` comments) come from the Korean template, which must agree with
    ``tech_titles_ko.json`` beside the taxonomy (DF-Insight ``title.ko``).
    """

    from Analyzer.technique_taxonomy import TaxonomyError, load_taxonomy

    try:
        taxonomy = load_taxonomy(path)
    except TaxonomyError as exc:
        raise PaperMarkdownError(f"unable to read {path}: {exc}") from exc
    template_kor = _template_technique_labels(KOREAN_TEMPLATE, korean=True)
    template_eng = _template_technique_labels(ENGLISH_TEMPLATE, korean=False)
    if set(template_kor) != set(template_eng):
        raise PaperMarkdownError("bilingual template technique IDs differ")
    title_kor: dict[str, str] = {}
    title_eng: dict[str, str] = {}
    id_by_title: dict[str, str] = {}
    for identifier, item in taxonomy.techniques.items():
        if not LEVEL3_ID_RE.fullmatch(identifier):
            raise PaperMarkdownError(f"{path}: unexpected technique id: {identifier}")
        if template_eng.get(identifier) != item.title_en or template_kor.get(identifier) != item.title_ko:
            raise PaperMarkdownError(
                f"template/taxonomy title or code mismatch: {identifier}: {item.title_ko} / {item.title_en}"
            )
        title_kor[identifier] = template_kor[identifier]
        title_eng[identifier] = template_eng[identifier]
        id_by_title[title_kor[identifier]] = identifier
    if not title_kor:
        raise PaperMarkdownError(f"{path}: taxonomy has no Level-3 techniques")
    if set(title_kor) != set(template_kor):
        raise PaperMarkdownError("template/taxonomy technique coverage differs")
    return TechniqueTaxonomy(
        title_kor=title_kor, title_eng=title_eng, id_by_title=id_by_title
    )


def _template_technique_labels(path: Path, *, korean: bool) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PaperMarkdownError(f"unable to read {path}: {exc}") from exc
    block = content.partition("\nclassification:")[2].partition("\ntags:")[0]
    result: dict[str, str] = {}
    for identifier, label in re.findall(
        r"^\s+-\s+([A-Z]{4}-[0-9]{2}-[0-9]{2})\s+#\s+(.+)$",
        block, re.MULTILINE,
    ):
        if identifier in result:
            raise PaperMarkdownError(f"duplicate template technique: {identifier}")
        result[identifier] = label.split(" / ", 1)[0].strip() if korean else label.strip()
    if not result:
        raise PaperMarkdownError(f"template has no labeled techniques: {path}")
    return result


def _read_json_object(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperMarkdownError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PaperMarkdownError(f"{path}: expected a JSON object")
    return payload


def classification_for_techniques(
    techniques: Sequence[Technique],
    taxonomy: TechniqueTaxonomy,
    source_id: str,
) -> tuple[str, ...]:
    """Level-3 identifiers for a paper, in the workbook's own relevance rank."""

    ordered: list[str] = []
    for technique in techniques:
        title = technique.level_3
        if title in WITHDRAWN_LEVEL3_TITLES:
            continue
        identifier = taxonomy.id_by_title.get(
            RENAMED_LEVEL3_TITLES.get(title, title)
        )
        if identifier is None:
            raise PaperMarkdownError(
                f"{source_id}: Level-3 title is absent from the technique taxonomy: {title!r}"
            )
        if identifier not in ordered:
            ordered.append(identifier)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class TargetRecord:
    identifier: str
    name_kor: str
    name_eng: str
    operating_system: str | None


def load_target_registry(path: Path = TARGET_REGISTRY) -> tuple[TargetRecord, ...]:
    """Registered application targets of ``web/DF-Insight/data/targets_md``."""

    if not path.is_dir():
        raise PaperMarkdownError(f"missing target registry: {path}")
    records: list[TargetRecord] = []
    for directory in sorted(path.iterdir()):
        if not directory.is_dir():
            continue
        if not TARGET_ID_RE.fullmatch(directory.name):
            raise PaperMarkdownError(f"unexpected target id: {directory.name}")
        documents: dict[str, str] = {}
        for language in ("ko", "en"):
            document = directory / f"{language}.md"
            try:
                documents[language] = document.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise PaperMarkdownError(f"unable to read {document}: {exc}") from exc
        subjects = {}
        for language, text in documents.items():
            match = re.search(r'^subject:\s*"(.*)"\s*$', text, re.MULTILINE)
            if not match:
                raise PaperMarkdownError(f"{directory}/{language}.md: subject is missing")
            subjects[language] = _required_text(match.group(1), "subject", directory.name)
        front_matter = documents["ko"].split("---", 2)
        if len(front_matter) != 3:
            raise PaperMarkdownError(f"{directory}/ko.md: invalid front matter")
        operating_system = next(
            (
                _operating_system_name(value)
                for value in re.findall(r"^\s+-\s+(.+?)\s*$", front_matter[1], re.MULTILINE)
                if value.startswith("OS-")
            ),
            None,
        )
        records.append(
            TargetRecord(
                identifier=directory.name,
                name_kor=subjects["ko"],
                name_eng=subjects["en"],
                operating_system=operating_system,
            )
        )
    if not records:
        raise PaperMarkdownError(f"target registry is empty: {path}")
    return tuple(records)


def _operating_system_name(tag: str) -> str:
    """``OS-WIN`` (a tag code, the registry's current form) -> ``Windows``; ``OS-Windows`` (older form) -> ``Windows``."""

    members = [value for value, code in OS_TAGS.items() if code == tag]
    if len(members) == 1:
        return members[0]
    return tag[len("OS-"):]


def _target_key(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip()).casefold()


def target_applications_for_keyword(
    record: KeywordRecord, registry: Sequence[TargetRecord]
) -> tuple[tuple[str | None, str], ...]:
    """Recorded target applications paired with their registry id when known.

    A registry entry is specific to one platform, so an id is only attached when
    exactly one entry matches the recorded name and either carries no operating
    system or carries one the paper itself studies.  Anything else stays blank:
    naming a platform-specific target for the wrong platform would assert
    something the evidence does not support.
    """

    by_key: dict[str, list[TargetRecord]] = defaultdict(list)
    for target in registry:
        for key in {_target_key(target.name_kor), _target_key(target.name_eng)}:
            by_key[key].append(target)
    operating_systems = set(_split_values(record.target_os))
    rows: list[tuple[str | None, str]] = []
    for name in _split_values(record.target_app):
        candidates = [
            target
            for target in by_key.get(_target_key(name), ())
            if target.operating_system is None
            or target.operating_system in operating_systems
        ]
        identifier = candidates[0].identifier if len(candidates) == 1 else None
        rows.append((identifier, name))
    return tuple(rows)


def unmapped_keyword_evidence(
    papers: Sequence[PaperData],
) -> tuple[dict[str, tuple[str, ...]], dict[str, int]]:
    specifications = (
        ("target_device", ",", DEVICE_TAGS, False),
        ("filesystem", ",", FILESYSTEM_TAGS, True),
        ("target_os", ",", OS_TAGS, False),
        ("target_app_category", "|", APPLICATION_TAGS, False),
        ("cloud", ",", CLOUD_TAGS, False),
    )
    values_by_field: dict[str, set[str]] = defaultdict(set)
    paper_counts: Counter[str] = Counter()
    for paper in papers:
        saas_flavours = {
            SAAS_TAGS[category]
            for category in _split_values(paper.keyword.target_app_category, "|")
            if category in SAAS_TAGS
        }
        for field, delimiter, mapping, casefold in specifications:
            values = _split_values(getattr(paper.keyword, field), delimiter)
            unsupported = {
                value
                for value in values
                if (value.casefold() if casefold else value) not in mapping
            }
            # A recorded SaaS deployment is mapped when the paper's application
            # category supplies a service flavour for it.
            if field == "cloud" and saas_flavours:
                unsupported.discard(SAAS_MARKER)
            if unsupported:
                values_by_field[field].update(unsupported)
                paper_counts[field] += 1
    ordered_values = {
        field: tuple(sorted(values, key=lambda value: (value.casefold(), value)))
        for field, values in values_by_field.items()
    }
    return ordered_values, dict(paper_counts)


def _load_oa(source_ids: Sequence[str]) -> tuple[dict[str, str], int]:
    required = {
        "source_id",
        "is_open_access",
        "oa_classification",
        "collection_status",
        "field_present",
        "error_code",
    }
    rows = _read_sheet(OA_WORKBOOK, "OA 목록", required)
    indexed = _index_unique(rows, "source_id", "oa_list")
    result: dict[str, str] = {}
    unconfirmed = 0
    for source_id in source_ids:
        if source_id not in indexed:
            raise PaperMarkdownError(f"OA row is missing: {source_id}")
        row = indexed[source_id]
        status = row.get("collection_status")
        is_open = row.get("is_open_access")
        if status == "classified":
            if row.get("field_present") is not True or type(is_open) is not bool:
                raise PaperMarkdownError(f"inconsistent OA classification: {source_id}")
            expected = "Open Access" if is_open else "Not Open Access"
            if row.get("oa_classification") != expected:
                raise PaperMarkdownError(f"OA label mismatch: {source_id}")
            result[source_id] = expected
        else:
            if is_open is not None or row.get("oa_classification") is not None:
                raise PaperMarkdownError(f"unconfirmed OA row has a verdict: {source_id}")
            result[source_id] = MISSING
            unconfirmed += 1
    return result, unconfirmed


def _load_special(source_ids: Sequence[str]) -> tuple[dict[str, bool | None], int]:
    """Special-Issue verdicts; unconfirmed rows yield ``None`` and are counted."""

    required = {
        "source_id",
        "special_number",
        "special_number_field_present",
        "is_special_issue",
        "special_issue_classification",
        "identity_verified",
        "collection_status",
    }
    rows = _read_sheet(SPECIAL_WORKBOOK, "Special Issue 목록", required)
    indexed = _index_unique(rows, "source_id", "special_list")
    result: dict[str, bool | None] = {}
    unconfirmed = 0
    for source_id in source_ids:
        if source_id not in indexed:
            raise PaperMarkdownError(f"Special Issue row is missing: {source_id}")
        row = indexed[source_id]
        value = row.get("is_special_issue")
        if row.get("collection_status") != "classified":
            # The collector labels such rows "Unresolved"; only a boolean
            # verdict would contradict the unconfirmed status.
            label = _optional_text(row.get("special_issue_classification"))
            if value is not None or (label is not None and label.casefold() != "unresolved"):
                raise PaperMarkdownError(f"unconfirmed Special Issue row has a verdict: {source_id}")
            result[source_id] = None
            unconfirmed += 1
            continue
        if (
            row.get("identity_verified") is not True
            or row.get("special_number_field_present") is not True
            or type(value) is not bool
        ):
            raise PaperMarkdownError(f"unapproved Special Issue row: {source_id}")
        expected = "Special Issue" if value else "Not Special Issue"
        if row.get("special_issue_classification") != expected:
            raise PaperMarkdownError(f"Special Issue label mismatch: {source_id}")
        if value and _optional_text(row.get("special_number")) is None:
            raise PaperMarkdownError(f"Special Issue has no Special-Number: {source_id}")
        if not value and _optional_text(row.get("special_number")) is not None:
            raise PaperMarkdownError(f"ordinary issue has Special-Number: {source_id}")
        result[source_id] = value
    return result, unconfirmed


def _load_references(
    source_ids: Sequence[str],
) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], int]:
    """Reference rows per paper from the saved ``<referenceInfo>`` files.

    The manifest written by the reference stage names every paper's status
    and file.  A ``collected`` paper's XML must exist, hash to the recorded
    digest and parse to the recorded count; ``no_reference_data`` (KCI lists
    nothing), ``failed`` and ``no_record`` (not resolved yet, retried on the
    next run) render as an unavailable list.  A paper missing from the
    manifest means the reference stage has not covered it, which is an error.
    """

    try:
        payload = json.loads(REFERENCE_MANIFEST.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PaperMarkdownError(
            f"reference manifest is missing ({REFERENCE_MANIFEST}); "
            "run the reference stage first (python3 main.py -ref)"
        ) from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperMarkdownError(f"unreadable reference manifest: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("papers"), list):
        raise PaperMarkdownError("invalid reference manifest")
    entries: dict[str, Mapping[str, Any]] = {}
    for entry in payload["papers"]:
        if not isinstance(entry, dict):
            raise PaperMarkdownError("invalid reference manifest entry")
        source_id = _required_text(entry.get("source_id"), "source_id", "reference manifest")
        if source_id in entries:
            raise PaperMarkdownError(f"duplicate reference manifest entry: {source_id}")
        entries[source_id] = entry
    xml_directory = REFERENCE_MANIFEST.parent / str(payload.get("xml_directory") or "referenceInfo")
    result: dict[str, tuple[Mapping[str, Any], ...]] = {}
    unavailable = 0
    for source_id in source_ids:
        entry = entries.get(source_id)
        if entry is None:
            raise PaperMarkdownError(f"reference manifest has no entry: {source_id}")
        status = entry.get("status")
        if status == "collected":
            result[source_id] = _read_reference_file(xml_directory, source_id, entry)
        elif status == "no_reference_data":
            if entry.get("reference_count") != 0:
                raise PaperMarkdownError(f"no-reference status contradicts count: {source_id}")
            result[source_id] = ()
            unavailable += 1
        elif status in {"failed", "no_record"}:
            if entry.get("file") is not None:
                raise PaperMarkdownError(f"unresolved reference status names a file: {source_id}")
            result[source_id] = ()
            unavailable += 1
        else:
            raise PaperMarkdownError(f"unknown reference status: {source_id}: {status}")
    return result, unavailable


def _read_reference_file(
    xml_directory: Path, source_id: str, entry: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    file_name = entry.get("file")
    if not isinstance(file_name, str) or not file_name:
        raise PaperMarkdownError(f"collected reference entry names no file: {source_id}")
    path = REFERENCE_MANIFEST.parent / file_name
    if path.parent != xml_directory or path.name != f"{source_id}.xml":
        raise PaperMarkdownError(f"reference file is outside the referenceInfo folder: {source_id}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PaperMarkdownError(f"reference file is missing: {source_id}: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
        raise PaperMarkdownError(f"reference file differs from the manifest digest: {source_id}")
    try:
        records = parse_reference_info_xml(data)
    except InvalidReferenceResponse as exc:
        raise PaperMarkdownError(f"invalid reference file: {source_id}: {exc.code}") from exc
    if not records or len(records) != entry.get("reference_count"):
        raise PaperMarkdownError(f"reference count mismatch: {source_id}")
    rows = tuple(asdict(record) for record in records)
    orders = [row["reference_order"] for row in rows]
    if orders != list(range(1, len(rows) + 1)):
        raise PaperMarkdownError(f"invalid reference order: {source_id}")
    return rows


# Level-3 per paper from the Jev AI results (chapter III '논문 분류'), prepared by the jev stage
# (``Analyzer/JevAI/jev_fields.py``).  The earlier Level stage's ``Analyzer/LV/levels.json`` is no longer read here.
LEVELS_JSON = ANALYZER_DIR / "JevAI" / "levels.json"


def _level5_registry_ids(directory: Path | None = None) -> frozenset[str]:
    """Ids of DF-Insight's Level-5 nodes (``data/techniques/<id>.json`` with ``level: 5``)."""

    directory = directory or LEVEL5_REGISTRY
    if not directory.is_dir():
        raise PaperMarkdownError(f"DF-Insight technique registry is missing: {directory}")
    identifiers: set[str] = set()
    for path in directory.glob("*.json"):
        if not LEVEL5_ID_RE.fullmatch(path.stem):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaperMarkdownError(f"unable to read {path}: {exc}") from exc
        if isinstance(record, dict) and record.get("level") == 5 and record.get("id") == path.stem:
            identifiers.add(path.stem)
    return frozenset(identifiers)


def _load_targets(
    source_ids: Sequence[str], path: Path | None = None,
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[str | None, str, str], ...]]]:
    """Front-matter tags and section-4 rows per paper (``TARGETS_JSON``, the jev stage).

    A paper whose tags/applications are not final yet (``status`` other than ``ok``)
    stops generation, as a pending Level does: publishing it would claim "no tags /
    no applications".  (``load_corpus`` lets such a paper wait without a profile; only a
    paper that already has one gets here.)  Every Level-5 id must be a DF-Insight Level-5 node.
    """

    path = path or TARGETS_JSON
    payload = _read_json_object(path)
    papers = payload.get("papers")
    if not isinstance(papers, Mapping):
        raise PaperMarkdownError(f"{path}: no papers")
    registry = _level5_registry_ids()
    allowed = set(ALLOWED_TAGS)
    result: dict[str, tuple[tuple[str, ...], tuple[tuple[str | None, str, str], ...]]] = {}
    pending: list[str] = []
    for source_id in source_ids:
        record = papers.get(source_id)
        if not isinstance(record, Mapping):
            raise PaperMarkdownError(
                f"target applications are missing for {source_id}: run the jev stage (python3 main.py -jev) once its Jev AI result is in Analyzer/JevAI"
            )
        if record.get("status") != "ok":
            pending.append(source_id)
            continue
        tags = record.get("tags") or []
        if not isinstance(tags, list) or not set(tags) <= allowed or len(set(tags)) != len(tags):
            raise PaperMarkdownError(f"{path}: invalid tags for {source_id}: {tags!r}")
        rows: list[tuple[str | None, str, str]] = []
        for application in record.get("applications") or []:
            name = _required_text(application.get("name"), "application name", source_id)
            level5 = application.get("level5") or []
            if not level5:
                rows.append((None, name, name))
                continue
            for item in level5:
                identifier = str(item.get("id") or "")
                if not LEVEL5_ID_RE.fullmatch(identifier):
                    raise PaperMarkdownError(f"{source_id}: not a Level-5 id: {identifier!r}")
                if identifier not in registry:
                    raise PaperMarkdownError(
                        f"{source_id}: Level-5 id {identifier} is not a DF-Insight node any more; run the jev stage again (python3 main.py -jev)"
                    )
                rows.append((
                    identifier,
                    _required_text(item.get("title_ko"), "Level-5 title", identifier),
                    _required_text(item.get("title_en"), "Level-5 title", identifier),
                ))
        identifiers = [identifier for identifier, _kor, _eng in rows if identifier]
        if len(set(identifiers)) != len(identifiers):
            raise PaperMarkdownError(f"{source_id}: a Level-5 id appears twice in the application table")
        blank_names = [_target_key(kor) for identifier, kor, _eng in rows if not identifier]
        if len(set(blank_names)) != len(blank_names):
            raise PaperMarkdownError(f"{source_id}: an unregistered application appears twice")
        result[source_id] = (tuple(tag for tag in ALLOWED_TAGS if tag in set(tags)), tuple(rows))
    if pending:
        raise PaperMarkdownError(
            f"tags/target applications are not final for {len(pending)} papers that already have a profile (e.g. {pending[:3]}): "
            "their Jev AI result is missing — put it (mapping_scores_*.jsonl, results_kci-*.db) into Analyzer/JevAI and run the jev stage "
            "(python3 main.py -jev); a paper without a profile simply waits for it"
        )
    return result


def _load_levels(
    source_ids: Sequence[str], taxonomy: "TechniqueTaxonomy", path: Path | None = None,
) -> tuple[dict[str, tuple[str, ...]], int]:
    """Level-3 ids per paper from the Level result (``LEVELS_JSON``, the jev stage).

    Only Levels at or above Jev AI's threshold are recorded there, so a paper may
    legitimately have none; the second value counts those papers.
    """

    source = path or LEVELS_JSON
    payload = _read_json_object(source)
    papers = payload.get("papers")
    if not isinstance(papers, Mapping):
        raise PaperMarkdownError(f"{source}: Level result has no papers")
    allowed = set(taxonomy.identifiers)
    result: dict[str, tuple[str, ...]] = {}
    without_level = 0
    for source_id in source_ids:
        record = papers.get(source_id)
        if not isinstance(record, Mapping) or not isinstance(record.get("lv3"), list):
            raise PaperMarkdownError(f"Level result is missing (run the Level stage first: python3 main.py -jev): {source_id}")
        if record.get("status") == "pending":
            # The classifier has not answered for this paper yet; an empty list here would
            # publish "no Level" for a paper that simply was not classified.
            raise PaperMarkdownError(
                f"Level classification is not finished for {source_id}, which already has a profile: its Jev AI result is missing — "
                "put it (mapping_scores_*.jsonl, results_kci-*.db) into Analyzer/JevAI and run the jev stage (python3 main.py -jev); "
                "a paper without a profile simply waits for it"
            )
        identifiers = tuple(str(item) for item in record["lv3"])
        unknown = [item for item in identifiers if item not in allowed]
        if unknown or len(set(identifiers)) != len(identifiers):
            raise PaperMarkdownError(f"{source_id}: invalid Level-3 ids in the Level result: {unknown or identifiers}")
        result[source_id] = identifiers
        without_level += not identifiers
    return result, without_level


def _load_techniques(
    source_ids: Sequence[str],
) -> tuple[dict[str, tuple[Technique, ...]], int]:
    summary_required = {
        "source_id",
        "classification_status",
        "forensic_relevance",
        "level_3_count",
    }
    detail_required = {
        "source_id",
        "rank",
        "path_id",
        "level_2",
        "level_3",
        "level_2_en",
        "level_3_en",
        "review_status",
    }
    summaries = _index_unique(
        _read_sheet(LEVEL3_WORKBOOK, "논문별 분류", summary_required),
        "source_id",
        "Level-3 summary",
    )
    detail_rows = _read_sheet(LEVEL3_WORKBOOK, "분류 상세", detail_required)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    path_keys: set[tuple[str, str]] = set()
    for row in detail_rows:
        source_id = _required_text(row.get("source_id"), "source_id", "Level-3 detail")
        path_id = _required_text(row.get("path_id"), "path_id", source_id)
        key = (source_id, path_id)
        if key in path_keys:
            raise PaperMarkdownError(f"duplicate Level-3 path: {key}")
        path_keys.add(key)
        if row.get("review_status") != "approved":
            raise PaperMarkdownError(f"unapproved Level-3 detail: {key}")
        grouped[source_id].append(row)
    result: dict[str, tuple[Technique, ...]] = {}
    non_forensic = 0
    for source_id in source_ids:
        if source_id not in summaries:
            raise PaperMarkdownError(f"Level-3 summary is missing: {source_id}")
        summary = summaries[source_id]
        rows = sorted(grouped.get(source_id, []), key=lambda row: row["rank"])
        ranks = [row.get("rank") for row in rows]
        if any(type(value) is not int or value < 1 for value in ranks):
            raise PaperMarkdownError(f"invalid Level-3 rank: {source_id}")
        if len(set(ranks)) != len(ranks):
            raise PaperMarkdownError(f"duplicate Level-3 rank: {source_id}")
        status = summary.get("classification_status")
        if status == "classified":
            if summary.get("level_3_count") != len(rows) or not rows:
                raise PaperMarkdownError(f"Level-3 count mismatch: {source_id}")
        elif status == "non_forensic":
            if (
                summary.get("forensic_relevance") != "비관련"
                or summary.get("level_3_count") != 0
                or rows
            ):
                raise PaperMarkdownError(
                    f"non-forensic Level-3 status is inconsistent: {source_id}"
                )
            non_forensic += 1
        else:
            raise PaperMarkdownError(f"unknown Level-3 status: {source_id}: {status}")
        result[source_id] = tuple(
            Technique(
                rank=int(row["rank"]),
                level_2=_required_text(row.get("level_2"), "level_2", source_id),
                level_3=_required_text(row.get("level_3"), "level_3", source_id),
                level_2_en=_required_text(
                    row.get("level_2_en"), "level_2_en", source_id
                ),
                level_3_en=_required_text(
                    row.get("level_3_en"), "level_3_en", source_id
                ),
            )
            for row in rows
        )
    return result, non_forensic


def _json_text_list(value: Any, field: str, source_id: str) -> tuple[str, ...]:
    text = _optional_text(value)
    if text is None:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PaperMarkdownError(
            f"invalid {field} JSON for {source_id}: {exc}"
        ) from exc
    if not isinstance(parsed, list):
        raise PaperMarkdownError(f"{field} must be a JSON list: {source_id}")
    result: list[str] = []
    for index, item in enumerate(parsed):
        normalized = _optional_text(item)
        if not isinstance(item, str) or normalized is None:
            raise PaperMarkdownError(
                f"{field}[{index}] must be non-empty text: {source_id}"
            )
        result.append(normalized)
    return tuple(result)


def _publication_month(value: Any, year: Any) -> str | None:
    """``YYYY-MM`` when the stored value is a month of the paper's year; otherwise nothing (never a guess)."""

    text = _optional_text(value)
    if text and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", text) and (year is None or str(year) == text[:4]):
        return text
    return None


def _publication_date(year: Any, month: Any) -> str:
    """The template's '출판 날짜 / Publication Date' cell: ``YYYY. MM.``, ``YYYY.`` without a month."""

    if month:
        return f"{month[:4]}. {month[5:7]}."
    year_text = _optional_text(year)
    return f"{year_text}." if year_text else MISSING


def _load_database_metadata(source_ids: set[str]) -> dict[str, dict[str, Any]]:
    try:
        connection = sqlite3.connect(
            f"file:{DATABASE.resolve()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise PaperMarkdownError(f"unable to open database {DATABASE}: {exc}") from exc
    try:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(kci_papers)")
        }
        missing_columns = DATABASE_FIELDS - columns
        if missing_columns:
            raise PaperMarkdownError(
                f"kci_papers is missing columns: {sorted(missing_columns)}"
            )
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id, source, source_id, title, title_kor, title_eng, doi, "
                "url, abstract_kor, abstract_eng, keywords_json, authors_json, "
                "journal_id, institution_id, issue_id, publication_year, registered_at "
                "FROM kci_papers"
            )
            if row["source_id"] in source_ids
        ]
    except sqlite3.Error as exc:
        raise PaperMarkdownError(f"unable to read kci_papers: {exc}") from exc
    finally:
        connection.close()
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_id = _required_text(row.get("source_id"), "source_id", "kci_papers")
        if source_id in indexed:
            raise PaperMarkdownError(f"duplicate kci_papers source_id: {source_id}")
        if row.get("source") != "kci" or not SOURCE_ID_RE.fullmatch(source_id):
            raise PaperMarkdownError(f"invalid KCI database identity: {source_id}")
        url = _optional_text(row.get("url"))
        if url is not None and (source_id not in url or not url.startswith("https://")):
            raise PaperMarkdownError(f"database URL/identity mismatch: {source_id}")
        indexed[source_id] = {
            "database_id": row.get("id"),
            "title": _optional_text(row.get("title")),
            "title_kor": _optional_text(row.get("title_kor")),
            "title_eng": _optional_text(row.get("title_eng")),
            "doi": _doi(row.get("doi")),
            "url": url,
            "abstract_kor": _optional_text(
                row.get("abstract_kor"), preserve_lines=True
            ),
            "abstract_eng": _optional_text(
                row.get("abstract_eng"), preserve_lines=True
            ),
            "keywords": _json_text_list(
                row.get("keywords_json"), "keywords_json", source_id
            ),
            "authors": _json_text_list(
                row.get("authors_json"), "authors_json", source_id
            ),
            "journal_id": _optional_text(row.get("journal_id")),
            "institution_id": _optional_text(row.get("institution_id")),
            "issue_id": _optional_text(row.get("issue_id")),
            "publication_year": _optional_text(row.get("publication_year")),
            # ``registered_at`` is a legacy column name: Core.normalizer stores KCI's
            # journalInfo/pub-year + pub-mon there, i.e. the publication month.
            "publication_month": _publication_month(row.get("registered_at"), row.get("publication_year")),
        }
    if set(indexed) != source_ids:
        raise PaperMarkdownError(
            f"PDF/database coverage mismatch: "
            f"missing={sorted(source_ids - set(indexed))}, "
            f"unexpected={sorted(set(indexed) - source_ids)}"
        )
    return indexed


def assign_record_ids(
    source_ids: Iterable[str], existing: Mapping[str, str] | None = None,
    *, preferred_numbers: Mapping[str, int] | None = None,
) -> dict[str, str]:
    """Give every paper its profile id; an id already recorded in the index is kept.

    ``Analyzer/numbering.py`` owns the numbering (sorted, without gaps, record
    number == ``paper_index``) and writes the index for every selected paper,
    so normally nothing is assigned here.  A paper that is somehow missing from
    the index takes the number of its ``paper_index`` when that is free,
    otherwise the next unused number; ids are never reassigned by this function.
    """

    ordered = sorted(set(source_ids))
    assigned = dict(existing or {})
    for source_id in (*ordered, *assigned):
        if not SOURCE_ID_RE.fullmatch(source_id):
            raise PaperMarkdownError(f"unexpected KCI source id: {source_id}")
    if any(not RECORD_ID_RE.fullmatch(value) or int(value.rsplit("-", 1)[1]) == 0
           for value in assigned.values()):
        raise PaperMarkdownError("invalid reserved paper profile identity")
    if len(set(assigned.values())) != len(assigned):
        raise PaperMarkdownError("duplicate reserved paper profile identity")
    next_number = max((int(value.rsplit("-", 1)[1]) for value in assigned.values()), default=0)
    if next_number + len(set(ordered) - set(assigned)) > 10**RECORD_ID_DIGITS - 1:
        raise PaperMarkdownError(
            f"corpus of {len(ordered)} exceeds the {RECORD_ID_DIGITS}-digit record id space"
        )
    taken = set(assigned.values())
    for source_id in ordered:
        number = (preferred_numbers or {}).get(source_id)
        candidate = f"{RECORD_ID_PREFIX}{number:0{RECORD_ID_DIGITS}d}" if type(number) is int and 0 < number < 10**RECORD_ID_DIGITS else None
        if source_id not in assigned and candidate is not None and candidate not in taken:
            assigned[source_id] = candidate
            taken.add(candidate)
    next_number = max((int(value.rsplit("-", 1)[1]) for value in assigned.values()), default=0)
    for source_id in ordered:
        if source_id not in assigned:
            next_number += 1
            assigned[source_id] = f"{RECORD_ID_PREFIX}{next_number:0{RECORD_ID_DIGITS}d}"
    return {source_id: assigned[source_id] for source_id in ordered}


def load_profile_identities(path: Path = PROFILE_INDEX) -> dict[str, str]:
    if not path.exists():
        # An index may be absent on the first migration or lost separately
        # from the MD. Never renumber already published profile directories.
        return recover_profile_identities(DEFAULT_OUTPUT) if path == PROFILE_INDEX else {}
    payload = _read_json_object(path)
    records = payload.get("records")
    if payload.get("schema_version") != 1 or not isinstance(records, dict):
        raise PaperMarkdownError(f"invalid profile identity index: {path}")
    if any(not isinstance(value, str) for value in records.values()):
        raise PaperMarkdownError(f"invalid profile identity entries: {path}")
    assign_record_ids((), records)  # Validate even reserved papers no longer active.
    return records


def recover_profile_identities(output: Path) -> dict[str, str]:
    """Recover reservations only from complete, agreeing bilingual profiles."""
    recovered: dict[str, str] = {}
    for directory in sorted(output.glob("KNOW-DOMP-*")):
        if directory.is_symlink() or not directory.is_dir() or not RECORD_ID_RE.fullmatch(directory.name):
            raise PaperMarkdownError(f"invalid existing profile directory: {directory}")
        identities: list[str] = []
        for filename in LANGUAGE_FILES.values():
            path = directory / filename
            if path.is_symlink() or not path.is_file():
                raise PaperMarkdownError(f"cannot recover incomplete profile: {path}")
            content = path.read_text(encoding="utf-8")
            frontmatter = content.split("---", 2)
            match = re.search(r'^identifier:\s*("[^"\n]+")\s*$', frontmatter[1] if len(frontmatter) == 3 else "", re.MULTILINE)
            url = re.findall(r"^\| URL \| (.+?) \|$", content, re.MULTILINE)
            source = re.search(r"[?&]sereArticleSearchBean\.artiId=(ART[0-9]{9})(?:&|$)", html.unescape(url[0])) if len(url) == 1 else None
            if not match or json.loads(match.group(1)) != directory.name or not source:
                raise PaperMarkdownError(f"existing profile identity is inconsistent: {path}")
            identities.append(source.group(1))
        if len(set(identities)) != 1 or identities[0] in recovered:
            raise PaperMarkdownError(f"existing bilingual/duplicate profile identity: {directory}")
        recovered[identities[0]] = directory.name
    assign_record_ids((), recovered)
    return recovered


def save_profile_identities(corpus: CorpusData, path: Path = PROFILE_INDEX) -> None:
    """Persist the source-to-profile crosswalk only after validated generation."""
    existing = load_profile_identities(path)
    for paper in corpus.papers:
        source_id = paper.type_record.source_id
        if source_id in existing and existing[source_id] != paper.record_id:
            raise PaperMarkdownError(f"refusing to reassign profile identity: {source_id}")
        existing[source_id] = paper.record_id
    assign_record_ids((), existing)
    _atomic_write(path, json.dumps({
        "schema_version": 1,
        "records": dict(sorted(existing.items())),
    }, ensure_ascii=False, indent=2) + "\n")


def _reference_record_ids(
    rows: Sequence[Mapping[str, Any]], record_ids: Mapping[str, str]
) -> dict[int, str]:
    resolved: dict[int, str] = {}
    for row in rows:
        cited = _optional_text(row.get("referenced_kci_source_id"))
        if cited is None:
            continue
        record_id = record_ids.get(cited)
        if record_id is not None:
            resolved[int(row["reference_order"])] = record_id
    return resolved


def _pdf_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PaperMarkdownError(f"unable to hash PDF {path}: {exc}") from exc
    return digest.hexdigest()


def _jev_records() -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The papers of ``LEVELS_JSON`` and ``TARGETS_JSON`` (the jev stage's output)."""

    levels = _read_json_object(LEVELS_JSON).get("papers")
    targets = _read_json_object(TARGETS_JSON).get("papers")
    if not isinstance(levels, Mapping) or not isinstance(targets, Mapping):
        raise PaperMarkdownError(f"{LEVELS_JSON} / {TARGETS_JSON}: no papers")
    return levels, targets


def _jev_final(source_id: str, levels: Mapping[str, Any], targets: Mapping[str, Any]) -> bool:
    """The paper's Jev AI classification is final in both inputs (a pending or missing record is not)."""

    level, target = levels.get(source_id), targets.get(source_id)
    return (
        isinstance(level, Mapping) and isinstance(level.get("lv3"), list) and level.get("status") != "pending"
        and isinstance(target, Mapping) and target.get("status") == "ok"
    )


def _untyped_papers(source_ids: Sequence[str], identities: Mapping[str, str]) -> tuple[TypeRecord, ...]:
    """Indexed PDFs without a type decision that still get their profile now.

    The type decision comes from the analysis stage; the classification, tags and target
    applications come from the Jev AI results, which do not wait for it.  A paper whose
    classification is final, whose PDF is on disk and which already has its number (the
    renumber stage) gets a profile whose type cell is ``N/A`` (nothing is guessed); the type
    appears with the first MD run after its analysis.  Any other such paper stays pending.
    """

    if not source_ids:
        return ()
    levels, targets = _jev_records()
    records_by_id = load_paper_file_index(PAPER_INDEX)
    result: list[TypeRecord] = []
    for source_id in source_ids:
        file_record = records_by_id.get(source_id)
        if not _jev_final(source_id, levels, targets) or file_record is None or source_id not in identities:
            continue
        pdf_path = PAPER_DIRECTORY / file_record.filename
        if pdf_path.is_symlink() or not pdf_path.is_file():
            continue  # an index entry whose PDF is not on disk yet (collection in progress)
        if not SOURCE_ID_RE.fullmatch(source_id):
            raise PaperMarkdownError(f"unexpected KCI source id: {source_id}")
        result.append(
            TypeRecord(
                paper_index=0,  # numbered by the profile index (Analyzer/numbering.py), not by an analysis record
                source_id=source_id,
                title=file_record.title,
                title_kor=file_record.title,
                title_eng=file_record.title,
                doi=None,
                subtype=None,
                subtype_des=None,
                pdf_filename=file_record.filename,
                pdf_sha256=_pdf_sha256(pdf_path),
            )
        )
    return tuple(result)


def _typed_papers_waiting_for_jev(
    typed: Sequence[TypeRecord], identities: Mapping[str, str], output: Path,
) -> tuple[tuple[TypeRecord, ...], tuple[str, ...]]:
    """``(papers to profile, papers that wait)`` among the papers with a type decision.

    Jev AI results come from the researcher who runs Jev, after the pipeline analysed a new
    paper, so a typed paper whose classification is not final yet waits for it like an untyped
    one: no profile (publishing one would claim "no Level, no tags, no applications"), listed
    as pending, and every other paper's profile is still written.  A paper that already has a
    profile folder in ``output`` stays in, so that ``_load_levels`` / ``_load_targets`` stop the
    run: its published classification must not silently disappear.
    """

    if not typed:
        return (), ()
    levels, targets = _jev_records()
    kept: list[TypeRecord] = []
    waiting: list[str] = []
    for record in typed:
        record_id = identities.get(record.source_id)
        published = record_id is not None and (output / record_id).is_dir()
        if _jev_final(record.source_id, levels, targets) or published:
            kept.append(record)
        else:
            waiting.append(record.source_id)
    return tuple(kept), tuple(waiting)


def _no_keywords(record: TypeRecord) -> KeywordRecord:
    """A paper without an analysis has no keyword-workbook row (that row only cross-checks the DOI)."""

    return KeywordRecord(
        doi=None, title=record.title, target_device=None, target_os=None, target_app=None,
        target_app_category=None, filesystem=None, cloud=None,
    )


def load_corpus(output: Path | None = None) -> CorpusData:
    """The papers to profile.  ``output`` (default ``DEFAULT_OUTPUT``) is where their profiles are
    written; a paper that already has a profile there never waits silently (``_typed_papers_waiting_for_jev``)."""

    taxonomy = load_technique_taxonomy()
    validate_templates(taxonomy)
    typed, pending = _load_type_corpus()
    identities = load_profile_identities()
    untyped = _untyped_papers(pending, identities)
    untyped_ids = tuple(record.source_id for record in untyped)
    waiting = set(pending) - set(untyped_ids)
    typed, unscored = _typed_papers_waiting_for_jev(typed, identities, output or DEFAULT_OUTPUT)
    pending = (*(source_id for source_id in pending if source_id in waiting), *unscored)
    types = (*typed, *untyped)
    source_ids = [paper.source_id for paper in types]
    if len(set(source_ids)) != len(source_ids):
        raise PaperMarkdownError("duplicate source IDs in the PDF/type corpus")
    keywords = {**_load_keywords(typed), **{record.source_id: _no_keywords(record) for record in untyped}}
    oa, oa_unconfirmed = _load_oa(source_ids)
    special, special_unconfirmed = _load_special(source_ids)
    references, reference_unavailable = _load_references(source_ids)
    levels, non_forensic = _load_levels(source_ids, taxonomy)
    targets = _load_targets(source_ids)
    metadata = _load_database_metadata(set(source_ids))
    for record in types:
        source_id = record.source_id
        database_doi = metadata[source_id]["doi"]
        keyword_doi = keywords[source_id].doi
        if (
            record.doi
            and database_doi
            and _doi_key(record.doi) != _doi_key(database_doi)
        ):
            raise PaperMarkdownError(f"type/database DOI mismatch: {source_id}")
        if (
            keyword_doi
            and database_doi
            and _doi_key(keyword_doi) != _doi_key(database_doi)
        ):
            raise PaperMarkdownError(f"keyword/database DOI mismatch: {source_id}")
        if (
            record.doi
            and keyword_doi
            and _doi_key(record.doi) != _doi_key(keyword_doi)
        ):
            raise PaperMarkdownError(f"type/keyword DOI mismatch: {source_id}")
    record_ids = assign_record_ids(
        source_ids, identities,
        preferred_numbers={record.source_id: record.paper_index for record in types},
    )
    papers = tuple(
        PaperData(
            record_id=record_ids[record.source_id],
            type_record=record,
            keyword=keywords[record.source_id],
            metadata=metadata[record.source_id],
            is_special_issue=special[record.source_id],
            oa_classification=oa[record.source_id],
            reference_rows=references[record.source_id],
            techniques=(),
            reference_record_ids=_reference_record_ids(
                references[record.source_id], record_ids
            ),
            classification=levels[record.source_id],
            target_applications=targets[record.source_id][1],
            tags=targets[record.source_id][0],
        )
        for record in types
    )
    return CorpusData(
        papers=tuple(sorted(papers, key=lambda paper: paper.record_id)),
        metadata_coverage=len(metadata),
        oa_unconfirmed=oa_unconfirmed,
        special_issue_count=sum(bool(value) for value in special.values()),
        reference_unavailable_count=reference_unavailable,
        non_forensic_count=non_forensic,
        special_unconfirmed=special_unconfirmed,
        pending_source_ids=pending,
        untyped_source_ids=untyped_ids,
    )


def parse_authors(values: Sequence[str] | None) -> tuple[str, str]:
    if not values:
        return MISSING, MISSING
    names: list[str] = []
    affiliations: list[str] = []
    for item in values:
        depth = 0
        balanced = True
        for character in item:
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth < 0:
                    balanced = False
        balanced = balanced and depth == 0
        if "(" in item and item.endswith(")") and balanced:
            name, affiliation = item.split("(", 1)
            name = name.strip() or MISSING
            affiliation = affiliation[:-1].strip() or MISSING
        elif "(" in item:
            name = item.split("(", 1)[0].strip() or item.strip() or MISSING
            affiliation = MISSING
        else:
            name = item.strip() or MISSING
            affiliation = MISSING
        names.append(name)
        affiliations.append(f"{name}: {affiliation}")
    if not names:
        return MISSING, MISSING
    return ", ".join(names), "\n".join(affiliations)


def _reference_authors(value: str | None) -> str | None:
    """KCI separates a reference's authors with ``;``; the profile uses ``, ``.

    ``"정일훈 ; 오정훈 ; 이상진"`` becomes ``"정일훈, 오정훈, 이상진"``.  Commas
    that KCI itself put inside the field (``"Smith, J."``) are left untouched.
    """

    if not value:
        return None
    names = [name.strip() for name in value.split(";")]
    return ", ".join(name for name in names if name) or None


def _reference_value(row: Mapping[str, Any], field: str) -> str | None:
    if field == "reference_doi":
        return _doi(row.get(field))
    return _optional_text(row.get(field))


def format_reference(
    row: Mapping[str, Any], language: str, *, include_url: bool = True
) -> str:
    labels = (
        {
            "volume": "권",
            "issue": "호",
            "pages": "쪽",
            "serial": "일련번호",
            "degree": "학위",
            "university": "대학",
            "department": "학과",
            "accessed": "접속일",
            "registered": "등록일",
        }
        if language == "kor"
        else {
            "volume": "Vol.",
            "issue": "No.",
            "pages": "pp.",
            "serial": "Serial",
            "degree": "Degree",
            "university": "University",
            "department": "Department",
            "accessed": "Accessed",
            "registered": "Registered",
        }
    )
    citation = _reference_value(row, "reference_citation_text")
    if citation:
        # KCI delivered this reference as one citation string; it is the value.
        return citation
    parts: list[str] = []
    author = _reference_authors(_reference_value(row, "reference_author"))
    if author:
        parts.append(author)
    parts.append(_reference_value(row, "reference_title") or MISSING)
    for field in (
        "reference_journal_name",
        "reference_conference_name",
        "reference_site_name",
        "reference_publisher",
    ):
        value = _reference_value(row, field)
        if value and value not in parts:
            parts.append(value)
    volume = _reference_value(row, "reference_volume")
    issue = _reference_value(row, "reference_issue")
    if volume:
        parts.append(f"{labels['volume']} {volume}")
    if issue:
        parts.append(f"{labels['issue']} {issue}")
    pages = _reference_value(row, "reference_pages")
    # KCI records an unknown page range as a bare "-" (start and end both empty).
    if pages and pages.strip("-–~ "):
        parts.append(f"{labels['pages']} {pages}")
    year = _reference_value(row, "reference_publication_year")
    if year:
        parts.append(year)
    for field, label in (
        ("reference_serial_number", labels["serial"]),
        ("reference_degree", labels["degree"]),
        ("reference_university", labels["university"]),
        ("reference_department", labels["department"]),
    ):
        value = _reference_value(row, field)
        if value:
            parts.append(f"{label}: {value}")
    doi = _reference_value(row, "reference_doi")
    if doi:
        parts.append(f"DOI: {doi}")
    url = _reference_value(row, "reference_url")
    if url and include_url:
        parts.append(f"URL: {url}")
    accessed = _reference_value(row, "reference_citation_date")
    if accessed:
        parts.append(f"{labels['accessed']}: {accessed}")
    registered = _reference_value(row, "reference_registration_date")
    if registered:
        parts.append(f"{labels['registered']}: {registered}")
    return ". ".join(parts).rstrip(". ") + "."


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _heading_text(value: str) -> str:
    return html.escape(_optional_text(value) or MISSING, quote=False)


def _plain_text(value: str | None) -> str:
    return html.escape(_optional_text(value) or MISSING, quote=False)


def _table_text(value: str | None) -> str:
    text = html.escape(_optional_text(value, preserve_lines=True) or MISSING, quote=False)
    return text.replace("|", "&#124;").replace("\n", "<br>")


def _reference_cell(row: Mapping[str, Any], language: str) -> str:
    """One ``제목``/``TITLE`` cell: a Markdown link when the source records a URL."""

    url = _reference_value(row, "reference_url")
    if not url:
        return _table_text(format_reference(row, language))
    label = _table_text(format_reference(row, language, include_url=False))
    # Brackets and parentheses inside the label or target would end the link
    # early, so escape the label and percent-encode the delimiters in the target.
    label = label.replace("[", "\\[").replace("]", "\\]")
    target = (
        _table_text(url)
        .replace("(", "%28")
        .replace(")", "%29")
        .replace(" ", "%20")
    )
    return f"[{label}]({target})"


def _comment_text(value: str) -> str:
    """A trailing YAML comment: one line, no comment or delimiter characters."""

    text = re.sub(r"\s+", " ", value).strip()
    if not text or "#" in text or "---" in text:
        raise PaperMarkdownError(f"unsafe front-matter comment: {value!r}")
    return text


def _abstract_text(value: str | None) -> str:
    text = html.escape(_optional_text(value, preserve_lines=True) or MISSING, quote=False)
    return text.replace("\n", "<br>\n")


def _type_label(paper: PaperData) -> str:
    # No type decision yet (analysis pending): N/A, never a guessed type.
    return "Special Issue" if paper.is_special_issue else paper.type_record.subtype_des or MISSING


def _journal_label(value: Any, language: str) -> str:
    del language
    from Analyzer.journal_names import resolve_journal_name

    # A KCI serial id (``SER…``) is shown by its reviewed name, or as missing when it has none.
    return resolve_journal_name(_optional_text(value)) or MISSING


def _issue_label(value: Any, language: str) -> str:
    issue = _optional_text(value)
    if issue is None:
        return MISSING
    match = re.fullmatch(r"([0-9]+)_([0-9]+)", issue)
    if match:
        volume, number = match.groups()
        return f"{volume}권 {number}호" if language == "kor" else f"Vol. {volume}, No. {number}"
    # Do not invent a volume/issue interpretation for nonstandard source values.
    return issue


def render_document(
    paper: PaperData,
    *,
    language: str,
    updated_on: str,
    author: str,
    taxonomy: TechniqueTaxonomy | None = None,
) -> str:
    if language not in {"kor", "eng"}:
        raise PaperMarkdownError(f"unsupported language: {language}")
    taxonomy = taxonomy or load_technique_taxonomy()
    metadata = paper.metadata
    title = (
        metadata.get("title_kor") if language == "kor" else metadata.get("title_eng")
    ) or MISSING
    titles = taxonomy.title_kor if language == "kor" else taxonomy.title_eng
    classification_block = (
        "classification: []"
        if not paper.classification
        else "classification:\n"
        + "\n".join(
            f"  - {identifier} # {_comment_text(titles[identifier])}"
            for identifier in paper.classification
        )
    )
    tags = paper.tags
    tag_block = (
        "tags: []"
        if not tags
        else "tags:\n" + "\n".join(f"  - {tag}" for tag in tags)
    )
    authors, affiliations = parse_authors(metadata.get("authors"))
    keywords = ", ".join(metadata.get("keywords", ())) or MISSING
    abstract = (
        metadata.get("abstract_kor")
        if language == "kor"
        else metadata.get("abstract_eng")
    )
    values = (
        metadata.get("doi") or MISSING,
        metadata.get("url") or MISSING,
        _type_label(paper),
        _journal_label(metadata.get("journal_id"), language),
        _issue_label(metadata.get("issue_id"), language),
        metadata.get("institution_id") or MISSING,
        _publication_date(metadata.get("publication_year"), metadata.get("publication_month")),
        {"Open Access": "O", "Not Open Access": "X", MISSING: MISSING}[paper.oa_classification],
        authors,
        affiliations,
    )
    if language == "kor":
        information_heading = "## 1. 기본 정보"
        item_header = "항목"
        value_header = "내용"
        labels = (
            "DOI",
            "URL",
            "유형",
            "논문지",
            "권호",
            "출판 기관",
            "출판 날짜",
            "OA 여부",
            "저자",
            "소속 기관",
        )
        abstract_heading = "## 2. 초록"
        keyword_heading = "## 3. 키워드"
        application_heading = "## 4. 대상 애플리케이션"
        application_id_header = "ID"
        application_name_header = "이름"
        reference_heading = "## 5. 참고문헌"
        reference_id_header = "ID"
        reference_title_header = "REF"
    else:
        information_heading = "## 1. Basic Information"
        item_header = "Item"
        value_header = "Value"
        labels = (
            "DOI",
            "URL",
            "Type",
            "Journal",
            "Vol. and Issue",
            "Publisher",
            "Publication Date",
            "OA",
            "Authors",
            "Affiliations",
        )
        abstract_heading = "## 2. Abstract"
        keyword_heading = "## 3. Keywords"
        application_heading = "## 4. Target Applications"
        application_id_header = "ID"
        application_name_header = "Name"
        reference_heading = "## 5. References"
        reference_id_header = "ID"
        reference_title_header = "REF"
    information_rows = "\n".join(
        f"| {label} | {_table_text(value)} |"
        for label, value in zip(labels, values, strict=True)
    )
    if paper.reference_rows:
        reference_rows = "\n".join(
            f"| {paper.reference_record_ids.get(int(row['reference_order']), '')} | "
            f"{_reference_cell(row, language)} |"
            for row in paper.reference_rows
        )
    else:
        reference_rows = f"|  | {MISSING} |"
    if paper.target_applications:
        application_rows = "\n".join(
            f"| {identifier or ''} | {_table_text(name_kor if language == 'kor' else name_eng)} |"
            for identifier, name_kor, name_eng in paper.target_applications
        )
    else:
        application_rows = f"|  | {MISSING} |"
    return (
        "---\n"
        f"subject: {_yaml_string(title)}\n"
        f"identifier: {_yaml_string(paper.record_id)}\n"
        f"{classification_block}\n"
        f"{tag_block}\n"
        f"last_updated: {_yaml_string(updated_on)}\n"
        f"author: {_yaml_string(author)}\n"
        "---\n\n"
        f"# {_heading_text(title)}\n\n"
        f"{information_heading}\n"
        f"| {item_header} | {value_header} |\n"
        "|---|---|\n"
        f"{information_rows}\n\n"
        f"{abstract_heading}\n"
        '<div style="border:1px solid #ccc; padding:12px; border-radius:6px;">\n'
        f"{_abstract_text(abstract)}\n"
        "</div>\n\n"
        f"{keyword_heading}\n"
        f"{_plain_text(keywords)}\n\n"
        f"{application_heading}\n"
        f"| {application_id_header} | {application_name_header} |\n"
        "|---|---|\n"
        f"{application_rows}\n\n"
        f"{reference_heading}\n"
        f"| {reference_id_header} | {reference_title_header} |\n"
        "|---|---|\n"
        f"{reference_rows}\n"
    )


def _validate_document(
    text: str,
    paper: PaperData,
    *,
    language: str,
    updated_on: str,
    author: str,
    taxonomy: TechniqueTaxonomy | None = None,
) -> None:
    taxonomy = taxonomy or load_technique_taxonomy()
    expected = render_document(
        paper,
        language=language,
        updated_on=updated_on,
        author=author,
        taxonomy=taxonomy,
    )
    if text != expected:
        raise PaperMarkdownError(
            f"generated content mismatch: {language}/{paper.type_record.source_id}"
        )
    forbidden = (
        "{{",
        "<!--",
        "키워드1",
        "Keyword1",
        "참고문헌명",
        "Reference Title",
        "REF-00000",
        "REF-00001",
        "<![CDATA[",
        "KNOW-DOMP-0000000",
        "애플리케이션 이름",
        "Application name",
        "EXAM-04-01-00-000",
        "| | |",
    )
    if any(token in text for token in forbidden):
        raise PaperMarkdownError(
            f"template placeholder remains: {language}/{paper.type_record.source_id}"
        )
    sections = tuple(line.strip() for line in text.splitlines() if line.startswith("## "))
    if len(sections) != 5:
        raise PaperMarkdownError(
            f"section count mismatch: {language}/{paper.type_record.source_id}"
        )
    front_matter = text.split("---", 2)
    if len(front_matter) != 3:
        raise PaperMarkdownError(
            f"invalid front matter: {language}/{paper.type_record.source_id}"
        )
    identifier_match = re.search(
        r'^identifier:\s*("(?:[^"\\]|\\.)*")\s*$',
        front_matter[1],
        re.MULTILINE,
    )
    if not identifier_match or json.loads(identifier_match.group(1)) != paper.record_id:
        raise PaperMarkdownError(
            f"front-matter identity mismatch: {language}/{paper.type_record.source_id}"
        )
    classification_block, _, tag_block = front_matter[1].partition("\ntags:")
    classification = _template_sequence(classification_block)
    if classification != paper.classification:
        raise PaperMarkdownError(
            f"front-matter classification mismatch: {language}/{paper.type_record.source_id}"
        )
    if not set(classification) <= set(taxonomy.identifiers):
        raise PaperMarkdownError(
            f"front-matter classification is outside the taxonomy: "
            f"{language}/{paper.type_record.source_id}"
        )
    if _template_sequence(tag_block) != paper.tags or not set(paper.tags) <= set(ALLOWED_TAGS):
        raise PaperMarkdownError(
            f"front-matter tags mismatch: {language}/{paper.type_record.source_id}"
        )
    if not text.endswith("\n"):
        raise PaperMarkdownError(
            f"missing final newline: {language}/{paper.type_record.source_id}"
        )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PaperMarkdownError(f"unsafe output target: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _expected_paths(output: Path, papers: Sequence[PaperData]) -> set[Path]:
    return {
        output / paper.record_id / LANGUAGE_FILES[language]
        for language in ("kor", "eng")
        for paper in papers
    }


def _safe_output_path(output: Path) -> Path:
    expanded = output.expanduser()
    absolute = Path(os.path.abspath(expanded))
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise PaperMarkdownError(
                f"symlink is not allowed in output path: {candidate}"
            )
    if absolute.is_symlink() or (absolute.exists() and not absolute.is_dir()):
        raise PaperMarkdownError(f"unsafe output directory: {absolute}")
    if absolute.exists():
        for root, directory_names, file_names in os.walk(
            absolute, followlinks=False
        ):
            for name in (*directory_names, *file_names):
                candidate = Path(root) / name
                if candidate.is_symlink():
                    raise PaperMarkdownError(
                        f"symlink is not allowed in output directory: {candidate}"
                    )
    for language in ("kor", "eng"):
        directory = absolute / language
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise PaperMarkdownError(f"unsafe language output directory: {directory}")
    return absolute


def validate_output(
    output: Path,
    corpus: CorpusData,
    *,
    updated_on: str,
    author_kor: str,
    author_eng: str,
) -> GenerationSummary:
    expected_paths = _expected_paths(output, corpus.papers)
    actual_paths = set(output.rglob("*.md")) if output.exists() else set()
    if actual_paths != expected_paths:
        raise PaperMarkdownError(
            f"output file set mismatch: missing={len(expected_paths - actual_paths)}, "
            f"unexpected={len(actual_paths - expected_paths)}"
        )
    tag_counts: Counter[str] = Counter()
    authors = {"kor": author_kor, "eng": author_eng}
    taxonomy = load_technique_taxonomy()
    for language in ("kor", "eng"):
        for paper in corpus.papers:
            path = output / paper.record_id / LANGUAGE_FILES[language]
            if path.is_symlink() or not path.is_file():
                raise PaperMarkdownError(f"unsafe/missing generated file: {path}")
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise PaperMarkdownError(f"unable to read generated file {path}: {exc}") from exc
            _validate_document(
                text,
                paper,
                language=language,
                updated_on=updated_on,
                author=authors[language],
                taxonomy=taxonomy,
            )
            if language == "kor":
                tag_counts.update(paper.tags)
    # Tags come from the Jev AI results (TARGETS_JSON), not from the keyword workbook any more,
    # so the workbook's unmapped values say nothing about them.
    unmapped_values: dict[str, tuple[str, ...]] = {}
    unmapped_paper_counts: dict[str, int] = {}
    return GenerationSummary(
        paper_count=len(corpus.papers),
        markdown_count=len(actual_paths),
        metadata_coverage=corpus.metadata_coverage,
        oa_unconfirmed=corpus.oa_unconfirmed,
        special_issue_count=corpus.special_issue_count,
        reference_unavailable_count=corpus.reference_unavailable_count,
        non_forensic_count=corpus.non_forensic_count,
        tag_counts=dict(tag_counts),
        unmapped_values=unmapped_values,
        unmapped_paper_counts=unmapped_paper_counts,
        special_unconfirmed=corpus.special_unconfirmed,
        pending_source_ids=corpus.pending_source_ids,
        untyped_source_ids=corpus.untyped_source_ids,
    )


def generate(
    output: Path = DEFAULT_OUTPUT,
    *,
    updated_on: str | None = None,
    author_kor: str = DEFAULT_KOREAN_AUTHOR,
    author_eng: str = DEFAULT_ENGLISH_AUTHOR,
    validate_only: bool = False,
) -> GenerationSummary:
    updated_on = updated_on or date.today().isoformat()
    try:
        date.fromisoformat(updated_on)
    except ValueError:
        raise PaperMarkdownError(f"invalid updated-on date: {updated_on}") from None
    if not author_kor.strip() or not author_eng.strip():
        raise PaperMarkdownError("Korean and English authors must be non-empty")
    authors = {"kor": author_kor.strip(), "eng": author_eng.strip()}
    output = _safe_output_path(output)
    corpus = load_corpus(output)
    expected_paths = _expected_paths(output, corpus.papers)
    existing_markdown = set(output.rglob("*.md")) if output.exists() else set()
    unexpected = existing_markdown - expected_paths
    if unexpected:
        raise PaperMarkdownError(
            f"refusing to overwrite/delete unexpected Markdown files: "
            f"{sorted(str(path) for path in unexpected)[:5]}"
        )
    if not validate_only:
        taxonomy = load_technique_taxonomy()
        for language in ("kor", "eng"):
            for paper in corpus.papers:
                text = render_document(
                    paper,
                    language=language,
                    updated_on=updated_on,
                    author=authors[language],
                    taxonomy=taxonomy,
                )
                _validate_document(
                    text,
                    paper,
                    language=language,
                    updated_on=updated_on,
                    author=authors[language],
                    taxonomy=taxonomy,
                )
                _atomic_write(output / paper.record_id / LANGUAGE_FILES[language], text)
    summary = validate_output(
        output,
        corpus,
        updated_on=updated_on,
        author_kor=authors["kor"],
        author_eng=authors["eng"],
    )
    if not validate_only and output == DEFAULT_OUTPUT.resolve():
        save_profile_identities(corpus)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--updated-on", default=date.today().isoformat())
    parser.add_argument("--author-kor", default=DEFAULT_KOREAN_AUTHOR)
    parser.add_argument("--author-eng", default=DEFAULT_ENGLISH_AUTHOR)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        summary = generate(
            arguments.output,
            updated_on=arguments.updated_on,
            author_kor=arguments.author_kor,
            author_eng=arguments.author_eng,
            validate_only=arguments.validate_only,
        )
    except (PaperMarkdownError, OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    tag_summary = ", ".join(
        f"{tag}={count}" for tag, count in summary.tag_counts.items()
    )
    print(
        f"[DONE] papers={summary.paper_count} md={summary.markdown_count} "
        f"db={summary.metadata_coverage}/{summary.paper_count} "
        f"oa_NA={summary.oa_unconfirmed} "
        f"special={summary.special_issue_count} "
        f"special_NA={summary.special_unconfirmed} "
        f"references_NA={summary.reference_unavailable_count} "
        f"without_confirmed_level={summary.non_forensic_count} "
        f"pending={len(summary.pending_source_ids)} "
        f"type_NA={len(summary.untyped_source_ids)}"
    )
    if summary.pending_source_ids:
        print(f"[PENDING] no profile yet (waiting for the analysis or for the Jev AI result): {', '.join(summary.pending_source_ids)}")
    if summary.untyped_source_ids:
        print(f"[UNTYPED] profile written with type N/A (analysis pending): {', '.join(summary.untyped_source_ids)}")
    print(f"[TAGS] {tag_summary}")
    for field, values in summary.unmapped_values.items():
        print(
            f"[UNMAPPED] {field} papers={summary.unmapped_paper_counts[field]} "
            f"values={', '.join(values)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
