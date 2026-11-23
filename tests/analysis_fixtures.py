"""Shared fixtures: a realistic Claude analysis record and a tiny PDF corpus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pypdf import PdfWriter

from Analyzer.evidence_writer import PaperMetadata
from PaperPipeline.claude_analyzer import (
    ANALYSIS_SCHEMA_VERSION,
    AnalysisVocabulary,
    TaxonomyEntry,
)
from PaperPipeline.paper_files import PAPER_FILE_INDEX_SCHEMA, PAPER_FILENAME_POLICY, PaperFileRecord

SOURCE_ID = "ART000000001"
TITLE = "드롭박스 흔적 조사"
PDF_FILENAME = "드롭박스 흔적 조사.pdf"
REVIEWED_ON = "2026-09-16"
REVIEWER = "Claude (claude-opus-5) automated analysis"

VOCABULARY = AnalysisVocabulary(
    taxonomy=(
        TaxonomyEntry(
            "EXM-04-01", "EXM-04", "애플리케이션 및 파일 포렌식",
            "Application and File Forensics", "애플리케이션 아티팩트 조사",
            "Application Artifact Examination",
        ),
        TaxonomyEntry(
            "COL-02-04", "COL-02", "네트워크 및 클라우드 데이터 수집",
            "Network and Cloud Data Collection", "클라우드 데이터 수집",
            "Cloud Data Collection",
        ),
    ),
    taxonomy_sha256="a" * 64,
    application_categories=("클라우드 스토리지", "메신저"),
)


def write_pdf(path: Path, pages: int = 3) -> str:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        writer.write(handle)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence(pages: str = "PDF p. 3", status: str = "OBS") -> dict[str, str]:
    return {
        "status": status,
        "pages": pages,
        "support_ko": "직접 대상 맥락에서 명시된 값이다.",
        "support_en": "Value stated in direct-target context.",
    }


def analysis_record(
    *,
    source_id: str = SOURCE_ID,
    title: str = TITLE,
    pdf_sha256: str = "b" * 64,
    verdict: str = "관련",
    taxonomy_sha256: str = "a" * 64,
) -> dict[str, Any]:
    """A record shaped exactly like ``ClaudePaperAnalyzer.analyze`` output."""

    non_forensic = verdict == "비관련"
    research_type = "NON_FORENSIC" if non_forensic else "EMPIRICAL_ARTIFACT"
    record: dict[str, Any] = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analyzer_version": "test",
        "source_id": source_id,
        "title": title,
        "doi": "10.1234/example",
        "model": "claude-opus-5",
        "generated_at": "2026-09-16T00:00:00+00:00",
        "provenance": {
            "input_mode": "pdf",
            "pdf_sha256": pdf_sha256,
            "pdf_page_count": 3,
            "text_sha256": None,
            "text_char_count": None,
            "taxonomy_sha256": taxonomy_sha256,
            "prompt_sha256": "c" * 64,
            "effort": "high",
        },
        "relevance": {
            "verdict": verdict,
            "research_type": research_type,
            "summary_ko": (
                "위변조 이미지 생성 기법만을 제안하며 증거 분석을 수행하지 않는 연구다."
                if non_forensic
                else "Windows PC에 남은 Dropbox 흔적과 NTFS 시간정보를 함께 분석한 연구다."
            ),
            "summary_en": (
                "Proposes an image-forgery generation method without any evidence analysis."
                if non_forensic
                else "The paper analyzes Dropbox traces on Windows PCs together with NTFS timestamps."
            ),
            "rationale_ko": "실제 PC에서 Dropbox 아티팩트를 직접 관찰하고 실험하였다.",
            "explicit_limitations_ko": "",
            "explicit_limitations_en": "",
            "scope_evidence": {
                "status": "LIT" if non_forensic else "OBS",
                "pages": "PDF pp. 1–2, 3",
                "support_ko": "PDF pp.1–2는 분석 대상을, p.3은 실험 결과를 제시한다.",
                "support_en": "Pages 1–2 present the targets and page 3 the experiment.",
            },
        },
        "keywords": {
            "device": [] if non_forensic else ["PC"],
            "filesystem": [] if non_forensic else ["NTFS"],
            "operating_system": [] if non_forensic else ["Windows"],
            "application": [] if non_forensic else [{"name": "Dropbox", "category": "클라우드 스토리지"}],
            "cloud": [] if non_forensic else ["SaaS"],
            "rationale": "연구가 직접 다룬 증거 대상만 기록하였다.",
        },
        "level3": {
            "mappings": [] if non_forensic else [
                {
                    "rank": 1,
                    "role": "primary",
                    "path_id": "EXM-04-01",
                    "confidence": 0.97,
                    "rationale": "Dropbox 로컬 흔적을 직접 분석해 업로드와 동기화 행위를 판별한다.",
                }
            ],
            "not_applicable_reason": (
                "위변조 이미지를 생성하는 방법만 제안하며 증거 분석을 수행하지 않는다."
                if non_forensic else None
            ),
        },
        "document_type": {
            "subtype": "ar",
            "confidence": 0.95,
            "boundary_candidate": False,
            "rationale": "실증 아티팩트 분석에 해당하는 독자적 연구 기여이며 리뷰나 학위논문이 아니다.",
            "boundary_reason": "di/cp/dp/re/sh 성립 요건을 배제했다.",
        },
        "english_title": "Investigating traces of a Dropbox application for Windows",
        "structure": None if non_forensic else {
            "identification": {
                "version": "",
                "root_path": "%USERPROFILE%\\AppData\\Local\\Dropbox",
                "root_path_status": "OBS",
                "install_check_path": "",
                "evidence": evidence(),
            },
            "features": [
                {
                    "name_ko": "직접 아티팩트 분석",
                    "name_en": "Direct artifact analysis",
                    "description_ko": "Dropbox 흔적을 분석한다.",
                    "description_en": "Analyzes Dropbox traces.",
                    "default_provided_ko": "",
                    "default_provided_en": "",
                }
            ],
            "preservation_ko": "",
            "preservation_en": "",
            "artifacts": [
                {
                    "name": "home.db",
                    "related_feature": "직접 아티팩트 분석",
                    "path": "",
                    "access_privilege_ko": "",
                    "access_privilege_en": "",
                    "collection_method_ko": "",
                    "collection_method_en": "",
                    "decryption_required_ko": "",
                    "decryption_required_en": "",
                    "decryption_dependencies_ko": "",
                    "decryption_dependencies_en": "",
                    "storage_format_ko": "정형 | 파이프 포함",
                    "storage_format_en": "Structured",
                    "stored_content_ko": "",
                    "stored_content_en": "",
                    "retention_ko": "",
                    "retention_en": "",
                    "default_provided_ko": "",
                    "default_provided_en": "",
                    "status": "OBS",
                    "evidence": evidence(),
                }
            ],
            "decryption_ko": "",
            "decryption_en": "",
            "anti_forensics_ko": "",
            "anti_forensics_en": "",
            "tools": [
                {"name": "DB Browser for SQLite", "version": "3.12", "evidence": evidence("PDF p. 2", "OBS")}
            ],
            "excluded_candidate_count": 2,
        },
        "usage": {
            "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "calls": 2,
        },
        "review": {
            "status": "approved",
            "reviewed_on": REVIEWED_ON,
            "reviewer": REVIEWER,
            "mode": "automated",
        },
    }
    return record


def metadata(source_id: str = SOURCE_ID, title: str = TITLE) -> PaperMetadata:
    return PaperMetadata(
        source_id=source_id,
        title=title,
        title_eng=None,
        authors=("홍길동(테스트대학교)", "김철수(테스트대학교)"),
        journal="디지털포렌식연구",
        publication_year=2026,
        doi="10.1234/example",
        pdf_filename=PDF_FILENAME,
    )


def file_record(source_id: str = SOURCE_ID, title: str = TITLE, filename: str = PDF_FILENAME) -> PaperFileRecord:
    return PaperFileRecord(source_id=source_id, title=title, filename=filename)


def write_index(manifest_dir: Path, records: list[PaperFileRecord]) -> Path:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / "paper_files.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": PAPER_FILE_INDEX_SCHEMA,
                "filename_policy": PAPER_FILENAME_POLICY,
                "paper_count": len(records),
                "papers": [
                    {"source_id": r.source_id, "title": r.title, "filename": r.filename}
                    for r in records
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
