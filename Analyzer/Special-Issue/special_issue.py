#!/usr/bin/env python3
"""Collect KCI BibTeX evidence and classify Special Issue papers.

The classifier deliberately implements one rule only::

    a present, non-empty ``Special-Number`` field -> Special Issue
    a present, empty ``Special-Number`` field     -> Not Special Issue
    a missing/duplicated/malformed field          -> unresolved

KCI exposes the requested evidence through the authenticated
``서지정보 내보내기 -> BibTex`` UI.  Credentials are never accepted by this
program.  With ``--interactive-login`` the operator enters them directly in a
dedicated headed Chromium profile, acknowledges completion with a bare Enter,
and collection continues in that same browser context.

Run from any directory with::

    .venv/bin/python Analyzer/Special-Issue/special_issue.py \
        --allow-authenticated-collection --interactive-login

Successful evidence is checkpointed as ``BibTex/ART#########.bib`` plus a
small hash-bound cache record.  Interrupted runs can therefore resume without
re-exporting papers already verified against their exact KCI detail page.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo


SPECIAL_ISSUE_DIR = Path(__file__).resolve().parent
ANALYZER_DIR = SPECIAL_ISSUE_DIR.parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.storage import write_bytes_atomic, write_json_atomic
from Util.logger import ProgressLogger


DEFAULT_DATABASE = REPOSITORY_ROOT / "kci_pipeline.db"
DEFAULT_OUTPUT = SPECIAL_ISSUE_DIR / "special_list.xlsx"
DEFAULT_BIBTEX_DIR = SPECIAL_ISSUE_DIR / "BibTex"
DEFAULT_CACHE_DIR = SPECIAL_ISSUE_DIR / ".cache"
DEFAULT_PROFILE_MARKER = ".forensics-paper-kci-bibtex-profile.json"
DEFAULT_MAX_BIBTEX_BYTES = 256 * 1024
DEFAULT_NAVIGATION_TIMEOUT_SECONDS = 60.0
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60.0
DEFAULT_MIN_INTERVAL_SECONDS = 2.0
DEFAULT_RETRIES = 2
CACHE_SCHEMA_VERSION = "1.0"
REPORT_SCHEMA_VERSION = "1.0"
PROFILE_SCHEMA_VERSION = 1
PROFILE_PURPOSE = "forensics-paper-kci-bibtex-browser"
SEOUL = ZoneInfo("Asia/Seoul")

KCI_ORIGIN = "https://www.kci.go.kr"
KCI_HOST = "www.kci.go.kr"
KCI_DETAIL_PATH = "/kciportal/ci/sereArticleSearch/ciSereArtiView.kci"
KCI_EXPORT_PATH = "/kciportal/po/search/poArtiSearListExport.kci"
KCI_SAMPLE_ART_ID = "ART002830209"

_ART_ID_RE = re.compile(r"ART[0-9]{9}\Z")
_BIBTEX_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_INVALID_TEXT_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)
_INVALID_EXCEL_XML_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]"
)
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_EXPECTED_BIBTEX_FIELDS = {
    "volume": "volume",
    "number": "number",
    "special-number": "special_number",
    "pages": "pages",
    "article-number": "article_number",
    "article-no": "article_number",
    "da": "bibtex_date",
}
_RESULT_STATUSES = frozenset({"classified", "field_missing", "failed"})
_RUN_FATAL_ERROR_CODES = frozenset(
    {
        "authentication_required",
        "browser_dependency_missing",
        "browser_not_installed",
        "browser_launch_failed",
        "invalid_browser_profile",
        "profile_in_use",
        "interactive_login_cancelled",
        "interactive_login_acknowledgement_rejected",
        "export_dom_changed",
    }
)


class SpecialIssueError(RuntimeError):
    """Base exception carrying a stable, credential-free error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvalidBibtexError(SpecialIssueError):
    """Downloaded evidence is not bounded, well-formed KCI BibTeX."""


