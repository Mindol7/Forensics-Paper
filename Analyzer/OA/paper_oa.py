"""Classify every ``kci_papers`` row with KCI OAI-PMH OA metadata.

The only classification rule in this module is the one defined by KCI:

* ``<orte-open-yn>Y</orte-open-yn>`` -> Open Access
* ``<orte-open-yn>N</orte-open-yn>`` -> Not Open Access
* a missing, blank, or unexpected value -> unresolved (never silently ``N``)

The primary evidence is ``ListRecords`` with ``metadataPrefix=oai_dc`` and its
``rights`` field.  Every matched record is then cross-checked through
``GetRecord`` with ``metadataPrefix=oai_kci``: the OAI identifier, KCI ART ID,
and ``orte-open-yn`` value must all agree before a paper is classified.

Raw XML is deliberately never logged, cached, or exported.  Only the parsed
OAI identifier, ART ID, and OA tokens are persisted.  Missing records, missing
fields, and cross-check mismatches always remain unresolved; absence is never
silently interpreted as ``N``.

Run from any working directory with either command::

    python3 Analyzer/OA/paper_oa.py
    python3 -m Analyzer.OA.paper_oa
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import http.client
import json
import math
import os
import re
import socket
import sqlite3
import ssl
import stat
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

try:  # Linux-only inter-process cache locking; see _exclusive_file_lock().
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised through the fail-safe seam.
    _fcntl = None


OA_DIR = Path(__file__).resolve().parent
ANALYZER_DIR = OA_DIR.parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    ResponseTooLargeError,
    RetryableHttpError,
)
from Util.logger import ProgressLogger


KCI_ARTICLE_SEARCH_URL = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"
KCI_ARTICLE_SEARCH_API_CODE = "articleSearch"
KCI_OAI_PMH_URL = "https://open.kci.go.kr/oai/request"
KCI_OAI_PMH_LEGACY_ECHO_URL = "http://open.kci.go.kr/oai/request"
KCI_OAI_SET = "ARTI"
KCI_OAI_PRIMARY_PREFIX = "oai_dc"
KCI_OAI_VERIFY_PREFIX = "oai_kci"
KCI_OAI_EARLIEST_DATE = "2013-03-21"
OA_COLLECTION_STRATEGY = "oai_dc_listrecords_plus_oai_kci_getrecord"
ARTICLE_SEARCH_COLLECTION_STRATEGY = "article_search_orte_open_yn"
DEFAULT_DATABASE = REPOSITORY_ROOT / "kci_pipeline.db"
DEFAULT_OUTPUT = OA_DIR / "oa_list.xlsx"
DEFAULT_CACHE_DIR = OA_DIR / ".cache"
DEFAULT_ENV_FILE = REPOSITORY_ROOT / ".env"
DEFAULT_RPS = 2.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 45.0
DEFAULT_RETRIES = 2
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_OPEN_ACCESS_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_NOT_OPEN_ACCESS_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_UNRESOLVED_CACHE_TTL_SECONDS = 6 * 60 * 60
DEFAULT_HARVEST_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_OAI_PAGES_PER_WINDOW = 100_000
CACHE_SCHEMA_VERSION = "2.0-oai-pmh"
ARTICLE_SEARCH_CACHE_SCHEMA_VERSION = "1.0-article-search"
OAI_EVIDENCE_SIDECAR_SCHEMA_VERSION = "1.0-oai-evidence"
REPORT_SCHEMA_VERSION = "2.0-oai-pmh"
ARTICLE_SEARCH_REPORT_SCHEMA_VERSION = "1.0-article-search"
SEOUL = ZoneInfo("Asia/Seoul")

OAI_PMH_NAMESPACE = "http://www.openarchives.org/OAI/2.0/"
OAI_DC_CONTAINER_NAMESPACE = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_ELEMENTS_NAMESPACE = "http://purl.org/dc/elements/1.1/"
KCI_OAI_METADATA_NAMESPACE = "http://www.kci.go.kr/kciportal/OAI/"
XML_SCHEMA_INSTANCE_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
OAI_PMH_SCHEMA_LOCATION = (
    "http://www.openarchives.org/OAI/2.0/ "
    "http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd"
)

_ART_ID_RE = re.compile(r"ART[0-9]{9}\Z")
_OAI_IDENTIFIER_RE = re.compile(r"oai:kci\.go\.kr:ARTI/[0-9]+\Z")
_OAI_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_UNSAFE_XML_RE = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_INVALID_EXCEL_XML_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]"
)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SUCCESS_MESSAGES = frozenset({"정상 처리되었습니다.", "ok", "success"})
_NO_DATA_MESSAGES = frozenset({"no data", "검색 조건이 없습니다", "검색 조건이 없습니다."})
_CACHEABLE_STATUSES = frozenset(
    {"classified", "field_missing", "invalid_value", "no_record"}
)
_RESULT_STATUSES = frozenset(
    {"classified", "field_missing", "invalid_value", "no_record", "failed"}
)


class OaCollectionError(RuntimeError):
    """Base exception carrying a stable, credential-free error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RetryableOaError(OaCollectionError):
    """A request that may succeed after a short backoff."""

    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.retry_after = retry_after


class FatalOaError(OaCollectionError):
    """A run-wide authentication, service, or configuration error."""


