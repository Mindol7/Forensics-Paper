from __future__ import annotations

import dataclasses
import json
import types
from pathlib import Path

import pytest
from openpyxl import load_workbook

from Analyzer.LV import lv_compare
from Analyzer.LV import lv_experiment as lv
from Analyzer.LV import lv_retry as retry


@pytest.fixture(scope="module")
def techniques():
    return lv.load_techniques()[0]


def _paper(index: int, title: str = "", pdf: bool = True) -> lv.Paper:
    return lv.Paper(source_id=f"ART{index:09d}", paper_index=index, title=title or f"논문 {index}", title_kor=title or f"논문 {index}",
                    title_eng="", abstract_kor="족적 이미지를 딥러닝으로 분류했다.", abstract_eng="", abstract="", journal="j",
                    institution="i", publication_year=2026, subject_code="", pdf_path=Path("x.pdf") if pdf else None,
                    record=f"KNOW-DOMP-{index:07d}")


def _first_try(directory: Path, rows: dict[int, dict[str, str]], papers: list[lv.Paper]) -> None:
    """First-Try workbooks: paper index -> {case label: all_lv3}."""

    from openpyxl import Workbook

    directory.mkdir(parents=True, exist_ok=True)
    for case, label in lv.CASE_FILE_LABELS.items():
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "논문별 결과"
        sheet.append(list(lv.RESULT_COLUMNS))
        for paper in papers:
            sheet.append([paper.paper_index, paper.record_id, paper.display_title, "j", 2026, "", "", rows[paper.paper_index].get(label, "")])
        workbook.save(directory / f"(261005)-LV-{label}.xlsx")


def _mapping(path_id: str, confidence: float, fit: str = "부분적으로 부합", rank: int = 1) -> dict:
    return {"rank": rank, "path_id": path_id, "confidence": confidence, "directness": "핵심 기여", "definition_fit": fit,
            "evidence_explicitness": "명시적", "rationale_ko": "물리적 흔적이라 부분 부합", "evidence_quote": "족적 이미지를 딥러닝으로 분류했다.",
            "quote_verified": True}


def _record(case: int, source_id: str, mappings: list[dict], relevance: str = "부분 관련", version: str = lv.PROMPT_VERSION) -> dict:
    return {
        "schema_version": 1, "prompt_version": version, "status": "ok", "case": case, "source_id": source_id,
        "taxonomy_sha256": lv.current_taxonomy_sha256(), "input_sha256": f"{version}-{case}",
        "result": {"forensic_relevance": relevance, "relevance_reason_ko": "관련성 이유", "no_mapping_reason_ko": "",
                   "mappings": mappings, "rejected_candidates": [{"path_id": "CMNF-02-03", "confidence": 0.4, "reason_ko": "탈락 이유"}]},
    }


def _unavailable(case: int, source_id: str, version: str = lv.PROMPT_VERSION) -> dict:
    return {"schema_version": 1, "prompt_version": version, "status": "input_unavailable", "case": case, "source_id": source_id}


def _write_first(tmp_path: Path, paper: lv.Paper, mappings_by_case: dict[int, list] | None = None, relevance: str = "부분 관련") -> None:
    for case in retry.CASES:
        path = tmp_path / "cache" / f"case_{case}" / f"{paper.source_id}.json"
        if paper.pdf_path is None and case != 2:
            lv._write_json(path, _unavailable(case, paper.source_id))
        else:
            lv._write_json(path, _record(case, paper.source_id, (mappings_by_case or {}).get(case, []), relevance))


def _retry_records(paper: lv.Paper, cases: dict[int, tuple[str, list[dict]]]) -> dict[int, dict]:
    """case -> (relevance, mappings) as re-review records; a paper without a PDF has only case 2."""

    records = {}
    for case in retry.CASES:
        if paper.pdf_path is None and case != 2:
            records[case] = _unavailable(case, paper.source_id, retry.RETRY_PROMPT_VERSION)
        else:
            relevance, mappings = cases[case]
            records[case] = _record(case, paper.source_id, mappings, relevance, retry.RETRY_PROMPT_VERSION)
    return records


