#!/usr/bin/env python3
"""Build a canonical institution view over the compiled author graph.

The author graph remains the source of truth for papers, resolved authors,
publication dates, and internal paper-to-paper citations.  This builder adds a
reviewable institution identity layer without changing those citation
semantics: institutions select papers, and the selected papers are rendered as
the same publication timeline and internal citation flow used by the author
view.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Analyzer.Graph.build_author_graph import (  # noqa: E402
    DEFAULT_DATABASE,
    DEFAULT_SOURCE,
    CorpusValidationError,
    compile_graph as compile_author_graph,
    normalize_identity,
)


DEFAULT_OUTPUT = PROJECT_ROOT / "Analyzer" / "Graph" / "out" / "institution-graph.json"
DEFAULT_AUDIT = PROJECT_ROOT / "Analyzer" / "Graph" / "audit" / "institution_graph_audit.json"
DEFAULT_INSTITUTION_OVERRIDES = (
    PROJECT_ROOT / "Analyzer" / "Graph" / "institution_identity_overrides.json"
)

SPACE_RE = re.compile(r"\s+")
INSTITUTION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CanonicalizationRule:
    canonical_name: str
    identity_key: str
    aliases: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class SplitRule:
    raw_name: str
    institutions: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class ExclusionRule:
    raw_name: str
    rationale: str


@dataclass(frozen=True)
class InstitutionCatalog:
    canonical_by_alias_key: Mapping[str, CanonicalizationRule]
    canonical_rules: tuple[CanonicalizationRule, ...]
    splits_by_raw_key: Mapping[str, SplitRule]
    exclusions_by_raw_key: Mapping[str, ExclusionRule]
    source_path: Path | None
    source_sha256: str | None


class DuplicateJsonKeyError(ValueError):
    """Raised before JSON object construction can hide a duplicate key."""


@dataclass(frozen=True)
class ResolvedInstitution:
    source_name: str
    identity_key: str
    canonical_name: str | None
    aliases: tuple[str, ...]
    action: str
    rationale: str

    @property
    def identifier(self) -> str:
        return institution_id_from_key(self.identity_key)


def clean_institution_name(value: str) -> str:
    """Decode compatibility characters and collapse insignificant spacing."""

    return SPACE_RE.sub(" ", unicodedata.normalize("NFKC", html.unescape(value)).strip())


def normalize_institution_identity(value: str) -> str:
    """Return the deliberately conservative exact institution identity key.

    Only Unicode compatibility, case, whitespace, and punctuation differ in
    this automatic layer.  Department stripping, abbreviation expansion,
    translations, renames, and substring matching require a reviewed catalog
    rule.
    """

    cleaned = clean_institution_name(value).casefold()
    return "".join(character for character in cleaned if character.isalnum())


def institution_id_from_key(identity_key: str) -> str:
    if not identity_key:
        raise CorpusValidationError("institution identity key must not be empty")
    digest = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:12]
    return f"INS-{digest}"


def institution_id(value: str) -> str:
    return institution_id_from_key(normalize_institution_identity(value))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not clean_institution_name(value):
        raise CorpusValidationError(f"{location} must be a non-empty string")
    return clean_institution_name(value)


def _require_string_list(value: Any, location: str, *, minimum: int = 1) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CorpusValidationError(f"{location} must be an array")
    cleaned = tuple(_require_nonempty_string(item, f"{location}[]") for item in value)
    if len(cleaned) < minimum:
        raise CorpusValidationError(f"{location} must contain at least {minimum} values")
    if len(set(cleaned)) != len(cleaned):
        raise CorpusValidationError(f"{location} contains duplicate values")
    return cleaned


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def load_institution_catalog(path: Path | None) -> InstitutionCatalog:
    """Load and strictly validate reviewed canonicalization decisions."""

    if path is None:
        return InstitutionCatalog({}, (), {}, {}, None, None)
    path = path.resolve()
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise CorpusValidationError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CorpusValidationError(f"{path}: institution override schema_version must be 1")

    canonicalizations = payload.get("canonicalizations", [])
    splits = payload.get("splits", [])
    exclusions = payload.get("exclusions", [])
    if not isinstance(canonicalizations, list):
        raise CorpusValidationError(f"{path}: canonicalizations must be an array")
    if not isinstance(splits, list):
        raise CorpusValidationError(f"{path}: splits must be an array")
    if not isinstance(exclusions, list):
        raise CorpusValidationError(f"{path}: exclusions must be an array")

    rules: list[CanonicalizationRule] = []
    by_alias_key: dict[str, CanonicalizationRule] = {}
    by_canonical_key: dict[str, CanonicalizationRule] = {}
    for index, item in enumerate(canonicalizations):
        location = f"{path}: canonicalizations[{index}]"
        if not isinstance(item, dict):
            raise CorpusValidationError(f"{location} must be an object")
        canonical_name = _require_nonempty_string(item.get("canonical_name"), f"{location}.canonical_name")
        aliases = _require_string_list(item.get("aliases"), f"{location}.aliases")
        rationale = _require_nonempty_string(item.get("rationale"), f"{location}.rationale")
        identity_key = normalize_institution_identity(canonical_name)
        if not identity_key:
            raise CorpusValidationError(f"{location}.canonical_name has no identity characters")
        all_aliases = tuple(sorted(set((canonical_name, *aliases)), key=lambda value: (normalize_identity(value), value)))
        rule = CanonicalizationRule(canonical_name, identity_key, all_aliases, rationale)
        previous_rule = by_canonical_key.get(identity_key)
        if previous_rule is not None:
            raise CorpusValidationError(
                f"{location}: duplicate canonical institution identity {canonical_name!r}"
            )
        by_canonical_key[identity_key] = rule
        rules.append(rule)
        for alias in all_aliases:
            alias_key = normalize_institution_identity(alias)
            if not alias_key:
                raise CorpusValidationError(f"{location}: alias {alias!r} has no identity characters")
            previous = by_alias_key.get(alias_key)
            if previous is not None and previous.identity_key != identity_key:
                raise CorpusValidationError(
                    f"{location}: alias {alias!r} maps to both "
                    f"{previous.canonical_name!r} and {canonical_name!r}"
                )
            by_alias_key[alias_key] = rule

    splits_by_key: dict[str, SplitRule] = {}
    for index, item in enumerate(splits):
        location = f"{path}: splits[{index}]"
        if not isinstance(item, dict):
            raise CorpusValidationError(f"{location} must be an object")
        raw_name = _require_nonempty_string(item.get("raw_name"), f"{location}.raw_name")
        institutions = _require_string_list(
            item.get("institutions"), f"{location}.institutions", minimum=2
        )
        if len({normalize_institution_identity(value) for value in institutions}) != len(institutions):
            raise CorpusValidationError(f"{location}.institutions collapse to duplicate identities")
        rationale = _require_nonempty_string(item.get("rationale"), f"{location}.rationale")
        raw_key = normalize_institution_identity(raw_name)
        if raw_key in splits_by_key:
            raise CorpusValidationError(f"{location}: duplicate split source {raw_name!r}")
        splits_by_key[raw_key] = SplitRule(raw_name, institutions, rationale)

    exclusions_by_key: dict[str, ExclusionRule] = {}
    for index, item in enumerate(exclusions):
        location = f"{path}: exclusions[{index}]"
        if not isinstance(item, dict):
            raise CorpusValidationError(f"{location} must be an object")
        raw_name = _require_nonempty_string(item.get("raw_name"), f"{location}.raw_name")
        rationale = _require_nonempty_string(item.get("rationale"), f"{location}.rationale")
        raw_key = normalize_institution_identity(raw_name)
        if raw_key in exclusions_by_key:
            raise CorpusValidationError(f"{location}: duplicate exclusion {raw_name!r}")
        exclusions_by_key[raw_key] = ExclusionRule(raw_name, rationale)

    conflicts = (set(splits_by_key) & set(exclusions_by_key)) | (
        set(splits_by_key) & set(by_alias_key)
    ) | (set(exclusions_by_key) & set(by_alias_key))
    if conflicts:
        raise CorpusValidationError(
            f"{path}: the same raw identity has conflicting canonicalize/split/exclude rules: "
            f"{sorted(conflicts)}"
        )
    return InstitutionCatalog(
        canonical_by_alias_key=by_alias_key,
        canonical_rules=tuple(sorted(rules, key=lambda item: item.identity_key)),
        splits_by_raw_key=splits_by_key,
        exclusions_by_raw_key=exclusions_by_key,
        source_path=path,
        source_sha256=_file_sha256(path),
    )


def _resolve_component(value: str, catalog: InstitutionCatalog) -> ResolvedInstitution | None:
    source_name = clean_institution_name(value)
    key = normalize_institution_identity(source_name)
    if not key:
        return None
    exclusion = catalog.exclusions_by_raw_key.get(key)
    if exclusion is not None:
        return None
    rule = catalog.canonical_by_alias_key.get(key)
    if rule is not None:
        return ResolvedInstitution(
            source_name=source_name,
            identity_key=rule.identity_key,
            canonical_name=rule.canonical_name,
            aliases=rule.aliases,
            action="canonicalized",
            rationale=rule.rationale,
        )
    return ResolvedInstitution(
        source_name=source_name,
        identity_key=key,
        canonical_name=None,
        aliases=(source_name,),
        action="exact_normalized",
        rationale=(
            "Automatic identity uses only NFKC/HTML decoding, case folding, whitespace, "
            "and punctuation normalization; no semantic merge was applied."
        ),
    )


def resolve_affiliation(
    value: str,
    catalog: InstitutionCatalog,
) -> tuple[tuple[ResolvedInstitution, ...], dict[str, Any]]:
    """Resolve one source affiliation and return an auditable decision."""

    raw_name = clean_institution_name(value)
    raw_key = normalize_institution_identity(raw_name)
    exclusion = catalog.exclusions_by_raw_key.get(raw_key)
    if exclusion is not None:
        return (), {
            "raw_name": raw_name,
            "action": "excluded",
            "rationale": exclusion.rationale,
            "outputs": [],
        }

    split = catalog.splits_by_raw_key.get(raw_key)
    components = split.institutions if split is not None else (raw_name,)
    resolved: dict[str, ResolvedInstitution] = {}
    component_decisions: list[dict[str, Any]] = []
    for component in components:
        component_key = normalize_institution_identity(component)
        component_exclusion = catalog.exclusions_by_raw_key.get(component_key)
        item = _resolve_component(component, catalog)
        if item is None:
            component_decisions.append({
                "raw_name": clean_institution_name(component),
                "action": "excluded",
                "rationale": (
                    component_exclusion.rationale
                    if component_exclusion is not None
                    else "The value has no institution identity characters."
                ),
            })
            continue
        resolved[item.identity_key] = item
        component_decisions.append({
            "raw_name": item.source_name,
            "action": item.action,
            "rationale": item.rationale,
            "institution_id": item.identifier,
            "canonical_name": item.canonical_name or item.source_name,
        })

    values = tuple(sorted(resolved.values(), key=lambda item: (item.identity_key, item.source_name)))
    return values, {
        "raw_name": raw_name,
        "action": "split" if split is not None else (values[0].action if values else "excluded"),
        "rationale": (
            split.rationale
            if split is not None
            else (values[0].rationale if values else "No usable institution was resolved.")
        ),
        "outputs": component_decisions,
    }


def _build_input_provenance(
    author_build_inputs: Mapping[str, str | None],
    institution_catalog_sha256: str | None,
) -> tuple[dict[str, str | None], str]:
    inputs = dict(author_build_inputs)
    inputs["institution_identity_overrides_sha256"] = institution_catalog_sha256
    digest = hashlib.sha256()
    for key, value in sorted(inputs.items()):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update((value if value is not None else "none").encode("ascii"))
        digest.update(b"\0")
    return inputs, digest.hexdigest()


def _display_name(values: set[str]) -> str:
    if not values:
        raise AssertionError("institution has no display-name candidate")
    return min(
        values,
        key=lambda value: (
            not any("가" <= character <= "힣" for character in value),
            len(value),
            normalize_identity(value),
            value,
        ),
    )


def _portable_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def compile_institution_graph(
    source_dir: Path = DEFAULT_SOURCE,
    *,
    database_path: Path = DEFAULT_DATABASE,
    expected_count: int | None = None,
    citation_overrides_path: Path | None = None,
    author_overrides_path: Path | None = None,
    institution_overrides_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile the institution graph and its complete normalization audit."""

    resolved_source = source_dir.resolve()
    if institution_overrides_path is None and resolved_source == DEFAULT_SOURCE.resolve():
        institution_overrides_path = DEFAULT_INSTITUTION_OVERRIDES
    catalog = load_institution_catalog(
        institution_overrides_path.resolve() if institution_overrides_path is not None else None
    )
    author_index, author_audit, corpus = compile_author_graph(
        resolved_source,
        database_path=database_path,
        expected_count=expected_count,
        overrides_path=citation_overrides_path,
        author_overrides_path=author_overrides_path,
    )

    for paper_id in sorted(corpus["ko"]):
        ko_affiliation = clean_institution_name(corpus["ko"][paper_id].table["affiliations"])
        en_affiliation = clean_institution_name(corpus["en"][paper_id].table["affiliations"])
        if ko_affiliation != en_affiliation:
            raise CorpusValidationError(
                f"{paper_id}: Korean/English affiliation mismatch "
                f"({corpus['ko'][paper_id].table['affiliations']!r} != "
                f"{corpus['en'][paper_id].table['affiliations']!r})"
            )

    build_inputs, build_input_digest = _build_input_provenance(
        author_index["build_inputs"], catalog.source_sha256
    )
    raw_counts: Counter[str] = Counter()
    decision_by_raw: dict[str, dict[str, Any]] = {}
    node_state: dict[str, dict[str, Any]] = {}
    papers: dict[str, dict[str, Any]] = {}
    excluded_record_count = 0
    resolved_source_record_count = 0
    expanded_affiliation_count = 0
    raw_paper_affiliation_links: set[tuple[str, str]] = set()
    raw_affiliated_authorships: set[tuple[str, str]] = set()
    used_resolution_keys: set[str] = set()
    complete_raw_affiliation_paper_count = 0
    partial_raw_affiliation_paper_count = 0
    no_raw_affiliation_paper_count = 0

    for paper_id, source_paper in sorted(author_index["papers"].items()):
        membership: dict[tuple[str, str], dict[str, Any]] = {}
        paper_institution_ids: set[str] = set()
        paper_raw_affiliated_authors: set[str] = set()
        for affiliation in source_paper["affiliations"]:
            raw_name = str(affiliation["institution"]).strip()
            cleaned_raw_name = clean_institution_name(raw_name)
            raw_counts[raw_name] += 1
            raw_paper_affiliation_links.add((paper_id, raw_name))
            raw_affiliated_authorships.add((paper_id, affiliation["author_id"]))
            paper_raw_affiliated_authors.add(affiliation["author_id"])
            resolutions, decision = resolve_affiliation(raw_name, catalog)
            decision["raw_name"] = raw_name
            if cleaned_raw_name != raw_name:
                decision["clean_name"] = cleaned_raw_name
            decision_by_raw[raw_name] = decision
            if not resolutions:
                excluded_record_count += 1
                continue
            resolved_source_record_count += 1
            expanded_affiliation_count += len(resolutions)
            for resolution in resolutions:
                used_resolution_keys.add(normalize_institution_identity(resolution.source_name))
                identifier = resolution.identifier
                paper_institution_ids.add(identifier)
                state = node_state.setdefault(identifier, {
                    "identity_key": resolution.identity_key,
                    "canonical_name": resolution.canonical_name,
                    "aliases": set(resolution.aliases),
                    "name_variants": set(),
                    "source_affiliation_variants": set(),
                    "paper_ids": set(),
                    "author_ids": set(),
                    "authorships": set(),
                    "affiliation_record_count": 0,
                })
                if state["identity_key"] != resolution.identity_key:
                    raise AssertionError("institution ID collision")
                if state["canonical_name"] not in {None, resolution.canonical_name}:
                    raise AssertionError("institution canonical-name disagreement")
                if state["canonical_name"] is None and resolution.canonical_name is not None:
                    state["canonical_name"] = resolution.canonical_name
                state["aliases"].update(resolution.aliases)
                state["name_variants"].add(resolution.source_name)
                state["source_affiliation_variants"].add(raw_name)
                state["paper_ids"].add(paper_id)
                state["author_ids"].add(affiliation["author_id"])
                state["authorships"].add((paper_id, affiliation["author_id"]))
                state["affiliation_record_count"] += 1
                membership_key = (affiliation["author_id"], identifier)
                member = membership.setdefault(membership_key, {
                    "author_id": affiliation["author_id"],
                    "author_name": affiliation["author_name"],
                    "institution_id": identifier,
                    "source_affiliations": set(),
                })
                member["source_affiliations"].add(raw_name)

        institution_affiliations = [
            {
                **{key: value for key, value in item.items() if key != "source_affiliations"},
                "source_affiliations": sorted(item["source_affiliations"], key=lambda value: (normalize_identity(value), value)),
            }
            for _, item in sorted(membership.items(), key=lambda pair: (pair[0][1], pair[0][0]))
        ]
        papers[paper_id] = {
            **source_paper,
            "institution_ids": sorted(paper_institution_ids),
            "institution_affiliations": institution_affiliations,
        }
        paper_authors = set(source_paper["author_ids"])
        if paper_authors and paper_authors <= paper_raw_affiliated_authors:
            complete_raw_affiliation_paper_count += 1
        elif paper_raw_affiliated_authors:
            partial_raw_affiliation_paper_count += 1
        else:
            no_raw_affiliation_paper_count += 1

    institutions: list[dict[str, Any]] = []
    id_to_display: dict[str, str] = {}
    for identifier, state in sorted(node_state.items()):
        display_name = state["canonical_name"] or _display_name(state["name_variants"])
        id_to_display[identifier] = display_name
        paper_ids = sorted(
            state["paper_ids"], key=lambda value: (papers[value]["publication_date"], value)
        )
        author_ids = sorted(state["author_ids"])
        aliases = sorted(
            set((
                display_name,
                *state["aliases"],
                *state["name_variants"],
                *state["source_affiliation_variants"],
            )),
            key=lambda value: (normalize_identity(value), value),
        )
        publication_dates = [papers[paper_id]["publication_date"] for paper_id in paper_ids]
        years = [papers[paper_id]["publication_year"] for paper_id in paper_ids]
        institutions.append({
            "id": identifier,
            "display_name": display_name,
            "normalized_name": state["identity_key"],
            "aliases": aliases,
            "name_variants": sorted(
                state["name_variants"], key=lambda value: (normalize_identity(value), value)
            ),
            "source_affiliation_variants": sorted(
                state["source_affiliation_variants"],
                key=lambda value: (normalize_identity(value), value),
            ),
            "paper_ids": paper_ids,
            "author_ids": author_ids,
            "paper_count": len(paper_ids),
            "author_count": len(author_ids),
            "authorship_count": len(state["authorships"]),
            "affiliation_record_count": state["affiliation_record_count"],
            "first_publication_date": min(publication_dates),
            "last_publication_date": max(publication_dates),
            "year_min": min(years),
            "year_max": max(years),
            "internal_citations_total": sum(
                papers[paper_id]["internal_citation_count"] for paper_id in paper_ids
            ),
        })
    institutions.sort(key=lambda item: (normalize_identity(item["display_name"]), item["id"]))

    for paper in papers.values():
        paper["institution_names"] = [id_to_display[identifier] for identifier in paper["institution_ids"]]
        for affiliation in paper["institution_affiliations"]:
            affiliation["institution_name"] = id_to_display[affiliation["institution_id"]]

    citation_edges = [dict(edge) for edge in author_index["citation_edges"]]
    citation_edges.sort(key=lambda edge: (edge["source"], edge["target"]))
    if citation_edges != author_index["citation_edges"]:
        raise AssertionError("author citation edges are not deterministically sorted")
    if any(edge["source"] not in papers or edge["target"] not in papers for edge in citation_edges):
        raise AssertionError("institution graph citation edge points outside the corpus")

    current_ids = set(node_state)
    id_aliases: dict[str, str] = {}
    ambiguous_id_aliases: dict[str, list[str]] = {}
    for rule in catalog.canonical_rules:
        resolved_id = institution_id_from_key(rule.identity_key)
        if resolved_id not in current_ids:
            continue
        for alias in rule.aliases:
            alias_id = institution_id(alias)
            if alias_id != resolved_id:
                if alias_id in current_ids:
                    raise CorpusValidationError(
                        f"institution alias ID {alias_id} shadows a current institution"
                    )
                id_aliases[alias_id] = resolved_id
    for split in catalog.splits_by_raw_key.values():
        resolved, _ = resolve_affiliation(split.raw_name, catalog)
        destinations = sorted({item.identifier for item in resolved if item.identifier in current_ids})
        legacy_id = institution_id(split.raw_name)
        if len(destinations) == 1:
            id_aliases[legacy_id] = destinations[0]
        elif len(destinations) > 1:
            ambiguous_id_aliases[legacy_id] = destinations
    if set(id_aliases) & set(ambiguous_id_aliases):
        raise AssertionError("institution ID appears in both unique and ambiguous alias maps")

    decisions = []
    for raw_name, record_count in sorted(raw_counts.items(), key=lambda item: (normalize_identity(item[0]), item[0])):
        decision = dict(decision_by_raw[raw_name])
        decision["record_count"] = record_count
        decisions.append(decision)
    decision_value_counts = Counter(item["action"] for item in decisions)
    decision_record_counts = Counter()
    for item in decisions:
        decision_record_counts[item["action"]] += item["record_count"]

    used_raw_keys = {normalize_institution_identity(value) for value in raw_counts}
    unused_catalog = {
        "canonical_aliases": sorted(
            alias
            for rule in catalog.canonical_rules
            for alias in rule.aliases
            if normalize_identity(alias) != normalize_identity(rule.canonical_name)
            and normalize_institution_identity(alias) not in used_resolution_keys
        ),
        "splits": sorted(
            rule.raw_name
            for key, rule in catalog.splits_by_raw_key.items()
            if key not in used_raw_keys
        ),
        "exclusions": sorted(
            rule.raw_name
            for key, rule in catalog.exclusions_by_raw_key.items()
            if key not in used_raw_keys
        ),
    }
    paper_count = len(papers)
    institution_paper_link_count = sum(len(paper["institution_ids"]) for paper in papers.values())
    papers_with_institution_count = sum(bool(paper["institution_ids"]) for paper in papers.values())
    raw_affiliation_record_count = sum(raw_counts.values())
    authorship_count = author_index["corpus"]["authorship_count"]
    raw_paper_affiliation_link_count = len(raw_paper_affiliation_links)
    same_paper_raw_affiliation_duplicate_count = (
        raw_affiliation_record_count - raw_paper_affiliation_link_count
    )
    corpus_summary = {
        "paper_count": paper_count,
        "author_count": author_index["corpus"]["author_count"],
        "authorship_count": authorship_count,
        "institution_count": len(institutions),
        "institution_paper_link_count": institution_paper_link_count,
        "institution_authorship_count": sum(item["authorship_count"] for item in institutions),
        "raw_affiliation_record_count": raw_affiliation_record_count,
        "raw_affiliation_value_count": len(raw_counts),
        "raw_paper_affiliation_link_count": raw_paper_affiliation_link_count,
        "raw_affiliated_authorship_count": len(raw_affiliated_authorships),
        "authorship_without_affiliation_count": (
            authorship_count - len(raw_affiliated_authorships)
        ),
        "papers_with_complete_raw_affiliations": complete_raw_affiliation_paper_count,
        "papers_with_partial_raw_affiliations": partial_raw_affiliation_paper_count,
        "papers_without_raw_affiliations": no_raw_affiliation_paper_count,
        "same_paper_raw_affiliation_duplicate_count": same_paper_raw_affiliation_duplicate_count,
        "canonical_link_reduction_count": (
            raw_paper_affiliation_link_count - institution_paper_link_count
        ),
        "resolved_affiliation_record_count": resolved_source_record_count,
        "expanded_affiliation_record_count": expanded_affiliation_count,
        "excluded_affiliation_record_count": excluded_record_count,
        "papers_with_institution_count": papers_with_institution_count,
        "papers_without_institution_count": paper_count - papers_with_institution_count,
        "paper_institution_coverage": round(papers_with_institution_count / paper_count, 6) if paper_count else 0.0,
        "affiliation_record_coverage": (
            round(resolved_source_record_count / raw_affiliation_record_count, 6)
            if raw_affiliation_record_count
            else 0.0
        ),
        "citation_edge_count": len(citation_edges),
        "citation_scope": author_index["corpus"]["citation_scope"],
        "publication_date_precision": author_index["corpus"]["publication_date_precision"],
        "publication_date_min": author_index["corpus"]["publication_date_min"],
        "publication_date_max": author_index["corpus"]["publication_date_max"],
        "source_last_updated": author_index["corpus"]["source_last_updated"],
        "generated_at": author_index["corpus"]["generated_at"],
        "build_input_digest": build_input_digest,
    }
    index = {
        "schema_version": INSTITUTION_SCHEMA_VERSION,
        "source_digest": author_index["source_digest"],
        "build_inputs": build_inputs,
        "build_input_digest": build_input_digest,
        "institution_id_aliases": dict(sorted(id_aliases.items())),
        "ambiguous_institution_id_aliases": dict(sorted(ambiguous_id_aliases.items())),
        "corpus": corpus_summary,
        "institutions": institutions,
        "papers": papers,
        "citation_edges": citation_edges,
    }
    audit = {
        "schema_version": INSTITUTION_SCHEMA_VERSION,
        "generated_at": author_index["corpus"]["generated_at"],
        "build_inputs": build_inputs,
        "build_input_digest": build_input_digest,
        "corpus": corpus_summary,
        "validation": {
            "author_graph_compile": "passed",
            "bilingual_affiliation_clean_equality": "passed",
            "institution_membership_inverse": "passed",
            "citation_edges_unchanged": "passed",
            "upstream_unmatched_affiliations": author_audit["validation"]["unmatched_affiliations"],
        },
        "policy": {
            "automatic_identity": "NFKC + HTML decoding + casefold + alphanumeric exact key",
            "semantic_merge": "reviewed canonicalizations only",
            "substring_merge": False,
            "department_stripping": "reviewed canonicalizations only",
            "multi_affiliation": "semicolon from source parser or reviewed exact split",
            "unknown_or_noninstitution_values": "excluded; no synthetic institution node",
            "citation_edges": "unchanged internal paper-to-paper edges from author graph",
        },
        "catalog": {
            "path": _portable_path(catalog.source_path),
            "sha256": catalog.source_sha256,
            "canonicalization_count": len(catalog.canonical_rules),
            "split_count": len(catalog.splits_by_raw_key),
            "exclusion_count": len(catalog.exclusions_by_raw_key),
            "unused": unused_catalog,
        },
        "resolution_counts": {
            "raw_value_actions": dict(sorted(decision_value_counts.items())),
            "raw_record_actions": dict(sorted(decision_record_counts.items())),
            "unresolved_value_count": 0,
            "unresolved_record_count": 0,
        },
        "affiliation_decisions": decisions,
    }

    membership_inverse = defaultdict(set)
    for paper_id, paper in papers.items():
        for identifier in paper["institution_ids"]:
            membership_inverse[identifier].add(paper_id)
    if any(set(item["paper_ids"]) != membership_inverse[item["id"]] for item in institutions):
        raise AssertionError("institution/paper membership inverse does not match")
    if corpus_summary["institution_paper_link_count"] != sum(
        len(item["paper_ids"]) for item in institutions
    ):
        raise AssertionError("institution-paper link count does not match nodes")
    if raw_affiliation_record_count != (
        raw_paper_affiliation_link_count + same_paper_raw_affiliation_duplicate_count
    ):
        raise AssertionError("raw affiliation row/link duplicate accounting does not balance")
    if corpus_summary["canonical_link_reduction_count"] != (
        raw_paper_affiliation_link_count - institution_paper_link_count
    ):
        raise AssertionError("canonical institution-link reduction does not balance")
    if authorship_count != (
        len(raw_affiliated_authorships) + corpus_summary["authorship_without_affiliation_count"]
    ):
        raise AssertionError("authorship affiliation coverage does not balance")
    if paper_count != (
        complete_raw_affiliation_paper_count
        + partial_raw_affiliation_paper_count
        + no_raw_affiliation_paper_count
    ):
        raise AssertionError("paper affiliation coverage categories do not balance")
    return index, audit


