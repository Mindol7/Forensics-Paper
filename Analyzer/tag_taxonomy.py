"""Tag vocabulary of the paper profiles, read from ``Analyzer/Tags/tags_taxonomy.json``.

The JSON file is the single, user-maintained source of the tag axes (DEV, FS,
OS, APP, CLOUD).  Every table the profile generator and the analysis stage
need is derived from it, so adding a tag or a member value is a data change:

* ``allowed_tags``           every tag, in file order (the MD ``tags`` vocabulary)
* ``device_tags`` / ``filesystem_tags`` / ``os_tags`` / ``cloud_tags``
                             recorded value -> tag, from each tag's ``members``
* ``application_tags``       application category label -> ``APP-…`` tag
* ``saas_tags``              application category label -> ``CLOUD-SaaS:…`` tag
* ``application_categories`` the closed category vocabulary of the analyzer

An ``APP`` tag is only ever derived from a named application's category, so a
paper that carries one always records the concrete application name as well.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_TAG_TAXONOMY = Path(__file__).resolve().parent / "Tags" / "tags_taxonomy.json"
AXES = ("DEV", "FS", "OS", "APP", "CLOUD")
SAAS_PREFIX = "CLOUD-SaaS:"
_TAG_RE = re.compile(r"(?:DEV|FS|OS|APP)-[A-Z]{2,4}\Z|CLOUD-(?:SaaS:[A-Z]{3}|[A-Za-z]{4})\Z")


class TagTaxonomyError(ValueError):
    """The tag taxonomy file is missing, malformed, or inconsistent."""


@dataclass(frozen=True, slots=True)
class TagTaxonomy:
    allowed_tags: tuple[str, ...]
    titles: Mapping[str, str]
    axis_titles: Mapping[str, str]
    device_tags: Mapping[str, str]
    filesystem_tags: Mapping[str, str]   # keys are case-folded
    os_tags: Mapping[str, str]
    cloud_tags: Mapping[str, str]
    application_tags: Mapping[str, str]  # category label -> APP tag
    saas_tags: Mapping[str, str]         # category label -> CLOUD-SaaS tag

    @property
    def application_categories(self) -> tuple[str, ...]:
        return tuple(self.application_tags)


def _members(axis: str, tag: str, entry: Mapping[str, Any]) -> list[str]:
    members = entry.get("members", [])
    if not isinstance(members, list) or not all(isinstance(item, str) and item.strip() for item in members):
        raise TagTaxonomyError(f"{tag}: members must be a list of non-empty strings")
    return [" ".join(item.split()) for item in members]


def load_tag_taxonomy(path: str | Path = DEFAULT_TAG_TAXONOMY) -> TagTaxonomy:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TagTaxonomyError(f"unable to read the tag taxonomy {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TagTaxonomyError(f"{source}: the tag taxonomy must be a JSON object")
    unknown_axes = [key for key in payload if not key.startswith("_") and key not in AXES]
    if unknown_axes:
        raise TagTaxonomyError(f"{source}: unknown tag axes {unknown_axes}")

    allowed: list[str] = []
    titles: dict[str, str] = {}
    axis_titles: dict[str, str] = {}
    member_tables: dict[str, dict[str, str]] = {"DEV": {}, "FS": {}, "OS": {}, "CLOUD": {}}
    application_tags: dict[str, str] = {}
    saas_links: dict[str, str] = {}
    for axis in AXES:
        node = payload.get(axis)
        if not isinstance(node, dict) or not isinstance(node.get("child_tags"), dict) or not node["child_tags"]:
            raise TagTaxonomyError(f"{source}: axis {axis} needs a non-empty child_tags object")
        axis_titles[axis] = " ".join(str(node.get("title", "")).split()) or axis
        for tag, entry in node["child_tags"].items():
            if not _TAG_RE.fullmatch(tag) or not tag.startswith(axis + "-"):
                raise TagTaxonomyError(f"{source}: malformed tag {tag!r} under {axis}")
            if tag in titles:
                raise TagTaxonomyError(f"{source}: duplicate tag {tag}")
            if not isinstance(entry, dict) or not str(entry.get("title", "")).strip():
                raise TagTaxonomyError(f"{source}: tag {tag} needs a title")
            title = " ".join(str(entry["title"]).split())
            allowed.append(tag)
            titles[tag] = title
            if axis == "APP":
                if title in application_tags:
                    raise TagTaxonomyError(f"{source}: duplicate application category {title!r}")
                application_tags[title] = tag
            elif tag.startswith(SAAS_PREFIX):
                link = entry.get("application_tag", "APP-" + tag[len(SAAS_PREFIX):])
                saas_links[tag] = link
            if axis in member_tables:
                for member in _members(axis, tag, entry):
                    key = member.casefold() if axis == "FS" else member
                    if member_tables[axis].get(key, tag) != tag:
                        raise TagTaxonomyError(f"{source}: member {member!r} belongs to two {axis} tags")
                    member_tables[axis][key] = tag
    category_by_app_tag = {tag: category for category, tag in application_tags.items()}
    saas_tags: dict[str, str] = {}
    for tag, link in saas_links.items():
        if link not in category_by_app_tag:
            raise TagTaxonomyError(f"{source}: {tag} links to an unknown application tag {link!r}")
        saas_tags[category_by_app_tag[link]] = tag
    return TagTaxonomy(
        allowed_tags=tuple(allowed), titles=titles, axis_titles=axis_titles,
        device_tags=member_tables["DEV"], filesystem_tags=member_tables["FS"], os_tags=member_tables["OS"],
        cloud_tags=member_tables["CLOUD"], application_tags=application_tags, saas_tags=saas_tags,
    )


@lru_cache(maxsize=1)
def default_tag_taxonomy() -> TagTaxonomy:
    return load_tag_taxonomy()


__all__ = ["DEFAULT_TAG_TAXONOMY", "TagTaxonomy", "TagTaxonomyError", "default_tag_taxonomy", "load_tag_taxonomy"]
