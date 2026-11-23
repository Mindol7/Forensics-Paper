"""Map paper target applications to taxonomy categories and export an XLSX.

The source workbook stores one or more application names in ``target_app``.
This module resolves every application through
``Analyzer/LV/Techniques/tech_taxonomy.json`` and inserts a
``target_app_category`` column immediately after ``target_app``.

Unknown applications are an error by design.  Add them only as leaf items
under one of the fixed user-defined categories before rerunning the exporter;
root and intermediate nodes are immutable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openpyxl import load_workbook
from openpyxl.utils import (
    column_index_from_string,
    coordinate_to_tuple,
    get_column_letter,
    range_boundaries,
)
from openpyxl.worksheet.table import TableColumn


ANALYZER_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = ANALYZER_DIR / "Intelligence" / "kci_keyword_intelligence.xlsx"
DEFAULT_OUTPUT = (
    ANALYZER_DIR
    / "Intelligence"
    / "kci_keyword_intelligence_with_app_category.xlsx"
)
DEFAULT_TAXONOMY = ANALYZER_DIR / "LV" / "Techniques" / "tech_taxonomy.json"

APPLICATION_ROOT = "애플리케이션"
TARGET_APP_COLUMN = "target_app"
TARGET_CATEGORY_COLUMN = "target_app_category"
# Application names use commas in the source workbook.  Categories use a pipe
# because several reviewed category labels legitimately contain commas.
CATEGORY_SEPARATOR = " | "

# The user-defined hierarchy is a contract.  New discoveries may only be
# appended as empty-object leaves below these existing nodes; creating a new
# root, bracketed category, or SaaS subgroup is rejected.
FIXED_ROOTS = ("기기", "파일시스템", "운영체제", "애플리케이션", "클라우드")
FIXED_FILESYSTEM_CATEGORIES = (
    "블록 장치 기반 파일시스템",
    "플래시 메모리 파일시스템",
    "네트워크 파일시스템",
    "분산 파일시스템",
    "광학매체 파일시스템",
    "특수 목적 및 기타",
)
FIXED_OS_CATEGORIES = (
    "Windows",
    "Linux",
    "macOS",
    "Android",
    "iOS",
    "특수 목적 및 기타",
)
FIXED_APPLICATION_CATEGORIES = (
    "웹 브라우저",
    "메신저",
    "협업",
    "SNS",
    "이메일",
    "그룹웨어",
    "클라우드 스토리지",
    "파일 전송 및 공유",
    "파일 압축 및 아카이빙",
    "파일 백업 및 복구",
    "문서 보기 및 편집",
    "일정, 연락처 및 개인정보 관리",
    "프로젝트 및 업무 관리",
    "멀티미디어 재생 및 편집",
    "게임",
    "가상자산 지갑",
    "비밀번호 관리 및 사용자 인증",
    "원격 접속 및 원격 제어",
    "VPN, 프록시 및 익명화",
    "보안 및 엔드포인트 관리",
    "소프트웨어 개발",
    "생성형 AI 및 AI 에이전트",
    "웹 서버",
    "데이터베이스 서버",
    "특수 목적 및 기타",
)
FIXED_CLOUD_CATEGORIES = ("SaaS", "DaaS", "IaaS")
FIXED_SAAS_CATEGORIES = (
    "메신저",
    "협업",
    "SNS",
    "이메일",
    "그룹웨어",
    "스토리지",
    "결제&서명",
    "회계",
    "프로젝트 및 업무 관리",
    "생성형 AI",
)
# Leaves explicitly present in the user's original hierarchy are protected as
# seed items.  Additional leaves are allowed, but these cannot be deleted or
# silently moved to another category.
REQUIRED_FILESYSTEM_SEED_LEAVES = {
    "블록 장치 기반 파일시스템": ("NTFS", "Ext4", "Btrfs", "APFS"),
}
REQUIRED_OS_SEED_LEAVES = {
    "Windows": ("Windows 10", "Windows 11", "Windows Server 2025"),
}
REQUIRED_APPLICATION_SEED_LEAVES = {
    "웹 브라우저": ("Chrome", "Edge", "Whale"),
    "메신저": ("KakaoTalk", "WhatsApp"),
    "협업": ("Teams", "Slack"),
    "클라우드 스토리지": ("OneDrive", "Google Drive"),
    "문서 보기 및 편집": (
        "Microsoft Word",
        "Microsoft Excel",
        "한컴오피스 HWP",
    ),
}
REQUIRED_SAAS_SEED_LEAVES = {
    "메신저": ("KakaoTalk",),
    "협업": ("Teams", "Slack"),
    "이메일": ("Gmail", "Naver Mail"),
    "스토리지": ("OneDrive", "Google Drive"),
}


class ApplicationCategoryError(RuntimeError):
    """Raised when taxonomy or workbook data cannot be classified safely."""


@dataclass(frozen=True, slots=True)
class ApplicationTaxonomy:
    """Normalized application-name lookup backed by the reviewed taxonomy."""

    source_path: Path
    categories: tuple[str, ...]
    category_by_normalized_name: Mapping[str, str]
    names_by_normalized_name: Mapping[str, tuple[str, ...]]

    def category_for(self, application: str) -> str | None:
        return self.category_by_normalized_name.get(normalize_name(application))


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Summary returned after a categorized workbook is written."""

    path: Path
    rows: int
    application_mentions: int
    unique_applications: int
    categorized_rows: int
    category_counts: Mapping[str, int]


