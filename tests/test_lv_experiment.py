from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from Analyzer import paper_lv3
from Analyzer.LV import lv_experiment as lv
from Analyzer.technique_ids import canonical_technique_id, legacy_technique_id

def _current(record: dict) -> dict:
    """A cache record of the current prompts and taxonomy (records of an older taxonomy count as missing)."""

    return {"prompt_version": lv.PROMPT_VERSION, "taxonomy_sha256": lv.current_taxonomy_sha256(), **record}



@pytest.fixture(scope="module")
def techniques():
    loaded, sha256 = lv.load_techniques()
    assert len(sha256) == 64
    return loaded


def test_level_3_id_determines_level_1_and_level_2(techniques) -> None:
    assert len(techniques) == 79
    technique = techniques["CVGF-02-02"]
    assert (technique.level_2_id, technique.level_1, technique.level_2) == ("CVGF-02", "융합 포렌식", "멀티미디어 포렌식")
    assert technique.level_3 == "미디어 위변조 탐지 및 무결성 검증" and technique.description
    # every Level-3 id starts with its Level-2 id, which starts with its Level-1 code
    for path_id, item in techniques.items():
        assert path_id.startswith(item.level_2_id + "-") and item.level_2_id.split("-")[0] in lv.LEVEL_1_TITLES
    listing = lv.taxonomy_listing(techniques)
    assert "[Level 1] EXAM · 조사" in listing and "[Level 3] EXAM-04-01 · 애플리케이션 아티팩트 조사" in listing


def test_stage_codes_bridge_both_ways() -> None:
    assert canonical_technique_id("EXM-04-01") == "EXAM-04-01" == canonical_technique_id("EXAM-04-01")
    assert canonical_technique_id("CVG-02") == "CVGF-02" and canonical_technique_id("PRE") == "PREP"
    assert legacy_technique_id("ANYS-01-04") == "ANL-01-04" == legacy_technique_id("ANL-01-04")
    assert canonical_technique_id("DFIR-02-03-04") == "DFIR-02-03-04"


def test_production_taxonomy_loaders_follow_the_new_codes() -> None:
    taxonomy = paper_lv3.load_technique_taxonomy()
    assert len(taxonomy.paths) == 79 and "EXAM-04-01" in taxonomy.by_id
    assert taxonomy.by_id["PREP-01-02"].level_1 == "준비" and taxonomy.by_id["PREP-01-02"].level_3_en
    old = paper_lv3.SUPERSEDED_TECHNIQUE_TAXONOMY_SHA256[0]
    assert taxonomy.accepts(old) and taxonomy.accepts(taxonomy.sha256) and not taxonomy.accepts("0" * 64)
    assert taxonomy.current_family(old) == taxonomy.sha256 == taxonomy.family_of("EXAM-04-01")


@pytest.mark.parametrize(
    ("criteria", "band"),
    [
        (("핵심 기여", "정확히 부합", "명시적"), (0.90, 1.00)),
        (("핵심 기여", "대체로 부합", "명시적"), (0.80, 0.89)),
        (("주요 기여", "정확히 부합", "명시적"), (0.80, 0.89)),
        (("부수 기여", "정확히 부합", "명시적"), (0.65, 0.79)),
        (("핵심 기여", "정확히 부합", "추론 필요"), (0.65, 0.79)),
        (("주요 기여", "부분적으로 부합", "추론 필요"), (0.50, 0.64)),
        (("부수 기여", "부분적으로 부합", "추론 필요"), (0.50, 0.64)),
        (("배경 언급", "정확히 부합", "명시적"), (0.0, 0.49)),
        (("핵심 기여", "부합하지 않음", "명시적"), (0.0, 0.49)),
    ],
)
def test_confidence_follows_from_the_three_criteria(criteria, band) -> None:
    assert lv.confidence_band(*criteria) == band


def _mapping(rank: int, path_id: str, confidence: float, criteria=("핵심 기여", "정확히 부합", "명시적"), quote="메신저 아티팩트를 분석하였다") -> dict:
    return {
        "rank": rank, "path_id": path_id, "directness": criteria[0], "definition_fit": criteria[1],
        "evidence_explicitness": criteria[2], "confidence": confidence, "rationale_ko": "근거", "evidence_quote": quote,
    }


