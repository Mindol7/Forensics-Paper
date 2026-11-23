from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from Analyzer import numbering
from Analyzer.numbering import Layout, NumberingError, build_plan, record_id, renumber, verify


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Corpus:
    """A miniature repository: papers A..F, some analysed, some without a PDF, one removed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.layout = Layout(root)
        self.database = root / "kci.db"
        connection = sqlite3.connect(self.database)
        connection.execute("create table kci_papers (source_id text)")
        connection.commit()
        connection.close()
        self.files: list[dict[str, str]] = []
        _write(self.layout.profiles, {"schema_version": 1, "records": {}})

    def add(self, source_id: str, *, number: int | None, pdf: bool = True, analysed: bool = True, record: str | None = None,
            with_analysis_record: bool = False) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("insert into kci_papers values (?)", (source_id,))
        connection.commit()
        connection.close()
        if pdf:
            (self.layout.papers).mkdir(parents=True, exist_ok=True)
            (self.layout.papers / f"{source_id}.pdf").write_bytes(b"%PDF-1.4 " + source_id.encode())
            self.files.append({"source_id": source_id, "title": source_id, "filename": f"{source_id}.pdf"})
            _write(self.layout.paper_files, {"papers": self.files})
        if analysed:
            lv3 = self.layout.lv3 / f"{source_id}.json"
            _write(lv3, {"schema_version": "1", "source_id": source_id, "paper_index": number, "title": "제목 " + source_id, "structure_sha256": "s" * 64})
            decision = self.layout.type_decisions / f"{source_id}.json"
            _write(decision, {"source_id": source_id, "subtype": "ar", "lv3_manifest_sha256": _sha(lv3)})
            if with_analysis_record:
                _write(self.layout.analysis / f"{source_id}.json", {
                    "source_id": source_id, "paper_index": number,
                    "artifacts": {"lv3_manifest_sha256": _sha(lv3), "type_decision_sha256": _sha(decision), "structure_sha256": "s" * 64},
                })
        elif number is not None:
            payload = json.loads(self.layout.reservations.read_text(encoding="utf-8")) if self.layout.reservations.is_file() else {"reservations": {}}
            payload["reservations"][source_id] = number
            _write(self.layout.reservations, payload)
        if number is not None or record:
            profiles = json.loads(self.layout.profiles.read_text(encoding="utf-8"))
            profiles["records"][source_id] = record or record_id(number)
            _write(self.layout.profiles, profiles)
            if pdf and analysed:
                directory = self.layout.profile_tree / profiles["records"][source_id]
                directory.mkdir(parents=True, exist_ok=True)
                for name in ("ko.md", "en.md"):
                    (directory / name).write_text(f"identifier: {directory.name}\nartiId={source_id}\n", encoding="utf-8")


@pytest.fixture()
def corpus(tmp_path: Path) -> Corpus:
    built = Corpus(tmp_path)
    # Reviewed corpus 1..4 in KCI-id order; number 2 was removed after review (a gap), and the
    # increment arrived out of order: an older paper got 6, a paper without a PDF got 5.
    built.add("ART000000010", number=1)
    built.add("ART000000030", number=3)
    built.add("ART000000040", number=4, record=record_id(7))           # profile id disagrees with the number
    built.add("ART000000050", number=5, pdf=False, analysed=False)     # no PDF
    built.add("ART000000020", number=6, with_analysis_record=True)     # older paper selected late
    built.add("ART000000060", number=8, analysed=False)                # PDF arrived, analysis still to come
    profiles = json.loads(built.layout.profiles.read_text(encoding="utf-8"))
    profiles["records"]["ART000000099"] = record_id(2)                  # the removed paper still holds id 2
    _write(built.layout.profiles, profiles)
    _write(built.layout.excluded, {"schema_version": 1, "papers": [
        {"source_id": "ART000000099", "record_id": record_id(2), "paper_index": 2, "title": "제외된 논문", "reason": "검토 후 제외"}]})
    review = built.layout.linked_documents[0]
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(
        "| [ART000000020 — 늦게 온 논문](../../Paper/MD/KNOW-DOMP-0000006/ko.md) | [ART000000099 — 제외됨](../../Paper/MD/KNOW-DOMP-0000002/ko.md) |\n",
        encoding="utf-8")
    return built


def test_numbers_follow_the_rule_pdf_papers_by_kci_id_then_the_rest(corpus: Corpus) -> None:
    plan = build_plan(database=corpus.database, layout=corpus.layout)
    assert plan.target == {
        "ART000000010": 1, "ART000000020": 2, "ART000000030": 3, "ART000000040": 4, "ART000000060": 5,  # PDF on disk, by KCI id
        "ART000000050": 6,                                                                               # no PDF: after them
    }
    assert not plan.in_order and plan.stale_profile_entries == ("ART000000099",)
    assert set(plan.changed) == {"ART000000020", "ART000000040", "ART000000050", "ART000000060"}  # 010 and 030 keep 1 and 3
    assert plan.changed["ART000000040"] == (4, 4)  # the number is right, the profile id is not

    preview = renumber(database=corpus.database, layout=corpus.layout, dry_run=True)
    assert preview["changed_count"] == 4 and not preview["in_order"]
    assert json.loads((corpus.layout.lv3 / "ART000000020.json").read_text(encoding="utf-8"))["paper_index"] == 6  # untouched


def test_renumbering_rewrites_every_store_and_keeps_the_hash_chain_valid(corpus: Corpus) -> None:
    layout = corpus.layout
    untouched_before = (layout.lv3 / "ART000000010.json").read_bytes()
    report = renumber(database=corpus.database, layout=layout, now=datetime(2026, 9, 20, 22, 0, 0))

    assert report["changed_count"] == 4 and report["documents_relinked"] == 1
    assert (report["new_count"], report["moved_count"]) == (0, 4)
    plan = verify(database=corpus.database, layout=layout)  # raises unless everything follows the rule
    assert plan.in_order
    for source_id, number in plan.target.items():
        lv3 = layout.lv3 / f"{source_id}.json"
        if lv3.is_file():
            assert json.loads(lv3.read_text(encoding="utf-8"))["paper_index"] == number
            decision = json.loads((layout.type_decisions / f"{source_id}.json").read_text(encoding="utf-8"))
            assert decision["lv3_manifest_sha256"] == _sha(lv3) and decision["subtype"] == "ar"
    assert (layout.lv3 / "ART000000010.json").read_bytes() == untouched_before       # a number that stays is not rewritten
    record = json.loads((layout.analysis / "ART000000020.json").read_text(encoding="utf-8"))
    assert record["paper_index"] == 2
    assert record["artifacts"] == {
        "lv3_manifest_sha256": _sha(layout.lv3 / "ART000000020.json"),
        "type_decision_sha256": _sha(layout.type_decisions / "ART000000020.json"), "structure_sha256": "s" * 64,
    }
    # Papers without analysis evidence keep their number in the reservation file — and only those papers.
    assert json.loads(layout.reservations.read_text(encoding="utf-8"))["reservations"] == {"ART000000060": 5, "ART000000050": 6}
    # Profile id == number for every selected paper; the removed paper no longer holds a live id.
    assert json.loads(layout.profiles.read_text(encoding="utf-8"))["records"] == {source_id: record_id(number) for source_id, number in sorted(plan.target.items())}
    excluded = json.loads(layout.excluded.read_text(encoding="utf-8"))["papers"][0]
    assert excluded["former_paper_index"] == 2 and excluded["former_record_id"] == record_id(2) and "record_id" not in excluded and "paper_index" not in excluded
    # Profiles carry their id inside, so a folder whose id moved is removed for the MD stage to rebuild (never
    # renamed); folders that are still right stay, so a failed MD stage cannot leave the corpus without profiles.
    assert report["profiles_cleared"] == 2
    assert sorted(path.name for path in layout.profile_tree.iterdir()) == [record_id(1), record_id(3)]
    # Hand-written links follow the paper (by KCI id); a removed paper points at its archived profile.
    assert layout.linked_documents[0].read_text(encoding="utf-8") == (
        "| [ART000000020 — 늦게 온 논문](../../Paper/MD/KNOW-DOMP-0000002/ko.md) | [ART000000099 — 제외됨](../../Removed/ART000000099/profile_md/ko.md) |\n")
    # The old -> new table is kept for people who noted a paper by its old number.
    history = json.loads(layout.history.read_text(encoding="utf-8"))["runs"][0]
    assert {"source_id": "ART000000020", "from_paper_index": 6, "from_record_id": record_id(6), "to_paper_index": 2, "to_record_id": record_id(2)} in history["changed"]
    assert Path(history["backup"]).is_file() and not layout.journal.exists()

    # Already in order: nothing is touched again (no backup, no cleared profiles, no history entry).
    again = renumber(database=corpus.database, layout=layout)
    assert again["in_order"] and "backup" not in again and (layout.profile_tree / record_id(1)).is_dir()
    assert len(json.loads(layout.history.read_text(encoding="utf-8"))["runs"]) == 1


def test_an_interrupted_renumbering_is_finished_by_running_it_again(corpus: Corpus, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = corpus.layout
    original = numbering._rewrite_chain
    calls = {"count": 0}

    def crash_midway(layout_: Layout, source_id: str, number: int) -> bool:
        calls["count"] += 1
        if calls["count"] == 2:  # ART000000020, the paper whose Lv3 number really changes (6 -> 2)
            # Worst moment: the Lv3 file of this paper is rewritten, its Type pin is not yet.
            manifest = json.loads((layout_.lv3 / f"{source_id}.json").read_text(encoding="utf-8"))
            manifest["paper_index"] = number
            _write(layout_.lv3 / f"{source_id}.json", manifest)
            raise OSError("power cut")
        return original(layout_, source_id, number)

    monkeypatch.setattr(numbering, "_rewrite_chain", crash_midway)
    with pytest.raises(OSError):
        renumber(database=corpus.database, layout=layout)
    assert layout.journal.is_file()
    with pytest.raises(NumberingError):
        verify(database=corpus.database, layout=layout)

    monkeypatch.setattr(numbering, "_rewrite_chain", original)
    report = renumber(database=corpus.database, layout=layout)
    assert not report["in_order"] and not layout.journal.exists()
    assert verify(database=corpus.database, layout=layout).in_order
    history = json.loads(layout.history.read_text(encoding="utf-8"))["runs"]
    assert len(history) == 1 and len(history[0]["changed"]) == 4  # the old numbers come from the journal, not from the half-written state


def test_a_stale_pin_is_never_papered_over(corpus: Corpus) -> None:
    decision = corpus.layout.type_decisions / "ART000000030.json"
    payload = json.loads(decision.read_text(encoding="utf-8"))
    payload["lv3_manifest_sha256"] = "0" * 64  # the decision was made for different evidence
    _write(decision, payload)
    before = (corpus.layout.lv3 / "ART000000020.json").read_bytes()

    with pytest.raises(NumberingError, match="stale pin"):
        renumber(database=corpus.database, layout=corpus.layout)
    assert (corpus.layout.lv3 / "ART000000020.json").read_bytes() == before and not corpus.layout.journal.exists()


def test_a_kept_profile_that_links_a_moved_id_is_rebuilt_too(corpus: Corpus) -> None:
    """The reference table links cited papers by id; a profile that mentions an id that moved would point at another paper."""

    layout = corpus.layout
    citing = layout.profile_tree / record_id(1) / "ko.md"
    citing.write_text(citing.read_text(encoding="utf-8") + "| KNOW-DOMP-0000006 | 늦게 온 논문을 인용 |\n", encoding="utf-8")
    report = renumber(database=corpus.database, layout=layout)
    assert report["profiles_cleared"] == 3
    assert sorted(path.name for path in layout.profile_tree.iterdir()) == [record_id(3)]


def test_new_papers_are_appended_without_touching_existing_profiles(tmp_path: Path) -> None:
    """The monthly case: numbers of the existing papers stay, so no profile is removed and no alarm is raised."""

    built = Corpus(tmp_path)
    built.add("ART000000010", number=1)
    built.add("ART000000020", number=2)
    built.add("ART000000030", number=None, analysed=False)             # new paper with a PDF, not analysed yet
    built.add("ART000000040", number=None, pdf=False, analysed=False)  # new paper without a PDF

    report = renumber(database=built.database, layout=built.layout)
    assert (report["new_count"], report["moved_count"], report["profiles_cleared"]) == (2, 0, 0)
    assert json.loads(built.layout.reservations.read_text(encoding="utf-8"))["reservations"] == {"ART000000030": 3, "ART000000040": 4}
    assert sorted(path.name for path in built.layout.profile_tree.iterdir()) == [record_id(1), record_id(2)]

    # The analysis stage then writes the evidence of paper 3 under its reserved number; the reservation
    # that is left behind is not "out of order" (the tracker must not ask for a renumbering every month).
    lv3 = built.layout.lv3 / "ART000000030.json"
    _write(lv3, {"schema_version": "1", "source_id": "ART000000030", "paper_index": 3})
    _write(built.layout.type_decisions / "ART000000030.json", {"source_id": "ART000000030", "lv3_manifest_sha256": _sha(lv3)})
    assert numbering.numbering_health(database=built.database, layout=built.layout)["in_order"] is True
    assert renumber(database=built.database, layout=built.layout)["in_order"] is True


def test_an_interrupted_run_is_finished_even_if_the_corpus_changed_meanwhile(corpus: Corpus, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = corpus.layout
    original = numbering._rewrite_chain
    monkeypatch.setattr(numbering, "_rewrite_chain", lambda *args: (_ for _ in ()).throw(OSError("power cut")))
    with pytest.raises(OSError):
        renumber(database=corpus.database, layout=layout)
    monkeypatch.setattr(numbering, "_rewrite_chain", original)
    assert numbering.numbering_health(database=corpus.database, layout=layout) == {"in_order": False, "reason": "중단된 번호 정렬이 남아 있습니다"}
    assert renumber(database=corpus.database, layout=layout, dry_run=True)["interrupted"] is True and layout.journal.is_file()

    # Before anybody resumes, a new paper with a PDF is selected: the old plan no longer matches the corpus.
    corpus.add("ART000000005", number=None, analysed=False)
    report = renumber(database=corpus.database, layout=layout)
    assert report["resumed"] is True and not layout.journal.exists()
    plan = verify(database=corpus.database, layout=layout)
    assert plan.target["ART000000005"] == 1 and plan.target["ART000000010"] == 2
    runs = json.loads(layout.history.read_text(encoding="utf-8"))["runs"]
    assert len(runs) == 2 and len({run["backup"] for run in runs}) == 2  # the finished old plan, then today's plan


def test_problems_are_reported_before_anything_is_written(corpus: Corpus) -> None:
    layout = corpus.layout
    before = (layout.lv3 / "ART000000020.json").read_bytes()
    # An Analysis record whose Lv3 manifest is gone: the analysis stage would keep using its stale number.
    _write(layout.analysis / "ART000000030.json", {"source_id": "ART000000030", "paper_index": 3})
    (layout.lv3 / "ART000000030.json").unlink()
    with pytest.raises(NumberingError, match="Analysis.*Lv3|Lv3"):
        renumber(database=corpus.database, layout=layout)
    assert (layout.lv3 / "ART000000020.json").read_bytes() == before and not layout.journal.exists() and not layout.history.exists()
    (layout.analysis / "ART000000030.json").unlink()

    # Evidence of a paper that is not selected: the message says what to do, per cause.
    _write(layout.lv3 / "ART000000099.json", {"paper_index": 2})       # listed in excluded_papers.json -> interrupted removal
    _write(layout.lv3 / "ART000000777.json", {"paper_index": 9})       # unknown -> removed by hand
    with pytest.raises(NumberingError) as error:
        renumber(database=corpus.database, layout=layout)
    assert "paper_removal --source-ids ART000000099" in str(error.value) and "ART000000777" in str(error.value)

    # A Type decision without a pin is not "valid by default".
    (layout.lv3 / "ART000000099.json").unlink()
    (layout.lv3 / "ART000000777.json").unlink()
    _write(layout.type_decisions / "ART000000010.json", {"source_id": "ART000000010", "subtype": "ar"})
    with pytest.raises(NumberingError, match="stale pin"):
        renumber(database=corpus.database, layout=layout)


def test_tidying_leftovers_does_not_touch_profiles(tmp_path: Path) -> None:
    built = Corpus(tmp_path)
    built.add("ART000000010", number=1)
    built.add("ART000000020", number=2)
    # A reservation that names a paper which is no longer selected is a leftover to tidy.
    _write(built.layout.reservations, {"reservations": {"ART000000099": 3}})

    report = renumber(database=built.database, layout=built.layout)
    assert report["housekeeping_only"] and report["changed_count"] == 0
    assert json.loads(built.layout.reservations.read_text(encoding="utf-8"))["reservations"] == {}
    assert (built.layout.profile_tree / record_id(2) / "ko.md").is_file() and not built.layout.history.exists()
    assert numbering.numbering_health(database=built.database, layout=built.layout) == {"in_order": True, "papers_to_renumber": 0, "problems": []}


def test_frozen_workbooks_follow_the_numbers_by_kci_id(corpus: Corpus) -> None:
    from openpyxl import Workbook, load_workbook

    path = corpus.layout.numbered_workbooks[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "논문별 분류"
    for row in (["paper_index", "source", "source_id", "title"], [6, "kci", "ART000000020", "늦게 온 논문"], [2, "kci", "ART000000099", "제외된 논문"], [1, "kci", "ART000000010", "첫 논문"]):
        sheet.append(row)
    other = book.create_sheet("분류 요약")
    other.append(["path_id", "count"])
    other.append(["EXAM-04-01", 3])
    book.save(path)

    report = renumber(database=corpus.database, layout=corpus.layout)
    assert report["workbooks_synced"] == 1
    rows = list(load_workbook(path)["논문별 분류"].iter_rows(values_only=True))
    assert rows[1][0] == 2 and rows[3][0] == 1      # by KCI id, whatever number the row carried
    assert rows[2][0] is None                        # a paper that left the corpus has no number
    assert list(load_workbook(path)["분류 요약"].iter_rows(values_only=True))[1] == ("EXAM-04-01", 3)
