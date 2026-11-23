from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from Analyzer import collection_status, deliverables
from Analyzer.deliverables import Identity


def _book(path: Path, sheets: dict[str, list[list]]) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


PAPERS = [
    Identity("ART000000001", 1, "KNOW-DOMP-0000001", "메신저 흔적 분석", "디지털포렌식연구", 2024),
    Identity("ART000000002", 3, "KNOW-DOMP-0000003", "=제목이 수식처럼 시작", "형사정책연구", 2025),  # number 2 was retired
    Identity("ART000000003", 4, "KNOW-DOMP-0000004", "원문이 없는 논문", "해외지", 2026),
]


@pytest.fixture()
def sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    analyzer = tmp_path / "Analyzer"
    _book(analyzer / "OA" / "oa_list.xlsx", {"OA 목록": [
        ["source_id", "is_open_access", "oa_classification", "collection_status"],
        ["ART000000001", True, "Open Access", "classified"],
        ["ART000000002", False, "Not Open Access", "classified"],
        ["ART000000003", None, None, "no_record"],
    ]})
    _book(analyzer / "Special-Issue" / "special_list.xlsx", {"Special Issue 목록": [
        ["source_id", "is_special_issue", "special_issue_classification", "special_number", "collection_status"],
        ["ART000000001", True, "Special Issue", "12월특집호", "classified"],
        ["ART000000002", False, "Not Special Issue", None, "classified"],
        ["ART000000003", None, "Unresolved", None, "failed"],
    ]})
    _book(analyzer / "Type" / "type_list.xlsx", {"논문 유형 목록": [
        ["source_id", "subtype", "subtype_des", "subtype_description_ko", "classification_status"],
        ["ART000000001", "ar", "Article", "일반 Article", "classified"],
        ["ART000000002", "re", "Review", "리뷰 논문", "classified"],
    ]})
    _book(analyzer / "Intelligence" / "keywords.xlsx", {"keyword_intelligence": [
        ["source_id", "doi", "title", "target_device", "target_os", "target_app", "target_app_category", "filesystem", "cloud"],
        ["ART000000001", None, "메신저 흔적 분석", "스마트폰, 드론", "Android", "KakaoTalk, Telegram", "메신저", "ext4", "SaaS"],
        ["ART000000002", None, "=제목이 수식처럼 시작", None, None, None, None, None, None],
    ]})
    _book(analyzer / "Ref" / "ref_list.xlsx", {
        "논문별 요약": [
            ["source_id", "collection_status", "reference_info_present", "reference_count"],
            ["ART000000001", "collected", True, 2],
            ["ART000000002", "no_reference_data", False, 0],
        ],
        "참고문헌 상세": [
            ["citing_source_id", "reference_order", "reference_type", "referenced_kci_source_id", "reference_title", "reference_author",
             "reference_journal_name", "reference_conference_name", "reference_publisher", "reference_site_name",
             "reference_publication_year", "reference_doi", "reference_citation_text"],
            # The second reference came from KCI as one CDATA citation string (no structured fields).
            ["ART000000001", 2, "학술지", None, None, None, None, None, None, None, None, None, "Kim, D. 2020. Two. Journal B 1: 2–3."],
            ["ART000000001", 1, "학술지", "ART000000002", "첫 번째", "이", "학술지A", None, None, None, 2019, "10.1/x", None],
        ],
    })
    # Tags and applications come from the Level-5 stage (the source of the profiles' tags and section 4).
    (analyzer / "Application").mkdir(parents=True, exist_ok=True)
    (analyzer / "Application" / "targets.json").write_text(json.dumps({"schema_version": 1, "papers": {
        "ART000000001": {"record_id": "KNOW-DOMP-0000001", "status": "ok", "basis": "PDF",
                         "tags": ["DEV-MOB", "FS-BLK", "OS-AND", "APP-MES", "CLOUD-SaaS:MES"],
                         "applications": [
                             {"name": "KakaoTalk", "display_name": "KakaoTalk", "status": "mapped",
                              "level5": [{"id": "EXAM-04-01-02-001", "title_ko": "KakaoTalk", "title_en": "KakaoTalk"}]},
                             {"name": "Telegram", "display_name": "Telegram", "status": "unregistered", "level5": []},
                         ]},
        "ART000000002": {"record_id": "KNOW-DOMP-0000003", "status": "ok", "basis": "PDF", "tags": [], "applications": []},
        "ART000000003": {"record_id": "KNOW-DOMP-0000004", "status": "pending", "basis": "", "tags": [], "applications": []},
    }}, ensure_ascii=False), encoding="utf-8")
    for name, relative in (("OA_SOURCE", "OA/oa_list.xlsx"), ("SPECIAL_SOURCE", "Special-Issue/special_list.xlsx"),
                           ("TYPE_SOURCE", "Type/type_list.xlsx"), ("KEYWORD_SOURCE", "Intelligence/keywords.xlsx"),
                           ("REFERENCE_SOURCE", "Ref/ref_list.xlsx"), ("TARGETS_SOURCE", "Application/targets.json")):
        monkeypatch.setattr(deliverables, name, analyzer / relative)
    monkeypatch.setattr(deliverables, "OUTPUTS", {
        key: (analyzer / directory.name, suffix) for key, (directory, suffix) in deliverables.OUTPUTS.items()
    })
    return analyzer


