from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from Core.deduplicator import deduplicate_pdf_candidates
from Core.normalizer import NormalizedPaper
from Exporter.report_generator import generate_report
from Storage.db import DatabaseManager
from Util.debugger import PDF_PROVIDER_ORDER, PdfCollectionDebugger
from Util.logger import ProgressLogger


def _paper(source_id: str) -> NormalizedPaper:
    return NormalizedPaper(
        source="kci",
        source_id=source_id,
        title="디지털 포렌식 논문",
        title_kor="디지털 포렌식 논문",
        title_eng=None,
        title_other=None,
        doi=None,
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


def test_progress_logger_flushes_structured_progress() -> None:
    stream = io.StringIO()
    logger = ProgressLogger(
        stream=stream,
        clock=lambda: 5.0,
        now=lambda: datetime(2026, 8, 4, 12, 30, 0),
    )

    event = logger.pdf("download", current=2, total=4, counters={"saved": 1})

    assert event.current == 2
    assert "[KCI-PDF] download progress=2/4 (50.0%) saved=1" in stream.getvalue()


def test_pdf_debugger_enforces_order_and_unresolved_only(tmp_path: Path) -> None:
    debugger = PdfCollectionDebugger(tmp_path / "events.jsonl")
    unresolved = {"A", "B", "C"}
    for index, provider in enumerate(PDF_PROVIDER_ORDER):
        after = unresolved - ({sorted(unresolved)[0]} if unresolved else set())
        record = debugger.record_stage(provider, unresolved, after)
        assert record.attempted_count == len(unresolved)
        unresolved = after

    assert len(debugger.records) == 8
    assert len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 8

    with pytest.raises(ValueError, match="already been recorded"):
        debugger.record_stage("sciencedirect", (), ())


def test_pdf_debugger_rejects_provider_order_violation() -> None:
    debugger = PdfCollectionDebugger()
    with pytest.raises(ValueError, match="expected kci"):
        debugger.record_stage("openalex", {"A"}, {"A"})


def test_pdf_candidate_deduplication_uses_source_id() -> None:
    papers = [_paper("ART1"), _paper("art1"), _paper("ART2")]
    assert [paper.source_id for paper in deduplicate_pdf_candidates(papers)] == ["ART1", "ART2"]


def test_generate_report_reads_accumulated_database(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'papers.db'}"
    manager = DatabaseManager(database_url)
    manager.create_tables()
    manager.upsert_papers([_paper("ART000000001")])

    output = tmp_path / "kci_papers.xlsx"
    result = generate_report(database_url=database_url, output_path=output)

    assert result.row_count == 1
    assert result.path == output
    workbook = load_workbook(output, read_only=True)
    assert workbook["전체"].max_row == 2
