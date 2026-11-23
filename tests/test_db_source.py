from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from PaperPipeline.db_source import (
    InvalidDoiError,
    count_paper_candidates,
    iter_paper_candidates,
    normalize_doi,
)


_TABLE_SQL = """
CREATE TABLE {table_name} (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT,
    title_eng TEXT,
    title_other TEXT,
    doi TEXT,
    url TEXT,
    abstract TEXT,
    journal_id TEXT,
    institution_id TEXT,
    issue_id TEXT,
    authors_json TEXT,
    publication_year INTEGER,
    first_page TEXT,
    final_page TEXT
)
"""


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database_path = tmp_path / "papers.sqlite"
    instance = create_engine(f"sqlite+pysqlite:///{database_path}", future=True)
    with instance.begin() as connection:
        connection.execute(text(_TABLE_SQL.format(table_name="kci_papers")))
        connection.execute(text(_TABLE_SQL.format(table_name="kci_corpus")))
    try:
        yield instance
    finally:
        instance.dispose()


def _insert(
    engine: Engine,
    table_name: str,
    *,
    database_id: int,
    source_id: str,
    doi: str | None,
    source: str = "kci",
    title: str | None = None,
    title_eng: str | None = None,
    title_other: str | None = None,
    journal_id: str | None = None,
    institution_id: str | None = None,
    issue_id: str | None = None,
    authors_json: str | None = None,
) -> None:
    statement = text(
        f"""
        INSERT INTO {table_name} (
            id, source, source_id, title, title_eng, title_other,
            doi, url, abstract, journal_id, institution_id, issue_id,
            authors_json,
            publication_year, first_page, final_page
        ) VALUES (
            :id, :source, :source_id, :title, :title_eng, :title_other,
            :doi, :url, :abstract, :journal_id, :institution_id, :issue_id,
            :authors_json,
            :publication_year, :first_page, :final_page
        )
        """
    )
    with engine.begin() as connection:
        connection.execute(
            statement,
            {
                "id": database_id,
                "source": source,
                "source_id": source_id,
                "title": title or f"Paper {source_id}",
                "title_eng": title_eng,
                "title_other": title_other,
                "doi": doi,
                "url": f"https://www.kci.go.kr/article/{source_id}",
                "abstract": f"Abstract {source_id}",
                "journal_id": journal_id,
                "institution_id": institution_id,
                "issue_id": issue_id,
                "authors_json": authors_json,
                "publication_year": 2025,
                "first_page": "1",
                "final_page": "10",
            },
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("  ", None),
        (" 10.1234/AbC.Def ", "10.1234/AbC.Def"),
        ("doi: 10.1234/AbC.Def", "10.1234/AbC.Def"),
        ("DOI:10.1234/AbC.Def", "10.1234/AbC.Def"),
        ("http://dx.doi.org/10.1234/AbC.Def", "10.1234/AbC.Def"),
        ("https://doi.org/10.1234/AbC.Def", "10.1234/AbC.Def"),
        ("https://DOI.ORG./10.1234/AbC.Def", "10.1234/AbC.Def"),
        ("10.1177%2F22338659211072939", "10.1177/22338659211072939"),
        (
            "https://doi.org/10.1234%2Fencoded%252Fsuffix",
            "10.1234/encoded%2Fsuffix",
        ),
    ],
)
def test_normalize_doi_prefix_url_and_percent_forms(
    raw: str | None,
    expected: str | None,
) -> None:
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.org/10.1234/paper",
        "https://doi.org.evil.example/10.1234/paper",
        "10.123/no-registrant",
        "10.1234",
        "10.1234/",
        "10.abcd/paper",
        "10.1234/white space",
        "10.1234/line\nbreak",
        "10.1234/encoded%0Abreak",
        "10.1234/encoded%00control",
        "10.1234/encoded%7Fcontrol",
    ],
)
def test_normalize_doi_rejects_foreign_urls_malformed_values_and_controls(
    raw: str,
) -> None:
    with pytest.raises(InvalidDoiError):
        normalize_doi(raw)


def test_default_table_filters_missing_doi_and_projects_normalized_rows(
    engine: Engine,
) -> None:
    _insert(
        engine,
        "kci_papers",
        database_id=1,
        source_id="KCI-1",
        doi="http://dx.doi.org/10.1234/First",
        title="First paper",
    )
    _insert(
        engine,
        "kci_papers",
        database_id=2,
        source_id="KCI-2",
        doi=None,
    )
    _insert(
        engine,
        "kci_papers",
        database_id=3,
        source_id="KCI-3",
        doi="   ",
    )
    _insert(
        engine,
        "kci_papers",
        database_id=4,
        source_id="KCI-4",
        doi="https://publisher.example/10.1234/not-a-resolver",
    )

    candidates = list(iter_paper_candidates(engine))

    assert count_paper_candidates(engine) == 2
    assert [candidate.database_id for candidate in candidates] == [1, 4]
    assert candidates[0].source == "kci"
    assert candidates[0].source_id == "KCI-1"
    assert candidates[0].title == "First paper"
    assert candidates[0].alternate_titles == ()
    assert candidates[0].doi_original == "http://dx.doi.org/10.1234/First"
    assert candidates[0].doi == "10.1234/First"
    assert candidates[0].landing_url == "https://www.kci.go.kr/article/KCI-1"
    assert candidates[0].abstract == "Abstract KCI-1"
    assert candidates[0].publication_year == 2025
    assert candidates[0].first_page == "1"
    assert candidates[0].final_page == "10"
    assert candidates[1].doi_original == (
        "https://publisher.example/10.1234/not-a-resolver"
    )
    assert candidates[1].doi is None

    including_missing = list(
        iter_paper_candidates(engine, include_missing_doi=True)
    )
    assert [candidate.database_id for candidate in including_missing] == [1, 2, 3, 4]
    assert count_paper_candidates(engine, include_missing_doi=True) == 4