class KciBrowserError(SpecialIssueError):
    """Sanitized browser failure; URLs and authentication state are omitted."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.retryable = retryable


_EXCLUDED_PAPERS_FILE = Path(__file__).resolve().parents[2] / "Processor" / "refs" / "excluded_papers.json"


def _removed_paper_count() -> int:
    """How many papers were removed from the corpus after review (0 when none)."""

    try:
        payload = json.loads(_EXCLUDED_PAPERS_FILE.read_text(encoding="utf-8"))
        return len(payload.get("papers", []))
    except (OSError, ValueError, AttributeError):
        return 0


@dataclass(frozen=True, slots=True)
class KciPaper:
    paper_index: int
    database_id: int
    source_id: str
    title: str
    title_kor: str | None
    title_eng: str | None
    doi_raw: str | None
    doi: str | None
    journal_id: str | None
    institution_id: str | None
    issue_id: str | None
    publication_year: int | None

    @property
    def fingerprint(self) -> str:
        """Digest of the paper's bibliographic identity.

        ``paper_index`` and ``database_id`` are positions in the current
        database, not properties of the paper: removing one paper shifts every
        later position, which must not invalidate the verified evidence of all
        the others.
        """

        identity = {key: value for key, value in asdict(self).items() if key not in ("paper_index", "database_id")}
        payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _positional_fingerprint(self, paper_index: int) -> str:
        payload = json.dumps(
            {**asdict(self), "paper_index": paper_index}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def positional_fingerprint(self) -> str:
        """The digest written by earlier versions (it included the row position)."""

        return self._positional_fingerprint(self.paper_index)

    def matches_fingerprint(self, recorded: Any) -> bool:
        """Accept the identity digest, or a positional digest written before later removals.

        A positional record carries the index the paper had when it was written:
        its current index plus however many earlier papers were removed since,
        which is at most the length of the exclusion list.
        """

        if not isinstance(recorded, str):
            return False
        if recorded == self.fingerprint:
            return True
        return any(
            recorded == self._positional_fingerprint(self.paper_index + shift)
            for shift in range(_removed_paper_count() + 1)
        )

    @property
    def candidate_titles(self) -> tuple[str, ...]:
        values: list[str] = []
        for value in (self.title, self.title_kor, self.title_eng):
            if value and value.strip() and value.strip() not in values:
                values.append(value.strip())
                repaired = _repair_adjacent_duplicate_title(value)
                if repaired and repaired not in values:
                    values.append(repaired)
        return tuple(values)


@dataclass(frozen=True, slots=True)
class ParsedBibtex:
    volume: str | None
    number: str | None
    special_number: str | None
    pages: str | None
    article_number: str | None
    bibtex_date: str | None
    special_number_field_present: bool
    is_special_issue: bool | None

    def __post_init__(self) -> None:
        for name in (
            "volume",
            "number",
            "special_number",
            "pages",
            "article_number",
            "bibtex_date",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")
        if self.special_number_field_present:
            if self.special_number is None:
                raise ValueError("classification_rule_requires_special_number_value")
            expected = bool(self.special_number.strip())
            if self.is_special_issue is not expected:
                raise ValueError("classification_rule_mismatch")
        elif self.special_number is not None or self.is_special_issue is not None:
            raise ValueError("classification_rule_missing_field_mismatch")

    @property
    def classification(self) -> str:
        if self.is_special_issue is True:
            return "Special Issue"
        if self.is_special_issue is False:
            return "Not Special Issue"
        return "Unresolved"


@dataclass(frozen=True, slots=True)
class PaperSpecialIssueResult:
    paper: KciPaper
    status: str
    collected_at: str
    attempts: int
    metadata: ParsedBibtex | None = None
    identity_verified: bool = False
    bibtex_file: str | None = None
    bibtex_sha256: str | None = None
    bibtex_size: int | None = None
    cache_hit: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise ValueError(f"unsupported result status: {self.status}")
        if self.attempts < 0:
            raise ValueError("attempts must be non-negative")
        if self.status == "classified":
            if (
                self.metadata is None
                or self.metadata.is_special_issue is None
                or not self.metadata.special_number_field_present
                or not self.identity_verified
                or not self.bibtex_file
                or not self.bibtex_sha256
                or self.bibtex_size is None
            ):
                raise ValueError("classified result requires verified BibTeX evidence")
        elif self.status == "field_missing":
            if (
                self.metadata is None
                or self.metadata.special_number_field_present
                or self.metadata.is_special_issue is not None
            ):
                raise ValueError("field_missing result requires parsed missing field")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed result requires an error code")

    @property
    def is_special_issue(self) -> bool | None:
        return self.metadata.is_special_issue if self.metadata else None


@dataclass(frozen=True, slots=True)
class CollectionRun:
    results: tuple[PaperSpecialIssueResult, ...]
    total_database_papers: int
    selection_complete: bool
    network_requests: int
    cache_hits: int

    @property
    def classification_complete(self) -> bool:
        return bool(
            self.selection_complete
            and len(self.results) == self.total_database_papers
            and all(result.status == "classified" for result in self.results)
        )


class BibtexBrowserSession(Protocol):
    request_count: int

    def fetch(self, paper: KciPaper) -> bytes:
        """Download VP-only BibTeX after exact detail-page identity checks."""

    def close(self) -> None:
        """Close browser resources on their owner thread."""


def default_kci_profile_dir() -> Path:
    configured = os.getenv("XDG_DATA_HOME")
    base = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else Path.home() / ".local" / "share"
    )
    return (base / "forensics-paper" / "kci-bibtex-browser-profile").resolve()


def build_detail_url(source_id: str) -> str:
    if not isinstance(source_id, str) or _ART_ID_RE.fullmatch(source_id) is None:
        raise ValueError("invalid KCI ART ID")
    return f"{KCI_ORIGIN}{KCI_DETAIL_PATH}?" + urlencode(
        {"sereArticleSearchBean.artiId": source_id}
    )


def validate_detail_url(url: object, *, expected_source_id: str) -> str:
    if _ART_ID_RE.fullmatch(expected_source_id) is None:
        raise ValueError("invalid expected KCI ART ID")
    if not isinstance(url, str) or not url or len(url) > 2_048:
        raise ValueError("invalid KCI detail URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError("invalid KCI detail URL") from exc
    if not (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == KCI_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == KCI_DETAIL_PATH
        and not parsed.fragment
        and pairs == [("sereArticleSearchBean.artiId", expected_source_id)]
    ):
        raise ValueError("invalid KCI detail URL")
    return build_detail_url(expected_source_id)


def _is_exact_export_url(url: object) -> bool:
    if not isinstance(url, str) or not url or len(url) > 2_048:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == KCI_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == KCI_EXPORT_PATH
        and not parsed.fragment
    )


def parse_kci_bibtex(
    content: bytes | str,
    *,
    max_bytes: int = DEFAULT_MAX_BIBTEX_BYTES,
) -> ParsedBibtex:
    """Parse one bounded KCI VP-only BibTeX record without a BibTeX library.

    KCI emits a non-standard keyless record such as
    ``@article{Volume={16},...}``.  A small brace-aware state machine is used
    so ``Volume`` is not mistaken for a citation key.  Standard records with
    a citation key are accepted as well, which makes cached evidence resilient
    to a harmless KCI formatter change.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(content, bytes):
        if not content or len(content) > max_bytes:
            raise InvalidBibtexError(
                "empty_bibtex" if not content else "bibtex_too_large"
            )
        text = _decode_bibtex(content)
    elif isinstance(content, str):
        if not content or len(content.encode("utf-8")) > max_bytes:
            raise InvalidBibtexError(
                "empty_bibtex" if not content else "bibtex_too_large"
            )
        text = content.removeprefix("\ufeff")
    else:
        raise InvalidBibtexError("invalid_bibtex_type")

    if _INVALID_TEXT_CONTROL_RE.search(text):
        raise InvalidBibtexError("unsafe_bibtex_controls")
    lowered = text.lstrip().casefold()
    if lowered.startswith(("<!doctype", "<html", "<?xml")) or "<html" in lowered[:512]:
        raise InvalidBibtexError("html_instead_of_bibtex")

    scanner = _BibtexScanner(text)
    fields = scanner.parse()
    known: dict[str, list[str]] = {}
    for raw_name, raw_value in fields:
        key = raw_name.casefold()
        known.setdefault(key, []).append(_normalize_bibtex_value(raw_value))

    duplicates = [name for name, values in known.items() if len(values) != 1]
    if duplicates:
        raise InvalidBibtexError("duplicate_bibtex_field")

    mapped: dict[str, str | None] = {
        "volume": None,
        "number": None,
        "special_number": None,
        "pages": None,
        "article_number": None,
        "bibtex_date": None,
    }
    for key, values in known.items():
        destination = _EXPECTED_BIBTEX_FIELDS.get(key)
        if destination is not None:
            mapped[destination] = values[0]

    special_present = "special-number" in known
    special_value = mapped["special_number"] if special_present else None
    is_special = bool(special_value.strip()) if special_value is not None else (
        False if special_present else None
    )
    return ParsedBibtex(
        volume=mapped["volume"],
        number=mapped["number"],
        special_number=special_value,
        pages=mapped["pages"],
        article_number=mapped["article_number"],
        bibtex_date=mapped["bibtex_date"],
        special_number_field_present=special_present,
        is_special_issue=is_special,
    )


class _BibtexScanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.length = len(text)
        self.position = 0

    def parse(self) -> list[tuple[str, str]]:
        self._skip_space()
        self._expect("@")
        entry_type = self._read_identifier().casefold()
        if entry_type != "article":
            raise InvalidBibtexError("unexpected_bibtex_entry_type")
        self._skip_space()
        self._expect("{")
        self._skip_space()

        fields: list[tuple[str, str]] = []
        if self._peek() == "}":
            raise InvalidBibtexError("empty_bibtex_record")

        # KCI's formatter omits a citation key.  If the first identifier is
        # not followed by '=', treat text through the first comma as a normal
        # BibTeX citation key and then begin field parsing.
        checkpoint = self.position
        first_identifier = self._read_identifier()
        self._skip_space()
        if self._peek() == "=":
            self.position = checkpoint
        else:
            self.position = checkpoint
            citation_key = self._read_until_top_level_comma()
            if not citation_key.strip() or any(c in citation_key for c in "{}@"):
                raise InvalidBibtexError("invalid_bibtex_citation_key")
            self._expect(",")
            self._skip_space()

        while True:
            if len(fields) >= 64:
                raise InvalidBibtexError("too_many_bibtex_fields")
            if self._peek() == "}":
                self.position += 1
                break
            name = self._read_identifier()
            self._skip_space()
            self._expect("=")
            self._skip_space()
            value = self._read_braced_value()
            fields.append((name, value))
            self._skip_space()
            marker = self._peek()
            if marker == ",":
                self.position += 1
                self._skip_space()
                continue
            if marker == "}":
                self.position += 1
                break
            raise InvalidBibtexError("malformed_bibtex_separator")

        self._skip_space()
        if self.position != self.length:
            raise InvalidBibtexError("trailing_bibtex_content")
        if not fields:
            raise InvalidBibtexError("empty_bibtex_record")
        return fields

    def _read_identifier(self) -> str:
        start = self.position
        while self.position < self.length and (
            self.text[self.position].isalnum()
            or self.text[self.position] in {"_", "-"}
        ):
            self.position += 1
        value = self.text[start : self.position]
        if not value or _BIBTEX_KEY_RE.fullmatch(value) is None:
            raise InvalidBibtexError("invalid_bibtex_field_name")
        return value

    def _read_braced_value(self) -> str:
        self._expect("{")
        depth = 1
        start = self.position
        output: list[str] = []
        while self.position < self.length:
            character = self.text[self.position]
            if character == "\\":
                if self.position + 1 >= self.length:
                    raise InvalidBibtexError("malformed_bibtex_escape")
                output.append(character)
                output.append(self.text[self.position + 1])
                self.position += 2
                continue
            if character == "{":
                depth += 1
                if depth > 32:
                    raise InvalidBibtexError("bibtex_nesting_too_deep")
                output.append(character)
                self.position += 1
                continue
            if character == "}":
                depth -= 1
                if depth == 0:
                    self.position += 1
                    return "".join(output)
                output.append(character)
                self.position += 1
                continue
            output.append(character)
            self.position += 1
        del start
        raise InvalidBibtexError("unclosed_bibtex_value")

    def _read_until_top_level_comma(self) -> str:
        start = self.position
        while self.position < self.length and self.text[self.position] not in ",}":
            self.position += 1
        return self.text[start : self.position]

    def _skip_space(self) -> None:
        while self.position < self.length and self.text[self.position].isspace():
            self.position += 1

    def _peek(self) -> str | None:
        return self.text[self.position] if self.position < self.length else None

    def _expect(self, expected: str) -> None:
        if self._peek() != expected:
            raise InvalidBibtexError("malformed_bibtex_structure")
        self.position += 1