class InvalidOaResponse(OaCollectionError):
    """One article response violated the expected API contract."""


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold a process-wide exclusive lock on a stable, non-symlink inode.

    Checkpoint JSON files are atomically replaced, so locking a JSON file would
    protect only the old inode.  Callers therefore pass a dedicated lock file
    that is deliberately retained after unlock.  ``flock`` locks are advisory,
    but every writer in this module cooperates and the kernel releases a lock
    automatically if its process exits.

    The production collector is Linux-only for inter-process cache mutation.
    Silently falling back to an in-process lock elsewhere would reintroduce the
    exact provenance/checkpoint race this guard exists to prevent.
    """

    if not sys.platform.startswith("linux") or _fcntl is None:
        raise OaCollectionError("interprocess_cache_lock_unsupported")

    lock_path = Path(path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise OaCollectionError("cache_lock_directory_failed") from None

    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    # Linux provides O_NOFOLLOW.  Treat its absence as unsupported rather than
    # opening a user-controlled symlink and locking an unrelated inode.
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:  # pragma: no cover - Linux always defines O_NOFOLLOW.
        raise OaCollectionError("interprocess_cache_lock_unsupported")
    flags |= nofollow

    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError:
        raise OaCollectionError("unsafe_or_unavailable_cache_lock") from None

    locked = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OaCollectionError("unsafe_or_unavailable_cache_lock")
        os.fchmod(descriptor, 0o600)
        while True:
            try:
                _fcntl.flock(descriptor, _fcntl.LOCK_EX)
                locked = True
                break
            except InterruptedError:
                continue
            except OSError:
                raise OaCollectionError("cache_lock_failed") from None
        yield
    finally:
        if locked:
            try:
                _fcntl.flock(descriptor, _fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


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
    publication_year: int | None
    journal_id: str | None
    institution_id: str | None

    @property
    def fingerprint(self) -> str:
        """Digest of the paper's bibliographic identity.

        ``paper_index`` and ``database_id`` describe where the row currently
        sits in the database, not the paper: removing one paper shifts every
        later position, which must not invalidate the evidence of the others.
        """

        # ``database_id`` is a row id as well: a full re-match re-inserts the rows under new ids.
        identity = {key: value for key, value in asdict(self).items() if key not in ("paper_index", "database_id")}
        payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _positional_fingerprint(self, paper_index: int) -> str:
        payload = json.dumps(
            {**asdict(self), "paper_index": paper_index}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def matches_fingerprint(self, recorded: Any) -> bool:
        """Accept the identity digest, or the positional digest earlier versions wrote.

        A positional record is valid for the index the paper had when it was
        written: its current index plus however many earlier papers have been
        removed since, which is at most the length of the exclusion list.
        """

        if not isinstance(recorded, str):
            return False
        if recorded == self.fingerprint:
            return True
        return any(
            recorded == self._positional_fingerprint(self.paper_index + shift)
            for shift in range(_removed_paper_count() + 1)
        )


@dataclass(frozen=True, slots=True)
class ParsedOaValue:
    status: str
    returned_source_id: str | None
    orte_open_yn: str | None
    field_present: bool | None
    observed_value: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"classified", "field_missing", "invalid_value", "no_record"}:
            raise ValueError("invalid parsed OA status")
        if self.status == "classified":
            if self.orte_open_yn not in {"Y", "N"} or self.field_present is not True:
                raise ValueError("classified OA value must be Y or N")
            if self.returned_source_id is None or self.observed_value is not None:
                raise ValueError("invalid classified OA identity")
        elif self.orte_open_yn is not None:
            raise ValueError("unresolved OA value cannot contain a Y/N token")
        if self.status == "field_missing":
            if self.returned_source_id is None or self.observed_value is not None:
                raise ValueError("invalid missing-field result")
            if self.field_present not in {False, True}:
                raise ValueError("missing-field presence must be known")
        if self.status == "invalid_value":
            if (
                self.returned_source_id is None
                or self.field_present is not True
                or self.observed_value is None
            ):
                raise ValueError("invalid-value result must retain the observed token")
        if self.status == "no_record" and any(
            value is not None
            for value in (
                self.returned_source_id,
                self.orte_open_yn,
                self.field_present,
                self.observed_value,
            )
        ):
            raise ValueError("no-record result cannot contain article fields")


@dataclass(frozen=True, slots=True)
class PaperOaResult:
    paper: KciPaper
    status: str
    returned_source_id: str | None
    orte_open_yn: str | None
    field_present: bool | None
    observed_value: str | None
    collected_at: str
    attempts: int
    error_code: str | None = None
    cache_hit: bool = False
    evidence_strategy: str = ARTICLE_SEARCH_COLLECTION_STRATEGY
    oai_identifier: str | None = None
    dc_rights: str | None = None
    dc_rights_field_present: bool | None = None
    observed_dc_rights: str | None = None
    kci_orte_open_yn: str | None = None
    kci_orte_open_yn_field_present: bool | None = None
    observed_kci_orte_open_yn: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise ValueError("invalid OA collection status")
        if self.attempts < 0:
            raise ValueError("attempts must be non-negative")
        if self.evidence_strategy not in {
            ARTICLE_SEARCH_COLLECTION_STRATEGY,
            OA_COLLECTION_STRATEGY,
        }:
            raise ValueError("invalid OA evidence strategy")
        if self.oai_identifier is not None and (
            not isinstance(self.oai_identifier, str)
            or _OAI_IDENTIFIER_RE.fullmatch(self.oai_identifier) is None
        ):
            raise ValueError("invalid OAI identifier")
        if self.dc_rights not in {None, "Y", "N"}:
            raise ValueError("invalid dc:rights value")
        if self.kci_orte_open_yn not in {None, "Y", "N"}:
            raise ValueError("invalid oai_kci orte-open-yn value")
        _validate_normalized_evidence(
            value=self.dc_rights,
            field_present=self.dc_rights_field_present,
            observed_value=self.observed_dc_rights,
            field_name="dc_rights",
        )
        _validate_normalized_evidence(
            value=self.kci_orte_open_yn,
            field_present=self.kci_orte_open_yn_field_present,
            observed_value=self.observed_kci_orte_open_yn,
            field_name="kci_orte_open_yn",
        )
        if self.evidence_strategy == ARTICLE_SEARCH_COLLECTION_STRATEGY and any(
            value is not None
            for value in (
                self.oai_identifier,
                self.dc_rights,
                self.dc_rights_field_present,
                self.observed_dc_rights,
                self.kci_orte_open_yn,
                self.kci_orte_open_yn_field_present,
                self.observed_kci_orte_open_yn,
            )
        ):
            raise ValueError("articleSearch result cannot claim OAI evidence")
        if self.evidence_strategy == OA_COLLECTION_STRATEGY and self.status == "classified":
            if (
                self.oai_identifier is None
                or self.returned_source_id != self.paper.source_id
                or self.dc_rights not in {"Y", "N"}
                or self.kci_orte_open_yn != self.dc_rights
                or self.orte_open_yn != self.dc_rights
                or self.dc_rights_field_present is not True
                or self.kci_orte_open_yn_field_present is not True
                or self.observed_dc_rights is not None
                or self.observed_kci_orte_open_yn is not None
            ):
                raise ValueError("classified OAI result lacks matching evidence")
        if self.status == "failed":
            if not self.error_code:
                raise ValueError("failed result requires an error code")
            if any(
                value is not None
                for value in (
                    self.orte_open_yn,
                    self.field_present,
                    self.observed_value,
                )
            ):
                raise ValueError("failed result cannot claim article fields")
            if self.returned_source_id is not None and not (
                self.evidence_strategy == OA_COLLECTION_STRATEGY
                and self.error_code == "oai_oa_value_mismatch"
                and self.returned_source_id == self.paper.source_id
            ):
                raise ValueError("failed result has invalid returned identity")
            return
        parsed = ParsedOaValue(
            status=self.status,
            returned_source_id=self.returned_source_id,
            orte_open_yn=self.orte_open_yn,
            field_present=self.field_present,
            observed_value=self.observed_value,
        )
        expected_errors = {
            "classified": {None},
            "field_missing": {"orte_open_yn_missing"},
            "invalid_value": {"orte_open_yn_invalid"},
            "no_record": {"article_not_found"},
        }[parsed.status]
        if self.evidence_strategy == OA_COLLECTION_STRATEGY:
            if parsed.status == "field_missing":
                expected_errors = {"dc_rights_missing", "orte_open_yn_missing"}
            elif parsed.status == "invalid_value":
                expected_errors = {"dc_rights_invalid", "orte_open_yn_invalid"}
        if self.error_code not in expected_errors:
            raise ValueError("OA result error code does not match its status")

    @property
    def is_open_access(self) -> bool | None:
        if self.orte_open_yn == "Y":
            return True
        if self.orte_open_yn == "N":
            return False
        return None

    @property
    def oa_classification(self) -> str | None:
        if self.is_open_access is True:
            return "Open Access"
        if self.is_open_access is False:
            return "Not Open Access"
        return None


@dataclass(frozen=True, slots=True)
class OaCollectionRun:
    results: tuple[PaperOaResult, ...]
    network_requests: int
    cache_hits: int
    strategy: str = ARTICLE_SEARCH_COLLECTION_STRATEGY

    def __post_init__(self) -> None:
        if self.strategy not in {
            ARTICLE_SEARCH_COLLECTION_STRATEGY,
            OA_COLLECTION_STRATEGY,
        }:
            raise ValueError("invalid OA collection strategy")


@dataclass(frozen=True, slots=True)
class OaExportResult:
    path: Path
    paper_count: int
    open_access_count: int
    not_open_access_count: int
    unresolved_count: int


@dataclass(frozen=True, slots=True)
class OaiDcRecord:
    """Credential-free primary evidence extracted from one ``oai_dc`` record."""

    source_id: str
    oai_identifier: str
    rights: str | None
    field_present: bool
    observed_value: str | None = None

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        _validate_oai_identifier(self.oai_identifier)
        if not isinstance(self.field_present, bool):
            raise ValueError("field_present must be boolean")
        if self.rights not in {None, "Y", "N"}:
            raise ValueError("invalid normalized rights")
        if self.rights is not None:
            if not self.field_present or self.observed_value is not None:
                raise ValueError("classified rights evidence is inconsistent")
        elif not self.field_present and self.observed_value is not None:
            raise ValueError("a missing rights field cannot have an observed value")


@dataclass(frozen=True, slots=True)
class OaiRecordDiagnostic:
    """Sanitized evidence that one ListRecords record could not be trusted.

    ``source_id`` is populated only when an exact target ART ID can still be
    recovered from the record.  Keeping the OAI identifier when it is valid
    makes the unresolved result auditable without retaining raw XML.
    """

    code: str
    source_id: str | None = None
    oai_identifier: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not re.fullmatch(r"[a-z0-9_]{1,96}", self.code)
        ):
            raise ValueError("invalid OAI record diagnostic code")
        if self.source_id is not None:
            _validate_source_id(self.source_id)
        if self.oai_identifier is not None:
            _validate_oai_identifier(self.oai_identifier)


@dataclass(frozen=True, slots=True)
class OaiListPage:
    """One parsed ListRecords page; ``next_token`` remains fully opaque."""

    matches: tuple[OaiDcRecord, ...]
    next_token: str | None
    record_count: int
    no_records_match: bool = False
    diagnostics: tuple[OaiRecordDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.record_count < 0:
            raise ValueError("record_count must be non-negative")
        if self.next_token is not None:
            _validate_resumption_token(self.next_token)
        if self.no_records_match and (self.record_count or self.next_token):
            raise ValueError("noRecordsMatch page cannot contain records or a token")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, OaiRecordDiagnostic) for item in self.diagnostics
        ):
            raise ValueError("invalid OAI page diagnostics")


@dataclass(frozen=True, slots=True)
class OaiHarvestState:
    mappings: Mapping[str, OaiDcRecord]
    next_token: str | None
    exhausted: bool
    pages: int
    records_scanned: int
    diagnostics: tuple[OaiRecordDiagnostic, ...] = ()


class OaiPmhClient(Protocol):
    request_count: int

    def fetch_dc_page(
        self,
        *,
        target_source_ids: set[str],
        resumption_token: str | None = None,
        from_date: str | None = None,
        until_date: str | None = None,
    ) -> OaiListPage:
        """Fetch one oai_dc ListRecords page and retain target records only."""

    def fetch_kci_record(
        self,
        *,
        oai_identifier: str,
        expected_source_id: str,
    ) -> ParsedOaValue:
        """Cross-check one primary record through oai_kci GetRecord."""


class ArticleSearchClient(Protocol):
    request_count: int

    def fetch(self, source_id: str) -> ParsedOaValue:
        """Fetch and parse one exact KCI ART ID."""


class _RedactedQueryParams(Mapping[str, str]):
    """Query mapping whose representations never disclose the API key."""

    __slots__ = ("_values",)

    def __init__(self, *, api_key: str, source_id: str) -> None:
        self._values = {
            "apiCode": KCI_ARTICLE_SEARCH_API_CODE,
            "key": api_key,
            "id": source_id,
            "page": "1",
            "displayCount": "1",
        }

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        values = dict(self._values)
        values["key"] = "<redacted>"
        return f"{type(self).__name__}({values!r})"

    __str__ = __repr__


class RequestRateLimiter:
    """Space request starts by a fixed minimum interval."""

    def __init__(
        self,
        requests_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            not math.isfinite(requests_per_second)
            or requests_per_second <= 0
            or requests_per_second > 4
        ):
            raise ValueError("requests_per_second must be finite and in (0, 4]")
        self._interval = 1.0 / requests_per_second
        self._clock = clock
        self._sleeper = sleeper
        self._next_start = 0.0

    def __call__(self, _url: str) -> None:
        now = self._clock()
        delay = self._next_start - now
        if delay > 0:
            self._sleeper(delay)
            now = self._clock()
        self._next_start = max(self._next_start, now) + self._interval


class KciArticleSearchHttpClient:
    """Fixed-origin, redirect-free transport for exact KCI article searches."""

    def __init__(
        self,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        user_agent: str = "Forensics-Paper/1.0 (KCI OA collector)",
        request_gate: Callable[[str], None] | None = None,
    ) -> None:
        if (
            isinstance(connect_timeout, bool)
            or isinstance(read_timeout, bool)
            or not math.isfinite(float(connect_timeout))
            or not math.isfinite(float(read_timeout))
            or connect_timeout <= 0
            or read_timeout <= 0
        ):
            raise ValueError("HTTP timeouts must be positive")
        if _has_control(user_agent):
            raise ValueError("user_agent must contain no control characters")
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.user_agent = user_agent
        self._request_gate = request_gate

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        if url != KCI_ARTICLE_SEARCH_URL or not _is_exact_endpoint(url):
            raise HttpClientError("KCI articleSearch endpoint is not allowlisted")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if set(params) != {"apiCode", "key", "id", "page", "displayCount"}:
            raise HttpClientError("unexpected KCI articleSearch parameters")
        if (
            params["apiCode"] != KCI_ARTICLE_SEARCH_API_CODE
            or params["page"] != "1"
            or params["displayCount"] != "1"
            or _ART_ID_RE.fullmatch(params["id"]) is None
            or not params["key"]
            or any(_has_control(value) for value in params.values())
        ):
            raise HttpClientError("invalid KCI articleSearch parameters")

        parsed = urlsplit(url)
        request_target = parsed.path + "?" + urlencode(
            (
                ("apiCode", params["apiCode"]),
                ("key", params["key"]),
                ("id", params["id"]),
                ("page", params["page"]),
                ("displayCount", params["displayCount"]),
            )
        )
        accept = (headers or {}).get("Accept", "application/xml, text/xml")
        if _has_control(accept):
            raise HttpClientError("invalid Accept header")
        request_headers = {
            "Accept": accept,
            "User-Agent": self.user_agent,
            "Connection": "close",
        }

        connection: http.client.HTTPSConnection | None = None
        try:
            if self._request_gate is not None:
                self._request_gate(KCI_ARTICLE_SEARCH_URL)
            connection = http.client.HTTPSConnection(
                parsed.hostname,
                port=443,
                timeout=self.connect_timeout,
                context=ssl.create_default_context(),
            )
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self.read_timeout)
            connection.request("GET", request_target, headers=request_headers)
            response = connection.getresponse()
            content_length = _content_length(response.getheader("Content-Length"))
            if content_length is not None and content_length > max_bytes:
                raise ResponseTooLargeError("declared KCI response is too large")
            body = bytearray()
            while True:
                chunk = response.read(min(64 * 1024, max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLargeError("decoded KCI response is too large")
            safe_headers = {
                name: value
                for name in ("Content-Type", "Content-Length", "Retry-After")
                if (value := response.getheader(name)) is not None
            }
            return HttpFetch(
                requested_url=KCI_ARTICLE_SEARCH_URL,
                final_url=KCI_ARTICLE_SEARCH_URL,
                status_code=int(response.status),
                headers=safe_headers,
                body=bytes(body),
                redirects=(),
            )
        except ResponseTooLargeError:
            raise
        except (OSError, socket.timeout, TimeoutError, http.client.HTTPException):
            # Underlying exceptions may contain the query target and API key.
            raise RetryableHttpError("KCI articleSearch request failed") from None
        finally:
            if connection is not None:
                connection.close()


class KciOaClient:
    """Fetch one exact ART ID and retain only its parsed OA value."""

    def __init__(
        self,
        api_key: str,
        *,
        requests_per_second: float = DEFAULT_RPS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        http_client: Any | None = None,
    ) -> None:
        self._api_key = _validate_api_key(api_key)
        self._max_response_bytes = _positive_int(max_response_bytes, "max_response_bytes")
        if http_client is None:
            http_client = KciArticleSearchHttpClient(
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                request_gate=RequestRateLimiter(requests_per_second),
            )
        self._http = http_client
        self.request_count = 0

    def fetch(self, source_id: str) -> ParsedOaValue:
        _validate_source_id(source_id)
        self.request_count += 1
        try:
            fetched = self._http.get_bytes(
                KCI_ARTICLE_SEARCH_URL,
                params=_RedactedQueryParams(
                    api_key=self._api_key,
                    source_id=source_id,
                ),
                max_bytes=self._max_response_bytes,
                headers={"Accept": "application/xml, text/xml"},
            )
        except RetryableHttpError:
            raise RetryableOaError("network_error") from None
        except ResponseTooLargeError:
            raise InvalidOaResponse("response_too_large") from None
        except HttpClientError:
            raise FatalOaError("http_client_error") from None

        status_code = int(fetched.status_code)
        if status_code in _RETRYABLE_HTTP_STATUSES:
            raise RetryableOaError(
                f"http_{status_code}",
                retry_after=_parse_retry_after(fetched.headers.get("Retry-After")),
            )
        if status_code in {401, 403}:
            raise FatalOaError("authentication_failed")
        if status_code != 200:
            # Exact ART lookup reports an absent paper inside a HTTP 200 XML
            # envelope.  Any other non-retryable HTTP status is therefore an
            # endpoint-wide contract/configuration failure, not a per-paper N.
            raise FatalOaError(f"http_{status_code}")
        return parse_article_search_xml(
            fetched.body,
            expected_source_id=source_id,
        )


class KciOaiPmhHttpClient:
    """Fixed-origin, redirect-free transport for the public KCI OAI endpoint."""

    def __init__(
        self,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        user_agent: str = "Forensics-Paper/2.0 (KCI OAI-PMH collector)",
        request_gate: Callable[[str], None] | None = None,
    ) -> None:
        if (
            isinstance(connect_timeout, bool)
            or isinstance(read_timeout, bool)
            or not math.isfinite(float(connect_timeout))
            or not math.isfinite(float(read_timeout))
            or connect_timeout <= 0
            or read_timeout <= 0
        ):
            raise ValueError("HTTP timeouts must be positive")
        if _has_control(user_agent):
            raise ValueError("user_agent must contain no control characters")
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.user_agent = user_agent
        self._request_gate = request_gate

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        if url != KCI_OAI_PMH_URL or not _is_exact_oai_endpoint(url):
            raise HttpClientError("KCI OAI-PMH endpoint is not allowlisted")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        _validate_oai_request_params(params)

        parsed = urlsplit(url)
        request_target = parsed.path + "?" + urlencode(tuple(params.items()))
        accept = (headers or {}).get("Accept", "application/xml, text/xml")
        if _has_control(accept):
            raise HttpClientError("invalid Accept header")
        request_headers = {
            "Accept": accept,
            "User-Agent": self.user_agent,
            "Connection": "close",
        }

        connection: http.client.HTTPSConnection | None = None
        try:
            if self._request_gate is not None:
                self._request_gate(KCI_OAI_PMH_URL)
            connection = http.client.HTTPSConnection(
                parsed.hostname,
                port=443,
                timeout=self.connect_timeout,
                context=ssl.create_default_context(),
            )
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self.read_timeout)
            connection.request("GET", request_target, headers=request_headers)
            response = connection.getresponse()
            content_length = _content_length(response.getheader("Content-Length"))
            if content_length is not None and content_length > max_bytes:
                raise ResponseTooLargeError("declared KCI OAI response is too large")
            body = bytearray()
            while True:
                chunk = response.read(min(64 * 1024, max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLargeError("decoded KCI OAI response is too large")
            safe_headers = {
                name: value
                for name in ("Content-Type", "Content-Length", "Retry-After")
                if (value := response.getheader(name)) is not None
            }
            return HttpFetch(
                requested_url=KCI_OAI_PMH_URL,
                final_url=KCI_OAI_PMH_URL,
                status_code=int(response.status),
                headers=safe_headers,
                body=bytes(body),
                redirects=(),
            )
        except ResponseTooLargeError:
            raise
        except (OSError, socket.timeout, TimeoutError, http.client.HTTPException):
            raise RetryableHttpError("KCI OAI-PMH request failed") from None
        finally:
            if connection is not None:
                connection.close()


class KciOaiPmhClient:
    """Harvest ``oai_dc`` and cross-check target records with ``oai_kci``."""

    def __init__(
        self,
        *,
        requests_per_second: float = DEFAULT_RPS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        http_client: Any | None = None,
    ) -> None:
        self._max_response_bytes = _positive_int(
            max_response_bytes, "max_response_bytes"
        )
        if http_client is None:
            http_client = KciOaiPmhHttpClient(
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                request_gate=RequestRateLimiter(requests_per_second),
            )
        self._http = http_client
        self.request_count = 0

    def fetch_dc_page(
        self,
        *,
        target_source_ids: set[str],
        resumption_token: str | None = None,
        from_date: str | None = None,
        until_date: str | None = None,
    ) -> OaiListPage:
        for source_id in target_source_ids:
            _validate_source_id(source_id)
        if resumption_token is not None:
            _validate_resumption_token(resumption_token)
            params: dict[str, str] = {
                "verb": "ListRecords",
                "resumptionToken": resumption_token,
            }
        else:
            params = {
                "verb": "ListRecords",
                "set": KCI_OAI_SET,
                "metadataPrefix": KCI_OAI_PRIMARY_PREFIX,
            }
            if from_date is not None:
                params["from"] = _validate_oai_date(from_date)
            if until_date is not None:
                params["until"] = _validate_oai_date(until_date)
        fetched = self._request(params)
        return parse_oai_dc_list_records_xml(
            fetched.body,
            target_source_ids=target_source_ids,
        )

    def fetch_kci_record(
        self,
        *,
        oai_identifier: str,
        expected_source_id: str,
    ) -> ParsedOaValue:
        _validate_oai_identifier(oai_identifier)
        _validate_source_id(expected_source_id)
        fetched = self._request(
            {
                "verb": "GetRecord",
                "identifier": oai_identifier,
                "metadataPrefix": KCI_OAI_VERIFY_PREFIX,
            }
        )
        return parse_oai_kci_get_record_xml(
            fetched.body,
            expected_oai_identifier=oai_identifier,
            expected_source_id=expected_source_id,
        )

    def _request(self, params: Mapping[str, str]) -> HttpFetch:
        self.request_count += 1
        try:
            fetched = self._http.get_bytes(
                KCI_OAI_PMH_URL,
                params=params,
                max_bytes=self._max_response_bytes,
                headers={"Accept": "application/xml, text/xml"},
            )
        except RetryableHttpError:
            raise RetryableOaError("network_error") from None
        except ResponseTooLargeError:
            raise InvalidOaResponse("response_too_large") from None
        except HttpClientError:
            raise FatalOaError("http_client_error") from None

        status_code = int(fetched.status_code)
        if status_code in _RETRYABLE_HTTP_STATUSES:
            raise RetryableOaError(
                f"http_{status_code}",
                retry_after=_parse_retry_after(fetched.headers.get("Retry-After")),
            )
        if status_code in {401, 403}:
            raise FatalOaError("oai_access_denied")
        if status_code != 200:
            raise FatalOaError(f"http_{status_code}")
        return fetched


class OaCache:
    """Strategy-scoped per-paper cache containing no raw API response.

    OAI keeps its schema-2.0 base payload byte-contract compatible with
    already-running collectors.  Expanded per-source evidence is written to a
    separate sidecar.  A schema-2.0 classified result is losslessly readable
    without a sidecar because matching ``dc_rights`` and ``orte_open_yn``
    prove both fields were present.  Ambiguous unresolved schema-2.0 entries
    are deliberately treated as cache misses unless a valid sidecar exists.
    """

    _OAI_BASE_KEYS = {
        "schema_version",
        "strategy",
        "source_id",
        "paper_fingerprint",
        "status",
        "returned_source_id",
        "oai_identifier",
        "dc_rights",
        "orte_open_yn",
        "field_present",
        "observed_value",
        "collected_at",
        "error_code",
    }
    _ARTICLE_KEYS = {
        "schema_version",
        "strategy",
        "api_code",
        "source_id",
        "paper_fingerprint",
        "status",
        "returned_source_id",
        "orte_open_yn",
        "field_present",
        "observed_value",
        "collected_at",
        "error_code",
    }
    _EVIDENCE_KEYS = {
        "schema_version",
        "strategy",
        "source_id",
        "paper_fingerprint",
        "base_payload_sha256",
        "oai_identifier",
        "dc_rights",
        "dc_rights_field_present",
        "observed_dc_rights",
        "kci_orte_open_yn",
        "kci_orte_open_yn_field_present",
        "observed_kci_orte_open_yn",
    }
    _DETERMINISTIC_OAI_FAILURES = frozenset(
        {"oai_oa_value_mismatch", "oai_kci_record_not_found"}
    )

    def __init__(
        self,
        directory: Path,
        *,
        strategy: str = ARTICLE_SEARCH_COLLECTION_STRATEGY,
        open_access_ttl_seconds: int = DEFAULT_OPEN_ACCESS_CACHE_TTL_SECONDS,
        not_open_access_ttl_seconds: int = DEFAULT_NOT_OPEN_ACCESS_CACHE_TTL_SECONDS,
        unresolved_ttl_seconds: int = DEFAULT_UNRESOLVED_CACHE_TTL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if strategy not in {
            ARTICLE_SEARCH_COLLECTION_STRATEGY,
            OA_COLLECTION_STRATEGY,
        }:
            raise ValueError("invalid OA cache strategy")
        self.directory = Path(directory)
        self.strategy = strategy
        self.open_access_ttl_seconds = _positive_int(
            open_access_ttl_seconds, "open_access_ttl_seconds"
        )
        self.not_open_access_ttl_seconds = _positive_int(
            not_open_access_ttl_seconds, "not_open_access_ttl_seconds"
        )
        self.unresolved_ttl_seconds = _positive_int(
            unresolved_ttl_seconds, "unresolved_ttl_seconds"
        )
        self._now = now or (lambda: datetime.now(SEOUL))

    def load(self, paper: KciPaper) -> PaperOaResult | None:
        with self._paper_lock(paper.source_id):
            if self.strategy == OA_COLLECTION_STRATEGY:
                return self._load_oai(paper)
            return self._load_article_search(paper)

    def _load_article_search(self, paper: KciPaper) -> PaperOaResult | None:
        payload = self._read_json(self._path(paper.source_id))
        if payload is None or set(payload) != self._ARTICLE_KEYS:
            return None
        if (
            payload.get("schema_version") != ARTICLE_SEARCH_CACHE_SCHEMA_VERSION
            or payload.get("strategy") != ARTICLE_SEARCH_COLLECTION_STRATEGY
            or payload.get("api_code") != KCI_ARTICLE_SEARCH_API_CODE
            or not self._valid_common_payload(payload, paper)
        ):
            return None
        try:
            result = PaperOaResult(
                paper=paper,
                status=payload["status"],
                returned_source_id=payload["returned_source_id"],
                orte_open_yn=payload["orte_open_yn"],
                field_present=payload["field_present"],
                observed_value=payload["observed_value"],
                collected_at=payload["collected_at"],
                attempts=0,
                error_code=payload["error_code"],
                cache_hit=True,
                evidence_strategy=ARTICLE_SEARCH_COLLECTION_STRATEGY,
            )
            _validate_article_search_results((result,))
            return result
        except (TypeError, ValueError, OaCollectionError):
            return None

    def _load_oai(self, paper: KciPaper) -> PaperOaResult | None:
        payload = self._read_json(self._path(paper.source_id))
        allowed_statuses = _CACHEABLE_STATUSES | {"failed"}
        if payload is None or set(payload) != self._OAI_BASE_KEYS:
            return None
        if (
            payload.get("schema_version") != CACHE_SCHEMA_VERSION
            or payload.get("strategy") != OA_COLLECTION_STRATEGY
            or payload.get("status") not in allowed_statuses
            or not self._valid_common_payload(payload, paper)
            or (
                payload.get("oai_identifier") is not None
                and (
                    not isinstance(payload.get("oai_identifier"), str)
                    or _OAI_IDENTIFIER_RE.fullmatch(payload["oai_identifier"]) is None
                )
            )
            or payload.get("dc_rights") not in {None, "Y", "N"}
        ):
            return None
        if payload["status"] == "failed" and payload.get(
            "error_code"
        ) not in self._DETERMINISTIC_OAI_FAILURES:
            return None

        evidence = self._load_evidence_sidecar(paper, payload)
        if payload["status"] == "classified":
            # Safe migration for schema-2.0 files written by active legacy
            # processes: a classified base contains all lossless evidence.
            if not (
                payload.get("returned_source_id") == paper.source_id
                and payload.get("oai_identifier") is not None
                and payload.get("dc_rights") in {"Y", "N"}
                and payload.get("orte_open_yn") == payload.get("dc_rights")
                and payload.get("field_present") is True
                and payload.get("observed_value") is None
                and payload.get("error_code") is None
            ):
                return None
            if evidence is None:
                evidence = {
                    "oai_identifier": payload["oai_identifier"],
                    "dc_rights": payload["dc_rights"],
                    "dc_rights_field_present": True,
                    "observed_dc_rights": None,
                    "kci_orte_open_yn": payload["orte_open_yn"],
                    "kci_orte_open_yn_field_present": True,
                    "observed_kci_orte_open_yn": None,
                }
        elif payload["status"] == "no_record":
            evidence = evidence or {
                "oai_identifier": None,
                "dc_rights": None,
                "dc_rights_field_present": None,
                "observed_dc_rights": None,
                "kci_orte_open_yn": None,
                "kci_orte_open_yn_field_present": None,
                "observed_kci_orte_open_yn": None,
            }
        elif evidence is None:
            # Schema-2.0 overloaded field_present/observed_value for two
            # sources, so unresolved records cannot be migrated losslessly.
            return None

        try:
            result = PaperOaResult(
                paper=paper,
                status=payload["status"],
                returned_source_id=payload["returned_source_id"],
                orte_open_yn=payload["orte_open_yn"],
                field_present=payload["field_present"],
                observed_value=payload["observed_value"],
                collected_at=payload["collected_at"],
                attempts=0,
                error_code=payload["error_code"],
                cache_hit=True,
                evidence_strategy=OA_COLLECTION_STRATEGY,
                **evidence,
            )
            _validate_oai_results((result,))
            return result
        except (TypeError, ValueError, OaCollectionError):
            return None

    def save(self, result: PaperOaResult) -> None:
        if result.evidence_strategy != self.strategy:
            raise OaCollectionError("cache_strategy_mismatch")
        with self._paper_lock(result.paper.source_id):
            if self.strategy == OA_COLLECTION_STRATEGY:
                self._save_oai(result)
            else:
                self._save_article_search(result)

    def _save_article_search(self, result: PaperOaResult) -> None:
        if result.status not in _CACHEABLE_STATUSES:
            return
        _validate_article_search_results((result,))
        payload = {
            "schema_version": ARTICLE_SEARCH_CACHE_SCHEMA_VERSION,
            "strategy": ARTICLE_SEARCH_COLLECTION_STRATEGY,
            "api_code": KCI_ARTICLE_SEARCH_API_CODE,
            "source_id": result.paper.source_id,
            "paper_fingerprint": result.paper.fingerprint,
            "status": result.status,
            "returned_source_id": result.returned_source_id,
            "orte_open_yn": result.orte_open_yn,
            "field_present": result.field_present,
            "observed_value": result.observed_value,
            "collected_at": result.collected_at,
            "error_code": result.error_code,
        }
        _write_json_atomic(self._path(result.paper.source_id), payload)

    def _save_oai(self, result: PaperOaResult) -> None:
        cacheable = result.status in _CACHEABLE_STATUSES or (
            result.status == "failed"
            and result.error_code in self._DETERMINISTIC_OAI_FAILURES
        )
        if not cacheable:
            return
        _validate_oai_results((result,))
        payload = {
            # Keep this exact schema-2.0 shape for active collectors already
            # running with the previous implementation.
            "schema_version": CACHE_SCHEMA_VERSION,
            "strategy": OA_COLLECTION_STRATEGY,
            "source_id": result.paper.source_id,
            "paper_fingerprint": result.paper.fingerprint,
            "status": result.status,
            "returned_source_id": result.returned_source_id,
            "oai_identifier": result.oai_identifier,
            "dc_rights": result.dc_rights,
            "orte_open_yn": result.orte_open_yn,
            "field_present": result.field_present,
            "observed_value": result.observed_value,
            "collected_at": result.collected_at,
            "error_code": result.error_code,
        }
        sidecar = {
            "schema_version": OAI_EVIDENCE_SIDECAR_SCHEMA_VERSION,
            "strategy": OA_COLLECTION_STRATEGY,
            "source_id": result.paper.source_id,
            "paper_fingerprint": result.paper.fingerprint,
            "base_payload_sha256": _json_payload_sha256(payload),
            "oai_identifier": result.oai_identifier,
            "dc_rights": result.dc_rights,
            "dc_rights_field_present": result.dc_rights_field_present,
            "observed_dc_rights": result.observed_dc_rights,
            "kci_orte_open_yn": result.kci_orte_open_yn,
            "kci_orte_open_yn_field_present": (
                result.kci_orte_open_yn_field_present
            ),
            "observed_kci_orte_open_yn": result.observed_kci_orte_open_yn,
        }
        _write_json_atomic(self._path(result.paper.source_id), payload)
        _write_json_atomic(self._evidence_path(result.paper.source_id), sidecar)

    def _load_evidence_sidecar(
        self,
        paper: KciPaper,
        base_payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        payload = self._read_json(self._evidence_path(paper.source_id))
        if payload is None or set(payload) != self._EVIDENCE_KEYS:
            return None
        if (
            payload.get("schema_version") != OAI_EVIDENCE_SIDECAR_SCHEMA_VERSION
            or payload.get("strategy") != OA_COLLECTION_STRATEGY
            or payload.get("source_id") != paper.source_id
            or not paper.matches_fingerprint(payload.get("paper_fingerprint"))
            or payload.get("base_payload_sha256")
            != _json_payload_sha256(base_payload)
            or payload.get("oai_identifier") != base_payload.get("oai_identifier")
            or payload.get("dc_rights") != base_payload.get("dc_rights")
        ):
            return None
        return {
            key: payload[key]
            for key in (
                "oai_identifier",
                "dc_rights",
                "dc_rights_field_present",
                "observed_dc_rights",
                "kci_orte_open_yn",
                "kci_orte_open_yn_field_present",
                "observed_kci_orte_open_yn",
            )
        }

    def _valid_common_payload(
        self,
        payload: Mapping[str, Any],
        paper: KciPaper,
    ) -> bool:
        return bool(
            payload.get("source_id") == paper.source_id
            and paper.matches_fingerprint(payload.get("paper_fingerprint"))
            and payload.get("status") in (_CACHEABLE_STATUSES | {"failed"})
            and isinstance(payload.get("collected_at"), str)
            and self._is_fresh(
                str(payload["status"]),
                payload.get("orte_open_yn"),
                payload["collected_at"],
            )
            and payload.get("returned_source_id") in {None, paper.source_id}
            and payload.get("orte_open_yn") in {None, "Y", "N"}
            and payload.get("field_present") in {None, True, False}
            and (
                payload.get("observed_value") is None
                or isinstance(payload.get("observed_value"), str)
            )
            and (
                payload.get("error_code") is None
                or isinstance(payload.get("error_code"), str)
            )
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def invalidate(self, paper: KciPaper) -> None:
        with self._paper_lock(paper.source_id):
            paths = [self._path(paper.source_id)]
            if self.strategy == OA_COLLECTION_STRATEGY:
                paths.append(self._evidence_path(paper.source_id))
            removed_parents: set[Path] = set()
            for path in paths:
                try:
                    path.unlink()
                    removed_parents.add(path.parent)
                except FileNotFoundError:
                    continue
            for parent in removed_parents:
                _fsync_directory(parent)

    def _is_fresh(
        self,
        status: str,
        orte_open_yn: Any,
        collected_at: str,
    ) -> bool:
        try:
            collected = datetime.fromisoformat(collected_at)
        except ValueError:
            return False
        if collected.tzinfo is None:
            return False
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=SEOUL)
        age = (now.astimezone(SEOUL) - collected.astimezone(SEOUL)).total_seconds()
        if age < -300:
            return False
        if status == "classified" and orte_open_yn == "Y":
            ttl = self.open_access_ttl_seconds
        elif status == "classified" and orte_open_yn == "N":
            # A paywalled paper can later become OA, so negative decisions are
            # deliberately refreshed much sooner than positive decisions.
            ttl = self.not_open_access_ttl_seconds
        else:
            ttl = self.unresolved_ttl_seconds
        return age <= ttl

    def _path(self, source_id: str) -> Path:
        _validate_source_id(source_id)
        if self.strategy == OA_COLLECTION_STRATEGY:
            # Compatibility path used by schema-2.0 collectors already active.
            return self.directory / f"{source_id}.json"
        return self.directory / "_article_search" / f"{source_id}.json"

    def _evidence_path(self, source_id: str) -> Path:
        _validate_source_id(source_id)
        return self.directory / "_evidence" / f"{source_id}.json"

    def _paper_lock_path(self, source_id: str) -> Path:
        _validate_source_id(source_id)
        strategy = (
            "oai" if self.strategy == OA_COLLECTION_STRATEGY else "article_search"
        )
        return self.directory / "_locks" / strategy / f"{source_id}.lock"

    @contextmanager
    def _paper_lock(self, source_id: str) -> Iterator[None]:
        """Serialize a paper's base, evidence sidecar, and invalidation."""

        with _exclusive_file_lock(self._paper_lock_path(source_id)):
            yield


