"""Collect KCI article references and create ``Analyzer/Ref/ref_list.xlsx``.

Each row in ``kci_papers`` is looked up by its exact ``ART#########`` ID via
KCI's ``articleDetail`` API.  The response echoes the API key inside its XML,
so raw bodies and request URLs are intentionally never logged or persisted.
Only parsed, credential-free reference fields enter the resume cache or Excel.

The ``<referenceInfo>`` element of every response (the part that never carries
the key) is kept verbatim, byte for byte, as
``Analyzer/Ref/api-response/referenceInfo/<ART id>.xml`` and indexed by
``Analyzer/Ref/api-response/manifest.json``; the paper profiles
(``Analyzer/MD/paper_md.py``) read their reference lists from those files.
KCI delivers a reference either as structured child elements (``<title>``,
``<author>`` ...) or as one ``<![CDATA[...]]>`` citation string; the latter is
kept as ``reference_citation_text``.

Run from any working directory with either command::

    python3 Analyzer/Ref/ref_collect.py
    python3 -m Analyzer.Ref.ref_collect
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo


REF_DIR = Path(__file__).resolve().parent
ANALYZER_DIR = REF_DIR.parent
REPOSITORY_ROOT = ANALYZER_DIR.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from PaperPipeline.http_client import (
    HttpClientError,
    ResponseTooLargeError,
    RetryableHttpError,
)
from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.kci_openapi_identifier import (
    KCI_OPENAPI_API_CODE,
    KCI_OPENAPI_DETAIL_URL,
    KciOpenApiHttpClient,
)
from Util.logger import ProgressLogger


DEFAULT_DATABASE = REPOSITORY_ROOT / "kci_pipeline.db"
DEFAULT_OUTPUT = REF_DIR / "ref_list.xlsx"
DEFAULT_CACHE_DIR = REF_DIR / ".cache"
DEFAULT_XML_DIR = REF_DIR / "api-response" / "referenceInfo"
DEFAULT_MANIFEST = REF_DIR / "api-response" / "manifest.json"
DEFAULT_ENV_FILE = REPOSITORY_ROOT / ".env"
DEFAULT_RPS = 0.5
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 45.0
DEFAULT_RETRIES = 2
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_POSITIVE_CACHE_TTL_SECONDS = 90 * 24 * 60 * 60
DEFAULT_ZERO_DATA_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
# 1.1 (2026-09-28): entries carry ``reference_citation_text`` and the SHA-256 of
# the verbatim ``<referenceInfo>`` file, which must exist for a cache hit.
CACHE_SCHEMA_VERSION = "1.1"
MANIFEST_SCHEMA_VERSION = "1.0"
REPORT_SCHEMA_VERSION = "1.0"
SEOUL = ZoneInfo("Asia/Seoul")

_ART_ID_RE = re.compile(r"ART[0-9]{9}\Z")
_UNSAFE_XML_RE = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_DECLARED_ENCODING_RE = re.compile(
    br"^\s*<\?xml[^>]*?encoding=[\"']([A-Za-z0-9._-]+)[\"']"
)
# Tokens that matter when slicing the verbatim ``<referenceInfo>`` element out
# of a response: CDATA sections and comments are skipped as opaque text so a
# citation string can never open or close the container.
_REFERENCE_INFO_TOKEN_RE = re.compile(
    r"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<referenceInfo(?:\s[^<>]*)?/>"
    r"|<referenceInfo(?:\s[^<>]*)?>|</referenceInfo\s*>",
    re.DOTALL,
)
_INVALID_EXCEL_XML_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]"
)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SUCCESS_MESSAGES = frozenset({"정상 처리되었습니다.", "ok", "success"})
# Live articleDetail returns exactly "No Data" for an absent ART record.
# Other API messages indicate a run-wide key/service/parameter contract error.
_NO_DATA_MESSAGES = frozenset({"no data"})
_CACHEABLE_STATUSES = frozenset({"collected", "no_reference_data"})
_RESULT_STATUSES = frozenset(
    {"collected", "no_reference_data", "no_record", "failed"}
)


class ReferenceCollectionError(RuntimeError):
    """Base exception carrying a stable, credential-free error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RetryableReferenceError(ReferenceCollectionError):
    """A request that may succeed after a short backoff."""

    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.retry_after = retry_after


class FatalReferenceError(ReferenceCollectionError):
    """A run-wide authentication or configuration failure."""


