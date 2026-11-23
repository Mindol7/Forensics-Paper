"""Paper numbers (``paper_index`` / ``KNOW-DOMP-NNNNNNN``): kept sorted and without gaps.

Every selected paper has one number; its profile id is the same number
(``paper_index`` 44  <->  ``KNOW-DOMP-0000044``).  The order is fixed by rule,
not by arrival:

1. papers whose full-text PDF is on disk, by ascending KCI article id
   (``ART…``) — these are the papers that get a Markdown profile, so the
   profile folders are numbered 1, 2, 3 … with no holes;
2. then the papers without a PDF, by ascending KCI article id.

Numbers therefore change when the corpus changes shape: a paper is removed
after review, an older paper is selected late, or a paper without a PDF
finally gets one.  ``renumber()`` brings every store to the rule in one go and
is a no-op when the numbers already follow it; the pipeline runs it before the
analysis stage and ``python3 main.py -renumber`` runs it on demand.

Where a number lives (everything else is regenerated from these):

* ``Analyzer/Manifest/Lv3/<ART>.json``        ``paper_index`` of an analysed paper
* ``Analyzer/Manifest/Type/<ART>.json``       pins the SHA-256 of that Lv3 file
* ``Analyzer/Manifest/Analysis/<ART>.json``   ``paper_index`` + the two pins above
* ``Analyzer/Manifest/paper_index_reservations.json``  numbers of papers not analysed yet
* ``Analyzer/Manifest/paper_profiles.json``   ``source_id -> KNOW-DOMP-…``
* ``Analyzer/Paper/MD/<record id>/``          profiles; cleared when ids move, rebuilt by the MD stage

The three manifest files of a paper form a hash chain (Lv3 <- Type <- Analysis),
so they are rewritten together, byte-for-byte in the serialization the pipeline
itself uses, and only after the old pins were verified: a pin that is already
stale is evidence of a different problem and is never papered over.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PaperPipeline.storage import write_json_atomic, write_text_atomic

ANALYZER_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
RECORD_PREFIX = "KNOW-DOMP-"
RECORD_DIGITS = 7
RECORD_RE = re.compile(r"KNOW-DOMP-([0-9]{7})\Z")
SOURCE_RE = re.compile(r"ART[0-9]{9}\Z")
# Hand-written documents that link to profiles by record id; the link label starts with the KCI id.
PROFILE_LINK_RE = re.compile(
    r"\[(?P<source>ART[0-9]{9})(?P<label>[^\]]*)\]\((?P<prefix>(?:\.\./)+)(?:Paper/MD/KNOW-DOMP-[0-9]{7}|Removed/ART[0-9]{9}/profile_md)/(?P<file>ko|en)\.md\)"
)


class NumberingError(RuntimeError):
    """The stores disagree in a way renumbering must not hide."""


@dataclass(frozen=True, slots=True)
class Layout:
    """Where the numbers live; tests point this at a temporary tree."""

    root: Path = REPOSITORY_ROOT

    @property
    def manifest(self) -> Path:
        return self.root / "Analyzer" / "Manifest"

    @property
    def lv3(self) -> Path:
        return self.manifest / "Lv3"

    @property
    def type_decisions(self) -> Path:
        return self.manifest / "Type"

    @property
    def analysis(self) -> Path:
        return self.manifest / "Analysis"

    @property
    def reservations(self) -> Path:
        return self.manifest / "paper_index_reservations.json"

    @property
    def profiles(self) -> Path:
        return self.manifest / "paper_profiles.json"

    @property
    def history(self) -> Path:
        return self.manifest / "renumber_history.json"

    @property
    def journal(self) -> Path:
        return self.manifest / ".renumber-journal.json"

    @property
    def paper_files(self) -> Path:
        return self.manifest / "paper_files.json"

    @property
    def papers(self) -> Path:
        return self.root / "Analyzer" / "Paper"

    @property
    def profile_tree(self) -> Path:
        return self.papers / "MD"

    @property
    def excluded(self) -> Path:
        return self.root / "Processor" / "refs" / "excluded_papers.json"

    @property
    def linked_documents(self) -> tuple[Path, ...]:
        return (self.root / "Analyzer" / "Graph" / "audit" / "topic_similarity_review.md",)

    @property
    def numbered_workbooks(self) -> tuple[Path, ...]:
        """Workbooks no pipeline stage regenerates but which show a ``paper_index`` column next to ``source_id``."""

        return (self.root / "Analyzer" / "Intelligence" / "kci_paper_level3_classification.xlsx",)

    @property
    def backups(self) -> Path:
        return self.root / "reports" / "renumber"


@dataclass(slots=True)
class Plan:
    """Current and target number of every selected paper."""

    target: dict[str, int]                     # source_id -> number under the rule
    current_index: dict[str, int | None]       # what the manifests / reservations say today
    current_record: dict[str, str]             # what the profile index says today
    with_pdf: tuple[str, ...]
    without_pdf: tuple[str, ...]
    stale_profile_entries: tuple[str, ...]     # profile-index entries of papers no longer selected
    stale_reservations: tuple[str, ...] = ()
    problems: list[str] = field(default_factory=list)
    # Lv3 and Analysis disagree on the number: an error, except for the papers an interrupted run was rewriting.
    index_disagreements: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def changed(self) -> dict[str, tuple[int | None, int]]:
        return {
            source_id: (self.current_index.get(source_id), number)
            for source_id, number in self.target.items()
            if self.current_index.get(source_id) != number or self.current_record.get(source_id) != record_id(number)
        }

    @property
    def in_order(self) -> bool:
        return not self.changed and not self.stale_profile_entries and not self.stale_reservations


def record_id(number: int) -> str:
    return f"{RECORD_PREFIX}{number:0{RECORD_DIGITS}d}"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selected_source_ids(database: Path) -> list[str]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return sorted(str(row[0]) for row in connection.execute("select source_id from kci_papers"))
    finally:
        connection.close()


def _papers_with_pdf(layout: Layout) -> set[str]:
    if not layout.paper_files.is_file():
        return set()
    entries = _read(layout.paper_files).get("papers", [])
    return {
        str(entry["source_id"]) for entry in entries
        if isinstance(entry, dict) and entry.get("source_id") and entry.get("filename") and (layout.papers / entry["filename"]).is_file()
    }


def _manifest_index(path: Path) -> int | None:
    if not path.is_file():
        return None
    value = _read(path).get("paper_index")
    return value if type(value) is int and value > 0 else None


def build_plan(*, database: Path, layout: Layout | None = None) -> Plan:
    layout = layout or Layout()
    selected = _selected_source_ids(database)
    bad = [source_id for source_id in selected if not SOURCE_RE.fullmatch(source_id)]
    if bad:
        raise NumberingError(f"unexpected KCI ids in the database: {bad[:5]}")
    have_pdf = _papers_with_pdf(layout)
    with_pdf = tuple(source_id for source_id in selected if source_id in have_pdf)
    without_pdf = tuple(source_id for source_id in selected if source_id not in have_pdf)
    target = {source_id: number for number, source_id in enumerate((*with_pdf, *without_pdf), start=1)}

    reservations = _read(layout.reservations).get("reservations", {}) if layout.reservations.is_file() else {}
    records = _read(layout.profiles).get("records", {}) if layout.profiles.is_file() else {}
    current_index: dict[str, int | None] = {}
    problems: list[str] = []
    disagreements: dict[str, tuple[int, int]] = {}
    for source_id in selected:
        lv3 = _manifest_index(layout.lv3 / f"{source_id}.json")
        analysis = _manifest_index(layout.analysis / f"{source_id}.json")
        if lv3 is not None and analysis is not None and lv3 != analysis:
            disagreements[source_id] = (lv3, analysis)
        reserved = reservations.get(source_id)
        current_index[source_id] = lv3 if lv3 is not None else analysis if analysis is not None else reserved if type(reserved) is int else None
    orphans = sorted(path.stem for path in layout.lv3.glob("ART*.json") if path.stem not in target) if layout.lv3.is_dir() else []
    if orphans:
        excluded = set()
        if layout.excluded.is_file():
            excluded = {str(item.get("source_id")) for item in _read(layout.excluded).get("papers", []) if isinstance(item, dict)}
        interrupted = [source_id for source_id in orphans if source_id in excluded]
        unknown = [source_id for source_id in orphans if source_id not in excluded]
        if interrupted:
            problems.append(
                f"논문 제외가 중간에 끊겼습니다(분석 근거가 남아 있음): {interrupted[:5]} — "
                f"`python3 -m Collector.paper_removal --source-ids {','.join(interrupted[:5])}`를 다시 실행하면 마저 옮깁니다"
            )
        if unknown:
            problems.append(
                f"선별 목록(kci_papers)에 없는 논문의 분석 근거가 남아 있습니다: {unknown[:5]} — DB에서 직접 지운 논문이면 "
                "`python3 -m Collector.paper_removal --source-ids …`로 정식 제외하고, 아니면 `python3 main.py -filter`로 다시 선별하세요"
            )
    for path in sorted(layout.analysis.glob("ART*.json")) if layout.analysis.is_dir() else []:
        if path.stem in target and not (layout.lv3 / path.name).is_file():
            # The analysis stage prefers this record's number; without its Lv3 manifest it can never be rewritten consistently.
            problems.append(f"{path.stem}: 분석 기록(Analysis)은 있는데 Level-3 근거(Lv3)가 없습니다 — `python3 main.py -analyze --analysis-force`로 다시 분석하세요")
    return Plan(
        target=target,
        current_index=current_index,
        current_record={source_id: str(records[source_id]) for source_id in selected if source_id in records},
        with_pdf=with_pdf,
        without_pdf=without_pdf,
        stale_profile_entries=tuple(sorted(source_id for source_id in records if source_id not in target)),
        # A reservation is wrong when it names a paper that is not selected or another number than
        # the rule gives.  The entry of a paper that has been analysed since (same number) is only a
        # leftover: it is tidied with the next real change and never reported as "out of order".
        stale_reservations=tuple(sorted(
            source_id for source_id, number in reservations.items()
            if source_id not in target or number != target[source_id]
        )),
        problems=problems,
        index_disagreements=disagreements,
    )


# --------------------------------------------------------------------------- #
# Applying the plan
# --------------------------------------------------------------------------- #

def _verify_chain(layout: Layout, source_id: str, accepted_lv3_sha: str | None, before_index: int | None = None) -> None:
    """The Type decision must pin the Lv3 file as it is now (or as the interrupted run left it)."""

    lv3_path, type_path = layout.lv3 / f"{source_id}.json", layout.type_decisions / f"{source_id}.json"
    if not type_path.is_file():
        raise NumberingError(f"{source_id}: analysed paper without a Type decision file ({type_path.name}); run the type stage first")
    pinned = _read(type_path).get("lv3_manifest_sha256")
    valid = isinstance(pinned, str) and pinned == _sha256(lv3_path)
    if not valid and isinstance(pinned, str) and isinstance(accepted_lv3_sha, str) and pinned == accepted_lv3_sha and before_index is not None:
        # Interrupted run: the Lv3 file already carries the new number.  The old pin is only honoured
        # if the file, with the old number put back, is byte-for-byte what was pinned.
        manifest = _read(lv3_path)
        manifest["paper_index"] = before_index
        restored = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
        valid = hashlib.sha256(restored).hexdigest() == accepted_lv3_sha
    if not valid:
        raise NumberingError(
            f"{source_id}: the Type decision does not belong to the current Level-3 evidence (stale pin); "
            "this is not a numbering problem — fix it before renumbering"
        )


def _rewrite_chain(layout: Layout, source_id: str, number: int) -> bool:
    """Set the number in Lv3 -> re-pin Type -> update Analysis.  Returns whether anything changed."""

    lv3_path = layout.lv3 / f"{source_id}.json"
    type_path = layout.type_decisions / f"{source_id}.json"
    analysis_path = layout.analysis / f"{source_id}.json"
    changed = False
    manifest = _read(lv3_path)
    if manifest.get("paper_index") != number:
        manifest["paper_index"] = number
        write_json_atomic(lv3_path, manifest)
        changed = True
    decision = _read(type_path)
    lv3_sha = _sha256(lv3_path)
    if decision.get("lv3_manifest_sha256") != lv3_sha:
        decision["lv3_manifest_sha256"] = lv3_sha
        write_json_atomic(type_path, decision)
        changed = True
    if analysis_path.is_file():
        record = _read(analysis_path)
        artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), dict) else None
        wanted = {"lv3_manifest_sha256": lv3_sha, "type_decision_sha256": _sha256(type_path)}
        if record.get("paper_index") != number or (artifacts is not None and any(artifacts.get(key) != value for key, value in wanted.items() if key in artifacts)):
            record["paper_index"] = number
            if artifacts is not None:
                for key, value in wanted.items():
                    if key in artifacts:
                        artifacts[key] = value
            write_json_atomic(analysis_path, record)
            changed = True
    return changed


def _retire_numbers_of_removed_papers(layout: Layout) -> None:
    """The numbers a removed paper once had are history: keep them, but under keys nothing reads as live ids."""

    if not layout.excluded.is_file():
        return
    payload = _read(layout.excluded)
    touched = False
    for entry in payload.get("papers", []):
        if not isinstance(entry, dict):
            continue
        for key, former in (("paper_index", "former_paper_index"), ("record_id", "former_record_id")):
            if key in entry:
                value = entry.pop(key)
                entry.setdefault(former, value)
                touched = True
    if touched:
        write_json_atomic(layout.excluded, payload)


def _relink_documents(layout: Layout, target: Mapping[str, int]) -> int:
    rewritten = 0
    for path in layout.linked_documents:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")

        def replace(match: re.Match[str]) -> str:
            source_id = match.group("source")
            head = f"[{source_id}{match.group('label')}]({match.group('prefix')}"
            if source_id in target:
                return f"{head}Paper/MD/{record_id(target[source_id])}/{match.group('file')}.md)"
            return f"{head}Removed/{source_id}/profile_md/{match.group('file')}.md)"  # removed after review: archived profile

        updated = PROFILE_LINK_RE.sub(replace, text)
        if updated != text:
            write_text_atomic(path, updated)
            rewritten += 1
    return rewritten


RESERVATION_NOTE = (
    "아직 분석 근거(Lv3)가 없는 선별 논문의 번호. 번호는 Analyzer/numbering.py의 규칙(PDF가 있는 논문을 KCI 논문 ID 순으로, "
    "그 뒤에 PDF가 없는 논문)으로 정해지며 코퍼스가 바뀌면 다시 매겨집니다."
)


def _write_reservations(layout: Layout, plan: Plan, analysed: set[str], moment: datetime) -> None:
    waiting = {source_id: number for source_id, number in plan.target.items() if source_id not in analysed}
    write_json_atomic(layout.reservations, {
        "schema_version": 1,
        "description": RESERVATION_NOTE,
        "reserved_on": moment.date().isoformat(),
        "reservations": dict(sorted(waiting.items(), key=lambda item: item[1])),
    })


def _write_profiles(layout: Layout, plan: Plan) -> None:
    write_json_atomic(layout.profiles, {
        "schema_version": 1,
        "records": {source_id: record_id(number) for source_id, number in sorted(plan.target.items())},
    })


def _sync_numbered_workbooks(layout: Layout, target: Mapping[str, int]) -> int:
    """Set the ``paper_index`` column of frozen workbooks from ``source_id``; a paper that left the corpus has no number."""

    from openpyxl import load_workbook

    updated = 0
    for path in layout.numbered_workbooks:
        if not path.is_file():
            continue
        workbook = load_workbook(path)
        touched = False
        for sheet in workbook.worksheets:
            header = [cell.value for cell in sheet[1]]
            if "paper_index" not in header or "source_id" not in header:
                continue
            number_column, source_column = header.index("paper_index") + 1, header.index("source_id") + 1
            for row in range(2, sheet.max_row + 1):
                source_id = sheet.cell(row=row, column=source_column).value
                wanted = target.get(str(source_id)) if source_id else None
                cell = sheet.cell(row=row, column=number_column)
                if source_id and cell.value != wanted:
                    cell.value = wanted
                    touched = True
        if touched:
            temporary = path.with_name(f".{path.name}.tmp")
            workbook.save(temporary)
            temporary.replace(path)
            updated += 1
    return updated


def _backup(layout: Layout, stamp: str) -> Path:
    destination = layout.backups / stamp
    attempt = 1
    while destination.exists():  # a resumed run and today's run can start within the same second
        attempt += 1
        destination = layout.backups / f"{stamp}-{attempt}"
    destination.mkdir(parents=True)
    archive = destination / "state-before.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in (layout.lv3, layout.type_decisions, layout.analysis, layout.reservations, layout.profiles, layout.excluded,
                     layout.profile_tree, *layout.linked_documents, *layout.numbered_workbooks):
            if path.exists():
                tar.add(path, arcname=str(path.relative_to(layout.root)))
    return archive


BACKUPS_KEPT = 12
RECORD_TOKEN_RE = re.compile(r"KNOW-DOMP-[0-9]{7}")


def _clear_moved_profiles(layout: Layout, target: Mapping[str, int], before: Mapping[str, Mapping[str, Any]]) -> int:
    """Remove the profile folders that would now be wrong; keep the ones that are still right.

    A folder is still right when the paper that owned its id keeps that id *and* the profile
    mentions no id that moved (the reference table links cited papers by id).  In the usual
    monthly case — new papers appended — that is every folder, so nothing is deleted and a
    later failure of the MD stage cannot leave the corpus without profiles.
    """

    if not layout.profile_tree.is_dir():
        return 0
    keeps = {record_id(number) for source_id, number in target.items() if before.get(source_id, {}).get("record_id") == record_id(number)}
    moved = {str(entry.get("record_id")) for source_id, entry in before.items()
             if entry.get("record_id") and entry.get("record_id") != (record_id(target[source_id]) if source_id in target else None)}
    cleared = 0
    for directory in sorted(layout.profile_tree.glob(f"{RECORD_PREFIX}*")):
        if directory.is_symlink() or not directory.is_dir() or not RECORD_RE.fullmatch(directory.name):
            continue
        keep = directory.name in keeps
        if keep and moved:
            for name in ("ko.md", "en.md"):
                try:
                    mentioned = set(RECORD_TOKEN_RE.findall((directory / name).read_text(encoding="utf-8"))) - {directory.name}
                except OSError:
                    keep = False
                    break
                if mentioned & moved:
                    keep = False
                    break
        if not keep:
            shutil.rmtree(directory)
            cleared += 1
    return cleared


def _prune_backups(layout: Layout) -> None:
    if not layout.backups.is_dir():
        return
    runs = sorted(path for path in layout.backups.iterdir() if path.is_dir() and re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:-[0-9]+)?", path.name))
    for old in runs[:-BACKUPS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)


def _apply_journal(layout: Layout, journal: Mapping[str, Any]) -> dict[str, Any]:
    """Carry out (or finish) exactly the renumbering the journal describes; every step is idempotent."""

    target: dict[str, int] = dict(journal["target"])
    before: dict[str, dict[str, Any]] = dict(journal["before"])
    accepted: dict[str, str] = dict(journal.get("lv3_sha256_before", {}))
    moment = datetime.fromisoformat(journal["started_at"])
    # Papers may have left the corpus since an interrupted run was planned; only evidence that still exists is rewritten.
    analysed = [source_id for source_id in target if (layout.lv3 / f"{source_id}.json").is_file()]
    for source_id in analysed:
        _verify_chain(layout, source_id, accepted.get(source_id), before.get(source_id, {}).get("paper_index"))

    result: dict[str, Any] = {"backup": journal["backup"]}
    # 1) the per-paper hash chain
    result["manifests_rewritten"] = sum(_rewrite_chain(layout, source_id, target[source_id]) for source_id in analysed)
    # 2) numbers of papers that are not analysed yet (no PDF, or waiting for the analysis stage)
    waiting = {source_id: number for source_id, number in target.items() if source_id not in set(analysed)}
    write_json_atomic(layout.reservations, {
        "schema_version": 1, "description": RESERVATION_NOTE, "reserved_on": moment.date().isoformat(),
        "reservations": dict(sorted(waiting.items(), key=lambda item: item[1])),
    })
    # 3) profile ids = numbers; entries of papers that are no longer selected would collide with live ids
    write_json_atomic(layout.profiles, {"schema_version": 1, "records": {source_id: record_id(number) for source_id, number in sorted(target.items())}})
    _retire_numbers_of_removed_papers(layout)
    result["documents_relinked"] = _relink_documents(layout, target)
    result["workbooks_synced"] = _sync_numbered_workbooks(layout, target)
    # 4) profiles carry ids inside (identifier, reference links): wrong ones are removed for the MD stage to rebuild
    result["profiles_cleared"] = _clear_moved_profiles(layout, target, before)

    history = _read(layout.history) if layout.history.is_file() else {"schema_version": 1, "description": "번호를 다시 매긴 기록. 이전 번호로 적어 둔 메모를 새 번호로 옮길 때 씁니다.", "runs": []}
    if not any(run.get("backup") == journal["backup"] for run in history["runs"]):  # a crash right after this write must not record the run twice
        history["runs"].append({
            "renumbered_at": journal["started_at"], "backup": journal["backup"], "paper_count": len(target),
            "changed": [
                {"source_id": source_id, "from_paper_index": before.get(source_id, {}).get("paper_index"),
                 "from_record_id": before.get(source_id, {}).get("record_id", ""), "to_paper_index": number, "to_record_id": record_id(number)}
                for source_id, number in sorted(target.items(), key=lambda item: item[1])
                if before.get(source_id, {}).get("paper_index") != number or before.get(source_id, {}).get("record_id") != record_id(number)
            ],
        })
        write_json_atomic(layout.history, history)
    layout.journal.unlink()
    _prune_backups(layout)
    return result


def renumber(*, database: Path, layout: Layout | None = None, dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Bring every store to the numbering rule.  Safe to run again after an interruption."""

    layout = layout or Layout()
    resumed: dict[str, Any] | None = None
    if layout.journal.is_file():
        if dry_run:
            return {"in_order": False, "interrupted": True, "changed_count": 0, "new_count": 0, "moved_count": 0, "first_changes": [],
                    "note": "중단된 번호 정렬이 남아 있습니다. `python3 main.py -renumber`를 실행하면 이어서 마칩니다."}
        # First finish what was started, exactly as planned then; afterwards plan again for today's corpus
        # (it may have changed in between: a removal, a new PDF).
        resumed = _apply_journal(layout, _read(layout.journal))

    plan = build_plan(database=database, layout=layout)
    plan.problems.extend(f"{source_id}: Lv3 paper_index {a} != Analysis paper_index {b}" for source_id, (a, b) in plan.index_disagreements.items())
    if plan.problems:
        raise NumberingError("; ".join(plan.problems))
    analysed = [source_id for source_id in plan.target if (layout.lv3 / f"{source_id}.json").is_file()]
    for source_id in analysed:
        _verify_chain(layout, source_id, None)

    new_papers = [source_id for source_id, (old, _new) in plan.changed.items() if old is None]
    report: dict[str, Any] = {
        "paper_count": len(plan.target), "with_pdf": len(plan.with_pdf), "without_pdf": len(plan.without_pdf),
        "changed_count": len(plan.changed), "new_count": len(new_papers), "moved_count": len(plan.changed) - len(new_papers),
        "in_order": plan.in_order and resumed is None,
        "stale_profile_entries": len(plan.stale_profile_entries), "first_changes": [
            {"source_id": source_id, "from": old, "to": new} for source_id, (old, new) in list(sorted(plan.changed.items(), key=lambda item: item[1][1]))[:10]
        ],
    }
    if resumed is not None:
        report.update(resumed)
        report["resumed"] = True
    if dry_run or plan.in_order:
        return report

    moment = now or datetime.now().astimezone()
    if not plan.changed:
        # No paper changes its number: only leftovers to tidy (the profile entry of a removed paper,
        # a reservation that names a paper that is gone).  Profiles stay where they are.
        _write_reservations(layout, plan, set(analysed), moment)
        _write_profiles(layout, plan)
        _retire_numbers_of_removed_papers(layout)
        report["housekeeping_only"] = True
        verify(database=database, layout=layout)
        return report

    backup = _backup(layout, moment.strftime("%Y%m%dT%H%M%S"))
    write_json_atomic(layout.journal, {
        "started_at": moment.isoformat(timespec="seconds"), "backup": str(backup), "target": plan.target,
        "lv3_sha256_before": {source_id: _sha256(layout.lv3 / f"{source_id}.json") for source_id in analysed},
        "before": {source_id: {"paper_index": plan.current_index.get(source_id), "record_id": plan.current_record.get(source_id, "")} for source_id in plan.target},
    })
    applied = _apply_journal(layout, _read(layout.journal))
    report.update({key: (report.get(key, 0) + value if key == "profiles_cleared" and resumed else value) for key, value in applied.items()})
    verify(database=database, layout=layout)
    return report


