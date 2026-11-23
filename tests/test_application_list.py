from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from Analyzer.Application.application_list import (
    ApplicationListError,
    PaperApplications,
    export_application_list,
    export_level5_workbook,
    load_paper_applications,
    merge_workbooks,
    read_application_rows,
    read_applications,
    read_workbook_papers,
    unique_application_names,
)


def _profile(directory: Path, name: str, rows: str, *, heading: str, header: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        f"---\nidentifier: \"{directory.name}\"\n---\n\n# 제목\n\n## 3. 키워드\nx\n\n{heading}\n{header}\n|---|---|\n{rows}\n\n## 5. 참고문헌\n| ID | REF |\n|---|---|\n|  | N/A |\n",
        encoding="utf-8",
    )


def _paper(md_dir: Path, record_id: str, ko_rows: str, en_rows: str | None = None) -> None:
    directory = md_dir / record_id
    _profile(directory, "ko.md", ko_rows, heading="## 4. 대상 애플리케이션", header="| ID | 이름 |")
    _profile(directory, "en.md", en_rows if en_rows is not None else ko_rows, heading="## 4. Target Applications", header="| ID | Name |")


def test_workbook_lists_every_paper_with_its_applications_or_na(tmp_path: Path) -> None:
    md_dir = tmp_path / "MD"
    _paper(md_dir, "KNOW-DOMP-0000002", "|  | Dropbox |\n| COLL-02-04-06-016 | OneDrive |\n| EXAM-04-01-07-016 | OneDrive |")
    _paper(md_dir, "KNOW-DOMP-0000001", "|  | File Commander Manager &amp; Vault |\n|  | Tool &#124; Notes |")
    _paper(md_dir, "KNOW-DOMP-0000006", "|  | N/A |")
    (md_dir / "notes.txt").write_text("ignored", encoding="utf-8")

    papers = load_paper_applications(md_dir)
    result = export_application_list(papers, tmp_path / "out" / "application_list.xlsx")

    assert [paper.record_id for paper in papers] == ["KNOW-DOMP-0000001", "KNOW-DOMP-0000002", "KNOW-DOMP-0000006"]
    assert (result.paper_count, result.with_applications, result.application_count) == (3, 2, 4)
    workbook = load_workbook(result.path, read_only=True)
    assert workbook.sheetnames == ["대상 애플리케이션", "애플리케이션 목록"]
    rows = list(workbook["대상 애플리케이션"].iter_rows(values_only=True))
    assert rows == [
        ("ID", "Target Application"),
        ("KNOW-DOMP-0000001", "File Commander Manager & Vault, Tool | Notes"),
        ("KNOW-DOMP-0000002", "Dropbox, OneDrive"),
        ("KNOW-DOMP-0000006", "N/A"),
    ]
    listing = list(workbook["애플리케이션 목록"].iter_rows(values_only=True))
    assert listing == [("List",), ("Dropbox",), ("File Commander Manager & Vault",), ("OneDrive",), ("Tool | Notes",)]
    assert result.unique_application_count == 4


def test_unique_names_are_deduplicated_and_sorted_alphabet_then_hangul() -> None:
    papers = [
        PaperApplications("KNOW-DOMP-0000001", ("카카오톡", "Telegram", "Dropbox")),
        PaperApplications("KNOW-DOMP-0000002", ("Dropbox", "1Gallery", "네이버 지도", "apple Music", "Zoom")),
        PaperApplications("KNOW-DOMP-0000003", ()),
        PaperApplications("KNOW-DOMP-0000004", ("네이버", "FaceSwap", "Faceswap")),
    ]
    assert unique_application_names(papers) == [
        "1Gallery",  # digits/symbols come first
        "apple Music", "Dropbox", "FaceSwap", "Faceswap", "Telegram", "Zoom",  # A-Z, case-insensitive; exact spellings stay distinct
        "네이버", "네이버 지도", "카카오톡",  # then 가나다
    ]