class OaiHarvestCache:
    """Checkpoint an OAI scan after every page so long harvests can resume.

    New checkpoints include the target snapshot in their filename.  A valid
    date-only schema-2 checkpoint from a collector that was already running
    when this code was deployed is copied to the new path on first locked
    load.  The legacy file is intentionally retained because that old process
    may still replace it with a later page.
    """

    def __init__(
        self,
        directory: Path,
        *,
        ttl_seconds: int = DEFAULT_HARVEST_CACHE_TTL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = _positive_int(ttl_seconds, "ttl_seconds")
        self._now = now or (lambda: datetime.now(SEOUL))

    def load(
        self,
        target_source_ids: set[str],
        *,
        from_date: str | None,
        until_date: str | None,
    ) -> OaiHarvestState:
        """Load or losslessly migrate a checkpoint while its window is locked."""

        primary_path = self._path(target_source_ids, from_date, until_date)
        loaded = self._load_path(
            primary_path,
            target_source_ids,
            from_date=from_date,
            until_date=until_date,
        )
        if loaded is not None:
            return loaded[0]

        legacy = self._load_path(
            self._legacy_path(from_date, until_date),
            target_source_ids,
            from_date=from_date,
            until_date=until_date,
        )
        if legacy is None:
            return OaiHarvestState({}, None, False, 0, 0)

        state, payload = legacy
        # Atomic copy, never rename/unlink: active schema-2 processes continue
        # to own the legacy path and cannot clobber this target-scoped file.
        _write_json_atomic(primary_path, payload)
        return state

    def _load_path(
        self,
        path: Path,
        target_source_ids: set[str],
        *,
        from_date: str | None,
        until_date: str | None,
    ) -> tuple[OaiHarvestState, dict[str, Any]] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        expected_keys = {
            "schema_version",
            "strategy",
            "metadata_prefix",
            "set",
            "target_source_ids_sha256",
            "from",
            "until",
            "next_token",
            "exhausted",
            "pages",
            "records_scanned",
            "mappings",
            "updated_at",
        }
        payload_keys = set(payload)
        if (
            payload_keys not in (expected_keys, expected_keys | {"diagnostics"})
            or payload.get("schema_version") != CACHE_SCHEMA_VERSION
            or payload.get("strategy") != OA_COLLECTION_STRATEGY
            or payload.get("metadata_prefix") != KCI_OAI_PRIMARY_PREFIX
            or payload.get("set") != KCI_OAI_SET
            or payload.get("target_source_ids_sha256")
            != _source_id_snapshot_sha256(target_source_ids)
            or payload.get("from") != from_date
            or payload.get("until") != until_date
            or not isinstance(payload.get("exhausted"), bool)
            or not isinstance(payload.get("pages"), int)
            or isinstance(payload.get("pages"), bool)
            or payload.get("pages", -1) < 0
            or not isinstance(payload.get("records_scanned"), int)
            or isinstance(payload.get("records_scanned"), bool)
            or payload.get("records_scanned", -1) < 0
            or not isinstance(payload.get("mappings"), dict)
            or not isinstance(payload.get("updated_at"), str)
            or not self._is_fresh(payload.get("updated_at"))
        ):
            return None
        token = payload.get("next_token")
        if token is not None:
            try:
                _validate_resumption_token(token)
            except ValueError:
                return None
        if payload["exhausted"] and token is not None:
            return None

        mappings: dict[str, OaiDcRecord] = {}
        diagnostics: list[OaiRecordDiagnostic] = []
        try:
            for source_id, raw in payload["mappings"].items():
                if source_id not in target_source_ids or not isinstance(raw, dict):
                    raise ValueError("invalid harvest mapping")
                if set(raw) != {
                    "oai_identifier",
                    "rights",
                    "field_present",
                    "observed_value",
                }:
                    raise ValueError("invalid harvest mapping shape")
                mappings[source_id] = OaiDcRecord(
                    source_id=source_id,
                    oai_identifier=raw["oai_identifier"],
                    rights=raw["rights"],
                    field_present=raw["field_present"],
                    observed_value=raw["observed_value"],
                )
            raw_diagnostics = payload.get("diagnostics", [])
            if not isinstance(raw_diagnostics, list):
                raise ValueError("invalid harvest diagnostics")
            for raw in raw_diagnostics:
                if not isinstance(raw, dict) or set(raw) != {
                    "code",
                    "source_id",
                    "oai_identifier",
                }:
                    raise ValueError("invalid harvest diagnostic shape")
                diagnostic = OaiRecordDiagnostic(
                    code=raw["code"],
                    source_id=raw["source_id"],
                    oai_identifier=raw["oai_identifier"],
                )
                if (
                    diagnostic.source_id is not None
                    and diagnostic.source_id not in target_source_ids
                ):
                    raise ValueError("diagnostic source is outside target snapshot")
                diagnostics.append(diagnostic)
        except (TypeError, ValueError):
            return None
        return (
            OaiHarvestState(
                mappings=mappings,
                next_token=token,
                exhausted=payload["exhausted"],
                pages=payload["pages"],
                records_scanned=payload["records_scanned"],
                diagnostics=tuple(diagnostics),
            ),
            payload,
        )

    def save(
        self,
        state: OaiHarvestState,
        target_source_ids: set[str],
        *,
        from_date: str | None,
        until_date: str | None,
        updated_at: datetime,
    ) -> None:
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "strategy": OA_COLLECTION_STRATEGY,
            "metadata_prefix": KCI_OAI_PRIMARY_PREFIX,
            "set": KCI_OAI_SET,
            "target_source_ids_sha256": _source_id_snapshot_sha256(
                target_source_ids
            ),
            "from": from_date,
            "until": until_date,
            "next_token": state.next_token,
            "exhausted": state.exhausted,
            "pages": state.pages,
            "records_scanned": state.records_scanned,
            "mappings": {
                source_id: {
                    "oai_identifier": record.oai_identifier,
                    "rights": record.rights,
                    "field_present": record.field_present,
                    "observed_value": record.observed_value,
                }
                for source_id, record in sorted(state.mappings.items())
            },
            "diagnostics": [
                {
                    "code": diagnostic.code,
                    "source_id": diagnostic.source_id,
                    "oai_identifier": diagnostic.oai_identifier,
                }
                for diagnostic in sorted(
                    state.diagnostics,
                    key=lambda item: (
                        item.code,
                        item.source_id or "",
                        item.oai_identifier or "",
                    ),
                )
            ],
            "updated_at": _iso_timestamp(updated_at),
        }
        _write_json_atomic(
            self._path(target_source_ids, from_date, until_date), payload
        )

    def invalidate(self) -> None:
        """Reject unsafe global invalidation; reset one locked window instead."""

        raise OaCollectionError("global_harvest_cache_invalidation_disabled")

    @contextmanager
    def window_lock(
        self,
        target_source_ids: set[str],
        *,
        from_date: str | None,
        until_date: str | None,
    ) -> Iterator[None]:
        """Serialize load, network traversal, and saves for one cache identity."""

        with _exclusive_file_lock(
            self._lock_path(target_source_ids, from_date, until_date)
        ):
            yield

    def invalidate_window_locked(
        self,
        target_source_ids: set[str],
        *,
        from_date: str | None,
        until_date: str | None,
    ) -> None:
        """Remove only the target-scoped checkpoint under ``window_lock``.

        The legacy date-only checkpoint is retained for active schema-2
        collectors and is deliberately bypassed by a force run.
        """

        path = self._path(target_source_ids, from_date, until_date)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(path.parent)

    def _identity(
        self,
        target_source_ids: set[str],
        from_date: str | None,
        until_date: str | None,
    ) -> str:
        identity = json.dumps(
            {
                "from": from_date,
                "metadata_prefix": KCI_OAI_PRIMARY_PREFIX,
                "set": KCI_OAI_SET,
                "strategy": OA_COLLECTION_STRATEGY,
                "target_source_ids_sha256": _source_id_snapshot_sha256(
                    target_source_ids
                ),
                "until": until_date,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]

    def _path(
        self,
        target_source_ids: set[str],
        from_date: str | None,
        until_date: str | None,
    ) -> Path:
        suffix = self._identity(target_source_ids, from_date, until_date)
        return self.directory / f"_oai_dc_harvest_v3_{suffix}.json"

    def _lock_path(
        self,
        target_source_ids: set[str],
        from_date: str | None,
        until_date: str | None,
    ) -> Path:
        suffix = self._identity(target_source_ids, from_date, until_date)
        return self.directory / "_locks" / "harvest" / f"{suffix}.lock"

    def _legacy_path(
        self,
        from_date: str | None,
        until_date: str | None,
    ) -> Path:
        identity = json.dumps(
            {"from": from_date, "until": until_date},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        suffix = hashlib.sha256(identity.encode("ascii")).hexdigest()[:16]
        return self.directory / f"_oai_dc_harvest_v2_{suffix}.json"

    def _is_fresh(self, value: str) -> bool:
        try:
            updated = datetime.fromisoformat(value)
        except ValueError:
            return False
        if updated.tzinfo is None:
            return False
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=SEOUL)
        age = (now.astimezone(SEOUL) - updated.astimezone(SEOUL)).total_seconds()
        return -300 <= age <= self.ttl_seconds


def load_kci_papers(database: Path = DEFAULT_DATABASE) -> list[KciPaper]:
    """Load a deterministic, read-only snapshot of all ``kci_papers`` rows."""

    path = Path(database).expanduser().resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise OaCollectionError("database_not_found_or_empty")
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error:
        raise OaCollectionError("database_open_failed") from None
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(kci_papers)")
        }
        required = {"id", "source_id", "title", "doi", "publication_year"}
        if not required <= columns:
            raise OaCollectionError("invalid_kci_papers_schema")
        optional_names = (
            "source",
            "title_kor",
            "title_eng",
            "journal_id",
            "institution_id",
        )
        optional = [
            name if name in columns else f"NULL AS {name}"
            for name in optional_names
        ]
        rows = connection.execute(
            "SELECT id, source_id, title, doi, publication_year, "
            + ", ".join(optional)
            + " FROM kci_papers ORDER BY id ASC"
        ).fetchall()
    except sqlite3.Error:
        raise OaCollectionError("database_query_failed") from None
    finally:
        connection.close()
    if not rows:
        raise OaCollectionError("kci_papers_empty")

    papers: list[KciPaper] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        source = _clean_text(row["source"])
        source_id = _clean_text(row["source_id"])
        title = _clean_text(row["title"])
        if source not in {None, "kci"}:
            raise OaCollectionError("non_kci_row_in_kci_papers")
        if source_id is None or _ART_ID_RE.fullmatch(source_id) is None:
            raise OaCollectionError("invalid_source_id_in_database")
        if source_id in seen:
            raise OaCollectionError("duplicate_source_id_in_database")
        if title is None:
            raise OaCollectionError("missing_title_in_database")
        database_id = row["id"]
        if not isinstance(database_id, int) or isinstance(database_id, bool):
            raise OaCollectionError("invalid_database_id")
        seen.add(source_id)
        doi_raw = _clean_text(row["doi"])
        papers.append(
            KciPaper(
                paper_index=index,
                database_id=database_id,
                source_id=source_id,
                title=title,
                title_kor=_clean_text(row["title_kor"]),
                title_eng=_clean_text(row["title_eng"]),
                doi_raw=doi_raw,
                doi=_normalize_doi(doi_raw),
                publication_year=_optional_year(row["publication_year"]),
                journal_id=_clean_text(row["journal_id"]),
                institution_id=_clean_text(row["institution_id"]),
            )
        )
    return papers


