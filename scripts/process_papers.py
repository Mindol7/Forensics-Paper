#!/usr/bin/env python3
"""Resolve all selected KCI papers to public PDFs and optionally analyze them."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import math
import os
import re
import shutil
import sys
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
MAX_CORPUS_BATCH = 10_000
PROVIDER_CIRCUIT_BREAKER_THRESHOLD = 3
DBPIA_OPENAPI_DAILY_LIMIT = 2_500
DBPIA_OPENAPI_DAILY_STATE_FILE = "dbpia-openapi-daily-quota.json"
DBPIA_BROWSER_RESUME_SCHEMA_VERSION = 1
DBPIA_BROWSER_RESUME_FILE = ".dbpia-browser-resume.json"
sys.path.insert(0, str(REPOSITORY_ROOT))
load_dotenv(REPOSITORY_ROOT / ".env")

from PaperPipeline.codex_analyzer import (  # noqa: E402
    DEFAULT_CODEX_MODEL,
    CodexConfig,
    CodexPaperAnalyzer,
)
from PaperPipeline.db_source import (  # noqa: E402
    InvalidDoiError,
    iter_paper_candidates,
    normalize_doi,
)
from PaperPipeline.dbpia_browser import (  # noqa: E402
    DBPIA_BROWSER_RESOLVER_NAME,
    AuthenticatedDbpiaBrowserResolver,
    DbpiaBrowserConfig,
    default_dbpia_profile_dir,
    validate_dbpia_profile_dir,
)
from PaperPipeline.dbpia_openapi import (  # noqa: E402
    DBPIA_KDFS_JOURNAL,
    DBPIA_KDFS_PUBLISHER,
    DbpiaOpenApiConfig,
    DbpiaOpenApiHttpClient,
    DbpiaOpenApiResolver,
)
from PaperPipeline.downloader import (  # noqa: E402
    DOWNLOAD_SCHEMA_VERSION,
    DownloadConfig,
    DbpiaBrowserResolver,
    KdfsResolver,
    KicjResolver,
    PdfDownloader,
    PoliceResolver,
    download_artifact_is_current,
    sanitize_dbpia_download_manifest,
)
from PaperPipeline.http_client import (  # noqa: E402
    HttpClientError,
    InterProcessRateLimiter,
    PerHostRateLimiter,
    SafeHttpClient,
)
from PaperPipeline.identifier_resolver import (  # noqa: E402
    CrossrefTitleResolver,
    IdentifierResolution,
    IdentifierResolverConfig,
    paper_title_queries,
)
from PaperPipeline.kci_journal import (  # noqa: E402
    KciJournalError,
    KciJournalIndexLoader,
)
from PaperPipeline.kci_openapi_identifier import (  # noqa: E402
    KCI_OPENAPI_DETAIL_URL,
    KciOpenApiHttpClient,
    KciOpenApiIdentifierConfig,
    KciOpenApiIdentifierResolver,
)
from PaperPipeline.kdfs_resolver import (  # noqa: E402
    KdfsJamsAuthorizedPdfResolver,
    KdfsResolverConfig,
)
from PaperPipeline.kicj_resolver import (  # noqa: E402
    KicjPublicPdfResolver,
    KicjResolution,
)
from PaperPipeline.records import PaperCandidate, ResolverCandidate  # noqa: E402
from PaperPipeline.paper_files import (  # noqa: E402
    PAPER_FILE_INDEX_NAME,
    extend_paper_file_index_if_present,
)
from PaperPipeline.openalex_identifier import (  # noqa: E402
    OpenAlexIdentifierConfig,
    OpenAlexIdentifierResolution,
    OpenAlexTitleResolver,
)
from PaperPipeline.police_resolver import (  # noqa: E402
    PoliceResolution,
    PoliceResolverConfig,
    PoliceUniversityPublicPdfResolver,
)
from PaperPipeline.semantic_scholar import (  # noqa: E402
    SemanticScholarBatchResolver,
    SemanticScholarConfig,
    SemanticScholarResolution,
)
from PaperPipeline.semantic_scholar_title import (  # noqa: E402
    SemanticScholarTitleConfig,
    SemanticScholarTitleResolution,
    SemanticScholarTitleResolver,
)
from PaperPipeline.storage import (  # noqa: E402
    ArtifactPaths,
    load_json_object,
    write_json_atomic,
)
from PaperPipeline.spo_resolver import (  # noqa: E402
    SpoPublicPdfResolver,
    SpoResolverConfig,
)
from PaperPipeline.taxonomy import load_taxonomy  # noqa: E402
from PaperPipeline.unpaywall_identifier import (  # noqa: E402
    UnpaywallIdentifierConfig,
    UnpaywallIdentifierResolution,
    UnpaywallTitleResolver,
)


@dataclass(frozen=True, slots=True)
class DownloadRuntime:
    config: DownloadConfig
    connect_timeout: float
    read_timeout: float
    retries: int
    allow_http: bool
    rate_limiter: PerHostRateLimiter
    kicj_resolver: KicjResolver
    police_resolver: PoliceResolver
    spo_resolver: SpoPublicPdfResolver
    kdfs_resolver: KdfsResolver
    dbpia_browser_resolver: DbpiaBrowserResolver | None = None


@dataclass(frozen=True, slots=True)
class DownloadBatchResult:
    counts: Counter[str]
    successful_source_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class DownloadPreparation:
    papers: list[PaperCandidate]
    kci_journal_pdf_urls: Mapping[str, str]
    preverified_candidates: Mapping[str, tuple[ResolverCandidate, ...]]
    semantic_scholar_candidates: Mapping[str, ResolverCandidate]
    semantic_scholar_resolutions: Mapping[str, Mapping[str, Any]]
    platform_landing_urls: Mapping[str, str]
    ephemeral_platform_landing_source_ids: frozenset[str]
    identifier_counts: Counter[str]
    semantic_scholar_counts: Counter[str]


class _SynchronizedKicjResolver:
    """Share one robots-aware resolver safely across download workers."""

    def __init__(self, resolver: KicjPublicPdfResolver) -> None:
        self._resolver = resolver
        self._lock = threading.Lock()

    def resolve(self, paper: PaperCandidate) -> KicjResolution:
        with self._lock:
            return self._resolver.resolve(paper)


class _SynchronizedPoliceResolver:
    """Share one robots/form-aware resolver safely across all workers."""

    def __init__(
        self,
        resolver: PoliceUniversityPublicPdfResolver,
    ) -> None:
        self._resolver = resolver
        self._lock = threading.Lock()

    def resolve(self, paper: PaperCandidate) -> PoliceResolution:
        with self._lock:
            return self._resolver.resolve(paper)


class _DbpiaOpenApiRunGate:
    """Apply shared per-run and cross-process daily request budgets."""

    def __init__(
        self,
        *,
        max_requests: int,
        limiter: PerHostRateLimiter,
        daily_state_path: Path | None = None,
        daily_limit: int = DBPIA_OPENAPI_DAILY_LIMIT,
        day_provider: Callable[[], str] | None = None,
    ) -> None:
        if not 1 <= max_requests <= 2_500:
            raise ValueError("invalid DBpia Open API run budget")
        if not 1 <= daily_limit <= DBPIA_OPENAPI_DAILY_LIMIT:
            raise ValueError("invalid DBpia Open API daily budget")
        self.max_requests = max_requests
        self.requests_started = 0
        self._limiter = limiter
        self._daily_state_path = daily_state_path
        self._daily_limit = daily_limit
        self._day_provider = day_provider or _dbpia_quota_day
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        with self._lock:
            if self.requests_started >= self.max_requests:
                raise HttpClientError("DBpia Open API run request budget exhausted")
            self.requests_started += 1
        # Pace first so a request sleeping across KST midnight is charged to
        # the day on which it can actually be sent.
        self._limiter.wait(url)
        if self._daily_state_path is not None:
            self._reserve_daily_request()

    def _reserve_daily_request(self) -> None:
        assert self._daily_state_path is not None
        state_path = self._daily_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = state_path.with_name(f"{state_path.name}.lock")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        handle: Any = None
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "a+", encoding="ascii")
            descriptor = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            today = self._day_provider()
            state_exists = state_path.exists()
            state = load_json_object(state_path)
            if state_exists and not _valid_dbpia_daily_quota_state(state):
                raise HttpClientError(
                    "DBpia Open API daily budget state is invalid"
                )
            if state is not None and state["date"] > today:
                raise HttpClientError(
                    "DBpia Open API daily budget state is from the future"
                )
            if state is None or state.get("date") != today:
                used = 0
            else:
                used = state["requests_started"]
            if used >= self._daily_limit:
                raise HttpClientError(
                    "DBpia Open API daily request budget exhausted"
                )
            write_json_atomic(
                state_path,
                {
                    "schema_version": 1,
                    "date": today,
                    "requests_started": used + 1,
                    "daily_limit": self._daily_limit,
                    "updated_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
        except HttpClientError:
            raise
        except OSError:
            raise HttpClientError(
                "DBpia Open API daily budget state unavailable"
            ) from None
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    handle.close()
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)


def _dbpia_quota_day() -> str:
    """Return the current Korea calendar day without depending on host TZ."""

    return (
        datetime.now(timezone.utc) + timedelta(hours=9)
    ).date().isoformat()


def _valid_dbpia_daily_quota_state(
    state: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(state, Mapping) or state.get("schema_version") != 1:
        return False
    date = state.get("date")
    used = state.get("requests_started")
    limit = state.get("daily_limit")
    valid_date = False
    if isinstance(date, str):
        with contextlib.suppress(ValueError):
            valid_date = (
                datetime.strptime(date, "%Y-%m-%d")
                .date()
                .isoformat()
                == date
            )
    return bool(
        valid_date
        and not isinstance(used, bool)
        and isinstance(used, int)
        and 0 <= used <= DBPIA_OPENAPI_DAILY_LIMIT
        and not isinstance(limit, bool)
        and isinstance(limit, int)
        and 1 <= limit <= DBPIA_OPENAPI_DAILY_LIMIT
    )


def default_dbpia_openapi_daily_state_path() -> Path:
    """Return one stable per-user ledger independent of output directory."""

    configured = os.getenv("XDG_STATE_HOME")
    configured_path = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else None
    )
    base = (
        configured_path
        if configured_path is not None and configured_path.is_absolute()
        else Path.home() / ".local" / "state"
    )
    return (
        base / "forensics-paper" / DBPIA_OPENAPI_DAILY_STATE_FILE
    ).resolve()


def _repository_path(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (REPOSITORY_ROOT / path).resolve()


def _manifest_dir_for_args(
    args: argparse.Namespace,
    papers_dir: Path,
) -> Path:
    configured = getattr(args, "manifest_dir", None)
    return _repository_path(configured) if configured else papers_dir


def _artifact_paths_for_args(
    args: argparse.Namespace,
    papers_dir: Path,
    source_id: str,
) -> ArtifactPaths:
    return ArtifactPaths.for_source(
        papers_dir,
        source_id,
        manifest_root=_manifest_dir_for_args(args, papers_dir),
        # Preparation and dry-run paths are read-only and may include DB rows
        # that have never produced a PDF.  The actual downloader constructs a
        # strict path after main() extends the title index.
        allow_unindexed_pdf=True,
    )


def _database_url(value: str | None) -> str:
    raw = value or os.getenv("DATABASE_URL", "sqlite:///kci_pipeline.db")
    url = make_url(raw)
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        database_path = Path(url.database)
        if not database_path.is_absolute():
            database_path = (REPOSITORY_ROOT / database_path).resolve()
        if not database_path.is_file():
            raise ValueError(f"SQLite DB 파일이 존재하지 않습니다: {database_path}")
        url = url.set(database=str(database_path))
    return url.render_as_string(hide_password=False)


def _add_selection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        help="SQLAlchemy DB URL (기본: DATABASE_URL 또는 sqlite:///kci_pipeline.db)",
    )
    parser.add_argument(
        "--source-table",
        choices=["kci_papers", "kci_corpus"],
        default="kci_papers",
        help="기본값은 선별 논문이 있는 kci_papers",
    )
    parser.add_argument(
        "--allow-corpus",
        action="store_true",
        help="대형 kci_corpus 사용을 명시적으로 허용(--limit/--source-id도 필요)",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="특정 source_id만 처리(반복 가능)",
    )
    parser.add_argument(
        "--institution-id",
        action="append",
        default=[],
        help="특정 발행기관/학회 institution_id만 처리(반복 가능)",
    )
    parser.add_argument("--after-id", type=int, default=0, help="이 DB PK 이후부터 처리")
    parser.add_argument("--limit", type=int, help="이번 실행의 최대 논문 수")
    doi_selection = parser.add_mutually_exclusive_group()
    doi_selection.add_argument(
        "--include-missing-doi",
        dest="include_missing_doi",
        action="store_true",
        help="DOI 없는 행도 source_id/제목으로 해소(기본값)",
    )
    doi_selection.add_argument(
        "--doi-only",
        dest="include_missing_doi",
        action="store_false",
        help="DB에 DOI가 저장된 행만 선택",
    )
    doi_selection.add_argument(
        "--missing-doi-only",
        action="store_true",
        help="DB에 DOI가 없거나 유효하지 않은 행만 선택",
    )
    parser.set_defaults(include_missing_doi=True, missing_doi_only=False)
    parser.add_argument(
        "--papers-dir",
        default=os.getenv("ANALYZER_PAPER_DIR", "Analyzer/Paper"),
        help="PDF 산출물 디렉터리",
    )
    parser.add_argument(
        "--manifest-dir",
        default=os.getenv("ANALYZER_MANIFEST_DIR", "Analyzer/Manifest"),
        help="download manifest·lock·분석 상태 디렉터리",
    )
    parser.add_argument("--force", action="store_true", help="현재 산출물을 다시 생성")
    parser.add_argument("--dry-run", action="store_true", help="네트워크/Codex 호출 없이 대상만 확인")


def _add_download_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help=(
            "선택한 모든 논문의 PDF가 downloaded/reused 상태여야 성공으로 종료 "
            "(기본은 일부 성공도 허용하는 best-effort)"
        ),
    )
    parser.add_argument(
        "--download-workers",
        type=int,
        default=int(os.getenv("PAPER_DOWNLOAD_WORKERS", "4")),
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=float(os.getenv("PAPER_CONNECT_TIMEOUT", "10")),
    )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=float(os.getenv("PAPER_READ_TIMEOUT", "60")),
    )
    parser.add_argument(
        "--http-retries",
        type=int,
        default=int(os.getenv("PAPER_HTTP_RETRIES", "2")),
    )
    parser.add_argument(
        "--per-host-rps",
        type=float,
        default=float(os.getenv("PAPER_PER_HOST_RPS", "2")),
        help="동일 호스트에 대한 전체 worker 합산 초당 최대 요청 수",
    )
    parser.add_argument(
        "--max-pdf-mb",
        type=int,
        default=int(os.getenv("PAPER_MAX_PDF_MB", "50")),
    )
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="PDF resolver의 평문 HTTP URL 허용(기본은 HTTPS만)",
    )
    parser.add_argument(
        "--unpaywall-email",
        default=os.getenv("UNPAYWALL_EMAIL"),
        help="Unpaywall API가 요구하는 연락 이메일",
    )
    parser.add_argument(
        "--kci-detail-rps",
        type=float,
        default=float(os.getenv("KCI_DETAIL_RPS", "0.5")),
        help="KCI articleDetail DOI 복구의 초당 최대 요청 수(기본 0.5)",
    )
    parser.add_argument(
        "--openalex-api-key",
        default=os.getenv("OPENALEX_API_KEY"),
        help="선택적 OpenAlex API 키",
    )
    parser.add_argument(
        "--elsevier-api-key",
        default=os.getenv("ELSEVIER_API_KEY"),
        help="선택적 Elsevier Article Retrieval API 키",
    )
    parser.add_argument(
        "--semantic-scholar-api-key",
        default=os.getenv("SEMANTIC_SCHOLAR_API_KEY"),
        help="선택적 Semantic Scholar API 키(없으면 비인증 공용 batch 사용)",
    )
    parser.add_argument(
        "--semantic-scholar-rps",
        type=float,
        default=float(os.getenv("SEMANTIC_SCHOLAR_RPS", "0.045")),
        help=(
            "Semantic Scholar 전체 endpoint 합산 초당 요청 수(1 미만; "
            "title-match 실측 안전 기본값 0.045)"
        ),
    )
    parser.add_argument(
        "--semantic-scholar-batch-size",
        type=int,
        default=int(os.getenv("SEMANTIC_SCHOLAR_BATCH_SIZE", "100")),
        help="Semantic Scholar paper/batch 요청당 DOI 수(1~500, 기본 100)",
    )
    parser.add_argument(
        "--crossref-email",
        default=os.getenv("CROSSREF_EMAIL"),
        help="Crossref polite-pool 연락 이메일",
    )
    # DBpia puts its key in the request query.  Accept it only from .env/the
    # process environment so it cannot also leak through shell history or the
    # OS process list as a command-line argument.
    parser.set_defaults(dbpia_api_key=os.getenv("DBPIA_API_KEY"))
    # Authentication secrets are environment-only so they never appear in
    # shell history or the process list.  They are passed directly to the
    # Playwright session and are excluded from dataclass representations.
    parser.set_defaults(
        dbpia_login_id=os.getenv("DBPIA_LOGIN_ID"),
        dbpia_login_password=os.getenv("DBPIA_LOGIN_PASSWORD"),
        dbpia_required_institution_name=os.getenv(
            "DBPIA_REQUIRED_INSTITUTION_NAME"
        ),
    )
    parser.add_argument(
        "--dbpia-search-rps",
        type=float,
        default=float(os.getenv("DBPIA_SEARCH_RPS", "0.5")),
        help="DBpia 공식 검색 API 초당 최대 요청 수(기본 0.5)",
    )
    parser.add_argument(
        "--dbpia-openapi-max-requests",
        type=int,
        default=int(os.getenv("DBPIA_OPENAPI_MAX_REQUESTS", "500")),
        help=(
            "한 실행의 DBpia Open API 실제 HTTP 요청 상한"
            "(1~2500, retry 포함, 기본 500)"
        ),
    )
    parser.add_argument(
        "--dbpia-node",
        action="append",
        default=[],
        metavar="SOURCE_ID=NODE_ID",
        help=(
            "이미 확인한 DBpia NODE를 특정 KCI source_id에 연결"
            "(반복 가능; 예: ART003071074=NODE11750870)"
        ),
    )
    parser.add_argument(
        "--enable-dbpia-openapi-discovery",
        action="store_true",
        help=(
            "DBpia 공식 검색 Open API로 설정된 범위의 상세페이지를 실행 "
            "중에만 식별(원문 API가 아니며 검색 결과를 캐시하지 않음)"
        ),
    )
    parser.add_argument(
        "--dbpia-openapi-scope",
        choices=("kdfs", "selected"),
        default=os.getenv("DBPIA_OPENAPI_SCOPE", "kdfs"),
        help=(
            "DBpia 공식 검색 범위: kdfs는 디지털포렌식연구만, selected는 "
            "현재 선택된 논문의 정확한 간행물/발행기관별 검색(기본 kdfs)"
        ),
    )
    parser.add_argument(
        "--enable-dbpia-browser-download",
        action="store_true",
        help=(
            "전용 Playwright 세션으로 DBpia 웹에서 논문명을 검색하고 공식 "
            "논문/다운로드 UI를 사용(기본 비활성, worker 1 강제)"
        ),
    )
    parser.add_argument(
        "--dbpia-browser-redownload-campaign",
        metavar="NAME",
        help=(
            "선택한 논문을 기존 PDF 유무와 관계없이 DBpia 웹 제목 검색 → "
            "상세페이지 '다운받기'만으로 다시 수집하는 재개 가능 캠페인. "
            "같은 NAME에서 검증·교체가 끝난 논문은 재사용"
        ),
    )
    parser.add_argument(
        "--dbpia-browser-profile-dir",
        default=(
            os.getenv("DBPIA_BROWSER_PROFILE_DIR")
            or str(default_dbpia_profile_dir())
        ),
        help=(
            "scripts/dbpia_browser_auth.py로 만든 저장소 밖 전용 프로필 "
            "경로(일반 Chrome 프로필 사용 금지)"
        ),
    )
    parser.add_argument(
        "--dbpia-browser-headed",
        action="store_true",
        help="DBpia 다운로드 브라우저 창을 표시(기본은 headless)",
    )
    parser.add_argument(
        "--dbpia-browser-min-interval",
        type=float,
        default=float(os.getenv("DBPIA_BROWSER_MIN_INTERVAL", "10")),
        help="DBpia 브라우저 다운로드 동작 사이 최소 간격(초, 기본 10)",
    )
    parser.add_argument(
        "--dbpia-browser-max-attempts",
        type=int,
        default=int(os.getenv("DBPIA_BROWSER_MAX_ATTEMPTS", "25")),
        help=(
            "한 실행에서 실제 DBpia 브라우저 상세페이지를 여는 최대 건수"
            "(1~1000, 기본 25; 재실행 시 완료분 재사용)"
        ),
    )
    parser.add_argument(
        "--dbpia-browser-max-search-results",
        type=int,
        default=int(os.getenv("DBPIA_BROWSER_MAX_SEARCH_RESULTS", "20")),
        help="제목 검색에서 검사할 DBpia 결과 카드 상한(1~100, 기본 20)",
    )
    parser.add_argument(
        "--require-open-access-evidence",
        action="store_true",
        help="공개 응답만으로는 부족하고 OA/오픈 라이선스 메타데이터까지 요구",
    )
    parser.add_argument(
        "--enable-kci-ui-download",
        action="store_true",
        help=(
            "KCI 공개 다운로드 버튼 자동화. 현재 robots 정책상 기본 비활성이며 "
            "기관의 명시적 자동화 허가가 있을 때만 사용"
        ),
    )
    parser.add_argument(
        "--enable-kci-scienceon-download",
        action="store_true",
        help=(
            "KCI '원문 찾아보기'가 명시한 ScienceON PDF 자동화. 기본 "
            "비활성이며 KCI UI 자동화 허가가 있을 때만 사용"
        ),
    )
    parser.add_argument(
        "--enable-spo-download",
        action="store_true",
        help=(
            "대검찰청 공식 연구자료 PDF/ZIP 자동 다운로드. 기본 비활성이며 "
            "대검찰청의 명시적 서면 허가를 받은 경우에만 사용"
        ),
    )
    parser.add_argument(
        "--enable-kdfs-download",
        action="store_true",
        help=(
            "한국디지털포렌식학회 JAMS 공식 PDF 자동 다운로드. 기본 "
            "비활성이며 자동 수집·복제 허가를 받은 경우에만 사용"
        ),
    )
    parser.add_argument(
        "--enable-platform-download",
        action="store_true",
        help=(
            "DBpia·eArticle·KISS·교보의 자격증명 주입 없는 공식 UI 자동화. "
            "기본 비활성이며 자동 수집·복제 허가를 받은 경우에만 사용"
        ),
    )
    parser.add_argument(
        "--no-kci-journal-sitemap",
        dest="enable_kci_journal_sitemap",
        action="store_false",
        help="robots 허용 공식 journal.kci.go.kr sitemap/PDF 경로를 사용하지 않음",
    )
    parser.add_argument(
        "--no-kci-detail-resolution",
        dest="enable_kci_detail_resolution",
        action="store_false",
        help="DOI 없는 논문의 KCI 공식 articleDetail DOI 복구를 사용하지 않음",
    )
    parser.add_argument(
        "--no-crossref-title-resolution",
        dest="enable_crossref_title_resolution",
        action="store_false",
        help="DOI 없는 논문의 exact-title Crossref DOI 복구를 사용하지 않음",
    )
    parser.add_argument(
        "--no-unpaywall-title-resolution",
        dest="enable_unpaywall_title_resolution",
        action="store_false",
        help="DOI 없는 논문의 exact-title Unpaywall DOI 복구를 사용하지 않음",
    )
    parser.add_argument(
        "--no-openalex-title-resolution",
        dest="enable_openalex_title_resolution",
        action="store_false",
        help="DOI 없는 논문의 exact-title OpenAlex DOI 복구를 사용하지 않음",
    )
    semantic_scholar = parser.add_mutually_exclusive_group()
    semantic_scholar.add_argument(
        "--semantic-scholar",
        dest="enable_semantic_scholar",
        action="store_true",
        help="Semantic Scholar 공식 OA PDF batch 조회를 명시적으로 사용",
    )
    semantic_scholar.add_argument(
        "--no-semantic-scholar",
        dest="enable_semantic_scholar",
        action="store_false",
        help="Semantic Scholar 공식 OA PDF batch 조회를 사용하지 않음",
    )
    parser.add_argument(
        "--identifier-workers",
        type=int,
        default=int(os.getenv("PAPER_IDENTIFIER_WORKERS", "4")),
        help="DOI 식별자 복구 worker 수",
    )
    parser.add_argument(
        "--refresh-resolver-cache",
        action="store_true",
        help="KCI Journal sitemap 및 식별자 negative cache를 새로 조회",
    )
    parser.set_defaults(
        enable_kci_journal_sitemap=True,
        enable_kci_detail_resolution=True,
        enable_crossref_title_resolution=True,
        enable_unpaywall_title_resolution=True,
        enable_openalex_title_resolution=True,
        enable_semantic_scholar=False,
    )


def _add_analysis_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--taxonomy", default="filters/tech_taxonomy.json")
    parser.add_argument("--codex", default=os.getenv("CODEX_EXECUTABLE", "codex"))
    parser.add_argument("--model", default=os.getenv("CODEX_MODEL", DEFAULT_CODEX_MODEL))
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max", "ultra"],
        default=os.getenv("CODEX_REASONING_EFFORT", "high"),
    )
    parser.add_argument(
        "--codex-timeout",
        type=int,
        default=int(os.getenv("CODEX_ANALYSIS_TIMEOUT", "900")),
    )
    parser.add_argument(
        "--codex-retries",
        type=int,
        default=int(os.getenv("CODEX_ANALYSIS_RETRIES", "1")),
    )
    parser.add_argument(
        "--max-text-chars",
        type=int,
        default=int(os.getenv("PAPER_MAX_TEXT_CHARS", "600000")),
    )
    parser.add_argument(
        "--min-text-chars",
        type=int,
        default=int(os.getenv("PAPER_MIN_TEXT_CHARS", "200")),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KCI DB 논문 → 공개 PDF 다운로드 → Codex 정밀 분류 파이프라인"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="공개/OA PDF 다운로드")
    _add_selection_options(download)
    _add_download_options(download)

    analyze = subparsers.add_parser("analyze", help="기존 PDF를 Codex로 분석")
    _add_selection_options(analyze)
    _add_analysis_options(analyze)

    run = subparsers.add_parser("run", help="다운로드 후 성공 PDF를 바로 분석")
    _add_selection_options(run)
    _add_download_options(run)
    _add_analysis_options(run)

    status = subparsers.add_parser("status", help="PDF/manifest 산출물 상태 요약")
    status.add_argument(
        "--papers-dir",
        default=os.getenv("ANALYZER_PAPER_DIR", "Analyzer/Paper"),
    )
    status.add_argument(
        "--manifest-dir",
        default=os.getenv("ANALYZER_MANIFEST_DIR", "Analyzer/Manifest"),
    )
    return parser


def _parse_dbpia_node_args(values: list[str]) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for raw in values:
        source_id, separator, node_id = raw.strip().partition("=")
        if (
            separator != "="
            or not re.fullmatch(r"ART[0-9]{6,20}", source_id)
            or not re.fullmatch(r"NODE[0-9]{5,20}", node_id)
        ):
            raise ValueError(
                "--dbpia-node는 SOURCE_ID=NODE_ID 형식이어야 합니다 "
                "(예: ART003071074=NODE11750870)"
            )
        previous = mappings.get(source_id)
        if previous is not None and previous != node_id:
            raise ValueError(
                f"동일 source_id에 서로 다른 DBpia NODE가 지정되었습니다: {source_id}"
            )
        mappings[source_id] = node_id
    return mappings


def _dbpia_article_url(node_id: str) -> str:
    if not re.fullmatch(r"NODE[0-9]{5,20}", node_id):
        raise ValueError("invalid DBpia NODE identifier")
    return (
        "https://www.dbpia.co.kr/journal/articleDetail?"
        f"nodeId={node_id}"
    )


def _validate_args(args: argparse.Namespace) -> None:
    if hasattr(args, "dbpia_openapi_scope") and args.dbpia_openapi_scope not in {
        "kdfs",
        "selected",
    }:
        raise ValueError("--dbpia-openapi-scope 값은 kdfs 또는 selected여야 합니다")
    if hasattr(args, "source_id"):
        args.source_id = list(
            dict.fromkeys(value.strip() for value in args.source_id if value.strip())
        )
    if hasattr(args, "institution_id"):
        args.institution_id = list(
            dict.fromkeys(
                value.strip()
                for value in args.institution_id
                if value.strip()
            )
        )
    for name in (
        "unpaywall_email",
        "openalex_api_key",
        "elsevier_api_key",
        "semantic_scholar_api_key",
        "crossref_email",
        "dbpia_api_key",
        "dbpia_login_id",
        "dbpia_required_institution_name",
        "dbpia_browser_redownload_campaign",
    ):
        if hasattr(args, name):
            value = getattr(args, name)
            setattr(
                args,
                name,
                value.strip() if isinstance(value, str) and value.strip() else None,
            )
    if hasattr(args, "dbpia_node"):
        args.dbpia_node_map = _parse_dbpia_node_args(args.dbpia_node)
    if hasattr(args, "dbpia_browser_profile_dir"):
        profile_value = str(args.dbpia_browser_profile_dir or "").strip()
        args.dbpia_browser_profile_path = (
            Path(profile_value).expanduser()
            if profile_value
            else None
        )
    if getattr(args, "source_table", None) == "kci_corpus" and not args.allow_corpus:
        raise ValueError("kci_corpus를 처리하려면 --allow-corpus가 필요합니다")
    if getattr(args, "source_table", None) == "kci_corpus" and not (
        args.limit is not None or args.source_id
    ):
        raise ValueError("kci_corpus에는 반드시 --limit 또는 --source-id를 지정하세요")
    if (
        getattr(args, "source_table", None) == "kci_corpus"
        and args.limit is not None
        and args.limit > MAX_CORPUS_BATCH
    ):
        raise ValueError(
            f"kci_corpus의 한 실행 --limit은 {MAX_CORPUS_BATCH} 이하여야 합니다; "
            "--after-id로 나누어 실행하세요"
        )
    for name in (
        "download_workers",
        "identifier_workers",
        "http_retries",
        "max_pdf_mb",
        "semantic_scholar_batch_size",
        "codex_timeout",
        "codex_retries",
        "max_text_chars",
        "min_text_chars",
    ):
        if hasattr(args, name) and getattr(args, name) < (0 if "retries" in name else 1):
            raise ValueError(f"--{name.replace('_', '-')} 값이 올바르지 않습니다")
    for name in ("connect_timeout", "read_timeout"):
        if hasattr(args, name) and getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} 값은 양수여야 합니다")
    if hasattr(args, "per_host_rps") and args.per_host_rps <= 0:
        raise ValueError("--per-host-rps 값은 양수여야 합니다")
    if hasattr(args, "kci_detail_rps") and not (
        0 < args.kci_detail_rps <= 4
    ):
        raise ValueError("--kci-detail-rps 값은 0보다 크고 4 이하여야 합니다")
    if hasattr(args, "semantic_scholar_rps") and not (
        0 < args.semantic_scholar_rps < 1
    ):
        raise ValueError("--semantic-scholar-rps 값은 0보다 크고 1보다 작아야 합니다")
    if hasattr(args, "dbpia_search_rps") and not (
        0 < args.dbpia_search_rps <= 2
    ):
        raise ValueError("--dbpia-search-rps 값은 0보다 크고 2 이하여야 합니다")
    if hasattr(args, "dbpia_openapi_max_requests") and not (
        1 <= args.dbpia_openapi_max_requests <= 2_500
    ):
        raise ValueError("--dbpia-openapi-max-requests 값은 1~2500이어야 합니다")
    if (
        hasattr(args, "dbpia_browser_min_interval")
        and (
            not math.isfinite(args.dbpia_browser_min_interval)
            or args.dbpia_browser_min_interval < 5
        )
    ):
        raise ValueError("--dbpia-browser-min-interval 값은 5초 이상이어야 합니다")
    if hasattr(args, "dbpia_browser_max_attempts") and not (
        1 <= args.dbpia_browser_max_attempts <= 1_000
    ):
        raise ValueError("--dbpia-browser-max-attempts 값은 1~1000이어야 합니다")
    if hasattr(args, "dbpia_browser_max_search_results") and not (
        1 <= args.dbpia_browser_max_search_results <= 100
    ):
        raise ValueError(
            "--dbpia-browser-max-search-results 값은 1~100이어야 합니다"
        )
    if hasattr(args, "semantic_scholar_batch_size") and not (
        1 <= args.semantic_scholar_batch_size <= 500
    ):
        raise ValueError("--semantic-scholar-batch-size 값은 1~500이어야 합니다")
    if (
        getattr(args, "elsevier_api_key", None)
        and (
            not args.elsevier_api_key.isascii()
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in args.elsevier_api_key
            )
        )
    ):
        raise ValueError(
            "ELSEVIER_API_KEY에는 공백이나 제어문자가 없는 ASCII 키만 입력하세요"
        )
    if (
        getattr(args, "enable_semantic_scholar", False)
        and args.semantic_scholar_api_key
        and (
            not args.semantic_scholar_api_key.isascii()
            or any(character.isspace() for character in args.semantic_scholar_api_key)
        )
    ):
        raise ValueError(
            "SEMANTIC_SCHOLAR_API_KEY에는 발급된 ASCII 키만 입력하세요; "
            "발급 대기 중에는 값을 비우고 --semantic-scholar를 사용하지 마세요"
        )
    if (
        getattr(args, "dbpia_api_key", None)
        and (
            not args.dbpia_api_key.isascii()
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in args.dbpia_api_key
            )
        )
    ):
        raise ValueError(
            "DBPIA_API_KEY에는 공백이나 제어문자가 없는 ASCII 키만 입력하세요"
        )
    if getattr(args, "enable_dbpia_openapi_discovery", False):
        if not getattr(args, "dbpia_api_key", None):
            raise ValueError(
                "--enable-dbpia-openapi-discovery에는 DBPIA_API_KEY가 필요합니다"
            )
        if not (
            getattr(args, "enable_platform_download", False)
            or getattr(args, "enable_dbpia_browser_download", False)
        ):
            raise ValueError(
                "DBpia Open API NODE 매핑은 메모리에만 유지되므로 "
                "--enable-platform-download 또는 "
                "--enable-dbpia-browser-download도 함께 지정해야 합니다"
            )
    if (
        getattr(args, "dbpia_node_map", {})
        and not (
            getattr(args, "enable_platform_download", False)
            or getattr(args, "enable_dbpia_browser_download", False)
        )
    ):
        raise ValueError(
            "--dbpia-node를 사용하려면 --enable-platform-download 또는 "
            "--enable-dbpia-browser-download도 지정해야 합니다"
        )
    if getattr(args, "enable_dbpia_browser_download", False):
        if getattr(args, "download_workers", 0) != 1:
            raise ValueError(
                "--enable-dbpia-browser-download에는 --download-workers 1이 필요합니다"
            )
        if getattr(args, "require_open_access_evidence", False):
            raise ValueError(
                "구독 세션은 OA 증거가 아니므로 --require-open-access-evidence와 "
                "DBpia browser backend를 함께 사용할 수 없습니다"
            )
        login_id = getattr(args, "dbpia_login_id", None)
        login_password = getattr(args, "dbpia_login_password", None)
        if bool(login_id) != bool(login_password):
            raise ValueError(
                "DBPIA_LOGIN_ID와 DBPIA_LOGIN_PASSWORD는 둘 다 설정하거나 "
                "둘 다 비워야 합니다"
            )
        profile_path = getattr(args, "dbpia_browser_profile_path", None)
        if profile_path is None:
            raise ValueError("--dbpia-browser-profile-dir가 필요합니다")
        args.dbpia_browser_profile_path = validate_dbpia_profile_dir(
            profile_path,
            repository_root=REPOSITORY_ROOT,
        )
    redownload_campaign = getattr(
        args,
        "dbpia_browser_redownload_campaign",
        None,
    )
    if redownload_campaign:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", redownload_campaign):
            raise ValueError(
                "--dbpia-browser-redownload-campaign은 영문자/숫자로 시작하는 "
                "1~64자의 영문자·숫자·점·밑줄·하이픈만 허용합니다"
            )
        if not getattr(args, "enable_dbpia_browser_download", False):
            raise ValueError(
                "--dbpia-browser-redownload-campaign에는 "
                "--enable-dbpia-browser-download가 필요합니다"
            )
        if getattr(args, "force", False):
            raise ValueError(
                "재다운로드 캠페인은 자체 진행 상태를 사용하므로 --force와 "
                "함께 사용할 수 없습니다"
            )
        if getattr(args, "enable_dbpia_openapi_discovery", False):
            raise ValueError(
                "재다운로드 캠페인은 DBpia 웹 제목 검색만 사용하므로 "
                "--enable-dbpia-openapi-discovery와 함께 사용할 수 없습니다"
            )
    if (
        getattr(args, "dbpia_api_key", None)
        and not getattr(args, "enable_dbpia_openapi_discovery", False)
    ):
        print(
            "[dbpia-openapi] disabled; configured value ignored",
            file=sys.stderr,
            flush=True,
        )
    if (
        hasattr(args, "semantic_scholar_api_key")
        and args.semantic_scholar_api_key
        and not getattr(args, "enable_semantic_scholar", False)
    ):
        print(
            "[semantic-scholar] disabled; configured value ignored",
            file=sys.stderr,
            flush=True,
        )
    if getattr(args, "enable_kci_ui_download", False):
        print(
            "[주의] KCI UI 자동화는 해당 환경에서 명시적 허가를 받은 경우에만 사용하세요.",
            file=sys.stderr,
        )
    if getattr(args, "enable_kci_scienceon_download", False):
        print(
            "[주의] KCI/ScienceON UI 자동화는 해당 환경에서 명시적 "
            "허가를 받은 경우에만 사용하세요.",
            file=sys.stderr,
        )
    if getattr(args, "enable_spo_download", False):
        print(
            "[주의] SPO 자동 다운로드는 대검찰청의 명시적 서면 허가를 "
            "받은 환경에서만 사용하세요.",
            file=sys.stderr,
        )
    if getattr(args, "enable_kdfs_download", False):
        print(
            "[주의] KDFS/JAMS 자동 다운로드는 자동 수집·복제 허가를 "
            "받은 환경에서만 사용하세요.",
            file=sys.stderr,
        )
    if getattr(args, "enable_platform_download", False):
        print(
            "[주의] 학술 플랫폼 UI 자동 다운로드는 자동 수집·복제 허가를 "
            "받은 환경에서만 사용하세요.",
            file=sys.stderr,
        )
    if getattr(args, "enable_dbpia_browser_download", False):
        print(
            "[주의] DBpia 인증 브라우저는 전용 프로필의 현재 구독 권한과 "
            "자동 수집 허가 범위 안에서만 사용하세요. CAPTCHA는 자동으로 "
            "해결하지 않습니다.",
            file=sys.stderr,
        )


def _load_candidates(args: argparse.Namespace) -> list[PaperCandidate]:
    database_url = _database_url(args.database_url)
    engine = create_engine(database_url, future=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _set_sqlite_query_only(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA query_only=ON")
            finally:
                cursor.close()
    try:
        papers = list(
            iter_paper_candidates(
                engine,
                source_table=args.source_table,
                include_missing_doi=args.include_missing_doi,
                source_ids=args.source_id,
                after_id=args.after_id,
                limit=args.limit,
            )
        )
        if args.missing_doi_only:
            papers = [paper for paper in papers if not paper.doi]
        if args.institution_id:
            selected_institutions = set(args.institution_id)
            papers = [
                paper
                for paper in papers
                if paper.institution_id in selected_institutions
            ]
        return papers
    finally:
        engine.dispose()


def _download_runtime(
    args: argparse.Namespace,
    *,
    kci_journal_pdf_urls: Mapping[str, str] | None = None,
    platform_landing_urls: Mapping[str, str] | None = None,
    ephemeral_platform_landing_source_ids: frozenset[str] = frozenset(),
    preverified_candidates: (
        Mapping[str, tuple[ResolverCandidate, ...]] | None
    ) = None,
    semantic_scholar_candidates: Mapping[str, ResolverCandidate] | None = None,
    semantic_scholar_resolutions: Mapping[str, Mapping[str, Any]] | None = None,
    rate_limiter: PerHostRateLimiter | None = None,
) -> DownloadRuntime:
    enable_kdfs_download = bool(
        getattr(args, "enable_kdfs_download", False)
    )
    enable_platform_download = bool(
        getattr(args, "enable_platform_download", False)
    )
    enable_dbpia_browser_download = bool(
        getattr(args, "enable_dbpia_browser_download", False)
    )
    effective_rate_limiter = rate_limiter or PerHostRateLimiter(
        args.per_host_rps
    )
    kicj_resolver = _SynchronizedKicjResolver(
        KicjPublicPdfResolver(
            http=SafeHttpClient(
                connect_timeout=args.connect_timeout,
                read_timeout=args.read_timeout,
                retries=args.http_retries,
                allow_http=False,
                request_gate=effective_rate_limiter.wait,
            )
        )
    )
    police_resolver = _SynchronizedPoliceResolver(
        PoliceUniversityPublicPdfResolver(
            PoliceResolverConfig(
                requests_per_second=min(0.5, args.per_host_rps),
                connect_timeout=args.connect_timeout,
                read_timeout=args.read_timeout,
                max_pdf_bytes=args.max_pdf_mb * 1024 * 1024,
            )
        )
    )
    # One resolver is shared by every worker.  Its internal lock serializes
    # SPO requests and its per-instance List/View cache avoids repeated board
    # crawls across the 50 applicable rows.
    spo_resolver = SpoPublicPdfResolver(
        SpoResolverConfig(
            permission_granted=args.enable_spo_download,
            requests_per_second=min(0.5, args.per_host_rps),
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            max_attachment_bytes=args.max_pdf_mb * 1024 * 1024,
        )
    )
    kdfs_resolver = KdfsJamsAuthorizedPdfResolver(
        KdfsResolverConfig(
            permission_granted=enable_kdfs_download,
            requests_per_second=min(0.5, args.per_host_rps),
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            http_retries=args.http_retries,
            max_pdf_bytes=args.max_pdf_mb * 1024 * 1024,
        )
    )
    dbpia_browser_resolver = (
        AuthenticatedDbpiaBrowserResolver(
            DbpiaBrowserConfig(
                permission_granted=True,
                profile_dir=getattr(
                    args,
                    "dbpia_browser_profile_path",
                    None,
                ),
                repository_root=REPOSITORY_ROOT,
                headless=not bool(
                    getattr(args, "dbpia_browser_headed", False)
                ),
                navigation_timeout_seconds=args.read_timeout,
                download_timeout_seconds=args.read_timeout,
                min_interval_seconds=getattr(
                    args,
                    "dbpia_browser_min_interval",
                    10.0,
                ),
                max_attempts_per_run=getattr(
                    args,
                    "dbpia_browser_max_attempts",
                    25,
                ),
                max_search_results=getattr(
                    args,
                    "dbpia_browser_max_search_results",
                    20,
                ),
                login_id=getattr(args, "dbpia_login_id", None),
                login_password=getattr(
                    args,
                    "dbpia_login_password",
                    None,
                ),
                required_institution_name=getattr(
                    args,
                    "dbpia_required_institution_name",
                    None,
                ),
                max_pdf_bytes=args.max_pdf_mb * 1024 * 1024,
            )
        )
        if enable_dbpia_browser_download
        else None
    )
    return DownloadRuntime(
        config=DownloadConfig(
            max_pdf_bytes=args.max_pdf_mb * 1024 * 1024,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            allow_http=args.allow_http,
            request_gate=effective_rate_limiter.wait,
            unpaywall_email=args.unpaywall_email,
            openalex_api_key=args.openalex_api_key,
            elsevier_api_key=args.elsevier_api_key,
            crossref_email=args.crossref_email,
            require_open_access_evidence=args.require_open_access_evidence,
            enable_kci_ui_download=args.enable_kci_ui_download,
            enable_kci_scienceon_download=bool(
                getattr(args, "enable_kci_scienceon_download", False)
            ),
            enable_spo_download=args.enable_spo_download,
            enable_kdfs_download=enable_kdfs_download,
            enable_platform_download=enable_platform_download,
            enable_dbpia_browser_download=(
                enable_dbpia_browser_download
            ),
            dbpia_browser_redownload_campaign=getattr(
                args,
                "dbpia_browser_redownload_campaign",
                None,
            ),
            dbpia_browser_profile_dir=getattr(
                args,
                "dbpia_browser_profile_path",
                None,
            ),
            repository_root=REPOSITORY_ROOT,
            dbpia_browser_headless=not bool(
                getattr(args, "dbpia_browser_headed", False)
            ),
            dbpia_browser_min_interval_seconds=getattr(
                args,
                "dbpia_browser_min_interval",
                10.0,
            ),
            dbpia_browser_max_attempts_per_run=getattr(
                args,
                "dbpia_browser_max_attempts",
                25,
            ),
            dbpia_browser_max_search_results=getattr(
                args,
                "dbpia_browser_max_search_results",
                20,
            ),
            dbpia_login_id=getattr(args, "dbpia_login_id", None),
            dbpia_login_password=getattr(
                args,
                "dbpia_login_password",
                None,
            ),
            dbpia_required_institution_name=getattr(
                args,
                "dbpia_required_institution_name",
                None,
            ),
            kci_journal_pdf_urls=kci_journal_pdf_urls or {},
            platform_landing_urls=platform_landing_urls or {},
            ephemeral_platform_landing_source_ids=(
                ephemeral_platform_landing_source_ids
            ),
            preverified_candidates=preverified_candidates or {},
            semantic_scholar_candidates=semantic_scholar_candidates or {},
            semantic_scholar_resolutions=semantic_scholar_resolutions or {},
        ),
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        retries=args.http_retries,
        allow_http=args.allow_http,
        rate_limiter=effective_rate_limiter,
        kicj_resolver=kicj_resolver,
        police_resolver=police_resolver,
        spo_resolver=spo_resolver,
        kdfs_resolver=kdfs_resolver,
        dbpia_browser_resolver=dbpia_browser_resolver,
    )


def _resolve_kci_detail_identifiers(
    papers: list[PaperCandidate],
    *,
    papers_dir: Path,
    args: argparse.Namespace,
    skip_source_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[PaperCandidate], Counter[str]]:
    """Recover missing DOI values from each paper's exact official KCI record."""

    unresolved = [
        paper
        for paper in papers
        if not paper.doi and paper.source_id not in skip_source_ids
    ]
    counts: Counter[str] = Counter()
    if skip_source_ids:
        counts["kci-openapi-detail:existing-artifact-reused"] = sum(
            1
            for paper in papers
            if not paper.doi and paper.source_id in skip_source_ids
        )
    if not unresolved:
        return papers, counts
    if not getattr(args, "enable_kci_detail_resolution", True):
        counts["kci-openapi-detail:disabled"] = len(unresolved)
        return papers, counts
    api_key = getattr(args, "kci_open_api_key", None) or os.getenv(
        "KCI_OPEN_API_KEY"
    )
    if not api_key:
        counts["kci-openapi-detail:configuration_missing"] = len(unresolved)
        return papers, counts

    limiter = PerHostRateLimiter(args.kci_detail_rps)
    resolver = KciOpenApiIdentifierResolver(
        KciOpenApiIdentifierConfig(
            api_key=api_key,
            cache_dir=(
                _manifest_dir_for_args(args, papers_dir)
                / ".identifier-cache-kci"
            ),
            force_refresh=args.refresh_resolver_cache,
        ),
        http=KciOpenApiHttpClient(
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            request_gate=limiter.wait,
        ),
    )
    prepared_by_source: dict[str, PaperCandidate] = {}
    consecutive_retryable = 0
    for index, paper in enumerate(unresolved):
        try:
            resolution = resolver.resolve(paper)
            record = _identifier_resolution_record(resolution)
            prepared = replace(
                resolution.paper,
                identifier_resolution=record,
            )
        except Exception as exc:  # noqa: BLE001 - isolate each official lookup
            # Log only the exception type. Transport exceptions must never
            # render a query string that carries the KCI API key.
            record = {
                "resolver": "kci-openapi-detail",
                "status": "pipeline_error",
                "reason": type(exc).__name__,
            }
            prepared = replace(paper, identifier_resolution=record)
            print(
                "[identifier:error] "
                f"kci-openapi-detail {paper.source_id}: {type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

        status = str(record.get("status") or "unknown")
        counts[f"kci-openapi-detail:{status}"] += 1
        prepared_by_source[paper.source_id] = prepared
        if prepared.doi:
            print(
                "[identifier:resolved] "
                f"{paper.source_id} resolver=kci-openapi-detail "
                f"doi={prepared.doi}",
                flush=True,
            )
        processed = index + 1
        if processed % 25 == 0 or processed == len(unresolved):
            print(
                "[identifier:kci-detail-progress] "
                f"processed={processed}/{len(unresolved)}",
                flush=True,
            )

        fatal_configuration = status == "configuration_error" or (
            status == "http_error"
            and record.get("http_status") in {400, 401, 403}
        )
        if fatal_configuration:
            deferred = unresolved[index + 1 :]
            for deferred_paper in deferred:
                deferred_record = {
                    "resolver": "kci-openapi-detail",
                    "status": "configuration_error",
                    "reason": "provider_circuit_open_after_configuration_error",
                    "error_code": record.get("error_code"),
                    "http_status": record.get("http_status"),
                }
                prepared_by_source[deferred_paper.source_id] = replace(
                    deferred_paper,
                    identifier_resolution=deferred_record,
                )
                counts["kci-openapi-detail:configuration_error"] += 1
            print(
                "[identifier:circuit-open] "
                "kci-openapi-detail configuration_error "
                f"deferred={len(deferred)}",
                file=sys.stderr,
                flush=True,
            )
            break

        if _public_title_attempt_is_retryable(record):
            consecutive_retryable += 1
        else:
            consecutive_retryable = 0
        if consecutive_retryable < PROVIDER_CIRCUIT_BREAKER_THRESHOLD:
            continue

        deferred = unresolved[index + 1 :]
        for deferred_paper in deferred:
            deferred_record = {
                "resolver": "kci-openapi-detail",
                "status": "retryable_error",
                "reason": "provider_circuit_open",
                "error_code": "consecutive_retryable_failures",
            }
            prepared_by_source[deferred_paper.source_id] = replace(
                deferred_paper,
                identifier_resolution=deferred_record,
            )
            counts["kci-openapi-detail:retryable_error"] += 1
        print(
            "[identifier:circuit-open] "
            "kci-openapi-detail consecutive_retryable_failures="
            f"{consecutive_retryable} deferred={len(deferred)}",
            file=sys.stderr,
            flush=True,
        )
        break

    return (
        [
            prepared_by_source.get(paper.source_id, paper)
            for paper in papers
        ],
        counts,
    )


def _preserve_current_downloads(args: argparse.Namespace) -> bool:
    """Browser repair mode never reprocesses an already valid artifact."""

    return bool(
        not getattr(args, "force", False)
        or getattr(args, "enable_dbpia_browser_download", False)
    )


def _resolve_dbpia_landing_urls(
    papers: list[PaperCandidate],
    *,
    papers_dir: Path,
    args: argparse.Namespace,
    explicit_urls: Mapping[str, str],
) -> tuple[dict[str, str], Counter[str]]:
    """Find exact DBpia article pages through memory-only official catalogs.

    The default scope remains KDFS.  ``selected`` is an explicit opt-in that
    partitions unresolved rows by their exact journal/publisher strings and
    runs the same strict bibliographic matcher independently in each scope.
    Opaque KCI identifiers are never guessed into DBpia names.
    """

    urls = dict(explicit_urls)
    counts: Counter[str] = Counter()
    if explicit_urls:
        counts["explicit"] = len(explicit_urls)
    if not getattr(args, "enable_dbpia_openapi_discovery", False):
        return urls, counts

    scope_mode = getattr(args, "dbpia_openapi_scope", "kdfs")
    targets_by_scope: dict[tuple[str, str], list[PaperCandidate]] = {}
    for paper in papers:
        if paper.source_id in urls:
            continue
        scope = _dbpia_openapi_scope_for_paper(paper, mode=scope_mode)
        is_current = (
            _preserve_current_downloads(args)
            and download_artifact_is_current(
                paper,
                _artifact_paths_for_args(args, papers_dir, paper.source_id),
            )
        )
        if scope is None:
            if scope_mode == "selected" and not is_current:
                counts["scope_metadata_missing"] += 1
            continue
        if is_current:
            counts["existing_artifact_reused"] += 1
            continue
        targets_by_scope.setdefault(scope, []).append(paper)

    target_count = sum(len(value) for value in targets_by_scope.values())
    if not target_count:
        return urls, counts

    api_key = getattr(args, "dbpia_api_key", None)
    if not api_key:
        counts["configuration_missing"] = target_count
        print(
            "[dbpia-openapi:disabled] DBPIA_API_KEY가 없어 "
            f"targets={target_count}건을 건너뜁니다.",
            file=sys.stderr,
            flush=True,
        )
        return urls, counts

    limiter = PerHostRateLimiter(args.dbpia_search_rps)
    request_budget = _DbpiaOpenApiRunGate(
        max_requests=getattr(args, "dbpia_openapi_max_requests", 500),
        limiter=limiter,
        daily_state_path=(
            getattr(args, "dbpia_openapi_daily_state_path", None)
            or default_dbpia_openapi_daily_state_path()
        ),
    )
    ordered_scopes = sorted(targets_by_scope)
    total_pages = 0
    total_returned = 0
    completed_scopes = 0
    for scope_index, (journal, publisher) in enumerate(ordered_scopes):
        targets = targets_by_scope[(journal, publisher)]
        config = DbpiaOpenApiConfig(
            api_key=api_key,
            journal=journal,
            publisher=publisher,
        )
        resolver = DbpiaOpenApiResolver(
            config,
            http=DbpiaOpenApiHttpClient(
                connect_timeout=args.connect_timeout,
                read_timeout=args.read_timeout,
                retries=args.http_retries,
                request_gate=request_budget.wait,
                search_journal=journal,
                search_publisher=publisher,
            ),
        )
        try:
            batch = resolver.resolve(targets)
        except Exception as exc:  # noqa: BLE001 - keep the batch auditable
            counts["pipeline_error"] += len(targets)
            deferred = sum(
                len(targets_by_scope[scope])
                for scope in ordered_scopes[scope_index + 1 :]
            )
            counts["provider_circuit_deferred"] += deferred
            print(
                "[dbpia-openapi:error] "
                f"batch_failed={type(exc).__name__} targets={len(targets)} "
                f"deferred={deferred}",
                file=sys.stderr,
                flush=True,
            )
            break

        completed_scopes += 1
        total_pages += batch.pages_fetched
        total_returned += batch.returned_items
        provider_failure = bool(getattr(batch, "stop_provider", False))
        for paper in targets:
            resolution = batch.resolutions.get(paper.source_id)
            if resolution is None:
                counts["pipeline_error"] += 1
                provider_failure = True
                continue
            counts[resolution.status] += 1
            if resolution.status in {"configuration_error", "retryable_error"}:
                provider_failure = True
            if resolution.resolved and resolution.landing_url is not None:
                urls[paper.source_id] = resolution.landing_url

        if provider_failure:
            deferred = sum(
                len(targets_by_scope[scope])
                for scope in ordered_scopes[scope_index + 1 :]
            )
            counts["provider_circuit_deferred"] += deferred
            print(
                "[dbpia-openapi:circuit-open] "
                f"scope={scope_index + 1}/{len(ordered_scopes)} "
                f"deferred={deferred}",
                file=sys.stderr,
                flush=True,
            )
            break

    # Never print NODEs, link URLs, raw API fields, or credentials.  The exact
    # mapping remains in this process and is handed directly to the downloader.
    print(
        "[dbpia-openapi:catalog] "
        f"scope_mode={scope_mode} scopes={completed_scopes}/{len(ordered_scopes)} "
        f"requests={request_budget.requests_started}/{request_budget.max_requests} "
        f"pages={total_pages} returned={total_returned} "
        f"resolved={counts['resolved']}",
        flush=True,
    )
    return urls, counts


def _dbpia_openapi_scope_for_paper(
    paper: PaperCandidate,
    *,
    mode: str,
) -> tuple[str, str] | None:
    """Return one exact DBpia search scope without guessing KCI identifiers."""

    doi = (paper.doi or paper.doi_original or "").casefold()
    is_kdfs = (
        paper.institution_id == DBPIA_KDFS_PUBLISHER
        or paper.journal_id == DBPIA_KDFS_JOURNAL
        or paper.journal_id == "SER000003109"
        or doi.startswith("10.22798/kdfs.")
        or "/10.22798/kdfs." in doi
    )
    if is_kdfs:
        return DBPIA_KDFS_JOURNAL, DBPIA_KDFS_PUBLISHER
    if mode == "kdfs":
        return None
    if mode != "selected":
        raise ValueError("invalid DBpia Open API scope mode")

    journal = (
        paper.journal_id.strip()
        if isinstance(paper.journal_id, str)
        else ""
    )
    publisher = (
        paper.institution_id.strip()
        if isinstance(paper.institution_id, str)
        else ""
    )
    if (
        not journal
        or not publisher
        or len(journal) > 255
        or len(publisher) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in journal)
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in publisher
        )
        or re.fullmatch(r"SER[0-9]+", journal, flags=re.IGNORECASE)
        or re.fullmatch(r"INS[0-9]+", publisher, flags=re.IGNORECASE)
    ):
        return None
    return journal, publisher