def _decode_bibtex(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise InvalidBibtexError("unsupported_bibtex_encoding")


def _normalize_bibtex_value(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _normalize_identity_title(value: str) -> str:
    """Normalize only an orphaned terminal subtitle separator.

    One KCI detail page appends ``" -"`` although the same KCI API record
    ends immediately before it.  This is not fuzzy matching: every title
    character must remain exact and only a separator with no following
    subtitle may be removed.
    """

    normalized = _normalize_title(value)
    return re.sub(r"(?:\s+(?:-|–|—|:|：))+\Z", "", normalized).rstrip()


def _repair_adjacent_duplicate_title(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if len(normalized) < 16 or len(normalized) % 2:
        return None
    midpoint = len(normalized) // 2
    return normalized[:midpoint].strip() if normalized[:midpoint] == normalized[midpoint:] else None


def load_kci_papers(database: Path = DEFAULT_DATABASE) -> list[KciPaper]:
    """Load and validate all ``kci_papers`` rows in stable DB id order."""

    path = Path(database).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise SpecialIssueError("database_not_found_or_unsafe")
    uri = path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        raise SpecialIssueError("database_open_failed") from None
    try:
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(kci_papers)")
        }
        required = {
            "id",
            "source_id",
            "title",
            "title_kor",
            "title_eng",
            "doi",
            "journal_id",
            "institution_id",
            "issue_id",
            "publication_year",
        }
        if not required.issubset(columns):
            raise SpecialIssueError("invalid_kci_papers_schema")
        rows = connection.execute(
            """
            SELECT id, source_id, title, title_kor, title_eng, doi,
                   journal_id, institution_id, issue_id, publication_year
              FROM kci_papers
             ORDER BY id
            """
        ).fetchall()
    except sqlite3.Error:
        raise SpecialIssueError("database_read_failed") from None
    finally:
        connection.close()

    if not rows:
        raise SpecialIssueError("empty_kci_papers_table")
    papers: list[KciPaper] = []
    seen_ids: set[int] = set()
    seen_sources: set[str] = set()
    for index, row in enumerate(rows, start=1):
        database_id = row[0]
        source_id = row[1]
        if isinstance(database_id, bool) or not isinstance(database_id, int):
            raise SpecialIssueError("invalid_database_id")
        if database_id in seen_ids:
            raise SpecialIssueError("duplicate_database_id")
        if not isinstance(source_id, str) or _ART_ID_RE.fullmatch(source_id) is None:
            raise SpecialIssueError("invalid_kci_source_id")
        if source_id in seen_sources:
            raise SpecialIssueError("duplicate_kci_source_id")
        title_values = [value.strip() for value in row[2:5] if isinstance(value, str) and value.strip()]
        if not title_values:
            raise SpecialIssueError("missing_paper_title")
        doi_raw = row[5].strip() if isinstance(row[5], str) and row[5].strip() else None
        doi: str | None = None
        if doi_raw:
            try:
                doi = normalize_doi(doi_raw)
            except InvalidDoiError:
                doi = None
        publication_year = row[9]
        if publication_year is not None and (
            isinstance(publication_year, bool) or not isinstance(publication_year, int)
        ):
            raise SpecialIssueError("invalid_publication_year")
        paper = KciPaper(
            paper_index=index,
            database_id=database_id,
            source_id=source_id,
            title=title_values[0],
            title_kor=row[3].strip() if isinstance(row[3], str) and row[3].strip() else None,
            title_eng=row[4].strip() if isinstance(row[4], str) and row[4].strip() else None,
            doi_raw=doi_raw,
            doi=doi,
            journal_id=row[6].strip() if isinstance(row[6], str) and row[6].strip() else None,
            institution_id=row[7].strip() if isinstance(row[7], str) and row[7].strip() else None,
            issue_id=row[8].strip() if isinstance(row[8], str) and row[8].strip() else None,
            publication_year=publication_year,
        )
        papers.append(paper)
        seen_ids.add(database_id)
        seen_sources.add(source_id)
    return papers


class SpecialIssueCache:
    """Hash-bind a verified BibTeX artifact to one exact DB paper snapshot."""

    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        bibtex_dir: Path = DEFAULT_BIBTEX_DIR,
        *,
        max_bibtex_bytes: int = DEFAULT_MAX_BIBTEX_BYTES,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.bibtex_dir = Path(bibtex_dir)
        self.max_bibtex_bytes = max_bibtex_bytes

    def load(self, paper: KciPaper) -> PaperSpecialIssueResult | None:
        cache_path = self._cache_path(paper)
        bibtex_path = self._bibtex_path(paper)
        if any(path.is_symlink() for path in (cache_path, bibtex_path)):
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            content = _read_regular_file(bibtex_path, self.max_bibtex_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError, SpecialIssueError):
            return None
        if not isinstance(payload, dict):
            return None
        digest = hashlib.sha256(content).hexdigest()
        if not (
            payload.get("schema_version") == CACHE_SCHEMA_VERSION
            and payload.get("source_id") == paper.source_id
            and paper.matches_fingerprint(payload.get("paper_fingerprint"))
            and payload.get("bibtex_file") == bibtex_path.name
            and payload.get("bibtex_sha256") == digest
            and payload.get("bibtex_size") == len(content)
            and payload.get("identity_verified") is True
        ):
            return None
        try:
            metadata = parse_kci_bibtex(content, max_bytes=self.max_bibtex_bytes)
        except InvalidBibtexError:
            return None
        if metadata.is_special_issue is None:
            return None
        if payload.get("metadata") != asdict(metadata):
            return None
        collected_at = payload.get("collected_at")
        if not isinstance(collected_at, str) or not collected_at:
            return None
        return PaperSpecialIssueResult(
            paper=paper,
            status="classified",
            collected_at=collected_at,
            attempts=0,
            metadata=metadata,
            identity_verified=True,
            bibtex_file=bibtex_path.name,
            bibtex_sha256=digest,
            bibtex_size=len(content),
            cache_hit=True,
        )

    def save(
        self,
        result: PaperSpecialIssueResult,
        bibtex_content: bytes,
    ) -> PaperSpecialIssueResult:
        if result.status != "classified" or result.metadata is None:
            raise ValueError("only classified evidence can be cached")
        parsed = parse_kci_bibtex(
            bibtex_content,
            max_bytes=self.max_bibtex_bytes,
        )
        if parsed != result.metadata:
            raise ValueError("result metadata does not match BibTeX")
        self._ensure_directories()
        bibtex_path = self._bibtex_path(result.paper)
        cache_path = self._cache_path(result.paper)
        canonical = _canonical_bibtex_bytes(bibtex_content)
        write_bytes_atomic(bibtex_path, canonical)
        digest = hashlib.sha256(canonical).hexdigest()
        saved = replace(
            result,
            bibtex_file=bibtex_path.name,
            bibtex_sha256=digest,
            bibtex_size=len(canonical),
            cache_hit=False,
        )
        write_json_atomic(
            cache_path,
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "source_id": result.paper.source_id,
                "paper_fingerprint": result.paper.fingerprint,
                "bibtex_file": bibtex_path.name,
                "bibtex_sha256": digest,
                "bibtex_size": len(canonical),
                "identity_verified": True,
                "metadata": asdict(result.metadata),
                "collected_at": result.collected_at,
            },
        )
        os.chmod(cache_path, 0o600)
        return saved

    def invalidate(self, paper: KciPaper) -> None:
        # Force mode removes only this collector's two exact ART-ID files.
        self._cache_path(paper).unlink(missing_ok=True)
        self._bibtex_path(paper).unlink(missing_ok=True)

    def _ensure_directories(self) -> None:
        for path in (self.cache_dir, self.bibtex_dir):
            if path.exists() and (not path.is_dir() or path.is_symlink()):
                raise SpecialIssueError("unsafe_cache_directory")
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)

    def _cache_path(self, paper: KciPaper) -> Path:
        return self.cache_dir / f"{paper.source_id}.json"

    def _bibtex_path(self, paper: KciPaper) -> Path:
        return self.bibtex_dir / f"{paper.source_id}.bib"


