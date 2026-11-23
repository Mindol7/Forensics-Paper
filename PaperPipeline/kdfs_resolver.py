"""Permission-gated resolver for the official KDFS JAMS publication pages.

The Korean Digital Forensics Society (KDFS) publishes article metadata through
its JAMS site.  JAMS currently disallows generic crawlers in ``robots.txt`` and
the anonymous article UI may withhold the file control.  This resolver is
therefore disabled unless the operator explicitly confirms that they have
permission to automate the site by setting ``permission_granted=True``.

Permission to automate is not treated as permission to evade access controls.
The resolver remains anonymous and cookie-less, never logs in, never solves a
CAPTCHA, and never derives a file identifier from an article identifier.  It:

* submits an exact-title query to the fixed official HTTPS search endpoint;
* accepts only a same-row, exact KDFS journal/year/volume/issue/page/author
  match and the ``artiId`` explicitly advertised by that row;
* verifies the corresponding official detail page independently;
* follows only an explicit JAMS file control present in the anonymous search
  or detail HTML; and
* returns bytes only after the PDF itself matches the exact DOI (when known),
  title, journal, year, volume/issue, author, and printed page boundaries.

If the anonymous page says that login is required but does not expose a file
control, the result is ``permission_required``.  The module intentionally does
not attempt the otherwise predictable JAMS download URL in that case.
"""

from __future__ import annotations

import html
import io
import re
import threading
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    PerHostRateLimiter,
    SafeHttpClient,
    redact_url,
)
from PaperPipeline.identifier_resolver import normalize_title, paper_title_queries
from PaperPipeline.records import PaperCandidate, ResolverCandidate


KDFS_ORIGIN = "https://kdfs.jams.or.kr"
KDFS_HOST = "kdfs.jams.or.kr"
KDFS_SEARCH_PATH = "/po/search/poArtiSearList.kci"
KDFS_DETAIL_PATH = "/po/volisse/sjPubsArtiPopView.kci"
KDFS_DOWNLOAD_PATH = "/co/download/popup/poDownload.kci"
KDFS_SEARCH_URL = f"{KDFS_ORIGIN}{KDFS_SEARCH_PATH}"
KDFS_RESOLVER_NAME = "kdfs-jams-authorized"
KDFS_USER_AGENT_TOKEN = "Forensics-Paper-KDFS-Research-Resolver"

KDFS_INSTITUTION = "한국디지털포렌식학회"
KDFS_JOURNAL = "디지털포렌식연구"
KDFS_JOURNAL_ALIASES = frozenset({KDFS_JOURNAL, "SER000003109"})
KDFS_SOCE_ID = "INS000009412"
KDFS_SERE_ID = "SER000000001"
KDFS_INTERNAL_USE_NOTICE = (
    "Internal research copy collected under operator-confirmed automation "
    "permission; no redistribution licence is asserted."
)

_ART_ID_RE = re.compile(r"\AART[0-9]+\Z")
_JAMS_ARTICLE_ID_RE = re.compile(r"\ASJ[0-9]{10}\Z")
_SUBMISSION_COUNT_RE = re.compile(r"\A[1-9][0-9]{0,3}\Z")
_FILE_ID_RE = re.compile(r"\AKCI_(?:FI|ORTE)[0-9]{8,16}\Z")
_ISSUE_ID_RE = re.compile(
    r"\A0*(?P<volume>[1-9][0-9]*)_0*(?P<issue>[1-9][0-9]*)\Z"
)
_DOI_RE = re.compile(
    r"10[.][0-9]{4,9}/[A-Z0-9._;()/:+-]+",
    re.IGNORECASE,
)
_KDFS_DOI_RE = re.compile(
    r"\A10[.]22798/KDFS[.]"
    r"(?P<year>[0-9]{4})[.]"
    r"(?P<volume>[0-9]+)[.]"
    r"(?P<issue>[0-9]+)[.]"
    r"(?P<first_page>[0-9]+)\Z",
    re.IGNORECASE,
)
_DETAIL_ACTION_RE = re.compile(
    r"""\Ajavascript\s*:\s*fncGoList\(\s*
        (?P<q1>['"])(?P<soce>INS[0-9]+)(?P=q1)\s*,\s*
        (?P<q2>['"])(?P<article>SJ[0-9]+)(?P=q2)\s*,\s*
        (?P<q3>['"])(?P<sere>SER[0-9]+)(?P=q3)\s*,\s*
        (?P<q4>['"])(?P<count>[0-9]+)(?P=q4)\s*
        \)\s*;?\s*\Z""",
    re.IGNORECASE | re.VERBOSE,
)
_DOWNLOAD_ACTION_RE = re.compile(
    r"""\A(?:javascript\s*:\s*)?fncFileIframe\(\s*
        (?P<quote>['"])(?P<file>KCI_(?:FI|ORTE)[0-9]+)(?P=quote)\s*
        \)\s*;?\s*\Z""",
    re.IGNORECASE | re.VERBOSE,
)
_DOWNLOAD_TEMPLATE_RE = re.compile(
    r"""ifrmFile[.]location[.]href\s*=\s*
        ["']/co/download/popup/poDownload[.]kci[?]orteFileId=["']
        \s*[+]\s*orteFileId\s*;""",
    re.IGNORECASE | re.VERBOSE,
)
_VOLUME_ISSUE_RE = re.compile(
    r"(?:제\s*)?0*(?P<volume>[1-9][0-9]*)\s*권"
    r".{0,40}?"
    r"(?:제\s*)?0*(?P<issue>[1-9][0-9]*)\s*호",
    re.IGNORECASE | re.DOTALL,
)
_PAGE_RANGE_RE = re.compile(
    r"(?:pp?[.]?\s*)?"
    r"0*(?P<first>[0-9]+)\s*(?:~|[-–—])\s*0*(?P<final>[0-9]+)",
    re.IGNORECASE,
)
_LABELED_PAGE_RANGE_RE = re.compile(
    r"\bpp?[.]?\s*"
    r"0*(?P<first>[0-9]+)\s*(?:~|[-–—])\s*0*(?P<final>[0-9]+)",
    re.IGNORECASE,
)
_LEADING_RESULT_NUMBER_RE = re.compile(r"\A\s*[0-9]+\s*[.]\s*")
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_COOKIE_RESPONSE_HEADERS = frozenset({"set-cookie", "set-cookie2"})
_SENSITIVE_REQUEST_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)


