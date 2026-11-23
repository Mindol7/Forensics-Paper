from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from analysis_fixtures import (
    PDF_FILENAME,
    SOURCE_ID,
    TITLE,
    VOCABULARY,
    analysis_record,
    file_record,
    metadata,
    write_pdf,
)

from Analyzer import paper_lv3
from Analyzer.Type import paper_type
from Analyzer.evidence_writer import (
    APPLICATION_CATEGORIES,
    EvidenceWriterError,
    keyword_section_sha256,
    load_vocabulary,
    regenerate_structure_index,
    render_structure_documents,
    write_paper_artifacts,
)


def _taxonomy_accepting(sha256: str) -> paper_lv3.Type1Taxonomy:
    path = paper_lv3.Level3Path(
        path_id="EXM-04-01",
        level_1="조사",
        level_2="애플리케이션 및 파일 포렌식",
        level_3="애플리케이션 아티팩트 조사",
        level_1_en="Examination",
        level_2_en="Application and File Forensics",
        level_3_en="Application Artifact Examination",
        source_line_ko=0,
        source_line_en=0,
    )
    return paper_lv3.Type1Taxonomy(
        paths=(path,),
        sha256=sha256,
        source_path=Path("tech_taxonomy.json"),
        excluded_misplaced_path_count=0,
        accepted_sha256=frozenset({sha256}),
        family_by_path={"EXM-04-01": sha256},
        family_labels={sha256: "tech_taxonomy.json"},
    )


def test_rendered_structure_document_satisfies_both_exporter_parsers(tmp_path: Path) -> None:
    pdf_sha256 = write_pdf(tmp_path / "Paper" / PDF_FILENAME)
    record = analysis_record(pdf_sha256=pdf_sha256)
    korean, english = render_structure_documents(
        record, metadata(), updated_on="2026-09-16", author="tester"
    )
    kor_dir = tmp_path / "Structure" / "kor"
    kor_dir.mkdir(parents=True)
    (kor_dir / f"{SOURCE_ID}.md").write_text(korean, encoding="utf-8")

    parsed = paper_type.load_structure(kor_dir / f"{SOURCE_ID}.md", pdf_page_count=3)
    assert parsed.research_type == "EMPIRICAL_ARTIFACT"
    assert parsed.forensic_relevance == "관련"
    assert parsed.evidence_id == "E-02"
    assert parsed.evidence_pages == "PDF pp. 1–2, 3"
    assert parsed.summary == record["relevance"]["summary_ko"]

    evidence = paper_lv3.load_structure_evidence(
        kor_dir, [paper_lv3.IntelligencePaper(1, SOURCE_ID, TITLE, "관련")]
    )[SOURCE_ID]
    assert evidence.evidence_pages == "PDF pp. 1–2, 3"
    assert evidence.evidence_support == record["relevance"]["summary_ko"]
    assert evidence.sha256 == hashlib.sha256(korean.encode("utf-8")).hexdigest()

    # Table cells never carry raw pipes, so parsed text equals manifest text.
    assert "정형 / 파이프 포함" in korean
    assert "\\|" not in korean
    # Every referenced evidence id is defined exactly once in the evidence table.
    assert korean.count("| E-03 | OBS |") == 1  # root path
    assert korean.count("| E-04 | OBS |") == 1  # artifact
    assert korean.count("| E-05 | OBS |") == 1  # tool
    assert "## Supporting Tools" in english and "DB Browser for SQLite" in english
    assert f"(<../../Paper/{PDF_FILENAME}>)" in korean