def test_profile_pairs_must_agree_and_tables_must_be_well_formed(tmp_path: Path) -> None:
    md_dir = tmp_path / "MD"
    _paper(md_dir, "KNOW-DOMP-0000001", "|  | Dropbox |", "|  | OneDrive |")
    with pytest.raises(ApplicationListError, match="different applications"):
        load_paper_applications(md_dir)

    mixed = tmp_path / "mixed"
    _paper(mixed, "KNOW-DOMP-0000001", "|  | Dropbox |\n|  | N/A |")
    with pytest.raises(ApplicationListError, match="mixes N/A"):
        read_applications(mixed / "KNOW-DOMP-0000001" / "ko.md")

    duplicated = tmp_path / "dup"
    _paper(duplicated, "KNOW-DOMP-0000001", "|  | Dropbox |\n|  | Dropbox |")
    with pytest.raises(ApplicationListError, match="duplicate"):
        read_applications(duplicated / "KNOW-DOMP-0000001" / "ko.md")

    with pytest.raises(ApplicationListError, match="missing"):
        load_paper_applications(tmp_path / "nowhere")


def test_merge_combines_workbooks_of_different_paper_families(tmp_path: Path) -> None:
    kci = export_application_list(
        [PaperApplications("KNOW-DOMP-0000001", ("Dropbox", "카카오톡")), PaperApplications("KNOW-DOMP-0000002", ())],
        tmp_path / "kci.xlsx",
    )
    scopus = export_application_list(
        [PaperApplications("KNOW-INTP-0000001", ()), PaperApplications("KNOW-INTP-0000002", ("WebGoat", "Dropbox"))],
        tmp_path / "scopus.xlsx",
    )
    # Round trip: a written workbook reads back to the same rows (N/A -> no applications).
    assert [(p.record_id, p.applications) for p in read_workbook_papers(kci.path)] == [
        ("KNOW-DOMP-0000001", ("Dropbox", "카카오톡")), ("KNOW-DOMP-0000002", ()),
    ]

    merged = merge_workbooks([scopus.path, kci.path], tmp_path / "all.xlsx")

    assert (merged.paper_count, merged.with_applications, merged.application_count, merged.unique_application_count) == (4, 2, 4, 3)
    workbook = load_workbook(merged.path, read_only=True)
    assert workbook.sheetnames == ["대상 애플리케이션", "애플리케이션 목록"]
    assert list(workbook["대상 애플리케이션"].iter_rows(values_only=True)) == [
        ("ID", "Target Application"),
        ("KNOW-DOMP-0000001", "Dropbox, 카카오톡"),
        ("KNOW-DOMP-0000002", "N/A"),
        ("KNOW-INTP-0000001", "N/A"),
        ("KNOW-INTP-0000002", "WebGoat, Dropbox"),
    ]
    assert [row[0] for row in workbook["애플리케이션 목록"].iter_rows(values_only=True)] == ["List", "Dropbox", "WebGoat", "카카오톡"]

    with pytest.raises(ApplicationListError, match="more than one workbook"):
        merge_workbooks([kci.path, kci.path], tmp_path / "dup.xlsx")
    with pytest.raises(ApplicationListError, match="at least two"):
        merge_workbooks([kci.path], tmp_path / "one.xlsx")


