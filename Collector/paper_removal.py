#!/usr/bin/env python3
"""Remove reviewed-out papers from the collection, durably and reversibly.

A person sometimes decides that a selected paper does not belong in the
collection (the keyword filter let it through, but it is not about digital
forensics).  Removing it means three things:

1. it is written to ``Processor/refs/excluded_papers.json`` so that no later
   filtering run (incremental or full) selects it again;
2. its row leaves the ``kci_papers`` table (the raw KCI record in
   ``kci_corpus`` is kept);
3. everything produced for it — PDF, download record, extracted text, analysis
   evidence, Structure documents, profile MD — is *moved* to
   ``Analyzer/Removed/<source_id>/`` rather than deleted, and the PDF name
   index and the legacy keyword snapshot forget it.

Numbers (``paper_index`` / ``KNOW-DOMP-…``) stay sorted and without gaps: the
removed paper's number is recorded as ``former_…`` history and released, and
``python3 main.py -renumber`` closes the gap (``Analyzer/numbering.py``).  A
review workbook is matched by number *and* title, so one made before a
renumbering can never remove the wrong paper.

    python -m Collector.paper_removal --from-workbook "Analyzer/LV/(260920)-filter-lv.xlsx"
    python -m Collector.paper_removal --source-ids ART0000001,ART0000002 --reason "..."
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import unicodedata
import sys
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from PaperPipeline.paper_files import remove_paper_file_index_entries  # noqa: E402
from Storage.db import sqlite_database_path  # noqa: E402
from config import load_settings  # noqa: E402

ANALYZER_DIR = REPOSITORY_ROOT / "Analyzer"
MANIFEST_DIR = ANALYZER_DIR / "Manifest"
EXCLUSION_FILE = REPOSITORY_ROOT / "Processor" / "refs" / "excluded_papers.json"
REMOVED_DIR = ANALYZER_DIR / "Removed"
HIGHLIGHT_SUFFIX = "FFFF00"  # yellow marker in a review workbook


def load_excluded_papers(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or EXCLUSION_FILE
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {entry["source_id"]: entry for entry in payload.get("papers", [])}


def load_excluded_source_ids(path: Path | None = None) -> frozenset[str]:
    return frozenset(load_excluded_papers(path))


def highlighted_rows(workbook_path: Path) -> list[tuple[str, str]]:
    """``(record_id, title)`` of every row whose title cell carries the yellow marker."""

    from openpyxl import load_workbook

    marked: list[tuple[str, str]] = []
    for sheet in load_workbook(workbook_path).worksheets:
        header = [str(cell.value or "") for cell in sheet[1]]
        if "record_id" not in header or "title" not in header:
            continue
        record_column, title_column = header.index("record_id"), header.index("title")
        for row in sheet.iter_rows(min_row=2):
            fill = row[title_column].fill
            if fill is not None and fill.fill_type == "solid" and str(fill.fgColor.rgb).upper().endswith(HIGHLIGHT_SUFFIX):
                marked.append((str(row[record_column].value), str(row[title_column].value or "")))
    return marked


def highlighted_record_ids(workbook_path: Path) -> list[str]:
    return [record for record, _title in highlighted_rows(workbook_path)]


def _title_key(value: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKC", value).casefold() if character.isalnum())


def resolve_highlighted(workbook_path: Path, database: Path, profiles: dict[str, str]) -> list[str]:
    """Source ids of the highlighted rows — but only when the row's *title* is that paper's title.

    Numbers move when the corpus is renumbered, so a review workbook made
    before a renumbering names other papers by the same ids.  The title in the
    row must match the database title of the paper the id resolves to;
    otherwise nothing is removed and the mismatch is reported.
    """

    by_record = {record: source_id for source_id, record in profiles.items()}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        titles = {str(row[0]): [str(value) for value in row[1:] if value] for row in connection.execute("select source_id, title, title_kor, title_eng from kci_papers")}
    finally:
        connection.close()
    resolved: list[str] = []
    mismatched: list[str] = []
    for record, title in highlighted_rows(workbook_path):
        source_id = by_record.get(record)
        if source_id is None or _title_key(title) not in {_title_key(value) for value in titles.get(source_id, [])}:
            mismatched.append(f"{record} «{title[:40]}»")
            continue
        resolved.append(source_id)
    if mismatched:
        raise SystemExit(
            "워크북의 번호와 제목이 현재 논문 목록과 맞지 않습니다 (번호를 다시 매기기 전에 만든 워크북일 수 있습니다). "
            "최신 결과 워크북에서 다시 표시해 주세요: " + ", ".join(mismatched[:10])
        )
    return resolved


def _artifacts(source_id: str, pdf_filename: str | None) -> list[tuple[Path, str]]:
    """(existing path, name inside the archive) pairs for one paper."""

    candidates = [
        (ANALYZER_DIR / "Paper" / pdf_filename if pdf_filename else None, "paper.pdf"),
        (MANIFEST_DIR / f"{source_id}.download.json", "download.json"),
        (MANIFEST_DIR / f"{source_id}.txt", "text.txt"),
        (MANIFEST_DIR / "Lv3" / f"{source_id}.json", "lv3.json"),
        (MANIFEST_DIR / "Type" / f"{source_id}.json", "type.json"),
        (MANIFEST_DIR / "Analysis" / f"{source_id}.json", "analysis.json"),
        (ANALYZER_DIR / "Structure" / "kor" / f"{source_id}.md", "structure.kor.md"),
        (ANALYZER_DIR / "Structure" / "eng" / f"{source_id}.md", "structure.eng.md"),
        (ANALYZER_DIR / "Special-Issue" / "BibTex" / f"{source_id}.bib", "special_issue.bib"),
    ]
    return [(path, name) for path, name in candidates if path is not None and path.exists()]


def _write_exclusions(excluded: dict[str, dict[str, Any]]) -> None:
    """Atomically persist the review decisions; this file is their only record."""

    payload = json.dumps({
        "schema_version": 1,
        "description": "사람이 검토해 수집 대상에서 제외한 논문. 필터링 단계는 여기 적힌 source_id를 다시 선별하지 않습니다.",
        "papers": sorted(excluded.values(), key=lambda entry: entry["source_id"]),
    }, ensure_ascii=False, indent=2) + "\n"
    EXCLUSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = EXCLUSION_FILE.with_name(f".{EXCLUSION_FILE.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, EXCLUSION_FILE)


def _paper_index_of(source_id: str) -> int | None:
    """The number the paper has now, read from its evidence before that is archived."""

    for path in (MANIFEST_DIR / "Lv3" / f"{source_id}.json", MANIFEST_DIR / "Analysis" / f"{source_id}.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("paper_index")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, int) and value > 0:
            return value
    reservations = MANIFEST_DIR / "paper_index_reservations.json"
    try:
        value = json.loads(reservations.read_text(encoding="utf-8")).get("reservations", {}).get(source_id)
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, int) and value > 0 else None


def _profile_belongs_to(profile_dir: Path, source_id: str) -> bool:
    """A profile names its paper in the KCI URL; an id alone may belong to another paper after a renumbering."""

    try:
        return f"artiId={source_id}" in (profile_dir / "ko.md").read_text(encoding="utf-8")
    except OSError:
        return False


def _archive_artifacts(source_id: str, pdf_filename: str | None, record_id: str) -> None:
    archive = REMOVED_DIR / source_id
    archive.mkdir(parents=True, exist_ok=True)
    for path, name in _artifacts(source_id, pdf_filename):
        if not (archive / name).exists():
            shutil.move(str(path), str(archive / name))
    profile_dir = ANALYZER_DIR / "Paper" / "MD" / record_id if record_id else None
    if (profile_dir is not None and profile_dir.is_dir() and not (archive / "profile_md").exists()
            and _profile_belongs_to(profile_dir, source_id)):
        shutil.move(str(profile_dir), str(archive / "profile_md"))


def _release_numbers(removed: set[str]) -> None:
    """A removed paper keeps no live number: its profile id and reservation are dropped (renumbering closes the gap)."""

    for path, key in ((MANIFEST_DIR / "paper_profiles.json", "records"), (MANIFEST_DIR / "paper_index_reservations.json", "reservations")):
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get(key)
        if isinstance(entries, dict) and removed & set(entries):
            payload[key] = {source_id: value for source_id, value in entries.items() if source_id not in removed}
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)


def remove_papers(source_ids: Iterable[str], *, reason: str, reviewed_in: str | None, dry_run: bool = False) -> dict[str, Any]:
    """Remove reviewed-out papers, one paper at a time and in a crash-safe order.

    For each paper: archive the database row → record the exclusion (atomic
    write) → delete the row → move the artifacts.  If the run dies anywhere in
    between, the decision is already recorded, so the filter can never select
    the paper again, and running the same command again finishes the move:
    an already excluded paper is not an error, its leftovers are archived.
    """

    settings = load_settings(validate=False)
    database = sqlite_database_path(settings.database_url)
    wanted = sorted(set(source_ids))
    excluded = load_excluded_papers()
    index = json.loads((MANIFEST_DIR / "paper_files.json").read_text(encoding="utf-8"))
    filenames = {entry["source_id"]: entry["filename"] for entry in index["papers"]}
    profiles_path = MANIFEST_DIR / "paper_profiles.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8")).get("records", {}) if profiles_path.is_file() else {}

    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    report: dict[str, Any] = {"removed": [], "already_excluded": [], "not_selected": []}
    finished: set[str] = set()
    try:
        for source_id in wanted:
            row = connection.execute("select * from kci_papers where source_id = ?", (source_id,)).fetchone()
            record_id = profiles.get(source_id, "")
            if row is None:
                if source_id not in excluded:
                    report["not_selected"].append(source_id)
                    continue
                report["already_excluded"].append(source_id)
                if not dry_run:  # finish a removal that was interrupted after its decision was recorded
                    _archive_artifacts(source_id, filenames.get(source_id), record_id)
                    finished.add(source_id)
                continue
            entry = {
                # The numbers the paper had while it was in the corpus: history only, another paper takes them over.
                "source_id": source_id, "former_record_id": record_id, "former_paper_index": _paper_index_of(source_id),
                "title": row["title_kor"] or row["title"],
                "journal": row["journal_id"], "publication_year": row["publication_year"],
                "removed_on": date.today().isoformat(), "reason": reason, "reviewed_in": reviewed_in,
            }
            report["removed"].append(entry)
            if dry_run:
                continue
            archive = REMOVED_DIR / source_id
            archive.mkdir(parents=True, exist_ok=True)
            (archive / "kci_paper_row.json").write_text(json.dumps(dict(row), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
            excluded[source_id] = entry
            _write_exclusions(excluded)
            connection.execute("delete from kci_papers where source_id = ?", (source_id,))
            connection.commit()
            _archive_artifacts(source_id, filenames.get(source_id), record_id)
            finished.add(source_id)
    finally:
        connection.close()
    if dry_run or not finished:
        return report

    # The PDFs were moved above, so their index entries are now released.
    remove_paper_file_index_entries(MANIFEST_DIR / "paper_files.json", finished & set(filenames), papers_dir=ANALYZER_DIR / "Paper")
    snapshot_path = MANIFEST_DIR / "keyword_intelligence_legacy.json"
    if snapshot_path.is_file():
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        kept = [row for row in snapshot.get("rows", []) if row.get("source_id") not in finished]
        if len(kept) != len(snapshot.get("rows", [])):
            snapshot["rows"], snapshot["row_count"] = kept, len(kept)
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from Analyzer.evidence_writer import regenerate_structure_index

    regenerate_structure_index(ANALYZER_DIR / "Structure")
    _release_numbers(finished)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검토로 제외한 논문을 수집 대상에서 제거합니다 (산출물은 Analyzer/Removed로 이동)")
    parser.add_argument("--from-workbook", type=Path, help="제목 칸을 노란색으로 표시한 검토 워크북")
    parser.add_argument("--source-ids", default="", help="쉼표로 구분한 KCI source_id")
    parser.add_argument("--reason", default="사용자 검토: 디지털 포렌식 논문이 아니어서 제외")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    source_ids = {part.strip() for part in args.source_ids.split(",") if part.strip()}
    if args.from_workbook:
        profiles = json.loads((MANIFEST_DIR / "paper_profiles.json").read_text(encoding="utf-8"))["records"]
        database = Path(sqlite_database_path(load_settings(validate=False).database_url))
        source_ids.update(resolve_highlighted(args.from_workbook, database, profiles))
    if not source_ids:
        parser.error("제거할 논문이 없습니다")
    from contextlib import nullcontext

    from Util.run_lock import exclusive_run

    with nullcontext() if args.dry_run else exclusive_run("논문 제외"):
        report = remove_papers(source_ids, reason=args.reason, reviewed_in=str(args.from_workbook) if args.from_workbook else None, dry_run=args.dry_run)
    print(json.dumps({key: (len(value), [item["source_id"] if isinstance(item, dict) else item for item in value][:50]) for key, value in report.items()}, ensure_ascii=False, indent=1))
    if report["removed"] and not args.dry_run:
        print("다음 단계: `python3 main.py -renumber` — 빈 번호를 메워 번호를 다시 정렬하고 워크북·MD·그래프를 새로 만듭니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