def _recover_record_oai_identifier(
    headers: Sequence[ElementTree.Element],
) -> str | None:
    """Return a valid record identity for diagnostics, never a partial value."""

    if len(headers) != 1:
        return None
    identifiers = _direct_children_ns(
        headers[0], OAI_PMH_NAMESPACE, "identifier"
    )
    if len(identifiers) != 1:
        return None
    identifier = _element_text(identifiers[0])
    if identifier is None or _OAI_IDENTIFIER_RE.fullmatch(identifier) is None:
        return None
    return identifier


def _recover_target_art_ids(
    record: ElementTree.Element,
    target_source_ids: set[str],
) -> tuple[str, ...]:
    """Recover only exact target ART IDs from an otherwise malformed record."""

    recovered = {
        value
        for element in record.iter()
        if element.tag == f"{{{DC_ELEMENTS_NAMESPACE}}}identifier"
        and str(element.get("type", "")).casefold() == "artiid"
        and (value := _element_text(element)) in target_source_ids
    }
    return tuple(sorted(recovered))


def _is_kci_unqualified_root_no_records_match(
    root: ElementTree.Element,
) -> bool:
    """Recognize KCI's one known broken, data-free ListRecords envelope.

    Some fixed historical windows return an unqualified ``OAI-PMH`` root even
    though its three children use the official namespace.  This exception is
    intentionally narrower than normal envelope validation: all three
    children must be leaves, the request must still identify ListRecords, and
    the sole error must be exactly ``noRecordsMatch``.  Any data container,
    extra/mixed child, nonstandard attribute, or other error remains invalid.
    """

    allowed_schema_attribute = {
        f"{{{XML_SCHEMA_INSTANCE_NAMESPACE}}}schemaLocation": (
            OAI_PMH_SCHEMA_LOCATION
        )
    }
    if root.tag != "OAI-PMH" or root.attrib not in (
        {},
        allowed_schema_attribute,
    ):
        return False
    children = list(root)
    expected_tags = (
        f"{{{OAI_PMH_NAMESPACE}}}responseDate",
        f"{{{OAI_PMH_NAMESPACE}}}request",
        f"{{{OAI_PMH_NAMESPACE}}}error",
    )
    if tuple(child.tag for child in children) != expected_tags:
        return False
    response_date, request, error = children
    if any(list(child) for child in children):
        return False
    if set(request.attrib) != {
        "verb",
        "metadataPrefix",
        "set",
        "from",
        "until",
    }:
        return False
    if (
        request.get("verb") != "ListRecords"
        or request.get("metadataPrefix") != KCI_OAI_PRIMARY_PREFIX
        or request.get("set") != KCI_OAI_SET
        or _element_text(request)
        not in {KCI_OAI_PMH_URL, KCI_OAI_PMH_LEGACY_ECHO_URL}
    ):
        return False
    try:
        from_date = _validate_oai_date(request.get("from", ""))
        until_date = _validate_oai_date(request.get("until", ""))
    except OaCollectionError:
        return False
    if from_date > until_date:
        return False
    if set(error.attrib) != {"code"} or error.get("code") != "noRecordsMatch":
        return False
    return _element_text(response_date) is not None


def parse_oai_dc_list_records_xml(
    body: bytes,
    *,
    target_source_ids: set[str],
    max_elements: int = 250_000,
    max_depth: int = 96,
) -> OaiListPage:
    """Parse one OAI ``oai_dc`` page and retain only requested ART IDs."""

    for source_id in target_source_ids:
        _validate_source_id(source_id)
    root = _safe_parse_xml(body, max_elements=max_elements, max_depth=max_depth)
    if _is_kci_unqualified_root_no_records_match(root):
        return OaiListPage((), None, 0, no_records_match=True)
    errors = _validated_oai_envelope(root)
    if errors:
        if errors == (("noRecordsMatch", ""),) or (
            len(errors) == 1 and errors[0][0] == "noRecordsMatch"
        ):
            return OaiListPage((), None, 0, no_records_match=True)
        raise FatalOaError(_oai_error_code(errors))

    containers = _direct_children_ns(root, OAI_PMH_NAMESPACE, "ListRecords")
    if len(containers) != 1:
        raise InvalidOaResponse("invalid_oai_listrecords_envelope")
    container = containers[0]
    records = _direct_children_ns(container, OAI_PMH_NAMESPACE, "record")
    tokens = _direct_children_ns(container, OAI_PMH_NAMESPACE, "resumptionToken")
    if len(tokens) > 1:
        raise InvalidOaResponse("multiple_resumption_tokens")
    next_token = _element_text(tokens[0]) if tokens else None
    if next_token is not None:
        try:
            _validate_resumption_token(next_token)
        except ValueError:
            raise InvalidOaResponse("invalid_resumption_token") from None

    matches: dict[str, OaiDcRecord] = {}
    diagnostics: dict[
        tuple[str, str | None, str | None], OaiRecordDiagnostic
    ] = {}
    invalid_source_ids: set[str] = set()

    def retain_diagnostic(
        code: str,
        *,
        source_ids: Sequence[str] = (),
        oai_identifier: str | None = None,
    ) -> None:
        associated_ids: Sequence[str | None] = source_ids or (None,)
        for source_id in associated_ids:
            diagnostic = OaiRecordDiagnostic(
                code=code,
                source_id=source_id,
                oai_identifier=oai_identifier,
            )
            diagnostics[
                (diagnostic.code, diagnostic.source_id, diagnostic.oai_identifier)
            ] = diagnostic
            if source_id is not None:
                invalid_source_ids.add(source_id)
                matches.pop(source_id, None)

    for record in records:
        headers = _direct_children_ns(record, OAI_PMH_NAMESPACE, "header")
        metadata_nodes = _direct_children_ns(
            record, OAI_PMH_NAMESPACE, "metadata"
        )
        recovered_oai_identifier = _recover_record_oai_identifier(headers)
        recovered_target_ids = _recover_target_art_ids(
            record, target_source_ids
        )
        if len(headers) != 1:
            retain_diagnostic(
                "invalid_oai_record_header",
                source_ids=recovered_target_ids,
                oai_identifier=recovered_oai_identifier,
            )
            continue
        header = headers[0]
        if _clean_text(header.get("status")) == "deleted":
            continue
        if len(metadata_nodes) != 1 or len(list(metadata_nodes[0])) != 1:
            retain_diagnostic(
                "invalid_oai_record_metadata",
                source_ids=recovered_target_ids,
                oai_identifier=recovered_oai_identifier,
            )
            continue
        metadata = list(metadata_nodes[0])[0]
        if metadata.tag != f"{{{OAI_DC_CONTAINER_NAMESPACE}}}dc":
            retain_diagnostic(
                "unexpected_oai_metadata_prefix",
                source_ids=recovered_target_ids,
                oai_identifier=recovered_oai_identifier,
            )
            continue

        arti_nodes = [
            child
            for child in list(metadata)
            if child.tag == f"{{{DC_ELEMENTS_NAMESPACE}}}identifier"
            and str(child.get("type", "")).casefold() == "artiid"
        ]
        if len(arti_nodes) != 1:
            retain_diagnostic(
                "invalid_oai_art_id_count",
                source_ids=recovered_target_ids,
                oai_identifier=recovered_oai_identifier,
            )
            continue
        source_id = _element_text(arti_nodes[0])
        if source_id not in target_source_ids:
            continue
        # Membership in target_source_ids proves the ART ID was validated by
        # the caller before parsing.
        if source_id is None:  # pragma: no cover - guarded by membership.
            raise AssertionError("target source ID unexpectedly missing")

        identifier_nodes = _direct_children_ns(
            header, OAI_PMH_NAMESPACE, "identifier"
        )
        if len(identifier_nodes) != 1:
            retain_diagnostic(
                "invalid_oai_header_identifier", source_ids=(source_id,)
            )
            continue
        oai_identifier = _element_text(identifier_nodes[0])
        if oai_identifier is None:
            retain_diagnostic(
                "invalid_oai_header_identifier", source_ids=(source_id,)
            )
            continue
        try:
            _validate_oai_identifier(oai_identifier)
        except ValueError:
            retain_diagnostic(
                "invalid_oai_header_identifier", source_ids=(source_id,)
            )
            continue

        rights_nodes = [
            child
            for child in list(metadata)
            if child.tag == f"{{{DC_ELEMENTS_NAMESPACE}}}rights"
        ]
        if len(rights_nodes) > 1:
            primary = OaiDcRecord(
                source_id,
                oai_identifier,
                None,
                True,
                "multiple_values",
            )
        elif not rights_nodes:
            primary = OaiDcRecord(source_id, oai_identifier, None, False)
        else:
            observed = _element_text(rights_nodes[0])
            canonical = observed.upper() if observed is not None else None
            if canonical in {"Y", "N"}:
                primary = OaiDcRecord(
                    source_id,
                    oai_identifier,
                    canonical,
                    True,
                )
            else:
                primary = OaiDcRecord(
                    source_id,
                    oai_identifier,
                    None,
                    True,
                    observed[:256] if observed is not None else None,
                )
        previous = matches.get(source_id)
        if previous is not None and previous != primary:
            retain_diagnostic(
                "conflicting_oai_dc_records",
                source_ids=(source_id,),
                oai_identifier=oai_identifier,
            )
            continue
        if source_id in invalid_source_ids:
            continue
        matches[source_id] = primary
    return OaiListPage(
        matches=tuple(matches.values()),
        next_token=next_token,
        record_count=len(records),
        diagnostics=tuple(diagnostics.values()),
    )


def parse_oai_kci_get_record_xml(
    body: bytes,
    *,
    expected_oai_identifier: str,
    expected_source_id: str,
    max_elements: int = 100_000,
    max_depth: int = 96,
) -> ParsedOaValue:
    """Parse an ``oai_kci`` GetRecord response and verify both identities."""

    _validate_oai_identifier(expected_oai_identifier)
    _validate_source_id(expected_source_id)
    root = _safe_parse_xml(body, max_elements=max_elements, max_depth=max_depth)
    errors = _validated_oai_envelope(root)
    if errors:
        if len(errors) == 1 and errors[0][0] in {
            "idDoesNotExist",
            "noRecordsMatch",
        }:
            return ParsedOaValue("no_record", None, None, None)
        raise FatalOaError(_oai_error_code(errors))

    containers = _direct_children_ns(root, OAI_PMH_NAMESPACE, "GetRecord")
    if len(containers) != 1:
        raise InvalidOaResponse("invalid_oai_getrecord_envelope")
    records = _direct_children_ns(
        containers[0], OAI_PMH_NAMESPACE, "record"
    )
    if len(records) != 1:
        raise InvalidOaResponse("unexpected_oai_record_count")
    record = records[0]
    headers = _direct_children_ns(record, OAI_PMH_NAMESPACE, "header")
    metadata_nodes = _direct_children_ns(record, OAI_PMH_NAMESPACE, "metadata")
    if len(headers) != 1 or len(metadata_nodes) != 1:
        raise InvalidOaResponse("invalid_oai_getrecord_record")
    identifiers = _direct_children_ns(
        headers[0], OAI_PMH_NAMESPACE, "identifier"
    )
    if len(identifiers) != 1 or _element_text(identifiers[0]) != expected_oai_identifier:
        raise InvalidOaResponse("oai_identifier_mismatch")
    if len(list(metadata_nodes[0])) != 1:
        raise InvalidOaResponse("invalid_oai_getrecord_metadata")
    metadata = list(metadata_nodes[0])[0]
    if metadata.tag != f"{{{KCI_OAI_METADATA_NAMESPACE}}}oai_kci":
        raise InvalidOaResponse("unexpected_oai_metadata_prefix")
    article_infos = [
        child
        for child in list(metadata)
        if child.tag == f"{{{KCI_OAI_METADATA_NAMESPACE}}}articleInfo"
    ]
    if len(article_infos) != 1:
        raise InvalidOaResponse("invalid_article_info")
    article_info = article_infos[0]
    returned_source_id = _clean_text(article_info.get("article-id"))
    if returned_source_id != expected_source_id:
        raise InvalidOaResponse("article_identity_mismatch")
    return _parsed_orte_open_yn(
        article_info,
        returned_source_id,
        namespace=KCI_OAI_METADATA_NAMESPACE,
    )