def normalize_name(value: str) -> str:
    """Normalize an application label for conservative case-insensitive lookup."""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.strip().split())
    return normalized.casefold()


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApplicationCategoryError(f"duplicate JSON key in taxonomy: {key!r}")
        result[key] = value
    return result


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApplicationCategoryError(f"unable to read taxonomy: {path}: {exc}") from exc
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ApplicationCategoryError(
            f"invalid taxonomy JSON: {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise ApplicationCategoryError("taxonomy root must be a JSON object")
    return payload


def _require_exact_children(
    node: Any,
    expected: Sequence[str],
    *,
    path: tuple[str, ...],
) -> Mapping[str, Any]:
    if not isinstance(node, dict):
        raise ApplicationCategoryError(
            f"taxonomy node must be an object: {' > '.join(path)}"
        )
    actual = tuple(node)
    if actual != tuple(expected):
        expected_set = set(expected)
        actual_set = set(actual)
        raise ApplicationCategoryError(
            f"fixed taxonomy hierarchy changed at {' > '.join(path)}: "
            f"missing={sorted(expected_set - actual_set)!r}, "
            f"extra={sorted(actual_set - expected_set)!r}, "
            f"order_matches={actual == tuple(expected)}"
        )
    return node


def _require_leaf_items(node: Any, *, path: tuple[str, ...]) -> None:
    if not isinstance(node, dict):
        raise ApplicationCategoryError(
            f"taxonomy node must be an object: {' > '.join(path)}"
        )
    for item, child in node.items():
        if not isinstance(item, str) or not item.strip():
            raise ApplicationCategoryError(
                f"taxonomy contains an empty leaf at {' > '.join(path)}"
            )
        if not isinstance(child, dict) or child:
            raise ApplicationCategoryError(
                "only lowest-level leaf items may be added; expected an empty "
                f"object at {' > '.join(path + (item,))}"
            )


def _require_seed_leaves(
    node: Mapping[str, Any],
    required: Mapping[str, Sequence[str]],
    *,
    path: tuple[str, ...],
) -> None:
    for category, leaves in required.items():
        category_node = node[category]
        missing = [leaf for leaf in leaves if leaf not in category_node]
        if missing:
            raise ApplicationCategoryError(
                "required taxonomy leaf was deleted or moved at "
                f"{' > '.join(path + (category,))}: missing={missing!r}"
            )


def _validate_fixed_taxonomy_schema(payload: Mapping[str, Any]) -> None:
    roots = _require_exact_children(payload, FIXED_ROOTS, path=("root",))
    _require_leaf_items(roots["기기"], path=("기기",))

    filesystems = _require_exact_children(
        roots["파일시스템"],
        FIXED_FILESYSTEM_CATEGORIES,
        path=("파일시스템",),
    )
    for category in FIXED_FILESYSTEM_CATEGORIES:
        _require_leaf_items(
            filesystems[category], path=("파일시스템", category)
        )
    _require_seed_leaves(
        filesystems,
        REQUIRED_FILESYSTEM_SEED_LEAVES,
        path=("파일시스템",),
    )

    operating_systems = _require_exact_children(
        roots["운영체제"], FIXED_OS_CATEGORIES, path=("운영체제",)
    )
    for category in FIXED_OS_CATEGORIES:
        _require_leaf_items(
            operating_systems[category], path=("운영체제", category)
        )
    _require_seed_leaves(
        operating_systems,
        REQUIRED_OS_SEED_LEAVES,
        path=("운영체제",),
    )

    applications = _require_exact_children(
        roots[APPLICATION_ROOT],
        FIXED_APPLICATION_CATEGORIES,
        path=(APPLICATION_ROOT,),
    )
    for category in FIXED_APPLICATION_CATEGORIES:
        _require_leaf_items(
            applications[category], path=(APPLICATION_ROOT, category)
        )
    _require_seed_leaves(
        applications,
        REQUIRED_APPLICATION_SEED_LEAVES,
        path=(APPLICATION_ROOT,),
    )

    cloud = _require_exact_children(
        roots["클라우드"], FIXED_CLOUD_CATEGORIES, path=("클라우드",)
    )
    saas = _require_exact_children(
        cloud["SaaS"], FIXED_SAAS_CATEGORIES, path=("클라우드", "SaaS")
    )
    for category in FIXED_SAAS_CATEGORIES:
        _require_leaf_items(
            saas[category], path=("클라우드", "SaaS", category)
        )
    _require_seed_leaves(
        saas,
        REQUIRED_SAAS_SEED_LEAVES,
        path=("클라우드", "SaaS"),
    )
    _require_leaf_items(cloud["DaaS"], path=("클라우드", "DaaS"))
    _require_leaf_items(cloud["IaaS"], path=("클라우드", "IaaS"))


def _descendant_names(node: Mapping[str, Any], path: tuple[str, ...]) -> Iterable[str]:
    """Yield product names from a category subtree.

    The fixed-schema validator requires direct children to be empty-object
    leaves.  The recursive implementation remains defensive, but any future
    intermediate node is rejected before this function is reached.
    """

    for name, child in node.items():
        if not isinstance(name, str) or not name.strip():
            raise ApplicationCategoryError(
                f"taxonomy contains an empty application name at {' > '.join(path)}"
            )
        if not isinstance(child, dict):
            raise ApplicationCategoryError(
                f"taxonomy node must be an object: {' > '.join(path + (name,))}"
            )
        yield name
        yield from _descendant_names(child, path + (name,))


def load_application_taxonomy(path: str | Path = DEFAULT_TAXONOMY) -> ApplicationTaxonomy:
    """Load application products and their immediate category from the taxonomy."""

    source_path = Path(path)
    payload = _load_json_object(source_path)
    _validate_fixed_taxonomy_schema(payload)
    application_root = payload.get(APPLICATION_ROOT)
    if not isinstance(application_root, dict) or not application_root:
        raise ApplicationCategoryError(
            f"taxonomy must contain a non-empty {APPLICATION_ROOT!r} object"
        )

    category_by_name: dict[str, str] = {}
    display_names: dict[str, list[str]] = {}
    categories: list[str] = []
    for category, products in application_root.items():
        if not isinstance(category, str) or not category.strip():
            raise ApplicationCategoryError("application category name must not be empty")
        if "|" in category:
            raise ApplicationCategoryError(
                f"application category contains the output delimiter '|': {category!r}"
            )
        if not isinstance(products, dict):
            raise ApplicationCategoryError(
                f"application category must contain an object: {category!r}"
            )
        categories.append(category)
        for application in _descendant_names(products, (APPLICATION_ROOT, category)):
            if "," in application:
                raise ApplicationCategoryError(
                    f"application name contains the workbook delimiter ',': {application!r}"
                )
            key = normalize_name(application)
            previous = category_by_name.get(key)
            if previous is not None and previous != category:
                names = display_names.get(key, []) + [application]
                raise ApplicationCategoryError(
                    "application is assigned to multiple categories: "
                    f"{names!r}: {previous!r}, {category!r}"
                )
            category_by_name[key] = category
            display_names.setdefault(key, []).append(application)

    return ApplicationTaxonomy(
        source_path=source_path,
        categories=tuple(categories),
        category_by_normalized_name=category_by_name,
        names_by_normalized_name={
            key: tuple(values) for key, values in display_names.items()
        },
    )


def split_application_cell(value: object) -> list[str]:
    """Split the workbook's comma-separated target application convention."""

    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r",\s*", text) if part.strip()]