class InvalidReferenceResponse(ReferenceCollectionError):
    """One article response violated the expected API contract."""


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
        payload = json.dumps(
            {
                "database_id": self.database_id,
                "source_id": self.source_id,
                "title": self.title,
                "title_kor": self.title_kor,
                "title_eng": self.title_eng,
                "doi_raw": self.doi_raw,
                "doi": self.doi,
                "publication_year": self.publication_year,
                "journal_id": self.journal_id,
                "institution_id": self.institution_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReferenceRecord:
    reference_order: int
    reference_id: str | None = None
    reference_type_code: str | None = None
    reference_type: str | None = None
    referenced_kci_source_id: str | None = None
    reference_title: str | None = None
    reference_author: str | None = None
    reference_journal_name: str | None = None
    reference_conference_name: str | None = None
    reference_publisher: str | None = None
    reference_publication_year: str | None = None
    reference_volume: str | None = None
    reference_issue: str | None = None
    reference_serial_number: str | None = None
    reference_pages: str | None = None
    reference_doi_raw: str | None = None
    reference_doi: str | None = None
    reference_doi_status: str = "missing"
    reference_url: str | None = None
    reference_citation_date: str | None = None
    reference_site_name: str | None = None
    reference_registration_date: str | None = None
    reference_degree: str | None = None
    reference_university: str | None = None
    reference_department: str | None = None
    # A reference KCI delivers as one ``<![CDATA[...]]>`` citation string instead
    # of structured fields; the text is kept exactly as received.
    reference_citation_text: str | None = None
    duplicate_key_type: str | None = None
    is_duplicate_within_paper: bool = False
    duplicate_of_order: int | None = None
    additional_fields_json: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedArticleDetail:
    status: str
    reference_info_present: bool | None
    references: tuple[ReferenceRecord, ...]
    # The ``<referenceInfo>`` element exactly as it appears in the response
    # (line endings included); ``None`` when the record has no such element.
    reference_info_xml: str | None = None


@dataclass(frozen=True, slots=True)
class PaperReferenceResult:
    paper: KciPaper
    status: str
    reference_info_present: bool | None
    references: tuple[ReferenceRecord, ...]
    collected_at: str
    attempts: int
    error_code: str | None = None
    cache_hit: bool = False
    reference_info_xml: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise ValueError(f"unsupported collection status: {self.status}")
        if self.attempts < 0:
            raise ValueError("attempts must be non-negative")
        if (self.reference_info_present is True) != (self.reference_info_xml is not None):
            raise ValueError("referenceInfo XML must accompany exactly the results whose element is present")
        if self.reference_info_xml is not None and not self.reference_info_xml.strip():
            raise ValueError("referenceInfo XML cannot be blank")
        if self.status == "collected":
            if not self.references or self.reference_info_present is not True:
                raise ValueError("collected result requires reference data")
        elif self.references:
            raise ValueError("non-collected result cannot contain references")
        if self.status == "no_reference_data" and self.reference_info_present is None:
            raise ValueError("referenceInfo presence must be known")
        if self.status in {"no_record", "failed"} and self.reference_info_present is not None:
            raise ValueError("unresolved result cannot claim referenceInfo state")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed result requires an error code")

    @property
    def duplicate_count(self) -> int | None:
        if self.status in {"no_record", "failed"}:
            return None
        return sum(reference.is_duplicate_within_paper for reference in self.references)

    @property
    def unique_count(self) -> int | None:
        duplicate_count = self.duplicate_count
        return None if duplicate_count is None else len(self.references) - duplicate_count


@dataclass(frozen=True, slots=True)
class CollectionRun:
    results: tuple[PaperReferenceResult, ...]
    network_requests: int
    cache_hits: int


@dataclass(frozen=True, slots=True)
class ReferenceExportResult:
    path: Path
    paper_count: int
    reference_count: int
    failed_count: int


class ArticleDetailClient(Protocol):
    request_count: int

    def fetch(self, source_id: str) -> ParsedArticleDetail:
        """Fetch and parse one exact KCI ART ID."""


class _RedactedQueryParams(Mapping[str, str]):
    """Mapping accepted by the transport while redacting its key in repr."""

    __slots__ = ("_values",)

    def __init__(self, *, api_key: str, source_id: str) -> None:
        self._values = {
            "apiCode": KCI_OPENAPI_API_CODE,
            "key": api_key,
            "id": source_id,
        }

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return (
            "_RedactedQueryParams({'apiCode': 'articleDetail', "
            f"'key': '<redacted>', 'id': {self._values['id']!r}}})"
        )

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


class KciReferenceClient:
    """Fixed-origin, redirect-free KCI ``articleDetail`` client."""

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
        self._max_response_bytes = _positive_int(
            max_response_bytes, "max_response_bytes"
        )
        if http_client is None:
            http_client = KciOpenApiHttpClient(
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                user_agent="Forensics-Paper/1.0 (KCI reference collector)",
                request_gate=RequestRateLimiter(requests_per_second),
            )
        self._http = http_client
        self.request_count = 0

    def fetch(self, source_id: str) -> ParsedArticleDetail:
        _validate_source_id(source_id)
        self.request_count += 1
        try:
            fetched = self._http.get_bytes(
                KCI_OPENAPI_DETAIL_URL,
                params=_RedactedQueryParams(
                    api_key=self._api_key,
                    source_id=source_id,
                ),
                max_bytes=self._max_response_bytes,
                headers={"Accept": "application/xml, text/xml"},
            )
        except RetryableHttpError:
            raise RetryableReferenceError("network_error") from None
        except ResponseTooLargeError:
            raise InvalidReferenceResponse("response_too_large") from None
        except HttpClientError:
            raise FatalReferenceError("http_client_error") from None

        status_code = int(fetched.status_code)
        if status_code in _RETRYABLE_HTTP_STATUSES:
            raise RetryableReferenceError(
                f"http_{status_code}",
                retry_after=_parse_retry_after(fetched.headers.get("Retry-After")),
            )
        if status_code in {401, 403}:
            raise FatalReferenceError("authentication_failed")
        if status_code != 200:
            raise InvalidReferenceResponse(f"http_{status_code}")
        parsed = parse_article_detail_xml(
            fetched.body,
            expected_source_id=source_id,
        )
        # The key is echoed in ``<inputData>``, never inside ``<referenceInfo>``;
        # refuse to hand out a segment that would put it on disk all the same.
        if parsed.reference_info_xml is not None and self._api_key in parsed.reference_info_xml:
            raise InvalidReferenceResponse("secret_in_reference_info")
        return parsed


class ReferenceCache:
    """Per-paper cache containing only parsed, credential-free data."""

    def __init__(
        self,
        directory: Path,
        *,
        xml_directory: Path | None = None,
        positive_ttl_seconds: int = DEFAULT_POSITIVE_CACHE_TTL_SECONDS,
        zero_data_ttl_seconds: int = DEFAULT_ZERO_DATA_CACHE_TTL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.directory = Path(directory)
        # Verbatim ``<referenceInfo>`` files live beside the cache by default:
        # ``Analyzer/Ref/.cache`` -> ``Analyzer/Ref/api-response/referenceInfo``.
        self.xml_directory = (
            Path(xml_directory)
            if xml_directory is not None
            else self.directory.parent / "api-response" / "referenceInfo"
        )
        self.positive_ttl_seconds = _positive_int(
            positive_ttl_seconds, "positive_ttl_seconds"
        )
        self.zero_data_ttl_seconds = _positive_int(
            zero_data_ttl_seconds, "zero_data_ttl_seconds"
        )
        self._now = now or (lambda: datetime.now(SEOUL))

    def load(self, paper: KciPaper) -> PaperReferenceResult | None:
        try:
            payload = json.loads(self._path(paper.source_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if (
            payload.get("schema_version") != CACHE_SCHEMA_VERSION
            or payload.get("api_code") != KCI_OPENAPI_API_CODE
            or payload.get("source_id") != paper.source_id
            or payload.get("paper_fingerprint") != paper.fingerprint
            or payload.get("status") not in _CACHEABLE_STATUSES
            or not isinstance(payload.get("collected_at"), str)
            or not isinstance(payload.get("reference_info_present"), bool)
            or not isinstance(payload.get("references"), list)
        ):
            return None
        if not self._is_fresh(payload["status"], payload["collected_at"]):
            return None
        reference_info_xml: str | None = None
        if payload["reference_info_present"]:
            # The verbatim element is part of the cached result: a missing or
            # altered file means the paper is fetched again.
            digest = payload.get("reference_info_sha256")
            if not isinstance(digest, str):
                return None
            try:
                data = self.xml_path(paper.source_id).read_bytes()
                reference_info_xml = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return None
            if hashlib.sha256(data).hexdigest() != digest:
                return None
        elif payload.get("reference_info_sha256") is not None:
            return None
        try:
            references = tuple(
                _reference_from_mapping(value) for value in payload["references"]
            )
            result = PaperReferenceResult(
                paper=paper,
                status=payload["status"],
                reference_info_present=payload["reference_info_present"],
                references=references,
                collected_at=payload["collected_at"],
                attempts=0,
                cache_hit=True,
                reference_info_xml=reference_info_xml,
            )
        except (TypeError, ValueError):
            return None
        if payload.get("reference_count") != len(references):
            return None
        if tuple(reference.reference_order for reference in references) != tuple(
            range(1, len(references) + 1)
        ):
            return None
        return result

    def save(self, result: PaperReferenceResult) -> None:
        if result.status not in _CACHEABLE_STATUSES:
            return
        xml_path = self.xml_path(result.paper.source_id)
        digest: str | None = None
        if result.reference_info_xml is not None:
            data = result.reference_info_xml.encode("utf-8")
            digest = hashlib.sha256(data).hexdigest()
            _write_bytes_atomic(xml_path, data, default_mode=0o644)
        else:
            _remove_file(xml_path)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "api_code": KCI_OPENAPI_API_CODE,
            "source_id": result.paper.source_id,
            "paper_fingerprint": result.paper.fingerprint,
            "status": result.status,
            "reference_info_present": result.reference_info_present,
            "reference_count": len(result.references),
            "reference_info_sha256": digest,
            "collected_at": result.collected_at,
            "references": [asdict(reference) for reference in result.references],
        }
        _write_json_atomic(self._path(result.paper.source_id), payload)

    def invalidate(self, paper: KciPaper) -> None:
        """Remove one derived cache entry before an explicit forced refresh."""

        _remove_file(self.xml_path(paper.source_id))
        path = self._path(paper.source_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(path.parent)

    def xml_path(self, source_id: str) -> Path:
        _validate_source_id(source_id)
        return self.xml_directory / f"{source_id}.xml"

    def _is_fresh(self, status: str, collected_at: str) -> bool:
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
        ttl = (
            self.positive_ttl_seconds
            if status == "collected"
            else self.zero_data_ttl_seconds
        )
        return age <= ttl

    def _path(self, source_id: str) -> Path:
        _validate_source_id(source_id)
        return self.directory / f"{source_id}.json"


def load_kci_papers(database: Path = DEFAULT_DATABASE) -> list[KciPaper]:
    """Snapshot every ``kci_papers`` row through a read-only SQLite handle."""

    path = Path(database).expanduser().resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise ReferenceCollectionError("database_not_found_or_empty")
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error:
        raise ReferenceCollectionError("database_open_failed") from None
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(kci_papers)")
        }
        required = {"id", "source_id", "title", "doi", "publication_year"}
        if not required <= columns:
            raise ReferenceCollectionError("invalid_kci_papers_schema")
        optional = [
            name if name in columns else f"NULL AS {name}"
            for name in (
                "title_kor",
                "title_eng",
                "journal_id",
                "institution_id",
            )
        ]
        rows = connection.execute(
            "SELECT id, source_id, title, doi, publication_year, "
            + ", ".join(optional)
            + " FROM kci_papers ORDER BY id ASC"
        ).fetchall()
    except sqlite3.Error:
        raise ReferenceCollectionError("database_query_failed") from None
    finally:
        connection.close()
    if not rows:
        raise ReferenceCollectionError("kci_papers_empty")

    papers: list[KciPaper] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        source_id = _clean_text(row["source_id"])
        title = _clean_text(row["title"])
        if source_id is None or _ART_ID_RE.fullmatch(source_id) is None:
            raise ReferenceCollectionError("invalid_source_id_in_database")
        if source_id in seen:
            raise ReferenceCollectionError("duplicate_source_id_in_database")
        if title is None:
            raise ReferenceCollectionError("missing_title_in_database")
        database_id = row["id"]
        if not isinstance(database_id, int) or isinstance(database_id, bool):
            raise ReferenceCollectionError("invalid_database_id")
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


def parse_article_detail_xml(
    body: bytes,
    *,
    expected_source_id: str,
    max_elements: int = 100_000,
    max_depth: int = 96,
) -> ParsedArticleDetail:
    """Parse one response without retaining the key-bearing XML envelope."""

    _validate_source_id(expected_source_id)
    root = _safe_parse_xml(
        body,
        max_elements=max_elements,
        max_depth=max_depth,
    )
    records = _validated_records(root)
    if not records:
        return ParsedArticleDetail(
            status="no_record",
            reference_info_present=None,
            references=(),
        )
    if len(records) != 1:
        raise InvalidReferenceResponse("unexpected_record_count")
    record = records[0]
    article_nodes = _direct_children(record, "articleInfo")
    if len(article_nodes) != 1:
        raise InvalidReferenceResponse("invalid_article_info")
    if _clean_text(article_nodes[0].get("article-id")) != expected_source_id:
        raise InvalidReferenceResponse("article_identity_mismatch")

    reference_infos = _direct_children(record, "referenceInfo")
    if len(reference_infos) > 1:
        raise InvalidReferenceResponse("multiple_reference_containers")
    segment = _extract_reference_info_segment(_decode_xml(body))
    if (segment is None) != (not reference_infos):
        raise InvalidReferenceResponse("reference_info_segment")
    if not reference_infos:
        return ParsedArticleDetail(
            status="no_reference_data",
            reference_info_present=False,
            references=(),
        )
    nodes = _direct_children(reference_infos[0], "reference")
    references = tuple(
        _mark_duplicates(
            [_parse_reference(node, order) for order, node in enumerate(nodes, start=1)]
        )
    )
    # The sliced text must stand on its own and describe exactly the same
    # references as the envelope it was cut from.
    assert segment is not None
    if parse_reference_info_xml(segment, max_elements=max_elements, max_depth=max_depth) != references:
        raise InvalidReferenceResponse("reference_info_segment")
    if not nodes:
        return ParsedArticleDetail(
            status="no_reference_data",
            reference_info_present=True,
            references=(),
            reference_info_xml=segment,
        )
    return ParsedArticleDetail(
        status="collected",
        reference_info_present=True,
        references=references,
        reference_info_xml=segment,
    )


def parse_reference_info_xml(
    text: str | bytes,
    *,
    max_elements: int = 100_000,
    max_depth: int = 96,
) -> tuple[ReferenceRecord, ...]:
    """Parse one saved ``<referenceInfo>`` element into ordered references.

    This is the reader for ``Analyzer/Ref/api-response/referenceInfo/*.xml``;
    a structured ``<reference>`` yields its fields, a CDATA-only one yields
    ``reference_citation_text``.  Duplicates within the list are marked.
    """

    body = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    root = _safe_parse_xml(body, max_elements=max_elements, max_depth=max_depth)
    if _local_name(root.tag) != "referenceInfo":
        raise InvalidReferenceResponse("invalid_reference_info")
    nodes = _direct_children(root, "reference")
    return tuple(
        _mark_duplicates(
            [_parse_reference(node, order) for order, node in enumerate(nodes, start=1)]
        )
    )


def _decode_xml(body: bytes) -> str:
    match = _XML_DECLARED_ENCODING_RE.match(body)
    encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return body.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        raise InvalidReferenceResponse("undecodable_xml") from None


def _extract_reference_info_segment(text: str) -> str | None:
    """Return the ``<referenceInfo>`` element verbatim, or ``None`` if absent.

    The slice keeps KCI's own whitespace and line endings so the saved file
    equals the response fragment.  CDATA sections and comments are opaque, so
    citation text cannot forge a container tag; nesting or an unclosed
    container is rejected.
    """

    segments: list[str] = []
    start: int | None = None
    for match in _REFERENCE_INFO_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token.startswith("<![CDATA[") or token.startswith("<!--"):
            continue
        if token.startswith("</"):
            if start is None:
                raise InvalidReferenceResponse("reference_info_segment")
            segments.append(text[start : match.end()])
            start = None
        elif token.endswith("/>"):
            if start is not None:
                raise InvalidReferenceResponse("reference_info_segment")
            segments.append(token)
        else:
            if start is not None:
                raise InvalidReferenceResponse("reference_info_segment")
            start = match.start()
    if start is not None:
        raise InvalidReferenceResponse("reference_info_segment")
    if len(segments) > 1:
        raise InvalidReferenceResponse("multiple_reference_containers")
    return segments[0] if segments else None


def _safe_parse_xml(
    body: bytes,
    *,
    max_elements: int,
    max_depth: int,
) -> ElementTree.Element:
    if not body:
        raise InvalidReferenceResponse("empty_xml")
    if b"\x00" in body or _UNSAFE_XML_RE.search(body):
        raise InvalidReferenceResponse("unsafe_xml")
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, RecursionError):
        raise InvalidReferenceResponse("invalid_xml") from None
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > max_elements or depth > max_depth:
            raise InvalidReferenceResponse("complex_xml")
        stack.extend((child, depth + 1) for child in element)
    return root


def _validated_records(root: ElementTree.Element) -> list[ElementTree.Element]:
    if _local_name(root.tag) != "MetaData":
        raise InvalidReferenceResponse("invalid_envelope")
    outputs = _direct_children(root, "outputData")
    if len(outputs) != 1:
        raise InvalidReferenceResponse("invalid_envelope")
    output = outputs[0]
    results = _direct_children(output, "result")
    if len(results) != 1:
        raise InvalidReferenceResponse("invalid_envelope")
    result = results[0]
    codes = [
        value
        for node in _direct_children(result, "resultCode")
        if (value := _element_text(node)) is not None
    ]
    if len(codes) > 1 or (codes and re.fullmatch(r"0+", codes[0]) is None):
        raise InvalidReferenceResponse("api_reported_error")
    message_state = _message_state(result)
    if message_state == "authentication_error":
        raise FatalReferenceError("api_authentication_error")
    if message_state == "error":
        raise FatalReferenceError("api_reported_error")
    totals = [
        value
        for node in _direct_children(result, "total")
        if (value := _element_text(node)) is not None
    ]
    records = _direct_children(output, "record")
    if message_state == "no_data":
        if records:
            raise InvalidReferenceResponse("invalid_no_data_response")
        if len(totals) > 1 or (
            totals
            and (
                re.fullmatch(r"[0-9]+", totals[0]) is None
                or int(totals[0]) != 0
            )
        ):
            raise InvalidReferenceResponse("invalid_no_data_response")
        return []
    if len(totals) != 1 or re.fullmatch(r"[0-9]+", totals[0]) is None:
        raise InvalidReferenceResponse("invalid_total")
    if int(totals[0]) != len(records):
        raise InvalidReferenceResponse("record_count_mismatch")
    return records


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


def _parse_reference(node: ElementTree.Element, order: int) -> ReferenceRecord:
    values: dict[str, list[str]] = {}
    for child in list(node):
        value = _element_text(child)
        if value is not None:
            values.setdefault(_local_name(child.tag), []).append(value)

    known_children = {
        "title",
        "author",
        "journal-name",
        "conference-name",
        "pubilisher",
        "publisher",
        "publisher-name",
        "pubi-year",
        "pub-year",
        "volume",
        "isseue",
        "issue",
        "serno",
        "page",
        "doi",
        "url",
        "citation-day",
        "site-name",
        "registration-day",
        "degree",
        "university",
        "department",
    }
    known_attributes = {"refebibl-id", "arti-id", "type-code", "type-name"}
    extras: dict[str, Any] = {
        name: entries if len(entries) > 1 else entries[0]
        for name, entries in values.items()
        if name not in known_children
    }
    for name, entries in values.items():
        if name in known_children and len(entries) > 1:
            extras[f"{name}[]"] = entries
    alias_groups = {
        "publisher_aliases": ("pubilisher", "publisher", "publisher-name"),
        "publication_year_aliases": ("pubi-year", "pub-year"),
        "issue_aliases": ("isseue", "issue"),
    }
    for extra_name, aliases in alias_groups.items():
        observed = [
            {"tag": alias, "value": entry}
            for alias in aliases
            for entry in values.get(alias, [])
        ]
        if len(observed) > 1:
            extras[extra_name] = observed
    for raw_name, raw_value in node.attrib.items():
        name = _local_name(raw_name)
        if name not in known_attributes and (value := _clean_text(raw_value)):
            extras[f"@{name}"] = value

    doi_raw = _first(values, "doi")
    doi = _normalize_doi(doi_raw)
    # ``<reference><![CDATA[Author. Title. Journal ...]]></reference>``: the
    # element's own text is the whole citation (a structured reference only has
    # whitespace there).  CDATA is already unwrapped by the XML parser.
    citation_text = _clean_text(node.text)
    return ReferenceRecord(
        reference_order=order,
        reference_id=_clean_text(node.get("refebibl-id")),
        reference_type_code=_clean_text(node.get("type-code")),
        reference_type=_clean_text(node.get("type-name")),
        referenced_kci_source_id=_valid_optional_art_id(node.get("arti-id")),
        reference_title=_joined(values, "title"),
        reference_author=_joined(values, "author"),
        reference_journal_name=_joined(values, "journal-name"),
        reference_conference_name=_joined(values, "conference-name"),
        reference_publisher=_first_nonempty(
            values, "pubilisher", "publisher", "publisher-name"
        ),
        reference_publication_year=_first_nonempty(
            values, "pubi-year", "pub-year"
        ),
        reference_volume=_joined(values, "volume"),
        reference_issue=_first_nonempty(values, "isseue", "issue"),
        reference_serial_number=_joined(values, "serno"),
        reference_pages=_joined(values, "page"),
        reference_doi_raw=doi_raw,
        reference_doi=doi,
        reference_doi_status=(
            "missing" if doi_raw is None else "normalized" if doi else "invalid"
        ),
        reference_url=_joined(values, "url"),
        reference_citation_date=_joined(values, "citation-day"),
        reference_site_name=_joined(values, "site-name"),
        reference_registration_date=_joined(values, "registration-day"),
        reference_degree=_joined(values, "degree"),
        reference_university=_joined(values, "university"),
        reference_department=_joined(values, "department"),
        reference_citation_text=citation_text,
        additional_fields_json=(
            json.dumps(extras, ensure_ascii=False, sort_keys=True)
            if extras
            else None
        ),
    )


def _mark_duplicates(references: Sequence[ReferenceRecord]) -> list[ReferenceRecord]:
    seen: dict[tuple[str, ...], int] = {}
    marked: list[ReferenceRecord] = []
    for reference in references:
        identities = _reference_identities(reference)
        matched = next(
            (
                (identity[0], seen[identity])
                for identity in identities
                if identity in seen
            ),
            None,
        )
        duplicate_of = matched[1] if matched is not None else None
        for identity in identities:
            seen.setdefault(identity, reference.reference_order)
        values = asdict(reference)
        if duplicate_of is not None:
            values["duplicate_key_type"] = matched[0]
            values["is_duplicate_within_paper"] = True
            values["duplicate_of_order"] = duplicate_of
        marked.append(ReferenceRecord(**values))
    return marked


def _reference_identities(reference: ReferenceRecord) -> tuple[tuple[str, ...], ...]:
    identities: list[tuple[str, ...]] = []
    if reference.reference_doi:
        identities.append(("doi", reference.reference_doi.casefold()))
    if reference.referenced_kci_source_id:
        identities.append(("kci_source_id", reference.referenced_kci_source_id))
    if identities:
        return tuple(identities)
    title = _normalize_bibliographic_text(reference.reference_title)
    if not title:
        citation = _normalize_bibliographic_text(reference.reference_citation_text)
        return (("citation_text", citation),) if citation else ()
    venue = reference.reference_journal_name or reference.reference_conference_name
    return (
        (
            "bibliographic",
            _normalize_bibliographic_text(reference.reference_type_code),
            title,
            _normalize_bibliographic_text(reference.reference_author),
            _normalize_bibliographic_text(reference.reference_publication_year),
            _normalize_bibliographic_text(venue),
            _normalize_bibliographic_text(reference.reference_publisher),
            _normalize_bibliographic_text(reference.reference_url),
        ),
    )


def collect_references(
    papers: Sequence[KciPaper],
    client: ArticleDetailClient,
    *,
    cache: ReferenceCache | None = None,
    force: bool = False,
    retries: int = DEFAULT_RETRIES,
    logger: ProgressLogger | None = None,
    log_every: int = 10,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> CollectionRun:
    """Collect all papers sequentially and checkpoint every resolved result."""

    if retries < 0:
        raise ValueError("retries must be non-negative")
    if log_every < 1:
        raise ValueError("log_every must be positive")
    if len({paper.source_id for paper in papers}) != len(papers):
        raise ReferenceCollectionError("duplicate_input_source_id")
    timestamp = now or (lambda: datetime.now(SEOUL))
    results: list[PaperReferenceResult] = []
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
        counters[result.status] += 1
        counters["references"] += len(result.references)
        if logger is not None and (
            position == 1
            or position == total
            or position % log_every == 0
            or result.status == "failed"
        ):
            suffix = " (cache)" if result.cache_hit else ""
            logger.emit(
                "KCI-REFERENCE",
                f"{paper.source_id} {result.status}{suffix}",
                current=position,
                total=total,
                counters=counters,
            )

    _validate_results(papers, results)
    return CollectionRun(
        results=tuple(results),
        network_requests=int(getattr(client, "request_count", 0)),
        cache_hits=cache_hits,
    )


def _collect_one(
    paper: KciPaper,
    client: ArticleDetailClient,
    *,
    retries: int,
    sleeper: Callable[[float], None],
    now: Callable[[], datetime],
) -> PaperReferenceResult:
    for attempt in range(1, retries + 2):
        try:
            parsed = client.fetch(paper.source_id)
            return PaperReferenceResult(
                paper=paper,
                status=parsed.status,
                reference_info_present=parsed.reference_info_present,
                references=parsed.references,
                collected_at=_iso_timestamp(now()),
                attempts=attempt,
                reference_info_xml=parsed.reference_info_xml,
            )
        except FatalReferenceError:
            raise
        except RetryableReferenceError as exc:
            if attempt > retries:
                return PaperReferenceResult(
                    paper=paper,
                    status="failed",
                    reference_info_present=None,
                    references=(),
                    collected_at=_iso_timestamp(now()),
                    attempts=attempt,
                    error_code=exc.code,
                )
            delay = max(float(2 ** (attempt - 1)), exc.retry_after or 0.0)
            sleeper(min(delay, 60.0))
        except InvalidReferenceResponse as exc:
            return PaperReferenceResult(
                paper=paper,
                status="failed",
                reference_info_present=None,
                references=(),
                collected_at=_iso_timestamp(now()),
                attempts=attempt,
                error_code=exc.code,
            )
        except Exception:
            # Exception strings can contain a query or response fragment.  Only
            # a stable code is retained so credentials can never leak to disk.
            return PaperReferenceResult(
                paper=paper,
                status="failed",
                reference_info_present=None,
                references=(),
                collected_at=_iso_timestamp(now()),
                attempts=attempt,
                error_code="unexpected_error",
            )
    raise AssertionError("unreachable")


DETAIL_COLUMNS = (
    "paper_index",
    "citing_source_id",
    "citing_title",
    "citing_doi",
    "citing_journal_id",
    "citing_publication_year",
    "reference_order",
    "reference_id",
    "reference_type_code",
    "reference_type",
    "referenced_kci_source_id",
    "reference_title",
    "reference_author",
    "reference_journal_name",
    "reference_conference_name",
    "reference_publisher",
    "reference_publication_year",
    "reference_volume",
    "reference_issue",
    "reference_serial_number",
    "reference_pages",
    "reference_doi_raw",
    "reference_doi",
    "reference_doi_status",
    "reference_url",
    "reference_citation_date",
    "reference_site_name",
    "reference_registration_date",
    "reference_degree",
    "reference_university",
    "reference_department",
    "reference_citation_text",
    "duplicate_key_type",
    "is_duplicate_within_paper",
    "duplicate_of_order",
    "additional_fields_json",
)

SUMMARY_COLUMNS = (
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
    "collection_status",
    "reference_info_present",
    "reference_count",
    "unique_reference_count",
    "duplicate_reference_count",
    "kci_linked_reference_count",
    "doi_reference_count",
    "text_only_reference_count",
    "collected_at",
    "attempts",
    "cache_hit",
    "error_code",
)

VALIDATION_COLUMNS = ("항목", "값", "기대값/정의", "결과")

_COLUMN_WIDTHS = {
    "paper_index": 12,
    "database_id": 13,
    "citing_source_id": 18,
    "source_id": 18,
    "citing_title": 52,
    "title": 52,
    "title_kor": 52,
    "title_eng": 52,
    "citing_doi": 31,
    "doi_raw": 34,
    "doi": 31,
    "citing_journal_id": 28,
    "journal_id": 28,
    "institution_id": 30,
    "citing_publication_year": 18,
    "publication_year": 16,
    "reference_order": 15,
    "reference_id": 18,
    "reference_type_code": 18,
    "reference_type": 23,
    "referenced_kci_source_id": 22,
    "reference_title": 52,
    "reference_author": 38,
    "reference_journal_name": 36,
    "reference_conference_name": 40,
    "reference_publisher": 30,
    "reference_publication_year": 23,
    "reference_volume": 18,
    "reference_issue": 18,
    "reference_serial_number": 23,
    "reference_pages": 18,
    "reference_doi_raw": 34,
    "reference_doi": 32,
    "reference_doi_status": 21,
    "reference_url": 48,
    "reference_citation_date": 24,
    "reference_site_name": 34,
    "reference_registration_date": 28,
    "reference_degree": 20,
    "reference_university": 30,
    "reference_department": 30,
    "reference_citation_text": 60,
    "duplicate_key_type": 20,
    "is_duplicate_within_paper": 25,
    "duplicate_of_order": 20,
    "additional_fields_json": 48,
    "collection_status": 22,
    "reference_info_present": 24,
    "reference_count": 18,
    "unique_reference_count": 24,
    "duplicate_reference_count": 27,
    "kci_linked_reference_count": 28,
    "doi_reference_count": 23,
    "text_only_reference_count": 26,
    "collected_at": 27,
    "attempts": 12,
    "cache_hit": 13,
    "error_code": 28,
    "항목": 34,
    "값": 45,
    "기대값/정의": 52,
    "결과": 14,
}


def export_reference_workbook(
    run: CollectionRun,
    output: Path = DEFAULT_OUTPUT,
    *,
    database: Path = DEFAULT_DATABASE,
    generated_at: datetime | None = None,
    canonical_when_unresolved: bool = False,
) -> ReferenceExportResult:
    """Atomically export detailed references, status, and validation sheets.

    Unresolved papers (``failed`` / ``no_record``) normally divert the workbook
    to ``*.partial.xlsx``.  With ``canonical_when_unresolved`` the canonical
    file is written anyway; those papers keep their unresolved status rows so
    downstream consumers can render them as unavailable.
    """

    results = list(run.results)
    papers = [result.paper for result in results]
    _validate_results(papers, results)
    generated = generated_at or datetime.now(SEOUL)
    detail_rows = [
        _detail_row(result.paper, reference)
        for result in results
        for reference in result.references
    ]
    summary_rows = [_summary_row(result) for result in results]
    validation_rows = _validation_rows(
        results,
        detail_rows,
        run=run,
        database=database,
        generated_at=generated,
    )
    if len(detail_rows) + 1 > 1_048_576:
        raise ReferenceCollectionError("excel_row_limit_exceeded")
    unresolved_count = sum(
        result.status in {"failed", "no_record"} for result in results
    )

    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "논문별 요약"
    detail_sheet = workbook.create_sheet("참고문헌 상세")
    validation_sheet = workbook.create_sheet("검증")
    _write_sheet(
        summary_sheet,
        SUMMARY_COLUMNS,
        summary_rows,
        table_name="KciReferenceStatus",
    )
    _write_sheet(
        detail_sheet,
        DETAIL_COLUMNS,
        detail_rows,
        table_name="KciReferences",
    )
    _write_sheet(
        validation_sheet,
        VALIDATION_COLUMNS,
        validation_rows,
        table_name="KciReferenceValidation",
    )
    workbook.active = 0
    workbook.properties.creator = "Forensics-Paper"
    workbook.properties.title = "KCI 논문 참고문헌 목록"
    workbook.properties.description = (
        "KCI articleDetail 기반 참고문헌과 논문별 수집 상태"
    )

    requested_destination = Path(output).expanduser().resolve()
    destination = (
        _partial_output_path(requested_destination)
        if unresolved_count
        and not canonical_when_unresolved
        and not requested_destination.stem.endswith(".partial")
        else requested_destination
    )
    database_path = Path(database).expanduser().resolve()
    if destination == database_path:
        raise ReferenceCollectionError("output_conflicts_with_database")
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
            expected_papers=len(results),
            expected_references=len(detail_rows),
            expected_validation=len(validation_rows),
        )
        os.chmod(temporary, existing_mode if existing_mode is not None else 0o644)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        workbook.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise

    return ReferenceExportResult(
        path=destination,
        paper_count=len(results),
        reference_count=len(detail_rows),
        failed_count=unresolved_count,
    )


@dataclass(frozen=True, slots=True)
class ReferenceManifestResult:
    path: Path
    paper_count: int
    file_count: int
    text_only_reference_count: int
    unresolved_count: int


def _is_text_only(reference: ReferenceRecord) -> bool:
    return reference.reference_citation_text is not None and reference.reference_title is None


def write_reference_manifest(
    run: CollectionRun,
    path: Path = DEFAULT_MANIFEST,
    *,
    xml_directory: Path = DEFAULT_XML_DIR,
    database: Path = DEFAULT_DATABASE,
    generated_at: datetime | None = None,
) -> ReferenceManifestResult:
    """Index the verbatim ``<referenceInfo>`` files of one collection run.

    One entry per paper in ``kci_papers`` order records the collection status,
    the file (relative to the manifest) and its SHA-256, so a reader such as
    ``paper_md.py`` can tell "KCI lists no references" from "not collected"
    and detect a file edited by hand.  Unresolved papers are listed too; the
    manifest is written for partial runs as well.
    """

    results = list(run.results)
    papers = [result.paper for result in results]
    _validate_results(papers, results)
    generated = generated_at or datetime.now(SEOUL)
    destination = Path(path).expanduser().resolve()
    directory = Path(xml_directory).expanduser().resolve()
    try:
        relative_directory = directory.relative_to(destination.parent)
    except ValueError:
        raise ReferenceCollectionError("manifest_xml_directory_outside") from None
    entries: list[dict[str, Any]] = []
    file_count = 0
    text_only = 0
    for result in results:
        source_id = result.paper.source_id
        file_name: str | None = None
        digest: str | None = None
        if result.reference_info_xml is not None:
            file_path = directory / f"{source_id}.xml"
            try:
                data = file_path.read_bytes()
            except OSError:
                raise ReferenceCollectionError("reference_info_file_missing") from None
            if data != result.reference_info_xml.encode("utf-8"):
                raise ReferenceCollectionError("reference_info_file_mismatch")
            digest = hashlib.sha256(data).hexdigest()
            file_name = (relative_directory / f"{source_id}.xml").as_posix()
            file_count += 1
        resolved = result.status in _CACHEABLE_STATUSES
        paper_text_only = sum(_is_text_only(reference) for reference in result.references)
        text_only += paper_text_only
        entries.append(
            {
                "paper_index": result.paper.paper_index,
                "source_id": source_id,
                "status": result.status,
                "reference_info_present": result.reference_info_present,
                "reference_count": len(result.references) if resolved else None,
                "text_only_reference_count": paper_text_only if resolved else None,
                "kci_linked_reference_count": (
                    sum(reference.referenced_kci_source_id is not None for reference in result.references)
                    if resolved
                    else None
                ),
                "collected_at": result.collected_at,
                "cache_hit": result.cache_hit,
                "error_code": result.error_code,
                "file": file_name,
                "sha256": digest,
            }
        )
    unresolved = sum(result.status not in _CACHEABLE_STATUSES for result in results)
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "api_code": KCI_OPENAPI_API_CODE,
        "generated_at": _iso_timestamp(generated),
        "database": os.path.relpath(Path(database).expanduser().resolve(), REPOSITORY_ROOT),
        "database_snapshot_sha256": _paper_snapshot_sha256(papers),
        "xml_directory": relative_directory.as_posix(),
        "paper_count": len(results),
        "file_count": file_count,
        "text_only_reference_count": text_only,
        "unresolved_count": unresolved,
        "papers": entries,
    }
    _write_json_atomic(destination, payload, default_mode=0o644)
    return ReferenceManifestResult(
        path=destination,
        paper_count=len(results),
        file_count=file_count,
        text_only_reference_count=text_only,
        unresolved_count=unresolved,
    )


def _detail_row(paper: KciPaper, reference: ReferenceRecord) -> dict[str, Any]:
    values = asdict(reference)
    return {
        "paper_index": paper.paper_index,
        "citing_source_id": paper.source_id,
        "citing_title": paper.title,
        "citing_doi": paper.doi,
        "citing_journal_id": paper.journal_id,
        "citing_publication_year": paper.publication_year,
        **values,
    }


def _summary_row(result: PaperReferenceResult) -> dict[str, Any]:
    resolved = result.status in {"collected", "no_reference_data"}
    references = result.references
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
        "collection_status": result.status,
        "reference_info_present": result.reference_info_present,
        "reference_count": len(references) if resolved else None,
        "unique_reference_count": result.unique_count,
        "duplicate_reference_count": result.duplicate_count,
        "kci_linked_reference_count": (
            sum(reference.referenced_kci_source_id is not None for reference in references)
            if resolved
            else None
        ),
        "doi_reference_count": (
            sum(reference.reference_doi is not None for reference in references)
            if resolved
            else None
        ),
        "text_only_reference_count": (
            sum(_is_text_only(reference) for reference in references)
            if resolved
            else None
        ),
        "collected_at": result.collected_at,
        "attempts": result.attempts,
        "cache_hit": result.cache_hit,
        "error_code": result.error_code,
    }


def _validation_rows(
    results: Sequence[PaperReferenceResult],
    details: Sequence[Mapping[str, Any]],
    *,
    run: CollectionRun,
    database: Path,
    generated_at: datetime,
) -> list[dict[str, Any]]:
    statuses = Counter(result.status for result in results)
    reference_count = sum(len(result.references) for result in results)
    duplicate_count = sum(
        result.duplicate_count or 0 for result in results
    )
    linked_count = sum(
        reference.referenced_kci_source_id is not None
        for result in results
        for reference in result.references
    )
    doi_count = sum(
        reference.reference_doi is not None
        for result in results
        for reference in result.references
    )
    unresolved_count = statuses["failed"] + statuses["no_record"]
    collection_complete = unresolved_count == 0
    snapshot_sha256 = _paper_snapshot_sha256(
        [result.paper for result in results]
    )
    checks = {
        "상태 합계": sum(statuses.values()) == len(results),
        "상세 행 합계": len(details) == reference_count,
        "source_id 고유성": (
            len({result.paper.source_id for result in results}) == len(results)
        ),
        "참고문헌 순서": all(
            tuple(reference.reference_order for reference in result.references)
            == tuple(range(1, len(result.references) + 1))
            for result in results
        ),
        "API 원문 미저장": True,
    }
    values: list[tuple[str, Any, str, str]] = [
        ("report_schema_version", REPORT_SCHEMA_VERSION, "현재 보고서 계약", "PASS"),
        ("generated_at", _iso_timestamp(generated_at), "Asia/Seoul ISO 8601", "PASS"),
        ("database_file", Path(database).name, "읽기 전용 입력 DB 파일명", "PASS"),
        ("api_code", KCI_OPENAPI_API_CODE, "articleDetail", "PASS"),
        (
            "report_kind",
            "complete" if collection_complete else "partial",
            "complete 또는 partial",
            "PASS" if collection_complete else "FAIL",
        ),
        (
            "collection_complete",
            collection_complete,
            "canonical 게시 조건은 True",
            "PASS" if collection_complete else "FAIL",
        ),
        (
            "database_snapshot_sha256",
            snapshot_sha256,
            "수집 시작 시 DB 대상 전체 메타데이터 지문",
            "PASS",
        ),
        ("paper_count", len(results), "DB 스냅샷 전체 행 수", "PASS"),
        ("status_collected", statuses["collected"], "참고문헌 1건 이상", "PASS"),
        (
            "status_no_reference_data",
            statuses["no_reference_data"],
            "KCI API 제공 참고문헌 0건",
            "PASS",
        ),
        ("status_no_record", statuses["no_record"], "API record 미제공", "PASS"),
        ("status_failed", statuses["failed"], "수집 미확정", "PASS"),
        ("unresolved_count", unresolved_count, "canonical은 0이어야 함", "PASS" if collection_complete else "FAIL"),
        ("reference_count", reference_count, "상세 데이터 행 수", "PASS"),
        (
            "unique_reference_count",
            reference_count - duplicate_count,
            "중복 의심 표시가 없는 행 수",
            "PASS",
        ),
        (
            "duplicate_reference_count",
            duplicate_count,
            "삭제하지 않고 표시한 논문 내 중복 의심 수",
            "PASS",
        ),
        (
            "kci_linked_reference_count",
            linked_count,
            "referenced_kci_source_id 보유 수",
            "PASS",
        ),
        ("doi_reference_count", doi_count, "정규화 DOI 보유 수", "PASS"),
        ("cache_hits", run.cache_hits, "재개 캐시 사용 논문 수", "PASS"),
        (
            "network_requests_including_retries",
            run.network_requests,
            "이번 실행의 articleDetail HTTP 요청 수",
            "PASS",
        ),
    ]
    values.extend(
        (name, passed, "True", "PASS" if passed else "FAIL")
        for name, passed in checks.items()
    )
    if not all(checks.values()):
        raise ReferenceCollectionError("report_validation_failed")
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
    worksheet.auto_filter.ref = (
        f"A1:{get_column_letter(len(columns))}{max(1, worksheet.max_row)}"
    )

    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    header_alignment = Alignment(
        horizontal="center", vertical="center", wrap_text=True
    )
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

    status_index = (
        columns.index("collection_status") if "collection_status" in columns else None
    )
    for row in worksheet.iter_rows(min_row=2):
        status = row[status_index].value if status_index is not None else None
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = border
            if status == "failed":
                cell.fill = PatternFill(fill_type="solid", fgColor="FCE4D6")
            elif status == "no_record":
                cell.fill = PatternFill(fill_type="solid", fgColor="FFF2CC")
    for index, name in enumerate(columns, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = _COLUMN_WIDTHS.get(
            name, 18
        )
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
            text = _excel_text(str(raw))
            cell.value = text
            cell.data_type = "s"


def _verify_workbook(
    path: Path,
    *,
    expected_papers: int,
    expected_references: int,
    expected_validation: int,
) -> None:
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        if workbook.sheetnames != ["논문별 요약", "참고문헌 상세", "검증"]:
            raise ReferenceCollectionError("workbook_sheet_validation_failed")
        expected_rows = {
            "논문별 요약": expected_papers + 1,
            "참고문헌 상세": expected_references + 1,
            "검증": expected_validation + 1,
        }
        for name, row_count in expected_rows.items():
            worksheet = workbook[name]
            if worksheet.max_row != row_count:
                raise ReferenceCollectionError("workbook_row_validation_failed")
            for row in worksheet.iter_rows():
                if any(cell.data_type == "f" for cell in row):
                    raise ReferenceCollectionError("workbook_formula_validation_failed")
    finally:
        workbook.close()


def _validate_results(
    papers: Sequence[KciPaper],
    results: Sequence[PaperReferenceResult],
) -> None:
    if len(papers) != len(results):
        raise ReferenceCollectionError("result_count_mismatch")
    expected_ids = [paper.source_id for paper in papers]
    actual_ids = [result.paper.source_id for result in results]
    if expected_ids != actual_ids or len(set(actual_ids)) != len(actual_ids):
        raise ReferenceCollectionError("result_identity_mismatch")
    for result in results:
        orders = tuple(reference.reference_order for reference in result.references)
        if orders != tuple(range(1, len(result.references) + 1)):
            raise ReferenceCollectionError("reference_order_mismatch")
        for reference in result.references:
            duplicate = reference.duplicate_of_order
            if reference.is_duplicate_within_paper != (duplicate is not None):
                raise ReferenceCollectionError("invalid_duplicate_marker")
            if duplicate is not None and not (1 <= duplicate < reference.reference_order):
                raise ReferenceCollectionError("invalid_duplicate_marker")


def _reference_from_mapping(value: Any) -> ReferenceRecord:
    expected = set(ReferenceRecord.__dataclass_fields__)
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid cached reference")
    order = value.get("reference_order")
    duplicate = value.get("duplicate_of_order")
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        raise ValueError("invalid cached reference order")
    if duplicate is not None and (
        not isinstance(duplicate, int)
        or isinstance(duplicate, bool)
        or duplicate < 1
        or duplicate >= order
    ):
        raise ValueError("invalid cached duplicate order")
    for name, item in value.items():
        if name in {
            "reference_order",
            "is_duplicate_within_paper",
            "duplicate_of_order",
        }:
            continue
        if item is not None and not isinstance(item, str):
            raise ValueError("invalid cached reference value")
    if not isinstance(value.get("is_duplicate_within_paper"), bool):
        raise ValueError("invalid cached duplicate flag")
    if value.get("reference_doi_status") not in {"missing", "normalized", "invalid"}:
        raise ValueError("invalid cached DOI status")
    return ReferenceRecord(**value)


def _load_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return values
    if len(raw) > 1024 * 1024:
        raise ReferenceCollectionError("env_file_too_large")
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp949", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ReferenceCollectionError("env_file_decode_failed")
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
            raise ReferenceCollectionError("invalid_env_quoting")
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _resolve_api_key(env_file: Path) -> str:
    configured = os.environ.get("KCI_OPEN_API_KEY")
    if configured is None:
        configured = _load_env_values(env_file).get("KCI_OPEN_API_KEY")
    if configured is None:
        raise FatalReferenceError("api_key_not_configured")
    return _validate_api_key(configured)


def _resolve_rps(cli_value: float | None, env_file: Path) -> float:
    if cli_value is not None:
        value = cli_value
    else:
        raw = os.environ.get("KCI_DETAIL_RPS")
        if raw is None:
            raw = _load_env_values(env_file).get("KCI_DETAIL_RPS")
        try:
            value = DEFAULT_RPS if raw in {None, ""} else float(raw)
        except ValueError:
            raise ReferenceCollectionError("invalid_kci_detail_rps") from None
    if not math.isfinite(value) or value <= 0 or value > 4:
        raise ReferenceCollectionError("invalid_kci_detail_rps")
    return value


def _validate_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise FatalReferenceError("invalid_api_key")
    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > 4096
        or any(character.isspace() or ord(character) < 32 for character in stripped)
    ):
        raise FatalReferenceError("invalid_api_key")
    return stripped


def _validate_source_id(value: str) -> None:
    if not isinstance(value, str) or _ART_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid KCI article ID")


def _valid_optional_art_id(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    if _ART_ID_RE.fullmatch(cleaned) is None:
        raise InvalidReferenceResponse("invalid_referenced_article_id")
    return cleaned


def _normalize_doi(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    try:
        return normalize_doi(unicodedata.normalize("NFKC", cleaned))
    except InvalidDoiError:
        return None


def _normalize_bibliographic_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _direct_children(
    element: ElementTree.Element,
    name: str,
) -> list[ElementTree.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _element_text(element: ElementTree.Element) -> str | None:
    return _clean_text("".join(element.itertext()))


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _joined(values: Mapping[str, list[str]], name: str) -> str | None:
    entries = values.get(name, [])
    return " ; ".join(entries) if entries else None


def _first(values: Mapping[str, list[str]], name: str) -> str | None:
    entries = values.get(name, [])
    return entries[0] if entries else None


def _first_nonempty(
    values: Mapping[str, list[str]],
    *names: str,
) -> str | None:
    for name in names:
        if value := _first(values, name):
            return value
    return None


def _optional_year(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ReferenceCollectionError("invalid_publication_year")
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ReferenceCollectionError("invalid_publication_year") from None
    if year < 1000 or year > 9999:
        raise ReferenceCollectionError("invalid_publication_year")
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
            deadline = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), 60.0)


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=SEOUL)
    return value.astimezone(SEOUL).isoformat(timespec="seconds")


def _excel_text(value: str) -> str:
    sanitized = _INVALID_EXCEL_XML_RE.sub("", value)
    if len(sanitized) > 32767:
        raise ReferenceCollectionError("excel_cell_too_long")
    return sanitized


def _write_json_atomic(path: Path, value: Any, *, default_mode: int = 0o600) -> None:
    serialized = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    _write_bytes_atomic(path, serialized, default_mode=default_mode)


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _write_bytes_atomic(path: Path, serialized: bytes, *, default_mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, existing_mode if existing_mode is not None else default_mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _partial_output_path(path: Path) -> Path:
    suffix = path.suffix if path.suffix else ".xlsx"
    return path.with_name(f"{path.stem}.partial{suffix}")


def _paper_snapshot_sha256(papers: Sequence[KciPaper]) -> str:
    payload = json.dumps(
        [asdict(paper) for paper in papers],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "kci_papers의 ART ID를 articleDetail로 조회해 참고문헌 Excel을 생성합니다."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--xml-dir",
        type=Path,
        default=DEFAULT_XML_DIR,
        help="응답의 <referenceInfo> 원문을 논문별 <ART id>.xml로 저장할 폴더",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="XML 파일 목록·상태·SHA-256을 기록할 JSON (paper_md.py가 읽음)",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--rps",
        type=float,
        default=None,
        help="초당 요청 수(기본: KCI_DETAIL_RPS 또는 0.5, 최대 4)",
    )
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument(
        "--force",
        action="store_true",
        help="성공 캐시도 무시하고 모든 논문을 다시 조회",
    )
    parser.add_argument(
        "--canonical-when-unresolved",
        action="store_true",
        help=(
            "미확정 논문이 있어도 --output 경로에 그대로 기록(.partial 우회 없음). "
            "미확정 행은 그대로 남고 종료 코드 2는 유지"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logger = ProgressLogger()
    try:
        papers = load_kci_papers(args.database)
        rps = _resolve_rps(args.rps, args.env_file)
        api_key = _resolve_api_key(args.env_file)
        logger.emit(
            "KCI-REFERENCE",
            f"수집 시작: 논문 {len(papers)}편, 요청 속도 {rps:g} req/s",
            current=0,
            total=len(papers),
        )
        client = KciReferenceClient(
            api_key,
            requests_per_second=rps,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
        )
        run = collect_references(
            papers,
            client,
            cache=ReferenceCache(args.cache_dir, xml_directory=args.xml_dir),
            force=args.force,
            retries=args.retries,
            logger=logger,
            log_every=args.log_every,
        )
        exported = export_reference_workbook(
            run,
            args.output,
            database=args.database,
            canonical_when_unresolved=args.canonical_when_unresolved,
        )
        manifest = write_reference_manifest(
            run,
            args.manifest,
            xml_directory=args.xml_dir,
            database=args.database,
        )
    except KeyboardInterrupt:
        print(
            "[KCI-REFERENCE] 사용자가 중단했습니다. 완료 캐시는 보존됩니다.",
            file=sys.stderr,
        )
        return 130
    except FatalReferenceError as exc:
        print(f"[KCI-REFERENCE] 실행 중단: {exc.code}", file=sys.stderr)
        return 2
    except (ReferenceCollectionError, ValueError, OSError) as exc:
        code = (
            exc.code if isinstance(exc, ReferenceCollectionError) else type(exc).__name__
        )
        print(f"[KCI-REFERENCE] 실행 실패: {code}", file=sys.stderr)
        return 2

    print(
        "[KCI-REFERENCE] 완료: "
        f"papers={exported.paper_count}, references={exported.reference_count}, "
        f"unresolved={exported.failed_count}, output={exported.path}, "
        f"referenceInfo_files={manifest.file_count}, "
        f"text_only_references={manifest.text_only_reference_count}, "
        f"manifest={manifest.path}",
        flush=True,
    )
    return 2 if exported.failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
