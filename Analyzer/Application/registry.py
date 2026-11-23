"""Which target applications the system has registered: DF-Insight's Level-5 nodes (Step 2).

A Level-5 node lives in ``web/DF-Insight/data/techniques/<id>.json``
(``COLL-02-04-01-01`` = KakaoTalk under "SaaS: 메신저" under "클라우드 데이터 수집"; DF-Insight wrote
the last segment with 3 digits until 2026-10, ``COLL-02-04-01-001``).
Its upper levels follow from the id: Level 4 = id without the last segment,
Level 3 = first three segments, Level 2 = first two, Level 1 = the four letters.

One product usually has several nodes (cloud collection, application artifacts,
per-OS artifact and memory examination), and a few products are registered twice
under different spellings (``Chrome`` / ``Google Chrome``).  A *product* here is
the set of nodes whose titles name the same product; ``match`` finds the product
for a name written in a paper, ignoring case, width, spacing and quote style, and
also through Korean names:

1. the node titles themselves (``title.ko`` / ``title.en``);
2. DF-Insight's own alias table (``data/keyword-aliases.json`` → ``applications``);
3. the pipeline's reviewed list ``Analyzer/Application/application_aliases.json``
   (Korean names and duplicate spellings that DF-Insight does not cover).

Names that none of these resolve are handed to the LLM step in ``level5.py``.
Substring or fuzzy matching is never used: generic titles exist (Mail, Notes, Photos).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DF_INSIGHT_DIR = REPOSITORY_ROOT / "web" / "DF-Insight"
TECHNIQUE_DIR = DF_INSIGHT_DIR / "data" / "techniques"
DF_ALIASES_PATH = DF_INSIGHT_DIR / "data" / "keyword-aliases.json"
PIPELINE_ALIASES_PATH = REPOSITORY_ROOT / "Analyzer" / "Application" / "application_aliases.json"
LEVEL5_RE = re.compile(r"[A-Z]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2,3}")   # 2-digit last segment since 2026-10
# Level-5 nodes that are not applications (file systems, Windows releases).
NON_APPLICATION_FAMILIES = ("EXAM-03-02-",)
NON_APPLICATION_TITLES = frozenset({"windows 10", "windows 11", "windows server 2025"})


class RegistryError(RuntimeError):
    pass


def name_key(value: str) -> str:
    """Case-, width-, spacing- and quote-insensitive form of a product name."""

    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'", "“": '"', "”": '"'}))
    return " ".join(text.split()).casefold()


def squash_key(value: str) -> str:
    """``name_key`` without spaces and joiners ('Kakao Talk' = 'KakaoTalk'); keeps + and #."""

    return re.sub(r"[\s\-_.'\"·・/()]", "", name_key(value))


@dataclass(frozen=True, slots=True)
class Level5Node:
    id: str
    title_ko: str
    title_en: str
    level_4_id: str
    level_4_ko: str
    level_4_en: str
    level_3_id: str
    level_3_ko: str
    level_3_en: str
    level_3_description_ko: str
    level_3_description_en: str
    level_2_id: str
    level_2_ko: str
    level_2_en: str
    level_1_id: str
    level_1_ko: str
    level_1_en: str
    targets: tuple[str, ...] = ()

    def path_ko(self) -> str:
        return f"{self.level_3_ko}({self.level_3_id}) > {self.level_4_ko}({self.level_4_id}) > {self.title_ko}({self.id})"


@dataclass(frozen=True, slots=True)
class Match:
    product: str            # product key (see Registry.products)
    surface: str            # the name in the paper that matched
    method: str             # title | title_squash | df_alias | pipeline_alias
    via: str = ""           # alias text or title that produced the match