class KdfsHttp(Protocol):
    """The two anonymous, bounded operations used by the resolver."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Fetch one official detail page or explicitly advertised file."""

    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Submit one exact-title query to the official search page."""


@dataclass(frozen=True, slots=True)
class KdfsResolverConfig:
    """Explicit authority switch and hard resource bounds."""

    permission_granted: bool = False
    requests_per_second: float = 0.5
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    http_retries: int = 2
    max_search_response_bytes: int = 2 * 1024 * 1024
    max_detail_response_bytes: int = 2 * 1024 * 1024
    max_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 2_000
    max_pdf_text_chars: int = 2_000_000
    max_title_queries: int = 3
    request_gate: Callable[[str], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class KdfsResolverAttempt:
    """One redacted network or identity decision."""

    stage: str
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class KdfsResolution:
    """One auditable KDFS decision, including a validated fetch on success."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = KDFS_RESOLVER_NAME
    candidate: ResolverCandidate | None = None
    fetch: HttpFetch | None = field(default=None, repr=False)
    matched_title: str | None = None
    jams_article_id: str | None = None
    detail_url: str | None = None
    download_url: str | None = None
    expected_doi: str | None = None
    expected_year: int | None = None
    expected_volume: int | None = None
    expected_issue: int | None = None
    expected_first_page: int | None = None
    expected_final_page: int | None = None
    attempts: tuple[KdfsResolverAttempt, ...] = ()
    http_status: int | None = None
    error_code: str | None = None
    usage_notice: str = KDFS_INTERNAL_USE_NOTICE

    @property
    def resolved(self) -> bool:
        return (
            self.status == "resolved"
            and self.candidate is not None
            and self.fetch is not None
        )

    @property
    def pdf_bytes(self) -> bytes | None:
        if not self.resolved or self.fetch is None:
            return None
        return self.fetch.body

    @property
    def bytes_received(self) -> int:
        return len(self.fetch.body) if self.fetch is not None else 0


@dataclass(frozen=True, slots=True)
class _ExpectedIdentity:
    titles: tuple[tuple[str, str], ...]
    authors: tuple[str, ...]
    doi: str | None
    year: int
    volume: int
    issue: int
    first_page: int
    final_page: int


@dataclass(frozen=True, slots=True)
class _Failure:
    status: str
    reason: str
    error_code: str
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class _Control:
    href: str | None
    onclick: str | None
    label: str


@dataclass(frozen=True, slots=True)
class _SearchRow:
    text: str
    anchors: tuple[tuple[str, str], ...]
    controls: tuple[_Control, ...]


@dataclass(frozen=True, slots=True)
class _SearchMatch:
    title: str
    article_id: str
    submission_count: str
    detail_url: str
    controls: tuple[_Control, ...]


@dataclass(frozen=True, slots=True)
class _DetailDocument:
    paragraphs: tuple[str, ...]
    info_text: str
    controls: tuple[_Control, ...]
    advertises_download_template: bool


@dataclass(frozen=True, slots=True)
class _PdfDocument:
    page_count: int
    first_pages: tuple[str, ...]
    last_page: str
    metadata_text: str


@dataclass
class _RowBuilder:
    depth: int
    text: list[str] = field(default_factory=list)
    anchors: list[tuple[str, str]] = field(default_factory=list)
    controls: list[_Control] = field(default_factory=list)


@dataclass
class _TextBuilder:
    depth: int
    parts: list[str] = field(default_factory=list)


