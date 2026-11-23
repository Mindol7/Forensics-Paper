"""Target applications per paper -> ``Analyzer/Application/application_list.xlsx``.

Since the v5.2 rewrite the workbook is written by the Level-5 stage
(``Analyzer/Application/level5.py``) from ``Analyzer/Application/targets.json``,
the same source the profiles' "4. 대상 애플리케이션" table is rendered from.
The Level-5 mapping is done once per input so the inputs can be compared paper
by paper; the target applications themselves are the same for the three.
Five sheets:

    개요            what each sheet and cell value means, counts per input, agreement between inputs
    PDF-Based       ID | Target Application | Level-5   (the full PDF text)
    Abstract-Based  ID | Target Application | Level-5   (the abstract only)
    Summary-Based   ID | Target Application | Level-5   (the structured summary of the PDF text only)
    종합본          ID | Target Application | PDF Level-5 | Abstract Level-5 | Summary Level-5

Every paper of the corpus is there; one row per paper x application (the same
rows, in the same order, on the four data sheets), and one ``N/A`` row for a
paper without target applications::

    ID                 | Target Application | Level-5
    KNOW-DOMP-0000001  | Dropbox            | COLL-02-04-06-003, EXAM-04-01-07-005
    KNOW-DOMP-0000043  | QQ                 | 미등록
    KNOW-DOMP-0000006  | N/A                |

A Level-5 cell holds the ids the application is mapped to, or what kept it
blank (미등록 / 매핑 안 됨 / 다른 제품으로 판단 / 원문 PDF 없음 / 대기 — see
``CELL_LABELS``).  The profiles take the PDF-Based result (Abstract-Based for a
paper without a PDF).  On 종합본 each row is coloured by whether the inputs
agree.

``read_applications`` still reads a profile's section-4 table (one row per
Level-5 id; the same application may repeat with different ids).

The two-column workbook (``export_application_list``, also ``--from-md``) has
sheet 2 (``List``): every distinct application name once, one per row,
sorted with Latin-alphabet names first (A-Z, case-insensitive) and Korean
names after them (가나다 order); a name starting with a digit or symbol
comes before the alphabet.  Names are compared exactly (after Unicode NFC
and whitespace normalisation), so spelling variants stay separate rows.

The English profile must list the same applications; a difference is an
error rather than a silent choice.  Nothing is inferred from other sources.

Run from the repository root (rewrites the workbook from ``targets.json``)::

    .venv/bin/python -m Analyzer.Application.application_list

Workbooks made for other paper families (for example the Scopus one,
``application_list_scopus-*.xlsx`` with ``KNOW-INTP`` ids) can be combined with
this one into a two-sheet workbook (one row per paper; a Level-5 workbook adds
its profile result — PDF-Based, or Abstract-Based without a PDF)::

    .venv/bin/python -m Analyzer.Application.application_list --merge \
        Analyzer/Application/application_list.xlsx \
        "Analyzer/Application/application_list_scopus-(1553).xlsx" \
        --output Analyzer/Application/application_list_all.xlsx

Sheet 1 of the merged workbook is every paper of every input (ids must be
unique across inputs), sorted by id; sheet 2 is rebuilt from those rows.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

MODULE_DIR = Path(__file__).resolve().parent
ANALYZER_DIR = MODULE_DIR.parent
DEFAULT_MD_DIR = ANALYZER_DIR / "Paper" / "MD"
DEFAULT_OUTPUT = MODULE_DIR / "application_list.xlsx"
SHEET_NAME = "대상 애플리케이션"
COLUMNS = ("ID", "Target Application")
# Merged workbooks (one row per paper) keep the Level-5 workbook's profile result in two more columns.
LEVEL5_COLUMNS = ("ID", "Target Application", "Level-5", "*-Based")
LIST_SHEET_NAME = "애플리케이션 목록"
LIST_COLUMNS = ("List",)
TARGETS_JSON = MODULE_DIR / "targets.json"
LEVEL5_ID_RE = re.compile(r"[A-Z]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2,3}\Z")   # DF-Insight: 2-digit last segment since 2026-10
# The Level-5 workbook: one sheet per input the mapping read, and all three side by side.
BASES = ("PDF", "Abstract", "Summary")
BASIS_LABELS = {"PDF": "PDF-Based", "Abstract": "Abstract-Based", "Summary": "Summary-Based"}
OVERVIEW_SHEET = "개요"
BASIS_COLUMNS = ("ID", "Target Application", "Level-5")
COMBINED_SHEET = "종합본"
COMBINED_COLUMNS = ("ID", "Target Application", "PDF Level-5", "Abstract Level-5", "Summary Level-5")
CELL_LABELS = {"no_fit": "매핑 안 됨", "unregistered": "미등록", "identity_mismatch": "다른 제품으로 판단", "pending": "대기"}
NO_INPUT_LABELS = {"PDF": "원문 PDF 없음", "Abstract": "초록 없음", "Summary": "요약 없음"}
# Results that can be compared between inputs (the application is registered and the input exists).
COMPARABLE = ("mapped", "no_fit", "identity_mismatch")
AGREEMENT_FILLS = {"same": "E2EFDA", "different": "FFF2CC", "none": "F2F2F2"}
UNREGISTERED_LABEL = CELL_LABELS["unregistered"]
MISSING = "N/A"
LIST_SEPARATOR = ", "
RECORD_ID_RE = re.compile(r"KNOW-DOMP-[0-9]{7}\Z")
ANY_RECORD_ID_RE = re.compile(r"KNOW-[A-Z]{4}-[0-9]{7}\Z")
HEADINGS = {"ko.md": "## 4. 대상 애플리케이션", "en.md": "## 4. Target Applications"}
TABLE_HEADERS = {"ko.md": ("ID", "이름"), "en.md": ("ID", "Name")}


class ApplicationListError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PaperApplications:
    record_id: str
    applications: tuple[str, ...]
    # Optional columns of the Level-5 workbook (kept when workbooks are merged).
    level5: str = ""
    basis: str = ""
    # The paper's result is not final yet: its cells stay blank (never "N/A" = no applications).
    pending: bool = False

    @property
    def cell(self) -> str:
        if self.pending:
            return ""
        return join_names(self.applications) if self.applications else MISSING


def join_names(names: Sequence[str]) -> str:
    """``", "``-joined names; a name that itself contains a comma or a quote is double-quoted (CSV style)."""

    return LIST_SEPARATOR.join(f'"{name.replace(chr(34), chr(34) * 2)}"' if ("," in name or '"' in name) else name for name in names)


def split_names(cell: str) -> tuple[str, ...]:
    """Inverse of ``join_names`` (also reads cells written before quoting existed)."""

    row = next(csv.reader([cell], delimiter=",", quotechar='"', skipinitialspace=True), [])
    return tuple(name.strip() for name in row if name.strip())


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    paper_count: int
    with_applications: int
    application_count: int
    unique_application_count: int = 0
    mapped_applications: int = 0
    unregistered_applications: int = 0
    no_fit_applications: int = 0
    pending_papers: int = 0


def _table_rows(path: Path, text: str, heading: str) -> list[tuple[str, str]]:
    positions = [index for index, line in enumerate(text.splitlines()) if line.strip() == heading]
    if len(positions) != 1:
        raise ApplicationListError(f"{path}: expected one {heading!r} heading, found {len(positions)}")
    rows: list[tuple[str, str]] = []
    for line in text.splitlines()[positions[0] + 1 :]:
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if not stripped or (stripped.startswith("<!--") and stripped.endswith("-->")):
            continue
        if re.fullmatch(r"\|\s*-+\s*\|\s*-+\s*\|", stripped):
            continue
        match = re.fullmatch(r"\|(.*?)\|(.*)\|", stripped)
        if not match:
            raise ApplicationListError(f"{path}: invalid application row: {line!r}")
        rows.append((match.group(1).strip(), match.group(2).strip()))
    if not rows:
        raise ApplicationListError(f"{path}: application section has no table")
    return rows


def _cell_text(value: str) -> str:
    # Profiles escape ``&``/``<``/``>`` and write ``|`` as ``&#124;``; the
    # workbook carries the plain application name.
    return " ".join(unicodedata.normalize("NFC", html.unescape(value)).split())


def _sort_group(name: str) -> int:
    """0 = starts with a digit/symbol, 1 = Latin alphabet, 2 = Hangul."""

    first = name[0]
    if first.isascii() and first.isalpha():
        return 1
    if "HANGUL" in unicodedata.name(first, ""):
        return 2
    return 0


def unique_application_names(papers: Sequence[PaperApplications]) -> list[str]:
    """Distinct application names: digits/symbols, then A-Z, then 가나다."""

    names = {name for paper in papers for name in paper.applications}
    return sorted(names, key=lambda name: (_sort_group(name), name.casefold(), name))


def read_application_rows(path: Path) -> tuple[tuple[str, str], ...]:
    """``(Level-5 id or "", name)`` rows of one profile file, ``()`` when the table says N/A.

    One row per Level-5 id: an application mapped to several ids repeats its name.
    An id must be a Level-5 id (``EXAM-04-01-02-003``) and may appear once.
    """

    name = path.name
    if name not in HEADINGS:
        raise ApplicationListError(f"{path}: not a ko.md/en.md profile")
    rows = _table_rows(path, path.read_text(encoding="utf-8"), HEADINGS[name])
    if tuple(value.casefold() for value in rows[0]) != tuple(value.casefold() for value in TABLE_HEADERS[name]):
        raise ApplicationListError(f"{path}: unexpected application table header: {rows[0]!r}")
    body = rows[1:]
    if not body:
        raise ApplicationListError(f"{path}: application table has no rows")
    if len(body) == 1 and not body[0][0] and body[0][1].casefold() == MISSING.casefold():
        return ()
    result: list[tuple[str, str]] = []
    for identifier, raw_name in body:
        application = _cell_text(raw_name)
        if not application or application.casefold() == MISSING.casefold():
            raise ApplicationListError(f"{path}: application table mixes N/A with real applications")
        identifier = identifier.strip()
        if identifier and not LEVEL5_ID_RE.fullmatch(identifier):
            raise ApplicationListError(f"{path}: not a Level-5 id: {identifier!r}")
        result.append((identifier, application))
    identifiers = [identifier for identifier, _ in result if identifier]
    if len(set(identifiers)) != len(identifiers):
        raise ApplicationListError(f"{path}: a Level-5 id appears twice: {identifiers}")
    blanks = [application for identifier, application in result if not identifier]
    if len(set(blanks)) != len(blanks):
        raise ApplicationListError(f"{path}: duplicate unregistered application names: {blanks}")
    return tuple(result)


def read_applications(path: Path) -> tuple[str, ...]:
    """Distinct application names of one profile file in table order, ``()`` for N/A."""

    return tuple(dict.fromkeys(application for _identifier, application in read_application_rows(path)))


def load_paper_applications(md_dir: Path = DEFAULT_MD_DIR) -> list[PaperApplications]:
    """One entry per ``KNOW-DOMP-*`` profile directory, in id order."""

    if not md_dir.is_dir():
        raise ApplicationListError(f"profile directory is missing: {md_dir}")
    directories = sorted(
        entry for entry in md_dir.iterdir() if entry.is_dir() and RECORD_ID_RE.fullmatch(entry.name)
    )
    if not directories:
        raise ApplicationListError(f"no KNOW-DOMP profiles under {md_dir}")
    papers: list[PaperApplications] = []
    for directory in directories:
        korean = read_application_rows(directory / "ko.md")
        english = read_application_rows(directory / "en.md")
        # The same Level-5 ids on the same rows; a registered row's name is the node title
        # in each language (they differ for a few nodes), an unregistered row's name is the same.
        if [identifier for identifier, _ in korean] != [identifier for identifier, _ in english] or [
            name for identifier, name in korean if not identifier
        ] != [name for identifier, name in english if not identifier]:
            raise ApplicationListError(
                f"{directory.name}: ko.md and en.md list different applications: {korean} != {english}"
            )
        papers.append(PaperApplications(record_id=directory.name, applications=read_applications(directory / "en.md")))
    return papers


def read_workbook_papers(path: Path) -> list[PaperApplications]:
    """Rows of a workbook's first sheet (``ID`` | ``Target Application``).

    The cell is split on ``", "`` back into names; ``N/A`` means none.  Ids
    must look like ``KNOW-XXXX-0000000`` and be unique within the file.
    """

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except (OSError, ValueError) as exc:
        raise ApplicationListError(f"{path}: cannot open workbook: {exc}") from exc
    try:
        if not workbook.sheetnames:
            raise ApplicationListError(f"{path}: workbook has no sheet")
        combined = COMBINED_SHEET in workbook.sheetnames
        rows = list(workbook[COMBINED_SHEET if combined else workbook.sheetnames[0]].iter_rows(values_only=True))
    finally:
        workbook.close()
    if combined:
        return _papers_from_combined(path, rows)
    if not rows or tuple(str(cell or "").strip() for cell in rows[0][:2]) != COLUMNS:
        raise ApplicationListError(f"{path}: first sheet must start with the columns {COLUMNS}")
    level5_layout = tuple(str(cell or "").strip() for cell in rows[0][:4]) == LEVEL5_COLUMNS
    papers: list[PaperApplications] = []
    seen: set[str] = set()
    for number, row in enumerate(rows[1:], start=2):
        cells = list(row) + [None, None, None, None]
        record_id = str(cells[0] or "").strip()
        if not record_id and cells[1] in (None, ""):
            continue  # trailing empty row
        level5 = str(cells[2] or "") if level5_layout else ""
        basis = str(cells[3] or "").strip() if level5_layout else ""
        if not ANY_RECORD_ID_RE.fullmatch(record_id):
            raise ApplicationListError(f"{path}: row {number}: invalid id {record_id!r}")
        if record_id in seen:
            raise ApplicationListError(f"{path}: duplicate id {record_id}")
        seen.add(record_id)
        cell = _cell_text(str(cells[1] or ""))
        if not cell and level5_layout:
            # A blank cell of the Level-5 workbook is a paper whose result is not final yet.
            papers.append(PaperApplications(record_id=record_id, applications=(), pending=True))
            continue
        if not cell or cell.casefold() == MISSING.casefold():
            applications: tuple[str, ...] = ()
        else:
            applications = split_names(cell)
            if not applications or len(set(applications)) != len(applications):
                raise ApplicationListError(f"{path}: row {number}: invalid application list {cell!r}")
        papers.append(PaperApplications(record_id=record_id, applications=applications, level5=level5, basis=basis))
    if not papers:
        raise ApplicationListError(f"{path}: first sheet has no paper rows")
    return papers


def _papers_from_combined(path: Path, rows: Sequence[Sequence[Any]]) -> list[PaperApplications]:
    """Papers of a Level-5 workbook's 종합본 sheet (one row per paper x application).

    The profile result of each application is its PDF-Based cell, or the
    Abstract-Based cell for a paper without a PDF.
    """

    if not rows or tuple(str(cell or "").strip() for cell in rows[0][: len(COMBINED_COLUMNS)]) != COMBINED_COLUMNS:
        raise ApplicationListError(f"{path}: sheet {COMBINED_SHEET!r} must have the columns {COMBINED_COLUMNS}")
    grouped: dict[str, list[list[str]]] = {}
    for number, row in enumerate(rows[1:], start=2):
        cells = [str(cell or "").strip() for cell in list(row) + [None] * len(COMBINED_COLUMNS)]
        record_id, name = cells[0], _cell_text(cells[1])
        if not record_id and not name:
            continue
        if not ANY_RECORD_ID_RE.fullmatch(record_id):
            raise ApplicationListError(f"{path}: row {number}: invalid id {record_id!r}")
        grouped.setdefault(record_id, []).append([name, *cells[2:5]])
    papers: list[PaperApplications] = []
    for record_id, entries in grouped.items():
        # A paper without a PDF says so on every row (its PDF column); its profile result is Abstract-Based.
        no_pdf = any(pdf == NO_INPUT_LABELS["PDF"] for _name, pdf, _abstract, _summary in entries)
        basis = BASIS_LABELS["Abstract" if no_pdf else "PDF"]
        profile = [(name, abstract if no_pdf else pdf) for name, pdf, abstract, _summary in entries]
        names = [name for name, _cell in profile]
        if not all(names) or any(cell == CELL_LABELS["pending"] for _name, cell in profile):
            papers.append(PaperApplications(record_id=record_id, applications=(), pending=True))
        elif names == [MISSING]:
            papers.append(PaperApplications(record_id=record_id, applications=(), basis=basis))
        else:
            if MISSING in names or len(set(names)) != len(names):
                raise ApplicationListError(f"{path}: {record_id}: invalid application rows {names}")
            papers.append(PaperApplications(
                record_id=record_id, applications=tuple(names),
                level5="\n".join(f"{name}: {cell}" for name, cell in profile), basis=basis,
            ))
    if not papers:
        raise ApplicationListError(f"{path}: sheet {COMBINED_SHEET!r} has no paper rows")
    return papers


def merge_workbooks(inputs: Sequence[Path], output: Path) -> ExportResult:
    """One workbook (same two sheets) for every paper of every input workbook."""

    if len(inputs) < 2:
        raise ApplicationListError("merging needs at least two workbooks")
    papers: list[PaperApplications] = []
    for path in inputs:
        papers.extend(read_workbook_papers(Path(path)))
    ids = [paper.record_id for paper in papers]
    duplicates = sorted({record_id for record_id in ids if ids.count(record_id) > 1})
    if duplicates:
        raise ApplicationListError(f"ids present in more than one workbook: {duplicates[:5]}")
    papers.sort(key=lambda paper: paper.record_id)
    return export_application_list(papers, output)


def _workbook_value(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)[:32000]


def keep_text(sheet) -> None:
    """openpyxl stores a string starting with "=" as a formula; such names stay text, unchanged."""

    for row in sheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value[:1] == "=":
                cell.data_type = "s"


def export_application_list(
    papers: Sequence[PaperApplications], output: Path = DEFAULT_OUTPUT
) -> ExportResult:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_NAME
    # Merging a Level-5 workbook keeps its Level-5 and *-Based columns (blank for papers without them).
    wide = any(paper.level5 or paper.basis or paper.pending for paper in papers)
    sheet.append(list(LEVEL5_COLUMNS if wide else COLUMNS))
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="305496")
        cell.alignment = Alignment(vertical="center")
    for paper in papers:
        row = [paper.record_id, _workbook_value(paper.cell)]
        sheet.append(row + [_workbook_value(paper.level5), paper.basis] if wide else row)
    keep_text(sheet)
    sheet.column_dimensions["A"].width = 20
    sheet.column_dimensions["B"].width = 70
    if wide:
        sheet.column_dimensions["C"].width = 70
        sheet.column_dimensions["D"].width = 15
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    names = unique_application_names(papers)
    listing = workbook.create_sheet(LIST_SHEET_NAME)
    listing.append(list(LIST_COLUMNS))
    for cell in listing[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="305496")
        cell.alignment = Alignment(vertical="center")
    for name in names:
        listing.append([_workbook_value(name)])
    keep_text(listing)
    listing.column_dimensions["A"].width = 60
    listing.freeze_panes = "A2"
    listing.auto_filter.ref = listing.dimensions
    workbook.active = 0

    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".xlsx", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        workbook.save(temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        workbook.close()
        temporary.unlink(missing_ok=True)
    return ExportResult(
        path=destination,
        paper_count=len(papers),
        with_applications=sum(bool(paper.applications) for paper in papers),
        application_count=sum(len(paper.applications) for paper in papers),
        unique_application_count=len(names),
    )


def save_workbook(workbook: Workbook, output: Path) -> Path:
    """Atomic save (temporary file in the same folder, then rename); 0-byte placeholders are replaced."""

    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".xlsx", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        workbook.save(temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        workbook.close()
        temporary.unlink(missing_ok=True)
    return destination


def _style_header(sheet) -> None:
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="305496")
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def load_targets(path: Path | None = None) -> list[dict[str, Any]]:
    """Papers of ``targets.json`` in the shape ``export_level5_workbook`` takes."""

    path = path or TARGETS_JSON
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ApplicationListError(f"{path}: cannot read the targets file: {exc}") from exc
    papers = data.get("papers") if isinstance(data, dict) else None
    if not isinstance(papers, dict):
        raise ApplicationListError(f"{path}: no papers")
    return [{"source_id": source_id, **record} for source_id, record in papers.items()]


def load_registry_info(path: Path | None = None) -> dict[str, Any]:
    """The ``registry`` block of ``targets.json`` (node count and DF-Insight revision) for the overview."""

    try:
        data = json.loads(Path(path or TARGETS_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    registry = data.get("registry") if isinstance(data, dict) else None
    return registry if isinstance(registry, dict) else {}


def _application_label(application: Mapping[str, Any]) -> str:
    return str(application.get("display_name") or application.get("name") or "")


@dataclass(frozen=True, slots=True)
class BasisCell:
    """One application's result under one input."""

    text: str
    kind: str                       # mapped | no_fit | identity_mismatch | unregistered | unavailable | pending
    ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplicationRow:
    """One row of the four data sheets: a paper's application, or the paper's N/A (or pending) row."""

    record_id: str
    name: str
    paper_basis: str
    cells: Mapping[str, BasisCell] | None = None    # None: the paper has no application (N/A) or is pending