def categorize_applications(
    applications: Sequence[str], taxonomy: ApplicationTaxonomy
) -> tuple[list[str], list[str]]:
    """Return stable unique categories and unknown application names."""

    categories: list[str] = []
    unknown: list[str] = []
    for application in applications:
        category = taxonomy.category_for(application)
        if category is None:
            if application not in unknown:
                unknown.append(application)
            continue
        if category not in categories:
            categories.append(category)
    return categories, unknown


def _header_positions(worksheet) -> dict[str, int]:
    positions: dict[str, int] = {}
    for cell in worksheet[1]:
        value = "" if cell.value is None else str(cell.value).strip()
        if not value:
            continue
        if value in positions:
            raise ApplicationCategoryError(f"duplicate workbook header: {value!r}")
        positions[value] = cell.column
    return positions


def _shift_range_for_insert(
    reference: str,
    inserted_column: int,
    *,
    include_after_column: int | None = None,
) -> str:
    min_col, min_row, max_col, max_row = range_boundaries(reference)
    # Inserting at the first column moves the whole range.  Inserting inside a
    # range expands it, while inserting immediately after a target_app-ending
    # range may explicitly include the new derived column.
    if inserted_column <= min_col:
        min_col += 1
        max_col += 1
    elif min_col <= inserted_column <= max_col or (
        include_after_column is not None
        and min_col <= include_after_column <= max_col
        and inserted_column == max_col + 1
    ):
        max_col += 1
    return (
        f"{get_column_letter(min_col)}{min_row}:"
        f"{get_column_letter(max_col)}{max_row}"
    )