def _dbpia_browser_resume_path(papers_dir: Path) -> Path:
    return papers_dir / DBPIA_BROWSER_RESUME_FILE


def _dbpia_browser_title_search_ready(paper: PaperCandidate) -> bool:
    """Mirror the browser's network-free title-search metadata precheck."""

    if not paper_title_queries(paper) or paper.publication_year is None:
        return False
    if paper.doi:
        return True
    return bool(
        paper.institution_id
        and paper.journal_id
        and re.fullmatch(
            r"(?:[0-9]{1,6}_[0-9]{1,6}|[0-9]{1,6})",
            (paper.issue_id or "").strip(),
        )
        and (paper.first_page or "").strip()
        and (paper.final_page or "").strip()
        and bool(paper.authors)
        and all(
            isinstance(author, str) and author.strip()
            for author in paper.authors
        )
    )


def _load_dbpia_browser_resume_cursor(papers_dir: Path) -> int | None:
    """Load the last actually attempted DB row from a URL-free state file."""

    state = load_json_object(_dbpia_browser_resume_path(papers_dir))
    if not isinstance(state, Mapping):
        return None
    database_id = state.get("last_database_id")
    if (
        state.get("schema_version") != DBPIA_BROWSER_RESUME_SCHEMA_VERSION
        or isinstance(database_id, bool)
        or not isinstance(database_id, int)
        or database_id < 1
    ):
        return None
    return database_id


