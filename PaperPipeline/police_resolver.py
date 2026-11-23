"""Conservative resolver for the Police University public research board.

The official Police University board exposes individual ``범죄수사학연구``
articles through an anonymous search form.  Each result row contains an
explicit attachment control followed by four hidden form values.  The public
UI submits exactly those values to ``/police/common/fileDown.do``.

This resolver deliberately mirrors that read-only UI workflow:

* it checks the official ``robots.txt`` before touching the board;
* it discovers the search form from the fixed HTTPS board page;
* it searches by an exact DB title and accepts only same-row attachment
  values (``fnm``, ``orgFnm``, ``path``, and ``articleSeq``);
* it never invents a file id, article sequence, path, or pagination value;
* it uses anonymous, bounded, cookie-less, redirect-free requests; and
* it returns a PDF only after the filename and PDF contents establish the
  title, journal, year, volume/issue, authors, and page range.

The returned bytes are intended for the repository owner's internal research
workflow.  Public download availability does not grant redistribution rights,
and this module intentionally makes no licence or redistribution claim.
"""

from __future__ import annotations

import html
import io
import math
import re
import threading
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

from PaperPipeline.http_client import (
    HttpClientError,
    HttpFetch,
    PerHostRateLimiter,
    SafeHttpClient,
)
from PaperPipeline.identifier_resolver import paper_title_queries
from PaperPipeline.records import PaperCandidate, ResolverCandidate


POLICE_ORIGIN = "https://www.police.ac.kr"
POLICE_HOST = "www.police.ac.kr"
POLICE_BOARD_PATH = "/police/police/master/208/boardList.do"
POLICE_DOWNLOAD_PATH = "/police/common/fileDown.do"
POLICE_BOARD_URL = (
    f"{POLICE_ORIGIN}{POLICE_BOARD_PATH}?mdex=police175"
)
POLICE_DOWNLOAD_URL = f"{POLICE_ORIGIN}{POLICE_DOWNLOAD_PATH}"
POLICE_ROBOTS_URL = f"{POLICE_ORIGIN}/robots.txt"
POLICE_RESOLVER_NAME = "police-university-public-board"
POLICE_USER_AGENT_TOKEN = "Forensics-Paper-Police-Resolver"
POLICE_INTERNAL_USE_NOTICE = (
    "Internal research copy; public availability does not authorize "
    "redistribution."
)

POLICE_INSTITUTION = "범죄수사연구원"
POLICE_JOURNAL = "범죄수사학연구"
_JOURNAL_ALIASES = frozenset({POLICE_JOURNAL, "SER000005144"})

# Read-only audit snapshot.  These constants document expected coverage but
# never bypass live metadata or PDF identity validation.
POLICE_AUDITED_RESOLVABLE_SOURCE_IDS = frozenset(
    {
        "ART003198151",
        "ART003237353",
        "ART003237379",
        "ART003237411",
        "ART003286669",
        "ART003286695",
        "ART003286806",
    }
)
POLICE_AUDITED_ABSENT_SOURCE_IDS = frozenset(
    {
        "ART003330239",
        "ART003330395",
        "ART003330400",
    }
)

_ISSUE_ID_RE = re.compile(
    r"\A0*(?P<volume>[1-9][0-9]*)_0*(?P<issue>[1-9][0-9]*)\Z"
)
_VOLUME_ISSUE_RE = re.compile(
    r"제\s*0*(?P<volume>[1-9][0-9]*)\s*권"
    r"\s*제\s*0*(?P<issue>[1-9][0-9]*)\s*호"
)
_SUBTITLE_DELIMITER_RE = re.compile(r"\s+(?:-|–|—|:)\s+")
_STORED_PDF_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,254}[.]pdf\Z",
    re.IGNORECASE,
)
_DECIMAL_RE = re.compile(r"\A[1-9][0-9]{0,19}\Z")
_CHARSET_RE = re.compile(
    r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"'\s]+)",
    re.IGNORECASE,
)
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_COOKIE_HEADERS = frozenset({"set-cookie", "set-cookie2"})
_SENSITIVE_REQUEST_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)
_FORM_FIELDS = ("fnm", "orgFnm", "path", "articleSeq")


class PoliceHttp(Protocol):
    """Bounded anonymous HTTP seam implemented by :class:`SafeHttpClient`."""

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Fetch one public resource."""

    def post_form_bytes(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpFetch:
        """Submit one explicit public form without following redirects."""


@dataclass(frozen=True, slots=True)
class PoliceResolverConfig:
    """Network and PDF-inspection bounds for the official board workflow."""

    requests_per_second: float = 0.5
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    max_robots_bytes: int = 64 * 1024
    max_board_response_bytes: int = 2 * 1024 * 1024
    max_search_response_bytes: int = 2 * 1024 * 1024
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
class PoliceResolverAttempt:
    """One auditable request or cache operation; form values are not logged."""

    stage: str
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PoliceResolution:
    """One conservative official-board decision and, if resolved, its PDF."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = POLICE_RESOLVER_NAME
    candidate: ResolverCandidate | None = None
    fetch: HttpFetch | None = field(default=None, repr=False)
    matched_title: str | None = None
    matched_authors: tuple[str, ...] = ()
    attachment_filename: str | None = None
    article_seq: str | None = None
    expected_year: int | None = None
    expected_volume: int | None = None
    expected_issue: int | None = None
    expected_first_page: int | None = None
    expected_final_page: int | None = None
    attempts: tuple[PoliceResolverAttempt, ...] = ()
    http_status: int | None = None
    error_code: str | None = None
    usage_notice: str = POLICE_INTERNAL_USE_NOTICE

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
    year: int
    volume: int
    issue: int
    first_page: int
    final_page: int


