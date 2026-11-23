"""Conservative resolver for two public Supreme Prosecutors' Office journals.

The SPO board is a public web page, but public reachability is not the same as
permission for automated copying.  Consequently this resolver is disabled by
default.  A caller must make an explicit policy decision by setting
``permission_granted=True`` before any network request is made.

When enabled, the resolver:

* crawls only the fixed HTTPS SPO research-board List/View pages;
* follows only detail and PDF/ZIP links explicitly advertised by those pages;
* never follows KCI links and never derives a download URL from a filename;
* keeps requests sequential, bounded, rate limited, redirect free, and
  cookie-less; and
* returns a PDF only after its contents establish the paper identity.

For DOI-bearing rows, an exact normalized DOI is the primary identity.  A
historic PDF with no DOI at all may fall back to the same strict multi-factor
identity required for DOI-less rows: exact known title and first-author name,
journal/issue/year header, printed boundary pages, and page count.  A
conflicting PDF DOI always fails closed.  Attachment names are retained only
for audit and are never identity evidence.
"""

from __future__ import annotations

import html
import io
import math
import re
import stat
import threading
import unicodedata
import zipfile
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    PerHostRateLimiter,
    RedirectLimitError,
    ResponseTooLargeError,
    RetryableHttpError,
    SafeHttpClient,
    redact_url,
)
from PaperPipeline.records import PaperCandidate, ResolverCandidate


SPO_ORIGIN = "https://www.spo.go.kr"
SPO_HOST = "www.spo.go.kr"
SPO_BOARD_ID = "1303"
SPO_LIST_PATH = "/site/spo/ex/board/List.do"
SPO_VIEW_PATH = "/site/spo/ex/board/View.do"
SPO_DOWNLOAD_PATH = "/common/board/Download.do"
SPO_LIST_URL = f"{SPO_ORIGIN}{SPO_LIST_PATH}?cbIdx={SPO_BOARD_ID}"
SPO_RESOLVER_NAME = "spo-public-board"

SPO_INSTITUTION = "대검찰청"
SPO_FORENSIC_JOURNAL = "법과학의 신동향"
SPO_CRIMINAL_LAW_JOURNAL = "형사법의 신동향"
SPO_JOURNALS = frozenset(
    {SPO_FORENSIC_JOURNAL, SPO_CRIMINAL_LAW_JOURNAL}
)

# Read-only audit of the current 50 selected SPO DB rows (2026-07-27).
# These values are informational and never bypass runtime content validation.
SPO_AUDITED_SELECTED_COUNT = 50
SPO_AUDITED_RESOLVABLE_COUNT = 47
SPO_AUDITED_UNAVAILABLE_SOURCE_IDS = frozenset(
    {
        # Issue 13: the official ZIP omits these and the View exposes only KCI.
        "ART003331345",
        "ART003331352",
        # Issue 86: no publication post exists on the official board.
        "ART003188894",
    }
)

# One KCI row stores the historic journal identifier instead of its title.
# This is an exact, DB-observed alias rather than a fuzzy journal mapping.
_JOURNAL_ALIASES = {
    "SER000001908": SPO_CRIMINAL_LAW_JOURNAL,
}

# The first half of criminal-law issue 76 is mislabeled "75" by SPO.  It is
# not discovered by guessing this id: the correctly labeled issue-76 part 2-2
# page explicitly advertises 1031680 as its previous View page.  The exception
# is deliberately pinned to that one server id and still requires exact PDF
# identity (the selected DB rows have DOI 10.23026/...76...).
_DOCUMENTED_TITLE_CORRECTIONS: Mapping[str, tuple[str, int, int]] = {
    "1031680": (SPO_CRIMINAL_LAW_JOURNAL, 76, 2022),
}

_DETAIL_JAVASCRIPT_RE = re.compile(
    r"""\Ajavascript\s*:\s*doBbsContentView\(\s*
        (?P<quote>['"])(?P<bc_idx>[0-9]+)(?P=quote)\s*
        \)\s*;?\s*\Z""",
    re.IGNORECASE | re.VERBOSE,
)
_NEIGHBOR_JAVASCRIPT_RE = re.compile(
    r"""\Ajavascript\s*:\s*doPrevNext\(\s*
        (?P<q1>['"])1303(?P=q1)\s*,\s*
        (?P<q2>['"])(?P<bc_idx>[0-9]+)(?P=q2)\s*
        \)\s*;?\s*\Z""",
    re.IGNORECASE | re.VERBOSE,
)
_ISSUE_YEAR_RE = re.compile(
    r"통권\s*(?:제\s*)?0*(?P<issue>[1-9][0-9]*)\s*호"
    r"\s*[\(\[]?\s*(?P<year>20[0-9]{2})(?![0-9])",
    re.IGNORECASE,
)
_DECIMAL_RE = re.compile(r"\A[0-9]+\Z")
_DOWNLOAD_TOKEN_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,254}\.(?P<kind>pdf|zip)\Z",
    re.IGNORECASE,
)
_SAFE_DOWNLOAD_TOKEN_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,258}\Z",
)
_DOI_IN_TEXT_RE = re.compile(
    r"10\.[0-9]{4,9}/[A-Z0-9._;()/:+-]+",
    re.IGNORECASE,
)
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_COOKIE_RESPONSE_HEADERS = frozenset({"set-cookie", "set-cookie2"})
_COOKIE_REQUEST_HEADERS = frozenset(
    {"cookie", "authorization", "proxy-authorization"}
)


class SpoHttp(Protocol):
    """The one bounded, redirect-free GET operation used by the resolver."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Fetch one response without retaining or retransmitting cookies."""


