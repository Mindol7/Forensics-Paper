from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table

from Analyzer.application_category import (
    ApplicationCategoryError,
    DEFAULT_INPUT,
    DEFAULT_TAXONOMY,
    FIXED_APPLICATION_CATEGORIES,
    FIXED_CLOUD_CATEGORIES,
    FIXED_FILESYSTEM_CATEGORIES,
    FIXED_OS_CATEGORIES,
    FIXED_ROOTS,
    FIXED_SAAS_CATEGORIES,
    REQUIRED_APPLICATION_SEED_LEAVES,
    REQUIRED_FILESYSTEM_SEED_LEAVES,
    REQUIRED_OS_SEED_LEAVES,
    REQUIRED_SAAS_SEED_LEAVES,
    categorize_applications,
    export_application_categories,
    load_application_taxonomy,
    split_application_cell,
)


def _write_taxonomy(path: Path, applications: dict[str, dict[str, object]]) -> Path:
    application_tree: dict[str, dict[str, object]] = {
        category: {
            leaf: {} for leaf in REQUIRED_APPLICATION_SEED_LEAVES.get(category, ())
        }
        for category in FIXED_APPLICATION_CATEGORIES
    }
    for category, products in applications.items():
        if category in application_tree:
            application_tree[category].update(products)
        else:
            application_tree[category] = products
    taxonomy = {
        "기기": {},
        "파일시스템": {
            category: {
                leaf: {}
                for leaf in REQUIRED_FILESYSTEM_SEED_LEAVES.get(category, ())
            }
            for category in FIXED_FILESYSTEM_CATEGORIES
        },
        "운영체제": {
            category: {leaf: {} for leaf in REQUIRED_OS_SEED_LEAVES.get(category, ())}
            for category in FIXED_OS_CATEGORIES
        },
        "애플리케이션": application_tree,
        "클라우드": {
            "SaaS": {
                category: {
                    leaf: {} for leaf in REQUIRED_SAAS_SEED_LEAVES.get(category, ())
                }
                for category in FIXED_SAAS_CATEGORIES
            },
            "DaaS": {},
            "IaaS": {},
        },
    }
    assert tuple(taxonomy) == FIXED_ROOTS
    assert tuple(taxonomy["클라우드"]) == FIXED_CLOUD_CATEGORIES
    path.write_text(
        json.dumps(taxonomy, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_workbook(path: Path, rows: list[list[object]]) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "keyword_intelligence"
    worksheet.append(
        [
            "doi",
            "title",
            "target_device",
            "target_os",
            "target_app",
            "filesystem",
            "cloud",
        ]
    )
    for row in rows:
        worksheet.append(row)
    worksheet.auto_filter.ref = f"A1:G{worksheet.max_row}"
    worksheet.freeze_panes = "A2"
    worksheet.column_dimensions["F"].width = 21
    worksheet.column_dimensions["G"].width = 22
    worksheet.add_table(
        Table(displayName="KeywordIntelligenceTable", ref=f"A1:G{worksheet.max_row}")
    )
    workbook.save(path)
    return path


def test_fixed_application_categories_match_user_contract() -> None:
    assert FIXED_APPLICATION_CATEGORIES == (
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


@pytest.mark.skip(
    reason=(
        "Analyzer/LV/Techniques/tech_taxonomy.json is the technique taxonomy; the application "
        "taxonomy file this contract describes is no longer in the repository"
    ),
)
def test_project_taxonomy_keeps_fixed_nodes_and_covers_source_apps() -> None:
    taxonomy = load_application_taxonomy(DEFAULT_TAXONOMY)
    assert taxonomy.categories == FIXED_APPLICATION_CATEGORIES

    payload = json.loads(DEFAULT_TAXONOMY.read_text(encoding="utf-8"))
    assert tuple(payload) == FIXED_ROOTS
    assert tuple(payload["파일시스템"]) == FIXED_FILESYSTEM_CATEGORIES
    assert tuple(payload["운영체제"]) == FIXED_OS_CATEGORIES
    assert tuple(payload["애플리케이션"]) == FIXED_APPLICATION_CATEGORIES
    assert tuple(payload["클라우드"]) == FIXED_CLOUD_CATEGORIES
    assert tuple(payload["클라우드"]["SaaS"]) == FIXED_SAAS_CATEGORIES
    assert "음성 녹음" not in payload["애플리케이션"]
    assert taxonomy.category_for("Easy Voice Recorder") == (
        "멀티미디어 재생 및 편집"
    )

    workbook = load_workbook(DEFAULT_INPUT, read_only=True, data_only=True)
    worksheet = workbook.active
    headers = next(worksheet.iter_rows(values_only=True))
    target_app_index = headers.index("target_app")
    source_applications = {
        application
        for row in worksheet.iter_rows(min_row=2, values_only=True)
        for application in split_application_cell(row[target_app_index])
    }
    workbook.close()

    assert source_applications
    assert not {
        application
        for application in source_applications
        if taxonomy.category_for(application) is None
    }


def test_load_and_categorize_taxonomy_case_insensitively(tmp_path: Path) -> None:
    taxonomy = load_application_taxonomy(
        _write_taxonomy(
            tmp_path / "taxonomy.json",
            {
                "메신저": {"KakaoTalk": {}, "WhatsApp": {}},
                "협업": {"Slack": {}},
            },
        )
    )

    categories, unknown = categorize_applications(
        ["kakaotalk", "WhatsApp", "KakaoTalk", "Slack"], taxonomy
    )

    assert categories == ["메신저", "협업"]
    assert unknown == []


def test_category_separator_does_not_conflict_with_commas_in_category_names(
    tmp_path: Path,
) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [[None, "paper", None, None, "Browsec VPN, Slack", None, None]],
    )
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json",
        {
            "VPN, 프록시 및 익명화": {"Browsec VPN": {}},
            "협업": {"Slack": {}},
        },
    )
    destination = tmp_path / "output.xlsx"

    export_application_categories(source, destination, taxonomy)

    assert load_workbook(destination).active["F2"].value == (
        "VPN, 프록시 및 익명화 | 협업"
    )


def test_rejects_normalized_name_in_multiple_categories(tmp_path: Path) -> None:
    path = _write_taxonomy(
        tmp_path / "taxonomy.json",
        {
            "메신저": {"KakaoTalk": {}},
            "협업": {"kakaotalk": {}},
        },
    )

    with pytest.raises(ApplicationCategoryError, match="multiple categories"):
        load_application_taxonomy(path)


def test_rejects_new_intermediate_category(tmp_path: Path) -> None:
    path = _write_taxonomy(
        tmp_path / "taxonomy.json",
        {"음성 녹음": {"Easy Voice Recorder": {}}},
    )

    with pytest.raises(ApplicationCategoryError, match="fixed taxonomy hierarchy"):
        load_application_taxonomy(path)


def test_rejects_nested_application_node(tmp_path: Path) -> None:
    path = _write_taxonomy(
        tmp_path / "taxonomy.json",
        {
            "멀티미디어 재생 및 편집": {
                "Easy Voice Recorder": {"new subgroup": {}}
            }
        },
    )

    with pytest.raises(ApplicationCategoryError, match="only lowest-level leaf"):
        load_application_taxonomy(path)


def test_rejects_deleting_or_moving_original_seed_leaf(tmp_path: Path) -> None:
    path = _write_taxonomy(tmp_path / "taxonomy.json", {})
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["애플리케이션"]["웹 브라우저"]["Chrome"]
    payload["애플리케이션"]["특수 목적 및 기타"]["Chrome"] = {}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ApplicationCategoryError, match="deleted or moved"):
        load_application_taxonomy(path)


