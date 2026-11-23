#!/usr/bin/env python3
"""Jev AI results -> what the paper profiles show: Level-3, tags and target applications.

The paper classification follows chapter III ('다) 논문 분류'): Jev AI (an external tool, run by
the researcher who owns it) scores the selected papers and delivers into ``Analyzer/JevAI``

    mapping_scores_<time>_<run>.jsonl   the log of one scoring run (stage 1 and stage 3 records)
    results_kci-(MMDD-HHMM).db          the final results (taxonomy, entity, target_app per paper)

This module turns them into the two inputs of ``Analyzer/MD/paper_md.py`` (the ``jev`` stage of
the pipeline), in the shape the earlier Level and Level-5 stages wrote theirs
(``Analyzer/LV/levels.json`` and ``Analyzer/Application/targets.json``, which are left as they are):

    Analyzer/JevAI/levels.json    per paper: the Level-3 ids, highest Jev probability first
    Analyzer/JevAI/targets.json   per paper: the tags and the target applications with their
                                  Level-5 ids and DF-Insight node titles

Decisions are Jev's own, made by ``jevai_report`` (the module that also writes the six JevAI
workbooks, so profiles and workbooks always agree):

    Level-3   probability >= the run's taxonomy threshold (0.33), probability descending, ties by id
    tags      entities: stage 1 (DEV/FS/OS/CLOUD-IaaS/DaaS) >= 0.36, APP-* >= 0.44,
              CLOUD-SaaS:* >= 0.32; printed in the template's tag order
    Level-5   a catalog node is mapped to an application when min(identity, context, scope)
              reaches 0.44 in one of its contexts

Every run log of the folder is read and the runs are merged per paper (a paper scored by more
than one run keeps the latest run's result, ``jevai_report.merge_runs``); the merged decisions
must reproduce the latest results DB for every paper, otherwise nothing is written.

Section-4 rows (``4. 대상 애플리케이션``), applications in Jev's order: a mapped application gives one
row per Level-5 id (ids in Jev's sorted order) named by the DF-Insight node title (``title.ko`` in
ko.md, ``title.en`` in en.md); an application without a Level-5 node gives one blank-id row with
the name Jev extracted; an id already listed for an earlier application of the paper, or a blank
name already listed, is not repeated (the profile generator refuses duplicates).  A selected
paper without a Jev result is written as ``pending`` (no profile claims "no Level" for it).

    python -m Analyzer.JevAI.jev_fields            # write levels.json and targets.json
    python -m Analyzer.JevAI.jev_fields --check    # only check (decisions vs DB, vocabularies)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from Analyzer.JevAI import jevai_report as jr  # noqa: E402
from PaperPipeline.storage import write_json_atomic  # noqa: E402

JEVAI_DIR = REPOSITORY_ROOT / "Analyzer" / "JevAI"
LEVELS_JSON = JEVAI_DIR / "levels.json"
TARGETS_JSON = JEVAI_DIR / "targets.json"
TECHNIQUE_DIR = REPOSITORY_ROOT / "web" / "DF-Insight" / "data" / "techniques"
DATABASE = REPOSITORY_ROOT / "kci_pipeline.db"
SCHEMA_VERSION = 1
# DF-Insight writes the last segment with 2 digits since 2026-10 (3 digits before); an id is only
# accepted when it is a current DF-Insight Level-5 node.
LEVEL5_ID_RE = re.compile(r"[A-Z]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2,3}\Z")
METADATA_ONLY_REASON = "원문 없이 제목·초록만으로 판정(Jev AI)"
NO_RESULT_REASON = "Jev AI 결과 없음"
EMPTY_LEVEL_NOTE = "Jev AI 확률이 기준값(0.33) 이상인 Level-3가 없어 비워 둠"


class JevFieldsError(RuntimeError):
    pass


@dataclass
class ExportResult:
    levels: Path | None
    targets: Path | None
    paper_count: int
    scored: int
    pending: list[str]
    not_selected: list[str]
    with_level3: int
    with_tags: int
    with_applications: int
    level5_rows: int
    blank_rows: int
    metadata_only: int
    runs: list[dict[str, Any]]
    database: str
    checked: int
    duplicates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "levels": str(self.levels) if self.levels else None,
            "targets": str(self.targets) if self.targets else None,
            "paper_count": self.paper_count, "scored_count": self.scored,
            "pending_source_ids": list(self.pending), "not_selected_source_ids": list(self.not_selected),
            "papers_with_level3": self.with_level3, "papers_with_tags": self.with_tags,
            "papers_with_applications": self.with_applications, "level5_rows": self.level5_rows,
            "blank_rows": self.blank_rows, "metadata_only_count": self.metadata_only,
            "runs": self.runs, "results_db": self.database, "db_checked": self.checked,
            "duplicates_not_repeated": list(self.duplicates),
        }


def target_key(value: str) -> str:
    """``paper_md._target_key``: the blank-id duplicate check of the profile generator."""

    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip()).casefold()


def level5_titles(directory: Path = TECHNIQUE_DIR) -> dict[str, tuple[str, str]]:
    """DF-Insight's Level-5 nodes, ``id -> (title_ko, title_en)``.

    The nodes ``paper_md`` accepts (``data/techniques/<id>.json`` with ``level: 5``), titles read
    as ``Analyzer/Application/registry.py`` reads them (whitespace normalised, ko falls back to en).
    """

    if not directory.is_dir():
        raise JevFieldsError(f"DF-Insight technique registry is missing: {directory}")
    nodes: dict[str, tuple[str, str]] = {}
    for path in sorted(directory.glob("*.json")):
        if not LEVEL5_ID_RE.fullmatch(path.stem):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JevFieldsError(f"unable to read {path}: {exc}") from exc
        if not isinstance(record, dict) or record.get("level") != 5 or record.get("id") != path.stem:
            continue
        title = record.get("title") or {}
        title_en = " ".join(str(title.get("en") or "").split())
        title_ko = " ".join(str(title.get("ko") or title_en).split())
        if not title_en:
            raise JevFieldsError(f"{path.name}: the Level-5 node has no title")
        nodes[path.stem] = (title_ko, title_en)
    if not nodes:
        raise JevFieldsError(f"no Level-5 node in {directory}")
    return nodes


def selected_source_ids(database: Path = DATABASE) -> list[str]:
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    try:
        return sorted(str(row[0]) for row in connection.execute("select source_id from kci_papers"))
    except sqlite3.Error as exc:
        raise JevFieldsError(f"unable to read the selected papers from {database}: {exc}") from exc
    finally:
        connection.close()


def latest_database(directory: Path = JEVAI_DIR) -> Path | None:
    """The results DB ``jevai_report`` checks against: the most recent ``results_kci*.db``."""

    return max(directory.glob(jr.DB_PATTERN), key=lambda path: path.stat().st_mtime, default=None)


def _run_row(run: jr.Run, merged: jr.Merged) -> dict[str, Any]:
    first = next(iter(run.stage1.values()))
    return {
        "file": run.path.name, "run_id": run.run_id,
        "started": run.start.get("time"), "finished": (run.finish or {}).get("time"),
        "papers": len(run.stage1), "papers_used": len(merged.papers_of(run)),
        "model": first.get("model"),
        "taxonomy_policy_version": run.start.get("taxonomy_policy_version"),
        "level5_policy_version": run.start.get("level5_policy_version"),
        "application_entity_policy_version": run.start.get("application_entity_policy_version"),
        "thresholds": {**first.get("thresholds", {}), **next(iter(run.stage3.values())).get("thresholds", {})},
    }


def paper_fields(
    paper_id: str, merged: jr.Merged, *, nodes: Mapping[str, tuple[str, str]],
    allowed_tags: Sequence[str], level3_ids: frozenset[str],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """``(levels record, targets record, duplicates not repeated)`` of one scored paper."""

    stage1, stage3, run = merged.stage1[paper_id], merged.stage3[paper_id], merged.run_of[paper_id]
    level3 = jr.lv3_of(stage1)
    unknown = [identifier for identifier, _probability in level3 if identifier not in level3_ids]
    if unknown:
        raise JevFieldsError(f"{paper_id}: Level-3 ids outside the profile template: {unknown}")
    entities = jr.entities_of(stage1, stage3)
    unknown = [tag for tag, _probability in entities if tag not in allowed_tags]
    if unknown:
        raise JevFieldsError(f"{paper_id}: tags outside the profile template: {unknown}")
    chosen = {tag for tag, _probability in entities}
    metadata_only = jr.metadata_only(stage1, stage3)
    basis = "Abstract" if metadata_only else "PDF"

    applications: list[dict[str, Any]] = []
    duplicates: list[str] = []
    listed_ids: set[str] = set()
    listed_names: set[str] = set()
    for app in jr.applications_of(stage3, merged.catalog_of(paper_id)):
        name = " ".join(str(app.name).split())
        if not name:
            raise JevFieldsError(f"{paper_id}: application {app.index} has no name")
        if app.mapped:
            fresh = [identifier for identifier in app.lv5 if identifier not in listed_ids]
            if len(fresh) < len(app.lv5):
                duplicates.append(f"{paper_id}: {name}: {', '.join(i for i in app.lv5 if i in listed_ids)} already listed")
            if not fresh:
                continue
            level5 = []
            for identifier in fresh:
                if identifier not in nodes:
                    raise JevFieldsError(f"{paper_id}: Jev Level-5 id {identifier} is not a DF-Insight Level-5 node")
                title_ko, title_en = nodes[identifier]
                level5.append({"id": identifier, "title_ko": title_ko, "title_en": title_en,
                               "probability": app.lv5[identifier]})
                listed_ids.add(identifier)
            applications.append({"name": name, "level5": level5})
            continue
        key = target_key(name)
        if key in listed_names:
            duplicates.append(f"{paper_id}: {name}: the same name is already listed")
            continue
        listed_names.add(key)
        applications.append({"name": name, "level5": []})

    identifiers = [identifier for identifier, _probability in level3]
    levels = {
        "status": "classified", "basis": basis, "basis_reason": METADATA_ONLY_REASON if metadata_only else "",
        "lv1": list(dict.fromkeys(identifier.split("-", 1)[0] for identifier in identifiers)),
        "lv2": list(dict.fromkeys(identifier[:7] for identifier in identifiers)),
        "lv3": identifiers,
        "probabilities": {identifier: probability for identifier, probability in level3},
        "run_id": run.run_id,
    }
    targets = {
        "status": "ok", "basis": basis, "basis_label": "제목·초록" if metadata_only else "원문 PDF",
        "tags": [tag for tag in allowed_tags if tag in chosen],
        "entities": [{"tag": tag, "probability": probability} for tag, probability in entities],
        "applications": applications,
        "run_id": run.run_id,
    }
    return levels, targets, duplicates


def build(
    *, directory: Path = JEVAI_DIR, database: Path = DATABASE, technique_dir: Path = TECHNIQUE_DIR,
    results_db: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], ExportResult]:
    """The two payloads (nothing written) and what they contain."""

    from Analyzer.tag_taxonomy import default_tag_taxonomy
    from Analyzer.technique_taxonomy import TaxonomyError, load_taxonomy

    paths = sorted(directory.glob(jr.RUN_PATTERN))
    if not paths:
        raise JevFieldsError(f"no Jev AI run log ({jr.RUN_PATTERN}) in {directory}")
    merged = jr.load_runs(paths)
    results_db = results_db or latest_database(directory)
    if results_db is None:
        raise JevFieldsError(f"no Jev AI results DB ({jr.DB_PATTERN}) in {directory}: the decisions cannot be checked")
    final, _surfaces = jr.load_db(results_db)
    apps = {paper_id: jr.applications_of(merged.stage3[paper_id], merged.catalog_of(paper_id)) for paper_id in merged.stage3}
    check = jr.check_against_db(merged, apps, final)
    if not check.ok:
        raise JevFieldsError(
            f"the Jev decisions differ from {results_db.name} for {len(check.mismatches)} papers: {check.mismatches[:10]}"
        )
    try:
        level3_ids = frozenset(load_taxonomy().techniques)
    except TaxonomyError as exc:
        raise JevFieldsError(str(exc)) from exc
    allowed_tags = default_tag_taxonomy().allowed_tags
    nodes = level5_titles(technique_dir)
    selected = selected_source_ids(database)

    levels: dict[str, Any] = {}
    targets: dict[str, Any] = {}
    duplicates: list[str] = []
    pending: list[str] = []
    for paper_id in selected:
        if paper_id not in merged.stage1:
            pending.append(paper_id)
            levels[paper_id] = {"status": "pending", "basis": "", "basis_reason": NO_RESULT_REASON, "lv1": [], "lv2": [], "lv3": [],
                                "probabilities": {}, "run_id": None}
            targets[paper_id] = {"status": "pending", "basis": "", "basis_label": "", "reason": NO_RESULT_REASON, "tags": [],
                                 "entities": [], "applications": [], "run_id": None}
            continue
        levels[paper_id], targets[paper_id], repeated = paper_fields(
            paper_id, merged, nodes=nodes, allowed_tags=allowed_tags, level3_ids=level3_ids,
        )
        duplicates.extend(repeated)
    runs = [_run_row(run, merged) for run in merged.runs]
    scored = [record for record in targets.values() if record["status"] == "ok"]
    common = {
        "schema_version": SCHEMA_VERSION,
        "generated_on": date.today().isoformat(),
        "source": "Jev AI — III '다) 논문 분류'",
        "runs": runs,
        "results_db": {"file": results_db.name, "papers": check.db_papers, "checked": check.papers},
        "paper_count": len(selected),
        "pending_count": len(pending),
    }
    levels_payload = {
        **common,
        "description": "논문별 기술분류(Level-3). Analyzer/MD/paper_md.py가 MD의 classification을 여기서 읽는다.",
        "rule": "Jev AI Level-3 확률이 실행의 기준값(taxonomy, 0.33) 이상(같으면 포함)인 기술, 확률이 높은 순(같으면 ID 순). "
                "여러 실행에서 채점된 논문은 가장 나중 실행의 결과.",
        "empty_note": EMPTY_LEVEL_NOTE,
        "papers": levels,
    }
    targets_payload = {
        **common,
        "description": "논문별 태그와 대상 애플리케이션(Level-5 ID 포함). Analyzer/MD/paper_md.py가 MD의 tags와 '4. 대상 애플리케이션'을 여기서 읽는다.",
        "rules": {
            "tags": "Jev AI 엔티티: stage 1(DEV·FS·OS·CLOUD-IaaS·DaaS) 0.36 이상, APP-* 0.44 이상, CLOUD-SaaS:* 0.32 이상(같으면 포함); "
                    "템플릿의 태그 순서",
            "applications": "Jev가 추출한 애플리케이션 순서. Level-5 노드(맥락 중 하나에서 min(identity, context, scope) 0.44 이상)가 있으면 "
                            "ID마다 한 행(DF-Insight 노드 제목), 없으면 ID 공란 한 행(Jev가 추출한 이름). 앞의 애플리케이션에 이미 나온 ID·이름은 다시 쓰지 않음",
        },
        "registry": {"techniques": "web/DF-Insight/data/techniques", "level5_nodes": len(nodes)},
        "papers": targets,
    }
    result = ExportResult(
        levels=None, targets=None, paper_count=len(selected), scored=len(scored), pending=pending,
        not_selected=sorted(set(merged.stage1) - set(selected)),
        with_level3=sum(1 for record in levels.values() if record["lv3"]),
        with_tags=sum(1 for record in scored if record["tags"]),
        with_applications=sum(1 for record in scored if record["applications"]),
        level5_rows=sum(len(item["level5"]) for record in scored for item in record["applications"]),
        blank_rows=sum(1 for record in scored for item in record["applications"] if not item["level5"]),
        metadata_only=sum(1 for record in scored if record["basis"] == "Abstract"),
        runs=runs, database=results_db.name, checked=check.papers, duplicates=duplicates,
    )
    return levels_payload, targets_payload, result


def export(
    *, directory: Path = JEVAI_DIR, database: Path = DATABASE, technique_dir: Path = TECHNIQUE_DIR,
    results_db: Path | None = None, levels_path: Path | None = None, targets_path: Path | None = None,
) -> ExportResult:
    """Check the Jev results and write ``levels.json`` and ``targets.json`` (atomically)."""

    levels_payload, targets_payload, result = build(
        directory=directory, database=database, technique_dir=technique_dir, results_db=results_db,
    )
    result.levels = levels_path or LEVELS_JSON
    result.targets = targets_path or TARGETS_JSON
    write_json_atomic(result.levels, levels_payload)
    write_json_atomic(result.targets, targets_payload)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jev AI 결과 → MD 입력(Analyzer/JevAI/levels.json·targets.json)")
    parser.add_argument("--check", action="store_true", help="검증만 하고 파일은 쓰지 않음")
    parser.add_argument("--db", type=Path, default=None, help="검증할 결과 DB. 기본: Analyzer/JevAI의 가장 최근 results_kci*.db")
    args = parser.parse_args(argv)
    try:
        if args.check:
            _levels, _targets, result = build(results_db=args.db)
        else:
            result = export(results_db=args.db)
    except (JevFieldsError, jr.JevAIError, OSError, ValueError) as exc:
        print(f"[JEV] 오류: {exc}", file=sys.stderr)
        return 1
    runs = " + ".join(f"{run['file']} {run['papers_used']}편" for run in result.runs)
    print(f"[JEV] {runs} → 선별 논문 {result.paper_count}편 중 Jev 결과 {result.scored}편 (결과 DB {result.database}와 {result.checked}편 일치)")
    print(f"[JEV] Level-3 있음 {result.with_level3}편 · 태그 있음 {result.with_tags}편 · 대상 애플리케이션 있음 {result.with_applications}편 "
          f"(Level-5 행 {result.level5_rows} · ID 공란 행 {result.blank_rows}) · 제목·초록만으로 판정 {result.metadata_only}편")
    if result.pending:
        print(f"[JEV] Jev 결과가 없는 선별 논문 {len(result.pending)}편(MD 대기): {', '.join(result.pending[:10])}", file=sys.stderr)
    if result.not_selected:
        print(f"[JEV] 선별 목록에 없어 쓰지 않은 Jev 논문 {len(result.not_selected)}편: {', '.join(result.not_selected[:10])}")
    if result.levels:
        print(f"  {result.levels}\n  {result.targets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
