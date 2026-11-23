from __future__ import annotations

import json
from pathlib import Path

from Analyzer.journal_names import JOURNAL_SERIALS, is_serial, resolve_journal_name


def test_a_name_passes_through_and_a_reviewed_serial_is_named(tmp_path: Path) -> None:
    table = tmp_path / "journal_serials.json"
    table.write_text(json.dumps({"journals": {
        "SER000000001": {"name": " 시험학술지 "}, "SER000000002": {"name": ""}, "not-a-serial": {"name": "무시"},
    }}, ensure_ascii=False), encoding="utf-8")

    assert resolve_journal_name("디지털포렌식연구", path=table) == "디지털포렌식연구"
    assert resolve_journal_name("SER000000001", path=table) == "시험학술지"
    # Unknown or unnamed serials stay unresolved: callers show "missing", never a guess or the raw code.
    assert resolve_journal_name("SER000000002", path=table) is None
    assert resolve_journal_name("SER000000009", path=table) is None
    assert resolve_journal_name(None, path=table) is None and resolve_journal_name("  ", path=table) is None
    assert resolve_journal_name("SER000000001", path=tmp_path / "missing.json") is None
    assert is_serial("SER000003109") and not is_serial("SERIES") and not is_serial(None)


def test_every_serial_in_the_corpus_has_a_reviewed_name() -> None:
    import sqlite3

    database = Path(__file__).resolve().parents[1] / "kci_pipeline.db"
    if not database.is_file():
        return
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        serials = [row[0] for row in connection.execute("select distinct journal_id from kci_papers where journal_id like 'SER%'")]
    finally:
        connection.close()
    assert JOURNAL_SERIALS.is_file()
    assert [serial for serial in serials if resolve_journal_name(serial) is None] == []