def test_a_profile_lists_one_row_per_level5_id(tmp_path: Path) -> None:
    md_dir = tmp_path / "MD"
    _paper(md_dir, "KNOW-DOMP-0000001", "| COLL-02-04-01-002 | Discord |\n| EXAM-04-01-02-003 | Discord |\n|  | KakaoMap |")
    path = md_dir / "KNOW-DOMP-0000001" / "ko.md"
    assert read_application_rows(path) == (("COLL-02-04-01-002", "Discord"), ("EXAM-04-01-02-003", "Discord"), ("", "KakaoMap"))
    assert read_applications(path) == ("Discord", "KakaoMap")  # the same application on several rows counts once

    # DF-Insight's ids end in two digits since 2026-10 (three before); both forms read, a one-digit end does not.
    _paper(md_dir, "KNOW-DOMP-0000005", "| COLL-02-04-01-02 | Discord |\n| EXAM-04-01-02-03 | Discord |")
    assert read_application_rows(md_dir / "KNOW-DOMP-0000005" / "ko.md") == (("COLL-02-04-01-02", "Discord"), ("EXAM-04-01-02-03", "Discord"))
    _paper(md_dir, "KNOW-DOMP-0000002", "| EXAM-04-01-02-3 | Discord |")
    with pytest.raises(ApplicationListError, match="not a Level-5 id"):
        read_application_rows(md_dir / "KNOW-DOMP-0000002" / "ko.md")
    _paper(md_dir, "KNOW-DOMP-0000003", "| EXAM-04-01-02-003 | Discord |\n| EXAM-04-01-02-003 | Discord |")
    with pytest.raises(ApplicationListError, match="appears twice"):
        read_application_rows(md_dir / "KNOW-DOMP-0000003" / "ko.md")
    # ko.md and en.md may name a registered node in their own language, but the ids must be the same rows.
    _paper(md_dir / "x", "KNOW-DOMP-0000004", "| EXAM-04-01-11-003 | 한컴오피스 HWP |", "| EXAM-04-01-11-003 | Hancom Office HWP |")
    assert [paper.applications for paper in load_paper_applications(md_dir / "x")] == [("Hancom Office HWP",)]


def _by_basis(pdf, abstract, summary) -> dict:
    def outcome(value):
        return {"status": "mapped", "ids": value} if isinstance(value, list) else {"status": value, "ids": []}

    return {"PDF": outcome(pdf), "Abstract": outcome(abstract), "Summary": outcome(summary)}


def _level5_papers() -> list[dict]:
    discord = ["COLL-02-04-01-002", "EXAM-04-01-02-003"]
    return [
        {"source_id": "ART000000002", "record_id": "KNOW-DOMP-0000002", "status": "ok", "basis": "PDF", "applications": []},
        {"source_id": "ART000000001", "record_id": "KNOW-DOMP-0000001", "status": "ok", "basis": "PDF", "applications": [
            {"name": "Discord", "display_name": "Discord", "status": "mapped", "level5": [{"id": item} for item in discord],
             "level5_by_basis": _by_basis(discord, ["EXAM-04-01-02-003"], discord)},
            {"name": "KakaoTalk", "display_name": "KakaoTalk", "status": "mapped", "level5": [{"id": "EXAM-04-01-02-001"}],
             "level5_by_basis": _by_basis(["EXAM-04-01-02-001"], ["EXAM-04-01-02-001"], ["EXAM-04-01-02-001"])},
            {"name": "Netflix", "display_name": "Netflix", "status": "no_fit", "level5": [],
             "level5_by_basis": _by_basis("no_fit", "no_fit", ["EXAM-04-01-14-001"])},
            {"name": "KakaoMap", "display_name": "KakaoMap", "status": "unregistered", "level5": [],
             "level5_by_basis": _by_basis("unregistered", "unregistered", "unregistered")},
        ]},
        {"source_id": "ART000000003", "record_id": "KNOW-DOMP-0000003", "status": "ok", "basis": "Abstract", "applications": [
            {"name": "Discord", "display_name": "Discord", "status": "mapped", "level5": [{"id": "EXAM-04-01-02-003"}],
             "level5_by_basis": _by_basis("unavailable", ["EXAM-04-01-02-003"], "unavailable")},
        ]},
        {"source_id": "ART000000004", "record_id": "KNOW-DOMP-0000004", "status": "pending", "basis": "", "applications": []},
    ]


