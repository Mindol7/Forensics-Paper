"""Authorized, fail-closed PDF resolution for Korean scholarly platforms.

This module implements only the ordinary credential-free browser workflows
exposed by the official article pages of DBpia, eArticle, KISS, and Kyobo
Scholar.  It does not accept caller-supplied cookies, credentials, CAPTCHA
answers, or download tokens.  A provider may still recognize the caller's
network as an institutional subscriber.  The resolver is disabled by default
and must be explicitly enabled after the operator has confirmed that automated
research copying is authorized.

The supported workflows are intentionally narrow:

* DBpia: an exact article page advertises one ``NODE`` identifier, and the
  official ``/download/downloadData`` endpoint returns a one-use link.
* eArticle: an exact ``/Article/A...`` page advertises its article identifier,
  and ``/Download/Basic`` reports ``SUCCESS`` before returning a one-use key.
* KISS: an exact article page contains one ``pdfDown(...)`` identifier, and
  ``/Down/Download`` reports ``Success`` before returning a one-use key.
* Kyobo Scholar: only an explicit ``citation_pdf_url`` or literal HTTPS PDF
  anchor is accepted.  Its authenticated ``/file/authCheck`` workflow is not
  automated here.

Every landing page must match a known paper title (and DOI when the page
publishes one).  Every response URL is checked against exact host/path/query
allowlists.  Finally, the downloaded bytes must parse as a non-encrypted PDF
and independently match the expected DOI or title.  Any ambiguity fails
closed.
"""

from __future__ import annotations

import html
import io
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import HttpClientError, HttpFetch, SafeHttpClient
from PaperPipeline.identifier_resolver import normalize_title, paper_title_queries
from PaperPipeline.records import PaperCandidate, ResolverCandidate


PLATFORM_RESOLVER_NAME = "authorized-platform-public-ui"
DEFAULT_MAX_HTML_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_AUTH_BYTES = 256 * 1024
DEFAULT_MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 2_000

_DOI_HOST = "doi.org"
_DBPIA_HOST = "www.dbpia.co.kr"
_DBPIA_DOWNLOAD_HOST = "download.dbpia.co.kr"
_DBPIA_CDN_HOST = "downloadcf.dbpia.co.kr"
_EARTICLE_HOST = "www.earticle.net"
_EARTICLE_FILE_HOST = "file1.earticle.net"
_KISS_HOST = "kiss.kstudy.com"
_KISS_FILE_HOST = "kisspdf.kstudy.com"
_KYOBO_HOST = "scholar.kyobobook.co.kr"
_KYOBO_CONTENT_HOST = "contents.kyobobook.co.kr"

_DBPIA_ARTICLE_PATH = "/journal/articleDetail"
_DBPIA_VIEWER_PATHS = frozenset(
    {
        "/pdf/pdfView.do",
        "/pdf/pdfAiChatView.do",
    }
)
_DBPIA_AUTH_URL = f"https://{_DBPIA_HOST}/download/downloadData"
_EARTICLE_SEARCH_PATH = "/Search/Result"
_EARTICLE_AUTH_URL = f"https://{_EARTICLE_HOST}/Download/Basic"
_KISS_AUTH_PATH = "/Down/Download"
_MAX_EARTICLE_SEARCH_TITLE_CHARS = 300

_NODE_ID_RE = re.compile(r"\ANODE[0-9]{5,20}\Z")
_DBPIA_LEGACY_ARTICLE_PATH_RE = re.compile(
    r"\A/journal/articledetail/[1-9][0-9]{0,19}\Z",
    flags=re.IGNORECASE,
)
_DBPIA_NODE_ARTICLE_PATH_RE = re.compile(
    r"\A/journal/articledetail/(?P<node>NODE[0-9]{5,20})\Z",
    flags=re.IGNORECASE,
)
_DBPIA_VIEWER_WIDTH_RE = re.compile(r"\A[1-9][0-9]{1,4}\Z")
_DBPIA_SEARCH_CONTEXT_NAMES = frozenset(
    {
        "Collection",
        "Multimedia",
        "Page",
        "PageSize",
        "SearchAll",
        "Seq",
        "Sort",
        "SortType",
        "isIdentifyAuthor",
        "q",
        "searchWord",
    }
)
_EARTICLE_PATH_RE = re.compile(r"\A/Article/A(?P<article>[0-9]{1,20})\Z")
_KYOBO_PATH_RE = re.compile(r"\A/article/detail/(?P<article>[0-9]{8,20})\Z")
_DBPIA_CDN_PATH_RE = re.compile(
    r"\A/(?:[0-9]{1,4}/){3}[0-9]{4,20}[.]pdf\Z",
    re.IGNORECASE,
)
_DIRECT_PDF_PATH_RE = re.compile(r"[.]pdf\Z", re.IGNORECASE)
_DBPIA_BUTTON_RE = re.compile(
    r"""onClickDownloadNode\(\s*(['"])(?P<node>NODE[0-9]{5,20})\1""",
    re.IGNORECASE,
)
_KISS_BUTTON_RE = re.compile(
    r"""pdfDown\(\s*(['"])(?P<key>[0-9]{1,20})\1\s*\)""",
    re.IGNORECASE,
)
_DOI_IN_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9])10[.][0-9]{4,9}/[A-Za-z0-9._;/:+-]+",
    re.IGNORECASE,
)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SENSITIVE_AUDIT_QUERY_PARTS = (
    "key",
    "keyn",
    "code",
    "jwt",
    "token",
    "auth",
    "signature",
    "credential",
    "cookie",
    "email",
    "mailto",
)
DBPIA_RESOLVER_VERIFIED_PROVENANCE = frozenset(
    {
        "landing_doi_exact",
        "landing_year_exact",
        "landing_title_compact_exact",
        "dbpia_landing_node_exact",
        "dbpia_download_button_node_exact",
        "dbpia_authorization_node_exact",
        "dbpia_cdn_filename_title_prefix",
        "pdf_structure_valid",
    }
)
DBPIA_LAYOUT_VERIFIED_PROVENANCE = frozenset(
    {
        "landing_doi_exact",
        "landing_year_exact",
        "landing_title_normalized_exact",
        "dbpia_landing_node_exact",
        "dbpia_download_button_node_exact",
        "dbpia_authorization_node_exact",
        "pdf_doi_layout_exact",
        "pdf_title_exact",
        "pdf_structure_valid",
    }
)
KISS_RESOLVER_VERIFIED_PROVENANCE = frozenset(
    {
        "landing_doi_exact",
        "landing_year_exact",
        "landing_title_compact_exact",
        "kiss_landing_article_exact",
        "kiss_single_download_identifier_exact",
        "kiss_authorization_request_identifier_exact",
        "kiss_authorization_success_exact",
        "kiss_pdf_url_exact",
        "kiss_pdf_title_exact",
        "kiss_pdf_journal_exact",
        "kiss_pdf_issue_exact",
        "kiss_pdf_page_range_exact",
        "kiss_pdf_page_count_exact",
        "pdf_structure_valid",
    }
)