def verify(*, database: Path, layout: Layout | None = None) -> Plan:
    """Raise unless every store follows the rule (used after renumbering and by the tests)."""

    layout = layout or Layout()
    if layout.journal.is_file():
        raise NumberingError("an interrupted renumbering is pending; run `python3 main.py -renumber` to finish it")
    plan = build_plan(database=database, layout=layout)
    plan.problems.extend(f"{source_id}: Lv3 paper_index {a} != Analysis paper_index {b}" for source_id, (a, b) in plan.index_disagreements.items())
    if plan.problems or not plan.in_order:
        raise NumberingError(f"numbers are not in order: problems={plan.problems[:3]} changed={list(plan.changed.items())[:3]} "
                             f"stale_profiles={plan.stale_profile_entries[:3]} stale_reservations={plan.stale_reservations[:3]}")
    for source_id in plan.target:
        if (layout.lv3 / f"{source_id}.json").is_file():
            _verify_chain(layout, source_id, None)
    return plan


def numbering_health(*, database: Path, layout: Layout | None = None) -> dict[str, Any]:
    """A cheap, never-raising summary for the collection tracker."""

    layout = layout or Layout()
    if layout.journal.is_file():
        return {"in_order": False, "reason": "중단된 번호 정렬이 남아 있습니다"}
    try:
        plan = build_plan(database=database, layout=layout)
    except Exception as exc:  # noqa: BLE001 - reporting only
        return {"in_order": False, "reason": f"{type(exc).__name__}: {exc}"}
    problems = [*plan.problems, *(f"{source_id}: Lv3 {a} != Analysis {b}" for source_id, (a, b) in plan.index_disagreements.items())]
    return {"in_order": plan.in_order and not problems, "papers_to_renumber": len(plan.changed), "problems": problems[:3]}


def main(argv: Sequence[str] | None = None) -> int:
    from Storage.db import sqlite_database_path
    from config import load_settings

    parser = argparse.ArgumentParser(description="논문 번호(paper_index / KNOW-DOMP-…)를 규칙대로 정렬합니다: PDF가 있는 논문을 KCI 논문 ID 순으로 1번부터, 그 뒤에 PDF가 없는 논문.")
    parser.add_argument("--dry-run", action="store_true", help="바뀔 번호만 보여 주고 아무것도 고치지 않음")
    args = parser.parse_args(argv)
    from contextlib import nullcontext

    from Util.run_lock import exclusive_run

    database = Path(sqlite_database_path(load_settings(validate=False).database_url))
    with nullcontext() if args.dry_run else exclusive_run("논문 번호 정렬"):
        report = renumber(database=database, dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if not args.dry_run and not report["in_order"]:
        print("번호를 다시 매겼습니다. 이어서 `python3 main.py -derive -md -graph`로 워크북·MD·그래프를 새 번호로 다시 만드세요 (`python3 main.py -renumber`는 이 과정을 한 번에 합니다).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