def test_normalization_has_no_count_cap_and_no_forced_assignment(techniques) -> None:
    source = "본 논문은 메신저  아티팩트를\n분석하였다. 또한 복호화 방안을 제시한다."
    payload = {
        "forensic_relevance": "관련", "relevance_reason_ko": "r", "no_mapping_reason_ko": "",
        "mappings": [
            _mapping(2, "CMNF-01-02", 0.97, ("주요 기여", "정확히 부합", "명시적"), quote="복호화 방안을 제시한다"),  # inflated -> 0.89
            _mapping(1, "EXAM-04-01", 0.93),
            _mapping(3, "ANYS-01-04", 0.70, ("부수 기여", "대체로 부합", "명시적"), quote="없는 문장입니다 전혀"),
            _mapping(4, "EXAM-03-02", 0.60, ("부수 기여", "부분적으로 부합", "명시적")),
            _mapping(5, "CVGF-01-05", 0.30, ("배경 언급", "부분적으로 부합", "명시적")),
            _mapping(6, "EXAM-04-01", 0.90),  # duplicate path is ignored
        ],
        "rejected_candidates": [{"path_id": "EXAM-05-04", "confidence": 0.4, "reason_ko": "로컬 아티팩트"}],
    }
    result = lv.normalize_classification(payload, techniques, source)
    assert [m["path_id"] for m in result["mappings"]] == ["EXAM-04-01", "CMNF-01-02", "ANYS-01-04", "EXAM-03-02"]
    assert [m["rank"] for m in result["mappings"]] == [1, 2, 3, 4] and result["mappings"][0]["role"] == "primary"
    assert [m["confidence"] for m in result["mappings"]] == [0.93, 0.89, 0.70, 0.60]
    assert result["mappings"][1]["confidence_adjusted"] and result["mappings"][1]["reported_confidence"] == 0.97
    assert [m["quote_verified"] for m in result["mappings"]] == [True, True, False, True]
    assert [lv.decision(m["confidence"]) for m in result["mappings"]] == ["확정", "확정", "검토", "약함"]
    assert [r["path_id"] for r in result["rejected_candidates"]] == ["CVGF-01-05", "EXAM-05-04"]

    # A relevant paper with no fitting technique stays empty: nothing is forced in.
    empty = lv.normalize_classification(
        {"forensic_relevance": "관련", "relevance_reason_ko": "r", "mappings": [], "rejected_candidates": [], "no_mapping_reason_ko": "맞는 기술 없음"},
        techniques, source,
    )
    assert empty["mappings"] == [] and empty["no_mapping_reason_ko"] == "맞는 기술 없음"
    with pytest.raises(lv.ExperimentError, match="non-forensic verdict"):
        lv.normalize_classification({"forensic_relevance": "비관련", "mappings": [_mapping(1, "EXAM-04-01", 0.95)]}, techniques, source)
    with pytest.raises(lv.ExperimentError, match="unknown path_id"):
        lv.normalize_classification({"forensic_relevance": "관련", "mappings": [_mapping(1, "EXM-04-01", 0.95)]}, techniques, source)


def test_prompt_states_the_rules_without_a_cap_or_threshold(techniques) -> None:
    prompt = lv.classification_system_prompt(techniques, 2)
    assert "개수 제한은 없지만" in prompt and "핵심 기여 + 정확히 부합 + 명시적" in prompt
    # no cap on assignments, no acceptance threshold told to the model, no forced assignment
    # (the only "최대 3개" left is the length of the rejected-candidates report)
    assert "0~3개" not in prompt and "0.80 이상만" not in prompt and "강제" not in prompt
    assert prompt.count("최대 3개") == 1 and "탈락 후보" in prompt
    assert "초록" in prompt and "CVGF-05-03" in prompt
    schema = lv.classification_schema(techniques)
    assert "maxItems" not in schema["properties"]["mappings"]
    assert set(schema["properties"]["mappings"]["items"]["properties"]["path_id"]["enum"]) == set(techniques)