@pytest.mark.parametrize("table_name", ["kci_papers", "kci_corpus"])
def test_projects_normalized_distinct_alternate_titles_as_an_immutable_tuple(
    engine: Engine,
    table_name: str,
) -> None:
    _insert(
        engine,
        table_name,
        database_id=1,
        source_id="KCI-TITLES",
        doi="10.1234/titles",
        title="  모바일 \n 포렌식  ",
        title_eng="  Mobile   Forensics  ",
        title_other="\tMOBILE FORENSICS\n",
    )

    candidate = next(
        iter_paper_candidates(
            engine,
            source_table=table_name,
            source_ids=["KCI-TITLES"],
        )
    )

    assert candidate.title == "모바일 포렌식"
    assert candidate.alternate_titles == ("Mobile Forensics",)
    assert isinstance(candidate.alternate_titles, tuple)


def test_projects_archive_identity_and_normalized_authors(
    engine: Engine,
) -> None:
    _insert(
        engine,
        "kci_papers",
        database_id=1,
        source_id="KCI-ARCHIVE",
        doi=None,
        journal_id="법과학의 신동향",
        institution_id="대검찰청",
        issue_id="11",
        authors_json='["  홍 길동 (기관) ", "김\\n철수", "홍 길동 (기관)", 3]',
    )

    candidate = next(
        iter_paper_candidates(engine, include_missing_doi=True)
    )

    assert candidate.journal_id == "법과학의 신동향"
    assert candidate.institution_id == "대검찰청"
    assert candidate.issue_id == "11"
    assert candidate.authors == ("홍 길동 (기관)", "김 철수")


def test_empty_primary_title_keeps_compatibility_and_retains_available_alternates(
    engine: Engine,
) -> None:
    _insert(
        engine,
        "kci_papers",
        database_id=1,
        source_id="KCI-ALTERNATE-ONLY",
        doi="10.1234/alternate-only",
        title=" ",
        title_eng="\nEnglish title\t",
        title_other="   ",
    )

    candidate = next(iter_paper_candidates(engine))

    assert candidate.title is None
    assert candidate.alternate_titles == ("English title",)


def test_iteration_uses_keyset_limit_chunks_kci_source_and_source_id_selection(
    engine: Engine,
) -> None:
    for database_id in (1, 2, 4, 7, 9, 12):
        _insert(
            engine,
            "kci_papers",
            database_id=database_id,
            source_id=f"KCI-{database_id}",
            doi=f"10.1234/{database_id}",
        )
    _insert(
        engine,
        "kci_papers",
        database_id=13,
        source_id="OTHER-13",
        doi="10.1234/other",
        source="other",
    )

    limited = list(
        iter_paper_candidates(
            engine,
            after_id=2,
            limit=3,
            chunk_size=2,
        )
    )
    assert [candidate.database_id for candidate in limited] == [4, 7, 9]

    selected = list(
        iter_paper_candidates(
            engine,
            source_ids=[" KCI-12 ", "KCI-4", "KCI-12", ""],
            chunk_size=1,
        )
    )
    assert [candidate.source_id for candidate in selected] == ["KCI-4", "KCI-12"]
    assert list(iter_paper_candidates(engine, limit=0)) == []
    assert all(candidate.source == "kci" for candidate in iter_paper_candidates(engine))


def test_corpus_requires_an_explicit_bounded_or_source_id_selection(
    engine: Engine,
) -> None:
    _insert(
        engine,
        "kci_corpus",
        database_id=1,
        source_id="CORPUS-1",
        doi="10.1234/corpus-one",
    )
    _insert(
        engine,
        "kci_corpus",
        database_id=2,
        source_id="CORPUS-2",
        doi="10.1234/corpus-two",
    )

    with pytest.raises(ValueError, match="explicit limit/source_id"):
        list(iter_paper_candidates(engine, source_table="kci_corpus"))
    with pytest.raises(ValueError, match="explicit limit/source_id"):
        list(
            iter_paper_candidates(
                engine,
                source_table="kci_corpus",
                source_ids=["", "   "],
            )
        )

    bounded = list(
        iter_paper_candidates(engine, source_table="kci_corpus", limit=1)
    )
    assert [candidate.source_id for candidate in bounded] == ["CORPUS-1"]

    selected = list(
        iter_paper_candidates(
            engine,
            source_table="kci_corpus",
            source_ids=["CORPUS-2"],
        )
    )
    assert [candidate.source_id for candidate in selected] == ["CORPUS-2"]
    assert count_paper_candidates(engine, source_table="kci_corpus") == 2


@pytest.mark.parametrize(
    "source_table",
    [
        "KCI_PAPERS",
        "kci_papers; DROP TABLE kci_papers; --",
        "kci_papers WHERE 1=1",
        "",
    ],
)
def test_rejects_unallowlisted_or_injected_table_names(
    engine: Engine,
    source_table: str,
) -> None:
    with pytest.raises(ValueError, match="unsupported source table"):
        count_paper_candidates(engine, source_table=source_table)
    with pytest.raises(ValueError, match="unsupported source table"):
        list(iter_paper_candidates(engine, source_table=source_table))


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": -1},
        {"chunk_size": 0},
    ],
)
def test_rejects_invalid_iteration_bounds(
    engine: Engine,
    arguments: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        list(iter_paper_candidates(engine, **arguments))
