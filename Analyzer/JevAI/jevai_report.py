#!/usr/bin/env python3
"""Jev AI mapping scores (JSONL) -> six result workbooks under ``Analyzer/JevAI``.

Jev AI (an external tool) scores every paper in two stages and logs one JSON line per
paper and stage (``mapping_scores_<time>_<run>.jsonl``):

stage 1  Level-3 taxonomy: a probability for each of the 79 Level-3 ids; entity tags of the
         paper (DEV/FS/OS/CLOUD-IaaS/DaaS) with probabilities.
stage 3  target applications extracted from the paper, scored against Jev's own Level-5
         catalog (151 nodes of 52 products, ids with a 2-digit last segment): for each
         application, research context and catalog node the probability is the minimum of
         three factor answers — identity (is it that product), context (the node's Level-3)
         and scope (the node's Level-4); application-level entity tags (APP-*, CLOUD-SaaS:*).

Final decisions, all inclusive (verified to reproduce the run's results DB for every paper):

    Level-3     probability >= thresholds.taxonomy (0.33)
    entity      stage 1 >= thresholds.entity (0.36); APP-* >= thresholds.app (0.44);
                CLOUD-SaaS:* >= thresholds.saas (0.32)
    Level-5     a node is mapped to an application when one of its contexts reaches
                thresholds.level5 (0.44)

An application that is not mapped gets a reason from its factor answers:

    시스템 등록 X                 no catalog product reaches 0.44 on identity
    시스템에 등록은 되어있지만 …   identity >= 0.44 for a product, but the paper's context (Level-3)
                                  or scope (Level-4) does not fit any node of it
    Threshold 만족X               identity >= 0.44 and the best probability is a near miss
                                  (NEAR_MISS <= p < 0.44)

Several runs.  Jev is run again for papers selected later (2026-10-12: 517 papers; 2026-10-25: the
46 selected since), so by default every ``mapping_scores_*.jsonl`` of the folder is read and the runs
are merged per paper: a paper scored by more than one run keeps the records of the latest run (by
the run's start time).  Each paper is decided with its own run's thresholds and Level-5 catalog, and
the merged decisions are checked against the latest results DB, which holds every paper's final
result.  The summary sheets describe every run (time, papers, model and policy versions) and what
differs between them.  ``--jsonl FILE`` builds the workbooks from that one run, as before.

Outputs (names as requested): JevAI-LV3.xlsx, JevAI-LV3-Fail.xlsx, JevAI-application-LV5.xlsx,
JevAI-application_empty-LV5.xlsx, JevAI-application-list.xlsx, JevAI-Entity-list.xlsx.
Paper titles, journals and the paper id (KNOW-DOMP, as in the project's other workbooks)
come from the project corpus; a paper that has no KNOW-DOMP number yet (selected after the
last renumbering) is listed under its KCI article id (ART…) and gets its KNOW-DOMP id
automatically when the workbooks are built again after renumbering.  The results DB is read
only to check the decisions, for the applications' aliases and the extraction model.

    python -m Analyzer.JevAI.jevai_report                      # every run of the folder, latest DB
    python -m Analyzer.JevAI.jevai_report --jsonl <file> [<file> ...] --db <results db> --out Analyzer/JevAI
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

JEVAI_DIR = REPOSITORY_ROOT / "Analyzer" / "JevAI"
TECHNIQUES_DIR = REPOSITORY_ROOT / "web" / "DF-Insight" / "data" / "techniques"
TITLES_KO = REPOSITORY_ROOT / "Analyzer" / "LV" / "Techniques" / "tech_titles_ko.json"
RUN_PATTERN = "mapping_scores_*.jsonl"
DB_PATTERN = "results_kci*.db"
NEAR_MISS = 0.40                 # best probability in [0.40, threshold) -> 'Threshold 만족X'
NOISE_IDENTITY = 0.10            # below this the 'closest product' says nothing
KST = timezone(timedelta(hours=9))
# An application name whose PDF text came out with a private-use glyph (the font's '한').
NAME_FIXES = {"\uf53a 글": "한글"}
# DF-Insight's Level-5 ids had a 3-digit last segment until 2026-10 and have 2 digits since (Jev's form).
LEVEL5_ANY_RE = re.compile(r"[A-Z]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2,3}")
FILES = {
    "lv3": "JevAI-LV3.xlsx",
    "lv3_fail": "JevAI-LV3-Fail.xlsx",
    "app_lv5": "JevAI-application-LV5.xlsx",
    "app_empty": "JevAI-application_empty-LV5.xlsx",
    "app_list": "JevAI-application-list.xlsx",
    "entity": "JevAI-Entity-list.xlsx",
}


class JevAIError(RuntimeError):
    pass


def _f2(value: float) -> str:
    return f"{value:.2f}"


def _visible(text: str) -> str:
    """Private-use characters (PDF font glyphs) shown as [U+XXXX]."""

    return "".join(f"[U+{ord(character):04X}]" if 0xE000 <= ord(character) <= 0xF8FF else character for character in text)


def _moment(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Reading the runs
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Run:
    start: dict[str, Any]
    finish: dict[str, Any] | None
    stage1: dict[str, dict[str, Any]]
    stage3: dict[str, dict[str, Any]]
    path: Path
    repairs: list[tuple[int, int]] = field(default_factory=list)    # (first line, last line) of records split over lines

    @property
    def catalog(self) -> dict[str, str]:
        return {item["id"]: item["name"] for item in self.start["level5_catalog"]}

    @property
    def candidate_names(self) -> dict[str, str]:
        return dict(self.start.get("candidate_names") or {})

    @property
    def run_id(self) -> str:
        return str(self.start.get("run_id") or self.path.stem)

    @property
    def started(self) -> datetime | None:
        return _moment(self.start.get("time"))

    def catalog_of(self, paper_id: str) -> dict[str, str]:
        """The Level-5 catalog the paper was scored with (one run: the run's)."""

        return self.catalog


def load_run(path: Path) -> Run:
    """The records of one scoring run.  A record broken over several physical lines (a stray
    newline inside it) is joined back without the newline; any other unreadable line is an error."""

    lines = Path(path).read_text(encoding="utf-8").split("\n")
    records: list[dict[str, Any]] = []
    repairs: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        text, last = lines[index], index
        while True:
            try:
                records.append(json.loads(text))
                break
            except json.JSONDecodeError as exc:
                if last + 1 >= len(lines) or last - index >= 3:
                    raise JevAIError(f"{path}: line {index + 1}: unreadable record ({exc})") from exc
                last += 1
                text += lines[last]
        if last > index:
            repairs.append((index + 1, last + 1))
        index = last + 1
    start = next((record for record in records if record.get("event") == "score_run_started"), None)
    if start is None:
        raise JevAIError(f"{path}: no score_run_started record")
    finish = next((record for record in records if record.get("event") == "score_run_finished"), None)
    stage1: dict[str, dict[str, Any]] = {}
    stage3: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("event") != "mapping_scores":
            continue
        target = {"1": stage1, "3": stage3}.get(str(record.get("stage")))
        if target is None:
            raise JevAIError(f"{path}: unknown stage {record.get('stage')!r} for {record.get('paper_id')}")
        if record["paper_id"] in target:
            raise JevAIError(f"{path}: two stage-{record.get('stage')} records for {record['paper_id']}")
        if record.get("status") != "computed" or not record.get("scores_available"):
            raise JevAIError(f"{path}: {record['paper_id']} stage {record.get('stage')} was not scored ({record.get('status')})")
        target[record["paper_id"]] = record
    if set(stage1) != set(stage3):
        raise JevAIError(f"{path}: stage 1 and stage 3 cover different papers")
    return Run(start, finish, stage1, stage3, Path(path), repairs)


@dataclass
class Merged:
    """Several runs combined per paper: a paper scored by more than one run keeps the latest run's records."""

    runs: list[Run]                                   # oldest first (start time, then file name)
    stage1: dict[str, dict[str, Any]]
    stage3: dict[str, dict[str, Any]]
    run_of: dict[str, Run]                            # paper -> the run whose records are used
    superseded: dict[str, list[Run]]                  # paper -> earlier runs that also scored it (not used)
    catalogs: dict[str, dict[str, str]] = field(default_factory=dict)    # run id -> its Level-5 catalog

    @property
    def latest(self) -> Run:
        return self.runs[-1]

    @property
    def catalog(self) -> dict[str, str]:
        """Every run's catalog (a later run's name wins) — for descriptions; decisions use ``catalog_of``."""

        merged: dict[str, str] = {}
        for run in self.runs:
            merged.update(self.catalogs[run.run_id])
        return merged

    @property
    def candidate_names(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for run in self.runs:
            merged.update(run.candidate_names)
        return merged

    def catalog_of(self, paper_id: str) -> dict[str, str]:
        return self.catalogs[self.run_of[paper_id].run_id]

    def papers_of(self, run: Run) -> list[str]:
        """The papers whose result comes from ``run``."""

        return sorted(paper_id for paper_id, owner in self.run_of.items() if owner is run)


def run_order(run: Run) -> tuple[datetime, str]:
    return run.started or datetime.min.replace(tzinfo=timezone.utc), run.path.name


def merge_runs(runs: Sequence[Run]) -> Merged:
    """The runs merged per paper; the latest run (start time, then file name) wins for a paper in several."""

    if not runs:
        raise JevAIError("no Jev run to read")
    ordered = sorted(runs, key=run_order)
    seen: dict[str, Run] = {}
    for run in ordered:
        if not run.stage1:
            raise JevAIError(f"{run.path.name}: no scored paper")
        if run.run_id in seen:
            raise JevAIError(f"{seen[run.run_id].path.name} and {run.path.name} are the same run ({run.run_id})")
        seen[run.run_id] = run
    stage1: dict[str, dict[str, Any]] = {}
    stage3: dict[str, dict[str, Any]] = {}
    run_of: dict[str, Run] = {}
    superseded: dict[str, list[Run]] = defaultdict(list)
    for run in ordered:
        for paper_id in sorted(run.stage1):
            if paper_id in run_of:
                superseded[paper_id].append(run_of[paper_id])
            stage1[paper_id], stage3[paper_id], run_of[paper_id] = run.stage1[paper_id], run.stage3[paper_id], run
    return Merged(ordered, stage1, stage3, run_of, dict(superseded), {run.run_id: run.catalog for run in ordered})


def load_runs(paths: Sequence[Path]) -> Merged:
    return merge_runs([load_run(Path(path)) for path in paths])


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #

def lv3_of(record: Mapping[str, Any]) -> list[tuple[str, float]]:
    """(Level-3 id, probability) at or above the taxonomy threshold, probability descending."""

    threshold = record["thresholds"]["taxonomy"]
    ranked = sorted(record["taxonomy"].items(), key=lambda item: (-item[1]["probability"], item[0]))
    return [(identifier, value["probability"]) for identifier, value in ranked if value["probability"] >= threshold]


def top_lv3(record: Mapping[str, Any], count: int = 3) -> list[tuple[str, float]]:
    ranked = sorted(record["taxonomy"].items(), key=lambda item: (-item[1]["probability"], item[0]))
    return [(identifier, value["probability"]) for identifier, value in ranked[:count]]


def metadata_only(stage1: Mapping[str, Any], stage3: Mapping[str, Any]) -> bool:
    """Scored from the title and abstract only (no full text)."""

    return stage3.get("source_mode") == "metadata_only" or all(
        value.get("substantive_relevance_method") == "metadata_only_direct" for value in stage1["taxonomy"].values()
    )


def entity_threshold_key(tag: str) -> str:
    return "app" if tag.startswith("APP-") else "saas" if tag.startswith("CLOUD-SaaS") else "entity"


def entity_label(tag: str, stage3: Mapping[str, Any]) -> str:
    """An application tag (APP-*, CLOUD-SaaS:*) with the applications it came from (score at or above
    its threshold, highest first) — an application tag is only meaningful with the applications."""

    if not (tag.startswith("APP-") or tag.startswith("CLOUD-SaaS")):
        return tag
    from Analyzer.Application.application_list import join_names

    threshold = stage3["thresholds"][entity_threshold_key(tag)]
    names = {int(app["index"]): NAME_FIXES.get(str(app["name"]), str(app["name"])) for app in stage3.get("applications") or []}
    scores = sorted(
        ((score["probability"], names[int(score["application_index"])]) for score in (stage3["entity"][tag].get("application_scores") or [])
         if score["probability"] >= threshold and score.get("application_index") is not None and int(score["application_index"]) in names),
        key=lambda item: (-item[0], item[1]),
    )
    return f"{tag} ({join_names(list(dict.fromkeys(name for _probability, name in scores)))})" if scores else tag


def entities_of(stage1: Mapping[str, Any], stage3: Mapping[str, Any]) -> list[tuple[str, float]]:
    """(tag, probability) of the paper's accepted entity tags, probability descending."""

    found = [(tag, value["probability"]) for tag, value in stage1["entity"].items() if value["probability"] >= stage1["thresholds"]["entity"]]
    found += [
        (tag, value["probability"]) for tag, value in stage3["entity"].items()
        if value["probability"] >= stage3["thresholds"][entity_threshold_key(tag)]
    ]
    return sorted(found, key=lambda item: (-item[1], item[0]))


@dataclass(slots=True)
class Factor:
    node: str
    context: int
    identity: float
    scope_context: float      # the node's Level-3 context answer
    scope: float              # the node's Level-4 scope answer
    probability: float


@dataclass(slots=True)
class Application:
    paper_id: str
    index: int
    name: str
    raw_name: str
    lv5: dict[str, float]                      # catalog id -> best probability at or above the threshold
    identity: dict[str, float]                 # catalog product -> identity answer
    factors: list[Factor]
    category: str = ""
    reason: str = ""
    subcategory: str = ""

    @property
    def mapped(self) -> bool:
        return bool(self.lv5)


def _identity_question(index: int, product: str) -> str:
    return f"identity_a{index}_{hashlib.sha256(product.encode('utf-8')).hexdigest()[:16]}"


def applications_of(stage3: Mapping[str, Any], catalog: Mapping[str, str]) -> list[Application]:
    """Every extracted application of the paper with its Level-5 result (no reasons yet)."""

    answers = stage3.get("factor_answers") or {}
    threshold = stage3["thresholds"]["level5"]
    products = sorted(set(catalog.values()))
    rows_by_app: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in stage3.get("target_app") or []:
        if row.get("application_index") is not None:
            rows_by_app[int(row["application_index"])].append(row)
    result: list[Application] = []
    for app in stage3.get("applications") or []:
        index, raw_name = int(app["index"]), str(app["name"])
        lv5: dict[str, float] = {}
        factors: list[Factor] = []
        for row in rows_by_app.get(index, []):
            if row["probability"] >= threshold:
                lv5[row["id"]] = max(lv5.get(row["id"], 0.0), row["probability"])
            values = {question.split("_", 1)[0]: answers[question] for question in row["factor_question_ids"]}
            factors.append(Factor(row["id"], int(row["context_index"]), values["identity"], values["context"], values["scope"], row["probability"]))
        identity = {product: answers[_identity_question(index, product)] for product in products if _identity_question(index, product) in answers}
        result.append(Application(stage3["paper_id"], index, NAME_FIXES.get(raw_name, raw_name), raw_name,
                                  dict(sorted(lv5.items())), identity, factors))
    return result


def db_target_app(applications: Sequence[Application], catalog: Mapping[str, str]) -> list[dict[str, Any]]:
    """The results DB's ``target_app`` value for these applications (grouped by catalog name)."""

    groups: dict[str, set[str]] = defaultdict(set)
    for app in applications:
        for node in app.lv5:
            groups[catalog[node]].add(node)
    return sorted(({"application_name": name, "id": sorted(ids)} for name, ids in groups.items()), key=lambda item: item["application_name"])


# --------------------------------------------------------------------------- #
# Reasons for applications without a Level-5 node
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Names:
    level3_ko: dict[str, str]
    level4_ko: dict[str, str]

    def l3(self, node: str) -> str:
        key = node[:10]
        return self.level3_ko.get(key, key)

    def l4(self, node: str) -> str:
        key = node.rsplit("-", 1)[0]
        return self.level4_ko.get(key, key)


def load_names(catalog: Mapping[str, str]) -> Names:
    titles = json.loads(TITLES_KO.read_text(encoding="utf-8"))
    level4: dict[str, str] = {}
    for node in catalog:
        key = node.rsplit("-", 1)[0]
        path = TECHNIQUES_DIR / f"{key}.json"
        if key not in level4 and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            level4[key] = (data.get("title") or {}).get("ko") or key
    return Names(dict(titles.get("level_3") or {}), level4)


def to_df_insight(node: str) -> str:
    """Jev's catalog id (2-digit last segment) in the 3-digit form DF-Insight used until 2026-10."""

    head, tail = node.rsplit("-", 1)
    return f"{head}-{int(tail):03d}"


def node_key(node: str) -> tuple[str, int]:
    """A Level-5 id independent of the width of its last segment (COLL-02-04-06-04 = COLL-02-04-06-004)."""

    head, tail = node.rsplit("-", 1)
    return head, int(tail)


@dataclass(slots=True)
class RegistryNote:
    product: str
    nodes: int
    in_catalog: str | None      # the Jev catalog product the DF-Insight product corresponds to, if any


def registry_note(surfaces: Sequence[str], registry, catalog: Mapping[str, str]) -> RegistryNote | None:
    """Whether DF-Insight (exact name and alias rules) knows the application, for context only."""

    if registry is None:
        return None
    match = registry.match(surfaces)
    if not match:
        return None
    by_key = {node_key(node): name for node, name in catalog.items()}
    nodes = registry.products.get(match.product, ())
    in_catalog = next((by_key[node_key(node)] for node in nodes if node_key(node) in by_key), None)
    return RegistryNote(match.product, len(nodes), in_catalog)


@dataclass(slots=True)
class DFInsight:
    """DF-Insight's application registry as read for the notes (``registry`` None: not readable)."""

    registry: Any | None
    adapted: list[str] = field(default_factory=list)    # how the registry was read differently from the pipeline's default
    error: str = ""


def load_df_insight(**paths: Path) -> DFInsight:
    """DF-Insight's application registry (``Analyzer.Application.registry``) for the reasons' notes.

    The registry module can lag DF-Insight: in 2026-10 DF-Insight moved its Level-5 ids to a 2-digit
    last segment and dropped many products, while the module still expected 3 digits and the reviewed
    alias table (``application_aliases.json``) still names the dropped products.  Then the registry is
    read again accepting both id forms and without the alias entries that name a product DF-Insight no
    longer has (an entry naming a title of a reviewed product group is redirected to the group's
    remaining title); ``adapted`` says what was done.  If even that fails, the notes are left out and
    ``error`` says why.  ``paths``: ``load_registry``'s folder and file arguments (tests)."""

    from Analyzer.Application import registry as module

    try:
        return DFInsight(module.load_registry(**paths))
    except module.RegistryError as exc:
        first = str(exc)
    try:
        registry, adapted = _load_adapted_registry(module, paths)
    except (module.RegistryError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return DFInsight(None, [], f"{first}; {exc}")
    return DFInsight(registry, adapted)


def _load_adapted_registry(module, paths: Mapping[str, Path]) -> tuple[Any, list[str]]:
    adapted: list[str] = []
    saved = module.LEVEL5_RE
    try:
        if not saved.fullmatch("COLL-02-04-01-01"):
            module.LEVEL5_RE = LEVEL5_ANY_RE
            adapted.append("파이프라인 등록 목록 모듈이 Level-5 ID 끝자리 3자리만 받아, 지금 DF-Insight의 2자리 ID도 받도록 읽음")
        source = Path(paths.get("pipeline_aliases", module.PIPELINE_ALIASES_PATH))
        table = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
        others = {key: value for key, value in paths.items() if key != "pipeline_aliases"}
        with tempfile.TemporaryDirectory(prefix="jevai-registry-") as folder:
            base = module.load_registry(pipeline_aliases=Path(folder) / "none.json", **others)

            def known(title: str) -> bool:
                return module.squash_key(title) in base.squashed

            groups, changed, redirect = [], 0, {}
            for group in table.get("product_groups") or []:
                kept = [title for title in group if known(title)]
                if len(kept) < len(group):
                    changed += 1
                    redirect.update({title: kept[0] for title in group if kept and not known(title)})
                if len(kept) >= 2:
                    groups.append(kept)
            aliases, dropped, redirected = {}, 0, 0
            for alias, title in (table.get("aliases") or {}).items():
                if base.product_for_title(title) is not None:
                    aliases[alias] = title
                elif title in redirect:
                    aliases[alias], redirected = redirect[title], redirected + 1
                else:
                    dropped += 1
            if changed or dropped or redirected:
                adapted.append(
                    f"검토 별칭표({source.name})의 별칭 {len(table.get('aliases') or {})}개 중 {dropped}개는 지금 DF-Insight에 없는 제품을 가리켜 빼고"
                    + (f" {redirected}개는 같은 제품 묶음의 남은 이름으로 읽음" if redirected else "")
                    + f"; 제품 묶음 {len(table.get('product_groups') or [])}개 중 {changed}개에서 DF-Insight에 없는 이름을 뺌"
                )
            filtered = Path(folder) / "application_aliases.json"
            filtered.write_text(json.dumps({**table, "product_groups": groups, "aliases": aliases}, ensure_ascii=False), encoding="utf-8")
            registry = module.load_registry(pipeline_aliases=filtered, **others)
    finally:
        module.LEVEL5_RE = saved
    return registry, adapted


def catalog_in_registry(catalog: Mapping[str, str], registry) -> str:
    """How Jev's catalog ids appear in DF-Insight ('' without a registry)."""

    if registry is None:
        return ""
    nodes = registry.nodes
    same = [node for node in catalog if node in nodes]
    titled = [node for node in same if catalog[node] in (nodes[node].title_en, nodes[node].title_ko)]
    wide = [node for node in catalog if node not in nodes and to_df_insight(node) in nodes]
    if len(titled) == len(catalog):
        return f"DF-Insight도 같은 형식이며 {len(catalog)}개 노드 모두 DF-Insight에 같은 ID·같은 제품명으로 있음"
    if len(wide) == len(catalog):
        example = next(iter(catalog))
        return f"DF-Insight는 3자리: 예 {example} = {to_df_insight(example)}"
    return (f"DF-Insight에 같은 ID {len(same)}개(그중 제품명도 같은 노드 {len(titled)}개) · 끝자리 3자리로 {len(wide)}개 · "
            f"없음 {len(catalog) - len(same) - len(wide)}개")


def explain(app: Application, threshold: float, names: Names, catalog: Mapping[str, str], note: RegistryNote | None) -> None:
    """Category and one Korean sentence for an application without a Level-5 node."""

    n_products, n_nodes = len(set(catalog.values())), len(catalog)
    best_identity = max(app.identity.values(), default=0.0)
    if best_identity < threshold:
        closest = sorted(product for product, value in app.identity.items() if value == best_identity)
        if best_identity >= NOISE_IDENTITY:
            detail = f"가장 가까운 제품 {', '.join(repr(product) for product in closest)}의 동일 제품 확률 {_f2(best_identity)} < 기준 {_f2(threshold)}"
        else:
            detail = f"등록 제품 {n_products}개 모두 동일 제품 확률 {_f2(best_identity)} 이하"
        app.category = "시스템 등록 X"
        app.reason = f"시스템 등록 X — Jev Level-5 카탈로그({n_products}개 제품·{n_nodes}개 노드)에 같은 제품이 없음({detail})"
        if note and note.in_catalog:
            app.subcategory = "이름은 카탈로그 제품과 연결되지만 동일 제품 확률 미달"
            app.reason += f" · 참고: 이름·별칭으로는 카탈로그의 '{note.in_catalog}'와 연결되지만 Jev는 같은 제품으로 보지 않음"
        elif note:
            app.subcategory = "DF-Insight에는 있으나 Jev 카탈로그에 없음"
            app.reason += f" · 참고: DF-Insight에는 '{note.product}'(Level-5 노드 {note.nodes}개)로 등록되어 있으나 Jev 카탈로그에는 없음"
        else:
            app.subcategory = "DF-Insight에도 없음"
        return
    product = max(app.identity, key=lambda name: (app.identity[name], name))
    own = [factor for factor in app.factors if catalog[factor.node] == product]
    best = max(own, key=lambda factor: (factor.probability, -factor.context))
    if best.probability >= NEAR_MISS:
        app.category, app.subcategory = "Threshold 만족X", "근소 미달"
        app.reason = (f"Threshold 만족X — 등록 제품 '{product}'의 최고 확률 {_f2(best.probability)} < 기준 {_f2(threshold)}"
                      f"(제품 일치 {_f2(best.identity)} · 맥락 {names.l3(best.node)} {_f2(best.scope_context)} · "
                      f"범위 {names.l4(best.node)} {_f2(best.scope)}; 세 값 중 가장 낮은 값이 최종 확률)")
        return
    context_by_l3: dict[str, float] = {}
    for factor in own:
        key = names.l3(factor.node)
        context_by_l3[key] = max(context_by_l3.get(key, 0.0), factor.scope_context)
    context_ok = [factor for factor in own if factor.scope_context >= threshold]
    lead = f"시스템에 등록은 되어있지만('{product}', 동일 제품 확률 {_f2(app.identity[product])})"
    app.category = "시스템에 등록은 되어있지만"
    if not context_ok:
        parts = ", ".join(f"{name} {_f2(value)}" for name, value in sorted(context_by_l3.items(), key=lambda item: -item[1]))
        app.subcategory = "Level-3 맥락 불일치"
        app.reason = f"{lead} 논문에서 이 앱을 다룬 맥락이 등록 노드의 Level-3({parts})에 해당하지 않기 때문(기준 {_f2(threshold)})"
        return
    scope_by_l4: dict[str, float] = {}
    for factor in context_ok:
        key = names.l4(factor.node)
        scope_by_l4[key] = max(scope_by_l4.get(key, 0.0), factor.scope)
    l3_parts = ", ".join(sorted({f"{names.l3(factor.node)} {_f2(max(f.scope_context for f in context_ok if f.node[:10] == factor.node[:10]))}" for factor in context_ok}))
    l4_parts = ", ".join(f"{name} {_f2(value)}" for name, value in sorted(scope_by_l4.items(), key=lambda item: -item[1]))
    app.subcategory = "Level-4 범위 불일치"
    app.reason = f"{lead} Level-3 맥락({l3_parts})은 맞으나 등록 노드의 Level-4 범위({l4_parts})에 해당하지 않기 때문(기준 {_f2(threshold)})"


# --------------------------------------------------------------------------- #
# Papers, the results DB and the check
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class PaperInfo:
    paper_id: str
    record_id: str
    paper_index: int | None
    title: str
    journal: str


def corpus_info() -> dict[str, PaperInfo]:
    from Analyzer.LV import lv_experiment as lv

    return {
        paper.source_id: PaperInfo(paper.source_id, paper.record_id, paper.paper_index, " ".join(paper.display_title.split()), paper.journal)
        for paper in lv.load_corpus(lv.DEFAULT_DATABASE)
    }


def _connect(path: Path) -> sqlite3.Connection:
    # Read-only and immutable: a WAL-mode database gets no -wal/-shm side files.
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro&immutable=1", uri=True)


def load_db(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[list[str]]]]:
    """(paper_id -> final taxonomy/entity/target_app, paper_id -> the extracted applications' names and aliases)."""

    connection = _connect(path)
    try:
        results = {
            paper_id: {"taxonomy": json.loads(taxonomy or "[]"), "entity": json.loads(entity or "[]"), "target_app": json.loads(target or "[]")}
            for paper_id, taxonomy, entity, target in connection.execute("select paper_id, taxonomy, entity, target_app from paper_results")
        }
        surfaces: dict[str, list[list[str]]] = {}
        for paper_id, applications_json in connection.execute("select paper_id, applications_json from llm_extractions"):
            apps = json.loads(applications_json or "[]")
            apps = apps.get("applications", apps) if isinstance(apps, dict) else apps
            per_app = []
            for app in apps:
                name = app.get("name")
                values = [name.get("value") if isinstance(name, dict) else name]
                for alias in app.get("aliases") or []:
                    values.append(alias.get("value") if isinstance(alias, dict) else alias)
                per_app.append([value for value in values if value])
            surfaces[paper_id] = per_app
    finally:
        connection.close()
    return results, surfaces


def load_extractions(path: Path) -> dict[str, tuple[str, str]]:
    """paper_id -> (model, prompt version) of the application extraction stored in the results DB."""

    connection = _connect(path)
    try:
        return {
            paper_id: (str(model or "?"), str(prompt or "?"))
            for paper_id, model, prompt in connection.execute("select paper_id, model, prompt_version from llm_extractions")
        }
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


@dataclass(slots=True)
class Check:
    papers: int
    taxonomy: int
    entity: int
    target_app: int
    mismatches: list[str]
    db_papers: int = 0                                   # rows of the results DB
    db_only: list[str] = field(default_factory=list)     # DB papers that no compared run scored

    @property
    def ok(self) -> bool:
        return not self.mismatches


def check_against_db(run, apps: Mapping[str, list[Application]], db: Mapping[str, Mapping[str, Any]]) -> Check:
    """``run``: one Run or the Merged runs — every paper is compared with its own run's catalog."""

    mismatches: list[str] = []
    counts = Counter()
    for paper_id in run.stage1:
        final = db.get(paper_id)
        if final is None:
            mismatches.append(f"{paper_id}: not in the results DB")
            continue
        if sorted(identifier for identifier, _ in lv3_of(run.stage1[paper_id])) == sorted(final["taxonomy"]):
            counts["taxonomy"] += 1
        else:
            mismatches.append(f"{paper_id}: taxonomy")
        if sorted(tag for tag, _ in entities_of(run.stage1[paper_id], run.stage3[paper_id])) == sorted(final["entity"]):
            counts["entity"] += 1
        else:
            mismatches.append(f"{paper_id}: entity")
        if db_target_app(apps[paper_id], run.catalog_of(paper_id)) == final["target_app"]:
            counts["target_app"] += 1
        else:
            mismatches.append(f"{paper_id}: target_app")
    return Check(len(run.stage1), counts["taxonomy"], counts["entity"], counts["target_app"], mismatches,
                 len(db), sorted(set(db) - set(run.stage1)))


# --------------------------------------------------------------------------- #
# Workbooks
# --------------------------------------------------------------------------- #

HEADER_FILL = "305496"


def _style(sheet, widths: Sequence[int], probability_columns: Sequence[int] = (), freeze: str = "A2") -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    from Analyzer.Application.application_list import keep_text

    for cell in sheet[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if cell.column in probability_columns and isinstance(cell.value, float):
                cell.number_format = "0.00"
    keep_text(sheet)
    for position, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(position)].width = width
    sheet.freeze_panes = freeze
    sheet.auto_filter.ref = sheet.dimensions


def _table_sheet(workbook, title: str, header: Sequence[str], rows: Sequence[Sequence[Any]], widths: Sequence[int],
                 probability_columns: Sequence[int] = (), first: bool = False):
    sheet = workbook.active if first else workbook.create_sheet()
    sheet.title = title
    sheet.append(list(header))
    for row in rows:
        sheet.append([_cell(value) for value in row])
    _style(sheet, widths, probability_columns)
    return sheet


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)[:32000]
    return value


def _summary_sheet(workbook, sections: Sequence[tuple[str, Sequence[Sequence[Any]]]], first: bool = False) -> None:
    """요약: titled sections of 'item | value' rows (a section can carry a header row, marked with a leading None)."""

    from openpyxl.styles import Alignment, Font, PatternFill

    from Analyzer.Application.application_list import keep_text

    sheet = workbook.active if first else workbook.create_sheet()
    sheet.title = "요약"
    for title, rows in sections:
        sheet.append([title])
        sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True, size=12)
        for row in rows:
            header = bool(row) and row[0] is None
            values = list(row[1:]) if header else list(row)
            sheet.append([_cell(value) for value in values])
            for cell in sheet[sheet.max_row]:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if isinstance(cell.value, float):
                    cell.number_format = "0.00" if cell.value <= 1 else "0.0"
                if header:
                    cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=HEADER_FILL)
        sheet.append([])
    keep_text(sheet)
    for column, width in zip("ABCDE", (34, 70, 14, 14, 14)):
        sheet.column_dimensions[column].width = width