def basis_cell(paper: Mapping[str, Any], application: Mapping[str, Any], basis: str) -> BasisCell:
    paper_basis = str(paper.get("basis") or "")
    if paper_basis and paper_basis != "PDF" and basis != paper_basis:
        return BasisCell(NO_INPUT_LABELS["PDF"], "unavailable")      # no PDF: no PDF text and no summary
    outcome = (application.get("level5_by_basis") or {}).get(basis)
    if outcome is None:
        if basis != paper_basis:   # targets.json written before the per-input mappings
            if application.get("status") in ("unregistered", "identity_mismatch"):
                return BasisCell(UNREGISTERED_LABEL, "unregistered")
            return BasisCell(CELL_LABELS["pending"], "pending")
        outcome = {"status": application.get("status"), "ids": [item["id"] for item in application.get("level5") or []]}
    status = str(outcome.get("status") or "")
    ids = tuple(outcome.get("ids") or ())
    if status == "mapped" and ids:
        return BasisCell(LIST_SEPARATOR.join(ids), "mapped", ids)
    if status == "unavailable":
        return BasisCell(NO_INPUT_LABELS["PDF" if paper_basis != "PDF" else basis], "unavailable")
    if status in CELL_LABELS:
        return BasisCell(CELL_LABELS[status], status)
    return BasisCell(CELL_LABELS["pending"], "pending")