def test_workbook_export_uses_the_dated_name_and_level_columns(tmp_path: Path, monkeypatch, techniques) -> None:
    monkeypatch.setattr(lv, "LV_DIR", tmp_path)
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    paper = lv.Paper(
        source_id="ART000000001", paper_index=7, title="메신저 분석", title_kor="메신저 분석", title_eng="", abstract_kor="초록",
        abstract_eng="", abstract="", journal="디지털포렌식연구", institution="학회", publication_year=2026, subject_code="컴퓨터학",
        pdf_path=None, record="KNOW-DOMP-0000007", existing_relevance="관련",
        existing_paths=[{"rank": 1, "stored_path_id": "EXM-04-01", "stored_level_3": "애플리케이션 아티팩트 조사", "path_id": "EXAM-04-01", "confidence": 0.9}],
    )
    missing = lv.Paper(
        source_id="ART000000002", paper_index=None, title="PDF 없는 논문", title_kor="PDF 없는 논문", title_eng="", abstract_kor="초록",
        abstract_eng="", abstract="", journal="j", institution="i", publication_year=2026, subject_code="법학", pdf_path=None,
    )
    result = lv.normalize_classification(
        {"forensic_relevance": "관련", "relevance_reason_ko": "r", "no_mapping_reason_ko": "", "rejected_candidates": [],
         "mappings": [_mapping(1, "EXAM-04-01", 0.93, quote="초록"), _mapping(2, "CMNF-01-02", 0.7, ("부수 기여", "정확히 부합", "명시적"), quote="초록")]},
        techniques, "초록 본문입니다 초록",
    )
    lv._write_json(tmp_path / "cache" / "case_2" / "ART000000001.json", _current({
        "status": "ok", "case": 2, "source_id": "ART000000001", "result": result, "input_kind": "abstract", "input_chars": 10, "model": "claude-opus-5",
    }))
    lv._write_json(tmp_path / "cache" / "case_2" / "ART000000002.json", _current({"status": "input_unavailable", "case": 2, "source_id": "ART000000002", "reason": "초록이 없습니다"}))

    target = lv.export_case(2, [paper, missing], techniques, "a" * 64, "claude-opus-5", "260919")
    assert target.name == "(260919)-LV-Case2.xlsx" and lv.workbook_path(3, "260919").name == "(260919)-LV-Case3.xlsx"
    workbook = load_workbook(target)
    assert workbook.sheetnames == ["논문별 결과", "분류 상세", "탈락 후보", "분류 요약", "분포", "실험 조건"]
    sheet = workbook["논문별 결과"]
    header = [cell.value for cell in sheet[1]]
    row = dict(zip(header, [cell.value for cell in sheet[2]]))
    assert row["record_id"] == "KNOW-DOMP-0000007" and row["확정 개수(0.80 이상)"] == 1 and row["검토 개수(0.65~0.79)"] == 1
    assert (row["primary_LV1_id"], row["primary_LV2_id"], row["primary_LV3_id"]) == ("EXAM", "EXAM-04", "EXAM-04-01")
    assert row["확정 LV3_ids"] == "EXAM-04-01" and row["all_LV3_ids"] == "EXAM-04-01 | CMNF-01-02" and row["기존 primary와 일치"] == "일치"
    second = dict(zip(header, [cell.value for cell in sheet[3]]))
    assert second["status"] == "input_unavailable" and second["record_id"] in (None, "")
    detail = workbook["분류 상세"]
    detail_header = [cell.value for cell in detail[1]]
    assert detail_header[6:12] == ["LV3_id", "LV1_id", "LV1", "LV2_id", "LV2", "LV3"]
    assert [detail.cell(row=2, column=detail_header.index(name) + 1).value for name in ("confidence", "판정")] == [0.93, "확정"]
    assert workbook["분류 요약"].max_row == 80


def _write_case(tmp_path: Path, case: int, source_id: str, mappings: list[dict], relevance: str = "관련") -> None:
    lv._write_json(tmp_path / "cache" / f"case_{case}" / f"{source_id}.json", _current({
        "status": "ok", "case": case, "source_id": source_id, "input_kind": "x", "input_chars": 1,
        "result": {"forensic_relevance": relevance, "relevance_reason_ko": "", "mappings": mappings,
                   "rejected_candidates": [], "no_mapping_reason_ko": ""},
    }))