def _write_json_atomic(path: Path, value: Any) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            os.fchmod(handle.fileno(), 0o644)
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def write_outputs(
    index: Mapping[str, Any],
    audit: Mapping[str, Any],
    *,
    output_path: Path = DEFAULT_OUTPUT,
    audit_path: Path = DEFAULT_AUDIT,
) -> None:
    """Write only institution artifacts; never replace the shared graph dir."""

    _write_json_atomic(output_path, index)
    _write_json_atomic(audit_path, audit)


def build(
    source_dir: Path = DEFAULT_SOURCE,
    output_path: Path = DEFAULT_OUTPUT,
    audit_path: Path = DEFAULT_AUDIT,
    *,
    database_path: Path = DEFAULT_DATABASE,
    expected_count: int | None = None,
    citation_overrides_path: Path | None = None,
    author_overrides_path: Path | None = None,
    institution_overrides_path: Path | None = None,
    check_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    index, audit = compile_institution_graph(
        source_dir,
        database_path=database_path,
        expected_count=expected_count,
        citation_overrides_path=citation_overrides_path,
        author_overrides_path=author_overrides_path,
        institution_overrides_path=institution_overrides_path,
    )
    if not check_only:
        write_outputs(index, audit, output_path=output_path, audit_path=audit_path)
    return index, audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--citation-overrides", type=Path, default=None)
    parser.add_argument("--author-overrides", type=Path, default=None)
    parser.add_argument("--institution-overrides", type=Path, default=None)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--check-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        index, audit = build(
            source_dir=args.source,
            database_path=args.database,
            output_path=args.output,
            audit_path=args.audit_output,
            expected_count=args.expected_count,
            citation_overrides_path=args.citation_overrides,
            author_overrides_path=args.author_overrides,
            institution_overrides_path=args.institution_overrides,
            check_only=args.check_only,
        )
    except (CorpusValidationError, OSError) as exc:
        print(f"institution graph build failed: {exc}", file=sys.stderr)
        return 1
    action = "validated" if args.check_only else "built"
    print(
        f"Institution graph {action}: {index['corpus']['paper_count']} papers, "
        f"{index['corpus']['institution_count']} institutions, "
        f"{index['corpus']['institution_paper_link_count']} paper links, "
        f"{index['corpus']['citation_edge_count']} unchanged internal citation edges, "
        f"{index['corpus']['paper_institution_coverage']:.1%} paper coverage."
    )
    if not args.check_only:
        print(f"Index: {args.output.resolve()}")
        print(f"Audit: {args.audit_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