class KdfsJamsAuthorizedPdfResolver:
    """Resolve an explicitly advertised KDFS JAMS PDF under permission."""

    def __init__(
        self,
        config: KdfsResolverConfig | None = None,
        *,
        http: KdfsHttp | None = None,
    ) -> None:
        self.config = config or KdfsResolverConfig()
        _validate_config(self.config)

        if self.config.request_gate is not None:
            request_gate = self.config.request_gate
        elif http is None:
            request_gate = PerHostRateLimiter(
                self.config.requests_per_second
            ).wait
        else:
            request_gate = None

        self.http: KdfsHttp = http or _StatelessKdfsHttp(
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
            retries=self.config.http_retries,
            request_gate=request_gate,
        )
        # A single resolver may be shared by worker threads.  JAMS requests are
        # intentionally sequential even when the caller supplies its own gate.
        self._lock = threading.RLock()

    def resolve(self, paper: PaperCandidate) -> KdfsResolution:
        """Return an already-fetched PDF only after exact identity checks."""

        if not self.config.permission_granted:
            return KdfsResolution(
                status="permission_required",
                reason="kdfs_automation_permission_not_enabled",
                error_code="permission_required",
                paper=paper,
            )
        with self._lock:
            return self._resolve_locked(paper)

    def _resolve_locked(self, paper: PaperCandidate) -> KdfsResolution:
        expected, eligibility = _expected_identity(paper)
        if eligibility is not None:
            return KdfsResolution(paper=paper, **eligibility)
        assert expected is not None

        identity_fields = {
            "expected_doi": expected.doi,
            "expected_year": expected.year,
            "expected_volume": expected.volume,
            "expected_issue": expected.issue,
            "expected_first_page": expected.first_page,
            "expected_final_page": expected.final_page,
        }
        attempts: list[KdfsResolverAttempt] = []

        match: _SearchMatch | None = None
        for title, _normalized in expected.titles[: self.config.max_title_queries]:
            fetch, failure = self._post(
                stage="kdfs-title-search",
                url=KDFS_SEARCH_URL,
                data={
                    "startPg": "1",
                    "itemTypeCd": "13",
                    "fieldList": "title_idx",
                    "keywordList": title,
                    "operationList": "AND",
                    "pubiStYr": "",
                    "pubiEndYr": "",
                },
                max_bytes=self.config.max_search_response_bytes,
                accept="text/html,application/xhtml+xml;q=0.9",
                require_html=True,
                attempts=attempts,
            )
            if failure is not None:
                return self._failure_resolution(
                    paper,
                    failure,
                    attempts,
                    **identity_fields,
                )
            assert fetch is not None
            document = _decode_html(fetch)
            if document is None:
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="invalid_response",
                        reason="invalid_kdfs_search_html",
                        error_code="invalid_html",
                        http_status=fetch.status_code,
                    ),
                    attempts,
                    **identity_fields,
                )
            parser = _SearchRowsParser()
            try:
                parser.feed(document)
                parser.close()
            except (AssertionError, ValueError):
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="invalid_response",
                        reason="invalid_kdfs_search_html",
                        error_code="invalid_html",
                        http_status=fetch.status_code,
                    ),
                    attempts,
                    **identity_fields,
                )

            match, selection_failure = _select_search_match(
                parser.rows,
                expected,
            )
            if selection_failure is not None:
                return self._failure_resolution(
                    paper,
                    selection_failure,
                    attempts,
                    **identity_fields,
                )
            if match is not None:
                break

        if match is None:
            return KdfsResolution(
                paper=paper,
                status="not_found",
                reason="exact_kdfs_article_not_found",
                error_code="not_found",
                attempts=tuple(attempts),
                **identity_fields,
            )

        detail_fetch, failure = self._get(
            stage="kdfs-article-detail",
            url=match.detail_url,
            max_bytes=self.config.max_detail_response_bytes,
            accept="text/html,application/xhtml+xml;q=0.9",
            require_html=True,
            attempts=attempts,
        )
        if failure is not None:
            return self._failure_resolution(
                paper,
                failure,
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                **identity_fields,
            )
        assert detail_fetch is not None
        detail_html = _decode_html(detail_fetch)
        if detail_html is None:
            return self._failure_resolution(
                paper,
                _Failure(
                    status="invalid_response",
                    reason="invalid_kdfs_detail_html",
                    error_code="invalid_html",
                    http_status=detail_fetch.status_code,
                ),
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                **identity_fields,
            )
        detail_parser = _DetailParser(detail_html)
        try:
            detail_parser.feed(detail_html)
            detail_parser.close()
        except (AssertionError, ValueError):
            return self._failure_resolution(
                paper,
                _Failure(
                    status="invalid_response",
                    reason="invalid_kdfs_detail_html",
                    error_code="invalid_html",
                    http_status=detail_fetch.status_code,
                ),
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                **identity_fields,
            )
        detail = detail_parser.document()
        detail_failure = _detail_identity_failure(detail, expected)
        if detail_failure is not None:
            return self._failure_resolution(
                paper,
                detail_failure,
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                **identity_fields,
            )

        download_url, control_failure = _select_download_url(
            (*match.controls, *detail.controls),
            template_advertised=detail.advertises_download_template,
        )
        if control_failure is not None:
            return self._failure_resolution(
                paper,
                control_failure,
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                **identity_fields,
            )
        if download_url is None:
            return KdfsResolution(
                paper=paper,
                status="permission_required",
                reason="kdfs_anonymous_download_not_advertised",
                error_code="permission_required",
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                attempts=tuple(attempts),
                **identity_fields,
            )

        pdf_fetch, failure = self._get(
            stage="kdfs-pdf-download",
            url=download_url,
            max_bytes=self.config.max_pdf_bytes,
            accept="application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
            require_html=False,
            attempts=attempts,
            referer=match.detail_url,
        )
        if failure is not None:
            return self._failure_resolution(
                paper,
                failure,
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                download_url=download_url,
                **identity_fields,
            )
        assert pdf_fetch is not None
        if _is_html(pdf_fetch):
            return self._failure_resolution(
                paper,
                _Failure(
                    status="access_denied",
                    reason="kdfs_download_returned_html_or_login",
                    error_code="access_denied",
                    http_status=pdf_fetch.status_code,
                ),
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                download_url=download_url,
                **identity_fields,
            )
        try:
            pdf = _read_pdf_document(
                pdf_fetch.body,
                max_pages=self.config.max_pdf_pages,
                max_text_chars=self.config.max_pdf_text_chars,
            )
        except ValueError:
            return self._failure_resolution(
                paper,
                _Failure(
                    status="invalid_pdf",
                    reason="kdfs_attachment_is_not_a_safe_parseable_pdf",
                    error_code="invalid_pdf",
                    http_status=pdf_fetch.status_code,
                ),
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                download_url=download_url,
                **identity_fields,
            )
        pdf_failure = _pdf_identity_failure(pdf, expected)
        if pdf_failure is not None:
            return self._failure_resolution(
                paper,
                pdf_failure,
                attempts,
                matched_title=match.title,
                jams_article_id=match.article_id,
                detail_url=match.detail_url,
                download_url=download_url,
                **identity_fields,
            )

        sanitized_fetch = _without_cookie_headers(pdf_fetch)
        return KdfsResolution(
            paper=paper,
            status="resolved",
            reason="official_kdfs_jams_exact_pdf_identity",
            candidate=ResolverCandidate(
                url=download_url,
                resolver=KDFS_RESOLVER_NAME,
                landing_url=match.detail_url,
                is_open_access=None,
                verified_doi=expected.doi,
            ),
            fetch=sanitized_fetch,
            matched_title=match.title,
            jams_article_id=match.article_id,
            detail_url=match.detail_url,
            download_url=download_url,
            attempts=tuple(attempts),
            **identity_fields,
        )

    def _get(
        self,
        *,
        stage: str,
        url: str,
        max_bytes: int,
        accept: str,
        require_html: bool,
        attempts: list[KdfsResolverAttempt],
        referer: str | None = None,
    ) -> tuple[HttpFetch | None, _Failure | None]:
        headers = {"Accept": accept}
        if referer is not None:
            headers["Referer"] = referer
        try:
            fetch = self.http.get_bytes(
                url,
                max_bytes=max_bytes,
                headers=headers,
            )
        except HttpClientError as exc:
            return None, _http_exception_failure(stage, url, exc, attempts)
        return _validate_fetch(
            stage=stage,
            url=url,
            fetch=fetch,
            require_html=require_html,
            attempts=attempts,
        )

    def _post(
        self,
        *,
        stage: str,
        url: str,
        data: Mapping[str, str],
        max_bytes: int,
        accept: str,
        require_html: bool,
        attempts: list[KdfsResolverAttempt],
    ) -> tuple[HttpFetch | None, _Failure | None]:
        try:
            fetch = self.http.post_form_bytes(
                url,
                data=data,
                max_bytes=max_bytes,
                headers={"Accept": accept},
            )
        except HttpClientError as exc:
            return None, _http_exception_failure(stage, url, exc, attempts)
        return _validate_fetch(
            stage=stage,
            url=url,
            fetch=fetch,
            require_html=require_html,
            attempts=attempts,
        )

    @staticmethod
    def _failure_resolution(
        paper: PaperCandidate,
        failure: _Failure,
        attempts: list[KdfsResolverAttempt],
        **fields: Any,
    ) -> KdfsResolution:
        return KdfsResolution(
            paper=paper,
            status=failure.status,
            reason=failure.reason,
            error_code=failure.error_code,
            http_status=failure.http_status,
            attempts=tuple(attempts),
            **fields,
        )