def test_exports_category_column_and_updates_table_range(tmp_path: Path) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [
            [None, "paper 1", None, None, "KakaoTalk, WhatsApp", None, None],
            [None, "paper 2", None, None, "KakaoTalk, Slack", None, None],
            [None, "paper 3", None, None, None, None, None],
        ],
    )
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json",
        {
            "메신저": {"KakaoTalk": {}, "WhatsApp": {}},
            "협업": {"Slack": {}},
        },
    )
    destination = tmp_path / "categorized.xlsx"

    result = export_application_categories(source, destination, taxonomy)

    assert result.rows == 3
    assert result.application_mentions == 4
    assert result.unique_applications == 3
    assert result.categorized_rows == 2
    workbook = load_workbook(destination, data_only=True)
    worksheet = workbook.active
    assert [cell.value for cell in worksheet[1]] == [
        "doi",
        "title",
        "target_device",
        "target_os",
        "target_app",
        "target_app_category",
        "filesystem",
        "cloud",
    ]
    assert worksheet["F2"].value == "메신저"
    assert worksheet["F3"].value == "메신저 | 협업"
    assert worksheet["F4"].value is None
    assert worksheet.auto_filter.ref == "A1:H4"
    assert worksheet.column_dimensions["F"].width == 36
    assert worksheet.column_dimensions["G"].width == 21
    assert worksheet.column_dimensions["H"].width == 22
    assert worksheet.tables["KeywordIntelligenceTable"].ref == "A1:H4"
    assert [
        column.name
        for column in worksheet.tables["KeywordIntelligenceTable"].tableColumns
    ] == [
        "doi",
        "title",
        "target_device",
        "target_os",
        "target_app",
        "target_app_category",
        "filesystem",
        "cloud",
    ]
    assert load_workbook(source, read_only=True).active.max_column == 7


