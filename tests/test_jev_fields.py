"""Jev AI results -> the profiles' Level-3, tags and target applications (Analyzer/JevAI/jev_fields.py)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from Analyzer.JevAI import jev_fields
from Analyzer.JevAI.jev_fields import JevFieldsError

CATALOG = [
    {"id": "EXAM-04-01-02-01", "name": "KakaoTalk"},
    {"id": "COLL-02-04-01-01", "name": "KakaoTalk"},
    {"id": "EXAM-04-01-07-05", "name": "Dropbox"},
]
NODES = {"EXAM-04-01-02-01": ("카카오톡", "KakaoTalk"), "COLL-02-04-01-01": ("KakaoTalk", "KakaoTalk"), "EXAM-04-01-07-05": ("Dropbox", "Dropbox")}
T1 = {"taxonomy": 0.33, "entity": 0.36, "level5": 0.44}
T3 = {**T1, "app": 0.44, "saas": 0.32, "eligibility": 0.6}
A, B, C, D, E = "ART000000001", "ART000000002", "ART000000003", "ART000000009", "ART000000005"


def _stage1(paper_id: str, taxonomy: dict[str, float], entity: dict[str, float]) -> dict:
    return {"event": "mapping_scores", "paper_id": paper_id, "stage": 1, "status": "computed", "scores_available": True,
            "thresholds": T1, "model": "jev-test",
            "taxonomy": {key: {"probability": value, "substantive_relevance_method": "whole_document_direct"} for key, value in taxonomy.items()},
            "entity": {key: {"probability": value} for key, value in entity.items()}}


def _stage3(paper_id: str, apps: list[tuple[str, dict[str, float]]], entity: dict[str, float] | None = None,
            source_mode: str = "application_extraction") -> dict:
    """``apps``: (name, {catalog node: probability}); one research context per node, all three factors equal."""

    answers: dict[str, float] = {}
    rows = []
    for index, (_name, nodes) in enumerate(apps):
        for context, (node, probability) in enumerate(nodes.items()):
            ids = [f"identity_a{index}_{node}", f"context_a{index}_c{context}", f"scope_a{index}_c{context}"]
            answers.update({question: probability for question in ids})
            rows.append({"application_index": index, "context_index": context, "id": node, "probability": probability,
                         "factor_question_ids": ids})
    return {"event": "mapping_scores", "paper_id": paper_id, "stage": 3, "status": "computed", "scores_available": True,
            "thresholds": T3, "source_mode": source_mode, "factor_answers": answers, "target_app": rows,
            "applications": [{"index": index, "name": name} for index, (name, _nodes) in enumerate(apps)],
            "entity": {key: {"probability": value, "application_scores": []} for key, value in (entity or {}).items()}}


def _write_run(path: Path, run_id: str, time: str, records: list[dict]) -> None:
    start = {"event": "score_run_started", "run_id": run_id, "time": time, "level5_catalog": CATALOG,
             "taxonomy_policy_version": "5.2", "level5_policy_version": "4.11", "application_entity_policy_version": "1.24"}
    finish = {"event": "score_run_finished", "time": time, "succeeded": len(records) // 2, "failed": 0}
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in (start, *records, finish)) + "\n", encoding="utf-8")


def _write_db(path: Path, final: dict[str, dict], mtime: float) -> None:
    connection = sqlite3.connect(path)
    connection.execute("create table paper_results (paper_id text primary key, taxonomy text, entity text, target_app text)")
    connection.execute("create table llm_extractions (paper_id text primary key, applications_json text not null)")
    for paper_id, value in final.items():
        connection.execute("insert into paper_results values (?, ?, ?, ?)",
                           (paper_id, json.dumps(value["taxonomy"]), json.dumps(value["entity"]), json.dumps(value["target_app"])))
    connection.commit()
    connection.close()
    os.utime(path, (mtime, mtime))


# What Jev's latest results DB says (A and D from the first run, B rescored and C by the second run).
FINAL = {
    A: {"taxonomy": ["CMNF-01-02", "COLL-01-03", "EXAM-04-01"], "entity": ["APP-MES", "CLOUD-SaaS:MES", "OS-WIN"],
        "target_app": [{"application_name": "KakaoTalk", "id": ["COLL-02-04-01-01", "EXAM-04-01-02-01"]}]},
    B: {"taxonomy": ["EXAM-04-01"], "entity": ["OS-WIN"], "target_app": []},
    C: {"taxonomy": ["COLL-01-03"], "entity": [], "target_app": []},
    D: {"taxonomy": [], "entity": [], "target_app": []},
}


@pytest.fixture()
def site(tmp_path: Path) -> dict[str, Path]:
    jev = tmp_path / "JevAI"
    jev.mkdir()
    _write_run(jev / "mapping_scores_20261012T080305Z_r1.jsonl", "r1", "2026-10-12T08:03:05+00:00", [
        _stage1(A, {"EXAM-04-01": 0.96, "CMNF-01-02": 0.96, "COLL-01-03": 0.33, "REPT-01-03": 0.32}, {"OS-WIN": 0.36, "DEV-PC": 0.35}),
        _stage3(A, [
            ("카카오톡", {"EXAM-04-01-02-01": 0.9, "COLL-02-04-01-01": 0.5}),
            ("KakaoTalk PC", {"EXAM-04-01-02-01": 0.8}),          # its only node is listed already
            ("Foo Tool", {}),
            ("foo  tool", {}),                                    # the same name again
            ("Dropbox", {"EXAM-04-01-07-05": 0.3}),               # below 0.44: no Level-5 node
        ], {"APP-MES": 0.44, "APP-CST": 0.43, "CLOUD-SaaS:MES": 0.32}),
        _stage1(B, {"EXAM-04-01": 0.2}, {"OS-WIN": 0.1}),
        _stage3(B, []),
        _stage1(D, {"EXAM-04-01": 0.1}, {}),
        _stage3(D, []),
    ])
    _write_run(jev / "mapping_scores_20261025T163109Z_r2.jsonl", "r2", "2026-10-25T16:31:09+00:00", [
        _stage1(B, {"EXAM-04-01": 0.5}, {"OS-WIN": 0.4}),
        _stage3(B, []),
        _stage1(C, {"COLL-01-03": 0.7}, {}),
        _stage3(C, [], source_mode="metadata_only"),
    ])
    old = {key: value for key, value in FINAL.items() if key != C}
    old[B] = {"taxonomy": [], "entity": [], "target_app": []}                        # B as the first run decided it
    _write_db(jev / "results_kci-(1012-1951).db", old, 1_700_000_000)
    _write_db(jev / "results_kci-(1026-0132).db", FINAL, 1_800_000_000)
    techniques = tmp_path / "techniques"
    techniques.mkdir()
    for node, (title_ko, title_en) in NODES.items():
        (techniques / f"{node}.json").write_text(json.dumps({"id": node, "level": 5, "title": {"ko": title_ko, "en": title_en}}), encoding="utf-8")
    (techniques / "EXAM-04-01-02.json").write_text(json.dumps({"id": "EXAM-04-01-02", "level": 4, "title": {"ko": "메신저", "en": "Messenger"}}), encoding="utf-8")
    database = tmp_path / "kci_pipeline.db"
    connection = sqlite3.connect(database)
    connection.execute("create table kci_papers (source_id text)")
    connection.executemany("insert into kci_papers values (?)", [(A,), (B,), (C,), (E,)])
    connection.commit()
    connection.close()
    return {"jev": jev, "techniques": techniques, "database": database, "root": tmp_path}


def _export(site: dict[str, Path]) -> jev_fields.ExportResult:
    return jev_fields.export(directory=site["jev"], database=site["database"], technique_dir=site["techniques"],
                             levels_path=site["root"] / "levels.json", targets_path=site["root"] / "targets.json")


def test_profile_fields_follow_jev_with_the_latest_run_per_paper(site: dict[str, Path]) -> None:
    result = _export(site)
    levels = json.loads((site["root"] / "levels.json").read_text(encoding="utf-8"))
    targets = json.loads((site["root"] / "targets.json").read_text(encoding="utf-8"))

    # Every selected paper has a record; D was scored but is not selected (any more).
    assert set(levels["papers"]) == set(targets["papers"]) == {A, B, C, E}
    assert (result.paper_count, result.scored, result.pending, result.not_selected) == (4, 3, [E], [D])
    assert levels["results_db"] == {"file": "results_kci-(1026-0132).db", "papers": 4, "checked": 4}
    assert [(run["run_id"], run["papers"], run["papers_used"]) for run in levels["runs"]] == [("r1", 3, 2), ("r2", 2, 2)]

    # Level-3: probability >= 0.33 (inclusive), highest first, ties by id.
    assert levels["papers"][A]["lv3"] == ["CMNF-01-02", "EXAM-04-01", "COLL-01-03"]
    assert levels["papers"][A]["lv2"] == ["CMNF-01", "EXAM-04", "COLL-01"] and levels["papers"][A]["lv1"] == ["CMNF", "EXAM", "COLL"]
    assert levels["papers"][A]["status"] == "classified" and levels["papers"][A]["basis"] == "PDF" and levels["papers"][A]["run_id"] == "r1"
    # B was scored again by the later run: that result is used.
    assert levels["papers"][B]["lv3"] == ["EXAM-04-01"] and levels["papers"][B]["run_id"] == "r2"
    assert targets["papers"][B]["tags"] == ["OS-WIN"]
    # C was scored from its title and abstract only.
    assert levels["papers"][C]["basis"] == "Abstract" and levels["papers"][C]["basis_reason"] and targets["papers"][C]["basis"] == "Abstract"
    # A selected paper without a Jev result is pending, never "no Level".
    assert levels["papers"][E]["status"] == "pending" and targets["papers"][E]["status"] == "pending"

    # Tags: the accepted entities in the template's order (APP-CST 0.43 < 0.44 and DEV-PC 0.35 < 0.36 are out).
    assert targets["papers"][A]["tags"] == ["OS-WIN", "APP-MES", "CLOUD-SaaS:MES"]
    # Applications in Jev's order: one row per Level-5 id with the DF-Insight titles; an id or blank name already listed is not repeated.
    assert targets["papers"][A]["applications"] == [
        {"name": "카카오톡", "level5": [
            {"id": "COLL-02-04-01-01", "title_ko": "KakaoTalk", "title_en": "KakaoTalk", "probability": 0.5},
            {"id": "EXAM-04-01-02-01", "title_ko": "카카오톡", "title_en": "KakaoTalk", "probability": 0.9}]},
        {"name": "Foo Tool", "level5": []},
        {"name": "Dropbox", "level5": []},
    ]
    assert len(result.duplicates) == 2 and (result.level5_rows, result.blank_rows) == (2, 2)
    assert (result.with_level3, result.with_tags, result.with_applications, result.metadata_only) == (3, 2, 1, 1)


def test_the_profile_generator_reads_what_the_jev_stage_writes(site: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from Analyzer.MD import paper_md

    _export(site)
    monkeypatch.setattr(paper_md, "LEVEL5_REGISTRY", site["techniques"])
    taxonomy = paper_md.load_technique_taxonomy()
    levels, without_level = paper_md._load_levels([A, B, C], taxonomy, site["root"] / "levels.json")
    assert levels[A] == ("CMNF-01-02", "EXAM-04-01", "COLL-01-03") and without_level == 0
    targets = paper_md._load_targets([A, B, C], site["root"] / "targets.json")
    assert targets[A] == (
        ("OS-WIN", "APP-MES", "CLOUD-SaaS:MES"),
        (("COLL-02-04-01-01", "KakaoTalk", "KakaoTalk"), ("EXAM-04-01-02-01", "카카오톡", "KakaoTalk"),
         (None, "Foo Tool", "Foo Tool"), (None, "Dropbox", "Dropbox")),
    )
    assert targets[C] == ((), ())
    with pytest.raises(paper_md.PaperMarkdownError, match="not finished"):
        paper_md._load_levels([E], taxonomy, site["root"] / "levels.json")
    with pytest.raises(paper_md.PaperMarkdownError, match="not final"):
        paper_md._load_targets([E], site["root"] / "targets.json")


def test_nothing_is_written_when_the_decisions_differ_from_the_results_db(site: dict[str, Path]) -> None:
    final = json.loads(json.dumps(FINAL))
    final[A]["taxonomy"] = ["EXAM-04-01"]
    (site["jev"] / "results_kci-(1026-0132).db").unlink()
    _write_db(site["jev"] / "results_kci-(1026-0132).db", final, 1_800_000_000)
    with pytest.raises(JevFieldsError, match="differ from results_kci-\\(1026-0132\\).db"):
        _export(site)
    assert not (site["root"] / "levels.json").exists() and not (site["root"] / "targets.json").exists()

    # The older DB lacks C: checking against it would fail too (the latest DB is the one that counts).
    with pytest.raises(JevFieldsError, match="not in the results DB"):
        jev_fields.build(directory=site["jev"], database=site["database"], technique_dir=site["techniques"],
                         results_db=site["jev"] / "results_kci-(1012-1951).db")


def test_a_level5_id_that_is_not_a_df_insight_node_stops_the_export(site: dict[str, Path]) -> None:
    (site["techniques"] / "COLL-02-04-01-01.json").unlink()
    with pytest.raises(JevFieldsError, match="COLL-02-04-01-01 is not a DF-Insight Level-5 node"):
        _export(site)
    assert not (site["root"] / "levels.json").exists()


def test_missing_inputs_are_reported(site: dict[str, Path], tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(JevFieldsError, match="no Jev AI run log"):
        jev_fields.build(directory=empty, database=site["database"], technique_dir=site["techniques"])
    for path in site["jev"].glob("results_kci*.db"):
        path.unlink()
    with pytest.raises(JevFieldsError, match="no Jev AI results DB"):
        jev_fields.build(directory=site["jev"], database=site["database"], technique_dir=site["techniques"])