def _canonical_bibtex_bytes(content: bytes) -> bytes:
    text = _decode_bibtex(content)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    return normalized.encode("utf-8")


class _ProfileLock:
    def __init__(self, profile_dir: Path) -> None:
        self.path = profile_dir / ".forensics-paper-session.lock"
        self.handle: Any = None

    def acquire(self) -> None:
        if self.handle is not None:
            return
        if self.path.is_symlink():
            raise KciBrowserError("invalid_browser_profile")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        handle: Any = None
        try:
            descriptor = os.open(self.path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "a+", encoding="ascii")
            descriptor = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if handle is not None:
                handle.close()
            if descriptor is not None:
                os.close(descriptor)
            raise KciBrowserError("profile_in_use") from None
        except OSError:
            if handle is not None:
                handle.close()
            if descriptor is not None:
                os.close(descriptor)
            raise KciBrowserError("invalid_browser_profile") from None
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def initialize_kci_profile_dir(
    profile_dir: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    raw = Path(profile_dir).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValueError("KCI browser profile must be an absolute non-symlink path")
    resolved = raw.resolve(strict=False)
    repository = Path(repository_root).resolve()
    if resolved == Path(resolved.anchor) or _is_relative_to(resolved, repository):
        raise ValueError("KCI browser profile must be outside the repository")
    lowered_parts = {part.casefold() for part in resolved.parts}
    if {"google-chrome", "chromium", "firefox"} & lowered_parts:
        raise ValueError("personal default browser profiles are not supported")
    marker = resolved / DEFAULT_PROFILE_MARKER
    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink():
            raise ValueError("unsafe KCI browser profile path")
        if any(resolved.iterdir()) and not marker.is_file():
            raise ValueError("refusing to reuse an unmarked browser profile")
    else:
        resolved.mkdir(parents=True, mode=0o700)
    os.chmod(resolved, 0o700)
    _require_private_owned_path(resolved, directory=True)
    if marker.is_symlink():
        raise ValueError("unsafe KCI browser profile marker")
    if not marker.exists():
        write_json_atomic(
            marker,
            {"schema_version": PROFILE_SCHEMA_VERSION, "purpose": PROFILE_PURPOSE},
        )
        os.chmod(marker, 0o600)
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid KCI browser profile marker") from exc
    _require_private_owned_path(marker, directory=False)
    if value != {"schema_version": PROFILE_SCHEMA_VERSION, "purpose": PROFILE_PURPOSE}:
        raise ValueError("invalid KCI browser profile marker")
    return resolved


@dataclass(frozen=True, slots=True)
class KciBrowserConfig:
    permission_granted: bool
    profile_dir: Path = field(repr=False, compare=False)
    interactive_login: bool = False
    headless: bool = True
    input_fn: Callable[[str], str] = field(default=input, repr=False, compare=False)
    # > 0: instead of waiting for Enter on the terminal, watch the browser until the
    # signed-in state appears (for runs whose terminal nobody can type into).
    login_wait_seconds: float = 0.0
    navigation_timeout_seconds: float = DEFAULT_NAVIGATION_TIMEOUT_SECONDS
    download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    max_bibtex_bytes: int = DEFAULT_MAX_BIBTEX_BYTES

    def __post_init__(self) -> None:
        if not self.permission_granted:
            raise ValueError("authenticated KCI collection requires explicit permission")
        for name in (
            "navigation_timeout_seconds",
            "download_timeout_seconds",
            "min_interval_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_bibtex_bytes < 1:
            raise ValueError("max_bibtex_bytes must be positive")


class PlaywrightKciBibtexSession:
    """One sequential authenticated KCI UI session with exact DOM checks."""

    def __init__(self, config: KciBrowserConfig) -> None:
        self.config = config
        self.profile_dir = initialize_kci_profile_dir(config.profile_dir)
        self.profile_lock = _ProfileLock(self.profile_dir)
        self.playwright: Any = None
        self.context: Any = None
        self.page: Any = None
        self.owner_thread: int | None = None
        self.next_allowed = 0.0
        self.request_count = 0
        self.dialog_code: str | None = None

    def fetch(self, paper: KciPaper) -> bytes:
        self._require_owner_thread()
        self._ensure_started()
        assert self.page is not None
        self._wait_for_interval()
        self.dialog_code = None
        url = build_detail_url(paper.source_id)
        try:
            response = self.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=_seconds_to_ms(self.config.navigation_timeout_seconds),
            )
            self.request_count += 1
            if response is None or response.status >= 400:
                raise KciBrowserError("detail_navigation_failed", retryable=True)
            validate_detail_url(self.page.url, expected_source_id=paper.source_id)
            self._validate_detail_identity(paper)
            content = self._export_current_page(paper)
            metadata = parse_kci_bibtex(
                content,
                max_bytes=self.config.max_bibtex_bytes,
            )
            # Parsing here ensures no HTML/empty/auth response reaches storage.
            if not metadata.special_number_field_present:
                return content
            return content
        except KciBrowserError:
            raise
        except InvalidBibtexError:
            raise
        except ValueError:
            raise KciBrowserError("unsafe_or_changed_kci_detail") from None
        except Exception:
            if self.dialog_code:
                raise KciBrowserError(self.dialog_code) from None
            if self._login_page_open():
                raise KciBrowserError("authentication_required") from None
            raise KciBrowserError("browser_operation_failed", retryable=True) from None

    def close(self) -> None:
        if self.owner_thread is not None:
            self._require_owner_thread()
        if self.context is not None:
            with contextlib.suppress(Exception):
                self.context.close()
        if self.playwright is not None:
            with contextlib.suppress(Exception):
                self.playwright.stop()
        self.context = None
        self.page = None
        self.playwright = None
        self.owner_thread = None
        self.profile_lock.release()

    def _ensure_started(self) -> None:
        if self.context is not None:
            return
        self.profile_lock.acquire()
        playwright: Any = None
        context: Any = None
        try:
            _configure_user_playwright_libraries()
            sync_playwright = _load_sync_playwright()
            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False if self.config.interactive_login else self.config.headless,
                accept_downloads=True,
                service_workers="block",
                args=["--restore-last-session"] if self.config.interactive_login else [],
            )
            pages = context.pages
            page = pages[0] if pages else context.new_page()
            self.playwright = playwright
            self.context = context
            self.page = page
            self.owner_thread = threading.get_ident()
            if self.config.interactive_login:
                self._perform_interactive_login()
            self._select_collection_page()
            assert self.page is not None
            self.page.on("dialog", self._handle_dialog)
        except (KciBrowserError, ValueError):
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if playwright is not None:
                with contextlib.suppress(Exception):
                    playwright.stop()
            self.context = None
            self.page = None
            self.playwright = None
            self.owner_thread = None
            self.profile_lock.release()
            raise
        except Exception as exc:
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if playwright is not None:
                with contextlib.suppress(Exception):
                    playwright.stop()
            self.context = None
            self.page = None
            self.playwright = None
            self.owner_thread = None
            self.profile_lock.release()
            lowered = str(exc).casefold()
            code = (
                "browser_not_installed"
                if "executable doesn't exist" in lowered or "playwright install" in lowered
                else "browser_launch_failed"
            )
            raise KciBrowserError(code) from None

    def _close_restored_pages(self) -> None:
        """Keep one tab: session restore reopens every window of the previous run.

        Those windows include the finished login popup and detail pages drawn
        from the browser cache in their old signed-in state; both would be
        mistaken for the current login state.
        """

        assert self.context is not None and self.page is not None
        for page in list(self.context.pages):
            if page is not self.page and not page.is_closed():
                with contextlib.suppress(Exception):
                    page.close()

    def _perform_interactive_login(self) -> None:
        assert self.page is not None
        try:
            self._close_restored_pages()
            self.page.goto(
                build_detail_url(KCI_SAMPLE_ART_ID),
                wait_until="domcontentloaded",
                timeout=_seconds_to_ms(self.config.navigation_timeout_seconds),
            )
            if self.config.login_wait_seconds > 0:
                self._wait_until_signed_in()
                acknowledgement = ""
            else:
                acknowledgement = self.config.input_fn(
                "열린 KCI 상세 화면에서 '서지정보 내보내기 > BibTex'를 눌러 "
                "로그인 창을 연 뒤, 브라우저 안에서만 로그인을 완료하세요. "
                "비밀번호·쿠키를 이 터미널에 입력하지 마십시오. 완료 후 아무 "
                "값도 입력하지 말고 Enter만 누르세요: "
            )
        except (KeyboardInterrupt, EOFError):
            raise KciBrowserError("interactive_login_cancelled") from None
        except KciBrowserError:
            raise
        except Exception:
            raise KciBrowserError("interactive_login_failed") from None
        if not isinstance(acknowledgement, str) or acknowledgement != "":
            del acknowledgement
            raise KciBrowserError("interactive_login_acknowledgement_rejected")
        del acknowledgement
        # Login status is rendered into fnArtiExportView by the server.  The
        # old detail page must not be reused after the popup login completes.
        self._select_collection_page()
        assert self.page is not None
        self.page.goto(
            build_detail_url(KCI_SAMPLE_ART_ID),
            wait_until="domcontentloaded",
            timeout=_seconds_to_ms(self.config.navigation_timeout_seconds),
        )

    def _wait_until_signed_in(self) -> None:
        """Watch the KCI window until a signed-in page appears; nothing is typed here."""

        assert self.context is not None
        print(
            "▶ KCI 로그인이 필요합니다. 열린 브라우저 창의 KCI 화면에서 직접 로그인해 주세요 "
            "(오른쪽 위 '로그인' 또는 '서지정보 내보내기 > BibTex'). 로그인이 확인되면 자동으로 수집을 시작합니다 "
            f"(최대 {int(self.config.login_wait_seconds // 60)}분 대기).",
            flush=True,
        )
        deadline = time.monotonic() + self.config.login_wait_seconds
        while time.monotonic() < deadline:
            time.sleep(3)
            for page in list(self.context.pages):
                try:
                    if page.is_closed() or not _is_official_kci_page(page.url):
                        continue
                    if "로그아웃" not in page.inner_text("body", timeout=5_000):
                        continue
                    # Only a page the server has just rendered counts; a cached page keeps its old state.
                    page.reload(wait_until="domcontentloaded", timeout=_seconds_to_ms(self.config.navigation_timeout_seconds))
                    if "로그아웃" in page.inner_text("body", timeout=5_000):
                        print("KCI 로그인 확인 — Special Issue 수집을 이어갑니다.", flush=True)
                        return
                except Exception:  # noqa: BLE001 - a navigating page has no body yet
                    continue
        raise KciBrowserError("interactive_login_cancelled")

    def _select_collection_page(self) -> None:
        assert self.context is not None
        candidates = [page for page in self.context.pages if not page.is_closed()]
        preferred = [
            page
            for page in candidates
            if _is_official_kci_page(page.url) and "/popup/loginForm.kci" not in page.url
        ]
        self.page = preferred[0] if preferred else (
            candidates[0] if candidates else self.context.new_page()
        )

    def _validate_detail_identity(self, paper: KciPaper) -> None:
        """Verify the page once more after a reload before calling it a different paper.

        KCI occasionally answers with a throttling or half-rendered page that
        lacks the identity fields.  That is not evidence about the paper, so
        the page is reloaded once; a real mismatch fails both times.
        """

        assert self.page is not None
        try:
            self._check_detail_identity(paper)
            return
        except KciBrowserError:
            time.sleep(3)
        response = self.page.reload(
            wait_until="domcontentloaded",
            timeout=_seconds_to_ms(self.config.navigation_timeout_seconds),
        )
        self.request_count += 1
        if response is None or response.status >= 400:
            raise KciBrowserError("detail_navigation_failed", retryable=True)
        validate_detail_url(self.page.url, expected_source_id=paper.source_id)
        self._check_detail_identity(paper)

    def _check_detail_identity(self, paper: KciPaper) -> None:
        assert self.page is not None
        source_fields = self.page.locator("input#hdnArtiId")
        if source_fields.count() != 1 or source_fields.input_value() != paper.source_id:
            raise KciBrowserError("detail_identity_mismatch")
        titles = self.page.locator("#artiTitle")
        if titles.count() < 1 or titles.count() > 10:
            raise KciBrowserError("detail_identity_mismatch")
        # KCI emits the same invalidly duplicated ``#artiTitle`` for desktop
        # and mobile layouts.  During a short responsive-style loading race,
        # both can report visible.  Accept the duplication only when every
        # rendered value is exactly the same after conservative normalization.
        observed_titles = {
            _normalize_identity_title(titles.nth(index).inner_text())
            for index in range(titles.count())
        }
        if len(observed_titles) != 1:
            raise KciBrowserError("detail_identity_mismatch")
        observed_title = next(iter(observed_titles))
        expected_titles = {
            _normalize_identity_title(value) for value in paper.candidate_titles
        }
        if observed_title not in expected_titles:
            raise KciBrowserError("detail_identity_mismatch")
        if paper.doi:
            doi_fields = self.page.locator("input#hdnDoi")
            if doi_fields.count() != 1:
                raise KciBrowserError("detail_identity_mismatch")
            observed_raw = doi_fields.input_value().strip()
            if not observed_raw:
                # KCI shows no DOI for this paper (the API record has one).  The
                # page is already tied to the paper by KCI's own article id and
                # the exact title; only a *different* DOI contradicts that.
                return
            try:
                observed_doi = normalize_doi(observed_raw)
            except InvalidDoiError:
                raise KciBrowserError("detail_identity_mismatch") from None
            if observed_doi != paper.doi:
                raise KciBrowserError("detail_identity_mismatch")

    def _export_current_page(self, paper: KciPaper) -> bytes:
        assert self.page is not None
        add_buttons = self.page.locator("#addBtn")
        if add_buttons.count() != 1 or not add_buttons.is_visible() or not add_buttons.is_enabled():
            raise KciBrowserError("export_dom_changed")
        add_buttons.click(timeout=_seconds_to_ms(self.config.navigation_timeout_seconds))
        bibtex_links = self.page.locator(
            "a[onclick=\"fnArtiExportView('BIBTEX')\"]"
        )
        if bibtex_links.count() != 1 or not bibtex_links.is_visible() or not bibtex_links.is_enabled():
            raise KciBrowserError("export_dom_changed")
        bibtex_links.click(timeout=_seconds_to_ms(self.config.navigation_timeout_seconds))
        if self.dialog_code:
            raise KciBrowserError(self.dialog_code)
        if self._login_page_open():
            raise KciBrowserError("authentication_required")

        popup = self.page.locator("#artiExportPop")
        try:
            popup.wait_for(
                state="visible",
                timeout=_seconds_to_ms(self.config.navigation_timeout_seconds),
            )
        except Exception:
            if self._login_page_open() or self.dialog_code:
                raise KciBrowserError("authentication_required") from None
            raise KciBrowserError("export_dom_changed") from None
        form = popup.locator("form#artiExportForm")
        if form.count() != 1:
            raise KciBrowserError("export_dom_changed")
        export_code = form.locator("#exportCode")
        if export_code.count() != 1 or export_code.input_value().casefold() != "bibtex":
            raise KciBrowserError("export_dom_changed")
        id_area = form.locator("#exportIdList")
        if id_area.count() != 1:
            raise KciBrowserError("export_identity_missing")
        identity_markup = id_area.inner_html()
        if len(identity_markup.encode("utf-8")) > 16 * 1024:
            raise KciBrowserError("export_identity_missing")
        exported_art_ids = set(re.findall(r"ART[0-9]{9}", identity_markup))
        if exported_art_ids != {paper.source_id}:
            # Logged-in KCI injects the selected ART identity here.  Do not
            # guess a hidden form field if that contract disappears.
            raise KciBrowserError("export_identity_missing")

        fields = form.locator("input[name='exportBean.exportFieldList']")
        if fields.count() < 1 or fields.count() > 100:
            raise KciBrowserError("export_dom_changed")
        # KCI visually replaces the native checkboxes with styled labels, so
        # Playwright's actionability check can wait forever on the hidden
        # ``input`` even though the form is ready.  Change only the validated
        # checkbox state in the live official form and verify the serialized
        # state immediately below before allowing the submit control to run.
        fields.evaluate_all(
            """
            elements => {
              for (const element of elements) {
                if (!(element instanceof HTMLInputElement) || element.type !== 'checkbox') {
                  throw new Error('unexpected export field element');
                }
                element.checked = false;
                element.dispatchEvent(new Event('change', {bubbles: true}));
              }
            }
            """
        )
        vp = form.locator("#AEB12")
        if (
            vp.count() != 1
            or vp.get_attribute("name") != "exportBean.exportFieldList"
            or vp.get_attribute("value") != "VP"
        ):
            raise KciBrowserError("export_dom_changed")
        vp.evaluate(
            """
            element => {
              if (!(element instanceof HTMLInputElement) || element.type !== 'checkbox') {
                throw new Error('unexpected VP element');
              }
              element.checked = true;
              element.dispatchEvent(new Event('change', {bubbles: true}));
            }
            """
        )
        checked = form.locator(
            "input[name='exportBean.exportFieldList']:checked"
        )
        if checked.count() != 1 or checked.get_attribute("value") != "VP":
            raise KciBrowserError("export_field_selection_failed")
        save_buttons = form.locator(
            "#exportBtnArea button[onclick='fnArtiExport()']"
        )
        if save_buttons.count() != 1 or not save_buttons.is_visible() or not save_buttons.is_enabled():
            raise KciBrowserError("export_dom_changed")

        try:
            with self.page.expect_download(
                timeout=_seconds_to_ms(self.config.download_timeout_seconds)
            ) as download_info:
                save_buttons.click(
                    timeout=_seconds_to_ms(self.config.navigation_timeout_seconds)
                )
            download = download_info.value
        except Exception:
            if self.dialog_code or self._login_page_open():
                raise KciBrowserError("authentication_required") from None
            raise KciBrowserError("native_download_not_emitted", retryable=True) from None
        try:
            if not _is_exact_export_url(download.url):
                raise KciBrowserError("unexpected_download_origin")
            if download.failure() is not None:
                raise KciBrowserError("native_download_failed", retryable=True)
            path_value = download.path()
            if path_value is None:
                raise KciBrowserError("native_download_missing", retryable=True)
            return _read_regular_file(
                Path(path_value), self.config.max_bibtex_bytes
            )
        finally:
            with contextlib.suppress(Exception):
                download.delete()

    def _handle_dialog(self, dialog: Any) -> None:
        text = unicodedata.normalize("NFKC", str(dialog.message)).casefold()
        self.dialog_code = (
            "authentication_required"
            if "로그인" in text or "login" in text or "세션" in text
            else "unexpected_kci_dialog"
        )
        with contextlib.suppress(Exception):
            dialog.accept()

    def _login_page_open(self) -> bool:
        if self.context is None:
            return False
        return any(
            not page.is_closed() and "/popup/loginForm.kci" in page.url
            for page in self.context.pages
        )

    def _wait_for_interval(self) -> None:
        now = time.monotonic()
        delay = self.next_allowed - now
        if delay > 0:
            time.sleep(delay)
        self.next_allowed = time.monotonic() + self.config.min_interval_seconds

    def _require_owner_thread(self) -> None:
        if self.owner_thread is not None and self.owner_thread != threading.get_ident():
            raise RuntimeError("KCI browser session used from a different thread")


