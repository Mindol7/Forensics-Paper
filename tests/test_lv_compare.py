from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from Analyzer.LV import lv_compare as cmp
from Analyzer.LV import lv_experiment as lv

def _current(record: dict) -> dict:
    """A cache record of the current prompts and taxonomy (records of an older taxonomy count as missing)."""

    return {"prompt_version": lv.PROMPT_VERSION, "taxonomy_sha256": lv.current_taxonomy_sha256(), **record}



@pytest.fixture(scope="module")
def techniques():
    return lv.load_techniques()[0]


def _paper(source_id: str, index: int, pdf: bool = True) -> lv.Paper:
    return lv.Paper(source_id=source_id, paper_index=index, title=source_id, title_kor=f"논문 {index}", title_eng="", abstract_kor="a",
                    abstract_eng="", abstract="", journal="j", institution="i", publication_year=2026, subject_code="",
                    pdf_path=Path("x.pdf") if pdf else None, record=f"KNOW-DOMP-{index:07d}")


def _case(tmp_path: Path, case: int, source_id: str, mappings: list[tuple[str, float]], relevance: str = "관련") -> None:
    lv._write_json(tmp_path / "cache" / f"case_{case}" / f"{source_id}.json", _current({
        "status": "ok", "result": {
            "forensic_relevance": relevance, "relevance_reason_ko": "관련성 설명", "no_mapping_reason_ko": "",
            "rejected_candidates": [],
            "mappings": [{"rank": i + 1, "path_id": p, "confidence": c, "rationale_ko": "r"} for i, (p, c) in enumerate(mappings)],
        },
    }))


def _judge(tmp_path: Path, judge: str, source_id: str, verdicts: dict[str, str]) -> None:
    lv._write_json(tmp_path / "cache" / f"judge_{judge}" / f"{source_id}.json", {
        "status": "ok", "judge": judge, "source_id": source_id, "candidates": list(verdicts), "contributions_ko": "c",
        "verdicts": {path_id: {"verdict": verdict, "reason_ko": f"{judge} 이유"} for path_id, verdict in verdicts.items()},
    })


