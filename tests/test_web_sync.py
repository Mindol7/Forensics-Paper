from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from Analyzer import web_sync
from Analyzer.web_sync import (
    EMPTY_WEB_TABLE,
    IDS_CHANGED,
    NAMES_CHANGED,
    REPLACED_TABLE_IS_BACK,
    SEVERAL_WEB_COPIES,
    UNIDENTIFIED_WEB_COPY,
    UNREADABLE_WEB_TABLE,
    WEB_COPY_GONE,
    SyncReport,
    WebSyncError,
    application_names,
    application_rows,
    export_profile_text,
    load_state,
    paper_key,
    restore_unchanged_after_build,
    sync_profiles,
)


def _profile(root: Path, record_id: str, text: str) -> None:
    for name in ("ko.md", "en.md"):
        (root / record_id).mkdir(parents=True, exist_ok=True)
        (root / record_id / name).write_text(f"{text} {name}\n", encoding="utf-8")


@pytest.fixture()
def web(tmp_path: Path) -> Path:
    web = tmp_path / "DF-Insight"
    (web / "scripts").mkdir(parents=True)
    (web / "scripts" / "build-graphs.mjs").write_text("// upstream build", encoding="utf-8")
    references = web / "data" / "references_md"
    _profile(references, "KNOW-DOMP-0000001", "old profile")      # will be updated
    _profile(references, "KNOW-DOMP-0000002", "same profile")     # unchanged
    _profile(references, "KNOW-DOMP-0000009", "removed paper")    # no longer in the corpus
    _profile(references, "KNOW-INTP-0000001", "international")    # another family: never touched
    return web


def test_only_the_domestic_paper_family_is_mirrored(tmp_path: Path, web: Path) -> None:
    profiles = tmp_path / "MD"
    _profile(profiles, "KNOW-DOMP-0000001", "new profile")
    _profile(profiles, "KNOW-DOMP-0000002", "same profile")
    _profile(profiles, "KNOW-DOMP-0000003", "brand new")
    (profiles / "notes.txt").write_text("not a profile", encoding="utf-8")

    preview = sync_profiles(profiles=profiles, web=web, dry_run=True)
    assert (preview.added, preview.updated, preview.removed, preview.unchanged) == (["KNOW-DOMP-0000003"], ["KNOW-DOMP-0000001"], ["KNOW-DOMP-0000009"], 1)
    references = web / "data" / "references_md"
    assert (references / "KNOW-DOMP-0000009").is_dir() and not (references / "KNOW-DOMP-0000003").exists()  # dry run changes nothing

    report = sync_profiles(profiles=profiles, web=web)
    assert report.as_dict()["added"] == 1 and report.changed == 3
    assert (references / "KNOW-DOMP-0000001" / "ko.md").read_text(encoding="utf-8") == "new profile ko.md\n"
    assert (references / "KNOW-DOMP-0000003" / "en.md").read_text(encoding="utf-8") == "brand new en.md\n"
    assert not (references / "KNOW-DOMP-0000009").exists()
    assert (references / "KNOW-INTP-0000001" / "ko.md").read_text(encoding="utf-8") == "international ko.md\n"
    assert not list(references.glob("KNOW-DOMP-*/.*.tmp"))
    # A second run finds nothing to do.
    again = sync_profiles(profiles=profiles, web=web)
    assert again.changed == 0 and again.unchanged == 3


def test_a_missing_or_foreign_web_checkout_is_an_error(tmp_path: Path, web: Path) -> None:
    profiles = tmp_path / "MD"
    _profile(profiles, "KNOW-DOMP-0000001", "p")
    with pytest.raises(WebSyncError, match="DF-Insight"):
        sync_profiles(profiles=profiles, web=tmp_path / "elsewhere")
    with pytest.raises(WebSyncError, match="프로필이 없습니다"):
        sync_profiles(profiles=tmp_path / "empty", web=web)
    assert SyncReport().changed == 0


def test_exported_profiles_follow_df_insight_document_conventions() -> None:
    source = (
        "| DOI | N/A |\n| URL | https://example.org/a?b=1&c=2 |\n| 저자 | N/A |\n"
        "|  | Tool &amp; Notes &lt;v2&gt; &#124; more |\n| 항목 | N/A로 시작하는 값 |\n"
        "## 4. 대상 애플리케이션\n| ID | 이름 |\n|---|---|\n|  | N/A |\n\n"
    )
    assert export_profile_text(source) == (
        "| DOI |  |\n| URL | https://example.org/a?b=1&c=2 |\n| 저자 |  |\n"
        "|  | Tool & Notes <v2> &#124; more |\n| 항목 | N/A로 시작하는 값 |\n"
        "## 4. 대상 애플리케이션\n| ID | 이름 |\n|---|---|\n\n"   # an empty table keeps no placeholder row
    )


def test_sync_writes_the_exported_form_and_treats_it_as_unchanged(tmp_path: Path, web: Path) -> None:
    profiles = tmp_path / "MD"
    (profiles / "KNOW-DOMP-0000002").mkdir(parents=True)
    for name in ("ko.md", "en.md"):
        (profiles / "KNOW-DOMP-0000002" / name).write_text('last_updated: "2026-10-01"\n| DOI | N/A |\n|  | A &amp; B |\n', encoding="utf-8")
    references = web / "data" / "references_md"
    for name in ("ko.md", "en.md"):
        # Already in the exported form; only the generation date differs, which is not a change worth a commit.
        (references / "KNOW-DOMP-0000002" / name).write_text('last_updated: "2026-09-20"\n| DOI |  |\n|  | A & B |\n', encoding="utf-8")

    report = sync_profiles(profiles=profiles, web=web)
    assert report.unchanged == 1 and report.updated == [] and report.removed == ["KNOW-DOMP-0000001", "KNOW-DOMP-0000009"]
    assert (references / "KNOW-DOMP-0000002" / "ko.md").read_text(encoding="utf-8") == 'last_updated: "2026-09-20"\n| DOI |  |\n|  | A & B |\n'