class _SearchRowsParser(HTMLParser):
    """Capture each result block without borrowing metadata across rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_SearchRow] = []
        self._row: _RowBuilder | None = None
        self._anchor_depth = 0
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = _attributes(attrs)
        folded = tag.casefold()
        classes = frozenset(attributes.get("class", "").casefold().split())
        if self._row is None and folded == "div" and "col-12" in classes:
            self._row = _RowBuilder(depth=1)
        elif self._row is not None and folded == "div":
            self._row.depth += 1

        if self._row is None:
            return
        control = _control_from_attributes(folded, attributes)
        if control is not None:
            self._row.controls.append(control)
        if folded == "a":
            self._anchor_depth = 1
            self._anchor_href = attributes.get("href", "").strip() or None
            self._anchor_text = []
        elif self._anchor_depth:
            self._anchor_depth += 1

    def handle_data(self, data: str) -> None:
        if self._row is not None:
            self._row.text.append(data)
        if self._anchor_depth:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if self._row is None:
            return
        if self._anchor_depth:
            self._anchor_depth -= 1
            if self._anchor_depth == 0:
                if self._anchor_href:
                    self._row.anchors.append(
                        (
                            self._anchor_href,
                            # Inline search-highlight tags can split a Korean
                            # word (for example ``딥`` + ``페이크``).  Preserve
                            # the source text rather than inventing whitespace.
                            _plain_text("".join(self._anchor_text)),
                        )
                    )
                self._anchor_href = None
                self._anchor_text = []
        if folded == "div":
            self._row.depth -= 1
            if self._row.depth == 0:
                self.rows.append(
                    _SearchRow(
                        text=_plain_text(" ".join(self._row.text)),
                        anchors=tuple(self._row.anchors),
                        controls=tuple(self._row.controls),
                    )
                )
                self._row = None


class _DetailParser(HTMLParser):
    """Capture the article's own info block and explicit file controls."""

    def __init__(self, raw_document: str) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.controls: list[_Control] = []
        self._info_depth = 0
        self._info_text: list[str] = []
        self._paragraph: _TextBuilder | None = None
        self._advertises_download_template = (
            _DOWNLOAD_TEMPLATE_RE.search(raw_document) is not None
        )

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = _attributes(attrs)
        folded = tag.casefold()
        classes = frozenset(attributes.get("class", "").casefold().split())
        if self._info_depth == 0 and folded == "div" and "infos" in classes:
            self._info_depth = 1
        elif self._info_depth and folded == "div":
            self._info_depth += 1

        if self._info_depth:
            if self._paragraph is None and folded == "p":
                self._paragraph = _TextBuilder(depth=1)
            elif self._paragraph is not None:
                self._paragraph.depth += 1

        control = _control_from_attributes(folded, attributes)
        if control is not None:
            self.controls.append(control)

    def handle_data(self, data: str) -> None:
        if self._info_depth:
            self._info_text.append(data)
        if self._paragraph is not None:
            self._paragraph.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if self._paragraph is not None:
            self._paragraph.depth -= 1
            if self._paragraph.depth == 0:
                self.paragraphs.append(
                    _plain_text("".join(self._paragraph.parts))
                )
                self._paragraph = None
        if self._info_depth and folded == "div":
            self._info_depth -= 1

    def document(self) -> _DetailDocument:
        return _DetailDocument(
            paragraphs=tuple(self.paragraphs),
            info_text=_plain_text(" ".join(self._info_text)),
            controls=tuple(self.controls),
            advertises_download_template=self._advertises_download_template,
        )


