from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis_fixtures import (
    VOCABULARY,
    analysis_record,
    file_record,
    write_index,
    write_pdf,
)

from Analyzer.analysis_stage import (
    next_paper_index,
    run_analysis_stage,
    select_targets,
)
from Core.normalizer import NormalizedPaper
from PaperPipeline.claude_analyzer import ClaudeAnalysisError, ClaudeConfig
from PaperPipeline.storage import sha256_file
from Storage.db import DatabaseManager


A, B = "ART000000001", "ART000000002"


def _paper(source_id: str, title: str) -> NormalizedPaper:
    return NormalizedPaper(
        source="kci",
        source_id=source_id,
        title=title,
        title_kor=title,
        title_eng="English " + source_id,
        title_other=None,
        doi=None,
        uci=None,
        url=None,
        abstract="초록",
        abstract_kor=None,
        abstract_eng=None,
        abstract_other=None,
        authors=["홍길동(테스트대학교)"],
        journal_id="디지털포렌식연구",
        publication_year=2026,
        categories=["디지털 포렌식"],
        matched_keywords=["디지털 포렌식"],
        relevance_reasons=["matched_in:title"],
        is_relevant=True,
    )


class _Corpus:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.papers_dir = root / "Paper"
        self.manifest_dir = root / "Manifest"
        self.analysis_dir = self.manifest_dir / "Analysis"
        self.lv3_dir = self.manifest_dir / "Lv3"
        self.type_dir = self.manifest_dir / "Type"
        self.structure_dir = root / "Structure"
        self.records = [file_record(A, "첫 논문", "첫 논문.pdf"), file_record(B, "둘째 논문", "둘째 논문.pdf")]
        self.sha = {r.source_id: write_pdf(self.papers_dir / r.filename) for r in self.records}
        write_index(self.manifest_dir, self.records)
        # Paper A already analyzed with paper_index 1 (legacy-style Lv3 manifest).
        self.analysis_dir.mkdir(parents=True)
        self.lv3_dir.mkdir()
        record_a = analysis_record(source_id=A, title="첫 논문", pdf_sha256=self.sha[A])
        (self.analysis_dir / f"{A}.json").write_text(
            json.dumps({**record_a, "paper_index": 1}, ensure_ascii=False), encoding="utf-8"
        )
        (self.lv3_dir / f"{A}.json").write_text(json.dumps({"paper_index": 1}), encoding="utf-8")
        database = DatabaseManager(f"sqlite:///{root / 'papers.db'}")
        database.create_tables()
        database.upsert_papers([_paper(A, "첫 논문"), _paper(B, "둘째 논문 (DB 표기)")])
        database.close()
        self.database_url = f"sqlite:///{root / 'papers.db'}"

    def run(self, **overrides):
        options = dict(
            database_url=self.database_url,
            papers_dir=self.papers_dir,
            manifest_dir=self.manifest_dir,
            analysis_dir=self.analysis_dir,
            structure_dir=self.structure_dir,
            lv3_dir=self.lv3_dir,
            type_decision_dir=self.type_dir,
            config=ClaudeConfig(),
            vocabulary=VOCABULARY,
        )
        options.update(overrides)
        return run_analysis_stage(**options)


class _FakeAnalyzer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list = []

    def analyze(self, paper, paths, vocabulary):
        self.calls.append((paper, paths))
        if self.error is not None:
            raise self.error
        return analysis_record(
            source_id=paper.source_id, title=paper.title, pdf_sha256=sha256_file(paths.pdf)
        )


def test_only_papers_without_a_current_record_are_analyzed(tmp_path: Path) -> None:
    corpus = _Corpus(tmp_path)
    analyzer = _FakeAnalyzer()

    result = corpus.run(analyzer=analyzer)

    assert result.status == "ok"
    assert result.up_to_date_count == 1
    assert [outcome.source_id for outcome in result.outcomes] == [B]
    assert result.outcomes[0].status == "analyzed"
    assert result.outcomes[0].paper_index == 2
    assert result.index_regenerated is True
    # The analyzer receives the index title as identity; the DB title travels as an alternate.
    paper, _paths = analyzer.calls[0]
    assert paper.title == "둘째 논문"
    assert "둘째 논문 (DB 표기)" in paper.alternate_titles
    for path in (
        corpus.analysis_dir / f"{B}.json",
        corpus.lv3_dir / f"{B}.json",
        corpus.type_dir / f"{B}.json",
        corpus.structure_dir / "kor" / f"{B}.md",
        corpus.structure_dir / "eng" / f"{B}.md",
    ):
        assert path.is_file(), path
    assert json.loads((corpus.lv3_dir / f"{B}.json").read_text(encoding="utf-8"))["paper_index"] == 2
    assert B in (corpus.structure_dir / "INDEX.md").read_text(encoding="utf-8")

    # Re-running finds nothing to do.
    again = corpus.run(analyzer=_FakeAnalyzer())
    assert again.status == "ok" and again.up_to_date_count == 2 and again.outcomes == ()