@dataclass(frozen=True, slots=True)
class _SearchForm:
    action: str
    search_key: str
    type_value: str
    page_index: str

    def data_for(self, title: str) -> Mapping[str, str]:
        # Empty ``type`` and ``pageIndex`` are the values explicitly present
        # in the official form.  The server treats the empty page as its first
        # result page, so no pagination id is synthesized.
        return {
            "searchKey": self.search_key,
            "searchKeyword": title,
            "type": self.type_value,
            "pageIndex": self.page_index,
        }


@dataclass(frozen=True, slots=True)
class _ResultRow:
    text: str
    attachment_control: bool
    values: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _Attachment:
    fnm: str
    org_fnm: str
    path: str
    article_seq: str
    matched_title: str

    @property
    def post_data(self) -> Mapping[str, str]:
        return {
            "fnm": self.fnm,
            # The official JavaScript applies encodeURIComponent before the
            # normal HTML form encoder runs.  Match that public UI exactly.
            "orgFnm": quote(self.org_fnm, safe="-_.!~*'()"),
            "path": self.path,
            "articleSeq": self.article_seq,
        }


@dataclass(frozen=True, slots=True)
class _PdfDocument:
    page_count: int
    first_pages: tuple[str, ...]
    last_page: str


@dataclass(frozen=True, slots=True)
class _Failure:
    status: str
    reason: str
    error_code: str
    http_status: int | None = None


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
        return any(rule.allow for rule in strongest)