def collect_special_issues(
    papers: Sequence[KciPaper],
    session: BibtexBrowserSession,
    *,
    cache: SpecialIssueCache,
    total_database_papers: int,
    selection_complete: bool,
    force: bool = False,
    retries: int = DEFAULT_RETRIES,
    logger: ProgressLogger | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> CollectionRun:
    if retries < 0:
        raise ValueError("retries must be non-negative")
    if len({paper.source_id for paper in papers}) != len(papers):
        raise SpecialIssueError("duplicate_input_source_id")
    timestamp = now or (lambda: datetime.now(SEOUL))
    results: list[PaperSpecialIssueResult] = []
    counters: Counter[str] = Counter()
    cache_hits = 0
    fatal_code: str | None = None
    total = len(papers)

    for position, paper in enumerate(papers, start=1):
        # Verified evidence on disk needs no browser, so an expired login must
        # not turn already classified papers into unresolved ones.  ``force``
        # only skips the lookup: the old evidence stays until a new fetch
        # succeeds and overwrites it.
        cached = None if force and fatal_code is None else cache.load(paper)
        if cached is not None:
            result = cached
            cache_hits += 1
        elif fatal_code is not None:
            result = _failed_result(
                paper,
                error_code=f"not_attempted_after_{fatal_code}",
                attempts=0,
                now=timestamp,
            )
        else:
            result, fatal_code = _collect_one(
                paper,
                session,
                cache=cache,
                retries=retries,
                sleeper=sleeper,
                now=timestamp,
            )
        results.append(result)
        counters[result.status] += 1
        if result.is_special_issue is True:
            counters["special_issue_yes"] += 1
        elif result.is_special_issue is False:
            counters["special_issue_no"] += 1
        if logger is not None:
            suffix = " (cache)" if result.cache_hit else ""
            failure = f" error={result.error_code}" if result.error_code else ""
            logger.emit(
                "KCI-SPECIAL-ISSUE",
                f"{paper.source_id} {result.status}{suffix}{failure}",
                current=position,
                total=total,
                counters=counters,
            )

    return CollectionRun(
        results=tuple(results),
        total_database_papers=total_database_papers,
        selection_complete=selection_complete,
        network_requests=session.request_count,
        cache_hits=cache_hits,
    )


def _collect_one(
    paper: KciPaper,
    session: BibtexBrowserSession,
    *,
    cache: SpecialIssueCache,
    retries: int,
    sleeper: Callable[[float], None],
    now: Callable[[], datetime],
) -> tuple[PaperSpecialIssueResult, str | None]:
    for attempt in range(1, retries + 2):
        try:
            content = session.fetch(paper)
            metadata = parse_kci_bibtex(content)
            collected_at = _iso_timestamp(now())
            if not metadata.special_number_field_present:
                return (
                    PaperSpecialIssueResult(
                        paper=paper,
                        status="field_missing",
                        collected_at=collected_at,
                        attempts=attempt,
                        metadata=metadata,
                        identity_verified=True,
                        error_code="special_number_field_missing",
                    ),
                    None,
                )
            result = PaperSpecialIssueResult(
                paper=paper,
                status="classified",
                collected_at=collected_at,
                attempts=attempt,
                metadata=metadata,
                identity_verified=True,
                bibtex_file=f"{paper.source_id}.bib",
                bibtex_sha256="pending",
                bibtex_size=len(content),
            )
            return cache.save(result, content), None
        except KciBrowserError as exc:
            if exc.retryable and attempt <= retries:
                sleeper(min(8.0, float(2 ** (attempt - 1))))
                continue
            fatal = exc.code if exc.code in _RUN_FATAL_ERROR_CODES else None
            return (
                _failed_result(
                    paper,
                    error_code=exc.code,
                    attempts=attempt,
                    now=now,
                ),
                fatal,
            )
        except InvalidBibtexError as exc:
            return (
                _failed_result(
                    paper,
                    error_code=exc.code,
                    attempts=attempt,
                    now=now,
                ),
                None,
            )
        except (OSError, ValueError):
            return (
                _failed_result(
                    paper,
                    error_code="evidence_checkpoint_failed",
                    attempts=attempt,
                    now=now,
                ),
                "evidence_checkpoint_failed",
            )
    raise AssertionError("unreachable retry loop")


def _failed_result(
    paper: KciPaper,
    *,
    error_code: str,
    attempts: int,
    now: Callable[[], datetime],
) -> PaperSpecialIssueResult:
    return PaperSpecialIssueResult(
        paper=paper,
        status="failed",
        collected_at=_iso_timestamp(now()),
        attempts=attempts,
        error_code=error_code,
    )


def export_special_issue_workbook(
    run: CollectionRun,
    output: Path = DEFAULT_OUTPUT,
    *,
    database: Path = DEFAULT_DATABASE,
    generated_at: datetime | None = None,
    canonical_when_unresolved: bool = False,
) -> Path:
    """Atomically write the workbook.

    By default the canonical path is used only when every paper classified;
    otherwise the file is diverted to ``*.partial.xlsx``.  With
    ``canonical_when_unresolved`` the canonical file is written regardless, and
    unresolved rows remain unresolved (``collection_status`` other than
    ``classified``) for downstream consumers to render as ``N/A``.
    """

    requested = Path(output)
    actual = (
        requested
        if run.classification_complete or canonical_when_unresolved
        else requested.with_name(f"{requested.stem}.partial{requested.suffix}")
    )
    rows = [_result_row(result) for result in run.results]
    summaries = _summary_rows(run, database=database, generated_at=generated_at)
    validations = _validation_rows(run)

    workbook = Workbook()
    listing = workbook.active
    listing.title = "Special Issue 목록"
    columns = list(rows[0]) if rows else _result_columns()
    _write_sheet(listing, columns, rows, table_name="SpecialIssueTable")
    summary = workbook.create_sheet("요약")
    _write_sheet(summary, ["항목", "값", "정의"], summaries, table_name="SpecialIssueSummary")
    validation = workbook.create_sheet("검증")
    _write_sheet(
        validation,
        ["검증 항목", "값", "기대값", "결과"],
        validations,
        table_name="SpecialIssueValidation",
    )
    listing.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(1, listing.max_row)}"

    actual.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{actual.stem}-", suffix=".xlsx", dir=actual.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        workbook.save(temporary)
        workbook.close()
        _validate_exported_workbook(temporary, run)
        os.replace(temporary, actual)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return actual


