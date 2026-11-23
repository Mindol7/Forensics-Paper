from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as app_config
import Processor as processor_package
import Processor.filter as processor_filter
from Core.normalizer import NormalizedPaper
from Processor.filter import (
    KeywordFilterConfig,
    KeywordRule,
    apply_keyword_filter,
    apply_rule_based_filter,
    exclude_matches_text,
    filter_papers_by_keywords,
    keyword_matches_text,
    load_keyword_filter_config,
    load_keyword_filter_configs,
)
from config import Settings


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize(
    ("canonical", "removed_legacy"),
    [
        ("Processor/refs/blacklist.list", "filters/blacklist.txt"),
        ("Processor/refs/keywords_json.ko", "filters/keywords_ko.json"),
        ("Processor/refs/keywords_json.en", "filters/keywords_en.json"),
    ],
)
def test_canonical_reference_files_replace_legacy_duplicates(
    canonical: str,
    removed_legacy: str,
) -> None:
    assert (REPOSITORY_ROOT / canonical).is_file()
    assert not (REPOSITORY_ROOT / removed_legacy).exists()


def test_settings_defaults_point_to_canonical_reference_files() -> None:
    settings = Settings(kci_open_api_key=None)

    assert settings.kci_blacklist_path == Path("Processor/refs/blacklist.list")
    assert settings.kci_keyword_filter_keywords_path == Path(
        "Processor/refs/keywords_json.ko"
    )
    assert settings.kci_keyword_filter_english_keywords_path == Path(
        "Processor/refs/keywords_json.en"
    )
    assert settings.database_url == "sqlite:///papers.db"


def test_settings_loader_uses_refs_and_honors_existing_override_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_names = (
        "KCI_KEYWORD_FILTER_KEYWORDS_PATH",
        "KCI_KEYWORD_FILTER_ENGLISH_KEYWORDS_PATH",
        "KCI_BLACKLIST_PATH",
    )
    for name in env_names:
        monkeypatch.delenv(name, raising=False)
    app_config.load_settings.cache_clear()

    settings = app_config.load_settings(validate=False)
    assert settings.kci_blacklist_path == Path("Processor/refs/blacklist.list")
    assert settings.kci_keyword_filter_keywords_path == Path(
        "Processor/refs/keywords_json.ko"
    )
    assert settings.kci_keyword_filter_english_keywords_path == Path(
        "Processor/refs/keywords_json.en"
    )
    monkeypatch.setenv("KCI_BLACKLIST_PATH", "custom/blacklist.list")
    app_config.load_settings.cache_clear()
    assert app_config.load_settings(validate=False).kci_blacklist_path == Path(
        "custom/blacklist.list"
    )
    app_config.load_settings.cache_clear()


def test_reference_path_prefers_processor_refs_then_legacy_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_REFERENCE_PATH", raising=False)
    monkeypatch.setattr(app_config, "PROJECT_ROOT", tmp_path)
    legacy = tmp_path / "filters" / "keywords_en.json"
    legacy.parent.mkdir()
    legacy.write_text("{}", encoding="utf-8")

    assert app_config._reference_path(
        "TEST_REFERENCE_PATH",
        "Processor/refs/keywords_json.en",
        "refs/keywords_json.en",
        "filters/keywords_en.json",
    ) == Path("filters/keywords_en.json")

    old_refs = tmp_path / "refs" / "keywords_json.en"
    old_refs.parent.mkdir()
    old_refs.write_text("{}", encoding="utf-8")
    assert app_config._reference_path(
        "TEST_REFERENCE_PATH",
        "Processor/refs/keywords_json.en",
        "refs/keywords_json.en",
        "filters/keywords_en.json",
    ) == Path("refs/keywords_json.en")


def test_processor_filter_is_the_canonical_package_api() -> None:
    assert processor_package.__all__ == processor_filter.__all__
    for name in processor_filter.__all__:
        assert getattr(processor_package, name) is getattr(processor_filter, name)
    assert Path(processor_filter.__file__).parent.name == "Processor"

def test_real_reference_taxonomies_load_without_rule_loss() -> None:
    config = load_keyword_filter_configs(
        [
            REPOSITORY_ROOT / "Processor" / "refs" / "keywords_json.ko",
            REPOSITORY_ROOT / "Processor" / "refs" / "keywords_json.en",
        ]
    )

    assert len(config.rules) == 342
    assert len(config.exclude_any) == 70
    assert len(config.include_any) == 322
    assert len(config.direct_keywords) == 69
    assert len(config.categories) == 18
    assert {rule.mode for rule in config.rules} == {
        "direct",
        "anchored",
        "anchored_digital_forensic",
    }