def test_step2_lists_the_papers_with_no_level_in_all_three_first_try_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    papers = [_paper(1), _paper(2), _paper(3, pdf=False)]
    _write_first(tmp_path, papers[0], {1: [_mapping("EXAM-04-01", 0.9, "정확히 부합")]})
    _write_first(tmp_path, papers[1])
    _write_first(tmp_path, papers[2])
    _first_try(tmp_path / "First-Try", {1: {"PDF-Based": "EXAM-04-01"}, 2: {}, 3: {}}, papers)
    first_try = retry.read_first_try(tmp_path / "First-Try")
    assert [paper.source_id for paper in retry.unmatched_papers(papers, first_try)] == ["ART000000002", "ART000000003"]

    # Renumbered since: the rows are matched by title, so the result is the same.
    moved = [dataclasses.replace(paper, record=f"KNOW-DOMP-{paper.paper_index + 10:07d}") for paper in papers]
    assert [paper.source_id for paper in retry.unmatched_papers(moved, first_try)] == ["ART000000002", "ART000000003"]
    with pytest.raises(retry.RetryError, match="no single paper"):
        retry.unmatched_papers([papers[0], _paper(2, title="다른 논문"), papers[2]], first_try)
    # The cache must still be what the First-Try files show, for every paper.
    lv._write_json(tmp_path / "cache" / "case_1" / "ART000000001.json",
                   _record(1, "ART000000001", [_mapping("EXAM-04-01", 0.9, "정확히 부합"), _mapping("CVGF-05-01", 0.85, "대체로 부합", 2)]))
    with pytest.raises(retry.RetryError, match="differ"):
        retry.unmatched_papers(papers, first_try)
    (tmp_path / "cache" / "case_1" / "ART000000001.json").unlink()
    with pytest.raises(retry.RetryError, match="no current first-try record"):
        retry.unmatched_papers(papers, first_try)


def test_step3_relevance_decides_the_paper_and_step4_gives_related_papers_one_level_per_input() -> None:
    paper = _paper(1)
    related = retry.Outcome(paper, _retry_records(paper, {
        1: ("부분 관련", [_mapping("CVGF-05-01", 0.72), _mapping("PREP-01-01", 0.65, rank=2)]),
        2: ("관련", [_mapping("CVGF-02-02", 0.85, "대체로 부합")]),
        3: ("비관련", []),
    }))
    assert related.relevance == retry.RELATED and related.verdicts == {1: "부분 관련", 2: "관련", 3: "비관련"}
    assert related.assigned(1) == ([related.retry[1]["result"]["mappings"][0]], retry.METHOD_TOP)      # the top candidate only
    assert related.assigned(2)[1] == retry.METHOD_CONFIRMED
    assert related.needs_force(3) and related.assigned(3) == ([], "미완료") and not related.complete
    related.forced = {3: {"status": "ok", "result": {"mappings": [_mapping("ANYS-01-01", 0.3, "부합하지 않음")]}}}
    assert related.assigned(3)[1] == retry.METHOD_FORCED and related.complete

    partly = retry.Outcome(paper, _retry_records(paper, {case: ("부분 관련", [_mapping("CVGF-05-01", 0.7)]) for case in retry.CASES}))
    assert partly.relevance == retry.PARTLY
    unrelated = retry.Outcome(paper, _retry_records(paper, {case: ("비관련", []) for case in retry.CASES}))
    assert unrelated.relevance == retry.UNRELATED and unrelated.assigned(1) == ([], "") and not unrelated.needs_force(1)
    # A paper without a PDF is judged on its abstract alone; its PDF and Summary columns stay empty.
    no_pdf = _paper(2, pdf=False)
    abstract_only = retry.Outcome(no_pdf, _retry_records(no_pdf, {2: ("부분 관련", [])}))
    assert abstract_only.relevance == retry.PARTLY and abstract_only.needs_force(2) and not abstract_only.needs_force(1)
    # An unconfirmed re-review: no verdict yet.
    assert retry.Outcome(paper, {1: None, 2: None, 3: None}).relevance == "미완료"


class _FakeCaller:
    answer: dict = {}
    calls: list[dict] = []

    def __init__(self, cli, retries=3, **_kwargs):
        self.cli = cli
        self.stop = types.SimpleNamespace(is_set=lambda: False)

    def call(self, *, system_prompt, prompt, schema, validate, tags):
        type(self).calls.append({"system": system_prompt, "prompt": prompt, "tags": tags, "schema": schema})
        return {"result": validate(type(self).answer), "model": "claude-opus-5", "usage": {}, "attempts": 1, "fallback_for": None}