def _copy_cell_style(source, destination) -> None:
    if source.has_style:
        destination._style = copy(source._style)  # noqa: SLF001 - openpyxl API
    destination.number_format = source.number_format
    destination.font = copy(source.font)
    destination.fill = copy(source.fill)
    destination.border = copy(source.border)
    destination.alignment = copy(source.alignment)
    destination.protection = copy(source.protection)


def _filtered_state_present(auto_filter) -> bool:
    if auto_filter is None:
        return False
    return bool(auto_filter.filterColumn) or auto_filter.sortState is not None


def _preflight_column_insert(workbook, worksheet) -> None:
    """Reject workbook features that openpyxl cannot shift without corruption.

    ``Worksheet.insert_cols`` moves cells but deliberately does not rewrite every
    dependent Excel reference.  Failing before publication is safer than silently
    changing a formula, validation rule, filter condition, or drawing target.
    """

    unsupported: list[str] = []
    formula_examples = [
        f"{sheet.title}!{cell.coordinate}"
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
        if cell.data_type == "f"
    ][:5]
    if formula_examples:
        unsupported.append(f"formulas ({', '.join(formula_examples)})")
    if worksheet.data_validations is not None and worksheet.data_validations.count:
        unsupported.append("data validations")
    if len(worksheet.conditional_formatting):
        unsupported.append("conditional formatting")
    if worksheet.merged_cells.ranges:
        unsupported.append("merged cells")
    if any(
        dimension.min is not None
        and dimension.max is not None
        and dimension.min != dimension.max
        for dimension in worksheet.column_dimensions.values()
    ):
        unsupported.append("grouped column dimensions")
    if worksheet._images or worksheet._charts:  # noqa: SLF001 - openpyxl API
        unsupported.append("images or charts")
    if any(
        cell.hyperlink is not None
        for row in worksheet.iter_rows()
        for cell in row
    ):
        unsupported.append("hyperlinks")
    if len(workbook.defined_names):
        unsupported.append("defined names")
    if _filtered_state_present(worksheet.auto_filter):
        unsupported.append("active worksheet filter or sort conditions")
    if any(
        _filtered_state_present(table.autoFilter)
        or getattr(table, "sortState", None) is not None
        for table in worksheet.tables.values()
    ):
        unsupported.append("active table filter or sort conditions")

    if unsupported:
        raise ApplicationCategoryError(
            "cannot safely insert target_app_category while the worksheet contains "
            + ", ".join(unsupported)
        )