def test_levels_are_honest_by_default_and_minimum_one_is_optional(tmp_path: Path, monkeypatch, techniques) -> None:
    monkeypatch.setattr(lv, "LV_DIR", tmp_path)
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")

    def paper(source_id: str, index: int) -> lv.Paper:
        return lv.Paper(source_id=source_id, paper_index=index, title=source_id, title_kor=source_id, title_eng="", abstract_kor="a",
                        abstract_eng="", abstract="", journal="j", institution="i", publication_year=2026, subject_code="",
                        pdf_path=None, record=f"KNOW-DOMP-{index:07d}")

    def mapping(path_id: str, confidence: float) -> dict:
        return {"rank": 1, "path_id": path_id, "confidence": confidence, "rationale_ko": "r"}

    confirmed, candidate, reanalysed, no_pdf = paper("ART1", 1), paper("ART2", 2), paper("ART3", 3), paper("ART4", 4)
    _write_case(tmp_path, 1, "ART1", [mapping("EXAM-04-01", 0.93), mapping("CMNF-01-02", 0.85), mapping("ANYS-01-04", 0.7)])
    _write_case(tmp_path, 1, "ART2", [mapping("PREP-01-02", 0.72), mapping("REPT-01-03", 0.6)])
    _write_case(tmp_path, 1, "ART3", [], relevance="비관련")
    lv._write_json(tmp_path / "cache" / "case_1_min1" / "ART3.json", _current({"status": "ok", "result": {"analysis_ko": "", "mappings": [mapping("REPT-01-03", 0.3), mapping("PREP-01-02", 0.2)]}}))
    lv._write_json(tmp_path / "cache" / "case_1" / "ART4.json", _current({"status": "input_unavailable", "reason": "원문 PDF 없음"}))
    _write_case(tmp_path, 2, "ART4", [mapping("COLL-02-04", 0.9)])

    # Honest default: only confirmed (>= 0.80) Levels; everything else stays empty.
    assert lv.final_levels(1, confirmed) == ([mapping("EXAM-04-01", 0.93), mapping("CMNF-01-02", 0.85)], lv.BASIS_CONFIRMED)
    for paper_without_confirmed in (candidate, reanalysed, no_pdf):
        assert lv.final_levels(1, paper_without_confirmed) == ([], lv.BASIS_NONE)
    target = lv.export_result(1, [confirmed, candidate, reanalysed, no_pdf], techniques, "260920")
    assert target.name == "(260920)-LV-PDF-Based.xlsx"
    rows = list(load_workbook(target)["논문별 결과"].iter_rows(values_only=True))
    assert rows[0] == lv.RESULT_COLUMNS and load_workbook(target).sheetnames == ["논문별 결과"]
    assert rows[1][5:] == ("EXAM, CMNF", "EXAM-04, CMNF-01", "EXAM-04-01, CMNF-01-02")
    assert all(row[5:] == (None, None, None) for row in rows[2:])  # no confirmed Level -> left blank
    assert all(row[0] and row[1] for row in rows[1:])

    # Optional rule (--min-one): every paper ends with at least one Level.
    assert lv.final_levels(1, candidate, minimum_one=True) == ([mapping("PREP-01-02", 0.72)], lv.BASIS_TOP_CANDIDATE)
    assert lv.final_levels(1, reanalysed, minimum_one=True) == ([mapping("REPT-01-03", 0.3)], lv.BASIS_REANALYSED)
    levels, basis = lv.final_levels(1, no_pdf, minimum_one=True)
    assert [item["path_id"] for item in levels] == ["COLL-02-04"] and basis == lv.BASIS_ABSTRACT
    rows = list(load_workbook(lv.export_result(1, [confirmed, candidate, reanalysed, no_pdf], techniques, "260920", minimum_one=True))["논문별 결과"].iter_rows(values_only=True))
    assert rows[2][5:] == ("PREP", "PREP-01", "PREP-01-02")
    assert rows[3][5:] == ("REPT", "REPT-01", "REPT-01-03") and rows[4][5:] == ("COLL", "COLL-02", "COLL-02-04")