def test_copies_the_build_turns_back_are_restored_with_their_old_date(tmp_path: Path, web: Path) -> None:
    """DF-Insight's link step fills the identifiers an export leaves blank; a copy that ends up as before is not a change."""

    profiles = tmp_path / "MD"
    references = web / "data" / "references_md"
    # A KNOW-DOMP number found by the build is resolved again at every sync (a renumbering can make it stale).
    linked = 'last_updated: "2026-09-20"\n| KNOW-DOMP-0000007 | A cited domestic paper. 2020. |\n'
    raw = 'last_updated: "2026-09-28"\n|  | A cited domestic paper. 2020. |\n'
    for record_id, body in (("KNOW-DOMP-0000001", raw), ("KNOW-DOMP-0000002", 'last_updated: "2026-09-28"\n|  | Another paper. 2021. |\n')):
        (profiles / record_id).mkdir(parents=True)
        for name in ("ko.md", "en.md"):
            (profiles / record_id / name).write_text(body, encoding="utf-8")
    for name in ("ko.md", "en.md"):
        (references / "KNOW-DOMP-0000001" / name).write_text(linked, encoding="utf-8")
        (references / "KNOW-DOMP-0000002" / name).write_text('last_updated: "2026-09-20"\n| KNOW-INTP-0000034 | A paper cited before. 2019. |\n', encoding="utf-8")

    report = sync_profiles(profiles=profiles, web=web)
    assert sorted(report.updated) == ["KNOW-DOMP-0000001", "KNOW-DOMP-0000002"] and len(report.previous) == 4
    # Pretend DF-Insight built: it finds the cited paper again (paper 1 is as before) but paper 2 really changed.
    for name in ("ko.md", "en.md"):
        (references / "KNOW-DOMP-0000001" / name).write_text('last_updated: "2026-09-28"\n| KNOW-DOMP-0000007 | A cited domestic paper. 2020. |\n', encoding="utf-8")
        (references / "KNOW-DOMP-0000002" / name).write_text('last_updated: "2026-09-28"\n| KNOW-INTP-0000056 | Another paper. 2021. |\n', encoding="utf-8")

    assert restore_unchanged_after_build(report) == 2
    assert (references / "KNOW-DOMP-0000001" / "ko.md").read_text(encoding="utf-8") == linked            # old date kept
    assert "Another paper" in (references / "KNOW-DOMP-0000002" / "ko.md").read_text(encoding="utf-8")  # a real change stays
    assert report.as_dict()["restored_after_build"] == 2


def test_a_reference_id_the_build_filled_in_is_not_a_difference(tmp_path: Path, web: Path) -> None:
    profiles = tmp_path / "MD"
    references = web / "data" / "references_md"
    exported = 'last_updated: "2026-10-03"\n| DOI |  |\n|  | Cited abroad. 2020. |\n|  | Not found anywhere. 2019. |\n| KNOW-DOMP-0000005 | Cited at home. 2021. |\n'
    for record_id in ("KNOW-DOMP-0000001", "KNOW-DOMP-0000002", "KNOW-DOMP-0000009"):
        (profiles / record_id).mkdir(parents=True)
        for name in ("ko.md", "en.md"):
            (profiles / record_id / name).write_text(exported, encoding="utf-8")
    filled = exported.replace("2026-10-03", "2026-09-20").replace("|  | Cited abroad.", "| KNOW-INTP-0000012 | Cited abroad.")
    for name in ("ko.md", "en.md"):
        (references / "KNOW-DOMP-0000001" / name).write_text(filled, encoding="utf-8")                                   # only an id the build filled in
        (references / "KNOW-DOMP-0000002" / name).write_text(filled.replace("Cited abroad.", "Cited abroad!"), encoding="utf-8")   # another citation text
        (references / "KNOW-DOMP-0000009" / name).write_text(filled.replace("| KNOW-DOMP-0000005 |", "| KNOW-INTP-0000099 |"), encoding="utf-8")   # an id the pipeline sets itself

    report = sync_profiles(profiles=profiles, web=web)

    assert report.unchanged == 1 and report.updated == ["KNOW-DOMP-0000002", "KNOW-DOMP-0000009"]
    assert (references / "KNOW-DOMP-0000001" / "ko.md").read_text(encoding="utf-8") == filled            # not written at all: no build needed to stay clean
    assert (references / "KNOW-DOMP-0000009" / "en.md").read_text(encoding="utf-8") == exported


# --- application tables: DF-Insight's links of a paper are kept unless the paper names other applications -----------

KCI = "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId="
LABELS = {
    "ko.md": ("## 1. 기본 정보", "## 2. 초록", "## 4. 대상 애플리케이션", "| ID | 이름 |", "## 5. 참고문헌"),
    "en.md": ("## 1. Basic Information", "## 2. Abstract", "## 4. Target Applications", "| ID | Name |", "## 5. References"),
}
CHROME, GMAIL = "| EXAM-04-01-01-001 | Chrome |", "| EXAM-04-01-05-006 | Gmail |"
# The registry a link consults: "Google Chrome" used to resolve to the Chrome node through an alias ...
OLD_REGISTRY = {"Google Chrome": [("EXAM-04-01-01-001", "Chrome")], "Gmail": [("EXAM-04-01-05-006", "Gmail")]}
# ... until upstream registered a node of that very name, which a fresh link now prefers.
NEW_REGISTRY = {**OLD_REGISTRY, "Google Chrome": [("EXAM-04-01-01-009", "Google Chrome")],
                "Slack": [("EXAM-04-01-02-014", "Slack"), ("EXAM-04-01-03-002", "Slack")]}


def _document(name: str, source_id: str, rows: list[str] | None, *, date: str = "2026-10-01", abstract: str = "초록") -> str:
    information, summary, applications, header, references = LABELS[name]
    table = [applications, header, "|---|---|", *rows, ""] if rows is not None else []
    return "\n".join([
        "---", 'identifier: "X"', f'last_updated: "{date}"', "---", "", "# 제목", "",
        information, "| 항목 | 내용 |", "|---|---|", f"| URL | {KCI}{source_id} |", "",
        summary, abstract, "",
        *table,
        references, "| ID | REF |", "|---|---|", f"|  | [다른 KCI 논문]({KCI}ART999999999). 2020. |", "",
    ])


def _raw(*names: str) -> list[str]:
    return [f"|  | {name} |" for name in names] or ["|  | N/A |"]


def _put(root: Path, record_id: str, source_id: str, rows: list[str] | None, *, only: str | None = None, **options: str) -> None:
    (root / record_id).mkdir(parents=True, exist_ok=True)
    for name in ("ko.md", "en.md"):
        if only in (None, name):
            (root / record_id / name).write_text(_document(name, source_id, rows, **options), encoding="utf-8")


def _rows(root: Path, record_id: str, name: str = "ko.md") -> list[str]:
    rows = web_sync._table((root / record_id / name).read_text(encoding="utf-8"))
    assert rows is not None
    return rows


def _build(references: Path, registry: dict[str, list[tuple[str, str]]]) -> None:
    """DF-Insight's link step in miniature (scripts/link-identifiers.mjs, pass A): the Korean table
    decides; a row with an id is kept as it is, a plain name is resolved through the registry, and
    both language files get the rebuilt table (sorted by id, unresolved names last) only when an id
    was gained."""

    for directory in sorted(references.glob("KNOW-DOMP-*")):
        found = web_sync._application_table((directory / "ko.md").read_text(encoding="utf-8").split("\n"))
        if found is None:
            continue
        linked: dict[str, str] = {}
        plain: list[str] = []
        gained = False
        for row in found[2]:
            identifier, title = web_sync._cells(row)[:2]
            if identifier:
                linked[identifier] = title
            elif title in registry:
                linked.update(dict(registry[title]))
                gained = True
            elif title:
                plain.append(title)
        if not gained:
            continue
        body = [f"| {identifier} | {title} |" for identifier, title in sorted(linked.items())] + [f"|  | {title} |" for title in plain]
        for name in ("ko.md", "en.md"):
            lines = (directory / name).read_text(encoding="utf-8").split("\n")
            span = web_sync._application_table(lines)
            if span is not None:
                (directory / name).write_text("\n".join(lines[:span[0]] + body + lines[span[1]:]), encoding="utf-8")