def test_written_manifests_validate_against_both_exporters(tmp_path: Path) -> None:
    papers_dir = tmp_path / "Paper"
    pdf_sha256 = write_pdf(papers_dir / PDF_FILENAME)
    record = analysis_record(pdf_sha256=pdf_sha256)
    structure_dir = tmp_path / "Structure"
    lv3_dir = tmp_path / "Manifest" / "Lv3"
    type_dir = tmp_path / "Manifest" / "Type"
    analysis_dir = tmp_path / "Manifest" / "Analysis"

    written = write_paper_artifacts(
        record,
        metadata(),
        paper_index=7,
        analysis_dir=analysis_dir,
        structure_dir=structure_dir,
        lv3_dir=lv3_dir,
        type_decision_dir=type_dir,
    )
    assert written.structure_ko_path.is_file() and written.structure_en_path.is_file()

    # --- paper_lv3 -------------------------------------------------------
    paper = paper_lv3.IntelligencePaper(7, SOURCE_ID, TITLE, "관련")
    structure_evidence = paper_lv3.load_structure_evidence(structure_dir / "kor", [paper])
    decisions = paper_lv3.load_classifications(
        lv3_dir,
        papers=[paper],
        intelligence_sha256={SOURCE_ID: keyword_section_sha256(record)},
        taxonomy=_taxonomy_accepting(record["provenance"]["taxonomy_sha256"]),
        structure_index={SOURCE_ID: ("관련", "EMPIRICAL_ARTIFACT")},
        structure_evidence=structure_evidence,
        papers_dir=papers_dir,
        paper_files={SOURCE_ID: file_record()},
    )
    decision = decisions[SOURCE_ID]
    assert decision.mappings[0].path_id == "EXM-04-01"
    assert decision.pdf_sha256 == pdf_sha256
    assert decision.structure_sha256 == written.structure_sha256

    # A manifest pinning the other taxonomy family for this id fails closed.
    with pytest.raises(paper_lv3.PaperLevel3Error, match="stale taxonomy_sha256"):
        paper_lv3.load_classifications(
            lv3_dir,
            papers=[paper],
            intelligence_sha256={SOURCE_ID: keyword_section_sha256(record)},
            taxonomy=_taxonomy_accepting("f" * 64),
            structure_index={SOURCE_ID: ("관련", "EMPIRICAL_ARTIFACT")},
            structure_evidence=structure_evidence,
            papers_dir=papers_dir,
            paper_files={SOURCE_ID: file_record()},
        )

    # --- paper_type ------------------------------------------------------
    structure = paper_type.load_structure(written.structure_ko_path, pdf_page_count=3)
    pdf = paper_type.PdfEvidence(
        page_count=3, sha256=pdf_sha256, text_extractable=False, text_char_count=0, text_sha256="0" * 64
    )
    lv3_value, lv3_sha256 = paper_type._load_json_snapshot(written.lv3_path)
    assert paper_type._validate_lv3_manifest(
        lv3_value, path=written.lv3_path, record=file_record(), paper_index=None, pdf=pdf, structure=structure
    ) == 7
    assert lv3_sha256 == written.lv3_sha256
    taxonomy, taxonomy_sha256 = paper_type.load_taxonomy()
    corpus_paper = paper_type.CorpusPaper(
        paper_index=7, file_record=file_record(), metadata={}, structure=structure, pdf=pdf, lv3_sha256=lv3_sha256
    )
    type_decisions = paper_type.load_type_decisions(
        type_dir, [corpus_paper], taxonomy=taxonomy, taxonomy_sha256=taxonomy_sha256
    )
    assert type_decisions[SOURCE_ID].subtype == "ar"
    assert type_decisions[SOURCE_ID].reviewer.startswith("Claude")

    # The decision is bound to the Level-3 manifest bytes.
    tampered = json.loads(written.lv3_path.read_text(encoding="utf-8"))
    tampered["mappings"][0]["confidence"] = 0.9
    written.lv3_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    stale = paper_type.CorpusPaper(
        paper_index=7, file_record=file_record(), metadata={}, structure=structure, pdf=pdf,
        lv3_sha256=hashlib.sha256(written.lv3_path.read_bytes()).hexdigest(),
    )
    with pytest.raises(paper_type.PaperTypeError, match="different PDF/Structure/Lv3"):
        paper_type.load_type_decisions(type_dir, [stale], taxonomy=taxonomy, taxonomy_sha256=taxonomy_sha256)

    analysis = json.loads(written.analysis_path.read_text(encoding="utf-8"))
    assert analysis["paper_index"] == 7
    assert analysis["artifacts"]["structure_sha256"] == written.structure_sha256


def test_non_forensic_record_renders_a_not_applicable_document(tmp_path: Path) -> None:
    pdf_sha256 = write_pdf(tmp_path / "Paper" / PDF_FILENAME)
    record = analysis_record(pdf_sha256=pdf_sha256, verdict="비관련")
    korean, english = render_structure_documents(
        record, metadata(), updated_on="2026-09-16", author="tester"
    )
    kor_dir = tmp_path / "kor"
    kor_dir.mkdir()
    path = kor_dir / f"{SOURCE_ID}.md"
    path.write_text(korean, encoding="utf-8")

    parsed = paper_type.load_structure(path, pdf_page_count=3)
    assert parsed.research_type == "NON_FORENSIC"
    assert parsed.forensic_relevance == "비관련"
    assert "해당 없음 (N/A)" in korean and "Not applicable (N/A)" in english
    assert "- [LIT] " in korean
    evidence = paper_lv3.load_structure_evidence(
        kor_dir, [paper_lv3.IntelligencePaper(1, SOURCE_ID, TITLE, "비관련")]
    )
    assert evidence[SOURCE_ID].evidence_pages == "PDF pp. 1–2, 3"

    written = write_paper_artifacts(
        record,
        metadata(),
        paper_index=1,
        analysis_dir=tmp_path / "A",
        structure_dir=tmp_path / "S",
        lv3_dir=tmp_path / "L",
        type_decision_dir=tmp_path / "T",
    )
    manifest = json.loads(written.lv3_path.read_text(encoding="utf-8"))
    assert manifest["classification_status"] == "non_forensic"
    assert manifest["mappings"] == []
    assert manifest["not_applicable_reason"].startswith("위변조")


