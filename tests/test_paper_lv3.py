from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from openpyxl import Workbook
from pypdf import PdfWriter

from PaperPipeline.paper_files import PaperFileRecord

from Analyzer.paper_lv3 import (
    IntelligencePaper,
    Level3Path,
    MappingDecision,
    PaperDecision,
    PaperLevel3Error,
    StructureEvidence,
    Type1Taxonomy,
    _set_text_cell,
    build_workbook,
    load_classifications,
    load_type1_taxonomy,
    validate_corpus_files,
)


def _write_manifest(
    directory: Path,
    *,
    paper: IntelligencePaper,
    taxonomy_sha256: str,
    intelligence_sha256: str,
    pdf_sha256: str,
    structure: StructureEvidence,
) -> Path:
    value = {
        "schema_version": "1.0",
        "source_id": paper.source_id,
        "paper_index": paper.index,
        "title": paper.title,
        "forensic_relevance": paper.verdict,
        "research_type": "EMPIRICAL_ARTIFACT",
        "classification_status": "classified",
        "taxonomy_sha256": taxonomy_sha256,
        "intelligence_sha256": intelligence_sha256,
        "pdf_sha256": pdf_sha256,
        "structure_sha256": structure.sha256,
        "mappings": [
            {
                "rank": 1,
                "role": "primary",
                "path_id": "DFIR-03-03-01",
                "confidence": 0.95,
                "rationale": "애플리케이션이 남긴 아티팩트를 직접 분석한 연구다.",
                "evidence_id": structure.evidence_id,
                "evidence_pages": structure.evidence_pages,
                "evidence_support": structure.evidence_support,
            }
        ],
        "not_applicable_reason": None,
        "review": {
            "status": "approved",
            "reviewed_on": "2026-08-10",
            "reviewer": "test reviewer",
        },
    }
    path = directory / f"{paper.source_id}.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_type1_markdown_reconciles_to_70_bilingual_level3_paths() -> None:
    taxonomy = load_type1_taxonomy()

    assert len(taxonomy.paths) == 70
    assert len({path.level_1 for path in taxonomy.paths}) == 5
    assert len({(path.level_1, path.level_2) for path in taxonomy.paths}) == 17
    assert taxonomy.by_id["DFIR-03-03-01"].level_3 == "애플리케이션 아티팩트 조사"
    assert taxonomy.by_id["DFIR-05-05-03"].level_3 == (
        "인공지능 생성물 식별 및 악용 범죄 대응"
    )
    assert not any(
        path.level_2 == "네트워크 및 클라우드 데이터 수집"
        and path.level_3 in {"애플리케이션 레벨 메모리 조사", "파일 저장 포맷 조사"}
        for path in taxonomy.paths
    )


def test_taxonomy_rejects_duplicate_english_path_and_changed_known_error(
    tmp_path: Path,
) -> None:
    taxonomy = load_type1_taxonomy()
    source = taxonomy.source_path.read_text(encoding="utf-8-sig")

    duplicate_english = source.replace(
        "Network asset identification",
        "Search and seizure / Digital device identification",
        1,
    )
    english_path = tmp_path / "duplicate-english.md"
    english_path.write_text(duplicate_english, encoding="utf-8")
    with pytest.raises(PaperLevel3Error, match="duplicate canonical English"):
        load_type1_taxonomy(english_path)

    changed_copy_block = source.replace(
        "|  |  | 파일 저장 포맷 조사 |  |  |",
        "|  |  | 애플리케이션 레벨 메모리 조사 |  |  |",
        1,
    )
    changed_path = tmp_path / "changed-copy-block.md"
    changed_path.write_text(changed_copy_block, encoding="utf-8")
    with pytest.raises(PaperLevel3Error, match="duplicated Korean taxonomy block changed"):
        load_type1_taxonomy(changed_path)