class _StatelessKdfsHttp:
    """Use a fresh safe client per request so JAMS cookies cannot carry over."""

    def __init__(
        self,
        *,
        connect_timeout: float,
        read_timeout: float,
        retries: int,
        request_gate: Callable[[str], None] | None,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retries = retries
        self.request_gate = request_gate

    def _client(self) -> SafeHttpClient:
        return SafeHttpClient(
            user_agent=(
                f"{KDFS_USER_AGENT_TOKEN}/1.0 "
                "(authorized internal academic research; no redistribution)"
            ),
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            max_redirects=0,
            retries=self.retries,
            allow_http=False,
            request_gate=self.request_gate,
        )

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        return self._client().get_bytes(
            url,
            max_bytes=max_bytes,
            headers=_safe_headers(headers),
        )

    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        return self._client().post_form_bytes(
            url,
            data=data,
            max_bytes=max_bytes,
            headers=_safe_headers(headers),
        )


def _validate_config(config: KdfsResolverConfig) -> None:
    if not 0 < config.requests_per_second <= 1:
        raise ValueError("requests_per_second must be in (0, 1]")
    if config.connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
    if config.read_timeout <= 0:
        raise ValueError("read_timeout must be positive")
    if config.http_retries < 0:
        raise ValueError("http_retries must be non-negative")
    for name in (
        "max_search_response_bytes",
        "max_detail_response_bytes",
        "max_pdf_bytes",
        "max_pdf_pages",
        "max_pdf_text_chars",
        "max_title_queries",
    ):
        if int(getattr(config, name)) < 1:
            raise ValueError(f"{name} must be positive")


def _expected_identity(
    paper: PaperCandidate,
) -> tuple[_ExpectedIdentity | None, dict[str, Any] | None]:
    if paper.institution_id != KDFS_INSTITUTION:
        return None, {
            "status": "unsupported",
            "reason": "unsupported_kdfs_institution",
            "error_code": "unsupported",
        }
    if paper.journal_id not in KDFS_JOURNAL_ALIASES:
        return None, {
            "status": "unsupported",
            "reason": "unsupported_kdfs_journal",
            "error_code": "unsupported",
        }
    if not _ART_ID_RE.fullmatch(paper.source_id):
        return None, {
            "status": "missing_metadata",
            "reason": "invalid_kci_source_id",
            "error_code": "missing_metadata",
        }

    titles = paper_title_queries(paper)
    issue_match = _ISSUE_ID_RE.fullmatch((paper.issue_id or "").strip())
    authors = tuple(
        value
        for author in paper.authors
        if (value := _author_name(author)) is not None
    )
    try:
        year = int(paper.publication_year) if paper.publication_year is not None else 0
        first_page = int((paper.first_page or "").strip())
        final_page = int((paper.final_page or "").strip())
    except (TypeError, ValueError):
        year = first_page = final_page = 0
    if (
        not titles
        or issue_match is None
        or not authors
        or not 1900 <= year <= 2100
        or first_page < 1
        or final_page < first_page
    ):
        return None, {
            "status": "missing_metadata",
            "reason": "incomplete_kdfs_bibliographic_identity",
            "error_code": "missing_metadata",
        }

    doi: str | None = None
    raw_doi = paper.doi or paper.doi_original
    if raw_doi:
        try:
            doi = normalize_doi(raw_doi)
        except InvalidDoiError:
            return None, {
                "status": "missing_metadata",
                "reason": "invalid_kdfs_doi",
                "error_code": "missing_metadata",
            }
        doi_match = _KDFS_DOI_RE.fullmatch(doi)
        if (
            doi_match is None
            or int(doi_match.group("year")) != year
            or int(doi_match.group("volume")) != int(issue_match.group("volume"))
            or int(doi_match.group("issue")) != int(issue_match.group("issue"))
        ):
            return None, {
                "status": "identity_mismatch",
                "reason": "kdfs_doi_bibliographic_identity_mismatch",
                "error_code": "identity_mismatch",
            }
        # Most KDFS DOI suffixes end in the first printed page, but that is a
        # publisher naming convention rather than a DOI guarantee.  At least
        # one official DB row (ART003226378) uses ``...19.3.157`` for an
        # article whose official range starts at 153.  The exact DOI is still
        # required in the PDF; page identity is verified independently.

    return (
        _ExpectedIdentity(
            titles=titles,
            authors=authors,
            doi=doi,
            year=year,
            volume=int(issue_match.group("volume")),
            issue=int(issue_match.group("issue")),
            first_page=first_page,
            final_page=final_page,
        ),
        None,
    )


def _select_search_match(
    rows: tuple[_SearchRow, ...] | list[_SearchRow],
    expected: _ExpectedIdentity,
) -> tuple[_SearchMatch | None, _Failure | None]:
    title_matches: list[tuple[_SearchRow, str, re.Match[str]]] = []
    known_titles = {
        normalized: title for title, normalized in expected.titles
    }
    for row in rows:
        for href, label in row.anchors:
            detail_match = _DETAIL_ACTION_RE.fullmatch(html.unescape(href).strip())
            if detail_match is None:
                continue
            candidate_title = _LEADING_RESULT_NUMBER_RE.sub("", label).strip()
            normalized = normalize_title(candidate_title)
            if normalized in known_titles:
                title_matches.append(
                    (row, known_titles[normalized], detail_match)
                )

    if not title_matches:
        return None, None

    valid: dict[str, _SearchMatch] = {}
    saw_bibliographic_mismatch = False
    for row, title, match in title_matches:
        if (
            match.group("soce") != KDFS_SOCE_ID
            or match.group("sere") != KDFS_SERE_ID
            or _JAMS_ARTICLE_ID_RE.fullmatch(match.group("article")) is None
            or _SUBMISSION_COUNT_RE.fullmatch(match.group("count")) is None
        ):
            saw_bibliographic_mismatch = True
            continue
        if _search_row_identity_failure(row.text, expected) is not None:
            saw_bibliographic_mismatch = True
            continue
        detail_url = _detail_url(
            article_id=match.group("article"),
            submission_count=match.group("count"),
        )
        valid[detail_url] = _SearchMatch(
            title=title,
            article_id=match.group("article"),
            submission_count=match.group("count"),
            detail_url=detail_url,
            controls=row.controls,
        )

    if len(valid) == 1:
        return next(iter(valid.values())), None
    if len(valid) > 1:
        return None, _Failure(
            status="ambiguous",
            reason="multiple_exact_kdfs_article_matches",
            error_code="ambiguous",
        )
    if saw_bibliographic_mismatch:
        return None, _Failure(
            status="identity_mismatch",
            reason="kdfs_search_bibliographic_identity_mismatch",
            error_code="identity_mismatch",
        )
    return None, None


def _search_row_identity_failure(
    text: str,
    expected: _ExpectedIdentity,
) -> str | None:
    token = _identity_token(text)
    if _identity_token(KDFS_JOURNAL) not in token:
        return "journal"
    if not _year_present(text, expected.year):
        return "year"
    if not _volume_issue_present(text, expected.volume, expected.issue):
        return "volume_issue"
    if not _page_range_present(text, expected.first_page, expected.final_page):
        return "page_range"
    if _identity_token(expected.authors[0]) not in token:
        return "author"
    return None


def _detail_identity_failure(
    detail: _DetailDocument,
    expected: _ExpectedIdentity,
) -> _Failure | None:
    known_titles = {
        normalized for _title, normalized in expected.titles
    }
    observed_titles = {
        normalize_title(_strip_detail_label(paragraph))
        for paragraph in detail.paragraphs
    }
    if not (known_titles & observed_titles):
        return _identity_failure("kdfs_detail_title_mismatch")
    token = _identity_token(detail.info_text)
    if _identity_token(KDFS_JOURNAL) not in token:
        return _identity_failure("kdfs_detail_journal_mismatch")
    if not _volume_issue_present(
        detail.info_text,
        expected.volume,
        expected.issue,
    ):
        return _identity_failure("kdfs_detail_volume_issue_mismatch")
    if not _page_range_present(
        detail.info_text,
        expected.first_page,
        expected.final_page,
    ):
        return _identity_failure("kdfs_detail_page_range_mismatch")
    if _identity_token(expected.authors[0]) not in token:
        return _identity_failure("kdfs_detail_author_mismatch")
    return None


def _select_download_url(
    controls: tuple[_Control, ...],
    *,
    template_advertised: bool,
) -> tuple[str | None, _Failure | None]:
    urls: set[str] = set()
    unsafe_explicit_control = False
    for control in controls:
        if control.href:
            href = html.unescape(control.href).strip()
            if _DOWNLOAD_ACTION_RE.fullmatch(href):
                if not template_advertised:
                    unsafe_explicit_control = True
                else:
                    action = _DOWNLOAD_ACTION_RE.fullmatch(href)
                    assert action is not None
                    url = _download_url(action.group("file"))
                    if url is None:
                        unsafe_explicit_control = True
                    else:
                        urls.add(url)
            elif _looks_like_download_reference(href, control.label):
                absolute = urljoin(KDFS_ORIGIN, href)
                if _is_exact_official_url(absolute, kind="download"):
                    urls.add(absolute)
                else:
                    unsafe_explicit_control = True
        if control.onclick:
            onclick = html.unescape(control.onclick).strip()
            action = _DOWNLOAD_ACTION_RE.fullmatch(onclick)
            if action is not None:
                if not template_advertised:
                    unsafe_explicit_control = True
                else:
                    url = _download_url(action.group("file"))
                    if url is None:
                        unsafe_explicit_control = True
                    else:
                        urls.add(url)
            elif "fncfileiframe" in onclick.casefold():
                unsafe_explicit_control = True

    if len(urls) > 1:
        return None, _Failure(
            status="ambiguous",
            reason="multiple_kdfs_download_controls",
            error_code="ambiguous",
        )
    if unsafe_explicit_control:
        return None, _Failure(
            status="invalid_response",
            reason="unsafe_kdfs_download_control",
            error_code="unsafe_url",
        )
    return (next(iter(urls)) if urls else None), None


def _control_from_attributes(
    tag: str,
    attributes: Mapping[str, str],
) -> _Control | None:
    if tag not in {"a", "button", "input"}:
        return None
    href = attributes.get("href", "").strip() or None
    onclick = attributes.get("onclick", "").strip() or None
    label = _plain_text(
        " ".join(
            value
            for value in (
                attributes.get("title", ""),
                attributes.get("aria-label", ""),
                attributes.get("value", ""),
                attributes.get("class", ""),
            )
            if value
        )
    )
    if (
        (href and (
            "podownload.kci" in href.casefold()
            or "fncfileiframe" in href.casefold()
        ))
        or (onclick and "fncfileiframe" in onclick.casefold())
    ):
        return _Control(href=href, onclick=onclick, label=label)
    return None


def _looks_like_download_reference(href: str, label: str) -> bool:
    lowered = href.casefold()
    label_token = _identity_token(label)
    return (
        "podownload.kci" in lowered
        or any(value in label_token for value in ("pdf", "원문", "다운로드"))
    )


def _detail_url(*, article_id: str, submission_count: str) -> str:
    return (
        f"{KDFS_ORIGIN}{KDFS_DETAIL_PATH}?"
        + urlencode(
            {
                "soceId": KDFS_SOCE_ID,
                "artiId": article_id,
                "sereId": KDFS_SERE_ID,
                "submCnt": submission_count,
            }
        )
    )


def _download_url(file_id: str) -> str | None:
    if _FILE_ID_RE.fullmatch(file_id) is None:
        return None
    return (
        f"{KDFS_ORIGIN}{KDFS_DOWNLOAD_PATH}?"
        + urlencode({"orteFileId": file_id})
    )


def _read_pdf_document(
    body: bytes,
    *,
    max_pages: int,
    max_text_chars: int,
) -> _PdfDocument:
    if body.find(b"%PDF-", 0, 1024) < 0:
        raise ValueError("missing PDF signature")
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(body), strict=False)
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    raise ValueError("encrypted PDF")
            except (NotImplementedError, TypeError) as exc:
                raise ValueError("encrypted PDF") from exc
        page_count = len(reader.pages)
        if not 1 <= page_count <= max_pages:
            raise ValueError("unsafe PDF page count")
        indices = list(range(min(3, page_count)))
        if page_count - 1 not in indices:
            indices.append(page_count - 1)
        texts: dict[int, str] = {}
        consumed = 0
        for index in indices:
            value = reader.pages[index].extract_text() or ""
            consumed += len(value)
            if consumed > max_text_chars:
                raise ValueError("PDF text exceeds inspection bound")
            texts[index] = _plain_text(value)
        metadata = reader.metadata
        metadata_text = _plain_text(
            " ".join(
                str(value)
                for value in (
                    getattr(metadata, "title", None),
                    getattr(metadata, "author", None),
                    getattr(metadata, "subject", None),
                )
                if value
            )
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("unable to inspect PDF") from exc
    return _PdfDocument(
        page_count=page_count,
        first_pages=tuple(texts[index] for index in range(min(3, page_count))),
        last_page=texts[page_count - 1],
        metadata_text=metadata_text,
    )


def _pdf_identity_failure(
    pdf: _PdfDocument,
    expected: _ExpectedIdentity,
) -> _Failure | None:
    expected_span = expected.final_page - expected.first_page
    if pdf.page_count not in {expected_span, expected_span + 1}:
        return _identity_failure("kdfs_pdf_page_count_mismatch")

    first_text = _plain_text(
        " ".join((*pdf.first_pages, pdf.metadata_text))
    )
    first_token = _identity_token(first_text)
    known_title_tokens = {
        _identity_token(title)
        for title, _normalized in expected.titles
        if _identity_token(title)
    }
    if not any(token in first_token for token in known_title_tokens):
        return _identity_failure("kdfs_pdf_title_mismatch")

    pdf_doi_exact = False
    if expected.doi is not None:
        observed_dois = _normalized_dois(first_text)
        # Some official KDFS PDFs omit a DOI from their rendered text.  Treat
        # absence separately from contradiction: the remaining complete PDF
        # bibliography still has to match below, while any explicitly
        # observed different DOI remains a hard identity failure.
        if observed_dois and expected.doi.casefold() not in observed_dois:
            return _identity_failure("kdfs_pdf_doi_mismatch")
        pdf_doi_exact = expected.doi.casefold() in observed_dois
    if _identity_token(KDFS_JOURNAL) not in first_token:
        return _identity_failure("kdfs_pdf_journal_mismatch")
    # A KDFS DOI embeds the publication year and first page.  Those DOI
    # components cannot double as independent visible bibliography evidence.
    bibliography_text = _DOI_RE.sub(" ", first_text)
    if not _year_present(bibliography_text, expected.year):
        return _identity_failure("kdfs_pdf_year_mismatch")
    if not _volume_issue_present(first_text, expected.volume, expected.issue):
        return _identity_failure("kdfs_pdf_volume_issue_mismatch")
    if _identity_token(expected.authors[0]) not in first_token:
        return _identity_failure("kdfs_pdf_author_mismatch")
    # PDF body text contains many unrelated numeric ranges (dates, standards,
    # experiment intervals, and citations).  Only a range explicitly labeled
    # as a page range is contradictory bibliography evidence here.  Search
    # and detail HTML already established the exact official article range;
    # an unlabeled/absent PDF range falls back to page count plus the printed
    # terminal page below.
    first_page_bibliography_text = _DOI_RE.sub(
        " ",
        _plain_text(
            " ".join(
                (
                    pdf.first_pages[0] if pdf.first_pages else "",
                    pdf.metadata_text,
                )
            )
        ),
    )
    observed_ranges = tuple(
        (
            int(match.group("first")),
            int(match.group("final")),
        )
        for match in _LABELED_PAGE_RANGE_RE.finditer(
            unicodedata.normalize("NFKC", first_page_bibliography_text)
        )
    )
    exact_range = any(
        first == expected.first_page and final == expected.final_page
        for first, final in observed_ranges
    )
    if observed_ranges and not any(
        first == expected.first_page for first, _final in observed_ranges
    ):
        # An explicitly printed different first page is contradictory.
        return _identity_failure("kdfs_pdf_first_page_mismatch")
    if observed_ranges and not exact_range:
        # Likewise, do not override an explicitly different complete range.
        return _identity_failure("kdfs_pdf_final_page_mismatch")
    # Official KDFS PDFs do not consistently print a combined ``pp. N-M``
    # range in their opening pages.  When no range is advertised at all, the
    # already-checked exact title/journal/year/volume/issue/author and bounded
    # page count are combined with the printed terminal page below.  This is
    # an absence fallback only; explicit conflicting ranges were rejected.
    if (
        not _page_number_present(pdf.last_page, expected.final_page)
        and not pdf_doi_exact
    ):
        return _identity_failure("kdfs_pdf_final_page_mismatch")
    return None


def _normalized_dois(text: str) -> frozenset[str]:
    result: set[str] = set()
    for match in _DOI_RE.finditer(text):
        raw = match.group(0).rstrip(".,;)]}")
        try:
            result.add(normalize_doi(raw).casefold())
        except InvalidDoiError:
            continue
    return frozenset(result)


def _identity_failure(reason: str) -> _Failure:
    return _Failure(
        status="identity_mismatch",
        reason=reason,
        error_code="identity_mismatch",
    )


def _validate_fetch(
    *,
    stage: str,
    url: str,
    fetch: HttpFetch,
    require_html: bool,
    attempts: list[KdfsResolverAttempt],
) -> tuple[HttpFetch | None, _Failure | None]:
    kind = (
        "search"
        if url == KDFS_SEARCH_URL
        else "detail"
        if urlsplit(url).path == KDFS_DETAIL_PATH
        else "download"
    )
    if (
        fetch.requested_url != url
        or fetch.final_url != url
        or fetch.redirects
        or not _is_exact_official_url(url, kind=kind)
    ):
        attempts.append(
            KdfsResolverAttempt(
                stage=stage,
                url=redact_url(url),
                status="failed",
                http_status=fetch.status_code,
                final_url=redact_url(fetch.final_url),
                error_code="unsafe_redirect",
            )
        )
        return None, _Failure(
            status="invalid_response",
            reason="kdfs_response_url_or_redirect_mismatch",
            error_code="unsafe_redirect",
            http_status=fetch.status_code,
        )

    status = int(fetch.status_code)
    if status in _RETRYABLE_STATUSES:
        failure = _Failure(
            status="retryable_error",
            reason="retryable_kdfs_http_status",
            error_code="retryable_error",
            http_status=status,
        )
    elif status in {401, 403}:
        failure = _Failure(
            status="access_denied",
            reason="kdfs_http_access_denied",
            error_code="access_denied",
            http_status=status,
        )
    elif status == 404:
        failure = _Failure(
            status="not_found",
            reason="kdfs_http_not_found",
            error_code="not_found",
            http_status=status,
        )
    elif not 200 <= status < 300:
        failure = _Failure(
            status="invalid_response",
            reason="unexpected_kdfs_http_status",
            error_code="http_error",
            http_status=status,
        )
    elif require_html and not _is_html(fetch):
        failure = _Failure(
            status="invalid_response",
            reason="kdfs_response_is_not_html",
            error_code="invalid_html",
            http_status=status,
        )
    else:
        attempts.append(
            KdfsResolverAttempt(
                stage=stage,
                url=redact_url(url),
                status="succeeded",
                http_status=status,
                final_url=redact_url(fetch.final_url),
            )
        )
        return fetch, None

    attempts.append(
        KdfsResolverAttempt(
            stage=stage,
            url=redact_url(url),
            status="failed",
            http_status=status,
            final_url=redact_url(fetch.final_url),
            error_code=failure.error_code,
        )
    )
    return None, failure


def _http_exception_failure(
    stage: str,
    url: str,
    exc: HttpClientError,
    attempts: list[KdfsResolverAttempt],
) -> _Failure:
    error_code = getattr(exc, "code", "http_error")
    status = (
        "retryable_error"
        if error_code == "retryable_error"
        else "invalid_response"
    )
    attempts.append(
        KdfsResolverAttempt(
            stage=stage,
            url=redact_url(url),
            status="failed",
            error_code=error_code,
        )
    )
    return _Failure(
        status=status,
        reason=f"kdfs_{stage}_http_error",
        error_code=error_code,
    )


def _is_exact_official_url(url: str, *, kind: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold().rstrip(".") != KDFS_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        return False
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if kind == "search":
        return parsed.path == KDFS_SEARCH_PATH and not pairs
    if kind == "detail":
        if parsed.path != KDFS_DETAIL_PATH:
            return False
        return pairs == [
            ("soceId", KDFS_SOCE_ID),
            ("artiId", next(
                (value for key, value in pairs if key == "artiId"),
                "",
            )),
            ("sereId", KDFS_SERE_ID),
            ("submCnt", next(
                (value for key, value in pairs if key == "submCnt"),
                "",
            )),
        ] and (
            _JAMS_ARTICLE_ID_RE.fullmatch(pairs[1][1]) is not None
            and _SUBMISSION_COUNT_RE.fullmatch(pairs[3][1]) is not None
        )
    if kind == "download":
        return (
            parsed.path == KDFS_DOWNLOAD_PATH
            and len(pairs) == 1
            and pairs[0][0] == "orteFileId"
            and _FILE_ID_RE.fullmatch(pairs[0][1]) is not None
        )
    return False


def _is_html(fetch: HttpFetch) -> bool:
    if fetch.content_type in {"text/html", "application/xhtml+xml"}:
        return True
    prefix = fetch.body[:1024].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html"))


def _decode_html(fetch: HttpFetch) -> str | None:
    content_type = fetch.headers.get("Content-Type", "")
    match = re.search(
        r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"'\s]+)",
        content_type,
        flags=re.IGNORECASE,
    )
    encodings = (
        match.group(1) if match is not None else None,
        "utf-8",
        "cp949",
        "euc-kr",
    )
    for encoding in encodings:
        if encoding is None:
            continue
        try:
            return fetch.body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return None


def _attributes(
    attrs: list[tuple[str, str | None]],
) -> dict[str, str]:
    values: dict[str, str] = {}
    duplicates: set[str] = set()
    for name, value in attrs:
        key = name.casefold()
        if key in values:
            duplicates.add(key)
        else:
            values[key] = value or ""
    for key in duplicates:
        values[key] = ""
    return values


def _plain_text(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", html.unescape(value)).split()
    )


def _identity_token(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKC",
        html.unescape(value),
    ).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _author_name(value: str) -> str | None:
    normalized = _plain_text(value)
    name = re.split(r"\s*[(（\[]", normalized, maxsplit=1)[0].strip()
    return name if len(_identity_token(name)) >= 2 else None


def _strip_detail_label(value: str) -> str:
    return re.sub(
        r"\A\s*제목\s*(?:[(（]\s*원어\s*[)）])?\s*:?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()


def _year_present(text: str, year: int) -> bool:
    return (
        re.search(
            rf"(?<![0-9]){re.escape(str(year))}(?![0-9])",
            unicodedata.normalize("NFKC", text),
        )
        is not None
    )


def _volume_issue_present(text: str, volume: int, issue: int) -> bool:
    return any(
        int(match.group("volume")) == volume
        and int(match.group("issue")) == issue
        for match in _VOLUME_ISSUE_RE.finditer(
            unicodedata.normalize("NFKC", text)
        )
    )


def _page_range_present(text: str, first: int, final: int) -> bool:
    return any(
        int(match.group("first")) == first
        and int(match.group("final")) == final
        for match in _PAGE_RANGE_RE.finditer(
            unicodedata.normalize("NFKC", text)
        )
    )


def _page_number_present(text: str, page: int) -> bool:
    return (
        re.search(
            rf"(?<![0-9])0*{re.escape(str(page))}(?![0-9])",
            unicodedata.normalize("NFKC", text),
        )
        is not None
    )


def _safe_headers(
    headers: Mapping[str, str] | None,
) -> Mapping[str, str]:
    return {
        key: value
        for key, value in (headers or {}).items()
        if key.casefold() not in _SENSITIVE_REQUEST_HEADERS
    }


def _without_cookie_headers(fetch: HttpFetch) -> HttpFetch:
    return HttpFetch(
        requested_url=fetch.requested_url,
        final_url=fetch.final_url,
        status_code=fetch.status_code,
        headers={
            key: value
            for key, value in fetch.headers.items()
            if key.casefold() not in _COOKIE_RESPONSE_HEADERS
        },
        body=fetch.body,
        redirects=fetch.redirects,
    )
