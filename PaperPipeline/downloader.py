"""Resolve DOI metadata to public PDFs and persist audited artifacts."""

from __future__ import annotations

import fcntl
import hashlib
import html
import io
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urldefrag, urljoin, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.dbpia_browser import (
    DBPIA_BROWSER_RESOLVER_NAME,
    AuthenticatedDbpiaBrowserResolver,
    DbpiaBrowserConfig,
    DbpiaBrowserResolution,
    is_exact_dbpia_article_url,
)
from PaperPipeline.dcs_resolver import DcsPublicPdfResolver
from PaperPipeline.elsevier_resolver import (
    ElsevierArticleResolver,
    ElsevierResolverConfig,
)
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    SafeHttpClient,
    redact_url,
)
from PaperPipeline.identifier_resolver import normalize_title
from PaperPipeline.jiis_resolver import JiisPublicSearchResolver
from PaperPipeline.jams_browser import (
    JAMS_BROWSER_RESOLVER_NAME,
    AuthenticatedKdfsJamsBrowserResolver,
    JamsBrowserConfig,
    JamsBrowserResolution,
)
from PaperPipeline.kci_scienceon_resolver import (
    KciScienceOnResolution,
    KciScienceOnResolver,
)
from PaperPipeline.kdfs_resolver import (
    KdfsJamsAuthorizedPdfResolver,
    KdfsResolution,
    KdfsResolverConfig,
)
from PaperPipeline.kicj_resolver import (
    KicjPublicPdfResolver,
    KicjResolution,
)
from PaperPipeline.kyobo_browser import (
    KYOBO_BROWSER_RESOLVER_NAME,
    AuthenticatedKyoboBrowserResolver,
    KyoboBrowserConfig,
    KyoboBrowserResolution,
)
from PaperPipeline.police_resolver import (
    POLICE_INSTITUTION,
    POLICE_INTERNAL_USE_NOTICE,
    POLICE_JOURNAL,
    POLICE_RESOLVER_NAME,
    PoliceResolution,
    PoliceResolverConfig,
    PoliceUniversityPublicPdfResolver,
)
from PaperPipeline.platform_resolver import (
    AuthorizedPlatformPdfResolver,
    DBPIA_LAYOUT_VERIFIED_PROVENANCE,
    DBPIA_RESOLVER_VERIFIED_PROVENANCE,
    DbpiaPdfIdentityError,
    KISS_RESOLVER_VERIFIED_PROVENANCE,
    PLATFORM_RESOLVER_NAME,
    PlatformResolution,
    PlatformResolverConfig,
    validate_dbpia_pdf_document,
)
from PaperPipeline.records import PaperCandidate, ResolverCandidate
from PaperPipeline.spo_resolver import (
    SPO_CRIMINAL_LAW_JOURNAL,
    SPO_FORENSIC_JOURNAL,
    SPO_INSTITUTION,
    SpoPublicPdfResolver,
    SpoResolution,
    SpoResolverConfig,
)
from PaperPipeline.storage import (
    ArtifactPaths,
    load_json_object,
    safe_source_stem,
    sha256_file,
    write_bytes_atomic,
    write_json_atomic,
)


DOWNLOAD_SCHEMA_VERSION = "1.1"
PDF_MAGIC = b"%PDF-"
HTML_LIMIT_BYTES = 2 * 1024 * 1024
METADATA_LIMIT_BYTES = 4 * 1024 * 1024
MAX_PDF_PAGES = 2_000
KCI_DOI_CONFLICT_IDENTITY_STATUS = (
    "resolver_source_id_title_match_doi_conflict"
)
_KCI_DOI_CONFLICT_REQUIRED_PROVENANCE = frozenset(
    {
        "kci_landing_exact_database_doi",
        "kci_explicit_source_file_binding",
        "pdf_normalized_title_match",
    }
)
_PDF_DIFFERENT_DOI_MESSAGE = (
    "PDF identity mismatch: first pages contain a different DOI"
)
_URL_IN_ERROR_RE = re.compile(r"""https?://[^\s<>"']+""", re.IGNORECASE)
_SENSITIVE_ERROR_QUERY_RE = re.compile(
    r"""(?i)\b(email|mailto|api[_-]?key|token|signature|credential|auth|cookie)"""
    r"""=([^&\s,;]+)"""
)
TITLE_DOI_RESOLVERS = frozenset(
    {
        "kci-openapi-detail",
        "crossref-title",
        "unpaywall-title",
        "openalex-title",
        "semantic-scholar-title",
    }
)
TITLE_PDF_IDENTITY_RESOLVERS = frozenset(
    {"unpaywall-title", "openalex-title", "semantic-scholar-title"}
)
DOWNLOAD_PROVIDER_SCOPES = frozenset(
    {
        "kci",
        "unpaywall",
        "openalex",
        "semantic_scholar",
        "dbpia",
        "kyobo",
        "jams",
        "sciencedirect",
    }
)
_DBPIA_OPENAPI_LANDING_SOURCE = "dbpia_openapi_memory"
_DBPIA_DISCOVERY_URL_RE = re.compile(
    r"https?://www[.]dbpia[.]co[.]kr/(?:journal/articledetail|Journal/ArticleDetail)"
    r"[^\s<>\"']*",
    re.IGNORECASE,
)
_DBPIA_NODE_AUDIT_RE = re.compile(r"\bNODE[0-9]{5,20}\b", re.IGNORECASE)
_DBPIA_ARTICLE_PATH_AUDIT_RE = re.compile(
    r"\A/journal/articledetail/(?:NODE[0-9]{5,20}|[1-9][0-9]{0,19})\Z",
    re.IGNORECASE,
)
_DBPIA_BROWSER_LAYOUT_PROVENANCE = frozenset(
    {
        "dbpia_landing_identity_exact",
        "dbpia_download_button_node_exact",
        "native_browser_download_event",
        "dbpia_pdf_doi_layout_exact",
        "dbpia_pdf_title_exact",
    }
)
_JAMS_BROWSER_PDF_IDENTITY_PROVENANCE = frozenset(
    {
        "jams_download_control_exactly_one",
        "native_browser_download_event",
        "kdfs_pdf_identity_exact",
    }
)
_JAMS_BROWSER_IDENTITY_STATUS = "jams_browser_bibliographic_identity_match"
_KYOBO_BROWSER_IDENTITY_STATUS = "kyobo_browser_bibliographic_identity_match"
_KYOBO_BROWSER_CORE_PROVENANCE = frozenset(
    {
        "kyobo_landing_year_exact",
        "kyobo_landing_journal_exact",
        "kyobo_landing_volume_issue_exact",
        "kyobo_landing_authors_exact",
        "kyobo_landing_canonical_article_exact",
        "kyobo_visible_down_control_exact",
        "native_browser_download_event",
    }
)
_KYOBO_BROWSER_TITLE_PROVENANCE = frozenset(
    {
        "kyobo_landing_title_normalized_exact",
        "kyobo_landing_title_compact_exact",
        "kyobo_landing_title_main_exact",
        "kyobo_landing_title_doi_bibliographic_equivalent",
    }
)
_KYOBO_BROWSER_PUBLISHER_PROVENANCE = frozenset(
    {
        "kyobo_landing_publisher_exact",
        "kyobo_landing_publisher_qualified_generic_exact",
        # exact organisation name, differing only by a legal-form prefix such as
        # "사단법인"; the resolver grants it only together with an exact landing DOI
        "kyobo_landing_publisher_legal_form_exact",
    }
)
_KYOBO_BROWSER_PAGE_PROVENANCE = frozenset(
    {
        "kyobo_landing_page_range_exact",
        "kyobo_landing_final_page_off_by_one",
    }
)
_KYOBO_BROWSER_DISCOVERY_PROVENANCE = frozenset(
    {
        "kyobo_doi_redirect_article_exact",
        "kyobo_web_title_search_exact",
        "kyobo_supplied_article_reference_exact",
    }
)
_KYOBO_BROWSER_PDF_PROVENANCE = frozenset(
    {
        "kyobo_pdf_page_count_exact",
        "kyobo_pdf_page_count_off_by_one_title_exact",
    }
)
_DBPIA_BROWSER_REDOWNLOAD_AUDIT = "dbpia_browser_redownload"
_DBPIA_BROWSER_REDOWNLOAD_ROLLBACK = "_dbpia_browser_commit_rollback"
_DBPIA_BROWSER_REDOWNLOAD_CAMPAIGN_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
)
_DBPIA_BIBLIOGRAPHY_CORE_PROVENANCE = frozenset(
    {
        "dbpia_landing_publisher_exact",
        "dbpia_landing_journal_exact",
        "dbpia_landing_page_range_exact",
        "dbpia_landing_authors_exact",
    }
)
class PdfValidationError(ValueError):
    code = "invalid_pdf"


class EncryptedPdfError(PdfValidationError):
    code = "unreadable_encrypted"


class KicjResolver(Protocol):
    """Small dependency seam for a shared KICJ archive resolver."""

    def resolve(self, paper: PaperCandidate) -> KicjResolution:
        """Resolve one paper to an explicit official KICJ PDF candidate."""


class PoliceResolver(Protocol):
    """Dependency seam for one shared Police University board resolver."""

    def resolve(self, paper: PaperCandidate) -> PoliceResolution:
        """Resolve one applicable row to its already-fetched public PDF."""


class SpoResolver(Protocol):
    """Small dependency seam for one shared, internally synchronized SPO resolver."""

    def resolve(self, paper: PaperCandidate) -> SpoResolution:
        """Resolve an applicable paper to an already content-bound PDF fetch."""


class KdfsResolver(Protocol):
    """Dependency seam for the permission-gated KDFS JAMS resolver."""

    def resolve(self, paper: PaperCandidate) -> KdfsResolution:
        """Resolve one applicable KDFS record to an already-validated fetch."""


class PlatformResolver(Protocol):
    """Dependency seam for authorized credential-free publisher UI flows."""

    def resolve(
        self,
        paper: PaperCandidate,
        *,
        landing_url: str | None = None,
    ) -> PlatformResolution:
        """Resolve one supported platform record to an already-fetched PDF."""


class DbpiaBrowserResolver(Protocol):
    """Dependency seam for an explicitly authenticated native browser flow."""

    def resolve(
        self,
        paper: PaperCandidate,
        *,
        landing_url: str | None = None,
    ) -> DbpiaBrowserResolution:
        """Return URL-free PDF bytes from one exact DBpia article page."""


class KyoboBrowserResolver(Protocol):
    """Dependency seam for an explicitly authorized Kyobo browser flow."""

    def resolve(
        self,
        paper: PaperCandidate,
        *,
        landing_url: str | None = None,
    ) -> KyoboBrowserResolution:
        """Return URL-free bytes from one exact Kyobo article page."""