def test_minimum_one_pass_requires_a_level_and_keeps_honest_confidence(techniques) -> None:
    prompt = lv.minimum_one_system_prompt(techniques, 1)
    assert "반드시 1개 이상" in prompt and "가장 가까운" in prompt
    assert lv.minimum_one_schema(techniques)["properties"]["mappings"]["minItems"] == 1
    result = lv.normalize_minimum_one(
        {"analysis_ko": "신학 논문", "mappings": [_mapping(1, "REPT-01-03", 0.85, ("배경 언급", "부분적으로 부합", "추론 필요"), quote="조사심판")]},
        techniques, "본문에 조사심판 이라는 말이 있다",
    )
    assert result["mappings"][0]["confidence"] == 0.49  # a forced choice never looks confirmed
    with pytest.raises(lv.ExperimentError, match="no Level-3"):
        lv.normalize_minimum_one({"analysis_ko": "", "mappings": []}, techniques, "")


def test_published_numbers_come_from_the_profile_index_and_reservations() -> None:
    papers = lv.load_corpus(lv.DEFAULT_DATABASE, lv.load_techniques()[0])
    assert all(paper.paper_index and paper.record_id.startswith("KNOW-DOMP-") for paper in papers)
    assert len({paper.paper_index for paper in papers}) == len(papers) == len({paper.record_id for paper in papers})
    assert [paper.paper_index for paper in papers] == sorted(paper.paper_index for paper in papers)


def test_levels_json_uses_the_pdf_result_and_the_abstract_only_without_a_pdf(tmp_path: Path, monkeypatch, techniques) -> None:
    import json

    monkeypatch.setattr(lv, "LV_DIR", tmp_path)
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(lv, "LEVELS_JSON", tmp_path / "levels.json")

    def paper(source_id: str, index: int) -> lv.Paper:
        return lv.Paper(source_id=source_id, paper_index=index, title=source_id, title_kor=source_id, title_eng="", abstract_kor="a",
                        abstract_eng="", abstract="", journal="j", institution="i", publication_year=2026, subject_code="",
                        pdf_path=None, record=f"KNOW-DOMP-{index:07d}")

    def mapping(path_id: str, confidence: float) -> dict:
        return {"rank": 1, "path_id": path_id, "confidence": confidence, "rationale_ko": "r"}

    _write_case(tmp_path, 1, "ART1", [mapping("EXAM-04-01", 0.93), mapping("EXAM-04-02", 0.85), mapping("ANYS-01-04", 0.79)])
    _write_case(tmp_path, 2, "ART1", [mapping("CMNF-01-02", 0.95)])
    _write_case(tmp_path, 3, "ART1", [mapping("EXAM-04-01", 0.9)])
    _write_case(tmp_path, 1, "ART2", [mapping("PREP-01-02", 0.7)])              # full text read, nothing confirmed
    _write_case(tmp_path, 2, "ART2", [mapping("PREP-01-02", 0.9)])
    lv._write_json(tmp_path / "cache" / "case_1" / "ART3.json", _current({"status": "input_unavailable", "reason": "원문 PDF 없음"}))
    _write_case(tmp_path, 2, "ART3", [mapping("COLL-02-04", 0.9)])

    path = lv.export_levels_json([paper("ART1", 1), paper("ART2", 2), paper("ART3", 7)], techniques, "sha")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["paper_count"] == 3 and payload["taxonomy_sha256"] == "sha"
    one, two, three = (payload["papers"][key] for key in ("ART1", "ART2", "ART3"))
    assert (one["basis"], one["lv3"], one["lv2"], one["lv1"]) == ("PDF", ["EXAM-04-01", "EXAM-04-02"], ["EXAM-04"], ["EXAM"])
    assert one["abstract_based"] == ["CMNF-01-02"] and one["summary_based"] == ["EXAM-04-01"]
    # The abstract never overrides a full-text result, even an empty one.
    assert (two["basis"], two["lv3"]) == ("PDF", [])
    assert (three["basis"], three["lv3"], three["paper_index"], three["record_id"]) == ("Abstract", ["COLL-02-04"], 7, "KNOW-DOMP-0000007")