def _shift_freeze_panes(worksheet, inserted_column: int) -> None:
    pane = worksheet.freeze_panes
    if pane is None:
        return
    coordinate = pane.coordinate if hasattr(pane, "coordinate") else str(pane)
    row, column = coordinate_to_tuple(coordinate)
    if column >= inserted_column:
        column += 1
        worksheet.freeze_panes = f"{get_column_letter(column)}{row}"


def _shift_column_dimensions(worksheet, inserted_column: int) -> None:
    """Move explicit widths/styles because ``insert_cols`` does not do so."""

    dimensions_to_move = {
        key: copy(dimension)
        for key, dimension in worksheet.column_dimensions.items()
        if column_index_from_string(key) >= inserted_column
    }
    for key in dimensions_to_move:
        del worksheet.column_dimensions[key]
    for key, dimension in sorted(
        dimensions_to_move.items(),
        key=lambda item: column_index_from_string(item[0]),
        reverse=True,
    ):
        old_index = column_index_from_string(key)
        new_index = old_index + 1
        new_key = get_column_letter(new_index)
        dimension.index = new_key
        if dimension.min is not None:
            dimension.min += 1
        if dimension.max is not None:
            dimension.max += 1
        worksheet.column_dimensions[new_key] = dimension


def _insert_category_column(worksheet, target_column: int) -> int:
    category_column = target_column + 1
    _preflight_column_insert(worksheet.parent, worksheet)
    original_filter = worksheet.auto_filter.ref
    original_table_refs = {
        table.name: table.ref for table in worksheet.tables.values()
    }

    worksheet.insert_cols(category_column, 1)
    _shift_freeze_panes(worksheet, category_column)
    _shift_column_dimensions(worksheet, category_column)
    for row_index in range(1, worksheet.max_row + 1):
        _copy_cell_style(
            worksheet.cell(row=row_index, column=target_column),
            worksheet.cell(row=row_index, column=category_column),
        )
    worksheet.cell(row=1, column=category_column, value=TARGET_CATEGORY_COLUMN)
    worksheet.column_dimensions[get_column_letter(category_column)].width = 36

    if original_filter:
        worksheet.auto_filter.ref = _shift_range_for_insert(
            original_filter,
            category_column,
            include_after_column=target_column,
        )
    for table_name, original_ref in original_table_refs.items():
        table = worksheet.tables[table_name]
        table.ref = _shift_range_for_insert(
            original_ref,
            category_column,
            include_after_column=target_column,
        )
        min_col, _, max_col, _ = range_boundaries(original_ref)
        if min_col <= target_column <= max_col and category_column <= max_col + 1:
            relative_index = category_column - min_col
            columns = list(table.tableColumns)
            if columns:
                columns.insert(
                    relative_index,
                    TableColumn(
                        id=relative_index + 1,
                        name=TARGET_CATEGORY_COLUMN,
                    ),
                )
            else:
                # Some newly-created openpyxl tables initialize their columns
                # only when first saved.  Build the complete list explicitly.
                new_min_col, header_row, new_max_col, _ = range_boundaries(table.ref)
                columns = [
                    TableColumn(
                        id=index,
                        name=str(
                            worksheet.cell(
                                row=header_row,
                                column=column,
                            ).value
                            or ""
                        ),
                    )
                    for index, column in enumerate(
                        range(new_min_col, new_max_col + 1), start=1
                    )
                ]
            for index, column in enumerate(columns, start=1):
                column.id = index
            table.tableColumns = columns
        if table.autoFilter is not None:
            table.autoFilter.ref = table.ref
    return category_column