class JamsBrowserResolver(Protocol):
    """Dependency seam for an explicitly authorized KDFS JAMS flow."""

    def resolve(
        self,
        paper: PaperCandidate,
        *,
        public_resolution: KdfsResolution | None = None,
    ) -> JamsBrowserResolution:
        """Return URL-free bytes from one exact KDFS JAMS detail page."""


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    max_pdf_bytes: int = 50 * 1024 * 1024
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    allow_http: bool = False
    request_gate: Callable[[str], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    unpaywall_email: str | None = None
    openalex_api_key: str | None = None
    elsevier_api_key: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    elsevier_institution_token: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    crossref_email: str | None = None
    require_open_access_evidence: bool = False
    enable_kci_ui_download: bool = False
    enable_kci_scienceon_download: bool = False
    # Public reachability does not grant bulk-copy permission.  This switch is
    # false unless the operator has obtained written SPO permission.
    enable_spo_download: bool = False
    enable_kdfs_download: bool = False
    enable_platform_download: bool = False
    enable_dbpia_browser_download: bool = False
    enable_kyobo_browser_download: bool = False
    enable_jams_browser_download: bool = False
    dbpia_browser_redownload_campaign: str | None = None
    dbpia_browser_profile_dir: Path | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    repository_root: Path | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    dbpia_browser_headless: bool = True
    dbpia_browser_min_interval_seconds: float = 10.0
    dbpia_browser_max_attempts_per_run: int = 25
    dbpia_browser_max_search_results: int = 20
    dbpia_login_id: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    dbpia_login_password: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    dbpia_required_institution_name: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    kyobo_browser_profile_dir: Path | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    kyobo_browser_headless: bool = True
    kyobo_browser_min_interval_seconds: float = 10.0
    kyobo_browser_max_attempts_per_run: int = 25
    kyobo_browser_max_search_results: int = 20
    jams_browser_profile_dir: Path | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    jams_browser_headless: bool = True
    jams_browser_interactive_login: bool = False
    jams_browser_min_interval_seconds: float = 10.0
    jams_browser_max_attempts_per_run: int = 25
    kci_journal_pdf_urls: Mapping[str, str] = field(default_factory=dict)
    # Exact, pre-resolved publisher article pages.  DBpia entries are produced
    # only from an operator-supplied NODE mapping or its official search API;
    # temporary signed download URLs must never be stored here.
    platform_landing_urls: Mapping[str, str] = field(default_factory=dict)
    # Official DBpia search results are transient metadata.  For these source
    # rows the downloader may consume the landing URL in memory, but must not
    # persist that URL/NODE in either successful or failed manifests.  DBpia
    # identifiers are suppressed for manually mapped landings as well so a
    # pre-marker manifest can be migrated safely on reuse.
    ephemeral_platform_landing_source_ids: frozenset[str] = frozenset()
    # Direct PDF candidates prepared by an identifier resolver and bound to a
    # source row. They are attempted before any per-DOI metadata request.
    preverified_candidates: Mapping[str, tuple[ResolverCandidate, ...]] = field(
        default_factory=dict
    )
    semantic_scholar_candidates: Mapping[str, ResolverCandidate] = field(
        default_factory=dict
    )
    semantic_scholar_resolutions: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    # Restrict one downloader invocation to exactly one application provider.
    # ``None`` preserves the historical combined resolver flow unchanged.
    provider_scope: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedPdf:
    content: bytes
    page_count: int
    identity_status: str = "not_checked"
    matched_doi: str | None = None
    matched_source_id: str | None = None


class PdfDownloader:
    """One audited resolver instance with optional, explicit auth backends.

    The KCI UI resolver is intentionally not exercised unless explicitly
    enabled. KCI currently disallows generic crawlers in robots.txt; the opt-in
    is reserved for environments that have permission to automate that UI.
    """

    def __init__(
        self,
        config: DownloadConfig,
        *,
        http: SafeHttpClient | None = None,
        kicj_resolver: KicjResolver | None = None,
        police_resolver: PoliceResolver | None = None,
        spo_resolver: SpoResolver | None = None,
        kci_scienceon_resolver: KciScienceOnResolver | None = None,
        kdfs_resolver: KdfsResolver | None = None,
        platform_resolver: PlatformResolver | None = None,
        dbpia_browser_resolver: DbpiaBrowserResolver | None = None,
        kyobo_browser_resolver: KyoboBrowserResolver | None = None,
        jams_browser_resolver: JamsBrowserResolver | None = None,
    ) -> None:
        self.config = config
        if (
            self.config.provider_scope is not None
            and self.config.provider_scope not in DOWNLOAD_PROVIDER_SCOPES
        ):
            raise ValueError(
                "provider_scope must be one of: "
                + ", ".join(sorted(DOWNLOAD_PROVIDER_SCOPES))
            )
        self._dbpia_browser_redownload_campaign = (
            _validated_dbpia_redownload_campaign(
                config.dbpia_browser_redownload_campaign
            )
        )
        self.http = http or SafeHttpClient()
        self.kicj_resolver = kicj_resolver or KicjPublicPdfResolver(
            http=self.http
        )
        self.police_resolver = (
            police_resolver
            or PoliceUniversityPublicPdfResolver(
                PoliceResolverConfig(
                    connect_timeout=self.config.connect_timeout,
                    read_timeout=self.config.read_timeout,
                    max_pdf_bytes=self.config.max_pdf_bytes,
                    request_gate=self.config.request_gate,
                )
            )
        )
        self.spo_resolver = spo_resolver or SpoPublicPdfResolver(
            SpoResolverConfig(
                permission_granted=self.config.enable_spo_download,
                max_attachment_bytes=self.config.max_pdf_bytes,
                connect_timeout=self.config.connect_timeout,
                read_timeout=self.config.read_timeout,
            )
        )
        self.kci_scienceon_resolver = (
            kci_scienceon_resolver or KciScienceOnResolver(http=self.http)
        )
        self.kdfs_resolver = (
            kdfs_resolver
            or KdfsJamsAuthorizedPdfResolver(
                KdfsResolverConfig(
                    permission_granted=self.config.enable_kdfs_download,
                    connect_timeout=self.config.connect_timeout,
                    read_timeout=self.config.read_timeout,
                    max_pdf_bytes=self.config.max_pdf_bytes,
                )
            )
        )
        self.platform_resolver = (
            platform_resolver
            or AuthorizedPlatformPdfResolver(
                PlatformResolverConfig(
                    permission_granted=self.config.enable_platform_download,
                    max_pdf_bytes=self.config.max_pdf_bytes,
                ),
                http=self.http,
            )
        )
        self.dbpia_browser_resolver = dbpia_browser_resolver
        self.kyobo_browser_resolver = kyobo_browser_resolver
        self.jams_browser_resolver = jams_browser_resolver
        if (
            self.dbpia_browser_resolver is None
            and (
                self.config.enable_dbpia_browser_download
                or self._dbpia_browser_redownload_campaign is not None
            )
        ):
            self.dbpia_browser_resolver = AuthenticatedDbpiaBrowserResolver(
                DbpiaBrowserConfig(
                    permission_granted=True,
                    profile_dir=self.config.dbpia_browser_profile_dir,
                    repository_root=self.config.repository_root,
                    headless=self.config.dbpia_browser_headless,
                    navigation_timeout_seconds=self.config.read_timeout,
                    download_timeout_seconds=self.config.read_timeout,
                    min_interval_seconds=(
                        self.config.dbpia_browser_min_interval_seconds
                    ),
                    max_attempts_per_run=(
                        self.config.dbpia_browser_max_attempts_per_run
                    ),
                    max_search_results=(
                        self.config.dbpia_browser_max_search_results
                    ),
                    login_id=self.config.dbpia_login_id,
                    login_password=self.config.dbpia_login_password,
                    required_institution_name=(
                        self.config.dbpia_required_institution_name
                    ),
                    max_pdf_bytes=self.config.max_pdf_bytes,
                )
            )
        if (
            self.kyobo_browser_resolver is None
            and self.config.enable_kyobo_browser_download
        ):
            self.kyobo_browser_resolver = AuthenticatedKyoboBrowserResolver(
                KyoboBrowserConfig(
                    permission_granted=True,
                    profile_dir=self.config.kyobo_browser_profile_dir,
                    repository_root=self.config.repository_root,
                    headless=self.config.kyobo_browser_headless,
                    navigation_timeout_seconds=self.config.read_timeout,
                    download_timeout_seconds=self.config.read_timeout,
                    min_interval_seconds=(
                        self.config.kyobo_browser_min_interval_seconds
                    ),
                    max_attempts_per_run=(
                        self.config.kyobo_browser_max_attempts_per_run
                    ),
                    max_search_results=(
                        self.config.kyobo_browser_max_search_results
                    ),
                    max_pdf_bytes=self.config.max_pdf_bytes,
                )
            )
        if (
            self.jams_browser_resolver is None
            and self.config.enable_jams_browser_download
        ):
            self.jams_browser_resolver = AuthenticatedKdfsJamsBrowserResolver(
                JamsBrowserConfig(
                    permission_granted=True,
                    profile_dir=self.config.jams_browser_profile_dir,
                    repository_root=self.config.repository_root,
                    headless=self.config.jams_browser_headless,
                    interactive_login=(
                        self.config.jams_browser_interactive_login
                    ),
                    navigation_timeout_seconds=self.config.read_timeout,
                    download_timeout_seconds=self.config.read_timeout,
                    min_interval_seconds=(
                        self.config.jams_browser_min_interval_seconds
                    ),
                    max_attempts_per_run=(
                        self.config.jams_browser_max_attempts_per_run
                    ),
                    max_pdf_bytes=self.config.max_pdf_bytes,
                )
            )

    def download(
        self,
        paper: PaperCandidate,
        paths: ArtifactPaths,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.state_root.mkdir(parents=True, exist_ok=True)
        if paths.stem != safe_source_stem(paper.source_id):
            raise ValueError("artifact path stem does not match paper source_id")
        if paths.pdf.is_symlink() or paths.download_manifest.is_symlink():
            raise ValueError(f"refusing to write through an artifact symlink: {paths.stem}")

        with paths.lock.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            return self._download_locked(paper, paths, force=force)

    def _download_locked(
        self,
        paper: PaperCandidate,
        paths: ArtifactPaths,
        *,
        force: bool,
    ) -> dict[str, Any]:
        previous = load_json_object(paths.download_manifest)
        if previous is not None:
            previous = _migrate_dbpia_identifier_manifest(paths, previous)
        recovered = self._recover_interrupted_commit(paper, paths, previous)
        if recovered is not None:
            if self._dbpia_browser_redownload_campaign is not None:
                # Recovery proves that the PDF and manifest now form a valid
                # pair; it does not prove that this particular DBpia refresh
                # campaign produced them.  Let the campaign runner inspect
                # its audit marker before deciding whether it may reuse the
                # recovered artifact or must perform a fresh browser attempt.
                return self._run_dbpia_redownload_campaign(
                    paper,
                    paths,
                    previous=recovered,
                    current_artifact=True,
                )
            return {**recovered, "_runtime": {"action": "recovered"}}
        current_artifact = download_artifact_is_current(
            paper,
            paths,
            previous,
        )
        if self._dbpia_browser_redownload_campaign is not None:
            return self._run_dbpia_redownload_campaign(
                paper,
                paths,
                previous=previous,
                current_artifact=current_artifact,
            )
        if current_artifact and (
            not force or self.config.enable_dbpia_browser_download
        ):
            assert previous is not None
            current_manifest = self._upgrade_current_manifest(paper, paths, previous)
            return {**current_manifest, "_runtime": {"action": "reused"}}
        interrupted_commit = bool(
            previous
            and previous.get("status") == "committing"
            and isinstance(previous.get("paper"), dict)
            and previous["paper"].get("source_id") == paper.source_id
            and (previous["paper"].get("doi") or "").casefold()
            == (paper.doi or "").casefold()
        )
        if paths.pdf.exists() and not force and not interrupted_commit:
            manifest = self._base_manifest(paper)
            manifest.update(
                {
                    "status": "unmanaged_existing_file",
                    "last_error": {
                        "code": "unmanaged_existing_file",
                        "message": "PDF exists without a matching download manifest; use --force to replace it",
                    },
                }
            )
            write_json_atomic(paths.download_manifest, manifest)
            return manifest

        manifest = self._base_manifest(paper)
        if self.config.provider_scope is not None:
            return self._download_provider_scoped(
                paper,
                paths,
                manifest,
                previous=previous,
            )
        attempts: list[dict[str, Any]] = manifest["attempts"]
        kci_journal_url = self.config.kci_journal_pdf_urls.get(paper.source_id)
        if kci_journal_url and self.config.require_open_access_evidence:
            attempts.append(
                {
                    "resolver": "kci-journal-sitemap",
                    "url": redact_url(kci_journal_url),
                    "status": "skipped",
                    "error_code": "oa_evidence_required",
                    "message": (
                        "The public sitemap proves anonymous availability, "
                        "but does not provide an open-license assertion"
                    ),
                }
            )
        elif kci_journal_url:
            candidate = ResolverCandidate(
                url=kci_journal_url,
                resolver="kci-journal-sitemap",
                landing_url=kci_journal_url.replace(
                    "/archive/articlePdf?",
                    "/archive/articleView?",
                ),
                is_open_access=None,
                verified_source_id=paper.source_id,
            )
            result = self._fetch_pdf_candidate(candidate, paper, attempts)
            if result is not None:
                validated, fetch = result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    fetch,
                )
        else:
            attempts.append(
                {
                    "resolver": "kci-journal-sitemap",
                    "status": "skipped",
                    "error_code": "not_in_public_sitemap",
                }
            )

        if self.config.enable_kci_ui_download:
            kci_result = self._try_kci_ui(paper, attempts)
            if kci_result is not None:
                validated, fetch, candidate = kci_result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    fetch,
                )
        else:
            attempts.append(
                {
                    "resolver": "kci-ui",
                    "status": "skipped",
                    "error_code": "robots_disallowed",
                    "message": (
                        "KCI UI automation is disabled; enable it only where the "
                        "institution has explicit permission"
                    ),
                }
            )

        if self.config.enable_kci_scienceon_download:
            scienceon_result = self._try_kci_scienceon(paper, attempts)
            if scienceon_result is not None:
                validated, fetch, candidate = scienceon_result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    fetch,
                )

        if self.config.enable_kdfs_download:
            kdfs_result = self._try_kdfs_authorized(paper, attempts)
            if kdfs_result is not None:
                validated, fetch, candidate = kdfs_result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    fetch,
                )

        self._record_semantic_scholar_resolution(paper, attempts)

        kicj_result = self._try_kicj_public_archive(paper, attempts)
        if kicj_result is not None:
            validated, fetch, candidate = kicj_result
            return self._persist_download(
                manifest,
                paths,
                candidate,
                validated,
                fetch,
            )

        # Police University publishes the selected journal through a public
        # title-search form.  Its resolver has already POSTed and fetched the
        # attachment, so this integration must consume that exact response
        # rather than issuing a GET to the form endpoint.
        police_result = self._try_police_public_board(paper, attempts)
        if police_result is not None:
            validated, fetch, candidate = police_result
            return self._persist_download(
                manifest,
                paths,
                candidate,
                validated,
                fetch,
            )

        # SPO includes both DOI and DOI-less rows.  It must therefore run
        # before the DOI-less terminal branch.  With the default policy switch
        # off, the shared resolver records permission_required without making
        # a network request.
        spo_result = self._try_spo_public_board(paper, attempts)
        if spo_result is not None:
            validated, fetch, candidate = spo_result
            return self._persist_download(
                manifest,
                paths,
                candidate,
                validated,
                fetch,
            )

        platform_attempt_start = len(attempts)
        if self.config.enable_platform_download and not paper.doi:
            platform_result = self._try_authorized_platform(paper, attempts)
            if platform_result is not None:
                validated, fetch, candidate = platform_result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    fetch,
                )

        if (
            self.config.enable_dbpia_browser_download
            and not paper.doi
            and (
                not self.config.enable_platform_download
                or _dbpia_browser_fallback_allowed(
                    attempts[platform_attempt_start:]
                )
            )
        ):
            browser_result = self._try_dbpia_authenticated_browser(
                paper,
                attempts,
            )
            if browser_result is not None:
                validated, candidate, content_type = browser_result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    None,
                    content_type=content_type,
                )

        if not paper.doi:
            spo_permission_required = _spo_permission_is_required(attempts)
            spo_failure_status = _spo_failure_status(attempts)
            police_failure_status = _police_failure_status(attempts)
            kdfs_attempted = any(
                str(attempt.get("resolver") or "").startswith(
                    "kdfs-jams-authorized"
                )
                for attempt in attempts
            )
            kdfs_failure_status = (
                _failure_status(
                    [
                        attempt
                        for attempt in attempts
                        if str(attempt.get("resolver") or "").startswith(
                            "kdfs-jams-authorized"
                        )
                    ]
                )
                if kdfs_attempted
                else None
            )
            dbpia_browser_failure_status = _dbpia_browser_failure_status(
                attempts
            )
            identifier_status = (
                paper.identifier_resolution.get("status")
                if isinstance(paper.identifier_resolution, Mapping)
                else None
            )
            if spo_permission_required:
                code = "permission_required"
                message = (
                    "SPO attachment automation requires explicit written "
                    "permission and --enable-spo-download"
                )
            elif spo_failure_status is not None:
                code = spo_failure_status
                message = (
                    "The SPO resolver could not produce a project-validated "
                    f"PDF ({spo_failure_status})"
                )
            elif police_failure_status is not None:
                code = police_failure_status
                message = (
                    "The Police University resolver could not produce a "
                    f"project-validated PDF ({police_failure_status})"
                )
            elif kdfs_failure_status is not None:
                code = kdfs_failure_status
                message = (
                    "The authorized KDFS resolver could not produce an "
                    f"anonymous project-validated PDF ({kdfs_failure_status})"
                )
            elif dbpia_browser_failure_status is not None:
                code = dbpia_browser_failure_status
                message = (
                    "The authenticated DBpia browser resolver could not "
                    "produce a project-validated PDF "
                    f"({dbpia_browser_failure_status})"
                )
            elif identifier_status == "retryable_error":
                code = "retryable_error"
                message = (
                    "Identifier resolution failed transiently; retry before "
                    "treating this paper as unresolved"
                )
            elif (paper.doi_original or "").strip():
                code = "invalid_doi"
                message = "The DB DOI is invalid"
            else:
                code = "identifier_unresolved"
                message = (
                    "No DOI or source-id-bound public PDF was found; "
                    "identifier resolution details are recorded in the manifest"
                )
            manifest.update(
                {
                    "status": code,
                    "updated_at": _utc_now(),
                    "last_error": {"code": code, "message": message},
                }
            )
            write_json_atomic(paths.download_manifest, manifest)
            return manifest

        dcs_result = self._try_dcs_public_reader(paper, attempts)
        if dcs_result is not None:
            validated, fetch, candidate = dcs_result
            return self._persist_download(
                manifest,
                paths,
                candidate,
                validated,
                fetch,
            )

        jiis_result = self._try_jiis_public_search(paper, attempts)
        if jiis_result is not None:
            validated, fetch, candidate = jiis_result
            return self._persist_download(
                manifest,
                paths,
                candidate,
                validated,
                fetch,
            )

        elsevier_result = self._try_elsevier_article_api(paper, attempts)
        if elsevier_result is not None:
            validated, fetch, candidate = elsevier_result
            return self._persist_download(
                manifest,
                paths,
                candidate,
                validated,
                fetch,
            )

        platform_attempt_start = len(attempts)
        if self.config.enable_platform_download and paper.doi:
            platform_result = self._try_authorized_platform(paper, attempts)
            if platform_result is not None:
                validated, fetch, candidate = platform_result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    fetch,
                )

        if (
            self.config.enable_dbpia_browser_download
            and paper.doi
            and (
                not self.config.enable_platform_download
                or _dbpia_browser_fallback_allowed(
                    attempts[platform_attempt_start:]
                )
            )
        ):
            browser_result = self._try_dbpia_authenticated_browser(
                paper,
                attempts,
            )
            if browser_result is not None:
                validated, candidate, content_type = browser_result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    None,
                    content_type=content_type,
                )

        had_candidates = False
        seen_candidate_urls: set[str] = set()
        for candidates in self._candidate_batches(paper, attempts):
            for candidate in candidates:
                candidate_key = urldefrag(candidate.url)[0]
                if candidate_key in seen_candidate_urls:
                    continue
                seen_candidate_urls.add(candidate_key)
                had_candidates = True
                result = self._fetch_pdf_candidate(candidate, paper, attempts)
                if result is None:
                    continue
                validated, fetch = result
                return self._persist_download(
                    manifest,
                    paths,
                    candidate,
                    validated,
                    fetch,
                )

        failure_status = _failure_status(attempts)
        if (
            self.config.require_open_access_evidence
            and not had_candidates
            and not _attempts_have_oa_evidence(attempts)
            and failure_status
            not in {"retryable_error", "too_large", "permission_required"}
        ):
            status = "not_open_access"
            message = "No resolver returned a PDF with open-access evidence"
        else:
            status = failure_status
            message = (
                "No valid PDF could be resolved through the enabled public "
                "or authenticated routes"
                if self.config.enable_dbpia_browser_download
                else "No public, valid PDF could be resolved without authentication"
            )
        manifest.update(
            {
                "status": status,
                "updated_at": _utc_now(),
                "last_error": {"code": status, "message": message},
            }
        )
        write_json_atomic(paths.download_manifest, manifest)
        return manifest

    def _download_provider_scoped(
        self,
        paper: PaperCandidate,
        paths: ArtifactPaths,
        manifest: dict[str, Any],
        *,
        previous: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Run only the provider selected by ``DownloadConfig.provider_scope``.

        This public configuration seam lets application orchestrators enforce
        provider ordering while retaining this class's lock, validation,
        interrupted-commit recovery, and atomic manifest/PDF commit policy.
        The historical ``provider_scope=None`` route never enters this method.
        """

        scope = self.config.provider_scope
        assert scope in DOWNLOAD_PROVIDER_SCOPES
        manifest["provider_scope"] = scope
        history: dict[str, Any] = {}
        if isinstance(previous, Mapping):
            raw_history = previous.get("provider_attempts")
            if isinstance(raw_history, Mapping):
                history = {
                    str(name): list(records)
                    for name, records in raw_history.items()
                    if isinstance(name, str) and isinstance(records, list)
                }
        attempts: list[dict[str, Any]] = manifest["attempts"]

        def persist(
            candidate: ResolverCandidate,
            validated: ValidatedPdf,
            fetch: HttpFetch | None,
            *,
            content_type: str | None = None,
        ) -> dict[str, Any]:
            history[scope] = list(attempts)
            manifest["provider_attempts"] = history
            return self._persist_download(
                manifest,
                paths,
                candidate,
                validated,
                fetch,
                content_type=content_type,
            )

        if scope == "kci":
            kci_journal_url = self.config.kci_journal_pdf_urls.get(
                paper.source_id
            )
            if kci_journal_url and self.config.require_open_access_evidence:
                attempts.append(
                    {
                        "resolver": "kci-journal-sitemap",
                        "url": redact_url(kci_journal_url),
                        "status": "skipped",
                        "error_code": "oa_evidence_required",
                    }
                )
            elif kci_journal_url:
                candidate = ResolverCandidate(
                    url=kci_journal_url,
                    resolver="kci-journal-sitemap",
                    landing_url=kci_journal_url.replace(
                        "/archive/articlePdf?",
                        "/archive/articleView?",
                    ),
                    is_open_access=None,
                    verified_source_id=paper.source_id,
                )
                result = self._fetch_pdf_candidate(candidate, paper, attempts)
                if result is not None:
                    validated, fetch = result
                    return persist(candidate, validated, fetch)
            else:
                attempts.append(
                    {
                        "resolver": "kci-journal-sitemap",
                        "status": "skipped",
                        "error_code": "not_in_public_sitemap",
                    }
                )

            if self.config.enable_kci_ui_download:
                result = self._try_kci_ui(paper, attempts)
                if result is not None:
                    validated, fetch, candidate = result
                    return persist(candidate, validated, fetch)
            if self.config.enable_kci_scienceon_download:
                result = self._try_kci_scienceon(paper, attempts)
                if result is not None:
                    validated, fetch, candidate = result
                    return persist(candidate, validated, fetch)
            if self.config.enable_spo_download:
                # The Supreme Prosecutors' Office publishes its two journals on a
                # public board; KCI only previews them.  Same resolver and identity
                # rules as the historical route, enabled by the operator.
                spo_result = self._try_spo_public_board(paper, attempts)
                if spo_result is not None:
                    validated, fetch, candidate = spo_result
                    return persist(candidate, validated, fetch)

        elif scope in {"unpaywall", "openalex"}:
            if not paper.doi:
                attempts.append(
                    {
                        "resolver": scope,
                        "status": "skipped",
                        "error_code": "identifier_unresolved",
                    }
                )
            else:
                if scope == "unpaywall":
                    if self.config.unpaywall_email:
                        candidates, _has_oa = self._unpaywall_candidates(
                            paper.doi,
                            attempts,
                        )
                    else:
                        candidates = []
                        attempts.append(
                            {
                                "resolver": scope,
                                "status": "skipped",
                                "error_code": "email_not_configured",
                            }
                        )
                else:
                    if self.config.openalex_api_key:
                        candidates, _has_oa = self._openalex_candidates(
                            paper.doi,
                            attempts,
                        )
                    else:
                        candidates = []
                        attempts.append(
                            {
                                "resolver": scope,
                                "status": "skipped",
                                "error_code": "api_key_not_configured",
                            }
                        )
                seen_urls: set[str] = set()
                for candidate in candidates:
                    key = urldefrag(candidate.url)[0]
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    result = self._fetch_pdf_candidate(
                        candidate,
                        paper,
                        attempts,
                    )
                    if result is not None:
                        validated, fetch = result
                        return persist(candidate, validated, fetch)

        elif scope == "semantic_scholar":
            self._record_semantic_scholar_resolution(paper, attempts)
            candidate = self.config.semantic_scholar_candidates.get(
                paper.source_id
            )
            if candidate is not None:
                result = self._fetch_pdf_candidate(candidate, paper, attempts)
                if result is not None:
                    validated, fetch = result
                    return persist(candidate, validated, fetch)

        elif scope == "dbpia":
            if not self.config.enable_dbpia_browser_download:
                attempts.append(
                    {
                        "resolver": DBPIA_BROWSER_RESOLVER_NAME,
                        "status": "skipped",
                        "error_code": "permission_required",
                    }
                )
            else:
                result = self._try_dbpia_authenticated_browser(
                    paper,
                    attempts,
                )
                if result is not None:
                    validated, candidate, content_type = result
                    return persist(
                        candidate,
                        validated,
                        None,
                        content_type=content_type,
                    )

        elif scope == "kyobo":
            if not self.config.enable_kyobo_browser_download:
                attempts.append(
                    {
                        "resolver": KYOBO_BROWSER_RESOLVER_NAME,
                        "status": "skipped",
                        "error_code": "permission_required",
                    }
                )
            else:
                result = self._try_kyobo_authenticated_browser(paper, attempts)
                if result is not None:
                    validated, candidate, content_type = result
                    return persist(
                        candidate,
                        validated,
                        None,
                        content_type=content_type,
                    )

        elif scope == "jams":
            public_resolution: KdfsResolution | None = None
            if self.config.enable_kdfs_download:
                public_resolution = self.kdfs_resolver.resolve(paper)
                result = self._try_kdfs_authorized(
                    paper,
                    attempts,
                    resolution=public_resolution,
                )
                if result is not None:
                    validated, fetch, candidate = result
                    return persist(candidate, validated, fetch)
                if public_resolution.status == "not_applicable":
                    public_resolution = None

            if not self.config.enable_jams_browser_download:
                attempts.append(
                    {
                        "resolver": JAMS_BROWSER_RESOLVER_NAME,
                        "status": "skipped",
                        "error_code": "permission_required",
                    }
                )
            else:
                browser_reference = (
                    public_resolution
                    if public_resolution is not None
                    and public_resolution.reason
                    in {
                        "kdfs_anonymous_download_not_advertised",
                        "official_kdfs_jams_exact_pdf_identity",
                    }
                    else None
                )
                result = self._try_jams_authenticated_browser(
                    paper,
                    attempts,
                    public_resolution=browser_reference,
                )
                if result is not None:
                    validated, candidate, content_type = result
                    return persist(
                        candidate,
                        validated,
                        None,
                        content_type=content_type,
                    )

        else:
            assert scope == "sciencedirect"
            result = self._try_elsevier_article_api(paper, attempts)
            if result is not None:
                validated, fetch, candidate = result
                return persist(candidate, validated, fetch)

        status = _failure_status(attempts)
        history[scope] = list(attempts)
        manifest.update(
            {
                "provider_attempts": history,
                "status": status,
                "updated_at": _utc_now(),
                "last_error": {
                    "code": status,
                    "message": f"No valid PDF was resolved by {scope}",
                },
            }
        )
        write_json_atomic(paths.download_manifest, manifest)
        return manifest

    def _run_dbpia_redownload_campaign(
        self,
        paper: PaperCandidate,
        paths: ArtifactPaths,
        *,
        previous: dict[str, Any] | None,
        current_artifact: bool,
    ) -> dict[str, Any]:
        """Run one DBpia-only refresh while retaining a usable old artifact.

        Resolver attempts remain in memory until a replacement has passed the
        common DBpia/PDF validators.  Therefore an ordinary browser failure can
        update only the small URL-free campaign audit when a current artifact
        already exists; its downloaded metadata and bytes remain untouched.
        """

        campaign = self._dbpia_browser_redownload_campaign
        assert campaign is not None
        if (
            current_artifact
            and previous is not None
            and _dbpia_redownload_campaign_completed(previous, campaign)
        ):
            current_manifest = self._upgrade_current_manifest(
                paper,
                paths,
                previous,
            )
            return {
                **current_manifest,
                "_runtime": {
                    "action": "redownload_reused",
                    "browser_attempted": False,
                },
            }

        started_at = _utc_now()
        manifest = self._base_manifest(paper)
        attempts: list[dict[str, Any]] = manifest["attempts"]
        browser_result = self._try_dbpia_authenticated_browser(
            paper,
            attempts,
        )
        browser_attempted = _dbpia_browser_was_attempted(
            attempts,
            resolver_configured=self.dbpia_browser_resolver is not None,
            open_access_only=self.config.require_open_access_evidence,
        )
        if browser_result is not None:
            validated, candidate, content_type = browser_result
            completed_at = _utc_now()
            audit: dict[str, Any] = {
                "campaign": campaign,
                "status": "replaced" if current_artifact else "downloaded",
                "started_at": started_at,
                "completed_at": completed_at,
                "browser_attempted": True,
                "replacement_sha256": hashlib.sha256(
                    validated.content
                ).hexdigest(),
            }
            if current_artifact and previous is not None:
                previous_pdf = previous.get("pdf")
                if isinstance(previous_pdf, dict) and isinstance(
                    previous_pdf.get("sha256"),
                    str,
                ):
                    audit["previous_sha256"] = previous_pdf["sha256"]
                # Keep the last valid manifest inside the temporary
                # `committing` record.  If the process stops before the PDF
                # replace, recovery can restore this exact manifest; if it
                # stops after the replace, recovery finalizes the new one.
                # The field is removed from every completed manifest.
                manifest[_DBPIA_BROWSER_REDOWNLOAD_ROLLBACK] = previous
            manifest[_DBPIA_BROWSER_REDOWNLOAD_AUDIT] = audit
            persisted = self._persist_download(
                manifest,
                paths,
                candidate,
                validated,
                None,
                content_type=content_type,
            )
            return {
                **persisted,
                "_runtime": {
                    "action": "redownloaded",
                    "browser_attempted": True,
                },
            }

        failure_status = (
            _dbpia_browser_failure_status(attempts)
            or _failure_status(attempts)
        )
        deferred = bool(
            failure_status == "retryable_error" and not browser_attempted
        )
        action = "redownload_deferred" if deferred else "redownload_failed"
        audit = {
            "campaign": campaign,
            "status": "deferred" if deferred else "failed",
            "started_at": started_at,
            "completed_at": _utc_now(),
            "browser_attempted": browser_attempted,
            "failure_status": failure_status,
        }
        runtime = {
            "action": action,
            "browser_attempted": browser_attempted,
            "failure_status": failure_status,
        }
        if current_artifact and previous is not None:
            preserved = dict(previous)
            preserved[_DBPIA_BROWSER_REDOWNLOAD_AUDIT] = audit
            write_json_atomic(paths.download_manifest, preserved)
            return {**preserved, "_runtime": runtime}

        manifest.update(
            {
                "status": failure_status,
                "updated_at": _utc_now(),
                "last_error": {
                    "code": failure_status,
                    "message": (
                        "The authenticated DBpia browser resolver could not "
                        "produce a project-validated PDF"
                    ),
                },
                _DBPIA_BROWSER_REDOWNLOAD_AUDIT: audit,
            }
        )
        write_json_atomic(paths.download_manifest, manifest)
        return {**manifest, "_runtime": runtime}

    def _try_spo_public_board(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        """Use one shared SPO board resolver before the DOI-less terminal.

        The standalone resolver already performs the strict issue and PDF
        content binding.  This integration intentionally runs the common
        project PDF validator again with ``require_identity=True`` before the
        existing atomic persistence path is allowed to commit any bytes.
        """

        if not _is_spo_paper(paper):
            return None

        # Enforce the policy at the downloader boundary as well as inside the
        # resolver.  Even a mistakenly injected, enabled resolver cannot make
        # a request while this config flag is false.
        if not self.config.enable_spo_download:
            attempts.append(
                {
                    "resolver": "spo-public-board",
                    "status": "skipped",
                    "resolution_status": "permission_required",
                    "reason": "spo_automated_copying_not_enabled",
                    "skipped_kci_links": 0,
                    "error_code": "permission_required",
                }
            )
            return None

        resolution = self.spo_resolver.resolve(paper)
        for stage_attempt in resolution.attempts:
            attempt: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage_attempt.stage}",
                "status": stage_attempt.status,
            }
            if stage_attempt.url:
                attempt["url"] = redact_url(stage_attempt.url)
            if stage_attempt.http_status is not None:
                attempt["http_status"] = stage_attempt.http_status
            if stage_attempt.final_url is not None:
                attempt["final_url"] = redact_url(stage_attempt.final_url)
            if stage_attempt.error_code is not None:
                attempt["error_code"] = stage_attempt.error_code
            if stage_attempt.detail is not None:
                attempt["detail"] = stage_attempt.detail
            attempts.append(attempt)

        fetch = resolution.fetch
        candidate = resolution.candidate
        attempt = {
            "resolver": resolution.resolver,
            "status": (
                "ok"
                if resolution.resolved
                else "failed"
                if resolution.status
                in {
                    "request_error",
                    "retryable_error",
                    "http_error",
                    "invalid_response",
                    "identity_mismatch",
                    "ambiguous",
                    "access_denied",
                }
                else "skipped"
            ),
            "resolution_status": resolution.status,
            "reason": resolution.reason,
            "skipped_kci_links": resolution.skipped_kci_links,
        }
        if resolution.error_code is not None:
            attempt["error_code"] = resolution.error_code
        if resolution.http_status is not None:
            attempt["http_status"] = resolution.http_status
        if resolution.post_url is not None:
            attempt["post_url"] = redact_url(resolution.post_url)
        if resolution.attachment_url is not None:
            attempt["attachment_url"] = redact_url(
                resolution.attachment_url
            )
        if resolution.archive_entry is not None:
            attempt["archive_entry"] = resolution.archive_entry
        if resolution.matched_title is not None:
            attempt["matched_title"] = resolution.matched_title
        if resolution.matched_doi is not None:
            attempt["matched_doi"] = resolution.matched_doi
        if fetch is not None:
            attempt.update(
                {
                    "url": redact_url(fetch.requested_url),
                    "final_url": redact_url(fetch.final_url),
                    "content_type": fetch.content_type or None,
                    "bytes": len(fetch.body),
                    "redirects": [
                        redact_url(url) for url in fetch.redirects
                    ],
                }
            )
        if not resolution.resolved or fetch is None or candidate is None:
            attempts.append(attempt)
            return None

        try:
            validated = validate_pdf(
                fetch.body,
                expected_doi=paper.doi,
                expected_title=paper.title or resolution.matched_title,
                require_identity=True,
            )
        except PdfValidationError as exc:
            attempt.update(
                {
                    "status": "failed",
                    "error_code": getattr(exc, "code", "invalid_pdf"),
                    "message": str(exc)[:500],
                }
            )
            attempts.append(attempt)
            return None

        attempt.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
            }
        )
        attempts.append(attempt)
        persisted_candidate = ResolverCandidate(
            url=candidate.url,
            resolver=candidate.resolver,
            landing_url=candidate.landing_url,
            license=candidate.license,
            version=(
                f"zip-entry:{resolution.archive_entry}"
                if resolution.archive_entry is not None
                else candidate.version
            ),
            is_open_access=candidate.is_open_access,
            verified_doi=candidate.verified_doi,
            verified_source_id=candidate.verified_source_id,
        )
        return validated, fetch, persisted_candidate

    def _record_semantic_scholar_resolution(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> None:
        record = self.config.semantic_scholar_resolutions.get(paper.source_id)
        if record is None:
            attempts.append(
                {
                    "resolver": "semantic-scholar-metadata",
                    "status": "skipped",
                    "error_code": "disabled_or_not_prepared",
                }
            )
            return
        resolution_status = str(record.get("status") or "invalid_response")
        metadata_http_status = record.get("http_status")
        transient_failure = (
            resolution_status in {
                "request_error",
                "invalid_response",
                "retryable_error",
            }
            or (
                resolution_status == "http_error"
                and isinstance(metadata_http_status, int)
                and (
                    metadata_http_status in {408, 425, 429}
                    or metadata_http_status >= 500
                )
            )
        )
        attempt: dict[str, Any] = {
            "resolver": "semantic-scholar-metadata",
            "status": (
                "ok"
                if resolution_status == "resolved"
                else "failed" if transient_failure else "skipped"
            ),
            "resolution_status": resolution_status,
            "reason": record.get("reason"),
            "cache_hit": bool(record.get("cache_hit")),
        }
        if transient_failure:
            attempt["error_code"] = "retryable_error"
        if metadata_http_status is not None:
            attempt["metadata_http_status"] = metadata_http_status
        if record.get("error_code") is not None:
            attempt["metadata_error_code"] = record.get("error_code")
        pdf = record.get("open_access_pdf")
        if isinstance(pdf, Mapping):
            attempt["is_open_access"] = True
            attempt["license"] = pdf.get("license")
            attempt["version"] = pdf.get("status")
            attempt["disclaimer"] = pdf.get("disclaimer")
        attempts.append(attempt)

    def _try_kicj_public_archive(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        if (
            _metadata_field_key(paper.journal_id) != "형사정책연구"
            or _metadata_field_key(paper.institution_id)
            != "한국형사법무정책연구원"
        ):
            return None
        resolution = self.kicj_resolver.resolve(paper)

        for stage_attempt in resolution.attempts:
            attempt: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage_attempt.stage}",
                "url": redact_url(stage_attempt.url),
                "status": stage_attempt.status,
            }
            if stage_attempt.http_status is not None:
                attempt["http_status"] = stage_attempt.http_status
            if stage_attempt.final_url is not None:
                attempt["final_url"] = redact_url(stage_attempt.final_url)
            if stage_attempt.error_code is not None:
                attempt["error_code"] = stage_attempt.error_code
            attempts.append(attempt)

        attempt = {
            "resolver": resolution.resolver,
            "status": (
                "ok"
                if resolution.resolved
                else "failed"
                if resolution.status
                in {
                    "request_error",
                    "retryable_error",
                    "http_error",
                    "invalid_response",
                    "identity_mismatch",
                    "ambiguous",
                }
                else "skipped"
            ),
            "resolution_status": resolution.status,
            "reason": resolution.reason,
            "archive_pages_checked": resolution.archive_pages_checked,
            "exact_title_matches": resolution.exact_title_matches,
            "author_matches": resolution.author_matches,
        }
        if resolution.http_status is not None:
            attempt["http_status"] = resolution.http_status
        if resolution.error_code is not None:
            attempt["error_code"] = resolution.error_code
        if resolution.detail_url is not None:
            attempt["detail_url"] = redact_url(resolution.detail_url)
        if resolution.matched_title is not None:
            attempt["matched_title"] = resolution.matched_title
        if resolution.matched_authors:
            attempt["matched_authors"] = list(resolution.matched_authors)
        attempts.append(attempt)
        if not resolution.resolved or resolution.candidate is None:
            return None

        result = self._fetch_pdf_candidate(
            resolution.candidate,
            paper,
            attempts,
        )
        if result is None:
            return None
        validated, fetch = result
        return validated, fetch, resolution.candidate

    def _try_police_public_board(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        """Consume the exact PDF response already fetched by the form POST."""

        if not _is_police_paper(paper):
            return None

        resolution = self.police_resolver.resolve(paper)
        for stage_attempt in resolution.attempts:
            attempt: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage_attempt.stage}",
                "status": stage_attempt.status,
            }
            if stage_attempt.url:
                attempt["url"] = redact_url(stage_attempt.url)
            if stage_attempt.http_status is not None:
                attempt["http_status"] = stage_attempt.http_status
            if stage_attempt.final_url is not None:
                attempt["final_url"] = redact_url(stage_attempt.final_url)
            if stage_attempt.error_code is not None:
                attempt["error_code"] = stage_attempt.error_code
            attempts.append(attempt)

        fetch = resolution.fetch
        candidate = resolution.candidate
        attempt: dict[str, Any] = {
            "resolver": resolution.resolver,
            "status": (
                "ok"
                if resolution.resolved
                else "failed"
                if resolution.status
                in {
                    "request_error",
                    "retryable_error",
                    "http_error",
                    "invalid_response",
                    "invalid_pdf",
                    "identity_mismatch",
                    "access_denied",
                }
                else "skipped"
            ),
            "resolution_status": resolution.status,
            "reason": resolution.reason,
            # Record how the official form established provenance, never the
            # hidden values or the server-side filename/article sequence.
            "form_provenance": "same-result-row-hidden-fields",
            "transport": "anonymous-form-post",
            "usage_notice": POLICE_INTERNAL_USE_NOTICE,
        }
        if resolution.error_code is not None:
            attempt["error_code"] = resolution.error_code
        if resolution.http_status is not None:
            attempt["http_status"] = resolution.http_status
        if resolution.matched_title is not None:
            attempt["matched_title"] = resolution.matched_title
        if resolution.matched_authors:
            attempt["matched_authors"] = list(resolution.matched_authors)
        if fetch is not None:
            attempt.update(
                {
                    "url": redact_url(fetch.requested_url),
                    "final_url": redact_url(fetch.final_url),
                    "content_type": fetch.content_type or None,
                    "bytes": len(fetch.body),
                    "redirects": [
                        redact_url(url) for url in fetch.redirects
                    ],
                }
            )
        if not resolution.resolved or fetch is None or candidate is None:
            attempts.append(attempt)
            return None

        try:
            validated = validate_pdf(
                fetch.body,
                expected_doi=paper.doi,
                expected_title=paper.title or resolution.matched_title,
                require_identity=True,
            )
        except PdfValidationError as exc:
            attempt.update(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "message": str(exc)[:500],
                }
            )
            attempts.append(attempt)
            return None

        attempt.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
            }
        )
        attempts.append(attempt)
        persisted_candidate = ResolverCandidate(
            url=candidate.url,
            resolver=candidate.resolver,
            landing_url=candidate.landing_url,
            # The public UI provides access, not a redistribution licence.
            license=None,
            version=candidate.version,
            is_open_access=candidate.is_open_access,
            verified_doi=candidate.verified_doi,
            verified_source_id=candidate.verified_source_id,
        )
        return validated, fetch, persisted_candidate

    def _try_dcs_public_reader(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        if not paper.doi or not paper.doi.casefold().startswith("10.9728/dcs."):
            return None
        if not self.config.allow_http:
            attempts.append(
                {
                    "resolver": "dcs-public-reader",
                    "status": "skipped",
                    "error_code": "http_not_explicitly_allowed",
                }
            )
            return None

        resolution = DcsPublicPdfResolver(
            self.http,
            allow_http=True,
        ).resolve(paper)
        for stage_attempt in resolution.attempts:
            attempt: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage_attempt.stage}",
                "url": stage_attempt.url,
                "status": stage_attempt.status,
            }
            if stage_attempt.http_status is not None:
                attempt["http_status"] = stage_attempt.http_status
            if stage_attempt.final_url is not None:
                attempt["final_url"] = stage_attempt.final_url
            if stage_attempt.error_code is not None:
                attempt["error_code"] = stage_attempt.error_code
            attempts.append(attempt)

        attempts.append(
            {
                "resolver": resolution.resolver,
                "status": (
                    "ok"
                    if resolution.resolved
                    else "failed"
                    if resolution.status
                    in {
                        "request_error",
                        "http_error",
                        "invalid_response",
                        "identity_mismatch",
                    }
                    else "skipped"
                ),
                "resolution_status": resolution.status,
                "reason": resolution.reason,
            }
        )
        if not resolution.resolved or resolution.candidate is None:
            return None

        result = self._fetch_pdf_candidate(
            resolution.candidate,
            paper,
            attempts,
        )
        if result is None:
            return None
        validated, fetch = result
        return validated, fetch, resolution.candidate

    def _try_jiis_public_search(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        if not paper.doi or not paper.doi.casefold().startswith("10.13088/jiis."):
            return None

        resolution = JiisPublicSearchResolver(http=self.http).resolve(paper)
        attempt: dict[str, Any] = {
            "resolver": resolution.resolver,
            "status": (
                "ok"
                if resolution.resolved
                else "failed"
                if resolution.status
                in {
                    "request_error",
                    "retryable_error",
                    "http_error",
                    "invalid_response",
                    "ambiguous",
                }
                else "skipped"
            ),
            "resolution_status": resolution.status,
            "reason": resolution.reason,
            "query_count": resolution.query_count,
            "exact_title_matches": resolution.exact_title_matches,
            "bibliographic_matches": resolution.bibliographic_matches,
        }
        if resolution.http_status is not None:
            attempt["http_status"] = resolution.http_status
        if resolution.error_code is not None:
            attempt["error_code"] = resolution.error_code
        if resolution.detail_url is not None:
            attempt["detail_url"] = redact_url(resolution.detail_url)
        attempts.append(attempt)
        if not resolution.resolved or resolution.candidate is None:
            return None

        result = self._fetch_pdf_candidate(
            resolution.candidate,
            paper,
            attempts,
        )
        if result is None:
            return None
        validated, fetch = result
        return validated, fetch, resolution.candidate

    def _try_elsevier_article_api(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        if not paper.doi or not paper.doi.casefold().startswith("10.1016/"):
            return None
        if not self.config.elsevier_api_key:
            attempts.append(
                {
                    "resolver": "elsevier-article-retrieval",
                    "status": "skipped",
                    "error_code": "api_key_not_configured",
                }
            )
            return None
        if self.config.require_open_access_evidence:
            attempts.append(
                {
                    "resolver": "elsevier-article-retrieval",
                    "status": "skipped",
                    "error_code": "oa_evidence_required",
                }
            )
            return None

        resolution = ElsevierArticleResolver(
            ElsevierResolverConfig(
                api_key=self.config.elsevier_api_key,
                institution_token=self.config.elsevier_institution_token,
                max_pdf_bytes=self.config.max_pdf_bytes,
                connect_timeout=self.config.connect_timeout,
                read_timeout=self.config.read_timeout,
                request_gate=self.config.request_gate,
            )
        ).resolve(paper)
        fetch = resolution.fetch
        attempt: dict[str, Any] = {
            "resolver": resolution.resolver,
            "status": (
                "ok"
                if resolution.resolved
                else "failed"
            ),
            "resolution_status": resolution.status,
            "reason": resolution.reason,
        }
        if resolution.http_status is not None:
            attempt["http_status"] = resolution.http_status
        if resolution.error_code is not None:
            attempt["error_code"] = resolution.error_code
        if fetch is not None:
            attempt.update(
                {
                    "url": redact_url(fetch.requested_url),
                    "final_url": redact_url(fetch.final_url),
                    "content_type": fetch.content_type or None,
                    "bytes": len(fetch.body),
                    "redirects": [
                        redact_url(url) for url in fetch.redirects
                    ],
                }
            )
        if not resolution.resolved or fetch is None:
            attempts.append(attempt)
            return None

        try:
            validated = validate_pdf(
                fetch.body,
                expected_doi=paper.doi,
                expected_title=paper.title,
                require_identity=True,
            )
        except PdfValidationError as exc:
            attempt.update(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "message": str(exc)[:500],
                }
            )
            attempts.append(attempt)
            return None
        attempt.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
            }
        )
        attempts.append(attempt)
        return (
            validated,
            fetch,
            ResolverCandidate(
                url=fetch.final_url,
                resolver=resolution.resolver,
                is_open_access=None,
            ),
        )

    def _try_kdfs_authorized(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
        *,
        resolution: KdfsResolution | None = None,
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        """Consume an already-validated anonymous KDFS JAMS fetch."""

        resolution = resolution or self.kdfs_resolver.resolve(paper)
        if resolution.status == "not_applicable":
            return None
        for stage in resolution.attempts:
            item: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage.stage}",
                "status": stage.status,
                "url": redact_url(stage.url),
            }
            if stage.http_status is not None:
                item["http_status"] = stage.http_status
            if stage.final_url is not None:
                item["final_url"] = redact_url(stage.final_url)
            if stage.error_code is not None:
                item["error_code"] = stage.error_code
            attempts.append(item)

        summary: dict[str, Any] = {
            "resolver": resolution.resolver,
            "status": "ok" if resolution.resolved else "skipped",
            "resolution_status": resolution.status,
            "reason": resolution.reason,
            "usage_notice": resolution.usage_notice,
        }
        if resolution.error_code is not None:
            summary["error_code"] = resolution.error_code
            if resolution.error_code == "retryable_error":
                summary["status"] = "failed"
        if resolution.detail_url is not None:
            summary["detail_url"] = redact_url(resolution.detail_url)
        if resolution.download_url is not None:
            summary["download_url"] = redact_url(resolution.download_url)
        if resolution.matched_title is not None:
            summary["matched_title"] = resolution.matched_title

        fetch = resolution.fetch
        candidate = resolution.candidate
        if not resolution.resolved or fetch is None or candidate is None:
            attempts.append(summary)
            return None
        try:
            validated = validate_pdf(
                fetch.body,
                expected_doi=paper.doi,
                expected_title=paper.title or resolution.matched_title,
                require_identity=True,
            )
        except PdfValidationError as exc:
            summary.update(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "message": str(exc)[:500],
                }
            )
            attempts.append(summary)
            return None
        summary.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
            }
        )
        attempts.append(summary)
        return validated, fetch, candidate

    def _try_dbpia_authenticated_browser(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, ResolverCandidate, str | None] | None:
        """Use a dedicated logged-in browser without retaining transport URLs."""

        landing_url = self.config.platform_landing_urls.get(paper.source_id)
        if not is_exact_dbpia_article_url(landing_url):
            # No API-derived NODE is required.  The authenticated browser
            # resolver searches the ordinary DBpia web UI by the paper's
            # database title and fails closed unless one exact result exists.
            landing_url = None
        if self.config.require_open_access_evidence:
            attempts.append(
                {
                    "resolver": DBPIA_BROWSER_RESOLVER_NAME,
                    "platform": "dbpia",
                    "status": "skipped",
                    "resolution_status": "not_open_access",
                    "reason": "subscription_access_is_not_open_access_evidence",
                    "error_code": "not_open_access",
                }
            )
            return None
        resolver = self.dbpia_browser_resolver
        if resolver is None:
            attempts.append(
                {
                    "resolver": DBPIA_BROWSER_RESOLVER_NAME,
                    "platform": "dbpia",
                    "status": "failed",
                    "resolution_status": "configuration_error",
                    "reason": "dbpia_browser_resolver_not_configured",
                    "error_code": "browser_dependency_missing",
                }
            )
            return None

        resolution = resolver.resolve(paper, landing_url=landing_url)
        for stage in resolution.attempts:
            item: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage.stage}",
                "status": stage.status,
            }
            if stage.error_code is not None:
                item["error_code"] = stage.error_code
            attempts.append(item)

        summary: dict[str, Any] = {
            "resolver": resolution.resolver,
            "platform": "dbpia",
            "status": "ok" if resolution.resolved else "failed",
            "resolution_status": resolution.status,
            "reason": resolution.reason,
        }
        if resolution.matched_title is not None:
            summary["matched_title"] = resolution.matched_title
        if resolution.matched_doi is not None:
            summary["matched_doi"] = resolution.matched_doi
        if resolution.identity_provenance:
            summary["identity_provenance"] = list(
                resolution.identity_provenance
            )
        if (
            paper.source_id
            in self.config.ephemeral_platform_landing_source_ids
        ):
            summary["landing_source"] = _DBPIA_OPENAPI_LANDING_SOURCE
        elif landing_url is None:
            summary["landing_source"] = "dbpia_browser_title_search"
        if resolution.error_code is not None:
            summary["error_code"] = resolution.error_code
        if not resolution.resolved or resolution.content is None:
            attempts.append(summary)
            return None
        title_search_identity_missing = bool(
            landing_url is None
            and (
                (
                    bool(paper.doi)
                    and (
                        (
                            not resolution.matched_doi
                            or resolution.matched_doi.casefold()
                            != paper.doi.casefold()
                            or "dbpia_web_title_search_doi_year_exact"
                            not in resolution.identity_provenance
                        )
                        and "dbpia_web_title_search_bibliography_exact"
                        not in resolution.identity_provenance
                    )
                )
                or (
                    not paper.doi
                    and "dbpia_web_title_search_bibliography_exact"
                    not in resolution.identity_provenance
                )
            )
        )
        doi_less_bibliography_missing = bool(
            not paper.doi
            and (
                not _DBPIA_BIBLIOGRAPHY_CORE_PROVENANCE.issubset(
                    resolution.identity_provenance
                )
                or (
                    (
                        "dbpia_landing_volume_issue_exact"
                        if "_" in (paper.issue_id or "")
                        else "dbpia_landing_issue_only_exact"
                    )
                    not in resolution.identity_provenance
                )
            )
        )
        if title_search_identity_missing or doi_less_bibliography_missing:
            summary.update(
                {
                    "status": "failed",
                    "resolution_status": "identity_mismatch",
                    "reason": (
                        "dbpia_title_search_missing_strict_identity_evidence"
                        if title_search_identity_missing
                        else "dbpia_missing_strict_bibliographic_identity"
                    ),
                    "error_code": "identity_mismatch",
                }
            )
            attempts.append(summary)
            return None
        if len(resolution.content) > self.config.max_pdf_bytes:
            summary.update(
                {
                    "status": "failed",
                    "error_code": "too_large",
                    "resolution_status": "too_large",
                }
            )
            attempts.append(summary)
            return None

        try:
            dbpia_identity = validate_dbpia_pdf_document(
                resolution.content,
                paper,
                landing_title=resolution.matched_title,
            )
            validated = validate_pdf(
                resolution.content,
                expected_doi=(
                    None
                    if dbpia_identity.status
                    == "doi_layout_and_title_match"
                    else paper.doi
                ),
                expected_title=resolution.matched_title or paper.title,
                require_identity=True,
            )
        except (PdfValidationError, DbpiaPdfIdentityError) as exc:
            summary.update(
                {
                    "status": "failed",
                    "error_code": getattr(exc, "code", "invalid_pdf"),
                    "resolution_status": "invalid_pdf",
                    "message": str(exc)[:500],
                }
            )
            attempts.append(summary)
            return None
        if dbpia_identity.status == "doi_layout_and_title_match":
            validated = ValidatedPdf(
                content=validated.content,
                page_count=validated.page_count,
                identity_status="dbpia_browser_doi_layout_title_match",
                matched_doi=paper.doi,
            )
            provenance = list(summary.get("identity_provenance") or [])
            provenance.extend(
                (
                    "dbpia_pdf_doi_layout_exact",
                    "dbpia_pdf_title_exact",
                )
            )
            summary["identity_provenance"] = list(
                dict.fromkeys(provenance)
            )

        summary.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
                "identity_status": validated.identity_status,
            }
        )
        attempts.append(summary)
        return (
            validated,
            ResolverCandidate(
                # This in-memory placeholder is never persisted.  The native
                # transport deliberately has no representable signed URL.
                url="https://www.dbpia.co.kr/",
                resolver=DBPIA_BROWSER_RESOLVER_NAME,
                landing_url=None,
                is_open_access=False,
            ),
            resolution.content_type,
        )

    def _try_kyobo_authenticated_browser(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, ResolverCandidate, str | None] | None:
        """Use Kyobo's exact rendered 원문저장 control without retaining URLs."""

        if self.config.require_open_access_evidence:
            attempts.append(
                {
                    "resolver": KYOBO_BROWSER_RESOLVER_NAME,
                    "platform": "kyobo",
                    "status": "skipped",
                    "resolution_status": "not_open_access",
                    "reason": "subscription_access_is_not_open_access_evidence",
                    "error_code": "not_open_access",
                }
            )
            return None
        resolver = self.kyobo_browser_resolver
        if resolver is None:
            attempts.append(
                {
                    "resolver": KYOBO_BROWSER_RESOLVER_NAME,
                    "platform": "kyobo",
                    "status": "failed",
                    "resolution_status": "configuration_error",
                    "reason": "kyobo_browser_resolver_not_configured",
                    "error_code": "browser_dependency_missing",
                }
            )
            return None

        landing_url = self.config.platform_landing_urls.get(paper.source_id)
        resolution = resolver.resolve(paper, landing_url=landing_url)
        for stage in resolution.attempts:
            item: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage.stage}",
                "status": stage.status,
            }
            if stage.error_code is not None:
                item["error_code"] = stage.error_code
            attempts.append(item)
        summary: dict[str, Any] = {
            "resolver": resolution.resolver,
            "platform": "kyobo",
            "status": "ok" if resolution.resolved else "failed",
            "resolution_status": resolution.status,
            "reason": resolution.reason,
        }
        if resolution.matched_title is not None:
            summary["matched_title"] = resolution.matched_title
        if resolution.matched_doi is not None:
            summary["matched_doi"] = resolution.matched_doi
        if resolution.identity_provenance:
            summary["identity_provenance"] = list(
                resolution.identity_provenance
            )
        if resolution.error_code is not None:
            summary["error_code"] = resolution.error_code
        if not resolution.resolved or resolution.content is None:
            attempts.append(summary)
            return None
        if not _kyobo_browser_resolution_identity_is_complete(
            paper,
            resolution,
        ):
            summary.update(
                {
                    "status": "failed",
                    "resolution_status": "invalid_pdf",
                    "error_code": "identity_mismatch",
                    "message": "Kyobo browser identity evidence is incomplete",
                }
            )
            attempts.append(summary)
            return None
        if len(resolution.content) > self.config.max_pdf_bytes:
            summary.update(
                {
                    "status": "failed",
                    "resolution_status": "too_large",
                    "error_code": "too_large",
                }
            )
            attempts.append(summary)
            return None
        try:
            validated = validate_pdf(
                resolution.content,
                # The exact Kyobo article page, its complete bibliography, the
                # unique visible save control and the native download event
                # already bind these bytes to one article.  Reparse structure
                # and title here without treating an issue-cover/reference DOI
                # as the article DOI.
                expected_doi=None,
                expected_title=resolution.matched_title or paper.title,
                require_identity=False,
            )
            if not (
                isinstance(resolution.landing_page_count, int)
                and resolution.landing_page_count > 0
            ):
                raise PdfValidationError(
                    "Kyobo PDF page count does not match the exact landing bibliography"
                )
            if validated.page_count == resolution.landing_page_count:
                pdf_page_provenance = "kyobo_pdf_page_count_exact"
            elif (
                abs(validated.page_count - resolution.landing_page_count) == 1
                and validated.identity_status
                in {"title_match", "metadata_title_match"}
            ):
                # A publisher PDF can omit an unnumbered cover counted by the
                # landing citation.  Permit one page only when the PDF itself
                # independently contains the exact landing title.
                pdf_page_provenance = (
                    "kyobo_pdf_page_count_off_by_one_title_exact"
                )
            else:
                raise PdfValidationError(
                    "Kyobo PDF page count does not match the exact landing bibliography"
                )
        except PdfValidationError as exc:
            summary.update(
                {
                    "status": "failed",
                    "resolution_status": "invalid_pdf",
                    "error_code": exc.code,
                    "message": str(exc)[:500],
                }
            )
            attempts.append(summary)
            return None
        provenance = list(summary.get("identity_provenance") or [])
        provenance.append(pdf_page_provenance)
        if validated.identity_status in {"title_match", "metadata_title_match"}:
            provenance.append("kyobo_pdf_title_exact")
        summary["identity_provenance"] = list(dict.fromkeys(provenance))
        summary["landing_page_count"] = resolution.landing_page_count
        validated = ValidatedPdf(
            content=validated.content,
            page_count=validated.page_count,
            identity_status=_KYOBO_BROWSER_IDENTITY_STATUS,
            matched_doi=resolution.matched_doi,
        )
        summary.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
                "identity_status": validated.identity_status,
            }
        )
        attempts.append(summary)
        return (
            validated,
            ResolverCandidate(
                url="https://scholar.kyobobook.co.kr/",
                resolver=KYOBO_BROWSER_RESOLVER_NAME,
                landing_url=None,
                is_open_access=False,
            ),
            resolution.content_type,
        )

    def _try_jams_authenticated_browser(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
        *,
        public_resolution: KdfsResolution | None = None,
    ) -> tuple[ValidatedPdf, ResolverCandidate, str | None] | None:
        """Use one exact rendered KDFS JAMS download control after login."""

        if self.config.require_open_access_evidence:
            attempts.append(
                {
                    "resolver": JAMS_BROWSER_RESOLVER_NAME,
                    "platform": "jams",
                    "status": "skipped",
                    "resolution_status": "not_open_access",
                    "reason": "subscription_access_is_not_open_access_evidence",
                    "error_code": "not_open_access",
                }
            )
            return None
        resolver = self.jams_browser_resolver
        if resolver is None:
            attempts.append(
                {
                    "resolver": JAMS_BROWSER_RESOLVER_NAME,
                    "platform": "jams",
                    "status": "failed",
                    "resolution_status": "configuration_error",
                    "reason": "jams_browser_resolver_not_configured",
                    "error_code": "browser_dependency_missing",
                }
            )
            return None

        resolution = resolver.resolve(
            paper,
            public_resolution=public_resolution,
        )
        for stage in resolution.attempts:
            item: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage.stage}",
                "status": stage.status,
            }
            if stage.error_code is not None:
                item["error_code"] = stage.error_code
            attempts.append(item)
        summary: dict[str, Any] = {
            "resolver": resolution.resolver,
            "platform": "jams",
            "status": "ok" if resolution.resolved else "failed",
            "resolution_status": resolution.status,
            "reason": resolution.reason,
        }
        if resolution.matched_title is not None:
            summary["matched_title"] = resolution.matched_title
        if resolution.matched_doi is not None:
            summary["matched_doi"] = resolution.matched_doi
        if resolution.identity_provenance:
            summary["identity_provenance"] = list(
                resolution.identity_provenance
            )
        if resolution.error_code is not None:
            summary["error_code"] = resolution.error_code
        if not resolution.resolved or resolution.content is None:
            attempts.append(summary)
            return None
        if len(resolution.content) > self.config.max_pdf_bytes:
            summary.update(
                {
                    "status": "failed",
                    "resolution_status": "too_large",
                    "error_code": "too_large",
                }
            )
            attempts.append(summary)
            return None
        if not _JAMS_BROWSER_PDF_IDENTITY_PROVENANCE.issubset(
            resolution.identity_provenance
        ):
            summary.update(
                {
                    "status": "failed",
                    "resolution_status": "invalid_pdf",
                    "error_code": "identity_mismatch",
                    "message": "JAMS PDF identity evidence is incomplete",
                }
            )
            attempts.append(summary)
            return None
        try:
            validated = validate_pdf(
                resolution.content,
                expected_doi=paper.doi,
                expected_title=paper.title or resolution.matched_title,
                # The JAMS resolver has already performed the complete
                # KDFS-specific identity check represented by the required
                # provenance above.  Reparse structure/page bounds here and
                # still reject an explicitly different DOI, but do not make
                # the generic two-page text extractor override that stronger
                # provider-specific evidence for Korean-font PDFs.
                require_identity=False,
            )
        except PdfValidationError as exc:
            summary.update(
                {
                    "status": "failed",
                    "resolution_status": "invalid_pdf",
                    "error_code": exc.code,
                    "message": str(exc)[:500],
                }
            )
            attempts.append(summary)
            return None
        validated = ValidatedPdf(
            content=validated.content,
            page_count=validated.page_count,
            identity_status=_JAMS_BROWSER_IDENTITY_STATUS,
            matched_doi=resolution.matched_doi or validated.matched_doi,
        )
        summary.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
                "identity_status": validated.identity_status,
            }
        )
        attempts.append(summary)
        return (
            validated,
            ResolverCandidate(
                url="https://kdfs.jams.or.kr/",
                resolver=JAMS_BROWSER_RESOLVER_NAME,
                landing_url=None,
                is_open_access=False,
            ),
            resolution.content_type,
        )

    def _try_authorized_platform(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        """Consume a platform resolver's one-use, already-fetched PDF."""

        landing_url = self.config.platform_landing_urls.get(paper.source_id)
        ephemeral_landing = (
            paper.source_id
            in self.config.ephemeral_platform_landing_source_ids
        )
        resolution = self.platform_resolver.resolve(
            paper,
            landing_url=landing_url,
        )
        if resolution.status in {"unsupported", "not_applicable"}:
            return None
        suppress_dbpia_identifiers = bool(
            ephemeral_landing
            or _is_dbpia_article_reference(landing_url)
            or resolution.platform == "dbpia"
        )
        for stage in resolution.attempts:
            item: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage.stage}",
                "status": stage.status,
            }
            if not suppress_dbpia_identifiers:
                item["url"] = redact_url(stage.url)
            if stage.http_status is not None:
                item["http_status"] = stage.http_status
            if (
                stage.final_url is not None
                and not suppress_dbpia_identifiers
            ):
                item["final_url"] = redact_url(stage.final_url)
            if stage.error_code is not None:
                item["error_code"] = stage.error_code
            attempts.append(item)

        summary: dict[str, Any] = {
            "resolver": resolution.resolver,
            "status": "ok" if resolution.resolved else "skipped",
            "resolution_status": resolution.status,
            "reason": resolution.reason,
        }
        if resolution.platform is not None:
            summary["platform"] = resolution.platform
        if (
            resolution.landing_url is not None
            and not suppress_dbpia_identifiers
        ):
            summary["landing_url"] = redact_url(resolution.landing_url)
        if resolution.matched_title is not None:
            summary["matched_title"] = resolution.matched_title
        if resolution.matched_doi is not None:
            summary["matched_doi"] = resolution.matched_doi
        if resolution.identity_status is not None:
            summary["identity_status"] = resolution.identity_status
        if resolution.identity_provenance:
            summary["identity_provenance"] = list(
                resolution.identity_provenance
            )
        if ephemeral_landing:
            # This non-secret marker lets schema upgrades scrub any manifest
            # produced by an older implementation that accidentally retained
            # the memory-only Open API landing identity.
            summary["landing_source"] = _DBPIA_OPENAPI_LANDING_SOURCE
        if resolution.error_code is not None:
            summary["error_code"] = resolution.error_code
            if resolution.error_code == "retryable_error":
                summary["status"] = "failed"

        fetch = resolution.fetch
        candidate = resolution.candidate
        if not resolution.resolved or fetch is None or candidate is None:
            attempts.append(summary)
            return None
        if suppress_dbpia_identifiers:
            candidate = ResolverCandidate(
                url=candidate.url,
                resolver=candidate.resolver,
                landing_url=None,
                license=candidate.license,
                version=candidate.version,
                is_open_access=candidate.is_open_access,
                verified_doi=candidate.verified_doi,
                verified_source_id=candidate.verified_source_id,
            )
        resolver_identity = False
        layout_identity = resolution.resolver_verified_doi_layout
        if resolution.resolver_verified_doi or layout_identity:
            verified_doi: str | None = None
            if candidate.verified_doi:
                try:
                    verified_doi = normalize_doi(candidate.verified_doi)
                except InvalidDoiError:
                    verified_doi = None
            resolver_identity = bool(
                verified_doi
                and paper.doi
                and verified_doi.casefold() == paper.doi.casefold()
                and resolution.matched_doi
                and resolution.matched_doi.casefold()
                == paper.doi.casefold()
            )
            if not resolver_identity:
                summary.update(
                    {
                        "status": "failed",
                        "error_code": "identity_mismatch",
                        "message": (
                            "Platform resolver-verified DOI does not match "
                            "the paper DOI"
                        ),
                    }
                )
                attempts.append(summary)
                return None
        try:
            validated = validate_pdf(
                fetch.body,
                expected_doi=None if layout_identity else paper.doi,
                # Prefer the exact title published by the already verified
                # platform landing.  KCI occasionally stores spacing defects
                # (for example ``앱아티팩트`` vs ``앱 아티팩트``); using the
                # landing title avoids rejecting the same document during the
                # downloader's independent second validation pass.
                expected_title=resolution.matched_title or paper.title,
                require_identity=layout_identity or not resolver_identity,
            )
        except PdfValidationError as exc:
            summary.update(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "message": str(exc)[:500],
                }
            )
            attempts.append(summary)
            return None
        if layout_identity:
            validated = ValidatedPdf(
                content=validated.content,
                page_count=validated.page_count,
                identity_status="resolver_doi_layout_title_match",
                matched_doi=paper.doi,
            )
        elif resolver_identity and validated.identity_status == "unconfirmed":
            validated = ValidatedPdf(
                content=validated.content,
                page_count=validated.page_count,
                identity_status="resolver_doi_match",
                matched_doi=paper.doi,
            )
        summary.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
            }
        )
        attempts.append(summary)
        return validated, fetch, candidate

    def _try_kci_scienceon(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        """Use the permission-gated KCI UI's official ScienceON lookup."""

        resolution: KciScienceOnResolution = (
            self.kci_scienceon_resolver.resolve(paper)
        )
        for stage in resolution.attempts:
            item: dict[str, Any] = {
                "resolver": f"{resolution.resolver}:{stage.stage}",
                "status": stage.status,
                "url": redact_url(stage.url),
            }
            if stage.http_status is not None:
                item["http_status"] = stage.http_status
            if stage.error_code is not None:
                item["error_code"] = stage.error_code
            attempts.append(item)

        summary: dict[str, Any] = {
            "resolver": resolution.resolver,
            "status": "ok" if resolution.resolved else "skipped",
            "resolution_status": resolution.status,
            "reason": resolution.reason,
            "returned_items": resolution.returned_items,
            "exact_matches": resolution.exact_matches,
            "fulltext_links": resolution.fulltext_links,
        }
        if resolution.matched_title is not None:
            summary["matched_title"] = resolution.matched_title
        if resolution.matched_year is not None:
            summary["matched_year"] = resolution.matched_year
        if resolution.error_code is not None:
            summary["error_code"] = resolution.error_code
            if resolution.error_code == "retryable_error":
                summary["status"] = "failed"
        attempts.append(summary)

        candidate = resolution.candidate
        if not resolution.resolved or candidate is None:
            return None
        result = self._fetch_pdf_candidate(candidate, paper, attempts)
        if result is None:
            return None
        validated, fetch = result
        return validated, fetch, candidate

    def _try_kci_ui(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch, ResolverCandidate] | None:
        """Use a KCI page's explicit download button, never a guessed file ID.

        KCI's generic robots policy currently disallows this automation. This
        method is therefore reachable only through the explicit permission
        opt-in in ``DownloadConfig``.
        """

        if not re.fullmatch(r"ART[0-9]+", paper.source_id):
            return None
        landing_url = (
            "https://www.kci.go.kr/kciportal/landing/article.kci?"
            + urlencode({"arti_id": paper.source_id})
        )
        try:
            landing = self.http.get_bytes(
                landing_url,
                max_bytes=HTML_LIMIT_BYTES,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except HttpClientError as exc:
            attempts.append(_attempt_error("kci-ui-landing", landing_url, exc))
            return None
        attempts.append(_attempt_from_fetch("kci-ui-landing", landing))
        if not (200 <= landing.status_code < 300) or not _looks_like_html(landing):
            return None
        document = landing.body.decode(_response_encoding(landing), errors="replace")
        doi_match = re.search(
            r"""<meta\s+[^>]*name=["']citation_doi["'][^>]*content=["']([^"']+)["']""",
            document,
            flags=re.IGNORECASE,
        )
        if paper.doi and (
            not doi_match
            or doi_match.group(1).strip().casefold() != paper.doi.casefold()
        ):
            attempts.append(
                {
                    "resolver": "kci-ui",
                    "url": redact_url(landing.final_url),
                    "status": "failed",
                    "error_code": "identity_mismatch",
                    "message": "KCI landing DOI does not match the DB DOI",
                }
            )
            return None
        button_match = re.search(
            r"""fncDown\(\s*['"](?P<sid>ART[0-9]+)['"]\s*,\s*"""
            r"""['"](?P<fid>KCI_FI[0-9]+)['"]\s*\)""",
            document,
            flags=re.IGNORECASE,
        )
        if not button_match or button_match.group("sid") != paper.source_id:
            attempts.append(
                {
                    "resolver": "kci-ui",
                    "url": redact_url(landing.final_url),
                    "status": "skipped",
                    "error_code": "not_public_on_kci",
                    "message": "No explicit KCI PDF download button was exposed",
                }
            )
            return None
        file_id = button_match.group("fid")
        statistics_url = (
            "https://www.kci.go.kr/kciportal/landing/updateArticleStatistics.kci"
        )
        try:
            statistics = self.http.post_form_bytes(
                statistics_url,
                data={
                    "landingLogBean.artiId": paper.source_id,
                    "landingLogBean.pdfCnt": "1",
                    "landingLogBean.printCnt": "0",
                },
                max_bytes=64 * 1024,
                headers={"Referer": landing.final_url},
            )
        except HttpClientError as exc:
            attempts.append(_attempt_error("kci-ui-statistics", statistics_url, exc))
            return None
        attempts.append(_attempt_from_fetch("kci-ui-statistics", statistics))
        if not (200 <= statistics.status_code < 300):
            return None
        download_url = (
            "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/"
            "ciSereArtiOrteServHistIFrame.kci?"
            + urlencode(
                {
                    "sereArticleSearchBean.artiId": paper.source_id,
                    "sereArticleSearchBean.orteFileId": file_id,
                }
            )
        )
        try:
            fetch = self.http.get_bytes(
                download_url,
                max_bytes=self.config.max_pdf_bytes,
                headers={
                    "Accept": "application/pdf,application/octet-stream;q=0.8,*/*;q=0.1",
                    "Referer": landing.final_url,
                },
            )
        except HttpClientError as exc:
            attempts.append(
                _redact_kci_file_ids_in_attempt(
                    _attempt_error("kci-ui", download_url, exc)
                )
            )
            return None
        attempt = _redact_kci_file_ids_in_attempt(
            _attempt_from_fetch("kci-ui", fetch)
        )
        if not (200 <= fetch.status_code < 300):
            attempts.append(attempt)
            return None
        doi_conflict_evidence: dict[str, Any] | None = None
        try:
            validated = validate_pdf(
                fetch.body,
                expected_doi=paper.doi,
                expected_title=paper.title,
                require_identity=False,
            )
        except PdfValidationError as exc:
            exact_file_binding = (
                file_id == f"KCI_FI{paper.source_id.removeprefix('ART')}"
            )
            if (
                paper.doi
                and exact_file_binding
                and str(exc) == _PDF_DIFFERENT_DOI_MESSAGE
            ):
                try:
                    validated, doi_conflict_evidence = (
                        _validate_kci_source_bound_doi_conflict(
                            fetch.body,
                            paper,
                        )
                    )
                except PdfValidationError:
                    validated = None
            else:
                validated = None
            if validated is None:
                attempt.update(
                    {
                        "status": "failed",
                        "error_code": exc.code,
                        "message": str(exc)[:500],
                    }
                )
                attempts.append(attempt)
                return None
        if validated.identity_status == "unconfirmed":
            validated = ValidatedPdf(
                content=validated.content,
                page_count=validated.page_count,
                identity_status="resolver_source_id_match",
                matched_doi=paper.doi,
                matched_source_id=paper.source_id,
            )
        attempt.update(
            {
                "status": "downloaded",
                "page_count": validated.page_count,
                "identity_status": validated.identity_status,
                "matched_source_id": validated.matched_source_id,
            }
        )
        if doi_conflict_evidence is not None:
            attempt.update(doi_conflict_evidence)
        attempts.append(attempt)
        return (
            validated,
            fetch,
            ResolverCandidate(
                url=download_url,
                resolver="kci-ui",
                landing_url=landing.final_url,
                is_open_access=True,
                verified_doi=paper.doi,
                verified_source_id=paper.source_id,
            ),
        )

    @staticmethod
    def _upgrade_current_manifest(
        paper: PaperCandidate,
        paths: ArtifactPaths,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Add backward-compatible provenance fields after full validation."""

        upgraded = dict(manifest)
        manifest_paper = manifest.get("paper")
        pdf = manifest.get("pdf")
        changed = manifest.get("schema_version") != DOWNLOAD_SCHEMA_VERSION
        upgraded["schema_version"] = DOWNLOAD_SCHEMA_VERSION
        if isinstance(manifest_paper, dict):
            upgraded_paper = dict(manifest_paper)
            if "doi_source" not in upgraded_paper:
                upgraded_paper["doi_source"] = paper.doi_source
                changed = True
            if "identifier_resolution" not in upgraded_paper:
                upgraded_paper["identifier_resolution"] = paper.identifier_resolution
                changed = True
            if upgraded_paper.get("alternate_titles") != list(
                paper.alternate_titles
            ):
                upgraded_paper["alternate_titles"] = list(
                    paper.alternate_titles
                )
                changed = True
            if _source_bound_identifier_transition(paper, manifest):
                upgraded_paper.update(
                    {
                        "doi_original": paper.doi_original,
                        "doi": paper.doi,
                        "doi_source": paper.doi_source,
                        "identifier_resolution": paper.identifier_resolution,
                    }
                )
                changed = True
            upgraded["paper"] = upgraded_paper
        if isinstance(pdf, dict) and "matched_source_id" not in pdf:
            upgraded_pdf = dict(pdf)
            upgraded_pdf["matched_source_id"] = None
            upgraded["pdf"] = upgraded_pdf
            changed = True
        upgraded, identifiers_scrubbed = _scrub_dbpia_identifier_manifest(
            upgraded
        )
        changed = changed or identifiers_scrubbed
        if changed:
            upgraded["updated_at"] = _utc_now()
            write_json_atomic(paths.download_manifest, upgraded)
        return upgraded

    @staticmethod
    def _persist_download(
        manifest: dict[str, Any],
        paths: ArtifactPaths,
        candidate: ResolverCandidate,
        validated: ValidatedPdf,
        fetch: HttpFetch | None,
        *,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(validated.content).hexdigest()
        downloaded_at = _utc_now()
        suppress_dbpia_transport = bool(
            candidate.resolver == DBPIA_BROWSER_RESOLVER_NAME
            or candidate.resolver.startswith(
                "authorized-platform-public-ui:dbpia"
            )
        )
        download_url = (
            redact_url(fetch.final_url)
            if fetch is not None and not suppress_dbpia_transport
            else None
        )
        if candidate.resolver == "kci-ui" and download_url is not None:
            download_url = _redact_kci_file_ids(download_url)
        manifest.update(
            {
                # Commit marker is written first. If the process dies after the
                # PDF replace, the next run can verify and finalize it.
                "status": "committing",
                "resolver": {
                    "name": candidate.resolver,
                    "is_open_access": candidate.is_open_access,
                    "license": candidate.license,
                    "version": candidate.version,
                    "landing_url": (
                        redact_url(candidate.landing_url)
                        if candidate.landing_url
                        and not suppress_dbpia_transport
                        else None
                    ),
                    "download_url": download_url,
                },
                "pdf": {
                    "path": paths.pdf.name,
                    "sha256": digest,
                    "bytes": len(validated.content),
                    "page_count": validated.page_count,
                    "identity_status": validated.identity_status,
                    "matched_doi": validated.matched_doi,
                    "matched_source_id": validated.matched_source_id,
                    "content_type": (
                        fetch.content_type or None
                        if fetch is not None
                        else content_type
                    ),
                },
                "downloaded_at": downloaded_at,
                "updated_at": downloaded_at,
                "last_error": None,
            }
        )
        write_json_atomic(paths.download_manifest, manifest)
        write_bytes_atomic(paths.pdf, validated.content)
        if sha256_file(paths.pdf) != digest:
            raise PdfValidationError("PDF checksum changed during atomic commit")
        manifest["status"] = "downloaded"
        manifest.pop(_DBPIA_BROWSER_REDOWNLOAD_ROLLBACK, None)
        manifest["updated_at"] = _utc_now()
        write_json_atomic(paths.download_manifest, manifest)
        return manifest

    @staticmethod
    def _recover_interrupted_commit(
        paper: PaperCandidate,
        paths: ArtifactPaths,
        manifest: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not manifest or manifest.get("status") != "committing":
            return None
        rollback = manifest.get(_DBPIA_BROWSER_REDOWNLOAD_ROLLBACK)
        if isinstance(rollback, dict):
            committed = dict(manifest)
            committed.pop(_DBPIA_BROWSER_REDOWNLOAD_ROLLBACK, None)
            committed["status"] = "downloaded"
            if download_artifact_is_current(paper, paths, committed):
                committed["updated_at"] = _utc_now()
                write_json_atomic(paths.download_manifest, committed)
                return PdfDownloader._upgrade_current_manifest(
                    paper,
                    paths,
                    committed,
                )
            if download_artifact_is_current(paper, paths, rollback):
                restored = dict(rollback)
                write_json_atomic(paths.download_manifest, restored)
                return PdfDownloader._upgrade_current_manifest(
                    paper,
                    paths,
                    restored,
                )
            return None
        # Pre-rollback manifests are recoverable only if the exact prospective
        # downloaded record passes today's complete artifact policy.  This is
        # deliberately fail-closed for old DOI-less DBpia records that lack a
        # durable bibliography fingerprint or exact author provenance.
        committed = dict(manifest)
        committed["status"] = "downloaded"
        if not download_artifact_is_current(paper, paths, committed):
            return None
        committed["updated_at"] = _utc_now()
        write_json_atomic(paths.download_manifest, committed)
        return PdfDownloader._upgrade_current_manifest(
            paper,
            paths,
            committed,
        )

    def _candidate_batches(
        self,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ):
        assert paper.doi is not None
        known_open_access = False
        preverified = list(
            self.config.preverified_candidates.get(paper.source_id, ())
        )
        if self.config.require_open_access_evidence:
            eligible: list[ResolverCandidate] = []
            for candidate in preverified:
                if candidate.is_open_access is True:
                    eligible.append(candidate)
                    continue
                attempts.append(
                    {
                        "resolver": candidate.resolver,
                        "url": redact_url(candidate.url),
                        "status": "skipped",
                        "error_code": "oa_evidence_required",
                    }
                )
            preverified = eligible
        if preverified:
            known_open_access = any(
                candidate.is_open_access is True for candidate in preverified
            )
            yield preverified
        semantic_scholar = self.config.semantic_scholar_candidates.get(
            paper.source_id
        )
        if semantic_scholar is not None:
            known_open_access = (
                known_open_access or semantic_scholar.is_open_access is True
            )
            yield [semantic_scholar]
        if self.config.unpaywall_email:
            candidates, has_oa_evidence = self._unpaywall_candidates(
                paper.doi,
                attempts,
            )
            known_open_access = known_open_access or has_oa_evidence
            yield candidates
        else:
            attempts.append(
                {
                    "resolver": "unpaywall",
                    "status": "skipped",
                    "error_code": "email_not_configured",
                }
            )
        if self.config.openalex_api_key:
            candidates, has_oa_evidence = self._openalex_candidates(
                paper.doi,
                attempts,
            )
            known_open_access = known_open_access or has_oa_evidence
            yield candidates
        else:
            attempts.append(
                {
                    "resolver": "openalex",
                    "status": "skipped",
                    "error_code": "api_key_not_configured",
                }
            )

        crossref_candidates, crossref_has_open_license = self._crossref_candidates(
            paper.doi,
            attempts,
        )
        known_open_access = known_open_access or crossref_has_open_license
        yield crossref_candidates
        allow_unlicensed_landing = (
            not self.config.require_open_access_evidence or known_open_access
        )
        yield self._doi_landing_candidates(
            paper.doi,
            attempts,
            allow_unlicensed=allow_unlicensed_landing,
            known_open=known_open_access,
        )

    def _unpaywall_candidates(
        self,
        doi: str,
        attempts: list[dict[str, Any]],
    ) -> tuple[list[ResolverCandidate], bool]:
        query = urlencode({"email": self.config.unpaywall_email or ""})
        url = f"https://api.unpaywall.org/v2/{_quote_doi(doi)}?{query}"
        payload = self._fetch_json("unpaywall", url, attempts)
        has_oa_evidence = bool(payload and payload.get("is_oa") is True)
        if not has_oa_evidence:
            return [], False
        assert payload is not None
        _mark_latest_oa_evidence(attempts, "unpaywall")
        locations = _ordered_locations(
            payload.get("best_oa_location"),
            payload.get("oa_locations"),
        )
        results: list[ResolverCandidate] = []
        landing_locations: list[tuple[str, str | None, str | None]] = []
        for location in locations:
            pdf_url = _nonempty_string(location.get("url_for_pdf"))
            landing_url = _nonempty_string(
                location.get("url_for_landing_page") or location.get("url")
            )
            if not pdf_url:
                if landing_url:
                    landing_locations.append(
                        (
                            landing_url,
                            _nonempty_string(location.get("license")),
                            _nonempty_string(location.get("version")),
                        )
                    )
                continue
            results.append(
                ResolverCandidate(
                    url=pdf_url,
                    resolver="unpaywall",
                    landing_url=landing_url,
                    license=_nonempty_string(location.get("license")),
                    version=_nonempty_string(location.get("version")),
                    is_open_access=True,
                )
            )
        if not results:
            results.extend(
                self._oa_landing_candidates(
                    resolver="unpaywall",
                    doi=doi,
                    locations=landing_locations,
                    attempts=attempts,
                )
            )
        return (
            sorted(results, key=lambda item: _version_rank(item.version)),
            True,
        )

    def _openalex_candidates(
        self,
        doi: str,
        attempts: list[dict[str, Any]],
    ) -> tuple[list[ResolverCandidate], bool]:
        identifier = f"https://doi.org/{doi}"
        query = urlencode({"api_key": self.config.openalex_api_key or ""})
        url = f"https://api.openalex.org/works/{quote(identifier, safe=':/._-')}?{query}"
        payload = self._fetch_json("openalex", url, attempts)
        if not payload:
            return [], False
        best_location = payload.get("best_oa_location")
        raw_locations: list[Mapping[str, Any]] = []
        if isinstance(best_location, Mapping):
            raw_locations.append(best_location)
        payload_locations = payload.get("locations")
        if isinstance(payload_locations, list):
            raw_locations.extend(
                location
                for location in payload_locations
                if isinstance(location, Mapping)
            )
        locations = _ordered_locations(
            best_location,
            payload_locations,
        )
        open_access = payload.get("open_access")
        has_oa_evidence = bool(
            payload.get("is_oa") is True
            or (
                isinstance(open_access, Mapping)
                and open_access.get("is_oa") is True
            )
            or any(
                location.get("is_oa") is True
                for location in raw_locations
            )
        )
        if has_oa_evidence:
            _mark_latest_oa_evidence(attempts, "openalex")
        results: list[ResolverCandidate] = []
        landing_locations: list[tuple[str, str | None, str | None]] = []
        for location in locations:
            is_best_oa_location = (
                isinstance(best_location, Mapping)
                and location is best_location
                and has_oa_evidence
            )
            if (
                location.get("is_oa") is not True
                and not is_best_oa_location
            ):
                continue
            pdf_url = _nonempty_string(location.get("pdf_url"))
            landing_url = _nonempty_string(location.get("landing_page_url"))
            if not pdf_url:
                if landing_url:
                    landing_locations.append(
                        (
                            landing_url,
                            _nonempty_string(location.get("license")),
                            _nonempty_string(location.get("version")),
                        )
                    )
                continue
            results.append(
                ResolverCandidate(
                    url=pdf_url,
                    resolver="openalex",
                    landing_url=landing_url,
                    license=_nonempty_string(location.get("license")),
                    version=_nonempty_string(location.get("version")),
                    is_open_access=True,
                )
            )
        if not results:
            results.extend(
                self._oa_landing_candidates(
                    resolver="openalex",
                    doi=doi,
                    locations=landing_locations,
                    attempts=attempts,
                )
            )
        return (
            sorted(results, key=lambda item: _version_rank(item.version)),
            has_oa_evidence,
        )

    def _oa_landing_candidates(
        self,
        *,
        resolver: str,
        doi: str,
        locations: list[tuple[str, str | None, str | None]],
        attempts: list[dict[str, Any]],
    ) -> list[ResolverCandidate]:
        """Resolve only explicit scholarly PDF metadata on OA landing pages.

        These URLs come from an OA API response for the exact DOI. Generic
        anchors are deliberately ignored because they frequently refer to
        supplements, issue files, purchases, or login workflows.
        """

        landing_resolver = f"{resolver}-landing"
        results: list[ResolverCandidate] = []
        seen_landings: set[str] = set()
        for landing_url, license_value, version in locations:
            landing_key = urldefrag(landing_url)[0].casefold()
            if not landing_key or landing_key in seen_landings:
                continue
            seen_landings.add(landing_key)
            try:
                fetch = self.http.get_bytes(
                    landing_url,
                    max_bytes=self.config.max_pdf_bytes,
                    max_html_bytes=HTML_LIMIT_BYTES,
                    headers={
                        "Accept": (
                            "text/html,application/xhtml+xml,"
                            "application/pdf;q=0.5"
                        )
                    },
                )
            except HttpClientError as exc:
                attempts.append(
                    _attempt_error(landing_resolver, landing_url, exc)
                )
                continue
            attempts.append(_attempt_from_fetch(landing_resolver, fetch))
            if fetch.status_code < 200 or fetch.status_code >= 300:
                continue
            if _looks_like_pdf(fetch.body):
                results.append(
                    ResolverCandidate(
                        url=fetch.final_url,
                        resolver=landing_resolver,
                        landing_url=landing_url,
                        license=license_value,
                        version=version,
                        is_open_access=True,
                    )
                )
                continue
            if not _looks_like_html(fetch):
                continue
            parser = _ScholarlyHtmlParser()
            try:
                parser.feed(
                    fetch.body.decode(
                        _response_encoding(fetch),
                        errors="replace",
                    )
                )
            except Exception:
                continue
            verified_doi: str | None = None
            if parser.citation_dois:
                landing_dois: list[str | None] = []
                for citation_doi in parser.citation_dois:
                    try:
                        landing_dois.append(normalize_doi(citation_doi))
                    except InvalidDoiError:
                        landing_dois.append(None)
                if any(
                    not landing_doi
                    or landing_doi.casefold() != doi.casefold()
                    for landing_doi in landing_dois
                ):
                    attempts.append(
                        {
                            "resolver": landing_resolver,
                            "url": redact_url(fetch.final_url),
                            "status": "failed",
                            "error_code": "identity_mismatch",
                            "message": (
                                "OA landing citation DOI does not match the "
                                "requested DOI"
                            ),
                        }
                    )
                    continue
                verified_doi = doi
            results.extend(
                ResolverCandidate(
                    url=pdf_url,
                    resolver=landing_resolver,
                    landing_url=fetch.final_url,
                    license=license_value,
                    version=version,
                    is_open_access=True,
                    verified_doi=verified_doi,
                )
                for pdf_url in parser.standard_pdf_urls(fetch.final_url)
            )
        return _deduplicate_candidates(results)

    def _crossref_candidates(
        self,
        doi: str,
        attempts: list[dict[str, Any]],
    ) -> tuple[list[ResolverCandidate], bool]:
        query = ""
        if self.config.crossref_email:
            query = "?" + urlencode({"mailto": self.config.crossref_email})
        url = f"https://api.crossref.org/works/{quote(doi, safe='')}{query}"
        payload = self._fetch_json("crossref", url, attempts)
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return [], False
        licenses = message.get("license")
        open_license = _active_open_license(licenses)
        is_open = open_license is not None
        if is_open:
            _mark_latest_oa_evidence(attempts, "crossref")
        results: list[ResolverCandidate] = []
        links = message.get("link")
        if not isinstance(links, list):
            return results, is_open
        for link in links:
            if not isinstance(link, dict):
                continue
            content_type = str(link.get("content-type") or "").casefold()
            pdf_url = _nonempty_string(link.get("URL"))
            if "pdf" not in content_type or not pdf_url:
                continue
            # Crossref links can be TDM endpoints whose presence does not
            # imply public access. Only follow them with an active open
            # license; DOI publisher metadata remains the public fallback.
            if not is_open:
                attempts.append(
                    {
                        "resolver": "crossref",
                        "url": redact_url(pdf_url),
                        "status": "skipped",
                        "error_code": "license_required",
                    }
                )
                continue
            results.append(
                ResolverCandidate(
                    url=pdf_url,
                    resolver="crossref",
                    landing_url=_nonempty_string(message.get("URL")),
                    license=open_license,
                    version=None,
                    is_open_access=True if is_open else None,
                )
            )
        return results, is_open

    def _doi_landing_candidates(
        self,
        doi: str,
        attempts: list[dict[str, Any]],
        *,
        allow_unlicensed: bool,
        known_open: bool,
    ) -> list[ResolverCandidate]:
        if not allow_unlicensed:
            attempts.append(
                {
                    "resolver": "doi-landing",
                    "status": "skipped",
                    "error_code": "oa_evidence_required",
                }
            )
            return []
        url = f"https://doi.org/{_quote_doi(doi)}"
        try:
            fetch = self.http.get_bytes(
                url,
                # A DOI may resolve directly to a multi-megabyte PDF. HTML is
                # still bounded by the same global artifact cap.
                max_bytes=self.config.max_pdf_bytes,
                max_html_bytes=HTML_LIMIT_BYTES,
                headers={"Accept": "text/html,application/xhtml+xml,application/pdf;q=0.5"},
            )
        except HttpClientError as exc:
            attempts.append(_attempt_error("doi-landing", url, exc))
            return []
        attempts.append(_attempt_from_fetch("doi-landing", fetch))
        if fetch.status_code < 200 or fetch.status_code >= 300:
            return []
        if _looks_like_pdf(fetch.body):
            return [
                ResolverCandidate(
                    url=fetch.final_url,
                    resolver="doi-direct",
                    landing_url=url,
                    is_open_access=True if known_open else None,
                )
            ]
        if not _looks_like_html(fetch):
            return []
        parser = _ScholarlyHtmlParser()
        try:
            document = fetch.body.decode(
                _response_encoding(fetch),
                errors="replace",
            )
            parser.feed(document)
        except Exception:
            return []
        verified_doi: str | None = None
        if parser.citation_doi:
            try:
                landing_doi = normalize_doi(parser.citation_doi)
            except InvalidDoiError:
                landing_doi = None
            if landing_doi and landing_doi.casefold() != doi.casefold():
                attempts.append(
                    {
                        "resolver": "doi-landing",
                        "url": redact_url(fetch.final_url),
                        "status": "failed",
                        "error_code": "identity_mismatch",
                        "message": "Publisher landing citation DOI does not match the DB DOI",
                    }
                )
                return []
            if landing_doi and landing_doi.casefold() == doi.casefold():
                verified_doi = doi
        results = [
            ResolverCandidate(
                url=candidate_url,
                resolver="publisher-metadata",
                landing_url=fetch.final_url,
                is_open_access=True if known_open else None,
                verified_doi=verified_doi,
            )
            for candidate_url in parser.pdf_urls(fetch.final_url)
        ]
        for candidate_url in _trusted_publisher_pdf_urls(parser, fetch.final_url):
            results.append(
                ResolverCandidate(
                    url=candidate_url,
                    resolver="publisher-explicit-pdf-link",
                    landing_url=fetch.final_url,
                    is_open_access=True if known_open else None,
                    # The DOI resolver itself selected this exact article page,
                    # and the allowlisted page exposed the exact PDF link.
                    verified_doi=doi,
                )
            )
        ksbe_download_url = _ksbe_explicit_pdf_url(fetch.final_url, document)
        if ksbe_download_url:
            results.append(
                ResolverCandidate(
                    url=ksbe_download_url,
                    resolver="publisher-explicit-pdf-link",
                    landing_url=fetch.final_url,
                    is_open_access=True if known_open else None,
                    verified_doi=doi,
                )
            )
        drive_download_url = _google_drive_download_url(fetch.final_url, document)
        if drive_download_url:
            results.append(
                ResolverCandidate(
                    url=drive_download_url,
                    resolver="google-drive-explicit-download",
                    landing_url=fetch.final_url,
                    is_open_access=True if known_open else None,
                    verified_doi=doi,
                )
            )
        return results

    def _fetch_json(
        self,
        resolver: str,
        url: str,
        attempts: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        try:
            fetch = self.http.get_bytes(
                url,
                max_bytes=METADATA_LIMIT_BYTES,
                headers={"Accept": "application/json"},
            )
        except HttpClientError as exc:
            attempts.append(_attempt_error(resolver, url, exc))
            return None
        attempts.append(_attempt_from_fetch(resolver, fetch))
        if fetch.status_code < 200 or fetch.status_code >= 300:
            return None
        try:
            payload = json.loads(fetch.body)
        except (UnicodeError, json.JSONDecodeError):
            attempts.append(
                {
                    "resolver": resolver,
                    "url": redact_url(fetch.final_url),
                    "status": "failed",
                    "error_code": "invalid_metadata_json",
                }
            )
            return None
        return payload if isinstance(payload, dict) else None

    def _fetch_pdf_candidate(
        self,
        candidate: ResolverCandidate,
        paper: PaperCandidate,
        attempts: list[dict[str, Any]],
    ) -> tuple[ValidatedPdf, HttpFetch] | None:
        verified_doi: str | None = None
        if candidate.verified_doi:
            try:
                verified_doi = normalize_doi(candidate.verified_doi)
            except InvalidDoiError:
                verified_doi = None
            if (
                not verified_doi
                or not paper.doi
                or verified_doi.casefold() != paper.doi.casefold()
            ):
                attempts.append(
                    {
                        "resolver": candidate.resolver,
                        "url": redact_url(candidate.url),
                        "status": "failed",
                        "error_code": "identity_mismatch",
                        "message": (
                            "Preverified candidate DOI does not match the "
                            "paper DOI"
                        ),
                    }
                )
                return None
        if (
            candidate.verified_source_id
            and candidate.verified_source_id != paper.source_id
        ):
            attempts.append(
                {
                    "resolver": candidate.resolver,
                    "url": redact_url(candidate.url),
                    "status": "failed",
                    "error_code": "identity_mismatch",
                    "message": (
                        "Preverified candidate source id does not match the "
                        "paper source id"
                    ),
                }
            )
            return None
        try:
            fetch = self.http.get_bytes(
                candidate.url,
                max_bytes=self.config.max_pdf_bytes,
                headers={"Accept": "application/pdf,application/octet-stream;q=0.8,*/*;q=0.1"},
            )
        except HttpClientError as exc:
            attempts.append(_attempt_error(candidate.resolver, candidate.url, exc))
            return None
        attempt = _attempt_from_fetch(candidate.resolver, fetch)
        if fetch.status_code < 200 or fetch.status_code >= 300:
            attempts.append(attempt)
            return None
        resolver_identity = bool(
            verified_doi
            and paper.doi
            and verified_doi.casefold() == paper.doi.casefold()
        )
        source_identity = bool(
            candidate.verified_source_id
            and candidate.verified_source_id == paper.source_id
        )
        try:
            validated = validate_pdf(
                fetch.body,
                expected_doi=paper.doi,
                expected_title=paper.title,
                require_identity=not (resolver_identity or source_identity),
            )
        except PdfValidationError as exc:
            attempt.update(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "message": str(exc)[:500],
                }
            )
            attempts.append(attempt)
            return None
        if (
            (resolver_identity or source_identity)
            and validated.identity_status == "unconfirmed"
        ):
            validated = ValidatedPdf(
                content=validated.content,
                page_count=validated.page_count,
                identity_status=(
                    "resolver_doi_match"
                    if resolver_identity
                    else "resolver_source_id_match"
                ),
                matched_doi=paper.doi if resolver_identity else None,
                matched_source_id=(
                    paper.source_id if source_identity else None
                ),
            )
        attempt.update({"status": "downloaded", "page_count": validated.page_count})
        attempts.append(attempt)
        return validated, fetch

    @staticmethod
    def _base_manifest(paper: PaperCandidate) -> dict[str, Any]:
        now = _utc_now()
        return {
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
                "kci_landing_url": paper.landing_url,
                "publication_year": paper.publication_year,
                "bibliographic_identity": _paper_bibliographic_identity(
                    paper
                ),
            },
            "status": "resolving",
            "resolver": None,
            "pdf": None,
            "attempts": [],
            "started_at": now,
            "updated_at": now,
            "downloaded_at": None,
            "last_error": None,
        }

class _ScholarlyHtmlParser(HTMLParser):
    META_KEYS = {
        "citation_pdf_url",
        "eprints.document_url",
        "wkhealth_pdf_url",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url: str | None = None
        self.meta_urls: list[str] = []
        self.citation_pdf_urls: list[str] = []
        self.citation_doi: str | None = None
        self.citation_dois: list[str] = []
        self.link_urls: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        lowered_tag = tag.casefold()
        if lowered_tag == "base" and attributes.get("href") and self.base_url is None:
            self.base_url = attributes["href"]
        elif lowered_tag == "meta":
            key = (attributes.get("name") or attributes.get("property") or "").casefold()
            content = attributes.get("content", "").strip()
            if key in self.META_KEYS and content:
                self.meta_urls.append(content)
                if key == "citation_pdf_url":
                    self.citation_pdf_urls.append(content)
            elif key == "citation_doi" and content:
                self.citation_doi = content
                self.citation_dois.append(content)
        elif lowered_tag == "link":
            content_type = attributes.get("type", "").casefold()
            href = attributes.get("href", "").strip()
            if "application/pdf" in content_type and href:
                self.link_urls.append(href)
        elif lowered_tag == "a":
            self._anchor_href = attributes.get("href", "").strip() or None
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._anchor_href:
            self.anchors.append((self._anchor_href, " ".join(self._anchor_text).strip()))
            self._anchor_href = None
            self._anchor_text = []

    def pdf_urls(self, landing_url: str) -> list[str]:
        # Standard scholarly metadata is intentionally preferred over broad
        # "download" anchors, which frequently point at supplements/issues.
        return self._absolute_urls(
            [*self.meta_urls, *self.link_urls],
            landing_url,
        )

    def standard_pdf_urls(self, landing_url: str) -> list[str]:
        """Return only citation_pdf_url and typed PDF link metadata."""

        return self._absolute_urls(
            [*self.citation_pdf_urls, *self.link_urls],
            landing_url,
        )

    def _absolute_urls(
        self,
        values: list[str],
        landing_url: str,
    ) -> list[str]:
        base = urljoin(landing_url, self.base_url) if self.base_url else landing_url
        result: list[str] = []
        for value in values:
            absolute, _fragment = urldefrag(urljoin(base, html.unescape(value.strip())))
            if absolute.casefold().startswith(("https://", "http://")):
                result.append(absolute)
        return list(dict.fromkeys(result))


def _trusted_publisher_pdf_urls(
    parser: _ScholarlyHtmlParser,
    landing_url: str,
) -> list[str]:
    """Return only exact, allowlisted public PDF anchors.

    Broad "PDF" or "download" anchor following is intentionally avoided:
    those links often point to supplements, issue files, login endpoints, or
    purchases. These patterns were verified against publishers that expose a
    stable anonymous PDF anchor on the DOI-selected article page.
    """

    result: list[str] = []
    for href, _text in parser.anchors:
        absolute, _fragment = urldefrag(urljoin(landing_url, html.unescape(href)))
        parsed = urlsplit(absolute)
        if parsed.scheme.casefold() != "https":
            continue
        hostname = (parsed.hostname or "").casefold().removeprefix("www.")
        path = parsed.path
        trusted = False
        if hostname == "ksbe-jbe.org":
            match = re.fullmatch(r"/xml/([0-9]+)/([0-9]+)\.pdf", path)
            trusted = bool(match and match.group(1) == match.group(2))
        elif hostname == "hcisj.com":
            segments = path.split("/")
            trusted = bool(
                re.fullmatch(
                    r"/data/file/article/[A-Za-z0-9._/-]+\.pdf",
                    path,
                    flags=re.IGNORECASE,
                )
                and "." not in segments
                and ".." not in segments
            )
        if trusted:
            result.append(absolute)
    return list(dict.fromkeys(result))


def _ksbe_explicit_pdf_url(landing_url: str, document: str) -> str | None:
    """Extract KSBE-JBE's article-bound public PDF action.

    The publisher exposes its PDF through an inline ``window.open`` action
    rather than a normal anchor.  Accept it only when the DOI-selected landing
    page, availability flag, query ``aidx``, and both path identifiers agree.
    """

    parsed = urlsplit(landing_url)
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    if parsed.scheme.casefold() != "https" or hostname != "ksbe-jbe.org":
        return None
    query_ids = re.findall(r"(?:^|&)aidx=([0-9]+)(?:&|$)", parsed.query)
    if len(query_ids) != 1:
        return None
    article_id = query_ids[0]
    if not re.search(
        r"""\bvar\s+use_yn\s*=\s*["']Y["']\s*;""",
        document,
        flags=re.IGNORECASE,
    ):
        return None
    actions = re.findall(
        r"""window\.open\(\s*["']"""
        r"""/xml/(?P<directory>[0-9]+)/(?P<file>[0-9]+)\.pdf"""
        r"""["']\s*(?:,|\))""",
        document,
        flags=re.IGNORECASE,
    )
    matching = {
        directory
        for directory, file_id in actions
        if directory == file_id == article_id
    }
    if len(matching) != 1:
        return None
    return f"https://{parsed.hostname}/xml/{article_id}/{article_id}.pdf"


def _google_drive_download_url(landing_url: str, document: str) -> str | None:
    """Extract Google's explicit anonymous download action from a viewer page."""

    parsed = urlsplit(landing_url)
    if (parsed.hostname or "").casefold() != "drive.google.com":
        return None
    path_match = re.fullmatch(
        r"/file/d/(?P<file_id>[A-Za-z0-9_-]{10,})/view/?",
        parsed.path,
    )
    if not path_match:
        return None
    file_id = path_match.group("file_id")
    explicit_download = re.search(
        r"https://drive\.usercontent\.google\.com/uc\?"
        r"id\\u003d(?P<file_id>[A-Za-z0-9_-]{10,})"
        r"\\u0026export\\u003ddownload",
        document,
    )
    if not explicit_download or explicit_download.group("file_id") != file_id:
        return None
    return (
        "https://drive.usercontent.google.com/uc?"
        + urlencode({"id": file_id, "export": "download"})
    )


def _validate_kci_source_bound_doi_conflict(
    content: bytes,
    paper: PaperCandidate,
) -> tuple[ValidatedPdf, dict[str, Any]]:
    """Validate a KCI PDF whose printed DOI conflicts with KCI metadata.

    This helper is deliberately private and is called only after the KCI
    landing page has matched the database DOI and its explicit ``fncDown``
    control has bound the exact ART id to the exact exposed file id.  It does
    not relax generic PDF validation.
    """

    structural = validate_pdf(content)
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)
        first_page_text = reader.pages[0].extract_text() or ""
        identity_text = "\n".join(
            (reader.pages[index].extract_text() or "")
            for index in range(min(2, len(reader.pages)))
        )
        trailing_text = "\n".join(
            (reader.pages[index].extract_text() or "")
            for index in range(max(0, len(reader.pages) - 2), len(reader.pages))
        )
    except Exception as exc:
        raise PdfValidationError(
            f"unable to inspect source-bound KCI PDF identity: {exc}"
        ) from exc

    printed_dois = _extract_first_page_printed_dois(first_page_text)
    expected_doi = paper.doi.casefold() if paper.doi else None
    conflicting_dois = tuple(
        doi
        for doi in printed_dois
        if expected_doi is not None and doi.casefold() != expected_doi
    )
    if (
        expected_doi is None
        or not conflicting_dois
        or any(doi.casefold() == expected_doi for doi in printed_dois)
    ):
        raise PdfValidationError(
            "KCI DOI-conflict exception requires a distinct printed DOI"
        )

    matched_title = next(
        (
            title
            for title in (paper.title, *paper.alternate_titles)
            if title and _title_is_present(title, identity_text)
        ),
        None,
    )
    if matched_title is None:
        raise PdfValidationError(
            "KCI source-bound PDF does not contain an exact normalized title"
        )

    provenance = [
        "kci_landing_exact_database_doi",
        "kci_explicit_source_file_binding",
        "pdf_normalized_title_match",
    ]
    publication_year_match: bool | None = None
    if paper.publication_year is not None:
        publication_year_match = bool(
            re.search(
                rf"(?<![0-9]){re.escape(str(paper.publication_year))}(?![0-9])",
                identity_text,
            )
        )
        if not publication_year_match:
            raise PdfValidationError(
                "KCI source-bound PDF publication year does not match"
            )
        provenance.append("pdf_publication_year_match")

    author_match: bool | None = None
    if paper.authors:
        author_names = tuple(
            name
            for value in paper.authors
            if (name := value.split("(", 1)[0].strip())
        )
        normalized_identity = _normalized_title(identity_text)
        author_match = bool(author_names) and all(
            len(normalized := _normalized_title(name)) >= 2
            and normalized in normalized_identity
            for name in author_names
        )
        if not author_match:
            raise PdfValidationError(
                "KCI source-bound PDF author does not match"
            )
        provenance.append("pdf_author_match")

    first_page_match = _page_marker_is_present(
        paper.first_page,
        first_page_text,
    )
    final_page_match = _page_marker_is_present(
        paper.final_page,
        trailing_text,
    )
    if first_page_match is True and final_page_match is True:
        provenance.append("pdf_page_range_match")

    return (
        ValidatedPdf(
            content=structural.content,
            page_count=structural.page_count,
            identity_status=KCI_DOI_CONFLICT_IDENTITY_STATUS,
            matched_source_id=paper.source_id,
        ),
        {
            "conflicting_printed_dois": list(conflicting_dois),
            "identity_provenance": provenance,
            "publication_year_match": publication_year_match,
            "author_match": author_match,
            "first_page_match": first_page_match,
            "final_page_match": final_page_match,
        },
    )


def _extract_first_page_printed_dois(page_text: str) -> tuple[str, ...]:
    """Extract DOI text while repairing only digit-to-digit layout spaces."""

    values: list[str] = []
    for line in page_text.splitlines():
        compacted = re.sub(r"(?<=[0-9])[\t ]+(?=[0-9])", "", line)
        for value in _extract_dois(compacted):
            if value.casefold() not in {
                existing.casefold() for existing in values
            }:
                values.append(value)
    return tuple(values)


def _page_marker_is_present(
    expected_page: str | None,
    page_text: str,
) -> bool | None:
    if not expected_page or not expected_page.strip().isdigit():
        return None
    return bool(
        re.search(
            rf"(?<![0-9]){re.escape(expected_page.strip())}(?![0-9])",
            page_text,
        )
    )


def validate_pdf(
    content: bytes,
    *,
    expected_doi: str | None = None,
    expected_title: str | None = None,
    require_identity: bool = False,
) -> ValidatedPdf:
    marker = content.find(PDF_MAGIC, 0, 1024)
    if marker < 0:
        raise PdfValidationError("response does not contain a PDF header")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise PdfValidationError(
            "pypdf is required; install the project requirements"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise EncryptedPdfError("PDF is encrypted and has no empty-password access")
        page_count = len(reader.pages)
    except EncryptedPdfError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError) as exc:
        raise PdfValidationError(f"PDF parser rejected the response: {exc}") from exc
    if page_count < 1:
        raise PdfValidationError("PDF has no pages")
    if page_count > MAX_PDF_PAGES:
        raise PdfValidationError(
            f"PDF page count {page_count} exceeds safety limit {MAX_PDF_PAGES}"
        )
    identity_status = "not_checked"
    matched_doi: str | None = None
    if expected_doi or expected_title:
        try:
            page_text = "\n".join(
                (reader.pages[index].extract_text() or "")
                for index in range(min(2, page_count))
            )
            metadata_title = (
                str(getattr(reader.metadata, "title", "") or "")
                if reader.metadata is not None
                else ""
            )
        except Exception as exc:
            raise PdfValidationError(f"unable to inspect PDF identity: {exc}") from exc
        doi_values = _extract_dois(page_text)
        expected_key = expected_doi.casefold() if expected_doi else None
        if expected_key and (
            expected_key in page_text.casefold()
            or expected_key in {value.casefold() for value in doi_values}
        ):
            identity_status = "doi_match"
            matched_doi = expected_doi
        elif expected_key and doi_values:
            raise PdfValidationError(_PDF_DIFFERENT_DOI_MESSAGE)
        elif expected_title and _title_is_present(expected_title, page_text):
            identity_status = "title_match"
        elif expected_title and _titles_equivalent(expected_title, metadata_title):
            identity_status = "metadata_title_match"
        elif require_identity:
            raise PdfValidationError(
                "PDF identity could not be confirmed from its DOI or title"
            )
        else:
            identity_status = "unconfirmed"
    return ValidatedPdf(
        content=content,
        page_count=page_count,
        identity_status=identity_status,
        matched_doi=matched_doi,
    )


def _attempt_from_fetch(resolver: str, fetch: HttpFetch) -> dict[str, Any]:
    status = "ok" if 200 <= fetch.status_code < 300 else "http_error"
    return {
        "resolver": resolver,
        "url": redact_url(fetch.requested_url),
        "final_url": redact_url(fetch.final_url),
        "status": status,
        "http_status": fetch.status_code,
        "content_type": fetch.content_type or None,
        "bytes": len(fetch.body),
        "redirects": [redact_url(url) for url in fetch.redirects],
    }


_KCI_FILE_ID_RE = re.compile(r"KCI_FI[0-9]+", re.IGNORECASE)


def _redact_kci_file_ids(value: str) -> str:
    """Mask KCI file identifiers in persisted provenance URLs."""

    return _KCI_FILE_ID_RE.sub("%5BREDACTED%5D", value)


def _redact_kci_file_ids_in_attempt(
    attempt: dict[str, Any],
) -> dict[str, Any]:
    redacted = dict(attempt)
    for key in ("url", "final_url"):
        value = redacted.get(key)
        if isinstance(value, str):
            redacted[key] = _redact_kci_file_ids(value)
    redirects = redacted.get("redirects")
    if isinstance(redirects, list):
        redacted["redirects"] = [
            _redact_kci_file_ids(value) if isinstance(value, str) else value
            for value in redirects
        ]
    return redacted


def _attempt_error(resolver: str, url: str, exc: BaseException) -> dict[str, Any]:
    return {
        "resolver": resolver,
        "url": redact_url(url),
        "status": "failed",
        "error_code": getattr(exc, "code", "request_error"),
        "message": _safe_error_message(exc),
    }


def _safe_error_message(exc: BaseException) -> str:
    """Retain diagnostics without persisting credentials from request URLs."""

    def replace_url(match: re.Match[str]) -> str:
        try:
            return redact_url(match.group(0))
        except (TypeError, ValueError):
            return "[REDACTED_URL]"

    message = _URL_IN_ERROR_RE.sub(replace_url, str(exc))
    message = _SENSITIVE_ERROR_QUERY_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        message,
    )
    return message[:500]


def _mark_latest_oa_evidence(
    attempts: list[dict[str, Any]],
    resolver: str,
) -> None:
    """Attach an OA assertion to the metadata response that supplied it."""

    for attempt in reversed(attempts):
        if attempt.get("resolver") == resolver:
            attempt["is_open_access"] = True
            return


def _attempts_have_oa_evidence(attempts: list[dict[str, Any]]) -> bool:
    return any(attempt.get("is_open_access") is True for attempt in attempts)


def _deduplicate_candidates(
    candidates: list[ResolverCandidate],
) -> list[ResolverCandidate]:
    seen: set[str] = set()
    unique: list[ResolverCandidate] = []
    for candidate in candidates:
        key = urldefrag(candidate.url)[0]
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _ordered_locations(best: Any, locations: Any) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if isinstance(best, dict):
        values.append(best)
    if isinstance(locations, list):
        values.extend(value for value in locations if isinstance(value, dict))
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        url = _nonempty_string(
            value.get("url_for_pdf")
            or value.get("pdf_url")
            or value.get("url_for_landing_page")
            or value.get("landing_page_url")
            or value.get("url")
        ) or ""
        key = url.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(value)
    return deduplicated


def _version_rank(version: str | None) -> int:
    ranks = {"publishedversion": 0, "acceptedversion": 1, "submittedversion": 2}
    normalized = re.sub(r"[^a-z]", "", (version or "").casefold())
    return ranks.get(normalized, 3)


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _is_open_license(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "creativecommons.org/licenses/",
            "creativecommons.org/publicdomain/",
            "opensource.org/licenses/",
        )
    )


