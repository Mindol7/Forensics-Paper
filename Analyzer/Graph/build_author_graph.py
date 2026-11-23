#!/usr/bin/env python3
"""Build the author graph from bilingual Markdown and canonical KCI dates.

The Markdown corpus below ``Analyzer/Paper/MD`` supplies paper, author, and
reference content.  KCI ``pub-year``/``pub-mon`` values cached in the root
``kci_pipeline.db`` supply strict month-precision publication dates.  The
generated JSON and copied Markdown files are consumed by DF-Insight.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = PROJECT_ROOT / "Analyzer" / "Paper" / "MD"
DEFAULT_DATABASE = PROJECT_ROOT / "kci_pipeline.db"
DEFAULT_OUTPUT = PROJECT_ROOT / "Analyzer" / "Graph" / "out"  # the pipeline's own graphs; DF-Insight builds the ones it shows
DEFAULT_AUDIT = PROJECT_ROOT / "Analyzer" / "Graph" / "audit" / "author_graph_audit.json"
DEFAULT_OVERRIDES = PROJECT_ROOT / "Analyzer" / "Graph" / "citation_overrides.json"
DEFAULT_AUTHOR_OVERRIDES = PROJECT_ROOT / "Analyzer" / "Graph" / "author_identity_overrides.json"

# The corpus is keyed by the KCI article id, which is stable across corpus
# growth. Each bilingual profile lives in ``KNOW-DOMP-0000001/{ko,en}.md``;
# the article id is recovered from its canonical KCI URL in section 1 and the
# profile id travels alongside it. Profile ids must never replace ART graph ids.
IDENTIFIER_RE = re.compile(r"ART\d{9}\Z")
RECORD_ID_RE = re.compile(r"KNOW-DOMP-\d{7}\Z")
LEVEL3_ID_RE = re.compile(r"(?:PREP|IDEC|PRCP|COLL|PSCP|EXAM|ANYS|REPT|CMNF|CVGF)-\d{2}-\d{2}\Z")
KCI_ARTICLE_URL_RE = re.compile(r"(?i)\bartiId=(ART\d{9})\b")
PUBLICATION_DATE_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])\Z")
TABLE_ROW_RE = re.compile(r"\|\s*([^|]*?)\s*\|\s*(.*?)\s*\|\s*\Z")
TABLE_RULE_RE = re.compile(r"\|(?:\s*:?-{3,}:?\s*\|)+\s*\Z")
DOI_RE = re.compile(r"(?i)\b10\.\d{4,9}/[^\s<>\"']+")
BR_RE = re.compile(r"(?i)<br\s*/?>")
SPACE_RE = re.compile(r"\s+")
TITLE_PUNCTUATION_LETTERS = frozenset("ㆍᆞʻʼ")

LANG_CONFIG: dict[str, dict[str, Any]] = {
    "ko": {
        "filename": "ko.md",
        "paper_heading": "## 1. 기본 정보",
        "table_header": ("항목", "내용"),
        "references_heading": "## 5. 참고문헌",
        "reference_table_header": ("ID", "REF"),
        "fields": {
            "doi": "DOI",
            "url": "URL",
            "type": "유형",
            "journal": "논문지",
            "volume_issue": "권호",
            "publisher": "출판 기관",
            "date": "출판 날짜",
            "oa": "OA 여부",
            "authors": "저자",
            "affiliations": "소속 기관",
        },
    },
    "en": {
        "filename": "en.md",
        "paper_heading": "## 1. Basic Information",
        "table_header": ("Item", "Value"),
        "references_heading": "## 5. References",
        "reference_table_header": ("ID", "REF"),
        "fields": {
            "doi": "DOI",
            "url": "URL",
            "type": "Type",
            "journal": "Journal",
            "volume_issue": "Vol. and Issue",
            "publisher": "Publisher",
            "date": "Publication Date",
            "oa": "OA",
            "authors": "Authors",
            "affiliations": "Affiliations",
        },
    },
}

# Profiles written before 2026-10-14 carry '출판 연도' (and, from 2026-09-28,
# '발행일') where DF-Insight's template now has one '출판 날짜 | YYYY. MM.' row.
LEGACY_DATE_LABELS = {"ko": ("출판 연도", "발행일"), "en": ("Publication Year", "Published")}
MARKDOWN_DATE_RE = re.compile(r"(\d{4})\.(?:\s*(?:0[1-9]|1[0-2])\.)?")

# ``kci_link`` is KCI's own recorded link between a citing paper and the cited
# article, carried by the ID column of the Markdown reference table.  It is
# recorded evidence rather than inference, so it outranks DOI matching.
METHOD_PRIORITY = {"kci_link": 0, "doi": 1, "reviewed": 2}
STRATEGY_PRIORITY = {"kci_link": 0, "doi": 1, "reviewed": 2}


class CorpusValidationError(ValueError):
    """Raised when a source Markdown file violates the corpus contract."""


@dataclass(frozen=True)
class Reference:
    number: int
    text: str
    # Record id named by the ID column, or None when the cited work is outside
    # this corpus.  Resolved to an article id during citation compilation.
    target_record_id: str | None = None


@dataclass(frozen=True)
class ParsedPaper:
    language: str
    path: Path
    identifier: str
    record_id: str
    subject: str
    # Level-3 technique identifiers of the classification front matter.
    classification: tuple[str, ...]
    tags: tuple[str, ...]
    last_updated: str
    table: Mapping[str, str]
    references: tuple[Reference, ...]


@dataclass(frozen=True)
class PublicationDate:
    """Exact month-precision publication date supplied by KCI metadata."""

    source_id: str
    value: str
    year: int
    month: int


@dataclass(frozen=True)
class ResolvedReference:
    source: str
    target: str
    method: str
    strategy: str
    reference_number: int


@dataclass(frozen=True)
class AuthorOccurrence:
    occurrence_id: str
    paper_id: str
    position: int
    year: int
    name: str
    normalized_name: str
    affiliations: tuple[str, ...]
    coauthor_names: tuple[str, ...]
    title: str
    journal: str | None
    tags: tuple[str, ...]
    source_md_sha256: str


def _error(path: Path, message: str) -> CorpusValidationError:
    return CorpusValidationError(f"{path}: {message}")


def normalize_identity(value: str) -> str:
    """Normalize only compatibility, whitespace and case for exact identity."""

    return SPACE_RE.sub(" ", unicodedata.normalize("NFKC", value).strip()).casefold()


def normalize_title(value: str) -> str:
    """Normalize punctuation-separated title tokens for conservative matching."""

    decoded = html.unescape(unicodedata.normalize("NFKC", value)).casefold()
    # Compacting whitespace is intentional: exported KCI bibliography text
    # occasionally inserts/removes spaces inside English words.  The complete
    # normalized title must still occur contiguously and uniquely.
    return "".join(
        character
        for character in decoded
        if character not in TITLE_PUNCTUATION_LETTERS and (character.isalpha() or character.isdigit())
    )


def normalize_doi(value: str | None) -> str | None:
    if value is None:
        return None
    decoded = unquote(html.unescape(value)).strip()
    if decoded.casefold() in {"", "n/a", "na", "none", "null"}:
        return None
    decoded = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", decoded)
    decoded = re.sub(r"(?i)^doi\s*:\s*", "", decoded)
    match = DOI_RE.search(decoded)
    if not match:
        return decoded.rstrip(".,;:").casefold()
    doi = match.group(0).rstrip(".,;:")
    # Reference prose often closes a sentence or Markdown link after the DOI.
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1]
    return doi.rstrip("]}").casefold()


def _plain_or_none(value: str) -> str | None:
    cleaned = html.unescape(value).strip()
    return None if cleaned.casefold() in {"", "n/a", "na", "none", "null"} else cleaned


def _is_unknown_affiliation(value: str | None) -> bool:
    if value is None:
        return True
    normalized = normalize_identity(html.unescape(value))
    compact = "".join(character for character in normalized if character.isalnum())
    return compact in {
        "",
        "na",
        "none",
        "null",
        "unknown",
        "없음",
        "미상",
        "소속미상",
        "소속없음",
    }


def _parse_frontmatter(path: Path, text: str) -> tuple[dict[str, Any], int]:
    lines = text.splitlines()
    if not lines or lines[0].lstrip("\ufeff") != "---":
        raise _error(path, "missing opening YAML frontmatter delimiter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise _error(path, "missing closing YAML frontmatter delimiter") from exc

    values: dict[str, Any] = {}
    current_list: str | None = None
    for line_number, raw in enumerate(lines[1:end], start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # Sequence entries may carry a trailing ``# label`` comment describing
        # the code; the value is the entry, never the label.
        list_match = re.fullmatch(r"\s+-\s+([^#\s]\S*)\s*(?:#[^#]*)?", raw)
        if list_match and current_list:
            values[current_list].append(_parse_yaml_scalar(list_match.group(1), path, line_number))
            continue
        field_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):(?:\s*(.*))?", raw)
        if not field_match:
            raise _error(path, f"unsupported frontmatter syntax at line {line_number}: {raw!r}")
        key, raw_value = field_match.group(1), (field_match.group(2) or "").strip()
        if key in values:
            raise _error(path, f"duplicate frontmatter field {key!r}")
        if raw_value == "":
            values[key] = []
            current_list = key
        elif raw_value == "[]":
            values[key] = []
            current_list = None
        else:
            values[key] = _parse_yaml_scalar(raw_value, path, line_number)
            current_list = None

    required = {"subject", "identifier", "tags", "last_updated", "author"}
    missing = sorted(required - values.keys())
    if missing:
        raise _error(path, f"missing frontmatter fields: {', '.join(missing)}")
    if not isinstance(values["tags"], list) or any(not isinstance(tag, str) for tag in values["tags"]):
        raise _error(path, "frontmatter tags must be a string list")
    if not isinstance(values.get("classification"), list) or any(
        not isinstance(value, str) for value in values["classification"]
    ):
        raise _error(path, "frontmatter classification must be a string list")
    for key in ("subject", "identifier", "last_updated", "author"):
        if not isinstance(values[key], str):
            raise _error(path, f"frontmatter {key!r} must be a string")
    return values, end + 1


def _parse_yaml_scalar(raw: str, path: Path, line_number: int) -> str:
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _error(path, f"invalid quoted scalar at line {line_number}") from exc
        if not isinstance(value, str):
            raise _error(path, f"non-string scalar at line {line_number}")
        return value
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def _parse_information_table(path: Path, lines: Sequence[str], language: str) -> dict[str, str]:
    config = LANG_CONFIG[language]
    heading = config["paper_heading"]
    positions = [index for index, line in enumerate(lines) if line.strip() == heading]
    if len(positions) != 1:
        raise _error(path, f"expected one {heading!r} heading, found {len(positions)}")
    index = positions[0] + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index + 1 >= len(lines):
        raise _error(path, "paper-information table is incomplete")

    header = _parse_table_row(path, lines[index], index + 1)
    if tuple(header) != tuple(config["table_header"]):
        raise _error(path, f"unexpected paper-information table header: {header!r}")
    separator = lines[index + 1].strip()
    if not re.fullmatch(r"\|\s*:?-{3,}:?\s*\|\s*:?-{3,}:?\s*\|", separator):
        raise _error(path, f"invalid table separator at line {index + 2}")

    index += 2
    rows: dict[str, str] = {}
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.startswith("## "):
            break
        key, value = _parse_table_row(path, line, index + 1)
        if key in rows:
            raise _error(path, f"duplicate table field {key!r}")
        rows[key] = value
        index += 1

    expected_labels = set(config["fields"].values())
    date_label = config["fields"]["date"]
    year_label, month_label = LEGACY_DATE_LABELS[language]
    legacy_labels = (expected_labels - {date_label}) | {year_label, month_label}
    if set(rows) == expected_labels:
        match = MARKDOWN_DATE_RE.fullmatch(rows[date_label].strip())
        if not match:
            raise _error(path, f"invalid publication date: {rows[date_label]!r}")
        year = match.group(1)
    elif legacy_labels - {month_label} <= set(rows) <= legacy_labels:
        year = rows[year_label].strip()
    else:
        missing = sorted(expected_labels - rows.keys())
        extra = sorted(rows.keys() - expected_labels)
        raise _error(path, f"paper-information fields differ (missing={missing}, extra={extra})")
    table = {field: rows.get(label, "").strip() for field, label in config["fields"].items()}
    table["year"] = year
    return table


def _parse_table_row(path: Path, raw: str, line_number: int) -> tuple[str, str]:
    match = re.fullmatch(r"\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*", raw)
    if not match:
        raise _error(path, f"invalid two-column Markdown row at line {line_number}: {raw!r}")
    return match.group(1), match.group(2)


def _parse_references(path: Path, lines: Sequence[str], language: str) -> tuple[Reference, ...]:
    """Parse the two-column reference table into ordered references.

    The table has no printed numbering, so the reference number is the row's
    1-based position; the generator emits rows in ``reference_order``. A single
    ``N/A`` title with an empty ID means the paper has no collected references.
    """

    config = LANG_CONFIG[language]
    heading = config["references_heading"]
    positions = [index for index, line in enumerate(lines) if line.strip() == heading]
    if len(positions) != 1:
        raise _error(path, f"expected one {heading!r} heading, found {len(positions)}")

    rows: list[tuple[str, str]] = []
    # The table runs to the next section heading, or to the end of the document
    # when references are the last section.
    for index in range(positions[0] + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("## "):
            break
        if not stripped or (stripped.startswith("<!--") and stripped.endswith("-->")):
            continue
        if TABLE_RULE_RE.fullmatch(stripped):
            continue
        match = TABLE_ROW_RE.fullmatch(stripped)
        if not match:
            raise _error(path, f"invalid reference row at line {index + 1}: {lines[index]!r}")
        rows.append((match.group(1).strip(), match.group(2).strip()))

    if not rows:
        raise _error(path, "reference section has no table")
    if tuple(value.casefold() for value in rows[0]) != tuple(
        value.casefold() for value in config["reference_table_header"]
    ):
        raise _error(path, f"unexpected reference table header: {rows[0]!r}")
    body = rows[1:]
    if not body:
        raise _error(path, "reference table has no rows")
    if len(body) == 1 and not body[0][0] and body[0][1].casefold() == "n/a":
        return ()

    references: list[Reference] = []
    for number, (record_id, title) in enumerate(body, start=1):
        if not title:
            raise _error(path, f"reference {number} has an empty title cell")
        if title.casefold() == "n/a":
            raise _error(path, "reference table mixes N/A with real references")
        if record_id and not RECORD_ID_RE.fullmatch(record_id):
            raise _error(path, f"reference {number} has an invalid record id: {record_id!r}")
        references.append(
            Reference(number=number, text=title, target_record_id=record_id or None)
        )
    return tuple(references)


def parse_markdown(path: Path, language: str) -> ParsedPaper:
    if language not in LANG_CONFIG:
        raise ValueError(f"unsupported language: {language}")
    if path.name != LANG_CONFIG[language]["filename"] or not RECORD_ID_RE.fullmatch(path.parent.name):
        raise _error(path, "path must be KNOW-DOMP followed by seven digits and ko.md/en.md")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    frontmatter, body_start = _parse_frontmatter(path, text)
    record_id = frontmatter["identifier"].strip()
    if record_id != path.parent.name or not RECORD_ID_RE.fullmatch(record_id):
        raise _error(path, f"directory/frontmatter identifier mismatch: {record_id!r}")

    headings = [line[2:].strip() for line in lines[body_start:] if line.startswith("# ")]
    if len(headings) != 1:
        raise _error(path, f"expected exactly one H1 title, found {len(headings)}")
    subject = frontmatter["subject"].strip()
    if unicodedata.normalize("NFKC", html.unescape(headings[0])) != unicodedata.normalize(
        "NFKC", html.unescape(subject)
    ):
        raise _error(path, "frontmatter subject and H1 title differ")

    table = _parse_information_table(path, lines, language)
    url_match = KCI_ARTICLE_URL_RE.search(html.unescape(table["url"]))
    if not url_match:
        raise _error(path, f"URL does not carry a KCI article id: {table['url']!r}")
    identifier = url_match.group(1).upper()
    year_text = table["year"].strip()
    if not re.fullmatch(r"\d{4}", year_text):
        raise _error(path, f"invalid publication year: {year_text!r}")
    year = int(year_text)
    if not 1800 <= year <= 2200:
        raise _error(path, f"publication year outside accepted bounds: {year}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", frontmatter["last_updated"]):
        raise _error(path, f"invalid last_updated date: {frontmatter['last_updated']!r}")

    tags = tuple(frontmatter["tags"])
    if len(tags) != len(set(tags)):
        raise _error(path, "frontmatter tags contain duplicates")
    classification = tuple(frontmatter.get("classification", ()))
    if len(classification) != len(set(classification)):
        raise _error(path, "frontmatter classification contains duplicates")
    for value in classification:
        if not LEVEL3_ID_RE.fullmatch(value):
            raise _error(path, f"invalid classification id: {value!r}")
    references = _parse_references(path, lines, language)
    return ParsedPaper(
        language=language,
        path=path,
        identifier=identifier,
        record_id=record_id,
        subject=subject,
        classification=classification,
        tags=tags,
        last_updated=frontmatter["last_updated"],
        table=table,
        references=references,
    )


def _load_language(source_dir: Path, language: str) -> dict[str, ParsedPaper]:
    if not source_dir.is_dir():
        raise CorpusValidationError(f"missing source directory: {source_dir}")
    unexpected = sorted(
        path.name for path in source_dir.iterdir()
        if path.is_dir() and path.name.startswith("KNOW-") and not RECORD_ID_RE.fullmatch(path.name)
    )
    if unexpected:
        raise CorpusValidationError(f"unexpected profile directories in {source_dir}: {unexpected[:10]}")
    papers: dict[str, ParsedPaper] = {}
    record_ids: dict[str, str] = {}
    for directory in sorted(source_dir.glob("KNOW-DOMP-*")):
        if not directory.is_dir():
            raise CorpusValidationError(f"profile path is not a directory: {directory}")
        path = directory / LANG_CONFIG[language]["filename"]
        if not path.is_file():
            raise CorpusValidationError(f"missing {language} source Markdown: {path}")
        paper = parse_markdown(path, language)
        if paper.identifier in papers:
            raise CorpusValidationError(f"duplicate {language} identifier: {paper.identifier}")
        if paper.record_id in record_ids:
            raise CorpusValidationError(f"duplicate {language} record id: {paper.record_id}")
        papers[paper.identifier] = paper
        record_ids[paper.record_id] = paper.identifier
    return papers


def load_corpus(
    source_dir: Path = DEFAULT_SOURCE,
    *,
    expected_count: int | None = None,
) -> dict[str, dict[str, ParsedPaper]]:
    source_dir = source_dir.resolve()
    by_language = {language: _load_language(source_dir, language) for language in ("ko", "en")}
    ko_ids, en_ids = set(by_language["ko"]), set(by_language["en"])
    if ko_ids != en_ids:
        raise CorpusValidationError(
            "Korean/English identifier sets differ "
            f"(ko_only={sorted(ko_ids - en_ids)}, en_only={sorted(en_ids - ko_ids)})"
        )
    if expected_count is not None and len(ko_ids) != expected_count:
        raise CorpusValidationError(f"expected {expected_count} bilingual papers, found {len(ko_ids)}")

    for identifier in sorted(ko_ids):
        ko, en = by_language["ko"][identifier], by_language["en"][identifier]
        pairs = (
            ("year", ko.table["year"], en.table["year"]),
            ("DOI", normalize_doi(ko.table["doi"]), normalize_doi(en.table["doi"])),
            ("URL", _plain_or_none(ko.table["url"]), _plain_or_none(en.table["url"])),
            ("OA", _normalize_oa(ko.table["oa"], ko.path), _normalize_oa(en.table["oa"], en.path)),
            ("authors", _author_keys(ko.table["authors"]), _author_keys(en.table["authors"])),
            ("tags", ko.tags, en.tags),
            ("classification", ko.classification, en.classification),
            ("record id", ko.record_id, en.record_id),
            ("reference count", len(ko.references), len(en.references)),
            (
                "reference record ids",
                tuple(reference.target_record_id for reference in ko.references),
                tuple(reference.target_record_id for reference in en.references),
            ),
        )
        for field, left, right in pairs:
            if left != right:
                raise CorpusValidationError(
                    f"{identifier}: Korean/English {field} mismatch ({left!r} != {right!r})"
                )
    return by_language


def _split_authors(raw: str) -> list[str]:
    value = _plain_or_none(raw)
    if value is None:
        return []
    # Semicolons remain accepted for previously collected bibliographic values;
    # the current template writes author lists with commas.
    authors = [SPACE_RE.sub(" ", html.unescape(part).strip()) for part in re.split(r"[;,]", value)]
    return [author for author in authors if author]


def _author_keys(raw: str) -> tuple[str, ...]:
    return tuple(normalize_identity(author) for author in _split_authors(raw))


def author_id(name: str) -> str:
    digest = hashlib.sha256(normalize_identity(name).encode("utf-8")).hexdigest()[:12]
    return f"AUT-{digest}"


def _normalize_oa(raw: str, path: Path) -> str:
    value = SPACE_RE.sub(" ", html.unescape(raw).strip()).casefold()
    mapping = {
        "open access": "open",
        "not open access": "closed",
        "o": "open",
        "x": "closed",
        "n/a": "unknown",
        "na": "unknown",
        "unknown": "unknown",
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise _error(path, f"unsupported OA value: {raw!r}") from exc


def _parse_affiliations(
    raw: str,
    author_key_to_id: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    value = _plain_or_none(raw)
    if value is None:
        return [], []
    records: list[dict[str, Any]] = []
    unmatched: list[dict[str, str]] = []
    for part in BR_RE.split(value):
        part = html.unescape(part).strip()
        if not part:
            continue
        if _is_unknown_affiliation(part):
            continue
        if ":" not in part:
            unmatched.append({"raw": part, "reason": "missing_author_separator"})
            records.append({"author_id": None, "author_name": None, "institution": part})
            continue
        name, institutions = (piece.strip() for piece in part.split(":", 1))
        key = normalize_identity(name)
        matched_id = author_key_to_id.get(key)
        institution_values = [SPACE_RE.sub(" ", item.strip()) for item in institutions.split(";") if item.strip()]
        if not institution_values:
            institution_values = [""]
        if matched_id is None:
            unmatched.append({"raw": part, "reason": "author_not_in_paper"})
        for institution in institution_values:
            institution_value = institution or None
            if _is_unknown_affiliation(institution_value):
                continue
            records.append(
                {
                    "author_id": matched_id,
                    "author_name": name,
                    "institution": institution_value,
                }
            )
    return records, unmatched


def _extract_dois(text: str) -> list[str]:
    values = {normalize_doi(match.group(0)) for match in DOI_RE.finditer(html.unescape(text))}
    return sorted(value for value in values if value)


def _reference_first_author(reference_text: str) -> str | None:
    """First author of a reference, used only as a reviewed-override guard.

    ``format_reference`` in the Markdown generator writes the author block
    first, so the leading ``. ``-delimited segment is the author list. A
    reference without a recorded author starts with its title instead, which is
    why this is a guard rather than a resolution signal.
    """

    value = html.unescape(reference_text).strip()
    if not value:
        return None
    first = re.split(r"\s*;\s*|\.\s+", value, maxsplit=1)[0].strip()
    return normalize_identity(first) if first else None


def _contains_year(reference_text: str, year: int) -> bool:
    return re.search(rf"(?<!\d){year}(?!\d)", reference_text) is not None


def _load_reviewed_overrides(
    path: Path | None,
    ko_papers: Mapping[str, ParsedPaper],
) -> dict[tuple[str, int], dict[str, Any]]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusValidationError(f"cannot read citation overrides {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CorpusValidationError(f"{path}: unsupported citation override schema")
    rows = payload.get("overrides")
    if not isinstance(rows, list):
        raise CorpusValidationError(f"{path}: overrides must be a list")

    overrides: dict[tuple[str, int], dict[str, Any]] = {}
    required = {
        "source",
        "reference_number",
        "target",
        "reference_text_sha256",
        "target_year",
        "target_title",
        "evidence",
    }
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not required.issubset(row):
            raise CorpusValidationError(f"{path}: override {position} lacks required fields")
        source, target, number = row["source"], row["target"], row["reference_number"]
        if source not in ko_papers or target not in ko_papers:
            raise CorpusValidationError(f"{path}: override {position} points outside the corpus")
        if source == target:
            raise CorpusValidationError(f"{path}: override {position} is a self-loop")
        if not isinstance(number, int) or number <= 0:
            raise CorpusValidationError(f"{path}: override {position} has an invalid reference number")
        key = (source, number)
        if key in overrides:
            raise CorpusValidationError(f"{path}: duplicate override endpoint {source}#{number}")
        references = [reference for reference in ko_papers[source].references if reference.number == number]
        if len(references) != 1:
            raise CorpusValidationError(f"{path}: override endpoint {source}#{number} is not unique")
        reference = references[0]
        actual_hash = hashlib.sha256(reference.text.encode("utf-8")).hexdigest()
        if row["reference_text_sha256"] != actual_hash:
            raise CorpusValidationError(f"{path}: override {source}#{number} reference hash is stale")
        target_paper = ko_papers[target]
        if row["target_year"] != int(target_paper.table["year"]):
            raise CorpusValidationError(f"{path}: override {source}#{number} target year is stale")
        if row["target_title"] != target_paper.subject:
            raise CorpusValidationError(f"{path}: override {source}#{number} target title is stale")
        if not _contains_year(reference.text, row["target_year"]):
            raise CorpusValidationError(f"{path}: override {source}#{number} reference lacks target year")
        first_author = _reference_first_author(reference.text)
        target_authors = _author_keys(target_paper.table["authors"])
        if not target_authors or first_author != target_authors[0]:
            raise CorpusValidationError(f"{path}: override {source}#{number} first-author guard failed")
        if not isinstance(row["evidence"], str) or not row["evidence"].strip():
            raise CorpusValidationError(f"{path}: override {source}#{number} lacks review evidence")
        overrides[key] = dict(row)
    return overrides


def _resolve_citations(
    corpus: Mapping[str, Mapping[str, ParsedPaper]],
    reviewed_overrides: Mapping[tuple[str, int], Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], Counter[str]]:
    ko_papers = corpus["ko"]
    doi_index: dict[str, set[str]] = defaultdict(set)
    by_record_id: dict[str, str] = {}
    for identifier in sorted(ko_papers):
        by_record_id[ko_papers[identifier].record_id] = identifier
        doi = normalize_doi(ko_papers[identifier].table["doi"])
        if doi:
            doi_index[doi].add(identifier)

    resolved_mentions: list[ResolvedReference] = []
    unresolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    self_loops: list[dict[str, Any]] = []
    reviewed_audit: list[dict[str, Any]] = []
    used_overrides: set[tuple[str, int]] = set()
    counts: Counter[str] = Counter()

    for source in sorted(ko_papers):
        for reference in ko_papers[source].references:
            counts["total_references"] += 1
            diagnostic: dict[str, Any] = {
                "source": source,
                "reference_number": reference.number,
                "reference": reference.text,
            }
            selected_target: str | None = None
            selected_method: str | None = None
            selected_strategy: str | None = None
            stage_seen = False
            dois = _extract_dois(reference.text)

            # KCI records which corpus article a reference points at; the
            # generator carries that link in the ID column. It is recorded
            # evidence, so it wins outright and never needs disambiguation.
            if reference.target_record_id is not None:
                linked = by_record_id.get(reference.target_record_id)
                if linked is None:
                    raise CorpusValidationError(
                        f"{source}#{reference.number} cites unknown record id "
                        f"{reference.target_record_id}"
                    )
                stage_seen = True
                diagnostic["target_record_id"] = reference.target_record_id
                diagnostic["candidates"] = [linked]
                selected_target = linked
                selected_method = "kci_link"
                selected_strategy = "kci_link"

            if selected_target is None:
                doi_candidates = (
                    set().union(*(doi_index.get(doi, set()) for doi in dois)) if dois else set()
                )
                if doi_candidates:
                    stage_seen = True
                    diagnostic["dois"] = dois
                    diagnostic["candidates"] = sorted(doi_candidates)
                    if len(doi_candidates) == 1:
                        selected_target = next(iter(doi_candidates))
                        selected_method = "doi"
                        selected_strategy = "doi"
                    else:
                        diagnostic["reason"] = "multiple_doi_candidates"

            override_key = (source, reference.number)
            override = reviewed_overrides.get(override_key)
            if override is not None:
                if selected_target is not None:
                    raise CorpusValidationError(
                        f"reviewed override {source}#{reference.number} became redundant with automatic resolution"
                    )
                selected_target = str(override["target"])
                selected_method = "reviewed"
                selected_strategy = "reviewed"
                stage_seen = True
                diagnostic.pop("reason", None)
                used_overrides.add(override_key)
                reviewed_audit.append(
                    {
                        "source": source,
                        "reference_number": reference.number,
                        "target": selected_target,
                        "reference_text_sha256": override["reference_text_sha256"],
                        "evidence": override["evidence"],
                    }
                )

            if selected_target is None:
                if diagnostic.get("reason", "").startswith("multiple_"):
                    counts["ambiguous"] += 1
                    ambiguous.append(diagnostic)
                else:
                    diagnostic["reason"] = "no_internal_exact_match" if not stage_seen else "no_unique_match"
                    diagnostic["dois"] = diagnostic.get("dois", dois)
                    counts["unresolved"] += 1
                    unresolved.append(diagnostic)
                continue

            if selected_target == source:
                diagnostic.update({"target": selected_target, "method": selected_method, "reason": "self_loop"})
                counts["self_loop"] += 1
                self_loops.append(diagnostic)
                continue

            assert selected_method is not None and selected_strategy is not None
            counts[f"method_{selected_method}"] += 1
            counts[f"strategy_{selected_strategy}"] += 1
            counts["resolved_reference_mentions"] += 1
            resolved_mentions.append(
                ResolvedReference(
                    source=source,
                    target=selected_target,
                    method=selected_method,
                    strategy=selected_strategy,
                    reference_number=reference.number,
                )
            )

    unused_overrides = sorted(set(reviewed_overrides) - used_overrides)
    if unused_overrides:
        endpoints = ", ".join(f"{source}#{number}" for source, number in unused_overrides)
        raise CorpusValidationError(f"reviewed citation overrides were not applied: {endpoints}")

    grouped: dict[tuple[str, str], list[ResolvedReference]] = defaultdict(list)
    for resolved in resolved_mentions:
        grouped[(resolved.source, resolved.target)].append(resolved)
    edges: list[dict[str, Any]] = []
    for (source, target), mentions in sorted(grouped.items()):
        method = min((mention.method for mention in mentions), key=METHOD_PRIORITY.__getitem__)
        edges.append(
            {
                "source": source,
                "target": target,
                "method": method,
                "reference_numbers": sorted({mention.reference_number for mention in mentions}),
            }
        )
    counts["unique_edges"] = len(edges)
    counts["collapsed_duplicate_mentions"] = len(resolved_mentions) - len(edges)
    checkpoints = {}
    for name, maximum_strategy in (
        ("kci_link", 0),
        ("doi", 1),
        ("reviewed", 2),
    ):
        checkpoints[name] = len(
            {
                (mention.source, mention.target)
                for mention in resolved_mentions
                if STRATEGY_PRIORITY[mention.strategy] <= maximum_strategy
            }
        )
    audit = {
        "counts": dict(sorted(counts.items())),
        "edge_count_checkpoints": checkpoints,
        "resolved_references": [
            {
                "source": mention.source,
                "reference_number": mention.reference_number,
                "target": mention.target,
                "method": mention.method,
                "resolution_strategy": mention.strategy,
            }
            for mention in sorted(
                resolved_mentions,
                key=lambda item: (item.source, item.reference_number, item.target),
            )
        ],
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        "self_loops": self_loops,
        "reviewed_overrides": reviewed_audit,
        "ambiguous_corpus_dois": {
            doi: sorted(ids) for doi, ids in sorted(doi_index.items()) if len(ids) > 1
        },
    }
    return edges, audit, counts


def _deterministic_generated_at(corpus: Mapping[str, Mapping[str, ParsedPaper]]) -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch is not None:
        try:
            timestamp = int(epoch)
        except ValueError as exc:
            raise CorpusValidationError("SOURCE_DATE_EPOCH must be an integer") from exc
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
    return max(paper.last_updated for language in corpus.values() for paper in language.values())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_digest(corpus: Mapping[str, Mapping[str, ParsedPaper]]) -> str:
    """Hash every logical source path and byte digest in stable order."""

    digest = hashlib.sha256()
    for language in ("ko", "en"):
        for identifier, paper in sorted(corpus[language].items()):
            logical_path = f"{language}/{identifier}.md".encode("utf-8")
            digest.update(len(logical_path).to_bytes(4, "big"))
            digest.update(logical_path)
            digest.update(bytes.fromhex(_file_sha256(paper.path)))
    return digest.hexdigest()


def _publication_dates_digest(publication_dates: Mapping[str, PublicationDate]) -> str:
    """Hash the canonical KCI identifier/date projection, not the whole DB file."""

    digest = hashlib.sha256()
    for source_id, publication_date in sorted(publication_dates.items()):
        digest.update(source_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(publication_date.value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def load_publication_dates(
    database_path: Path,
    papers: Mapping[str, ParsedPaper],
) -> tuple[dict[str, PublicationDate], dict[str, Any]]:
    """Strictly join KCI month-precision publication dates to the MD corpus.

    ``kci_papers.registered_at`` is a legacy column name.  Its value is built
    by :mod:`Core.normalizer` from KCI ``journalInfo/pub-year`` and
    ``journalInfo/pub-mon``.  It is therefore the canonical publication month,
    not a database registration timestamp.
    """

    database_path = database_path.resolve()
    if not database_path.is_file():
        raise CorpusValidationError(f"publication-date database is missing: {database_path}")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(kci_papers)").fetchall()
        }
        required_columns = {"source", "source_id", "publication_year", "registered_at"}
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            raise CorpusValidationError(
                "kci_papers is missing publication-date columns: "
                + ", ".join(missing_columns)
            )
        rows = connection.execute(
            "SELECT source, source_id, publication_year, registered_at "
            "FROM kci_papers ORDER BY source_id"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise CorpusValidationError(
            f"unable to read publication dates from {database_path}: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    rows_by_id: dict[str, tuple[Any, Any, Any]] = {}
    duplicate_ids: list[str] = []
    for source, source_id, publication_year, registered_at in rows:
        if not isinstance(source_id, str) or not source_id:
            raise CorpusValidationError("kci_papers contains an empty or non-string source_id")
        if source_id in rows_by_id:
            duplicate_ids.append(source_id)
            continue
        rows_by_id[source_id] = (source, publication_year, registered_at)
    if duplicate_ids:
        raise CorpusValidationError(
            "kci_papers contains duplicate source_id values: "
            + ", ".join(sorted(set(duplicate_ids))[:10])
        )

    expected_ids = set(papers)
    missing_ids = sorted(expected_ids - rows_by_id.keys())
    if missing_ids:
        raise CorpusValidationError(
            f"publication-date join is missing {len(missing_ids)} corpus IDs: {missing_ids[:10]}"
        )

    publication_dates: dict[str, PublicationDate] = {}
    for source_id in sorted(expected_ids):
        source, database_year, raw_date = rows_by_id[source_id]
        if source != "kci":
            raise CorpusValidationError(
                f"{source_id}: publication-date row source must be 'kci' (got {source!r})"
            )
        if isinstance(database_year, bool) or not isinstance(database_year, int):
            raise CorpusValidationError(
                f"{source_id}: publication_year must be an integer (got {database_year!r})"
            )
        if not isinstance(raw_date, str):
            raise CorpusValidationError(
                f"{source_id}: registered_at must contain KCI YYYY-MM (got {raw_date!r})"
            )
        match = PUBLICATION_DATE_RE.fullmatch(raw_date)
        if match is None:
            raise CorpusValidationError(
                f"{source_id}: registered_at must be exact KCI YYYY-MM (got {raw_date!r})"
            )
        year, month = int(match.group(1)), int(match.group(2))
        markdown_year = int(papers[source_id].table["year"])
        if database_year != year or markdown_year != year:
            raise CorpusValidationError(
                f"{source_id}: publication year mismatch "
                f"(registered_at={raw_date}, database={database_year}, Markdown={markdown_year})"
            )
        publication_dates[source_id] = PublicationDate(
            source_id=source_id,
            value=raw_date,
            year=year,
            month=month,
        )

    date_values = [item.value for item in publication_dates.values()]
    month_counts = Counter(date_values)
    publication_dates_sha256 = _publication_dates_digest(publication_dates)
    audit = {
        "source": "KCI articleDetail journalInfo/pub-year and journalInfo/pub-mon",
        "database_projection": "kci_papers(source, source_id, publication_year, registered_at)",
        "precision": "month",
        "expected_corpus_count": len(expected_ids),
        "matched_corpus_count": len(publication_dates),
        "database_row_count": len(rows),
        "database_extra_source_id_count": len(rows_by_id.keys() - expected_ids),
        "publication_date_min": min(date_values),
        "publication_date_max": max(date_values),
        "publication_month_counts": dict(sorted(month_counts.items())),
        "publication_dates_sha256": publication_dates_sha256,
        "format_validation": "passed",
        "database_year_consistency": "passed",
        "markdown_year_consistency": "passed",
    }
    return publication_dates, audit


def _build_input_provenance(
    *,
    markdown_source_sha256: str,
    publication_dates_sha256: str,
    citation_overrides_path: Path | None,
    author_identity_overrides_path: Path | None,
) -> tuple[dict[str, str | None], str]:
    """Describe and hash every mutable input that can change graph semantics."""

    inputs: dict[str, str | None] = {
        "author_identity_overrides_sha256": (
            _file_sha256(author_identity_overrides_path)
            if author_identity_overrides_path is not None
            else None
        ),
        "citation_overrides_sha256": (
            _file_sha256(citation_overrides_path)
            if citation_overrides_path is not None
            else None
        ),
        "markdown_source_sha256": markdown_source_sha256,
        "publication_dates_sha256": publication_dates_sha256,
    }
    digest = hashlib.sha256()
    for key, value in sorted(inputs.items()):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update((value if value is not None else "none").encode("ascii"))
        digest.update(b"\0")
    return inputs, digest.hexdigest()


def _normalize_affiliation(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    normalized = normalized.replace("경찰대학교", "경찰대학")
    normalized = normalized.replace("성신여대", "성신여자대학교")
    normalized = normalized.replace("이화여대", "이화여자대학교")
    normalized = re.sub(r"동아대(?!학교)", "동아대학교", normalized)
    normalized = re.sub(r"상명대(?!학교)", "상명대학교", normalized)
    normalized = re.sub(r"국립목포대(?!학교)", "국립목포대학교", normalized)
    return "".join(character for character in normalized if character.isalnum())


def _affiliations_compatible(left: str, right: str) -> bool:
    left_key, right_key = _normalize_affiliation(left), _normalize_affiliation(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    return min(len(left_key), len(right_key)) >= 4 and (
        left_key in right_key or right_key in left_key
    )


def _occurrence_evidence(occurrence: AuthorOccurrence) -> dict[str, Any]:
    return {
        "occurrence_id": occurrence.occurrence_id,
        "paper_id": occurrence.paper_id,
        "position": occurrence.position,
        "year": occurrence.year,
        "name": occurrence.name,
        "normalized_name": occurrence.normalized_name,
        "affiliations": list(occurrence.affiliations),
        "coauthor_names": list(occurrence.coauthor_names),
        "title": occurrence.title,
        "journal": occurrence.journal,
        "tags": list(occurrence.tags),
        "source_md_sha256": occurrence.source_md_sha256,
    }


def _identity_evidence(occurrence: AuthorOccurrence) -> dict[str, Any]:
    """Canonical identity-only fields that bind a reviewed decision.

    Abstracts, titles, tags, journals, whole-Markdown hashes, and the global
    source digest are intentionally excluded.  A content-only edit must not
    invalidate an identity review, while a changed author/coauthor,
    affiliation, year, or source occurrence must fail closed.
    """

    return {
        "paper_id": occurrence.paper_id,
        "position": occurrence.position,
        "year": occurrence.year,
        "name": occurrence.normalized_name,
        "valid_affiliations": sorted({
            normalized
            for value in occurrence.affiliations
            if (normalized := _normalize_affiliation(value))
        }),
        "coauthors": sorted({normalize_identity(value) for value in occurrence.coauthor_names}),
    }


def _occurrence_evidence_sha256(occurrences: Iterable[AuthorOccurrence]) -> str:
    payload = [_identity_evidence(occurrence) for occurrence in sorted(
        occurrences, key=lambda item: (item.paper_id, item.position)
    )]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _collect_author_occurrences(
    corpus: Mapping[str, Mapping[str, ParsedPaper]],
) -> tuple[dict[str, list[AuthorOccurrence]], dict[str, list[AuthorOccurrence]], list[dict[str, Any]]]:
    by_name: dict[str, list[AuthorOccurrence]] = defaultdict(list)
    by_paper: dict[str, list[AuthorOccurrence]] = {}
    unmatched_affiliations: list[dict[str, Any]] = []
    for paper_id, paper in sorted(corpus["ko"].items()):
        names = _split_authors(paper.table["authors"])
        normalized_names = [normalize_identity(name) for name in names]
        if len(normalized_names) != len(set(normalized_names)):
            raise CorpusValidationError(f"{paper_id}: duplicate exact author identities in paper")
        temporary_ids = {name_key: author_id(name_key) for name_key in normalized_names}
        affiliations, unmatched = _parse_affiliations(paper.table["affiliations"], temporary_ids)
        for item in unmatched:
            unmatched_affiliations.append({"paper_id": paper_id, **item})
        affiliation_by_temporary_id: dict[str, set[str]] = defaultdict(set)
        for affiliation in affiliations:
            if affiliation["author_id"] and affiliation["institution"]:
                affiliation_by_temporary_id[affiliation["author_id"]].add(affiliation["institution"])

        paper_occurrences: list[AuthorOccurrence] = []
        for position, (name, name_key) in enumerate(zip(names, normalized_names, strict=True), start=1):
            occurrence = AuthorOccurrence(
                occurrence_id=f"{paper_id}#{position}",
                paper_id=paper_id,
                position=position,
                year=int(paper.table["year"]),
                name=name,
                normalized_name=name_key,
                affiliations=tuple(sorted(affiliation_by_temporary_id[temporary_ids[name_key]])),
                coauthor_names=tuple(name_value for index, name_value in enumerate(names) if index != position - 1),
                title=paper.subject,
                journal=_plain_or_none(paper.table["journal"]),
                tags=paper.tags,
                source_md_sha256=_file_sha256(paper.path),
            )
            paper_occurrences.append(occurrence)
            by_name[name_key].append(occurrence)
        by_paper[paper_id] = paper_occurrences
    return dict(by_name), by_paper, unmatched_affiliations


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parents = list(range(size))

    def find(self, item: int) -> int:
        while self.parents[item] != item:
            self.parents[item] = self.parents[self.parents[item]]
            item = self.parents[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root


def _load_author_identity_overrides(
    path: Path | None,
    *,
    occurrences_by_name: Mapping[str, Sequence[AuthorOccurrence]],
) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusValidationError(f"cannot read author identity overrides {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CorpusValidationError(f"{path}: unsupported author identity override schema")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise CorpusValidationError(f"{path}: decisions must be a list")

    required = {
        "normalized_name",
        "action",
        "identity_key",
        "paper_ids",
        "evidence_sha256",
        "rationale",
    }
    validated: list[dict[str, Any]] = []
    claimed_occurrences: set[str] = set()
    identity_keys: set[tuple[str, str]] = set()
    removed_papers = _removed_paper_ids()
    for position, decision in enumerate(decisions, start=1):
        if not isinstance(decision, dict) or not required.issubset(decision):
            raise CorpusValidationError(f"{path}: decision {position} lacks required fields")
        # A reviewed decision may name a paper that was later removed from the
        # corpus (Processor/refs/excluded_papers.json).  The decision itself is
        # still correct, but it has nothing left to do once fewer papers remain
        # than the action needs, so it is retired instead of failing the build.
        if isinstance(decision["paper_ids"], list) and removed_papers.intersection(
            paper_id for paper_id in decision["paper_ids"] if isinstance(paper_id, str)
        ):
            remaining = [paper_id for paper_id in decision["paper_ids"] if paper_id not in removed_papers]
            if len(remaining) < (2 if decision["action"] == "merge" else 1):
                continue
            raise CorpusValidationError(
                f"{path}: decision {position} lost {sorted(set(decision['paper_ids']) - set(remaining))} to the exclusion list; "
                f"review the remaining papers {remaining} and update its paper_ids and evidence_sha256"
            )
        normalized_name = decision["normalized_name"]
        action = decision["action"]
        identity_key = decision["identity_key"]
        paper_ids = decision["paper_ids"]
        if (
            not isinstance(normalized_name, str)
            or normalize_identity(normalized_name) != normalized_name
        ):
            raise CorpusValidationError(f"{path}: decision {position} has a non-canonical normalized_name")
        if normalized_name not in occurrences_by_name:
            raise CorpusValidationError(f"{path}: decision {position} names an absent author")
        if action not in {"merge", "assign"}:
            raise CorpusValidationError(f"{path}: decision {position} action must be merge or assign")
        if not isinstance(identity_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", identity_key):
            raise CorpusValidationError(f"{path}: decision {position} has an invalid identity_key")
        if (normalized_name, identity_key) in identity_keys:
            raise CorpusValidationError(f"{path}: duplicate identity_key for {normalized_name!r}")
        identity_keys.add((normalized_name, identity_key))
        minimum_size = 2 if action == "merge" else 1
        if (
            not isinstance(paper_ids, list)
            or len(paper_ids) < minimum_size
            or any(not isinstance(paper_id, str) or not IDENTIFIER_RE.fullmatch(paper_id) for paper_id in paper_ids)
            or len(paper_ids) != len(set(paper_ids))
        ):
            raise CorpusValidationError(
                f"{path}: decision {position} paper_ids must be a unique list of {minimum_size}+"
            )
        occurrence_lookup = {
            occurrence.paper_id: occurrence for occurrence in occurrences_by_name[normalized_name]
        }
        if any(paper_id not in occurrence_lookup for paper_id in paper_ids):
            raise CorpusValidationError(f"{path}: decision {position} references a paper without that author")
        selected = [occurrence_lookup[paper_id] for paper_id in sorted(paper_ids)]
        if any(occurrence.occurrence_id in claimed_occurrences for occurrence in selected):
            raise CorpusValidationError(f"{path}: author occurrence is claimed by multiple decisions")
        claimed_occurrences.update(occurrence.occurrence_id for occurrence in selected)
        actual_evidence_hash = _occurrence_evidence_sha256(selected)
        if (
            not isinstance(decision["evidence_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", decision["evidence_sha256"])
            or decision["evidence_sha256"] != actual_evidence_hash
        ):
            raise CorpusValidationError(f"{path}: decision {position} evidence hash is stale")
        if not isinstance(decision["rationale"], str) or not decision["rationale"].strip():
            raise CorpusValidationError(f"{path}: decision {position} lacks rationale")
        review_sources = decision.get("review_sources", [])
        if not isinstance(review_sources, list):
            raise CorpusValidationError(f"{path}: decision {position} review_sources must be a list")
        for source_position, source in enumerate(review_sources, start=1):
            if not isinstance(source, dict) or not {
                "paper_id", "path", "sha256", "source_kind", "evidence_note"
            }.issubset(source):
                raise CorpusValidationError(
                    f"{path}: decision {position} review source {source_position} is incomplete"
                )
            if source["paper_id"] not in paper_ids:
                raise CorpusValidationError(
                    f"{path}: decision {position} review source is outside its paper_ids"
                )
            if (
                not isinstance(source["path"], str)
                or not isinstance(source["source_kind"], str)
                or not source["source_kind"].strip()
                or not isinstance(source["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
            ):
                raise CorpusValidationError(
                    f"{path}: decision {position} review source {source_position} has invalid fields"
                )
            source_path = (PROJECT_ROOT / source["path"]).resolve()
            try:
                source_path.relative_to((PROJECT_ROOT / "Analyzer" / "Paper").resolve())
            except ValueError as exc:
                raise CorpusValidationError(
                    f"{path}: decision {position} review source leaves Analyzer/Paper"
                ) from exc
            if not source_path.is_file() or _file_sha256(source_path) != source["sha256"]:
                raise CorpusValidationError(
                    f"{path}: decision {position} review source hash is stale"
                )
            if not isinstance(source["evidence_note"], str) or not source["evidence_note"].strip():
                raise CorpusValidationError(
                    f"{path}: decision {position} review source lacks evidence_note"
                )
        validated.append({**decision, "paper_ids": sorted(paper_ids)})
    return validated


def _removed_paper_ids(path: Path | None = None) -> frozenset[str]:
    """Papers removed from the corpus after review (``Collector/paper_removal.py``)."""

    source = path or PROJECT_ROOT / "Processor" / "refs" / "excluded_papers.json"
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    papers = payload.get("papers", []) if isinstance(payload, dict) else []
    return frozenset(str(item["source_id"]) for item in papers if isinstance(item, dict) and item.get("source_id"))


def _resolved_author_id(normalized_name: str, identity_key: str) -> str:
    material = f"{normalized_name}\0{identity_key}"
    return f"AUT-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def _automatic_identity_key(component: Sequence[AuthorOccurrence]) -> str:
    """Return an append-stable key anchored to an existing source occurrence.

    The key never depends on the number or ordinal position of namesake
    components.  Therefore adding a disconnected namesake cannot rename an
    already published identity.  A later reviewed override may deliberately
    replace it and is exposed through the migration aliases.
    """

    anchor = min(component, key=lambda item: (item.paper_id, item.position))
    return f"paper-{anchor.paper_id.casefold()}-{anchor.position}"


def _resolve_author_identities(
    corpus: Mapping[str, Mapping[str, ParsedPaper]],
    *,
    overrides_path: Path | None,
) -> tuple[
    dict[str, list[str]],
    dict[str, dict[str, Any]],
    dict[str, list[AuthorOccurrence]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, str],
    dict[str, list[str]],
]:
    occurrences_by_name, occurrences_by_paper, unmatched_affiliations = _collect_author_occurrences(corpus)
    overrides = _load_author_identity_overrides(
        overrides_path,
        occurrences_by_name=occurrences_by_name,
    )
    overrides_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in overrides:
        overrides_by_name[decision["normalized_name"]].append(decision)

    author_meta: dict[str, dict[str, Any]] = {}
    author_id_aliases: dict[str, str] = {}
    ambiguous_author_id_aliases: dict[str, list[str]] = {}
    automatic_anchor_aliases: set[str] = set()
    name_only_aliases: set[str] = set()
    occurrence_to_author: dict[str, str] = {}
    candidate_audit: list[dict[str, Any]] = []
    repeated_name_count = 0
    reviewed_occurrences: set[str] = set()
    reviewed_effects: dict[tuple[str, str], str] = {}

    for normalized_name, raw_occurrences in sorted(occurrences_by_name.items()):
        occurrences = sorted(raw_occurrences, key=lambda item: (item.year, item.paper_id, item.position))
        name_decisions = overrides_by_name.get(normalized_name, [])
        occurrence_by_paper = {occurrence.paper_id: occurrence for occurrence in occurrences}
        assignment_key_by_occurrence: dict[str, str] = {}
        assignment_decisions = [decision for decision in name_decisions if decision["action"] == "assign"]
        for decision in assignment_decisions:
            for paper_id in decision["paper_ids"]:
                assignment_key_by_occurrence[occurrence_by_paper[paper_id].occurrence_id] = decision["identity_key"]
        if assignment_decisions and set(assignment_key_by_occurrence) != {
            occurrence.occurrence_id for occurrence in occurrences
        }:
            raise CorpusValidationError(
                f"assign author overrides for {normalized_name!r} must partition every occurrence"
            )
        if len(occurrences) > 1:
            repeated_name_count += 1
        # Keep the raw automatic graph separate from the effective graph.  A
        # reviewed ``assign`` decision may deliberately cut an otherwise
        # high-confidence automatic edge, and the audit must show the exact
        # before/after component counts.
        automatic_union_find = _UnionFind(len(occurrences))
        union_find = _UnionFind(len(occurrences))
        evidence_edges: list[dict[str, Any]] = []
        for right in range(len(occurrences)):
            for left in range(right):
                reasons: list[str] = []
                affiliation_pairs = sorted(
                    (left_affiliation, right_affiliation)
                    for left_affiliation in occurrences[left].affiliations
                    for right_affiliation in occurrences[right].affiliations
                    if _affiliations_compatible(left_affiliation, right_affiliation)
                )
                if affiliation_pairs:
                    reasons.append("compatible_affiliation")
                left_coauthors = {normalize_identity(name) for name in occurrences[left].coauthor_names}
                right_coauthors = {normalize_identity(name) for name in occurrences[right].coauthor_names}
                shared_coauthors = sorted(left_coauthors & right_coauthors)
                if shared_coauthors:
                    reasons.append("shared_coauthor")
                if reasons:
                    automatic_union_find.union(left, right)
                    left_assignment = assignment_key_by_occurrence.get(occurrences[left].occurrence_id)
                    right_assignment = assignment_key_by_occurrence.get(occurrences[right].occurrence_id)
                    blocked = bool(
                        assignment_decisions
                        and left_assignment is not None
                        and right_assignment is not None
                        and left_assignment != right_assignment
                    )
                    if not blocked:
                        union_find.union(left, right)
                    evidence_edges.append(
                        {
                            "left": occurrences[left].occurrence_id,
                            "right": occurrences[right].occurrence_id,
                            "reasons": reasons,
                            "affiliation_pairs": [list(pair) for pair in affiliation_pairs],
                            "shared_coauthors": shared_coauthors,
                            "blocked_by_assignment_override": blocked,
                        }
                    )

        automatic_roots = [automatic_union_find.find(index) for index in range(len(occurrences))]
        automatic_components: dict[int, list[AuthorOccurrence]] = defaultdict(list)
        for index, occurrence in enumerate(occurrences):
            automatic_components[automatic_roots[index]].append(occurrence)

        occurrence_index = {occurrence.paper_id: index for index, occurrence in enumerate(occurrences)}
        decision_key_by_occurrence: dict[str, str] = {}
        decision_rationale_by_key: dict[str, str] = {}
        decision_sources_by_key: dict[str, list[dict[str, Any]]] = {}
        decision_effect_by_key: dict[str, str] = {}
        for decision in name_decisions:
            indexes = [occurrence_index[paper_id] for paper_id in decision["paper_ids"]]
            automatic_groups = {automatic_roots[index] for index in indexes}
            if (
                decision["action"] == "merge"
                and len(automatic_groups) < 2
                and not decision.get("review_sources")
            ):
                raise CorpusValidationError(
                    f"author override {normalized_name}/{decision['identity_key']} is redundant"
                )
            for index in indexes[1:]:
                union_find.union(indexes[0], index)
            for index in indexes:
                occurrence = occurrences[index]
                decision_key_by_occurrence[occurrence.occurrence_id] = decision["identity_key"]
                reviewed_occurrences.add(occurrence.occurrence_id)
            decision_rationale_by_key[decision["identity_key"]] = decision["rationale"]
            decision_sources_by_key[decision["identity_key"]] = decision.get("review_sources", [])
            effect = (
                "hard_assignment"
                if decision["action"] == "assign"
                else ("confirmed_automatic" if len(automatic_groups) == 1 else "merged_components")
            )
            decision_effect_by_key[decision["identity_key"]] = effect
            reviewed_effects[(normalized_name, decision["identity_key"])] = effect

        final_components_mutable: dict[int, list[AuthorOccurrence]] = defaultdict(list)
        for index, occurrence in enumerate(occurrences):
            final_components_mutable[union_find.find(index)].append(occurrence)
        final_components = sorted(
            final_components_mutable.values(),
            key=lambda component: min((item.year, item.paper_id, item.position) for item in component),
        )
        identity_count = len(final_components)
        resolved_component_audit: list[dict[str, Any]] = []
        resolved_ids_for_name: list[str] = []
        for component in final_components:
            override_keys = {
                decision_key_by_occurrence[item.occurrence_id]
                for item in component
                if item.occurrence_id in decision_key_by_occurrence
            }
            if len(override_keys) > 1:
                raise CorpusValidationError(f"automatic evidence joins conflicting overrides for {normalized_name}")
            identity_key = next(iter(override_keys)) if override_keys else _automatic_identity_key(component)
            resolved_id = _resolved_author_id(normalized_name, identity_key)
            if resolved_id in author_meta:
                raise CorpusValidationError(f"resolved author ID collision: {resolved_id}")
            resolved_ids_for_name.append(resolved_id)
            # Any occurrence may have been the stable anchor of a component in
            # an earlier corpus snapshot.  Preserve every such previously
            # publishable ID when review or a new bridge joins components.
            for occurrence in component:
                anchor_id = _resolved_author_id(
                    normalized_name, _automatic_identity_key([occurrence])
                )
                if anchor_id == resolved_id:
                    continue
                existing_target = author_id_aliases.get(anchor_id)
                if existing_target is not None and existing_target != resolved_id:
                    raise CorpusValidationError(
                        f"automatic author migration alias is ambiguous: {anchor_id}"
                    )
                author_id_aliases[anchor_id] = resolved_id
                automatic_anchor_aliases.add(anchor_id)
            variants = sorted({item.name for item in component}, key=lambda value: (normalize_identity(value), value))
            rationale = decision_rationale_by_key.get(identity_key)
            review_sources = decision_sources_by_key.get(identity_key, [])
            notes: list[str] = []
            if rationale:
                notes.append(rationale)
            notes.extend(
                f"검토 근거: {source['evidence_note']} ({source['path']})"
                for source in review_sources
            )
            if identity_count > 1:
                notes.append(
                    f"동일 이름 표기에서 확인된 {identity_count}개 후보 중 별도 identity로 보수적으로 구분했습니다."
                )
            if len(component) > 1 and not rationale:
                notes.append("동일 소속 또는 반복 공저자라는 MD 내부 근거로 저자 기록을 연결했습니다.")
            confidence = (
                "reviewed"
                if rationale
                else (
                    "conservative"
                    if identity_count > 1
                    else ("evidence_based" if len(component) > 1 else "high")
                )
            )
            for occurrence in component:
                occurrence_to_author[occurrence.occurrence_id] = resolved_id
            author_meta[resolved_id] = {
                "id": resolved_id,
                "name": variants[0],
                "display_name": variants[0],
                "normalized_name": normalized_name,
                "name_variants": variants,
                "identity_key": identity_key,
                "identity_confidence": confidence,
                "identity_notes": notes,
                "identity_review_effect": decision_effect_by_key.get(identity_key),
                "namesake_identity_count": identity_count,
                "legacy_author_ids": [],
            }
            resolved_component_audit.append(
                {
                    "author_id": resolved_id,
                    "identity_key": identity_key,
                    "identity_confidence": confidence,
                    "paper_ids": sorted(item.paper_id for item in component),
                    "affiliations": sorted({value for item in component for value in item.affiliations}),
                    "identity_notes": notes,
                    "identity_review_effect": decision_effect_by_key.get(identity_key),
                    "review_sources": review_sources,
                }
            )

        legacy_id = author_id(normalized_name)
        if identity_count == 1:
            resolved_id = resolved_ids_for_name[0]
            if legacy_id != resolved_id:
                existing_target = author_id_aliases.get(legacy_id)
                if existing_target is not None and existing_target != resolved_id:
                    raise CorpusValidationError(
                        f"name-only author migration alias is ambiguous: {legacy_id}"
                    )
                author_id_aliases[legacy_id] = resolved_id
                name_only_aliases.add(legacy_id)
        else:
            # A name-only historical ID represented multiple people and cannot
            # be migrated to one of them without silently choosing a namesake.
            ambiguous_author_id_aliases[legacy_id] = sorted(resolved_ids_for_name)

        if len(occurrences) > 1:
            decision_status = (
                "reviewed_override"
                if name_decisions
                else ("conservative_split" if identity_count > 1 else "automatic_connected")
            )
            candidate_audit.append(
                {
                    "normalized_name": normalized_name,
                    "display_name": occurrences[0].name,
                    "occurrence_count": len(occurrences),
                    "automatic_component_count": len(automatic_components),
                    "resolved_identity_count": identity_count,
                    "decision": decision_status,
                    "automatic_components": [
                        {
                            "paper_ids": sorted(item.paper_id for item in component),
                            "affiliations": sorted({value for item in component for value in item.affiliations}),
                            "coauthor_names": sorted({value for item in component for value in item.coauthor_names}),
                        }
                        for component in sorted(
                            automatic_components.values(),
                            key=lambda values: min(item.paper_id for item in values),
                        )
                    ],
                    "resolved_components": resolved_component_audit,
                    "automatic_evidence_edges": evidence_edges,
                    "occurrences": [_occurrence_evidence(item) for item in occurrences],
                }
            )

    if set(author_id_aliases) & set(ambiguous_author_id_aliases):
        raise CorpusValidationError("an author ID appears in both unique and ambiguous migration maps")
    for legacy_id, resolved_id in author_id_aliases.items():
        if legacy_id in author_meta and legacy_id != resolved_id:
            raise CorpusValidationError(
                f"author migration alias shadows a current identity: {legacy_id}"
            )
        author_meta[resolved_id]["legacy_author_ids"].append(legacy_id)
    for meta in author_meta.values():
        meta["legacy_author_ids"] = sorted(set(meta["legacy_author_ids"]))

    if reviewed_occurrences != {
        f"{paper_id}#{occurrence.position}"
        for decision in overrides
        for paper_id in decision["paper_ids"]
        for occurrence in occurrences_by_name[decision["normalized_name"]]
        if occurrence.paper_id == paper_id
    }:
        raise AssertionError("not every reviewed author occurrence was consumed")

    paper_author_ids = {
        paper_id: [occurrence_to_author[occurrence.occurrence_id] for occurrence in occurrences]
        for paper_id, occurrences in occurrences_by_paper.items()
    }
    identity_audit = {
        "policy": {
            "candidate_key": "NFKC + collapsed whitespace + casefold exact name",
            "automatic_merge": ["compatible author affiliation", "exact repeated coauthor"],
            "default_when_disconnected": "conservative split",
            "invalid_affiliation_evidence": ["N/A", "NA", "없음", "소속 미상", "unknown"],
            "resolved_id": "SHA-256(normalized_name + NUL + stable identity_key), first 12 hex",
            "automatic_identity_key": "source paper/author-position anchor; independent of namesake count",
            "legacy_name_only_id_migration": "unique aliases and ambiguous aliases are reported separately",
            "automatic_anchor_migration": "every prior source-occurrence anchor aliases to its resolved identity",
            "override_evidence": "canonical paper/name/valid-affiliation/coauthor/year fields only",
            "fuzzy_name_merge": False,
        },
        "counts": {
            "name_key_count": len(occurrences_by_name),
            "repeated_name_count": repeated_name_count,
            "candidate_name_count": len(candidate_audit),
            "raw_disconnected_name_count": sum(
                item["automatic_component_count"] > 1 for item in candidate_audit
            ),
            "resolved_namesake_name_count": sum(
                item["resolved_identity_count"] > 1 for item in candidate_audit
            ),
            "automatic_connected_unreviewed_name_count": sum(
                item["decision"] == "automatic_connected" for item in candidate_audit
            ),
            "conservative_split_name_count": sum(
                item["decision"] == "conservative_split" for item in candidate_audit
            ),
            "reviewed_override_name_count": len(overrides_by_name),
            "reviewed_decision_count": len(overrides),
            "reviewed_merge_name_count": len({
                decision["normalized_name"] for decision in overrides if decision["action"] == "merge"
            }),
            "reviewed_assignment_name_count": len({
                decision["normalized_name"] for decision in overrides if decision["action"] == "assign"
            }),
            "resolved_author_count": len(author_meta),
            "authorship_count": sum(len(values) for values in occurrences_by_paper.values()),
            "unique_legacy_alias_count": len(author_id_aliases),
            "name_only_legacy_alias_count": len(name_only_aliases),
            "automatic_anchor_alias_count": len(automatic_anchor_aliases),
            "ambiguous_legacy_alias_count": len(ambiguous_author_id_aliases),
        },
        "author_identity_candidates": candidate_audit,
        "reviewed_decisions": [
            {
                "normalized_name": decision["normalized_name"],
                "action": decision["action"],
                "identity_key": decision["identity_key"],
                "paper_ids": decision["paper_ids"],
                "evidence_sha256": decision["evidence_sha256"],
                "rationale": decision["rationale"],
                "review_sources": decision.get("review_sources", []),
                "effect": reviewed_effects[(decision["normalized_name"], decision["identity_key"])],
            }
            for decision in overrides
        ],
    }
    return (
        paper_author_ids,
        author_meta,
        occurrences_by_paper,
        identity_audit,
        unmatched_affiliations,
        dict(sorted(author_id_aliases.items())),
        dict(sorted(ambiguous_author_id_aliases.items())),
    )


def compile_graph(
    source_dir: Path = DEFAULT_SOURCE,
    *,
    database_path: Path = DEFAULT_DATABASE,
    expected_count: int | None = None,
    overrides_path: Path | None = None,
    author_overrides_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, ParsedPaper]]]:
    resolved_source = source_dir.resolve()
    corpus = load_corpus(resolved_source, expected_count=expected_count)
    ko_papers = corpus["ko"]
    if overrides_path is None and resolved_source == DEFAULT_SOURCE.resolve():
        overrides_path = DEFAULT_OVERRIDES
    if author_overrides_path is None and resolved_source == DEFAULT_SOURCE.resolve():
        author_overrides_path = DEFAULT_AUTHOR_OVERRIDES
    reviewed_overrides = _load_reviewed_overrides(
        overrides_path.resolve() if overrides_path is not None else None,
        ko_papers,
    )
    publication_dates, publication_date_audit = load_publication_dates(
        database_path,
        ko_papers,
    )
    generated_at = _deterministic_generated_at(corpus)
    source_digest = _source_digest(corpus)
    build_inputs, build_input_digest = _build_input_provenance(
        markdown_source_sha256=source_digest,
        publication_dates_sha256=publication_date_audit["publication_dates_sha256"],
        citation_overrides_path=overrides_path.resolve() if overrides_path is not None else None,
        author_identity_overrides_path=(
            author_overrides_path.resolve() if author_overrides_path is not None else None
        ),
    )
    (
        paper_author_ids,
        author_meta,
        occurrences_by_paper,
        author_identity_audit,
        affiliation_audit,
        author_id_aliases,
        ambiguous_author_id_aliases,
    ) = _resolve_author_identities(
        corpus,
        overrides_path=author_overrides_path.resolve() if author_overrides_path is not None else None,
    )
    citation_edges, resolution_audit, resolution_counts = _resolve_citations(corpus, reviewed_overrides)

    incoming: Counter[str] = Counter(edge["target"] for edge in citation_edges)
    author_paper_ids: dict[str, set[str]] = defaultdict(set)
    author_affiliations: dict[str, set[str]] = defaultdict(set)
    author_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    papers: dict[str, dict[str, Any]] = {}

    for identifier in sorted(ko_papers):
        ko, en = ko_papers[identifier], corpus["en"][identifier]
        publication_date = publication_dates[identifier]
        occurrences = occurrences_by_paper[identifier]
        names = [occurrence.name for occurrence in occurrences]
        ids = paper_author_ids[identifier]
        affiliations = [
            {
                "author_id": resolved_author_id,
                "author_name": occurrence.name,
                "institution": institution,
            }
            for occurrence, resolved_author_id in zip(occurrences, ids, strict=True)
            for institution in occurrence.affiliations
        ]
        year = publication_date.year
        for occurrence, identifier_for_author in zip(occurrences, ids, strict=True):
            author_paper_ids[identifier_for_author].add(identifier)
            author_affiliations[identifier_for_author].update(occurrence.affiliations)
            author_history[identifier_for_author].append(
                {
                    "paper_id": identifier,
                    "year": year,
                    "publication_date": publication_date.value,
                    "publication_year": publication_date.year,
                    "publication_month": publication_date.month,
                    "affiliations": list(occurrence.affiliations),
                }
            )

        papers[identifier] = {
            "id": identifier,
            # Persistent profile id printed in the Markdown front matter; the
            # graph keys on the stable KCI article id above.
            "record_id": ko.record_id,
            "title": {"ko": ko.subject, "en": en.subject},
            "year": year,
            "publication_date": publication_date.value,
            "publication_year": publication_date.year,
            "publication_month": publication_date.month,
            "publication_date_precision": "month",
            "doi": normalize_doi(ko.table["doi"]),
            "url": _plain_or_none(ko.table["url"]),
            "journal": _plain_or_none(ko.table["journal"]),
            "publisher": _plain_or_none(ko.table["publisher"]),
            "type": _plain_or_none(ko.table["type"]),
            "oa": _normalize_oa(ko.table["oa"], ko.path),
            "tags": list(ko.tags),
            "author_ids": ids,
            "author_names": names,
            "affiliations": affiliations,
            "reference_count": len(ko.references),
            "internal_citation_count": incoming[identifier],
            "md": {
                "ko": f"./data/graph/papers/{ko.record_id}/ko.md",
                "en": f"./data/graph/papers/{ko.record_id}/en.md",
            },
            "md_sha256": {
                "ko": _file_sha256(ko.path),
                "en": _file_sha256(en.path),
            },
        }

    authors: list[dict[str, Any]] = []
    for identifier_for_author in sorted(author_paper_ids):
        paper_ids = sorted(
            author_paper_ids[identifier_for_author],
            key=lambda pid: (papers[pid]["publication_date"], pid),
        )
        years = [papers[paper_id]["year"] for paper_id in paper_ids]
        publication_date_values = [papers[paper_id]["publication_date"] for paper_id in paper_ids]
        meta = author_meta[identifier_for_author]
        affiliation_values = sorted(author_affiliations[identifier_for_author])
        affiliation_frequency = Counter(
            affiliation
            for history in author_history[identifier_for_author]
            for affiliation in history["affiliations"]
        )
        representative_affiliation = (
            min(
                affiliation_frequency,
                key=lambda value: (-affiliation_frequency[value], len(value), normalize_identity(value)),
            )
            if affiliation_frequency
            else None
        )
        disambiguation_label = representative_affiliation or ""
        if meta["namesake_identity_count"] > 1:
            year_label = str(min(years)) if min(years) == max(years) else f"{min(years)}–{max(years)}"
            disambiguation_label = (
                f"{representative_affiliation} · {year_label}"
                if representative_affiliation
                else year_label
            )
        authors.append(
            {
                **meta,
                "id": identifier_for_author,
                "disambiguation_label": disambiguation_label,
                "representative_affiliation": representative_affiliation,
                "paper_ids": paper_ids,
                "paper_count": len(paper_ids),
                "year_min": min(years),
                "year_max": max(years),
                "publication_date_min": min(publication_date_values),
                "publication_date_max": max(publication_date_values),
                "affiliations": affiliation_values,
                "affiliation_history": sorted(
                    author_history[identifier_for_author],
                    key=lambda item: (item["publication_date"], item["paper_id"]),
                ),
                "internal_citations_total": sum(papers[paper_id]["internal_citation_count"] for paper_id in paper_ids),
            }
        )
    authors_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for author in authors:
        authors_by_name[author["normalized_name"]].append(author)
    for namesakes in authors_by_name.values():
        if len(namesakes) < 2:
            continue
        label_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for author in namesakes:
            label_groups[author["disambiguation_label"]].append(author)
        for duplicates in label_groups.values():
            if len(duplicates) < 2:
                continue
            for ordinal, author in enumerate(sorted(duplicates, key=lambda item: item["id"]), start=1):
                author["disambiguation_label"] = f"{author['disambiguation_label']} · #{ordinal}"
        if len({author["disambiguation_label"] for author in namesakes}) != len(namesakes):
            raise AssertionError("namesake disambiguation labels are not unique")
    authors.sort(key=lambda item: (normalize_identity(item["display_name"]), item["disambiguation_label"], item["id"]))

    edge_incoming = Counter(edge["target"] for edge in citation_edges)
    if any(papers[paper_id]["internal_citation_count"] != edge_incoming[paper_id] for paper_id in papers):
        raise AssertionError("paper incoming citation counts do not match citation edges")
    if any(edge["source"] not in papers or edge["target"] not in papers for edge in citation_edges):
        raise AssertionError("citation edge points outside the corpus")
    if any(edge["source"] == edge["target"] for edge in citation_edges):
        raise AssertionError("citation edge contains a self-loop")
    if len({(edge["source"], edge["target"]) for edge in citation_edges}) != len(citation_edges):
        raise AssertionError("citation edges are not unique by source and target")

    years = [paper["year"] for paper in papers.values()]
    publication_date_values = [paper["publication_date"] for paper in papers.values()]
    publication_month_counts = Counter(publication_date_values)
    total_references = sum(paper["reference_count"] for paper in papers.values())
    corpus_summary = {
        "paper_count": len(papers),
        "language_file_count": {"ko": len(corpus["ko"]), "en": len(corpus["en"])},
        "author_count": len(authors),
        "author_identity_count": len(authors),
        "authorship_count": sum(len(paper["author_ids"]) for paper in papers.values()),
        "author_name_key_count": author_identity_audit["counts"]["name_key_count"],
        "author_identity_candidate_count": author_identity_audit["counts"]["candidate_name_count"],
        "author_reviewed_identity_decision_count": author_identity_audit["counts"]["reviewed_decision_count"],
        "author_reviewed_identity_name_count": author_identity_audit["counts"]["reviewed_override_name_count"],
        "author_ambiguous_legacy_alias_count": author_identity_audit["counts"]["ambiguous_legacy_alias_count"],
        "year_min": min(years),
        "year_max": max(years),
        "year_counts": {str(year): sum(value == year for value in years) for year in sorted(set(years))},
        "publication_date_coverage": len(publication_date_values),
        "publication_date_precision": "month",
        "publication_date_source": publication_date_audit["source"],
        "publication_date_min": min(publication_date_values),
        "publication_date_max": max(publication_date_values),
        "publication_month_counts": dict(sorted(publication_month_counts.items())),
        "publication_dates_sha256": publication_date_audit["publication_dates_sha256"],
        "reference_count": total_references,
        "citation_edge_count": len(citation_edges),
        "source_last_updated": max(paper.last_updated for paper in corpus["ko"].values()),
        "generated_at": generated_at,
        "build_input_digest": build_input_digest,
        "citation_scope": "internal_corpus_only",
        "citation_source": "paper_markdown_references_with_hash_bound_reviews",
        "automatic_citation_source": "paper_markdown_references_only",
        "reviewed_override_count": len(reviewed_overrides),
    }
    index = {
        "schema_version": 2,
        "source_digest": source_digest,
        "build_inputs": build_inputs,
        "build_input_digest": build_input_digest,
        "author_id_aliases": author_id_aliases,
        "ambiguous_author_id_aliases": ambiguous_author_id_aliases,
        "corpus": corpus_summary,
        "authors": authors,
        "papers": papers,
        "citation_edges": citation_edges,
    }
    audit = {
        "schema_version": 2,
        "generated_at": generated_at,
        "build_inputs": build_inputs,
        "build_input_digest": build_input_digest,
        "corpus": corpus_summary,
        "validation": {
            "identifier_pairing": "passed",
            "filename_frontmatter_h1": "passed",
            "bilingual_metadata_consistency": "passed",
            "publication_date_join": "passed",
            "publication_date_year_consistency": "passed",
            "citation_incoming_inverse": "passed",
            "unmatched_affiliations": affiliation_audit,
        },
        "publication_dates": publication_date_audit,
        "author_identity": author_identity_audit,
        "citation_resolution": resolution_audit,
    }
    if resolution_counts["total_references"] != total_references:
        raise AssertionError("citation resolver did not inspect every reference")
    return index, audit, corpus


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _replace_directory(staged: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=f".{destination.name}.previous-", dir=destination.parent))
    backup.rmdir()
    had_destination = destination.exists()
    if had_destination:
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except BaseException:
        if had_destination and backup.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _institution_graph_matches_author_inputs(
    path: Path,
    author_index: Mapping[str, Any],
) -> bool:
    """Return whether a sibling institution graph is safe to preserve.

    The author build transaction owns the shared graph directory.  A valid
    institution artifact may contain one additional institution-catalog input,
    but every author semantic input and the Markdown source digest must match
    exactly.  Malformed or stale artifacts fail closed and are removed by the
    directory replacement.
    """

    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("source_digest") != author_index["source_digest"]:
        return False
    if payload.get("schema_version") != 1:
        return False
    if not isinstance(payload.get("institutions"), list):
        return False
    if not isinstance(payload.get("papers"), dict):
        return False
    if not isinstance(payload.get("citation_edges"), list):
        return False
    institution_inputs = payload.get("build_inputs")
    if not isinstance(institution_inputs, dict):
        return False
    if "institution_identity_overrides_sha256" not in institution_inputs:
        return False
    if not all(
        institution_inputs.get(key) == value
        for key, value in author_index["build_inputs"].items()
    ):
        return False
    digest = hashlib.sha256()
    for key, value in sorted(institution_inputs.items()):
        if value is not None and not isinstance(value, str):
            return False
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update((value if value is not None else "none").encode("ascii"))
        digest.update(b"\0")
    return payload.get("build_input_digest") == digest.hexdigest()


def _reference_graph_matches_author_inputs(path: Path, author_index: Mapping[str, Any]) -> bool:
    """Preserve only a fresh reference view during the shared-directory swap."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != 1 or payload.get("graph_type") != "reference":
        return False
    if any(payload.get(key) != author_index[key] for key in ("source_digest", "papers", "citation_edges")):
        return False
    inputs = payload.get("build_inputs")
    method = payload.get("method")
    if not isinstance(inputs, dict) or not isinstance(method, dict) or not isinstance(payload.get("similarity_edges"), list):
        return False
    if not all(inputs.get(key) == value for key, value in author_index["build_inputs"].items()):
        return False
    builder = Path(__file__).with_name("build_reference_graph.py")
    if not builder.is_file() or inputs.get("reference_builder_sha256") != _file_sha256(builder):
        return False
    if "application_taxonomy_sha256" in inputs:
        taxonomy = PROJECT_ROOT / "Analyzer/LV/Techniques/tech_taxonomy.json"
        if not taxonomy.is_file() or inputs["application_taxonomy_sha256"] != _file_sha256(taxonomy):
            return False
    method_bytes = json.dumps(method, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if inputs.get("similarity_method_sha256") != hashlib.sha256(method_bytes).hexdigest():
        return False
    digest = hashlib.sha256()
    for key, value in sorted(inputs.items()):
        if value is not None and (not isinstance(value, str) or not value.isascii()):
            return False
        digest.update(key.encode("utf-8") + b"\0")
        digest.update((value if value is not None else "none").encode("ascii") + b"\0")
    return payload.get("build_input_digest") == digest.hexdigest()


def write_outputs(
    index: Mapping[str, Any],
    audit: Mapping[str, Any],
    corpus: Mapping[str, Mapping[str, ParsedPaper]],
    *,
    output_dir: Path = DEFAULT_OUTPUT,
    audit_path: Path = DEFAULT_AUDIT,
) -> None:
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.build-", dir=output_dir.parent))
    try:
        _write_json(stage / "author-graph.json", index)
        existing_institution_graph = output_dir / "institution-graph.json"
        if _institution_graph_matches_author_inputs(existing_institution_graph, index):
            shutil.copyfile(existing_institution_graph, stage / "institution-graph.json")
        existing_reference_graph = output_dir / "reference-graph.json"
        if _reference_graph_matches_author_inputs(existing_reference_graph, index):
            shutil.copyfile(existing_reference_graph, stage / "reference-graph.json")
        for language in ("ko", "en"):
            for identifier, paper in sorted(corpus[language].items()):
                destination = stage / "papers" / paper.record_id
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(paper.path, destination / f"{language}.md")
        expected_ids = {paper["record_id"] for paper in index["papers"].values()}
        for language in ("ko", "en"):
            copied = {path.parent.name for path in (stage / "papers").glob(f"KNOW-DOMP-*/{language}.md")}
            if copied != expected_ids:
                raise AssertionError(f"staged {language} Markdown copy set differs from graph index")
        _replace_directory(stage, output_dir)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    _write_json_atomic(audit_path.resolve(), audit)


def build(
    source_dir: Path = DEFAULT_SOURCE,
    output_dir: Path = DEFAULT_OUTPUT,
    audit_path: Path = DEFAULT_AUDIT,
    *,
    database_path: Path = DEFAULT_DATABASE,
    expected_count: int | None = None,
    overrides_path: Path | None = None,
    author_overrides_path: Path | None = None,
    check_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    index, audit, corpus = compile_graph(
        source_dir,
        database_path=database_path,
        expected_count=expected_count,
        overrides_path=overrides_path,
        author_overrides_path=author_overrides_path,
    )
    if not check_only:
        write_outputs(index, audit, corpus, output_dir=output_dir, audit_path=audit_path)
    return index, audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="directory containing KNOW-DOMP-0000001/{ko,en}.md profiles")
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLite database containing canonical KCI publication year/month",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="generated web data/graph directory")
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT, help="citation-resolution audit JSON")
    parser.add_argument("--overrides", type=Path, default=None, help="reviewed citation override JSON")
    parser.add_argument(
        "--author-overrides",
        type=Path,
        default=None,
        help="reviewed author identity override JSON",
    )
    parser.add_argument("--expected-count", type=int, default=None, help="require exactly this many bilingual papers (default: any)")
    parser.add_argument("--check-only", action="store_true", help="validate and compile without writing files")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        index, audit = build(
            source_dir=args.source,
            database_path=args.database,
            output_dir=args.output,
            audit_path=args.audit_output,
            expected_count=args.expected_count,
            overrides_path=args.overrides,
            author_overrides_path=args.author_overrides,
            check_only=args.check_only,
        )
    except (CorpusValidationError, OSError) as exc:
        print(f"author graph build failed: {exc}", file=os.sys.stderr)
        return 1
    resolution = audit["citation_resolution"]["counts"]
    action = "validated" if args.check_only else "built"
    print(
        f"Author graph {action}: {index['corpus']['paper_count']} papers, "
        f"{index['corpus']['author_identity_count']} resolved author identities, "
        f"{index['corpus']['publication_date_coverage']} publication months, "
        f"{index['corpus']['citation_edge_count']} internal citation edges, "
        f"{resolution.get('unresolved', 0)} unresolved references."
    )
    if not args.check_only:
        print(f"Index: {args.output.resolve() / 'author-graph.json'}")
        print(f"Audit: {args.audit_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
