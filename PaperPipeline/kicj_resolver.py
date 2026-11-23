"""Conservative public-PDF resolution for the official KICJ archive.

The Korean Institute of Criminology and Justice (KICJ) publishes
``형사정책연구`` on its own HTTPS archive.  The archive exposes:

* explicit issue-detail links from the official journal listing; and
* one explicit ``boardDownload.es`` link in the same table row as each
  article title and author list.

This resolver never invents ``list_no``, ``seq``, or pagination values.  It
starts at the fixed official archive URL and follows only links found in
bounded HTML responses.  It also fails closed unless the official
``robots.txt`` explicitly permits the archive and download paths.

KICJ's issue page does not bind its download link to a DOI in HTML.  A
successful candidate therefore intentionally leaves ``verified_doi`` unset;
the downloader must still validate the downloaded PDF by DOI or exact title.
"""

from __future__ import annotations

import html
import re
import threading
import unicodedata
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import HttpClientError, HttpFetch, SafeHttpClient
from PaperPipeline.records import PaperCandidate, ResolverCandidate


KICJ_ORIGIN = "https://www.kicj.re.kr"
KICJ_HOST = "www.kicj.re.kr"
KICJ_ARCHIVE_PATH = "/board.es"
KICJ_DOWNLOAD_PATH = "/boardDownload.es"
KICJ_ARCHIVE_URL = (
    f"{KICJ_ORIGIN}{KICJ_ARCHIVE_PATH}"
    "?mid=a10103010000&bid=0003"
)
KICJ_ROBOTS_URL = f"{KICJ_ORIGIN}/robots.txt"
KICJ_RESOLVER_NAME = "kicj-public-archive"
KICJ_USER_AGENT_TOKEN = "Forensics-Paper-KICJ-Resolver"

_JOURNAL_ID = "형사정책연구"
_INSTITUTION_ID = "한국형사법무정책연구원"
_MID = "a10103010000"
_BID = "0003"
_DECIMAL_RE = re.compile(r"\A[0-9]+\Z")
_ISSUE_ID_RE = re.compile(r"\A(?P<volume>[0-9]+)_(?P<issue>[1-4])\Z")
_KICJ_DOI_RE = re.compile(
    r"\A10[.]36889/KCR[.]"
    r"(?P<year>[0-9]{4})[.]"
    r"(?P<month>[0-9]{1,2})[.]"
    r"(?P<day>[0-9]{1,2})[.]"
    r"(?P<issue>[1-4])[.]"
    r"(?P<first_page>[0-9]+)\Z",
    re.IGNORECASE,
)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class KicjHttp(Protocol):
    """Small bounded GET seam used by :class:`KicjPublicPdfResolver`."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Fetch one anonymous response."""


@dataclass(frozen=True, slots=True)
class KicjResolverConfig:
    """Network bounds for official robots, listing, and detail responses."""

    max_robots_bytes: int = 64 * 1024
    max_archive_response_bytes: int = 2 * 1024 * 1024
    max_detail_response_bytes: int = 2 * 1024 * 1024
    # The official archive currently advertises 145 issue records across
    # 15 pages.  Keep the traversal bounded to that published archive size so
    # older issues remain discoverable without inventing page numbers.
    max_archive_pages: int = 15