@dataclass(frozen=True, slots=True)
class SpoResolverConfig:
    """Policy switch and hard resource bounds for the SPO workflow."""

    permission_granted: bool = False
    requests_per_second: float = 0.5
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    max_list_pages: int = 32
    max_list_response_bytes: int = 4 * 1024 * 1024
    max_view_response_bytes: int = 4 * 1024 * 1024
    max_attachment_bytes: int = 50 * 1024 * 1024
    max_attachments_per_post: int = 64
    max_zip_entries: int = 64
    max_zip_uncompressed_bytes: int = 100 * 1024 * 1024
    max_zip_compression_ratio: float = 100.0
    max_pdf_pages: int = 2_000
    max_pdf_text_chars: int = 2_000_000
    request_gate: Callable[[str], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class SpoResolverAttempt:
    """One redacted network, cache, archive, or identity decision."""

    stage: str
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    error_code: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SpoResolution:
    """One auditable SPO resolution, including the already-validated PDF."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = SPO_RESOLVER_NAME
    candidate: ResolverCandidate | None = None
    fetch: HttpFetch | None = field(default=None, repr=False)
    post_url: str | None = None
    attachment_url: str | None = None
    archive_entry: str | None = None
    matched_title: str | None = None
    matched_doi: str | None = None
    attempts: tuple[SpoResolverAttempt, ...] = ()
    skipped_kci_links: int = 0
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
    def pdf_bytes(self) -> bytes | None:
        return self.fetch.body if self.resolved and self.fetch is not None else None

    @property
    def bytes_received(self) -> int:
        return len(self.fetch.body) if self.fetch is not None else 0


@dataclass(frozen=True, slots=True)
class _IssueKey:
    journal: str
    issue: int
    year: int


@dataclass(frozen=True, slots=True)
class _PageIdentityCorrection:
    """One source-id-pinned correction audited against an official SPO PDF."""

    source_id: str
    issue_key: _IssueKey
    database_first_page: int
    database_final_page: int
    official_first_page: int
    official_final_page: int
    official_page_count: int


# These are not general page-range relaxations.  Each correction is pinned to
# one KCI ART id, its exact journal/issue/year, and the exact DB values audited
# on 2026-07-28.  If any part of that envelope changes, the correction is not
# applied and normal fail-closed identity matching remains in force.
_PAGE_IDENTITY_CORRECTIONS: Mapping[str, _PageIdentityCorrection] = {
    "ART003030161": _PageIdentityCorrection(
        source_id="ART003030161",
        issue_key=_IssueKey(SPO_CRIMINAL_LAW_JOURNAL, 81, 2023),
        database_first_page=65,
        database_final_page=98,
        official_first_page=65,
        official_final_page=96,
        official_page_count=32,
    ),
    "ART003210490": _PageIdentityCorrection(
        source_id="ART003210490",
        issue_key=_IssueKey(SPO_FORENSIC_JOURNAL, 10, 2024),
        database_first_page=74,
        database_final_page=111,
        official_first_page=76,
        official_final_page=111,
        official_page_count=33,
    ),
}


@dataclass(frozen=True, slots=True)
class _ExpectedIdentity:
    issue_key: _IssueKey
    doi: str | None
    titles: tuple[str, ...]
    first_author: str | None
    first_page: int | None
    final_page: int | None
    page_count: int | None
    page_correction: _PageIdentityCorrection | None


@dataclass(frozen=True, slots=True)
class _BoardPost:
    bc_idx: str
    title: str
    url: str
    corrected_title: bool = False


@dataclass(frozen=True, slots=True)
class _Attachment:
    url: str
    label: str
    kind: str


@dataclass(frozen=True, slots=True)
class _ViewData:
    title: str
    attachments: tuple[_Attachment, ...]
    neighbor_ids: tuple[str, ...]
    skipped_kci_links: int


@dataclass(frozen=True, slots=True)
class _PdfDocument:
    pages: tuple[str, ...]
    metadata_text: str

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True, slots=True)
class _IdentityEvidence:
    matched_title: str | None
    matched_doi: str | None


@dataclass(frozen=True, slots=True)
class _PdfMatch:
    post: _BoardPost
    attachment: _Attachment
    archive_entry: str | None
    fetch: HttpFetch
    evidence: _IdentityEvidence


class _AbortResolution(RuntimeError):
    def __init__(
        self,
        *,
        status: str,
        reason: str,
        error_code: str,
        http_status: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error_code = error_code
        self.http_status = http_status


class _PdfInspectionError(ValueError):
    pass


class _ArchiveSafetyError(ValueError):
    pass


class SpoPublicPdfResolver:
    """Resolve one exact SPO paper without guessing or persisting cookies."""

    def __init__(
        self,
        config: SpoResolverConfig | None = None,
        *,
        http: SpoHttp | None = None,
    ) -> None:
        self.config = config or SpoResolverConfig()
        _validate_config(self.config)

        # Injected transports are assumed to be deterministic test/application
        # seams.  The production transport gets a shared per-host gate.
        if self.config.request_gate is not None:
            self._request_gate = self.config.request_gate
        elif http is None:
            self._request_gate = PerHostRateLimiter(
                self.config.requests_per_second
            ).wait
        else:
            self._request_gate = None

        self.http: SpoHttp = http or _StatelessSpoHttp(
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
        )
        # A resolver instance deliberately serializes all resolution work.
        # This makes cache state deterministic and prevents a worker pool from
        # bursting requests around the rate limiter.
        self._lock = threading.RLock()
        self._index_cache: Mapping[_IssueKey, tuple[_BoardPost, ...]] | None = None
        self._view_cache: dict[str, _ViewData] = {}

    def resolve(self, paper: PaperCandidate) -> SpoResolution:
        """Return the uniquely content-verified official PDF, if one exists."""

        with self._lock:
            return self._resolve_locked(paper)

    def _resolve_locked(self, paper: PaperCandidate) -> SpoResolution:
        expected, eligibility = _expected_identity(paper)
        if eligibility is not None:
            return SpoResolution(paper=paper, **eligibility)
        assert expected is not None

        if not self.config.permission_granted:
            return SpoResolution(
                paper=paper,
                status="permission_required",
                reason="spo_automated_copying_not_enabled",
                error_code="permission_required",
            )

        attempts: list[SpoResolverAttempt] = []
        skipped_kci_links = 0
        try:
            index = self._board_index(attempts)
            initial_posts = index.get(expected.issue_key, ())
            if not initial_posts:
                return SpoResolution(
                    paper=paper,
                    status="not_found",
                    reason="exact_spo_issue_post_not_found",
                    attempts=tuple(attempts),
                    error_code="not_found",
                )

            posts: list[tuple[_BoardPost, _ViewData]] = []
            seen_post_ids: set[str] = set()
            for post in initial_posts:
                view = self._view(post, expected.issue_key, attempts)
                posts.append((post, view))
                seen_post_ids.add(post.bc_idx)
                skipped_kci_links += view.skipped_kci_links

            # Follow only the one documented correction when it is explicitly
            # advertised by a correctly identified issue-76 View page.
            for _post, view in tuple(posts):
                for neighbor_id in view.neighbor_ids:
                    correction = _DOCUMENTED_TITLE_CORRECTIONS.get(neighbor_id)
                    if (
                        correction
                        != (
                            expected.issue_key.journal,
                            expected.issue_key.issue,
                            expected.issue_key.year,
                        )
                        or neighbor_id in seen_post_ids
                    ):
                        continue
                    corrected_post = _BoardPost(
                        bc_idx=neighbor_id,
                        title="documented SPO title correction",
                        url=_view_url(neighbor_id),
                        corrected_title=True,
                    )
                    corrected_view = self._view(
                        corrected_post,
                        expected.issue_key,
                        attempts,
                    )
                    posts.append((corrected_post, corrected_view))
                    seen_post_ids.add(neighbor_id)
                    skipped_kci_links += corrected_view.skipped_kci_links

            matches: dict[tuple[str, str | None], _PdfMatch] = {}
            attachment_count = 0
            for post, view in posts:
                attachment_count += len(view.attachments)
                for attachment in view.attachments:
                    fetch = self._request(
                        stage="spo-attachment",
                        url=attachment.url,
                        max_bytes=self.config.max_attachment_bytes,
                        accept=(
                            "application/pdf,application/zip,"
                            "application/octet-stream"
                        ),
                        attempts=attempts,
                    )
                    if attachment.kind == "pdf":
                        entry_values = ((None, fetch.body, fetch),)
                    else:
                        archive_entries = _safe_zip_pdf_entries(
                            fetch.body,
                            self.config,
                        )
                        entry_values = tuple(
                            (
                                entry_name,
                                body,
                                _archive_pdf_fetch(
                                    archive_fetch=fetch,
                                    entry_name=entry_name,
                                    body=body,
                                ),
                            )
                            for entry_name, body in archive_entries
                        )

                    for entry_name, pdf_body, pdf_fetch in entry_values:
                        detail = entry_name or attachment.label or None
                        try:
                            document = _read_pdf_document(
                                pdf_body,
                                max_pages=self.config.max_pdf_pages,
                                max_text_chars=self.config.max_pdf_text_chars,
                            )
                        except _PdfInspectionError as exc:
                            attempts.append(
                                SpoResolverAttempt(
                                    stage="spo-pdf-inspection",
                                    url=redact_url(attachment.url),
                                    status="failed",
                                    http_status=fetch.status_code,
                                    error_code="invalid_pdf",
                                    detail=_bounded_detail(detail, exc),
                                )
                            )
                            raise _AbortResolution(
                                status="invalid_response",
                                reason="spo_attachment_pdf_invalid",
                                error_code="invalid_pdf",
                                http_status=fetch.status_code,
                            ) from exc

                        evidence = _match_pdf_identity(
                            document,
                            expected,
                            corrected_post=post.corrected_title,
                        )
                        attempts.append(
                            SpoResolverAttempt(
                                stage="spo-pdf-identity",
                                url=redact_url(attachment.url),
                                status="matched" if evidence is not None else "mismatch",
                                http_status=fetch.status_code,
                                detail=_bounded_detail(detail),
                            )
                        )
                        if evidence is None:
                            continue
                        if (
                            evidence.matched_title is not None
                            and expected.page_correction is not None
                        ):
                            attempts.append(
                                SpoResolverAttempt(
                                    stage="spo-page-identity-correction",
                                    url=redact_url(attachment.url),
                                    status="applied",
                                    http_status=fetch.status_code,
                                    detail=_page_correction_audit_detail(
                                        expected.page_correction
                                    ),
                                )
                            )
                        key = (attachment.url, entry_name)
                        matches[key] = _PdfMatch(
                            post=post,
                            attachment=attachment,
                            archive_entry=entry_name,
                            fetch=pdf_fetch,
                            evidence=evidence,
                        )

            if not matches:
                reason = (
                    "no_official_spo_attachment"
                    if attachment_count == 0
                    else "exact_pdf_identity_not_found"
                )
                return SpoResolution(
                    paper=paper,
                    status="not_found",
                    reason=reason,
                    attempts=tuple(attempts),
                    skipped_kci_links=skipped_kci_links,
                    error_code="not_found",
                )
            if len(matches) != 1:
                return SpoResolution(
                    paper=paper,
                    status="ambiguous",
                    reason="multiple_exact_spo_pdf_matches",
                    attempts=tuple(attempts),
                    skipped_kci_links=skipped_kci_links,
                    error_code="ambiguous",
                )

            match = next(iter(matches.values()))
            candidate = ResolverCandidate(
                url=match.attachment.url,
                resolver=SPO_RESOLVER_NAME,
                landing_url=match.post.url,
                is_open_access=None,
                verified_doi=match.evidence.matched_doi,
                # SPO does not expose the KCI ART id.  The source_id binding is
                # recorded by SpoResolution only after content validation.
                verified_source_id=None,
            )
            return SpoResolution(
                paper=paper,
                status="resolved",
                reason="official_spo_pdf_exact_content_identity",
                candidate=candidate,
                fetch=match.fetch,
                post_url=match.post.url,
                attachment_url=match.attachment.url,
                archive_entry=match.archive_entry,
                matched_title=match.evidence.matched_title,
                matched_doi=match.evidence.matched_doi,
                attempts=tuple(attempts),
                skipped_kci_links=skipped_kci_links,
                http_status=match.fetch.status_code,
            )
        except _ArchiveSafetyError as exc:
            attempts.append(
                SpoResolverAttempt(
                    stage="spo-archive",
                    url="",
                    status="failed",
                    error_code="unsafe_archive",
                    detail=_bounded_detail(None, exc),
                )
            )
            return SpoResolution(
                paper=paper,
                status="invalid_response",
                reason="spo_zip_safety_check_failed",
                attempts=tuple(attempts),
                skipped_kci_links=skipped_kci_links,
                error_code="unsafe_archive",
            )
        except _AbortResolution as exc:
            return SpoResolution(
                paper=paper,
                status=exc.status,
                reason=exc.reason,
                attempts=tuple(attempts),
                skipped_kci_links=skipped_kci_links,
                http_status=exc.http_status,
                error_code=exc.error_code,
            )

    def _board_index(
        self,
        attempts: list[SpoResolverAttempt],
    ) -> Mapping[_IssueKey, tuple[_BoardPost, ...]]:
        if self._index_cache is not None:
            attempts.append(
                SpoResolverAttempt(
                    stage="spo-list-cache",
                    url=SPO_LIST_URL,
                    status="cache_hit",
                )
            )
            return self._index_cache

        queue: deque[tuple[str, int]] = deque([(SPO_LIST_URL, 1)])
        queued = {SPO_LIST_URL}
        visited: set[str] = set()
        posts_by_id: dict[str, _BoardPost] = {}

        while queue:
            url, page_index = queue.popleft()
            if url in visited:
                continue
            if len(visited) >= self.config.max_list_pages:
                raise _AbortResolution(
                    status="invalid_response",
                    reason="spo_list_page_limit_exceeded",
                    error_code="too_many_pages",
                )
            fetch = self._request(
                stage="spo-list",
                url=url,
                max_bytes=self.config.max_list_response_bytes,
                accept="text/html,application/xhtml+xml",
                attempts=attempts,
            )
            document = _decode_spo_html(fetch)
            parser = _ListPageParser()
            parser.feed(document)
            parser.close()
            visited.add(url)

            for bc_idx, title in parser.posts:
                identity = _post_identity(title)
                if identity is None:
                    continue
                post = _BoardPost(
                    bc_idx=bc_idx,
                    title=title,
                    url=_view_url(bc_idx),
                )
                previous = posts_by_id.get(bc_idx)
                if previous is not None and previous.title != title:
                    raise _AbortResolution(
                        status="invalid_response",
                        reason="spo_conflicting_list_post_identity",
                        error_code="ambiguous",
                    )
                posts_by_id[bc_idx] = post

            for href in parser.hrefs:
                next_page = _canonical_list_page(
                    href,
                    current_url=url,
                    max_page_index=self.config.max_list_pages,
                )
                if next_page is None:
                    continue
                next_url, next_index = next_page
                if next_index > self.config.max_list_pages:
                    raise _AbortResolution(
                        status="invalid_response",
                        reason="spo_list_page_limit_exceeded",
                        error_code="too_many_pages",
                    )
                if next_url not in queued:
                    queued.add(next_url)
                    queue.append((next_url, next_index))

        index: dict[_IssueKey, list[_BoardPost]] = {}
        for post in posts_by_id.values():
            identity = _post_identity(post.title)
            if identity is not None:
                index.setdefault(identity, []).append(post)
        frozen_index = {
            key: tuple(sorted(values, key=lambda value: int(value.bc_idx)))
            for key, values in index.items()
        }
        self._index_cache = frozen_index
        return frozen_index

    def _view(
        self,
        post: _BoardPost,
        expected_key: _IssueKey,
        attempts: list[SpoResolverAttempt],
    ) -> _ViewData:
        cached = self._view_cache.get(post.bc_idx)
        if cached is not None:
            attempts.append(
                SpoResolverAttempt(
                    stage="spo-view-cache",
                    url=redact_url(post.url),
                    status="cache_hit",
                )
            )
            _validate_view_identity(cached.title, post, expected_key)
            return cached

        fetch = self._request(
            stage="spo-view",
            url=post.url,
            max_bytes=self.config.max_view_response_bytes,
            accept="text/html,application/xhtml+xml",
            attempts=attempts,
        )
        document = _decode_spo_html(fetch)
        parser = _ViewPageParser()
        parser.feed(document)
        parser.close()

        matching_titles = [
            title
            for title in parser.titles
            if _post_identity(title) is not None
        ]
        if not matching_titles:
            raise _AbortResolution(
                status="invalid_response",
                reason="spo_view_missing_issue_title",
                error_code="missing_identity",
                http_status=fetch.status_code,
            )
        # Repeated blind/visible title nodes are expected, but distinct board
        # identities are not.
        unique_titles = tuple(dict.fromkeys(matching_titles))
        if len({_post_identity(value) for value in unique_titles}) != 1:
            raise _AbortResolution(
                status="invalid_response",
                reason="spo_view_ambiguous_issue_title",
                error_code="ambiguous",
                http_status=fetch.status_code,
            )
        title = unique_titles[0]

        attachments: dict[str, _Attachment] = {}
        neighbor_ids: list[str] = []
        skipped_kci_links = 0
        for anchor in parser.anchors:
            href = anchor.href.strip()
            if not href:
                continue
            neighbor_id = _explicit_neighbor_id(href)
            if neighbor_id is not None:
                neighbor_ids.append(neighbor_id)
                continue
            if _is_kci_link(href, post.url):
                skipped_kci_links += 1
                continue
            absolute = urljoin(post.url, html.unescape(href))
            if urlsplit(absolute).path != SPO_DOWNLOAD_PATH:
                continue
            attachment = _canonical_attachment(
                href,
                post_url=post.url,
                expected_bc_idx=post.bc_idx,
                label=anchor.label,
            )
            if attachment is None:
                # Safe official non-PDF/ZIP files (for example the HWP forms
                # on a call-for-papers post) are irrelevant, not fatal.  The
                # resolver must continue to the other exact issue posts.
                continue
            previous = attachments.get(attachment.url)
            if previous is not None and previous.kind != attachment.kind:
                raise _AbortResolution(
                    status="invalid_response",
                    reason="spo_conflicting_attachment_identity",
                    error_code="ambiguous",
                )
            attachments[attachment.url] = attachment

        if len(attachments) > self.config.max_attachments_per_post:
            raise _AbortResolution(
                status="invalid_response",
                reason="spo_attachment_count_limit_exceeded",
                error_code="too_many_attachments",
            )
        view = _ViewData(
            title=title,
            attachments=tuple(attachments.values()),
            neighbor_ids=tuple(dict.fromkeys(neighbor_ids)),
            skipped_kci_links=skipped_kci_links,
        )
        _validate_view_identity(view.title, post, expected_key)
        self._view_cache[post.bc_idx] = view
        return view

    def _request(
        self,
        *,
        stage: str,
        url: str,
        max_bytes: int,
        accept: str,
        attempts: list[SpoResolverAttempt],
    ) -> HttpFetch:
        headers = {"Accept": accept}
        if any(key.casefold() in _COOKIE_REQUEST_HEADERS for key in headers):
            raise AssertionError("credential headers are forbidden")
        try:
            if self._request_gate is not None:
                self._request_gate(url)
            fetch = self.http.get_bytes(
                url,
                max_bytes=max_bytes,
                headers=headers,
            )
        except ResponseTooLargeError as exc:
            attempts.append(
                SpoResolverAttempt(
                    stage=stage,
                    url=redact_url(url),
                    status="failed",
                    error_code="too_large",
                    detail=_bounded_detail(None, exc),
                )
            )
            raise _AbortResolution(
                status="invalid_response",
                reason="spo_response_too_large",
                error_code="too_large",
            ) from exc
        except RedirectLimitError as exc:
            attempts.append(
                SpoResolverAttempt(
                    stage=stage,
                    url=redact_url(url),
                    status="failed",
                    error_code="redirect_not_allowed",
                )
            )
            raise _AbortResolution(
                status="invalid_response",
                reason="spo_redirect_rejected",
                error_code="redirect_not_allowed",
            ) from exc
        except RetryableHttpError as exc:
            attempts.append(
                SpoResolverAttempt(
                    stage=stage,
                    url=redact_url(url),
                    status="failed",
                    error_code="retryable_error",
                    detail=_bounded_detail(None, exc),
                )
            )
            raise _AbortResolution(
                status="retryable_error",
                reason="spo_request_retryable",
                error_code="retryable_error",
            ) from exc
        except HttpClientError as exc:
            attempts.append(
                SpoResolverAttempt(
                    stage=stage,
                    url=redact_url(url),
                    status="failed",
                    error_code=getattr(exc, "code", "http_error"),
                    detail=_bounded_detail(None, exc),
                )
            )
            raise _AbortResolution(
                status="request_error",
                reason="spo_request_failed",
                error_code=getattr(exc, "code", "http_error"),
            ) from exc

        fetch = _without_cookie_headers(fetch)
        attempts.append(
            SpoResolverAttempt(
                stage=stage,
                url=redact_url(url),
                status="ok" if 200 <= fetch.status_code < 300 else "http_error",
                http_status=fetch.status_code,
                final_url=redact_url(fetch.final_url),
            )
        )
        if (
            fetch.requested_url != url
            or fetch.final_url != url
            or fetch.redirects
            or not _is_exact_spo_url(url)
        ):
            raise _AbortResolution(
                status="invalid_response",
                reason="spo_redirect_or_url_change_rejected",
                error_code="redirect_not_allowed",
                http_status=fetch.status_code,
            )
        if len(fetch.body) > max_bytes:
            raise _AbortResolution(
                status="invalid_response",
                reason="spo_response_too_large",
                error_code="too_large",
                http_status=fetch.status_code,
            )
        if fetch.status_code in _RETRYABLE_STATUSES:
            raise _AbortResolution(
                status="retryable_error",
                reason=(
                    "spo_rate_limited"
                    if fetch.status_code == 429
                    else "spo_retryable_http_error"
                ),
                error_code="retryable_error",
                http_status=fetch.status_code,
            )
        if fetch.status_code in {401, 403}:
            raise _AbortResolution(
                status="access_denied",
                reason="spo_access_denied",
                error_code="access_denied",
                http_status=fetch.status_code,
            )
        if 300 <= fetch.status_code < 400:
            raise _AbortResolution(
                status="invalid_response",
                reason="spo_redirect_rejected",
                error_code="redirect_not_allowed",
                http_status=fetch.status_code,
            )
        if fetch.status_code == 404:
            raise _AbortResolution(
                status="not_found",
                reason="spo_official_resource_not_found",
                error_code="not_found",
                http_status=fetch.status_code,
            )
        if not 200 <= fetch.status_code < 300:
            raise _AbortResolution(
                status="http_error",
                reason="spo_http_error",
                error_code="http_error",
                http_status=fetch.status_code,
            )
        return fetch


class _StatelessSpoHttp:
    """Create and close a fresh cookie jar for every individual request."""

    def __init__(self, *, connect_timeout: float, read_timeout: float) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def _client(self) -> SafeHttpClient:
        return SafeHttpClient(
            user_agent="Forensics-Paper/1.0 (SPO public-board resolver)",
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            max_redirects=0,
            retries=0,
            allow_http=False,
        )

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        client = self._client()
        try:
            safe_headers = {
                key: value
                for key, value in (headers or {}).items()
                if key.casefold() not in _COOKIE_REQUEST_HEADERS
            }
            return client.get_bytes(
                url,
                max_bytes=max_bytes,
                headers=safe_headers,
            )
        finally:
            # requests may have accepted Set-Cookie into this jar, so destroy
            # the entire Session before another request is made.
            client.session.cookies.clear()
            client.session.close()


@dataclass(frozen=True, slots=True)
class _Anchor:
    href: str
    label: str


class _ListPageParser(HTMLParser):
    """Extract explicit board ids, their title nodes, and pagination hrefs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[tuple[str, str]] = []
        self.hrefs: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_bc_idx: str | None = None
        self._title_depth = 0
        self._title_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        attributes = _attributes(attrs)
        if lowered == "a":
            self._finish_anchor()
            href = attributes.get("href", "").strip()
            self._anchor_href = href
            self._anchor_bc_idx = _explicit_list_post_id(href)
            if href:
                self.hrefs.append(href)
        elif (
            lowered == "b"
            and self._anchor_bc_idx is not None
            and "title"
            in frozenset(attributes.get("class", "").casefold().split())
        ):
            self._title_depth = 1
            self._title_parts = []
        elif self._title_depth:
            self._title_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag, attrs

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if self._title_depth:
            self._title_depth -= 1
        if lowered == "a":
            self._finish_anchor()

    def close(self) -> None:
        super().close()
        self._finish_anchor()

    def _finish_anchor(self) -> None:
        if self._anchor_bc_idx is not None:
            title = _visible_text(self._title_parts)
            if title:
                self.posts.append((self._anchor_bc_idx, title))
        self._anchor_href = None
        self._anchor_bc_idx = None
        self._title_depth = 0
        self._title_parts = []


class _ViewPageParser(HTMLParser):
    """Extract visible board titles and explicit anchors from one View page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.titles: list[str] = []
        self.anchors: list[_Anchor] = []
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        attributes = _attributes(attrs)
        classes = frozenset(attributes.get("class", "").casefold().split())
        if not self._title_depth and (
            "title" in classes or (lowered == "h4" and "blind" in classes)
        ):
            self._title_depth = 1
            self._title_parts = []
        elif self._title_depth:
            self._title_depth += 1

        if lowered == "a":
            self._finish_anchor()
            self._anchor_href = attributes.get("href", "").strip() or None
            self._anchor_parts = []
        elif lowered == "img" and self._anchor_parts is not None:
            for key in ("title", "alt"):
                if attributes.get(key):
                    self._anchor_parts.append(attributes[key])

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = _attributes(attrs)
        if tag.casefold() == "img" and self._anchor_parts is not None:
            for key in ("title", "alt"):
                if attributes.get(key):
                    self._anchor_parts.append(attributes[key])

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._anchor_parts is not None:
            self._anchor_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if self._title_depth:
            self._title_depth -= 1
            if not self._title_depth:
                title = _visible_text(self._title_parts)
                if title:
                    self.titles.append(title)
                self._title_parts = []
        if lowered == "a":
            self._finish_anchor()

    def close(self) -> None:
        super().close()
        self._finish_anchor()

    def _finish_anchor(self) -> None:
        if self._anchor_parts is not None:
            self.anchors.append(
                _Anchor(
                    href=self._anchor_href or "",
                    label=_visible_text(self._anchor_parts),
                )
            )
        self._anchor_href = None
        self._anchor_parts = None


def _validate_config(config: SpoResolverConfig) -> None:
    integer_fields = (
        "max_list_pages",
        "max_list_response_bytes",
        "max_view_response_bytes",
        "max_attachment_bytes",
        "max_attachments_per_post",
        "max_zip_entries",
        "max_zip_uncompressed_bytes",
        "max_pdf_pages",
        "max_pdf_text_chars",
    )
    for name in integer_fields:
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    for name in ("connect_timeout", "read_timeout"):
        value = getattr(config, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be positive and finite")
    if (
        isinstance(config.requests_per_second, bool)
        or not isinstance(config.requests_per_second, (int, float))
        or not math.isfinite(config.requests_per_second)
        or not 0 < config.requests_per_second <= 1
    ):
        raise ValueError("requests_per_second must be positive and at most 1")
    if (
        isinstance(config.max_zip_compression_ratio, bool)
        or not isinstance(config.max_zip_compression_ratio, (int, float))
        or not math.isfinite(config.max_zip_compression_ratio)
        or config.max_zip_compression_ratio <= 0
    ):
        raise ValueError("max_zip_compression_ratio must be positive and finite")
    if not isinstance(config.permission_granted, bool):
        raise ValueError("permission_granted must be a boolean")


def _expected_identity(
    paper: PaperCandidate,
) -> tuple[_ExpectedIdentity | None, dict[str, Any] | None]:
    if paper.source.casefold() != "kci":
        return None, {
            "status": "unsupported",
            "reason": "source_not_kci",
            "error_code": "unsupported_source",
        }
    if paper.institution_id != SPO_INSTITUTION:
        return None, {
            "status": "unsupported",
            "reason": "institution_not_spo",
            "error_code": "unsupported_institution",
        }
    journal = _canonical_journal(paper.journal_id)
    if journal is None:
        return None, {
            "status": "unsupported",
            "reason": "journal_not_supported_by_spo_resolver",
            "error_code": "unsupported_journal",
        }
    issue = _positive_decimal(paper.issue_id)
    if issue is None:
        return None, {
            "status": "missing_metadata",
            "reason": "missing_or_invalid_issue",
            "error_code": "missing_metadata",
        }
    year = paper.publication_year
    if (
        isinstance(year, bool)
        or not isinstance(year, int)
        or not 2000 <= year <= 2100
    ):
        return None, {
            "status": "missing_metadata",
            "reason": "missing_or_invalid_publication_year",
            "error_code": "missing_metadata",
        }

    normalized_dois: list[str] = []
    raw_doi_seen = False
    for value in (paper.doi, paper.doi_original):
        if value is None or not isinstance(value, str) or not value.strip():
            continue
        raw_doi_seen = True
        try:
            normalized = normalize_doi(value)
        except InvalidDoiError:
            return None, {
                "status": "missing_metadata",
                "reason": "invalid_doi",
                "error_code": "invalid_doi",
            }
        if normalized is not None and normalized.casefold() not in {
            item.casefold() for item in normalized_dois
        }:
            normalized_dois.append(normalized)
    if raw_doi_seen and not normalized_dois:
        return None, {
            "status": "missing_metadata",
            "reason": "invalid_doi",
            "error_code": "invalid_doi",
        }
    if len(normalized_dois) > 1:
        return None, {
            "status": "identity_mismatch",
            "reason": "conflicting_paper_dois",
            "error_code": "identity_mismatch",
        }
    doi = normalized_dois[0] if normalized_dois else None

    issue_key = _IssueKey(journal, issue, year)
    titles = tuple(
        dict.fromkeys(
            value.strip()
            for value in (paper.title, *paper.alternate_titles)
            if isinstance(value, str)
            and value.strip()
            and len(_identity_token(value)) >= 6
        )
    )
    first_author = _first_author_name(paper.authors)
    database_first_page = _positive_decimal(
        paper.first_page,
        allow_zero=True,
    )
    database_final_page = _positive_decimal(
        paper.final_page,
        allow_zero=True,
    )
    page_range_is_valid = bool(
        database_first_page is not None
        and database_final_page is not None
        and database_final_page >= database_first_page
    )
    page_correction = (
        _audited_page_identity_correction(
            paper,
            issue_key=issue_key,
            database_first_page=database_first_page,
            database_final_page=database_final_page,
        )
        if page_range_is_valid
        else None
    )
    if page_correction is not None:
        first_page = page_correction.official_first_page
        final_page = page_correction.official_final_page
        page_count = page_correction.official_page_count
    elif page_range_is_valid:
        assert database_first_page is not None
        assert database_final_page is not None
        first_page = database_first_page
        final_page = database_final_page
        page_count = final_page - first_page + 1
    else:
        first_page = None
        final_page = None
        page_count = None

    if doi is not None:
        return (
            _ExpectedIdentity(
                issue_key=issue_key,
                doi=doi,
                titles=titles,
                first_author=first_author,
                first_page=first_page,
                final_page=final_page,
                page_count=page_count,
                page_correction=page_correction,
            ),
            None,
        )

    if not titles:
        return None, {
            "status": "missing_metadata",
            "reason": "missing_title_for_doi_less_paper",
            "error_code": "missing_metadata",
        }
    if first_author is None:
        return None, {
            "status": "missing_metadata",
            "reason": "missing_first_author_for_doi_less_paper",
            "error_code": "missing_metadata",
        }
    if not page_range_is_valid:
        return None, {
            "status": "missing_metadata",
            "reason": "missing_or_invalid_page_range_for_doi_less_paper",
            "error_code": "missing_metadata",
        }
    assert first_page is not None
    assert final_page is not None
    assert page_count is not None
    return (
        _ExpectedIdentity(
            issue_key=issue_key,
            doi=None,
            titles=titles,
            first_author=first_author,
            first_page=first_page,
            final_page=final_page,
            page_count=page_count,
            page_correction=page_correction,
        ),
        None,
    )


def _canonical_journal(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped in SPO_JOURNALS:
        return stripped
    return _JOURNAL_ALIASES.get(stripped)


def _positive_decimal(value: Any, *, allow_zero: bool = False) -> int | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not _DECIMAL_RE.fullmatch(stripped):
        return None
    number = int(stripped)
    if number < 0 or (number == 0 and not allow_zero):
        return None
    return number


def _first_author_name(authors: tuple[str, ...]) -> str | None:
    if not authors or not isinstance(authors[0], str):
        return None
    # KCI author strings append affiliation in parentheses.  The prefix is
    # the exact author name supplied by KCI, not a fuzzy name extraction.
    value = re.split(r"[\(（]", authors[0], maxsplit=1)[0].strip()
    return value if len(_identity_token(value)) >= 2 else None


def _audited_page_identity_correction(
    paper: PaperCandidate,
    *,
    issue_key: _IssueKey,
    database_first_page: int | None,
    database_final_page: int | None,
) -> _PageIdentityCorrection | None:
    correction = _PAGE_IDENTITY_CORRECTIONS.get(paper.source_id)
    if correction is None:
        return None
    if (
        correction.source_id != paper.source_id
        or correction.issue_key != issue_key
        or correction.database_first_page != database_first_page
        or correction.database_final_page != database_final_page
    ):
        return None
    return correction


def _page_correction_audit_detail(
    correction: _PageIdentityCorrection,
) -> str:
    return (
        f"source_id={correction.source_id};"
        f"db_pages={correction.database_first_page}-"
        f"{correction.database_final_page};"
        f"official_pages={correction.official_first_page}-"
        f"{correction.official_final_page};"
        f"official_pdf_pages={correction.official_page_count}"
    )


def _post_identity(title: str) -> _IssueKey | None:
    normalized = unicodedata.normalize("NFKC", html.unescape(title))
    title_token = _identity_token(normalized)
    journals = [
        journal
        for journal in SPO_JOURNALS
        if _identity_token(journal) in title_token
    ]
    matches = {
        (int(match.group("issue")), int(match.group("year")))
        for match in _ISSUE_YEAR_RE.finditer(normalized)
    }
    if len(journals) != 1 or len(matches) != 1:
        return None
    issue, year = next(iter(matches))
    return _IssueKey(journals[0], issue, year)


def _validate_view_identity(
    title: str,
    post: _BoardPost,
    expected: _IssueKey,
) -> None:
    observed = _post_identity(title)
    if observed == expected:
        return
    correction = _DOCUMENTED_TITLE_CORRECTIONS.get(post.bc_idx)
    if (
        post.corrected_title
        and correction == (expected.journal, expected.issue, expected.year)
        and observed == _IssueKey(expected.journal, expected.issue - 1, expected.year)
    ):
        return
    raise _AbortResolution(
        status="identity_mismatch",
        reason="spo_view_issue_identity_mismatch",
        error_code="identity_mismatch",
    )


def _explicit_list_post_id(href: str) -> str | None:
    match = _DETAIL_JAVASCRIPT_RE.fullmatch(html.unescape(href).strip())
    if match is not None:
        return match.group("bc_idx")
    try:
        parsed = urlsplit(urljoin(SPO_ORIGIN + "/", html.unescape(href)))
    except ValueError:
        return None
    if not _is_spo_origin(parsed) or parsed.path != SPO_VIEW_PATH or parsed.fragment:
        return None
    values = _strict_query(parsed.query, {"bcIdx", "cbIdx"})
    if (
        values is None
        or values.get("cbIdx") != SPO_BOARD_ID
        or not _is_decimal_id(values.get("bcIdx"))
    ):
        return None
    return values["bcIdx"]


def _explicit_neighbor_id(href: str) -> str | None:
    match = _NEIGHBOR_JAVASCRIPT_RE.fullmatch(html.unescape(href).strip())
    if match is not None:
        return match.group("bc_idx")
    return None


def _canonical_list_page(
    href: str,
    *,
    current_url: str,
    max_page_index: int,
) -> tuple[str, int] | None:
    unescaped = html.unescape(href).strip()
    if not unescaped or unescaped.startswith("#"):
        return None
    try:
        parsed = urlsplit(urljoin(current_url, unescaped))
    except ValueError:
        return None
    if not _is_spo_origin(parsed) or parsed.path != SPO_LIST_PATH or parsed.fragment:
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "pageIndex" for key, _value in pairs):
        return None
    values = _strict_query(parsed.query, {"pageIndex", "cbIdx"})
    if values is None:
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_invalid_pagination_link",
            error_code="unsafe_url",
        )
    if values.get("cbIdx") not in {None, SPO_BOARD_ID}:
        return None
    raw_index = values.get("pageIndex")
    if not _is_decimal_id(raw_index):
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_invalid_pagination_link",
            error_code="unsafe_url",
        )
    page_index = int(raw_index)
    if page_index < 1 or page_index > max_page_index:
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_list_page_limit_exceeded",
            error_code="too_many_pages",
        )
    if page_index == 1:
        return SPO_LIST_URL, 1
    query = urlencode({"cbIdx": SPO_BOARD_ID, "pageIndex": str(page_index)})
    return f"{SPO_ORIGIN}{SPO_LIST_PATH}?{query}", page_index


def _view_url(bc_idx: str) -> str:
    if not _is_decimal_id(bc_idx):
        raise ValueError("bc_idx must be a positive decimal id")
    query = urlencode({"bcIdx": bc_idx, "cbIdx": SPO_BOARD_ID})
    return f"{SPO_ORIGIN}{SPO_VIEW_PATH}?{query}"


def _canonical_attachment(
    href: str,
    *,
    post_url: str,
    expected_bc_idx: str,
    label: str,
) -> _Attachment | None:
    try:
        parsed = urlsplit(urljoin(post_url, html.unescape(href)))
    except ValueError as exc:
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_unsafe_attachment_url",
            error_code="unsafe_url",
        ) from exc
    if (
        not _is_spo_origin(parsed)
        or parsed.path != SPO_DOWNLOAD_PATH
        or parsed.fragment
    ):
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_unsafe_attachment_url",
            error_code="unsafe_url",
        )
    values = _strict_query(parsed.query, {"bcIdx", "cbIdx", "streFileNm"})
    if (
        values is None
        or values.get("bcIdx") != expected_bc_idx
        or values.get("cbIdx") != SPO_BOARD_ID
    ):
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_attachment_query_mismatch",
            error_code="unsafe_url",
        )
    token = values.get("streFileNm", "")
    if (
        any(ord(character) < 32 or ord(character) == 127 for character in token)
        or "/" in token
        or "\\" in token
        or _SAFE_DOWNLOAD_TOKEN_RE.fullmatch(token) is None
    ):
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_unsafe_attachment_filename",
            error_code="unsafe_url",
        )
    match = _DOWNLOAD_TOKEN_RE.fullmatch(token)
    if match is None:
        return None
    query = urlencode(
        {
            "bcIdx": expected_bc_idx,
            "cbIdx": SPO_BOARD_ID,
            "streFileNm": token,
        }
    )
    return _Attachment(
        url=f"{SPO_ORIGIN}{SPO_DOWNLOAD_PATH}?{query}",
        label=label,
        kind=match.group("kind").casefold(),
    )


def _is_kci_link(href: str, base_url: str) -> bool:
    try:
        parsed = urlsplit(urljoin(base_url, html.unescape(href)))
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    return hostname == "kci.go.kr" or hostname.endswith(".kci.go.kr")


def _safe_zip_pdf_entries(
    body: bytes,
    config: SpoResolverConfig,
) -> tuple[tuple[str, bytes], ...]:
    if len(body) > config.max_attachment_bytes:
        raise _ArchiveSafetyError("ZIP exceeds the compressed-byte limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise _ArchiveSafetyError("attachment is not a valid ZIP") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > config.max_zip_entries:
            raise _ArchiveSafetyError("ZIP entry-count limit exceeded")
        total_size = 0
        seen_names: set[str] = set()
        pdf_infos: list[zipfile.ZipInfo] = []
        for info in infos:
            name = unicodedata.normalize("NFC", info.filename)
            path = PurePosixPath(name)
            mode = (info.external_attr >> 16) & 0o170000
            if (
                not name
                or name.startswith(("/", "\\"))
                or "\\" in name
                or "\x00" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or mode == stat.S_IFLNK
                or info.flag_bits & 0x1
            ):
                raise _ArchiveSafetyError("ZIP contains an unsafe entry")
            identity = name.casefold()
            if identity in seen_names:
                raise _ArchiveSafetyError("ZIP contains duplicate entry names")
            seen_names.add(identity)
            total_size += info.file_size
            if total_size > config.max_zip_uncompressed_bytes:
                raise _ArchiveSafetyError("ZIP expanded-byte limit exceeded")
            if info.file_size:
                if info.compress_size <= 0:
                    raise _ArchiveSafetyError("ZIP entry has an invalid compressed size")
                ratio = info.file_size / info.compress_size
                if ratio > config.max_zip_compression_ratio:
                    raise _ArchiveSafetyError("ZIP compression-ratio limit exceeded")
            if not info.is_dir() and path.suffix.casefold() == ".pdf":
                if info.file_size > config.max_attachment_bytes:
                    raise _ArchiveSafetyError("archived PDF exceeds the PDF-byte limit")
                pdf_infos.append(info)

        if not pdf_infos:
            raise _ArchiveSafetyError("ZIP has no PDF entries")
        extracted: list[tuple[str, bytes]] = []
        for info in pdf_infos:
            try:
                with archive.open(info, "r") as source:
                    value = source.read(config.max_attachment_bytes + 1)
            except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                raise _ArchiveSafetyError("unable to safely extract ZIP entry") from exc
            if len(value) != info.file_size or len(value) > config.max_attachment_bytes:
                raise _ArchiveSafetyError("archived PDF size does not match metadata")
            if value.find(b"%PDF-", 0, 1024) < 0:
                raise _ArchiveSafetyError("ZIP PDF entry has no PDF magic")
            extracted.append((unicodedata.normalize("NFC", info.filename), value))
        return tuple(extracted)


def _read_pdf_document(
    body: bytes,
    *,
    max_pages: int,
    max_text_chars: int,
) -> _PdfDocument:
    if body.find(b"%PDF-", 0, 1024) < 0:
        raise _PdfInspectionError("response has no PDF header")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise _PdfInspectionError("pypdf is required") from exc
    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
        if reader.is_encrypted:
            raise _PdfInspectionError("encrypted PDFs are not accepted")
        page_count = len(reader.pages)
        if page_count < 1 or page_count > max_pages:
            raise _PdfInspectionError("PDF page-count limit violated")
        pages: list[str] = []
        total_chars = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total_chars += len(text)
            if total_chars > max_text_chars:
                raise _PdfInspectionError("PDF extracted-text limit exceeded")
            pages.append(text)
        metadata_parts: list[str] = []
        if reader.metadata is not None:
            for value in reader.metadata.values():
                if isinstance(value, str):
                    metadata_parts.append(value)
    except _PdfInspectionError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError, KeyError) as exc:
        raise _PdfInspectionError("PDF parser rejected the response") from exc
    except Exception as exc:
        raise _PdfInspectionError("unable to extract PDF identity") from exc
    return _PdfDocument(
        pages=tuple(pages),
        metadata_text="\n".join(metadata_parts),
    )


def _match_pdf_identity(
    document: _PdfDocument,
    expected: _ExpectedIdentity,
    *,
    corrected_post: bool,
) -> _IdentityEvidence | None:
    all_text = "\n".join((*document.pages, document.metadata_text))
    if expected.doi is not None:
        observed_dois = {
            value.casefold()
            for value in _extract_dois(all_text)
        }
        if expected.doi.casefold() not in observed_dois:
            # Historic SPO PDFs frequently predate DOI stamping.  Fall back
            # only when the document contains no DOI at all and every
            # independent bibliographic/content field agrees.  A different
            # DOI is an explicit conflict and always fails closed.
            if observed_dois:
                return None
            return _match_multifactor_pdf_identity(
                document,
                expected,
                all_text=all_text,
            )
        # The sole corrected View-page exception must additionally establish
        # issue 76 from the PDF itself; the DOI contains an exact "..76."
        # segment for all selected rows, but a journal header is stronger.
        if corrected_post and not _document_issue_header_matches(
            all_text,
            expected.issue_key,
        ):
            doi_issue = f"..{expected.issue_key.issue}."
            if doi_issue not in expected.doi:
                return None
        return _IdentityEvidence(
            matched_title=None,
            matched_doi=expected.doi,
        )

    return _match_multifactor_pdf_identity(
        document,
        expected,
        all_text=all_text,
    )


def _match_multifactor_pdf_identity(
    document: _PdfDocument,
    expected: _ExpectedIdentity,
    *,
    all_text: str,
) -> _IdentityEvidence | None:
    if (
        not expected.titles
        or expected.first_author is None
        or expected.first_page is None
        or expected.final_page is None
        or expected.page_count is None
    ):
        return None
    if document.page_count != expected.page_count:
        return None
    first_pages = "\n".join(document.pages[: min(3, document.page_count)])
    known_tokens = {
        _identity_token(title): title
        for title in expected.titles
    }
    observed_first = _identity_token(first_pages)
    matched_title = next(
        (
            title
            for token, title in known_tokens.items()
            if token and token in observed_first
        ),
        None,
    )
    if matched_title is None:
        return None
    if _identity_token(expected.first_author) not in observed_first:
        return None
    if not _page_number_present(document.pages[0], expected.first_page):
        return None
    if not _page_number_present(document.pages[-1], expected.final_page):
        return None
    if not _document_issue_header_matches(all_text, expected.issue_key):
        return None
    return _IdentityEvidence(
        matched_title=matched_title,
        matched_doi=None,
    )


def _document_issue_header_matches(text: str, key: _IssueKey) -> bool:
    compact = _identity_token(text)
    journal = _identity_token(key.journal)
    # Punctuation and whitespace are ignored, but wording/order is exact.
    pattern = re.compile(
        re.escape(journal)
        + r".{0,80}통권(?:제)?0*"
        + re.escape(str(key.issue))
        + r"호.{0,40}"
        + re.escape(str(key.year))
    )
    return pattern.search(compact) is not None


def _page_number_present(text: str, page_number: int) -> bool:
    normalized = unicodedata.normalize("NFKC", text)
    return (
        re.search(
            rf"(?<![0-9])0*{re.escape(str(page_number))}(?![0-9])",
            normalized,
        )
        is not None
    )


def _extract_dois(text: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for match in _DOI_IN_TEXT_RE.finditer(
        unicodedata.normalize("NFKC", text)
    ):
        candidate = match.group(0).rstrip(".,;:)]}")
        try:
            normalized = normalize_doi(candidate)
        except InvalidDoiError:
            continue
        if normalized is None or normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        values.append(normalized)
    return tuple(values)


def _archive_pdf_fetch(
    *,
    archive_fetch: HttpFetch,
    entry_name: str,
    body: bytes,
) -> HttpFetch:
    return HttpFetch(
        requested_url=archive_fetch.requested_url,
        final_url=archive_fetch.final_url,
        status_code=archive_fetch.status_code,
        headers={
            "Content-Type": "application/pdf",
            "X-SPO-Archive-Entry": entry_name,
        },
        body=body,
        redirects=(),
    )


def _decode_spo_html(fetch: HttpFetch) -> str:
    content_type = fetch.content_type
    if content_type and "html" not in content_type:
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_expected_html_response",
            error_code="invalid_content_type",
            http_status=fetch.status_code,
        )
    try:
        document = fetch.body.decode("utf-8-sig")
    except UnicodeError as exc:
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_html_is_not_utf8",
            error_code="invalid_encoding",
            http_status=fetch.status_code,
        ) from exc
    if "\x00" in document:
        raise _AbortResolution(
            status="invalid_response",
            reason="spo_html_contains_nul",
            error_code="invalid_encoding",
            http_status=fetch.status_code,
        )
    return document


def _without_cookie_headers(fetch: HttpFetch) -> HttpFetch:
    headers = {
        key: value
        for key, value in fetch.headers.items()
        if key.casefold() not in _COOKIE_RESPONSE_HEADERS
    }
    return HttpFetch(
        requested_url=fetch.requested_url,
        final_url=fetch.final_url,
        status_code=fetch.status_code,
        headers=headers,
        body=fetch.body,
        redirects=fetch.redirects,
    )


def _strict_query(
    query: str,
    allowed_keys: set[str],
) -> dict[str, str] | None:
    pairs = parse_qsl(query, keep_blank_values=True)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in allowed_keys or key in values:
            return None
        values[key] = value
    return values


def _is_decimal_id(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and _DECIMAL_RE.fullmatch(value) is not None
        and int(value) > 0
    )


def _is_spo_origin(parsed: Any) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == SPO_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _is_exact_spo_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    return (
        _is_spo_origin(parsed)
        and not parsed.fragment
        and parsed.path
        in {SPO_LIST_PATH, SPO_VIEW_PATH, SPO_DOWNLOAD_PATH}
    )


def _attributes(
    attrs: list[tuple[str, str | None]],
) -> dict[str, str]:
    return {
        key.casefold(): html.unescape(value or "")
        for key, value in attrs
    }


def _visible_text(parts: list[str]) -> str:
    normalized = unicodedata.normalize(
        "NFKC",
        html.unescape(" ".join(parts)),
    )
    return " ".join(normalized.split())


def _identity_token(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKC",
        html.unescape(value),
    ).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _bounded_detail(
    value: str | None,
    exc: BaseException | None = None,
) -> str | None:
    parts = [part for part in (value, str(exc) if exc is not None else None) if part]
    if not parts:
        return None
    return " | ".join(parts)[:300]


__all__ = [
    "SPO_AUDITED_RESOLVABLE_COUNT",
    "SPO_AUDITED_SELECTED_COUNT",
    "SPO_AUDITED_UNAVAILABLE_SOURCE_IDS",
    "SPO_LIST_URL",
    "SPO_RESOLVER_NAME",
    "SpoPublicPdfResolver",
    "SpoResolution",
    "SpoResolverAttempt",
    "SpoResolverConfig",
]