def _active_open_license(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    now = datetime.now(timezone.utc)
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _nonempty_string(item.get("URL"))
        if not url or not _is_open_license(url):
            continue
        start = item.get("start")
        if isinstance(start, dict):
            timestamp = start.get("timestamp")
            date_time = start.get("date-time")
            try:
                if timestamp is not None:
                    effective = datetime.fromtimestamp(
                        float(timestamp) / 1000.0,
                        tz=timezone.utc,
                    )
                elif isinstance(date_time, str):
                    effective = datetime.fromisoformat(
                        date_time.replace("Z", "+00:00")
                    )
                    if effective.tzinfo is None:
                        effective = effective.replace(tzinfo=timezone.utc)
                else:
                    effective = None
            except (TypeError, ValueError, OverflowError):
                effective = None
            if effective is not None and effective > now:
                continue
        return url
    return None


_DOI_IN_TEXT_RE = re.compile(
    r"10\.[0-9]{4,9}/[^\s<>\[\]\"']+",
    flags=re.IGNORECASE,
)


def _extract_dois(text: str) -> list[str]:
    values: list[str] = []
    for match in _DOI_IN_TEXT_RE.finditer(text):
        candidate = match.group(0).rstrip(".,;:")
        try:
            normalized = normalize_doi(candidate)
        except InvalidDoiError:
            continue
        if normalized and normalized.casefold() not in {
            value.casefold() for value in values
        }:
            values.append(normalized)
    return values


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _metadata_field_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    return " ".join(normalized.split()).casefold()


def _is_spo_paper(paper: PaperCandidate) -> bool:
    """Return whether the row belongs to the two audited SPO journals."""

    if paper.source.casefold() != "kci":
        return False
    institution = (
        paper.institution_id.strip()
        if isinstance(paper.institution_id, str)
        else None
    )
    journal = (
        paper.journal_id.strip()
        if isinstance(paper.journal_id, str)
        else None
    )
    return (
        institution == SPO_INSTITUTION
        and journal
        in {
            SPO_FORENSIC_JOURNAL,
            SPO_CRIMINAL_LAW_JOURNAL,
            "SER000001908",
        }
    )


def _is_police_paper(paper: PaperCandidate) -> bool:
    """Return whether this row belongs to the audited Police journal."""

    if paper.source.casefold() != "kci":
        return False
    institution = (
        paper.institution_id.strip()
        if isinstance(paper.institution_id, str)
        else None
    )
    journal = (
        paper.journal_id.strip()
        if isinstance(paper.journal_id, str)
        else None
    )
    return (
        institution == POLICE_INSTITUTION
        and journal in {POLICE_JOURNAL, "SER000005144"}
    )


def _police_failure_status(
    attempts: list[dict[str, Any]],
) -> str | None:
    summaries = [
        attempt
        for attempt in attempts
        if attempt.get("resolver") == POLICE_RESOLVER_NAME
    ]
    if not summaries:
        return None
    summary = summaries[-1]
    error_code = str(summary.get("error_code") or "")
    resolution_status = str(summary.get("resolution_status") or "")
    if error_code in {"too_large", "unreadable_encrypted", "invalid_pdf"}:
        return error_code
    if resolution_status == "retryable_error":
        return "retryable_error"
    if resolution_status in {"access_denied", "robots_disallowed"}:
        return "access_denied"
    if resolution_status in {
        "invalid_pdf",
        "identity_mismatch",
        "invalid_response",
    }:
        return "invalid_pdf"
    return "not_found"


def _spo_permission_is_required(
    attempts: list[dict[str, Any]],
) -> bool:
    return any(
        attempt.get("resolver") == "spo-public-board"
        and attempt.get("resolution_status") == "permission_required"
        for attempt in attempts
    )


def _spo_failure_status(
    attempts: list[dict[str, Any]],
) -> str | None:
    spo_attempts = [
        attempt
        for attempt in attempts
        if str(attempt.get("resolver") or "").startswith("spo-public-board")
    ]
    if not spo_attempts:
        return None
    status = _failure_status(spo_attempts)
    return (
        status
        if status
        in {
            "too_large",
            "unreadable_encrypted",
            "invalid_pdf",
            "access_denied",
            "retryable_error",
        }
        else None
    )


def _dbpia_browser_fallback_allowed(
    platform_attempts: list[dict[str, Any]],
) -> bool:
    """Use credentials only after an anonymous DBpia access rejection.

    Identity, ambiguity, unsafe-URL, invalid-PDF, and transient failures must
    never be converted into an authenticated retry of a different document.
    """

    summaries = [
        attempt
        for attempt in platform_attempts
        if attempt.get("resolver") == PLATFORM_RESOLVER_NAME
        and attempt.get("platform") == "dbpia"
    ]
    if not summaries:
        return not platform_attempts
    status = str(summaries[-1].get("resolution_status") or "")
    if status in {
        "access_denied",
        "authentication_required",
        "permission_required",
    }:
        return True
    return any(
        attempt.get("http_status") in {401, 403}
        for attempt in platform_attempts
    )


def _dbpia_browser_failure_status(
    attempts: list[dict[str, Any]],
) -> str | None:
    browser_attempts = [
        attempt
        for attempt in attempts
        if str(attempt.get("resolver") or "").startswith(
            DBPIA_BROWSER_RESOLVER_NAME
        )
    ]
    if not browser_attempts:
        return None
    return _failure_status(browser_attempts)


def _validated_dbpia_redownload_campaign(value: str | None) -> str | None:
    """Return one bounded audit-safe campaign identifier."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("DBpia redownload campaign must be a string")
    campaign = value.strip()
    if (
        _DBPIA_BROWSER_REDOWNLOAD_CAMPAIGN_RE.fullmatch(campaign) is None
        or _DBPIA_NODE_AUDIT_RE.search(campaign) is not None
    ):
        raise ValueError(
            "DBpia redownload campaign must be a safe 1-128 character identifier"
        )
    return campaign


def _dbpia_redownload_campaign_completed(
    manifest: Mapping[str, Any],
    campaign: str,
) -> bool:
    """Return whether this exact campaign already committed valid bytes."""

    audit = manifest.get(_DBPIA_BROWSER_REDOWNLOAD_AUDIT)
    return bool(
        isinstance(audit, Mapping)
        and audit.get("campaign") == campaign
        and audit.get("status") in {"replaced", "downloaded"}
    )


def _dbpia_browser_was_attempted(
    attempts: list[dict[str, Any]],
    *,
    resolver_configured: bool,
    open_access_only: bool,
) -> bool:
    """Distinguish actual browser work from budget/circuit deferrals."""

    if not resolver_configured or open_access_only:
        return False
    no_navigation_stages = {
        f"{DBPIA_BROWSER_RESOLVER_NAME}:browser-precheck",
        f"{DBPIA_BROWSER_RESOLVER_NAME}:browser-budget",
        f"{DBPIA_BROWSER_RESOLVER_NAME}:browser-circuit",
    }
    if any(
        attempt.get("resolver") in no_navigation_stages
        for attempt in attempts
    ):
        return False
    # The resolver call itself counts as an attempted browser operation.  This
    # fallback also supports small injected test resolvers that omit stages.
    return True


def _title_is_present(expected_title: str, page_text: str) -> bool:
    expected = _normalized_title(expected_title)
    observed = _normalized_title(page_text)
    return len(expected) >= 12 and expected in observed


def _is_dbpia_article_reference(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 4_096:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "www.dbpia.co.kr"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    path = parsed.path.casefold()
    if _DBPIA_ARTICLE_PATH_AUDIT_RE.fullmatch(parsed.path):
        return True
    if path in {
        "/journal/articledetail",
        "/pdf/pdfview.do",
        "/pdf/pdfaichatview.do",
    }:
        return _DBPIA_NODE_AUDIT_RE.search(value) is not None
    return False


def _manifest_requires_dbpia_identifier_scrub(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("landing_source") == _DBPIA_OPENAPI_LANDING_SOURCE:
            return True
        resolver_block = value.get("resolver")
        if isinstance(resolver_block, dict):
            if (
                resolver_block.get("name")
                in {
                    "authorized-platform-public-ui:dbpia",
                    DBPIA_BROWSER_RESOLVER_NAME,
                }
            ):
                return True
        if (
            value.get("platform") == "dbpia"
            and isinstance(resolver_block, str)
            and (
                resolver_block.startswith("authorized-platform-public-ui")
                or resolver_block.startswith(DBPIA_BROWSER_RESOLVER_NAME)
            )
        ):
            return True
        resolver_name = value.get("resolver")
        if (
            isinstance(resolver_name, str)
            and resolver_name.startswith("authorized-platform-public-ui")
            and any(
                _is_dbpia_article_reference(value.get(field_name))
                for field_name in ("url", "final_url", "landing_url")
            )
        ):
            return True
        for name in ("resolver", "discovery_source", "source"):
            marker = value.get(name)
            if (
                isinstance(marker, str)
                and "dbpia" in marker.casefold()
                and "openapi" in marker.casefold().replace(" ", "")
            ):
                return True
        return any(
            _manifest_requires_dbpia_identifier_scrub(item)
            for item in value.values()
        )
    if isinstance(value, list):
        return any(
            _manifest_requires_dbpia_identifier_scrub(item)
            for item in value
        )
    return False


def _scrub_dbpia_identifier_manifest(
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Remove DBpia article IDs, including from pre-marker manifests."""

    if not _manifest_requires_dbpia_identifier_scrub(manifest):
        return manifest, False
    changed = False

    def scrub(value: object, *, key: str | None = None) -> object:
        nonlocal changed
        if isinstance(value, dict):
            resolver_name = value.get("resolver")
            resolver_record = value.get("name") in {
                "authorized-platform-public-ui:dbpia",
                DBPIA_BROWSER_RESOLVER_NAME,
            }
            platform_attempt = bool(
                isinstance(resolver_name, str)
                and (
                    resolver_name.startswith("authorized-platform-public-ui")
                    or resolver_name.startswith(
                        DBPIA_BROWSER_RESOLVER_NAME
                    )
                    or "dbpia-openapi" in resolver_name.casefold()
                )
            )
            cleaned: dict[str, object] = {}
            for child_key, child_value in value.items():
                if resolver_record and child_key in {
                    "url",
                    "final_url",
                    "landing_url",
                    "download_url",
                }:
                    if child_value is not None:
                        changed = True
                    cleaned[child_key] = None
                    continue
                if platform_attempt and child_key in {
                    "url",
                    "final_url",
                    "landing_url",
                    "download_url",
                }:
                    changed = True
                    continue
                cleaned[child_key] = scrub(
                    child_value,
                    key=child_key,
                )
            return cleaned
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            cleaned_text = _DBPIA_DISCOVERY_URL_RE.sub("[REDACTED]", value)
            cleaned_text = _DBPIA_NODE_AUDIT_RE.sub(
                "[REDACTED]",
                cleaned_text,
            )
            if cleaned_text != value:
                changed = True
                if key == "landing_url":
                    return None
            return cleaned_text
        return value

    scrubbed = scrub(manifest)
    assert isinstance(scrubbed, dict)
    return scrubbed, changed


def _migrate_dbpia_identifier_manifest(
    paths: ArtifactPaths,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    scrubbed, changed = _scrub_dbpia_identifier_manifest(manifest)
    if changed:
        scrubbed["updated_at"] = _utc_now()
        write_json_atomic(paths.download_manifest, scrubbed)
    return scrubbed


def sanitize_dbpia_download_manifest(paths: ArtifactPaths) -> bool:
    """One-shot scrub of legacy DBpia transport identifiers, without PDF I/O."""

    # There is nothing to sanitize before a manifest exists.  Avoid creating
    # an orphan lock for papers that have never reached the download stage.
    if not paths.download_manifest.is_file():
        return False
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state_root.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        # Re-read under the same artifact lock used by PdfDownloader.download
        # so a stale sanitizer snapshot cannot overwrite a concurrent commit.
        manifest = load_json_object(paths.download_manifest)
        if manifest is None:
            return False
        scrubbed, changed = _scrub_dbpia_identifier_manifest(manifest)
        if changed:
            scrubbed["updated_at"] = _utc_now()
            write_json_atomic(paths.download_manifest, scrubbed)
        return changed


def _titles_equivalent(expected_title: str, observed_title: str) -> bool:
    expected = _normalized_title(expected_title)
    observed = _normalized_title(observed_title)
    return len(expected) >= 12 and expected == observed


def _author_identity_keys(values: tuple[str, ...]) -> tuple[str, ...]:
    keys: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = unicodedata.normalize("NFKC", html.unescape(value))
        normalized = re.sub(
            r"\s*[\(\[\{（].*\Z",
            "",
            normalized,
        ).strip()
        key = "".join(
            character.casefold()
            for character in normalized
            if character.isalnum()
        )
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return tuple(keys)


def _paper_bibliographic_identity(
    paper: PaperCandidate,
) -> dict[str, Any] | None:
    """Return the exact DOI-less DBpia identity fields and their digest."""

    if paper.doi or isinstance(paper.publication_year, bool):
        return None
    title = normalize_title(paper.title or "")
    publisher = normalize_title(paper.institution_id or "")
    journal = normalize_title(paper.journal_id or "")
    issue = (paper.issue_id or "").strip()
    first_page = (paper.first_page or "").strip()
    final_page = (paper.final_page or "").strip()
    authors = _author_identity_keys(paper.authors)
    if (
        not title
        or not publisher
        or not journal
        or not isinstance(paper.publication_year, int)
        or re.fullmatch(
            r"(?:[0-9]{1,6}_[0-9]{1,6}|[0-9]{1,6})",
            issue,
        )
        is None
        or not first_page
        or not final_page
        or not paper.authors
        or not authors
    ):
        return None
    values: dict[str, Any] = {
        "schema_version": 1,
        "title": title,
        "publication_year": paper.publication_year,
        "publisher": publisher,
        "journal": journal,
        "issue": issue,
        "first_page": first_page,
        "final_page": final_page,
        "authors": list(authors),
    }
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    values["sha256"] = hashlib.sha256(canonical).hexdigest()
    return values


def _is_dbpia_download_manifest(manifest: Mapping[str, Any]) -> bool:
    resolver = manifest.get("resolver")
    if not isinstance(resolver, Mapping):
        return False
    name = resolver.get("name")
    return bool(
        isinstance(name, str)
        and (
            name == DBPIA_BROWSER_RESOLVER_NAME
            or name == f"{PLATFORM_RESOLVER_NAME}:dbpia"
        )
    )


def _is_kyobo_download_manifest(manifest: Mapping[str, Any]) -> bool:
    resolver = manifest.get("resolver")
    return bool(
        isinstance(resolver, Mapping)
        and resolver.get("name") == KYOBO_BROWSER_RESOLVER_NAME
    )


def _kyobo_browser_resolution_identity_is_complete(
    paper: PaperCandidate,
    resolution: KyoboBrowserResolution,
) -> bool:
    """Require the full URL-free Kyobo article/download evidence chain."""

    provenance = frozenset(resolution.identity_provenance)
    if not (
        _KYOBO_BROWSER_CORE_PROVENANCE.issubset(provenance)
        and _KYOBO_BROWSER_TITLE_PROVENANCE.intersection(provenance)
        and _KYOBO_BROWSER_PUBLISHER_PROVENANCE.intersection(provenance)
        and _KYOBO_BROWSER_PAGE_PROVENANCE.intersection(provenance)
        and _KYOBO_BROWSER_DISCOVERY_PROVENANCE.intersection(provenance)
    ):
        return False
    try:
        matched_doi = normalize_doi(resolution.matched_doi)
    except InvalidDoiError:
        return False
    if matched_doi is None:
        return False
    if paper.doi is not None:
        if (
            matched_doi.casefold() != paper.doi.casefold()
            or "kyobo_landing_doi_exact" not in provenance
        ):
            return False
    elif not {
        "kyobo_landing_doi_unique_observed",
        "kyobo_landing_provider_doi_bibliographic_identity",
    }.issubset(provenance):
        return False
    if not (
        isinstance(resolution.landing_first_page, str)
        and isinstance(resolution.landing_final_page, str)
        and resolution.landing_first_page.isdigit()
        and resolution.landing_final_page.isdigit()
        and isinstance(resolution.landing_page_count, int)
        and resolution.landing_page_count
        == int(resolution.landing_final_page)
        - int(resolution.landing_first_page)
        + 1
        and resolution.landing_page_count > 0
    ):
        return False
    return True


def _manifest_has_dbpia_bibliographic_provenance(
    manifest: Mapping[str, Any],
    *,
    issue: str,
) -> bool:
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        return False
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        provenance = attempt.get("identity_provenance")
        if not (
            isinstance(provenance, list)
            and all(isinstance(value, str) for value in provenance)
        ):
            continue
        observed = set(provenance)
        required_issue = (
            "dbpia_landing_volume_issue_exact"
            if "_" in issue
            else "dbpia_landing_issue_only_exact"
        )
        if (
            _DBPIA_BIBLIOGRAPHY_CORE_PROVENANCE.issubset(observed)
            and required_issue in observed
        ):
            return True
    return False


def download_artifact_is_current(
    paper: PaperCandidate,
    paths: ArtifactPaths,
    manifest: dict[str, Any] | None = None,
) -> bool:
    """Verify manifest identity, bytes, PDF structure, and paper identity."""

    value = manifest if manifest is not None else load_json_object(paths.download_manifest)
    if not value or value.get("status") != "downloaded" or not paths.pdf.is_file():
        return False
    manifest_paper = value.get("paper")
    pdf = value.get("pdf")
    if not isinstance(manifest_paper, dict) or not isinstance(pdf, dict):
        return False
    if manifest_paper.get("source_id") != paper.source_id:
        return False
    doi_is_valid, effective_doi = _effective_manifest_doi(
        paper,
        manifest_paper,
    )
    if not doi_is_valid:
        if not _source_bound_identifier_transition(paper, value):
            return False
        effective_doi = paper.doi
    if effective_doi is None and _is_dbpia_download_manifest(value):
        stored_identity = manifest_paper.get("bibliographic_identity")
        expected_identity = _paper_bibliographic_identity(paper)
        if (
            not isinstance(stored_identity, dict)
            or stored_identity != expected_identity
            or not isinstance(expected_identity, dict)
            or not _manifest_has_dbpia_bibliographic_provenance(
                value,
                issue=str(expected_identity.get("issue") or ""),
            )
        ):
            return False
    if effective_doi is None and _is_kyobo_download_manifest(value):
        stored_identity = manifest_paper.get("bibliographic_identity")
        expected_identity = _paper_bibliographic_identity(paper)
        if (
            not isinstance(stored_identity, dict)
            or stored_identity != expected_identity
            or not isinstance(expected_identity, dict)
        ):
            return False
    if (
        not isinstance(pdf.get("sha256"), str)
        or not isinstance(pdf.get("bytes"), int)
        or pdf.get("path") != paths.pdf.name
    ):
        return False
    try:
        if paths.pdf.stat().st_size != pdf["bytes"]:
            return False
        if sha256_file(paths.pdf) != pdf["sha256"]:
            return False
        content = paths.pdf.read_bytes()
        if _manifest_has_kci_doi_conflict_identity(paper, value):
            _, observed_evidence = _validate_kci_source_bound_doi_conflict(
                content,
                paper,
            )
            if not _manifest_has_kci_doi_conflict_identity(
                paper,
                value,
                observed_evidence=observed_evidence,
            ):
                return False
        elif _is_kyobo_download_manifest(value):
            if not _manifest_has_kyobo_browser_identity(paper, value):
                return False
            kyobo_attempt = next(
                (
                    attempt
                    for attempt in value.get("attempts", ())
                    if isinstance(attempt, Mapping)
                    and attempt.get("resolver") == KYOBO_BROWSER_RESOLVER_NAME
                    and attempt.get("status") == "downloaded"
                ),
                None,
            )
            if not isinstance(kyobo_attempt, Mapping):
                return False
            observed = validate_pdf(
                content,
                expected_title=(
                    str(kyobo_attempt["matched_title"])
                    if isinstance(kyobo_attempt.get("matched_title"), str)
                    else paper.title
                ),
                require_identity=False,
            )
            if observed.page_count != pdf.get("page_count"):
                return False
            provenance = frozenset(
                kyobo_attempt.get("identity_provenance", ())
            )
            if (
                "kyobo_pdf_page_count_off_by_one_title_exact" in provenance
                and observed.identity_status
                not in {"title_match", "metadata_title_match"}
            ):
                return False
        elif _manifest_has_dbpia_browser_layout_identity(
            paper,
            value,
            expected_doi=effective_doi,
        ):
            dbpia_identity = validate_dbpia_pdf_document(
                content,
                paper,
                landing_title=_manifest_expected_title(
                    paper,
                    manifest_paper,
                ),
            )
            if (
                dbpia_identity.status != "doi_layout_and_title_match"
                or not effective_doi
                or (dbpia_identity.matched_doi or "").casefold()
                != effective_doi.casefold()
            ):
                return False
            validate_pdf(
                content,
                expected_doi=None,
                expected_title=_manifest_expected_title(
                    paper,
                    manifest_paper,
                ),
                require_identity=True,
            )
        elif _manifest_has_platform_layout_identity(
            paper,
            value,
            expected_doi=effective_doi,
        ):
            validate_pdf(
                content,
                expected_doi=None,
                expected_title=_manifest_expected_title(
                    paper,
                    manifest_paper,
                ),
                require_identity=True,
            )
        else:
            validate_pdf(
                content,
                expected_doi=effective_doi,
                expected_title=_manifest_expected_title(paper, manifest_paper),
                require_identity=not _manifest_has_resolver_identity(
                    paper,
                    value,
                    expected_doi=effective_doi,
                ),
            )
    except (OSError, PdfValidationError, DbpiaPdfIdentityError):
        return False
    return True


def _manifest_expected_title(
    paper: PaperCandidate,
    manifest_paper: Mapping[str, Any],
) -> str | None:
    manifest_title = manifest_paper.get("title")
    normalized_manifest = (
        normalize_title(manifest_title)
        if isinstance(manifest_title, str)
        else None
    )
    if normalized_manifest is not None and normalized_manifest in {
        normalized
        for title in (paper.title, *paper.alternate_titles)
        if (normalized := normalize_title(title)) is not None
    }:
        return manifest_title
    return paper.title


def _manifest_has_resolver_identity(
    paper: PaperCandidate,
    manifest: dict[str, Any],
    *,
    expected_doi: str | None = None,
) -> bool:
    """Recognize an audited DOI-to-PDF binding from publisher metadata.

    This is needed for image-only/scanned PDFs whose DOI and title cannot be
    extracted from the PDF itself. An explicit different DOI in the PDF is
    still rejected by ``validate_pdf`` even when ``require_identity`` is false.
    """

    resolver = manifest.get("resolver")
    pdf = manifest.get("pdf")
    if not isinstance(resolver, dict) or not isinstance(pdf, dict):
        return False
    doi_value = expected_doi or paper.doi
    matched_doi = pdf.get("matched_doi")
    doi_identity = bool(
        doi_value
        and resolver.get("name")
        in {
            "publisher-metadata",
            "publisher-explicit-pdf-link",
            "google-drive-explicit-download",
            "unpaywall-landing",
            "openalex-landing",
            *TITLE_PDF_IDENTITY_RESOLVERS,
        }
        and pdf.get("identity_status") == "resolver_doi_match"
        and isinstance(matched_doi, str)
        and matched_doi.casefold() == doi_value.casefold()
    )
    platform_requirements = {
        f"{PLATFORM_RESOLVER_NAME}:dbpia": (
            "dbpia",
            DBPIA_RESOLVER_VERIFIED_PROVENANCE,
        ),
        f"{PLATFORM_RESOLVER_NAME}:kiss": (
            "kiss",
            KISS_RESOLVER_VERIFIED_PROVENANCE,
        ),
    }.get(str(resolver.get("name") or ""))
    platform_identity = bool(
        doi_value
        and platform_requirements is not None
        and pdf.get("identity_status") == "resolver_doi_match"
        and isinstance(matched_doi, str)
        and matched_doi.casefold() == doi_value.casefold()
        and _manifest_has_platform_resolver_provenance(
            manifest,
            expected_doi=doi_value,
            platform=platform_requirements[0],
            required_provenance=platform_requirements[1],
        )
    )
    return (
        doi_identity
        or platform_identity
        or _manifest_has_kyobo_browser_identity(paper, manifest)
        or _manifest_has_jams_browser_identity(paper, manifest)
        or _manifest_has_source_identity(paper, manifest)
    )


def _manifest_has_jams_browser_identity(
    paper: PaperCandidate,
    manifest: Mapping[str, Any],
) -> bool:
    """Recognize only the complete URL-free JAMS browser evidence chain."""

    resolver = manifest.get("resolver")
    pdf = manifest.get("pdf")
    if (
        not isinstance(resolver, Mapping)
        or resolver.get("name") != JAMS_BROWSER_RESOLVER_NAME
        or not isinstance(pdf, Mapping)
        or pdf.get("identity_status") != _JAMS_BROWSER_IDENTITY_STATUS
    ):
        return False
    matched_doi = pdf.get("matched_doi")
    if paper.doi is not None and not (
        isinstance(matched_doi, str)
        and matched_doi.casefold() == paper.doi.casefold()
    ):
        return False
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        isinstance(attempt, Mapping)
        and attempt.get("resolver") == JAMS_BROWSER_RESOLVER_NAME
        and attempt.get("platform") == "jams"
        and attempt.get("status") == "downloaded"
        and attempt.get("resolution_status") == "resolved"
        and attempt.get("identity_status") == _JAMS_BROWSER_IDENTITY_STATUS
        and isinstance(attempt.get("identity_provenance"), list)
        and _JAMS_BROWSER_PDF_IDENTITY_PROVENANCE.issubset(
            attempt["identity_provenance"]
        )
        for attempt in attempts
    )


def _manifest_has_kyobo_browser_identity(
    paper: PaperCandidate,
    manifest: Mapping[str, Any],
) -> bool:
    """Recognize only the complete URL-free Kyobo browser evidence chain."""

    resolver = manifest.get("resolver")
    pdf = manifest.get("pdf")
    if (
        not isinstance(resolver, Mapping)
        or resolver.get("name") != KYOBO_BROWSER_RESOLVER_NAME
        or not isinstance(pdf, Mapping)
        or pdf.get("identity_status") != _KYOBO_BROWSER_IDENTITY_STATUS
        or not isinstance(pdf.get("page_count"), int)
        or pdf.get("page_count", 0) < 1
    ):
        return False
    try:
        matched_doi = normalize_doi(pdf.get("matched_doi"))
    except InvalidDoiError:
        return False
    if matched_doi is None:
        return False
    if paper.doi is not None and matched_doi.casefold() != paper.doi.casefold():
        return False
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        return False
    for attempt in attempts:
        if not (
            isinstance(attempt, Mapping)
            and attempt.get("resolver") == KYOBO_BROWSER_RESOLVER_NAME
            and attempt.get("platform") == "kyobo"
            and attempt.get("status") == "downloaded"
            and attempt.get("resolution_status") == "resolved"
            and attempt.get("identity_status")
            == _KYOBO_BROWSER_IDENTITY_STATUS
            and isinstance(attempt.get("landing_page_count"), int)
            and isinstance(attempt.get("matched_doi"), str)
            and str(attempt["matched_doi"]).casefold()
            == matched_doi.casefold()
            and isinstance(attempt.get("identity_provenance"), list)
        ):
            continue
        provenance = frozenset(attempt["identity_provenance"])
        if not (
            _KYOBO_BROWSER_CORE_PROVENANCE.issubset(provenance)
            and _KYOBO_BROWSER_TITLE_PROVENANCE.intersection(provenance)
            and _KYOBO_BROWSER_PUBLISHER_PROVENANCE.intersection(provenance)
            and _KYOBO_BROWSER_PAGE_PROVENANCE.intersection(provenance)
            and _KYOBO_BROWSER_DISCOVERY_PROVENANCE.intersection(provenance)
            and _KYOBO_BROWSER_PDF_PROVENANCE.intersection(provenance)
        ):
            continue
        landing_page_count = int(attempt["landing_page_count"])
        pdf_page_count = int(pdf["page_count"])
        page_count_exact = bool(
            "kyobo_pdf_page_count_exact" in provenance
            and landing_page_count == pdf_page_count
        )
        page_count_variance = bool(
            "kyobo_pdf_page_count_off_by_one_title_exact" in provenance
            and "kyobo_pdf_title_exact" in provenance
            and abs(landing_page_count - pdf_page_count) == 1
        )
        if not (page_count_exact or page_count_variance):
            continue
        if paper.doi is not None:
            if "kyobo_landing_doi_exact" not in provenance:
                continue
        elif not {
            "kyobo_landing_doi_unique_observed",
            "kyobo_landing_provider_doi_bibliographic_identity",
        }.issubset(provenance):
            continue
        return True
    return False


def _manifest_has_platform_layout_identity(
    paper: PaperCandidate,
    manifest: Mapping[str, Any],
    *,
    expected_doi: str | None = None,
) -> bool:
    """Recognize only the complete DBpia layout-DOI evidence chain."""

    resolver = manifest.get("resolver")
    pdf = manifest.get("pdf")
    doi_value = expected_doi or paper.doi
    if (
        not doi_value
        or not isinstance(resolver, Mapping)
        or not isinstance(pdf, Mapping)
        or resolver.get("name") != f"{PLATFORM_RESOLVER_NAME}:dbpia"
        or pdf.get("identity_status")
        != "resolver_doi_layout_title_match"
    ):
        return False
    matched_doi = pdf.get("matched_doi")
    return bool(
        isinstance(matched_doi, str)
        and matched_doi.casefold() == doi_value.casefold()
        and _manifest_has_platform_resolver_provenance(
            manifest,
            expected_doi=doi_value,
            platform="dbpia",
            required_provenance=DBPIA_LAYOUT_VERIFIED_PROVENANCE,
            identity_status="resolver_verified_doi_layout",
        )
    )


def _manifest_has_dbpia_browser_layout_identity(
    paper: PaperCandidate,
    manifest: Mapping[str, Any],
    *,
    expected_doi: str | None = None,
) -> bool:
    """Recognize only the complete URL-free browser layout evidence chain."""

    resolver = manifest.get("resolver")
    pdf = manifest.get("pdf")
    doi_value = expected_doi or paper.doi
    if (
        not doi_value
        or not isinstance(resolver, Mapping)
        or resolver.get("name") != DBPIA_BROWSER_RESOLVER_NAME
        or not isinstance(pdf, Mapping)
        or pdf.get("identity_status")
        != "dbpia_browser_doi_layout_title_match"
        or not isinstance(pdf.get("matched_doi"), str)
        or str(pdf["matched_doi"]).casefold() != doi_value.casefold()
    ):
        return False
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        isinstance(attempt, Mapping)
        and attempt.get("resolver") == DBPIA_BROWSER_RESOLVER_NAME
        and attempt.get("platform") == "dbpia"
        and attempt.get("status") == "downloaded"
        and attempt.get("resolution_status") == "resolved"
        and attempt.get("identity_status")
        == "dbpia_browser_doi_layout_title_match"
        and isinstance(attempt.get("identity_provenance"), list)
        and _DBPIA_BROWSER_LAYOUT_PROVENANCE.issubset(
            attempt["identity_provenance"]
        )
        for attempt in attempts
    )