@pytest.fixture()
def tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    site = tmp_path / "site"
    (site / "scripts").mkdir(parents=True)
    (site / "scripts" / "build-graphs.mjs").write_text("// upstream build", encoding="utf-8")
    references = site / "data" / "references_md"
    references.mkdir(parents=True)
    profiles = tmp_path / "MD"
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Google Chrome", "Gmail"))
    _put(profiles, "KNOW-DOMP-0000002", "ART000000002", _raw())
    _put(profiles, "KNOW-DOMP-0000003", "ART000000003", _raw("Gmail", "카카오맵"))
    # The web copies as upstream committed them: linked, an older date, the empty table without a placeholder row.
    _put(references, "KNOW-DOMP-0000001", "ART000000001", [CHROME, GMAIL], date="2026-09-20")
    _put(references, "KNOW-DOMP-0000002", "ART000000002", [], date="2026-09-20")
    _put(references, "KNOW-DOMP-0000003", "ART000000003", [GMAIL, "|  | 카카오맵 |"], date="2026-09-20")
    return profiles, site, references, tmp_path / "web_sync_state.json"


def _snapshot(references: Path) -> dict[str, str]:
    return {str(path.relative_to(references)): path.read_text(encoding="utf-8") for path in sorted(references.rglob("*.md"))}


