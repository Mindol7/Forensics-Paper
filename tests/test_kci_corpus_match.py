from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from Collector import kci_metadata
from Collector.kci_metadata import _match_kci_corpus, run_kci_metadata_pipeline
from Core.normalizer import NormalizedPaper
from Storage.db import DatabaseManager
from config import Settings

BLACKLISTED_PUBLISHER = "테스트블랙연구원"


def _corpus_paper(source_id: str, title: str, year: int = 2024, publisher: str = "정상연구원") -> NormalizedPaper:
    return NormalizedPaper(
        source="kci", source_id=source_id, title=title, title_kor=title, title_eng=None,
        title_other=None, doi=None, uci=None, url=None, abstract=None, abstract_kor=None,
        abstract_eng=None, abstract_other=None, categories=[], matched_keywords=[],
        relevance_reasons=[], is_relevant=False, publication_year=year,
        journal_id="테스트학술지", institution_id=publisher,
    )


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    # Workers normally run in their own processes; threads keep the test in-process.
    monkeypatch.setattr(kci_metadata, "ProcessPoolExecutor", ThreadPoolExecutor)
    base = Settings(kci_open_api_key=None, database_url=f"sqlite:///{tmp_path / 'match.db'}", export_dir=tmp_path)
    blacklist = tmp_path / "blacklist.list"
    blacklist.write_text(base.kci_blacklist_path.read_text(encoding="utf-8"), encoding="utf-8")
    return replace(base, kci_blacklist_path=blacklist)


def _summaries(db: DatabaseManager) -> dict[str, str | None]:
    return {paper.source_id: paper.summary for paper in db.iter_kci_papers()}


def test_additive_match_keeps_selected_papers_and_reports_stale_ones(settings: Settings) -> None:
    db = DatabaseManager(settings.database_url)
    db.create_tables()
    db.upsert_corpus_papers([
        _corpus_paper("ART000000001", "안드로이드 스마트폰 디지털 포렌식 아티팩트 분석"),
        _corpus_paper("ART000000002", "김치 발효 미생물 군집의 계절별 변화"),
        _corpus_paper("ART000000003", "IoT 기기 디지털 포렌식 증거 수집", publisher=BLACKLISTED_PUBLISHER),
        _corpus_paper("ART000000009", "디지털 포렌식 초기 연구", year=2019),
    ])
    first = _match_kci_corpus(settings=settings, db=db, skip_export=True)
    assert first.relevant_count == 2
    assert sorted(db.kci_paper_source_ids()) == ["ART000000001", "ART000000003"]

    # Downstream enrichment that only exists on kci_papers, and a legacy row
    # (accepted by an earlier rule set, outside today's year window).
    enriched = replace(_corpus_paper("ART000000001", "안드로이드 스마트폰 디지털 포렌식 아티팩트 분석"),
                       categories=["디지털 포렌식"], matched_keywords=["디지털 포렌식"],
                       relevance_reasons=["matched_in:title"], is_relevant=True, summary="분석 요약")
    legacy = replace(_corpus_paper("ART000000009", "디지털 포렌식 초기 연구", year=2019),
                     categories=["디지털 포렌식"], matched_keywords=["legacy"],
                     relevance_reasons=["matched_in:title"], is_relevant=True, summary="옛 요약")
    db.upsert_papers([enriched, legacy])

    # The operator tightens the rules and a new paper arrives.
    with settings.kci_blacklist_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n{BLACKLISTED_PUBLISHER}\n")
    db.upsert_corpus_papers([_corpus_paper("ART000000004", "클라우드 디지털 포렌식 증거 수집 절차")])

    additive = _match_kci_corpus(settings=settings, db=db, skip_export=True, additive=True)

    assert additive.new_source_ids == ("ART000000004",)
    assert additive.stale_source_ids == ("ART000000003",)  # blacklisted now, but kept
    assert additive.outside_year_window_source_ids == ("ART000000009",)  # not a rule change
    assert sorted(db.kci_paper_source_ids()) == ["ART000000001", "ART000000003", "ART000000004", "ART000000009"]
    summaries = _summaries(db)
    assert summaries["ART000000001"] == "분석 요약"  # enrichment survives the re-filter
    assert summaries["ART000000009"] == "옛 요약"
    assert summaries["ART000000004"] is None

    # Running it again changes nothing.
    again = _match_kci_corpus(settings=settings, db=db, skip_export=True, additive=True)
    assert again.new_source_ids == () and again.stale_source_ids == ("ART000000003",)
    assert _summaries(db) == summaries

    # The classic full re-match still rebuilds the table from scratch.
    full = _match_kci_corpus(settings=settings, db=db, skip_export=True)
    assert full.stale_source_ids == () and full.outside_year_window_source_ids == ()
    assert sorted(db.kci_paper_source_ids()) == ["ART000000001", "ART000000004"]
    db.close()


def test_incremental_and_additive_modes_are_exclusive(settings: Settings) -> None:
    db = DatabaseManager(settings.database_url)
    db.create_tables()
    db.upsert_corpus_papers([_corpus_paper("ART000000001", "디지털 포렌식")])
    with pytest.raises(ValueError, match="mutually exclusive"):
        _match_kci_corpus(settings=settings, db=db, skip_export=True, incremental=True, additive=True)
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_kci_metadata_pipeline(
            settings=settings, db=db, match_corpus=True, match_incremental=True, match_additive=True,
            skip_export=True,
        )
    db.close()


def test_a_paper_removed_after_review_is_never_selected_again(settings: Settings, tmp_path: Path, monkeypatch) -> None:
    excluded = tmp_path / "excluded_papers.json"
    excluded.write_text('{"schema_version": 1, "papers": [{"source_id": "ART000000001", "reason": "검토 후 제외"}]}', encoding="utf-8")
    monkeypatch.setattr(kci_metadata, "EXCLUDED_PAPERS_PATH", excluded)
    db = DatabaseManager(settings.database_url)
    db.create_tables()
    db.upsert_corpus_papers([
        _corpus_paper("ART000000001", "안드로이드 스마트폰 디지털 포렌식 아티팩트 분석"),
        _corpus_paper("ART000000004", "윈도우 레지스트리 디지털 포렌식 분석"),
    ])

    for additive in (False, True):
        _match_kci_corpus(settings=settings, db=db, skip_export=True, additive=additive)
        assert sorted(db.kci_paper_source_ids()) == ["ART000000004"]

    assert kci_metadata._load_excluded_source_ids(tmp_path / "missing.json") == frozenset()