@dataclass(frozen=True, slots=True)
class KicjResolverAttempt:
    """One auditable anonymous KICJ request."""

    stage: str
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class KicjResolution:
    """A conservative KICJ public-archive decision."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = KICJ_RESOLVER_NAME
    candidate: ResolverCandidate | None = None
    detail_url: str | None = None
    matched_title: str | None = None
    matched_authors: tuple[str, ...] = ()
    doi: str | None = None
    expected_year: int | None = None
    expected_volume: int | None = None
    expected_issue: int | None = None
    expected_serial_issue: int | None = None
    expected_first_page: int | None = None
    expected_final_page: int | None = None
    archive_pages_checked: int = 0
    exact_title_matches: int = 0
    author_matches: int = 0
    attempts: tuple[KicjResolverAttempt, ...] = ()
    http_status: int | None = None
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.candidate is not None


@dataclass(frozen=True, slots=True)
class _ExpectedIdentity:
    titles: tuple[tuple[str, str], ...]
    authors: tuple[str, ...]
    doi: str | None
    year: int
    volume: int
    issue: int
    serial_issue: int
    first_page: int
    final_page: int


@dataclass(frozen=True, slots=True)
class _Failure:
    status: str
    reason: str
    http_status: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _Anchor:
    href: str | None
    label: str
    classes: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _Cell:
    text: str
    anchors: tuple[_Anchor, ...]


@dataclass(frozen=True, slots=True)
class _RobotsRule:
    pattern: str
    allow: bool


@dataclass(frozen=True, slots=True)
class _RobotsGroup:
    agents: tuple[str, ...]
    rules: tuple[_RobotsRule, ...]


@dataclass(frozen=True, slots=True)
class _RobotsPolicy:
    rules: tuple[_RobotsRule, ...]

    def allows(self, url: str) -> bool:
        parsed = urlsplit(url)
        target = parsed.path
        if parsed.query:
            target += f"?{parsed.query}"
        matches = [
            rule
            for rule in self.rules
            if _robots_rule_matches(rule.pattern, target)
        ]
        if not matches:
            return True
        longest = max(_robots_rule_length(rule.pattern) for rule in matches)
        strongest = [
            rule
            for rule in matches
            if _robots_rule_length(rule.pattern) == longest
        ]
        # RFC 9309 gives Allow precedence when equally specific rules conflict.
        return any(rule.allow for rule in strongest)


class KicjPublicPdfResolver:
    """Resolve one paper through exact official KICJ issue metadata."""

    def __init__(
        self,
        config: KicjResolverConfig | None = None,
        *,
        http: KicjHttp | None = None,
    ) -> None:
        self.config = config or KicjResolverConfig()
        if self.config.max_robots_bytes < 1:
            raise ValueError("max_robots_bytes must be positive")
        if self.config.max_archive_response_bytes < 1:
            raise ValueError("max_archive_response_bytes must be positive")
        if self.config.max_detail_response_bytes < 1:
            raise ValueError("max_detail_response_bytes must be positive")
        if self.config.max_archive_pages < 1:
            raise ValueError("max_archive_pages must be positive")
        self.http: KicjHttp = http or _StatelessKicjHttp()
        self._robots_lock = threading.Lock()
        self._robots_policy: _RobotsPolicy | None = None

    def resolve(self, paper: PaperCandidate) -> KicjResolution:
        """Return one explicit official PDF candidate, or an auditable failure."""

        identity, eligibility = _expected_identity(paper)
        if eligibility is not None:
            return KicjResolution(
                status=eligibility.status,
                reason=eligibility.reason,
                paper=paper,
                http_status=eligibility.http_status,
                error_code=eligibility.error_code,
            )
        assert identity is not None

        identity_fields = {
            "doi": identity.doi,
            "expected_year": identity.year,
            "expected_volume": identity.volume,
            "expected_issue": identity.issue,
            "expected_serial_issue": identity.serial_issue,
            "expected_first_page": identity.first_page,
            "expected_final_page": identity.final_page,
        }
        attempts: list[KicjResolverAttempt] = []

        policy, failure = self._official_robots_policy(attempts)
        if failure is not None:
            return self._resolution(
                paper,
                failure,
                attempts,
                identity_fields=identity_fields,
            )
        assert policy is not None

        detail_url, pages_checked, failure = self._find_issue_detail(
            identity,
            policy=policy,
            attempts=attempts,
        )
        if failure is not None:
            return self._resolution(
                paper,
                failure,
                attempts,
                archive_pages_checked=pages_checked,
                identity_fields=identity_fields,
            )
        assert detail_url is not None

        if not policy.allows(detail_url):
            return self._resolution(
                paper,
                _Failure(
                    status="robots_disallowed",
                    reason="kicj_detail_disallowed_by_robots",
                    error_code="robots_disallowed",
                ),
                attempts,
                detail_url=detail_url,
                archive_pages_checked=pages_checked,
                identity_fields=identity_fields,
            )

        detail_fetch, failure = self._fetch(
            stage="kicj-detail",
            url=detail_url,
            max_bytes=self.config.max_detail_response_bytes,
            accept="text/html,application/xhtml+xml;q=0.9",
            require_html=True,
            attempts=attempts,
        )
        if failure is not None:
            return self._resolution(
                paper,
                failure,
                attempts,
                detail_url=detail_url,
                archive_pages_checked=pages_checked,
                identity_fields=identity_fields,
            )
        assert detail_fetch is not None

        document = _decode_utf8(detail_fetch.body)
        if document is None:
            return self._resolution(
                paper,
                _Failure(
                    status="invalid_response",
                    reason="invalid_kicj_detail_html",
                    http_status=detail_fetch.status_code,
                    error_code="invalid_html",
                ),
                attempts,
                detail_url=detail_url,
                archive_pages_checked=pages_checked,
                identity_fields=identity_fields,
            )
        parser = _DetailPageParser()
        try:
            parser.feed(document)
            parser.close()
        except (AssertionError, ValueError):
            return self._resolution(
                paper,
                _Failure(
                    status="invalid_response",
                    reason="invalid_kicj_detail_html",
                    http_status=detail_fetch.status_code,
                    error_code="invalid_html",
                ),
                attempts,
                detail_url=detail_url,
                archive_pages_checked=pages_checked,
                identity_fields=identity_fields,
            )

        if not any(
            _issue_label_matches(label, identity)
            for label in parser.issue_labels
        ):
            return self._resolution(
                paper,
                _Failure(
                    status="identity_mismatch",
                    reason="kicj_detail_issue_identity_mismatch",
                    http_status=detail_fetch.status_code,
                    error_code="identity_mismatch",
                ),
                attempts,
                detail_url=detail_url,
                archive_pages_checked=pages_checked,
                identity_fields=identity_fields,
            )

        (
            pdf_url,
            matched_title,
            matched_authors,
            exact_title_matches,
            author_matches,
            failure,
        ) = _select_article_download(
            parser,
            identity=identity,
            detail_url=detail_url,
        )
        result_fields = {
            "detail_url": detail_url,
            "matched_title": matched_title,
            "matched_authors": matched_authors,
            "archive_pages_checked": pages_checked,
            "exact_title_matches": exact_title_matches,
            "author_matches": author_matches,
            "http_status": detail_fetch.status_code,
        }
        if failure is not None:
            return self._resolution(
                paper,
                failure,
                attempts,
                identity_fields=identity_fields,
                **result_fields,
            )
        assert pdf_url is not None

        if not policy.allows(pdf_url):
            return self._resolution(
                paper,
                _Failure(
                    status="robots_disallowed",
                    reason="kicj_download_disallowed_by_robots",
                    http_status=detail_fetch.status_code,
                    error_code="robots_disallowed",
                ),
                attempts,
                identity_fields=identity_fields,
                **result_fields,
            )

        candidate = ResolverCandidate(
            url=pdf_url,
            resolver=KICJ_RESOLVER_NAME,
            landing_url=detail_url,
            is_open_access=True,
            # The KICJ issue HTML does not expose a DOI beside this link.
            verified_doi=None,
        )
        return KicjResolution(
            status="resolved",
            reason="official_kicj_issue_exact_identity",
            paper=paper,
            candidate=candidate,
            attempts=tuple(attempts),
            **identity_fields,
            **result_fields,
        )

    def _official_robots_policy(
        self,
        attempts: list[KicjResolverAttempt],
    ) -> tuple[_RobotsPolicy | None, _Failure | None]:
        with self._robots_lock:
            if self._robots_policy is not None:
                return self._robots_policy, None

            fetch, failure = self._fetch(
                stage="kicj-robots",
                url=KICJ_ROBOTS_URL,
                max_bytes=self.config.max_robots_bytes,
                accept="text/plain;q=1.0",
                require_html=False,
                attempts=attempts,
            )
            if failure is not None:
                return None, failure
            assert fetch is not None
            document = _decode_utf8(fetch.body)
            if document is None or _looks_like_html(fetch.body):
                return None, _Failure(
                    status="invalid_response",
                    reason="invalid_kicj_robots",
                    http_status=fetch.status_code,
                    error_code="invalid_robots",
                )
            policy = _parse_robots_policy(
                document,
                user_agent=KICJ_USER_AGENT_TOKEN,
            )
            if policy is None:
                return None, _Failure(
                    status="robots_disallowed",
                    reason="kicj_robots_missing_explicit_allow",
                    http_status=fetch.status_code,
                    error_code="robots_disallowed",
                )
            if not policy.allows(KICJ_ARCHIVE_URL):
                return None, _Failure(
                    status="robots_disallowed",
                    reason="kicj_archive_disallowed_by_robots",
                    http_status=fetch.status_code,
                    error_code="robots_disallowed",
                )
            download_sentinel = f"{KICJ_ORIGIN}{KICJ_DOWNLOAD_PATH}"
            if not policy.allows(download_sentinel):
                return None, _Failure(
                    status="robots_disallowed",
                    reason="kicj_download_disallowed_by_robots",
                    http_status=fetch.status_code,
                    error_code="robots_disallowed",
                )
            self._robots_policy = policy
            return policy, None

    def _find_issue_detail(
        self,
        identity: _ExpectedIdentity,
        *,
        policy: _RobotsPolicy,
        attempts: list[KicjResolverAttempt],
    ) -> tuple[str | None, int, _Failure | None]:
        queue: deque[str] = deque([KICJ_ARCHIVE_URL])
        queued = {KICJ_ARCHIVE_URL}
        seen: set[str] = set()
        pages_checked = 0

        while queue and pages_checked < self.config.max_archive_pages:
            page_url = queue.popleft()
            if page_url in seen:
                continue
            seen.add(page_url)
            if not policy.allows(page_url):
                return None, pages_checked, _Failure(
                    status="robots_disallowed",
                    reason="kicj_archive_page_disallowed_by_robots",
                    error_code="robots_disallowed",
                )

            fetch, failure = self._fetch(
                stage=f"kicj-archive-{pages_checked + 1}",
                url=page_url,
                max_bytes=self.config.max_archive_response_bytes,
                accept="text/html,application/xhtml+xml;q=0.9",
                require_html=True,
                attempts=attempts,
            )
            if failure is not None:
                return None, pages_checked, failure
            assert fetch is not None
            pages_checked += 1

            document = _decode_utf8(fetch.body)
            if document is None:
                return None, pages_checked, _Failure(
                    status="invalid_response",
                    reason="invalid_kicj_archive_html",
                    http_status=fetch.status_code,
                    error_code="invalid_html",
                )
            parser = _ArchivePageParser()
            try:
                parser.feed(document)
                parser.close()
            except (AssertionError, ValueError):
                return None, pages_checked, _Failure(
                    status="invalid_response",
                    reason="invalid_kicj_archive_html",
                    http_status=fetch.status_code,
                    error_code="invalid_html",
                )

            detail_matches: set[str] = set()
            for anchor in parser.anchors:
                if not _issue_label_matches(anchor.label, identity):
                    continue
                try:
                    detail_matches.add(
                        _canonical_detail_url(anchor.href, page_url)
                    )
                except ValueError:
                    return None, pages_checked, _Failure(
                        status="invalid_response",
                        reason="unsafe_kicj_detail_link",
                        http_status=fetch.status_code,
                        error_code="unsafe_url",
                    )
            if len(detail_matches) == 1:
                return next(iter(detail_matches)), pages_checked, None
            if len(detail_matches) > 1:
                return None, pages_checked, _Failure(
                    status="ambiguous",
                    reason="multiple_kicj_issue_detail_links",
                    http_status=fetch.status_code,
                    error_code="ambiguous",
                )

            for anchor in parser.anchors:
                try:
                    archive_url = _canonical_archive_url(anchor.href, page_url)
                except ValueError:
                    continue
                if archive_url not in queued:
                    queued.add(archive_url)
                    queue.append(archive_url)

        if queue:
            return None, pages_checked, _Failure(
                status="no_match",
                reason="kicj_archive_page_limit_reached",
                error_code="bounded_search_exhausted",
            )
        return None, pages_checked, _Failure(
            status="no_match",
            reason="issue_not_in_public_kicj_archive",
            error_code="not_found",
        )

    def _fetch(
        self,
        *,
        stage: str,
        url: str,
        max_bytes: int,
        accept: str,
        require_html: bool,
        attempts: list[KicjResolverAttempt],
    ) -> tuple[HttpFetch | None, _Failure | None]:
        try:
            fetch = self.http.get_bytes(
                url,
                max_bytes=max_bytes,
                headers={"Accept": accept},
            )
        except HttpClientError as exc:
            code = getattr(exc, "code", "http_error")
            attempts.append(
                KicjResolverAttempt(
                    stage=stage,
                    url=url,
                    status="failed",
                    error_code=code,
                )
            )
            status = (
                "invalid_response"
                if code in {"unsafe_url", "redirect_limit", "too_large"}
                else "request_error"
            )
            return None, _Failure(
                status=status,
                reason=f"{stage}_request_failed",
                error_code=code,
            )

        attempts.append(
            KicjResolverAttempt(
                stage=stage,
                url=url,
                status="fetched",
                http_status=fetch.status_code,
                final_url=fetch.final_url,
            )
        )
        if (
            fetch.requested_url != url
            or fetch.final_url != url
            or fetch.redirects
        ):
            return None, _Failure(
                status="invalid_response",
                reason=f"{stage}_redirect_or_url_change_rejected",
                http_status=fetch.status_code,
                error_code="redirect_not_allowed",
            )
        if len(fetch.body) > max_bytes:
            return None, _Failure(
                status="invalid_response",
                reason=f"{stage}_response_too_large",
                http_status=fetch.status_code,
                error_code="too_large",
            )
        if not 200 <= fetch.status_code < 300:
            status = (
                "retryable_error"
                if fetch.status_code in _RETRYABLE_HTTP_STATUSES
                else "http_error"
            )
            return None, _Failure(
                status=status,
                reason=f"{stage}_http_error",
                http_status=fetch.status_code,
                error_code=(
                    "retryable_error" if status == "retryable_error" else "http_error"
                ),
            )
        if require_html and not _is_html(fetch):
            return None, _Failure(
                status="invalid_response",
                reason=f"{stage}_not_html",
                http_status=fetch.status_code,
                error_code="invalid_html",
            )
        return fetch, None

    @staticmethod
    def _resolution(
        paper: PaperCandidate,
        failure: _Failure,
        attempts: list[KicjResolverAttempt],
        *,
        identity_fields: Mapping[str, object],
        **fields: object,
    ) -> KicjResolution:
        values = dict(fields)
        values.setdefault("http_status", failure.http_status)
        return KicjResolution(
            status=failure.status,
            reason=failure.reason,
            paper=paper,
            attempts=tuple(attempts),
            error_code=failure.error_code,
            **identity_fields,
            **values,
        )


class _StatelessKicjHttp:
    """Default transport that cannot retain or resend cookies."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        client = SafeHttpClient(
            user_agent=f"{KICJ_USER_AGENT_TOKEN}/1.0",
            max_redirects=0,
            retries=0,
            allow_http=False,
        )
        try:
            return client.get_bytes(
                url,
                max_bytes=max_bytes,
                headers=headers,
            )
        finally:
            client.session.cookies.clear()
            client.session.close()


