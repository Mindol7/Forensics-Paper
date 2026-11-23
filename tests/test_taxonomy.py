from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from PaperPipeline.taxonomy import (
    DuplicateTaxonomyCodeError,
    EmptyTaxonomyError,
    MalformedTaxonomyError,
    TaxonomyError,
    load_taxonomy,
)


def _write_json(path: Path, value: object) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_loads_only_exact_level_three_and_preserves_source_values(tmp_path: Path) -> None:
    path = tmp_path / "taxonomy.json"
    group = "ABC-01 ·  원래 그룹명  "
    name = "  원래 기술명  "
    raw = _write_json(
        path,
        {
            "_meta": {"ignored": True},
            group: {
                "ABC-01": "Level 2",
                "ABC-01-01": name,
                "ABC-01-01-01": "Level 4",
                "ABC-01-01-01-01": "Level 5",
            },
        },
    )

    taxonomy = load_taxonomy(path)

    assert len(taxonomy) == 1
    assert taxonomy.entries[0].code == "ABC-01-01"
    assert taxonomy.entries[0].name == name
    assert taxonomy.entries[0].group == group
    assert taxonomy.entries[0].level == 3
    assert taxonomy.sha256 == hashlib.sha256(raw).hexdigest()
    assert taxonomy.version_hash == taxonomy.sha256
    assert taxonomy.require("ABC-01-01") == taxonomy.entries[0]
    assert "ABC-01-01\t  원래 기술명  " in taxonomy.to_prompt_block()


def test_real_taxonomy_contains_only_its_74_level_three_codes() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    legacy_file = repository_root / "filters" / "tech_taxonomy.json"
    if not legacy_file.is_file():
        pytest.skip("the legacy filters/tech_taxonomy.json (Gemini classifier vocabulary) was removed")
    taxonomy = load_taxonomy(legacy_file)

    assert len(taxonomy) == 74
    assert all(entry.code.count("-") == 2 for entry in taxonomy)
    assert len(taxonomy.sha256) == 64


def test_rejects_code_duplicated_across_groups(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    _write_json(
        path,
        {
            "Group A": {"ABC-01-01": "First"},
            "Group B": {"ABC-01-01": "Second"},
        },
    )

    with pytest.raises(DuplicateTaxonomyCodeError, match="ABC-01-01"):
        load_taxonomy(path)


def test_rejects_duplicate_json_key_within_a_group(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-key.json"
    path.write_text(
        '{"Group":{"ABC-01-01":"First","ABC-01-01":"Second"}}',
        encoding="utf-8",
    )

    with pytest.raises(DuplicateTaxonomyCodeError, match="ABC-01-01"):
        load_taxonomy(path)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"Group": []},
        {"Group": {"not-a-code": "Name"}},
        {"Group": {"ABC-01-01": ""}},
        {"": {"ABC-01-01": "Name"}},
    ],
)
def test_rejects_malformed_taxonomy(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "malformed.json"
    _write_json(path, payload)

    with pytest.raises(MalformedTaxonomyError):
        load_taxonomy(path)


def test_rejects_invalid_json_and_empty_level_three_set(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(MalformedTaxonomyError, match="invalid taxonomy JSON"):
        load_taxonomy(invalid)

    empty = tmp_path / "empty.json"
    _write_json(empty, {"_meta": {}, "Group": {"ABC-01-01-01": "Level 4 only"}})
    with pytest.raises(EmptyTaxonomyError):
        load_taxonomy(empty)


def test_missing_taxonomy_is_a_domain_error(tmp_path: Path) -> None:
    with pytest.raises(TaxonomyError, match="unable to read taxonomy"):
        load_taxonomy(tmp_path / "missing.json")