def test_comparison_judges_only_differing_papers_and_picks_the_more_accurate_file(tmp_path: Path, monkeypatch, techniques) -> None:
    monkeypatch.setattr(lv, "LV_DIR", tmp_path)
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    more, same, empty, no_pdf = _paper("ART1", 1), _paper("ART2", 2), _paper("ART3", 3), _paper("ART4", 4, pdf=False)
    # ART1: the full text matched four Levels, the abstract two, the summary three.
    _case(tmp_path, 1, "ART1", [("EXAM-04-01", 0.93), ("CMNF-01-02", 0.9), ("ANYS-01-04", 0.82), ("CVGF-01-05", 0.8), ("EXAM-05-04", 0.6)])
    _case(tmp_path, 2, "ART1", [("EXAM-04-01", 0.93), ("CMNF-01-02", 0.88)])
    _case(tmp_path, 3, "ART1", [("EXAM-04-01", 0.93), ("CMNF-01-02", 0.9), ("ANYS-01-04", 0.8)])
    _judge(tmp_path, "A", "ART1", {"EXAM-04-01": "맞음", "CMNF-01-02": "맞음", "ANYS-01-04": "맞음", "CVGF-01-05": "틀림"})
    _judge(tmp_path, "B", "ART1", {"EXAM-04-01": "맞음", "CMNF-01-02": "맞음", "ANYS-01-04": "애매", "CVGF-01-05": "틀림"})
    for case in (1, 2, 3):
        _case(tmp_path, case, "ART2", [("PREP-01-02", 0.92)])
        _case(tmp_path, case, "ART3", [("REPT-01-03", 0.7)] if case == 1 else [], relevance="부분 관련" if case == 1 else "비관련")
    lv._write_json(tmp_path / "cache" / "case_1" / "ART4.json", _current({"status": "input_unavailable"}))
    _case(tmp_path, 2, "ART4", [("COLL-02-04", 0.9)])
    lv._write_json(tmp_path / "cache" / "case_3" / "ART4.json", _current({"status": "input_unavailable"}))

    comparisons = cmp.load_comparisons([more, same, empty, no_pdf])
    by_id = {c.paper.source_id: c for c in comparisons}
    assert by_id["ART1"].ids(1) == ["EXAM-04-01", "CMNF-01-02", "ANYS-01-04", "CVGF-01-05"]  # 0.60 is not confirmed
    assert not by_id["ART1"].identical and by_id["ART2"].identical and by_id["ART3"].identical
    assert not by_id["ART4"].comparable
    assert by_id["ART1"].union == ["EXAM-04-01", "CMNF-01-02", "ANYS-01-04", "CVGF-01-05"]

    verdicts = cmp.consensus(by_id["ART1"])
    assert {k: v["verdict"] for k, v in verdicts.items()} == {
        "EXAM-04-01": "맞음", "CMNF-01-02": "맞음", "ANYS-01-04": "애매", "CVGF-01-05": "틀림"}
    conclusion, scores = cmp.paper_conclusion(by_id["ART1"], verdicts)
    # two correct Levels: the abstract found exactly those; the full text added one vague and one wrong.
    assert conclusion == "가장 정확: Abstract"
    assert (scores[1]["right"], scores[1]["vague"], scores[1]["wrong"]) == (2, 1, 1) and scores[2]["precision"] == 1.0

    assert cmp.unmatched_reason(by_id["ART3"], 1) == "후보는 있으나 confidence 0.80 미만"
    assert cmp.unmatched_reason(by_id["ART3"], 2) == "디지털 포렌식 비관련으로 판정"
    assert cmp.unmatched_reason(by_id["ART4"], 1) == "입력 없음(원문 PDF 미확보)"

    target = cmp.export(comparisons, techniques, "260920")
    assert target.name == "(260920)-compare-lv.xlsx"
    workbook = load_workbook(target)
    assert workbook.sheetnames == ["요약", "논문별 비교", "분류별 판정", "매칭 안 된 논문"]
    summary = {row[0]: row[1:] for row in workbook["요약"].iter_rows(values_only=True) if row[0]}
    assert summary["매칭되지 않은 논문 수(빈칸)"][:3] == (2, 1, 2)
    assert summary["  └ 입력 없음(원문 PDF 미확보)"][:3] == (1, 0, 1)
    assert summary["  └ 서로 다른 논문"][0] == 1 and summary["해당 논문 수"][0] == 1
    assert summary["본문에만 있는 추가 매칭 건수"][0] == 2 and summary["이 논문들에서 초록 결과가 더 정확"][0] == 1
    rows = list(workbook["논문별 비교"].iter_rows(min_row=2, values_only=True))
    assert rows[0][11:13] == ("다름", "가장 정확: Abstract") and rows[1][11] == "세 파일 동일"
    assert rows[2][11] == "세 파일 모두 매칭 없음" and rows[3][11].startswith("비교 불가")
    levels = list(workbook["분류별 판정"].iter_rows(min_row=2, values_only=True))
    assert len(levels) == 4 and levels[3][3] == "CVGF-01-05" and levels[3][7:10] == ("O", None, None) and levels[3][13] == "틀림"


def test_judges_are_blind_and_must_rate_every_candidate(techniques) -> None:
    prompt_a, prompt_b = cmp.judge_system_prompt(techniques, "A"), cmp.judge_system_prompt(techniques, "B")
    assert "어느 분류기가 어떤 후보를 냈는지" in prompt_a and "확신도가 얼마였는지는 알려 드리지 않습니다" in prompt_a
    assert "후보 목록을 보지 않은 상태라고 생각하고" in prompt_b and "후보 목록을 보지 않은 상태라고 생각하고" not in prompt_a
    schema = cmp.judge_schema(["EXAM-04-01", "CMNF-01-02"])
    assert schema["properties"]["verdicts"]["minItems"] == schema["properties"]["verdicts"]["maxItems"] == 2
    assert cmp.blind_order(["a", "b", "c"], "ART1") == cmp.blind_order(["c", "a", "b"], "ART1")
    assert cmp.JUDGES["A"] != cmp.JUDGES["B"]


def test_a_missing_judge_leaves_the_paper_unjudged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    paper = _paper("ART1", 1)
    _case(tmp_path, 1, "ART1", [("EXAM-04-01", 0.93)])
    _case(tmp_path, 2, "ART1", [])
    _case(tmp_path, 3, "ART1", [("EXAM-04-01", 0.9)])
    _judge(tmp_path, "A", "ART1", {"EXAM-04-01": "맞음"})
    comparison = cmp.load_comparisons([paper])[0]
    assert cmp.consensus(comparison) == {}
    _judge(tmp_path, "B", "ART1", {"EXAM-04-01": "틀림"})
    merged = cmp.consensus(comparison)
    assert merged["EXAM-04-01"]["verdict"] == "애매" and merged["EXAM-04-01"]["split"] is True