def _parsed_orte_open_yn(
    article_info: ElementTree.Element,
    returned_source_id: str,
    *,
    namespace: str | None = None,
) -> ParsedOaValue:
    expected_tag = (
        f"{{{namespace}}}orte-open-yn" if namespace is not None else None
    )
    fields = [
        child
        for child in list(article_info)
        if (
            child.tag == expected_tag
            if expected_tag is not None
            else _local_name(child.tag) == "orte-open-yn"
        )
    ]
    if len(fields) > 1:
        raise InvalidOaResponse("multiple_orte_open_yn_fields")
    if not fields:
        return ParsedOaValue("field_missing", returned_source_id, None, False)
    observed = _element_text(fields[0])
    if observed is None:
        return ParsedOaValue("field_missing", returned_source_id, None, True)
    canonical = observed.upper()
    if canonical in {"Y", "N"}:
        return ParsedOaValue(
            "classified", returned_source_id, canonical, True
        )
    return ParsedOaValue(
        "invalid_value",
        returned_source_id,
        None,
        True,
        observed_value=observed[:256],
    )


def parse_article_search_xml(
    body: bytes,
    *,
    expected_source_id: str,
    max_elements: int = 100_000,
    max_depth: int = 96,
) -> ParsedOaValue:
    """Parse one exact-ID response without retaining its key-bearing XML."""

    _validate_source_id(expected_source_id)
    root = _safe_parse_xml(body, max_elements=max_elements, max_depth=max_depth)
    records = _validated_records(root)
    if not records:
        return ParsedOaValue("no_record", None, None, None)
    if len(records) != 1:
        raise InvalidOaResponse("unexpected_record_count")

    article_infos = [
        child
        for child in list(records[0])
        if _local_name(child.tag) == "articleInfo"
    ]
    if len(article_infos) != 1:
        raise InvalidOaResponse("invalid_article_info")
    article_info = article_infos[0]
    returned_source_id = _clean_text(article_info.get("article-id"))
    if returned_source_id != expected_source_id:
        raise InvalidOaResponse("article_identity_mismatch")

    return _parsed_orte_open_yn(article_info, returned_source_id)