def application_rows(papers: Sequence[Mapping[str, Any]]) -> list[ApplicationRow]:
    """Rows of the data sheets in id order; the same rows on every sheet."""

    rows: list[ApplicationRow] = []
    for paper in sorted(papers, key=lambda item: (str(item.get("record_id") or "~"), str(item.get("source_id")))):
        record_id, paper_basis = str(paper.get("record_id") or ""), str(paper.get("basis") or "")
        applications = list(paper.get("applications") or [])
        if not applications:
            rows.append(ApplicationRow(record_id, MISSING if paper.get("status") == "ok" else "", paper_basis))
            continue
        for application in applications:
            rows.append(ApplicationRow(
                record_id, _application_label(application), paper_basis,
                {basis: basis_cell(paper, application, basis) for basis in BASES},
            ))
    return rows


def agreement(cells: Mapping[str, BasisCell], bases: Sequence[str] = BASES) -> str:
    """``same`` / ``different`` between the inputs that have a result, ``none`` when fewer than two do."""

    compared = [cells[basis] for basis in bases if cells[basis].kind in COMPARABLE]
    if len(compared) < 2:
        return "none"
    return "same" if len({cell.text for cell in compared}) == 1 else "different"


def _pair_counts(rows: Sequence[ApplicationRow], bases: Sequence[str]) -> tuple[int, int, int, int]:
    """(compared, same, different but some two of them share an id, no two share one) over rows where every one of ``bases`` has a result."""

    compared = same = overlap = disjoint = 0
    for row in rows:
        if not row.cells or any(row.cells[basis].kind not in COMPARABLE for basis in bases):
            continue
        compared += 1
        if agreement(row.cells, bases) == "same":
            same += 1
        elif any(set(row.cells[first].ids) & set(row.cells[second].ids) for first, second in combinations(bases, 2)):
            overlap += 1
        else:
            disjoint += 1
    return compared, same, overlap, disjoint