def test_unknown_application_fails_without_publishing_output(tmp_path: Path) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [[None, "paper", None, None, "Unknown App", None, None]],
    )
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json", {"메신저": {"KakaoTalk": {}}}
    )
    destination = tmp_path / "categorized.xlsx"

    with pytest.raises(ApplicationCategoryError, match="missing from the taxonomy"):
        export_application_categories(source, destination, taxonomy)

    assert not destination.exists()


def test_table_ending_at_target_app_expands_to_include_category(
    tmp_path: Path,
) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [[None, "paper", None, None, "KakaoTalk", None, None]],
    )
    workbook = load_workbook(source)
    worksheet = workbook.active
    del worksheet.tables["KeywordIntelligenceTable"]
    worksheet.add_table(Table(displayName="TargetTable", ref="A1:E2"))
    workbook.save(source)
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json", {"메신저": {"KakaoTalk": {}}}
    )
    destination = tmp_path / "categorized.xlsx"

    export_application_categories(source, destination, taxonomy)

    worksheet = load_workbook(destination).active
    table = worksheet.tables["TargetTable"]
    assert table.ref == "A1:F2"
    assert [column.name for column in table.tableColumns] == [
        "doi",
        "title",
        "target_device",
        "target_os",
        "target_app",
        "target_app_category",
    ]


def test_rejects_existing_category_column_in_wrong_position(tmp_path: Path) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [[None, "paper", None, None, "KakaoTalk", None, None]],
    )
    workbook = load_workbook(source)
    worksheet = workbook.active
    worksheet.insert_cols(1)
    worksheet["A1"] = "target_app_category"
    workbook.save(source)
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json", {"메신저": {"KakaoTalk": {}}}
    )

    with pytest.raises(ApplicationCategoryError, match="must immediately follow"):
        export_application_categories(source, tmp_path / "output.xlsx", taxonomy)


def test_rejects_formula_before_column_insertion(tmp_path: Path) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [[None, "paper", None, None, "KakaoTalk", None, None]],
    )
    workbook = load_workbook(source)
    workbook.active["G2"] = "=E2"
    workbook.save(source)
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json", {"메신저": {"KakaoTalk": {}}}
    )
    destination = tmp_path / "output.xlsx"

    with pytest.raises(ApplicationCategoryError, match="formulas"):
        export_application_categories(source, destination, taxonomy)

    assert not destination.exists()


def test_freeze_pane_to_right_of_insert_is_shifted(tmp_path: Path) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [[None, "paper", None, None, "KakaoTalk", None, None]],
    )
    workbook = load_workbook(source)
    workbook.active.freeze_panes = "F2"
    workbook.save(source)
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json", {"메신저": {"KakaoTalk": {}}}
    )
    destination = tmp_path / "output.xlsx"

    export_application_categories(source, destination, taxonomy)

    assert load_workbook(destination).active.freeze_panes == "G2"


def test_table_starting_at_insert_column_moves_as_a_whole(tmp_path: Path) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [[None, "paper", None, None, "KakaoTalk", None, None]],
    )
    workbook = load_workbook(source)
    worksheet = workbook.active
    del worksheet.tables["KeywordIntelligenceTable"]
    worksheet.add_table(Table(displayName="RightTable", ref="F1:G2"))
    workbook.save(source)
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json", {"메신저": {"KakaoTalk": {}}}
    )
    destination = tmp_path / "output.xlsx"

    export_application_categories(source, destination, taxonomy)

    table = load_workbook(destination).active.tables["RightTable"]
    assert table.ref == "G1:H2"
    assert len(table.tableColumns) == 2


def test_rejects_formula_on_another_sheet_before_insertion(tmp_path: Path) -> None:
    source = _write_workbook(
        tmp_path / "source.xlsx",
        [[None, "paper", None, None, "KakaoTalk", None, None]],
    )
    workbook = load_workbook(source)
    workbook.create_sheet("summary")["A1"] = "=keyword_intelligence!F2"
    workbook.save(source)
    taxonomy = _write_taxonomy(
        tmp_path / "taxonomy.json", {"메신저": {"KakaoTalk": {}}}
    )
    destination = tmp_path / "output.xlsx"

    with pytest.raises(ApplicationCategoryError, match="summary!A1"):
        export_application_categories(source, destination, taxonomy)

    assert not destination.exists()