def _manifest_has_platform_resolver_provenance(
    manifest: Mapping[str, Any],
    *,
    expected_doi: str,
    platform: str,
    required_provenance: frozenset[str],
    identity_status: str = "resolver_verified_doi",
) -> bool:
    """Require a complete platform evidence chain on artifact reuse."""

    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        return False
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        provenance = attempt.get("identity_provenance")
        matched_doi = attempt.get("matched_doi")
        if (
            attempt.get("resolver") == PLATFORM_RESOLVER_NAME
            and attempt.get("status") == "downloaded"
            and attempt.get("resolution_status") == "resolved"
            and attempt.get("platform") == platform
            and attempt.get("identity_status") == identity_status
            and isinstance(matched_doi, str)
            and matched_doi.casefold() == expected_doi.casefold()
            and isinstance(provenance, list)
            and all(isinstance(item, str) for item in provenance)
            and required_provenance.issubset(provenance)
        ):
            return True
    return False


def _manifest_has_kci_doi_conflict_identity(
    paper: PaperCandidate,
    manifest: Mapping[str, Any],
    *,
    observed_evidence: Mapping[str, Any] | None = None,
) -> bool:
    """Require the complete, narrow KCI DOI-conflict evidence chain."""

    resolver = manifest.get("resolver")
    pdf = manifest.get("pdf")
    attempts = manifest.get("attempts")
    if (
        not paper.doi
        or not isinstance(resolver, Mapping)
        or not isinstance(pdf, Mapping)
        or not isinstance(attempts, list)
        or resolver.get("name") != "kci-ui"
        or pdf.get("identity_status") != KCI_DOI_CONFLICT_IDENTITY_STATUS
        or pdf.get("matched_source_id") != paper.source_id
    ):
        return False

    resolver_download_url = resolver.get("download_url")
    if (
        isinstance(resolver_download_url, str)
        and _KCI_FILE_ID_RE.search(resolver_download_url)
    ):
        return False

    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        provenance = attempt.get("identity_provenance")
        conflicting_dois = attempt.get("conflicting_printed_dois")
        if not (
            attempt.get("resolver") == "kci-ui"
            and attempt.get("status") == "downloaded"
            and attempt.get("identity_status")
            == KCI_DOI_CONFLICT_IDENTITY_STATUS
            and attempt.get("matched_source_id") == paper.source_id
            and isinstance(provenance, list)
            and all(isinstance(value, str) for value in provenance)
            and _KCI_DOI_CONFLICT_REQUIRED_PROVENANCE.issubset(provenance)
            and isinstance(conflicting_dois, list)
            and conflicting_dois
            and all(isinstance(value, str) for value in conflicting_dois)
        ):
            continue
        try:
            normalized_conflicts = {
                normalized.casefold()
                for value in conflicting_dois
                if (normalized := normalize_doi(value)) is not None
            }
        except InvalidDoiError:
            continue
        if (
            not normalized_conflicts
            or paper.doi.casefold() in normalized_conflicts
        ):
            continue
        if any(
            isinstance(attempt.get(key), str)
            and _KCI_FILE_ID_RE.search(str(attempt[key]))
            for key in ("url", "final_url")
        ):
            continue
        redirects = attempt.get("redirects")
        if isinstance(redirects, list) and any(
            isinstance(value, str) and _KCI_FILE_ID_RE.search(value)
            for value in redirects
        ):
            continue
        if observed_evidence is not None:
            observed_conflicts = observed_evidence.get(
                "conflicting_printed_dois"
            )
            if not (
                isinstance(observed_conflicts, list)
                and {
                    str(value).casefold() for value in observed_conflicts
                }
                == normalized_conflicts
            ):
                continue
        return True
    return False