def test_level5_workbook_has_one_sheet_per_input_and_a_combined_sheet(tmp_path: Path) -> None:
    result = export_level5_workbook(_level5_papers(), tmp_path / "application_list.xlsx", registry={"level5_nodes": 2267})
    assert (result.paper_count, result.with_applications, result.application_count, result.unique_application_count) == (4, 2, 5, 4)
    assert (result.mapped_applications, result.unregistered_applications, result.no_fit_applications) == (3, 1, 1)
    workbook = load_workbook(result.path)
    assert workbook.sheetnames == ["개요", "PDF-Based", "Abstract-Based", "Summary-Based", "종합본"]
    expected_ids = ["KNOW-DOMP-0000001"] * 4 + ["KNOW-DOMP-0000002", "KNOW-DOMP-0000003", "KNOW-DOMP-0000004"]
    names = ["Discord", "KakaoTalk", "Netflix", "KakaoMap", "N/A", "Discord", None]
    columns = {
        "PDF-Based": ["COLL-02-04-01-002, EXAM-04-01-02-003", "EXAM-04-01-02-001", "매핑 안 됨", "미등록", None, "원문 PDF 없음", "대기"],
        "Abstract-Based": ["EXAM-04-01-02-003", "EXAM-04-01-02-001", "매핑 안 됨", "미등록", None, "EXAM-04-01-02-003", "대기"],
        "Summary-Based": ["COLL-02-04-01-002, EXAM-04-01-02-003", "EXAM-04-01-02-001", "EXAM-04-01-14-001", "미등록", None, "원문 PDF 없음", "대기"],
    }
    for sheet_name, values in columns.items():
        rows = list(workbook[sheet_name].iter_rows(values_only=True))
        assert rows[0] == ("ID", "Target Application", "Level-5")
        assert rows[1:] == list(zip(expected_ids, names, values)), sheet_name     # the same rows, in the same order, on every sheet
    combined = workbook["종합본"]
    rows = list(combined.iter_rows(values_only=True))
    assert rows[0] == ("ID", "Target Application", "PDF Level-5", "Abstract Level-5", "Summary Level-5")
    assert rows[1:] == list(zip(expected_ids, names, *columns.values()))
    # Row colours: the inputs agree (green), differ (yellow), or there is nothing to compare (grey).
    colour = {row: combined.cell(row=row, column=3).fill.fgColor.rgb[-6:] for row in range(2, 9)}
    assert colour == {2: "FFF2CC", 3: "E2EFDA", 4: "FFF2CC", 5: "F2F2F2", 6: "000000", 7: "F2F2F2", 8: "000000"}
    assert combined.freeze_panes == "C2" and combined.auto_filter.ref == "A1:E8"

    overview = {row[0]: row[1:] for row in workbook["개요"].iter_rows(values_only=True) if row[0]}
    assert overview["PDF-Based"][:8] == (2, 3, 1, 0, 1, 1, 0, 1)            # mapped, ids, 매핑 안 됨, 다른 제품, 미등록, 입력 없음, 대기, papers
    assert overview["Summary-Based"][:2] == (3, 4)
    # PDF vs Abstract: Discord shares one id, KakaoTalk and Netflix are the same (KakaoMap is unregistered,
    # paper 3 has no PDF).  Summary differs from both on Netflix.
    assert overview["PDF ↔ Abstract"][:5] == (3, 2, 1, 0, 2 / 3)
    assert overview["PDF ↔ Summary"][:5] == (3, 2, 0, 1, 2 / 3)
    assert overview["세 입력 모두"][:5] == (3, 1, 1, 1, 1 / 3)
    assert "2,267" in str(workbook["개요"]["A2"].value)
    workbook.close()

    # The combined sheet reads back one paper per id with its profile result (merge compatibility).
    papers = read_workbook_papers(result.path)
    assert [(paper.record_id, paper.applications, paper.basis, paper.pending) for paper in papers] == [
        ("KNOW-DOMP-0000001", ("Discord", "KakaoTalk", "Netflix", "KakaoMap"), "PDF-Based", False),
        ("KNOW-DOMP-0000002", (), "PDF-Based", False),
        ("KNOW-DOMP-0000003", ("Discord",), "Abstract-Based", False),
        ("KNOW-DOMP-0000004", (), "", True),
    ]
    assert papers[0].level5 == "Discord: COLL-02-04-01-002, EXAM-04-01-02-003\nKakaoTalk: EXAM-04-01-02-001\nNetflix: 매핑 안 됨\nKakaoMap: 미등록"
    assert papers[2].level5 == "Discord: EXAM-04-01-02-003"
    scopus = export_application_list([PaperApplications("KNOW-INTP-0000001", ("WebGoat",))], tmp_path / "scopus.xlsx")
    merged = merge_workbooks([result.path, scopus.path], tmp_path / "all.xlsx")
    merged_rows = list(load_workbook(merged.path, read_only=True)["대상 애플리케이션"].iter_rows(values_only=True))
    assert merged_rows[0] == ("ID", "Target Application", "Level-5", "*-Based")
    assert merged_rows[3] == ("KNOW-DOMP-0000003", "Discord", "Discord: EXAM-04-01-02-003", "Abstract-Based")
    assert merged_rows[4] == ("KNOW-DOMP-0000004", None, None, None) and merged_rows[5] == ("KNOW-INTP-0000001", "WebGoat", None, None)