def _save(workbook, target: Path) -> Path:
    from Analyzer.Application.application_list import save_workbook

    return save_workbook(workbook, target)


def _kst(stamp: str | None) -> str:
    if not stamp:
        return "?"
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(KST)
    return moment.strftime("%Y-%m-%d %H:%M:%S KST")


def _counted(counter: Counter) -> str:
    """'a' for one value, else 'a 3편·b 2편' (values in order)."""

    if len(counter) == 1:
        return str(next(iter(counter)))
    return "·".join(f"{value} {count}편" for value, count in sorted(counter.items()))


@dataclass(slots=True)
class Report:
    source: Merged
    papers: dict[str, PaperInfo]
    apps: dict[str, list[Application]]
    check: Check | None
    names: Names
    db_path: Path | None
    df_insight: DFInsight | None = None
    extraction: dict[str, tuple[str, str]] = field(default_factory=dict)    # paper -> (model, prompt) from the DB
    not_scored: list[str] = field(default_factory=list)                     # selected papers that no run scored

    @property
    def runs(self) -> list[Run]:
        return self.source.runs

    @property
    def merged(self) -> bool:
        return len(self.source.runs) > 1

    @property
    def registry(self):
        return self.df_insight.registry if self.df_insight else None

    def record_of(self, paper_id: str) -> str:
        """The paper's KNOW-DOMP id, or its KCI article id (ART…) while it has no number yet."""

        return self.papers[paper_id].record_id or paper_id

    def ordered(self) -> list[str]:
        """KNOW-DOMP order; papers without a number after them, by KCI article id."""

        def key(paper_id: str) -> tuple[bool, str, str]:
            record = self.papers[paper_id].record_id
            return not record, record, paper_id

        return sorted(self.source.stage1, key=key)

    def unnumbered(self) -> list[str]:
        return [paper_id for paper_id in self.ordered() if not self.papers[paper_id].record_id]

    def label(self, run: Run) -> str:
        return f"실행 {next(number for number, other in enumerate(self.runs, 1) if other is run)}"

    def threshold(self, stage: int, key: str) -> str:
        """The threshold of all papers ('0.33'), or each run's when the runs differ."""

        def values(run: Run, papers: Sequence[str]) -> list[float]:
            records = run.stage1 if stage == 1 else run.stage3
            return sorted({records[paper_id]["thresholds"][key] for paper_id in papers})

        everything = sorted({value for run in self.runs for value in values(run, self.source.papers_of(run))})
        if len(everything) == 1:
            return _f2(everything[0])
        return " · ".join(f"{self.label(run)} {'/'.join(_f2(value) for value in values(run, self.source.papers_of(run)))}"
                          for run in self.runs if self.source.papers_of(run))

    def per_run(self, text: Callable[[Run, list[str]], str]) -> str:
        """' (실행 1 … · 실행 2 …)' for merged runs, '' for one run."""

        if not self.merged:
            return ""
        return " (" + " · ".join(f"{self.label(run)} {text(run, self.source.papers_of(run))}" for run in self.runs) + ")"

    def extraction_text(self, run: Run) -> str:
        rows = [self.extraction[paper_id] for paper_id in run.stage1 if paper_id in self.extraction]
        if not rows:
            return ""
        return f"{_counted(Counter(model for model, _ in rows))}(프롬프트 {_counted(Counter(prompt for _, prompt in rows))}, {len(rows)}편)"

    def check_text(self) -> str:
        if self.check is None:
            return "결과 DB 없이 생성(검증 안 함)"
        check = self.check
        name = self.db_path.name if self.db_path else "DB"
        if self.merged or check.db_only:
            name += f"({check.db_papers}편)"
        text = (f"{name}의 최종 결과와 비교{'(합친 결과 기준)' if self.merged else ''}: Level-3 {check.taxonomy}/{check.papers}편, "
                f"엔티티 {check.entity}/{check.papers}편, 애플리케이션 Level-5 {check.target_app}/{check.papers}편 일치"
                + ("" if check.ok else f" — 불일치 {len(check.mismatches)}건"))
        if check.db_only:
            text += f" · DB의 나머지 {len(check.db_only)}편은 {'실행 기록' if self.merged else '이 실행'}에 없어 비교하지 않음"
        return text

    def run_rows(self, run: Run) -> list[tuple[str, str]]:
        start, finish = run.start, run.finish or {}
        stage1, stage3 = next(iter(run.stage1.values())), next(iter(run.stage3.values()))
        papers = list(run.stage1)
        metadata = sum(metadata_only(run.stage1[paper_id], run.stage3[paper_id]) for paper_id in papers)
        count = (f"{len(papers)}편 (원문 {len(papers) - metadata}편 · 원문 없이 제목·초록만 {metadata}편) · "
                 f"실행 결과 성공 {finish.get('succeeded', '?')} · 실패 {finish.get('failed', '?')}")
        replaced = len(papers) - len(self.source.papers_of(run))
        if replaced:
            count += f" · 그중 {replaced}편은 나중 실행이 다시 채점해 그 결과를 씀"
        versions = (f"{stage1.get('model', '?')} · 분류체계 {start.get('taxonomy_policy_version', '?')} · "
                    f"Level-5 {start.get('level5_policy_version', '?')} · 애플리케이션·엔티티 {start.get('application_entity_policy_version', '?')} · "
                    f"{stage3.get('mapping_contract', '')}")
        extraction = self.extraction_text(run)
        if extraction:
            versions += f" · 앱 추출 LLM {extraction}"
        repaired = (", ".join(f"{first}~{last}번째 줄" for first, last in run.repairs) + "이 한 레코드였는데 중간에 줄바꿈이 들어가 있어, 줄바꿈 없이 이어 붙여 읽음"
                    if run.repairs else "없음")
        return [
            ("원본 파일", run.path.name),
            ("실행 ID", start.get("run_id", "")),
            ("실행 시각", f"{_kst(start.get('time'))} ~ {_kst(finish.get('time'))}"),
            ("논문 수", count),
            ("모델 · 정책 버전", versions),
            ("데이터 복구", repaired),
        ]

    def differences(self) -> str:
        """What differs between the runs and what they share."""

        runs = self.runs

        def joined(values: Sequence[str]) -> str:
            return " → ".join(values)

        def thresholds(run: Run) -> str:
            # Each decision reads its threshold from its own stage (see lv3_of, entities_of, applications_of).
            keys = ((1, "taxonomy", "LV3"), (1, "entity", "엔티티"), (3, "app", "APP-*"), (3, "saas", "CLOUD-SaaS:*"), (3, "level5", "Level-5"))
            parts = []
            for stage, key, label in keys:
                records = run.stage1 if stage == 1 else run.stage3
                values = sorted({record["thresholds"][key] for record in records.values() if key in record["thresholds"]})
                parts.append(f"{label} {'/'.join(_f2(value) for value in values) or '?'}")
            return ", ".join(parts)

        def catalog(run: Run) -> str:
            return f"{len(run.catalog)}개 노드·{len(set(run.catalog.values()))}개 제품"

        def models(run: Run) -> str:
            return "/".join(sorted({str(record.get("model", "?")) for record in run.stage1.values()}))

        def extraction(run: Run) -> str:
            return "/".join(sorted({self.extraction[paper_id][0] for paper_id in run.stage1 if paper_id in self.extraction})) or "-"

        items = [
            ("분류체계 정책", [str(run.start.get("taxonomy_policy_version", "?")) for run in runs]),
            ("Jev 모델", [models(run) for run in runs]),
            *([("앱 추출 LLM", [extraction(run) for run in runs])] if self.extraction else []),
            ("Level-5 정책", [str(run.start.get("level5_policy_version", "?")) for run in runs]),
            ("애플리케이션·엔티티 정책", [str(run.start.get("application_entity_policy_version", "?")) for run in runs]),
            ("매핑 방식", ["/".join(sorted({str(record.get("mapping_contract", "")) for record in run.stage3.values()})) for run in runs]),
            ("기준값", [thresholds(run) for run in runs]),
        ]
        differ = [f"{name} {joined(values)}" for name, values in items if len(set(values)) > 1]
        same = [f"{name} {values[0]}" if name != "기준값" else f"기준값({values[0]})" for name, values in items if len(set(values)) == 1]
        level3 = [len([key for key in run.candidate_names if re.fullmatch(r"[A-Z]{4}-[0-9]{2}-[0-9]{2}", key)]) for run in runs]
        if all(run.candidate_names == runs[0].candidate_names for run in runs):
            same.insert(0, f"Level-3 후보 {level3[0]}개와 이름")
        else:
            differ.append(f"Level-3 후보·이름 {joined([f'{count}개' for count in level3])}(내용이 다름)")
        if all(run.catalog == runs[0].catalog for run in runs):
            same.append(f"Level-5 카탈로그({catalog(runs[0])})")
        else:
            differ.append(f"Level-5 카탈로그 {joined([catalog(run) for run in runs])}(내용이 다름 — 논문마다 그 실행의 카탈로그로 판정)")
        return f"다름 — {' · '.join(differ) or '없음'} / 같음 — {' · '.join(same) or '없음'}"

    def source_sections(self, title: str) -> list[tuple[str, list[tuple[Any, ...]]]]:
        """The workbook's first section (what was read and checked); merged runs add one section per run."""

        coverage: list[tuple[str, str]] = []
        selected = len(self.papers)
        if self.not_scored:
            coverage.append(("선별 논문 대비", f"프로젝트 선별 논문 {selected}편 중 {len(self.not_scored)}편은 "
                                            f"{'Jev 결과가 없어' if self.merged else '이 실행에 없어'} 빠짐: "
                                            + ", ".join(self.not_scored[:10]) + (" …" if len(self.not_scored) > 10 else "")))
        elif self.merged:
            coverage.append(("선별 논문 대비", f"프로젝트 선별 논문 {selected}편 모두 Jev 결과 있음"))
        if not self.merged:
            rows = self.run_rows(self.runs[0])
            return [(title, [*rows[:5], ("검증", self.check_text()), rows[5], *coverage])]
        used = [(run, self.source.papers_of(run)) for run in self.runs]
        both = sum(1 for runs in self.source.superseded.values() if runs)
        several = "두 실행" if len(self.runs) == 2 else "여러 실행"
        composed = (f"Jev 실행 기록 {len(self.runs)}개를 논문별로 합침: {len(self.source.stage1)}편 = "
                    + " + ".join(f"{self.label(run)} {len(papers)}편" for run, papers in used)
                    + (f" · {several}에 모두 있는 논문 {both}편은 가장 나중 실행의 결과를 씀" if both
                       else f" · {several}에 함께 있는 논문은 없음(있으면 가장 나중 실행의 결과를 씀)"))
        sections: list[tuple[str, list[tuple[Any, ...]]]] = [(title, [
            ("결과 구성", composed),
            ("실행 간 차이", self.differences()),
            ("검증", self.check_text()),
            *coverage,
        ])]
        for run in self.runs:
            sections.append((f"{self.label(run)} — {_kst(run.start.get('time'))[:10]} ({len(run.stage1)}편)", self.run_rows(run)))
        return sections

    def id_note(self) -> str:
        unnumbered = self.unnumbered()
        if not unnumbered:
            return ""
        return (f"KNOW-DOMP 번호가 아직 없는 논문 {len(unnumbered)}편은 KCI 논문 ID(ART…)를 그대로 적음 — "
                "번호를 매긴 뒤(renumber) 다시 만들면 자동으로 KNOW-DOMP 번호로 바뀜")

    def registry_text(self) -> str:
        if self.df_insight is None:
            return "DF-Insight 등록 목록을 읽지 않고 생성(참고 정보 없음)"
        if self.df_insight.registry is None:
            return f"DF-Insight 등록 목록을 읽지 못해 참고 정보 없이 판정함({self.df_insight.error})"
        registry = self.df_insight.registry
        text = (f"web/DF-Insight{' ' + registry.revision if getattr(registry, 'revision', '') else ''}의 Level-5 애플리케이션 노드 "
                f"{len(registry.nodes)}개·제품 {len(registry.products)}개(이름·별칭 정확 일치로만 연결)")
        if self.df_insight.adapted:
            text += " · " + "; ".join(self.df_insight.adapted)
        return text