def _write_rows(sheet, header: Sequence[str], rows: Sequence[Sequence[Any]], widths: Sequence[int], freeze: str) -> None:
    sheet.append(list(header))
    for row in rows:
        sheet.append([_workbook_value(value) if isinstance(value, str) else value for value in row])
    _style_header(sheet)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    keep_text(sheet)
    for index, width in enumerate(widths):
        sheet.column_dimensions[chr(ord("A") + index)].width = width
    sheet.freeze_panes = freeze
    sheet.auto_filter.ref = sheet.dimensions


def _write_overview(sheet, papers: Sequence[Mapping[str, Any]], rows: Sequence[ApplicationRow], registry: Mapping[str, Any]) -> None:
    ok = [paper for paper in papers if paper.get("status") == "ok"]
    with_applications = sum(1 for paper in papers if paper.get("applications"))
    no_pdf = sum(1 for paper in ok if paper.get("basis") != "PDF")
    application_rows_ = [row for row in rows if row.cells]
    pending = len(papers) - len(ok)
    header_font, header_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="305496")

    def table(title: str, header: Sequence[str], body: Sequence[Sequence[Any]], fills: Sequence[str | None] = ()) -> None:
        sheet.append([title])
        sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True, size=12)
        sheet.append(list(header))
        for cell in sheet[sheet.max_row]:
            cell.font, cell.fill = header_font, header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for index, values in enumerate(body):
            sheet.append([_workbook_value(value) if isinstance(value, str) else value for value in values])
            for cell in sheet[sheet.max_row]:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if isinstance(cell.value, float):
                    cell.number_format = "0.0%"
            fill = fills[index] if index < len(fills) else None
            if fill:
                sheet.cell(row=sheet.max_row, column=1).fill = PatternFill("solid", fgColor=fill)
        sheet.append([])

    sheet.append(["대상 애플리케이션 Level-5 매핑 — 입력별(PDF·Abstract·Summary) 비교"])
    sheet["A1"].font = Font(bold=True, size=14)
    snapshot = f"DF-Insight Level-5 노드 {registry.get('level5_nodes', '?'):,}개" if isinstance(registry.get("level5_nodes"), int) else ""
    sheet.append([
        f"생성일 {date.today().isoformat()} · 논문 {len(papers)}편(대상 애플리케이션 있음 {with_applications}편, 애플리케이션 {len(application_rows_)}건"
        + (f", 아직 끝나지 않은 논문 {pending}편" if pending else "") + ")" + (f" · {snapshot}" if snapshot else "")
    ])
    sheet.append([])
    table("시트", ("시트", "설명"), [
        (BASIS_LABELS["PDF"], "원문 PDF 본문 전체를 보고 Level-5 노드를 고른 결과(1단계 추출 메모 포함). MD의 '4. 대상 애플리케이션' ID 칸이 이 결과입니다"
                              + (f" — 원문 PDF가 없는 논문 {no_pdf}편은 Abstract-Based 결과." if no_pdf else ".")),
        (BASIS_LABELS["Abstract"], "논문의 국문·영문 초록만 보고 고른 결과."),
        (BASIS_LABELS["Summary"], "원문 본문을 먼저 요약한 구조화 요약(연구 목적·대상·방법·기여·결과)만 보고 고른 결과."),
        (COMBINED_SHEET, "세 결과를 한 행에 나란히 둔 비교표. 행 색은 아래 '종합본 색'을 보세요."),
        ("공통", "한 행 = 논문 하나의 대상 애플리케이션 하나(대상 애플리케이션이 없는 논문은 N/A 한 행). 대상 애플리케이션은 세 입력이 같고"
                 "(1단계에서 원문 PDF로 추출, 원문이 없으면 초록), Level-5 매핑만 입력을 바꿔 따로 했습니다. 네 시트의 행 순서가 같습니다."),
    ])
    table("칸 값", ("값", "뜻"), [
        ("COLL-02-04-06-003, …", "매핑된 Level-5 노드 id(여러 개면 쉼표로 구분). 상위 Level은 id 앞부분입니다."),
        (CELL_LABELS["no_fit"], "등록된 애플리케이션이지만 그 입력으로는 맞는 Level-5 노드를 고르지 않음(그 입력에 애플리케이션이나 한 일이 드러나지 않는 경우 포함)."),
        (CELL_LABELS["unregistered"], "DF-Insight에 아직 등록되지 않은 애플리케이션(applicationempty_level5.xlsx '등록되지 않은 Application'). "
                                      "PDF-Based 매핑이 같은 이름의 노드를 다른 제품으로 판단한 경우도 여기에 들어갑니다."),
        (CELL_LABELS["identity_mismatch"], "같은 이름의 노드는 있지만 그 입력으로 보면 다른 제품."),
        (" · ".join(NO_INPUT_LABELS[basis] for basis in BASES), "그 논문에는 그 입력이 없음."),
        (CELL_LABELS["pending"], "아직 끝나지 않음(다음 실행에서 채움)."),
        (MISSING, "대상 애플리케이션이 없는 논문."),
    ])
    table("종합본 색", ("색", "뜻"), [
        ("일치", "결과가 있는 입력들의 값이 모두 같음."),
        ("불일치", "입력마다 값이 다름."),
        ("비교 안 함", "미등록이거나, 결과가 있는 입력이 하나뿐(원문 PDF 없음 등)."),
    ], fills=(AGREEMENT_FILLS["same"], AGREEMENT_FILLS["different"], AGREEMENT_FILLS["none"]))
    by_basis = []
    for basis in BASES:
        cells = [row.cells[basis] for row in application_rows_]
        kinds = [cell.kind for cell in cells]
        mapped_papers = {row.record_id for row in application_rows_ if row.cells[basis].kind == "mapped"}
        by_basis.append((
            BASIS_LABELS[basis], kinds.count("mapped"), sum(len(cell.ids) for cell in cells), kinds.count("no_fit"),
            kinds.count("identity_mismatch"), kinds.count("unregistered"), kinds.count("unavailable"), kinds.count("pending"), len(mapped_papers),
        ))
    table("입력별 결과(애플리케이션 행 수)", (
        "입력", "매핑됨", "Level-5 id 수", CELL_LABELS["no_fit"], CELL_LABELS["identity_mismatch"], CELL_LABELS["unregistered"],
        "입력 없음", CELL_LABELS["pending"], "매핑된 애플리케이션이 있는 논문",
    ), by_basis)
    comparisons = []
    for label, bases in (("PDF ↔ Abstract", ("PDF", "Abstract")), ("PDF ↔ Summary", ("PDF", "Summary")),
                         ("Abstract ↔ Summary", ("Abstract", "Summary")), ("세 입력 모두", BASES)):
        compared, same, overlap, disjoint = _pair_counts(application_rows_, bases)
        comparisons.append((label, compared, same, overlap, disjoint, (same / compared) if compared else "-"))
    table("입력 간 비교(등록된 애플리케이션 중 비교하는 입력 모두에 결과가 있는 행)",
          ("비교", "비교한 행", "같음", "다름 — 어느 두 입력이 id를 일부 공유", "다름 — 공유하는 id 없음", "같은 비율"), comparisons)
    widths = (26, 16, 14, 16, 18, 12, 12, 10, 18)
    for column, width in zip("ABCDEFGHI", widths):
        sheet.column_dimensions[column].width = width
    # The explanation tables put long text in column B: spread it over B-I.  Excel does not
    # size a merged cell's row to its text, so the row height is set from the text length.
    text_rows = [
        row[0].row for row in sheet.iter_rows(min_row=3)
        if len(row) > 1 and isinstance(row[1].value, str) and all(cell.value in (None, "") for cell in row[2:])
    ]
    def units(value: Any) -> int:
        return sum(2 if ord(character) > 0x2E80 else 1 for character in str(value or ""))

    for number in text_rows:
        sheet.merge_cells(start_row=number, start_column=2, end_row=number, end_column=len(widths))
        lines = max(
            1, -(-units(sheet.cell(row=number, column=2).value) // (sum(widths[1:]) - 4)),
            -(-units(sheet.cell(row=number, column=1).value) // (widths[0] - 2)),
        )
        sheet.row_dimensions[number].height = 16 * lines + 2
    keep_text(sheet)


def _empty_row_cell(row: ApplicationRow, basis: str) -> str:
    """The Level-5 cell of a paper's N/A row (no applications) or pending row."""

    if not row.name:
        return CELL_LABELS["pending"]
    if row.paper_basis and row.paper_basis != "PDF" and basis != row.paper_basis:
        return NO_INPUT_LABELS["PDF"]
    return ""


def export_level5_workbook(
    papers: Sequence[Mapping[str, Any]], output: Path = DEFAULT_OUTPUT, registry: Mapping[str, Any] | None = None,
) -> ExportResult:
    """개요, one sheet per input (PDF-/Abstract-/Summary-Based) and 종합본 (the three side by side)."""

    rows = application_rows(papers)
    workbook = Workbook()
    overview = workbook.active
    overview.title = OVERVIEW_SHEET
    _write_overview(overview, papers, rows, registry or {})
    for basis in BASES:
        sheet = workbook.create_sheet(BASIS_LABELS[basis])
        _write_rows(sheet, BASIS_COLUMNS, [
            (row.record_id, row.name, row.cells[basis].text if row.cells else _empty_row_cell(row, basis))
            for row in rows
        ], (20, 34, 48), "C2")
        for row, cells in zip(rows, sheet.iter_rows(min_row=2)):
            if row.cells and row.cells[basis].kind == "unavailable":
                cells[2].fill = PatternFill("solid", fgColor=AGREEMENT_FILLS["none"])
    combined = workbook.create_sheet(COMBINED_SHEET)
    _write_rows(combined, COMBINED_COLUMNS, [
        (row.record_id, row.name, *(row.cells[basis].text if row.cells else _empty_row_cell(row, basis) for basis in BASES))
        for row in rows
    ], (20, 34, 42, 42, 42), "C2")
    for row, cells in zip(rows, combined.iter_rows(min_row=2)):
        if not row.cells:
            continue
        verdict = agreement(row.cells)
        for basis, cell in zip(BASES, cells[2:]):
            kind = row.cells[basis].kind
            colour = AGREEMENT_FILLS["none"] if kind == "unavailable" or verdict == "none" else (
                AGREEMENT_FILLS[verdict] if kind in COMPARABLE else None)
            if colour:
                cell.fill = PatternFill("solid", fgColor=colour)
    workbook.active = 0
    destination = save_workbook(workbook, output)
    application_rows_ = [row for row in rows if row.cells]

    def primary(row: ApplicationRow) -> BasisCell:
        return row.cells["PDF" if row.paper_basis == "PDF" else "Abstract"]

    return ExportResult(
        path=destination, paper_count=len(papers), pending_papers=sum(1 for paper in papers if paper.get("status") != "ok"),
        with_applications=sum(1 for paper in papers if paper.get("applications")),
        application_count=len(application_rows_), unique_application_count=len({row.name for row in application_rows_}),
        mapped_applications=sum(primary(row).kind == "mapped" for row in application_rows_),
        unregistered_applications=sum(primary(row).kind in ("unregistered", "identity_mismatch") for row in application_rows_),
        no_fit_applications=sum(primary(row).kind == "no_fit" for row in application_rows_),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--md-dir", type=Path, default=DEFAULT_MD_DIR)
    parser.add_argument("--targets", type=Path, default=TARGETS_JSON, help="Level-5 매핑 결과(targets.json)")
    parser.add_argument("--from-md", action="store_true", help="(예전 방식) MD 표에서 앱 이름만 읽어 두 열 워크북을 씀")
    parser.add_argument("--output", type=Path, default=None, help=f"기본: {DEFAULT_OUTPUT} (--merge 시에는 필수)")
    parser.add_argument(
        "--merge",
        nargs="+",
        type=Path,
        metavar="XLSX",
        help="MD 대신 같은 형식의 워크북 여러 개(시트 1: ID·Target Application)를 하나로 합침",
    )
    arguments = parser.parse_args(argv)
    try:
        if arguments.merge:
            if arguments.output is None:
                raise ApplicationListError("--merge에는 --output 경로가 필요합니다")
            result = merge_workbooks(arguments.merge, arguments.output)
        elif arguments.from_md:
            result = export_application_list(
                load_paper_applications(arguments.md_dir), arguments.output or DEFAULT_OUTPUT
            )
        else:
            result = export_level5_workbook(
                load_targets(arguments.targets), arguments.output or DEFAULT_OUTPUT, registry=load_registry_info(arguments.targets),
            )
    except (ApplicationListError, OSError) as exc:
        print(f"[APPLICATION-LIST] 실패: {exc}", file=sys.stderr)
        return 1
    waiting = f" · 아직 끝나지 않은 논문 {result.pending_papers}편" if result.pending_papers else ""
    print(
        f"[APPLICATION-LIST] 완료: 논문 {result.paper_count}편 "
        f"(대상 앱 있음 {result.with_applications}편 · 앱 {result.application_count}건 · "
        f"없음 {result.paper_count - result.with_applications - result.pending_papers}편 = N/A{waiting}) · "
        f"고유 앱 이름 {result.unique_application_count}개 → {result.path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