def test_judge_calls_are_tagged_for_the_ledger_and_a_refused_paper_falls_back(tmp_path: Path, monkeypatch, techniques) -> None:
    from Analyzer import claude_cli

    monkeypatch.setattr(lv, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(lv, "full_text", lambda paper: "본문")
    calls: list[tuple[str, dict]] = []

    class FakeCli:
        def __init__(self, model, timeout=1.0, executable=None, *, effort=None, ledger=None):
            self.model, self.timeout, self.executable, self.effort, self.ledger = model, timeout, executable, effort, ledger

        def request(self, *, system_prompt, prompt, schema, ledger_tags=None, validate=None, **_kwargs):
            calls.append((self.model, dict(ledger_tags or {})))
            if self.model == cmp.JUDGES["B"]:
                raise claude_cli.ClaudeCliError("CLI failure (safeguards flagged this request)")
            ids = schema["properties"]["verdicts"]["items"]["properties"]["path_id"]["enum"]
            answer = {"contributions_ko": "c", "verdicts": [{"path_id": i, "verdict": "맞음", "reason_ko": "r"} for i in ids]}
            return validate(answer), self.model, {"total_cost_usd": 0.1}

    monkeypatch.setattr(cmp, "ClaudeCli", FakeCli)
    monkeypatch.setattr(claude_cli, "ClaudeCli", FakeCli)        # the fallback model is built inside RetryingCaller
    _case(tmp_path, 1, "ART1", [("EXAM-04-01", 0.93)])
    _case(tmp_path, 2, "ART1", [])
    _case(tmp_path, 3, "ART1", [("EXAM-04-01", 0.9)])
    comparison = cmp.load_comparisons([_paper("ART1", 1)])[0]
    runner = cmp.JudgeRunner(techniques, timeout=1, retries=1)
    first = runner.judge(comparison, "A")
    assert (first["model"], first["fallback_for"]) == (cmp.JUDGES["A"], None)
    assert calls[0][1] == {"stage": "lv_compare", "task": "lv_judge_A", "source_id": "ART1", "attempt": 1}
    second = runner.judge(comparison, "B")
    assert (second["model"], second["fallback_for"]) == ("claude-sonnet-5", cmp.JUDGES["B"])
    assert [model for model, _tags in calls[1:]] == [cmp.JUDGES["B"], "claude-sonnet-5"]
    assert all(tags["task"] == "lv_judge_B" for _model, tags in calls[1:])
    assert runner.judge(comparison, "A") == first and len(calls) == 3            # cached
    assert cmp.fallback_judgements([comparison]) == {"B: claude-sonnet-5": 1}

    target = cmp.export([comparison], techniques, "261005", tmp_path / "First-Try")
    assert target == tmp_path / "First-Try" / "(261005)-compare-lv.xlsx" and not list((tmp_path / "First-Try").glob(".*"))
    workbook = load_workbook(target)
    summary = {row[0]: row[1] for row in workbook["요약"].iter_rows(values_only=True) if row[0]}
    assert summary["대체 심사"].endswith("B: claude-sonnet-5 1편")
    assert list(workbook["논문별 비교"].iter_rows(min_row=2, values_only=True))[0][11:13] == ("다름", "가장 정확: PDF, Summary")

    # A verdict given for another question (e.g. the old taxonomy) is not used, even for the same candidates.
    path = tmp_path / "cache" / "judge_A" / "ART1.json"
    stale = {**lv.json.loads(path.read_text(encoding="utf-8")), "input_sha256": "verdict-of-2026-09-20"}
    path.write_text(lv.json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    assert cmp.consensus(comparison) != {}                                     # without the hashes: candidates only
    target = cmp.export([comparison], techniques, "261005", tmp_path / "First-Try")
    assert list(load_workbook(target)["논문별 비교"].iter_rows(min_row=2, values_only=True))[0][11:13] == ("다름", "판정 미완료")


def test_the_closest_candidate_is_the_most_confident_one() -> None:
    result = {"forensic_relevance": "관련", "relevance_reason_ko": "관련성", "no_mapping_reason_ko": "",
              "mappings": [{"path_id": "CVGF-04-01", "confidence": 0.58, "rationale_ko": "부수"},
                           {"path_id": "COLL-02-02", "confidence": 0.66, "rationale_ko": "주요하지만 부분 부합"}],
              "rejected_candidates": [{"path_id": "EXAM-05-02", "confidence": 0.4, "reason_ko": "탈락"}]}
    top, explanation = cmp.closest_candidate(result)
    assert (top["path_id"], explanation) == ("COLL-02-02", "주요하지만 부분 부합")
    top, explanation = cmp.closest_candidate({**result, "mappings": [], "no_mapping_reason_ko": "맞는 기술 없음"})
    assert (top["path_id"], top["confidence"], explanation) == ("EXAM-05-02", 0.4, "맞는 기술 없음")
    assert cmp.closest_candidate(None) == (None, "")