@dataclass(slots=True)
class Registry:
    nodes: dict[str, Level5Node]
    products: dict[str, tuple[str, ...]]          # product key -> node ids (sorted)
    product_titles: dict[str, tuple[str, ...]]    # product key -> distinct English titles
    titles: dict[str, str] = field(default_factory=dict)        # name_key(title) -> product
    squashed: dict[str, str] = field(default_factory=dict)      # squash_key(title) -> product
    aliases: dict[str, tuple[str, str]] = field(default_factory=dict)  # name_key(alias) -> (product, source)
    snapshot: str = ""
    revision: str = ""

    def match(self, names: Iterable[str]) -> Match | None:
        """The registered product named by the first name that resolves (exact rules only)."""

        surfaces = [name for name in dict.fromkeys(str(item).strip() for item in names) if name]
        for surface in surfaces:
            key = name_key(surface)
            if key in self.titles:
                return Match(self.titles[key], surface, "title", surface)
        for surface in surfaces:
            key = squash_key(surface)
            if key and key in self.squashed:
                return Match(self.squashed[key], surface, "title_squash", surface)
        for surface in surfaces:
            hit = self.aliases.get(name_key(surface)) or self.aliases.get(squash_key(surface))
            if hit:
                return Match(hit[0], surface, hit[1], surface)
        return None

    def product_for_title(self, title: str) -> str | None:
        return self.titles.get(name_key(title)) or self.squashed.get(squash_key(title))

    def candidates(self, product: str) -> list[Level5Node]:
        return [self.nodes[node_id] for node_id in self.products.get(product, ())]

    def display_title(self, product: str) -> str:
        titles = self.product_titles.get(product) or (product,)
        return titles[0]

    def is_registered_id(self, identifier: str) -> bool:
        return identifier in self.nodes


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _git_revision(directory: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10, check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def load_registry(
    technique_dir: Path = TECHNIQUE_DIR, df_aliases: Path = DF_ALIASES_PATH, pipeline_aliases: Path = PIPELINE_ALIASES_PATH,
) -> Registry:
    """Every registered application node, grouped by product, with the alias tables."""

    if not technique_dir.is_dir():
        raise RegistryError(f"DF-Insight techniques folder not found: {technique_dir}")
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(technique_dir.glob("*.json")):
        try:
            record = _read_json(path)
        except ValueError as exc:
            raise RegistryError(f"unreadable technique file {path.name}: {exc}") from exc
        if isinstance(record, dict) and record.get("id"):
            records[str(record["id"])] = record

    def upper(identifier: str) -> dict[str, Any]:
        record = records.get(identifier)
        if record is None:
            raise RegistryError(f"missing parent node {identifier}")
        return record

    processes_dir = technique_dir.parent / "processes"
    groups_dir = technique_dir.parent / "techgroups"
    nodes: dict[str, Level5Node] = {}
    for identifier, record in records.items():
        if record.get("level") != 5:
            continue
        if not LEVEL5_RE.fullmatch(identifier):
            raise RegistryError(f"unexpected Level-5 id: {identifier}")
        title = record.get("title") or {}
        title_en = " ".join(str(title.get("en") or "").split())
        title_ko = " ".join(str(title.get("ko") or title_en).split())
        if identifier.startswith(NON_APPLICATION_FAMILIES) or name_key(title_en) in NON_APPLICATION_TITLES:
            continue
        level_4_id = identifier.rsplit("-", 1)[0]
        level_3_id = identifier[:10]
        level_2_id = identifier[:7]
        level_1_id = identifier[:4]
        level_4, level_3 = upper(level_4_id), upper(level_3_id)
        if record.get("parent_id") not in (None, level_4_id):
            raise RegistryError(f"{identifier}: parent_id {record.get('parent_id')} does not match the id")
        group_path, process_path = groups_dir / f"{level_2_id}.json", processes_dir / f"{level_1_id}.json"
        level_2 = _read_json(group_path) if group_path.is_file() else {"title": {"ko": level_2_id, "en": level_2_id}}
        level_1 = _read_json(process_path) if process_path.is_file() else {"title": {"ko": level_1_id, "en": level_1_id}}
        related = record.get("related") if isinstance(record.get("related"), dict) else {}
        nodes[identifier] = Level5Node(
            id=identifier, title_ko=title_ko, title_en=title_en,
            level_4_id=level_4_id, level_4_ko=level_4["title"]["ko"], level_4_en=level_4["title"]["en"],
            level_3_id=level_3_id, level_3_ko=level_3["title"]["ko"], level_3_en=level_3["title"]["en"],
            level_3_description_ko=str((level_3.get("description") or {}).get("ko") or ""),
            level_3_description_en=str((level_3.get("description") or {}).get("en") or ""),
            level_2_id=level_2_id, level_2_ko=level_2["title"]["ko"], level_2_en=level_2["title"]["en"],
            level_1_id=level_1_id, level_1_ko=level_1["title"]["ko"], level_1_en=level_1["title"]["en"],
            targets=tuple(str(item) for item in (related.get("targets") or [])),
        )
    if not nodes:
        raise RegistryError("no Level-5 application node found")

    # Products: titles that are equal after squashing, plus the reviewed duplicate groups.
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        while parent.setdefault(key, key) != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        parent[find(left)] = find(right)

    title_of_node = {node.id: node.title_en for node in nodes.values()}
    for node in nodes.values():
        for title in {node.title_en, node.title_ko}:
            union(squash_key(title), squash_key(node.title_en))
    pipeline = _read_json(pipeline_aliases) if pipeline_aliases.is_file() else {}
    known_squashed = {squash_key(title) for title in title_of_node.values()} | {squash_key(node.title_ko) for node in nodes.values()}
    for group in pipeline.get("product_groups") or []:
        keys = [squash_key(title) for title in group]
        missing = [title for title, key in zip(group, keys) if key not in known_squashed]
        if missing:
            raise RegistryError(f"application_aliases.json product group names unknown titles: {missing}")
        for key in keys[1:]:
            union(keys[0], key)

    members: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        members[find(squash_key(node.title_en))].append(node.id)
    products: dict[str, tuple[str, ...]] = {}
    product_titles: dict[str, tuple[str, ...]] = {}
    root_to_product: dict[str, str] = {}
    for root, ids in members.items():
        ids.sort()
        # Most nodes first, then alphabetical: "Google Chrome" (11 nodes) names the product, not "Chrome" (1).
        counts: dict[str, int] = defaultdict(int)
        for node_id in ids:
            counts[title_of_node[node_id]] += 1
        ordered = tuple(sorted(counts, key=lambda title: (-counts[title], title.casefold())))
        product = ordered[0]
        root_to_product[root] = product
        products[product] = tuple(ids)
        product_titles[product] = ordered

    registry = Registry(nodes=nodes, products=products, product_titles=product_titles)
    for node in nodes.values():
        product = root_to_product[find(squash_key(node.title_en))]
        for title in {node.title_en, node.title_ko}:
            registry.titles.setdefault(name_key(title), product)
            registry.squashed.setdefault(squash_key(title), product)

    def add_alias(alias: str, product: str, source: str) -> None:
        for key in (name_key(alias), squash_key(alias)):
            if key and key not in registry.titles and key not in registry.squashed:
                registry.aliases.setdefault(key, (product, source))

    df_alias_table = (_read_json(df_aliases).get("applications") or {}) if df_aliases.is_file() else {}
    for alias, node_id in df_alias_table.items():
        if node_id in nodes:
            add_alias(alias, root_to_product[find(squash_key(nodes[node_id].title_en))], "df_alias")
    for alias, title in (pipeline.get("aliases") or {}).items():
        product = registry.product_for_title(title)
        if product is None:
            raise RegistryError(f"application_aliases.json alias {alias!r} names an unknown title {title!r}")
        add_alias(alias, product, "pipeline_alias")

    fingerprint = hashlib.sha256()
    for node in sorted(nodes.values(), key=lambda item: item.id):
        fingerprint.update(f"{node.id}\t{node.title_ko}\t{node.title_en}\n".encode("utf-8"))
    fingerprint.update(json.dumps(df_alias_table, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    fingerprint.update(json.dumps(pipeline, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    registry.snapshot = fingerprint.hexdigest()
    registry.revision = _git_revision(DF_INSIGHT_DIR)
    return registry


def registry_titles_listing(registry: Registry) -> str:
    """One line per product (all spellings, where its nodes sit) for the LLM name check."""

    lines = []
    for product in sorted(registry.products, key=str.casefold):
        nodes = registry.candidates(product)
        families = sorted({node.level_3_id for node in nodes})
        categories = sorted({f"{node.level_4_en}" for node in nodes if node.level_3_id in ("EXAM-04-01", "COLL-02-04")})
        oses = sorted({node.level_4_en for node in nodes if node.level_3_id in ("EXAM-03-03", "EXAM-03-04")})
        titles = " / ".join(registry.product_titles[product])
        ko_titles = sorted({node.title_ko for node in nodes} - set(registry.product_titles[product]))
        extra = f" (한국어 제목: {', '.join(ko_titles)})" if ko_titles else ""
        lines.append(
            f"- {titles}{extra} — 범주: {', '.join(categories) or '-'}; OS: {', '.join(oses) or '-'}; Level-3: {', '.join(families)}"
        )
    return "\n".join(lines)


def describe(registry: Registry) -> Mapping[str, Any]:
    return {
        "nodes": len(registry.nodes), "products": len(registry.products), "aliases": len(registry.aliases),
        "snapshot": registry.snapshot, "revision": registry.revision,
    }
