"""Build the keyword-intelligence workbooks from legacy rows and analysis records.

``Analyzer/Intelligence/kci_keyword_intelligence_with_app_category.xlsx`` was
authored once for the reviewed corpus (rows keyed only by DOI/title).  This
module regenerates it deterministically from two sources:

* ``Analyzer/Manifest/keyword_intelligence_legacy.json`` - a one-time snapshot
  of those reviewed rows with the ``source_id`` each row was joined to;
* ``Analyzer/Manifest/Analysis/<ART>.json`` - the Claude analysis records of
  incrementally collected papers (the five keyword axes plus application
  categories from the fixed vocabulary).

The output keeps the historical columns and adds ``source_id`` first, so the
MD generator joins by identity instead of by DOI/title heuristics.  A paper
present in both sources follows its analysis record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook


ANALYZER_DIR = Path(__file__).resolve().parent
DEFAULT_LEGACY_SNAPSHOT = ANALYZER_DIR / "Manifest" / "keyword_intelligence_legacy.json"
DEFAULT_ANALYSIS_DIR = ANALYZER_DIR / "Manifest" / "Analysis"
DEFAULT_OUTPUT = (
    ANALYZER_DIR / "Intelligence" / "kci_keyword_intelligence_with_app_category.xlsx"
)
DEFAULT_BASE_OUTPUT = ANALYZER_DIR / "Intelligence" / "kci_keyword_intelligence.xlsx"
SHEET_NAME = "keyword_intelligence"
SNAPSHOT_SCHEMA_VERSION = 1
COLUMNS: tuple[str, ...] = (
    "source_id",
    "doi",
    "title",
    "target_device",
    "target_os",
    "target_app",
    "target_app_category",
    "filesystem",
    "cloud",
)
BASE_COLUMNS: tuple[str, ...] = tuple(
    column for column in COLUMNS if column != "target_app_category"
)
LIST_SEPARATOR = ", "
CATEGORY_SEPARATOR = " | "
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class KeywordWorkbookError(RuntimeError):
    """The keyword rows cannot be assembled or written consistently."""


@dataclass(frozen=True, slots=True)
class KeywordRow:
    source_id: str
    doi: str | None
    title: str
    target_device: str | None
    target_os: str | None
    target_app: str | None
    target_app_category: str | None
    filesystem: str | None
    cloud: str | None
    origin: str  # "legacy" | "analysis"

    def cells(self, columns: Sequence[str]) -> list[Any]:
        values = asdict(self)
        return [_excel_safe(values[column]) for column in columns]


@dataclass(frozen=True, slots=True)
class KeywordWorkbookResult:
    path: Path
    base_path: Path | None
    row_count: int
    legacy_count: int
    analysis_count: int
    missing_source_ids: tuple[str, ...]


def _excel_safe(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text[:32_767]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _joined(values: Iterable[Any], separator: str = LIST_SEPARATOR) -> str | None:
    items: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in items:
            items.append(text)
    return separator.join(items) if items else None


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeywordWorkbookError(f"unable to read {path}: {exc}") from exc


def load_legacy_snapshot(path: Path = DEFAULT_LEGACY_SNAPSHOT) -> dict[str, KeywordRow]:
    """Read the reviewed rows; absent snapshot means no legacy rows."""

    if not Path(path).is_file():
        return {}
    payload = _read_json(Path(path))
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise KeywordWorkbookError(f"{path}: unsupported snapshot schema")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise KeywordWorkbookError(f"{path}: rows must be a list")
    result: dict[str, KeywordRow] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise KeywordWorkbookError(f"{path}: row must be an object")
        source_id = _text(item.get("source_id"))
        title = _text(item.get("title"))
        if not source_id or not title:
            raise KeywordWorkbookError(f"{path}: row lacks source_id or title")
        if source_id in result:
            raise KeywordWorkbookError(f"{path}: duplicate source_id {source_id}")
        result[source_id] = KeywordRow(
            source_id=source_id,
            doi=_text(item.get("doi")),
            title=title,
            target_device=_text(item.get("target_device")),
            target_os=_text(item.get("target_os")),
            target_app=_text(item.get("target_app")),
            target_app_category=_text(item.get("target_app_category")),
            filesystem=_text(item.get("filesystem")),
            cloud=_text(item.get("cloud")),
            origin="legacy",
        )
    return result


def row_from_analysis_record(record: Mapping[str, Any]) -> KeywordRow:
    """Project one Claude analysis record onto the workbook columns."""

    source_id = _text(record.get("source_id"))
    title = _text(record.get("title"))
    keywords = record.get("keywords")
    if not source_id or not title or not isinstance(keywords, dict):
        raise KeywordWorkbookError(f"analysis record {source_id!r} lacks title or keywords")
    applications = keywords.get("application") or []
    if not isinstance(applications, list):
        raise KeywordWorkbookError(f"{source_id}: keywords.application must be a list")
    names = [item.get("name") for item in applications if isinstance(item, dict)]
    categories = [item.get("category") for item in applications if isinstance(item, dict)]
    return KeywordRow(
        source_id=source_id,
        doi=_text(record.get("doi")),
        title=title,
        target_device=_joined(keywords.get("device") or []),
        target_os=_joined(keywords.get("operating_system") or []),
        target_app=_joined(names),
        target_app_category=_joined(categories, CATEGORY_SEPARATOR),
        filesystem=_joined(keywords.get("filesystem") or []),
        cloud=_joined(keywords.get("cloud") or []),
        origin="analysis",
    )


def load_analysis_rows(directory: Path = DEFAULT_ANALYSIS_DIR) -> dict[str, KeywordRow]:
    analysis_dir = Path(directory)
    if not analysis_dir.is_dir():
        return {}
    result: dict[str, KeywordRow] = {}
    for path in sorted(analysis_dir.glob("ART*.json")):
        record = _read_json(path)
        if not isinstance(record, dict) or record.get("dry_run"):
            continue
        if record.get("source_id") != path.stem:
            raise KeywordWorkbookError(f"{path}: analysis record source_id mismatch")
        result[path.stem] = row_from_analysis_record(record)
    return result


def assemble_rows(
    legacy: Mapping[str, KeywordRow],
    analysis: Mapping[str, KeywordRow],
    *,
    source_ids: Sequence[str] | None = None,
) -> tuple[list[KeywordRow], tuple[str, ...]]:
    """Merge the sources; analysis wins.  Returns rows and missing corpus ids."""

    merged: dict[str, KeywordRow] = dict(legacy)
    merged.update(analysis)
    if source_ids is None:
        ordered = sorted(merged)
        missing: tuple[str, ...] = ()
    else:
        wanted = list(dict.fromkeys(source_ids))
        missing = tuple(source_id for source_id in wanted if source_id not in merged)
        ordered = [source_id for source_id in wanted if source_id in merged]
        ordered += sorted(set(merged) - set(wanted))
    return [merged[source_id] for source_id in ordered], missing


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def _atomic_save(workbook: Workbook, destination: Path, *, expected_rows: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = destination.stat().st_mode & 0o777 if destination.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".xlsx", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        workbook.save(temporary)
        workbook.close()
        verification = load_workbook(temporary, read_only=True, data_only=True)
        try:
            if verification.sheetnames != [SHEET_NAME]:
                raise KeywordWorkbookError("workbook sheet verification failed")
            row_count = sum(1 for _ in verification[SHEET_NAME].iter_rows()) - 1
            if row_count != expected_rows:
                raise KeywordWorkbookError(
                    f"workbook row verification failed: {row_count} != {expected_rows}"
                )
        finally:
            verification.close()
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_workbook(rows: Sequence[KeywordRow], destination: Path, *, columns: Sequence[str]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_NAME
    sheet.append(list(columns))
    for row in rows:
        sheet.append(row.cells(columns))
    sheet.freeze_panes = "A2"
    _atomic_save(workbook, destination, expected_rows=len(rows))


def build_keyword_workbooks(
    *,
    legacy_snapshot: Path = DEFAULT_LEGACY_SNAPSHOT,
    analysis_dir: Path = DEFAULT_ANALYSIS_DIR,
    output: Path = DEFAULT_OUTPUT,
    base_output: Path | None = DEFAULT_BASE_OUTPUT,
    source_ids: Sequence[str] | None = None,
    strict: bool = False,
) -> KeywordWorkbookResult:
    """Write the category workbook (and the plain one) for every known paper."""

    legacy = load_legacy_snapshot(legacy_snapshot)
    analysis = load_analysis_rows(analysis_dir)
    rows, missing = assemble_rows(legacy, analysis, source_ids=source_ids)
    if not rows:
        raise KeywordWorkbookError("no keyword rows are available")
    if strict and missing:
        raise KeywordWorkbookError(f"papers without keyword rows: {list(missing[:10])}")
    write_workbook(rows, Path(output), columns=COLUMNS)
    if base_output is not None:
        write_workbook(rows, Path(base_output), columns=BASE_COLUMNS)
    return KeywordWorkbookResult(
        path=Path(output),
        base_path=Path(base_output) if base_output is not None else None,
        row_count=len(rows),
        legacy_count=sum(row.origin == "legacy" for row in rows),
        analysis_count=sum(row.origin == "analysis" for row in rows),
        missing_source_ids=missing,
    )


# --------------------------------------------------------------------------- #
# One-time legacy snapshot
# --------------------------------------------------------------------------- #

def snapshot_legacy_workbook(
    destination: Path = DEFAULT_LEGACY_SNAPSHOT,
    *,
    workbook_path: Path = DEFAULT_OUTPUT,
) -> int:
    """Freeze the reviewed rows with the ``source_id`` the MD generator joined.

    Uses the MD generator's own DOI/title join so the snapshot records exactly
    the pairing that produced the published profiles.  Refuses to overwrite an
    existing snapshot.
    """

    if Path(destination).exists():
        raise KeywordWorkbookError(f"snapshot already exists: {destination}")
    from Analyzer.MD import paper_md

    types = paper_md._load_types()
    joined = paper_md._join_keywords_by_doi_title(types, workbook_path=Path(workbook_path))
    rows = [
        {
            "source_id": record.source_id,
            "doi": joined[record.source_id].doi,
            "title": joined[record.source_id].title,
            "target_device": joined[record.source_id].target_device,
            "target_os": joined[record.source_id].target_os,
            "target_app": joined[record.source_id].target_app,
            "target_app_category": joined[record.source_id].target_app_category,
            "filesystem": joined[record.source_id].filesystem,
            "cloud": joined[record.source_id].cloud,
        }
        for record in types
    ]
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source_workbook": Path(workbook_path).name,
        "source_workbook_sha256": hashlib.sha256(Path(workbook_path).read_bytes()).hexdigest(),
        "snapshot_on": date.today().isoformat(),
        "row_count": len(rows),
        "rows": rows,
    }
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{Path(destination).stem}.", suffix=".json", dir=Path(destination).parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-snapshot", type=Path, default=DEFAULT_LEGACY_SNAPSHOT)
    parser.add_argument("--analysis-dir", type=Path, default=DEFAULT_ANALYSIS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-output", type=Path, default=DEFAULT_BASE_OUTPUT)
    parser.add_argument("--no-base-output", action="store_true")
    parser.add_argument(
        "--snapshot-legacy",
        action="store_true",
        help="Create the legacy snapshot from the current workbook instead of building.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.snapshot_legacy:
            count = snapshot_legacy_workbook(args.legacy_snapshot, workbook_path=args.output)
            print(f"[DONE] snapshot rows={count} -> {args.legacy_snapshot}")
            return 0
        result = build_keyword_workbooks(
            legacy_snapshot=args.legacy_snapshot,
            analysis_dir=args.analysis_dir,
            output=args.output,
            base_output=None if args.no_base_output else args.base_output,
        )
    except KeywordWorkbookError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(
        f"[DONE] rows={result.row_count} legacy={result.legacy_count} "
        f"analysis={result.analysis_count} -> {result.path}"
    )
    return 0


__all__ = [
    "BASE_COLUMNS",
    "COLUMNS",
    "DEFAULT_LEGACY_SNAPSHOT",
    "DEFAULT_OUTPUT",
    "KeywordRow",
    "KeywordWorkbookError",
    "KeywordWorkbookResult",
    "assemble_rows",
    "build_keyword_workbooks",
    "load_analysis_rows",
    "load_legacy_snapshot",
    "row_from_analysis_record",
    "snapshot_legacy_workbook",
    "write_workbook",
]


if __name__ == "__main__":
    raise SystemExit(main())