class PoliceUniversityPublicPdfResolver:
    """Resolve exact public Police University attachments without guessing."""

    def __init__(
        self,
        config: PoliceResolverConfig | None = None,
        *,
        http: PoliceHttp | None = None,
    ) -> None:
        self.config = config or PoliceResolverConfig()
        _validate_config(self.config)

        if self.config.request_gate is not None:
            request_gate = self.config.request_gate
        elif http is None:
            request_gate = PerHostRateLimiter(
                self.config.requests_per_second
            ).wait
        else:
            request_gate = None

        self.http: PoliceHttp = http or _StatelessPoliceHttp(
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
            request_gate=request_gate,
        )
        # Search and download POSTs are intentionally serialized.  This also
        # protects the shared robots/form cache from worker-pool bursts.
        self._lock = threading.RLock()
        self._robots_policy: _RobotsPolicy | None = None
        self._search_form: _SearchForm | None = None

    def resolve(self, paper: PaperCandidate) -> PoliceResolution:
        """Return an already-fetched and content-validated official PDF."""

        with self._lock:
            return self._resolve_locked(paper)

    def _resolve_locked(self, paper: PaperCandidate) -> PoliceResolution:
        expected, eligibility = _expected_identity(paper)
        if eligibility is not None:
            return PoliceResolution(paper=paper, **eligibility)
        assert expected is not None

        identity_fields = {
            "expected_year": expected.year,
            "expected_volume": expected.volume,
            "expected_issue": expected.issue,
            "expected_first_page": expected.first_page,
            "expected_final_page": expected.final_page,
        }
        attempts: list[PoliceResolverAttempt] = []

        policy, failure = self._official_robots_policy(attempts)
        if failure is not None:
            return self._failure_resolution(
                paper,
                failure,
                attempts,
                **identity_fields,
            )
        assert policy is not None
        if not policy.allows(POLICE_BOARD_URL):
            return self._failure_resolution(
                paper,
                _Failure(
                    status="robots_disallowed",
                    reason="police_board_disallowed_by_robots",
                    error_code="robots_disallowed",
                ),
                attempts,
                **identity_fields,
            )
        if not policy.allows(POLICE_DOWNLOAD_URL):
            return self._failure_resolution(
                paper,
                _Failure(
                    status="robots_disallowed",
                    reason="police_download_disallowed_by_robots",
                    error_code="robots_disallowed",
                ),
                attempts,
                **identity_fields,
            )

        form, failure = self._official_search_form(attempts)
        if failure is not None:
            return self._failure_resolution(
                paper,
                failure,
                attempts,
                **identity_fields,
            )
        assert form is not None

        attachment: _Attachment | None = None
        for title in _search_title_queries(
            expected.titles,
            max_queries=self.config.max_title_queries,
        ):
            search_fetch, failure = self._post(
                stage="police-title-search",
                url=form.action,
                data=form.data_for(title),
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
            assert search_fetch is not None

            parser = _ResultRowsParser()
            document = _decode_html(search_fetch)
            if document is None:
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="invalid_response",
                        reason="invalid_police_search_html",
                        error_code="invalid_html",
                        http_status=search_fetch.status_code,
                    ),
                    attempts,
                    **identity_fields,
                )
            try:
                parser.feed(document)
                parser.close()
            except (AssertionError, ValueError):
                return self._failure_resolution(
                    paper,
                    _Failure(
                        status="invalid_response",
                        reason="invalid_police_search_html",
                        error_code="invalid_html",
                        http_status=search_fetch.status_code,
                    ),
                    attempts,
                    **identity_fields,
                )

            attachment, selection_failure = _select_attachment(
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
            if attachment is not None:
                break

        if attachment is None:
            return PoliceResolution(
                paper=paper,
                status="not_found",
                reason="exact_police_article_not_found",
                attempts=tuple(attempts),
                error_code="not_found",
                **identity_fields,
            )

        pdf_fetch, failure = self._post(
            stage="police-pdf-download",
            url=POLICE_DOWNLOAD_URL,
            data=attachment.post_data,
            max_bytes=self.config.max_pdf_bytes,
            accept="application/pdf,application/octet-stream;q=0.9",
            require_html=False,
            attempts=attempts,
        )
        if failure is not None:
            return self._failure_resolution(
                paper,
                failure,
                attempts,
                attachment_filename=attachment.org_fnm,
                article_seq=attachment.article_seq,
                **identity_fields,
            )
        assert pdf_fetch is not None

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
                    reason="police_attachment_is_not_a_safe_parseable_pdf",
                    error_code="invalid_pdf",
                    http_status=pdf_fetch.status_code,
                ),
                attempts,
                attachment_filename=attachment.org_fnm,
                article_seq=attachment.article_seq,
                **identity_fields,
            )

        identity_failure = _pdf_identity_failure(pdf, expected)
        if identity_failure is not None:
            return self._failure_resolution(
                paper,
                identity_failure,
                attempts,
                attachment_filename=attachment.org_fnm,
                article_seq=attachment.article_seq,
                **identity_fields,
            )

        sanitized_fetch = _without_cookie_headers(pdf_fetch)
        return PoliceResolution(
            paper=paper,
            status="resolved",
            reason="official_police_board_exact_pdf_identity",
            candidate=ResolverCandidate(
                url=POLICE_DOWNLOAD_URL,
                resolver=POLICE_RESOLVER_NAME,
                landing_url=POLICE_BOARD_URL,
                is_open_access=True,
            ),
            fetch=sanitized_fetch,
            matched_title=attachment.matched_title,
            matched_authors=expected.authors,
            attachment_filename=attachment.org_fnm,
            article_seq=attachment.article_seq,
            attempts=tuple(attempts),
            **identity_fields,
        )

    def _official_robots_policy(
        self,
        attempts: list[PoliceResolverAttempt],
    ) -> tuple[_RobotsPolicy | None, _Failure | None]:
        if self._robots_policy is not None:
            attempts.append(
                PoliceResolverAttempt(
                    stage="police-robots",
                    url=POLICE_ROBOTS_URL,
                    status="cached",
                )
            )
            return self._robots_policy, None

        fetch, failure = self._get(
            stage="police-robots",
            url=POLICE_ROBOTS_URL,
            max_bytes=self.config.max_robots_bytes,
            accept="text/plain",
            require_html=False,
            attempts=attempts,
        )
        if failure is not None:
            return None, _Failure(
                status=failure.status,
                reason="police_robots_unavailable",
                error_code=failure.error_code,
                http_status=failure.http_status,
            )
        assert fetch is not None
        text = _decode_robots(fetch.body)
        if text is None:
            return None, _Failure(
                status="invalid_response",
                reason="invalid_police_robots",
                error_code="invalid_robots",
                http_status=fetch.status_code,
            )
        try:
            policy = _parse_robots(text, POLICE_USER_AGENT_TOKEN)
        except ValueError:
            return None, _Failure(
                status="invalid_response",
                reason="invalid_police_robots",
                error_code="invalid_robots",
                http_status=fetch.status_code,
            )
        self._robots_policy = policy
        return policy, None

    def _official_search_form(
        self,
        attempts: list[PoliceResolverAttempt],
    ) -> tuple[_SearchForm | None, _Failure | None]:
        if self._search_form is not None:
            attempts.append(
                PoliceResolverAttempt(
                    stage="police-board-form",
                    url=POLICE_BOARD_URL,
                    status="cached",
                )
            )
            return self._search_form, None

        fetch, failure = self._get(
            stage="police-board-form",
            url=POLICE_BOARD_URL,
            max_bytes=self.config.max_board_response_bytes,
            accept="text/html,application/xhtml+xml;q=0.9",
            require_html=True,
            attempts=attempts,
        )
        if failure is not None:
            return None, failure
        assert fetch is not None
        document = _decode_html(fetch)
        if document is None:
            return None, _Failure(
                status="invalid_response",
                reason="invalid_police_board_html",
                error_code="invalid_html",
                http_status=fetch.status_code,
            )
        parser = _SearchFormParser()
        try:
            parser.feed(document)
            parser.close()
        except (AssertionError, ValueError):
            return None, _Failure(
                status="invalid_response",
                reason="invalid_police_board_html",
                error_code="invalid_html",
                http_status=fetch.status_code,
            )
        form = _select_search_form(parser.forms)
        if form is None:
            return None, _Failure(
                status="invalid_response",
                reason="exact_police_search_form_not_found",
                error_code="invalid_html",
                http_status=fetch.status_code,
            )
        self._search_form = form
        return form, None

    def _get(
        self,
        *,
        stage: str,
        url: str,
        max_bytes: int,
        accept: str,
        require_html: bool,
        attempts: list[PoliceResolverAttempt],
    ) -> tuple[HttpFetch | None, _Failure | None]:
        try:
            fetch = self.http.get_bytes(
                url,
                max_bytes=max_bytes,
                headers={"Accept": accept},
            )
        except HttpClientError as exc:
            return None, _http_exception_failure(
                stage,
                url,
                exc,
                attempts,
            )
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
        attempts: list[PoliceResolverAttempt],
    ) -> tuple[HttpFetch | None, _Failure | None]:
        try:
            fetch = self.http.post_form_bytes(
                url,
                data=data,
                max_bytes=max_bytes,
                headers={"Accept": accept},
            )
        except HttpClientError as exc:
            return None, _http_exception_failure(
                stage,
                url,
                exc,
                attempts,
            )
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
        attempts: list[PoliceResolverAttempt],
        **fields: Any,
    ) -> PoliceResolution:
        return PoliceResolution(
            paper=paper,
            status=failure.status,
            reason=failure.reason,
            error_code=failure.error_code,
            http_status=failure.http_status,
            attempts=tuple(attempts),
            **fields,
        )


