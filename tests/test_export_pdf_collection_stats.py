from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from openpyxl import load_workbook

from PaperPipeline.paper_files import (
    PAPER_FILE_INDEX_NAME,
    build_paper_file_records,
    paper_file_index_document,
)

from scripts.export_pdf_collection_stats import export_report


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE kci_papers (
                id INTEGER PRIMARY KEY,
                source TEXT,
                source_id TEXT,
                title TEXT,
                title_kor TEXT,
                title_eng TEXT,
                doi TEXT,
                url TEXT,
                publication_year INTEGER,
                first_page TEXT,
                final_page TEXT,
                journal_id TEXT,
                institution_id TEXT,
                issue_id TEXT,
                authors_json TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO kci_papers VALUES (
                ?, 'kci', ?, ?, ?, '', ?, '', 2024, '1', '10', ?, ?, '1_1', '[]'
            )
            """,
            [
                (1, "ART000000001", "수집 논문", "수집 논문", "10.1/a", "학술지 A", "학회 A"),
                (2, "ART000000002", "미수집 논문", "미수집 논문", "10.1/b", "학술지 A", "학회 A"),
                (3, "ART000000003", "수집 논문 2", "수집 논문 2", "10.1/c", "학술지 B", "학회 B"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _write_download(
    papers_dir: Path,
    source_id: str,
    resolver: str,
    host: str,
    *,
    manifest_dir: Path | None = None,
    pdf_filename: str | None = None,
) -> None:
    state_dir = manifest_dir or papers_dir
    content = b"%PDF-1.4\n%%EOF\n"
    pdf = papers_dir / (pdf_filename or f"{source_id}.pdf")
    pdf.write_bytes(content)
    manifest = {
        "status": "downloaded",
        "paper": {"source_id": source_id},
        "resolver": {
            "name": resolver,
            "landing_url": f"https://{host}/article",
            "download_url": f"https://{host}/paper.pdf?token=secret",
        },
        "pdf": {
            "path": pdf.name,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "page_count": 1,
        },
        "last_error": None,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{source_id}.download.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def test_exports_reconciling_society_and_route_sheets(tmp_path: Path) -> None:
    database = tmp_path / "papers.db"
    papers_dir = tmp_path / "Papers"
    output = tmp_path / "stats.xlsx"
    papers_dir.mkdir()
    _create_database(database)
    _write_download(papers_dir, "ART000000001", "kci-ui", "www.kci.go.kr")
    _write_download(
        papers_dir,
        "ART000000003",
        "publisher-metadata",
        "koreascience.or.kr",
    )
    (papers_dir / "ART000000002.download.json").write_text(
        json.dumps(
            {
                "status": "not_found",
                "paper": {"source_id": "ART000000002"},
                "resolver": None,
                "pdf": None,
                "last_error": {"code": "not_found", "message": "not found"},
            }
        ),
        encoding="utf-8",
    )

    summary = export_report(database, papers_dir, output)

    assert summary["target_count"] == 3
    assert summary["collected_count"] == 2
    assert summary["missing_count"] == 1
    assert summary["society_count"] == 2
    assert summary["integrity_failures"] == []

    workbook = load_workbook(output, read_only=True, data_only=True)
    assert workbook.sheetnames == [
        "요약",
        "학회별 통계",
        "학회×수집경로",
        "수집경로 요약",
        "논문별 상세",
        "미수집 목록",
    ]
    society = workbook["학회별 통계"]
    assert tuple(cell.value for cell in society[2][:7]) == (
        1,
        "학회 A",
        2,
        1,
        1,
        1,
        0.5,
    )
    route = workbook["수집경로 요약"]
    route_rows = [tuple(cell.value for cell in row) for row in route.iter_rows(min_row=2)]
    assert any(row[:4] == ("KCI 공개 상세페이지 다운로드", "kci-ui", "www.kci.go.kr", 1) for row in route_rows)
    assert any(row[:4] == ("KoreaScience 표준 PDF 메타데이터", "publisher-metadata", "koreascience.or.kr", 1) for row in route_rows)
    detail = workbook["논문별 상세"]
    headers = [cell.value for cell in detail[1]]
    download_url_index = headers.index("Download URL (민감값 마스킹)") + 1
    first_url = detail.cell(row=2, column=download_url_index).value
    assert "secret" not in first_url
    assert "%5BREDACTED%5D" in first_url
    assert workbook["미수집 목록"].max_row == 2


def test_export_can_reuse_valid_legacy_artifact_root(tmp_path: Path) -> None:
    database = tmp_path / "papers.db"
    papers_dir = tmp_path / "Analyzer" / "Paper"
    legacy_dir = tmp_path / "Papers"
    output = tmp_path / "stats.xlsx"
    papers_dir.mkdir(parents=True)
    legacy_dir.mkdir()
    _create_database(database)
    _write_download(legacy_dir, "ART000000001", "kci-ui", "www.kci.go.kr")

    summary = export_report(
        database,
        papers_dir,
        output,
        compatibility_papers_dirs=(legacy_dir,),
    )

    assert summary["target_count"] == 3
    assert summary["collected_count"] == 1


def test_export_joins_split_pdf_and_manifest_roots(tmp_path: Path) -> None:
    database = tmp_path / "papers.db"
    papers_dir = tmp_path / "Analyzer" / "Paper"
    manifest_dir = tmp_path / "Analyzer" / "Manifest"
    output = tmp_path / "stats.xlsx"
    papers_dir.mkdir(parents=True)
    _create_database(database)
    _write_download(
        papers_dir,
        "ART000000001",
        "kci-ui",
        "www.kci.go.kr",
        manifest_dir=manifest_dir,
    )

    summary = export_report(
        database,
        papers_dir,
        output,
        manifest_dir=manifest_dir,
    )

    assert summary["target_count"] == 3
    assert summary["collected_count"] == 1
    assert summary["manifest_files"] == 1
    assert summary["pdf_files"] == 1
    assert summary["integrity_failures"] == []


def test_export_resolves_title_pdf_and_reports_legacy_duplicate_as_orphan(
    tmp_path: Path,
) -> None:
    database = tmp_path / "papers.db"
    papers_dir = tmp_path / "Analyzer" / "Paper"
    manifest_dir = tmp_path / "Analyzer" / "Manifest"
    output = tmp_path / "stats.xlsx"
    papers_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    _create_database(database)
    records = build_paper_file_records(
        [
            ("ART000000001", "수집 논문"),
            ("ART999999999", "DB 밖 논문"),
        ]
    )
    (manifest_dir / PAPER_FILE_INDEX_NAME).write_text(
        json.dumps(paper_file_index_document(records), ensure_ascii=False),
        encoding="utf-8",
    )
    _write_download(
        papers_dir,
        "ART000000001",
        "kci-ui",
        "www.kci.go.kr",
        manifest_dir=manifest_dir,
        pdf_filename="수집 논문.pdf",
    )
    (papers_dir / "ART000000001.pdf").write_bytes(b"%PDF-legacy duplicate")
    (papers_dir / "DB 밖 논문.pdf").write_bytes(b"%PDF-orphan")

    summary = export_report(
        database,
        papers_dir,
        output,
        manifest_dir=manifest_dir,
    )

    assert summary["collected_count"] == 1
    assert summary["pdf_files"] == 3
    assert summary["orphan_pdfs"] == ["ART000000001.pdf", "DB 밖 논문.pdf"]