def _result_columns() -> list[str]:
    return [
        "paper_index",
        "database_id",
        "source_id",
        "title",
        "title_kor",
        "title_eng",
        "doi_raw",
        "doi",
        "journal_id",
        "institution_id",
        "issue_id",
        "publication_year",
        "evidence_strategy",
        "volume",
        "number",
        "special_number",
        "pages",
        "article_number",
        "bibtex_date",
        "special_number_field_present",
        "is_special_issue",
        "special_issue_classification",
        "identity_verified",
        "bibtex_file",
        "bibtex_sha256",
        "collection_status",
        "collected_at",
        "attempts",
        "cache_hit",
        "error_code",
    ]


def _result_row(result: PaperSpecialIssueResult) -> dict[str, Any]:
    metadata = result.metadata
    return {
        "paper_index": result.paper.paper_index,
        "database_id": result.paper.database_id,
        "source_id": result.paper.source_id,
        "title": result.paper.title,
        "title_kor": result.paper.title_kor,
        "title_eng": result.paper.title_eng,
        "doi_raw": result.paper.doi_raw,
        "doi": result.paper.doi,
        "journal_id": result.paper.journal_id,
        "institution_id": result.paper.institution_id,
        "issue_id": result.paper.issue_id,
        "publication_year": result.paper.publication_year,
        "evidence_strategy": "KCI authenticated UI / BibTex / VP only",
        "volume": metadata.volume if metadata else None,
        "number": metadata.number if metadata else None,
        "special_number": metadata.special_number if metadata else None,
        "pages": metadata.pages if metadata else None,
        "article_number": metadata.article_number if metadata else None,
        "bibtex_date": metadata.bibtex_date if metadata else None,
        "special_number_field_present": (
            metadata.special_number_field_present if metadata else None
        ),
        "is_special_issue": metadata.is_special_issue if metadata else None,
        "special_issue_classification": metadata.classification if metadata else "Unresolved",
        "identity_verified": result.identity_verified,
        "bibtex_file": result.bibtex_file,
        "bibtex_sha256": result.bibtex_sha256,
        "collection_status": result.status,
        "collected_at": result.collected_at,
        "attempts": result.attempts,
        "cache_hit": result.cache_hit,
        "error_code": result.error_code,
    }