class _ArchivePageParser(HTMLParser):
    """Collect explicit anchors from one official archive page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_Anchor] = []
        self._in_anchor = False
        self._anchor_href: str | None = None
        self._anchor_classes: frozenset[str] = frozenset()
        self._anchor_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        if lowered != "a" or self._in_anchor:
            return
        attributes = _attributes(attrs)
        self._in_anchor = True
        self._anchor_href = attributes.get("href")
        self._anchor_classes = frozenset(
            attributes.get("class", "").casefold().split()
        )
        self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_anchor:
            self._anchor_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or not self._in_anchor:
            return
        self.anchors.append(
            _Anchor(
                href=self._anchor_href,
                label=_visible_text(self._anchor_parts),
                classes=self._anchor_classes,
            )
        )
        self._in_anchor = False
        self._anchor_href = None
        self._anchor_classes = frozenset()
        self._anchor_parts = []


class _DetailPageParser(HTMLParser):
    """Collect issue labels and article table rows without executing scripts."""

    _LABEL_TAGS = frozenset({"title", "caption", "h1", "h2", "h3", "h4"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.issue_labels: list[str] = []
        self.rows: list[tuple[_Cell, ...]] = []
        self._label_tag: str | None = None
        self._label_parts: list[str] = []
        self._in_row = False
        self._row_cells: list[_Cell] = []
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._cell_anchors: list[_Anchor] = []
        self._in_anchor = False
        self._anchor_href: str | None = None
        self._anchor_classes: frozenset[str] = frozenset()
        self._anchor_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.casefold()
        attributes = _attributes(attrs)

        if self._label_tag is None and lowered in self._LABEL_TAGS:
            self._label_tag = lowered
            self._label_parts = []

        if lowered == "tr" and not self._in_row:
            self._in_row = True
            self._row_cells = []
        elif lowered == "td" and self._in_row and not self._in_cell:
            self._in_cell = True
            self._cell_parts = []
            self._cell_anchors = []

        if lowered == "a" and self._in_cell and not self._in_anchor:
            self._in_anchor = True
            self._anchor_href = attributes.get("href")
            self._anchor_classes = frozenset(
                attributes.get("class", "").casefold().split()
            )
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._label_tag is not None:
            self._label_parts.append(data)
        if self._in_cell:
            self._cell_parts.append(data)
        if self._in_anchor:
            self._anchor_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()

        if lowered == "a" and self._in_anchor:
            self._cell_anchors.append(
                _Anchor(
                    href=self._anchor_href,
                    label=_visible_text(self._anchor_parts),
                    classes=self._anchor_classes,
                )
            )
            self._in_anchor = False
            self._anchor_href = None
            self._anchor_classes = frozenset()
            self._anchor_parts = []

        if lowered == "td" and self._in_cell:
            self._row_cells.append(
                _Cell(
                    text=_visible_text(self._cell_parts),
                    anchors=tuple(self._cell_anchors),
                )
            )
            self._in_cell = False
            self._cell_parts = []
            self._cell_anchors = []

        if lowered == "tr" and self._in_row and not self._in_cell:
            if self._row_cells:
                self.rows.append(tuple(self._row_cells))
            self._in_row = False
            self._row_cells = []

        if self._label_tag == lowered:
            label = _visible_text(self._label_parts)
            if label:
                self.issue_labels.append(label)
            self._label_tag = None
            self._label_parts = []


def _expected_identity(
    paper: PaperCandidate,
) -> tuple[_ExpectedIdentity | None, _Failure | None]:
    if _field_key(paper.journal_id) != _field_key(_JOURNAL_ID):
        return None, _Failure(
            status="not_applicable",
            reason="unsupported_kicj_journal",
        )
    if _field_key(paper.institution_id) != _field_key(_INSTITUTION_ID):
        return None, _Failure(
            status="not_applicable",
            reason="unsupported_kicj_institution",
        )

    titles: list[tuple[str, str]] = []
    seen_titles: set[str] = set()
    for value in (paper.title, *paper.alternate_titles):
        key = _title_key(value)
        if key is None or key in seen_titles or not isinstance(value, str):
            continue
        seen_titles.add(key)
        titles.append((value.strip(), key))
    if not titles:
        return None, _Failure(
            status="missing_metadata",
            reason="missing_kicj_title",
        )

    if (
        isinstance(paper.publication_year, bool)
        or not isinstance(paper.publication_year, int)
        or not 1990 <= paper.publication_year <= 2200
    ):
        return None, _Failure(
            status="missing_metadata",
            reason="missing_or_invalid_kicj_year",
        )
    issue_match = (
        _ISSUE_ID_RE.fullmatch(paper.issue_id.strip())
        if isinstance(paper.issue_id, str)
        else None
    )
    if issue_match is None:
        return None, _Failure(
            status="missing_metadata",
            reason="missing_or_invalid_kicj_issue_id",
        )
    volume = int(issue_match.group("volume"))
    issue = int(issue_match.group("issue"))
    if volume < 1 or volume + 1989 != paper.publication_year:
        return None, _Failure(
            status="identity_mismatch",
            reason="kicj_year_volume_mismatch",
            error_code="identity_mismatch",
        )
    serial_issue = (volume - 1) * 4 + issue

    first_page = _decimal(paper.first_page)
    final_page = _decimal(paper.final_page)
    if first_page is None or final_page is None:
        return None, _Failure(
            status="missing_metadata",
            reason="missing_or_invalid_kicj_pages",
        )
    if first_page < 1 or final_page < first_page:
        return None, _Failure(
            status="identity_mismatch",
            reason="invalid_kicj_page_range",
            error_code="identity_mismatch",
        )

    authors = _paper_author_keys(paper.authors)
    if not authors:
        return None, _Failure(
            status="missing_metadata",
            reason="missing_kicj_authors",
        )

    raw_doi = paper.doi or paper.doi_original
    doi: str | None = None
    if raw_doi:
        try:
            doi = normalize_doi(raw_doi)
        except InvalidDoiError:
            return None, _Failure(
                status="identity_mismatch",
                reason="invalid_kicj_doi",
                error_code="identity_mismatch",
            )
        if doi is None:
            return None, _Failure(
                status="identity_mismatch",
                reason="invalid_kicj_doi",
                error_code="identity_mismatch",
            )
        doi_match = _KICJ_DOI_RE.fullmatch(doi)
        if doi_match is None:
            return None, _Failure(
                status="identity_mismatch",
                reason="unsupported_kicj_doi_format",
                error_code="identity_mismatch",
            )
        doi_year = int(doi_match.group("year"))
        doi_month = int(doi_match.group("month"))
        doi_day = int(doi_match.group("day"))
        try:
            date(doi_year, doi_month, doi_day)
        except ValueError:
            return None, _Failure(
                status="identity_mismatch",
                reason="invalid_kicj_doi_date",
                error_code="identity_mismatch",
            )
        if doi_year != paper.publication_year:
            return None, _Failure(
                status="identity_mismatch",
                reason="kicj_doi_year_mismatch",
                error_code="identity_mismatch",
            )
        if int(doi_match.group("issue")) != issue:
            return None, _Failure(
                status="identity_mismatch",
                reason="kicj_doi_issue_mismatch",
                error_code="identity_mismatch",
            )
        # The final numeric DOI component is normally the article's first
        # printed page, but KCI's projected page range is not authoritative.
        # At least one row in the corpus (ART003129011) starts at page 99 in
        # the official PDF/DOI while KCI reports page 107.  Keep both values
        # as provenance and let the official issue-row title/author match plus
        # the downstream PDF DOI/title validation establish identity.

    return (
        _ExpectedIdentity(
            titles=tuple(titles),
            authors=authors,
            doi=doi,
            year=paper.publication_year,
            volume=volume,
            issue=issue,
            serial_issue=serial_issue,
            first_page=first_page,
            final_page=final_page,
        ),
        None,
    )


def _select_article_download(
    parser: _DetailPageParser,
    *,
    identity: _ExpectedIdentity,
    detail_url: str,
) -> tuple[
    str | None,
    str | None,
    tuple[str, ...],
    int,
    int,
    _Failure | None,
]:
    known_titles = frozenset(key for _title, key in identity.titles)
    list_no = _detail_list_no(detail_url)
    exact_title_matches = 0
    author_matches = 0
    candidates: dict[str, tuple[str, tuple[str, ...]]] = {}

    for row in parser.rows:
        if len(row) < 3:
            continue
        row_title = row[0].text
        row_title_key = _title_key(row_title)
        if row_title_key is None or not any(
            _official_row_title_matches(row_title_key, expected)
            for expected in known_titles
        ):
            continue
        exact_title_matches += 1

        observed_authors = _official_author_names(row[1].text)
        observed_author_keys = tuple(
            key
            for value in observed_authors
            if (key := _author_key(value)) is not None
        )
        if not _authors_match(identity.authors, observed_author_keys):
            continue
        author_matches += 1

        marked = [
            anchor
            for cell in row[2:]
            for anchor in cell.anchors
            if _is_download_anchor(anchor)
        ]
        if not marked:
            continue
        for anchor in marked:
            try:
                pdf_url = _canonical_download_url(
                    anchor.href,
                    detail_url,
                    expected_list_no=list_no,
                )
            except ValueError:
                return (
                    None,
                    row_title,
                    observed_authors,
                    exact_title_matches,
                    author_matches,
                    _Failure(
                        status="invalid_response",
                        reason="unsafe_kicj_pdf_link",
                        error_code="unsafe_url",
                    ),
                )
            candidates[pdf_url] = (row_title, observed_authors)

    if not exact_title_matches:
        return (
            None,
            None,
            (),
            0,
            0,
            _Failure(
                status="no_match",
                reason="kicj_exact_title_not_found",
                error_code="not_found",
            ),
        )
    if not author_matches:
        return (
            None,
            None,
            (),
            exact_title_matches,
            0,
            _Failure(
                status="identity_mismatch",
                reason="kicj_author_mismatch",
                error_code="identity_mismatch",
            ),
        )
    if not candidates:
        return (
            None,
            None,
            (),
            exact_title_matches,
            author_matches,
            _Failure(
                status="no_match",
                reason="missing_explicit_kicj_pdf_link",
                error_code="not_found",
            ),
        )
    if len(candidates) > 1:
        return (
            None,
            None,
            (),
            exact_title_matches,
            author_matches,
            _Failure(
                status="ambiguous",
                reason="multiple_kicj_pdf_links",
                error_code="ambiguous",
            ),
        )
    pdf_url, (matched_title, matched_authors) = next(iter(candidates.items()))
    return (
        pdf_url,
        matched_title,
        matched_authors,
        exact_title_matches,
        author_matches,
        None,
    )


def _canonical_archive_url(href: str | None, base_url: str) -> str:
    parsed = _official_link(href, base_url, expected_path=KICJ_ARCHIVE_PATH)
    values = _exact_query(
        parsed.query,
        required=frozenset({"mid", "bid"}),
        optional=frozenset({"nPage"}),
    )
    if values["mid"] != _MID or values["bid"] != _BID:
        raise ValueError("wrong_kicj_archive")
    page = values.get("nPage")
    if page is not None:
        if not _DECIMAL_RE.fullmatch(page) or not 1 <= int(page) <= 100:
            raise ValueError("invalid_kicj_archive_page")
    query: list[tuple[str, str]] = [("mid", _MID), ("bid", _BID)]
    if page is not None and int(page) != 1:
        query.append(("nPage", str(int(page))))
    return urlunsplit(("https", KICJ_HOST, KICJ_ARCHIVE_PATH, urlencode(query), ""))


def _canonical_detail_url(href: str | None, base_url: str) -> str:
    parsed = _official_link(href, base_url, expected_path=KICJ_ARCHIVE_PATH)
    values = _exact_query(
        parsed.query,
        required=frozenset({"mid", "bid", "act", "list_no"}),
        optional=frozenset({"nPage"}),
    )
    if (
        values["mid"] != _MID
        or values["bid"] != _BID
        or values["act"] != "view"
        or not _DECIMAL_RE.fullmatch(values["list_no"])
        or int(values["list_no"]) < 1
    ):
        raise ValueError("invalid_kicj_detail")
    page = values.get("nPage")
    if page is not None and (
        not _DECIMAL_RE.fullmatch(page) or not 1 <= int(page) <= 100
    ):
        raise ValueError("invalid_kicj_detail_page")
    query = [
        ("mid", _MID),
        ("bid", _BID),
        ("act", "view"),
        ("list_no", str(int(values["list_no"]))),
    ]
    if page is not None:
        query.append(("nPage", str(int(page))))
    return urlunsplit(("https", KICJ_HOST, KICJ_ARCHIVE_PATH, urlencode(query), ""))


def _canonical_download_url(
    href: str | None,
    base_url: str,
    *,
    expected_list_no: str,
) -> str:
    parsed = _official_link(href, base_url, expected_path=KICJ_DOWNLOAD_PATH)
    values = _exact_query(
        parsed.query,
        required=frozenset({"bid", "list_no", "seq"}),
        optional=frozenset(),
    )
    if (
        values["bid"] != _BID
        or values["list_no"] != expected_list_no
        or not _DECIMAL_RE.fullmatch(values["seq"])
        or int(values["seq"]) < 1
    ):
        raise ValueError("invalid_kicj_download")
    query = [
        ("bid", _BID),
        ("list_no", expected_list_no),
        ("seq", str(int(values["seq"]))),
    ]
    return urlunsplit(("https", KICJ_HOST, KICJ_DOWNLOAD_PATH, urlencode(query), ""))


def _official_link(
    href: str | None,
    base_url: str,
    *,
    expected_path: str,
):
    if not isinstance(href, str):
        raise ValueError("missing_kicj_href")
    value = html.unescape(href).strip()
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("invalid_kicj_href")
    try:
        parsed = urlsplit(urljoin(base_url, value))
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_kicj_url") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold().rstrip(".") != KICJ_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or parsed.fragment
    ):
        raise ValueError("unsafe_kicj_url")
    return parsed


def _exact_query(
    query: str,
    *,
    required: frozenset[str],
    optional: frozenset[str],
) -> dict[str, str]:
    pairs = parse_qsl(query, keep_blank_values=True)
    values: dict[str, str] = {}
    allowed = required | optional
    for key, value in pairs:
        if key not in allowed or key in values or not value:
            raise ValueError("invalid_kicj_query")
        values[key] = value
    if set(values) - optional != required or not required.issubset(values):
        raise ValueError("missing_kicj_query")
    return values


def _detail_list_no(detail_url: str) -> str:
    parsed = urlsplit(detail_url)
    values = dict(parse_qsl(parsed.query, keep_blank_values=True))
    value = values.get("list_no")
    if value is None or not _DECIMAL_RE.fullmatch(value):
        raise ValueError("missing_kicj_list_no")
    return str(int(value))


def _issue_label_matches(label: str, identity: _ExpectedIdentity) -> bool:
    value = _field_text(label)
    if not value or _title_key(_JOURNAL_ID) not in (_title_key(value) or ""):
        return False
    # Recent archive labels use both ``2025년 겨울호`` and
    # ``2024·가을호``.  The independently checked volume, quarterly issue,
    # and serial issue keep a bare, digit-bounded year unambiguous.
    if re.search(rf"(?<![0-9]){identity.year}(?![0-9])", value) is None:
        return False
    if (
        re.search(
            rf"(?:통권\s*)?제\s*{identity.serial_issue}\s*호",
            value,
        )
        is None
    ):
        return False
    if re.search(r"제\s*[0-9]+\s*권", value) is not None:
        return (
            re.search(
                rf"제\s*{identity.volume}\s*권\s*"
                rf"제\s*{identity.issue}\s*호",
                value,
            )
            is not None
        )
    return True


def _is_download_anchor(anchor: _Anchor) -> bool:
    label = _field_text(anchor.label).casefold()
    return "다운로드" in label or "download" in label


def _authors_match(
    expected: tuple[str, ...],
    observed: tuple[str, ...],
) -> bool:
    if not expected or not observed or expected[0] != observed[0]:
        return False
    position = 0
    for value in observed:
        if position < len(expected) and value == expected[position]:
            position += 1
    return position == len(expected)


def _paper_author_keys(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _author_key(value)
        if key is None or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return tuple(result)


def _official_author_names(value: str) -> tuple[str, ...]:
    parts = re.split(r"\s*(?:[·ㆍ]|,|/|&)\s*", _field_text(value))
    return tuple(part for part in (_field_text(item) for item in parts) if part)


def _author_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _field_text(value)
    normalized = re.sub(r"\s*[\(\[\{（].*\Z", "", normalized).strip()
    normalized = "".join(character for character in normalized if not character.isspace())
    return normalized.casefold() or None


def _title_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    # KICJ alternates these two Korean middle-dot glyphs between its HTML and
    # PDFs for the same title.  This is a character-level equivalence, not
    # general punctuation stripping.
    normalized = (
        normalized.replace("ㆍ", "·")
        .replace("ᆞ", "·")
        .replace("‧", "·")
    )
    normalized = "".join(character for character in normalized if not character.isspace())
    return normalized or None


def _official_row_title_matches(observed: str, expected: str) -> bool:
    """Match an exact title or KICJ's documented main-title-only row label.

    Some older KICJ issue tables omit a subtitle after a colon or dash even
    though the downloadable PDF contains the full title.  A long exact main
    title followed immediately by an explicit subtitle delimiter is safe here
    because the same row must also match authors and issue identity, and the
    downloaded PDF is independently checked against the complete DB title.
    """

    if observed == expected:
        return True
    if len(observed) < 12 or not expected.startswith(observed):
        return False
    remainder = expected[len(observed) :]
    return bool(remainder) and remainder[0] in {":", "：", "-", "–", "—"}


def _field_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _field_text(value).casefold()
    return normalized or None


def _field_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    return " ".join(normalized.split())


def _visible_text(parts: list[str]) -> str:
    return _field_text(" ".join(parts))


def _decimal(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not _DECIMAL_RE.fullmatch(stripped):
        return None
    return int(stripped)


def _attributes(
    attrs: list[tuple[str, str | None]],
) -> dict[str, str]:
    return {
        key.casefold(): html.unescape(value or "")
        for key, value in attrs
    }


def _decode_utf8(body: bytes) -> str | None:
    try:
        return body.decode("utf-8-sig")
    except UnicodeError:
        return None


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return ""


def _looks_like_html(body: bytes) -> bool:
    prefix = body[:512].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html"))


def _is_html(fetch: HttpFetch) -> bool:
    content_type = _header(fetch.headers, "content-type").casefold()
    if "text/html" in content_type or "application/xhtml+xml" in content_type:
        return True
    return _looks_like_html(fetch.body)


def _parse_robots_policy(
    document: str,
    *,
    user_agent: str,
) -> _RobotsPolicy | None:
    groups: list[_RobotsGroup] = []
    agents: list[str] = []
    rules: list[_RobotsRule] = []

    def finish() -> None:
        nonlocal agents, rules
        if agents:
            groups.append(
                _RobotsGroup(
                    agents=tuple(agents),
                    rules=tuple(rules),
                )
            )
        agents = []
        rules = []

    for raw_line in document.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, raw_value = line.split(":", 1)
        field = field.strip().casefold()
        value = raw_value.strip()
        if field == "user-agent":
            if rules:
                finish()
            agents.append(value.casefold())
        elif field in {"allow", "disallow"} and agents:
            if not value:
                continue
            rules.append(
                _RobotsRule(
                    pattern=value,
                    allow=field == "allow",
                )
            )
    finish()

    token = user_agent.casefold()
    specific: list[tuple[int, _RobotsGroup]] = []
    wildcard: list[_RobotsGroup] = []
    for group in groups:
        for agent in group.agents:
            if agent == "*":
                wildcard.append(group)
            elif agent and agent in token:
                specific.append((len(agent), group))
    if specific:
        length = max(item[0] for item in specific)
        selected = [group for item_length, group in specific if item_length == length]
    else:
        selected = wildcard
    selected_rules = tuple(rule for group in selected for rule in group.rules)
    # The resolver is intentionally fail-closed: implicit permission is not
    # enough.  KICJ currently publishes "User-agent: *" and "Allow: /".
    if not any(rule.allow and rule.pattern == "/" for rule in selected_rules):
        return None
    return _RobotsPolicy(rules=selected_rules)


def _robots_rule_matches(pattern: str, target: str) -> bool:
    if not pattern.startswith("/"):
        return False
    terminal = pattern.endswith("$")
    body = pattern[:-1] if terminal else pattern
    expression = "^" + re.escape(body).replace(r"\*", ".*")
    if terminal:
        expression += "$"
    return re.search(expression, target) is not None


def _robots_rule_length(pattern: str) -> int:
    return len(pattern.replace("*", "").removesuffix("$"))