def _rows(path: Path, sheet: str = deliverables.RESULT_SHEET) -> list[tuple]:
    return list(load_workbook(path, read_only=True)[sheet].iter_rows(values_only=True))


def test_every_workbook_lists_the_whole_corpus_with_the_identity_columns(sources: Path) -> None:
    (sources / "OA").mkdir(exist_ok=True)
    stale = sources / "OA" / "(260101)-OA.xlsx"
    stale.write_bytes(b"old")
    unrelated = sources / "OA" / "oa_list.xlsx"

    results = {item.name: item for item in deliverables.export_all("260920", identities=PAPERS)}

    assert {item.path.name for item in results.values()} == {
        "(260920)-OA.xlsx", "(260920)-Special-Issue.xlsx", "(260920)-Type.xlsx", "(260920)-Tags.xlsx", "(260920)-ref_list.xlsx",
    }
    assert not stale.exists() and unrelated.exists()  # only the older dated copy of the same workbook is replaced
    for item in results.values():
        rows = _rows(item.path)
        assert rows[0][:5] == deliverables.IDENTITY_COLUMNS
        assert [row[0] for row in rows[1:]] == [1, 3, 4]  # every paper, retired numbers stay retired
        assert rows[2][2] == "'=제목이 수식처럼 시작"      # a title can never become a formula

    assert _rows(results["oa"].path)[1][5:] == ("Y", "Open Access")
    assert _rows(results["oa"].path)[3][5:] == (None, None)            # no KCI record: left empty, not guessed
    assert _rows(results["special"].path)[1][5:] == ("Y", "Special Issue", "12월특집호")
    assert _rows(results["special"].path)[2][5:] == ("N", "Not Special Issue", None)
    assert _rows(results["special"].path)[3][5:] == (None, None, None)  # unresolved is not "N"
    assert _rows(results["type"].path)[2][5:] == ("re", "Review", "리뷰 논문")
    assert _rows(results["type"].path)[3][5:] == (None, None, None)
    assert results["type"].filled_count == 2 and results["oa"].filled_count == 2


def test_tags_follow_the_tag_taxonomy_and_name_the_applications(sources: Path) -> None:
    results = {item.name: item for item in deliverables.export_all("260920", identities=PAPERS)}
    rows = _rows(results["tags"].path)

    assert rows[0][5:] == ("tags_DEV", "tags_FS", "tags_OS", "tags_APP", "tags_CLOUD", "application_names", "all_tags")
    # The tags of the Level-5 stage (v5.2 vocabulary ids); the APP tag comes with the application names.
    assert rows[1][5:] == ("DEV-MOB", "FS-BLK", "OS-AND", "APP-MES", "CLOUD-SaaS:MES", "KakaoTalk, Telegram",
                           "DEV-MOB, FS-BLK, OS-AND, APP-MES, CLOUD-SaaS:MES")
    assert rows[2][5:] == (None,) * 7          # analysed, but nothing to tag
    assert rows[3][5:] == (None,) * 7          # not final yet (pending): left empty, not "no tags"
    assert results["tags"].filled_count == 2   # papers whose tags are final
    legend = _rows(results["tags"].path, "태그 설명")
    assert ("APP-MES", "APP (애플리케이션)", "메신저") in legend


def test_reference_workbook_has_the_flag_and_the_ordered_list(sources: Path) -> None:
    results = {item.name: item for item in deliverables.export_all("260920", identities=PAPERS)}
    rows = _rows(results["ref"].path)
    assert rows[1][5:] == ("Y", 2) and rows[2][5:] == ("N", 0) and rows[3][5:] == (None, None)

    detail = _rows(results["ref"].path, deliverables.REFERENCE_SHEET)
    assert [row[2] for row in detail[1:]] == [1, 2]
    assert detail[1][3] == "이. 첫 번째. 학술지A. 2019"
    assert detail[2][3] == "Kim, D. 2020. Two. Journal B 1: 2–3."  # a CDATA citation is listed as delivered
    assert detail[1][7:] == ("ART000000002", "KNOW-DOMP-0000003")  # a cited paper of this corpus is linked by record_id


def test_missing_source_workbook_is_an_error(sources: Path) -> None:
    deliverables.TYPE_SOURCE.unlink()
    with pytest.raises(deliverables.DeliverableError, match="원본 워크북이 없습니다"):
        deliverables.export_all("260920", identities=PAPERS)