def test_changed_pdf_bytes_trigger_a_reanalysis_that_keeps_the_index(tmp_path: Path) -> None:
    corpus = _Corpus(tmp_path)
    write_pdf(corpus.papers_dir / "첫 논문.pdf", pages=5)  # bytes changed
    targets, current, missing = select_targets(
        {r.source_id: r for r in corpus.records},
        papers_dir=corpus.papers_dir,
        manifest_dir=corpus.manifest_dir,
        analysis_dir=corpus.analysis_dir,
        lv3_dir=corpus.lv3_dir,
    )
    assert current == [] and missing == []
    assert [(t.record.source_id, t.reason, t.existing_paper_index) for t in targets] == [
        (A, "pdf_changed", 1),
        (B, "new", None),
    ]
    assert next_paper_index(lv3_dir=corpus.lv3_dir, analysis_dir=corpus.analysis_dir) == 2

    result = corpus.run(analyzer=_FakeAnalyzer())
    by_id = {outcome.source_id: outcome for outcome in result.outcomes}
    assert by_id[A].paper_index == 1 and by_id[B].paper_index == 2


def test_legacy_evidence_without_an_analysis_record_counts_as_analyzed(tmp_path: Path) -> None:
    corpus = _Corpus(tmp_path)
    # Paper B was documented by the reviewed legacy workflow: a Level-3
    # manifest pinning the PDF hash plus both Structure documents, no record.
    (corpus.lv3_dir / f"{B}.json").write_text(
        json.dumps({"paper_index": 2, "pdf_sha256": corpus.sha[B]}), encoding="utf-8"
    )
    for language in ("kor", "eng"):
        (corpus.structure_dir / language).mkdir(parents=True, exist_ok=True)
        (corpus.structure_dir / language / f"{B}.md").write_text("# legacy", encoding="utf-8")

    targets, current, _missing = select_targets(
        {r.source_id: r for r in corpus.records},
        papers_dir=corpus.papers_dir,
        manifest_dir=corpus.manifest_dir,
        analysis_dir=corpus.analysis_dir,
        lv3_dir=corpus.lv3_dir,
        structure_dir=corpus.structure_dir,
    )
    assert targets == [] and sorted(current) == [A, B]

    # A legacy paper whose PDF bytes changed is re-analyzed with its index kept.
    write_pdf(corpus.papers_dir / "둘째 논문.pdf", pages=4)
    targets, current, _missing = select_targets(
        {r.source_id: r for r in corpus.records},
        papers_dir=corpus.papers_dir,
        manifest_dir=corpus.manifest_dir,
        analysis_dir=corpus.analysis_dir,
        lv3_dir=corpus.lv3_dir,
        structure_dir=corpus.structure_dir,
    )
    assert [(t.record.source_id, t.reason, t.existing_paper_index) for t in targets] == [(B, "pdf_changed", 2)]
    # ``force`` only re-runs papers with an analysis record, never legacy evidence.
    (corpus.structure_dir / "eng" / f"{B}.md").unlink()
    targets, _current, _missing = select_targets(
        {r.source_id: r for r in corpus.records},
        papers_dir=corpus.papers_dir,
        manifest_dir=corpus.manifest_dir,
        analysis_dir=corpus.analysis_dir,
        lv3_dir=corpus.lv3_dir,
        structure_dir=corpus.structure_dir,
        force=True,
    )
    assert [(t.record.source_id, t.reason) for t in targets] == [(A, "rerun"), (B, "new")]


def test_missing_credentials_skip_the_stage_and_report_pending(tmp_path: Path) -> None:
    corpus = _Corpus(tmp_path)
    result = corpus.run(credentials_check=lambda: False)
    assert result.status == "skipped"
    assert result.credentials_available is False
    assert result.pending_source_ids == (B,)
    assert not (corpus.analysis_dir / f"{B}.json").exists()
    assert result.to_dict()["outcomes"][0]["status"] == "not_attempted"