def _summary_rows(
    run: CollectionRun,
    *,
    database: Path,
    generated_at: datetime | None,
) -> list[dict[str, Any]]:
    statuses = Counter(result.status for result in run.results)
    yes = sum(result.is_special_issue is True for result in run.results)
    no = sum(result.is_special_issue is False for result in run.results)
    unresolved = len(run.results) - yes - no
    generated = generated_at or datetime.now(SEOUL)
    values = [
        ("report_schema_version", REPORT_SCHEMA_VERSION, "리포트 스키마"),
        ("generated_at", _iso_timestamp(generated), "Asia/Seoul 생성시각"),
        ("database", Path(database).name, "입력 DB 파일명"),
        ("paper_count", len(run.results), "결과 행 수"),
        ("database_paper_count", run.total_database_papers, "DB 전체 대상 수"),
        ("special_issue_yes", yes, "Special-Number 비어 있지 않음"),
        ("not_special_issue_no", no, "Special-Number 빈 값"),
        ("classified_total", yes + no, "Y + N"),
        ("field_missing", statuses["field_missing"], "필드 누락; 미판정"),
        ("failed", statuses["failed"], "수집/검증 실패; 미판정"),
        ("unresolved_total", unresolved, "canonical에서는 0"),
        ("classification_complete", run.classification_complete, "537편 모두 확정"),
        (
            "special_issue_rate",
            yes / (yes + no) if yes + no else None,
            "확정 논문 중 Special Issue 비율",
        ),
        ("cache_hits", run.cache_hits, "재사용한 검증 캐시"),
        ("network_requests", run.network_requests, "이번 브라우저 상세 요청 수"),
        (
            "classification_rule",
            "Special-Number가 존재하고 trim 후 비어 있지 않을 때만 Y",
            "DA/공개 상세 문구는 판정에 사용하지 않음",
        ),
    ]
    return [{"항목": key, "값": value, "정의": definition} for key, value, definition in values]


def _validation_rows(run: CollectionRun) -> list[dict[str, Any]]:
    results = run.results
    source_ids = [result.paper.source_id for result in results]
    database_ids = [result.paper.database_id for result in results]
    classified = [result for result in results if result.status == "classified"]
    checks = [
        (
            "result_count_matches_database",
            len(results),
            run.total_database_papers,
            len(results) == run.total_database_papers,
        ),
        (
            "source_id_unique",
            len(set(source_ids)),
            len(source_ids),
            len(set(source_ids)) == len(source_ids),
        ),
        (
            "database_id_order",
            database_ids == sorted(database_ids),
            True,
            database_ids == sorted(database_ids),
        ),
        (
            "classified_evidence_complete",
            all(
                result.identity_verified
                and result.metadata is not None
                and result.metadata.special_number_field_present
                and result.bibtex_sha256 is not None
                for result in classified
            ),
            True,
            all(
                result.identity_verified
                and result.metadata is not None
                and result.metadata.special_number_field_present
                and result.bibtex_sha256 is not None
                for result in classified
            ),
        ),
        (
            "classification_rule_consistent",
            all(
                result.metadata is not None
                and result.is_special_issue
                == bool((result.metadata.special_number or "").strip())
                for result in classified
            ),
            True,
            all(
                result.metadata is not None
                and result.is_special_issue
                == bool((result.metadata.special_number or "").strip())
                for result in classified
            ),
        ),
        (
            "canonical_ready",
            run.classification_complete,
            True,
            run.classification_complete,
        ),
    ]
    return [
        {
            "검증 항목": name,
            "값": value,
            "기대값": expected,
            "결과": "PASS" if passed else "FAIL",
        }
        for name, value, expected, passed in checks
    ]