def _paper(report: Report, paper_id: str) -> PaperInfo:
    return report.papers[paper_id]


def build_lv3(report: Report, out: Path) -> tuple[Path, Path]:
    from openpyxl import Workbook

    source = report.source
    threshold = report.threshold(1, "taxonomy")
    names = source.candidate_names
    rows: list[list[Any]] = []
    fail_rows: list[list[Any]] = []
    per_paper = Counter()
    by_lv3: Counter[str] = Counter()
    bands = Counter()
    failed: set[str] = set()
    for paper_id in report.ordered():
        info, record = _paper(report, paper_id), source.stage1[paper_id]
        accepted = lv3_of(record)
        per_paper[len(accepted)] += 1
        if accepted:
            for identifier, probability in accepted:
                rows.append([info.title, info.journal, identifier, probability])
                by_lv3[identifier] += 1
            continue
        failed.add(paper_id)
        rows.append([info.title, info.journal, None, None])
        top = top_lv3(record)
        best, own = top[0][1], record["thresholds"]["taxonomy"]
        bands["0.30 이상(근소 미달)" if best >= 0.30 else "0.20~0.29" if best >= 0.20 else "0.10~0.19" if best >= 0.10 else "0.10 미만"] += 1
        prefix = "원문 PDF 없이 제목·초록만으로 판정 · " if metadata_only(record, source.stage3[paper_id]) else ""
        listed = ", ".join(f"{identifier} {report.names.level3_ko.get(identifier, names.get(identifier, ''))} {_f2(probability)}" for identifier, probability in top)
        fail_rows.append([info.title, info.journal,
                          f"{prefix}{len(record['taxonomy'])}개 LV3 후보 모두 확률이 기준값(threshold) {_f2(own)} 미만 — "
                          f"최고 {_f2(best)}(부족분 {_f2(own - best)}); 상위 3개: {listed}"])
    total = len(source.stage1)
    mapped_papers = total - per_paper[0]
    workbook = Workbook()
    level1 = Counter(identifier.split("-", 1)[0] for identifier in by_lv3.elements())
    lv3_table = [(None, "LV3", "이름", "논문 수")] + [
        (identifier, report.names.level3_ko.get(identifier, names.get(identifier, "")), by_lv3[identifier])
        for identifier in sorted(identifier for identifier in names if re.fullmatch(r"[A-Z]{4}-\d{2}-\d{2}", identifier))
    ]
    unnumbered = len(report.unnumbered())
    order = "프로젝트 논문 번호(KNOW-DOMP) 순" + (f", KNOW-DOMP 번호가 아직 없는 논문 {unnumbered}편은 그 뒤에 KCI 논문 ID(ART…) 순" if unnumbered else "")
    _summary_sheet(workbook, [
        *report.source_sections("JevAI Level-3(기술 분류) 매핑 결과"),
        ("판정 규칙", [
            ("확정 기준", f"Level-3 79개 각각의 확률이 기준값 {threshold} 이상(같으면 포함)이면 그 논문의 LV3로 확정. 개수 제한 없음."),
            ("Probability", "JevAI 실행 기록에 남은 각 LV3의 최종 확률(probability) 그대로입니다(소수 둘째 자리). 결과 DB의 확정 여부와 같은 값을 씁니다."
             + (" 논문마다 그 논문을 채점한 실행(같은 논문이 여러 실행에 있으면 가장 나중 실행)의 기록을 씁니다." if report.merged else "")),
        ]),
        ("집계", [
            ("LV3가 있는 논문", f"{mapped_papers}편 / {total}편"
             + report.per_run(lambda run, papers: f"{sum(1 for paper_id in papers if paper_id not in failed)}/{len(papers)}편")),
            ("LV3가 없는 논문", f"{per_paper[0]}편 → JevAI-LV3-Fail.xlsx"
             + report.per_run(lambda run, papers: f"{sum(1 for paper_id in papers if paper_id in failed)}편")),
            ("(논문, LV3) 행", f"{sum(by_lv3.values())}건 · 논문당 평균 {sum(by_lv3.values()) / max(1, mapped_papers):.2f}개(LV3 있는 논문 기준) · 최대 {max(per_paper)}개"),
            ("LV3 개수별 논문 수", " · ".join(f"{count}개 {per_paper[count]}편" for count in sorted(per_paper))),
            ("Level-1별 행 수", " · ".join(f"{name} {count}" for name, count in level1.most_common())),
            ("쓰인 LV3", f"{len(by_lv3)}개 / {len([i for i in names if re.fullmatch(r'[A-Z]{4}-[0-9]{2}-[0-9]{2}', i)])}개"),
        ]),
        ("시트 '논문별 결과'", [
            ("한 행", "논문 하나의 LV3 하나(확률이 높은 순). LV3가 없는 논문도 LV3·Probability를 비운 한 행으로 넣었습니다."),
            ("논문 순서", order),
        ]),
        ("LV3별 논문 수", lv3_table),
    ], first=True)
    _table_sheet(workbook, "논문별 결과", ("title", "journal_name", "LV3", "Probability"), rows, (70, 28, 14, 12), probability_columns=(4,))
    workbook.active = 0
    first = _save(workbook, out / FILES["lv3"])

    workbook = Workbook()
    _summary_sheet(workbook, [
        *report.source_sections("JevAI Level-3 매핑 실패 논문"),
        ("실패 기준", [
            ("매핑 실패", f"Level-3 79개 후보 모두 확률이 기준값 {threshold} 미만이라 확정된 LV3가 하나도 없는 논문"),
            ("fail_reason", "기준값과 최고 확률·부족분, 확률 상위 3개 후보(코드·이름·확률). 원문 없이 제목·초록만으로 판정한 논문은 그 사실을 앞에 적음."),
        ]),
        ("집계", [
            ("실패 논문", f"{len(fail_rows)}편 / {total}편" + report.per_run(lambda run, papers: f"{sum(1 for paper_id in papers if paper_id in failed)}/{len(papers)}편")),
            ("최고 확률 구간", " · ".join(f"{name} {count}편" for name, count in sorted(bands.items(), reverse=True))),
        ]),
    ], first=True)
    _table_sheet(workbook, "매핑실패 논문", ("title", "journal_name", "fail_reason"), fail_rows, (70, 28, 110))
    workbook.active = 0
    second = _save(workbook, out / FILES["lv3_fail"])
    return first, second


