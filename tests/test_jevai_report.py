from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import types
from pathlib import Path

import pytest
from openpyxl import load_workbook

from Analyzer.Application import registry as registry_module
from Analyzer.JevAI import jevai_report as jev

CATALOG = [
    {"id": "EXAM-04-01-02-01", "name": "KakaoTalk"},
    {"id": "COLL-02-04-01-01", "name": "KakaoTalk"},
    {"id": "EXAM-04-01-07-05", "name": "Dropbox"},
]
THRESHOLDS1 = {"taxonomy": 0.33, "entity": 0.36, "level5": 0.44}
THRESHOLDS3 = {"taxonomy": 0.33, "entity": 0.36, "level5": 0.44, "app": 0.44, "saas": 0.32, "eligibility": 0.6}


def _hash(product: str) -> str:
    return hashlib.sha256(product.encode("utf-8")).hexdigest()[:16]


def _stage1(paper_id: str, taxonomy: dict[str, float], entity: dict[str, float], model: str = "jev-1.13.0") -> dict:
    return {"event": "mapping_scores", "paper_id": paper_id, "stage": 1, "status": "computed", "scores_available": True,
            "thresholds": THRESHOLDS1, "model": model,
            "taxonomy": {k: {"probability": v, "substantive_relevance_method": "whole_document_direct"} for k, v in taxonomy.items()},
            "entity": {k: {"probability": v} for k, v in entity.items()}}


def _stage3(paper_id: str, apps: list[dict], entity: dict[str, float] | None = None, scores: dict | None = None,
            catalog: list[dict] = CATALOG) -> dict:
    """apps: {name, identity: {product: p}, contexts: [{L3: p}], scopes: [{L4: p}]} -> a stage-3 record (min of three factors)."""

    answers, rows, scores = {}, [], scores or {}
    for index, app in enumerate(apps):
        for product in {item["name"] for item in catalog}:
            answers[f"identity_a{index}_{_hash(product)}"] = app["identity"].get(product, 0.02)
        for context, (l3s, l4s) in enumerate(zip(app["contexts"], app["scopes"])):
            for item in catalog:
                l3, l4 = item["id"][:10], item["id"].rsplit("-", 1)[0]
                ids = [f"identity_a{index}_{_hash(item['name'])}", f"context_a{index}_c{context}_{l3}", f"scope_a{index}_c{context}_{l4}"]
                answers[ids[1]] = l3s.get(l3, 0.03)
                answers[ids[2]] = l4s.get(l4, 0.03)
                rows.append({"application_index": index, "context_index": context, "id": item["id"],
                             "probability": min(answers[i] for i in ids), "factor_question_ids": ids})
    return {"event": "mapping_scores", "paper_id": paper_id, "stage": 3, "status": "computed", "scores_available": True,
            "thresholds": THRESHOLDS3, "source_mode": "application_extraction", "mapping_contract": "product-context-scope-v11",
            "factor_answers": answers, "target_app": rows, "applications": [{"index": i, "name": a["name"]} for i, a in enumerate(apps)],
            "entity": {k: {"probability": v, "application_scores": scores.get(k, [])} for k, v in (entity or {}).items()}}


APPS = [
    # mapped in two contexts (union): EXAM node via context 0, COLL node via context 1
    {"name": "카카오톡", "identity": {"KakaoTalk": 0.95}, "contexts": [{"EXAM-04-01": 0.9}, {"COLL-02-04": 0.6}],
     "scopes": [{"EXAM-04-01-02": 0.8}, {"COLL-02-04-01": 0.44}]},
    {"name": "Foo Tool", "identity": {}, "contexts": [{"EXAM-04-01": 0.9}], "scopes": [{"EXAM-04-01-07": 0.9}]},              # not in the catalog
    {"name": "Dropbox", "identity": {"Dropbox": 0.97}, "contexts": [{"EXAM-04-01": 0.3}], "scopes": [{"EXAM-04-01-07": 0.3}]},    # context
    {"name": "Dropbox, Pro", "identity": {"Dropbox": 0.96}, "contexts": [{"EXAM-04-01": 0.8}], "scopes": [{"EXAM-04-01-07": 0.3}]},  # scope
    {"name": "KakaoTalk PC", "identity": {"KakaoTalk": 0.97}, "contexts": [{"EXAM-04-01": 0.42}], "scopes": [{"EXAM-04-01-02": 0.65}]},  # near miss
]