def test_a_paper_without_a_pdf_is_read_back_as_abstract_based_and_names_stay_as_written(tmp_path: Path) -> None:
    papers = [
        {"source_id": "ART000000512", "record_id": "KNOW-DOMP-0000512", "status": "ok", "basis": "Abstract", "applications": []},
        {"source_id": "ART000000513", "record_id": "KNOW-DOMP-0000513", "status": "ok", "basis": "Abstract", "applications": [
            {"name": "QQ", "display_name": "QQ", "status": "unregistered", "level5": [],
             "level5_by_basis": _by_basis("unavailable", "unregistered", "unavailable")},
            {"name": "=B", "display_name": "=B", "status": "mapped", "level5": [{"id": "EXAM-04-01-02-003"}],
             "level5_by_basis": _by_basis("unavailable", ["EXAM-04-01-02-003"], "unavailable")}]},
        # targets.json written before the per-input results: no guessing 대기 where the answer is known.
        {"source_id": "ART000000514", "record_id": "KNOW-DOMP-0000514", "status": "ok", "basis": "Abstract", "applications": [
            {"name": "KakaoMap", "display_name": "KakaoMap", "status": "unregistered", "level5": []}]},
        {"source_id": "ART000000001", "record_id": "KNOW-DOMP-0000001", "status": "ok", "basis": "PDF", "applications": [
            {"name": "KakaoMap", "display_name": "KakaoMap", "status": "unregistered", "level5": []}]},
    ]
    result = export_level5_workbook(papers, tmp_path / "application_list.xlsx")
    rows = list(load_workbook(result.path, read_only=True)["종합본"].iter_rows(values_only=True))[1:]
    assert rows == [
        ("KNOW-DOMP-0000001", "KakaoMap", "미등록", "미등록", "미등록"),
        ("KNOW-DOMP-0000512", "N/A", "원문 PDF 없음", None, "원문 PDF 없음"),
        ("KNOW-DOMP-0000513", "QQ", "원문 PDF 없음", "미등록", "원문 PDF 없음"),
        ("KNOW-DOMP-0000513", "=B", "원문 PDF 없음", "EXAM-04-01-02-003", "원문 PDF 없음"),
        ("KNOW-DOMP-0000514", "KakaoMap", "원문 PDF 없음", "미등록", "원문 PDF 없음"),
    ]
    papers_back = {paper.record_id: paper for paper in read_workbook_papers(result.path)}
    assert {record_id: paper.basis for record_id, paper in papers_back.items()} == {
        "KNOW-DOMP-0000001": "PDF-Based", "KNOW-DOMP-0000512": "Abstract-Based", "KNOW-DOMP-0000513": "Abstract-Based",
        "KNOW-DOMP-0000514": "Abstract-Based"}
    assert papers_back["KNOW-DOMP-0000513"].applications == ("QQ", "=B")
    assert papers_back["KNOW-DOMP-0000513"].level5 == "QQ: 미등록\n=B: EXAM-04-01-02-003"


def test_the_overview_counts_pending_papers_apart_and_fits_its_labels(tmp_path: Path) -> None:
    papers = _level5_papers()
    result = export_level5_workbook(papers, tmp_path / "application_list.xlsx")
    assert (result.with_applications, result.pending_papers) == (2, 1)
    sheet = load_workbook(result.path)["개요"]
    row = next(cell.row for cell in sheet["A"] if cell.value == "원문 PDF 없음 · 초록 없음 · 요약 없음")
    assert sheet.row_dimensions[row].height >= 34                      # the label in column A takes two lines

