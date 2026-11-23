"""Render one paper's Claude analysis record into the Analyzer evidence files.

The reviewed 527-paper corpus was documented by an interactive Codex session
that wrote, per paper, a Korean and an English Structure document
(``Analyzer/Structure/{kor,eng}/<ART>.md``), a Level-3 manifest
(``Analyzer/Manifest/Lv3/<ART>.json``) and one monolithic type-review manifest.
This module produces the same artifacts deterministically from the structured
record returned by :class:`PaperPipeline.claude_analyzer.ClaudePaperAnalyzer`,
so the downstream validators (``Analyzer/Type/paper_type.py``,
``Analyzer/paper_lv3.py``, ``Analyzer/MD/paper_md.py``) keep their contracts:

* the Structure document carries the ``연구 유형`` / ``포렌식 관련성`` fields,
  the ``### 핵심 요약`` line and the ``E-xx`` evidence table those validators
  parse, with every referenced evidence id present in the table;
* the Level-3 manifest uses schema ``1.0`` with canonical
  ``Analyzer/LV/Techniques/tech_taxonomy_v5.2.json`` path ids, and binds itself to the
  PDF, the Korean Structure document and this record (``intelligence_sha256``
  is the hash of the record's keyword section, not of a shared file);
* the document-type decision is a per-paper file under
  ``Analyzer/Manifest/Type/`` bound to the same hashes.

Nothing here talks to the network; the analysis record is the only input.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from PaperPipeline.claude_analyzer import (
    AnalysisVocabulary,
    RESEARCH_TYPES,
    TaxonomyEntry,
)
from PaperPipeline.storage import write_json_atomic, write_text_atomic
from Analyzer.tag_taxonomy import default_tag_taxonomy
from Analyzer.technique_ids import legacy_technique_id


ANALYZER_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
DEFAULT_TECHNIQUE_TAXONOMY = ANALYZER_DIR / "LV" / "Techniques" / "tech_taxonomy_v5.2.json"
DEFAULT_TECHNIQUE_MANIFEST = (
    REPOSITORY_ROOT / "web" / "DF-Insight" / "data" / "manifest.json"
)
DEFAULT_STRUCTURE_DIR = ANALYZER_DIR / "Structure"
DEFAULT_LV3_DIR = ANALYZER_DIR / "Manifest" / "Lv3"
DEFAULT_TYPE_DECISION_DIR = ANALYZER_DIR / "Manifest" / "Type"
DEFAULT_ANALYSIS_DIR = ANALYZER_DIR / "Manifest" / "Analysis"
DEFAULT_TYPE_TAXONOMY = ANALYZER_DIR / "Type" / "paper_type.md"

TYPE_DECISION_SCHEMA_VERSION = "1.0"
LV3_MANIFEST_SCHEMA_VERSION = "1.0"

# Fixed application-category vocabulary shared with the keyword workbook and
# the profile tag axis (``Analyzer/MD/paper_md.py`` ``APPLICATION_TAGS``).
# Closed application-category vocabulary of the analyzer: the APP axis of the
# user-maintained tag taxonomy (``Analyzer/Tags/tags_taxonomy.json``).
APPLICATION_CATEGORIES: tuple[str, ...] = default_tag_taxonomy().application_categories

# Korean labels used by the Structure documents (parsed back by paper_type.py).
RESEARCH_TYPE_LABELS_KO = {
    "ACQUISITION_METHOD": "수집·복구 방법론",
    "DETECTION_METHOD": "탐지·분류 방법론",
    "EDUCATION_TRAINING": "교육·훈련",
    "EMPIRICAL_ARTIFACT": "실증 아티팩트 분석",
    "LAW_CASE_POLICY": "법·판례·정책",
    "NON_FORENSIC": "포렌식 비관련",
    "REVIEW_SURVEY": "문헌고찰·동향",
    "TOOL_SYSTEM": "도구·시스템 개발",
}
RESEARCH_TYPE_LABELS_EN = {
    "ACQUISITION_METHOD": "Acquisition and recovery method",
    "DETECTION_METHOD": "Detection and classification method",
    "EDUCATION_TRAINING": "Education and training",
    "EMPIRICAL_ARTIFACT": "Empirical artifact analysis",
    "LAW_CASE_POLICY": "Law, case and policy",
    "NON_FORENSIC": "Non-forensic",
    "REVIEW_SURVEY": "Literature review and survey",
    "TOOL_SYSTEM": "Tool and system development",
}
RELEVANCE_LABELS_EN = {"관련": "Relevant", "부분 관련": "Partially relevant", "비관련": "Not relevant"}
# Research types whose scope evidence is a discussion rather than an observation.
_LITERATURE_TYPES = {"LAW_CASE_POLICY", "REVIEW_SURVEY", "EDUCATION_TRAINING", "NON_FORENSIC"}

NR_KO = "논문 미제시 (NR)"
NR_EN = "Not reported (NR)"
NA_KO = "해당 없음 (N/A)"
NA_EN = "Not applicable (N/A)"

_KCI_URL = "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId={source_id}"
_INDEX_ROW_RE = re.compile(
    r"^\| (ART\d{9}) \| ([^|]+?) \| ([^|]+?) \| \[kor\]\(kor/ART\d{9}\.md\) \| \[eng\]\(eng/ART\d{9}\.md\) \| (\d+) \| (\d+) \|\s*$",
    re.MULTILINE,
)


class EvidenceWriterError(RuntimeError):
    """The analysis record cannot be rendered into consistent artifacts."""


@dataclass(frozen=True, slots=True)
class PaperMetadata:
    """The database fields the Structure document reproduces."""

    source_id: str
    title: str
    title_eng: str | None
    authors: tuple[str, ...]
    journal: str | None
    publication_year: int | None
    doi: str | None
    pdf_filename: str


@dataclass(frozen=True, slots=True)
class WrittenArtifacts:
    analysis_path: Path
    structure_ko_path: Path
    structure_en_path: Path
    lv3_path: Path
    type_decision_path: Path
    structure_sha256: str
    lv3_sha256: str


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceWriterError(f"unable to read {path}: {exc}") from exc


def load_vocabulary(
    taxonomy_path: Path = DEFAULT_TECHNIQUE_TAXONOMY,
    manifest_path: Path = DEFAULT_TECHNIQUE_MANIFEST,
    *,
    application_categories: Sequence[str] = APPLICATION_CATEGORIES,
) -> AnalysisVocabulary:
    """Build the closed vocabularies from the canonical technique taxonomy (v5.2).

    ``tech_taxonomy_v5.2.json`` carries the ids, English titles and the
    definition / indicators / insufficient_alone of every Level-3 technique; the
    Korean titles come from ``tech_titles_ko.json`` beside it, so every rendered
    document stays bilingual.  ``manifest_path`` is no longer needed (kept for
    callers that pass it).
    """

    from Analyzer.technique_taxonomy import TaxonomyError, load_taxonomy

    del manifest_path
    try:
        taxonomy = load_taxonomy(taxonomy_path)
    except TaxonomyError as exc:
        raise EvidenceWriterError(f"{taxonomy_path}: {exc}") from exc
    entries = [
        TaxonomyEntry(
            path_id=item.id,
            group_id=item.level_2_id,
            group_title_ko=item.level_2_ko,
            group_title_en=item.level_2_en,
            title_ko=item.title_ko,
            title_en=item.title_en,
            description_ko=item.description_text(),
        )
        for item in taxonomy.techniques.values()
    ]
    return AnalysisVocabulary(
        taxonomy=tuple(entries),
        taxonomy_sha256=taxonomy.sha256,
        application_categories=tuple(application_categories),
        device_examples=("PC", "노트북", "스마트폰", "태블릿", "드론", "IoT 기기", "자동차", "CCTV", "서버"),
        filesystem_examples=("NTFS", "FAT32", "exFAT", "ext4", "APFS", "F2FS"),
        operating_system_examples=("Windows", "Linux", "macOS", "Android", "iOS"),
        cloud_examples=("SaaS", "IaaS", "DaaS", "PaaS", "AWS", "Azure", "iCloud"),
    )


# --------------------------------------------------------------------------- #
# Record hashing helpers
# --------------------------------------------------------------------------- #

def keyword_section_sha256(record: Mapping[str, Any]) -> str:
    """Per-record replacement for the legacy whole-file ``intelligence_sha256``."""

    payload = json.dumps(
        {
            "source_id": record["source_id"],
            "verdict": record["relevance"]["verdict"],
            "keywords": record["keywords"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Structure document rendering
# --------------------------------------------------------------------------- #

def _or(value: str | None, missing: str) -> str:
    return value if value else missing


def _cell(value: str) -> str:
    """Make a value safe inside a Markdown table cell.

    Pipes are replaced rather than escaped so the text a validator parses back
    out of the document equals the text stored in the manifests.
    """

    return " ".join(value.replace("|", "/").split())


def _code(value: str) -> str:
    return f"<code>{_cell(value)}</code>"


def _tags(record: Mapping[str, Any]) -> list[str]:
    keywords = record["keywords"]
    tags: list[str] = []
    for prefix, values in (
        ("Dev", keywords["device"]),
        ("FS", keywords["filesystem"]),
        ("OS", keywords["operating_system"]),
        ("APP", sorted({item["category"] for item in keywords["application"]})),
        ("CLOUD", keywords["cloud"]),
    ):
        for value in values:
            tag = f"{prefix}-{value}".replace("\n", " ").strip()
            if tag not in tags:
                tags.append(tag)
    return tags


def _direct_targets(record: Mapping[str, Any], language: str) -> str:
    keywords = record["keywords"]
    labels = (
        ("기기", "Device", keywords["device"]),
        ("파일시스템", "Filesystem", keywords["filesystem"]),
        ("운영체제", "Operating system", keywords["operating_system"]),
        ("애플리케이션", "Application", [item["name"] for item in keywords["application"]]),
        ("클라우드", "Cloud", keywords["cloud"]),
    )
    parts = [
        f"{ko if language == 'ko' else en}: {', '.join(values)}"
        for ko, en, values in labels
        if values
    ]
    if parts:
        return "; ".join(parts)
    return record["relevance"]["summary_ko" if language == "ko" else "summary_en"]


def _summary_status(record: Mapping[str, Any]) -> str:
    """Status marker of the core-summary line; paper_lv3 accepts OBS/PROP/LIT."""

    scope_status = record["relevance"]["scope_evidence"]["status"]
    if scope_status in {"OBS", "PROP", "LIT"}:
        return scope_status
    return "LIT" if record["relevance"]["research_type"] in _LITERATURE_TYPES else "OBS"


@dataclass
class _Evidence:
    rows: list[tuple[str, str, str, str, str]]  # id, status, pages, support_ko, support_en

    def add(self, status: str, pages: str, support_ko: str, support_en: str) -> str:
        identifier = f"E-{len(self.rows) + 1:02d}"
        self.rows.append((identifier, status, pages, support_ko, support_en))
        return identifier


def render_structure_documents(
    record: Mapping[str, Any],
    metadata: PaperMetadata,
    *,
    updated_on: str,
    author: str,
) -> tuple[str, str]:
    """Return the Korean and English Structure documents for ``record``."""

    if record["source_id"] != metadata.source_id:
        raise EvidenceWriterError("record and metadata describe different papers")
    relevance = record["relevance"]
    research_type = relevance["research_type"]
    if research_type not in RESEARCH_TYPES:
        raise EvidenceWriterError(f"unknown research_type {research_type!r}")
    non_forensic = relevance["verdict"] == "비관련"
    scope = relevance["scope_evidence"]
    summary_status = _summary_status(record)
    structure = record.get("structure") or None

    evidence = _Evidence(rows=[])
    evidence.add("META", "PDF p. 1", "논문 제목과 서지 메타데이터.", "Paper title and bibliographic metadata.")
    scope_id = evidence.add(summary_status, scope["pages"], scope["support_ko"], scope["support_en"])
    assert scope_id == "E-02"

    root_path_id = install_id = None
    artifact_ids: list[str] = []
    tool_ids: list[str] = []
    if structure is not None:
        identification = structure["identification"]
        if identification["root_path"]:
            root_path_id = evidence.add(
                identification["root_path_status"],
                identification["evidence"]["pages"],
                identification["evidence"]["support_ko"]
                or f"직접 대상 맥락에서 명시된 경로: {identification['root_path']}",
                identification["evidence"]["support_en"]
                or f"Path stated in direct-target context: {identification['root_path']}",
            )
        for artifact in structure["artifacts"]:
            artifact_ids.append(
                evidence.add(
                    artifact["status"],
                    artifact["evidence"]["pages"],
                    artifact["evidence"]["support_ko"]
                    or f"직접 대상 맥락에서 명시된 아티팩트: {artifact['name']}",
                    artifact["evidence"]["support_en"]
                    or f"Artifact named in direct-target context: {artifact['name']}",
                )
            )
        for tool in structure["tools"]:
            tool_ids.append(
                evidence.add(
                    tool["evidence"]["status"],
                    tool["evidence"]["pages"],
                    tool["evidence"]["support_ko"] or f"지원 도구: {tool['name']}",
                    tool["evidence"]["support_en"] or f"Supporting tool: {tool['name']}",
                )
            )
    accepted = len(evidence.rows) - 2
    excluded = int(structure["excluded_candidate_count"]) if structure else 0

    documents: dict[str, str] = {}
    for language in ("ko", "en"):
        ko = language == "ko"
        nr = NR_KO if ko else NR_EN
        na = NA_KO if ko else NA_EN
        missing = na if non_forensic else nr
        title_display = metadata.title if ko else (record.get("english_title") or metadata.title_eng or metadata.title)
        lines: list[str] = []
        tags = _tags(record)
        lines.append("---")
        lines.append(f'subject: "{metadata.title}"')
        lines.append(f'identifier: "{metadata.source_id}"')
        if tags:
            lines.append("tags:")
            lines.extend(f"  - {tag}" for tag in tags)
        else:
            lines.append("tags: []")
        lines.append(f'last_updated: "{updated_on}"')
        lines.append(f'author: "{author}"')
        lines.append("---")
        lines.append("")
        lines.append(f"# {title_display}")
        lines.append("")
        lines.append("## 0. 논문 범위" if ko else "## 0. Paper Scope")
        lines.append("")
        lines.append("| 항목 | 내용 |" if ko else "| Item | Value |")
        lines.append("|---|---|")
        type_label = RESEARCH_TYPE_LABELS_KO[research_type] if ko else RESEARCH_TYPE_LABELS_EN[research_type]
        verdict_label = relevance["verdict"] if ko else RELEVANCE_LABELS_EN[relevance["verdict"]]
        method = type_label if not non_forensic else ("포렌식 비관련 연구" if ko else "Non-forensic research")
        limitations = relevance["explicit_limitations_ko" if ko else "explicit_limitations_en"] or nr
        validation_ko = (
            f"근거 페이지: {scope['pages']}. "
            + ("포렌식 비관련 판정의 근거 범위만 표시하며 기술 검증을 적용하지 않는다."
               if non_forensic else "논문이 직접 보고한 대상·실험·관찰 범위에만 한정한다.")
        )
        validation_en = (
            f"Evidence pages: {scope['pages']}. "
            + ("Only the basis of the non-forensic verdict is shown; no technical validation applies."
               if non_forensic else "Limited to the targets, experiments, and observations directly reported by the paper.")
        )
        rows0 = [
            ("원문 제목" if ko else "Original Title", f"{_cell(metadata.title)} [E-01]"),
            ("표시 제목" if ko else "Display Title", f"{_cell(title_display)} [E-01]"),
            ("연구 유형" if ko else "Research Type", f"{type_label} [E-02]"),
            ("포렌식 관련성" if ko else "Forensic Relevance", f"{verdict_label} [E-02]"),
            ("직접 연구 대상" if ko else "Direct Research Target", f"{_cell(_direct_targets(record, language))} [E-02]"),
            ("연구 방법" if ko else "Method", f"{method} [E-02]"),
            ("검증 수준" if ko else "Validation Level", f"{validation_ko if ko else validation_en} [E-02]"),
            ("명시적 한계" if ko else "Explicit Limitations", f"{_cell(limitations)} [E-02]"),
        ]
        lines.extend(f"| {label} | {value} |" for label, value in rows0)
        lines.append("")
        lines.append("### 핵심 요약" if ko else "### Core Summary")
        lines.append("")
        lines.append(f"- [{summary_status}] {_cell(relevance['summary_ko' if ko else 'summary_en'])} [E-02]")
        lines.append("")
        lines.append("### 서지 메타데이터" if ko else "### Bibliographic Metadata")
        lines.append("")
        lines.append("| 항목 | 내용 |" if ko else "| Item | Value |")
        lines.append("|---|---|")
        authors = ", ".join(metadata.authors) if metadata.authors else nr
        lines.append(f"| {'논문 저자' if ko else 'Paper Authors'} | {_cell(authors)} [E-01] |")
        lines.append(f"| {'학술지' if ko else 'Journal'} | {_cell(_or(metadata.journal, nr))} [E-01] |")
        lines.append(f"| {'발행연도' if ko else 'Year'} | {metadata.publication_year if metadata.publication_year else nr} [E-01] |")
        lines.append(f"| DOI | {_cell(_or(metadata.doi, nr))} [E-01] |")
        lines.append(f"| {'원문 URL' if ko else 'Source URL'} | {_KCI_URL.format(source_id=metadata.source_id)} [E-01] |")
        lines.append("")

        # 1. Identification
        lines.append("## 1. 식별" if ko else "## 1. Identification")
        lines.append("")
        lines.append("### 기본 정보" if ko else "### Basic Information")
        lines.append("")
        lines.append("| 항목 | 내용 |" if ko else "| Item | Value |")
        lines.append("|---|---|")
        version = structure["identification"]["version"] if structure else ""
        root_path = structure["identification"]["root_path"] if structure else ""
        install = structure["identification"]["install_check_path"] if structure else ""
        root_value = (
            f"[{structure['identification']['root_path_status']}] {_code(root_path)} [{root_path_id}]"
            if structure and root_path and root_path_id
            else missing
        )
        lines.append(f"| {'버전' if ko else 'Version'} | {_cell(version) if version else missing} |")
        lines.append(f"| {'저장 경로 (루트 경로)' if ko else 'Root Path'} | {root_value} |")
        lines.append(f"| {'설치 시점 확인 경로' if ko else 'Install Timestamp'} | {_code(install) if install else missing} |")
        lines.append("")
        lines.append("### 기능 명세" if ko else "### Feature Specification")
        lines.append("")
        lines.append("| 기능명 | 설명 | 기본 제공 여부 |" if ko else "| Feature | Description | Default Provided |")
        lines.append("|---|---|---|")
        features = structure["features"] if structure else []
        if features:
            for feature in features:
                name = feature["name_ko" if ko else "name_en"]
                description = feature["description_ko" if ko else "description_en"]
                default = feature["default_provided_ko" if ko else "default_provided_en"] or nr
                lines.append(f"| {_cell(name)} | {_cell(description)} [E-02] | {_cell(default)} |")
        elif non_forensic:
            lines.append(
                f"| {na} | {'기술 제품 기능을 직접 분석하지 않는 연구 유형임' if ko else 'This research type does not analyze product features directly'} [E-02] | {na} |"
            )
        else:
            lines.append(
                f"| {'직접 연구 범위' if ko else 'Direct research scope'} | {_cell(relevance['summary_ko' if ko else 'summary_en'])} [E-02] | {nr} |"
            )
        lines.append("")

        # 2. Preservation
        lines.append("## 2. 보존" if ko else "## 2. Preservation")
        lines.append("")
        if structure and structure["preservation_ko" if ko else "preservation_en"]:
            lines.append(f"- {_cell(structure['preservation_ko' if ko else 'preservation_en'])} [E-02]")
        elif non_forensic:
            lines.append(f"- {na} — {'비관련 연구로 디지털증거 보존 필드가 적용되지 않음' if ko else 'Evidence preservation fields do not apply to a non-forensic study'} [E-02]")
        else:
            lines.append(f"- {nr} — {'scope 및 고신뢰 후보가 구체적인 증거 보존 조치를 제공하지 않음' if ko else 'The scope and high-confidence candidates provide no concrete evidence-preservation measure'} [E-02]")
        lines.append("")

        # 3. Collection
        artifacts = structure["artifacts"] if structure else []
        lines.append("## 3. 수집" if ko else "## 3. Collection")
        lines.append("")
        lines.append("### 아티팩트 목록" if ko else "### Artifact List")
        lines.append("")
        if ko:
            lines.append("| 아티팩트명 | 관련 기능명 | 저장 경로 | 접근 권한 | 수집 방법 | 복호화 필요 여부 | 복호화를 위한 수집 필요 아티팩트 |")
        else:
            lines.append("| Artifact | Related Feature | Path | Access Privilege | Collection Method | Decryption Required | Artifacts Needed for Decryption |")
        lines.append("|---|---|---|---|---|---|---|")
        if artifacts:
            for artifact, evidence_id in zip(artifacts, artifact_ids, strict=True):
                suffix = "ko" if ko else "en"
                cells = [
                    f"[{artifact['status']}] {_code(artifact['name'])} [{evidence_id}]",
                    _cell(artifact["related_feature"]) or nr,
                    _code(artifact["path"]) if artifact["path"] else nr,
                    _cell(artifact[f"access_privilege_{suffix}"]) or nr,
                    _cell(artifact[f"collection_method_{suffix}"]) or nr,
                    _cell(artifact[f"decryption_required_{suffix}"]) or nr,
                    _cell(artifact[f"decryption_dependencies_{suffix}"]) or nr,
                ]
                lines.append("| " + " | ".join(cells) + " |")
        else:
            lines.append("| " + " | ".join([missing] * 7) + " |")
        lines.append("")
        for artifact, evidence_id in zip(artifacts, artifact_ids, strict=True):
            suffix = "ko" if ko else "en"
            lines.append(f"### {_code(artifact['name'])}")
            lines.append("")
            for label_ko, label_en, key in (
                ("저장 경로", "Path", None),
                ("접근 권한", "Access Privilege", f"access_privilege_{suffix}"),
                ("수집 방법", "Collection Method", f"collection_method_{suffix}"),
                ("복호화를 위한 수집 필요 아티팩트", "Artifacts Needed for Decryption", f"decryption_dependencies_{suffix}"),
            ):
                value = _code(artifact["path"]) if key is None and artifact["path"] else (
                    _cell(artifact[key]) if key and artifact[key] else nr
                )
                lines.append(f"- {label_ko if ko else label_en}")
                lines.append(f"  - {value} [{evidence_id}]")
            lines.append("")

        # 4. Examination
        lines.append("## 4. 조사" if ko else "## 4. Examination")
        lines.append("")
        lines.append("### 아티팩트 목록" if ko else "### Artifact List")
        lines.append("")
        if ko:
            lines.append("| 아티팩트명 | 관련 기능명 | 저장 형식 | 저장 내용 | 저장 기간 | 기본 제공 여부 |")
        else:
            lines.append("| Artifact | Related Feature | Storage Format | Stored Content | Retention Period | Default Provided |")
        lines.append("|---|---|---|---|---|---|")
        if artifacts:
            for artifact, evidence_id in zip(artifacts, artifact_ids, strict=True):
                suffix = "ko" if ko else "en"
                cells = [
                    f"[{artifact['status']}] {_code(artifact['name'])} [{evidence_id}]",
                    _cell(artifact["related_feature"]) or nr,
                    _cell(artifact[f"storage_format_{suffix}"]) or nr,
                    _cell(artifact[f"stored_content_{suffix}"]) or nr,
                    _cell(artifact[f"retention_{suffix}"]) or nr,
                    _cell(artifact[f"default_provided_{suffix}"]) or nr,
                ]
                lines.append("| " + " | ".join(cells) + " |")
        else:
            lines.append("| " + " | ".join([missing] * 6) + " |")
        lines.append("")
        for artifact, evidence_id in zip(artifacts, artifact_ids, strict=True):
            suffix = "ko" if ko else "en"
            lines.append(f"### {_code(artifact['name'])}")
            lines.append("")
            for label_ko, label_en, key in (
                ("저장 형식", "Storage Format", f"storage_format_{suffix}"),
                ("저장 내용", "Stored Content", f"stored_content_{suffix}"),
                ("저장 기간", "Retention Period", f"retention_{suffix}"),
                ("기본 제공 여부", "Default Provided", f"default_provided_{suffix}"),
            ):
                value = _cell(artifact[key]) if artifact[key] else nr
                lines.append(f"- {label_ko if ko else label_en}")
                lines.append(f"  - {value} [{evidence_id}]")
            lines.append("")
        lines.append("### 복호화 방안" if ko else "### Decryption Method")
        lines.append("")
        decryption = structure["decryption_ko" if ko else "decryption_en"] if structure else ""
        if decryption:
            lines.append(f"- {_cell(decryption)} [E-02]")
        else:
            lines.append(f"- {missing} — {'고신뢰 후보만으로 복호화 필요성·알고리즘을 확정하지 않음' if ko else 'High-confidence candidates alone are not used to assert decryption need or algorithm'} [E-02]")
        lines.append("")
        lines.append("### 안티포렌식 대응 방안" if ko else "### Anti-Forensic Countermeasures")
        lines.append("")
        anti = structure["anti_forensics_ko" if ko else "anti_forensics_en"] if structure else ""
        if anti:
            lines.append(f"- {_cell(anti)} [E-02]")
        else:
            lines.append(f"- {missing} — {'직접 검증되거나 명시적으로 제안된 대응 절차 없음' if ko else 'No directly verified or explicitly proposed countermeasure is available'} [E-02]")
        lines.append("")

        # Supporting tools
        lines.append("## 지원 도구" if ko else "## Supporting Tools")
        lines.append("")
        lines.append("| ID | 도구명 | 버전 |" if ko else "| ID | Tool Name | Version |")
        lines.append("|---|---|---|")
        tools = structure["tools"] if structure else []
        if tools:
            for tool, evidence_id in zip(tools, tool_ids, strict=True):
                lines.append(f"| {evidence_id} | {_cell(tool['name'])} | {_cell(tool['version']) if tool['version'] else nr} |")
        else:
            lines.append(f"| N/A | {missing} | {missing} |")
        lines.append("")

        # Evidence and limitations
        lines.append("## 근거 및 한계" if ko else "## Evidence and Limitations")
        lines.append("")
        lines.append("| ID | 상태 | PDF 근거 | 뒷받침하는 내용 |" if ko else "| ID | Status | PDF Evidence | Supported Content |")
        lines.append("|---|---|---|---|")
        for identifier, status, pages, support_ko, support_en in evidence.rows:
            lines.append(f"| {identifier} | {status} | {pages} | {_cell(support_ko if ko else support_en)} |")
        lines.append("")
        lines.append("| 항목 | 내용 |" if ko else "| Item | Value |")
        lines.append("|---|---|")
        lines.append(f"| {'채택 후보' if ko else 'Accepted Candidates'} | {accepted} |")
        lines.append(f"| {'제외 후보' if ko else 'Excluded Candidates'} | {excluded} |")
        lines.append(
            f"| {'후보 사용 원칙' if ko else 'Candidate-use Rule'} | "
            + ("고신뢰이며 직접 대상 맥락과 PDF 페이지가 함께 확인된 값만 사용" if ko else "Use only high-confidence values with direct-target context and a PDF page")
            + " |"
        )
        lines.append(
            f"| {'미채택 필드' if ko else 'Unfilled Fields'} | "
            + ("외부 상식으로 보완하지 않고 NR/N/A/UV 유지" if ko else "Retain NR/N/A/UV; do not supplement from outside knowledge")
            + " |"
        )
        lines.append("")
        lines.append(f"- [{'PDF 원문' if ko else 'Full-text PDF'}](<../../Paper/{metadata.pdf_filename}>)")
        documents[language] = "\n".join(lines) + "\n"
    return documents["ko"], documents["en"]


# --------------------------------------------------------------------------- #
# Manifests
# --------------------------------------------------------------------------- #

def build_lv3_manifest(
    record: Mapping[str, Any],
    *,
    paper_index: int,
    title: str,
    structure_sha256: str,
) -> dict[str, Any]:
    relevance = record["relevance"]
    scope = relevance["scope_evidence"]
    mappings = [
        {
            "rank": mapping["rank"],
            "role": mapping["role"],
            "path_id": mapping["path_id"],
            "confidence": mapping["confidence"],
            "rationale": mapping["rationale"],
            "evidence_id": "E-02",
            "evidence_pages": scope["pages"],
            "evidence_support": _cell(relevance["summary_ko"]),
        }
        for mapping in record["level3"]["mappings"]
    ]
    non_forensic = relevance["verdict"] == "비관련"
    return {
        "schema_version": LV3_MANIFEST_SCHEMA_VERSION,
        "source_id": record["source_id"],
        "paper_index": paper_index,
        "title": title,
        "forensic_relevance": relevance["verdict"],
        "research_type": relevance["research_type"],
        "classification_status": "non_forensic" if non_forensic else "classified",
        "taxonomy_sha256": record["provenance"]["taxonomy_sha256"],
        "intelligence_sha256": keyword_section_sha256(record),
        "pdf_sha256": record["provenance"]["pdf_sha256"],
        "structure_sha256": structure_sha256,
        "mappings": mappings,
        "not_applicable_reason": record["level3"]["not_applicable_reason"] if non_forensic else None,
        "review": {
            "status": record["review"]["status"],
            "reviewed_on": record["review"]["reviewed_on"],
            "reviewer": record["review"]["reviewer"],
        },
    }


def build_type_decision(
    record: Mapping[str, Any],
    *,
    structure_sha256: str,
    lv3_sha256: str,
    type_taxonomy_sha256: str,
) -> dict[str, Any]:
    decision = record["document_type"]
    return {
        "schema_version": TYPE_DECISION_SCHEMA_VERSION,
        "source_id": record["source_id"],
        "subtype": decision["subtype"],
        "confidence": decision["confidence"],
        "boundary_candidate": decision["boundary_candidate"],
        "rationale": decision["rationale"],
        "boundary_reason": decision["boundary_reason"],
        "pdf_sha256": record["provenance"]["pdf_sha256"],
        "structure_sha256": structure_sha256,
        "lv3_manifest_sha256": lv3_sha256,
        "taxonomy_sha256": type_taxonomy_sha256,
        "review": {
            "status": record["review"]["status"],
            "reviewed_on": record["review"]["reviewed_on"],
            "reviewer": record["review"]["reviewer"],
        },
    }


def write_paper_artifacts(
    record: Mapping[str, Any],
    metadata: PaperMetadata,
    *,
    paper_index: int,
    analysis_dir: Path = DEFAULT_ANALYSIS_DIR,
    structure_dir: Path = DEFAULT_STRUCTURE_DIR,
    lv3_dir: Path = DEFAULT_LV3_DIR,
    type_decision_dir: Path = DEFAULT_TYPE_DECISION_DIR,
    type_taxonomy_path: Path = DEFAULT_TYPE_TAXONOMY,
    updated_on: str | None = None,
) -> WrittenArtifacts:
    """Persist every artifact derived from one analysis record.

    The Korean Structure document is written first because both manifests bind
    to its hash; the Level-3 manifest follows because the type decision binds to
    it.  All writes are atomic, so a crash leaves either the old or the new
    complete file.
    """

    if record.get("dry_run"):
        raise EvidenceWriterError("a dry-run record carries no analysis to write")
    updated = updated_on or record["review"]["reviewed_on"]
    author = record["review"]["reviewer"]
    korean, english = render_structure_documents(
        record, metadata, updated_on=updated, author=author
    )
    source_id = metadata.source_id
    structure_ko_path = structure_dir / "kor" / f"{source_id}.md"
    structure_en_path = structure_dir / "eng" / f"{source_id}.md"
    write_text_atomic(structure_ko_path, korean)
    write_text_atomic(structure_en_path, english)
    structure_sha256 = _sha256_text(korean)

    lv3 = build_lv3_manifest(
        record, paper_index=paper_index, title=metadata.title, structure_sha256=structure_sha256
    )
    lv3_path = lv3_dir / f"{source_id}.json"
    write_json_atomic(lv3_path, lv3)
    lv3_sha256 = hashlib.sha256(lv3_path.read_bytes()).hexdigest()

    type_taxonomy_sha256 = hashlib.sha256(type_taxonomy_path.read_bytes()).hexdigest()
    decision = build_type_decision(
        record,
        structure_sha256=structure_sha256,
        lv3_sha256=lv3_sha256,
        type_taxonomy_sha256=type_taxonomy_sha256,
    )
    type_path = type_decision_dir / f"{source_id}.json"
    write_json_atomic(type_path, decision)

    analysis_path = analysis_dir / f"{source_id}.json"
    write_json_atomic(
        analysis_path,
        {
            **record,
            "paper_index": paper_index,
            "artifacts": {
                "structure_ko": str(structure_ko_path.relative_to(REPOSITORY_ROOT)) if structure_ko_path.is_relative_to(REPOSITORY_ROOT) else str(structure_ko_path),
                "structure_en": str(structure_en_path.relative_to(REPOSITORY_ROOT)) if structure_en_path.is_relative_to(REPOSITORY_ROOT) else str(structure_en_path),
                "structure_sha256": structure_sha256,
                "lv3_manifest_sha256": lv3_sha256,
                "type_decision_sha256": hashlib.sha256(type_path.read_bytes()).hexdigest(),
            },
        },
    )
    return WrittenArtifacts(
        analysis_path=analysis_path,
        structure_ko_path=structure_ko_path,
        structure_en_path=structure_en_path,
        lv3_path=lv3_path,
        type_decision_path=type_path,
        structure_sha256=structure_sha256,
        lv3_sha256=lv3_sha256,
    )


# --------------------------------------------------------------------------- #
# Structure index
# --------------------------------------------------------------------------- #

_FIELD_RE = re.compile(r"^\|\s*(연구 유형|포렌식 관련성)\s*\|\s*(.*?)\s*\[E-\d+\]\s*\|\s*$", re.MULTILINE)
_EVIDENCE_ROW_RE = re.compile(r"^\| (E-\d+) \| ", re.MULTILINE)
_ACCEPTED_RE = re.compile(r"^\| 채택 후보 \| (\d+) \|", re.MULTILINE)
_RESEARCH_TYPE_BY_LABEL = {label: code for code, label in RESEARCH_TYPE_LABELS_KO.items()}


def _document_row(path: Path) -> tuple[str, str, str, int]:
    text = path.read_text(encoding="utf-8")
    fields = dict(_FIELD_RE.findall(text))
    if set(fields) != {"연구 유형", "포렌식 관련성"}:
        raise EvidenceWriterError(f"{path}: cannot read verdict/research type")
    research_type = _RESEARCH_TYPE_BY_LABEL.get(fields["연구 유형"])
    if research_type is None:
        raise EvidenceWriterError(f"{path}: unknown research type label {fields['연구 유형']!r}")
    accepted_match = _ACCEPTED_RE.search(text)
    accepted = int(accepted_match.group(1)) if accepted_match else max(len(_EVIDENCE_ROW_RE.findall(text)) - 2, 0)
    return path.stem, fields["포렌식 관련성"], research_type, accepted


def regenerate_structure_index(
    structure_dir: Path = DEFAULT_STRUCTURE_DIR,
    *,
    generated_on: str | None = None,
) -> str:
    """Rebuild ``INDEX.md`` from the Structure documents on disk.

    Legacy rows keep their recorded warning counts; documents without a prior
    row get zero warnings.  The result is written atomically and returned.
    """

    index_path = structure_dir / "INDEX.md"
    previous_warnings: dict[str, int] = {}
    if index_path.is_file():
        for match in _INDEX_ROW_RE.finditer(index_path.read_text(encoding="utf-8")):
            previous_warnings[match.group(1)] = int(match.group(5))
    korean_dir, english_dir = structure_dir / "kor", structure_dir / "eng"
    korean = sorted(korean_dir.glob("ART*.md"))
    english_ids = {path.stem for path in english_dir.glob("ART*.md")}
    rows: list[tuple[str, str, str, int, int]] = []
    for path in korean:
        if path.stem not in english_ids:
            raise EvidenceWriterError(f"English Structure document is missing: {path.stem}")
        source_id, verdict, research_type, accepted = _document_row(path)
        rows.append((source_id, verdict, research_type, accepted, previous_warnings.get(source_id, 0)))
    if len(english_ids) != len(rows):
        extra = sorted(english_ids - {row[0] for row in rows})
        raise EvidenceWriterError(f"English Structure documents without Korean pair: {extra[:5]}")

    verdict_counts: dict[str, int] = {"관련": 0, "부분 관련": 0, "비관련": 0}
    type_counts: dict[str, int] = {}
    for _, verdict, research_type, _, _ in rows:
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        type_counts[research_type] = type_counts.get(research_type, 0) + 1
    lines = [
        "# Structure Document Index",
        "",
        f"- Generated: {generated_on or date.today().isoformat()}",
        f"- Source records: {len(rows)}",
        f"- Korean documents: {len(rows)}",
        f"- English documents: {len(rows)}",
        f"- Bilingual paper documents: {len(rows) * 2}",
        f"- Warnings: {sum(row[4] for row in rows)}",
        "",
        "## Verdict Counts",
        "",
        "| Verdict | Count |",
        "|---|---|",
    ]
    lines.extend(f"| {verdict} | {count} |" for verdict, count in verdict_counts.items())
    lines += ["", "## Research-type Counts", "", "| Research Type | Count |", "|---|---|"]
    lines.extend(f"| {code} | {type_counts[code]} |" for code in sorted(type_counts))
    lines += [
        "",
        "## Documents",
        "",
        "| source_id | Verdict | Research Type | Korean | English | Accepted Candidates | Warnings |",
        "|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {source_id} | {verdict} | {research_type} | [kor](kor/{source_id}.md) | [eng](eng/{source_id}.md) | {accepted} | {warnings} |"
        for source_id, verdict, research_type, accepted, warnings in rows
    )
    text = "\n".join(lines) + "\n"
    write_text_atomic(index_path, text)
    return text


__all__ = [
    "APPLICATION_CATEGORIES",
    "DEFAULT_ANALYSIS_DIR",
    "DEFAULT_LV3_DIR",
    "DEFAULT_STRUCTURE_DIR",
    "DEFAULT_TYPE_DECISION_DIR",
    "EvidenceWriterError",
    "PaperMetadata",
    "RESEARCH_TYPE_LABELS_KO",
    "WrittenArtifacts",
    "build_lv3_manifest",
    "build_type_decision",
    "keyword_section_sha256",
    "load_vocabulary",
    "regenerate_structure_index",
    "render_structure_documents",
    "write_paper_artifacts",
]