@dataclass
class _FormBuilder:
    action: str
    method: str
    select_name: str | None = None
    search_key_options: list[str] = field(default_factory=list)
    inputs: list[tuple[str, str, str]] = field(default_factory=list)


class _SearchFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_FormBuilder] = []
        self._active: _FormBuilder | None = None
        self._form_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = _attributes(attrs)
        if tag.casefold() == "form":
            if self._active is not None:
                self._form_depth += 1
                return
            identity = {
                attributes.get("id", "").casefold(),
                attributes.get("name", "").casefold(),
            }
            if "searchform" in identity:
                self._active = _FormBuilder(
                    action=attributes.get("action", ""),
                    method=attributes.get("method", "get"),
                )
                self._form_depth = 1
            return
        if self._active is None:
            return
        if tag.casefold() == "select":
            self._active.select_name = attributes.get("name")
        elif tag.casefold() == "option":
            if self._active.select_name == "searchKey":
                value = attributes.get("value")
                if value is not None:
                    self._active.search_key_options.append(value)
        elif tag.casefold() == "input":
            name = attributes.get("name")
            if name is not None:
                self._active.inputs.append(
                    (
                        name,
                        attributes.get("type", "text").casefold(),
                        attributes.get("value", ""),
                    )
                )

    def handle_endtag(self, tag: str) -> None:
        if self._active is None:
            return
        folded = tag.casefold()
        if folded == "select":
            self._active.select_name = None
        elif folded == "form":
            self._form_depth -= 1
            if self._form_depth == 0:
                self.forms.append(self._active)
                self._active = None


@dataclass
class _RowBuilder:
    text: list[str] = field(default_factory=list)
    attachment_control: bool = False
    values: dict[str, list[str]] = field(
        default_factory=lambda: {name: [] for name in _FORM_FIELDS}
    )
    anchor_depth: int = 0
    anchor_text: list[str] = field(default_factory=list)


class _ResultRowsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_ResultRow] = []
        self._row: _RowBuilder | None = None
        self._row_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        folded = tag.casefold()
        attributes = _attributes(attrs)
        if folded == "tr":
            if self._row is None:
                self._row = _RowBuilder()
                self._row_depth = 1
            else:
                self._row_depth += 1
            return
        if self._row is None:
            return
        if folded == "a":
            self._row.anchor_depth += 1
            if self._row.anchor_depth == 1:
                self._row.anchor_text = []
        elif folded == "img" and self._row.anchor_depth:
            label = " ".join(
                (
                    attributes.get("alt", ""),
                    attributes.get("title", ""),
                )
            )
            if "첨부파일" in _plain_text(label):
                self._row.attachment_control = True
        elif folded == "input":
            input_type = attributes.get("type", "text").casefold()
            if input_type != "hidden":
                return
            classes = frozenset(
                token
                for token in attributes.get("class", "").split()
                if token
            )
            for name in _FORM_FIELDS:
                if name in classes and "value" in attributes:
                    self._row.values[name].append(attributes["value"])

    def handle_data(self, data: str) -> None:
        if self._row is None:
            return
        self._row.text.append(data)
        if self._row.anchor_depth:
            self._row.anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return
        folded = tag.casefold()
        if folded == "a" and self._row.anchor_depth:
            if (
                self._row.anchor_depth == 1
                and "첨부" in _plain_text("".join(self._row.anchor_text))
            ):
                self._row.attachment_control = True
            self._row.anchor_depth -= 1
        elif folded == "tr":
            self._row_depth -= 1
            if self._row_depth == 0:
                self.rows.append(
                    _ResultRow(
                        text=_plain_text(" ".join(self._row.text)),
                        attachment_control=self._row.attachment_control,
                        values={
                            name: tuple(values)
                            for name, values in self._row.values.items()
                        },
                    )
                )
                self._row = None