def _manifest_has_source_identity(
    paper: PaperCandidate,
    manifest: dict[str, Any],
) -> bool:
    resolver = manifest.get("resolver")
    pdf = manifest.get("pdf")
    if not isinstance(resolver, dict) or not isinstance(pdf, dict):
        return False
    matched_source_id = pdf.get("matched_source_id")
    return bool(
        resolver.get("name") in {"kci-journal-sitemap", "kci-ui"}
        and (
            pdf.get("identity_status") == "resolver_source_id_match"
            or _manifest_has_kci_doi_conflict_identity(paper, manifest)
        )
        and isinstance(matched_source_id, str)
        and matched_source_id == paper.source_id
    )


def _source_bound_identifier_transition(
    paper: PaperCandidate,
    manifest: dict[str, Any],
) -> bool:
    """Allow a trusted source-id artifact to adopt a newly recovered DOI."""

    manifest_paper = manifest.get("paper")
    return bool(
        paper.doi
        and isinstance(manifest_paper, dict)
        and not _nonempty_string(manifest_paper.get("doi"))
        and _manifest_has_source_identity(paper, manifest)
    )


def _effective_manifest_doi(
    paper: PaperCandidate,
    manifest_paper: dict[str, Any],
) -> tuple[bool, str | None]:
    """Validate either the DB DOI or a conservatively title-resolved DOI."""

    manifest_doi_value = manifest_paper.get("doi")
    manifest_doi = manifest_doi_value if isinstance(manifest_doi_value, str) else None
    if paper.doi:
        return (
            bool(
                manifest_doi
                and manifest_doi.casefold() == paper.doi.casefold()
            ),
            paper.doi,
        )
    if not manifest_doi:
        return True, None
    current_resolution = paper.identifier_resolution
    if isinstance(current_resolution, Mapping) and (
        current_resolution.get("status") == "ambiguous"
        or current_resolution.get("reason") == "cross_source_doi_conflict"
    ):
        return False, None
    doi_source = manifest_paper.get("doi_source")
    if doi_source not in TITLE_DOI_RESOLVERS:
        return False, None
    resolution = manifest_paper.get("identifier_resolution")
    if not isinstance(resolution, dict):
        return False, None
    if (
        resolution.get("resolver") != doi_source
        or resolution.get("status") != "resolved"
    ):
        return False, None
    resolution_doi = resolution.get("doi")
    matched_title = resolution.get("matched_title")
    matched_year = resolution.get("matched_year")
    if (
        not isinstance(resolution_doi, str)
        or resolution_doi.casefold() != manifest_doi.casefold()
        or not isinstance(matched_title, str)
        or normalize_title(matched_title)
        not in {
            normalized
            for title in (paper.title, *paper.alternate_titles)
            if (normalized := normalize_title(title)) is not None
        }
        or isinstance(matched_year, bool)
        or not isinstance(matched_year, int)
        or isinstance(paper.publication_year, bool)
        or not isinstance(paper.publication_year, int)
        or abs(matched_year - paper.publication_year) > 1
    ):
        return False, None
    try:
        normalized = normalize_doi(manifest_doi)
    except InvalidDoiError:
        return False, None
    return (normalized is not None), normalized