def test_single_page_scope_evidence_is_accepted_by_lv3(tmp_path: Path) -> None:
    pdf_sha256 = write_pdf(tmp_path / "Paper" / PDF_FILENAME)
    record = analysis_record(pdf_sha256=pdf_sha256)
    record["relevance"]["scope_evidence"]["pages"] = "PDF p. 2"
    korean, _ = render_structure_documents(record, metadata(), updated_on="2026-09-16", author="t")
    kor_dir = tmp_path / "kor"
    kor_dir.mkdir()
    (kor_dir / f"{SOURCE_ID}.md").write_text(korean, encoding="utf-8")
    evidence = paper_lv3.load_structure_evidence(
        kor_dir, [paper_lv3.IntelligencePaper(1, SOURCE_ID, TITLE, "관련")]
    )
    assert evidence[SOURCE_ID].evidence_pages == "PDF p. 2"


def test_regenerate_structure_index_preserves_legacy_warning_counts(tmp_path: Path) -> None:
    structure_dir = tmp_path / "Structure"
    pdf_sha256 = write_pdf(tmp_path / "Paper" / PDF_FILENAME)
    for source_id, verdict in (("ART000000001", "관련"), ("ART000000002", "비관련")):
        record = analysis_record(source_id=source_id, pdf_sha256=pdf_sha256, verdict=verdict)
        write_paper_artifacts(
            record,
            metadata(source_id=source_id),
            paper_index=1 if source_id.endswith("1") else 2,
            analysis_dir=tmp_path / "A",
            structure_dir=structure_dir,
            lv3_dir=tmp_path / "L",
            type_decision_dir=tmp_path / "T",
        )
    (structure_dir / "INDEX.md").write_text(
        "| ART000000001 | 관련 | EMPIRICAL_ARTIFACT | [kor](kor/ART000000001.md) | [eng](eng/ART000000001.md) | 3 | 2 |\n",
        encoding="utf-8",
    )

    text = regenerate_structure_index(structure_dir, generated_on="2026-09-16")

    index = paper_lv3.load_structure_index(structure_dir / "INDEX.md")
    assert index == {
        "ART000000001": ("관련", "EMPIRICAL_ARTIFACT"),
        "ART000000002": ("비관련", "NON_FORENSIC"),
    }
    assert "| ART000000001 | 관련 | EMPIRICAL_ARTIFACT | [kor](kor/ART000000001.md) | [eng](eng/ART000000001.md) | 3 | 2 |" in text
    assert "| ART000000002 | 비관련 | NON_FORENSIC | [kor](kor/ART000000002.md) | [eng](eng/ART000000002.md) | 0 | 0 |" in text
    assert "- Source records: 2" in text

    (structure_dir / "eng" / "ART000000002.md").unlink()
    with pytest.raises(EvidenceWriterError, match="English Structure document is missing"):
        regenerate_structure_index(structure_dir)


def test_repository_vocabulary_is_bilingual_and_complete() -> None:
    vocabulary = load_vocabulary()
    assert len(vocabulary.taxonomy) == 79
    assert all(entry.title_en and entry.group_title_en for entry in vocabulary.taxonomy)
    assert len(vocabulary.taxonomy_sha256) == 64
    assert vocabulary.application_categories == APPLICATION_CATEGORIES
    assert len(APPLICATION_CATEGORIES) == 28


def test_dry_run_records_cannot_be_written(tmp_path: Path) -> None:
    record = {**analysis_record(), "dry_run": True}
    with pytest.raises(EvidenceWriterError, match="dry-run"):
        write_paper_artifacts(
            record, metadata(), paper_index=1,
            analysis_dir=tmp_path, structure_dir=tmp_path, lv3_dir=tmp_path, type_decision_dir=tmp_path,
        )
