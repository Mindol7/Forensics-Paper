from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from openpyxl.styles import PatternFill

from Collector import paper_removal


def test_only_rows_with_a_yellow_title_are_marked_for_removal(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["paper_index", "record_id", "title"])
    sheet.append([1, "KNOW-DOMP-0000001", "남길 논문"])
    sheet.append([2, "KNOW-DOMP-0000002", "뺄 논문"])
    sheet.append([3, "KNOW-DOMP-0000003", "다른 색으로 표시한 논문"])
    sheet["C3"].fill = PatternFill("solid", fgColor="FFFF00")
    sheet["C4"].fill = PatternFill("solid", fgColor="92D050")
    sheet["A2"].fill = PatternFill("solid", fgColor="FFFF00")  # a marker outside the title cell does not count
    other = workbook.create_sheet("메모")
    other.append(["아무 표"])
    path = tmp_path / "review.xlsx"
    workbook.save(path)

    assert paper_removal.highlighted_record_ids(path) == ["KNOW-DOMP-0000002"]


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    analyzer = tmp_path / "Analyzer"
    manifest = analyzer / "Manifest"
    for directory in (analyzer / "Paper" / "MD" / "KNOW-DOMP-0000002", manifest / "Lv3", analyzer / "Structure" / "kor"):
        directory.mkdir(parents=True)
    (analyzer / "Paper" / "뺄 논문.pdf").write_bytes(b"%PDF-1.4 removed")
    (analyzer / "Paper" / "남길 논문.pdf").write_bytes(b"%PDF-1.4 kept")
    (analyzer / "Paper" / "MD" / "KNOW-DOMP-0000002" / "ko.md").write_text(
        "| URL | https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId=ART000000002 |\n", encoding="utf-8")
    (manifest / "Lv3" / "ART000000002.json").write_text("{}", encoding="utf-8")
    (analyzer / "Structure" / "kor" / "ART000000002.md").write_text("structure", encoding="utf-8")
    (manifest / "paper_files.json").write_text(json.dumps({"papers": [
        {"source_id": "ART000000001", "title": "남길 논문", "filename": "남길 논문.pdf"},
        {"source_id": "ART000000002", "title": "뺄 논문", "filename": "뺄 논문.pdf"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (manifest / "paper_profiles.json").write_text(json.dumps({"schema_version": 1, "records": {
        "ART000000001": "KNOW-DOMP-0000001", "ART000000002": "KNOW-DOMP-0000002"}}), encoding="utf-8")
    (manifest / "keyword_intelligence_legacy.json").write_text(json.dumps({"row_count": 2, "rows": [
        {"source_id": "ART000000001"}, {"source_id": "ART000000002"}]}), encoding="utf-8")
    database = tmp_path / "kci.db"
    connection = sqlite3.connect(database)
    connection.execute("create table kci_papers (source_id text, title text, title_kor text, journal_id text, publication_year integer)")
    connection.executemany("insert into kci_papers values (?, ?, ?, ?, ?)", [
        ("ART000000001", "남길 논문", "남길 논문", "J", 2024), ("ART000000002", "뺄 논문", "뺄 논문", "J", 2025)])
    connection.commit()
    connection.close()

    released: list[set[str]] = []
    monkeypatch.setattr(paper_removal, "ANALYZER_DIR", analyzer)
    monkeypatch.setattr(paper_removal, "MANIFEST_DIR", manifest)
    monkeypatch.setattr(paper_removal, "REMOVED_DIR", analyzer / "Removed")
    monkeypatch.setattr(paper_removal, "EXCLUSION_FILE", tmp_path / "excluded_papers.json")
    monkeypatch.setattr(paper_removal, "load_settings", lambda validate=False: SimpleNamespace(database_url=f"sqlite:///{database}"))
    monkeypatch.setattr(paper_removal, "remove_paper_file_index_entries", lambda path, ids, papers_dir: released.append(set(ids)))
    import Analyzer.evidence_writer as evidence_writer

    monkeypatch.setattr(evidence_writer, "regenerate_structure_index", lambda directory: None)
    tmp_path.joinpath("released").write_text("", encoding="utf-8")
    monkeypatch.setattr(paper_removal, "_released_for_test", released, raising=False)
    return tmp_path


def _selected(repository: Path) -> list[str]:
    connection = sqlite3.connect(repository / "kci.db")
    try:
        return [row[0] for row in connection.execute("select source_id from kci_papers order by source_id")]
    finally:
        connection.close()


def test_dry_run_reports_without_touching_anything(repository: Path) -> None:
    report = paper_removal.remove_papers(["ART000000002", "ART000000404"], reason="검토 후 제외", reviewed_in=None, dry_run=True)

    assert [entry["source_id"] for entry in report["removed"]] == ["ART000000002"]
    assert report["not_selected"] == ["ART000000404"]
    assert _selected(repository) == ["ART000000001", "ART000000002"]
    assert not (repository / "excluded_papers.json").exists() and not (repository / "Analyzer" / "Removed").exists()


def test_removal_archives_the_artifacts_and_records_the_exclusion(repository: Path) -> None:
    analyzer = repository / "Analyzer"
    report = paper_removal.remove_papers(["ART000000002"], reason="검토 후 제외", reviewed_in="review.xlsx")

    assert [entry["former_record_id"] for entry in report["removed"]] == ["KNOW-DOMP-0000002"]
    assert _selected(repository) == ["ART000000001"]
    archive = analyzer / "Removed" / "ART000000002"
    # Nothing is destroyed: the PDF, the evidence and the profile move into the archive.
    assert sorted(path.name for path in archive.iterdir()) == ["kci_paper_row.json", "lv3.json", "paper.pdf", "profile_md", "structure.kor.md"]
    assert not (analyzer / "Paper" / "뺄 논문.pdf").exists() and (analyzer / "Paper" / "남길 논문.pdf").exists()
    assert not (analyzer / "Paper" / "MD" / "KNOW-DOMP-0000002").exists()
    assert paper_removal._released_for_test == [{"ART000000002"}]

    excluded = paper_removal.load_excluded_papers()
    assert list(excluded) == ["ART000000002"] and excluded["ART000000002"]["reason"] == "검토 후 제외"
    assert excluded["ART000000002"]["reviewed_in"] == "review.xlsx"
    assert paper_removal.load_excluded_source_ids() == frozenset({"ART000000002"})
    snapshot = json.loads((analyzer / "Manifest" / "keyword_intelligence_legacy.json").read_text(encoding="utf-8"))
    assert snapshot["row_count"] == 1 and [row["source_id"] for row in snapshot["rows"]] == ["ART000000001"]

    # Asking again is harmless and says why nothing happened.
    again = paper_removal.remove_papers(["ART000000002"], reason="검토 후 제외", reviewed_in=None)
    assert again == {"removed": [], "already_excluded": ["ART000000002"], "not_selected": []}


def test_an_interrupted_removal_is_finished_by_running_it_again(repository: Path) -> None:
    """The decision is recorded before anything is deleted, so a crash can never lose it."""

    analyzer = repository / "Analyzer"
    calls = {"count": 0}
    original = paper_removal._archive_artifacts

    def crash_once(*args: object, **kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("disk full while moving the PDF")
        original(*args, **kwargs)

    paper_removal._archive_artifacts = crash_once
    try:
        with pytest.raises(OSError):
            paper_removal.remove_papers(["ART000000002"], reason="검토 후 제외", reviewed_in=None)
        # Row gone, decision recorded (with its permanent number), artifacts still in place.
        assert _selected(repository) == ["ART000000001"]
        assert paper_removal.load_excluded_source_ids() == frozenset({"ART000000002"})
        assert (analyzer / "Paper" / "뺄 논문.pdf").exists()

        report = paper_removal.remove_papers(["ART000000002"], reason="검토 후 제외", reviewed_in=None)
    finally:
        paper_removal._archive_artifacts = original

    assert report["already_excluded"] == ["ART000000002"] and report["removed"] == []
    assert not (analyzer / "Paper" / "뺄 논문.pdf").exists()
    assert (analyzer / "Removed" / "ART000000002" / "paper.pdf").is_file()
    assert (analyzer / "Removed" / "ART000000002" / "profile_md" / "ko.md").is_file()
    assert paper_removal._released_for_test[-1] == {"ART000000002"}


def test_the_exclusion_entry_keeps_the_papers_permanent_number(repository: Path) -> None:
    lv3 = repository / "Analyzer" / "Manifest" / "Lv3" / "ART000000002.json"
    lv3.write_text(json.dumps({"paper_index": 42}), encoding="utf-8")
    paper_removal.remove_papers(["ART000000002"], reason="검토 후 제외", reviewed_in=None)
    entry = paper_removal.load_excluded_papers()["ART000000002"]
    # History only: the number is released (renumbering hands it to the next paper), so no live-id keys remain.
    assert entry["former_paper_index"] == 42 and "paper_index" not in entry and "record_id" not in entry
    profiles = json.loads((repository / "Analyzer" / "Manifest" / "paper_profiles.json").read_text(encoding="utf-8"))["records"]
    assert profiles == {"ART000000001": "KNOW-DOMP-0000001"}


def test_a_workbook_row_must_name_the_paper_by_number_and_title(repository: Path, tmp_path: Path) -> None:
    """After a renumbering an old workbook carries other papers' numbers; the title check refuses it."""

    def workbook(rows: list[tuple[str, str]]) -> Path:
        book = Workbook()
        sheet = book.active
        sheet.append(["paper_index", "record_id", "title"])
        for record, title in rows:
            sheet.append([1, record, title])
            sheet.cell(row=sheet.max_row, column=3).fill = PatternFill("solid", fgColor="FFFF00")
        path = tmp_path / f"review-{len(rows)}-{abs(hash(tuple(rows)))}.xlsx"
        book.save(path)
        return path

    profiles = {"ART000000001": "KNOW-DOMP-0000001", "ART000000002": "KNOW-DOMP-0000002"}
    database = repository / "kci.db"
    connection = sqlite3.connect(database)
    connection.execute("alter table kci_papers add column title_eng text")
    connection.commit()
    connection.close()

    current = workbook([("KNOW-DOMP-0000002", " 뺄  논문 ")])  # spacing differences are not a different paper
    assert paper_removal.resolve_highlighted(current, database, profiles) == ["ART000000002"]
    outdated = workbook([("KNOW-DOMP-0000001", "뺄 논문")])    # id 1 belongs to «남길 논문» today
    with pytest.raises(SystemExit, match="번호와 제목이 현재 논문 목록과 맞지 않습니다"):
        paper_removal.resolve_highlighted(outdated, database, profiles)
    assert _selected(repository) == ["ART000000001", "ART000000002"]


def test_a_profile_folder_is_only_archived_when_it_is_that_papers_profile(repository: Path) -> None:
    analyzer = repository / "Analyzer"
    # Pretend a renumbering gave KNOW-DOMP-0000002 to another paper: the folder no longer names ART000000002.
    (analyzer / "Paper" / "MD" / "KNOW-DOMP-0000002" / "ko.md").write_text("| URL | …artiId=ART000000777 |\n", encoding="utf-8")
    paper_removal.remove_papers(["ART000000002"], reason="검토 후 제외", reviewed_in=None)
    assert (analyzer / "Paper" / "MD" / "KNOW-DOMP-0000002" / "ko.md").is_file()
    assert not (analyzer / "Removed" / "ART000000002" / "profile_md").exists()
