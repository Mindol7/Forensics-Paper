from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from analysis_fixtures import analysis_record

from Analyzer.MD.paper_md import TypeRecord, _load_keywords
from Analyzer.keyword_workbook import (
    BASE_COLUMNS,
    COLUMNS,
    KeywordWorkbookError,
    assemble_rows,
    build_keyword_workbooks,
    load_analysis_rows,
    load_legacy_snapshot,
    row_from_analysis_record,
)


def _snapshot(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        json.dumps({"schema_version": 1, "rows": rows}, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _legacy_row(source_id: str, title: str) -> dict:
    return {
        "source_id": source_id,
        "doi": None,
        "title": title,
        "target_device": "PC",
        "target_os": "Windows",
        "target_app": "Dropbox",
        "target_app_category": "클라우드 스토리지",
        "filesystem": "NTFS",
        "cloud": "SaaS",
    }


def test_analysis_record_projects_onto_the_legacy_columns() -> None:
    record = analysis_record(source_id="ART000000009", title="새 논문")
    record["keywords"]["application"] = [
        {"name": "KakaoTalk", "category": "메신저"},
        {"name": "Telegram", "category": "메신저"},
        {"name": "Dropbox", "category": "클라우드 스토리지"},
    ]
    record["keywords"]["device"] = ["PC", "스마트폰"]
    row = row_from_analysis_record(record)
    assert row.source_id == "ART000000009"
    assert row.doi == "10.1234/example"
    assert row.target_device == "PC, 스마트폰"
    assert row.target_app == "KakaoTalk, Telegram, Dropbox"
    assert row.target_app_category == "메신저 | 클라우드 스토리지"
    assert row.origin == "analysis"


def test_build_merges_sources_with_analysis_taking_precedence(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path / "legacy.json",
        [_legacy_row("ART000000001", "레거시 제목"), _legacy_row("ART000000002", "둘째 논문")],
    )
    analysis_dir = tmp_path / "Analysis"
    analysis_dir.mkdir()
    for source_id, title in (("ART000000001", "재분석 제목"), ("ART000000003", "새 논문")):
        (analysis_dir / f"{source_id}.json").write_text(
            json.dumps(analysis_record(source_id=source_id, title=title), ensure_ascii=False),
            encoding="utf-8",
        )
    (analysis_dir / "ART000000004.json").write_text(
        json.dumps({**analysis_record(source_id="ART000000004"), "dry_run": True}), encoding="utf-8"
    )

    result = build_keyword_workbooks(
        legacy_snapshot=snapshot,
        analysis_dir=analysis_dir,
        output=tmp_path / "with_category.xlsx",
        base_output=tmp_path / "base.xlsx",
    )
    assert (result.row_count, result.legacy_count, result.analysis_count) == (3, 1, 2)

    workbook = load_workbook(result.path, read_only=True, data_only=True)
    rows = list(workbook["keyword_intelligence"].iter_rows(values_only=True))
    workbook.close()
    assert rows[0] == COLUMNS
    assert [row[0] for row in rows[1:]] == ["ART000000001", "ART000000002", "ART000000003"]
    assert rows[1][2] == "재분석 제목"  # analysis overrides the legacy row
    assert rows[1][6] == "클라우드 스토리지"
    base = load_workbook(result.base_path, read_only=True, data_only=True)
    assert next(base["keyword_intelligence"].iter_rows(values_only=True)) == BASE_COLUMNS
    base.close()

    # The MD generator now joins by identity, including papers whose DB title
    # differs from the index title.
    types = [
        TypeRecord(
            paper_index=index,
            source_id=source_id,
            title="다른 표기 " + source_id,
            title_kor="t",
            title_eng="t",
            doi=None,
            subtype="ar",
            subtype_des="Article",
            pdf_filename="x.pdf",
            pdf_sha256="0" * 64,
        )
        for index, source_id in enumerate(("ART000000001", "ART000000003"), start=1)
    ]
    keywords = _load_keywords(types, workbook_path=result.path)
    assert keywords["ART000000001"].title == "재분석 제목"
    assert keywords["ART000000003"].target_app == "Dropbox"
    with pytest.raises(Exception, match="keyword row is missing"):
        _load_keywords([*types, TypeRecord(3, "ART000000005", "t", "t", "t", None, "ar", "Article", "x.pdf", "0" * 64)], workbook_path=result.path)


def test_missing_corpus_papers_are_reported_or_fatal(tmp_path: Path) -> None:
    legacy = load_legacy_snapshot(_snapshot(tmp_path / "legacy.json", [_legacy_row("ART000000001", "a")]))
    rows, missing = assemble_rows(legacy, {}, source_ids=["ART000000001", "ART000000002"])
    assert [row.source_id for row in rows] == ["ART000000001"]
    assert missing == ("ART000000002",)
    assert load_analysis_rows(tmp_path / "absent") == {}
    with pytest.raises(KeywordWorkbookError, match="without keyword rows"):
        build_keyword_workbooks(
            legacy_snapshot=tmp_path / "legacy.json",
            analysis_dir=tmp_path / "absent",
            output=tmp_path / "out.xlsx",
            base_output=None,
            source_ids=["ART000000001", "ART000000002"],
            strict=True,
        )
    with pytest.raises(KeywordWorkbookError, match="no keyword rows"):
        build_keyword_workbooks(
            legacy_snapshot=tmp_path / "none.json",
            analysis_dir=tmp_path / "absent",
            output=tmp_path / "out.xlsx",
            base_output=None,
        )