def test_analysis_errors_leave_the_paper_pending(tmp_path: Path) -> None:
    corpus = _Corpus(tmp_path)
    result = corpus.run(analyzer=_FakeAnalyzer(ClaudeAnalysisError("unknown Level-3 path_id")))
    assert result.status == "failed"
    assert result.outcomes[0].status == "failed"
    assert "unknown Level-3" in result.outcomes[0].error
    assert result.pending_source_ids == (B,)
    assert not (corpus.structure_dir / "kor" / f"{B}.md").exists()


def test_authentication_errors_abort_the_remaining_papers(tmp_path: Path) -> None:
    corpus = _Corpus(tmp_path)
    (corpus.analysis_dir / f"{A}.json").unlink()  # both papers now need analysis

    class AuthenticationError(Exception):
        pass

    result = corpus.run(analyzer=_FakeAnalyzer(AuthenticationError("401")))
    assert result.status == "failed"
    assert [outcome.status for outcome in result.outcomes] == ["failed", "not_attempted"]
    assert result.credentials_available is False
    assert set(result.pending_source_ids) == {A, B}


def test_limit_defers_the_rest_as_pending(tmp_path: Path) -> None:
    corpus = _Corpus(tmp_path)
    (corpus.analysis_dir / f"{A}.json").unlink()
    result = corpus.run(analyzer=_FakeAnalyzer(), limit=1)
    assert result.status == "partial"
    assert [o.source_id for o in result.outcomes] == [A]
    assert result.pending_source_ids == (B,)


def test_dry_run_records_are_never_written(tmp_path: Path) -> None:
    corpus = _Corpus(tmp_path)

    class DryAnalyzer:
        def analyze(self, paper, paths, vocabulary):
            return {"dry_run": True, "source_id": paper.source_id}

    result = corpus.run(analyzer=DryAnalyzer(), config=ClaudeConfig(dry_run=True))
    assert result.status == "skipped"
    assert result.outcomes[0].status == "dry_run"
    assert not (corpus.analysis_dir / f"{B}.json").exists()


def test_reserved_paper_indexes_are_kept_by_the_analysis_stage(tmp_path: Path) -> None:
    import json

    from Analyzer.analysis_stage import _existing_paper_index, load_paper_index_reservations, next_paper_index

    manifest_dir = tmp_path / "Manifest"
    lv3_dir, analysis_dir = manifest_dir / "Lv3", manifest_dir / "Analysis"
    lv3_dir.mkdir(parents=True); analysis_dir.mkdir()
    (lv3_dir / "ART000000001.json").write_text(json.dumps({"paper_index": 7}), encoding="utf-8")
    assert next_paper_index(lv3_dir=lv3_dir, analysis_dir=analysis_dir) == 8
    (manifest_dir / "paper_index_reservations.json").write_text(
        json.dumps({"schema_version": 1, "reservations": {"ART000000009": 12, "ART000000010": "x"}}), encoding="utf-8"
    )
    assert load_paper_index_reservations(manifest_dir) == {"ART000000009": 12}
    # the promised number is used when the paper is finally analysed, and never handed to another paper
    assert _existing_paper_index("ART000000009", lv3_dir=lv3_dir, analysis_dir=analysis_dir) == 12
    assert _existing_paper_index("ART000000001", lv3_dir=lv3_dir, analysis_dir=analysis_dir) == 7
    assert _existing_paper_index("ART000000002", lv3_dir=lv3_dir, analysis_dir=analysis_dir) is None
    assert next_paper_index(lv3_dir=lv3_dir, analysis_dir=analysis_dir) == 13


def test_next_number_is_only_a_fallback_and_ignores_removed_papers(tmp_path: Path) -> None:
    """Numbers are kept sorted without gaps by Analyzer/numbering.py; a removed paper's number is free again."""

    import json

    from Analyzer.analysis_stage import next_paper_index

    lv3_dir = tmp_path / "Analyzer" / "Manifest" / "Lv3"
    lv3_dir.mkdir(parents=True)
    (lv3_dir / "ART000000001.json").write_text(json.dumps({"paper_index": 1}), encoding="utf-8")
    (lv3_dir / "ART000000002.json").write_text(json.dumps({"paper_index": 2}), encoding="utf-8")
    exclusions = tmp_path / "Processor" / "refs" / "excluded_papers.json"
    exclusions.parent.mkdir(parents=True)
    exclusions.write_text(json.dumps({"papers": [{"source_id": "ART000000003", "former_record_id": "KNOW-DOMP-0000009", "former_paper_index": 9}]}), encoding="utf-8")

    assert next_paper_index(lv3_dir=lv3_dir, analysis_dir=tmp_path / "Analyzer" / "Manifest" / "Analysis") == 3