def _quote_doi(doi: str) -> str:
    return quote(doi, safe="/():;._-")


def _looks_like_pdf(body: bytes) -> bool:
    return body.find(PDF_MAGIC, 0, 1024) >= 0


def _looks_like_html(fetch: HttpFetch) -> bool:
    if "html" in fetch.content_type:
        return True
    prefix = fetch.body[:512].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html"))


def _response_encoding(fetch: HttpFetch) -> str:
    content_type = fetch.headers.get("Content-Type", "")
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, flags=re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _failure_status(attempts: list[dict[str, Any]]) -> str:
    codes = {str(attempt.get("error_code") or "") for attempt in attempts}
    statuses = {attempt.get("http_status") for attempt in attempts}
    # Artifact validation/size failures and explicit access denial are terminal
    # outcomes, so they intentionally outrank a transient failure from another
    # resolver.
    if "too_large" in codes:
        return "too_large"
    if "unreadable_encrypted" in codes:
        return "unreadable_encrypted"
    if "invalid_pdf" in codes:
        return "invalid_pdf"
    if "not_entitled" in codes:
        return "not_entitled"
    if "authentication_required" in codes:
        return "authentication_required"
    if codes & {"captcha_required", "access_denied"}:
        return "access_denied"
    if codes & {
        "browser_dependency_missing",
        "browser_not_installed",
        "browser_launch_failed",
        "invalid_browser_profile",
        "profile_in_use",
    }:
        return "configuration_error"
    if 401 in statuses:
        return "authentication_required"
    if 403 in statuses:
        return "access_denied"
    has_retryable_http_status = any(
        isinstance(status, int)
        and (status in {408, 425, 429} or 500 <= status <= 599)
        for status in statuses
    )
    # A transient failure must be retried even if another resolver definitively
    # returned 404/410 during the same run.
    if codes & {
        "retryable_error",
        "request_error",
        "browser_error",
        "download_timeout",
        "browser_attempt_limit_reached",
        "consecutive_retryable_failures",
    } or has_retryable_http_status:
        return "retryable_error"
    if "permission_required" in codes:
        return "permission_required"
    if 404 in statuses or 410 in statuses:
        return "not_found"
    return "not_found"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