@pytest.mark.parametrize(
    ("keyword", "text", "expected"),
    [
        ("클라우드 포렌식", "클라우드포렌식 증거 분석", True),
        ("app", "apps preserve evidence", True),
        ("app", "appearance evidence", False),
        ("ai", "metallic detail artifact", False),
        ("logical", "psychological data collection", False),
        ("collect", "collected disk artifacts", True),
        ("network forensic", "forensic network evidence", True),
        ("network forensic", "network " + "x" * 40 + " forensic", False),
    ],
)
def test_keyword_matching_regressions(
    keyword: str,
    text: str,
    expected: bool,
) -> None:
    assert keyword_matches_text(keyword.casefold(), text.casefold()) is expected


def test_excludes_use_conservative_whole_word_matching() -> None:
    assert not exclude_matches_text("pcr", "app credential")
    assert exclude_matches_text("pcr", "pcr analysis")
    assert exclude_matches_text("forensic medicine", "a forensic medicine study")


def test_keyword_filter_uses_title_keyword_abstract_priority() -> None:
    config = KeywordFilterConfig(
        rules=(
            KeywordRule("제목", "disk forensic", "direct", ("제목",)),
            KeywordRule("키워드", "memory forensic", "direct", ("키워드",)),
            KeywordRule("초록", "cloud forensic", "direct", ("초록",)),
        ),
        exclude_any=(),
    )
    paper = _paper(
        title="Disk forensic acquisition",
        keywords=["memory forensic"],
        abstract="Cloud forensic workflow",
    )

    result = apply_keyword_filter(paper, config)

    assert result.is_relevant is True
    assert result.categories == ["제목"]
    assert result.matched_keywords == ["disk forensic"]
    assert result.relevance_reasons[0] == "matched_in:title"


def test_anchored_rule_requires_anchor_and_exclude_still_wins() -> None:
    config = KeywordFilterConfig(
        rules=(
            KeywordRule(
                "AI 포렌식",
                "ai",
                "anchored",
                ("AI 포렌식",),
                ("forensic", "forensics"),
            ),
        ),
        exclude_any=("medicine",),
    )

    without_anchor = apply_keyword_filter(_paper(title="AI analysis"), config)
    assert without_anchor.is_relevant is False
    assert without_anchor.matched_keywords == []

    with_anchor = apply_keyword_filter(
        _paper(title="AI analysis", abstract="Digital forensic investigation"),
        config,
    )
    assert with_anchor.is_relevant is True
    assert with_anchor.categories == ["AI 포렌식"]

    excluded = apply_keyword_filter(
        _paper(
            title="AI analysis",
            abstract="Digital forensic medicine investigation",
        ),
        config,
    )
    assert excluded.is_relevant is False
    assert "exclude:medicine" in excluded.relevance_reasons


def test_filter_collection_can_keep_or_drop_irrelevant_rows() -> None:
    config = KeywordFilterConfig(
        rules=(KeywordRule("직접", "dfir", "direct", ("직접",)),),
        exclude_any=(),
    )
    relevant = _paper(source_id="ART1", title="DFIR workflow")
    irrelevant = _paper(source_id="ART2", title="Unrelated research")

    assert [paper.source_id for paper in filter_papers_by_keywords([relevant, irrelevant], config)] == [
        "ART1"
    ]
    kept = filter_papers_by_keywords(
        [_paper(source_id="ART1", title="DFIR workflow"), _paper(source_id="ART2")],
        config,
        keep_irrelevant=True,
    )
    assert [paper.source_id for paper in kept] == ["ART1", "ART2"]


def test_score_based_compatibility_api_still_mutates_normalized_paper() -> None:
    result = apply_rule_based_filter(_paper(title="Digital forensics workflow"))

    assert result.is_relevant is True
    assert result.relevance_score is not None and result.relevance_score >= 3.0
    assert "strong:digital forensics" in result.relevance_reasons


def test_legacy_flat_config_and_invalid_payload_contracts(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {"include_any": [" DFIR ", "dfir"], "exclude_any": ["medicine"]}
        ),
        encoding="utf-8",
    )
    loaded = load_keyword_filter_config(legacy)
    assert loaded.include_any == ("DFIR",)
    assert loaded.rules[0].category == "키워드 매칭"
    assert loaded.exclude_any == ("medicine",)

    missing = load_keyword_filter_config(tmp_path / "missing.json")
    assert missing == KeywordFilterConfig(rules=(), exclude_any=())

    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_keyword_filter_config(invalid)
