from __future__ import annotations

import json
from pathlib import Path

import pytest

from Analyzer.MD import paper_md
from Analyzer.tag_taxonomy import DEFAULT_TAG_TAXONOMY, TagTaxonomyError, load_tag_taxonomy
from Analyzer import evidence_writer


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _minimal() -> dict:
    return {
        "_meta": {"title": "t"},
        "DEV": {"title": "기기", "child_tags": {"DEV-PC": {"title": "PC", "members": ["PC", "노트북"]}}},
        "FS": {"title": "파일 시스템", "child_tags": {"FS-BLK": {"title": "블록", "members": ["NTFS", "ext4"]}}},
        "OS": {"title": "운영체제", "child_tags": {"OS-WIN": {"title": "Windows", "members": ["Windows"]}}},
        "APP": {"title": "애플리케이션", "child_tags": {"APP-MES": {"title": "메신저"}, "APP-CST": {"title": "클라우드 스토리지"}}},
        "CLOUD": {
            "title": "클라우드",
            "child_tags": {
                "CLOUD-SaaS:CST": {"title": "스토리지", "application_tag": "APP-CST"},
                "CLOUD-IaaS": {"title": "IaaS", "members": ["IaaS"]},
            },
        },
    }


def test_repository_tag_taxonomy_is_valid_json_and_drives_the_pipeline_tables() -> None:
    json.loads(DEFAULT_TAG_TAXONOMY.read_text(encoding="utf-8"))  # a real JSON document, not YAML
    taxonomy = load_tag_taxonomy()

    assert len(taxonomy.allowed_tags) == len(set(taxonomy.allowed_tags)) == 55
    assert taxonomy.allowed_tags[0] == "DEV-PC" and taxonomy.allowed_tags[-1] == "CLOUD-IaaS"
    assert len(taxonomy.application_categories) == 28
    assert taxonomy.application_tags["메신저"] == "APP-MES"
    # The SaaS flavour follows the application category through its APP tag, even
    # where the two labels differ ("클라우드 스토리지" vs "스토리지").
    assert taxonomy.saas_tags["클라우드 스토리지"] == "CLOUD-SaaS:CST"
    assert taxonomy.saas_tags["생성형 AI 및 AI 에이전트"] == "CLOUD-SaaS:GAI"
    assert taxonomy.filesystem_tags["ntfs"] == "FS-BLK" and taxonomy.filesystem_tags["f2fs"] == "FS-FLH"
    assert taxonomy.device_tags["스마트폰"] == "DEV-MOB" and taxonomy.os_tags["HarmonyOS"] == "OS-ETC"

    # The profile generator and the analyzer read the same file.
    assert paper_md.ALLOWED_TAGS == taxonomy.allowed_tags
    assert paper_md.APPLICATION_TAGS == dict(taxonomy.application_tags)
    assert paper_md.SAAS_TAGS == dict(taxonomy.saas_tags)
    assert evidence_writer.APPLICATION_CATEGORIES == taxonomy.application_categories


def test_a_new_member_or_tag_is_a_data_change(tmp_path: Path) -> None:
    payload = _minimal()
    payload["FS"]["child_tags"]["FS-BLK"]["members"].append("XFS")
    payload["APP"]["child_tags"]["APP-GAM"] = {"title": "게임"}
    taxonomy = load_tag_taxonomy(_write(tmp_path / "tags.json", payload))
    assert taxonomy.filesystem_tags["xfs"] == "FS-BLK"
    assert taxonomy.application_tags["게임"] == "APP-GAM"
    assert taxonomy.allowed_tags == ("DEV-PC", "FS-BLK", "OS-WIN", "APP-MES", "APP-CST", "APP-GAM", "CLOUD-SaaS:CST", "CLOUD-IaaS")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["APP"]["child_tags"].update({"APP-mes": {"title": "x"}}), "malformed tag"),
        (lambda p: p["OS"]["child_tags"].update({"APP-WEB": {"title": "x"}}), "malformed tag"),
        (lambda p: p["APP"]["child_tags"].update({"APP-DUP": {"title": "메신저"}}), "duplicate application category"),
        (lambda p: p["CLOUD"]["child_tags"]["CLOUD-SaaS:CST"].update({"application_tag": "APP-NOPE"}), "unknown application tag"),
        (lambda p: p["DEV"]["child_tags"].update({"DEV-MOB": {"title": "Mobile", "members": ["PC"]}}), "belongs to two"),
        (lambda p: p.update({"NET": {"title": "x", "child_tags": {}}}), "unknown tag axes"),
        (lambda p: p.pop("CLOUD"), "needs a non-empty child_tags"),
        (lambda p: p["FS"]["child_tags"]["FS-BLK"].update({"members": "NTFS"}), "members must be a list"),
    ],
)
def test_malformed_tag_taxonomies_are_rejected(tmp_path: Path, mutate, message: str) -> None:
    payload = _minimal()
    mutate(payload)
    with pytest.raises(TagTaxonomyError, match=message):
        load_tag_taxonomy(_write(tmp_path / "tags.json", payload))


def test_the_original_yaml_style_list_is_not_accepted(tmp_path: Path) -> None:
    path = tmp_path / "tags.json"
    path.write_text("tags:\n  - DEV-PC   # PC\n", encoding="utf-8")
    with pytest.raises(TagTaxonomyError, match="unable to read"):
        load_tag_taxonomy(path)