def build_applications(report: Report, out: Path) -> tuple[Path, Path, Path]:
    from openpyxl import Workbook

    source = report.source
    catalog = source.catalog
    threshold = report.threshold(3, "level5")
    mapped_rows, empty_rows, list_rows = [], [], []
    categories, subcategories = Counter(), Counter()
    papers_with_apps, papers_mapped = set(), set()
    fixed_names = []
    for paper_id in report.ordered():
        record_id = report.record_of(paper_id)
        for app in report.apps[paper_id]:
            papers_with_apps.add(paper_id)
            if app.name != app.raw_name:
                fixed_names.append((record_id, app.raw_name, app.name))
            list_rows.append([record_id, app.name, "O" if app.mapped else "X"])
            if app.mapped:
                papers_mapped.add(paper_id)
                mapped_rows.append([record_id, app.name, ", ".join(app.lv5)])
            else:
                categories[app.category] += 1
                subcategories[(app.category, app.subcategory)] += 1
                empty_rows.append([record_id, app.name, app.reason])
    all_apps = [app for paper_id in report.ordered() for app in report.apps[paper_id]]
    pairs = sum(len(app.lv5) for app in all_apps if app.mapped)
    per_count = Counter(len(app.lv5) for app in all_apps if app.mapped)
    metadata_papers = [paper_id for paper_id in report.ordered() if metadata_only(source.stage1[paper_id], source.stage3[paper_id])]
    without_apps = [paper_id for paper_id in report.ordered() if not report.apps[paper_id]]
    metadata_set = set(metadata_papers)
    metadata_without_apps = [paper_id for paper_id in without_apps if paper_id in metadata_set]
    metadata_notes = []
    for paper_id in metadata_papers:
        record = source.stage3[paper_id]
        answers = record.get("factor_answers") or {}
        own = record["thresholds"]["level5"]
        recognized = sorted(
            (name, answers[key]) for name in set(source.catalog_of(paper_id).values())
            for key in [f"metadata_identity_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]}"]
            if answers.get(key, 0.0) >= own
        )
        best = max((row["probability"] for row in record.get("target_app") or []), default=0.0)
        if recognized:
            metadata_notes.append((report.record_of(paper_id),
                                   f"제목·초록에서 등록 제품 {', '.join(f'{name}({_f2(value)})' for name, value in recognized)}을 알아봤지만 "
                                   f"맥락이 기준 미달(최고 확률 {_f2(best)})이라 Level-5 매핑 없음"))
    category_rows = [(None, "실패 사유", "세부", "건수")] + [
        (category, sub, count) for (category, sub), count in sorted(subcategories.items(), key=lambda item: (-categories[item[0][0]], -item[1]))
    ]
    in_registry = catalog_in_registry(catalog, report.registry)
    same_catalog = all(run.catalog == report.runs[0].catalog for run in report.runs)
    catalog_runs = ("" if not report.merged else " 모든 실행이 같은 카탈로그를 씀." if same_catalog
                    else " 실행마다 카탈로그가 달라, 논문마다 그 논문을 채점한 실행의 카탈로그로 판정함(위 수치는 합친 카탈로그 기준).")
    id_note = report.id_note()
    workbook = Workbook()
    _summary_sheet(workbook, [
        *report.source_sections("JevAI 대상 애플리케이션 Level-5 매핑"),
        ("판정 규칙", [
            ("Jev Level-5 카탈로그", f"{len(catalog)}개 노드 · {len(set(catalog.values()))}개 제품 · Level-3 {len({node[:10] for node in catalog})}개(클라우드 데이터 수집 COLL-02-04, "
                                    f"애플리케이션 아티팩트 조사 EXAM-04-01) · Level-4 {len({node.rsplit('-', 1)[0] for node in catalog})}개. "
                                    f"ID 끝자리는 2자리{'(' + in_registry + ')' if in_registry else ''}." + catalog_runs),
            ("확률", "추출된 앱 × 논문 속 쓰임(맥락) × 카탈로그 노드마다 세 요인의 최솟값: ① 같은 제품인가(identity) ② 그 노드의 Level-3 맥락인가(context) "
                    "③ 그 노드의 Level-4 범위인가(scope)"),
            ("매핑 기준", f"맥락 중 하나라도 최솟값이 {threshold} 이상(같으면 포함)인 노드를 그 앱의 LV5로 확정(맥락별 결과를 합침)"),
            ("실패 사유 분류", f"시스템 등록 X = 카탈로그의 어떤 제품과도 '같은 제품' 확률이 {threshold} 미만 / "
                            f"시스템에 등록은 되어있지만 = 같은 제품 확률은 {threshold} 이상인데 논문 속 맥락(Level-3)이나 범위(Level-4)가 등록 노드와 맞지 않음 / "
                            f"Threshold 만족X = 같은 제품이고 최종 확률이 {_f2(NEAR_MISS)}~{threshold} 사이인 근소 미달. "
                            "'참고: DF-Insight에는 …'은 프로젝트의 DF-Insight 등록 목록(이름·별칭 일치)으로 확인한 정보입니다."),
        ]),
        ("집계", [
            ("추출된 앱", f"{len(all_apps)}개(논문 {len(papers_with_apps)}편) → JevAI-application-list.xlsx"
             + report.per_run(lambda run, papers: f"{sum(len(report.apps[paper_id]) for paper_id in papers)}개·{sum(1 for paper_id in papers if report.apps[paper_id])}편")),
            ("매핑된 앱", f"{len(mapped_rows)}개(논문 {len(papers_mapped)}편) · Level-5 {pairs}건 · 앱당 Level-5 개수 "
                        + " · ".join(f"{count}개 {per_count[count]}" for count in sorted(per_count))
                        + report.per_run(lambda run, papers: f"{sum(app.mapped for paper_id in papers for app in report.apps[paper_id])}개·"
                                                             f"{sum(1 for paper_id in papers if paper_id in papers_mapped)}편")),
            ("매핑 실패 앱", f"{len(empty_rows)}개 → JevAI-application_empty-LV5.xlsx"
             + report.per_run(lambda run, papers: f"{sum(not app.mapped for paper_id in papers for app in report.apps[paper_id])}개")),
            ("앱이 없는 논문", f"{len(without_apps)}편 (추출된 대상 애플리케이션 없음 {len(without_apps) - len(metadata_without_apps)}편 · "
                            f"원문 없이 제목·초록만 있는 논문 {len(metadata_without_apps)}편은 앱 추출을 하지 않음)"),
        ]),
        ("매핑 실패 사유별 건수", category_rows),
        ("참고", [
            ("Target Application", "논문에서 추출된 이름 그대로입니다(결과 DB의 application_name은 카탈로그 제품명이라 다를 수 있음: 예 카카오톡 → KakaoTalk)."
             + ("".join(f" {record}의 앱 이름 '{_visible(raw)}'은 PDF 글꼴의 사용자 정의 문자 때문에 깨진 것이라 '{fixed}'로 표기." for record, raw, fixed in fixed_names))),
            ("논문 ID", "프로젝트 논문 번호(KNOW-DOMP). JevAI의 paper_id(ART…)와 1:1로 대응합니다." + (f" {id_note}." if id_note else "")),
            ("DF-Insight 등록 목록", report.registry_text()),
            *[(record, note) for record, note in metadata_notes],
        ]),
    ], first=True)
    _table_sheet(workbook, "Application 매핑", ("논문 ID", "Target Application", "LV5"), mapped_rows, (20, 36, 60))
    workbook.active = 0
    lv5_file = _save(workbook, out / FILES["app_lv5"])

    workbook = Workbook()
    _table_sheet(workbook, "매핑실패 Application", ("논문ID", "Target Application", "매핑실패사유"), empty_rows, (20, 36, 120), first=True)
    empty_file = _save(workbook, out / FILES["app_empty"])

    workbook = Workbook()
    _table_sheet(workbook, "Target Application", ("논문ID", "Target Application", "매핑 여부 (O/X)"), list_rows, (20, 40, 16), first=True)
    list_file = _save(workbook, out / FILES["app_list"])
    return lv5_file, empty_file, list_file