def test_collection_tracker_is_readable_and_keeps_history(sources: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    analyzer = sources
    (analyzer / "Paper" / "MD" / "KNOW-DOMP-0000001").mkdir(parents=True)
    for name in ("ko.md", "en.md"):
        (analyzer / "Paper" / "MD" / "KNOW-DOMP-0000001" / name).write_text("x", encoding="utf-8")
    (analyzer / "LV").mkdir()
    (analyzer / "LV" / "levels.json").write_text(json.dumps({"papers": {
        "ART000000001": {"basis": "PDF", "lv3": ["EXAM-01-02"]},
        "ART000000002": {"basis": "PDF", "lv3": []},
        "ART000000003": {"basis": "Abstract", "lv3": ["COLL-02-04"]},
    }}), encoding="utf-8")
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "excluded_papers.json").write_text(json.dumps({"papers": [
        {"source_id": "ART000000009", "former_record_id": "KNOW-DOMP-0000002", "title": "제외된 논문", "journal": "J", "publication_year": 2023,
         "removed_on": "2026-09-20", "reason": "디지털 포렌식 논문이 아님"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (refs / "located_pdf_sources.json").write_text(json.dumps({"excluded_foreign_publisher_sites": {
        "reason": "해외 출판사 사이트는 제외", "source_ids": ["ART000000003"]}}, ensure_ascii=False), encoding="utf-8")
    (analyzer / "Manifest").mkdir()
    (analyzer / "Manifest" / "ART000000001.download.json").write_text(json.dumps({"started_at": "2026-08-04T04:27:08Z"}), encoding="utf-8")
    monkeypatch.setattr(collection_status, "PROFILE_DIRECTORY", analyzer / "Paper" / "MD")
    monkeypatch.setattr(collection_status, "MANIFEST_DIRECTORY", analyzer / "Manifest")
    monkeypatch.setattr(collection_status, "LEVELS_JSON", analyzer / "LV" / "levels.json")
    monkeypatch.setattr(collection_status, "EXCLUDED_PAPERS", refs / "excluded_papers.json")
    monkeypatch.setattr(collection_status, "LOCATED_SOURCES", refs / "located_pdf_sources.json")
    monkeypatch.setattr(collection_status, "_pdf_files", lambda: {"ART000000001": "a.pdf", "ART000000002": "b.pdf"})
    monkeypatch.setattr(collection_status, "_numbering_state", lambda: "정렬됨")
    tracker = analyzer / "KCI-Collector.json"

    first = collection_status.build_status(identities=PAPERS, today=date(2026, 9, 20), tracker_path=tracker)
    collection_status._atomic_write(tracker, first)

    summary = first["요약"]
    assert summary["현재 수집된 KCI 포렌식 논문 수"] == 3 and summary["제거된 포렌식 논문 수"] == 1
    # The total comes first and is the sum of the two, so "517 and 38 removed" cannot be misread.
    assert list(summary)[:3] == ["총 논문수(지금까지 선별된 KCI 포렌식 논문 = 현재 + 제거)", "현재 수집된 KCI 포렌식 논문 수", "제거된 포렌식 논문 수"]
    assert summary["총 논문수(지금까지 선별된 KCI 포렌식 논문 = 현재 + 제거)"] == 4 and first["날짜별 수집 이력"][0]["총 논문수"] == 4
    assert summary["원문 PDF 수집"] == "2 / 3편 (66.7%)" and summary["MD 파일 생성"] == "1 / 3편 (33.3%)"
    assert summary["Level이 1개 이상 확정된 논문"] == "2 / 3편 (66.7%)"
    one, two, three = first["논문별 현황"]
    assert one["수집일"] == "2026-08-04" and two["수집일"] == "2026-09-20"
    assert one["MD 파일"] == "생성 완료 (한국어·영어)" and one["애플리케이션 명"] == ["KakaoTalk", "Telegram"]
    assert one["Special Issue"] == "확인 완료 — Special Issue (12월특집호)" and one["참고문헌(ref)"] == "수집 완료 — 2건"
    assert two["Level 분류"].startswith("분류 완료 — confidence 0.80 이상으로 확정된 Level이 없어")
    assert two["참고문헌(ref)"] == "수집 완료 — KCI에 참고문헌 정보가 없는 논문"
    assert three["원문 PDF"] == "미수집 — 해외 출판사 사이트는 제외"
    assert "초록으로 분류" in three["Level 분류"] and three["MD 파일"].startswith("미생성 — 원문 PDF가 없어")
    assert first["아직 끝나지 않은 일"]["원문 PDF가 없는 논문"] == ["4번 원문이 없는 논문"]
    assert first["제거된 논문"][0]["제거 사유"] == "디지털 포렌식 논문이 아님" and first["번호 정렬 상태"] == "정렬됨"
    # A removed paper has no live id: only the number it had, labelled as such.
    assert first["제거된 논문"][0]["제거 당시 번호(지금은 다른 논문의 번호일 수 있음)"] == "KNOW-DOMP-0000002" and "record_id" not in first["제거된 논문"][0]

    # The next day: first-seen dates are kept and the history gains one line per day (same day = replaced).
    second = collection_status.build_status(identities=PAPERS, today=date(2026, 9, 21), tracker_path=tracker)
    assert [item["날짜"] for item in second["날짜별 수집 이력"]] == ["2026-09-20", "2026-09-21"]
    assert second["논문별 현황"][1]["수집일"] == "2026-09-20"
    again = collection_status.build_status(identities=PAPERS, today=date(2026, 9, 20), tracker_path=tracker)
    assert [item["날짜"] for item in again["날짜별 수집 이력"]] == ["2026-09-20"]