def _atomic_save(workbook, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_mode = (
        destination.stat().st_mode & 0o777 if destination.exists() else 0o644
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".xlsx", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        try:
            workbook.save(temporary)
        finally:
            # Closing before os.replace also makes in-place export work on
            # platforms that lock an open source XLSX, notably Windows.
            workbook.close()
        # Reopen before replacement so a truncated/corrupt archive is never published.
        verification = load_workbook(temporary, read_only=True, data_only=True)
        verification.close()
        os.chmod(temporary, destination_mode)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def export_application_categories(
    input_path: str | Path = DEFAULT_INPUT,
    output_path: str | Path = DEFAULT_OUTPUT,
    taxonomy_path: str | Path = DEFAULT_TAXONOMY,
    *,
    sheet_name: str | None = None,
) -> ExportResult:
    """Insert application categories into a copy of the keyword workbook."""

    source = Path(input_path)
    destination = Path(output_path)
    taxonomy = load_application_taxonomy(taxonomy_path)
    try:
        workbook = load_workbook(source)
    except Exception as exc:
        raise ApplicationCategoryError(
            f"unable to open source workbook: {source}: {exc}"
        ) from exc

    if sheet_name is None:
        worksheet = workbook.active
    elif sheet_name in workbook.sheetnames:
        worksheet = workbook[sheet_name]
    else:
        workbook.close()
        raise ApplicationCategoryError(
            f"worksheet not found: {sheet_name!r}; available={workbook.sheetnames!r}"
        )

    try:
        headers = _header_positions(worksheet)
    except Exception:
        workbook.close()
        raise
    target_column = headers.get(TARGET_APP_COLUMN)
    if target_column is None:
        workbook.close()
        raise ApplicationCategoryError(
            f"source workbook lacks required column: {TARGET_APP_COLUMN!r}"
        )
    category_column = headers.get(TARGET_CATEGORY_COLUMN)
    if category_column is None:
        try:
            category_column = _insert_category_column(worksheet, target_column)
        except Exception:
            workbook.close()
            raise
    elif category_column != target_column + 1:
        workbook.close()
        raise ApplicationCategoryError(
            f"{TARGET_CATEGORY_COLUMN!r} must immediately follow "
            f"{TARGET_APP_COLUMN!r}"
        )

    unknown_by_row: dict[int, list[str]] = {}
    category_counts: Counter[str] = Counter()
    unique_applications: set[str] = set()
    application_mentions = 0
    categorized_rows = 0
    values_by_row: dict[int, str | None] = {}
    for row_index in range(2, worksheet.max_row + 1):
        applications = split_application_cell(
            worksheet.cell(row=row_index, column=target_column).value
        )
        application_mentions += len(applications)
        # Report the source workbook's exact labels.  Case variants such as
        # FaceSwap/Faceswap remain distinct source values even though lookup is
        # deliberately case-insensitive.
        unique_applications.update(applications)
        categories, unknown = categorize_applications(applications, taxonomy)
        if unknown:
            unknown_by_row[row_index] = unknown
        if categories:
            categorized_rows += 1
            category_counts.update(categories)
            values_by_row[row_index] = CATEGORY_SEPARATOR.join(categories)
        else:
            values_by_row[row_index] = None

    if unknown_by_row:
        details = "; ".join(
            f"row {row}: {', '.join(values)}"
            for row, values in list(unknown_by_row.items())[:30]
        )
        remaining = len(unknown_by_row) - 30
        if remaining > 0:
            details += f"; ... and {remaining} more row(s)"
        workbook.close()
        raise ApplicationCategoryError(
            "target_app contains applications missing from the taxonomy: " + details
        )

    for row_index, value in values_by_row.items():
        cell = worksheet.cell(row=row_index, column=category_column)
        cell.value = value
        if value is not None:
            cell.data_type = "s"

    _atomic_save(workbook, destination)
    return ExportResult(
        path=destination,
        rows=max(worksheet.max_row - 1, 0),
        application_mentions=application_mentions,
        unique_applications=len(unique_applications),
        categorized_rows=categorized_rows,
        category_counts=dict(category_counts),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Map target_app values through the reviewed application taxonomy and "
            "write target_app_category to a new XLSX workbook."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--sheet", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = export_application_categories(
            args.input,
            args.output,
            args.taxonomy,
            sheet_name=args.sheet,
        )
    except ApplicationCategoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(result.path),
                "rows": result.rows,
                "application_mentions": result.application_mentions,
                "unique_applications": result.unique_applications,
                "categorized_rows": result.categorized_rows,
                "category_counts": result.category_counts,
                "errors": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