def test_the_re_review_and_the_forced_pick_keep_their_own_caches(tmp_path: Path, monkeypatch, techniques) -> None:
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(retry, "RetryingCaller", _FakeCaller)
    monkeypatch.setattr(retry, "ClaudeCli", lambda model, timeout: types.SimpleNamespace(model=model))
    paper = _paper(1)
    _write_first(tmp_path, paper, {2: [_mapping("CVGF-05-01", 0.72)]})
    first = json.loads((tmp_path / "cache" / "case_2" / f"{paper.source_id}.json").read_text(encoding="utf-8"))
    _FakeCaller.calls = []
    _FakeCaller.answer = {"forensic_relevance": "비관련", "relevance_reason_ko": "입력에 디지털 증거가 없음", "no_mapping_reason_ko": "해당 없음",
                          "rejected_candidates": [], "mappings": []}
    retrier = retry.Retrier(techniques, lv.current_taxonomy_sha256(), model="claude-opus-5", timeout=1, retries=1)
    record = retrier.classify(paper, 2)
    sent = _FakeCaller.calls[0]
    assert sent["system"] == lv.classification_system_prompt(techniques, 2) + retry.RETRY_NOTE
    assert sent["tags"]["task"] == "lv_case2_retry" and "0.72" not in sent["prompt"] and "[같은 입력에 대한 이전 판단(참고)]" in sent["prompt"]
    assert record["prompt_version"] == retry.RETRY_PROMPT_VERSION and retry.load_retry_record(2, paper.source_id)["status"] == "ok"
    assert json.loads((tmp_path / "cache" / "case_2" / f"{paper.source_id}.json").read_text(encoding="utf-8")) == first   # first try untouched
    with pytest.raises(retry.RetryError, match="no current first-try record"):
        retrier.classify(_paper(4), 2)

    # The paper is related through another input: this input gets the closest Level-3 (minimum-one prompt).
    _FakeCaller.answer = {"analysis_ko": "족적 이미지 분류", "mappings": [
        {"rank": 1, "path_id": "CVGF-05-01", "directness": "핵심 기여", "definition_fit": "부분적으로 부합", "evidence_explicitness": "명시적",
         "confidence": 0.7, "rationale_ko": "가장 가까움", "evidence_quote": "족적 이미지를 딥러닝으로 분류했다."}]}
    forcer = retry.Forcer(techniques, lv.current_taxonomy_sha256(), model="claude-opus-5", timeout=1, retries=1)
    forced = forcer.classify(paper, 2, record)
    sent = _FakeCaller.calls[-1]
    assert sent["system"] == lv.minimum_one_system_prompt(techniques, 2) + retry.FORCE_NOTE and sent["tags"]["task"] == "lv_case2_force"
    assert sent["schema"]["properties"]["mappings"]["minItems"] == 1 and "[이전 분류의 판단]" in sent["prompt"]
    assert forced["result"]["mappings"][0]["path_id"] == "CVGF-05-01"
    assert retry.load_force_record(2, paper.source_id, record)["status"] == "ok"
    assert retry.load_force_record(2, paper.source_id, {**record, "input_sha256": "another re-review"}) is None   # made from another answer
    calls = len(_FakeCaller.calls)
    assert forcer.classify(paper, 2, record)["created_at"] == forced["created_at"] and len(_FakeCaller.calls) == calls   # cached