def build_entities(report: Report, out: Path) -> Path:
    from openpyxl import Workbook

    rows = []
    for paper_id in report.ordered():
        record_id = report.record_of(paper_id)
        for tag, probability in entities_of(report.source.stage1[paper_id], report.source.stage3[paper_id]):
            rows.append([record_id, entity_label(tag, report.source.stage3[paper_id]), probability])
    workbook = Workbook()
    _table_sheet(workbook, "Entity", ("논문ID", "Entity", "Probability"), rows, (20, 60, 12), probability_columns=(3,), first=True)
    return _save(workbook, out / FILES["entity"])


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def build_report(jsonl: Path | Sequence[Path], db: Path | None, *, use_registry: bool = True) -> Report:
    """``jsonl``: one run's file, or several files merged per paper (the latest run wins)."""

    paths = [Path(jsonl)] if isinstance(jsonl, (str, Path)) else [Path(path) for path in jsonl]
    source = load_runs(paths)
    papers = corpus_info()
    missing = sorted(set(source.stage1) - set(papers))
    if missing:
        raise JevAIError(f"papers not in the project corpus: {missing[:5]}")
    names = load_names(source.catalog)
    final, surfaces = load_db(db) if db else ({}, {})
    extraction = load_extractions(db) if db else {}
    df_insight = load_df_insight() if use_registry else None
    registry = df_insight.registry if df_insight else None
    apps: dict[str, list[Application]] = {}
    for paper_id, record in source.stage3.items():
        catalog = source.catalog_of(paper_id)
        threshold = record["thresholds"]["level5"]
        apps[paper_id] = applications_of(record, catalog)
        for app in apps[paper_id]:
            if not app.mapped:
                known = surfaces.get(paper_id, [])
                names_of_app = known[app.index] if app.index < len(known) else [app.raw_name]
                explain(app, threshold, names, catalog, registry_note(names_of_app or [app.raw_name], registry, catalog))
    check = check_against_db(source, apps, final) if db else None
    return Report(source, papers, apps, check, names, db, df_insight, extraction, sorted(set(papers) - set(source.stage1)))