def _manifest_has_actual_dbpia_browser_attempt(
    manifest: Mapping[str, Any],
) -> bool:
    """Distinguish real browser work from budget/circuit deferrals."""

    runtime = manifest.get("_runtime")
    if isinstance(runtime, Mapping) and runtime.get("action") in {
        "redownloaded",
        "redownload_failed",
        "redownload_deferred",
    }:
        return runtime.get("browser_attempted") is True
    if isinstance(runtime, Mapping) and runtime.get("action") in {
        "reused",
        "recovered",
        "redownload_reused",
    }:
        return False
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        return False
    actual_stages = {
        f"{DBPIA_BROWSER_RESOLVER_NAME}:dbpia-browser",
        f"{DBPIA_BROWSER_RESOLVER_NAME}:dbpia-browser-search",
        f"{DBPIA_BROWSER_RESOLVER_NAME}:dbpia-browser-landing",
        f"{DBPIA_BROWSER_RESOLVER_NAME}:dbpia-browser-download",
    }
    return any(
        isinstance(attempt, Mapping)
        and attempt.get("resolver") in actual_stages
        for attempt in attempts
    )


def _advance_dbpia_browser_resume_cursor(
    papers_dir: Path,
    paper: PaperCandidate,
    manifest: Mapping[str, Any],
) -> bool:
    """Persist progress only after this run performed real browser work."""

    if not _manifest_has_actual_dbpia_browser_attempt(manifest):
        return False
    write_json_atomic(
        _dbpia_browser_resume_path(papers_dir),
        {
            "schema_version": DBPIA_BROWSER_RESUME_SCHEMA_VERSION,
            "last_database_id": paper.database_id,
            "last_source_id": paper.source_id,
            "updated_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    return True


def _rotate_dbpia_browser_candidates(
    papers: list[PaperCandidate],
    *,
    papers_dir: Path,
    candidate_source_ids: set[str] | frozenset[str],
) -> list[PaperCandidate]:
    """Resume after the last actual browser attempt, wrapping at corpus end."""

    ranked = sorted(
        (
            paper
            for paper in papers
            if paper.source_id in candidate_source_ids
        ),
        key=lambda paper: (paper.database_id, paper.source_id),
    )
    if len(ranked) < 2:
        return papers
    cursor = _load_dbpia_browser_resume_cursor(papers_dir)
    if cursor is not None:
        split_at = next(
            (
                index
                for index, paper in enumerate(ranked)
                if paper.database_id > cursor
            ),
            0,
        )
        ranked = ranked[split_at:] + ranked[:split_at]
    iterator = iter(ranked)
    return [
        next(iterator)
        if paper.source_id in candidate_source_ids
        else paper
        for paper in papers
    ]


def _dbpia_redownload_campaign_completed(
    paper: PaperCandidate,
    paths: ArtifactPaths,
    campaign: str,
) -> bool:
    """Return true only for a still-valid PDF replaced by this campaign."""

    manifest = load_json_object(paths.download_manifest)
    audit = (
        manifest.get("dbpia_browser_redownload")
        if isinstance(manifest, Mapping)
        else None
    )
    return bool(
        isinstance(audit, Mapping)
        and audit.get("campaign") == campaign
        and audit.get("status") in {"replaced", "downloaded"}
        and download_artifact_is_current(paper, paths, manifest)
    )


def _adopt_current_artifact_identifier(
    paper: PaperCandidate,
    paths: ArtifactPaths,
) -> PaperCandidate:
    """Reuse a DOI only after the existing PDF/manifest pair fully validates."""

    if paper.doi:
        return paper
    manifest = load_json_object(paths.download_manifest)
    if not isinstance(manifest, Mapping) or not download_artifact_is_current(
        paper,
        paths,
        manifest,
    ):
        return paper
    manifest_paper = manifest.get("paper")
    if not isinstance(manifest_paper, Mapping):
        return paper
    raw_doi = manifest_paper.get("doi")
    if not isinstance(raw_doi, str):
        return paper
    try:
        doi = normalize_doi(raw_doi)
    except InvalidDoiError:
        return paper
    if doi is None:
        return paper
    doi_original = manifest_paper.get("doi_original")
    doi_source = manifest_paper.get("doi_source")
    resolution = manifest_paper.get("identifier_resolution")
    return replace(
        paper,
        doi_original=(
            doi_original if isinstance(doi_original, str) else raw_doi
        ),
        doi=doi,
        doi_source=doi_source if isinstance(doi_source, str) else None,
        identifier_resolution=(
            dict(resolution) if isinstance(resolution, Mapping) else None
        ),
    )


def _prepare_dbpia_redownload_campaign(
    papers: list[PaperCandidate],
    *,
    papers_dir: Path,
    args: argparse.Namespace,
) -> DownloadPreparation:
    """Prepare a resumable DBpia-web-only pass over the selected rows."""

    campaign = getattr(args, "dbpia_browser_redownload_campaign", None)
    if not campaign:
        raise ValueError("DBpia 재다운로드 campaign 이름이 필요합니다")
    adopted_papers = [
        _adopt_current_artifact_identifier(
            paper,
            _artifact_paths_for_args(args, papers_dir, paper.source_id),
        )
        for paper in papers
    ]
    adopted_count = sum(
        not original.doi and bool(adopted.doi)
        for original, adopted in zip(papers, adopted_papers, strict=True)
    )
    papers = adopted_papers
    metadata_targets = [
        paper
        for paper in papers
        if not _dbpia_browser_title_search_ready(paper)
        or (not paper.doi and not paper.authors)
    ]
    metadata_counts: Counter[str] = Counter()
    if metadata_targets:
        enriched_targets, metadata_counts = _resolve_kci_detail_identifiers(
            metadata_targets,
            papers_dir=papers_dir,
            args=args,
        )
        enriched_by_source = {
            paper.source_id: paper for paper in enriched_targets
        }
        papers = [
            enriched_by_source.get(paper.source_id, paper)
            for paper in papers
        ]
    if adopted_count or metadata_targets:
        print(
            "[dbpia-browser:metadata-preflight] "
            f"validated_artifact_doi_adopted={adopted_count} "
            f"kci_metadata_targets={len(metadata_targets)} "
            f"ready_after_enrichment="
            f"{sum(_dbpia_browser_title_search_ready(p) for p in papers)}"
            f"/{len(papers)}",
            flush=True,
        )
    selected_source_ids = {paper.source_id for paper in papers}
    explicit_dbpia_nodes: Mapping[str, str] = getattr(
        args,
        "dbpia_node_map",
        {},
    )
    unknown_dbpia_sources = set(explicit_dbpia_nodes) - selected_source_ids
    if unknown_dbpia_sources:
        raise ValueError(
            "--dbpia-node source_id가 현재 선택 대상에 없습니다: "
            + ", ".join(sorted(unknown_dbpia_sources))
        )
    platform_landing_urls = {
        source_id: _dbpia_article_url(node_id)
        for source_id, node_id in explicit_dbpia_nodes.items()
    }
    completed_source_ids = {
        paper.source_id
        for paper in papers
        if _dbpia_redownload_campaign_completed(
            paper,
            _artifact_paths_for_args(args, papers_dir, paper.source_id),
            campaign,
        )
    }
    pending_source_ids = selected_source_ids - completed_source_ids
    browser_mapped_source_ids = (
        set(platform_landing_urls) & pending_source_ids
    )
    browser_search_source_ids = {
        paper.source_id
        for paper in papers
        if paper.source_id in pending_source_ids
        and paper.source_id not in browser_mapped_source_ids
        and _dbpia_browser_title_search_ready(paper)
    }
    browser_actionable_source_ids = (
        browser_mapped_source_ids | browser_search_source_ids
    )
    browser_precheck_source_ids = (
        pending_source_ids - browser_actionable_source_ids
    )
    prepared_papers = _rotate_dbpia_browser_candidates(
        papers,
        papers_dir=_manifest_dir_for_args(args, papers_dir),
        candidate_source_ids=browser_actionable_source_ids,
    )
    resume_cursor = _load_dbpia_browser_resume_cursor(
        _manifest_dir_for_args(args, papers_dir)
    )
    max_attempts = getattr(args, "dbpia_browser_max_attempts", 25)
    print(
        "[dbpia-browser:redownload-preflight] "
        f"campaign={campaign} selected={len(prepared_papers)} "
        f"campaign_completed={len(completed_source_ids)} "
        f"pending={len(pending_source_ids)} "
        f"exact_dbpia_mapped={len(browser_mapped_source_ids)} "
        f"web_title_search={len(browser_search_source_ids)} "
        f"browser_precheck={len(browser_precheck_source_ids)} "
        "route=dbpia_web_direct_download_only "
        "resume_order=after_persisted_cursor "
        f"resume_cursor={resume_cursor or 'start'} "
        f"attempt_budget={min(max_attempts, len(browser_actionable_source_ids))}"
        f"/{max_attempts}",
        flush=True,
    )
    return DownloadPreparation(
        papers=prepared_papers,
        kci_journal_pdf_urls={},
        preverified_candidates={},
        semantic_scholar_candidates={},
        semantic_scholar_resolutions={},
        platform_landing_urls=platform_landing_urls,
        ephemeral_platform_landing_source_ids=frozenset(),
        identifier_counts=(
            Counter(
                {
                    "dbpia-redownload:completed": len(completed_source_ids),
                    "dbpia-redownload:pending": len(pending_source_ids),
                    "dbpia-redownload:validated-artifact-doi-adopted": (
                        adopted_count
                    ),
                }
            )
            + metadata_counts
        ),
        semantic_scholar_counts=Counter({"skipped": len(papers)}),
    )


def _prepare_downloads(
    papers: list[PaperCandidate],
    *,
    papers_dir: Path,
    args: argparse.Namespace,
    download_rate_limiter: PerHostRateLimiter,
) -> DownloadPreparation:
    scrubbed_legacy_manifests = sum(
        sanitize_dbpia_download_manifest(
            _artifact_paths_for_args(args, papers_dir, paper.source_id)
        )
        for paper in papers
    )
    if scrubbed_legacy_manifests:
        print(
            "[dbpia-manifest:sanitized] "
            f"count={scrubbed_legacy_manifests}",
            flush=True,
        )
    if getattr(args, "dbpia_browser_redownload_campaign", None):
        return _prepare_dbpia_redownload_campaign(
            papers,
            papers_dir=papers_dir,
            args=args,
        )
    semantic_scholar_limiter = (
        InterProcessRateLimiter(
            args.semantic_scholar_rps,
            _manifest_dir_for_args(args, papers_dir)
            / ".semantic-scholar-rate-limit",
        )
        if args.enable_semantic_scholar
        else None
    )
    kci_journal_pdf_urls: dict[str, str] = {}
    if args.enable_kci_journal_sitemap:
        sitemap_http = SafeHttpClient(
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            retries=args.http_retries,
            allow_http=False,
            request_gate=download_rate_limiter.wait,
        )
        try:
            index = KciJournalIndexLoader(
                sitemap_http,
                cache_path=(
                    _manifest_dir_for_args(args, papers_dir)
                    / ".kci-journal-index.json"
                ),
            ).load(force_refresh=args.refresh_resolver_cache)
        except (KciJournalError, OSError) as exc:
            print(
                f"[kci-journal:error] {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        else:
            selected_ids = {paper.source_id for paper in papers}
            kci_journal_pdf_urls = {
                source_id: article.pdf_url
                for source_id, article in index.articles.items()
                if source_id in selected_ids
            }
            print(
                "[kci-journal:index] "
                f"selected={len(kci_journal_pdf_urls)} "
                f"indexed={len(index.articles)} "
                f"sitemaps={index.loaded_sitemaps}/{index.advertised_sitemaps} "
                f"partial_failures={len(index.failures)} "
                f"cache={'hit' if index.from_cache else 'refreshed'}",
                flush=True,
            )

    missing_doi = [paper for paper in papers if not paper.doi]
    current_without_doi = (
        {
            paper.source_id
            for paper in missing_doi
            if download_artifact_is_current(
                paper,
                _artifact_paths_for_args(args, papers_dir, paper.source_id),
            )
        }
        if _preserve_current_downloads(args)
        else set()
    )
    prepared_papers, kci_detail_counts = _resolve_kci_detail_identifiers(
        papers,
        papers_dir=papers_dir,
        args=args,
        skip_source_ids=current_without_doi,
    )
    unresolved_for_lookup = [
        paper
        for paper in prepared_papers
        if not paper.doi
        if paper.source_id not in current_without_doi
        if not (
            isinstance(paper.identifier_resolution, Mapping)
            and _public_title_attempt_is_ambiguous(
                paper.identifier_resolution
            )
        )
    ]
    identifier_counts: Counter[str] = Counter(kci_detail_counts)
    if current_without_doi:
        identifier_counts["existing-artifact:reused"] = len(current_without_doi)
    resolved_by_source: dict[str, PaperCandidate] = {}
    if unresolved_for_lookup and args.enable_crossref_title_resolution:
        # Crossref's public pool is deliberately paced more conservatively
        # than publisher downloads when no polite-pool email is configured.
        identifier_rps = min(args.per_host_rps, 1.0)
        identifier_limiter = PerHostRateLimiter(identifier_rps)
        resolver_config = IdentifierResolverConfig(
            cache_dir=(
                _manifest_dir_for_args(args, papers_dir)
                / ".identifier-cache"
            ),
            contact_email=args.crossref_email,
            force_refresh=args.refresh_resolver_cache,
        )

        def resolve_one(paper: PaperCandidate) -> IdentifierResolution:
            http = SafeHttpClient(
                connect_timeout=args.connect_timeout,
                read_timeout=args.read_timeout,
                retries=args.http_retries,
                allow_http=False,
                request_gate=identifier_limiter.wait,
            )
            return CrossrefTitleResolver(
                resolver_config,
                http=http,
            ).resolve(paper)

        workers = max(1, args.identifier_workers)
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="paper-identifier",
        ) as executor:
            futures = {
                executor.submit(resolve_one, paper): paper
                for paper in unresolved_for_lookup
            }
            for future in as_completed(futures):
                paper = futures[future]
                try:
                    resolution = future.result()
                    status = resolution.status
                    resolution_record = _identifier_resolution_record(
                        resolution
                    )
                    if isinstance(paper.identifier_resolution, dict):
                        resolution_record["attempts"] = [
                            paper.identifier_resolution
                        ]
                    prepared = replace(
                        resolution.paper,
                        identifier_resolution=resolution_record,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate each lookup
                    status = "pipeline_error"
                    resolution_record = {
                        "resolver": "crossref-title",
                        "status": status,
                        "reason": type(exc).__name__,
                        "message": str(exc)[:500],
                    }
                    if isinstance(paper.identifier_resolution, dict):
                        resolution_record["attempts"] = [
                            paper.identifier_resolution
                        ]
                    prepared = replace(
                        paper,
                        identifier_resolution=resolution_record,
                    )
                    print(
                        f"[identifier:error] {paper.source_id}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                identifier_counts[f"crossref-title:{status}"] += 1
                resolved_by_source[paper.source_id] = prepared
                if prepared.doi:
                    print(
                        f"[identifier:{status}] {paper.source_id} doi={prepared.doi}",
                        flush=True,
                    )
    else:
        skipped_reason = (
            "disabled"
            if unresolved_for_lookup
            else "not_needed"
        )
        for paper in unresolved_for_lookup:
            resolution_record: dict[str, Any] = {
                "resolver": "crossref-title",
                "status": "skipped",
                "reason": skipped_reason,
            }
            if isinstance(paper.identifier_resolution, dict):
                resolution_record["attempts"] = [
                    paper.identifier_resolution
                ]
            resolved_by_source[paper.source_id] = replace(
                paper,
                identifier_resolution=resolution_record,
            )
            identifier_counts["crossref-title:skipped"] += 1

    prepared_papers = [
        resolved_by_source.get(paper.source_id, paper)
        for paper in prepared_papers
    ]
    (
        prepared_papers,
        preverified_candidates,
        public_title_counts,
    ) = _resolve_public_title_sources(
        prepared_papers,
        papers_dir=papers_dir,
        args=args,
        skip_source_ids=current_without_doi,
        semantic_scholar_request_gate=(
            semantic_scholar_limiter.wait
            if semantic_scholar_limiter is not None
            else None
        ),
        semantic_scholar_defer_gate=(
            semantic_scholar_limiter.defer
            if semantic_scholar_limiter is not None
            else None
        ),
    )
    identifier_counts.update(public_title_counts)
    if missing_doi:
        print(
            "[identifier:summary]",
            dict(sorted(identifier_counts.items())),
            flush=True,
        )

    semantic_scholar_candidates: dict[str, ResolverCandidate] = {}
    semantic_scholar_resolutions: dict[str, Mapping[str, Any]] = {}
    semantic_scholar_counts: Counter[str] = Counter()
    if args.enable_semantic_scholar:
        semantic_index = SemanticScholarBatchResolver(
            SemanticScholarConfig(
                cache_path=(
                    _manifest_dir_for_args(args, papers_dir)
                    / ".semantic-scholar-index.json"
                ),
                batch_size=args.semantic_scholar_batch_size,
                connect_timeout=args.connect_timeout,
                read_timeout=args.read_timeout,
                api_key=args.semantic_scholar_api_key,
                requests_per_second=args.semantic_scholar_rps,
                retries=args.http_retries,
                force_refresh=args.refresh_resolver_cache,
            ),
            request_gate=(
                semantic_scholar_limiter.wait
                if semantic_scholar_limiter is not None
                else None
            ),
            defer_gate=(
                semantic_scholar_limiter.defer
                if semantic_scholar_limiter is not None
                else None
            ),
        ).resolve(prepared_papers)
        for paper in prepared_papers:
            resolution = semantic_index[paper.source_id]
            semantic_scholar_counts[resolution.status] += 1
            semantic_scholar_resolutions[paper.source_id] = (
                _semantic_scholar_resolution_record(resolution)
            )
            candidate = resolution.candidate
            if candidate is not None:
                semantic_scholar_candidates[paper.source_id] = candidate
        print(
            "[semantic-scholar:summary] "
            f"{dict(sorted(semantic_scholar_counts.items()))} "
            f"candidates={len(semantic_scholar_candidates)}",
            flush=True,
        )
    else:
        semantic_scholar_counts["skipped"] = len(prepared_papers)

    selected_source_ids = {paper.source_id for paper in prepared_papers}
    explicit_dbpia_nodes: Mapping[str, str] = getattr(
        args,
        "dbpia_node_map",
        {},
    )
    unknown_dbpia_sources = set(explicit_dbpia_nodes) - selected_source_ids
    if unknown_dbpia_sources:
        raise ValueError(
            "--dbpia-node source_id가 현재 선택 대상에 없습니다: "
            + ", ".join(sorted(unknown_dbpia_sources))
        )
    platform_landing_urls = {
        source_id: _dbpia_article_url(node_id)
        for source_id, node_id in explicit_dbpia_nodes.items()
    }
    if platform_landing_urls:
        print(
            "[dbpia-node:explicit] "
            f"mapped={len(platform_landing_urls)}",
            flush=True,
        )
    platform_landing_urls, dbpia_search_counts = (
        _resolve_dbpia_landing_urls(
            prepared_papers,
            papers_dir=papers_dir,
            args=args,
            explicit_urls=platform_landing_urls,
        )
    )
    if dbpia_search_counts:
        print(
            "[dbpia-openapi:summary] "
            f"{dict(sorted(dbpia_search_counts.items()))} "
            f"landings={len(platform_landing_urls)}",
            flush=True,
        )
    ephemeral_platform_landing_source_ids = frozenset(
        set(platform_landing_urls) - set(explicit_dbpia_nodes)
    )
    if getattr(args, "enable_dbpia_browser_download", False):
        current_source_ids = {
            paper.source_id
            for paper in prepared_papers
            if download_artifact_is_current(
                paper,
                _artifact_paths_for_args(args, papers_dir, paper.source_id),
            )
        }
        unresolved_source_ids = selected_source_ids - current_source_ids
        browser_mapped_source_ids = (
            set(platform_landing_urls) & unresolved_source_ids
        )
        browser_search_source_ids = {
            paper.source_id
            for paper in prepared_papers
            if paper.source_id in unresolved_source_ids
            and paper.source_id not in browser_mapped_source_ids
            and _dbpia_browser_title_search_ready(paper)
        }
        browser_actionable_source_ids = (
            browser_mapped_source_ids | browser_search_source_ids
        )
        browser_precheck_source_ids = (
            unresolved_source_ids - browser_actionable_source_ids
        )
        prepared_papers = _rotate_dbpia_browser_candidates(
            prepared_papers,
            papers_dir=_manifest_dir_for_args(args, papers_dir),
            candidate_source_ids=browser_actionable_source_ids,
        )
        resume_cursor = _load_dbpia_browser_resume_cursor(
            _manifest_dir_for_args(args, papers_dir)
        )
        max_attempts = getattr(args, "dbpia_browser_max_attempts", 25)
        print(
            "[dbpia-browser:preflight] "
            f"selected={len(prepared_papers)} "
            f"current_reused={len(current_source_ids)} "
            f"unresolved={len(unresolved_source_ids)} "
            f"exact_dbpia_mapped={len(browser_mapped_source_ids)} "
            f"web_title_search={len(browser_search_source_ids)} "
            f"browser_precheck={len(browser_precheck_source_ids)} "
            "resume_order=after_persisted_cursor "
            f"resume_cursor={resume_cursor or 'start'} "
            f"attempt_budget={min(max_attempts, len(browser_actionable_source_ids))}"
            f"/{max_attempts}",
            flush=True,
        )

    return DownloadPreparation(
        papers=prepared_papers,
        kci_journal_pdf_urls=kci_journal_pdf_urls,
        preverified_candidates=preverified_candidates,
        semantic_scholar_candidates=semantic_scholar_candidates,
        semantic_scholar_resolutions=semantic_scholar_resolutions,
        platform_landing_urls=platform_landing_urls,
        ephemeral_platform_landing_source_ids=(
            ephemeral_platform_landing_source_ids
        ),
        identifier_counts=identifier_counts,
        semantic_scholar_counts=semantic_scholar_counts,
    )


PublicTitleResolution = (
    UnpaywallIdentifierResolution
    | OpenAlexIdentifierResolution
    | SemanticScholarTitleResolution
)


def _resolve_public_title_sources(
    papers: list[PaperCandidate],
    *,
    papers_dir: Path,
    args: argparse.Namespace,
    skip_source_ids: set[str] | frozenset[str] = frozenset(),
    semantic_scholar_request_gate: Callable[[str], None] | None = None,
    semantic_scholar_defer_gate: Callable[[float], None] | None = None,
) -> tuple[
    list[PaperCandidate],
    dict[str, tuple[ResolverCandidate, ...]],
    Counter[str],
]:
    """Resolve DOI-less papers through independent exact-title OA indexes.

    Both configured providers are queried for every paper that Crossref could
    not resolve. A DOI is adopted only when the configured providers do not
    disagree. Their direct-PDF metadata is retained as a preverified first
    download attempt.
    """

    unresolved = [
        paper
        for paper in papers
        if not paper.doi and paper.source_id not in skip_source_ids
    ]
    counts: Counter[str] = Counter()
    if skip_source_ids:
        counts["public-title:existing-artifact-reused"] = sum(
            1
            for paper in papers
            if not paper.doi and paper.source_id in skip_source_ids
        )
    if not unresolved:
        return papers, {}, counts

    identifier_rps = min(args.per_host_rps, 1.0)
    providers: dict[
        str,
        Callable[[PaperCandidate], PublicTitleResolution],
    ] = {}

    if args.enable_unpaywall_title_resolution and args.unpaywall_email:
        unpaywall_limiter = PerHostRateLimiter(identifier_rps)
        unpaywall_config = UnpaywallIdentifierConfig(
            email=args.unpaywall_email,
            cache_dir=(
                _manifest_dir_for_args(args, papers_dir)
                / ".identifier-cache-unpaywall"
            ),
            force_refresh=args.refresh_resolver_cache,
        )

        def resolve_unpaywall(
            paper: PaperCandidate,
        ) -> UnpaywallIdentifierResolution:
            http = SafeHttpClient(
                connect_timeout=args.connect_timeout,
                read_timeout=args.read_timeout,
                retries=args.http_retries,
                allow_http=False,
                request_gate=unpaywall_limiter.wait,
            )
            return UnpaywallTitleResolver(
                unpaywall_config,
                http=http,
            ).resolve(paper)

        providers["unpaywall-title"] = resolve_unpaywall
    else:
        reason = (
            "disabled"
            if not args.enable_unpaywall_title_resolution
            else "configuration_missing"
        )
        counts[f"unpaywall-title:{reason}"] = len(unresolved)

    if args.enable_openalex_title_resolution and args.openalex_api_key:
        openalex_limiter = PerHostRateLimiter(identifier_rps)
        openalex_config = OpenAlexIdentifierConfig(
            api_key=args.openalex_api_key,
            cache_dir=(
                _manifest_dir_for_args(args, papers_dir)
                / ".identifier-cache-openalex"
            ),
            force_refresh=args.refresh_resolver_cache,
        )

        def resolve_openalex(
            paper: PaperCandidate,
        ) -> OpenAlexIdentifierResolution:
            http = SafeHttpClient(
                connect_timeout=args.connect_timeout,
                read_timeout=args.read_timeout,
                retries=args.http_retries,
                allow_http=False,
                request_gate=openalex_limiter.wait,
            )
            return OpenAlexTitleResolver(
                openalex_config,
                http=http,
            ).resolve(paper)

        providers["openalex-title"] = resolve_openalex
    else:
        reason = (
            "disabled"
            if not args.enable_openalex_title_resolution
            else "configuration_missing"
        )
        counts[f"openalex-title:{reason}"] = len(unresolved)

    semantic_scholar_enabled = getattr(
        args,
        "enable_semantic_scholar",
        False,
    )
    semantic_scholar_api_key = getattr(
        args,
        "semantic_scholar_api_key",
        None,
    )
    if semantic_scholar_enabled and semantic_scholar_api_key:
        if semantic_scholar_request_gate is None:
            semantic_scholar_request_gate = PerHostRateLimiter(
                getattr(args, "semantic_scholar_rps", 0.045)
            ).wait
        semantic_scholar_config = SemanticScholarTitleConfig(
            api_key=semantic_scholar_api_key,
            cache_dir=(
                _manifest_dir_for_args(args, papers_dir)
                / ".identifier-cache-semantic-scholar"
            ),
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            retries=args.http_retries,
            force_refresh=args.refresh_resolver_cache,
        )

        def resolve_semantic_scholar(
            paper: PaperCandidate,
        ) -> SemanticScholarTitleResolution:
            resolver_kwargs: dict[str, Any] = {
                "request_gate": semantic_scholar_request_gate,
            }
            if semantic_scholar_defer_gate is not None:
                resolver_kwargs["defer_gate"] = semantic_scholar_defer_gate
            return SemanticScholarTitleResolver(
                semantic_scholar_config,
                **resolver_kwargs,
            ).resolve(paper)

        providers["semantic-scholar-title"] = resolve_semantic_scholar
    else:
        reason = (
            "disabled"
            if not semantic_scholar_enabled
            else "configuration_missing"
        )
        counts[f"semantic-scholar-title:{reason}"] = len(unresolved)

    if not providers:
        counts["public-title:unresolved"] = len(unresolved)
        return papers, {}, counts

    ProviderResult = PublicTitleResolution | dict[str, Any]

    def run_provider(
        name: str,
        resolver: Callable[[PaperCandidate], PublicTitleResolution],
    ) -> tuple[str, dict[str, ProviderResult]]:
        results: dict[str, ProviderResult] = {}

        def resolve_safely(paper: PaperCandidate) -> ProviderResult:
            try:
                return resolver(paper)
            except Exception as exc:  # noqa: BLE001 - isolate each lookup
                print(
                    "[identifier:error] "
                    f"{name} {paper.source_id}: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                return {
                    "resolver": name,
                    "status": "pipeline_error",
                    "reason": type(exc).__name__,
                }

        probe_count = min(
            PROVIDER_CIRCUIT_BREAKER_THRESHOLD,
            len(unresolved),
        )
        for paper in unresolved[:probe_count]:
            results[paper.source_id] = resolve_safely(paper)
        probe_records = [
            (
                result
                if isinstance(result, dict)
                else _public_title_resolution_record(result)
            )
            for result in results.values()
        ]
        if (
            probe_count == PROVIDER_CIRCUIT_BREAKER_THRESHOLD
            and all(
                _public_title_attempt_is_retryable(record)
                for record in probe_records
            )
        ):
            for paper in unresolved[probe_count:]:
                results[paper.source_id] = {
                    "resolver": name,
                    "status": "request_error",
                    "reason": "provider_circuit_open",
                    "error_code": "consecutive_retryable_failures",
                }
            print(
                "[identifier:circuit-open] "
                f"{name} consecutive_retryable_failures={probe_count} "
                f"deferred={len(unresolved) - probe_count}",
                file=sys.stderr,
                flush=True,
            )
            return name, results

        # Each provider already has one aggregate request gate.  Running its
        # queue sequentially is no slower at that gate and lets us stop before
        # turning one mid-run outage/quota response into dozens of needless
        # requests. Providers themselves still run concurrently with one
        # another in the outer executor.
        remaining = unresolved[probe_count:]
        consecutive_retryable = 0
        for index, paper in enumerate(remaining):
            result = resolve_safely(paper)
            results[paper.source_id] = result
            record = (
                result
                if isinstance(result, dict)
                else _public_title_resolution_record(result)
            )
            if _public_title_attempt_is_retryable(record):
                consecutive_retryable += 1
            else:
                consecutive_retryable = 0
            if consecutive_retryable < PROVIDER_CIRCUIT_BREAKER_THRESHOLD:
                continue

            deferred = remaining[index + 1 :]
            for deferred_paper in deferred:
                results[deferred_paper.source_id] = {
                    "resolver": name,
                    "status": "request_error",
                    "reason": "provider_circuit_open",
                    "error_code": "consecutive_retryable_failures",
                }
            print(
                "[identifier:circuit-open] "
                f"{name} consecutive_retryable_failures="
                f"{consecutive_retryable} deferred={len(deferred)}",
                file=sys.stderr,
                flush=True,
            )
            break
        return name, results

    results_by_provider: dict[str, dict[str, ProviderResult]] = {}
    with ThreadPoolExecutor(
        max_workers=len(providers),
        thread_name_prefix="public-title-provider",
    ) as executor:
        futures = [
            executor.submit(run_provider, name, resolver)
            for name, resolver in providers.items()
        ]
        for future in as_completed(futures):
            name, provider_results = future.result()
            results_by_provider[name] = provider_results

    prepared_by_source: dict[str, PaperCandidate] = {}
    preverified: dict[str, tuple[ResolverCandidate, ...]] = {}
    for paper in unresolved:
        prior_attempts = (
            [paper.identifier_resolution]
            if isinstance(paper.identifier_resolution, dict)
            else []
        )
        provider_records: list[dict[str, Any]] = []
        resolved: list[PublicTitleResolution] = []
        raw_results: list[PublicTitleResolution] = []
        for name in providers:
            result = results_by_provider[name][paper.source_id]
            if isinstance(result, dict):
                record = result
            else:
                raw_results.append(result)
                record = _public_title_resolution_record(result)
                if result.resolved and result.doi:
                    resolved.append(result)
            provider_records.append(record)
            counts[f"{name}:{record['status']}"] += 1

        distinct_dois = {
            result.doi.casefold()
            for result in resolved
            if result.doi
        }
        provider_ambiguous = any(
            record.get("status") == "ambiguous"
            for record in provider_records
        )
        prior_ambiguous = any(
            _public_title_attempt_is_ambiguous(record)
            for record in prior_attempts
        )
        if (
            len(distinct_dois) == 1
            and not provider_ambiguous
            and not prior_ambiguous
        ):
            selected = next(
                (
                    result
                    for result in resolved
                    if _public_title_candidates(result)
                ),
                resolved[0],
            )
            selected_record = _public_title_resolution_record(selected)
            selected_record["attempts"] = prior_attempts + provider_records
            prepared_by_source[paper.source_id] = replace(
                selected.paper,
                identifier_resolution=selected_record,
            )
            candidates = _deduplicate_candidates(
                candidate
                for result in raw_results
                if result.doi
                and selected.doi
                and result.doi.casefold() == selected.doi.casefold()
                for candidate in _public_title_candidates(result)
                if (
                    not getattr(args, "require_open_access_evidence", False)
                    or candidate.is_open_access is True
                )
            )
            if candidates:
                preverified[paper.source_id] = candidates
            counts["public-title:resolved"] += 1
            print(
                "[identifier:resolved] "
                f"{paper.source_id} resolver={selected.resolver} "
                f"doi={selected.doi}",
                flush=True,
            )
            continue

        if len(distinct_dois) > 1 or provider_ambiguous or prior_ambiguous:
            final_record = {
                "resolver": "public-title-consensus",
                "status": "ambiguous",
                "reason": (
                    "cross_source_doi_conflict"
                    if len(distinct_dois) > 1
                    else (
                        "prior_title_resolution_ambiguous"
                        if prior_ambiguous
                        else "provider_returned_multiple_distinct_dois"
                    )
                ),
                "attempts": prior_attempts + provider_records,
            }
            counts["public-title:ambiguous"] += 1
        elif any(
            _public_title_attempt_is_retryable(record)
            for record in (*prior_attempts, *provider_records)
        ):
            final_record = {
                "resolver": "public-title-consensus",
                "status": "retryable_error",
                "reason": "public_title_lookup_temporarily_unavailable",
                "attempts": prior_attempts + provider_records,
            }
            counts["public-title:retryable_error"] += 1
        else:
            final_record = {
                "resolver": "public-title-consensus",
                "status": "unresolved",
                "reason": "all_configured_sources_exhausted",
                "attempts": prior_attempts + provider_records,
            }
            counts["public-title:unresolved"] += 1
        prepared_by_source[paper.source_id] = replace(
            paper,
            identifier_resolution=final_record,
        )

    return (
        [
            prepared_by_source.get(paper.source_id, paper)
            for paper in papers
        ],
        preverified,
        counts,
    )


def _public_title_resolution_record(
    resolution: PublicTitleResolution,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "resolver": resolution.resolver,
        "status": resolution.status,
        "reason": resolution.reason,
        "doi_original": resolution.doi_original,
        "doi": resolution.doi,
        "matched_title": resolution.matched_title,
        "matched_year": resolution.matched_year,
        "matched_authors": list(
            getattr(resolution, "matched_authors", ()) or ()
        ),
        "returned_items": resolution.returned_items,
        "exact_title_matches": resolution.exact_title_matches,
        "title_year_matches": resolution.title_year_matches,
        "cache_hit": resolution.cache_hit,
        "http_status": resolution.http_status,
        "error_code": resolution.error_code,
    }
    if isinstance(resolution, UnpaywallIdentifierResolution):
        location = resolution.oa_location
        record["oa_location"] = (
            {
                "url_for_pdf": location.url_for_pdf,
                "url_for_landing_page": location.url_for_landing_page,
                "license": location.license,
                "version": location.version,
                "host_type": location.host_type,
            }
            if location is not None
            else None
        )
    elif isinstance(resolution, OpenAlexIdentifierResolution):
        record["oa_locations"] = [
            {
                "pdf_url": location.pdf_url,
                "landing_page_url": location.landing_page_url,
                "license": location.license,
                "version": location.version,
                "is_open_access": location.is_open_access,
                "is_best": location.is_best,
                "source_id": location.source_id,
                "source_name": location.source_name,
            }
            for location in resolution.locations
        ]
    else:
        record["queried_titles"] = list(resolution.queried_titles)
        record["query_count"] = resolution.query_count
        pdf = resolution.pdf
        record["open_access_pdf"] = (
            {
                "url": pdf.url,
                "title": pdf.title,
                "year": pdf.year,
                "status": pdf.status,
                "license": pdf.license,
                "disclaimer": pdf.disclaimer,
            }
            if pdf is not None
            else None
        )
    return record


def _public_title_candidates(
    resolution: PublicTitleResolution,
) -> tuple[ResolverCandidate, ...]:
    if isinstance(resolution, UnpaywallIdentifierResolution):
        return (resolution.candidate,) if resolution.candidate is not None else ()
    if isinstance(resolution, OpenAlexIdentifierResolution):
        return resolution.candidates
    return (resolution.candidate,) if resolution.candidate is not None else ()


def _public_title_attempt_is_retryable(record: Mapping[str, Any]) -> bool:
    status = record.get("status")
    if status in {
        "retryable_error",
        "request_error",
        "invalid_response",
        "pipeline_error",
    }:
        return True
    if status != "http_error":
        return False
    http_status = record.get("http_status")
    return (
        not isinstance(http_status, int)
        or http_status in {408, 425, 429}
        or http_status >= 500
    )


def _public_title_attempt_is_ambiguous(record: Mapping[str, Any]) -> bool:
    """Return whether an earlier title lookup made DOI identity uncertain."""

    return record.get("status") in {
        "ambiguous",
        "conflict",
        "identity_mismatch",
    } or record.get("error_code") == "ambiguous_response" or record.get(
        "reason"
    ) in {
        "article_id_mismatch",
        "title_mismatch",
        "publication_year_mismatch",
        "invalid_doi",
        "multiple_kci_article_records",
        "multiple_distinct_dois",
        "cross_source_doi_conflict",
        "provider_returned_multiple_distinct_dois",
        "prior_title_resolution_ambiguous",
    }


def _deduplicate_candidates(
    candidates: Any,
) -> tuple[ResolverCandidate, ...]:
    results: list[ResolverCandidate] = []
    seen_urls: set[str] = set()
    for candidate in candidates:
        if candidate.url in seen_urls:
            continue
        seen_urls.add(candidate.url)
        results.append(candidate)
    return tuple(results)


def _identifier_resolution_record(
    resolution: IdentifierResolution,
) -> dict[str, Any]:
    return {
        "resolver": resolution.resolver,
        "status": resolution.status,
        "reason": resolution.reason,
        "doi_original": resolution.doi_original,
        "doi": resolution.doi,
        "matched_title": resolution.matched_title,
        "matched_year": resolution.matched_year,
        "returned_items": resolution.returned_items,
        "exact_title_matches": resolution.exact_title_matches,
        "title_year_matches": resolution.title_year_matches,
        "cache_hit": resolution.cache_hit,
        "http_status": resolution.http_status,
        "error_code": resolution.error_code,
    }


def _semantic_scholar_resolution_record(
    resolution: SemanticScholarResolution,
) -> dict[str, Any]:
    pdf = resolution.pdf
    return {
        "resolver": "semantic-scholar",
        "status": resolution.status,
        "reason": resolution.reason,
        "doi": resolution.doi,
        "cache_hit": resolution.cache_hit,
        "http_status": resolution.http_status,
        "error_code": resolution.error_code,
        "open_access_pdf": (
            {
                "url": pdf.url,
                "title": pdf.title,
                "year": pdf.year,
                "status": pdf.status,
                "license": pdf.license,
                "disclaimer": pdf.disclaimer,
            }
            if pdf is not None
            else None
        ),
    }


def _download_one(
    paper: PaperCandidate,
    papers_dir: Path,
    manifest_dir: Path,
    runtime: DownloadRuntime,
    force: bool,
) -> tuple[str, dict[str, Any]]:
    http = SafeHttpClient(
        connect_timeout=runtime.connect_timeout,
        read_timeout=runtime.read_timeout,
        retries=runtime.retries,
        allow_http=runtime.allow_http,
        request_gate=runtime.rate_limiter.wait,
    )
    resolver_kwargs: dict[str, Any] = {
        "http": http,
        "kicj_resolver": runtime.kicj_resolver,
        "police_resolver": runtime.police_resolver,
        "spo_resolver": runtime.spo_resolver,
        "kdfs_resolver": runtime.kdfs_resolver,
    }
    if runtime.dbpia_browser_resolver is not None:
        resolver_kwargs["dbpia_browser_resolver"] = (
            runtime.dbpia_browser_resolver
        )
    downloader = PdfDownloader(runtime.config, **resolver_kwargs)
    paths = ArtifactPaths.for_source(
        papers_dir,
        paper.source_id,
        manifest_root=manifest_dir,
    )
    return paper.source_id, downloader.download(paper, paths, force=force)


def _download_result_is_durable(
    paper: PaperCandidate,
    papers_dir: Path,
    manifest_dir: Path,
    *,
    redownload_campaign: str | None,
) -> bool:
    """Re-read the committed artifact under its inter-process lock."""

    paths = ArtifactPaths.for_source(
        papers_dir,
        paper.source_id,
        manifest_root=manifest_dir,
    )
    with paths.lock.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if redownload_campaign:
            return _dbpia_redownload_campaign_completed(
                paper,
                paths,
                redownload_campaign,
            )
        manifest = load_json_object(paths.download_manifest)
        return download_artifact_is_current(paper, paths, manifest)


def download_many(
    papers: list[PaperCandidate],
    *,
    papers_dir: Path,
    args: argparse.Namespace,
    runtime: DownloadRuntime | None = None,
) -> DownloadBatchResult:
    counts: Counter[str] = Counter()
    successful_source_ids: set[str] = set()
    runtime = runtime or _download_runtime(args)
    manifest_dir = _manifest_dir_for_args(args, papers_dir)
    workers = max(1, args.download_workers)
    browser_enabled = bool(runtime.config.enable_dbpia_browser_download)

    def record_result(
        paper: PaperCandidate,
        result: tuple[str, dict[str, Any]] | None,
        error: BaseException | None,
    ) -> None:
        if error is None:
            assert result is not None
            source_id, manifest = result
            runtime_action = (manifest.get("_runtime") or {}).get("action")
            if runtime_action in {
                "reused",
                "redownloaded",
                "redownload_reused",
                "redownload_failed",
                "redownload_deferred",
            }:
                status = str(runtime_action)
            else:
                status = str(manifest.get("status") or "unknown")
            if status in {
                "downloaded",
                "reused",
                "redownloaded",
                "redownload_reused",
            } and not _download_result_is_durable(
                paper,
                papers_dir,
                manifest_dir,
                redownload_campaign=getattr(
                    args,
                    "dbpia_browser_redownload_campaign",
                    None,
                ),
            ):
                status = "artifact_validation_failed"
            if browser_enabled:
                try:
                    _advance_dbpia_browser_resume_cursor(
                        manifest_dir,
                        paper,
                        manifest,
                    )
                except OSError as cursor_exc:
                    print(
                        "[dbpia-browser:resume-warning] "
                        f"{source_id}: {type(cursor_exc).__name__}",
                        file=sys.stderr,
                        flush=True,
                    )
        else:
            source_id = paper.source_id
            status = "pipeline_error"
            try:
                _write_download_pipeline_error(
                    paper,
                    ArtifactPaths.for_source(
                        papers_dir,
                        paper.source_id,
                        manifest_root=manifest_dir,
                    ),
                    error,
                    redact_details=browser_enabled,
                    redownload_campaign=getattr(
                        args,
                        "dbpia_browser_redownload_campaign",
                        None,
                    ),
                )
            except Exception as manifest_exc:  # noqa: BLE001
                detail = (
                    type(manifest_exc).__name__
                    if browser_enabled
                    else str(manifest_exc)
                )
                print(
                    f"[download:manifest-error] {source_id}: {detail}",
                    file=sys.stderr,
                    flush=True,
                )
            detail = type(error).__name__ if browser_enabled else str(error)
            print(
                f"[download:error] {source_id}: {detail}",
                file=sys.stderr,
                flush=True,
            )
        counts[status] += 1
        if status in {
            "downloaded",
            "reused",
            "redownloaded",
            "redownload_reused",
        }:
            successful_source_ids.add(source_id)
        print(f"[download:{status}] {source_id}", flush=True)

    try:
        if workers == 1:
            # Playwright's sync API is thread-affine.  The authenticated
            # resolver must be opened, used, and closed on this same thread.
            for paper in papers:
                try:
                    result = _download_one(
                        paper,
                        papers_dir,
                        manifest_dir,
                        runtime,
                        args.force,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate each paper
                    record_result(paper, None, exc)
                else:
                    record_result(paper, result, None)
        else:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="paper-download",
            ) as executor:
                futures = {
                    executor.submit(
                        _download_one,
                        paper,
                        papers_dir,
                        manifest_dir,
                        runtime,
                        args.force,
                    ): paper
                    for paper in papers
                }
                for future in as_completed(futures):
                    paper = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        record_result(paper, None, exc)
                    else:
                        record_result(paper, result, None)
    finally:
        resolver = runtime.dbpia_browser_resolver
        if resolver is not None:
            close = getattr(resolver, "close", None)
            if callable(close):
                close()
    return DownloadBatchResult(
        counts=counts,
        successful_source_ids=frozenset(successful_source_ids),
    )


def _write_download_pipeline_error(
    paper: PaperCandidate,
    paths: ArtifactPaths,
    error: BaseException,
    *,
    redact_details: bool = False,
    redownload_campaign: str | None = None,
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state_root.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        previous = load_json_object(paths.download_manifest)
        recovered = PdfDownloader._recover_interrupted_commit(
            paper,
            paths,
            previous,
        )
        if recovered is not None:
            previous = recovered
        if (
            previous is not None
            and download_artifact_is_current(paper, paths, previous)
        ):
            audit = previous.get("dbpia_browser_redownload")
            if (
                redownload_campaign
                and isinstance(audit, Mapping)
                and audit.get("campaign") == redownload_campaign
                and audit.get("status") in {"replaced", "downloaded"}
            ):
                # A concurrent process, or interrupted-commit recovery above,
                # already completed this campaign.  Never downgrade it to a
                # pipeline failure.
                return
            if redownload_campaign:
                preserved = dict(previous)
                preserved["dbpia_browser_redownload"] = {
                    "campaign": redownload_campaign,
                    "status": "failed",
                    "attempted_at": now,
                    "error_code": "pipeline_error",
                }
                preserved["updated_at"] = now
                write_json_atomic(paths.download_manifest, preserved)
            # Whether or not this is a campaign, a valid artifact committed by
            # another process must not be replaced by an error manifest.
            return
        write_json_atomic(
            paths.download_manifest,
            {
                "schema_version": DOWNLOAD_SCHEMA_VERSION,
                "paper": {
                    "database_id": paper.database_id,
                    "source": paper.source,
                    "source_id": paper.source_id,
                    "title": paper.title,
                    "alternate_titles": list(paper.alternate_titles),
                    "doi_original": paper.doi_original,
                    "doi": paper.doi,
                    "doi_source": paper.doi_source,
                    "identifier_resolution": paper.identifier_resolution,
                },
                "status": "pipeline_error",
                "resolver": None,
                "pdf": None,
                "attempts": [],
                "started_at": now,
                "updated_at": now,
                "downloaded_at": None,
                "last_error": {
                    "code": "pipeline_error",
                    "type": type(error).__name__,
                    "message": (
                        "authenticated browser pipeline error"
                        if redact_details
                        else str(error)[:1000]
                    ),
                },
            },
        )


def _codex_preflight(executable: str) -> None:
    if not shutil.which(executable):
        raise ValueError(f"Codex CLI를 찾을 수 없습니다: {executable}")


def analyze_many(
    papers: list[PaperCandidate],
    *,
    papers_dir: Path,
    args: argparse.Namespace,
) -> Counter[str]:
    _codex_preflight(args.codex)
    taxonomy = load_taxonomy(_repository_path(args.taxonomy))
    analyzer = CodexPaperAnalyzer(
        CodexConfig(
            executable=args.codex,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.codex_timeout,
            retries=args.codex_retries,
            max_text_chars=args.max_text_chars,
            min_text_chars=args.min_text_chars,
        ),
        repository_root=REPOSITORY_ROOT,
    )
    counts: Counter[str] = Counter()
    for index, paper in enumerate(papers, start=1):
        paths = _artifact_paths_for_args(args, papers_dir, paper.source_id)
        if not download_artifact_is_current(paper, paths):
            counts["invalid_download_artifact"] += 1
            print(f"[analyze:invalid_download_artifact] {paper.source_id}", flush=True)
            continue
        try:
            result = analyzer.analyze(paper, paths, taxonomy, force=args.force)
            action = ((result.get("_runtime") or {}).get("action")) if isinstance(result, dict) else None
            status = "reused" if action == "reused" else "completed"
        except Exception as exc:  # noqa: BLE001 - preserve progress across papers
            try:
                analyzer.failure_record(paper, paths, taxonomy, exc)
            except Exception as record_exc:  # noqa: BLE001
                print(
                    f"[analyze:failure-record-error] {paper.source_id}: {record_exc}",
                    file=sys.stderr,
                    flush=True,
                )
            status = "failed"
            print(f"[analyze:error] {paper.source_id}: {exc}", file=sys.stderr, flush=True)
        counts[status] += 1
        print(f"[analyze:{status}] {index}/{len(papers)} {paper.source_id}", flush=True)
    return counts


def _print_dry_run(
    papers: list[PaperCandidate],
    papers_dir: Path,
    manifest_dir: Path,
    command: str,
) -> None:
    print(
        f"[dry-run] command={command} papers={len(papers)} "
        f"papers_dir={papers_dir} manifest_dir={manifest_dir}"
    )
    for paper in papers[:20]:
        paths = ArtifactPaths.for_source(
            papers_dir,
            paper.source_id,
            manifest_root=manifest_dir,
            allow_unindexed_pdf=True,
        )
        print(
            f"  id={paper.database_id} source_id={paper.source_id} "
            f"doi={paper.doi or '(없음)'} pdf_exists={paths.pdf.is_file()}"
        )
    if len(papers) > 20:
        print(f"  ... {len(papers) - 20}건 생략")


def show_status(papers_dir: Path, manifest_dir: Path | None = None) -> int:
    state_dir = manifest_dir or papers_dir
    if not papers_dir.is_dir():
        print(f"PDF 디렉터리가 존재하지 않습니다: {papers_dir}", file=sys.stderr)
        return 1
    if not state_dir.is_dir():
        print(f"manifest 디렉터리가 존재하지 않습니다: {state_dir}", file=sys.stderr)
        return 1
    download_counts: Counter[str] = Counter()
    analysis_counts: Counter[str] = Counter()
    redownload_counts: Counter[str] = Counter()
    for path in state_dir.glob("*.download.json"):
        value = load_json_object(path)
        download_counts[str((value or {}).get("status") or "invalid_manifest")] += 1
        audit = (value or {}).get("dbpia_browser_redownload")
        if isinstance(audit, Mapping):
            campaign = audit.get("campaign")
            status = audit.get("status")
            if isinstance(campaign, str) and isinstance(status, str):
                redownload_counts[f"{campaign}:{status}"] += 1
    analysis_errors = {
        path.name.removesuffix(".analysis-error.json")
        for path in state_dir.glob("*.analysis-error.json")
    }
    for path in state_dir.glob("*.json"):
        if (
            path.name.startswith(".")
            or path.name.endswith((".download.json", ".analysis-error.json"))
        ):
            continue
        if path.stem in analysis_errors:
            continue
        value = load_json_object(path)
        analysis_counts[str((value or {}).get("status") or "invalid_json")] += 1
    if analysis_errors:
        analysis_counts["failed"] += len(analysis_errors)
    print(f"PDF: {papers_dir}")
    print(f"Manifest: {state_dir}")
    print(
        f"coverage: manifests={sum(download_counts.values())} "
        f"pdf_files={sum(1 for _ in papers_dir.glob('*.pdf'))}"
    )
    print("download:", dict(sorted(download_counts.items())))
    if redownload_counts:
        print("dbpia_redownload:", dict(sorted(redownload_counts.items())))
    print("analysis:", dict(sorted(analysis_counts.items())))
    return 0


def _download_result_requires_failure(
    result: DownloadBatchResult,
    *,
    selected_source_ids: set[str] | frozenset[str],
    require_complete: bool,
) -> bool:
    """Apply the default best-effort or opt-in complete-download exit policy."""

    if result.counts.get("pipeline_error", 0):
        return True
    if not result.successful_source_ids:
        return True
    return require_complete and not selected_source_ids.issubset(
        result.successful_source_ids
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_args(args)
        papers_dir = _repository_path(args.papers_dir)
        manifest_dir = _repository_path(args.manifest_dir)
        if args.command == "status":
            return show_status(papers_dir, manifest_dir)
        papers = _load_candidates(args)
        if args.dry_run:
            _print_dry_run(papers, papers_dir, manifest_dir, args.command)
            if args.command in {"analyze", "run"}:
                taxonomy = load_taxonomy(_repository_path(args.taxonomy))
                print(
                    f"[dry-run] taxonomy_level3={len(taxonomy)} "
                    f"taxonomy_sha256={taxonomy.sha256} model={args.model}"
                )
            return 0
        papers_dir.mkdir(parents=True, exist_ok=True)
        manifest_dir.mkdir(parents=True, exist_ok=True)
        extend_paper_file_index_if_present(
            manifest_dir / PAPER_FILE_INDEX_NAME,
            (
                (paper.source_id, paper.title)
                for paper in papers
                if paper.title and paper.title.strip()
            ),
            papers_dir=papers_dir,
        )
        download_result: DownloadBatchResult | None = None
        analysis_counts: Counter[str] | None = None
        if args.command in {"download", "run"}:
            download_rate_limiter = PerHostRateLimiter(args.per_host_rps)
            preparation = _prepare_downloads(
                papers,
                papers_dir=papers_dir,
                args=args,
                download_rate_limiter=download_rate_limiter,
            )
            papers = preparation.papers
            runtime = _download_runtime(
                args,
                kci_journal_pdf_urls=preparation.kci_journal_pdf_urls,
                platform_landing_urls=preparation.platform_landing_urls,
                ephemeral_platform_landing_source_ids=(
                    preparation.ephemeral_platform_landing_source_ids
                ),
                preverified_candidates=preparation.preverified_candidates,
                semantic_scholar_candidates=(
                    preparation.semantic_scholar_candidates
                ),
                semantic_scholar_resolutions=(
                    preparation.semantic_scholar_resolutions
                ),
                rate_limiter=download_rate_limiter,
            )
            download_result = download_many(
                papers,
                papers_dir=papers_dir,
                args=args,
                runtime=runtime,
            )
            print("[download:summary]", dict(sorted(download_result.counts.items())))
        if args.command in {"analyze", "run"}:
            analysis_papers = papers
            if args.command == "run" and download_result is not None:
                analysis_papers = [
                    paper
                    for paper in papers
                    if paper.source_id in download_result.successful_source_ids
                ]
            analysis_counts = (
                analyze_many(analysis_papers, papers_dir=papers_dir, args=args)
                if analysis_papers
                else Counter()
            )
            print("[analyze:summary]", dict(sorted(analysis_counts.items())))
        if not papers:
            return 1
        if download_result is not None:
            if _download_result_requires_failure(
                download_result,
                selected_source_ids={
                    paper.source_id
                    for paper in papers
                },
                require_complete=args.require_complete,
            ):
                return 1
        if analysis_counts is not None:
            analyzed = analysis_counts.get("completed", 0) + analysis_counts.get("reused", 0)
            expected = (
                len(download_result.successful_source_ids)
                if args.command == "run" and download_result is not None
                else len(papers)
            )
            if analysis_counts.get("failed", 0) or analyzed != expected:
                return 1
        return 0
    except (ValueError, OSError, SQLAlchemyError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