def _write_run(path: Path, *, split: bool = True) -> None:
    start = {"event": "score_run_started", "run_id": "r1", "time": "2026-10-12T08:03:05+00:00", "selected": 2, "level5_catalog": CATALOG,
             "taxonomy_policy_version": "5.2", "level5_policy_version": "4.11", "application_entity_policy_version": "1.24",
             "candidate_names": {"EXAM-04-01": "Application Artifact Examination", "COLL-01-03": "Logical Data Acquisition", "OS-WIN": "Windows"}}
    records = [
        start,
        _stage1("ART1", {"EXAM-04-01": 0.96, "COLL-01-03": 0.33, "REPT-01-03": 0.32}, {"OS-WIN": 0.36, "DEV-PC": 0.35}),
        _stage3("ART1", APPS, {"APP-MES": 0.44, "APP-CST": 0.43, "CLOUD-SaaS:MES": 0.32}, {
            "APP-MES": [{"application_index": 0, "probability": 0.44}, {"application_index": 4, "probability": 0.2}],
            "CLOUD-SaaS:MES": [{"application_index": 3, "probability": 0.32}, {"application_index": 1, "probability": 0.32}]}),
        _stage1("ART2", {"EXAM-04-01": 0.31, "COLL-01-03": 0.22}, {"OS-WIN": 0.1}),
        _stage3("ART2", []),
        {"event": "score_run_finished", "time": "2026-10-12T08:19:33+00:00", "status": "completed", "succeeded": 2, "failed": 0},
    ]
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    if split:   # the defect of the real file: a stray newline inside a key
        at = lines[1].index('"level5"') + 4
        lines[1:2] = [lines[1][:at], lines[1][at:]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# The later run's catalog names the Dropbox node differently, so a check that used the first run's
# catalog for its papers would fail.
CATALOG2 = [*CATALOG[:2], {"id": "EXAM-04-01-07-05", "name": "Dropbox Business"}]
DROPBOX = {"name": "드롭박스", "identity": {"Dropbox Business": 0.97}, "contexts": [{"EXAM-04-01": 0.9}], "scopes": [{"EXAM-04-01-07": 0.8}]}


def _write_second_run(path: Path, *, run_id: str = "r2") -> None:
    """A later run (taxonomy policy 6, jev-1.14.0): scores ART2 again and the newly selected ART3."""

    start = {"event": "score_run_started", "run_id": run_id, "time": "2026-10-25T16:31:09+00:00", "selected": 2, "level5_catalog": CATALOG2,
             "taxonomy_policy_version": "6", "level5_policy_version": "4.11", "application_entity_policy_version": "1.24",
             "candidate_names": {"EXAM-04-01": "Application Artifact Examination", "COLL-01-03": "Logical Data Acquisition", "OS-WIN": "Windows"}}
    records = [
        start,
        _stage1("ART2", {"EXAM-04-01": 0.5, "COLL-01-03": 0.22}, {"OS-WIN": 0.4}, model="jev-1.14.0"),
        _stage3("ART2", [], catalog=CATALOG2),
        _stage1("ART3", {"COLL-01-03": 0.7, "EXAM-04-01": 0.1}, {"DEV-MOB": 0.5}, model="jev-1.14.0"),
        _stage3("ART3", [DROPBOX], catalog=CATALOG2),
        {"event": "score_run_finished", "time": "2026-10-25T16:32:52+00:00", "status": "completed", "succeeded": 2, "failed": 0},
    ]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")


# The latest results DB: ART1 from the first run, ART2 and ART3 from the second.
FINAL = {
    "ART1": {"taxonomy": ["COLL-01-03", "EXAM-04-01"], "entity": ["APP-MES", "CLOUD-SaaS:MES", "OS-WIN"],
             "target_app": [{"application_name": "KakaoTalk", "id": ["COLL-02-04-01-01", "EXAM-04-01-02-01"]}]},
    "ART2": {"taxonomy": ["EXAM-04-01"], "entity": ["OS-WIN"], "target_app": []},
    "ART3": {"taxonomy": ["COLL-01-03"], "entity": ["DEV-MOB"], "target_app": [{"application_name": "Dropbox Business", "id": ["EXAM-04-01-07-05"]}]},
}


def _write_db(path: Path, final: dict, extractions: dict[str, tuple[str, str]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute("create table paper_results (paper_id text primary key, taxonomy text, entity text, target_app text)")
    connection.execute("create table llm_extractions (paper_id text primary key, prompt_version text, model text, applications_json text not null)")
    for paper_id, value in final.items():
        connection.execute("insert into paper_results values (?, ?, ?, ?)",
                           (paper_id, json.dumps(value["taxonomy"]), json.dumps(value["entity"]), json.dumps(value["target_app"])))
    for paper_id, (model, prompt) in extractions.items():
        connection.execute("insert into llm_extractions values (?, ?, ?, ?)", (paper_id, prompt, model, "[]"))
    connection.commit()
    connection.close()


def _papers(numbered_art3: bool = False) -> dict[str, jev.PaperInfo]:
    """ART3 was selected after the last renumbering: no KNOW-DOMP number until the next one."""

    return {"ART1": jev.PaperInfo("ART1", "KNOW-DOMP-0000001", 1, "논문 하나", "학술지"),
            "ART2": jev.PaperInfo("ART2", "KNOW-DOMP-0000002", 2, "논문 둘", "학술지"),
            "ART3": jev.PaperInfo("ART3", "KNOW-DOMP-0000003" if numbered_art3 else "", 3 if numbered_art3 else None, "논문 셋", "학술지")}


def _rows(path: Path, sheet: str) -> list[tuple]:
    return list(load_workbook(path)[sheet].iter_rows(values_only=True))


def _summary(path: Path) -> dict[str, str]:
    """The 요약 sheet's 'item | value' rows (first occurrence of an item) and the section titles (value '')."""

    found: dict[str, str] = {}
    for row in _rows(path, "요약"):
        if row and row[0] is not None:
            found.setdefault(str(row[0]), "" if len(row) < 2 or row[1] is None else str(row[1]))
    return found


def test_a_record_broken_over_two_lines_is_joined_back(tmp_path: Path) -> None:
    path = tmp_path / "scores.jsonl"
    _write_run(path)
    run = jev.load_run(path)
    assert run.repairs == [(2, 3)] and set(run.stage1) == {"ART1", "ART2"} and run.stage1["ART1"]["thresholds"]["level5"] == 0.44
    path.write_text('{"event": "score_run_started"\n{broken\n', encoding="utf-8")
    with pytest.raises(jev.JevAIError, match="unreadable"):
        jev.load_run(path)


def test_thresholds_are_inclusive_for_level3_and_entities(tmp_path: Path) -> None:
    path = tmp_path / "scores.jsonl"
    _write_run(path, split=False)
    run = jev.load_run(path)
    assert jev.lv3_of(run.stage1["ART1"]) == [("EXAM-04-01", 0.96), ("COLL-01-03", 0.33)]        # 0.33 in, 0.32 out
    assert jev.lv3_of(run.stage1["ART2"]) == []
    assert jev.entities_of(run.stage1["ART1"], run.stage3["ART1"]) == [("APP-MES", 0.44), ("OS-WIN", 0.36), ("CLOUD-SaaS:MES", 0.32)]


def test_level5_mapping_unions_contexts_and_every_failure_gets_its_reason(tmp_path: Path) -> None:
    path = tmp_path / "scores.jsonl"
    _write_run(path, split=False)
    run = jev.load_run(path)
    apps = jev.applications_of(run.stage3["ART1"], run.catalog)
    assert apps[0].lv5 == {"COLL-02-04-01-01": 0.44, "EXAM-04-01-02-01": 0.8}                   # union over contexts, 0.44 inclusive
    assert jev.db_target_app(apps, run.catalog) == [{"application_name": "KakaoTalk", "id": ["COLL-02-04-01-01", "EXAM-04-01-02-01"]}]
    names = jev.Names({"EXAM-04-01": "애플리케이션 아티팩트 조사", "COLL-02-04": "클라우드 데이터 수집"},
                      {"EXAM-04-01-02": "메신저", "EXAM-04-01-07": "클라우드 스토리지", "COLL-02-04-01": "SaaS: 메신저"})
    note = jev.RegistryNote("Foo Tool", 3, None)
    jev.explain(apps[1], 0.44, names, run.catalog, note)
    assert apps[1].category == "시스템 등록 X" and "DF-Insight에는 'Foo Tool'" in apps[1].reason and "모두 동일 제품 확률 0.02 이하" in apps[1].reason
    jev.explain(apps[2], 0.44, names, run.catalog, None)
    assert apps[2].category == "시스템에 등록은 되어있지만" and apps[2].subcategory == "Level-3 맥락 불일치"
    assert apps[2].reason.startswith("시스템에 등록은 되어있지만('Dropbox', 동일 제품 확률 0.97) 논문에서 이 앱을 다룬 맥락이 등록 노드의 Level-3(애플리케이션 아티팩트 조사 0.30)")
    jev.explain(apps[3], 0.44, names, run.catalog, None)
    assert apps[3].subcategory == "Level-4 범위 불일치" and "Level-4 범위(클라우드 스토리지 0.30)" in apps[3].reason
    jev.explain(apps[4], 0.44, names, run.catalog, None)
    assert apps[4].category == "Threshold 만족X" and "최고 확률 0.42 < 기준 0.44" in apps[4].reason


def test_the_six_workbooks(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "scores.jsonl"
    _write_run(path)
    papers = {"ART1": jev.PaperInfo("ART1", "KNOW-DOMP-0000001", 1, "논문 하나", "학술지"), "ART2": jev.PaperInfo("ART2", "KNOW-DOMP-0000002", 2, "논문 둘", "학술지")}
    monkeypatch.setattr(jev, "corpus_info", lambda: papers)
    monkeypatch.setattr(jev, "load_names", lambda catalog: jev.Names({}, {}))
    report = jev.build_report(path, None, use_registry=False)
    written = jev.write_all(report, tmp_path / "out")
    assert [p.name for p in written] == list(jev.FILES.values())

    def sheet(name: str, title: str) -> list[tuple]:
        book = load_workbook(tmp_path / "out" / name)
        assert title in book.sheetnames
        rows = list(book[title].iter_rows(values_only=True))
        return rows

    lv3 = sheet("JevAI-LV3.xlsx", "논문별 결과")
    assert lv3 == [("title", "journal_name", "LV3", "Probability"), ("논문 하나", "학술지", "EXAM-04-01", 0.96),
                   ("논문 하나", "학술지", "COLL-01-03", 0.33), ("논문 둘", "학술지", None, None)]
    assert load_workbook(tmp_path / "out" / "JevAI-LV3.xlsx").sheetnames == ["요약", "논문별 결과"]
    fail = sheet("JevAI-LV3-Fail.xlsx", "매핑실패 논문")
    assert fail[0] == ("title", "journal_name", "fail_reason") and fail[1][2].startswith("2개 LV3 후보 모두 확률이 기준값(threshold) 0.33 미만 — 최고 0.31(부족분 0.02)")
    mapped = sheet("JevAI-application-LV5.xlsx", "Application 매핑")
    assert mapped == [("논문 ID", "Target Application", "LV5"), ("KNOW-DOMP-0000001", "카카오톡", "COLL-02-04-01-01, EXAM-04-01-02-01")]
    empty = sheet("JevAI-application_empty-LV5.xlsx", "매핑실패 Application")
    assert empty[0] == ("논문ID", "Target Application", "매핑실패사유") and [row[1] for row in empty[1:]] == ["Foo Tool", "Dropbox", "Dropbox, Pro", "KakaoTalk PC"]
    listed = sheet("JevAI-application-list.xlsx", "Target Application")
    assert listed[0] == ("논문ID", "Target Application", "매핑 여부 (O/X)") and [row[2] for row in listed[1:]] == ["O", "X", "X", "X", "X"]
    entity = sheet("JevAI-Entity-list.xlsx", "Entity")
    # An application tag carries the applications it came from; a paper-level tag (OS-*) does not.
    assert entity == [("논문ID", "Entity", "Probability"), ("KNOW-DOMP-0000001", "APP-MES (카카오톡)", 0.44), ("KNOW-DOMP-0000001", "OS-WIN", 0.36),
                      ("KNOW-DOMP-0000001", "CLOUD-SaaS:MES (\"Dropbox, Pro\", Foo Tool)", 0.32)]      # equal scores: by name; a comma name is quoted
    summary = [row[0] for row in load_workbook(tmp_path / "out" / "JevAI-LV3.xlsx")["요약"].iter_rows(values_only=True) if row[0]]
    assert "데이터 복구" in summary and "검증" in summary


def test_the_check_against_the_results_db_finds_a_different_decision(tmp_path: Path) -> None:
    path = tmp_path / "scores.jsonl"
    _write_run(path, split=False)
    run = jev.load_run(path)
    apps = {paper_id: jev.applications_of(record, run.catalog) for paper_id, record in run.stage3.items()}
    final = {"ART1": {"taxonomy": ["COLL-01-03", "EXAM-04-01"], "entity": ["APP-MES", "CLOUD-SaaS:MES", "OS-WIN"],
                      "target_app": [{"application_name": "KakaoTalk", "id": ["COLL-02-04-01-01", "EXAM-04-01-02-01"]}]},
             "ART2": {"taxonomy": [], "entity": [], "target_app": []}}
    assert jev.check_against_db(run, apps, final).ok
    final["ART2"]["taxonomy"] = ["EXAM-04-01"]
    check = jev.check_against_db(run, apps, final)
    assert not check.ok and check.mismatches == ["ART2: taxonomy"] and check.taxonomy == 1


# --------------------------------------------------------------------------- #
# Several runs (2026-10-12: the selected papers; 2026-10-25: the papers selected since)
# --------------------------------------------------------------------------- #

def test_runs_are_merged_per_paper_and_the_latest_run_wins(tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _write_run(first)
    _write_second_run(second)
    merged = jev.load_runs([second, first])            # the start time orders the runs, not the argument order
    assert [run.run_id for run in merged.runs] == ["r1", "r2"] and sorted(merged.stage1) == ["ART1", "ART2", "ART3"]
    assert merged.stage1["ART2"]["model"] == "jev-1.14.0" and merged.run_of["ART2"] is merged.runs[1]
    assert list(merged.superseded) == ["ART2"] and [run.run_id for run in merged.superseded["ART2"]] == ["r1"]
    assert merged.papers_of(merged.runs[0]) == ["ART1"] and merged.papers_of(merged.runs[1]) == ["ART2", "ART3"]
    assert merged.catalog_of("ART1")["EXAM-04-01-07-05"] == "Dropbox" and merged.catalog_of("ART3")["EXAM-04-01-07-05"] == "Dropbox Business"
    _write_second_run(tmp_path / "copy.jsonl")          # the same run under another file name
    with pytest.raises(jev.JevAIError, match="same run"):
        jev.load_runs([first, second, tmp_path / "copy.jsonl"])


def test_the_check_compares_every_merged_paper_with_its_own_runs_catalog(tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _write_run(first, split=False)
    _write_second_run(second)
    merged = jev.load_runs([first, second])
    apps = {paper_id: jev.applications_of(record, merged.catalog_of(paper_id)) for paper_id, record in merged.stage3.items()}
    check = jev.check_against_db(merged, apps, {**FINAL, "ART9": FINAL["ART2"]})
    assert check.ok and (check.papers, check.taxonomy, check.entity, check.target_app) == (3, 3, 3, 3)
    assert check.db_papers == 4 and check.db_only == ["ART9"]
    # The first run alone disagrees with the final result of ART2, which the second run scored again.
    single = jev.load_run(first)
    single_apps = {paper_id: jev.applications_of(record, single.catalog) for paper_id, record in single.stage3.items()}
    assert jev.check_against_db(single, single_apps, FINAL).mismatches == ["ART2: taxonomy", "ART2: entity"]


def test_the_merged_workbooks_describe_both_runs_and_use_the_art_id_until_a_paper_is_numbered(tmp_path: Path, monkeypatch) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _write_run(first)
    _write_second_run(second)
    monkeypatch.setattr(jev, "corpus_info", lambda: _papers())
    monkeypatch.setattr(jev, "load_names", lambda catalog: jev.Names({}, {}))
    report = jev.build_report([second, first], None, use_registry=False)
    assert report.unnumbered() == ["ART3"] and report.ordered() == ["ART1", "ART2", "ART3"]
    out = tmp_path / "out"
    assert [path.name for path in jev.write_all(report, out)] == list(jev.FILES.values())
    assert _rows(out / "JevAI-LV3.xlsx", "논문별 결과")[1:] == [
        ("논문 하나", "학술지", "EXAM-04-01", 0.96), ("논문 하나", "학술지", "COLL-01-03", 0.33),
        ("논문 둘", "학술지", "EXAM-04-01", 0.5),           # the second run's record of ART2
        ("논문 셋", "학술지", "COLL-01-03", 0.7)]
    assert _rows(out / "JevAI-LV3-Fail.xlsx", "매핑실패 논문")[1:] == []
    assert _rows(out / "JevAI-application-LV5.xlsx", "Application 매핑")[1:] == [
        ("KNOW-DOMP-0000001", "카카오톡", "COLL-02-04-01-01, EXAM-04-01-02-01"), ("ART3", "드롭박스", "EXAM-04-01-07-05")]
    assert [row[0] for row in _rows(out / "JevAI-application-list.xlsx", "Target Application")[1:]] == ["KNOW-DOMP-0000001"] * 5 + ["ART3"]
    assert _rows(out / "JevAI-Entity-list.xlsx", "Entity")[-2:] == [("KNOW-DOMP-0000002", "OS-WIN", 0.4), ("ART3", "DEV-MOB", 0.5)]
    summary = _summary(out / "JevAI-LV3.xlsx")
    assert summary["결과 구성"] == ("Jev 실행 기록 2개를 논문별로 합침: 3편 = 실행 1 1편 + 실행 2 2편 · "
                                "두 실행에 모두 있는 논문 1편은 가장 나중 실행의 결과를 씀")
    differ, same = summary["실행 간 차이"].split(" / 같음 — ")
    assert "분류체계 정책 5.2 → 6" in differ and "Jev 모델 jev-1.13.0 → jev-1.14.0" in differ and "Level-5 카탈로그" in differ
    assert "Level-5 정책 4.11" in same and "기준값(LV3 0.33, 엔티티 0.36, APP-* 0.44, CLOUD-SaaS:* 0.32, Level-5 0.44)" in same
    assert "실행 1 — 2026-10-12 (2편)" in summary and "실행 2 — 2026-10-26 (2편)" in summary
    assert summary["논문 수"].endswith("실행 결과 성공 2 · 실패 0 · 그중 1편은 나중 실행이 다시 채점해 그 결과를 씀")      # the first run's row
    assert summary["LV3가 있는 논문"] == "3편 / 3편 (실행 1 1/1편 · 실행 2 2/2편)"
    assert summary["논문 순서"].endswith("KNOW-DOMP 번호가 아직 없는 논문 1편은 그 뒤에 KCI 논문 ID(ART…) 순")
    assert "KNOW-DOMP 번호가 아직 없는 논문 1편은 KCI 논문 ID(ART…)를 그대로 적음" in _summary(out / "JevAI-application-LV5.xlsx")["논문 ID"]
    # Once ART3 has its number (after renumbering) the same call writes it, without the note.
    monkeypatch.setattr(jev, "corpus_info", lambda: _papers(numbered_art3=True))
    jev.write_all(jev.build_report([first, second], None, use_registry=False), out)
    assert _rows(out / "JevAI-application-LV5.xlsx", "Application 매핑")[-1] == ("KNOW-DOMP-0000003", "드롭박스", "EXAM-04-01-07-05")
    assert _summary(out / "JevAI-application-LV5.xlsx")["논문 ID"] == "프로젝트 논문 번호(KNOW-DOMP). JevAI의 paper_id(ART…)와 1:1로 대응합니다."
    assert _summary(out / "JevAI-LV3.xlsx")["논문 순서"] == "프로젝트 논문 번호(KNOW-DOMP) 순"


def test_main_merges_every_run_of_the_folder_and_checks_the_latest_db(tmp_path: Path, monkeypatch, capsys) -> None:
    folder = tmp_path / "JevAI"
    folder.mkdir()
    _write_run(folder / "mapping_scores_20261012T080305Z_r1.jsonl")
    _write_second_run(folder / "mapping_scores_20261025T163109Z_r2.jsonl")
    _write_db(folder / "results_kci-(1026-0132).db", FINAL, {"ART1": ("gpt-6.1-sol", "2.23"), "ART3": ("gpt-6-luna", "2.24")})
    monkeypatch.setattr(jev, "JEVAI_DIR", folder)
    monkeypatch.setattr(jev, "corpus_info", lambda: _papers())
    monkeypatch.setattr(jev, "load_names", lambda catalog: jev.Names({}, {}))
    out = tmp_path / "out"
    assert jev.main(["--no-registry", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "논문 3편" in printed and "DB 검증(results_kci-(1026-0132).db) 3/3/3 of 3" in printed and "KNOW-DOMP 번호가 아직 없는 논문 1편" in printed
    summary = _summary(out / "JevAI-application-LV5.xlsx")
    assert summary["검증"] == ("results_kci-(1026-0132).db(3편)의 최종 결과와 비교(합친 결과 기준): "
                             "Level-3 3/3편, 엔티티 3/3편, 애플리케이션 Level-5 3/3편 일치")
    assert summary["선별 논문 대비"] == "프로젝트 선별 논문 3편 모두 Jev 결과 있음"
    assert "앱 추출 LLM gpt-6.1-sol → gpt-6-luna" in summary["실행 간 차이"]
    assert summary["모델 · 정책 버전"].endswith("앱 추출 LLM gpt-6.1-sol(프롬프트 2.23, 1편)")
    # One run alone, as before: the DB's other paper is not compared.
    single = tmp_path / "single"
    assert jev.main(["--jsonl", str(folder / "mapping_scores_20261025T163109Z_r2.jsonl"), "--no-registry", "--out", str(single)]) == 0
    summary = _summary(single / "JevAI-LV3.xlsx")
    assert summary["원본 파일"] == "mapping_scores_20261025T163109Z_r2.jsonl" and "결과 구성" not in summary
    assert summary["검증"].endswith("Level-5 2/2편 일치 · DB의 나머지 1편은 이 실행에 없어 비교하지 않음")
    assert summary["선별 논문 대비"] == "프로젝트 선별 논문 3편 중 1편은 이 실행에 없어 빠짐: ART1"
    # A DB without one of the merged papers stops before anything is written.
    _write_db(folder / "older.db", {key: value for key, value in FINAL.items() if key != "ART3"}, {})
    assert jev.main(["--db", str(folder / "older.db"), "--no-registry", "--out", str(tmp_path / "none")]) == 1
    assert "ART3: not in the results DB" in capsys.readouterr().err and not (tmp_path / "none").exists()


def test_an_application_name_broken_by_a_pdf_font_glyph_is_fixed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "scores.jsonl"
    start = {"event": "score_run_started", "run_id": "r1", "time": "2026-10-12T08:03:05+00:00", "level5_catalog": CATALOG,
             "taxonomy_policy_version": "5.2", "candidate_names": {}}
    broken = {"name": " 글", "identity": {}, "contexts": [{"EXAM-04-01": 0.9}], "scopes": [{"EXAM-04-01-07": 0.9}]}
    records = [start, _stage1("ART1", {"EXAM-04-01": 0.9}, {}),
               _stage3("ART1", [broken], {"APP-DOC": 0.5}, {"APP-DOC": [{"application_index": 0, "probability": 0.5}]})]
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    monkeypatch.setattr(jev, "corpus_info", lambda: {"ART1": jev.PaperInfo("ART1", "KNOW-DOMP-0000300", 300, "논문", "학술지")})
    monkeypatch.setattr(jev, "load_names", lambda catalog: jev.Names({}, {}))
    out = tmp_path / "out"
    jev.write_all(jev.build_report(path, None, use_registry=False), out)
    assert _rows(out / "JevAI-application-list.xlsx", "Target Application")[1:] == [("KNOW-DOMP-0000300", "한글", "X")]
    assert _rows(out / "JevAI-Entity-list.xlsx", "Entity")[1:] == [("KNOW-DOMP-0000300", "APP-DOC (한글)", 0.5)]
    assert ("KNOW-DOMP-0000300의 앱 이름 '[U+F53A] 글'은 PDF 글꼴의 사용자 정의 문자 때문에 깨진 것이라 '한글'로 표기."
            in _summary(out / "JevAI-application-LV5.xlsx")["Target Application"])


def _df_insight(root: Path) -> dict[str, Path]:
    """DF-Insight in its 2026-10 form (2-digit Level-5 ids) and a reviewed alias table that still names dropped products."""

    techniques = root / "data" / "techniques"
    techniques.mkdir(parents=True)
    nodes = {
        "COLL-02-04": (3, "클라우드 데이터 수집", "Cloud Data Collection"), "COLL-02-04-01": (4, "SaaS: 메신저", "SaaS: Messenger"),
        "COLL-02-04-01-01": (5, "KakaoTalk", "KakaoTalk"),
        "EXAM-04-01": (3, "애플리케이션 아티팩트 조사", "Application Artifact Examination"), "EXAM-04-01-02": (4, "메신저", "Messenger"),
        "EXAM-04-01-02-01": (5, "KakaoTalk", "KakaoTalk"), "EXAM-04-01-07": (4, "클라우드 스토리지", "Cloud Storage"),
        "EXAM-04-01-07-05": (5, "Dropbox", "Dropbox"),
    }
    for identifier, (level, ko, en) in nodes.items():
        record = {"id": identifier, "level": level, "title": {"ko": ko, "en": en}}
        if level == 5:
            record["parent_id"] = identifier.rsplit("-", 1)[0]
        (techniques / f"{identifier}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    (root / "data" / "keyword-aliases.json").write_text(json.dumps({"applications": {"kakao talk pc": "COLL-02-04-01-01"}}), encoding="utf-8")
    aliases = root / "application_aliases.json"
    aliases.write_text(json.dumps({"product_groups": [["Google Chrome", "Chrome"], ["Dropbox", "Dropbox Business"]],
                                   "aliases": {"카카오톡": "KakaoTalk", "구글 크롬": "Google Chrome", "드롭박스 비즈니스": "Dropbox Business"}},
                                  ensure_ascii=False), encoding="utf-8")
    return {"technique_dir": techniques, "df_aliases": root / "data" / "keyword-aliases.json", "pipeline_aliases": aliases}


def test_df_insight_with_two_digit_ids_and_stale_aliases_is_still_read(tmp_path: Path, monkeypatch) -> None:
    strict = re.compile(r"[A-Z]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{3}")       # the registry module before DF-Insight's change
    monkeypatch.setattr(registry_module, "LEVEL5_RE", strict)
    paths = _df_insight(tmp_path / "df")
    with pytest.raises(registry_module.RegistryError):
        registry_module.load_registry(**paths)
    found = jev.load_df_insight(**paths)
    assert registry_module.LEVEL5_RE is strict                                   # the module is left as it was
    assert found.registry is not None and not found.error and len(found.adapted) == 2 and "2자리 ID도 받도록" in found.adapted[0]
    assert found.adapted[1] == ("검토 별칭표(application_aliases.json)의 별칭 3개 중 1개는 지금 DF-Insight에 없는 제품을 가리켜 빼고 "
                                "1개는 같은 제품 묶음의 남은 이름으로 읽음; 제품 묶음 2개 중 2개에서 DF-Insight에 없는 이름을 뺌")
    registry = found.registry
    assert registry.match(["드롭박스 비즈니스"]).product == "Dropbox" and registry.match(["구글 크롬"]) is None
    catalog = {item["id"]: item["name"] for item in CATALOG}
    assert jev.registry_note(["카카오톡"], registry, catalog) == jev.RegistryNote("KakaoTalk", 2, "KakaoTalk")
    # DF-Insight's earlier 3-digit ids lead to the same catalog product.
    old_style = types.SimpleNamespace(match=registry.match, products={"KakaoTalk": ("COLL-02-04-01-001",)})
    assert jev.registry_note(["카카오톡"], old_style, catalog) == jev.RegistryNote("KakaoTalk", 1, "KakaoTalk")
    assert jev.catalog_in_registry(catalog, registry) == "DF-Insight도 같은 형식이며 3개 노드 모두 DF-Insight에 같은 ID·같은 제품명으로 있음"
    # A module that already reads both forms, with a clean alias table: nothing to adapt.
    monkeypatch.setattr(registry_module, "LEVEL5_RE", jev.LEVEL5_ANY_RE)
    paths["pipeline_aliases"].write_text(json.dumps({"product_groups": [], "aliases": {"카카오톡": "KakaoTalk"}}, ensure_ascii=False), encoding="utf-8")
    assert jev.load_df_insight(**paths).adapted == []
    # Nothing readable: no notes, and the reason.
    empty = tmp_path / "empty"
    empty.mkdir()
    broken = jev.load_df_insight(technique_dir=empty)
    assert broken.registry is None and "no Level-5 application node" in broken.error