class PlatformHttp(Protocol):
    """The bounded anonymous HTTP seam used by the resolver."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        max_html_bytes: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Fetch one GET response."""

    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Fetch one non-redirecting POST response."""


@dataclass(frozen=True, slots=True)
class PlatformResolverConfig:
    """Bounds and the explicit authorization switch."""

    permission_granted: bool = False
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES
    max_auth_bytes: int = DEFAULT_MAX_AUTH_BYTES
    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES
    require_pdf_identity: bool = True


@dataclass(frozen=True, slots=True)
class PlatformAttempt:
    """One redacted, auditable network attempt."""

    stage: str
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformResolution:
    """A fully fetched and identity-validated platform PDF decision."""

    status: str
    reason: str
    paper: PaperCandidate
    platform: str | None = None
    resolver: str = PLATFORM_RESOLVER_NAME
    candidate: ResolverCandidate | None = None
    fetch: HttpFetch | None = None
    page_count: int | None = None
    identity_status: str | None = None
    matched_doi: str | None = None
    matched_title: str | None = None
    landing_url: str | None = None
    identity_provenance: tuple[str, ...] = ()
    attempts: tuple[PlatformAttempt, ...] = ()
    http_status: int | None = None
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return (
            self.status == "resolved"
            and self.candidate is not None
            and self.fetch is not None
        )

    @property
    def resolver_verified_doi(self) -> bool:
        """Whether a narrow official evidence chain verified the DOI."""

        required = {
            "dbpia": DBPIA_RESOLVER_VERIFIED_PROVENANCE,
            "kiss": KISS_RESOLVER_VERIFIED_PROVENANCE,
        }.get(self.platform or "")
        return (
            self.resolved
            and required is not None
            and self.identity_status == "resolver_verified_doi"
            and self.matched_doi is not None
            and required.issubset(self.identity_provenance)
        )

    @property
    def resolver_verified_doi_layout(self) -> bool:
        """Whether exact DBpia metadata binds a layout-spaced DOI and title."""

        return (
            self.resolved
            and self.platform == "dbpia"
            and self.identity_status == "resolver_verified_doi_layout"
            and self.matched_doi is not None
            and DBPIA_LAYOUT_VERIFIED_PROVENANCE.issubset(
                self.identity_provenance
            )
        )


@dataclass(frozen=True, slots=True)
class DbpiaLandingIdentity:
    """Public, URL-free identity extracted from one exact DBpia article page."""

    title: str
    doi: str | None
    title_match: str
    doi_exact: bool
    year_exact: bool
    provenance: tuple[str, ...] = ()


class DbpiaLandingIdentityError(ValueError):
    """A stable DBpia landing validation failure safe to expose in audits."""

    def __init__(self, status: str, reason: str, error_code: str | None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class DbpiaPdfIdentity:
    """URL-free PDF identity shared by DBpia HTTP and browser backends."""

    page_count: int
    status: str
    matched_doi: str | None = None
    matched_title: str | None = None
    provenance: tuple[str, ...] = ()


class DbpiaPdfIdentityError(ValueError):
    """A stable PDF identity failure without transport details."""


@dataclass(frozen=True, slots=True)
class _LandingIdentity:
    title: str
    doi: str | None
    title_match: str
    doi_exact: bool
    year_exact: bool
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PdfIdentity:
    page_count: int
    status: str
    matched_doi: str | None = None
    matched_title: str | None = None
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _EarticleSearchResult:
    article_path: str
    title: str
    publication_year: int
    authors: tuple[str, ...]


@dataclass(slots=True)
class _EarticleSearchResultBuilder:
    title_links: list[tuple[str, str]] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    date_values: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Failure:
    status: str
    reason: str
    http_status: int | None = None
    error_code: str | None = None


class AuthorizedPlatformPdfResolver:
    """Resolve and fetch one PDF through an explicitly authorized UI flow."""

    def __init__(
        self,
        config: PlatformResolverConfig | None = None,
        *,
        http: PlatformHttp | None = None,
    ) -> None:
        self.config = config or PlatformResolverConfig()
        if self.config.max_html_bytes < 1:
            raise ValueError("max_html_bytes must be positive")
        if self.config.max_auth_bytes < 1:
            raise ValueError("max_auth_bytes must be positive")
        if self.config.max_pdf_bytes < 1:
            raise ValueError("max_pdf_bytes must be positive")
        self.http: PlatformHttp = http or SafeHttpClient()

    def resolve(
        self,
        paper: PaperCandidate,
        *,
        landing_url: str | None = None,
    ) -> PlatformResolution:
        """Return a fetched PDF only after platform and document identity checks."""

        if not self.config.permission_granted:
            return PlatformResolution(
                status="permission_required",
                reason="authorized_platform_collection_disabled",
                paper=paper,
            )

        known_titles = tuple(paper_title_queries(paper))
        if not known_titles:
            return PlatformResolution(
                status="missing_metadata",
                reason="missing_known_title",
                paper=paper,
            )

        expected_doi = _paper_doi(paper)
        attempts: list[PlatformAttempt] = []
        discovery_provenance: tuple[str, ...] = ()
        if landing_url is None:
            if expected_doi is None:
                (
                    landing_url,
                    discovery_provenance,
                    failure,
                ) = self._discover_earticle_landing(
                    paper,
                    known_titles=known_titles,
                    attempts=attempts,
                )
                if failure is not None:
                    return self._failure(
                        paper,
                        failure,
                        attempts,
                        platform="earticle",
                    )
                assert landing_url is not None
            else:
                landing_url = (
                    f"https://{_DOI_HOST}/"
                    f"{quote(expected_doi, safe='/():;._-')}"
                )

        (
            landing_url,
            viewer_provenance,
            viewer_failure,
        ) = _canonicalize_dbpia_viewer_url(landing_url)
        if viewer_failure is not None:
            return self._failure(
                paper,
                viewer_failure,
                attempts,
                platform="dbpia",
            )
        assert landing_url is not None
        discovery_provenance = (
            *discovery_provenance,
            *viewer_provenance,
        )

        landing, failure = self._get(
            stage="platform-landing",
            url=landing_url,
            max_bytes=self.config.max_html_bytes,
            max_html_bytes=self.config.max_html_bytes,
            headers={"Accept": "text/html,application/xhtml+xml;q=0.9"},
            attempts=attempts,
        )
        if failure is not None:
            return self._failure(
                paper,
                failure,
                attempts,
            )
        assert landing is not None

        platform, failure = _validate_landing_fetch(
            landing,
            via_doi=_is_doi_url(landing_url),
        )
        if failure is not None:
            return self._failure(
                paper,
                failure,
                attempts,
                landing_url=landing.final_url,
            )
        assert platform is not None

        document = _decode_html(landing)
        if document is None:
            return self._failure(
                paper,
                _Failure(
                    "invalid_response",
                    "platform_landing_is_not_html",
                    landing.status_code,
                    "invalid_html",
                ),
                attempts,
                platform=platform,
                landing_url=landing.final_url,
            )
        parser = _PlatformHtmlParser()
        try:
            parser.feed(document)
            parser.close()
        except (AssertionError, ValueError):
            return self._failure(
                paper,
                _Failure(
                    "invalid_response",
                    "invalid_platform_landing_html",
                    landing.status_code,
                    "invalid_html",
                ),
                attempts,
                platform=platform,
                landing_url=landing.final_url,
            )

        identity, failure = _landing_identity(
            parser,
            paper,
            platform=platform,
            expected_doi=expected_doi,
            known_titles=known_titles,
        )
        if failure is not None:
            return self._failure(
                paper,
                failure,
                attempts,
                platform=platform,
                landing_url=landing.final_url,
            )
        assert identity is not None

        candidate, failure = self._authorize_candidate(
            platform,
            landing,
            parser,
            attempts,
        )
        if failure is not None:
            return self._failure(
                paper,
                failure,
                attempts,
                platform=platform,
                landing_url=landing.final_url,
                matched_title=identity.title,
                matched_doi=identity.doi,
            )
        assert candidate is not None

        pdf_fetch, failure = self._get(
            stage=f"{platform}-pdf",
            url=candidate.url,
            max_bytes=self.config.max_pdf_bytes,
            max_html_bytes=min(
                self.config.max_html_bytes,
                self.config.max_pdf_bytes,
            ),
            headers={
                "Accept": "application/pdf,application/octet-stream;q=0.9",
                "Referer": landing.final_url,
            },
            attempts=attempts,
        )
        if failure is not None:
            return self._failure(
                paper,
                failure,
                attempts,
                platform=platform,
                landing_url=landing.final_url,
                matched_title=identity.title,
                matched_doi=identity.doi,
                candidate=candidate,
            )
        assert pdf_fetch is not None

        url_failure = _validate_pdf_fetch_urls(platform, pdf_fetch)
        if url_failure is not None:
            return self._failure(
                paper,
                url_failure,
                attempts,
                platform=platform,
                landing_url=landing.final_url,
                matched_title=identity.title,
                matched_doi=identity.doi,
                candidate=candidate,
            )
        if not 200 <= pdf_fetch.status_code < 300:
            status = (
                "retryable_error"
                if pdf_fetch.status_code in _RETRYABLE_HTTP_STATUSES
                else "access_denied"
                if pdf_fetch.status_code in {401, 403}
                else "http_error"
            )
            return self._failure(
                paper,
                _Failure(
                    status,
                    f"{platform}_pdf_http_error",
                    pdf_fetch.status_code,
                    "http_status",
                ),
                attempts,
                platform=platform,
                landing_url=landing.final_url,
                matched_title=identity.title,
                matched_doi=identity.doi,
                candidate=candidate,
            )

        try:
            pdf_identity = _validate_pdf_identity(
                pdf_fetch.body,
                paper,
                platform=platform,
                expected_doi=expected_doi,
                known_titles=known_titles,
                require_identity=False,
            )
        except ValueError as exc:
            return self._failure(
                paper,
                _Failure(
                    "invalid_pdf",
                    str(exc),
                    pdf_fetch.status_code,
                    "invalid_pdf",
                ),
                attempts,
                platform=platform,
                landing_url=landing.final_url,
                matched_title=identity.title,
                matched_doi=identity.doi,
                candidate=candidate,
            )

        if (
            platform == "dbpia"
            and identity.title_match == "dbpia_bibliographic_compact_exact"
            and expected_doi is not None
            and pdf_identity.status
            not in {"doi_match", "doi_layout_and_title_match"}
        ):
            return self._failure(
                paper,
                _Failure(
                    "invalid_pdf",
                    "DBpia PDF did not contain the expected DOI",
                    pdf_fetch.status_code,
                    "invalid_pdf",
                ),
                attempts,
                platform=platform,
                landing_url=landing.final_url,
                matched_title=identity.title,
                matched_doi=identity.doi,
                candidate=candidate,
            )

        provenance = list(discovery_provenance)
        provenance.extend(_landing_provenance(identity))
        provenance.append("pdf_structure_valid")
        provenance.extend(pdf_identity.provenance)
        if platform == "dbpia":
            provenance.extend(_dbpia_authorization_provenance(landing.final_url))
        elif platform == "kiss":
            provenance.extend(
                (
                    "kiss_landing_article_exact",
                    "kiss_single_download_identifier_exact",
                    "kiss_authorization_request_identifier_exact",
                    "kiss_authorization_success_exact",
                    "kiss_pdf_url_exact",
                )
            )
        resolver_verified = False
        layout_verified = False
        if pdf_identity.status == "unconfirmed":
            if (
                self.config.require_pdf_identity
                and platform == "dbpia"
                and identity.doi_exact
                and identity.year_exact
                and _dbpia_filename_title_prefix(
                    pdf_fetch.final_url,
                    identity.title,
                )
            ):
                provenance.append("dbpia_cdn_filename_title_prefix")
                if DBPIA_RESOLVER_VERIFIED_PROVENANCE.issubset(provenance):
                    assert expected_doi is not None
                    pdf_identity = _PdfIdentity(
                        page_count=pdf_identity.page_count,
                        status="resolver_verified_doi",
                        matched_doi=expected_doi,
                        matched_title=identity.title,
                    )
                    resolver_verified = True
            if self.config.require_pdf_identity and not resolver_verified:
                return self._failure(
                    paper,
                    _Failure(
                        "invalid_pdf",
                        "platform PDF DOI/title identity could not be confirmed",
                        pdf_fetch.status_code,
                        "invalid_pdf",
                    ),
                    attempts,
                    platform=platform,
                    landing_url=landing.final_url,
                    matched_title=identity.title,
                    matched_doi=identity.doi,
                    candidate=candidate,
                )
        elif pdf_identity.status == "kiss_bibliographic_match":
            if (
                self.config.require_pdf_identity
                and platform == "kiss"
                and identity.doi_exact
                and identity.year_exact
                and KISS_RESOLVER_VERIFIED_PROVENANCE.issubset(provenance)
            ):
                assert expected_doi is not None
                pdf_identity = _PdfIdentity(
                    page_count=pdf_identity.page_count,
                    status="resolver_verified_doi",
                    matched_doi=expected_doi,
                    matched_title=pdf_identity.matched_title,
                    provenance=pdf_identity.provenance,
                )
                resolver_verified = True
            elif self.config.require_pdf_identity:
                return self._failure(
                    paper,
                    _Failure(
                        "invalid_pdf",
                        "platform PDF DOI/title identity could not be confirmed",
                        pdf_fetch.status_code,
                        "invalid_pdf",
                    ),
                    attempts,
                    platform=platform,
                    landing_url=landing.final_url,
                    matched_title=identity.title,
                    matched_doi=identity.doi,
                    candidate=candidate,
                )
        elif pdf_identity.status == "doi_match":
            provenance.append("pdf_doi_exact")
        elif pdf_identity.status == "doi_layout_and_title_match":
            provenance.extend(
                (
                    "pdf_doi_layout_exact",
                    "pdf_title_exact",
                )
            )
            if (
                self.config.require_pdf_identity
                and platform == "dbpia"
                and identity.doi_exact
                and identity.year_exact
                and DBPIA_LAYOUT_VERIFIED_PROVENANCE.issubset(provenance)
            ):
                assert expected_doi is not None
                pdf_identity = _PdfIdentity(
                    page_count=pdf_identity.page_count,
                    status="resolver_verified_doi_layout",
                    matched_doi=expected_doi,
                    matched_title=pdf_identity.matched_title,
                    provenance=pdf_identity.provenance,
                )
                layout_verified = True
            elif self.config.require_pdf_identity:
                return self._failure(
                    paper,
                    _Failure(
                        "invalid_pdf",
                        "platform PDF DOI/title identity could not be confirmed",
                        pdf_fetch.status_code,
                        "invalid_pdf",
                    ),
                    attempts,
                    platform=platform,
                    landing_url=landing.final_url,
                    matched_title=identity.title,
                    matched_doi=identity.doi,
                    candidate=candidate,
                )
        elif pdf_identity.status in {"title_match", "metadata_title_match"}:
            provenance.append("pdf_title_exact")

        verified_doi = (
            expected_doi
            if identity.doi is not None
            and expected_doi is not None
            and identity.doi.casefold() == expected_doi.casefold()
            else None
        )
        final_candidate = ResolverCandidate(
            url=candidate.url,
            resolver=f"{PLATFORM_RESOLVER_NAME}:{platform}",
            landing_url=landing.final_url,
            is_open_access=None,
            verified_doi=verified_doi,
        )
        reason = (
            "authorized_official_ui_resolver_verified_doi"
            if resolver_verified
            else "authorized_official_ui_resolver_verified_doi_layout"
            if layout_verified
            else (
                "authorized_earticle_exact_title_discovery_and_pdf_identity_match"
                if platform == "earticle"
                and "earticle_search_request_exact" in discovery_provenance
                else "authorized_official_ui_and_pdf_identity_match"
            )
        )
        return PlatformResolution(
            status="resolved",
            reason=reason,
            paper=paper,
            platform=platform,
            candidate=final_candidate,
            fetch=pdf_fetch,
            page_count=pdf_identity.page_count,
            identity_status=pdf_identity.status,
            matched_doi=pdf_identity.matched_doi or identity.doi,
            matched_title=pdf_identity.matched_title or identity.title,
            landing_url=landing.final_url,
            identity_provenance=tuple(dict.fromkeys(provenance)),
            attempts=tuple(attempts),
            http_status=pdf_fetch.status_code,
        )

    def _discover_earticle_landing(
        self,
        paper: PaperCandidate,
        *,
        known_titles: tuple[tuple[str, str], ...],
        attempts: list[PlatformAttempt],
    ) -> tuple[str | None, tuple[str, ...], _Failure | None]:
        """Resolve one DOI-less paper through eArticle's official title search."""

        if paper.publication_year is None:
            return None, (), _Failure(
                "missing_metadata",
                "missing_earticle_search_year",
                error_code="missing_metadata",
            )
        query_title = _safe_earticle_search_title(known_titles[0][0])
        if query_title is None:
            return None, (), _Failure(
                "missing_metadata",
                "invalid_earticle_search_title",
                error_code="invalid_search_title",
            )
        search_url = (
            f"https://{_EARTICLE_HOST}{_EARTICLE_SEARCH_PATH}?"
            + urlencode({"sf": "2", "q": query_title})
        )
        fetch, failure = self._get(
            stage="earticle-title-search",
            url=search_url,
            max_bytes=self.config.max_html_bytes,
            max_html_bytes=self.config.max_html_bytes,
            headers={"Accept": "text/html,application/xhtml+xml;q=0.9"},
            attempts=attempts,
        )
        if failure is not None:
            return None, (), failure
        assert fetch is not None
        failure = _validate_earticle_search_fetch(
            fetch,
            expected_url=search_url,
            expected_title=query_title,
        )
        if failure is not None:
            return None, (), failure
        document = _decode_html(fetch)
        if document is None:
            return None, (), _Failure(
                "invalid_response",
                "earticle_search_is_not_html",
                fetch.status_code,
                "invalid_html",
            )
        parser = _EarticleSearchParser()
        try:
            parser.feed(document)
            parser.close()
        except (AssertionError, ValueError):
            return None, (), _Failure(
                "invalid_response",
                "invalid_earticle_search_html",
                fetch.status_code,
                "invalid_html",
            )
        selected, provenance, failure = _select_earticle_search_result(
            parser,
            paper,
            known_titles=known_titles,
        )
        if failure is not None:
            return None, (), failure
        assert selected is not None
        return (
            f"https://{_EARTICLE_HOST}{selected.article_path}",
            (
                "earticle_search_request_exact",
                *provenance,
            ),
            None,
        )

    def _authorize_candidate(
        self,
        platform: str,
        landing: HttpFetch,
        parser: "_PlatformHtmlParser",
        attempts: list[PlatformAttempt],
    ) -> tuple[ResolverCandidate | None, _Failure | None]:
        if platform == "dbpia":
            return self._authorize_dbpia(landing, parser, attempts)
        if platform == "earticle":
            return self._authorize_earticle(landing, parser, attempts)
        if platform == "kiss":
            return self._authorize_kiss(landing, parser, attempts)
        if platform == "kyobo":
            return self._authorize_kyobo(landing, parser)
        return None, _Failure("unsupported", "unsupported_platform")

    def _authorize_dbpia(
        self,
        landing: HttpFetch,
        parser: "_PlatformHtmlParser",
        attempts: list[PlatformAttempt],
    ) -> tuple[ResolverCandidate | None, _Failure | None]:
        parsed_landing = urlsplit(landing.final_url)
        query_node = _single_query_value(landing.final_url, "nodeId")
        path_node_match = _DBPIA_NODE_ARTICLE_PATH_RE.fullmatch(
            parsed_landing.path
        )
        advertised_nodes = [
            match.group("node").upper()
            for value in parser.onclick_values
            for match in _DBPIA_BUTTON_RE.finditer(value)
        ]
        unique_advertised_nodes = set(advertised_nodes)
        legacy_landing = bool(
            _DBPIA_LEGACY_ARTICLE_PATH_RE.fullmatch(parsed_landing.path)
            and _valid_dbpia_search_context(parsed_landing.query)
        )
        if query_node is not None and _NODE_ID_RE.fullmatch(query_node):
            node_id = query_node
            if set(advertised_nodes) != {node_id.upper()}:
                return None, _Failure(
                    "identity_mismatch",
                    "dbpia_download_button_node_mismatch",
                    landing.status_code,
                    "identity_mismatch",
                )
        elif (
            path_node_match is not None
            and _valid_dbpia_search_context(parsed_landing.query)
        ):
            node_id = path_node_match.group("node").upper()
            if unique_advertised_nodes != {node_id}:
                return None, _Failure(
                    "identity_mismatch",
                    "dbpia_download_button_node_mismatch",
                    landing.status_code,
                    "identity_mismatch",
                )
        elif legacy_landing:
            # The official Search API can return a numeric legacy article
            # path.  There is no documented numeric-ID-to-NODE conversion, so
            # bind only the single NODE that the fetched article page itself
            # advertises.  Never guess or derive it from the numeric path.
            if len(unique_advertised_nodes) != 1:
                return None, _Failure(
                    "identity_mismatch",
                    "dbpia_legacy_download_node_not_unique",
                    landing.status_code,
                    "identity_mismatch",
                )
            node_id = next(iter(unique_advertised_nodes))
        else:
            return None, _Failure(
                "invalid_response",
                "invalid_dbpia_node_id",
                landing.status_code,
                "invalid_article_id",
            )

        fetch, failure = self._post(
            stage="dbpia-download-authorization",
            url=_DBPIA_AUTH_URL,
            data={
                "nodeId": node_id,
                "depth": "Article",
                "shape": "download",
            },
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": landing.final_url,
            },
            attempts=attempts,
        )
        if failure is not None:
            return None, failure
        assert fetch is not None
        payload, failure = _json_response(fetch, "dbpia")
        if failure is not None:
            return None, failure
        assert payload is not None

        if payload.get("nodeId") != node_id:
            return None, _Failure(
                "identity_mismatch",
                "dbpia_authorization_node_mismatch",
                fetch.status_code,
                "identity_mismatch",
            )
        if payload.get("showAlert_YN") == "Y":
            return None, _Failure(
                "access_denied",
                "dbpia_authorization_alert",
                fetch.status_code,
                "access_denied",
            )
        result_code = payload.get("resultCode")
        if result_code not in {"", None}:
            return None, _Failure(
                "access_denied",
                "dbpia_authorization_rejected",
                fetch.status_code,
                "access_denied",
            )
        link = payload.get("link")
        if not isinstance(link, str) or not link or link == "null":
            return None, _Failure(
                "access_denied",
                "dbpia_download_link_not_authorized",
                fetch.status_code,
                "access_denied",
            )
        if not _valid_dbpia_entry_url(link):
            return None, _Failure(
                "invalid_response",
                "unsafe_dbpia_download_url",
                fetch.status_code,
                "unsafe_url",
            )
        return (
            ResolverCandidate(
                url=link,
                resolver=f"{PLATFORM_RESOLVER_NAME}:dbpia",
                landing_url=landing.final_url,
                is_open_access=None,
            ),
            None,
        )

    def _authorize_earticle(
        self,
        landing: HttpFetch,
        parser: "_PlatformHtmlParser",
        attempts: list[PlatformAttempt],
    ) -> tuple[ResolverCandidate | None, _Failure | None]:
        path_match = _EARTICLE_PATH_RE.fullmatch(urlsplit(landing.final_url).path)
        assert path_match is not None
        article = path_match.group("article")
        hidden_type = parser.input_value("d_artType")
        hidden_id = parser.input_value("d_id")
        if hidden_type != "A" or hidden_id != article:
            return None, _Failure(
                "identity_mismatch",
                "earticle_hidden_article_id_mismatch",
                landing.status_code,
                "identity_mismatch",
            )
        if not parser.has_class("btn-download"):
            return None, _Failure(
                "access_denied",
                "earticle_download_not_advertised",
                landing.status_code,
                "access_denied",
            )

        article_key = f"A{article}"
        fetch, failure = self._post(
            stage="earticle-download-authorization",
            url=_EARTICLE_AUTH_URL,
            data={"keys": article_key},
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": landing.final_url,
            },
            attempts=attempts,
        )
        if failure is not None:
            return None, failure
        assert fetch is not None
        payload, failure = _json_response(fetch, "earticle")
        if failure is not None:
            return None, failure
        assert payload is not None

        results = payload.get("result")
        if not isinstance(results, list) or len(results) != 1:
            return None, _Failure(
                "invalid_response",
                "invalid_earticle_authorization_result",
                fetch.status_code,
                "invalid_json",
            )
        result = results[0]
        if not isinstance(result, dict) or result.get("art") != article_key:
            return None, _Failure(
                "identity_mismatch",
                "earticle_authorization_article_mismatch",
                fetch.status_code,
                "identity_mismatch",
            )
        decision = result.get("result")
        if decision != "SUCCESS" or payload.get("count") != 1:
            status = "not_found" if decision == "ErrExists" else "access_denied"
            return None, _Failure(
                status,
                f"earticle_authorization_{str(decision or 'rejected').casefold()}",
                fetch.status_code,
                "access_denied",
            )
        token = payload.get("auth")
        if not _bounded_token(token):
            return None, _Failure(
                "invalid_response",
                "invalid_earticle_download_token",
                fetch.status_code,
                "invalid_json",
            )
        url = (
            f"https://{_EARTICLE_FILE_HOST}/PDF/Download?"
            + urlencode({"key": token})
        )
        return (
            ResolverCandidate(
                url=url,
                resolver=f"{PLATFORM_RESOLVER_NAME}:earticle",
                landing_url=landing.final_url,
                is_open_access=None,
            ),
            None,
        )

    def _authorize_kiss(
        self,
        landing: HttpFetch,
        parser: "_PlatformHtmlParser",
        attempts: list[PlatformAttempt],
    ) -> tuple[ResolverCandidate | None, _Failure | None]:
        pdf_keys = {
            match.group("key")
            for value in parser.onclick_values
            for match in _KISS_BUTTON_RE.finditer(value)
        }
        if len(pdf_keys) != 1:
            return None, _Failure(
                "invalid_response" if pdf_keys else "access_denied",
                (
                    "multiple_kiss_pdf_identifiers"
                    if pdf_keys
                    else "kiss_download_not_advertised"
                ),
                landing.status_code,
                "ambiguous" if pdf_keys else "access_denied",
            )
        pdf_key = next(iter(pdf_keys))
        auth_url = (
            f"https://{_KISS_HOST}{_KISS_AUTH_PATH}?"
            + urlencode({"keyn": pdf_key})
        )
        fetch, failure = self._post(
            stage="kiss-download-authorization",
            url=auth_url,
            data={},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": landing.final_url,
            },
            attempts=attempts,
        )
        if failure is not None:
            return None, failure
        assert fetch is not None
        payload, failure = _json_response(fetch, "kiss")
        if failure is not None:
            return None, failure
        assert payload is not None

        if payload.get("resultCode") != "Success":
            return None, _Failure(
                "access_denied",
                "kiss_authorization_rejected",
                fetch.status_code,
                "access_denied",
            )
        down = payload.get("down")
        code = payload.get("code")
        if not (
            isinstance(down, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{3,64}", down)
            and isinstance(code, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{3,128}", code)
        ):
            return None, _Failure(
                "invalid_response",
                "invalid_kiss_download_token",
                fetch.status_code,
                "invalid_json",
            )
        url = (
            f"https://{_KISS_FILE_HOST}/File/Down?"
            + urlencode({"keyn": down, "code": code})
        )
        return (
            ResolverCandidate(
                url=url,
                resolver=f"{PLATFORM_RESOLVER_NAME}:kiss",
                landing_url=landing.final_url,
                is_open_access=None,
            ),
            None,
        )

    def _authorize_kyobo(
        self,
        landing: HttpFetch,
        parser: "_PlatformHtmlParser",
    ) -> tuple[ResolverCandidate | None, _Failure | None]:
        advertised = set(parser.meta_values("citation_pdf_url"))
        advertised.update(
            urljoin(landing.final_url, href)
            for href in parser.anchor_hrefs
            if _DIRECT_PDF_PATH_RE.search(urlsplit(href).path)
        )
        valid = {url for url in advertised if _valid_kyobo_pdf_url(url)}
        if advertised != valid:
            return None, _Failure(
                "invalid_response",
                "unsafe_kyobo_pdf_url",
                landing.status_code,
                "unsafe_url",
            )
        if len(valid) != 1:
            return None, _Failure(
                "access_denied" if not valid else "invalid_response",
                (
                    "kyobo_requires_authenticated_file_authorization"
                    if not valid
                    else "multiple_kyobo_pdf_urls"
                ),
                landing.status_code,
                "access_denied" if not valid else "ambiguous",
            )
        url = next(iter(valid))
        return (
            ResolverCandidate(
                url=url,
                resolver=f"{PLATFORM_RESOLVER_NAME}:kyobo",
                landing_url=landing.final_url,
                is_open_access=None,
            ),
            None,
        )

    def _get(
        self,
        *,
        stage: str,
        url: str,
        max_bytes: int,
        max_html_bytes: int,
        headers: Mapping[str, str],
        attempts: list[PlatformAttempt],
    ) -> tuple[HttpFetch | None, _Failure | None]:
        try:
            fetch = self.http.get_bytes(
                url,
                max_bytes=max_bytes,
                max_html_bytes=max_html_bytes,
                headers=headers,
            )
        except HttpClientError as exc:
            attempts.append(
                PlatformAttempt(
                    stage=stage,
                    url=_redact_audit_url(url),
                    status="failed",
                    error_code=getattr(exc, "code", "http_error"),
                )
            )
            return None, _Failure(
                "request_error",
                f"{stage}_request_failed",
                error_code=getattr(exc, "code", "http_error"),
            )
        attempts.append(
            PlatformAttempt(
                stage=stage,
                url=_redact_audit_url(fetch.requested_url),
                status="fetched",
                http_status=fetch.status_code,
                final_url=_redact_audit_url(fetch.final_url),
            )
        )
        return fetch, None

    def _post(
        self,
        *,
        stage: str,
        url: str,
        data: Mapping[str, str],
        headers: Mapping[str, str],
        attempts: list[PlatformAttempt],
    ) -> tuple[HttpFetch | None, _Failure | None]:
        try:
            fetch = self.http.post_form_bytes(
                url,
                data=data,
                max_bytes=self.config.max_auth_bytes,
                headers=headers,
            )
        except HttpClientError as exc:
            attempts.append(
                PlatformAttempt(
                    stage=stage,
                    url=_redact_audit_url(url),
                    status="failed",
                    error_code=getattr(exc, "code", "http_error"),
                )
            )
            return None, _Failure(
                "request_error",
                f"{stage}_request_failed",
                error_code=getattr(exc, "code", "http_error"),
            )
        attempts.append(
            PlatformAttempt(
                stage=stage,
                url=_redact_audit_url(fetch.requested_url),
                status="fetched",
                http_status=fetch.status_code,
                final_url=_redact_audit_url(fetch.final_url),
            )
        )
        if fetch.redirects or fetch.final_url != fetch.requested_url:
            return None, _Failure(
                "invalid_response",
                f"{stage}_unexpected_redirect",
                fetch.status_code,
                "unsafe_redirect",
            )
        return fetch, None

    @staticmethod
    def _failure(
        paper: PaperCandidate,
        failure: _Failure,
        attempts: list[PlatformAttempt],
        *,
        platform: str | None = None,
        landing_url: str | None = None,
        matched_title: str | None = None,
        matched_doi: str | None = None,
        candidate: ResolverCandidate | None = None,
    ) -> PlatformResolution:
        return PlatformResolution(
            status=failure.status,
            reason=failure.reason,
            paper=paper,
            platform=platform,
            candidate=candidate,
            matched_title=matched_title,
            matched_doi=matched_doi,
            landing_url=landing_url,
            attempts=tuple(attempts),
            http_status=failure.http_status,
            error_code=failure.error_code,
        )


class _PlatformHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metas: dict[str, list[str]] = {}
        self.inputs: dict[str, list[str]] = {}
        self.onclick_values: list[str] = []
        self.anchor_hrefs: list[str] = []
        self.classes: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {
            key.casefold(): value
            for key, value in attrs
            if value is not None
        }
        classes = values.get("class", "")
        self.classes.update(
            item.casefold()
            for item in classes.split()
            if item
        )
        onclick = values.get("onclick")
        if onclick:
            self.onclick_values.append(onclick)
        lowered_tag = tag.casefold()
        if lowered_tag == "meta":
            name = (values.get("name") or values.get("property") or "").casefold()
            content = values.get("content")
            if name and content:
                self.metas.setdefault(name, []).append(content.strip())
        elif lowered_tag == "input":
            key = values.get("id") or values.get("name")
            value = values.get("value")
            if key and value is not None:
                self.inputs.setdefault(key, []).append(value.strip())
        elif lowered_tag == "a":
            href = values.get("href")
            if href:
                self.anchor_hrefs.append(href.strip())

    def meta_values(self, name: str) -> tuple[str, ...]:
        return tuple(self.metas.get(name.casefold(), ()))

    def input_value(self, name: str) -> str | None:
        values = self.inputs.get(name, ())
        return values[0] if len(values) == 1 else None

    def has_class(self, name: str) -> bool:
        return name.casefold() in self.classes


class _EarticleSearchParser(HTMLParser):
    """Parse only the identity fields in official eArticle result rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[_EarticleSearchResult] = []
        self.invalid = False
        self.saw_search_result = False
        self._page_title_parts: list[str] = []
        self._in_page_title = False
        self._div_depth = 0
        self._result_depth: int | None = None
        self._builder: _EarticleSearchResultBuilder | None = None
        self._section: str | None = None
        self._title_href: str | None = None
        self._title_parts: list[str] | None = None
        self._author_parts: list[str] | None = None
        self._date_parts: list[str] | None = None

    @property
    def page_title(self) -> str:
        return _plain_search_text("".join(self._page_title_parts))

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered_tag = tag.casefold()
        values = {
            key.casefold(): value
            for key, value in attrs
            if value is not None
        }
        classes = {
            item.casefold()
            for item in values.get("class", "").split()
            if item
        }
        if lowered_tag == "title":
            self._in_page_title = True
        if lowered_tag == "div":
            self._div_depth += 1
            if "search_result" in classes:
                self.saw_search_result = True
            if "art-summary" in classes:
                if self._builder is not None:
                    self.invalid = True
                else:
                    self._builder = _EarticleSearchResultBuilder()
                    self._result_depth = self._div_depth
        if self._builder is None:
            return
        if lowered_tag == "p":
            section = next(
                (
                    value
                    for value in ("title", "author", "pubinfo")
                    if value in classes
                ),
                None,
            )
            if self._section is not None:
                self.invalid = True
            self._section = section
        elif lowered_tag == "a" and self._section == "title":
            if self._title_parts is not None:
                self.invalid = True
            self._title_href = values.get("href", "")
            self._title_parts = []
        elif lowered_tag == "a" and self._section == "author":
            if self._author_parts is not None:
                self.invalid = True
            self._author_parts = []
        elif (
            lowered_tag == "span"
            and self._section == "pubinfo"
            and "dt" in classes
        ):
            if self._date_parts is not None:
                self.invalid = True
            title = values.get("title")
            self._date_parts = [title] if title else []

    def handle_data(self, data: str) -> None:
        if self._in_page_title:
            self._page_title_parts.append(data)
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._author_parts is not None:
            self._author_parts.append(data)
        if self._date_parts is not None:
            self._date_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered_tag = tag.casefold()
        if lowered_tag == "title":
            self._in_page_title = False
        if lowered_tag == "a" and self._title_parts is not None:
            assert self._builder is not None
            self._builder.title_links.append(
                (
                    self._title_href or "",
                    _plain_search_text("".join(self._title_parts)),
                )
            )
            self._title_href = None
            self._title_parts = None
        elif lowered_tag == "a" and self._author_parts is not None:
            assert self._builder is not None
            author = _plain_search_text("".join(self._author_parts))
            if author:
                self._builder.authors.append(author)
            self._author_parts = None
        elif lowered_tag == "span" and self._date_parts is not None:
            assert self._builder is not None
            date = _plain_search_text(" ".join(self._date_parts))
            if date:
                self._builder.date_values.append(date)
            self._date_parts = None
        if lowered_tag == "p" and self._builder is not None:
            self._section = None
        if lowered_tag != "div":
            return
        if (
            self._builder is not None
            and self._result_depth == self._div_depth
        ):
            self._finish_result()
        if self._div_depth < 1:
            self.invalid = True
        else:
            self._div_depth -= 1

    def close(self) -> None:
        super().close()
        if (
            self._builder is not None
            or self._result_depth is not None
            or self._div_depth != 0
            or self._title_parts is not None
            or self._author_parts is not None
            or self._date_parts is not None
        ):
            self.invalid = True

    def _finish_result(self) -> None:
        assert self._builder is not None
        builder = self._builder
        self._builder = None
        self._result_depth = None
        self._section = None
        if (
            self._title_parts is not None
            or self._author_parts is not None
            or self._date_parts is not None
            or len(builder.title_links) != 1
        ):
            self.invalid = True
            self._title_href = None
            self._title_parts = None
            self._author_parts = None
            self._date_parts = None
            return
        href, title = builder.title_links[0]
        article_path = _earticle_article_path(href)
        years = {
            int(match.group(0))
            for value in builder.date_values
            for match in re.finditer(
                r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])",
                value,
            )
        }
        if article_path is None or not title or len(years) != 1:
            self.invalid = True
            return
        self.results.append(
            _EarticleSearchResult(
                article_path=article_path,
                title=title,
                publication_year=next(iter(years)),
                authors=tuple(builder.authors),
            )
        )


def _validate_earticle_search_fetch(
    fetch: HttpFetch,
    *,
    expected_url: str,
    expected_title: str,
) -> _Failure | None:
    if not 200 <= fetch.status_code < 300:
        status = (
            "retryable_error"
            if fetch.status_code in _RETRYABLE_HTTP_STATUSES
            else "access_denied"
            if fetch.status_code in {401, 403}
            else "http_error"
        )
        return _Failure(
            status,
            "earticle_search_http_error",
            fetch.status_code,
            "http_status",
        )
    if (
        not _valid_earticle_search_url(
            expected_url,
            expected_title=expected_title,
        )
        or fetch.requested_url != expected_url
        or fetch.final_url != expected_url
        or fetch.redirects
    ):
        return _Failure(
            "invalid_response",
            "earticle_search_unexpected_redirect",
            fetch.status_code,
            "unsafe_redirect",
        )
    return None


def _select_earticle_search_result(
    parser: _EarticleSearchParser,
    paper: PaperCandidate,
    *,
    known_titles: tuple[tuple[str, str], ...],
) -> tuple[
    _EarticleSearchResult | None,
    tuple[str, ...],
    _Failure | None,
]:
    if parser.invalid:
        return None, (), _Failure(
            "invalid_response",
            "invalid_earticle_search_result_structure",
            error_code="invalid_html",
        )
    if not parser.saw_search_result:
        page_title = (normalize_title(parser.page_title) or "").casefold()
        if "로그인" in page_title or "login" in page_title:
            return None, (), _Failure(
                "access_denied",
                "earticle_search_returned_login_page",
                error_code="access_denied",
            )
        return None, (), _Failure(
            "invalid_response",
            "missing_earticle_search_result_container",
            error_code="invalid_html",
        )

    normalized_known = {normalized for _title, normalized in known_titles}
    title_matches = [
        result
        for result in parser.results
        if normalize_title(result.title) in normalized_known
    ]
    if not title_matches:
        return None, (), _Failure(
            "not_found",
            "earticle_exact_title_not_found",
            error_code="not_found",
        )
    assert paper.publication_year is not None
    year_matches = [
        result
        for result in title_matches
        if result.publication_year == paper.publication_year
    ]
    if not year_matches:
        return None, (), _Failure(
            "identity_mismatch",
            "earticle_search_year_mismatch",
            error_code="identity_mismatch",
        )

    provenance = [
        "earticle_search_title_normalized_exact",
        "earticle_search_year_exact",
    ]
    expected_authors = _earticle_author_keys(paper.authors)
    candidates = year_matches
    if expected_authors:
        candidates = [
            result
            for result in year_matches
            if _earticle_author_keys(result.authors) == expected_authors
        ]
        if not candidates:
            return None, (), _Failure(
                "identity_mismatch",
                "earticle_search_author_mismatch",
                error_code="identity_mismatch",
            )
        provenance.append("earticle_search_authors_exact")
    if len(candidates) != 1:
        return None, (), _Failure(
            "ambiguous",
            "ambiguous_earticle_exact_title_results",
            error_code="ambiguous",
        )
    return (
        candidates[0],
        (
            *provenance,
            "earticle_search_single_article_exact",
        ),
        None,
    )


def _validate_landing_fetch(
    fetch: HttpFetch,
    *,
    via_doi: bool,
) -> tuple[str | None, _Failure | None]:
    if not 200 <= fetch.status_code < 300:
        status = (
            "retryable_error"
            if fetch.status_code in _RETRYABLE_HTTP_STATUSES
            else "http_error"
        )
        return None, _Failure(
            status,
            "platform_landing_http_error",
            fetch.status_code,
            "http_status",
        )
    urls = [fetch.requested_url, *fetch.redirects, fetch.final_url]
    if via_doi:
        first = urlsplit(urls[0])
        if (
            first.scheme.casefold() != "https"
            or (first.hostname or "").casefold() != _DOI_HOST
            or first.query
            or first.fragment
        ):
            return None, _Failure(
                "invalid_response",
                "invalid_doi_request_url",
                fetch.status_code,
                "unsafe_url",
            )
    platform = _platform_from_landing_url(fetch.final_url)
    if platform is None:
        return None, _Failure(
            "unsupported",
            "unsupported_platform_landing",
            fetch.status_code,
        )
    allowed_hosts = {_DOI_HOST, _platform_host(platform)}
    for url in urls:
        parsed = urlsplit(url)
        if (
            parsed.scheme.casefold() != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (parsed.hostname or "").casefold() not in allowed_hosts
            or parsed.port not in {None, 443}
        ):
            return None, _Failure(
                "invalid_response",
                "platform_landing_redirect_outside_allowlist",
                fetch.status_code,
                "unsafe_redirect",
            )
    return platform, None


def _platform_from_landing_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() != "https" or port not in {None, 443}:
        return None
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return None
    host = (parsed.hostname or "").casefold()
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if host == _DBPIA_HOST and parsed.path == _DBPIA_ARTICLE_PATH:
        if len(pairs) == 1 and pairs[0][0] == "nodeId" and _NODE_ID_RE.fullmatch(pairs[0][1]):
            return "dbpia"
    if (
        host == _DBPIA_HOST
        and _DBPIA_LEGACY_ARTICLE_PATH_RE.fullmatch(parsed.path)
        and _valid_dbpia_search_context(parsed.query)
    ):
        return "dbpia"
    if (
        host == _DBPIA_HOST
        and _DBPIA_NODE_ARTICLE_PATH_RE.fullmatch(parsed.path)
        and _valid_dbpia_search_context(parsed.query)
    ):
        return "dbpia"
    if host == _EARTICLE_HOST and _EARTICLE_PATH_RE.fullmatch(parsed.path):
        if not pairs:
            return "earticle"
    if host == _KISS_HOST and parsed.path == "/Detail/Ar":
        if len(pairs) == 1 and pairs[0][0] == "key" and pairs[0][1]:
            return "kiss"
    if host == _KYOBO_HOST and _KYOBO_PATH_RE.fullmatch(parsed.path):
        if not pairs:
            return "kyobo"
    return None


def _canonicalize_dbpia_viewer_url(
    url: str,
) -> tuple[str | None, tuple[str, ...], _Failure | None]:
    """Turn one exact DBpia viewer URL into its metadata-rich article page.

    The viewer is never fetched or parsed.  Only its bounded ``NODE`` value is
    retained, while presentation-only width is discarded.  Viewer-shaped URLs
    on another origin, with duplicate/unknown parameters, or with URL
    credentials fail before any network request.
    """

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None, (), _Failure(
            "invalid_response",
            "invalid_dbpia_viewer_url",
            error_code="unsafe_url",
        )
    if parsed.path not in _DBPIA_VIEWER_PATHS:
        return url, (), None
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        pairs = []
    names = [name for name, _value in pairs]
    values = dict(pairs)
    valid_names = (
        len(names) in {1, 2}
        and len(set(names)) == len(names)
        and set(names).issubset({"nodeId", "width"})
        and "nodeId" in names
    )
    width = values.get("width")
    if not (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == _DBPIA_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and len(url) <= 2_048
        and valid_names
        and _NODE_ID_RE.fullmatch(values.get("nodeId", ""))
        and (
            width is None
            or _DBPIA_VIEWER_WIDTH_RE.fullmatch(width) is not None
        )
    ):
        return None, (), _Failure(
            "invalid_response",
            "invalid_dbpia_viewer_url",
            error_code="unsafe_url",
        )
    node_id = values["nodeId"]
    return (
        f"https://{_DBPIA_HOST}{_DBPIA_ARTICLE_PATH}?"
        + urlencode({"nodeId": node_id}),
        (
            "dbpia_viewer_node_exact",
            "dbpia_viewer_canonical_article_landing",
        ),
        None,
    )


def _platform_host(platform: str) -> str:
    return {
        "dbpia": _DBPIA_HOST,
        "earticle": _EARTICLE_HOST,
        "kiss": _KISS_HOST,
        "kyobo": _KYOBO_HOST,
    }[platform]


def _dbpia_authorization_provenance(landing_url: str) -> tuple[str, ...]:
    parsed = urlsplit(landing_url)
    if (
        _DBPIA_LEGACY_ARTICLE_PATH_RE.fullmatch(parsed.path)
        and _valid_dbpia_search_context(parsed.query)
    ):
        return (
            "dbpia_legacy_article_path_exact",
            "dbpia_unique_download_button_node_exact",
            "dbpia_authorization_node_exact",
        )
    return (
        "dbpia_landing_node_exact",
        "dbpia_download_button_node_exact",
        "dbpia_authorization_node_exact",
    )


def _valid_dbpia_search_context(query: str) -> bool:
    if not query:
        return True
    if len(query) > 2_048:
        return False
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return False
    names = [name for name, _value in pairs]
    return bool(
        1 <= len(pairs) <= len(_DBPIA_SEARCH_CONTEXT_NAMES)
        and len(set(names)) == len(names)
        and set(names).issubset(_DBPIA_SEARCH_CONTEXT_NAMES)
        and all(
            value
            and len(value) <= 1_024
            and all(
                ord(character) >= 32 and ord(character) != 127
                for character in value
            )
            for _name, value in pairs
        )
    )


def _dbpia_expected_journal(value: str | None) -> str:
    raw = (value or "").strip()
    if raw.casefold() == "ser000003109":
        raw = "디지털포렌식연구"
    return normalize_title(raw)


def _dbpia_landing_bibliography_provenance(
    parser: _PlatformHtmlParser,
    paper: PaperCandidate,
) -> tuple[tuple[str, ...], _Failure | None]:
    """Bind a DBpia spacing-only title variant without trusting a blank DOI.

    Some DBpia records omit ``citation_doi`` and insert a word boundary that
    is absent from KCI's title.  In that narrow case the canonical article
    page must independently agree on every available publisher, journal,
    volume/issue, page-range, and author value before its download button may
    be used.  The downloaded PDF is still subjected to the ordinary DOI/title
    identity checks later in the resolver.
    """

    missing = _Failure(
        "missing_metadata",
        "missing_dbpia_bibliographic_identity",
        error_code="missing_metadata",
    )
    mismatch = _Failure(
        "identity_mismatch",
        "dbpia_landing_bibliography_mismatch",
        error_code="identity_mismatch",
    )

    expected_publisher = normalize_title(paper.institution_id or "")
    if not expected_publisher:
        return (), missing
    publisher_values = {
        normalize_title(value)
        for name in ("publisher", "citation_publisher", "dc.publisher")
        for value in parser.meta_values(name)
        if normalize_title(value)
    }
    if not publisher_values:
        return (), missing
    if publisher_values != {expected_publisher}:
        return (), mismatch

    expected_journal = _dbpia_expected_journal(paper.journal_id)
    if not expected_journal:
        return (), missing
    journal_values = {
        normalize_title(value)
        for value in parser.meta_values("citation_journal_title")
        if normalize_title(value)
    }
    if not journal_values:
        return (), missing
    if journal_values != {expected_journal}:
        return (), mismatch

    issue_match = re.fullmatch(
        r"(?P<volume>[0-9]{1,6})_(?P<issue>[0-9]{1,6})",
        (paper.issue_id or "").strip(),
    )
    issue_only_match = re.fullmatch(
        r"(?P<issue>[0-9]{1,6})",
        (paper.issue_id or "").strip(),
    )
    if issue_match is None and issue_only_match is None:
        return (), missing
    raw_volume_values = parser.meta_values("citation_volume")
    raw_issue_values = parser.meta_values("citation_issue")
    issue_values = _integer_meta_values(parser, "citation_issue")
    if issue_match is not None:
        expected_volume = int(issue_match.group("volume"))
        expected_issue = int(issue_match.group("issue"))
        volume_values = _integer_meta_values(parser, "citation_volume")
        if not raw_volume_values or not raw_issue_values:
            return (), missing
        if (
            volume_values != {expected_volume}
            or issue_values != {expected_issue}
        ):
            return (), mismatch
        issue_provenance = "dbpia_landing_volume_issue_exact"
    else:
        assert issue_only_match is not None
        expected_issue = int(issue_only_match.group("issue"))
        # An issue-only KCI record cannot verify a DBpia volume.  Accept it
        # only when DBpia also publishes no volume value and its issue is exact.
        if raw_volume_values:
            return (), mismatch
        if not raw_issue_values:
            return (), missing
        if issue_values != {expected_issue}:
            return (), mismatch
        issue_provenance = "dbpia_landing_issue_only_exact"

    first_page = (paper.first_page or "").strip()
    final_page = (paper.final_page or "").strip()
    if not first_page or not final_page:
        return (), missing
    observed_first = {
        value.strip()
        for value in parser.meta_values("citation_firstpage")
        if value.strip()
    }
    observed_final = {
        value.strip()
        for value in parser.meta_values("citation_lastpage")
        if value.strip()
    }
    if not observed_first or not observed_final:
        return (), missing
    if observed_first != {first_page} or observed_final != {final_page}:
        return (), mismatch

    expected_authors = _earticle_author_keys(paper.authors)
    observed_authors = _earticle_author_keys(
        parser.meta_values("citation_author")
    )
    if not expected_authors or not observed_authors:
        return (), missing
    if observed_authors != expected_authors:
        return (), mismatch

    return (
        (
            "dbpia_landing_publisher_exact",
            "dbpia_landing_journal_exact",
            issue_provenance,
            "dbpia_landing_page_range_exact",
            "dbpia_landing_authors_exact",
        ),
        None,
    )


def _integer_meta_values(
    parser: _PlatformHtmlParser,
    name: str,
) -> set[int]:
    values: set[int] = set()
    raw_values = parser.meta_values(name)
    if not raw_values:
        return values
    for value in raw_values:
        stripped = value.strip()
        if not stripped.isascii() or not stripped.isdigit():
            return set()
        values.add(int(stripped))
    return values


def _landing_identity(
    parser: _PlatformHtmlParser,
    paper: PaperCandidate,
    *,
    platform: str,
    expected_doi: str | None,
    known_titles: tuple[tuple[str, str], ...],
) -> tuple[_LandingIdentity | None, _Failure | None]:
    title_values = (
        *parser.meta_values("citation_title"),
        *parser.meta_values("dc.title"),
        *parser.meta_values("og:title"),
    )
    doi_values: set[str] = set()
    for value in (
        *parser.meta_values("citation_doi"),
        *parser.meta_values("dc.identifier"),
        *parser.meta_values("dc.identifier.doi"),
    ):
        try:
            normalized = normalize_doi(value)
        except InvalidDoiError:
            continue
        if normalized:
            doi_values.add(normalized)
    if expected_doi is not None and doi_values and {
        value.casefold() for value in doi_values
    } != {expected_doi.casefold()}:
        return None, _Failure(
            "identity_mismatch",
            "platform_landing_doi_mismatch",
            error_code="identity_mismatch",
        )
    if len({value.casefold() for value in doi_values}) > 1:
        return None, _Failure(
            "identity_mismatch",
            "ambiguous_platform_landing_dois",
            error_code="ambiguous",
        )
    matched_doi = next(iter(doi_values), None)
    doi_exact = bool(
        expected_doi is not None
        and matched_doi is not None
        and matched_doi.casefold() == expected_doi.casefold()
    )

    years: set[int] = set()
    for value in (
        *parser.meta_values("citation_publication_date"),
        *parser.meta_values("citation_year"),
    ):
        match = re.search(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])", value)
        if match:
            years.add(int(match.group(0)))
    if (
        paper.publication_year is not None
        and years
        and years != {paper.publication_year}
    ):
        return None, _Failure(
            "identity_mismatch",
            "platform_landing_year_mismatch",
            error_code="identity_mismatch",
        )
    year_exact = bool(
        paper.publication_year is not None
        and years == {paper.publication_year}
    )

    normalized_known = {normalized for _title, normalized in known_titles}
    exact_titles = {
        value.strip()
        for value in title_values
        if normalize_title(value) in normalized_known
    }
    title_match = "normalized_exact"
    matched_titles = exact_titles
    extra_provenance: tuple[str, ...] = ()
    if not matched_titles:
        compact_known = {
            _compact_title(title)
            for title, _normalized in known_titles
            if len(_compact_title(title)) >= 12
        }
        compact_matches = {
            value.strip()
            for value in title_values
            if _compact_title(value) in compact_known
        }
        if compact_matches and doi_exact and year_exact:
            matched_titles = compact_matches
            title_match = "compact_exact"
        elif compact_matches and platform == "dbpia" and year_exact:
            extra_provenance, bibliography_failure = (
                _dbpia_landing_bibliography_provenance(parser, paper)
            )
            if bibliography_failure is not None:
                return None, bibliography_failure
            matched_titles = compact_matches
            title_match = "dbpia_bibliographic_compact_exact"
    if not matched_titles:
        return None, _Failure(
            "identity_mismatch",
            "platform_landing_title_mismatch",
            error_code="identity_mismatch",
        )
    if platform == "dbpia" and not doi_exact:
        if not year_exact:
            return None, _Failure(
                "missing_metadata",
                "missing_dbpia_bibliographic_identity",
                error_code="missing_metadata",
            )
        bibliography, bibliography_failure = (
            _dbpia_landing_bibliography_provenance(parser, paper)
        )
        if bibliography_failure is not None:
            return None, bibliography_failure
        # DOI-less identity must never collapse to title+year alone. KCI
        # metadata enrichment must supply the source author list first, then
        # DBpia must expose the exact same complete author set.
        if "dbpia_landing_authors_exact" not in bibliography:
            return None, _Failure(
                "missing_metadata",
                "missing_dbpia_bibliographic_identity",
                error_code="missing_metadata",
            )
        extra_provenance = tuple(
            dict.fromkeys((*extra_provenance, *bibliography))
        )
    compact_matches = {_compact_title(value) for value in matched_titles}
    if len(compact_matches) != 1:
        return None, _Failure(
            "identity_mismatch",
            "ambiguous_platform_landing_titles",
            error_code="ambiguous",
        )
    matched_title = sorted(
        matched_titles,
        key=lambda value: (len(value), value),
    )[0]
    return (
        _LandingIdentity(
            title=matched_title,
            doi=matched_doi,
            title_match=title_match,
            doi_exact=doi_exact,
            year_exact=year_exact,
            provenance=extra_provenance,
        ),
        None,
    )


def validate_dbpia_landing_document(
    document: str,
    paper: PaperCandidate,
) -> DbpiaLandingIdentity:
    """Validate DBpia article metadata without retaining its article URL.

    The authenticated browser resolver uses this pure helper before it clicks
    anything.  Keeping the identity policy here ensures that the ordinary HTTP
    and browser-assisted workflows apply the same exact title/DOI/year rules,
    including the narrowly audited compact-title fallback.
    """

    if not isinstance(document, str) or not document:
        raise DbpiaLandingIdentityError(
            "invalid_response",
            "empty_dbpia_landing_document",
            "invalid_html",
        )
    parser = _PlatformHtmlParser()
    try:
        parser.feed(document)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise DbpiaLandingIdentityError(
            "invalid_response",
            "invalid_dbpia_landing_html",
            "invalid_html",
        ) from exc

    known_titles = tuple(paper_title_queries(paper))
    if not known_titles:
        raise DbpiaLandingIdentityError(
            "missing_metadata",
            "missing_known_title",
            "missing_metadata",
        )
    identity, failure = _landing_identity(
        parser,
        paper,
        platform="dbpia",
        expected_doi=_paper_doi(paper),
        known_titles=known_titles,
    )
    if failure is not None:
        raise DbpiaLandingIdentityError(
            failure.status,
            failure.reason,
            failure.error_code,
        )
    assert identity is not None
    return DbpiaLandingIdentity(
        title=identity.title,
        doi=identity.doi,
        title_match=identity.title_match,
        doi_exact=identity.doi_exact,
        year_exact=identity.year_exact,
        provenance=identity.provenance,
    )


def _landing_provenance(identity: _LandingIdentity) -> tuple[str, ...]:
    evidence = ["landing_title_compact_exact"]
    if identity.title_match == "normalized_exact":
        evidence.append("landing_title_normalized_exact")
    if identity.doi_exact:
        evidence.append("landing_doi_exact")
    if identity.year_exact:
        evidence.append("landing_year_exact")
    evidence.extend(identity.provenance)
    return tuple(evidence)


def _json_response(
    fetch: HttpFetch,
    platform: str,
) -> tuple[dict[str, object] | None, _Failure | None]:
    if not 200 <= fetch.status_code < 300:
        status = (
            "retryable_error"
            if fetch.status_code in _RETRYABLE_HTTP_STATUSES
            else "access_denied"
            if fetch.status_code in {401, 403}
            else "http_error"
        )
        return None, _Failure(
            status,
            f"{platform}_authorization_http_error",
            fetch.status_code,
            "http_status",
        )
    try:
        document = fetch.body.decode("utf-8")
        payload = json.loads(document)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _Failure(
            "invalid_response",
            f"invalid_{platform}_authorization_json",
            fetch.status_code,
            "invalid_json",
        )
    if not isinstance(payload, dict):
        return None, _Failure(
            "invalid_response",
            f"invalid_{platform}_authorization_envelope",
            fetch.status_code,
            "invalid_json",
        )
    return payload, None


def _validate_pdf_fetch_urls(
    platform: str,
    fetch: HttpFetch,
) -> _Failure | None:
    urls = [fetch.requested_url, *fetch.redirects, fetch.final_url]
    if platform == "dbpia":
        if not _valid_dbpia_entry_url(urls[0]):
            return _unsafe_pdf_redirect(platform)
        for url in urls[1:]:
            if not (
                _valid_dbpia_entry_url(url)
                or _valid_dbpia_cdn_url(url)
            ):
                return _unsafe_pdf_redirect(platform)
        if not (
            _valid_dbpia_entry_url(fetch.final_url)
            or _valid_dbpia_cdn_url(fetch.final_url)
        ):
            return _unsafe_pdf_redirect(platform)
        return None
    validator = {
        "earticle": _valid_earticle_pdf_url,
        "kiss": _valid_kiss_pdf_url,
        "kyobo": _valid_kyobo_pdf_url,
    }.get(platform)
    if validator is None or any(not validator(url) for url in urls):
        return _unsafe_pdf_redirect(platform)
    return None


def _unsafe_pdf_redirect(platform: str) -> _Failure:
    return _Failure(
        "invalid_response",
        f"{platform}_pdf_redirect_outside_allowlist",
        error_code="unsafe_redirect",
    )


def _valid_dbpia_entry_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        _basic_https_url(parsed, _DBPIA_DOWNLOAD_HOST)
        and parsed.path == "/File/Download"
        and _exact_query(parsed.query, {"key"})
    )


def _valid_dbpia_cdn_url(url: str) -> bool:
    parsed = urlsplit(url)
    keys = {key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    return (
        _basic_https_url(parsed, _DBPIA_CDN_HOST)
        and bool(_DBPIA_CDN_PATH_RE.fullmatch(parsed.path))
        and keys.issubset({"filename", "jwt"})
        and _unique_nonempty_query(parsed.query)
    )


def _dbpia_filename_title_prefix(
    url: str,
    landing_title: str,
) -> bool:
    """Validate a bounded official CDN filename as a substantial title prefix."""

    if not _valid_dbpia_cdn_url(url):
        return False
    values = [
        value
        for key, value in parse_qsl(
            urlsplit(url).query,
            keep_blank_values=True,
        )
        if key == "filename"
    ]
    if len(values) != 1:
        return False
    filename = unicodedata.normalize("NFKC", html.unescape(values[0])).strip()
    if (
        not filename
        or len(filename) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or "/" in filename
        or "\\" in filename
    ):
        return False
    if filename.casefold().endswith(".pdf"):
        filename = filename[:-4].rstrip()
    filename_key = _compact_title(filename)
    title_key = _compact_title(landing_title)
    return (
        len(filename_key) >= 20
        and len(title_key) >= len(filename_key)
        and len(filename_key) / len(title_key) >= 0.65
        and title_key.startswith(filename_key)
    )


def _valid_earticle_pdf_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        _basic_https_url(parsed, _EARTICLE_FILE_HOST)
        and parsed.path == "/PDF/Download"
        and _exact_query(parsed.query, {"key"})
    )


def _safe_earticle_search_title(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    if any(
        unicodedata.category(character).startswith("C")
        for character in normalized
    ):
        return None
    normalized = normalized.strip()
    if not 1 <= len(normalized) <= _MAX_EARTICLE_SEARCH_TITLE_CHARS:
        return None
    return normalized


def _valid_earticle_search_url(
    url: str,
    *,
    expected_title: str,
) -> bool:
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    return (
        _basic_https_url(parsed, _EARTICLE_HOST)
        and parsed.path == _EARTICLE_SEARCH_PATH
        and len(url) <= 4_096
        and len(pairs) == 2
        and {key for key, _value in pairs} == {"sf", "q"}
        and len({key for key, _value in pairs}) == 2
        and dict(pairs) == {"sf": "2", "q": expected_title}
    )


def _earticle_article_path(value: str) -> str | None:
    parsed = urlsplit(html.unescape(value).strip())
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not _EARTICLE_PATH_RE.fullmatch(parsed.path)
    ):
        return None
    return parsed.path


def _earticle_author_keys(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
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
        result.append(key)
    return tuple(result)


def _plain_search_text(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", html.unescape(value)).split()
    )


def _valid_kiss_pdf_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        _basic_https_url(parsed, _KISS_FILE_HOST)
        and parsed.path == "/File/Down"
        and _exact_query(parsed.query, {"keyn", "code"})
    )


def _valid_kyobo_pdf_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        _basic_https_url(parsed, {_KYOBO_HOST, _KYOBO_CONTENT_HOST})
        and bool(_DIRECT_PDF_PATH_RE.search(parsed.path))
        and _unique_nonempty_query(parsed.query)
    )


def _basic_https_url(parsed, host: str | set[str]) -> bool:
    allowed = {host} if isinstance(host, str) else host
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() in allowed
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _exact_query(query: str, keys: set[str]) -> bool:
    pairs = parse_qsl(query, keep_blank_values=True)
    return (
        len(pairs) == len(keys)
        and {key for key, _value in pairs} == keys
        and all(value for _key, value in pairs)
    )


def _unique_nonempty_query(query: str) -> bool:
    pairs = parse_qsl(query, keep_blank_values=True)
    return (
        len({key for key, _value in pairs}) == len(pairs)
        and all(key and value for key, value in pairs)
    )


def _validate_pdf_identity(
    content: bytes,
    paper: PaperCandidate,
    *,
    platform: str,
    expected_doi: str | None,
    known_titles: tuple[tuple[str, str], ...],
    require_identity: bool,
) -> _PdfIdentity:
    if content.find(b"%PDF-", 0, 1024) < 0:
        raise ValueError("platform response does not contain a PDF header")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise ValueError("pypdf is required to validate platform PDFs") from exc
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError("platform PDF is encrypted")
        page_count = len(reader.pages)
    except ValueError:
        raise
    except (PdfReadError, OSError, TypeError) as exc:
        raise ValueError(f"platform PDF parser rejected the response: {exc}") from exc
    if page_count < 1:
        raise ValueError("platform PDF has no pages")
    if page_count > MAX_PDF_PAGES:
        raise ValueError("platform PDF exceeds the page-count safety limit")

    try:
        page_texts = tuple(
            reader.pages[index].extract_text() or ""
            for index in range(min(2, page_count))
        )
        page_text = "\n".join(page_texts)
        metadata_title = (
            str(getattr(reader.metadata, "title", "") or "")
            if reader.metadata is not None
            else ""
        )
    except Exception as exc:
        raise ValueError(f"unable to inspect platform PDF identity: {exc}") from exc

    expected_exact = bool(
        expected_doi
        and _expected_doi_spans(page_text, expected_doi)
    )
    expected_layout = bool(
        expected_doi
        and _expected_doi_layout_spans(page_text, expected_doi)
    )
    observed_dois = _extract_dois(
        page_text,
        expected_doi=expected_doi,
    )
    if expected_doi is not None and any(
        value.casefold() != expected_doi.casefold()
        for value in observed_dois
    ):
        raise ValueError("platform PDF contains a different DOI")

    title_identity = _matching_pdf_title(
        page_text,
        metadata_title,
        known_titles,
    )
    if expected_doi is not None and expected_exact:
        return _PdfIdentity(
            page_count=page_count,
            status="doi_match",
            matched_doi=expected_doi,
        )
    if (
        expected_doi is not None
        and expected_layout
        and title_identity is not None
    ):
        title_status, matched_title = title_identity
        return _PdfIdentity(
            page_count=page_count,
            status="doi_layout_and_title_match",
            matched_doi=expected_doi,
            matched_title=matched_title,
            provenance=(f"pdf_{title_status}",),
        )
    if platform == "kiss":
        matched_title, bibliography = _kiss_pdf_bibliographic_provenance(
            page_texts[0],
            page_count=page_count,
            paper=paper,
            known_titles=known_titles,
        )
        if bibliography:
            return _PdfIdentity(
                page_count=page_count,
                status="kiss_bibliographic_match",
                matched_title=matched_title,
                provenance=bibliography,
            )
    if title_identity is not None:
        title_status, matched_title = title_identity
        return _PdfIdentity(
            page_count=page_count,
            status=title_status,
            matched_title=matched_title,
        )
    if require_identity:
        raise ValueError("platform PDF DOI/title identity could not be confirmed")
    return _PdfIdentity(page_count=page_count, status="unconfirmed")


def validate_dbpia_pdf_document(
    content: bytes,
    paper: PaperCandidate,
    *,
    landing_title: str | None = None,
) -> DbpiaPdfIdentity:
    """Apply the audited DBpia PDF identity rules without any URL evidence."""

    known_titles = list(paper_title_queries(paper))
    normalized_landing = normalize_title(landing_title)
    if (
        isinstance(landing_title, str)
        and normalized_landing is not None
        and normalized_landing
        not in {normalized for _title, normalized in known_titles}
    ):
        known_titles.append((landing_title.strip(), normalized_landing))
    try:
        identity = _validate_pdf_identity(
            content,
            paper,
            platform="dbpia",
            expected_doi=_paper_doi(paper),
            known_titles=tuple(known_titles),
            require_identity=True,
        )
    except ValueError as exc:
        # The underlying messages describe PDF structure/identity only and do
        # not contain browser URLs, cookies, filenames, or profile paths.
        raise DbpiaPdfIdentityError(str(exc)) from exc
    return DbpiaPdfIdentity(
        page_count=identity.page_count,
        status=identity.status,
        matched_doi=identity.matched_doi,
        matched_title=identity.matched_title,
        provenance=identity.provenance,
    )


def _matching_pdf_title(
    page_text: str,
    metadata_title: str,
    known_titles: tuple[tuple[str, str], ...],
) -> tuple[str, str] | None:
    observed_text = _compact_title(page_text)
    metadata_key = _compact_title(metadata_title)
    for title, _normalized in known_titles:
        compact = _compact_title(title)
        if len(compact) >= 12 and compact in observed_text:
            return "title_match", title
        if len(compact) >= 12 and compact == metadata_key:
            return "metadata_title_match", title
    return None


def _kiss_pdf_bibliographic_provenance(
    first_page_text: str,
    *,
    page_count: int,
    paper: PaperCandidate,
    known_titles: tuple[tuple[str, str], ...],
) -> tuple[str | None, tuple[str, ...]]:
    """Bind one KISS PDF through exact first-page bibliographic fields."""

    if (
        not paper.journal_id
        or not paper.issue_id
        or not paper.first_page
        or not paper.final_page
    ):
        return None, ()
    try:
        issue = int(paper.issue_id)
        first_page = int(paper.first_page)
        final_page = int(paper.final_page)
    except ValueError:
        return None, ()
    if (
        issue < 1
        or first_page < 1
        or final_page < first_page
        or page_count != final_page - first_page + 1
    ):
        return None, ()

    normalized_text = unicodedata.normalize(
        "NFKC",
        html.unescape(first_page_text),
    )
    title_text = re.sub(
        r"(?m)^[ \t]*[0-9]{1,2}\s*[)]\s*",
        "",
        normalized_text,
    )
    observed_title = _compact_title(title_text)
    title_matches = [
        title
        for title, _normalized in known_titles
        if len(_compact_title(title)) >= 12
        and _compact_title(title) in observed_title
    ]
    if not title_matches:
        return None, ()
    journal_key = _compact_title(paper.journal_id)
    if len(journal_key) < 2 or journal_key not in _compact_title(normalized_text):
        return None, ()
    if re.search(
        rf"(?:제\s*{issue}\s*호|vol[.]?\s*{issue}(?![0-9]))",
        normalized_text,
        flags=re.IGNORECASE,
    ) is None:
        return None, ()
    if re.search(
        rf"(?<![0-9]){first_page}\s*(?:[-~–—−])\s*"
        rf"{final_page}(?![0-9])",
        normalized_text,
    ) is None:
        return None, ()
    return (
        title_matches[0],
        (
            "kiss_pdf_title_exact",
            "kiss_pdf_journal_exact",
            "kiss_pdf_issue_exact",
            "kiss_pdf_page_range_exact",
            "kiss_pdf_page_count_exact",
        ),
    )


def _extract_dois(
    text: str,
    *,
    expected_doi: str | None = None,
) -> set[str]:
    values: set[str] = set()
    spans = (
        (
            *_expected_doi_spans(text, expected_doi),
            *_expected_doi_layout_spans(text, expected_doi),
        )
        if expected_doi is not None
        else ()
    )
    searchable = text
    if spans:
        characters = list(text)
        for start, end in spans:
            characters[start:end] = " " * (end - start)
        searchable = "".join(characters)
        values.add(expected_doi)
    for match in _DOI_IN_TEXT_RE.finditer(searchable):
        candidate = match.group(0).rstrip(".,;:")
        try:
            normalized = normalize_doi(candidate)
        except InvalidDoiError:
            continue
        if normalized:
            values.add(normalized)
    return values


def _contains_expected_doi(text: str, expected_doi: str) -> bool:
    """Match the known DOI before generic extraction, with safe boundaries.

    PDF text extraction frequently removes layout whitespace.  The official
    DOI may consequently be followed by a Korean word, an institutional
    parenthesis, or a numbered Korean title such as ``4차``.  Those are
    accepted terminators, while an ASCII DOI continuation or a bare appended
    digit is rejected.
    """

    return bool(_expected_doi_spans(text, expected_doi))


def _expected_doi_spans(
    text: str,
    expected_doi: str,
) -> tuple[tuple[int, int], ...]:
    if not expected_doi:
        return ()
    spans: list[tuple[int, int]] = []
    for match in re.finditer(
        re.escape(expected_doi),
        text,
        flags=re.IGNORECASE,
    ):
        start, end = match.span()
        if start and text[start - 1].isascii() and text[start - 1].isalnum():
            continue
        if end < len(text) and not _safe_expected_doi_terminator(text, end):
            continue
        spans.append((start, end))
    return tuple(spans)


def _expected_doi_layout_spans(
    text: str,
    expected_doi: str,
) -> tuple[tuple[int, int], ...]:
    """Match only the exact expected DOI with bounded ASCII layout whitespace."""

    if not expected_doi:
        return ()
    separator = r"[ \t\r\n]{0,8}"
    pattern = separator.join(re.escape(character) for character in expected_doi)
    spans: list[tuple[int, int]] = []
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        start, end = match.span()
        value = match.group(0)
        whitespace_count = sum(
            character in " \t\r\n"
            for character in value
        )
        if not 1 <= whitespace_count <= 32:
            continue
        if start and text[start - 1].isascii() and text[start - 1].isalnum():
            continue
        if end < len(text) and not _safe_expected_doi_terminator(text, end):
            continue
        spans.append((start, end))
    return tuple(spans)


def _safe_expected_doi_terminator(text: str, index: int) -> bool:
    character = text[index]
    if character.isspace() or not character.isascii():
        return True
    if character in "([{<,!?\"'":
        return True
    if character.isdigit():
        following = text[index + 1] if index + 1 < len(text) else ""
        return bool(
            following
            and not following.isascii()
            and following.isalnum()
        )
    if character == "/" or character.isalnum():
        return False
    if character in "._;:+-%":
        following = text[index + 1] if index + 1 < len(text) else ""
        return bool(
            not following
            or following.isspace()
            or not following.isascii()
        )
    return True


def _compact_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _paper_doi(paper: PaperCandidate) -> str | None:
    for value in (paper.doi, paper.doi_original):
        if not value:
            continue
        try:
            normalized = normalize_doi(value)
        except InvalidDoiError:
            continue
        if normalized:
            return normalized
    return None


def _decode_html(fetch: HttpFetch) -> str | None:
    content_type = fetch.content_type
    if content_type and content_type not in {
        "text/html",
        "application/xhtml+xml",
    }:
        return None
    prefix = fetch.body[:1024].lstrip().lower()
    if not (
        prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
        or b"<html" in prefix
        or b"<meta" in prefix
    ):
        return None
    charset_match = re.search(
        r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)",
        fetch.headers.get("Content-Type", ""),
        re.IGNORECASE,
    )
    candidates = [
        charset_match.group(1) if charset_match else None,
        "utf-8",
        "euc-kr",
        "cp949",
    ]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return fetch.body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return None


def _single_query_value(url: str, key: str) -> str | None:
    values = [
        value
        for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
        if name == key
    ]
    return values[0] if len(values) == 1 and values[0] else None


def _bounded_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and 8 <= len(value) <= 4096
        and not any(character in value for character in "\r\n\0")
    )


def _redact_audit_url(url: str) -> str:
    """Remove every platform download credential from an auditable URL."""

    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [
        (
            key,
            (
                "[REDACTED]"
                if any(
                    part in key.casefold()
                    for part in _SENSITIVE_AUDIT_QUERY_PARTS
                )
                else value
            ),
        )
        for key, value in pairs
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(redacted, doseq=True),
            "",
        )
    )


def _is_doi_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == _DOI_HOST
    )