def collect_open_access(
    papers: Sequence[KciPaper],
    client: ArticleSearchClient,
    *,
    cache: OaCache | None = None,
    force: bool = False,
    retries: int = DEFAULT_RETRIES,
    logger: ProgressLogger | None = None,
    log_every: int = 10,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> OaCollectionRun:
    """Collect every paper sequentially and checkpoint each parsed outcome."""

    if retries < 0:
        raise ValueError("retries must be non-negative")
    if log_every < 1:
        raise ValueError("log_every must be positive")
    if cache is not None and cache.strategy != ARTICLE_SEARCH_COLLECTION_STRATEGY:
        raise OaCollectionError("cache_strategy_mismatch")
    if len({paper.source_id for paper in papers}) != len(papers):
        raise OaCollectionError("duplicate_input_source_id")
    timestamp = now or (lambda: datetime.now(SEOUL))
    results: list[PaperOaResult] = []
    cache_hits = 0
    counters: Counter[str] = Counter()
    total = len(papers)

    for position, paper in enumerate(papers, start=1):
        if force and cache is not None:
            cache.invalidate(paper)
        cached = None if force or cache is None else cache.load(paper)
        if cached is not None:
            result = cached
            cache_hits += 1
        else:
            result = _collect_one(
                paper,
                client,
                retries=retries,
                sleeper=sleeper,
                now=timestamp,
            )
            if cache is not None:
                cache.save(result)
        results.append(result)
        if result.orte_open_yn in {"Y", "N"}:
            counters[result.orte_open_yn] += 1
        elif result.status == "failed":
            counters["failed"] += 1
        else:
            counters["unknown"] += 1
        if logger is not None and (
            position == 1
            or position == total
            or position % log_every == 0
            or result.status == "failed"
        ):
            suffix = " (cache)" if result.cache_hit else ""
            logger.emit(
                "KCI-OA",
                f"{paper.source_id} {result.status}{suffix}",
                current=position,
                total=total,
                counters=counters,
            )

    _validate_results(papers, results)
    return OaCollectionRun(
        results=tuple(results),
        network_requests=int(getattr(client, "request_count", 0)),
        cache_hits=cache_hits,
        strategy=ARTICLE_SEARCH_COLLECTION_STRATEGY,
    )


def _collect_one(
    paper: KciPaper,
    client: ArticleSearchClient,
    *,
    retries: int,
    sleeper: Callable[[float], None],
    now: Callable[[], datetime],
) -> PaperOaResult:
    for attempt in range(1, retries + 2):
        try:
            parsed = client.fetch(paper.source_id)
            error_code = {
                "classified": None,
                "field_missing": "orte_open_yn_missing",
                "invalid_value": "orte_open_yn_invalid",
                "no_record": "article_not_found",
            }[parsed.status]
            return PaperOaResult(
                paper=paper,
                status=parsed.status,
                returned_source_id=parsed.returned_source_id,
                orte_open_yn=parsed.orte_open_yn,
                field_present=parsed.field_present,
                observed_value=parsed.observed_value,
                collected_at=_iso_timestamp(now()),
                attempts=attempt,
                error_code=error_code,
            )
        except FatalOaError:
            raise
        except RetryableOaError as exc:
            if attempt > retries:
                return _failed_result(paper, attempt, exc.code, now)
            delay = max(float(2 ** (attempt - 1)), exc.retry_after or 0.0)
            sleeper(min(delay, 60.0))
        except InvalidOaResponse as exc:
            return _failed_result(paper, attempt, exc.code, now)
        except Exception:
            # Exception strings may contain response fragments or credentials.
            return _failed_result(paper, attempt, "unexpected_error", now)
    raise AssertionError("unreachable")


def _failed_result(
    paper: KciPaper,
    attempts: int,
    error_code: str,
    now: Callable[[], datetime],
    *,
    evidence_strategy: str = ARTICLE_SEARCH_COLLECTION_STRATEGY,
    returned_source_id: str | None = None,
    oai_identifier: str | None = None,
    dc_rights: str | None = None,
    dc_rights_field_present: bool | None = None,
    observed_dc_rights: str | None = None,
    kci_orte_open_yn: str | None = None,
    kci_orte_open_yn_field_present: bool | None = None,
    observed_kci_orte_open_yn: str | None = None,
) -> PaperOaResult:
    return PaperOaResult(
        paper=paper,
        status="failed",
        returned_source_id=returned_source_id,
        orte_open_yn=None,
        field_present=None,
        observed_value=None,
        collected_at=_iso_timestamp(now()),
        attempts=attempts,
        error_code=error_code,
        evidence_strategy=evidence_strategy,
        oai_identifier=oai_identifier,
        dc_rights=dc_rights,
        dc_rights_field_present=dc_rights_field_present,
        observed_dc_rights=observed_dc_rights,
        kci_orte_open_yn=kci_orte_open_yn,
        kci_orte_open_yn_field_present=kci_orte_open_yn_field_present,
        observed_kci_orte_open_yn=observed_kci_orte_open_yn,
    )


def collect_open_access_oai(
    papers: Sequence[KciPaper],
    client: OaiPmhClient,
    *,
    cache: OaCache | None = None,
    harvest_cache: OaiHarvestCache | None = None,
    force: bool = False,
    retries: int = DEFAULT_RETRIES,
    logger: ProgressLogger | None = None,
    log_every: int = 10,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
    from_date: str | None = None,
    until_date: str | None = None,
    priority_dates: Sequence[str] = (),
    max_pages_per_window: int = DEFAULT_MAX_OAI_PAGES_PER_WINDOW,
) -> OaCollectionRun:
    """Harvest OAI records, cross-check each match, and preserve DB row order."""

    if retries < 0:
        raise ValueError("retries must be non-negative")
    if log_every < 1:
        raise ValueError("log_every must be positive")
    if cache is not None and cache.strategy != OA_COLLECTION_STRATEGY:
        raise OaCollectionError("cache_strategy_mismatch")
    max_pages_per_window = _positive_int(
        max_pages_per_window, "max_pages_per_window"
    )
    if len({paper.source_id for paper in papers}) != len(papers):
        raise OaCollectionError("duplicate_input_source_id")
    clock = now or (lambda: datetime.now(SEOUL))
    run_started_at = clock()
    if run_started_at.tzinfo is None:
        run_started_at = run_started_at.replace(tzinfo=SEOUL)
    else:
        run_started_at = run_started_at.astimezone(SEOUL)
    timestamp = lambda: run_started_at
    run_today = run_started_at.date().isoformat()
    fixed_until = _validate_oai_date(until_date or run_today)
    # A future caller-provided upper bound still covers the repository as of
    # run start, but must not generate requests/checkpoints for future dates.
    harvest_until = min(fixed_until, run_today)
    windows = _oai_date_windows(
        from_date=from_date,
        until_date=harvest_until,
        priority_dates=priority_dates,
    )
    effective_lower = _validate_oai_date(from_date or KCI_OAI_EARLIEST_DATE)
    full_repository_scope = (
        effective_lower == KCI_OAI_EARLIEST_DATE
        and fixed_until >= run_today
    )
    target_source_ids = {paper.source_id for paper in papers}

    if force:
        if cache is not None:
            for paper in papers:
                cache.invalidate(paper)

    by_source_id: dict[str, PaperOaResult] = {}
    cache_hits = 0
    if not force and cache is not None:
        for paper in papers:
            cached = cache.load(paper)
            if cached is not None:
                by_source_id[paper.source_id] = cached
                cache_hits += 1
    pending = target_source_ids - set(by_source_id)
    mappings: dict[str, OaiDcRecord] = {}
    record_diagnostics: dict[
        tuple[str, str | None, str | None], OaiRecordDiagnostic
    ] = {}
    invalid_source_ids: set[str] = set()
    # Exhausting a caller-limited date range does not prove that an article is
    # absent from the repository.  Only the official earliestDatestamp through
    # the run-start date is a complete repository-as-of negative search.
    scan_complete = full_repository_scope

    for window_index, (window_from, window_until) in enumerate(windows, start=1):
        if not pending:
            break
        state, succeeded = _harvest_oai_window(
            client,
            target_source_ids=target_source_ids,
            needed_source_ids=pending,
            harvest_cache=harvest_cache,
            from_date=window_from,
            until_date=window_until,
            retries=retries,
            logger=logger,
            window_index=window_index,
            window_count=len(windows),
            sleeper=sleeper,
            now=timestamp,
            max_pages=max_pages_per_window,
            force_reset=force,
        )
        for diagnostic in state.diagnostics:
            record_diagnostics[
                (diagnostic.code, diagnostic.source_id, diagnostic.oai_identifier)
            ] = diagnostic
            if diagnostic.source_id is not None:
                invalid_source_ids.add(diagnostic.source_id)
                mappings.pop(diagnostic.source_id, None)
        for source_id, record in state.mappings.items():
            if source_id in invalid_source_ids:
                continue
            previous = mappings.get(source_id)
            if previous is not None and previous != record:
                diagnostic = OaiRecordDiagnostic(
                    "conflicting_oai_window_records",
                    source_id=source_id,
                    oai_identifier=record.oai_identifier,
                )
                record_diagnostics[
                    (
                        diagnostic.code,
                        diagnostic.source_id,
                        diagnostic.oai_identifier,
                    )
                ] = diagnostic
                invalid_source_ids.add(source_id)
                mappings.pop(source_id, None)
                continue
            mappings[source_id] = record
        pending -= set(mappings)
        if not succeeded:
            scan_complete = False
            break

    if pending and len(windows) == 0:
        scan_complete = False

    counters: Counter[str] = Counter()
    uncached_papers = [
        paper for paper in papers if paper.source_id not in by_source_id
    ]
    for position, paper in enumerate(uncached_papers, start=1):
        primary = mappings.get(paper.source_id)
        paper_diagnostics = sorted(
            (
                item
                for item in record_diagnostics.values()
                if item.source_id == paper.source_id
            ),
            key=lambda item: (item.code, item.oai_identifier or ""),
        )
        if paper_diagnostics:
            diagnostic = paper_diagnostics[0]
            result = _failed_result(
                paper,
                0,
                diagnostic.code,
                timestamp,
                evidence_strategy=OA_COLLECTION_STRATEGY,
                oai_identifier=diagnostic.oai_identifier,
            )
        elif primary is None:
            if scan_complete:
                result = PaperOaResult(
                    paper=paper,
                    status="no_record",
                    returned_source_id=None,
                    orte_open_yn=None,
                    field_present=None,
                    observed_value=None,
                    collected_at=_iso_timestamp(timestamp()),
                    attempts=0,
                    error_code="article_not_found",
                    evidence_strategy=OA_COLLECTION_STRATEGY,
                )
            else:
                result = _failed_result(
                    paper,
                    0,
                    "oai_harvest_incomplete",
                    timestamp,
                    evidence_strategy=OA_COLLECTION_STRATEGY,
                )
        else:
            result = _verify_oai_record(
                paper,
                primary,
                client,
                retries=retries,
                sleeper=sleeper,
                now=timestamp,
            )
        by_source_id[paper.source_id] = result
        if cache is not None:
            cache.save(result)
        if result.orte_open_yn in {"Y", "N"}:
            counters[result.orte_open_yn] += 1
        elif result.status == "failed":
            counters["failed"] += 1
        else:
            counters["unknown"] += 1
        suppress_incomplete_detail = (
            result.error_code == "oai_harvest_incomplete"
        )
        if logger is not None and not suppress_incomplete_detail and (
            position == 1
            or position == len(uncached_papers)
            or position % log_every == 0
            or result.status == "failed"
        ):
            logger.emit(
                "KCI-OA",
                f"OAI 교차검증 {paper.source_id} {result.status}",
                current=position,
                total=len(uncached_papers),
                counters=counters,
            )

    results = tuple(by_source_id[paper.source_id] for paper in papers)
    _validate_results(papers, results)
    _validate_oai_results(results)
    return OaCollectionRun(
        results=results,
        network_requests=int(getattr(client, "request_count", 0)),
        cache_hits=cache_hits,
        strategy=OA_COLLECTION_STRATEGY,
    )


def _harvest_oai_window(
    client: OaiPmhClient,
    *,
    target_source_ids: set[str],
    needed_source_ids: set[str],
    harvest_cache: OaiHarvestCache | None,
    from_date: str,
    until_date: str,
    retries: int,
    logger: ProgressLogger | None,
    window_index: int,
    window_count: int,
    sleeper: Callable[[float], None],
    now: Callable[[], datetime],
    max_pages: int,
    force_reset: bool,
) -> tuple[OaiHarvestState, bool]:
    if harvest_cache is None:
        return _harvest_oai_window_locked(
            client,
            target_source_ids=target_source_ids,
            needed_source_ids=needed_source_ids,
            harvest_cache=None,
            from_date=from_date,
            until_date=until_date,
            retries=retries,
            logger=logger,
            window_index=window_index,
            window_count=window_count,
            sleeper=sleeper,
            now=now,
            max_pages=max_pages,
            resume=False,
        )

    # The lock spans the initial load, every OAI request/token transition, and
    # every atomic save.  Locking only each save would still permit a delayed
    # writer to replace a later checkpoint with its stale token.
    with harvest_cache.window_lock(
        target_source_ids,
        from_date=from_date,
        until_date=until_date,
    ):
        if force_reset:
            harvest_cache.invalidate_window_locked(
                target_source_ids,
                from_date=from_date,
                until_date=until_date,
            )
        return _harvest_oai_window_locked(
            client,
            target_source_ids=target_source_ids,
            needed_source_ids=needed_source_ids,
            harvest_cache=harvest_cache,
            from_date=from_date,
            until_date=until_date,
            retries=retries,
            logger=logger,
            window_index=window_index,
            window_count=window_count,
            sleeper=sleeper,
            now=now,
            max_pages=max_pages,
            resume=not force_reset,
        )


def _harvest_oai_window_locked(
    client: OaiPmhClient,
    *,
    target_source_ids: set[str],
    needed_source_ids: set[str],
    harvest_cache: OaiHarvestCache | None,
    from_date: str,
    until_date: str,
    retries: int,
    logger: ProgressLogger | None,
    window_index: int,
    window_count: int,
    sleeper: Callable[[float], None],
    now: Callable[[], datetime],
    max_pages: int,
    resume: bool,
) -> tuple[OaiHarvestState, bool]:
    state = (
        harvest_cache.load(
            target_source_ids,
            from_date=from_date,
            until_date=until_date,
        )
        if harvest_cache is not None and resume
        else OaiHarvestState({}, None, False, 0, 0)
    )
    mappings = dict(state.mappings)
    diagnostics = {
        (item.code, item.source_id, item.oai_identifier): item
        for item in state.diagnostics
    }
    invalid_source_ids = {
        item.source_id for item in diagnostics.values() if item.source_id is not None
    }
    for source_id in invalid_source_ids:
        mappings.pop(source_id, None)
    if needed_source_ids <= set(mappings) or state.exhausted:
        return OaiHarvestState(
            mappings,
            state.next_token,
            state.exhausted,
            state.pages,
            state.records_scanned,
            tuple(diagnostics.values()),
        ), True

    token = state.next_token
    pages = state.pages
    records_scanned = state.records_scanned
    seen_tokens: set[str] = {token} if token is not None else set()
    restarted_after_bad_token = False
    while True:
        if pages >= max_pages:
            raise InvalidOaResponse("oai_page_limit_exceeded")
        try:
            page = _fetch_oai_page_with_retries(
                client,
                target_source_ids=target_source_ids,
                resumption_token=token,
                from_date=from_date,
                until_date=until_date,
                retries=retries,
                sleeper=sleeper,
            )
        except FatalOaError as exc:
            # Resumption tokens may expire before the seven-day checkpoint
            # TTL. Restart this one window once; a second failure is a real
            # endpoint error and must abort rather than loop forever.
            if (
                exc.code == "oai_badResumptionToken"
                and token is not None
                and not restarted_after_bad_token
            ):
                mappings = {}
                diagnostics = {}
                invalid_source_ids.clear()
                token = None
                pages = 0
                records_scanned = 0
                seen_tokens.clear()
                restarted_after_bad_token = True
                continue
            raise
        except RetryableOaError:
            return OaiHarvestState(
                mappings,
                token,
                False,
                pages,
                records_scanned,
                tuple(diagnostics.values()),
            ), False

        for diagnostic in page.diagnostics:
            diagnostics[
                (diagnostic.code, diagnostic.source_id, diagnostic.oai_identifier)
            ] = diagnostic
            if diagnostic.source_id is not None:
                invalid_source_ids.add(diagnostic.source_id)
                mappings.pop(diagnostic.source_id, None)
        for record in page.matches:
            if record.source_id in invalid_source_ids:
                continue
            previous = mappings.get(record.source_id)
            if previous is not None and previous != record:
                diagnostic = OaiRecordDiagnostic(
                    "conflicting_oai_dc_records",
                    source_id=record.source_id,
                    oai_identifier=record.oai_identifier,
                )
                diagnostics[
                    (
                        diagnostic.code,
                        diagnostic.source_id,
                        diagnostic.oai_identifier,
                    )
                ] = diagnostic
                invalid_source_ids.add(record.source_id)
                mappings.pop(record.source_id, None)
                continue
            mappings[record.source_id] = record
        pages += 1
        records_scanned += page.record_count
        exhausted = page.no_records_match or page.next_token is None
        state = OaiHarvestState(
            mappings=mappings,
            next_token=None if exhausted else page.next_token,
            exhausted=exhausted,
            pages=pages,
            records_scanned=records_scanned,
            diagnostics=tuple(diagnostics.values()),
        )
        if harvest_cache is not None:
            harvest_cache.save(
                state,
                target_source_ids,
                from_date=from_date,
                until_date=until_date,
                updated_at=now(),
            )
        if logger is not None and (pages == 1 or pages % 10 == 0 or exhausted):
            matched_pending = len(set(mappings) & needed_source_ids)
            logger.emit(
                "KCI-OA",
                (
                    f"OAI harvest {window_from_text(from_date, until_date)} "
                    f"page={pages}, scanned={records_scanned}, "
                    f"matched_total={len(mappings)}, "
                    f"matched_pending={matched_pending}, "
                    f"record_diagnostics={len(diagnostics)} "
                    f"(window {window_index}/{window_count})"
                ),
                current=matched_pending,
                total=len(needed_source_ids),
            )
        if needed_source_ids <= set(mappings) or exhausted:
            return state, True
        # The token is opaque: do not parse, construct, or append metadataPrefix.
        next_token = page.next_token
        if next_token is None:
            raise AssertionError("non-exhausted OAI page must have a token")
        if next_token in seen_tokens:
            raise InvalidOaResponse("resumption_token_loop")
        seen_tokens.add(next_token)
        token = next_token


def _fetch_oai_page_with_retries(
    client: OaiPmhClient,
    *,
    target_source_ids: set[str],
    resumption_token: str | None,
    from_date: str,
    until_date: str,
    retries: int,
    sleeper: Callable[[float], None],
) -> OaiListPage:
    for attempt in range(1, retries + 2):
        try:
            return client.fetch_dc_page(
                target_source_ids=target_source_ids,
                resumption_token=resumption_token,
                from_date=from_date if resumption_token is None else None,
                until_date=until_date if resumption_token is None else None,
            )
        except RetryableOaError as exc:
            if attempt > retries:
                raise
            delay = max(float(2 ** (attempt - 1)), exc.retry_after or 0.0)
            sleeper(min(delay, 60.0))
    raise AssertionError("unreachable")


def _verify_oai_record(
    paper: KciPaper,
    primary: OaiDcRecord,
    client: OaiPmhClient,
    *,
    retries: int,
    sleeper: Callable[[float], None],
    now: Callable[[], datetime],
) -> PaperOaResult:
    primary_evidence = {
        "evidence_strategy": OA_COLLECTION_STRATEGY,
        "oai_identifier": primary.oai_identifier,
        "dc_rights": primary.rights,
        "dc_rights_field_present": primary.field_present,
        "observed_dc_rights": primary.observed_value,
    }
    for attempt in range(1, retries + 2):
        try:
            verified = client.fetch_kci_record(
                oai_identifier=primary.oai_identifier,
                expected_source_id=paper.source_id,
            )
            if verified.status == "no_record":
                return _failed_result(
                    paper,
                    attempt,
                    "oai_kci_record_not_found",
                    now,
                    **primary_evidence,
                )
            if verified.returned_source_id != paper.source_id:
                raise InvalidOaResponse("article_identity_mismatch")

            verification_evidence = {
                "kci_orte_open_yn": verified.orte_open_yn,
                "kci_orte_open_yn_field_present": verified.field_present,
                "observed_kci_orte_open_yn": verified.observed_value,
            }
            if primary.rights is None:
                status = (
                    "field_missing"
                    if primary.observed_value is None
                    else "invalid_value"
                )
                return PaperOaResult(
                    paper=paper,
                    status=status,
                    returned_source_id=verified.returned_source_id,
                    orte_open_yn=None,
                    field_present=primary.field_present,
                    observed_value=primary.observed_value,
                    collected_at=_iso_timestamp(now()),
                    attempts=attempt,
                    error_code=(
                        "dc_rights_missing"
                        if status == "field_missing"
                        else "dc_rights_invalid"
                    ),
                    **primary_evidence,
                    **verification_evidence,
                )
            if verified.status == "field_missing":
                return PaperOaResult(
                    paper=paper,
                    status="field_missing",
                    returned_source_id=verified.returned_source_id,
                    orte_open_yn=None,
                    field_present=verified.field_present,
                    observed_value=None,
                    collected_at=_iso_timestamp(now()),
                    attempts=attempt,
                    error_code="orte_open_yn_missing",
                    **primary_evidence,
                    **verification_evidence,
                )
            if verified.status == "invalid_value":
                return PaperOaResult(
                    paper=paper,
                    status="invalid_value",
                    returned_source_id=verified.returned_source_id,
                    orte_open_yn=None,
                    field_present=True,
                    observed_value=verified.observed_value,
                    collected_at=_iso_timestamp(now()),
                    attempts=attempt,
                    error_code="orte_open_yn_invalid",
                    **primary_evidence,
                    **verification_evidence,
                )
            if verified.orte_open_yn != primary.rights:
                return _failed_result(
                    paper,
                    attempt,
                    "oai_oa_value_mismatch",
                    now,
                    returned_source_id=verified.returned_source_id,
                    **primary_evidence,
                    **verification_evidence,
                )
            return PaperOaResult(
                paper=paper,
                status="classified",
                returned_source_id=verified.returned_source_id,
                orte_open_yn=verified.orte_open_yn,
                field_present=True,
                observed_value=None,
                collected_at=_iso_timestamp(now()),
                attempts=attempt,
                **primary_evidence,
                **verification_evidence,
            )
        except FatalOaError:
            raise
        except RetryableOaError as exc:
            if attempt > retries:
                return _failed_result(
                    paper,
                    attempt,
                    exc.code,
                    now,
                    **primary_evidence,
                )
            delay = max(float(2 ** (attempt - 1)), exc.retry_after or 0.0)
            sleeper(min(delay, 60.0))
        except InvalidOaResponse as exc:
            return _failed_result(
                paper,
                attempt,
                exc.code,
                now,
                **primary_evidence,
            )
        except Exception:
            return _failed_result(
                paper,
                attempt,
                "unexpected_error",
                now,
                **primary_evidence,
            )
    raise AssertionError("unreachable")


def _oai_date_windows(
    *,
    from_date: str | None,
    until_date: str,
    priority_dates: Sequence[str] = (),
) -> tuple[tuple[str, str], ...]:
    """Build priority-day then newest-first monthly windows.

    Priority dates are an optional cold-start accelerator for known repository
    bulk-update days.  The ordinary monthly windows still follow, so they
    cannot reduce coverage or turn a scoped miss into a negative decision.
    """

    upper = datetime.strptime(_validate_oai_date(until_date), "%Y-%m-%d").date()
    lower_text = from_date or KCI_OAI_EARLIEST_DATE
    lower = datetime.strptime(_validate_oai_date(lower_text), "%Y-%m-%d").date()
    if lower > upper:
        raise OaCollectionError("invalid_oai_date_range")
    windows: list[tuple[str, str]] = []
    seen_priority_dates: set[str] = set()
    for value in priority_dates:
        normalized = _validate_oai_date(value)
        if normalized in seen_priority_dates:
            continue
        priority = datetime.strptime(normalized, "%Y-%m-%d").date()
        if not lower <= priority <= upper:
            raise OaCollectionError("priority_date_outside_oai_range")
        seen_priority_dates.add(normalized)
        windows.append((normalized, normalized))
    year, month = upper.year, upper.month
    while (year, month) >= (lower.year, lower.month):
        month_start = datetime(year, month, 1).date()
        month_end = datetime(
            year,
            month,
            calendar.monthrange(year, month)[1],
        ).date()
        start = max(lower, month_start)
        end = min(upper, month_end)
        if start <= end:
            window = (start.isoformat(), end.isoformat())
            if window not in windows:
                windows.append(window)
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
    return tuple(windows)


def window_from_text(from_date: str, until_date: str) -> str:
    return f"{from_date}..{until_date}"


OA_COLUMNS = (
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
    "publication_year",
    "evidence_strategy",
    "oai_identifier",
    "dc_rights",
    "dc_rights_field_present",
    "observed_dc_rights",
    "returned_source_id",
    "kci_orte_open_yn",
    "kci_orte_open_yn_field_present",
    "observed_kci_orte_open_yn",
    "orte_open_yn",
    "observed_orte_open_yn",
    "is_open_access",
    "oa_classification",
    "collection_status",
    "field_present",
    "collected_at",
    "attempts",
    "cache_hit",
    "error_code",
)
SUMMARY_COLUMNS = ("metric", "value", "description")
VALIDATION_COLUMNS = ("항목", "값", "기대값/정의", "결과")

_COLUMN_WIDTHS = {
    "paper_index": 12,
    "database_id": 13,
    "source_id": 18,
    "title": 55,
    "title_kor": 55,
    "title_eng": 55,
    "doi_raw": 34,
    "doi": 31,
    "journal_id": 28,
    "institution_id": 30,
    "publication_year": 16,
    "evidence_strategy": 44,
    "oai_identifier": 32,
    "dc_rights": 14,
    "dc_rights_field_present": 25,
    "observed_dc_rights": 24,
    "returned_source_id": 22,
    "kci_orte_open_yn": 22,
    "kci_orte_open_yn_field_present": 34,
    "observed_kci_orte_open_yn": 30,
    "orte_open_yn": 18,
    "observed_orte_open_yn": 25,
    "is_open_access": 19,
    "oa_classification": 22,
    "collection_status": 22,
    "field_present": 16,
    "collected_at": 27,
    "attempts": 12,
    "cache_hit": 13,
    "error_code": 28,
    "metric": 32,
    "value": 24,
    "description": 58,
    "항목": 34,
    "값": 48,
    "기대값/정의": 58,
    "결과": 14,
}


def export_oa_workbook(
    run: OaCollectionRun,
    output: Path = DEFAULT_OUTPUT,
    *,
    database: Path = DEFAULT_DATABASE,
    generated_at: datetime | None = None,
    canonical_when_unresolved: bool = False,
) -> OaExportResult:
    """Atomically export one OA row per DB paper plus summary and validation.

    Unresolved rows normally divert the workbook to ``*.partial.xlsx`` so the
    canonical file only ever holds a complete classification.  The incremental
    pipeline passes ``canonical_when_unresolved`` to publish the canonical file
    anyway: every unresolved paper stays visibly unresolved in its row and in
    the validation sheet, and downstream consumers render it as ``N/A``.
    """

    results = list(run.results)
    papers = [result.paper for result in results]
    _validate_results(papers, results)
    _validate_run_evidence(run)
    generated = generated_at or datetime.now(SEOUL)
    oa_rows = [_oa_row(result) for result in results]
    counts = _classification_counts(results)
    summary_rows = _summary_rows(results, counts)
    validation_rows = _validation_rows(
        results,
        run=run,
        database=database,
        generated_at=generated,
        counts=counts,
    )

    workbook = Workbook()
    oa_sheet = workbook.active
    oa_sheet.title = "OA 목록"
    summary_sheet = workbook.create_sheet("요약")
    validation_sheet = workbook.create_sheet("검증")
    _write_sheet(oa_sheet, OA_COLUMNS, oa_rows, table_name="KciOpenAccess")
    _write_sheet(summary_sheet, SUMMARY_COLUMNS, summary_rows, table_name="KciOaSummary")
    _write_sheet(
        validation_sheet,
        VALIDATION_COLUMNS,
        validation_rows,
        table_name="KciOaValidation",
    )
    workbook.active = 0
    workbook.properties.creator = "Forensics-Paper"
    workbook.properties.title = "KCI 논문 Open Access 목록"
    workbook.properties.description = (
        "KCI OAI-PMH oai_dc rights와 oai_kci orte-open-yn을 교차검증한 OA 결과"
        if run.strategy == OA_COLLECTION_STRATEGY
        else "KCI articleSearch orte-open-yn을 사용한 이전 수집 방식의 OA 결과"
    )

    requested_destination = Path(output).expanduser().resolve()
    database_path = Path(database).expanduser().resolve()
    if requested_destination == database_path:
        raise OaCollectionError("output_conflicts_with_database")
    if requested_destination.suffix.casefold() != ".xlsx":
        raise OaCollectionError("output_must_be_xlsx")
    destination = (
        _partial_output_path(requested_destination)
        if counts["unresolved"]
        and not canonical_when_unresolved
        and not requested_destination.stem.casefold().endswith(".partial")
        else requested_destination
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = destination.stat().st_mode & 0o777 if destination.exists() else None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.",
            suffix=".xlsx",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        workbook.save(temporary)
        workbook.close()
        _verify_workbook(
            temporary,
            expected_source_ids=[result.paper.source_id for result in results],
            expected_summary_rows=len(summary_rows),
            expected_validation_rows=len(validation_rows),
        )
        os.chmod(temporary, existing_mode if existing_mode is not None else 0o644)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        workbook.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise

    return OaExportResult(
        path=destination,
        paper_count=len(results),
        open_access_count=counts["Y"],
        not_open_access_count=counts["N"],
        unresolved_count=counts["unresolved"],
    )


def _oa_row(result: PaperOaResult) -> dict[str, Any]:
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
        "publication_year": result.paper.publication_year,
        "evidence_strategy": result.evidence_strategy,
        "oai_identifier": result.oai_identifier,
        "dc_rights": result.dc_rights,
        "dc_rights_field_present": result.dc_rights_field_present,
        "observed_dc_rights": result.observed_dc_rights,
        "returned_source_id": result.returned_source_id,
        "kci_orte_open_yn": result.kci_orte_open_yn,
        "kci_orte_open_yn_field_present": result.kci_orte_open_yn_field_present,
        "observed_kci_orte_open_yn": result.observed_kci_orte_open_yn,
        "orte_open_yn": result.orte_open_yn,
        "observed_orte_open_yn": (
            result.observed_value
            if result.evidence_strategy == ARTICLE_SEARCH_COLLECTION_STRATEGY
            else result.observed_kci_orte_open_yn
        ),
        "is_open_access": result.is_open_access,
        "oa_classification": result.oa_classification,
        "collection_status": result.status,
        "field_present": result.field_present,
        "collected_at": result.collected_at,
        "attempts": result.attempts,
        "cache_hit": result.cache_hit,
        "error_code": result.error_code,
    }


def _classification_counts(results: Sequence[PaperOaResult]) -> Counter[str]:
    counts: Counter[str] = Counter(result.status for result in results)
    counts["Y"] = sum(result.orte_open_yn == "Y" for result in results)
    counts["N"] = sum(result.orte_open_yn == "N" for result in results)
    counts["classified_total"] = counts["Y"] + counts["N"]
    counts["unresolved"] = len(results) - counts["classified_total"]
    return counts


def _summary_rows(
    results: Sequence[PaperOaResult],
    counts: Counter[str],
) -> list[dict[str, Any]]:
    classified = counts["classified_total"]
    oa_rate = counts["Y"] / classified if classified else None
    complete = counts["unresolved"] == 0
    values = (
        ("paper_count", len(results), "DB에서 읽은 전체 논문 수"),
        ("open_access_y", counts["Y"], "rights=orte-open-yn=Y (Open Access)"),
        ("not_open_access_n", counts["N"], "rights=orte-open-yn=N (Not Open Access)"),
        ("classified_total", classified, "Y 또는 N으로 판정된 논문 수"),
        ("field_missing", counts["field_missing"], "응답에 필드가 없거나 비어 있음"),
        ("invalid_value", counts["invalid_value"], "필드 값이 Y/N 이외임"),
        ("no_record", counts["no_record"], "정확한 ART ID 레코드가 없음"),
        ("failed", counts["failed"], "통신 또는 응답 검증 실패"),
        ("unresolved_total", counts["unresolved"], "OA 여부가 미확정인 논문 수"),
        ("classification_complete", complete, "모든 논문이 Y/N이면 True"),
        ("oa_rate_among_classified", oa_rate, "Y / (Y + N), 판정 건이 없으면 빈칸"),
    )
    return [
        {"metric": metric, "value": value, "description": description}
        for metric, value, description in values
    ]


def _validation_rows(
    results: Sequence[PaperOaResult],
    *,
    run: OaCollectionRun,
    database: Path,
    generated_at: datetime,
    counts: Counter[str],
) -> list[dict[str, Any]]:
    complete = counts["unresolved"] == 0
    is_oai = run.strategy == OA_COLLECTION_STRATEGY
    checks = {
        "상태 합계": sum(
            counts[status]
            for status in _RESULT_STATUSES
        ) == len(results),
        "source_id 고유성": len({result.paper.source_id for result in results}) == len(results),
        "반환 ID 일치": all(
            result.returned_source_id in {None, result.paper.source_id}
            for result in results
        ),
        "Y 판정 일관성": all(
            result.orte_open_yn != "Y" or result.is_open_access is True
            for result in results
        ),
        "N 판정 일관성": all(
            result.orte_open_yn != "N" or result.is_open_access is False
            for result in results
        ),
        "근거 전략 일치": all(
            result.evidence_strategy == run.strategy for result in results
        ),
        (
            "OAI 이중 근거 일치" if is_oai else "articleSearch 근거 분리"
        ): (
            all(
                result.status != "classified"
                or (
                    result.returned_source_id == result.paper.source_id
                    and result.oai_identifier is not None
                    and result.dc_rights == result.orte_open_yn
                    and result.kci_orte_open_yn == result.orte_open_yn
                    and result.dc_rights_field_present is True
                    and result.kci_orte_open_yn_field_present is True
                )
                for result in results
            )
            if is_oai
            else all(result.oai_identifier is None for result in results)
        ),
        "미확정 False 금지": all(
            result.status == "classified" or result.is_open_access is None
            for result in results
        ),
        "API 원문·키 미저장": True,
    }
    if not all(checks.values()):
        raise OaCollectionError("report_validation_failed")
    report_schema = (
        REPORT_SCHEMA_VERSION if is_oai else ARTICLE_SEARCH_REPORT_SCHEMA_VERSION
    )
    primary_metadata = KCI_OAI_PRIMARY_PREFIX if is_oai else KCI_ARTICLE_SEARCH_API_CODE
    verification_metadata = KCI_OAI_VERIFY_PREFIX if is_oai else "not_applicable"
    classification_definition = (
        "oai_dc rights와 oai_kci orte-open-yn 일치 시에만 판정"
        if is_oai
        else "articleSearch orte-open-yn의 Y/N 값으로 판정"
    )
    values: list[tuple[str, Any, str, str]] = [
        ("report_schema_version", report_schema, "현재 보고서 계약", "PASS"),
        ("generated_at", _iso_timestamp(generated_at), "Asia/Seoul ISO 8601", "PASS"),
        ("database_file", Path(database).name, "읽기 전용 입력 DB 파일명", "PASS"),
        (
            "collection_strategy",
            run.strategy,
            "OAI-PMH 이중 검증" if is_oai else "articleSearch 단일 근거(호환 모드)",
            "PASS",
        ),
        (
            "primary_metadata",
            primary_metadata,
            "ListRecords dc:rights" if is_oai else "articleSearch orte-open-yn",
            "PASS",
        ),
        (
            "verification_metadata",
            verification_metadata,
            "GetRecord orte-open-yn" if is_oai else "교차검증 없음",
            "PASS",
        ),
        (
            "classification_rule",
            "Y=Open Access; N=Not Open Access; missing/other=unresolved",
            classification_definition,
            "PASS",
        ),
        ("report_kind", "complete" if complete else "partial", "complete 또는 partial", "PASS" if complete else "FAIL"),
        ("classification_complete", complete, "canonical 판정 완료 조건은 True", "PASS" if complete else "FAIL"),
        ("database_snapshot_sha256", _paper_snapshot_sha256([r.paper for r in results]), "대상 537편 스냅샷 지문", "PASS"),
        ("paper_count", len(results), "DB 스냅샷 전체 행 수", "PASS"),
        ("open_access_y", counts["Y"], "orte-open-yn=Y", "PASS"),
        ("not_open_access_n", counts["N"], "orte-open-yn=N", "PASS"),
        ("field_missing", counts["field_missing"], "필드 누락/공란", "PASS"),
        ("invalid_value", counts["invalid_value"], "Y/N 외 값", "PASS"),
        ("no_record", counts["no_record"], "API 레코드 없음", "PASS"),
        ("failed", counts["failed"], "수집 실패", "PASS"),
        ("unresolved_total", counts["unresolved"], "완료 보고서는 0이어야 함", "PASS" if complete else "FAIL"),
        ("cache_hits", run.cache_hits, "재개 캐시 사용 논문 수", "PASS"),
        ("network_requests_including_retries", run.network_requests, "이번 실행 HTTP 요청 수", "PASS"),
    ]
    values.extend(
        (name, passed, "True", "PASS" if passed else "FAIL")
        for name, passed in checks.items()
    )
    return [
        {"항목": name, "값": value, "기대값/정의": definition, "결과": outcome}
        for name, value, definition, outcome in values
    ]


def _write_sheet(
    worksheet: Any,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    table_name: str,
) -> None:
    _append_safe_row(worksheet, columns)
    for row in rows:
        _append_safe_row(worksheet, [row.get(column) for column in columns])
    worksheet.freeze_panes = "A2"
    worksheet.sheet_view.showGridLines = False
    worksheet.sheet_view.zoomScale = 90
    worksheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(1, worksheet.max_row)}"

    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    border_side = Side(style="thin", color="D9E2F3")
    border = Border(
        left=border_side,
        right=border_side,
        top=border_side,
        bottom=border_side,
    )
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
        cell.border = border
    worksheet.row_dimensions[1].height = 28

    status_index = columns.index("collection_status") if "collection_status" in columns else None
    for row in worksheet.iter_rows(min_row=2):
        status = row[status_index].value if status_index is not None else None
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = border
            if status == "failed":
                cell.fill = PatternFill(fill_type="solid", fgColor="FCE4D6")
            elif status in {"field_missing", "invalid_value", "no_record"}:
                cell.fill = PatternFill(fill_type="solid", fgColor="FFF2CC")
    for index, name in enumerate(columns, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = _COLUMN_WIDTHS.get(name, 18)
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


def _append_safe_row(worksheet: Any, values: Sequence[Any]) -> None:
    row_index = worksheet.max_row + 1
    if worksheet.max_row == 1 and worksheet.cell(1, 1).value is None:
        row_index = 1
    for column_index, raw in enumerate(values, start=1):
        cell = worksheet.cell(row=row_index, column=column_index)
        if raw is None:
            cell.value = None
        elif isinstance(raw, bool):
            cell.value = raw
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            cell.value = raw
        else:
            cell.value = _excel_text(str(raw))
            cell.data_type = "s"


def _verify_workbook(
    path: Path,
    *,
    expected_source_ids: Sequence[str],
    expected_summary_rows: int,
    expected_validation_rows: int,
) -> None:
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        if workbook.sheetnames != ["OA 목록", "요약", "검증"]:
            raise OaCollectionError("workbook_sheet_validation_failed")
        oa_sheet = workbook["OA 목록"]
        if oa_sheet.max_row != len(expected_source_ids) + 1:
            raise OaCollectionError("workbook_row_validation_failed")
        headers = [cell.value for cell in next(oa_sheet.iter_rows(min_row=1, max_row=1))]
        if tuple(headers) != OA_COLUMNS:
            raise OaCollectionError("workbook_header_validation_failed")
        source_id_column = headers.index("source_id") + 1
        actual_source_ids = [
            row[0].value
            for row in oa_sheet.iter_rows(
                min_row=2,
                min_col=source_id_column,
                max_col=source_id_column,
            )
        ]
        if actual_source_ids != list(expected_source_ids):
            raise OaCollectionError("workbook_identity_validation_failed")
        if workbook["요약"].max_row != expected_summary_rows + 1:
            raise OaCollectionError("workbook_summary_validation_failed")
        if workbook["검증"].max_row != expected_validation_rows + 1:
            raise OaCollectionError("workbook_validation_sheet_failed")
        if any(
            cell.data_type == "f"
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
        ):
            raise OaCollectionError("workbook_formula_validation_failed")
    finally:
        workbook.close()


def _validate_results(
    papers: Sequence[KciPaper],
    results: Sequence[PaperOaResult],
) -> None:
    if len(papers) != len(results):
        raise OaCollectionError("result_count_mismatch")
    expected_ids = [paper.source_id for paper in papers]
    actual_ids = [result.paper.source_id for result in results]
    if expected_ids != actual_ids or len(set(actual_ids)) != len(actual_ids):
        raise OaCollectionError("result_identity_mismatch")
    for result in results:
        if result.returned_source_id not in {None, result.paper.source_id}:
            raise OaCollectionError("returned_identity_mismatch")
        if result.status == "classified" and result.orte_open_yn not in {"Y", "N"}:
            raise OaCollectionError("classified_value_mismatch")
        if result.status != "classified" and result.is_open_access is not None:
            raise OaCollectionError("unresolved_boolean_mismatch")


def _validate_normalized_evidence(
    *,
    value: str | None,
    field_present: bool | None,
    observed_value: str | None,
    field_name: str,
) -> None:
    """Validate one parsed source field without conflating blank and unknown."""

    if field_present is not None and not isinstance(field_present, bool):
        raise ValueError(f"{field_name} presence must be boolean or None")
    if value not in {None, "Y", "N"}:
        raise ValueError(f"invalid normalized {field_name} value")
    if observed_value is not None and (
        not isinstance(observed_value, str) or not observed_value.strip()
    ):
        raise ValueError(f"invalid observed {field_name} value")
    if value is not None and (
        field_present is not True or observed_value is not None
    ):
        raise ValueError(f"normalized {field_name} evidence is inconsistent")
    if observed_value is not None and (
        field_present is not True or value is not None
    ):
        raise ValueError(f"observed {field_name} evidence is inconsistent")
    if field_present is not True and (
        value is not None or observed_value is not None
    ):
        raise ValueError(f"unknown/missing {field_name} cannot contain evidence")


def _validate_article_search_results(results: Sequence[PaperOaResult]) -> None:
    for result in results:
        if result.evidence_strategy != ARTICLE_SEARCH_COLLECTION_STRATEGY:
            raise OaCollectionError("article_search_strategy_mismatch")
        if any(
            value is not None
            for value in (
                result.oai_identifier,
                result.dc_rights,
                result.dc_rights_field_present,
                result.observed_dc_rights,
                result.kci_orte_open_yn,
                result.kci_orte_open_yn_field_present,
                result.observed_kci_orte_open_yn,
            )
        ):
            raise OaCollectionError("article_search_oai_evidence_present")


def _validate_oai_results(results: Sequence[PaperOaResult]) -> None:
    for result in results:
        if result.evidence_strategy != OA_COLLECTION_STRATEGY:
            raise OaCollectionError("oai_strategy_mismatch")

        has_primary_identity = result.oai_identifier is not None
        has_primary_evidence = any(
            value is not None
            for value in (
                result.dc_rights,
                result.dc_rights_field_present,
                result.observed_dc_rights,
            )
        )
        has_verification_evidence = any(
            value is not None
            for value in (
                result.kci_orte_open_yn,
                result.kci_orte_open_yn_field_present,
                result.observed_kci_orte_open_yn,
            )
        )
        if (has_primary_evidence or has_verification_evidence) and not has_primary_identity:
            raise OaCollectionError("oai_evidence_identity_missing")

        if result.status == "classified":
            if (
                result.oai_identifier is None
                or result.returned_source_id != result.paper.source_id
                or result.dc_rights not in {"Y", "N"}
                or result.kci_orte_open_yn != result.dc_rights
                or result.orte_open_yn != result.dc_rights
                or result.dc_rights_field_present is not True
                or result.kci_orte_open_yn_field_present is not True
                or result.observed_dc_rights is not None
                or result.observed_kci_orte_open_yn is not None
            ):
                raise OaCollectionError("oai_crosscheck_evidence_missing")
            continue

        if result.status == "no_record":
            if has_primary_identity or has_primary_evidence or has_verification_evidence:
                raise OaCollectionError("oai_no_record_claims_evidence")
            continue

        if result.status == "field_missing":
            if result.returned_source_id != result.paper.source_id:
                raise OaCollectionError("oai_unresolved_identity_missing")
            if result.error_code == "dc_rights_missing":
                valid = (
                    result.oai_identifier is not None
                    and result.dc_rights is None
                    and result.dc_rights_field_present in {False, True}
                    and result.observed_dc_rights is None
                    and result.field_present == result.dc_rights_field_present
                )
            elif result.error_code == "orte_open_yn_missing":
                valid = (
                    result.oai_identifier is not None
                    and result.dc_rights in {"Y", "N"}
                    and result.dc_rights_field_present is True
                    and result.observed_dc_rights is None
                    and result.kci_orte_open_yn is None
                    and result.kci_orte_open_yn_field_present in {False, True}
                    and result.observed_kci_orte_open_yn is None
                    and result.field_present
                    == result.kci_orte_open_yn_field_present
                )
            else:
                valid = False
            if not valid:
                raise OaCollectionError("oai_missing_evidence_mismatch")
            continue

        if result.status == "invalid_value":
            if result.returned_source_id != result.paper.source_id:
                raise OaCollectionError("oai_unresolved_identity_missing")
            if result.error_code == "dc_rights_invalid":
                valid = (
                    result.oai_identifier is not None
                    and result.dc_rights is None
                    and result.dc_rights_field_present is True
                    and result.observed_dc_rights is not None
                    and result.observed_value == result.observed_dc_rights
                )
            elif result.error_code == "orte_open_yn_invalid":
                valid = (
                    result.oai_identifier is not None
                    and result.dc_rights in {"Y", "N"}
                    and result.dc_rights_field_present is True
                    and result.observed_dc_rights is None
                    and result.kci_orte_open_yn is None
                    and result.kci_orte_open_yn_field_present is True
                    and result.observed_kci_orte_open_yn is not None
                    and result.observed_value
                    == result.observed_kci_orte_open_yn
                )
            else:
                valid = False
            if not valid:
                raise OaCollectionError("oai_invalid_evidence_mismatch")
            continue

        if result.error_code == "oai_oa_value_mismatch":
            if not (
                result.returned_source_id == result.paper.source_id
                and result.oai_identifier is not None
                and result.dc_rights in {"Y", "N"}
                and result.kci_orte_open_yn in {"Y", "N"}
                and result.dc_rights != result.kci_orte_open_yn
                and result.dc_rights_field_present is True
                and result.kci_orte_open_yn_field_present is True
                and result.observed_dc_rights is None
                and result.observed_kci_orte_open_yn is None
            ):
                raise OaCollectionError("oai_conflict_evidence_mismatch")
        elif result.error_code == "oai_kci_record_not_found":
            if not (
                result.returned_source_id is None
                and result.oai_identifier is not None
                and result.kci_orte_open_yn is None
                and result.kci_orte_open_yn_field_present is None
                and result.observed_kci_orte_open_yn is None
            ):
                raise OaCollectionError("oai_getrecord_absence_evidence_mismatch")


def _validate_run_evidence(run: OaCollectionRun) -> None:
    if any(result.evidence_strategy != run.strategy for result in run.results):
        raise OaCollectionError("run_evidence_strategy_mismatch")
    if run.strategy == OA_COLLECTION_STRATEGY:
        _validate_oai_results(run.results)
    elif run.strategy == ARTICLE_SEARCH_COLLECTION_STRATEGY:
        _validate_article_search_results(run.results)
    else:
        raise OaCollectionError("invalid_collection_strategy")


def _safe_parse_xml(
    body: bytes,
    *,
    max_elements: int,
    max_depth: int,
) -> ElementTree.Element:
    if not body:
        raise InvalidOaResponse("empty_xml")
    if b"\x00" in body or _UNSAFE_XML_RE.search(body):
        raise InvalidOaResponse("unsafe_xml")
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, RecursionError):
        raise InvalidOaResponse("malformed_xml") from None
    count = 0
    stack: list[tuple[ElementTree.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > max_elements:
            raise InvalidOaResponse("xml_element_limit_exceeded")
        if depth > max_depth:
            raise InvalidOaResponse("xml_depth_limit_exceeded")
        stack.extend((child, depth + 1) for child in list(element))
    return root


def _validated_records(root: ElementTree.Element) -> list[ElementTree.Element]:
    """Validate the documented KCI response envelope before reading a record."""

    if _local_name(root.tag) != "MetaData":
        raise InvalidOaResponse("invalid_envelope")
    outputs = _direct_children(root, "outputData")
    if len(outputs) != 1:
        raise InvalidOaResponse("invalid_envelope")
    output = outputs[0]
    results = _direct_children(output, "result")
    if len(results) != 1:
        raise InvalidOaResponse("invalid_envelope")
    result = results[0]

    codes = [
        value
        for node in _direct_children(result, "resultCode")
        if (value := _element_text(node)) is not None
    ]
    if len(codes) > 1 or (codes and re.fullmatch(r"[0-9]+", codes[0]) is None):
        raise InvalidOaResponse("invalid_result_code")
    if codes and re.fullmatch(r"0+", codes[0]) is None:
        raise FatalOaError("api_reported_error")

    message_state = _message_state(result)
    if message_state == "authentication_error":
        raise FatalOaError("api_authentication_error")
    if message_state == "error":
        raise FatalOaError("api_reported_error")

    totals = [
        value
        for node in _direct_children(result, "total")
        if (value := _element_text(node)) is not None
    ]
    records = _direct_children(output, "record")
    if message_state == "no_data":
        if records:
            raise InvalidOaResponse("invalid_no_data_response")
        if len(totals) > 1 or (
            totals
            and (
                re.fullmatch(r"[0-9]+", totals[0]) is None
                or int(totals[0]) != 0
            )
        ):
            raise InvalidOaResponse("invalid_no_data_response")
        return []
    if len(totals) != 1 or re.fullmatch(r"[0-9]+", totals[0]) is None:
        raise InvalidOaResponse("invalid_total")
    if int(totals[0]) != len(records):
        raise InvalidOaResponse("record_count_mismatch")
    return records


def _validated_oai_envelope(
    root: ElementTree.Element,
) -> tuple[tuple[str, str], ...]:
    if root.tag != f"{{{OAI_PMH_NAMESPACE}}}OAI-PMH":
        raise InvalidOaResponse("invalid_oai_envelope")
    if len(_direct_children_ns(root, OAI_PMH_NAMESPACE, "responseDate")) != 1:
        raise InvalidOaResponse("invalid_oai_response_date")
    if len(_direct_children_ns(root, OAI_PMH_NAMESPACE, "request")) != 1:
        raise InvalidOaResponse("invalid_oai_request_echo")
    errors: list[tuple[str, str]] = []
    for node in _direct_children_ns(root, OAI_PMH_NAMESPACE, "error"):
        code = _clean_text(node.get("code"))
        if code is None or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", code):
            raise InvalidOaResponse("invalid_oai_error")
        errors.append((code, _element_text(node) or ""))
    if errors and len(list(root)) > len(errors) + 2:
        raise InvalidOaResponse("mixed_oai_error_response")
    return tuple(errors)


def _oai_error_code(errors: Sequence[tuple[str, str]]) -> str:
    # Server text is intentionally discarded; only documented stable codes
    # can cross logging, cache, and workbook boundaries.
    if len(errors) != 1:
        return "oai_multiple_errors"
    code = re.sub(r"[^A-Za-z0-9]", "", errors[0][0])[:64]
    return f"oai_{code or 'reported_error'}"


def _message_state(result: ElementTree.Element) -> str | None:
    messages = [
        value.casefold()
        for name in ("resultMsg", "message")
        for node in _direct_children(result, name)
        if (value := _element_text(node)) is not None
    ]
    if not messages:
        return None
    success_messages = {value.casefold() for value in _SUCCESS_MESSAGES}
    no_data_messages = {value.casefold() for value in _NO_DATA_MESSAGES}
    if set(messages) <= success_messages:
        return "success"
    authentication_markers = (
        "등록되지 않은 key",
        "필수 요청 파라미터가 없음 => key",
        "사용기간",
        "invalid key",
        "authentication",
        "인증",
    )
    if any(
        marker.casefold() in message
        for message in messages
        for marker in authentication_markers
    ):
        return "authentication_error"
    if set(messages) <= success_messages | no_data_messages and set(messages) & no_data_messages:
        return "no_data"
    return "error"


def _direct_children(
    element: ElementTree.Element,
    local_name: str,
) -> list[ElementTree.Element]:
    return [child for child in list(element) if _local_name(child.tag) == local_name]


def _direct_children_ns(
    element: ElementTree.Element,
    namespace: str,
    local_name: str,
) -> list[ElementTree.Element]:
    expected = f"{{{namespace}}}{local_name}"
    return [child for child in list(element) if child.tag == expected]


def _element_text(element: ElementTree.Element) -> str | None:
    return _clean_text("".join(element.itertext()))


def _load_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return values
    if len(raw) > 1024 * 1024:
        raise OaCollectionError("env_file_too_large")
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp949", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise OaCollectionError("env_file_decode_failed")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        name, raw_value = stripped.split("=", 1)
        name = name.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            values[name] = _parse_env_value(raw_value)
    return values


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote_character = value[0]
        if len(value) < 2 or value[-1] != quote_character:
            raise OaCollectionError("invalid_env_quoting")
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _resolve_api_key(env_file: Path) -> str:
    configured = os.environ.get("KCI_OPEN_API_KEY")
    if configured is None:
        configured = _load_env_values(env_file).get("KCI_OPEN_API_KEY")
    if configured is None:
        raise FatalOaError("api_key_not_configured")
    return _validate_api_key(configured)


def _resolve_rps(cli_value: float | None, env_file: Path) -> float:
    if cli_value is not None:
        value = cli_value
    else:
        env_values = _load_env_values(env_file)
        raw = os.environ.get("KCI_OA_RPS")
        if raw is None:
            raw = env_values.get("KCI_OA_RPS")
        if raw is None:
            raw = os.environ.get("KCI_RATE_LIMIT_RPS")
        if raw is None:
            raw = env_values.get("KCI_RATE_LIMIT_RPS")
        try:
            value = DEFAULT_RPS if raw in {None, ""} else float(raw)
        except ValueError:
            raise OaCollectionError("invalid_kci_oa_rps") from None
    if not math.isfinite(value) or value <= 0 or value > 4:
        raise OaCollectionError("invalid_kci_oa_rps")
    return value


def _validate_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise FatalOaError("invalid_api_key")
    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > 4096
        or any(character.isspace() or ord(character) < 32 for character in stripped)
    ):
        raise FatalOaError("invalid_api_key")
    return stripped


def _validate_source_id(value: str) -> None:
    if not isinstance(value, str) or _ART_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid KCI article ID")


def _validate_oai_identifier(value: str) -> None:
    if not isinstance(value, str) or _OAI_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError("invalid KCI OAI identifier")


def _validate_resumption_token(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 8192
        or _has_control(value)
    ):
        raise ValueError("invalid OAI resumption token")


def _validate_oai_date(value: str) -> str:
    if not isinstance(value, str) or _OAI_DATE_RE.fullmatch(value) is None:
        raise OaCollectionError("invalid_oai_date")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise OaCollectionError("invalid_oai_date") from None
    return parsed.isoformat()


def _validate_oai_request_params(params: Mapping[str, str]) -> None:
    if not isinstance(params, Mapping) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or _has_control(key)
        or _has_control(value)
        for key, value in params.items()
    ):
        raise HttpClientError("invalid OAI-PMH parameters")
    verb = params.get("verb")
    if verb == "ListRecords" and set(params) == {"verb", "resumptionToken"}:
        try:
            _validate_resumption_token(params["resumptionToken"])
        except ValueError:
            raise HttpClientError("invalid OAI-PMH parameters") from None
        return
    if verb == "ListRecords":
        required = {"verb", "set", "metadataPrefix"}
        allowed = required | {"from", "until"}
        if (
            not required <= set(params) <= allowed
            or params.get("set") != KCI_OAI_SET
            or params.get("metadataPrefix") != KCI_OAI_PRIMARY_PREFIX
        ):
            raise HttpClientError("invalid OAI-PMH ListRecords parameters")
        try:
            if "from" in params:
                _validate_oai_date(params["from"])
            if "until" in params:
                _validate_oai_date(params["until"])
        except OaCollectionError:
            raise HttpClientError("invalid OAI-PMH date parameters") from None
        return
    if verb == "GetRecord" and set(params) == {
        "verb",
        "identifier",
        "metadataPrefix",
    }:
        if params.get("metadataPrefix") != KCI_OAI_VERIFY_PREFIX:
            raise HttpClientError("invalid OAI-PMH GetRecord format")
        try:
            _validate_oai_identifier(params["identifier"])
        except ValueError:
            raise HttpClientError("invalid OAI-PMH identifier") from None
        return
    raise HttpClientError("unexpected OAI-PMH parameters")


