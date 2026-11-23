from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import Collector.kci_metadata as kci_metadata
import Processor.filter as processor_filter
from Core.normalizer import NormalizedPaper
from Processor.filter import KeywordFilterConfig, KeywordRule


def _paper(**changes: object) -> NormalizedPaper:
    values: dict[str, object] = {
        "source": "kci",
        "source_id": "ART000000001",
        "title": None,
        "title_kor": None,
        "title_eng": None,
        "title_other": None,
        "doi": None,
        "uci": None,
        "url": None,
        "abstract": None,
        "abstract_kor": None,
        "abstract_eng": None,
        "abstract_other": None,
    }
    values.update(changes)
    return NormalizedPaper(**values)  # type: ignore[arg-type]


def _raw_paper(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "matched_query": "*",
        "matched_field": "sweep",
        "publisher_name": None,
        "journal_name": None,
        "title_original": None,
        "title_english": None,
        "title_foreign": None,
        "keywords": [],
        "abstract_original": None,
        "abstract_english": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_kci_filter_api_is_public_and_metadata_uses_it_by_alias() -> None:
    required = {
        "classify_kci_relevance",
        "filter_blacklisted_kci_papers",
        "filter_kci_sweep_candidates",
        "filter_kci_year_range",
        "load_kci_blacklist",
    }
    assert required <= set(processor_filter.__all__)
    assert "KciRawPaper" not in vars(processor_filter)
    assert (
        kci_metadata._classify_kci_relevance
        is processor_filter.classify_kci_relevance
    )
    assert (
        kci_metadata._filter_sweep_candidates
        is processor_filter.filter_kci_sweep_candidates
    )


def test_blacklist_loads_unique_names_and_matches_canonical_organization(
    tmp_path: Path,
) -> None:
    blacklist = tmp_path / "blacklist.list"
    blacklist.write_text(
        "# comment\n(사)한국도시설계학회\n  (사)한국도시설계학회  \n",
        encoding="utf-8",
    )

    terms = processor_filter.load_kci_blacklist(blacklist)

    assert terms == ("(사)한국도시설계학회",)
    assert processor_filter.matches_kci_blacklist(
        ["사단법인 한국도시설계학회"], terms
    )
    assert not processor_filter.matches_kci_blacklist(["범죄수사연구원"], terms)


def test_sweep_filter_applies_society_override_blacklist_and_keyword_rules() -> None:
    config = KeywordFilterConfig(
        rules=(KeywordRule("직접", "dfir", "direct", ("직접",)),),
        exclude_any=("medicine",),
    )
    society = _raw_paper(
        publisher_name="한국디지털포렌식학회",
        title_original="Unrelated title",
    )
    blacklisted = _raw_paper(
        publisher_name="사단법인 한국도시설계학회",
        title_original="DFIR workflow",
    )
    matched = _raw_paper(title_original="A DFIR workflow")
    excluded = _raw_paper(title_original="DFIR medicine workflow")

    kept = processor_filter.filter_kci_sweep_candidates(
        [society, blacklisted, matched, excluded],  # type: ignore[list-item]
        config,
        ("(사)한국도시설계학회",),
    )

    assert kept == [society, matched]
    assert society.matched_query == processor_filter.KCI_FORENSIC_SOCIETY_NAMES[0]
    assert society.matched_field == "journal"


def test_normalized_kci_year_blacklist_and_society_classification() -> None:
    society = _paper(
        publication_year=2025,
        institution_id="한국디지털포렌식학회",
    )
    blacklisted = _paper(
        source_id="ART2",
        publication_year=2025,
        institution_id="사단법인 한국도시설계학회",
    )
    too_old = _paper(source_id="ART3", publication_year=2019)

    in_range = processor_filter.filter_kci_year_range(
        [society, blacklisted, too_old],
        start_year=2020,
        end_year=2025,
    )
    assert in_range == [society, blacklisted]
    assert processor_filter.filter_blacklisted_kci_papers(
        in_range,
        ("(사)한국도시설계학회",),
    ) == [society]

    breakdown = {"title": 0, "keyword": 0, "abstract": 0, "journal": 0}
    relevant = processor_filter.classify_kci_relevance(
        [society],
        KeywordFilterConfig(rules=(), exclude_any=()),
        breakdown,
    )
    assert relevant == [society]
    assert society.categories == [processor_filter.KCI_SOCIETY_COLLECTION_CATEGORY]
    assert society.matched_keywords == [
        processor_filter.KCI_SOCIETY_COLLECTION_KEYWORD
    ]
    assert breakdown["journal"] == 1