class _StatelessPoliceHttp:
    """Fresh ``SafeHttpClient`` per request so cookies cannot carry over."""

    def __init__(
        self,
        *,
        connect_timeout: float,
        read_timeout: float,
        request_gate: Callable[[str], None] | None,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.request_gate = request_gate

    def _client(self) -> SafeHttpClient:
        return SafeHttpClient(
            user_agent=(
                f"{POLICE_USER_AGENT_TOKEN}/1.0 "
                "(internal academic research; no redistribution)"
            ),
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            max_redirects=0,
            retries=0,
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


def _validate_config(config: PoliceResolverConfig) -> None:
    positive_ints = (
        "max_robots_bytes",
        "max_board_response_bytes",
        "max_search_response_bytes",
        "max_pdf_bytes",
        "max_pdf_pages",
        "max_pdf_text_chars",
        "max_title_queries",
    )
    for name in positive_ints:
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    positive_numbers = (
        "requests_per_second",
        "connect_timeout",
        "read_timeout",
    )
    for name in positive_numbers:
        value = getattr(config, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be positive and finite")


def _expected_identity(
    paper: PaperCandidate,
) -> tuple[_ExpectedIdentity | None, Mapping[str, Any] | None]:
    if not isinstance(paper.source, str) or paper.source.casefold() != "kci":
        return None, {
            "status": "unsupported",
            "reason": "source_not_kci",
            "error_code": "unsupported_source",
        }
    if paper.institution_id != POLICE_INSTITUTION:
        return None, {
            "status": "unsupported",
            "reason": "institution_not_police_research_institute",
            "error_code": "unsupported_institution",
        }
    if paper.journal_id not in _JOURNAL_ALIASES:
        return None, {
            "status": "unsupported",
            "reason": "journal_not_police_criminal_investigation_studies",
            "error_code": "unsupported_journal",
        }

    match = (
        _ISSUE_ID_RE.fullmatch(paper.issue_id.strip())
        if isinstance(paper.issue_id, str)
        else None
    )
    if match is None:
        return None, {
            "status": "missing_metadata",
            "reason": "missing_or_invalid_volume_issue",
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
    first_page = _page_number(paper.first_page)
    final_page = _page_number(paper.final_page)
    if (
        first_page is None
        or final_page is None
        or final_page < first_page
    ):
        return None, {
            "status": "missing_metadata",
            "reason": "missing_or_invalid_page_range",
            "error_code": "missing_metadata",
        }
    titles = paper_title_queries(paper)
    if not titles:
        return None, {
            "status": "missing_metadata",
            "reason": "missing_known_title",
            "error_code": "missing_metadata",
        }
    authors = tuple(
        name
        for author in paper.authors
        if (name := _author_name(author)) is not None
    )
    return (
        _ExpectedIdentity(
            titles=titles,
            authors=authors,
            year=year,
            volume=int(match.group("volume")),
            issue=int(match.group("issue")),
            first_page=first_page,
            final_page=final_page,
        ),
        None,
    )


def _page_number(value: str | None) -> int | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not re.fullmatch(r"[0-9]+", stripped):
        return None
    number = int(stripped)
    return number if number >= 0 else None


def _author_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = re.split(r"[\(（]", value, maxsplit=1)[0].strip()
    return name if len(_identity_token(name)) >= 2 else None


def _select_search_form(
    forms: list[_FormBuilder],
) -> _SearchForm | None:
    matches: list[_SearchForm] = []
    for form in forms:
        try:
            action = urljoin(POLICE_BOARD_URL, html.unescape(form.action).strip())
        except ValueError:
            continue
        if (
            form.method.casefold() != "post"
            or not _is_exact_official_url(action, kind="board")
            or form.search_key_options.count("T") != 1
        ):
            continue
        by_name: dict[str, list[tuple[str, str]]] = {}
        for name, input_type, value in form.inputs:
            by_name.setdefault(name, []).append((input_type, value))
        if (
            len(by_name.get("searchKeyword", ())) != 1
            or by_name["searchKeyword"][0][0] not in {"text", "search"}
            or by_name.get("type") != [("hidden", "")]
            or by_name.get("pageIndex") != [("hidden", "")]
        ):
            continue
        matches.append(
            _SearchForm(
                action=POLICE_BOARD_URL,
                search_key="T",
                type_value="",
                page_index="",
            )
        )
    return matches[0] if len(matches) == 1 else None


def _select_attachment(
    rows: list[_ResultRow],
    expected: _ExpectedIdentity,
) -> tuple[_Attachment | None, _Failure | None]:
    known_titles = {
        _identity_token(title): title
        for title, _normalized in expected.titles
    }
    matches: dict[tuple[str, str, str, str], _Attachment] = {}
    for row in rows:
        org_values = row.values.get("orgFnm", ())
        for org_fnm in org_values:
            filename_match = _filename_match(org_fnm, known_titles)
            if filename_match is None:
                continue
            matched_title, filename_volume_issue = filename_match
            row_volume_issues = _volume_issue_matches(row.text)
            if len(row_volume_issues) > 1:
                return None, _Failure(
                    status="identity_mismatch",
                    reason="police_result_row_volume_issue_ambiguous",
                    error_code="identity_mismatch",
                )
            row_volume_issue = (
                next(iter(row_volume_issues))
                if row_volume_issues
                else None
            )
            supplied_identities = tuple(
                identity
                for identity in (
                    filename_volume_issue,
                    row_volume_issue,
                )
                if identity is not None
            )
            if not supplied_identities:
                continue
            expected_volume_issue = (expected.volume, expected.issue)
            if any(
                identity != expected_volume_issue
                for identity in supplied_identities
            ):
                return None, _Failure(
                    status="identity_mismatch",
                    reason="police_attachment_volume_issue_mismatch",
                    error_code="identity_mismatch",
                )
            if not row.attachment_control:
                return None, _Failure(
                    status="invalid_response",
                    reason="police_attachment_control_missing",
                    error_code="invalid_html",
                )
            if any(len(row.values.get(name, ())) != 1 for name in _FORM_FIELDS):
                return None, _Failure(
                    status="invalid_response",
                    reason="police_attachment_form_values_ambiguous",
                    error_code="invalid_html",
                )
            values = {
                name: row.values[name][0]
                for name in _FORM_FIELDS
            }
            if not _valid_form_values(values):
                return None, _Failure(
                    status="invalid_response",
                    reason="police_attachment_form_values_unsafe",
                    error_code="unsafe_form_value",
                )
            attachment = _Attachment(
                fnm=values["fnm"],
                org_fnm=values["orgFnm"],
                path=values["path"],
                article_seq=values["articleSeq"],
                matched_title=matched_title,
            )
            key = (
                attachment.fnm,
                attachment.org_fnm,
                attachment.path,
                attachment.article_seq,
            )
            matches[key] = attachment
    if not matches:
        return None, None
    if len(matches) != 1:
        return None, _Failure(
            status="identity_mismatch",
            reason="multiple_exact_police_attachments",
            error_code="ambiguous_match",
        )
    return next(iter(matches.values())), None


def _filename_match(
    filename: str,
    known_titles: Mapping[str, str],
) -> tuple[str, tuple[int, int] | None] | None:
    if (
        not isinstance(filename, str)
        or not filename.casefold().endswith(".pdf")
        or len(filename) > 512
        or _contains_unsafe_text(filename)
        or "/" in filename
        or "\\" in filename
    ):
        return None
    stem = filename[:-4]
    issue_matches = _volume_issue_matches(stem)
    if len(issue_matches) > 1:
        return None
    stem_token = _identity_token(stem)
    title_matches = [
        title
        for token, title in known_titles.items()
        if token and stem_token.endswith(token)
    ]
    if len(title_matches) != 1:
        return None
    volume_issue = next(iter(issue_matches)) if issue_matches else None
    return title_matches[0], volume_issue


def _search_title_queries(
    titles: tuple[tuple[str, str], ...],
    *,
    max_queries: int,
) -> tuple[str, ...]:
    """Return exact titles plus conservative main-title search fallbacks.

    Some official board rows omit a subtitle from their searchable title even
    though the attachment and PDF contain the complete source title.  A
    fallback is therefore generated only for an explicit, whitespace-delimited
    subtitle marker.  It broadens discovery only: attachment and PDF identity
    checks continue to use the complete known titles.
    """

    queries: list[str] = []
    seen: set[str] = set()
    for title, _normalized_title in titles:
        candidates = [title]
        normalized = unicodedata.normalize("NFKC", html.unescape(title))
        delimiter = _SUBTITLE_DELIMITER_RE.search(normalized)
        if delimiter is not None:
            main_title = normalized[: delimiter.start()].strip()
            if len(_identity_token(main_title)) >= 12:
                candidates.append(main_title)
        for candidate in candidates:
            token = _identity_token(candidate)
            if not token or token in seen:
                continue
            seen.add(token)
            queries.append(candidate)
            if len(queries) >= max_queries:
                return tuple(queries)
    return tuple(queries)


def _volume_issue_matches(value: str) -> set[tuple[int, int]]:
    return {
        (int(match.group("volume")), int(match.group("issue")))
        for match in _VOLUME_ISSUE_RE.finditer(
            unicodedata.normalize("NFKC", value)
        )
    }


def _valid_form_values(values: Mapping[str, str]) -> bool:
    if any(
        not isinstance(value, str)
        or not value
        or _contains_unsafe_text(value)
        for value in values.values()
    ):
        return False
    if _STORED_PDF_RE.fullmatch(values["fnm"]) is None:
        return False
    if (
        len(values["orgFnm"]) > 512
        or not values["orgFnm"].casefold().endswith(".pdf")
        or "/" in values["orgFnm"]
        or "\\" in values["orgFnm"]
    ):
        return False
    if values["path"] != "/pds/":
        return False
    return _DECIMAL_RE.fullmatch(values["articleSeq"]) is not None


def _read_pdf_document(
    body: bytes,
    *,
    max_pages: int,
    max_text_chars: int,
) -> _PdfDocument:
    if body.find(b"%PDF-", 0, 1024) < 0:
        raise ValueError("response has no PDF header")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise ValueError("pypdf is required") from exc
    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
        if reader.is_encrypted:
            raise ValueError("encrypted PDFs are not accepted")
        page_count = len(reader.pages)
        if page_count < 1 or page_count > max_pages:
            raise ValueError("PDF page-count limit violated")
        selected_indices = list(range(min(3, page_count)))
        if page_count - 1 not in selected_indices:
            selected_indices.append(page_count - 1)
        extracted: dict[int, str] = {}
        total_chars = 0
        for index in selected_indices:
            text = reader.pages[index].extract_text() or ""
            total_chars += len(text)
            if total_chars > max_text_chars:
                raise ValueError("PDF extracted-text limit exceeded")
            extracted[index] = text
    except ValueError:
        raise
    except (PdfReadError, OSError, TypeError, KeyError) as exc:
        raise ValueError("PDF parser rejected the response") from exc
    except Exception as exc:
        raise ValueError("unable to extract PDF identity") from exc
    return _PdfDocument(
        page_count=page_count,
        first_pages=tuple(
            extracted[index] for index in range(min(3, page_count))
        ),
        last_page=extracted[page_count - 1],
    )


def _pdf_identity_failure(
    document: _PdfDocument,
    expected: _ExpectedIdentity,
) -> _Failure | None:
    expected_page_count = expected.final_page - expected.first_page + 1
    if document.page_count != expected_page_count:
        return _identity_failure("police_pdf_page_count_mismatch")

    first_text = "\n".join(document.first_pages)
    first_token = _identity_token(first_text)
    matched_titles = [
        title
        for title, _normalized in expected.titles
        if (token := _identity_token(title)) and token in first_token
    ]
    if not matched_titles:
        return _identity_failure("police_pdf_title_mismatch")
    if _identity_token(POLICE_JOURNAL) not in first_token:
        return _identity_failure("police_pdf_journal_mismatch")
    if not _year_present(first_text, expected.year):
        return _identity_failure("police_pdf_year_mismatch")
    if not _volume_issue_present(
        first_text,
        expected.volume,
        expected.issue,
    ):
        return _identity_failure("police_pdf_volume_issue_mismatch")
    for author in expected.authors:
        if _identity_token(author) not in first_token:
            return _identity_failure("police_pdf_author_mismatch")
    if not _page_number_present(
        document.first_pages[0],
        expected.first_page,
    ):
        return _identity_failure("police_pdf_first_page_mismatch")
    if not _page_number_present(document.last_page, expected.final_page):
        return _identity_failure("police_pdf_final_page_mismatch")
    return None


def _identity_failure(reason: str) -> _Failure:
    return _Failure(
        status="identity_mismatch",
        reason=reason,
        error_code="identity_mismatch",
    )


def _year_present(text: str, year: int) -> bool:
    return (
        re.search(
            rf"(?<![0-9]){re.escape(str(year))}(?![0-9])",
            unicodedata.normalize("NFKC", text),
        )
        is not None
    )


def _volume_issue_present(text: str, volume: int, issue: int) -> bool:
    normalized = unicodedata.normalize("NFKC", text)
    korean = re.compile(
        rf"제?\s*0*{volume}\s*권"
        rf".{{0,40}}제?\s*0*{issue}\s*호",
        re.IGNORECASE | re.DOTALL,
    )
    english = re.compile(
        rf"\bvol(?:ume)?[.]?\s*0*{volume}\b"
        rf".{{0,40}}\bno[.]?\s*0*{issue}\b",
        re.IGNORECASE | re.DOTALL,
    )
    return korean.search(normalized) is not None or english.search(normalized) is not None


def _page_number_present(text: str, page: int) -> bool:
    return (
        re.search(
            rf"(?<![0-9])0*{re.escape(str(page))}(?![0-9])",
            unicodedata.normalize("NFKC", text),
        )
        is not None
    )


def _validate_fetch(
    *,
    stage: str,
    url: str,
    fetch: HttpFetch,
    require_html: bool,
    attempts: list[PoliceResolverAttempt],
) -> tuple[HttpFetch | None, _Failure | None]:
    kind = (
        "robots"
        if url == POLICE_ROBOTS_URL
        else "board"
        if url == POLICE_BOARD_URL
        else "download"
    )
    if (
        fetch.requested_url != url
        or fetch.final_url != url
        or fetch.redirects
        or not _is_exact_official_url(url, kind=kind)
    ):
        attempts.append(
            PoliceResolverAttempt(
                stage=stage,
                url=url,
                status="failed",
                http_status=fetch.status_code,
                final_url=fetch.final_url,
                error_code="unsafe_redirect",
            )
        )
        return None, _Failure(
            status="invalid_response",
            reason="police_response_url_or_redirect_mismatch",
            error_code="unsafe_redirect",
            http_status=fetch.status_code,
        )

    status = int(fetch.status_code)
    if status in _RETRYABLE_STATUSES:
        failure_status = "retryable_error"
        reason = "retryable_police_http_status"
        error_code = "retryable_error"
    elif status in {401, 403}:
        failure_status = "access_denied"
        reason = "police_http_access_denied"
        error_code = "access_denied"
    elif status == 404:
        failure_status = "not_found"
        reason = "police_http_not_found"
        error_code = "not_found"
    elif not 200 <= status < 300:
        failure_status = "invalid_response"
        reason = "unexpected_police_http_status"
        error_code = "http_error"
    elif require_html and not _is_html(fetch):
        failure_status = "invalid_response"
        reason = "police_response_is_not_html"
        error_code = "invalid_html"
    else:
        attempts.append(
            PoliceResolverAttempt(
                stage=stage,
                url=url,
                status="succeeded",
                http_status=status,
                final_url=fetch.final_url,
            )
        )
        return fetch, None

    attempts.append(
        PoliceResolverAttempt(
            stage=stage,
            url=url,
            status="failed",
            http_status=status,
            final_url=fetch.final_url,
            error_code=error_code,
        )
    )
    return None, _Failure(
        status=failure_status,
        reason=reason,
        error_code=error_code,
        http_status=status,
    )


def _http_exception_failure(
    stage: str,
    url: str,
    exc: HttpClientError,
    attempts: list[PoliceResolverAttempt],
) -> _Failure:
    error_code = getattr(exc, "code", "http_error")
    status = "retryable_error" if error_code == "retryable_error" else "invalid_response"
    attempts.append(
        PoliceResolverAttempt(
            stage=stage,
            url=url,
            status="failed",
            error_code=error_code,
        )
    )
    return _Failure(
        status=status,
        reason=f"police_{stage}_http_error",
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
        or (parsed.hostname or "").casefold().rstrip(".") != POLICE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        return False
    if kind == "robots":
        return parsed.path == "/robots.txt" and not parsed.query
    if kind == "download":
        return parsed.path == POLICE_DOWNLOAD_PATH and not parsed.query
    if kind != "board" or parsed.path != POLICE_BOARD_PATH:
        return False
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    return pairs == [("mdex", "police175")]


def _is_html(fetch: HttpFetch) -> bool:
    if fetch.content_type in {"text/html", "application/xhtml+xml"}:
        return True
    prefix = fetch.body[:1024].lstrip().casefold()
    return prefix.startswith((b"<!doctype html", b"<html"))


def _decode_html(fetch: HttpFetch) -> str | None:
    content_type = fetch.headers.get("Content-Type", "")
    match = _CHARSET_RE.search(content_type)
    encodings = [
        match.group(1) if match is not None else None,
        "utf-8",
        "cp949",
        "euc-kr",
    ]
    for encoding in encodings:
        if encoding is None:
            continue
        try:
            return fetch.body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return None


def _decode_robots(body: bytes) -> str | None:
    if b"\x00" in body:
        return None
    for encoding in ("utf-8-sig", "ascii"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _parse_robots(text: str, user_agent: str) -> _RobotsPolicy:
    groups: list[_RobotsGroup] = []
    agents: list[str] = []
    rules: list[_RobotsRule] = []
    saw_rule = False

    def finish_group() -> None:
        nonlocal agents, rules, saw_rule
        if agents:
            groups.append(
                _RobotsGroup(
                    agents=tuple(agents),
                    rules=tuple(rules),
                )
            )
        agents = []
        rules = []
        saw_rule = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            if saw_rule:
                finish_group()
            continue
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.casefold()
        if field == "user-agent":
            if saw_rule:
                finish_group()
            token = value.casefold()
            if token:
                agents.append(token)
            continue
        if field not in {"allow", "disallow"} or not agents:
            continue
        saw_rule = True
        # An empty Disallow is the RFC spelling of "allow everything" and
        # contributes no matching rule.
        if not value:
            continue
        if any(ord(character) < 32 for character in value):
            raise ValueError("invalid robots rule")
        rules.append(_RobotsRule(value, field == "allow"))
    finish_group()

    token = user_agent.casefold()
    specific = [
        group
        for group in groups
        if any(agent != "*" and agent in token for agent in group.agents)
    ]
    selected = specific or [
        group for group in groups if "*" in group.agents
    ]
    return _RobotsPolicy(
        rules=tuple(rule for group in selected for rule in group.rules)
    )


def _robots_rule_matches(pattern: str, target: str) -> bool:
    anchored = pattern.endswith("$")
    source = pattern[:-1] if anchored else pattern
    expression = "".join(
        ".*" if character == "*" else re.escape(character)
        for character in source
    )
    if anchored:
        expression += r"\Z"
    return re.match(expression, target) is not None


def _robots_rule_length(pattern: str) -> int:
    return len(pattern.rstrip("$").replace("*", ""))


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


def _contains_unsafe_text(value: str) -> bool:
    return any(
        character in {"\x00", "\r", "\n"}
        or ord(character) < 32
        or ord(character) == 127
        for character in value
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
    headers = {
        key: value
        for key, value in fetch.headers.items()
        if key.casefold() not in _COOKIE_HEADERS
    }
    return HttpFetch(
        requested_url=fetch.requested_url,
        final_url=fetch.final_url,
        status_code=fetch.status_code,
        headers=headers,
        body=fetch.body,
        redirects=fetch.redirects,
    )