def _normalize_doi(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_doi(value)
    except (InvalidDoiError, TypeError, ValueError):
        return None


def _optional_year(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise OaCollectionError("invalid_publication_year")
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise OaCollectionError("invalid_publication_year") from None
    if not 1000 <= year <= 9999:
        raise OaCollectionError("invalid_publication_year")
    return year


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        seconds = (parsed - datetime.now(parsed.tzinfo)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split()).strip()
    return cleaned or None


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=SEOUL)
    return value.astimezone(SEOUL).isoformat(timespec="seconds")


def _excel_text(value: str) -> str:
    cleaned = _INVALID_EXCEL_XML_RE.sub("", value)
    return cleaned[:32767]


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _paper_snapshot_sha256(papers: Sequence[KciPaper]) -> str:
    payload = json.dumps(
        [asdict(paper) for paper in papers],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_payload_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_id_snapshot_sha256(source_ids: set[str]) -> str:
    for source_id in source_ids:
        _validate_source_id(source_id)
    payload = json.dumps(sorted(source_ids), separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _partial_output_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.partial{path.suffix}")


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _is_exact_endpoint(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "open.kci.go.kr"
        and parsed.port is None
        and parsed.path == "/po/openapi/openApiSearch.kci"
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def _is_exact_oai_endpoint(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "open.kci.go.kr"
        and parsed.port is None
        and parsed.path == "/oai/request"
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "kci_papers의 전체 논문을 KCI OAI-PMH로 수집하고 "
            "oai_dc rights와 oai_kci orte-open-yn 교차검증 OA Excel을 생성합니다."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--from-date",
        default=None,
        help=(
            "OAI datestamp 하한(YYYY-MM-DD). 기본값은 2013-03-21이며 "
            "월 단위 창으로 최신 월부터 역순 수집"
        ),
    )
    parser.add_argument(
        "--until-date",
        default=None,
        help="OAI datestamp 상한(YYYY-MM-DD). 기본값은 실행 시작일로 고정",
    )
    parser.add_argument(
        "--priority-date",
        action="append",
        default=[],
        help=(
            "먼저 조회할 알려진 OAI 일괄 갱신일(YYYY-MM-DD, 반복 가능). "
            "이후 전체 월별 범위도 계속 조회하므로 완전성은 유지됨"
        ),
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=None,
        help="초당 요청 수(KCI_OA_RPS/KCI_RATE_LIMIT_RPS 또는 2, 최대 4)",
    )
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument(
        "--force",
        action="store_true",
        help="재개 캐시를 무시하고 모든 논문을 다시 조회",
    )
    parser.add_argument(
        "--canonical-when-unresolved",
        action="store_true",
        help=(
            "미확정 논문이 있어도 --output 경로에 그대로 기록(.partial 우회 없음). "
            "미확정 행은 그대로 미확정으로 남고 종료 코드 3은 유지"
        ),
    )
    parser.add_argument(
        "--open-access-ttl-days",
        type=float,
        default=DEFAULT_OPEN_ACCESS_CACHE_TTL_SECONDS / 86_400,
        help="Y(Open Access) 판정 캐시 유효기간(일). 만료된 논문만 다시 수확",
    )
    parser.add_argument(
        "--not-open-access-ttl-days",
        type=float,
        default=DEFAULT_NOT_OPEN_ACCESS_CACHE_TTL_SECONDS / 86_400,
        help="N(Not Open Access) 판정 캐시 유효기간(일)",
    )
    parser.add_argument(
        "--unresolved-ttl-hours",
        type=float,
        default=DEFAULT_UNRESOLVED_CACHE_TTL_SECONDS / 3_600,
        help="미확정 결과 캐시 유효기간(시간)",
    )
    return parser


def cache_ttl_seconds(args: argparse.Namespace) -> dict[str, int]:
    """Translate the day/hour CLI values into the cache's second-based TTLs."""

    values = {
        "open_access_ttl_seconds": round(args.open_access_ttl_days * 86_400),
        "not_open_access_ttl_seconds": round(args.not_open_access_ttl_days * 86_400),
        "unresolved_ttl_seconds": round(args.unresolved_ttl_hours * 3_600),
    }
    for name, value in values.items():
        if value < 1:
            raise ValueError(f"{name} must be positive")
    return values


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logger = ProgressLogger()
    try:
        papers = load_kci_papers(args.database)
        rps = _resolve_rps(args.rps, args.env_file)
        fixed_until = args.until_date or datetime.now(SEOUL).date().isoformat()
        fixed_until = _validate_oai_date(fixed_until)
        if args.from_date is not None:
            _validate_oai_date(args.from_date)
        for priority_date in args.priority_date:
            _validate_oai_date(priority_date)
        logger.emit(
            "KCI-OA",
            (
                f"OAI-PMH 수집 시작: 논문 {len(papers)}편, "
                f"datestamp={args.from_date or KCI_OAI_EARLIEST_DATE}..{fixed_until}, "
                f"priority={','.join(args.priority_date) or '-'}, "
                f"요청 속도 {rps:g} req/s"
            ),
            current=0,
            total=len(papers),
        )
        client = KciOaiPmhClient(
            requests_per_second=rps,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
        )
        cache = OaCache(
            args.cache_dir,
            strategy=OA_COLLECTION_STRATEGY,
            **cache_ttl_seconds(args),
        )
        run = collect_open_access_oai(
            papers,
            client,
            cache=cache,
            harvest_cache=OaiHarvestCache(args.cache_dir),
            force=args.force,
            retries=args.retries,
            logger=logger,
            log_every=args.log_every,
            from_date=args.from_date,
            until_date=fixed_until,
            priority_dates=args.priority_date,
        )
        exported = export_oa_workbook(
            run,
            args.output,
            database=args.database,
            canonical_when_unresolved=args.canonical_when_unresolved,
        )
    except KeyboardInterrupt:
        print("[KCI-OA] 사용자가 중단했습니다. 완료 캐시는 보존됩니다.", file=sys.stderr)
        return 130
    except FatalOaError as exc:
        print(f"[KCI-OA] 실행 중단: {exc.code}", file=sys.stderr)
        return 2
    except (OaCollectionError, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, OaCollectionError) else type(exc).__name__
        print(f"[KCI-OA] 실행 실패: {code}", file=sys.stderr)
        return 2

    print(
        "[KCI-OA] 완료: "
        f"papers={exported.paper_count}, Y={exported.open_access_count}, "
        f"N={exported.not_open_access_count}, unresolved={exported.unresolved_count}, "
        f"output={exported.path}",
        flush=True,
    )
    return 0 if exported.unresolved_count == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