def _write_sheet(
    worksheet: Any,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    table_name: str,
) -> None:
    worksheet.append([_excel_safe(column) for column in columns])
    for row in rows:
        worksheet.append([_excel_safe(row.get(column)) for column in columns])
    worksheet.freeze_panes = "A2"
    worksheet.sheet_view.showGridLines = False
    worksheet.sheet_view.zoomScale = 90
    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    side = Side(style="thin", color="D9E2F3")
    border = Border(left=side, right=side, top=side, bottom=side)
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = border
    if rows:
        reference = f"A1:{get_column_letter(len(columns))}{worksheet.max_row}"
        table = Table(displayName=table_name, ref=reference)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        worksheet.add_table(table)
    for index, column in enumerate(columns, start=1):
        widths = [len(str(column))]
        for cell in list(worksheet.columns)[index - 1][1:101]:
            if cell.value is not None:
                widths.append(len(str(cell.value)))
        worksheet.column_dimensions[get_column_letter(index)].width = min(
            60, max(10, max(widths) + 2)
        )


def _validate_exported_workbook(path: Path, run: CollectionRun) -> None:
    try:
        workbook = load_workbook(path, read_only=True, data_only=False)
        if workbook.sheetnames != ["Special Issue 목록", "요약", "검증"]:
            raise SpecialIssueError("workbook_sheet_validation_failed")
        listing = workbook["Special Issue 목록"]
        if listing.max_row != len(run.results) + 1:
            raise SpecialIssueError("workbook_row_validation_failed")
        headers = [cell.value for cell in next(listing.iter_rows(min_row=1, max_row=1))]
        source_index = headers.index("source_id") + 1
        observed = [
            listing.cell(row=index, column=source_index).value
            for index in range(2, listing.max_row + 1)
        ]
        expected = [result.paper.source_id for result in run.results]
        if observed != expected:
            raise SpecialIssueError("workbook_identity_validation_failed")
    finally:
        with contextlib.suppress(Exception):
            workbook.close()  # type: ignore[possibly-undefined]


def _excel_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, datetime)):
        return value
    text = _INVALID_EXCEL_XML_RE.sub("", str(value))
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text[:32_767]


def _read_regular_file(path: Path, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise SpecialIssueError("unsafe_or_missing_bibtex_file") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SpecialIssueError("unsafe_or_missing_bibtex_file")
        if info.st_size < 1:
            raise InvalidBibtexError("empty_bibtex")
        if info.st_size > max_bytes:
            raise InvalidBibtexError("bibtex_too_large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise InvalidBibtexError("bibtex_too_large")
        return content
    finally:
        os.close(descriptor)


def _configure_user_playwright_libraries() -> None:
    configured = os.getenv("XDG_DATA_HOME")
    base = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else Path.home() / ".local" / "share"
    )
    library_dir = (
        base
        / "forensics-paper"
        / "playwright-libs"
        / "usr"
        / "lib"
        / "x86_64-linux-gnu"
    ).resolve()
    if not library_dir.is_dir() or library_dir.is_symlink():
        return
    entries = [entry for entry in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if entry]
    if str(library_dir) not in entries:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join((str(library_dir), *entries))


def _load_sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise KciBrowserError("browser_dependency_missing") from None
    return sync_playwright


def _is_official_kci_page(url: object) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == KCI_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _require_private_owned_path(path: Path, *, directory: bool) -> None:
    info = path.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("KCI browser profile must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("KCI browser profile permissions are too broad")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError("KCI browser profile is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError("KCI browser profile marker is not a regular file")


def _seconds_to_ms(seconds: float) -> int:
    return max(1, int(seconds * 1_000))


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=SEOUL)
    return value.astimezone(SEOUL).isoformat(timespec="seconds")


def _select_papers(
    papers: Sequence[KciPaper],
    *,
    source_ids: Sequence[str],
    limit: int | None,
) -> tuple[list[KciPaper], bool]:
    selected = list(papers)
    if source_ids:
        requested = list(dict.fromkeys(source_ids))
        invalid = [value for value in requested if _ART_ID_RE.fullmatch(value) is None]
        if invalid:
            raise SpecialIssueError("invalid_requested_source_id")
        by_source = {paper.source_id: paper for paper in papers}
        missing = [value for value in requested if value not in by_source]
        if missing:
            raise SpecialIssueError("requested_source_id_not_found")
        requested_set = set(requested)
        selected = [paper for paper in papers if paper.source_id in requested_set]
    if limit is not None:
        if limit < 1:
            raise SpecialIssueError("limit_must_be_positive")
        selected = selected[:limit]
    complete = len(selected) == len(papers) and all(
        left.source_id == right.source_id for left, right in zip(selected, papers)
    )
    return selected, complete


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "KCI 로그인 브라우저의 BibTex VP 필드로 Special Issue 여부를 "
            "판정합니다. 인증정보는 프로그램이 받거나 기록하지 않습니다."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bibtex-dir", type=Path, default=DEFAULT_BIBTEX_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(
            os.getenv("KCI_BIBTEX_BROWSER_PROFILE_DIR")
            or str(default_kci_profile_dir())
        ),
    )
    parser.add_argument(
        "--allow-authenticated-collection",
        action="store_true",
        help="계정 및 KCI 약관상 허용된 내부 연구 수집임을 명시적으로 확인",
    )
    parser.add_argument(
        "--interactive-login",
        action="store_true",
        help="headed KCI 브라우저에서 사용자가 직접 로그인한 뒤 같은 세션으로 수집",
    )
    parser.add_argument(
        "--login-wait-seconds",
        type=float,
        default=0.0,
        help="Enter 입력 대신 브라우저의 로그인 완료를 이 시간(초)까지 지켜본 뒤 자동으로 이어감",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_INTERVAL_SECONDS,
        help="KCI 상세/내보내기 작업 사이의 최소 초 간격",
    )
    parser.add_argument(
        "--canonical-when-unresolved",
        action="store_true",
        help=(
            "미확정 논문이 있어도 --output 경로에 그대로 기록(.partial 우회 없음). "
            "미확정 행은 그대로 남고 종료 코드 1은 유지"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.allow_authenticated_collection:
        parser.error("--allow-authenticated-collection 확인이 필요합니다")
    if args.retries < 0:
        parser.error("--retries는 0 이상이어야 합니다")
    try:
        all_papers = load_kci_papers(args.database)
        selected, selection_complete = _select_papers(
            all_papers,
            source_ids=args.source_id,
            limit=args.limit,
        )
        cache = SpecialIssueCache(args.cache_dir, args.bibtex_dir)
        config = KciBrowserConfig(
            permission_granted=True,
            profile_dir=args.profile_dir,
            interactive_login=args.interactive_login,
            headless=not args.interactive_login,
            login_wait_seconds=args.login_wait_seconds,
            min_interval_seconds=args.min_interval,
        )
        session = PlaywrightKciBibtexSession(config)
        logger = ProgressLogger()
        logger.emit(
            "KCI-SPECIAL-ISSUE",
            "수집 시작",
            current=0,
            total=len(selected),
            counters={"database_papers": len(all_papers)},
        )
        try:
            run = collect_special_issues(
                selected,
                session,
                cache=cache,
                total_database_papers=len(all_papers),
                selection_complete=selection_complete,
                force=args.force,
                retries=args.retries,
                logger=logger,
            )
        finally:
            session.close()
        exported = export_special_issue_workbook(
            run,
            args.output,
            database=args.database,
            canonical_when_unresolved=args.canonical_when_unresolved,
        )
    except (SpecialIssueError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"KCI Special Issue 수집 실패: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "KCI Special Issue 수집을 중단했습니다. 완료된 BibTeX 체크포인트는 "
            "다음 실행에서 재사용됩니다.",
            file=sys.stderr,
        )
        return 130

    yes = sum(result.is_special_issue is True for result in run.results)
    no = sum(result.is_special_issue is False for result in run.results)
    unresolved = len(run.results) - yes - no
    logger.emit(
        "KCI-SPECIAL-ISSUE",
        f"완료: {exported}",
        current=len(run.results),
        total=len(run.results),
        counters={"yes": yes, "no": no, "unresolved": unresolved},
    )
    return 0 if run.classification_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