def write_all(report: Report, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    return [*build_lv3(report, out), *build_applications(report, out), build_entities(report, out)]


def _latest(pattern: str) -> Path | None:
    found = sorted(JEVAI_DIR.glob(pattern))
    return found[-1] if found else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jev AI 매핑 점수(JSONL) → 결과 xlsx 6개")
    parser.add_argument("--jsonl", type=Path, nargs="+", default=None,
                        help="실행 기록. 기본: Analyzer/JevAI의 mapping_scores_*.jsonl 전부를 논문별로 합침(같은 논문은 가장 나중 실행의 결과). "
                             "파일 하나만 주면 그 실행만으로 생성")
    parser.add_argument("--db", type=Path, default=None,
                        help="검증·별칭용 결과 DB(합친 모든 논문을 담은 것). 기본: Analyzer/JevAI의 가장 최근 results_kci*.db")
    parser.add_argument("--no-db", action="store_true", help="결과 DB 없이 생성(검증 생략)")
    parser.add_argument("--no-registry", action="store_true", help="DF-Insight 등록 목록(실패 사유의 참고 정보) 없이 생성")
    parser.add_argument("--out", type=Path, default=JEVAI_DIR)
    args = parser.parse_args(argv)
    jsonl = list(args.jsonl or sorted(JEVAI_DIR.glob(RUN_PATTERN)))
    if not jsonl:
        parser.error("mapping_scores_*.jsonl 파일이 없습니다")
    db = None if args.no_db else (args.db or max(JEVAI_DIR.glob(DB_PATTERN), key=lambda path: path.stat().st_mtime, default=None))
    report = build_report(jsonl, db, use_registry=not args.no_registry)
    if report.check and not report.check.ok:
        print(f"[JEVAI] 결과 DB와 다른 판정 {len(report.check.mismatches)}건: {report.check.mismatches[:10]}", file=sys.stderr)
        return 1
    written = write_all(report, args.out)
    apps = [app for values in report.apps.values() for app in values]
    runs = " + ".join(f"{run.path.name} {len(report.source.papers_of(run))}편" for run in report.runs)
    print(f"[JEVAI] {runs}: 논문 {len(report.source.stage1)}편 · 앱 {len(apps)}개(매핑 {sum(app.mapped for app in apps)}) · "
          + (f"DB 검증({report.db_path.name}) {report.check.taxonomy}/{report.check.entity}/{report.check.target_app} of {report.check.papers}"
             if report.check else "DB 검증 생략"))
    if report.unnumbered():
        print(f"[JEVAI] {report.id_note()}")
    if report.not_scored:
        print(f"[JEVAI] 선별 논문 중 Jev 결과가 없는 논문 {len(report.not_scored)}편: {report.not_scored[:10]}", file=sys.stderr)
    if report.df_insight and (report.df_insight.adapted or report.df_insight.registry is None):
        print(f"[JEVAI] DF-Insight 등록 목록: {report.registry_text()}", file=sys.stderr)
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