def test_only_the_older_copy_of_the_same_workbook_is_replaced(tmp_path: Path) -> None:
    for name in ("(260919)-LV-PDF-Based.xlsx", "(260920)-LV-PDF-Based.xlsx", "(260919)-LV-Abstract-Based.xlsx",
                 "(260919)-filter-lv.xlsx", "notes-LV-PDF-Based.xlsx"):
        (tmp_path / name).write_bytes(b"x")

    lv.replace_dated_workbook(tmp_path / "(260920)-LV-PDF-Based.xlsx")

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "(260919)-LV-Abstract-Based.xlsx", "(260919)-filter-lv.xlsx", "(260920)-LV-PDF-Based.xlsx", "notes-LV-PDF-Based.xlsx",
    ]


def test_a_paper_without_a_profile_never_borrows_another_papers_record_id() -> None:
    """``record_id`` is only what the corpus loader resolved; the dataclass invents nothing."""

    paper = lv.Paper(source_id="ART000000009", paper_index=528, title="t", title_kor="t", title_eng="", abstract_kor="a", abstract_eng="",
                     abstract="", journal="j", institution="i", publication_year=2026, subject_code="", pdf_path=None)
    assert paper.record_id == ""


def test_pending_level_is_not_reported_as_no_level(tmp_path: Path, monkeypatch, techniques) -> None:
    import json

    monkeypatch.setattr(lv, "LV_DIR", tmp_path)
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(lv, "LEVELS_JSON", tmp_path / "levels.json")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    def paper(source_id: str, pdf_path: Path | None) -> lv.Paper:
        return lv.Paper(source_id=source_id, paper_index=1, title=source_id, title_kor=source_id, title_eng="", abstract_kor="a",
                        abstract_eng="", abstract="", journal="j", institution="i", publication_year=2026, subject_code="",
                        pdf_path=pdf_path, record="KNOW-DOMP-0000001")

    # Full text exists for both papers with a PDF (cached), the third has no PDF at all.
    (tmp_path / "cache" / "text").mkdir(parents=True)
    for source_id in ("ART1", "ART2"):
        (tmp_path / "cache" / "text" / f"{source_id}.txt").write_text("=== PAGE 1 ===\n본문", encoding="utf-8")
    mapping = {"rank": 1, "path_id": "COLL-02-04", "confidence": 0.9, "rationale_ko": "r"}
    _write_case(tmp_path, 2, "ART1", [mapping])   # abstract answered, full-text call failed -> must NOT fall back to the abstract
    # A stale "no PDF" answer from before the PDF arrived must not count either.
    lv._write_json(tmp_path / "cache" / "case_1" / "ART2.json", _current({"status": "input_unavailable", "reason": "원문 PDF 없음"}))
    _write_case(tmp_path, 2, "ART2", [mapping])
    # No PDF and no abstract answer yet.
    payload = json.loads(lv.export_levels_json([paper("ART1", pdf), paper("ART2", pdf), paper("ART3", None)], techniques, "sha").read_text(encoding="utf-8"))

    assert payload["pending_count"] == 3
    for source_id, basis in (("ART1", "PDF"), ("ART2", "PDF"), ("ART3", "Abstract")):
        record = payload["papers"][source_id]
        assert (record["status"], record["basis"], record["lv3"]) == ("pending", basis, [])
    assert payload["papers"]["ART3"]["basis_reason"] == "원문 PDF 없음"


def test_older_dates_are_replaced_but_never_newer_ones(tmp_path: Path) -> None:
    for name in ("(260901)-LV-PDF-Based.xlsx", "(260920)-LV-PDF-Based.xlsx", "(260910)-LV-PDF-Based.xlsx"):
        (tmp_path / name).write_bytes(b"x")
    lv.replace_dated_workbook(tmp_path / "(260910)-LV-PDF-Based.xlsx")   # a run with an earlier --date
    assert sorted(path.name for path in tmp_path.iterdir()) == ["(260910)-LV-PDF-Based.xlsx", "(260920)-LV-PDF-Based.xlsx"]