def _recorded(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8"))["papers"]


def test_reading_the_paper_id_and_the_application_names() -> None:
    korean = export_profile_text(_document("ko.md", "ART000000042", _raw("Telegram", "Tool &amp; Notes", "Google  Chrome", "Telegram")))
    assert paper_key(korean) == "ART000000042"                      # the URL row, not the KCI link of a reference row
    assert application_names(korean) == ("Google Chrome", "Telegram", "Tool & Notes")
    assert application_names(export_profile_text(_document("ko.md", "ART000000042", _raw()))) == ()
    assert application_names(_document("en.md", "ART000000042", _raw("Signal"))) == ("Signal",)
    assert application_names(_document("ko.md", "ART000000042", None)) is None
    assert application_names("## 4. 대상 애플리케이션\n표가 없는 절\n\n## 5. 참고문헌\n| ID | REF |\n|---|---|\n") is None
    assert paper_key("no url row") is None and paper_key(f"| URL | {KCI}ART000000001 |\n| URL | {KCI}ART000000002 |\n") is None


def test_first_sync_keeps_the_links_upstream_committed_and_records_the_names(tree) -> None:
    profiles, site, references, state = tree
    before = _snapshot(references)

    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert (report.unchanged, report.changed, report.links_adopted, report.links_kept, report.relinked, report.state_found) == (3, 0, 2, 0, {}, False)
    _build(references, NEW_REGISTRY)
    restore_unchanged_after_build(report)

    assert _snapshot(references) == before                            # byte for byte, although "Google Chrome" would now link elsewhere
    assert _recorded(state) == {
        "ART000000001": {"applications": ["Gmail", "Google Chrome"]},
        "ART000000002": {"applications": []},
        "ART000000003": {"applications": ["Gmail", "카카오맵"]},
    }
    again = sync_profiles(profiles=profiles, web=site, state=state)
    assert (again.unchanged, again.links_kept, again.links_adopted, again.state_found) == (3, 2, 0, True)
    assert again.as_dict()["application_links_kept"] == 2 and again.as_dict()["application_links_rebuilt"] == 0


def test_a_changed_application_list_is_exported_plain_and_linked_by_the_build(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Google Chrome", "Gmail", "Slack"))
    untouched = _snapshot(references)

    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.updated == ["KNOW-DOMP-0000001"] and report.relinked == {"KNOW-DOMP-0000001": NAMES_CHANGED}
    assert _rows(references, "KNOW-DOMP-0000001") == _rows(references, "KNOW-DOMP-0000001", "en.md") == _raw("Google Chrome", "Gmail", "Slack")
    _build(references, NEW_REGISTRY)
    restore_unchanged_after_build(report)

    assert _rows(references, "KNOW-DOMP-0000001") == [
        "| EXAM-04-01-01-009 | Google Chrome |", "| EXAM-04-01-02-014 | Slack |", "| EXAM-04-01-03-002 | Slack |", GMAIL]
    after = _snapshot(references)
    assert {key for key in after if after[key] != untouched[key]} == {"KNOW-DOMP-0000001/ko.md", "KNOW-DOMP-0000001/en.md"}
    entry = _recorded(state)["ART000000001"]
    assert entry["applications"] == ["Gmail", "Google Chrome", "Slack"] and len(entry["replaced"]) == 1
    third = sync_profiles(profiles=profiles, web=site, state=state)   # and it is a fixed point again
    assert third.changed == 0 and third.relinked == {} and _snapshot(references) == after


def test_other_changes_reach_the_web_copy_and_keep_its_links(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    # The same set of names in another order, one of them repeated: not a new list.
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Gmail", "Google Chrome", "Gmail"), abstract="고친 초록", date="2026-10-02")

    report = sync_profiles(profiles=profiles, web=site, state=state)

    assert report.updated == ["KNOW-DOMP-0000001"] and report.relinked == {} and report.links_kept == 2
    for name in ("ko.md", "en.md"):
        text = (references / "KNOW-DOMP-0000001" / name).read_text(encoding="utf-8")
        assert "고친 초록" in text and 'last_updated: "2026-10-02"' in text
        assert _rows(references, "KNOW-DOMP-0000001", name) == [CHROME, GMAIL]
        assert f"{LABELS[name][2]}\n{LABELS[name][3]}\n|---|---|\n{CHROME}\n{GMAIL}\n\n{LABELS[name][4]}\n" in text   # the export's own heading and header


def test_links_upstream_changes_in_a_later_pull_are_kept_but_its_other_edits_are_not(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    relinked = [GMAIL, "| EXAM-04-01-25-099 | KakaoMap |"]
    _put(references, "KNOW-DOMP-0000003", "ART000000003", relinked, date="2026-09-20")                 # upstream linked the plain row
    assert sync_profiles(profiles=profiles, web=site, state=state).changed == 0

    _put(references, "KNOW-DOMP-0000003", "ART000000003", relinked, date="2026-09-20", abstract="upstream edit")   # ... and edited what the pipeline owns
    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.updated == ["KNOW-DOMP-0000003"] and report.relinked == {}
    assert "upstream edit" not in (references / "KNOW-DOMP-0000003" / "ko.md").read_text(encoding="utf-8")
    assert _rows(references, "KNOW-DOMP-0000003") == relinked


def test_links_follow_the_paper_when_numbers_move(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    # Paper 1 leaves the corpus, papers 2 and 3 move up, and a new paper takes number 3.
    _put(profiles, "KNOW-DOMP-0000001", "ART000000002", _raw())
    _put(profiles, "KNOW-DOMP-0000002", "ART000000003", _raw("Gmail", "카카오맵"))
    _put(profiles, "KNOW-DOMP-0000003", "ART000000004", _raw("Google Chrome"))

    report = sync_profiles(profiles=profiles, web=site, state=state)

    assert report.updated == ["KNOW-DOMP-0000001", "KNOW-DOMP-0000002", "KNOW-DOMP-0000003"] and report.relinked == {}
    assert _rows(references, "KNOW-DOMP-0000001") == []
    assert _rows(references, "KNOW-DOMP-0000002") == [GMAIL, "|  | 카카오맵 |"]       # carried from the old directory 3
    assert _rows(references, "KNOW-DOMP-0000003") == _raw("Google Chrome")            # the new paper does not inherit the directory's rows
    _build(references, NEW_REGISTRY)
    assert _rows(references, "KNOW-DOMP-0000003") == ["| EXAM-04-01-01-009 | Google Chrome |"]
    assert sorted(_recorded(state)) == ["ART000000002", "ART000000003", "ART000000004"]   # the removed paper is forgotten


def test_a_web_copy_put_back_after_a_fresh_link_is_linked_again(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    committed = _snapshot(references)
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Google Chrome", "Gmail", "Slack"))
    for expected in (NAMES_CHANGED, REPLACED_TABLE_IS_BACK, REPLACED_TABLE_IS_BACK):
        # Every time the web repository is put back (git checkout / reset / another branch) the new name is exported again.
        report = sync_profiles(profiles=profiles, web=site, state=state)
        assert report.relinked == {"KNOW-DOMP-0000001": expected}
        _build(references, NEW_REGISTRY)
        restore_unchanged_after_build(report)
        assert "| EXAM-04-01-02-014 | Slack |" in _rows(references, "KNOW-DOMP-0000001")
        assert sync_profiles(profiles=profiles, web=site, state=state).relinked == {}       # settled as long as it is not put back
        for key, text in committed.items():
            (references / key).write_text(text, encoding="utf-8")


def test_a_fresh_link_that_gives_the_same_table_is_not_repeated(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    before = _snapshot(references)
    registry = {**OLD_REGISTRY, "G-mail": OLD_REGISTRY["Gmail"]}
    _put(profiles, "KNOW-DOMP-0000003", "ART000000003", _raw("G-mail", "카카오맵"), date="2026-10-02")   # a rename of the same application

    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.relinked == {"KNOW-DOMP-0000003": NAMES_CHANGED}
    assert len(load_state(state)["ART000000003"]["replaced"]) == 1
    _build(references, registry)
    assert restore_unchanged_after_build(report) == 2 and _snapshot(references) == before   # the same table: old bytes, old date

    assert "replaced" not in _recorded(state)["ART000000003"]
    assert sync_profiles(profiles=profiles, web=site, state=state).relinked == {}


def test_a_build_that_did_not_run_is_picked_up_by_the_next_one(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Slack"))
    sync_profiles(profiles=profiles, web=site, state=state)            # the build fails: the web copy keeps the plain row
    assert _rows(references, "KNOW-DOMP-0000001") == _raw("Slack")

    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.changed == 0 and report.relinked == {}              # nothing to export again ...
    _build(references, NEW_REGISTRY)
    assert _rows(references, "KNOW-DOMP-0000001") == ["| EXAM-04-01-02-014 | Slack |", "| EXAM-04-01-03-002 | Slack |"]   # ... the build links it
    assert sync_profiles(profiles=profiles, web=site, state=state).changed == 0


@pytest.mark.parametrize("content", [
    None,
    "{ not json",
    '{"version": 99, "papers": {"ART000000001": {"applications": ["Other"]}}}',
    '{"version": 1, "papers": {"ART000000001": {"applications": "Gmail"}}}',
])
def test_a_missing_or_unusable_record_keeps_the_web_tables(tree, content: str | None) -> None:
    profiles, site, references, state = tree
    if content is not None:
        state.write_text(content, encoding="utf-8")
    before = _snapshot(references)

    report = sync_profiles(profiles=profiles, web=site, state=state)

    assert report.changed == 0 and report.relinked == {} and _snapshot(references) == before
    assert report.links_adopted >= 1 and report.state_found is (content is not None and '"version": 1' in content)
    assert sorted(_recorded(state)) == ["ART000000001", "ART000000002", "ART000000003"]       # a valid record is written


def test_without_a_state_path_or_in_a_dry_run_nothing_is_recorded(tree, tmp_path: Path) -> None:
    profiles, site, references, state = tree
    before = _snapshot(references)

    report = sync_profiles(profiles=profiles, web=site)               # no state: every table is kept, nothing is remembered
    assert report.changed == 0 and report.links_adopted == 2 and report.state is None and _snapshot(references) == before
    preview = sync_profiles(profiles=profiles, web=site, state=state, dry_run=True)
    assert preview.changed == 0 and not state.exists()
    assert [path.name for path in tmp_path.iterdir() if path.suffix == ".json"] == []
    assert web_sync.DEFAULT_STATE.name == "web_sync_state.json" and web_sync.DEFAULT_STATE.parent.name == "Manifest"


def test_a_web_copy_whose_table_cannot_be_used_is_exported_plain(tree) -> None:
    profiles, site, references, state = tree
    broken = (references / "KNOW-DOMP-0000003" / "en.md").read_text(encoding="utf-8").replace("Target Applications", "Target Apps")
    (references / "KNOW-DOMP-0000003" / "en.md").write_text(broken, encoding="utf-8")       # one language alone is not enough
    _put(references, "KNOW-DOMP-0000002", "ART000000002", ["| EXAM-04-01-02-001 | KakaoTalk |"], date="2026-09-20")   # rows for a paper that names none
    _put(profiles, "KNOW-DOMP-0000004", "ART000000004", _raw("Gmail"))
    _put(references, "KNOW-DOMP-0000004", "ART000000004", [], date="2026-09-20")             # an empty table for a paper that names one

    report = sync_profiles(profiles=profiles, web=site, state=state)

    assert report.unreadable == ["KNOW-DOMP-0000003"]
    assert report.relinked == {"KNOW-DOMP-0000003": UNREADABLE_WEB_TABLE, "KNOW-DOMP-0000004": EMPTY_WEB_TABLE}
    assert _rows(references, "KNOW-DOMP-0000003") == _rows(references, "KNOW-DOMP-0000003", "en.md") == _raw("Gmail", "카카오맵")
    assert _rows(references, "KNOW-DOMP-0000004") == _raw("Gmail")
    assert _rows(references, "KNOW-DOMP-0000002") == ["| EXAM-04-01-02-001 | KakaoTalk |"]   # upstream's rows stay
    assert _rows(references, "KNOW-DOMP-0000001") == [CHROME, GMAIL]


def test_the_same_paper_in_several_web_directories(tree) -> None:
    profiles, site, references, state = tree
    # Left behind by an interrupted renumbering: paper 1 also sits in directory 9. Its own directory wins.
    _put(references, "KNOW-DOMP-0000009", "ART000000001", ["| EXAM-04-01-01-009 | Google Chrome |"])
    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.relinked == {} and report.removed == ["KNOW-DOMP-0000009"] and _rows(references, "KNOW-DOMP-0000001") == [CHROME, GMAIL]

    # Two directories other than its own hold the paper: nobody can say which links are its links.
    shutil.rmtree(references / "KNOW-DOMP-0000001")
    _put(references, "KNOW-DOMP-0000008", "ART000000001", [CHROME])
    _put(references, "KNOW-DOMP-0000009", "ART000000001", ["| EXAM-04-01-01-009 | Google Chrome |"])
    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.relinked == {"KNOW-DOMP-0000001": SEVERAL_WEB_COPIES} and report.unreadable == ["KNOW-DOMP-0000001"]
    assert report.added == ["KNOW-DOMP-0000001"] and _rows(references, "KNOW-DOMP-0000001") == _raw("Google Chrome", "Gmail")


def test_a_run_that_dies_between_the_record_and_the_documents_is_repeated(tree, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    before = _snapshot(references)
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Slack"))
    real_write = web_sync._write

    def dying_write(destination: Path, text: str) -> None:
        if destination.suffix == ".md":
            raise OSError("disk full")
        real_write(destination, text)

    monkeypatch.setattr(web_sync, "_write", dying_write)
    with pytest.raises(OSError):
        sync_profiles(profiles=profiles, web=site, state=state)
    monkeypatch.setattr(web_sync, "_write", real_write)
    assert _snapshot(references) == before and _recorded(state)["ART000000001"]["applications"] == ["Slack"]

    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.relinked == {"KNOW-DOMP-0000001": REPLACED_TABLE_IS_BACK}
    assert _rows(references, "KNOW-DOMP-0000001") == _raw("Slack")


def test_a_lost_web_copy_is_exported_plain_and_a_restored_old_one_is_recognised(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    committed = _snapshot(references)
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Slack"))
    report = sync_profiles(profiles=profiles, web=site, state=state)
    _build(references, NEW_REGISTRY)
    restore_unchanged_after_build(report)

    shutil.rmtree(references / "KNOW-DOMP-0000001")                     # the copy disappears ...
    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.added == ["KNOW-DOMP-0000001"] and report.relinked == {"KNOW-DOMP-0000001": WEB_COPY_GONE}
    for key, text in committed.items():                                  # ... and comes back as the old commit had it
        (references / key).write_text(text, encoding="utf-8")
    assert sync_profiles(profiles=profiles, web=site, state=state).relinked == {"KNOW-DOMP-0000001": REPLACED_TABLE_IS_BACK}


def test_profiles_that_cannot_be_followed_are_mirrored_as_before_and_two_of_one_paper_are_refused(tree) -> None:
    profiles, site, references, state = tree
    # The two language files of profile 3 name different papers: its table is not followed, the export is copied as it is.
    _put(profiles, "KNOW-DOMP-0000003", "ART000000033", _raw("Gmail", "카카오맵"), only="en.md")
    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.updated == ["KNOW-DOMP-0000003"] and report.relinked == {} and report.unreadable == []
    assert _rows(references, "KNOW-DOMP-0000003") == _raw("Gmail", "카카오맵") and sorted(_recorded(state)) == ["ART000000001", "ART000000002"]

    before = _snapshot(references)
    recorded = state.read_text(encoding="utf-8")
    _put(profiles, "KNOW-DOMP-0000004", "ART000000001", _raw("Slack"))
    with pytest.raises(WebSyncError, match="ART000000001"):
        sync_profiles(profiles=profiles, web=site, state=state)
    assert _snapshot(references) == before and state.read_text(encoding="utf-8") == recorded   # refused before anything is written


def test_each_language_keeps_its_own_rows(tree) -> None:
    profiles, site, references, state = tree
    # DF-Insight writes a node's Korean title into ko.md and its English title into en.md.
    korean, english = "| EXAM-04-01-11-003 | 한컴오피스 HWP |", "| EXAM-04-01-11-003 | Hancom Office HWP |"
    _put(profiles, "KNOW-DOMP-0000004", "ART000000004", _raw("Hancom Office"))
    _put(references, "KNOW-DOMP-0000004", "ART000000004", [korean], only="ko.md", date="2026-09-20")
    _put(references, "KNOW-DOMP-0000004", "ART000000004", [english], only="en.md", date="2026-09-20")
    sync_profiles(profiles=profiles, web=site, state=state)
    _put(profiles, "KNOW-DOMP-0000004", "ART000000004", _raw("Hancom Office"), abstract="고친 초록")

    report = sync_profiles(profiles=profiles, web=site, state=state)

    assert report.updated == ["KNOW-DOMP-0000004"] and report.relinked == {}
    assert _rows(references, "KNOW-DOMP-0000004", "ko.md") == [korean] and _rows(references, "KNOW-DOMP-0000004", "en.md") == [english]


def test_the_table_is_read_the_way_the_linker_reads_it(tree) -> None:
    profiles, site, references, state = tree
    # Rows after a blank line, indented rows and a table without a rule row are all rows to DF-Insight's linker.
    for name in ("ko.md", "en.md"):
        path = references / "KNOW-DOMP-0000001" / name
        path.write_text(path.read_text(encoding="utf-8").replace(f"{CHROME}\n{GMAIL}\n", f"  {CHROME}\n\n{GMAIL}\n"), encoding="utf-8")
        path = references / "KNOW-DOMP-0000003" / name
        path.write_text(path.read_text(encoding="utf-8").replace(f"|---|---|\n{GMAIL}\n", f"{GMAIL}\n"), encoding="utf-8")
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Google Chrome", "Gmail"), abstract="고친 초록")
    _put(profiles, "KNOW-DOMP-0000003", "ART000000003", _raw("Gmail", "카카오맵"), abstract="고친 초록")

    report = sync_profiles(profiles=profiles, web=site, state=state)

    assert report.updated == ["KNOW-DOMP-0000001", "KNOW-DOMP-0000003"] and report.relinked == {} and report.unreadable == []
    assert _rows(references, "KNOW-DOMP-0000001") == [f"  {CHROME}", GMAIL]            # nothing dropped, nothing linked afresh
    assert _rows(references, "KNOW-DOMP-0000003") == [GMAIL, "|  | 카카오맵 |"]
    assert "고친 초록" in (references / "KNOW-DOMP-0000001" / "en.md").read_text(encoding="utf-8")


def test_only_the_newest_replaced_tables_are_remembered_and_only_the_reproduced_one_is_forgotten(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    fingerprints = []
    for round_number in range(web_sync.REPLACED_TABLES_KEPT + 2):
        fingerprints.append(web_sync._fingerprint(_rows(references, "KNOW-DOMP-0000001")))
        _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw(f"App {round_number}"))
        sync_profiles(profiles=profiles, web=site, state=state)
        _build(references, {f"App {round_number}": [(f"EXAM-04-01-99-{round_number:03d}", f"App {round_number}")]})
    remembered = load_state(state)["ART000000001"]["replaced"]
    assert remembered == fingerprints[-web_sync.REPLACED_TABLES_KEPT:]                  # the oldest two are dropped

    # A list change whose fresh link gives back one of the remembered tables: only that one is forgotten.
    reproduced = web_sync.REPLACED_TABLES_KEPT + 1
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Another name of App 9"))
    report = sync_profiles(profiles=profiles, web=site, state=state)
    _build(references, {"Another name of App 9": [(f"EXAM-04-01-99-{reproduced:03d}", f"App {reproduced}")]})
    restore_unchanged_after_build(report)
    assert load_state(state)["ART000000001"]["replaced"] == remembered[1:]              # one more remembered, the reproduced one forgotten
    assert sync_profiles(profiles=profiles, web=site, state=state).relinked == {}


def test_the_plain_rows_being_exported_are_not_remembered_as_a_replaced_table(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    earlier = state.read_text(encoding="utf-8")
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Slack"))
    sync_profiles(profiles=profiles, web=site, state=state)                              # no build: the plain row stays in the web copy
    state.write_text(earlier, encoding="utf-8")                                          # the record is put back to an older one

    report = sync_profiles(profiles=profiles, web=site, state=state)

    assert report.relinked == {"KNOW-DOMP-0000001": NAMES_CHANGED} and report.changed == 0
    # The web table already is the plain export; remembering it would make the next run export it "again".
    assert _recorded(state)["ART000000001"] == {"applications": ["Slack"]}
    assert sync_profiles(profiles=profiles, web=site, state=state).relinked == {}


def test_a_fresh_link_seen_by_a_later_run_is_settled_then(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    before = _snapshot(references)
    registry = {**OLD_REGISTRY, "G-mail": OLD_REGISTRY["Gmail"]}
    _put(profiles, "KNOW-DOMP-0000003", "ART000000003", _raw("G-mail", "카카오맵"), date="2026-09-20")   # a rename, exported in a run whose build is skipped
    assert sync_profiles(profiles=profiles, web=site, state=state).relinked == {"KNOW-DOMP-0000003": NAMES_CHANGED}

    report = sync_profiles(profiles=profiles, web=site, state=state)                     # the next run builds
    assert report.relinked == {} and report.changed == 0
    _build(references, registry)
    restore_unchanged_after_build(report)

    assert _snapshot(references) == before and "replaced" not in _recorded(state)["ART000000003"]
    assert sync_profiles(profiles=profiles, web=site, state=state).relinked == {}        # no "put back" false alarm


def test_a_run_that_dies_between_the_two_files_of_a_paper_is_repeated(tree, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", _raw("Unregistered App"))
    real_write = web_sync._write

    def dying_write(destination: Path, text: str) -> None:
        if destination.name == "ko.md":
            raise OSError("disk full")
        real_write(destination, text)

    monkeypatch.setattr(web_sync, "_write", dying_write)
    with pytest.raises(OSError):
        sync_profiles(profiles=profiles, web=site, state=state)
    monkeypatch.setattr(web_sync, "_write", real_write)
    # The Korean file, whose table decides, is written last: it still shows the old links, so the paper is recognised.
    assert _rows(references, "KNOW-DOMP-0000001", "en.md") == _raw("Unregistered App") and _rows(references, "KNOW-DOMP-0000001") == [CHROME, GMAIL]

    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.relinked == {"KNOW-DOMP-0000001": REPLACED_TABLE_IS_BACK}
    assert _rows(references, "KNOW-DOMP-0000001") == _rows(references, "KNOW-DOMP-0000001", "en.md") == _raw("Unregistered App")


def test_a_web_copy_that_cannot_be_identified_is_reported(tree) -> None:
    profiles, site, references, state = tree
    (references / "KNOW-DOMP-0000001" / "en.md").unlink()                                # half a copy
    _put(references, "KNOW-DOMP-0000002", "ART000000099", [], only="en.md", date="2026-09-20")   # the two files name different papers
    report = sync_profiles(profiles=profiles, web=site, state=state)

    assert report.unreadable == ["KNOW-DOMP-0000001", "KNOW-DOMP-0000002"]
    assert report.relinked == {"KNOW-DOMP-0000001": UNIDENTIFIED_WEB_COPY}               # paper 2 names no application: nothing to link
    assert _rows(references, "KNOW-DOMP-0000001") == _raw("Google Chrome", "Gmail")
    assert web_sync.paper_key((references / "KNOW-DOMP-0000002" / "en.md").read_text(encoding="utf-8")) == "ART000000002"


def test_a_paper_without_applications_is_never_counted_as_linked_again(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    shutil.rmtree(references / "KNOW-DOMP-0000002")                                      # the copy of the paper that names no application
    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.added == ["KNOW-DOMP-0000002"] and report.relinked == {} and report.unreadable == []
    assert (report.links_kept, report.links_adopted) == (2, 0)


def test_half_written_or_linked_folders_are_refused_before_anything_is_removed(tree, tmp_path: Path) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)
    before, recorded = _snapshot(references), state.read_text(encoding="utf-8")

    (profiles / "KNOW-DOMP-0000002" / "en.md").unlink()                                  # the MD stage did not finish
    with pytest.raises(WebSyncError, match="KNOW-DOMP-0000002"):
        sync_profiles(profiles=profiles, web=site, state=state)
    assert _snapshot(references) == before and state.read_text(encoding="utf-8") == recorded   # its web copy is not deleted

    _put(profiles, "KNOW-DOMP-0000002", "ART000000002", _raw())
    (profiles / "KNOW-DOMP-0000007").symlink_to(profiles / "KNOW-DOMP-0000001", target_is_directory=True)
    with pytest.raises(WebSyncError, match="symlink"):
        sync_profiles(profiles=profiles, web=site, state=state)
    (profiles / "KNOW-DOMP-0000007").unlink()

    shutil.rmtree(references / "KNOW-DOMP-0000003")
    (references / "KNOW-DOMP-0000003").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WebSyncError, match="symlink"):
        sync_profiles(profiles=profiles, web=site, state=state)
    assert sorted(path.name for path in tmp_path.iterdir() if path.suffix == ".md") == []   # nothing written through the link


def test_kept_links_to_nodes_df_insight_no_longer_has_are_reported(tree) -> None:
    profiles, site, references, state = tree
    assert web_sync.unknown_application_ids(site) == {}                                  # no registry in this checkout: nothing to compare with
    (site / "data" / "techniques").mkdir()
    for identifier in ("EXAM-04-01-01-001", "EXAM-04-01-05-006"):
        (site / "data" / "techniques" / f"{identifier}.json").write_text("{}", encoding="utf-8")
    assert web_sync.unknown_application_ids(site) == {}                                  # plain rows and known nodes are fine
    _put(references, "KNOW-DOMP-0000002", "ART000000002", ["| EXAM-04-01-01-009 | Google Chrome |", "| EXAM-04-01-01-01 | Chrome |", GMAIL])
    assert web_sync.unknown_application_ids(site) == {"KNOW-DOMP-0000002": ["EXAM-04-01-01-009", "EXAM-04-01-01-01"]}


# --- Level-5 ids written by the pipeline (Analyzer/Application/level5.py) ------------------------------------------

DISCORD_ROWS = ["| COLL-02-04-01-002 | Discord |", "| EXAM-04-01-02-003 | Discord |"]


def test_application_rows_carry_the_level5_ids() -> None:
    korean = export_profile_text(_document("ko.md", "ART000000042", [*DISCORD_ROWS, "|  | KakaoMap |"]))
    assert application_rows(korean) == (("COLL-02-04-01-002", "Discord"), ("EXAM-04-01-02-003", "Discord"), ("", "KakaoMap"))
    assert application_names(korean) == ("Discord", "KakaoMap")
    assert application_rows(export_profile_text(_document("ko.md", "ART000000042", _raw()))) == ()


def test_pipeline_level5_ids_replace_the_web_table_once_and_then_stay(tree) -> None:
    profiles, site, references, state = tree
    sync_profiles(profiles=profiles, web=site, state=state)          # records the names-only tables (blank ids)
    # The Level-5 stage maps the same application names to ids chosen from the paper's context.
    mapped = ["| EXAM-04-01-01-001 | Chrome |", "| EXAM-03-03-01-009 | Google Chrome |", "| COLL-02-04-04-001 | Gmail |"]
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", mapped)

    report = sync_profiles(profiles=profiles, web=site, state=state)
    assert report.updated == ["KNOW-DOMP-0000001"]
    assert report.relinked == {"KNOW-DOMP-0000001": NAMES_CHANGED}     # "Chrome" is a new name next to "Google Chrome"
    assert _rows(references, "KNOW-DOMP-0000001") == _rows(references, "KNOW-DOMP-0000001", "en.md") == mapped
    entry = _recorded(state)["ART000000001"]
    assert entry["rows"] == [["EXAM-04-01-01-001", "Chrome"], ["COLL-02-04-04-001", "Gmail"], ["EXAM-03-03-01-009", "Google Chrome"]]

    _build(references, NEW_REGISTRY)                                   # rows with ids are never linked again
    again = sync_profiles(profiles=profiles, web=site, state=state)
    assert again.changed == 0 and again.relinked == {} and _rows(references, "KNOW-DOMP-0000001") == mapped

    # Only the ids change (an application DF-Insight registered later is now mapped): exported again.
    _put(profiles, "KNOW-DOMP-0000003", "ART000000003", [GMAIL, "| EXAM-04-01-25-042 | 카카오맵 |"])
    third = sync_profiles(profiles=profiles, web=site, state=state)
    assert third.relinked == {"KNOW-DOMP-0000003": IDS_CHANGED}
    assert _rows(references, "KNOW-DOMP-0000003") == [GMAIL, "| EXAM-04-01-25-042 | 카카오맵 |"]


def test_a_names_only_record_stands_for_blank_ids(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"version": 1, "papers": {
        "ART000000001": {"applications": ["Discord"]},
        "ART000000002": {"applications": ["Discord"], "rows": [["EXAM-04-01-02-003", "Discord"]]},
        "ART000000003": {"applications": ["Discord"], "rows": "broken"},
    }}), encoding="utf-8")
    loaded = load_state(state)
    assert loaded["ART000000001"]["rows"] == (("", "Discord"),)
    assert loaded["ART000000002"]["rows"] == (("EXAM-04-01-02-003", "Discord"),)
    assert loaded["ART000000003"]["rows"] == (("", "Discord"),)


# --- profile_rows_win: the Jev AI classification decides every table (the pipeline's mode since 2026-10-26) ----------

def test_profile_rows_win_gives_every_web_copy_the_profile_table(tree) -> None:
    profiles, site, references, state = tree

    report = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    # Without a record nothing is adopted: the links an earlier build made by name are not Jev's decisions.
    assert report.relinked == {"KNOW-DOMP-0000001": web_sync.WEB_ROWS_DIFFER, "KNOW-DOMP-0000003": web_sync.WEB_ROWS_DIFFER}
    assert (report.links_adopted, report.links_kept, report.updated) == (0, 0, ["KNOW-DOMP-0000001", "KNOW-DOMP-0000003"])
    for name in ("ko.md", "en.md"):
        assert _rows(references, "KNOW-DOMP-0000001", name) == _raw("Google Chrome", "Gmail")
        assert _rows(references, "KNOW-DOMP-0000003", name) == _raw("Gmail", "카카오맵")
    assert _recorded(state)["ART000000001"] == {"applications": ["Gmail", "Google Chrome"]}
    settled = _snapshot(references)

    again = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    assert (again.changed, again.relinked, again.links_kept) == (0, {}, 2)

    _build(references, NEW_REGISTRY)            # somebody ran DF-Insight's full build (pass A links the blank rows by name)
    third = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    assert third.relinked == {"KNOW-DOMP-0000001": web_sync.WEB_ROWS_DIFFER, "KNOW-DOMP-0000003": web_sync.WEB_ROWS_DIFFER}
    assert _snapshot(references) == settled     # the profile's rows are back

    # A new classification of the paper: the reason says what changed on our side.
    mapped = ["| EXAM-04-01-01-01 | Chrome |", "|  | Gmail |"]
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", mapped)
    fourth = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    assert fourth.relinked == {"KNOW-DOMP-0000001": NAMES_CHANGED} and _rows(references, "KNOW-DOMP-0000001") == mapped
    _put(profiles, "KNOW-DOMP-0000001", "ART000000001", ["| EXAM-04-01-01-02 | Chrome |", "|  | Gmail |"])
    assert sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True).relinked == {"KNOW-DOMP-0000001": IDS_CHANGED}
    # A paper whose web copy lost its rows, and one whose web table cannot be read, are reported as well.
    _put(references, "KNOW-DOMP-0000003", "ART000000003", [], date="2026-09-20")
    broken = (references / "KNOW-DOMP-0000001" / "en.md").read_text(encoding="utf-8").replace("## 4. Target Applications", "## 4. Targets")
    (references / "KNOW-DOMP-0000001" / "en.md").write_text(broken, encoding="utf-8")
    fifth = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    assert fifth.relinked == {"KNOW-DOMP-0000001": UNREADABLE_WEB_TABLE, "KNOW-DOMP-0000003": EMPTY_WEB_TABLE}
    assert fifth.unreadable == ["KNOW-DOMP-0000001"] and _rows(references, "KNOW-DOMP-0000001", "en.md") == ["| EXAM-04-01-01-02 | Chrome |", "|  | Gmail |"]


TITLES = {   # ko title, en title, DOI of the papers of the ``tree`` fixture
    "ART000000001": ("첫 번째 논문의 충분히 긴 제목", "A sufficiently long title of the first paper", "10.1234/first"),
    "ART000000002": ("두 번째 논문의 충분히 긴 제목", "A sufficiently long title of the second paper", ""),
    "ART000000003": ("세 번째 논문의 충분히 긴 제목", "A sufficiently long title of the third paper", ""),
    "ART000000004": ("네 번째 논문의 충분히 긴 제목", "A sufficiently long title of the fourth paper", ""),
}


def _titled(root: Path, record_id: str, source_id: str, rows: list[str] | None) -> None:
    """A profile or web copy with the title row DF-Insight's linker reads (``subject``) and a DOI row."""

    _put(root, record_id, source_id, rows)
    ko, en, doi = TITLES[source_id]
    for name, title in (("ko.md", ko), ("en.md", en)):
        path = root / record_id / name
        text = path.read_text(encoding="utf-8").replace('identifier: "X"', f'subject: "{title}"\nidentifier: "{record_id}"', 1)
        path.write_text(text.replace("|---|---|\n| URL |", f"|---|---|\n| DOI | {doi} |\n| URL |", 1), encoding="utf-8")


def _target(site: Path, name: str, rows: dict[str, list[str]]) -> Path:
    directory = site / "data" / "targets_md" / name
    directory.mkdir(parents=True, exist_ok=True)
    for language, lines in rows.items():
        heading = "## 관련 참고문헌" if language == "ko.md" else "## Related References"
        (directory / language).write_text("\n".join(["# 대상", "", heading, "| ID | 제목 |", "|---|---|", *lines, ""]), encoding="utf-8")
    return directory


def _row(record_id: str, source_id: str, language: str = "ko.md") -> str:
    return f"| {record_id} | {TITLES[source_id][0 if language == 'ko.md' else 1]} |"


def test_rows_of_df_insights_own_documents_follow_a_renumbered_paper(tree, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles, site, references, state = tree
    for record_id, source_id in (("KNOW-DOMP-0000001", "ART000000001"), ("KNOW-DOMP-0000002", "ART000000002"), ("KNOW-DOMP-0000003", "ART000000003")):
        _titled(references, record_id, source_id, _raw())
    # The pass D rows of a target document and a citation (pass B) in another family's document, written under the old numbers.
    target = _target(site, "TAGT-0000001", {
        name: [_row("KNOW-DOMP-0000001", "ART000000001", name), _row("KNOW-DOMP-0000002", "ART000000002", name),
               _row("KNOW-DOMP-0000003", "ART000000003", name), "| KNOW-DOMP-0000002 | 누군가 손으로 적은 다른 문헌 |"]
        for name in ("ko.md", "en.md")})
    (references / "KNOW-INTP-0000001" / "ko.md").parent.mkdir(parents=True, exist_ok=True)
    citation = "| KNOW-DOMP-0000001 | Kim, A., [A sufficiently long title of the first paper](https://doi.org/10.1234/first). J. 1(1) (2020). |"
    (references / "KNOW-INTP-0000001" / "ko.md").write_text(f"## 5. 참고문헌\n| ID | REF |\n|---|---|\n{citation}\n", encoding="utf-8")
    # Renumbering: papers 1 and 2 swap their numbers, paper 3 leaves the corpus and a new paper takes its number.
    shutil.rmtree(profiles)
    _titled(profiles, "KNOW-DOMP-0000001", "ART000000002", _raw())
    _titled(profiles, "KNOW-DOMP-0000002", "ART000000001", _raw())
    _titled(profiles, "KNOW-DOMP-0000003", "ART000000004", _raw())
    before = _snapshot(site / "data")

    preview = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True, dry_run=True)
    assert preview.renumbered == {"KNOW-DOMP-0000001": "KNOW-DOMP-0000002", "KNOW-DOMP-0000002": "KNOW-DOMP-0000001", "KNOW-DOMP-0000003": None}
    assert (preview.renumbered_rows, preview.unlinked_rows) == (5, 2) and _snapshot(site / "data") == before
    assert preview.unverified_rows == ["targets_md/TAGT-0000001/ko.md: KNOW-DOMP-0000002", "targets_md/TAGT-0000001/en.md: KNOW-DOMP-0000002"]

    # The run dies after the documents and before the profiles: the web copies still tell the old numbers ...
    monkeypatch.setattr(web_sync, "_save_state", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    monkeypatch.undo()
    # ... so the next run finds the same moves, and leaves the rows that already carry their new number alone.
    report = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    assert (report.renumbered_rows, report.unlinked_rows, report.renumbered_documents) == (0, 0, [])
    assert report.unverified_rows == preview.unverified_rows                          # the hand-written row is still reported
    for name in ("ko.md", "en.md"):
        lines = (target / name).read_text(encoding="utf-8").split("\n")
        assert lines[5:9] == [_row("KNOW-DOMP-0000002", "ART000000001", name), _row("KNOW-DOMP-0000001", "ART000000002", name),
                              f"|  | {TITLES['ART000000003'][0 if name == 'ko.md' else 1]} |", "| KNOW-DOMP-0000002 | 누군가 손으로 적은 다른 문헌 |"]
    assert (references / "KNOW-INTP-0000001" / "ko.md").read_text(encoding="utf-8").endswith(citation.replace("KNOW-DOMP-0000001", "KNOW-DOMP-0000002", 1) + "\n")

    settled = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)    # the web copies have the new numbers now
    assert settled.renumbered == {} and settled.changed == 0 and settled.unverified_rows == []
    assert web_sync.paper_key((references / "KNOW-DOMP-0000002" / "ko.md").read_text(encoding="utf-8")) == "ART000000001"


def test_a_paper_without_a_profile_is_not_taken_for_one_that_left_unless_every_profile_can_be_read(tree) -> None:
    profiles, site, references, state = tree
    _titled(references, "KNOW-DOMP-0000003", "ART000000003", _raw())
    target = _target(site, "TAGT-0000001", {"ko.md": [_row("KNOW-DOMP-0000003", "ART000000003")]})
    shutil.rmtree(profiles / "KNOW-DOMP-0000003")
    _put(profiles, "KNOW-DOMP-0000004", "ART000000004", _raw())
    (profiles / "KNOW-DOMP-0000004" / "ko.md").write_text("no URL row: the paper of this profile is unknown\n", encoding="utf-8")

    report = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    assert report.renumbered == {} and _row("KNOW-DOMP-0000003", "ART000000003") in (target / "ko.md").read_text(encoding="utf-8")


def test_a_row_that_names_both_papers_of_a_swapped_number_is_left_alone_and_reported(tree) -> None:
    profiles, site, references, state = tree
    short, long = "A sufficiently long title", "A sufficiently long title of the second paper"   # one title inside the other
    for root, pairs in ((references, (("KNOW-DOMP-0000001", "ART000000001"), ("KNOW-DOMP-0000002", "ART000000002"))),
                        (profiles, (("KNOW-DOMP-0000001", "ART000000002"), ("KNOW-DOMP-0000002", "ART000000001")))):
        shutil.rmtree(root / "KNOW-DOMP-0000003", ignore_errors=True)
        for record_id, source_id in pairs:
            _put(root, record_id, source_id, _raw())
            for name in ("ko.md", "en.md"):
                path = root / record_id / name
                title = short if source_id == "ART000000001" else long
                path.write_text(path.read_text(encoding="utf-8").replace('identifier: "X"', f'subject: "{title}"\nidentifier: "{record_id}"', 1), encoding="utf-8")
    target = _target(site, "TAGT-0000001", {"ko.md": [f"| KNOW-DOMP-0000001 | {short} |", f"| KNOW-DOMP-0000002 | {long} |"]})

    report = sync_profiles(profiles=profiles, web=site, state=state, profile_rows_win=True)
    assert report.renumbered_rows == 1 and report.unverified_rows == ["targets_md/TAGT-0000001/ko.md: KNOW-DOMP-0000002"]
    assert (target / "ko.md").read_text(encoding="utf-8").split("\n")[5:7] == [f"| KNOW-DOMP-0000002 | {short} |", f"| KNOW-DOMP-0000002 | {long} |"]
