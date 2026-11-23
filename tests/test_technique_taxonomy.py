from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from Analyzer.tag_taxonomy import default_tag_taxonomy
from Analyzer.technique_taxonomy import (
    CLOSED_CATEGORIES,
    KOREAN_TITLES_PATH,
    TAXONOMY_PATH,
    TaxonomyError,
    load_taxonomy,
    load_vocabulary,
    vocabulary_matches_tag_taxonomy,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_v52_taxonomy_has_the_79_level3_ids_with_criteria_and_korean_titles() -> None:
    taxonomy = load_taxonomy()
    assert len(taxonomy) == 79 and taxonomy.version == "5.2" and len(taxonomy.sha256) == 64
    assert len({item.level_2_id for item in taxonomy.techniques.values()}) == 22
    item = taxonomy["EXAM-04-01"]
    assert (item.level_1_id, item.level_1_ko, item.level_2_id, item.title_ko, item.title_en) == (
        "EXAM", "조사", "EXAM-04", "애플리케이션 아티팩트 조사", "Application Artifact Examination")
    assert item.definition and item.indicators and item.insufficient_alone
    # The sentence every Level-2 description ends with is printed once, not 22 times.
    assert taxonomy.shared_group_note and not any(
        value.level_2_description.endswith(taxonomy.shared_group_note) for value in taxonomy.techniques.values())


def test_korean_titles_equal_the_korean_template_labels() -> None:
    taxonomy = load_taxonomy()
    template = (REPOSITORY_ROOT / "Analyzer" / "MD" / "reference-paper-profile.template.kor.md").read_text(encoding="utf-8")
    labels = dict(re.findall(r"^\s+- ([A-Z]{4}-\d{2}-\d{2})\s+#\s*(.+?)\s*$", template, re.MULTILINE))
    assert {identifier: label.split(" / ")[0] for identifier, label in labels.items()} == {
        identifier: item.title_ko for identifier, item in taxonomy.techniques.items()}
    assert {identifier: label.split(" / ")[1] for identifier, label in labels.items()} == {
        identifier: item.title_en for identifier, item in taxonomy.techniques.items()}


def test_a_korean_title_file_that_misses_an_id_is_refused(tmp_path: Path) -> None:
    titles = json.loads(KOREAN_TITLES_PATH.read_text(encoding="utf-8"))
    del titles["level_3"]["EXAM-04-01"]
    broken = tmp_path / "titles.json"
    broken.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(TaxonomyError, match="EXAM-04-01"):
        load_taxonomy(TAXONOMY_PATH, broken)


def test_vocabulary_tags_are_the_profile_tags_in_the_same_order() -> None:
    vocabulary = load_vocabulary()
    assert vocabulary.version == "5.2"
    assert vocabulary_matches_tag_taxonomy(vocabulary, default_tag_taxonomy().allowed_tags)
    assert [len(vocabulary.categories[name].values) for name in CLOSED_CATEGORIES] == [2, 6, 6, 28, 13]
    application = vocabulary.categories["target_application"]
    assert application.open and {"KakaoTalk", "Telegram", "Chrome"} <= set(application.values)
