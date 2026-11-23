"""Ordered facade for collecting PDF artifacts for KCI papers.

The mature PDF implementation lives in :mod:`PaperPipeline`.  It knows how to
validate PDF identity, write crash-safe manifests, and resume the authenticated
DBpia browser.  Its historical command, however, is a *multi-provider* command:
one invocation may try DBpia before Unpaywall/OpenAlex.  Calling that command
once therefore cannot implement the application-level provider contract.

This module deliberately owns only orchestration.  A provider runner receives
the still-unresolved papers for exactly one provider.  After it returns, the
facade re-validates the on-disk PDF/manifest pairs with
``download_artifact_is_current`` before constructing the input to the next
provider.  Runner-reported success is never treated as collection success.

Provider-specific networking is injected through :class:`ProviderRunner`.
This keeps the ordering testable without network access and provides a safe
boundary for the existing resolvers or a dedicated stage-only command.  Do not
adapt ``scripts/process_papers.py download`` directly: that command intentionally
contains the older combined resolver order.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

from sqlalchemy import create_engine

from Core.deduplicator import deduplicate_pdf_candidates
from Core.pdf_router import (
    DEFAULT_PDF_ROUTES_PATH,
    ROUTED_PDF_PROVIDERS,
    load_pdf_routes,
    paper_is_eligible_for_provider,
)
from PaperPipeline.db_source import iter_paper_candidates
from PaperPipeline.dbpia_browser import (
    DBPIA_BROWSER_RESOLVER_NAME,
    default_dbpia_profile_dir,
)
from PaperPipeline.downloader import (
    DownloadConfig,
    PdfDownloader,
    download_artifact_is_current,
)
from PaperPipeline.http_client import (
    InterProcessRateLimiter,
    PerHostRateLimiter,
    SafeHttpClient,
)
from PaperPipeline.jams_browser import default_jams_profile_dir
from PaperPipeline.kci_journal import KciJournalIndexLoader
from PaperPipeline.kyobo_browser import default_kyobo_profile_dir
from PaperPipeline.records import PaperCandidate
from PaperPipeline.paper_files import (
    PAPER_FILE_INDEX_NAME,
    PaperFileIndexMissingSourceError,
    extend_paper_file_index_if_present,
    remove_paper_file_index_entries,
    load_paper_file_index,
)
from PaperPipeline.semantic_scholar import (
    SemanticScholarBatchResolver,
    SemanticScholarConfig,
    SemanticScholarResolution,
)
from PaperPipeline.storage import (
    ArtifactPaths,
    load_json_object,
    sha256_file,
    write_json_atomic,
)


DEFAULT_PAPERS_DIR = Path("Analyzer/Paper")
"""Default PDF root for the reorganized application."""

DEFAULT_MANIFEST_DIR = Path("Analyzer/Manifest")
"""Default manifest, lock, cache, and collector-state root."""

LEGACY_PAPERS_DIR = Path("Papers")
"""Explicit compatibility root for artifacts produced by the legacy CLI."""

# Keep this public string tuple aligned with Util.debugger without importing a
# utility layer into the collector.  The typed enum below is used internally.
PDF_PROVIDER_ORDER: tuple[str, ...] = (
    "kci",
    "unpaywall",
    "openalex",
    "semantic_scholar",
    "dbpia",
    "kyobo",
    "jams",
    "sciencedirect",
)

DBPIA_BROWSER_RESUME_FILE = ".dbpia-browser-resume.json"
DBPIA_BROWSER_RESUME_SCHEMA_VERSION = 1


def default_papers_dir() -> Path:
    """Return the configured artifact root without freezing import-time env."""

    configured = os.getenv("ANALYZER_PAPER_DIR")
    return Path(configured) if configured and configured.strip() else DEFAULT_PAPERS_DIR


def default_manifest_dir() -> Path:
    """Return the configured state root without freezing import-time env."""

    configured = os.getenv("ANALYZER_MANIFEST_DIR")
    return (
        Path(configured)
        if configured and configured.strip()
        else DEFAULT_MANIFEST_DIR
    )


PROVIDER_LABELS_KO = {
    "kci": "KCI 공개 원문",
    "unpaywall": "Unpaywall(공개 저장소)",
    "openalex": "OpenAlex(공개 저장소)",
    "semantic_scholar": "Semantic Scholar(공개 저장소)",
    "dbpia": "DBpia(기관 인증 브라우저)",
    "kyobo": "교보 스콜라(기관 인증 브라우저)",
    "jams": "KDFS JAMS(학회 논문 시스템)",
    "sciencedirect": "ScienceDirect API",
}
STAGE_STATUS_LABELS_KO = {
    "completed": "완료", "not_needed": "대상 없음", "disabled": "사용 안 함", "unavailable": "사용 불가",
    "failed": "실패", "skipped": "건너뜀",
}


def provider_label(provider: "PdfProvider | str") -> str:
    value = provider.value if isinstance(provider, Enum) else str(provider)
    return PROVIDER_LABELS_KO.get(value, value)


class PdfProvider(str, Enum):
    """The only provider stages in the KCI PDF application workflow."""

    KCI = "kci"
    UNPAYWALL = "unpaywall"
    OPENALEX = "openalex"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    DBPIA = "dbpia"
    KYOBO = "kyobo"
    JAMS = "jams"
    SCIENCEDIRECT = "sciencedirect"


PROVIDERS_IN_ORDER: tuple[PdfProvider, ...] = tuple(
    PdfProvider(name) for name in PDF_PROVIDER_ORDER
)


class StageStatus(str, Enum):
    """How an ordered provider stage ended."""

    COMPLETED = "completed"
    NOT_NEEDED = "not_needed"
    DISABLED = "disabled"
    DRY_RUN = "dry_run"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class KciPdfConfig:
    """Application configuration shared by the ordered provider runners.

    ``papers_dir`` stores PDF bytes while ``manifest_dir`` stores download
    manifests, locks, caches, and stage state. ``compatibility_papers_dirs``
    remain read-only unified-layout roots: a fully valid PDF/manifest pair
    there is considered already collected and is not copied or downloaded.

    Subscription/browser stages are disabled by default.  DBpia, Kyobo and
    JAMS require explicit collection permission and dedicated authenticated
    profiles.  ScienceDirect uses only Elsevier's official Article Retrieval
    API; it never automates the ScienceDirect web UI.
    """

    papers_dir: Path = field(default_factory=default_papers_dir)
    manifest_dir: Path | None = None
    compatibility_papers_dirs: tuple[Path, ...] = ()
    database_url: str | None = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "sqlite:///kci_pipeline.db",
        )
    )
    source_table: str = "kci_papers"
    enable_dbpia: bool = False
    enable_kyobo: bool = False
    enable_jams: bool = False
    enable_sciencedirect: bool = False
    dry_run: bool = False
    continue_on_provider_error: bool = True
    provider_options: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        papers_dir = Path(self.papers_dir)
        manifest_dir = (
            Path(self.manifest_dir)
            if self.manifest_dir is not None
            else (
                default_manifest_dir()
                if papers_dir == default_papers_dir()
                else papers_dir
            )
        )
        compatibility = tuple(Path(path) for path in self.compatibility_papers_dirs)
        unique_compatibility: list[Path] = []
        seen = {papers_dir.absolute()}
        for path in compatibility:
            identity = path.absolute()
            if identity in seen:
                continue
            seen.add(identity)
            unique_compatibility.append(path)
        if self.source_table not in {"kci_papers", "kci_corpus"}:
            raise ValueError("source_table must be 'kci_papers' or 'kci_corpus'")
        if self.database_url is not None and not self.database_url.strip():
            raise ValueError("database_url must be non-empty when provided")
        object.__setattr__(self, "papers_dir", papers_dir)
        object.__setattr__(self, "manifest_dir", manifest_dir)
        object.__setattr__(
            self,
            "compatibility_papers_dirs",
            tuple(unique_compatibility),
        )

    @classmethod
    def for_legacy_papers(cls, **kwargs: Any) -> "KciPdfConfig":
        """Create a configuration that keeps using the existing ``Papers/``."""

        kwargs.setdefault("manifest_dir", LEGACY_PAPERS_DIR)
        return cls(papers_dir=LEGACY_PAPERS_DIR, **kwargs)

    @property
    def lookup_dirs(self) -> tuple[Path, ...]:
        """Roots searched, in preference order, for a current artifact."""

        return (self.papers_dir, *self.compatibility_papers_dirs)

    @property
    def lookup_layouts(self) -> tuple[tuple[Path, Path], ...]:
        """PDF/manifest roots searched in preference order."""

        assert self.manifest_dir is not None
        return (
            (self.papers_dir, self.manifest_dir),
            *((path, path) for path in self.compatibility_papers_dirs),
        )

    def provider_enabled(self, provider: "PdfProvider") -> bool:
        """Return the explicit opt-in state for a permission-gated stage."""

        flags = {
            PdfProvider.DBPIA: self.enable_dbpia,
            PdfProvider.KYOBO: self.enable_kyobo,
            PdfProvider.JAMS: self.enable_jams,
            PdfProvider.SCIENCEDIRECT: self.enable_sciencedirect,
        }
        return flags.get(provider, True)


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """Stable context passed to one provider runner."""

    provider: PdfProvider
    papers_dir: Path
    database_url: str | None
    options: Mapping[str, Any]
    manifest_dir: Path | None = None
    progress: Callable[[str], Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def emit(self, message: str) -> None:
        """Forward provider-internal progress to the application logger."""

        if self.progress is not None:
            self.progress(message)

    @property
    def state_dir(self) -> Path:
        return self.manifest_dir or self.papers_dir


@dataclass(frozen=True, slots=True)
class ProviderRunReport:
    """Optional claims and paper enrichments returned by a provider runner.

    ``successful_source_ids`` is diagnostic only.  The orchestrator determines
    success exclusively by re-reading and validating the durable artifact.
    ``updated_papers`` lets a resolver carry a conservatively recovered DOI to
    later stages without mutating the database or the immutable input record.
    """

    provider: PdfProvider
    attempted_source_ids: frozenset[str] = frozenset()
    successful_source_ids: frozenset[str] = frozenset()
    updated_papers: Mapping[str, PaperCandidate] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    metadata: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


@runtime_checkable
class ProviderRunner(Protocol):
    """A stage-only adapter for one PDF provider."""

    provider: PdfProvider

    def run(
        self,
        papers: Sequence[PaperCandidate],
        context: ProviderContext,
    ) -> ProviderRunReport:
        """Attempt only ``context.provider`` for the supplied unresolved rows."""


ProviderCallback: TypeAlias = Callable[
    [Sequence[PaperCandidate], ProviderContext], ProviderRunReport | None
]


@dataclass(slots=True)
class ProviderFunctionRunner:
    """Small adapter for an in-process existing resolver/application service."""

    provider: PdfProvider
    callback: ProviderCallback

    def run(
        self,
        papers: Sequence[PaperCandidate],
        context: ProviderContext,
    ) -> ProviderRunReport:
        if context.provider is not self.provider:
            raise ValueError("provider runner/context mismatch")
        report = self.callback(papers, context)
        if report is None:
            return ProviderRunReport(
                provider=self.provider,
                attempted_source_ids=frozenset(
                    paper.source_id for paper in papers
                ),
            )
        return report


class ProviderExecutionError(RuntimeError):
    """A stage-only subprocess adapter exited unsuccessfully."""


@dataclass(frozen=True, slots=True)
class SourceIdCommandBuilder:
    """Build an argv-only command for a dedicated single-provider adapter.

    A provider flag is included by default so accidentally pointing this at the
    historical combined downloader fails argument parsing instead of silently
    running providers out of order.  No shell is involved and source IDs are
    appended as individual argv elements.
    """

    command_prefix: tuple[str, ...]
    provider_flag: str = "--provider"
    papers_dir_flag: str = "--papers-dir"
    database_url_flag: str | None = None
    source_id_flag: str = "--source-id"

    def __post_init__(self) -> None:
        if not self.command_prefix or not self.command_prefix[0].strip():
            raise ValueError("command_prefix must contain an executable")
        values = (
            *self.command_prefix,
            self.provider_flag,
            self.papers_dir_flag,
            self.source_id_flag,
        )
        optional_values = (
            (self.database_url_flag,) if self.database_url_flag is not None else ()
        )
        if any("\x00" in value for value in (*values, *optional_values)):
            raise ValueError("command arguments must not contain NUL bytes")

    def build(
        self,
        papers: Sequence[PaperCandidate],
        context: ProviderContext,
    ) -> tuple[str, ...]:
        command = [
            *self.command_prefix,
            self.provider_flag,
            context.provider.value,
            self.papers_dir_flag,
            str(context.papers_dir),
        ]
        if context.database_url and self.database_url_flag:
            command.extend((self.database_url_flag, context.database_url))
        for paper in papers:
            command.extend((self.source_id_flag, paper.source_id))
        return tuple(command)


@dataclass(slots=True)
class SubprocessProviderRunner:
    """Safe subprocess boundary for a dedicated stage-only provider adapter.

    stdout/stderr are inherited so the application's real-time provider logs
    remain visible.  API keys and passwords must be supplied through the
    environment, never ``command_prefix``.
    """

    provider: PdfProvider
    command_builder: SourceIdCommandBuilder
    batch_size: int = 250
    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def run(
        self,
        papers: Sequence[PaperCandidate],
        context: ProviderContext,
    ) -> ProviderRunReport:
        if context.provider is not self.provider:
            raise ValueError("provider runner/context mismatch")
        attempted: set[str] = set()
        return_codes: list[int] = []
        environment = os.environ.copy()
        environment.update(self.environment)
        environment["KCI_PDF_PROVIDER"] = self.provider.value
        if context.database_url:
            # Keep future MySQL credentials out of argv/process listings.
            environment.setdefault("DATABASE_URL", context.database_url)
        for start in range(0, len(papers), self.batch_size):
            batch = papers[start : start + self.batch_size]
            command = self.command_builder.build(batch, context)
            attempted.update(paper.source_id for paper in batch)
            completed = subprocess.run(
                command,
                cwd=self.cwd,
                env=environment,
                check=False,
                timeout=self.timeout_seconds,
            )
            return_codes.append(completed.returncode)
            if completed.returncode != 0:
                raise ProviderExecutionError(
                    f"{self.provider.value} adapter exited with "
                    f"status {completed.returncode}"
                )
        return ProviderRunReport(
            provider=self.provider,
            attempted_source_ids=frozenset(attempted),
            metadata={"return_codes": tuple(return_codes)},
        )


def _option_text(
    options: Mapping[str, Any],
    *names: str,
    environment: str | None = None,
) -> str | None:
    for name in names:
        value = options.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    value = os.getenv(environment) if environment else None
    return value.strip() if value and value.strip() else None


def _option_bool(
    options: Mapping[str, Any],
    name: str,
    default: bool = False,
) -> bool:
    value = options.get(name, default)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _option_float(
    options: Mapping[str, Any],
    name: str,
    *,
    environment: str,
    default: float,
) -> float:
    value = options.get(name, os.getenv(environment, str(default)))
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _option_int(
    options: Mapping[str, Any],
    name: str,
    *,
    environment: str,
    default: int,
) -> int:
    value = options.get(name, os.getenv(environment, str(default)))
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")
    return parsed


def _option_nonnegative_int(
    options: Mapping[str, Any],
    name: str,
    *,
    environment: str,
    default: int,
) -> int:
    value = options.get(name, os.getenv(environment, str(default)))
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _semantic_resolution_record(
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


def _dbpia_resume_path(state_dir: Path) -> Path:
    return state_dir / DBPIA_BROWSER_RESUME_FILE


def _load_dbpia_resume_cursor(state_dir: Path) -> int | None:
    state = load_json_object(_dbpia_resume_path(state_dir))
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


def _rotate_after_dbpia_cursor(
    papers: Sequence[PaperCandidate],
    state_dir: Path,
) -> tuple[PaperCandidate, ...]:
    ranked = sorted(papers, key=lambda paper: (paper.database_id, paper.source_id))
    cursor = _load_dbpia_resume_cursor(state_dir)
    if cursor is None or len(ranked) < 2:
        return tuple(ranked)
    split_at = next(
        (
            index
            for index, paper in enumerate(ranked)
            if paper.database_id > cursor
        ),
        0,
    )
    return tuple(ranked[split_at:] + ranked[:split_at])


def _manifest_has_dbpia_browser_attempt(manifest: Mapping[str, Any]) -> bool:
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        return False
    actual = {
        f"{DBPIA_BROWSER_RESOLVER_NAME}:dbpia-browser",
        f"{DBPIA_BROWSER_RESOLVER_NAME}:dbpia-browser-search",
        f"{DBPIA_BROWSER_RESOLVER_NAME}:dbpia-browser-landing",
        f"{DBPIA_BROWSER_RESOLVER_NAME}:dbpia-browser-download",
    }
    return any(
        isinstance(attempt, Mapping) and attempt.get("resolver") in actual
        for attempt in attempts
    )


def _advance_dbpia_cursor(
    state_dir: Path,
    paper: PaperCandidate,
    manifest: Mapping[str, Any],
) -> bool:
    if not _manifest_has_dbpia_browser_attempt(manifest):
        return False
    write_json_atomic(
        _dbpia_resume_path(state_dir),
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


@dataclass(slots=True)
class PaperPipelineProviderRunner:
    """Production stage adapter over the existing audited PDF engine."""

    provider: PdfProvider

    def run(
        self,
        papers: Sequence[PaperCandidate],
        context: ProviderContext,
    ) -> ProviderRunReport:
        if context.provider is not self.provider:
            raise ValueError("provider runner/context mismatch")
        options = context.options
        if self.provider.value in ROUTED_PDF_PROVIDERS:
            configured_routes = _option_text(
                options,
                "routes_path",
                environment="PDF_PROVIDER_ROUTES_PATH",
            )
            route_path = Path(configured_routes or DEFAULT_PDF_ROUTES_PATH)
            if not route_path.is_absolute():
                route_path = Path(__file__).resolve().parents[1] / route_path
            routes = load_pdf_routes(route_path)
            selected = tuple(
                paper
                for paper in papers
                if paper_is_eligible_for_provider(
                    paper,
                    self.provider.value,
                    routes,
                )
            )
            if not selected:
                context.emit(
                    f"{provider_label(self.provider)}: 이 제공처로 보내도록 지정된 학술지의 논문이 없습니다"
                )
                return ProviderRunReport(
                    provider=self.provider,
                    metadata={
                        "eligible_count": 0,
                        "route_skipped_count": len(papers),
                    },
                )
            context.emit(
                f"{provider_label(self.provider)}: 학술지 지정표에 해당하는 논문 {len(selected)}편만 시도 "
                f"(해당 없음 {len(papers) - len(selected)}편)"
            )
            papers = selected
        connect_timeout = _option_float(
            options,
            "connect_timeout",
            environment="PAPER_CONNECT_TIMEOUT",
            default=10.0,
        )
        read_timeout = _option_float(
            options,
            "read_timeout",
            environment="PAPER_READ_TIMEOUT",
            default=60.0,
        )
        requests_per_second = _option_float(
            options,
            "requests_per_second",
            environment="PAPER_PER_HOST_RPS",
            default=2.0,
        )
        retries = _option_nonnegative_int(
            options,
            "http_retries",
            environment="PAPER_HTTP_RETRIES",
            default=2,
        )
        max_pdf_mb = _option_int(
            options,
            "max_pdf_mb",
            environment="PAPER_MAX_PDF_MB",
            default=50,
        )
        rate_limiter = PerHostRateLimiter(requests_per_second)
        http = SafeHttpClient(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries=retries,
            allow_http=False,
            request_gate=rate_limiter.wait,
        )

        kci_urls: dict[str, str] = {}
        semantic_candidates = {}
        semantic_records: dict[str, Mapping[str, Any]] = {}
        if self.provider is PdfProvider.KCI:
            context.emit(
                f"{provider_label(self.provider)}: KCI 학술지 공개 원문 목록을 준비하는 중"
            )
            cache_path = Path(
                options.get(
                    "cache_path",
                    context.state_dir / ".kci-journal-index.json",
                )
            )
            index = KciJournalIndexLoader(
                http,
                cache_path=cache_path,
            ).load(force_refresh=_option_bool(options, "force_refresh"))
            selected = {paper.source_id for paper in papers}
            kci_urls = {
                source_id: article.pdf_url
                for source_id, article in index.articles.items()
                if source_id in selected
            }
            context.emit(
                f"{provider_label(self.provider)}: 공개 원문 목록 준비 완료 — 대상 논문 중 {len(kci_urls)}편이 목록에 있음"
            )
        elif self.provider is PdfProvider.SEMANTIC_SCHOLAR:
            context.emit(
                f"{provider_label(self.provider)}: 논문 {len(papers)}편의 공개 PDF 주소를 한꺼번에 조회하는 중"
            )
            semantic_rps = _option_float(
                options,
                "requests_per_second",
                environment="SEMANTIC_SCHOLAR_RPS",
                default=0.045,
            )
            semantic_gate = InterProcessRateLimiter(
                semantic_rps,
                context.state_dir / ".semantic-scholar-rate-limit",
            )
            semantic = SemanticScholarBatchResolver(
                SemanticScholarConfig(
                    cache_path=Path(
                        options.get(
                            "cache_path",
                            context.state_dir / ".semantic-scholar-cache.json",
                        )
                    ),
                    batch_size=_option_int(
                        options,
                        "batch_size",
                        environment="SEMANTIC_SCHOLAR_BATCH_SIZE",
                        default=100,
                    ),
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    requests_per_second=semantic_rps,
                    retries=retries,
                    force_refresh=_option_bool(options, "force_refresh"),
                    api_key=_option_text(
                        options,
                        "api_key",
                        environment="SEMANTIC_SCHOLAR_API_KEY",
                    ),
                ),
                request_gate=semantic_gate.wait,
                defer_gate=semantic_gate.defer,
            ).resolve(papers)
            semantic_candidates = {
                source_id: resolution.candidate
                for source_id, resolution in semantic.items()
                if resolution.candidate is not None
            }
            semantic_records = {
                source_id: _semantic_resolution_record(resolution)
                for source_id, resolution in semantic.items()
            }
            context.emit(
                f"{provider_label(self.provider)}: 조회 완료 — 공개 PDF 후보 {len(semantic_candidates)}편"
            )

        jams_interactive_login = bool(
            self.provider is PdfProvider.JAMS
            and _option_bool(options, "interactive_login")
        )
        config = DownloadConfig(
            max_pdf_bytes=max_pdf_mb * 1024 * 1024,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            request_gate=rate_limiter.wait,
            unpaywall_email=_option_text(
                options,
                "email",
                "unpaywall_email",
                environment="UNPAYWALL_EMAIL",
            ),
            openalex_api_key=_option_text(
                options,
                "api_key",
                "openalex_api_key",
                environment="OPENALEX_API_KEY",
            ),
            elsevier_api_key=_option_text(
                options,
                "api_key",
                "elsevier_api_key",
                environment="ELSEVIER_API_KEY",
            ),
            elsevier_institution_token=_option_text(
                options,
                "institution_token",
                "elsevier_institution_token",
                environment="ELSEVIER_INSTITUTION_TOKEN",
            ),
            require_open_access_evidence=_option_bool(
                options,
                "require_open_access_evidence",
            ),
            enable_kci_ui_download=_option_bool(
                options,
                "enable_ui_download",
            ),
            enable_kci_scienceon_download=_option_bool(
                options,
                "enable_scienceon_download",
            ),
            # The Supreme Prosecutors' Office publishes its two journals on a public
            # board; the operator enables this (SPO_PUBLIC_DOWNLOAD=1 or the provider
            # option) and the resolver still verifies every PDF against the paper.
            enable_spo_download=(
                self.provider is PdfProvider.KCI
                and (
                    _option_bool(options, "enable_spo_download")
                    or os.getenv("SPO_PUBLIC_DOWNLOAD", "").strip().casefold() in {"1", "true", "yes", "on"}
                )
            ),
            enable_kdfs_download=(self.provider is PdfProvider.JAMS),
            enable_dbpia_browser_download=(
                self.provider is PdfProvider.DBPIA
            ),
            enable_kyobo_browser_download=(
                self.provider is PdfProvider.KYOBO
            ),
            enable_jams_browser_download=(
                self.provider is PdfProvider.JAMS
            ),
            dbpia_browser_profile_dir=(
                Path(
                    _option_text(
                        options,
                        "profile_dir",
                        environment="DBPIA_BROWSER_PROFILE_DIR",
                    )
                    or default_dbpia_profile_dir()
                )
                if self.provider is PdfProvider.DBPIA
                else None
            ),
            repository_root=Path(__file__).resolve().parents[1],
            dbpia_browser_headless=not _option_bool(
                options,
                "headed",
            ),
            dbpia_browser_min_interval_seconds=_option_float(
                options,
                "min_interval_seconds",
                environment="DBPIA_BROWSER_MIN_INTERVAL",
                default=10.0,
            ),
            dbpia_browser_max_attempts_per_run=_option_int(
                options,
                "max_attempts_per_run",
                environment="DBPIA_BROWSER_MAX_ATTEMPTS",
                default=25,
            ),
            dbpia_browser_max_search_results=_option_int(
                options,
                "max_search_results",
                environment="DBPIA_BROWSER_MAX_SEARCH_RESULTS",
                default=20,
            ),
            dbpia_login_id=_option_text(
                options,
                "login_id",
                environment="DBPIA_LOGIN_ID",
            ),
            dbpia_login_password=_option_text(
                options,
                "login_password",
                environment="DBPIA_LOGIN_PASSWORD",
            ),
            dbpia_required_institution_name=_option_text(
                options,
                "required_institution_name",
                environment="DBPIA_REQUIRED_INSTITUTION_NAME",
            ),
            kyobo_browser_profile_dir=(
                Path(
                    _option_text(
                        options,
                        "profile_dir",
                        environment="KYOBO_BROWSER_PROFILE_DIR",
                    )
                    or default_kyobo_profile_dir()
                )
                if self.provider is PdfProvider.KYOBO
                else None
            ),
            kyobo_browser_headless=not _option_bool(
                options,
                "headed",
            ),
            kyobo_browser_min_interval_seconds=_option_float(
                options,
                "min_interval_seconds",
                environment="KYOBO_BROWSER_MIN_INTERVAL",
                default=10.0,
            ),
            kyobo_browser_max_attempts_per_run=_option_int(
                options,
                "max_attempts_per_run",
                environment="KYOBO_BROWSER_MAX_ATTEMPTS",
                default=25,
            ),
            kyobo_browser_max_search_results=_option_int(
                options,
                "max_search_results",
                environment="KYOBO_BROWSER_MAX_SEARCH_RESULTS",
                default=20,
            ),
            jams_browser_profile_dir=(
                Path(
                    _option_text(
                        options,
                        "profile_dir",
                        environment="JAMS_BROWSER_PROFILE_DIR",
                    )
                    or default_jams_profile_dir()
                )
                if self.provider is PdfProvider.JAMS
                else None
            ),
            jams_browser_headless=not (
                _option_bool(options, "headed")
                or jams_interactive_login
            ),
            jams_browser_interactive_login=jams_interactive_login,
            jams_browser_min_interval_seconds=_option_float(
                options,
                "min_interval_seconds",
                environment="JAMS_BROWSER_MIN_INTERVAL",
                default=10.0,
            ),
            jams_browser_max_attempts_per_run=_option_int(
                options,
                "max_attempts_per_run",
                environment="JAMS_BROWSER_MAX_ATTEMPTS",
                default=25,
            ),
            kci_journal_pdf_urls=kci_urls,
            semantic_scholar_candidates=semantic_candidates,
            semantic_scholar_resolutions=semantic_records,
            provider_scope=self.provider.value,
        )
        downloader = PdfDownloader(config, http=http)
        ordered_papers = (
            _rotate_after_dbpia_cursor(papers, context.state_dir)
            if self.provider is PdfProvider.DBPIA
            else tuple(papers)
        )
        attempted: set[str] = set()
        successful: set[str] = set()
        errors: dict[str, int] = {}
        try:
            total = len(ordered_papers)
            for index, paper in enumerate(ordered_papers, start=1):
                attempted.add(paper.source_id)
                try:
                    paths = ArtifactPaths.for_source(
                        context.papers_dir,
                        paper.source_id,
                        manifest_root=context.state_dir,
                    )
                    if download_artifact_is_intact(paper, paths):
                        # A reviewed PDF whose manifest only fails the strict
                        # identity policy must not be replaced or re-marked.
                        successful.add(paper.source_id)
                        continue
                    manifest = downloader.download(
                        paper,
                        paths,
                        force=_option_bool(options, "force"),
                    )
                    if self.provider is PdfProvider.DBPIA:
                        try:
                            _advance_dbpia_cursor(
                                context.state_dir,
                                paper,
                                manifest,
                            )
                        except OSError:
                            errors["resume_state_error"] = (
                                errors.get("resume_state_error", 0) + 1
                            )
                    if download_artifact_is_current(paper, paths, manifest):
                        successful.add(paper.source_id)
                except Exception as exc:
                    name = type(exc).__name__
                    errors[name] = errors.get(name, 0) + 1
                    if _option_bool(options, "raise_on_paper_error"):
                        raise
                finally:
                    context.emit(
                        f"{provider_label(self.provider)}: {index}/{total}편 시도 ({index / total:.0%}) · "
                        f"확보 {len(successful)}편 · 오류 {sum(errors.values())}건"
                    )
        finally:
            closed: set[int] = set()
            for attribute in (
                "dbpia_browser_resolver",
                "kyobo_browser_resolver",
                "jams_browser_resolver",
            ):
                resolver = getattr(downloader, attribute, None)
                if resolver is None or id(resolver) in closed:
                    continue
                closed.add(id(resolver))
                close = getattr(resolver, "close", None)
                if callable(close):
                    close()
        return ProviderRunReport(
            provider=self.provider,
            attempted_source_ids=frozenset(attempted),
            successful_source_ids=frozenset(successful),
            metadata={"errors_by_type": errors},
        )


def build_default_provider_runners(
    config: KciPdfConfig,
) -> dict[PdfProvider, ProviderRunner]:
    """Build operational adapters whose required credentials are available."""

    runners: dict[PdfProvider, ProviderRunner] = {
        PdfProvider.KCI: PaperPipelineProviderRunner(PdfProvider.KCI),
    }
    unpaywall_options = config.provider_options.get("unpaywall", {})
    if _option_text(
        unpaywall_options,
        "email",
        "unpaywall_email",
        environment="UNPAYWALL_EMAIL",
    ):
        runners[PdfProvider.UNPAYWALL] = PaperPipelineProviderRunner(
            PdfProvider.UNPAYWALL
        )
    openalex_options = config.provider_options.get("openalex", {})
    if _option_text(
        openalex_options,
        "api_key",
        "openalex_api_key",
        environment="OPENALEX_API_KEY",
    ):
        runners[PdfProvider.OPENALEX] = PaperPipelineProviderRunner(
            PdfProvider.OPENALEX
        )
    semantic_options = config.provider_options.get("semantic_scholar", {})
    if not (
        "enabled" in semantic_options
        and not _option_bool(semantic_options, "enabled", True)
    ):
        # The official batch endpoint supports anonymous requests; an API key
        # is used automatically when configured.
        runners[PdfProvider.SEMANTIC_SCHOLAR] = PaperPipelineProviderRunner(
            PdfProvider.SEMANTIC_SCHOLAR
        )
    if config.enable_dbpia:
        runners[PdfProvider.DBPIA] = PaperPipelineProviderRunner(
            PdfProvider.DBPIA
        )
    if config.enable_kyobo:
        runners[PdfProvider.KYOBO] = PaperPipelineProviderRunner(
            PdfProvider.KYOBO
        )
    if config.enable_jams:
        runners[PdfProvider.JAMS] = PaperPipelineProviderRunner(
            PdfProvider.JAMS
        )
    sciencedirect_options = config.provider_options.get("sciencedirect", {})
    if config.enable_sciencedirect and _option_text(
        sciencedirect_options,
        "api_key",
        "elsevier_api_key",
        environment="ELSEVIER_API_KEY",
    ):
        runners[PdfProvider.SCIENCEDIRECT] = PaperPipelineProviderRunner(
            PdfProvider.SCIENCEDIRECT
        )
    return runners


@runtime_checkable
class ArtifactValidator(Protocol):
    """Validation seam used immediately before and after every stage."""

    def is_current(
        self,
        paper: PaperCandidate,
        root: Path,
        manifest_root: Path | None = None,
    ) -> bool:
        """Return true only for a complete, identity-valid durable artifact."""


def download_artifact_is_intact(
    paper: PaperCandidate,
    paths: ArtifactPaths,
    manifest: Mapping[str, Any] | None = None,
) -> bool:
    """Byte-level check that a ``downloaded`` manifest still describes its PDF.

    This is the contract the derived exporters (``Analyzer/Type/paper_type.py``)
    enforce: the manifest is ``downloaded`` for this ``source_id``, names the
    indexed filename, and the file on disk still hashes to the recorded
    SHA-256.  It deliberately ignores the stricter bibliographic identity
    rules of :func:`download_artifact_is_current`, which compare the manifest
    against the *current* database row: a metadata refresh (title spacing,
    subtitle, DOI recovery) must never turn a reviewed, hash-pinned PDF back
    into a download target, because the downloader would then replace the
    ``downloaded`` manifest with ``unmanaged_existing_file`` and the corpus
    would silently lose that paper.
    """

    value = manifest if manifest is not None else load_json_object(paths.download_manifest)
    if not value or value.get("status") != "downloaded":
        return False
    manifest_paper = value.get("paper")
    pdf = value.get("pdf")
    if not isinstance(manifest_paper, dict) or not isinstance(pdf, dict):
        return False
    if manifest_paper.get("source_id") != paper.source_id:
        return False
    if pdf.get("path") != paths.pdf.name or not isinstance(pdf.get("sha256"), str):
        return False
    if type(pdf.get("page_count")) is not int or pdf["page_count"] < 1:
        return False
    if paths.pdf.is_symlink() or not paths.pdf.is_file():
        return False
    try:
        if isinstance(pdf.get("bytes"), int) and paths.pdf.stat().st_size != pdf["bytes"]:
            return False
        return sha256_file(paths.pdf) == pdf["sha256"]
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class ManifestArtifactValidator:
    """Adapter over the mature manifest/PDF validation policy.

    An artifact counts as current when it passes the strict identity policy
    *or* is an intact, hash-verified ``downloaded`` artifact (see
    :func:`download_artifact_is_intact`).
    """

    def is_current(
        self,
        paper: PaperCandidate,
        root: Path,
        manifest_root: Path | None = None,
    ) -> bool:
        try:
            paths = ArtifactPaths.for_source(
                root,
                paper.source_id,
                manifest_root=manifest_root,
            )
        except PaperFileIndexMissingSourceError:
            return False
        return download_artifact_is_current(paper, paths) or download_artifact_is_intact(
            paper, paths
        )


@runtime_checkable
class PdfDebugger(Protocol):
    """Minimal interface implemented by ``Util.debugger``."""

    def record_stage(
        self,
        provider: str,
        before_source_ids: Sequence[str],
        after_source_ids: Sequence[str],
    ) -> Any:
        """Record unresolved IDs before and after one provider."""


@runtime_checkable
class PdfLogger(Protocol):
    """Minimal interface implemented by ``Util.logger.ProgressLogger``."""

    def pdf(self, message: str) -> Any:
        """Emit one PDF collection progress message."""


ProgressCallback: TypeAlias = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class ProviderStageResult:
    provider: PdfProvider
    status: StageStatus
    before_source_ids: tuple[str, ...]
    after_source_ids: tuple[str, ...]
    attempted_source_ids: frozenset[str] = frozenset()
    runner_reported_successful_source_ids: frozenset[str] = frozenset()
    durable_resolved_source_ids: frozenset[str] = frozenset()
    non_durable_reported_source_ids: frozenset[str] = frozenset()
    error: str | None = None
    metadata: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class KciPdfCollectionResult:
    papers_dir: Path
    manifest_dir: Path
    selected_source_ids: tuple[str, ...]
    initially_current_source_ids: frozenset[str]
    stages: tuple[ProviderStageResult, ...]
    current_source_ids: frozenset[str]
    unresolved_source_ids: tuple[str, ...]
    papers: tuple[PaperCandidate, ...] = field(repr=False, compare=False)

    @property
    def selected_count(self) -> int:
        return len(self.selected_source_ids)

    @property
    def current_count(self) -> int:
        return len(self.current_source_ids)

    @property
    def newly_collected_source_ids(self) -> frozenset[str]:
        return self.current_source_ids - self.initially_current_source_ids

    @property
    def complete(self) -> bool:
        return not self.unresolved_source_ids


class KciPdfCollector:
    """Run stage-only provider adapters in the required strict order."""

    def __init__(
        self,
        runners: Mapping[PdfProvider | str, ProviderRunner],
        *,
        config: KciPdfConfig | None = None,
        artifact_validator: ArtifactValidator | None = None,
        debugger: PdfDebugger | None = None,
        logger: PdfLogger | ProgressCallback | None = None,
    ) -> None:
        self.config = config or KciPdfConfig()
        self.artifact_validator = artifact_validator or ManifestArtifactValidator()
        self.debugger = debugger
        self.logger = logger
        normalized: dict[PdfProvider, ProviderRunner] = {}
        for raw_provider, runner in runners.items():
            provider = (
                raw_provider
                if isinstance(raw_provider, PdfProvider)
                else PdfProvider(str(raw_provider))
            )
            runner_provider = getattr(runner, "provider", None)
            if runner_provider != provider:
                raise ValueError(
                    f"runner registered for {provider.value!r} declares "
                    f"{runner_provider!r}"
                )
            normalized[provider] = runner
        self.runners = normalized

    def collect(
        self,
        papers: Iterable[PaperCandidate],
    ) -> KciPdfCollectionResult:
        ordered_papers = self._validated_papers(papers)
        self._prepare_artifact_roots()
        assert self.config.manifest_dir is not None
        index_path = self.config.manifest_dir / PAPER_FILE_INDEX_NAME
        if not self.config.dry_run:
            # Reserve a title filename up front so a provider can write the PDF
            # under its final name; reservations that stay empty are released
            # again at the end of the run (see below).
            extend_paper_file_index_if_present(
                index_path,
                (
                    (paper.source_id, paper.title)
                    for paper in ordered_papers
                    if paper.title and paper.title.strip()
                ),
                papers_dir=self.config.papers_dir,
            )
        papers_by_source = {
            paper.source_id: paper for paper in ordered_papers
        }
        selected_ids = tuple(paper.source_id for paper in ordered_papers)
        def missing_now(purpose: str) -> tuple[str, ...]:
            """Papers without a valid PDF right now (file, hash and manifest are re-checked).

            Hashing every stored PDF takes minutes, so the check reports itself
            instead of leaving the terminal silent.
            """

            self._emit(f"{purpose} — 선별 논문 {len(selected_ids):,}편의 PDF 파일 상태를 점검하는 중")
            missing: list[str] = []
            for position, source_id in enumerate(selected_ids, start=1):
                if not self._is_current(papers_by_source[source_id]):
                    missing.append(source_id)
                if position % 150 == 0 and position < len(selected_ids):
                    self._emit(f"  PDF 상태 점검 {position:,}/{len(selected_ids):,}편 ({position / len(selected_ids):.0%})")
            self._emit(f"  PDF 상태 점검 완료 — 정상 보유 {len(selected_ids) - len(missing):,}편 · PDF 없음 {len(missing):,}편")
            return tuple(missing)

        initially_missing = frozenset(missing_now("수집 시작 전 점검"))
        initially_current = frozenset(selected_ids) - initially_missing
        stage_results: list[ProviderStageResult] = []

        for provider in PROVIDERS_IN_ORDER:
            # This check intentionally happens immediately before *every*
            # provider.  A prior runner's report is not authoritative and an
            # artifact may also have been completed by another process.
            position = list(PdfProvider).index(provider) + 1
            before_ids = missing_now(f"[제공처 {position}/{len(PdfProvider)}] {provider_label(provider)} 준비")
            self._emit(
                f"[제공처 {position}/{len(PdfProvider)}] {provider_label(provider)} 시작 — 아직 PDF가 없는 논문 {len(before_ids)}편"
            )
            report: ProviderRunReport | None = None
            error: str | None = None
            if not before_ids:
                status = StageStatus.NOT_NEEDED
            elif self.config.dry_run:
                status = StageStatus.DRY_RUN
            elif not self.config.provider_enabled(provider):
                status = StageStatus.DISABLED
            elif provider not in self.runners:
                status = StageStatus.UNAVAILABLE
            else:
                status = StageStatus.COMPLETED
                runner = self.runners[provider]
                stage_papers = tuple(
                    papers_by_source[source_id] for source_id in before_ids
                )
                context = ProviderContext(
                    provider=provider,
                    papers_dir=self.config.papers_dir,
                    database_url=self.config.database_url,
                    options=self.config.provider_options.get(provider.value, {}),
                    manifest_dir=self.config.manifest_dir,
                    progress=self._emit,
                )
                try:
                    report = runner.run(stage_papers, context)
                    self._validate_report(report, provider, before_ids)
                    for source_id, paper in report.updated_papers.items():
                        papers_by_source[source_id] = paper
                except Exception as exc:
                    status = StageStatus.FAILED
                    error = f"{type(exc).__name__}: {exc}"
                    self._emit(
                        f"{provider_label(provider)} 오류: {error}"
                    )
                    if not self.config.continue_on_provider_error:
                        raise

            # Re-run the mature validator even when the adapter failed.  It may
            # have committed a valid PDF immediately before raising.
            after_ids = tuple(
                source_id
                for source_id in before_ids
                if not self._is_current(papers_by_source[source_id])
            )
            durable_resolved = frozenset(before_ids) - frozenset(after_ids)
            attempted = (
                report.attempted_source_ids if report is not None else frozenset()
            )
            reported_success = (
                report.successful_source_ids if report is not None else frozenset()
            )
            non_durable = reported_success - durable_resolved
            result = ProviderStageResult(
                provider=provider,
                status=status,
                before_source_ids=before_ids,
                after_source_ids=after_ids,
                attempted_source_ids=attempted,
                runner_reported_successful_source_ids=reported_success,
                durable_resolved_source_ids=durable_resolved,
                non_durable_reported_source_ids=non_durable,
                error=error,
                metadata=report.metadata if report is not None else {},
            )
            stage_results.append(result)
            if self.debugger is not None:
                self.debugger.record_stage(
                    provider.value,
                    before_ids,
                    after_ids,
                )
            self._emit(
                f"{provider_label(provider)} {STAGE_STATUS_LABELS_KO.get(status.value, status.value)} — "
                f"이번에 확보 {len(durable_resolved)}편 · 남은 논문 {len(after_ids)}편"
            )

        final_papers = tuple(
            papers_by_source[source_id] for source_id in selected_ids
        )
        unresolved = tuple(
            source_id
            for source_id in selected_ids
            if not self._is_current(papers_by_source[source_id])
        )
        current = frozenset(selected_ids) - frozenset(unresolved)
        if not self.config.dry_run and index_path.exists():
            # Release every reservation that never materialised as a PDF: the
            # ones made by this run and any left behind by an interrupted
            # earlier run.  Downstream stages define the corpus as "indexed
            # paper with a PDF", so a file-less entry must not linger.  Entries
            # whose PDF exists are always kept, even when the artifact failed
            # validation, so a temporarily invalid PDF is retried, not dropped.
            remove_paper_file_index_entries(
                index_path,
                set(load_paper_file_index(index_path, required=False)) - current,
                papers_dir=self.config.papers_dir,
            )
        return KciPdfCollectionResult(
            papers_dir=self.config.papers_dir,
            manifest_dir=self.config.manifest_dir,
            selected_source_ids=selected_ids,
            initially_current_source_ids=initially_current,
            stages=tuple(stage_results),
            current_source_ids=current,
            unresolved_source_ids=unresolved,
            papers=final_papers,
        )

    def collect_from_database(
        self,
        *,
        include_missing_doi: bool = True,
        source_ids: Sequence[str] | None = None,
        after_id: int = 0,
        limit: int | None = None,
        chunk_size: int = 250,
    ) -> KciPdfCollectionResult:
        """Load the existing DB projection and run the ordered PDF workflow."""

        if not self.config.database_url:
            raise ValueError("database_url is required for collect_from_database")
        engine = create_engine(self.config.database_url, future=True)
        try:
            papers = iter_paper_candidates(
                engine,
                source_table=self.config.source_table,
                include_missing_doi=include_missing_doi,
                source_ids=source_ids,
                after_id=after_id,
                limit=limit,
                chunk_size=chunk_size,
            )
            return self.collect(papers)
        finally:
            engine.dispose()

    @staticmethod
    def _validated_papers(
        papers: Iterable[PaperCandidate],
    ) -> tuple[PaperCandidate, ...]:
        validated: list[PaperCandidate] = []
        for paper in papers:
            if not isinstance(paper, PaperCandidate):
                raise TypeError("papers must contain PaperCandidate values")
            if not paper.source_id or not paper.source_id.strip():
                raise ValueError("paper source_id must be non-empty")
            validated.append(paper)
        return tuple(deduplicate_pdf_candidates(validated))

    def _prepare_artifact_roots(self) -> None:
        manifest_dir = self.config.manifest_dir
        assert manifest_dir is not None
        for label, root in (
            ("papers_dir", self.config.papers_dir),
            ("manifest_dir", manifest_dir),
        ):
            if root.is_symlink():
                raise ValueError(f"{label} must not be a symlink: {root}")
            root.mkdir(parents=True, exist_ok=True)
        for compatibility_root in self.config.compatibility_papers_dirs:
            if compatibility_root.is_symlink():
                raise ValueError(
                    "compatibility papers directory must not be a symlink: "
                    f"{compatibility_root}"
                )

    def _is_current(self, paper: PaperCandidate) -> bool:
        return any(
            self.artifact_validator.is_current(paper, paper_root, manifest_root)
            for paper_root, manifest_root in self.config.lookup_layouts
        )

    @staticmethod
    def _validate_report(
        report: ProviderRunReport,
        provider: PdfProvider,
        before_ids: Sequence[str],
    ) -> None:
        if not isinstance(report, ProviderRunReport):
            raise TypeError("provider runner must return ProviderRunReport")
        if report.provider is not provider:
            raise ValueError("provider report/provider mismatch")
        allowed = set(before_ids)
        attempted = set(report.attempted_source_ids)
        successful = set(report.successful_source_ids)
        if not attempted.issubset(allowed):
            raise ValueError("provider report contains an unselected attempted ID")
        if not successful.issubset(attempted):
            raise ValueError("reported successes must be a subset of attempted IDs")
        if not set(report.updated_papers).issubset(allowed):
            raise ValueError("provider report updated an unselected paper")
        for source_id, paper in report.updated_papers.items():
            if not isinstance(paper, PaperCandidate) or paper.source_id != source_id:
                raise ValueError("updated_papers must preserve source_id identity")

    def _emit(self, message: str) -> None:
        if self.logger is None:
            return
        pdf_method = getattr(self.logger, "pdf", None)
        if callable(pdf_method):
            pdf_method(message)
            return
        if callable(self.logger):
            self.logger(message)
            return
        raise TypeError("logger must be callable or provide pdf(message)")


def collect_kci_pdfs(
    papers: Iterable[PaperCandidate],
    *,
    runners: Mapping[PdfProvider | str, ProviderRunner],
    config: KciPdfConfig | None = None,
    artifact_validator: ArtifactValidator | None = None,
    debugger: PdfDebugger | None = None,
    logger: PdfLogger | ProgressCallback | None = None,
) -> KciPdfCollectionResult:
    """Functional entry point for the strict ordered KCI PDF workflow."""

    return KciPdfCollector(
        runners,
        config=config,
        artifact_validator=artifact_validator,
        debugger=debugger,
        logger=logger,
    ).collect(papers)


def run_kci_pdf_collection(
    *,
    runners: Mapping[PdfProvider | str, ProviderRunner] | None = None,
    database_url: str | None = None,
    source_table: str = "kci_papers",
    papers_dir: Path | None = None,
    manifest_dir: Path | None = None,
    compatibility_papers_dirs: tuple[Path, ...] = (),
    enable_dbpia: bool = False,
    enable_kyobo: bool = False,
    enable_jams: bool = False,
    enable_sciencedirect: bool = False,
    dry_run: bool = False,
    continue_on_provider_error: bool = True,
    provider_options: Mapping[str, Mapping[str, Any]] | None = None,
    artifact_validator: ArtifactValidator | None = None,
    debugger: PdfDebugger | None = None,
    logger: PdfLogger | ProgressCallback | None = None,
    include_missing_doi: bool = True,
    source_ids: Sequence[str] | None = None,
    after_id: int = 0,
    limit: int | None = None,
    chunk_size: int = 250,
) -> KciPdfCollectionResult:
    """High-level DB-to-artifacts entry point used by ``main``/Scheduler.

    The defaults select ``kci_papers`` and write PDFs to ``Analyzer/Paper``
    while manifests and locks go to ``Analyzer/Manifest``. Pass the same path
    as both ``papers_dir`` and ``manifest_dir`` for a legacy unified layout.

    ``dry_run`` performs DB selection, current-artifact validation, ordered
    stage/debugger reporting, and no provider calls.  When ``runners`` is not
    supplied, operational stage-only adapters are built for configured
    providers; missing credentials remain visibly ``unavailable``.
    """

    effective_papers_dir = papers_dir if papers_dir is not None else default_papers_dir()
    effective_manifest_dir = (
        manifest_dir
        if manifest_dir is not None
        else (
            default_manifest_dir()
            if papers_dir is None
            else effective_papers_dir
        )
    )
    config = KciPdfConfig(
        papers_dir=effective_papers_dir,
        manifest_dir=effective_manifest_dir,
        compatibility_papers_dirs=compatibility_papers_dirs,
        database_url=(
            database_url
            if database_url is not None
            else os.getenv("DATABASE_URL", "sqlite:///kci_pipeline.db")
        ),
        source_table=source_table,
        enable_dbpia=enable_dbpia,
        enable_kyobo=enable_kyobo,
        enable_jams=enable_jams,
        enable_sciencedirect=enable_sciencedirect,
        dry_run=dry_run,
        continue_on_provider_error=continue_on_provider_error,
        provider_options=provider_options or {},
    )
    effective_runners = (
        build_default_provider_runners(config) if runners is None else runners
    )
    return KciPdfCollector(
        effective_runners,
        config=config,
        artifact_validator=artifact_validator,
        debugger=debugger,
        logger=logger,
    ).collect_from_database(
        include_missing_doi=include_missing_doi,
        source_ids=source_ids,
        after_id=after_id,
        limit=limit,
        chunk_size=chunk_size,
    )


__all__ = [
    "ArtifactValidator",
    "DBPIA_BROWSER_RESUME_FILE",
    "DEFAULT_MANIFEST_DIR",
    "DEFAULT_PAPERS_DIR",
    "KciPdfCollectionResult",
    "KciPdfCollector",
    "KciPdfConfig",
    "LEGACY_PAPERS_DIR",
    "ManifestArtifactValidator",
    "PDF_PROVIDER_ORDER",
    "PaperPipelineProviderRunner",
    "download_artifact_is_intact",
    "PROVIDERS_IN_ORDER",
    "PdfDebugger",
    "PdfLogger",
    "PdfProvider",
    "ProviderContext",
    "ProviderExecutionError",
    "ProviderFunctionRunner",
    "ProviderRunReport",
    "default_manifest_dir",
    "default_papers_dir",
    "ProviderRunner",
    "ProviderStageResult",
    "SourceIdCommandBuilder",
    "StageStatus",
    "SubprocessProviderRunner",
    "build_default_provider_runners",
    "collect_kci_pdfs",
    "run_kci_pdf_collection",
]
