"""The technique taxonomy and extraction vocabulary (v5.2) as the pipeline reads them.

``Analyzer/LV/Techniques/tech_taxonomy_v5.2.json`` (Level 2 -> Level 3, English, every
Level-3 entry described by ``definition`` / ``indicators`` / ``insufficient_alone``) and
``Analyzer/LV/Techniques/extraction_vocabulary_v5.2.json`` (the closed tag categories and
the open ``target_application`` category) are the user-maintained sources of truth.

Both files are English only.  Korean titles for the Korean outputs come from
``tech_titles_ko.json`` beside them (DF-Insight ``title.ko``, identical to the Korean
profile template); its ids must cover the taxonomy exactly.

Ids: Level 1 = ``EXAM``, Level 2 = ``EXAM-04``, Level 3 = ``EXAM-04-01``; choosing a
Level-3 id determines the other two.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TECHNIQUES_DIR = REPOSITORY_ROOT / "Analyzer" / "LV" / "Techniques"
TAXONOMY_PATH = TECHNIQUES_DIR / "tech_taxonomy_v5.2.json"
VOCABULARY_PATH = TECHNIQUES_DIR / "extraction_vocabulary_v5.2.json"
KOREAN_TITLES_PATH = TECHNIQUES_DIR / "tech_titles_ko.json"
# Repository-relative names recorded in outputs (levels.json, workbooks, caches).
TAXONOMY_NAME = "Analyzer/LV/Techniques/tech_taxonomy_v5.2.json"
VOCABULARY_NAME = "Analyzer/LV/Techniques/extraction_vocabulary_v5.2.json"

LEVEL_3_RE = re.compile(r"[A-Z]{4}-[0-9]{2}-[0-9]{2}")
CLOSED_CATEGORIES = ("target_device", "target_filesystem", "target_os", "target_app_category", "target_cloud")
OPEN_CATEGORY = "target_application"


class TaxonomyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Level3:
    id: str
    level_1_id: str
    level_1_ko: str
    level_1_en: str
    level_2_id: str
    level_2_ko: str
    level_2_en: str
    level_2_description: str
    title_ko: str
    title_en: str
    definition: str
    indicators: tuple[str, ...]
    insufficient_alone: tuple[str, ...]

    def description_text(self) -> str:
        """One readable paragraph (workbooks, review sheets)."""

        parts = [self.definition]
        if self.indicators:
            parts.append("Indicators: " + "; ".join(self.indicators))
        if self.insufficient_alone:
            parts.append("Insufficient alone: " + "; ".join(self.insufficient_alone))
        return " / ".join(parts)


@dataclass(frozen=True, slots=True)
class Taxonomy:
    techniques: dict[str, Level3]
    sha256: str
    version: str
    shared_group_note: str  # the sentence every Level-2 description ends with (printed once)

    def __iter__(self):
        return iter(self.techniques)

    def __len__(self) -> int:
        return len(self.techniques)

    def __contains__(self, identifier: object) -> bool:
        return identifier in self.techniques

    def __getitem__(self, identifier: str) -> Level3:
        return self.techniques[identifier]


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise TaxonomyError(f"{where}: expected a list of non-empty strings")
    return tuple(_text(item) for item in value)


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=\.)\s+", text.strip()) if part]


def load_taxonomy(path: Path = TAXONOMY_PATH, korean_titles: Path = KOREAN_TITLES_PATH) -> Taxonomy:
    """The v5.2 technique taxonomy with Korean titles, validated."""

    try:
        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes)
        titles = json.loads(korean_titles.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaxonomyError(f"unable to read the technique taxonomy: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaxonomyError("the technique taxonomy must be a JSON object")
    meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
    groups = {key: value for key, value in raw.items() if not key.startswith("_")}
    level_1_titles = titles.get("level_1") or {}
    level_2_titles = titles.get("level_2") or {}
    level_3_titles = titles.get("level_3") or {}

    endings = {(_sentences(_text(group.get("description"))) or [""])[-1] for group in groups.values() if isinstance(group, dict)}
    shared_note = next(iter(endings)) if len(endings) == 1 else ""

    techniques: dict[str, Level3] = {}
    for group_id, group in groups.items():
        if not re.fullmatch(r"[A-Z]{4}-[0-9]{2}", group_id):
            raise TaxonomyError(f"unexpected Level-2 id: {group_id}")
        children = group.get("child_tech") if isinstance(group, dict) else None
        if not isinstance(children, dict) or not children:
            raise TaxonomyError(f"technique group has no child_tech: {group_id}")
        stage = group_id.split("-", 1)[0]
        stage_titles = level_1_titles.get(stage) or {}
        if not stage_titles.get("ko") or not stage_titles.get("en"):
            raise TaxonomyError(f"missing Level-1 title for {stage} in {korean_titles.name}")
        if not level_2_titles.get(group_id):
            raise TaxonomyError(f"missing Korean Level-2 title for {group_id} in {korean_titles.name}")
        description = _text(group.get("description"))
        if shared_note and description.endswith(shared_note):
            description = description[: -len(shared_note)].strip()
        for path_id, child in children.items():
            if not LEVEL_3_RE.fullmatch(path_id) or not path_id.startswith(group_id + "-"):
                raise TaxonomyError(f"unexpected Level-3 id under {group_id}: {path_id}")
            if path_id in techniques:
                raise TaxonomyError(f"duplicate Level-3 id: {path_id}")
            details = child.get("description") if isinstance(child, dict) else None
            if isinstance(details, str):  # tolerate a plain-text description
                details = {"definition": details}
            if not isinstance(details, dict) or not _text(details.get("definition")):
                raise TaxonomyError(f"Level-3 entry without a definition: {path_id}")
            title_en = _text(child.get("title"))
            title_ko = _text(level_3_titles.get(path_id))
            if not title_en or not title_ko:
                raise TaxonomyError(f"technique without a title: {path_id}")
            techniques[path_id] = Level3(
                id=path_id,
                level_1_id=stage, level_1_ko=_text(stage_titles["ko"]), level_1_en=_text(stage_titles["en"]),
                level_2_id=group_id, level_2_ko=_text(level_2_titles[group_id]), level_2_en=_text(group.get("title")),
                level_2_description=description,
                title_ko=title_ko, title_en=title_en,
                definition=_text(details.get("definition")),
                indicators=_strings(details.get("indicators"), f"{path_id}.indicators"),
                insufficient_alone=_strings(details.get("insufficient_alone"), f"{path_id}.insufficient_alone"),
            )
    extra = sorted(set(level_3_titles) - set(techniques)) + sorted(set(level_2_titles) - set(groups))
    if extra:
        raise TaxonomyError(f"{korean_titles.name} names ids that the taxonomy does not have: {', '.join(extra)}")
    for language, values in (("ko", [item.title_ko for item in techniques.values()]), ("en", [item.title_en for item in techniques.values()])):
        if len(set(values)) != len(values):
            raise TaxonomyError(f"duplicate Level-3 title ({language}) in the taxonomy")
    return Taxonomy(
        techniques=techniques,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        version=_text(meta.get("version")) or "?",
        shared_group_note=shared_note,
    )


# --------------------------------------------------------------------------- #
# Extraction vocabulary
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class VocabularyValue:
    id: str
    name: str
    definition: str
    indicators: tuple[str, ...]
    insufficient_alone: tuple[str, ...]
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VocabularyCategory:
    name: str
    open: bool
    output: str
    granularity: str
    values: dict[str, VocabularyValue]


@dataclass(frozen=True, slots=True)
class Vocabulary:
    categories: dict[str, VocabularyCategory]
    sha256: str
    version: str
    promotion_policy: str

    def closed_ids(self, category: str) -> tuple[str, ...]:
        return tuple(self.categories[category].values)

    def all_tag_ids(self) -> tuple[str, ...]:
        return tuple(tag for category in CLOSED_CATEGORIES for tag in self.categories[category].values)


def load_vocabulary(path: Path = VOCABULARY_PATH) -> Vocabulary:
    """The v5.2 extraction vocabulary (5 closed tag categories + open application names)."""

    try:
        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes)
    except (OSError, ValueError) as exc:
        raise TaxonomyError(f"unable to read the extraction vocabulary: {exc}") from exc
    categories_raw = raw.get("categories") if isinstance(raw, dict) else None
    if not isinstance(categories_raw, dict):
        raise TaxonomyError("the extraction vocabulary has no categories")
    missing = [name for name in (*CLOSED_CATEGORIES, OPEN_CATEGORY) if name not in categories_raw]
    if missing:
        raise TaxonomyError(f"the extraction vocabulary lacks categories: {', '.join(missing)}")
    categories: dict[str, VocabularyCategory] = {}
    seen_ids: set[str] = set()
    for name, category in categories_raw.items():
        if not isinstance(category, dict):
            raise TaxonomyError(f"malformed vocabulary category: {name}")
        values: dict[str, VocabularyValue] = {}
        for item in category.get("values") or []:
            identifier = _text(item.get("id")) if isinstance(item, dict) else ""
            if not identifier:
                raise TaxonomyError(f"vocabulary value without an id in {name}")
            details = item.get("description")
            if isinstance(details, str):
                details = {"definition": details}
            if not isinstance(details, dict):
                raise TaxonomyError(f"vocabulary value without a description: {identifier}")
            if identifier in values or (not category.get("open") and identifier in seen_ids):
                raise TaxonomyError(f"duplicate vocabulary id: {identifier}")
            values[identifier] = VocabularyValue(
                id=identifier, name=_text(item.get("name")) or identifier,
                definition=_text(details.get("definition")),
                indicators=_strings(details.get("indicators"), f"{identifier}.indicators"),
                insufficient_alone=_strings(details.get("insufficient_alone"), f"{identifier}.insufficient_alone"),
                aliases=_strings(item.get("aliases") or [], f"{identifier}.aliases"),
            )
            if not category.get("open"):
                seen_ids.add(identifier)
        categories[name] = VocabularyCategory(
            name=name, open=bool(category.get("open")), output=_text(category.get("output")),
            granularity=_text(category.get("granularity")), values=values,
        )
    for name in CLOSED_CATEGORIES:
        if categories[name].open or not categories[name].values:
            raise TaxonomyError(f"closed category {name} must be closed and non-empty")
    if not categories[OPEN_CATEGORY].open:
        raise TaxonomyError(f"{OPEN_CATEGORY} must be the open category")
    return Vocabulary(
        categories=categories,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        version=_text(raw.get("version")) or "?",
        promotion_policy=_text(raw.get("promotion_policy")),
    )


def vocabulary_matches_tag_taxonomy(vocabulary: Vocabulary, allowed_tags: Mapping[str, Any] | tuple[str, ...]) -> bool:
    """True when the vocabulary's closed ids are exactly the profile tag list (same order)."""

    return tuple(vocabulary.all_tag_ids()) == tuple(allowed_tags)
