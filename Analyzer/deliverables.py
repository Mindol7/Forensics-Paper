"""Dated result workbooks: OA, Special Issue, Type, Tags and the reference list.

Each collector keeps a detailed working workbook (``oa_list.xlsx``,
``special_list.xlsx``, ``type_list.xlsx``, ``ref_list.xlsx``, and the jev
stage's ``Analyzer/JevAI/targets.json`` for tags and target applications).
This module turns them into the workbooks people read, in the same
shape as the Level workbooks (``Analyzer/LV/(YYMMDD)-LV-…-Based.xlsx``):

    Analyzer/OA/(YYMMDD)-OA.xlsx
    Analyzer/Special-Issue/(YYMMDD)-Special-Issue.xlsx
    Analyzer/Type/(YYMMDD)-Type.xlsx
    Analyzer/Tags/(YYMMDD)-Tags.xlsx
    Analyzer/Ref/(YYMMDD)-ref_list.xlsx

Every workbook has one row per paper of the *whole* current corpus (earlier
papers and newly collected ones alike), starting with the same five identity
columns ``paper_index, record_id, title, journal, publication_year``.  A value
that could not be determined is left empty; nothing is guessed.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from Analyzer.tag_taxonomy import AXES, default_tag_taxonomy

ANALYZER_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
DEFAULT_DATABASE = REPOSITORY_ROOT / "kci_pipeline.db"

OA_SOURCE = ANALYZER_DIR / "OA" / "oa_list.xlsx"
SPECIAL_SOURCE = ANALYZER_DIR / "Special-Issue" / "special_list.xlsx"
TYPE_SOURCE = ANALYZER_DIR / "Type" / "type_list.xlsx"
REFERENCE_SOURCE = ANALYZER_DIR / "Ref" / "ref_list.xlsx"
KEYWORD_SOURCE = ANALYZER_DIR / "Intelligence" / "kci_keyword_intelligence_with_app_category.xlsx"
# Tags and target applications with their Level-5 ids (Jev AI, the jev stage):
# the same source the profiles' front matter and "4. 대상 애플리케이션" come from.
TARGETS_SOURCE = ANALYZER_DIR / "JevAI" / "targets.json"

IDENTITY_COLUMNS = ("paper_index", "record_id", "title", "journal", "publication_year")
RESULT_SHEET = "논문별 결과"
REFERENCE_SHEET = "참고문헌 목록"
LIST_SEPARATOR = ", "
YES, NO = "Y", "N"

OUTPUTS: dict[str, tuple[Path, str]] = {
    "oa": (ANALYZER_DIR / "OA", "OA.xlsx"),
    "special": (ANALYZER_DIR / "Special-Issue", "Special-Issue.xlsx"),
    "type": (ANALYZER_DIR / "Type", "Type.xlsx"),
    "tags": (ANALYZER_DIR / "Tags", "Tags.xlsx"),
    "ref": (ANALYZER_DIR / "Ref", "ref_list.xlsx"),
}


class DeliverableError(RuntimeError):
    """A source workbook is missing or does not have the expected shape."""


@dataclass(frozen=True, slots=True)
class Identity:
    source_id: str
    paper_index: int | None
    record_id: str
    title: str
    journal: str
    publication_year: int | None

    def cells(self) -> list[Any]:
        return [self.paper_index if self.paper_index is not None else "", self.record_id, _cell(self.title),
                _cell(self.journal), self.publication_year if self.publication_year is not None else ""]


@dataclass(frozen=True, slots=True)
class DeliverableResult:
    name: str
    path: Path
    paper_count: int
    filled_count: int  # papers whose value could be determined


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)[:32000]
        # openpyxl stores a string that starts with "=" as a formula; a title must stay text.
        return "'" + value if value[:1] == "=" else value
    return value


def load_identities(database: Path = DEFAULT_DATABASE) -> list[Identity]:
    """The current corpus in ``paper_index`` order (papers without a number last)."""

    from Analyzer.LV.lv_experiment import load_corpus

    papers = load_corpus(database)
    identities = [
        Identity(paper.source_id, paper.paper_index, paper.record_id, paper.display_title, paper.journal, paper.publication_year)
        for paper in papers
    ]
    return sorted(identities, key=lambda item: (item.paper_index is None, item.paper_index or 0, item.source_id))


def read_rows(path: Path, sheet: str, key: str = "source_id") -> dict[str, dict[str, Any]]:
    """Rows of one worksheet keyed by ``key``; a missing workbook is an error, not an empty result."""

    if not path.is_file():
        raise DeliverableError(f"원본 워크북이 없습니다 (해당 수집 단계를 먼저 실행하세요): {path}")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet not in workbook.sheetnames:
            raise DeliverableError(f"{path.name}: '{sheet}' 시트가 없습니다")
        iterator = workbook[sheet].iter_rows(values_only=True)
        header = [str(value) if value is not None else "" for value in next(iterator, ())]
        if key not in header:
            raise DeliverableError(f"{path.name}/{sheet}: '{key}' 열이 없습니다")
        rows: dict[str, dict[str, Any]] = {}
        for values in iterator:
            row = dict(zip(header, values))
            identifier = row.get(key)
            if identifier is None:
                continue
            if str(identifier) in rows:
                raise DeliverableError(f"{path.name}/{sheet}: {identifier} 행이 중복됩니다")
            rows[str(identifier)] = row
        return rows
    finally:
        workbook.close()


def read_reference_rows(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    path = path or REFERENCE_SOURCE
    if not path.is_file():
        raise DeliverableError(f"원본 워크북이 없습니다 (해당 수집 단계를 먼저 실행하세요): {path}")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if "참고문헌 상세" not in workbook.sheetnames:
            raise DeliverableError(f"{path.name}: '참고문헌 상세' 시트가 없습니다")
        iterator = workbook["참고문헌 상세"].iter_rows(values_only=True)
        header = [str(value) if value is not None else "" for value in next(iterator, ())]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for values in iterator:
            row = dict(zip(header, values))
            if row.get("citing_source_id"):
                grouped.setdefault(str(row["citing_source_id"]), []).append(row)
        return grouped
    finally:
        workbook.close()


def dated_path(name: str, stamp: str | None = None) -> Path:
    directory, suffix = OUTPUTS[name]
    return directory / f"({stamp or date.today().strftime('%y%m%d')})-{suffix}"


def _remove_older(target: Path) -> None:
    stamp, suffix = target.name[1:7], target.name.split(")-", 1)[1]
    for other in target.parent.glob(f"(*)-{suffix}"):
        # Only an earlier date is superseded: a run dated in the past must not delete newer results.
        if other != target and re.fullmatch(r"\([0-9]{6}\)-" + re.escape(suffix), other.name) and other.name[1:7] < stamp:
            other.unlink()


def _style(sheet, widths: Sequence[int], freeze: str = "C2") -> None:
    for cell in sheet[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="305496")
        cell.alignment = Alignment(vertical="center")
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width
    sheet.freeze_panes = freeze
    sheet.auto_filter.ref = sheet.dimensions


def _save(name: str, workbook: Workbook, stamp: str | None) -> Path:
    target = dated_path(name, stamp)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    workbook.save(temporary)
    temporary.replace(target)
    _remove_older(target)
    return target


def _flag(value: Any) -> str:
    if value is True or str(value).strip().casefold() in ("true", "y", "yes", "1"):
        return YES
    if value is False or str(value).strip().casefold() in ("false", "n", "no", "0"):
        return NO
    return ""


# --------------------------------------------------------------------------- #
# The five workbooks
# --------------------------------------------------------------------------- #

def oa_values(row: Mapping[str, Any] | None) -> list[Any]:
    if not row or row.get("collection_status") != "classified":
        return ["", ""]
    return [_flag(row.get("is_open_access")), row.get("oa_classification") or ""]


def special_values(row: Mapping[str, Any] | None) -> list[Any]:
    if not row or row.get("collection_status") != "classified":
        return ["", "", ""]
    flag = _flag(row.get("is_special_issue"))
    return [flag, row.get("special_issue_classification") or "", (row.get("special_number") or "") if flag == YES else ""]


def type_values(row: Mapping[str, Any] | None) -> list[Any]:
    if not row or row.get("classification_status") != "classified":
        return ["", "", ""]
    return [row.get("subtype") or "", row.get("subtype_des") or "", row.get("subtype_description_ko") or ""]


def read_targets(path: Path | None = None, *, required: bool = True) -> dict[str, dict[str, Any]]:
    """Per paper (``source_id``) the final tags and target applications (``TARGETS_SOURCE``).

    Only papers whose result is final (``status: ok``) are returned, so a paper still
    waiting for its extraction shows empty cells rather than "no tags".
    """

    path = path or TARGETS_SOURCE

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if not required:
            return {}
        raise DeliverableError(f"원본 파일이 없습니다(또는 읽을 수 없습니다): {path} — jev 단계(python3 main.py -jev)를 먼저 실행하세요 ({exc})") from exc
    papers = payload.get("papers") if isinstance(payload, dict) else None
    if not isinstance(papers, dict):
        if not required:
            return {}
        raise DeliverableError(f"{path}: papers가 없습니다")
    return {str(key): value for key, value in papers.items() if isinstance(value, dict) and value.get("status") == "ok"}


def application_names(record: Mapping[str, Any] | None) -> list[str]:
    return [str(item.get("display_name") or item.get("name")) for item in (record or {}).get("applications") or []]


def tags_by_axis(record: Mapping[str, Any] | None) -> dict[str, list[str]] | None:
    """Tags of one paper grouped by axis, in taxonomy order; ``None`` when the paper was not analysed."""

    if record is None:
        return None
    # The profiles render exactly these tags, so a workbook and an MD can never disagree.
    chosen = set(record.get("tags") or [])
    tags = [tag for tag in default_tag_taxonomy().allowed_tags if tag in chosen]
    return {axis: [tag for tag in tags if tag.split("-", 1)[0] == axis] for axis in AXES}


def tag_values(record: Mapping[str, Any] | None) -> list[Any]:
    grouped = tags_by_axis(record)
    if grouped is None:
        return [""] * (len(AXES) + 2)
    all_tags = [tag for axis in AXES for tag in grouped[axis]]
    # An application tag is only meaningful together with the concrete applications it came from.
    from Analyzer.Application.application_list import join_names

    names = join_names(application_names(record))
    return [*(LIST_SEPARATOR.join(grouped[axis]) for axis in AXES), names, LIST_SEPARATOR.join(all_tags)]


def reference_values(row: Mapping[str, Any] | None) -> list[Any]:
    if not row or row.get("collection_status") not in ("collected", "no_reference_data"):
        return ["", ""]
    present = _flag(row.get("reference_info_present"))
    return [present, int(row.get("reference_count") or 0) if present == YES else 0]


def _reference_text(row: Mapping[str, Any]) -> str:
    # A reference KCI delivered as one CDATA citation string is listed as is.
    citation = str(row.get("reference_citation_text") or "").strip()
    if citation:
        return citation
    parts = [row.get("reference_author"), row.get("reference_title"),
             row.get("reference_journal_name") or row.get("reference_conference_name") or row.get("reference_publisher") or row.get("reference_site_name"),
             row.get("reference_publication_year")]
    return ". ".join(str(part).strip() for part in parts if part not in (None, ""))


def _simple_workbook(identities: Sequence[Identity], columns: Sequence[str], values, widths: Sequence[int]) -> tuple[Workbook, int]:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = RESULT_SHEET
    sheet.append([*IDENTITY_COLUMNS, *columns])
    filled = 0
    for identity in identities:
        cells = [_cell(value) for value in values(identity)]
        filled += any(value not in ("", None) for value in cells[:1])
        sheet.append([*identity.cells(), *cells])
    _style(sheet, (12, 20, 60, 28, 16, *widths))
    return workbook, filled


def export_all(stamp: str | None = None, *, database: Path = DEFAULT_DATABASE, identities: Sequence[Identity] | None = None) -> list[DeliverableResult]:
    """Write the five dated workbooks for the whole current corpus."""

    papers = list(identities) if identities is not None else load_identities(database)
    oa = read_rows(OA_SOURCE, "OA 목록")
    special = read_rows(SPECIAL_SOURCE, "Special Issue 목록")
    types = read_rows(TYPE_SOURCE, "논문 유형 목록")
    # Tags come from the jev stage; without its result the Tags columns stay blank and the
    # other workbooks (OA, Special Issue, Type, references) are still written.
    targets = read_targets(required=False)
    reference_summary = read_rows(REFERENCE_SOURCE, "논문별 요약")
    references = read_reference_rows()
    record_ids = {paper.source_id: paper.record_id for paper in papers}
    taxonomy = default_tag_taxonomy()
    results: list[DeliverableResult] = []

    book, filled = _simple_workbook(papers, ("open_access", "oa_classification"), lambda p: oa_values(oa.get(p.source_id)), (14, 22))
    results.append(DeliverableResult("oa", _save("oa", book, stamp), len(papers), filled))

    book, filled = _simple_workbook(papers, ("special_issue", "special_issue_classification", "special_issue_name"),
                                    lambda p: special_values(special.get(p.source_id)), (14, 28, 24))
    results.append(DeliverableResult("special", _save("special", book, stamp), len(papers), filled))

    book, filled = _simple_workbook(papers, ("type", "type_name", "type_description"), lambda p: type_values(types.get(p.source_id)), (10, 18, 22))
    results.append(DeliverableResult("type", _save("type", book, stamp), len(papers), filled))

    # Tags: "filled" counts analysed papers (an analysed paper may legitimately carry no tag).
    book, _ = _simple_workbook(papers, (*(f"tags_{axis}" for axis in AXES), "application_names", "all_tags"),
                               lambda p: tag_values(targets.get(p.source_id)), (16, 16, 22, 30, 30, 46, 60))
    legend = book.create_sheet("태그 설명")
    legend.append(["tag", "axis", "title"])
    for tag in taxonomy.allowed_tags:
        axis = tag.split("-", 1)[0]
        legend.append([tag, f"{axis} ({taxonomy.axis_titles.get(axis, '')})", taxonomy.titles.get(tag, "")])
    _style(legend, (20, 22, 40), freeze="A2")
    results.append(DeliverableResult("tags", _save("tags", book, stamp), len(papers), sum(1 for p in papers if p.source_id in targets)))

    book, filled = _simple_workbook(papers, ("has_reference", "reference_count"), lambda p: reference_values(reference_summary.get(p.source_id)), (16, 18))
    detail = book.create_sheet(REFERENCE_SHEET)
    detail.append(["paper_index", "record_id", "reference_order", "reference", "reference_type", "reference_year", "reference_doi",
                   "referenced_kci_id", "referenced_record_id"])
    for paper in papers:
        for row in sorted(references.get(paper.source_id, ()), key=lambda item: int(item.get("reference_order") or 0)):
            cited = str(row.get("referenced_kci_source_id") or "")
            detail.append([paper.paper_index if paper.paper_index is not None else "", paper.record_id, row.get("reference_order"),
                           _cell(_reference_text(row)), row.get("reference_type") or "", row.get("reference_publication_year") or "",
                           row.get("reference_doi") or "", cited, record_ids.get(cited, "")])
    _style(detail, (12, 20, 16, 100, 22, 16, 30, 18, 22))
    results.append(DeliverableResult("ref", _save("ref", book, stamp), len(papers), filled))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OA·Special Issue·Type·Tags·참고문헌 결과 워크북((YYMMDD)-….xlsx) 생성")
    parser.add_argument("--date", default=None, help="파일명에 쓸 날짜 YYMMDD (기본: 오늘)")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    args = parser.parse_args(argv)
    if args.date and not re.fullmatch(r"[0-9]{6}", args.date):
        parser.error("--date는 YYMMDD 여섯 자리입니다")
    for result in export_all(args.date, database=Path(args.database)):
        print(f"wrote {result.path} (논문 {result.paper_count}편 중 값이 있는 논문 {result.filled_count}편)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