def test_classification_manifest_is_bound_to_pdf_and_structure_evidence(
    tmp_path: Path,
) -> None:
    taxonomy = load_type1_taxonomy()
    paper = IntelligencePaper(1, "ART000000001", "검증 논문", "관련")
    pdf_dir = tmp_path / "papers"
    manifest_dir = tmp_path / "manifests"
    pdf_dir.mkdir()
    manifest_dir.mkdir()
    pdf = pdf_dir / f"{paper.source_id}.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfixture")
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    structure = StructureEvidence(
        source_id=paper.source_id,
        title=paper.title,
        evidence_id="E-02",
        evidence_pages="PDF pp. 1–4",
        evidence_support="애플리케이션 아티팩트를 실험으로 분석했다.",
        sha256="1" * 64,
    )
    manifest = _write_manifest(
        manifest_dir,
        paper=paper,
        taxonomy_sha256=taxonomy.sha256,
        intelligence_sha256="2" * 64,
        pdf_sha256=pdf_sha256,
        structure=structure,
    )

    decisions = load_classifications(
        manifest_dir,
        papers=[paper],
        intelligence_sha256="2" * 64,
        taxonomy=taxonomy,
        structure_index={paper.source_id: ("관련", "EMPIRICAL_ARTIFACT")},
        structure_evidence={paper.source_id: structure},
        papers_dir=pdf_dir,
    )

    assert decisions[paper.source_id].mappings[0].path_id == "DFIR-03-03-01"
    assert decisions[paper.source_id].pdf_sha256 == pdf_sha256

    value = json.loads(manifest.read_text(encoding="utf-8"))
    unclassified = json.loads(json.dumps(value))
    unclassified["classification_status"] = "non_forensic"
    unclassified["mappings"] = []
    unclassified["not_applicable_reason"] = (
        "관련 논문을 임의로 미분류 상태로 두는 잘못된 회귀 사례다."
    )
    manifest.write_text(json.dumps(unclassified, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PaperLevel3Error, match="requires at least one Level-3"):
        load_classifications(
            manifest_dir,
            papers=[paper],
            intelligence_sha256="2" * 64,
            taxonomy=taxonomy,
            structure_index={paper.source_id: ("관련", "EMPIRICAL_ARTIFACT")},
            structure_evidence={paper.source_id: structure},
            papers_dir=pdf_dir,
        )


    partial_paper = IntelligencePaper(
        paper.index, paper.source_id, paper.title, "부분 관련"
    )
    partial_unclassified = json.loads(json.dumps(unclassified))
    partial_unclassified["forensic_relevance"] = "부분 관련"
    manifest.write_text(
        json.dumps(partial_unclassified, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(PaperLevel3Error, match="requires at least one Level-3"):
        load_classifications(
            manifest_dir,
            papers=[partial_paper],
            intelligence_sha256="2" * 64,
            taxonomy=taxonomy,
            structure_index={paper.source_id: ("부분 관련", "EMPIRICAL_ARTIFACT")},
            structure_evidence={paper.source_id: structure},
            papers_dir=pdf_dir,
        )

    non_forensic_paper = IntelligencePaper(
        paper.index, paper.source_id, paper.title, "비관련"
    )
    non_forensic = json.loads(json.dumps(unclassified))
    non_forensic["forensic_relevance"] = "비관련"
    manifest.write_text(
        json.dumps(non_forensic, ensure_ascii=False), encoding="utf-8"
    )
    non_forensic_decisions = load_classifications(
        manifest_dir,
        papers=[non_forensic_paper],
        intelligence_sha256="2" * 64,
        taxonomy=taxonomy,
        structure_index={paper.source_id: ("비관련", "EMPIRICAL_ARTIFACT")},
        structure_evidence={paper.source_id: structure},
        papers_dir=pdf_dir,
    )
    assert non_forensic_decisions[paper.source_id].classification_status == (
        "non_forensic"
    )
    assert non_forensic_decisions[paper.source_id].mappings == ()

    legacy_unclassified = json.loads(json.dumps(unclassified))
    legacy_unclassified["classification_status"] = "taxonomy_out_of_scope"
    manifest.write_text(
        json.dumps(legacy_unclassified, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(PaperLevel3Error, match="unknown classification_status"):
        load_classifications(
            manifest_dir,
            papers=[paper],
            intelligence_sha256="2" * 64,
            taxonomy=taxonomy,
            structure_index={paper.source_id: ("관련", "EMPIRICAL_ARTIFACT")},
            structure_evidence={paper.source_id: structure},
            papers_dir=pdf_dir,
        )

    value["paper_index"] = True
    manifest.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PaperLevel3Error, match="paper_index mismatch"):
        load_classifications(
            manifest_dir,
            papers=[paper],
            intelligence_sha256="2" * 64,
            taxonomy=taxonomy,
            structure_index={paper.source_id: ("관련", "EMPIRICAL_ARTIFACT")},
            structure_evidence={paper.source_id: structure},
            papers_dir=pdf_dir,
        )

    value["paper_index"] = 1
    value["mappings"][0]["rank"] = True
    manifest.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PaperLevel3Error, match="invalid mapping rank"):
        load_classifications(
            manifest_dir,
            papers=[paper],
            intelligence_sha256="2" * 64,
            taxonomy=taxonomy,
            structure_index={paper.source_id: ("관련", "EMPIRICAL_ARTIFACT")},
            structure_evidence={paper.source_id: structure},
            papers_dir=pdf_dir,
        )

    value["mappings"][0]["rank"] = 1
    value["mappings"][0]["evidence_support"] = "근거와 일치하지 않는 설명"
    manifest.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PaperLevel3Error, match="evidence differs"):
        load_classifications(
            manifest_dir,
            papers=[paper],
            intelligence_sha256="2" * 64,
            taxonomy=taxonomy,
            structure_index={paper.source_id: ("관련", "EMPIRICAL_ARTIFACT")},
            structure_evidence={paper.source_id: structure},
            papers_dir=pdf_dir,
        )


def test_level3_validation_resolves_title_based_pdf_from_index(tmp_path: Path) -> None:
    paper = IntelligencePaper(1, "ART000000001", "제목 기반 PDF", "관련")
    pdf_dir = tmp_path / "papers"
    ko_dir = tmp_path / "ko"
    en_dir = tmp_path / "en"
    for directory in (pdf_dir, ko_dir, en_dir):
        directory.mkdir()
    with (pdf_dir / "제목 기반 PDF.pdf").open("wb") as stream:
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(stream)
    canonical_ko = (
        'identifier: "ART000000001"\n## 0. 논문 범위\n[E-01]\n[E-02]\n## 4. 조사\n'
    )
    canonical_en = (
        'identifier: "ART000000001"\n## 0. Paper Scope\n[E-01]\n[E-02]\n'
        "## 4. Examination\n"
    )
    (ko_dir / f"{paper.source_id}.md").write_text(canonical_ko, encoding="utf-8")
    (en_dir / f"{paper.source_id}.md").write_text(canonical_en, encoding="utf-8")
    paper_files = {
        paper.source_id: PaperFileRecord(
            paper.source_id, paper.title, "제목 기반 PDF.pdf"
        )
    }

    validate_corpus_files(
        [paper],
        papers_dir=pdf_dir,
        structure_ko_dir=ko_dir,
        structure_en_dir=en_dir,
        expected_size=1,
        paper_files=paper_files,
    )

    extra_filename = "증분 수집 논문.pdf"
    (pdf_dir / extra_filename).write_bytes(b"not part of this reviewed corpus")
    superset_files = {
        **paper_files,
        "ART000000002": PaperFileRecord(
            "ART000000002", "증분 수집 논문", extra_filename
        ),
    }
    validate_corpus_files(
        [paper],
        papers_dir=pdf_dir,
        structure_ko_dir=ko_dir,
        structure_en_dir=en_dir,
        expected_size=1,
        paper_files=superset_files,
    )
    (pdf_dir / extra_filename).unlink()

    (pdf_dir / "인덱스 밖.pdf").write_bytes(b"%PDF-unmanaged")
    with pytest.raises(PaperLevel3Error, match="unindexed PDF"):
        validate_corpus_files(
            [paper],
            papers_dir=pdf_dir,
            structure_ko_dir=ko_dir,
            structure_en_dir=en_dir,
            expected_size=1,
            paper_files=paper_files,
        )
    (pdf_dir / "인덱스 밖.pdf").unlink()

    (pdf_dir / "제목 기반 PDF.pdf").rename(pdf_dir / f"{paper.source_id}.pdf")
    with pytest.raises(PaperLevel3Error, match="PDF filename set mismatch"):
        validate_corpus_files(
            [paper],
            papers_dir=pdf_dir,
            structure_ko_dir=ko_dir,
            structure_en_dir=en_dir,
            expected_size=1,
            paper_files=paper_files,
        )


def test_corpus_validation_rejects_non_pdf_payload(tmp_path: Path) -> None:
    paper = IntelligencePaper(1, "ART000000001", "검증 논문", "관련")
    pdf_dir = tmp_path / "papers"
    ko_dir = tmp_path / "ko"
    en_dir = tmp_path / "en"
    for directory in (pdf_dir, ko_dir, en_dir):
        directory.mkdir()
    (pdf_dir / f"{paper.source_id}.pdf").write_bytes(b"%PDF-THIS IS NOT A PDF")
    (ko_dir / f"{paper.source_id}.md").write_text("ko", encoding="utf-8")
    (en_dir / f"{paper.source_id}.md").write_text("en", encoding="utf-8")

    with pytest.raises(PaperLevel3Error, match="PDF cannot be parsed"):
        validate_corpus_files(
            [paper],
            papers_dir=pdf_dir,
            structure_ko_dir=ko_dir,
            structure_en_dir=en_dir,
            expected_size=1,
        )


def test_corpus_validation_rejects_empty_english_structure(tmp_path: Path) -> None:
    paper = IntelligencePaper(1, "ART000000001", "검증 논문", "관련")
    pdf_dir = tmp_path / "papers"
    ko_dir = tmp_path / "ko"
    en_dir = tmp_path / "en"
    for directory in (pdf_dir, ko_dir, en_dir):
        directory.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with (pdf_dir / f"{paper.source_id}.pdf").open("wb") as stream:
        writer.write(stream)
    (ko_dir / f"{paper.source_id}.md").write_text(
        'identifier: "ART000000001"\n## 0. 논문 범위\n[E-01]\n[E-02]\n## 4. 조사\n',
        encoding="utf-8",
    )
    (en_dir / f"{paper.source_id}.md").write_text("", encoding="utf-8")

    with pytest.raises(PaperLevel3Error, match="English structure lacks"):
        validate_corpus_files(
            [paper],
            papers_dir=pdf_dir,
            structure_ko_dir=ko_dir,
            structure_en_dir=en_dir,
            expected_size=1,
        )


def test_workbook_preserves_formula_like_metadata_as_text(tmp_path: Path) -> None:
    path = Level3Path(
        path_id="DFIR-03-03-01",
        level_1="조사",
        level_2="애플리케이션",
        level_3="아티팩트 조사",
        level_1_en="Examination",
        level_2_en="Application",
        level_3_en="Artifact examination",
        source_line_ko=1,
        source_line_en=2,
    )
    taxonomy = Type1Taxonomy(
        paths=(path,),
        sha256="a" * 64,
        source_path=tmp_path / "taxonomy.md",
        excluded_misplaced_path_count=0,
    )
    paper = IntelligencePaper(1, "ART000000001", "=1+1", "관련")
    mapping = MappingDecision(
        rank=1,
        role="primary",
        path_id=path.path_id,
        confidence=0.95,
        rationale="애플리케이션 아티팩트를 직접 분석한 연구다.",
        evidence_id="E-02",
        evidence_pages="PDF pp. 1–2",
        evidence_support="+외부 입력처럼 보이는 근거 문자열도 텍스트다.",
    )
    decision = PaperDecision(
        source_id=paper.source_id,
        paper_index=paper.index,
        forensic_relevance=paper.verdict,
        research_type="EMPIRICAL_ARTIFACT",
        classification_status="classified",
        mappings=(mapping,),
        not_applicable_reason=None,
        review_status="approved",
        evidence_id="E-02",
        evidence_pages="PDF pp. 1–2",
        evidence_support="+외부 입력처럼 보이는 근거 문자열도 텍스트다.",
        pdf_sha256="b" * 64,
        structure_sha256="c" * 64,
        manifest_sha256="d" * 64,
    )
    metadata = {
        paper.source_id: {
            "source": "KCI",
            "abstract": "@formula-like input",
            "keywords": [],
            "authors": [],
        }
    }

    workbook = build_workbook(
        papers=[paper],
        decisions={paper.source_id: decision},
        metadata=metadata,
        taxonomy=taxonomy,
        intelligence_sha256="e" * 64,
    )

    for worksheet in workbook.worksheets:
        assert all(cell.data_type != "f" for row in worksheet for cell in row)
    assert workbook["논문별 분류"]["D2"].value == "=1+1"
    assert workbook["논문별 분류"]["D2"].data_type == "s"
    workbook.close()


def test_excel_cell_overflow_fails_instead_of_silent_truncation() -> None:
    workbook = Workbook()
    try:
        with pytest.raises(PaperLevel3Error, match="32,767-character limit"):
            _set_text_cell(workbook.active["A1"], "x" * 32_768)
    finally:
        workbook.close()
