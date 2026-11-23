from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from Core.normalizer import NormalizedPaper
from Storage.db import DatabaseManager, sqlite_database_path


EXPECTED_KCI_COLUMNS = {
    "id",
    "source",
    "source_id",
    "title",
    "title_kor",
    "title_eng",
    "title_other",
    "doi",
    "uci",
    "url",
    "abstract",
    "abstract_kor",
    "abstract_eng",
    "abstract_other",
    "keywords_json",
    "authors_json",
    "journal_id",
    "institution_id",
    "issue_id",
    "first_page",
    "final_page",
    "page_count",
    "issn",
    "eissn",
    "subject_code",
    "is_fulltext",
    "registered_at",
    "updated_at",
    "publication_year",
    "category",
    "matched_keywords_json",
    "relevance_score",
    "relevance_reasons_json",
    "is_relevant",
    "summary",
    "raw_payload_json",
}


def _paper(*, title: str) -> NormalizedPaper:
    return NormalizedPaper(
        source="kci",
        source_id="ART000000001",
        title=title,
        title_kor=title,
        title_eng=None,
        title_other=None,
        doi="10.1234/example",
        uci=None,
        url=None,
        abstract=None,
        abstract_kor=None,
        abstract_eng=None,
        abstract_other=None,
        categories=["디지털 포렌식"],
        matched_keywords=["디지털 포렌식"],
        relevance_reasons=["matched_in:title"],
        is_relevant=True,
        publication_year=2026,
    )


def test_sqlite_storage_preserves_current_kci_columns(tmp_path: Path) -> None:
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'papers.db'}")
    manager.create_tables()

    assert manager.dialect_name == "sqlite"
    columns = {column["name"] for column in inspect(manager.engine).get_columns("kci_papers")}
    assert columns == EXPECTED_KCI_COLUMNS
    manager.close()


def test_upsert_is_idempotent_by_source_id(tmp_path: Path) -> None:
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'papers.db'}")
    manager.create_tables()

    manager.upsert_papers([_paper(title="초기 제목")])
    manager.upsert_papers([_paper(title="수정 제목")])

    assert manager.count_papers() == 1
    stored = list(manager.iter_kci_papers())
    assert stored[0].title == "수정 제목"
    manager.close()


def test_sqlite_database_path_resolves_file_url(tmp_path: Path) -> None:
    path = tmp_path / "papers.db"
    assert sqlite_database_path(f"sqlite:///{path}") == path.resolve()


def test_year_range_hygiene_can_be_scoped_to_rows_after_a_checkpoint(tmp_path: Path) -> None:
    database = DatabaseManager(f"sqlite:///{tmp_path / 'scoped.db'}")
    database.create_tables()

    def paper(source_id: str, year: int) -> NormalizedPaper:
        record = _paper(title=f"논문 {source_id}")
        record.source_id = source_id
        record.publication_year = year
        return record

    # Two rows already accepted by earlier runs, one of them outside the window.
    database.upsert_papers([paper("ART000000001", 2020), paper("ART000000002", 2024)])
    checkpoint = database.max_paper_id()
    database.upsert_papers([paper("ART000000003", 2019), paper("ART000000004", 2025)])

    removed = database.delete_kci_papers_outside_year_range(
        start_year=2022, end_year=2026, min_id=checkpoint + 1
    )

    assert removed == 1
    assert database.kci_paper_source_ids() == [
        "ART000000001", "ART000000002", "ART000000004",
    ]
    assert database.kci_paper_source_ids(min_id=checkpoint + 1) == ["ART000000004"]
    # Without a checkpoint the sweep still covers the whole table.
    assert database.delete_kci_papers_outside_year_range(start_year=2022, end_year=2026) == 1
    assert database.max_paper_id() == 4

