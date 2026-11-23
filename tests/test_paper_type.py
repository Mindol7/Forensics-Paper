from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from openpyxl import load_workbook

from Analyzer.Type.paper_type import (
    DEFAULT_DOWNLOAD_MANIFEST_DIR,
    DEFAULT_LV3_DIR,
    DEFAULT_PAPER_INDEX,
    DEFAULT_REVIEW,
    DEFAULT_TAXONOMY,
    DEFAULT_STRUCTURE_DIR,
    MAIN_COLUMNS,
    PdfEvidence,
    PaperTypeError,
    TaxonomyRow,
    VALID_SUBTYPES,
    _atomic_save,
    _load_json_snapshot,
    _validate_workbook,
    _validate_lv3_manifest,
    _workbook_content_digest,
    build_workbook,
    load_review_manifest,
    load_structure,
    load_taxonomy,
)
from PaperPipeline.paper_files import load_paper_file_index


def _review_context() -> tuple[list[str], dict[str, TaxonomyRow], str, dict]:
    """The legacy monolithic review covers exactly the 527 papers it lists.

    The PDF index keeps growing with monthly increments, so the reviewed set is
    taken from the manifest itself; every reviewed paper must still be indexed.
    """

    records = load_paper_file_index(DEFAULT_PAPER_INDEX)
    taxonomy, taxonomy_sha256 = load_taxonomy(DEFAULT_TAXONOMY)
    review = json.loads(DEFAULT_REVIEW.read_text(encoding="utf-8"))
    source_ids = list(review["paper_decisions"])
    # A reviewed paper may only be missing from the PDF index because it was removed from the corpus.
    excluded_path = Path(__file__).resolve().parents[1] / "Processor" / "refs" / "excluded_papers.json"
    excluded = (
        {item["source_id"] for item in json.loads(excluded_path.read_text(encoding="utf-8"))["papers"]}
        if excluded_path.is_file() else set()
    )
    assert set(source_ids) - set(records) <= excluded
    return source_ids, taxonomy, taxonomy_sha256, review


def _dummy_rows() -> list[dict]:
    rows: list[dict] = []
    for index in range(527):
        row = {column: "" for column in MAIN_COLUMNS}
        subtype = "re" if index < 11 else "sh" if index < 13 else "ar"
        row.update(
            source_id=f"ART{index:09d}",
            subtype=subtype,
            classification_status="classified",
            pdf_sha256=f"{index:064x}",
            text_extractable="Y",
        )
        rows.append(row)
    return rows


def test_taxonomy_and_full_review_cover_exactly_527_papers() -> None:
    source_ids, taxonomy, taxonomy_sha256, raw = _review_context()

    decisions, _audit, review, _ = load_review_manifest(
        DEFAULT_REVIEW,
        taxonomy_sha256=taxonomy_sha256,
        corpus_sha256=raw["corpus_sha256"],
        source_ids=source_ids,
        taxonomy=taxonomy,
    )

    assert tuple(taxonomy) == VALID_SUBTYPES
    assert len(source_ids) == len(decisions) == 527
    assert Counter(value["subtype"] for value in decisions.values()) == Counter(
        {"ar": 514, "re": 11, "sh": 2}
    )
    assert review["status"] == "approved"


def test_review_manifest_rejects_missing_decision_and_invalid_date(
    tmp_path: Path,
) -> None:
    source_ids, taxonomy, taxonomy_sha256, raw = _review_context()
    missing = json.loads(json.dumps(raw))
    missing["paper_decisions"].pop(source_ids[-1])
    missing_path = tmp_path / "missing.json"
    missing_path.write_text(json.dumps(missing), encoding="utf-8")

    with pytest.raises(PaperTypeError, match="every source ID"):
        load_review_manifest(
            missing_path,
            taxonomy_sha256=taxonomy_sha256,
            corpus_sha256=raw["corpus_sha256"],
            source_ids=source_ids,
            taxonomy=taxonomy,
        )

    invalid_date = json.loads(json.dumps(raw))
    invalid_date["review"]["reviewed_on"] = "2026-99-99"
    invalid_date_path = tmp_path / "invalid-date.json"
    invalid_date_path.write_text(json.dumps(invalid_date), encoding="utf-8")
    with pytest.raises(PaperTypeError, match="invalid calendar date"):
        load_review_manifest(
            invalid_date_path,
            taxonomy_sha256=taxonomy_sha256,
            corpus_sha256=raw["corpus_sha256"],
            source_ids=source_ids,
            taxonomy=taxonomy,
        )


def test_workbook_round_trip_and_auxiliary_sheet_tamper_detection(
    tmp_path: Path,
) -> None:
    rows = _dummy_rows()
    taxonomy = {
        code: TaxonomyRow(code, code, f"{code} description")
        for code in VALID_SUBTYPES
    }
    workbook = build_workbook(
        rows,
        taxonomy=taxonomy,
        taxonomy_sha256="t",
        review_sha256="r",
        corpus_sha256="c",
        db_count=537,
        review_manifest={
            "artifact_identity_priority": [],
            "boundary_candidate_source_ids": [],
            "zero_result_subtypes": ["cp", "dp", "di"],
        },
    )
    expected_digest = _workbook_content_digest(workbook)
    output = tmp_path / "type-list.xlsx"
    _atomic_save(workbook, output, rows)

    reopened = load_workbook(output)
    assert _workbook_content_digest(reopened) == expected_digest
    reopened["유형 요약"]["D2"] = 513
    reopened.save(output)
    reopened.close()

    with pytest.raises(PaperTypeError, match="content changed"):
        _validate_workbook(output, rows, expected_digest)


def test_structure_evidence_must_reference_a_real_pdf_page(tmp_path: Path) -> None:
    path = tmp_path / "ART000000001.md"
    template = """\
| 연구 유형 | 문헌고찰·동향 [E-02] |
| 포렌식 관련성 | 관련 [E-02] |

### 핵심 요약

- [OBS] 기존 연구를 체계적으로 종합한다. [E-02]

| E-02 | OBS | PDF pp. 1–{last_page} | 문헌의 선정과 분석 결과를 제시한다. |
"""
    path.write_text(template.format(last_page=3), encoding="utf-8")
    evidence = load_structure(path, pdf_page_count=3)
    assert evidence.research_type == "REVIEW_SURVEY"

    path.write_text(template.format(last_page=999), encoding="utf-8")
    with pytest.raises(PaperTypeError, match="outside PDF bounds"):
        load_structure(path, pdf_page_count=3)


def test_lv3_review_block_must_be_a_valid_approved_object() -> None:
    record = next(iter(load_paper_file_index(DEFAULT_PAPER_INDEX).values()))
    download = json.loads(
        (DEFAULT_DOWNLOAD_MANIFEST_DIR / f"{record.source_id}.download.json").read_text(
            encoding="utf-8"
        )
    )["pdf"]
    pdf = PdfEvidence(
        page_count=download["page_count"],
        sha256=download["sha256"],
        text_extractable=True,
        text_char_count=0,
        text_sha256="",
    )
    structure = load_structure(
        DEFAULT_STRUCTURE_DIR / f"{record.source_id}.md",
        pdf_page_count=pdf.page_count,
    )
    path = DEFAULT_LV3_DIR / f"{record.source_id}.json"
    value, _ = _load_json_snapshot(path)
    value["review"] = []

    with pytest.raises(PaperTypeError, match="invalid Lv3 review block"):
        _validate_lv3_manifest(
            value,
            path=path,
            record=record,
            paper_index=1,
            pdf=pdf,
            structure=structure,
        )