def test_failure_list_remove_list_and_second_try_files(tmp_path: Path, monkeypatch, techniques) -> None:
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    kept, mapped, forced, unrelated = _paper(1), _paper(2), _paper(3), _paper(4)
    _write_first(tmp_path, kept, {case: [_mapping("EXAM-04-01", 0.9, "정확히 부합")] for case in retry.CASES})
    for paper in (mapped, forced, unrelated):
        _write_first(tmp_path, paper, {case: [_mapping("CVGF-05-01", 0.72)] for case in retry.CASES})
    outcomes = {
        mapped.source_id: retry.Outcome(mapped, _retry_records(mapped, {case: ("부분 관련", [_mapping("CVGF-05-01", 0.72)]) for case in retry.CASES})),
        forced.source_id: retry.Outcome(forced, _retry_records(forced, {1: ("관련", [_mapping("COLL-02-04", 0.68)]), 2: ("비관련", []),
                                                                       3: ("관련", [_mapping("COLL-02-04", 0.6)])})),
        unrelated.source_id: retry.Outcome(unrelated, _retry_records(unrelated, {case: ("비관련", []) for case in retry.CASES})),
    }
    outcomes[forced.source_id].forced = {2: {"status": "ok", "result": {"mappings": [_mapping("EXAM-05-04", 0.3, "부합하지 않음")]}}}
    outcomes[mapped.source_id].judgements = {"CVGF-05-01": {"verdict": "애매", "verdict_A": "맞음", "verdict_B": "애매"}}
    ordered = list(outcomes.values())

    listed = retry.export_failure_list(ordered, techniques, tmp_path / "(261005)-Mapping-Failed-List.xlsx")
    workbook = load_workbook(listed)
    assert workbook.sheetnames == ["매핑 실패 목록", "2차 매핑 결과", "요약"]
    assert [row[1] for row in workbook["매핑 실패 목록"].iter_rows(min_row=2, values_only=True)] == [paper.record_id for paper in (mapped, forced, unrelated)]
    results = {row[1]: row for row in workbook["2차 매핑 결과"].iter_rows(min_row=2, values_only=True)}
    header = next(workbook["2차 매핑 결과"].iter_rows(max_row=1, values_only=True))
    assert results[mapped.record_id][header.index("논문 판정")] == retry.PARTLY and results[unrelated.record_id][header.index("처리")] == "제거 목록"
    assert results[forced.record_id][header.index("Abstract 배정")].startswith(f"EXAM-05-04") and retry.METHOD_FORCED in results[forced.record_id][header.index("Abstract 배정")]
    assert retry.METHOD_TOP in results[mapped.record_id][header.index("PDF 배정")] and "확신도 0.72" in results[mapped.record_id][header.index("PDF 배정")]
    assert results[mapped.record_id][header.index("본문 심사(배정 Level)")] == "CVGF-05-01: 애매 (A 맞음 / B 애매)"

    removal = retry.export_remove_list(ordered, techniques, tmp_path / "Remove-List" / "(261005)-Remove-List.xlsx")
    workbook = load_workbook(removal)
    assert workbook.sheetnames == ["논문별 결과", "요약"]
    rows = list(workbook["논문별 결과"].iter_rows(values_only=True))
    assert rows[0][:2] == ("paper_index", "record_id") and "title" in rows[0] and [row[1] for row in rows[1:]] == [unrelated.record_id]
    assert workbook["논문별 결과"]["C2"].fill.fgColor.rgb[-6:] != "FFFF00"      # the yellow 'remove' mark stays the user's decision

    remaining = [kept, mapped, forced]
    kept_outcomes = {source_id: outcome for source_id, outcome in outcomes.items() if source_id != unrelated.source_id}
    files = retry.export_second_try(remaining, kept_outcomes, techniques, "261005", tmp_path / "Second-Try")
    assert [path.name for path in files] == ["(261005)-LV-PDF-Based.xlsx", "(261005)-LV-Abstract-Based.xlsx", "(261005)-LV-Summary-Based.xlsx"]
    pdf = list(load_workbook(files[0])["논문별 결과"].iter_rows(values_only=True))
    abstract = list(load_workbook(files[1])["논문별 결과"].iter_rows(values_only=True))
    assert pdf[0] == lv.RESULT_COLUMNS
    assert [(row[1], row[7]) for row in pdf[1:]] == [(kept.record_id, "EXAM-04-01"), (mapped.record_id, "CVGF-05-01"), (forced.record_id, "COLL-02-04")]
    assert abstract[3][7] == "EXAM-05-04"                                         # the forced pick fills the abstract column

    # lv_compare reads the Levels each input was given.
    comparison = lv_compare.load_comparisons([forced], retry.second_try_overrides(kept_outcomes))[0]
    assert (comparison.ids(1), comparison.ids(2), comparison.ids(3)) == (["COLL-02-04"], ["EXAM-05-04"], ["COLL-02-04"])
