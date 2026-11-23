"""Opt-in DBpia downloads through a dedicated authenticated browser profile.

Ordinary DBpia credentials are optional and remain in memory only; institution
SSO is completed manually in a headed dedicated Playwright profile.  Cookies,
JWTs, download keys, and signed PDF URLs are never accepted as configuration or
persisted as audit data.  The batch searches with DBpia's rendered homepage UI,
validates one identity-bound article and clicks its official download control.
Authentication challenges and CAPTCHAs stop the browser circuit; no bypass or
direct replay of a transient download URL is attempted.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urljoin, urlsplit

from PaperPipeline.identifier_resolver import normalize_title, paper_title_queries
from PaperPipeline.platform_resolver import (
    DbpiaLandingIdentityError,
    validate_dbpia_landing_document,
)
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import write_json_atomic


DBPIA_BROWSER_RESOLVER_NAME = "dbpia-authenticated-browser"
DBPIA_HOME_URL = "https://www.dbpia.co.kr/"
DEFAULT_PROFILE_MARKER = ".forensics-paper-dbpia-profile.json"
DEFAULT_MAX_HTML_BYTES = 4 * 1024 * 1024
TRANSIENT_CIRCUIT_THRESHOLD = 3
MAX_ONCLICK_ELEMENTS = 2_000
PROFILE_SCHEMA_VERSION = 1
PROFILE_PURPOSE = "forensics-paper-dbpia-browser"
DBPIA_LOGIN_URL = "https://www.dbpia.co.kr/member/login"
_SEARCH_OPTION_SELECTOR = "#searchOption"
_SEARCH_INPUT_SELECTOR = "#searchInput"
_SEARCH_SUBMIT_SELECTOR = "#submitSearchInput"
_SEARCH_CARD_SELECTOR = "#paper-cards-section > div[name=search-article]"
_SEARCH_RESULT_LINK_SELECTOR = ".thesis__subject > a.thesis__pageLink"
_SEARCH_NO_DATA_SELECTOR = "#no-data-section"
_SEARCH_QUERY_MAX_CHARS = 100
_SEARCH_READY_SELECTOR = (
    f"{_SEARCH_CARD_SELECTOR}:visible, {_SEARCH_NO_DATA_SELECTOR}:visible"
)
_HEADER_LOGIN_SELECTOR = "#dpHeader a[href^='/member/login']"
_ORG_AUTH_COMPLETE_MODAL_SELECTOR = "#orgAuthCompleteModal"
_ORG_AUTH_COMPLETE_LOGIN_SELECTOR = "#orgAuthCompleteModal_login"
# DBpia's post-institution-auth dialog offers "바로 이용하기" (continue without
# a personal login).  While it is open, its dimmed backdrop intercepts every
# click on the page, including the official search submit button.
_ORG_AUTH_COMPLETE_LATER_SELECTOR = "#orgAuthCompleteModal_later"
_LOGIN_ID_SELECTOR = "#layer_userID"
_LOGIN_PASSWORD_SELECTOR = "#layer_userPW"
_LOGIN_SUBMIT_SELECTOR = "#dev_pub_alertPop"
_INSTITUTION_SELECTOR = "#dpHeader #span_b2bName, a.user-membership"

_DBPIA_ARTICLE_QUERY_PATH = "/journal/articledetail"
_DBPIA_ARTICLE_PATH_RE = re.compile(
    r"\A/journal/articledetail/(?P<article>NODE[0-9]{5,20}|[1-9][0-9]{0,19})\Z",
    re.IGNORECASE,
)
_NODE_RE = re.compile(r"\ANODE[0-9]{5,20}\Z", re.IGNORECASE)
_DOWNLOAD_BUTTON_RE = re.compile(
    r"""onClickDownloadNode\(\s*(['"])(?P<node>NODE[0-9]{5,20})\1""",
    re.IGNORECASE,
)
_DBPIA_CDN_PATH_RE = re.compile(
    r"\A(?:/[0-9]{1,6}){3}/[0-9]{4,20}[.]pdf\Z",
    re.IGNORECASE,
)
_DOCUMENTED_ARTICLE_CONTEXT_NAMES = frozenset(
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
_CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "로봇이 아닙니다",
    "자동입력 방지",
    "자동 입력 방지",
)
_AUTH_DIALOG_MARKERS = (
    "로그인",
    "login",
    "인증",
    "authentication",
    "세션",
    "session",
    "구독",
    "subscription",
    "권한",
)
_VISIBLE_CAPTCHA_SELECTORS = (
    "#recaptchaModal",
    "#recaptchaAlertModal",
)
_VISIBLE_AUTH_SELECTORS = (
    "#loginLayer",
    "#authenticateOrganizationA",
    "#authenticateOrganizationB",
    "#authenticateOrganizationC",
    "#authenticateOrganizationCE1",
    "#authenticateOrganizationCE2",
    "#b2bAuthModal",
)
_VISIBLE_ACCESS_GATE_SELECTORS = (
    "#multiPurchaseLayer",
    "#previewLayer",
)
_CIRCUIT_CODES = frozenset(
    {
        "authentication_required",
        "captcha_required",
        "browser_dependency_missing",
        "browser_not_installed",
        "browser_launch_failed",
        "invalid_browser_profile",
        "profile_in_use",
    }
)
_IMMEDIATE_CIRCUIT_REASONS = frozenset(
    {
        "dbpia_article_rate_limited",
    }
)
_SEARCH_BIBLIOGRAPHY_CORE_PROVENANCE = frozenset(
    {
        "dbpia_landing_publisher_exact",
        "dbpia_landing_journal_exact",
        "dbpia_landing_page_range_exact",
        "dbpia_landing_authors_exact",
    }
)
_SEARCH_BIBLIOGRAPHY_ISSUE_PROVENANCE = frozenset(
    {
        "dbpia_landing_volume_issue_exact",
        "dbpia_landing_issue_only_exact",
    }
)


@dataclass(frozen=True, slots=True)
class DbpiaArticleReference:
    """A strictly validated DBpia article reference retained only in memory."""

    node_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DbpiaBrowserSearchResult:
    """One title and safe official article URL read from a search card."""

    title: str
    article_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DbpiaBrowserConfig:
    """Bounds and explicit authorization for the browser-assisted backend."""

    permission_granted: bool = False
    profile_dir: Path | None = field(default=None, repr=False, compare=False)
    repository_root: Path | None = field(default=None, repr=False, compare=False)
    login_id: str | None = field(default=None, repr=False, compare=False)
    login_password: str | None = field(default=None, repr=False, compare=False)
    required_institution_name: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    headless: bool = True
    navigation_timeout_seconds: float = 60.0
    download_timeout_seconds: float = 90.0
    min_interval_seconds: float = 10.0
    # Bound actual article-page navigations in one process.  Successful
    # downloads are persisted immediately, so a later invocation safely
    # resumes from the next unresolved paper.
    max_attempts_per_run: int = 25
    max_search_results: int = 50
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES
    max_pdf_bytes: int = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DbpiaBrowserAttempt:
    """A URL- and credential-free audit event."""

    stage: str
    status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DbpiaBrowserResolution:
    """Native browser result; sensitive transport details are not representable."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = DBPIA_BROWSER_RESOLVER_NAME
    content: bytes | None = field(default=None, repr=False)
    content_type: str | None = None
    matched_title: str | None = None
    matched_doi: str | None = None
    identity_provenance: tuple[str, ...] = ()
    attempts: tuple[DbpiaBrowserAttempt, ...] = ()
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.content is not None


@dataclass(frozen=True, slots=True)
class DbpiaBrowserButton:
    """One DOM control that advertises a DBpia download-node callback."""

    dom_index: int = field(repr=False)
    node_ids: tuple[str, ...] = field(repr=False)
    visible: bool
    enabled: bool


@dataclass(frozen=True, slots=True)
class DbpiaPageSnapshot:
    final_url: str = field(repr=False)
    document: str = field(repr=False)
    buttons: tuple[DbpiaBrowserButton, ...] = field(repr=False)
    http_status: int | None = None


class DbpiaBrowserSession(Protocol):
    """Small seam that keeps Playwright optional and unit-testable."""

    def open_article(self, url: str, *, timeout_ms: int) -> DbpiaPageSnapshot:
        """Navigate to one already-known exact article page."""

    def search_articles(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_results: int,
    ) -> tuple[DbpiaBrowserSearchResult, ...]:
        """Submit the official homepage search UI and read result cards."""

    def download_button(
        self,
        button: DbpiaBrowserButton,
        *,
        expected_node_id: str,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        """Click one verified native control and return bounded PDF bytes."""

    def close(self) -> None:
        """Close browser resources on the same thread that opened them."""


class DbpiaBrowserOperationError(RuntimeError):
    """Sanitized browser failure that never embeds a URL or profile path."""

    def __init__(self, status: str, reason: str, error_code: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error_code = error_code


def default_dbpia_profile_dir() -> Path:
    """Return a dedicated profile location outside the repository."""

    configured = os.getenv("XDG_DATA_HOME")
    base = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else Path.home() / ".local" / "share"
    )
    return (base / "forensics-paper" / "dbpia-browser-profile").resolve()


def _configure_user_playwright_libraries() -> None:
    """Expose an optional unprivileged Chromium runtime dependency bundle.

    Minimal Linux research containers may not ship NSS/NSPR/ALSA and may not
    grant sudo.  The bootstrap procedure can extract those distribution
    packages below the same per-user data root as the dedicated profile.  If
    that bundle exists, only the Chromium child process needs to discover it.
    Normal desktop installations are unaffected.
    """

    configured = os.getenv("XDG_DATA_HOME")
    base = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else Path.home() / ".local" / "share"
    )
    library_dir = (
        base
        / "forensics-paper"
        / "playwright-libs"
        / "usr"
        / "lib"
        / "x86_64-linux-gnu"
    ).resolve()
    if not library_dir.is_dir() or library_dir.is_symlink():
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    value = str(library_dir)
    if value not in entries:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join((value, *entries))


def parse_dbpia_article_reference(url: str) -> DbpiaArticleReference:
    """Accept only exact HTTPS DBpia article URLs, never viewer/download URLs."""

    if not isinstance(url, str) or not url or len(url) > 2_048:
        raise ValueError("invalid DBpia article URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise ValueError("invalid DBpia article URL") from exc
    if not (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == "www.dbpia.co.kr"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    ):
        raise ValueError("invalid DBpia article URL")

    path = parsed.path
    if path.casefold() == _DBPIA_ARTICLE_QUERY_PATH:
        if (
            len(pairs) != 1
            or pairs[0][0] != "nodeId"
            or _NODE_RE.fullmatch(pairs[0][1]) is None
        ):
            raise ValueError("invalid DBpia article URL")
        return DbpiaArticleReference(node_id=pairs[0][1].upper())

    match = _DBPIA_ARTICLE_PATH_RE.fullmatch(path)
    if match is None or not _valid_article_context(pairs):
        raise ValueError("invalid DBpia article URL")
    article_id = match.group("article")
    return DbpiaArticleReference(
        node_id=article_id.upper() if _NODE_RE.fullmatch(article_id) else None
    )


def _valid_article_context(pairs: list[tuple[str, str]]) -> bool:
    """Accept only DBpia's documented, non-secret search-page context."""

    if not pairs:
        return True
    names = [name for name, _value in pairs]
    return bool(
        len(pairs) <= len(_DOCUMENTED_ARTICLE_CONTEXT_NAMES)
        and len(set(names)) == len(names)
        and set(names).issubset(_DOCUMENTED_ARTICLE_CONTEXT_NAMES)
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


def is_exact_dbpia_article_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parse_dbpia_article_reference(url)
    except ValueError:
        return False
    return True


def normalize_dbpia_article_url(href: object) -> str | None:
    """Normalize a card href to one canonical, exact DBpia article URL."""

    if not isinstance(href, str):
        return None
    raw = href.strip()
    if not raw or len(raw) > 2_048 or any(ord(char) < 0x20 for char in raw):
        return None
    if raw.startswith("/") and not raw.startswith("//"):
        candidate = urljoin(DBPIA_HOME_URL, raw)
    else:
        candidate = raw
    try:
        reference = parse_dbpia_article_reference(candidate)
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if reference.node_id is not None:
        return (
            "https://www.dbpia.co.kr/journal/articleDetail?nodeId="
            f"{reference.node_id}"
        )
    match = _DBPIA_ARTICLE_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return (
        "https://www.dbpia.co.kr/Journal/ArticleDetail/"
        f"{match.group('article')}"
    )


def _is_ignorable_dbpia_journal_search_href(href: object) -> bool:
    """Accept only the observed official journal-card link for skipping."""

    if not isinstance(href, str):
        return False
    raw = href.strip()
    if not raw or len(raw) > 2_048 or any(ord(char) < 0x20 for char in raw):
        return False
    candidate = (
        urljoin(DBPIA_HOME_URL, raw)
        if raw.startswith("/") and not raw.startswith("//")
        else raw
    )
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == "www.dbpia.co.kr"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.casefold() == "/journal/detail"
        and not parsed.fragment
        and len(pairs) == 1
        and pairs[0][0] == "nodeId"
        and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", pairs[0][1]) is not None
    )


def validate_dbpia_profile_dir(
    profile_dir: Path,
    *,
    repository_root: Path | None,
    require_marker: bool = True,
) -> Path:
    """Validate ownership, mode, marker, and repository separation."""

    raw = Path(profile_dir).expanduser()
    if not raw.is_absolute():
        raise ValueError("DBpia browser profile path must be absolute")
    if raw.is_symlink():
        raise ValueError("DBpia browser profile must not be a symlink")
    resolved = raw.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError("DBpia browser profile path is too broad")
    if repository_root is not None:
        repository = Path(repository_root).resolve()
        if _is_relative_to(resolved, repository):
            raise ValueError("DBpia browser profile must be outside the repository")
    if _looks_like_default_browser_profile(resolved):
        raise ValueError("default personal browser profiles are not supported")
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError("DBpia browser profile has not been initialized")
    if resolved.is_symlink():
        raise ValueError("DBpia browser profile must not be a symlink")
    _require_private_owned_path(resolved, directory=True)

    marker = resolved / DEFAULT_PROFILE_MARKER
    if require_marker:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("DBpia browser profile marker is missing")
        _require_private_owned_path(marker, directory=False)
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("DBpia browser profile marker is invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != PROFILE_SCHEMA_VERSION
            or value.get("purpose") != PROFILE_PURPOSE
        ):
            raise ValueError("DBpia browser profile marker is invalid")
    return resolved


def initialize_dbpia_profile_dir(
    profile_dir: Path,
    *,
    repository_root: Path | None,
) -> Path:
    """Create or reopen an explicitly dedicated, private Playwright profile."""

    raw = Path(profile_dir).expanduser()
    if not raw.is_absolute():
        raise ValueError("DBpia browser profile path must be absolute")
    if raw.is_symlink():
        raise ValueError("DBpia browser profile must not be a symlink")
    resolved = raw.resolve(strict=False)
    if repository_root is not None and _is_relative_to(
        resolved,
        Path(repository_root).resolve(),
    ):
        raise ValueError("DBpia browser profile must be outside the repository")
    if _looks_like_default_browser_profile(resolved):
        raise ValueError("default personal browser profiles are not supported")

    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink():
            raise ValueError("DBpia browser profile path is not a safe directory")
        marker = resolved / DEFAULT_PROFILE_MARKER
        existing = [child for child in resolved.iterdir() if child.name != marker.name]
        if existing and not marker.is_file():
            raise ValueError("refusing to reuse an unmarked browser profile")
    else:
        resolved.mkdir(parents=True, mode=0o700)
    os.chmod(resolved, 0o700)
    _require_private_owned_path(resolved, directory=True)

    marker = resolved / DEFAULT_PROFILE_MARKER
    if marker.is_symlink():
        raise ValueError("DBpia browser profile marker must not be a symlink")
    if not marker.exists():
        write_json_atomic(
            marker,
            {
                "schema_version": PROFILE_SCHEMA_VERSION,
                "purpose": PROFILE_PURPOSE,
            },
        )
        os.chmod(marker, 0o600)
    return validate_dbpia_profile_dir(
        resolved,
        repository_root=repository_root,
        require_marker=True,
    )


def run_interactive_dbpia_login(
    profile_dir: Path,
    *,
    repository_root: Path | None,
    input_fn: Callable[[str], str] = input,
) -> None:
    """Open a headed dedicated profile; all authentication remains manual."""

    validated = initialize_dbpia_profile_dir(
        profile_dir,
        repository_root=repository_root,
    )
    lock = _ProfileLock(validated)
    lock.acquire()
    playwright: Any = None
    context: Any = None
    try:
        _configure_user_playwright_libraries()
        sync_playwright = _load_sync_playwright()
        playwright = sync_playwright().start()
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(validated),
                headless=False,
                accept_downloads=False,
            )
        except Exception as exc:  # Playwright types are optional at import time.
            code = _launch_error_code(exc)
            raise DbpiaBrowserOperationError(
                "configuration_error",
                code,
                code,
            ) from None
        pages = context.pages
        page = pages[0] if pages else context.new_page()
        try:
            page.goto(DBPIA_HOME_URL, wait_until="domcontentloaded")
            input_fn(
                "브라우저에서 DBpia/기관 인증을 직접 완료한 뒤 이 터미널에서 "
                "Enter를 누르세요. 비밀번호는 이 프로그램에 입력하지 않습니다: "
            )
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception:
            raise DbpiaBrowserOperationError(
                "retryable_error",
                "dbpia_interactive_login_failed",
                "browser_error",
            ) from None
    finally:
        if context is not None:
            with contextlib.suppress(Exception):
                context.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                playwright.stop()
        lock.release()


class AuthenticatedDbpiaBrowserResolver:
    """Resolve one exact DBpia article through a dedicated authenticated UI."""

    def __init__(
        self,
        config: DbpiaBrowserConfig,
        *,
        session: DbpiaBrowserSession | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate_config(config)
        self.config = config
        self._session = session
        self._clock = clock
        self._sleep = sleeper
        self._next_allowed = 0.0
        self._lock = threading.Lock()
        self._circuit_error: tuple[str, str, str] | None = None
        self._consecutive_retryable_errors = 0
        self._attempts_started = 0

    def resolve(
        self,
        paper: PaperCandidate,
        *,
        landing_url: str | None = None,
    ) -> DbpiaBrowserResolution:
        if not self.config.permission_granted:
            return self._failure(
                paper,
                "permission_required",
                "dbpia_browser_collection_disabled",
                "permission_required",
            )
        title_queries = paper_title_queries(paper) if landing_url is None else ()
        if landing_url is None and not title_queries:
            return self._failure(
                paper,
                "not_found",
                "missing_dbpia_search_title",
                "not_found",
                stage="browser-precheck",
            )
        if (
            landing_url is None
            and not paper.doi
            and not _doi_less_search_metadata_ready(paper)
        ):
            return self._failure(
                paper,
                "missing_metadata",
                "missing_dbpia_bibliographic_search_metadata",
                "missing_metadata",
                stage="browser-precheck",
            )
        requested_reference: DbpiaArticleReference | None = None
        if landing_url is not None:
            try:
                requested_reference = parse_dbpia_article_reference(landing_url)
            except ValueError:
                return self._failure(
                    paper,
                    "invalid_response",
                    "unsafe_dbpia_article_reference",
                    "unsafe_url",
                )

        with self._lock:
            if self._circuit_error is not None:
                status, reason, error_code = self._circuit_error
                return self._failure(
                    paper,
                    status,
                    reason,
                    error_code,
                    stage="browser-circuit",
                )
            if self._attempts_started >= self.config.max_attempts_per_run:
                return self._failure(
                    paper,
                    "retryable_error",
                    "dbpia_browser_attempt_budget_exhausted",
                    "browser_attempt_limit_reached",
                    stage="browser-budget",
                )
            self._attempts_started += 1
            operation_stage = "dbpia-browser"
            used_search = False
            try:
                session = self._get_session()
                article_url = landing_url
                if article_url is None:
                    operation_stage = "dbpia-browser-search"
                    article_url = self._search_exact_article(
                        session,
                        title_queries,
                    )
                    try:
                        requested_reference = parse_dbpia_article_reference(
                            article_url
                        )
                    except ValueError:
                        raise DbpiaBrowserOperationError(
                            "invalid_response",
                            "unsafe_dbpia_search_article_reference",
                            "unsafe_url",
                        ) from None
                    used_search = True
                assert requested_reference is not None
                self._wait_for_interval()
                operation_stage = "dbpia-browser-landing"
                snapshot = session.open_article(
                    article_url,
                    timeout_ms=_seconds_to_ms(
                        self.config.navigation_timeout_seconds
                    ),
                )
                challenge = _page_challenge(snapshot)
                if challenge is not None:
                    raise challenge
                http_failure = _page_http_failure(snapshot.http_status)
                if http_failure is not None:
                    raise http_failure
                try:
                    final_reference = parse_dbpia_article_reference(
                        snapshot.final_url
                    )
                except ValueError:
                    raise DbpiaBrowserOperationError(
                        "authentication_required",
                        "dbpia_session_left_exact_article_page",
                        "authentication_required",
                    ) from None
                expected_node = _bind_article_references(
                    requested_reference,
                    final_reference,
                    snapshot.buttons,
                )
                identity = validate_dbpia_landing_document(
                    snapshot.document,
                    paper,
                )
                # A web-title result is only a shortlist. Require exact year
                # plus either landing DOI or the complete publisher/journal/
                # volume/issue/pages/authors chain. DOI-less rows can use only
                # that complete bibliography chain.
                if used_search and not identity.year_exact:
                    raise DbpiaBrowserOperationError(
                        "identity_mismatch",
                        "dbpia_search_landing_requires_exact_year",
                        "identity_mismatch",
                    )
                bibliography_exact = _has_complete_search_bibliography(
                    identity.provenance
                )
                if (
                    used_search
                    and paper.doi
                    and not identity.doi_exact
                    and not bibliography_exact
                ):
                    raise DbpiaBrowserOperationError(
                        "identity_mismatch",
                        "dbpia_search_landing_requires_doi_or_bibliography",
                        "identity_mismatch",
                    )
                if used_search and not paper.doi and not bibliography_exact:
                    raise DbpiaBrowserOperationError(
                        "identity_mismatch",
                        "dbpia_search_landing_requires_exact_bibliography",
                        "identity_mismatch",
                    )
                button = _select_download_button(
                    snapshot.buttons,
                    expected_node,
                )
                content = session.download_button(
                    button,
                    expected_node_id=expected_node,
                    timeout_ms=_seconds_to_ms(
                        self.config.download_timeout_seconds
                    ),
                    max_pdf_bytes=self.config.max_pdf_bytes,
                )
            except DbpiaLandingIdentityError as exc:
                self._record_operation_failure(
                    exc.status,
                    exc.reason,
                    exc.error_code or "identity_mismatch",
                )
                return self._failure(
                    paper,
                    exc.status,
                    exc.reason,
                    exc.error_code or "identity_mismatch",
                    stage="dbpia-landing-identity",
                )
            except DbpiaBrowserOperationError as exc:
                self._record_operation_failure(
                    exc.status,
                    exc.reason,
                    exc.error_code,
                )
                return self._failure(
                    paper,
                    exc.status,
                    exc.reason,
                    exc.error_code,
                    stage=operation_stage,
                )
            except Exception:
                self._record_operation_failure(
                    "retryable_error",
                    "dbpia_browser_operation_failed",
                    "browser_error",
                )
                return self._failure(
                    paper,
                    "retryable_error",
                    "dbpia_browser_operation_failed",
                    "browser_error",
                    stage=operation_stage,
                )

            self._consecutive_retryable_errors = 0
            return DbpiaBrowserResolution(
                status="resolved",
                reason="authenticated_official_download_control",
                paper=paper,
                content=content,
                content_type="application/pdf",
                matched_title=identity.title,
                matched_doi=identity.doi,
                identity_provenance=(
                    *((
                        "dbpia_web_title_search_exact",
                        (
                            "dbpia_web_title_search_doi_year_exact"
                            if paper.doi and identity.doi_exact
                            else "dbpia_web_title_search_bibliography_exact"
                        ),
                    ) if used_search else ()),
                    *identity.provenance,
                    "dbpia_landing_identity_exact",
                    "dbpia_download_button_node_exact",
                    "native_browser_download_event",
                ),
                attempts=(
                    *((
                        DbpiaBrowserAttempt("dbpia-browser-search", "ok"),
                    ) if used_search else ()),
                    DbpiaBrowserAttempt("dbpia-browser-landing", "ok"),
                    DbpiaBrowserAttempt("dbpia-browser-download", "downloaded"),
                ),
            )

    def _search_exact_article(
        self,
        session: DbpiaBrowserSession,
        title_queries: tuple[tuple[str, str], ...],
    ) -> str:
        """Return one exact-title official result across all known titles."""

        expected_titles = frozenset(
            normalized for _title, normalized in title_queries
        )
        compact_titles = frozenset(
            compact
            for title, _normalized in title_queries
            if len(compact := _compact_search_title(title)) >= 12
        )
        matches: dict[str, DbpiaBrowserSearchResult] = {}
        for query_title, _normalized in title_queries:
            self._wait_for_interval()
            results = session.search_articles(
                query_title,
                timeout_ms=_seconds_to_ms(
                    self.config.navigation_timeout_seconds
                ),
                max_results=self.config.max_search_results,
            )
            for result in results:
                normalized_result = normalize_title(result.title)
                compact_result = _compact_search_title(result.title)
                if (
                    normalized_result not in expected_titles
                    and not (
                        len(compact_result) >= 12
                        and compact_result in compact_titles
                    )
                ):
                    continue
                normalized_url = normalize_dbpia_article_url(
                    result.article_url
                )
                if normalized_url is None:
                    raise DbpiaBrowserOperationError(
                        "invalid_response",
                        "unsafe_dbpia_search_article_reference",
                        "unsafe_url",
                    )
                matches.setdefault(normalized_url, result)
            if len(matches) > 1:
                raise DbpiaBrowserOperationError(
                    "identity_mismatch",
                    "ambiguous_exact_dbpia_search_results",
                    "ambiguous",
                )
        if not matches:
            raise DbpiaBrowserOperationError(
                "not_found",
                "exact_dbpia_search_result_not_found",
                "not_found",
            )
        return next(iter(matches))

    def _record_operation_failure(
        self,
        status: str,
        reason: str,
        error_code: str,
    ) -> None:
        """Update the same-run circuit without exposing request details.

        Authentication and configuration failures retain their existing
        immediate circuit behavior.  Transient provider/browser failures open
        the circuit only after three consecutive retryable outcomes; a later
        successful resolution resets that streak.
        """

        if (
            error_code in _CIRCUIT_CODES
            or reason in _IMMEDIATE_CIRCUIT_REASONS
        ):
            self._circuit_error = (status, reason, error_code)
            return
        if status != "retryable_error":
            self._consecutive_retryable_errors = 0
            return
        self._consecutive_retryable_errors += 1
        if self._consecutive_retryable_errors >= TRANSIENT_CIRCUIT_THRESHOLD:
            self._circuit_error = (
                "retryable_error",
                "dbpia_browser_transient_circuit_open",
                "consecutive_retryable_failures",
            )

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    def _get_session(self) -> DbpiaBrowserSession:
        if self._session is not None:
            return self._session
        assert self.config.profile_dir is not None
        self._session = _PlaywrightDbpiaSession(self.config)
        return self._session

    def _wait_for_interval(self) -> None:
        now = self._clock()
        delay = max(0.0, self._next_allowed - now)
        if delay:
            self._sleep(delay)
            now = self._clock()
        self._next_allowed = max(now, self._next_allowed) + (
            self.config.min_interval_seconds
        )

    @staticmethod
    def _failure(
        paper: PaperCandidate,
        status: str,
        reason: str,
        error_code: str,
        *,
        stage: str = "dbpia-browser",
    ) -> DbpiaBrowserResolution:
        return DbpiaBrowserResolution(
            status=status,
            reason=reason,
            paper=paper,
            error_code=error_code,
            attempts=(DbpiaBrowserAttempt(stage, "failed", error_code),),
        )


class _PlaywrightDbpiaSession:
    """Lazy same-thread Playwright session with first-party route isolation."""

    def __init__(self, config: DbpiaBrowserConfig) -> None:
        assert config.profile_dir is not None
        self._config = config
        try:
            self._profile_dir = validate_dbpia_profile_dir(
                config.profile_dir,
                repository_root=config.repository_root,
            )
        except (OSError, ValueError):
            raise DbpiaBrowserOperationError(
                "configuration_error",
                "invalid_browser_profile",
                "invalid_browser_profile",
            ) from None
        self._profile_lock = _ProfileLock(self._profile_dir)
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._dialog_error: DbpiaBrowserOperationError | None = None
        self._blocked_document = False
        self._login_attempted = False
        self._owner_thread: int | None = None

    def search_articles(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_results: int,
    ) -> tuple[DbpiaBrowserSearchResult, ...]:
        """Search through the rendered DBpia homepage controls only."""

        self._ensure_started()
        self._require_owner_thread()
        assert self._page is not None
        if not isinstance(title, str) or not title.strip() or len(title) > 2_000:
            raise DbpiaBrowserOperationError(
                "invalid_response",
                "invalid_dbpia_search_title",
                "identity_mismatch",
            )
        # DBpia rejects longer rendered-form queries with an alert instead of
        # producing a result or no-data state.  The returned card title is
        # still compared with the complete, untruncated paper title by the
        # resolver before any article page can be opened.
        search_title = title.strip()[:_SEARCH_QUERY_MAX_CHARS].rstrip()
        if not 1 <= max_results <= 100:
            raise DbpiaBrowserOperationError(
                "configuration_error",
                "invalid_dbpia_search_result_limit",
                "invalid_browser_profile",
            )

        self._blocked_document = False
        self._dialog_error = None
        try:
            response = self._page.goto(
                DBPIA_HOME_URL,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self._raise_http_failure(response)
            if self._login_with_configured_credentials(timeout_ms=timeout_ms):
                self._blocked_document = False
                response = self._page.goto(
                    DBPIA_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                self._raise_http_failure(response)
            else:
                self._dismiss_org_auth_complete_modal(timeout_ms=timeout_ms)
            self._raise_current_challenge()
            self._verify_required_institution()

            # DBpia visually replaces this native select with a custom label
            # while retaining the official form control as a hidden element.
            # Select the documented `node` option on that exact control.
            selected_options = self._page.locator(
                _SEARCH_OPTION_SELECTOR
            ).select_option(
                "node",
                timeout=timeout_ms,
                force=True,
            )
            if selected_options != ["node"]:
                raise DbpiaBrowserOperationError(
                    "invalid_response",
                    "dbpia_search_scope_not_selected",
                    "identity_mismatch",
                )
            self._page.locator(_SEARCH_INPUT_SELECTOR).fill(
                search_title,
                timeout=timeout_ms,
            )
            self._page.locator(_SEARCH_SUBMIT_SELECTOR).click(
                timeout=timeout_ms,
            )
            self._page.wait_for_selector(
                _SEARCH_READY_SELECTOR,
                state="visible",
                timeout=timeout_ms,
            )
            self._raise_current_challenge()
            self._verify_required_institution()
            if not _is_allowed_first_party_url(self._page.url):
                raise DbpiaBrowserOperationError(
                    "authentication_required",
                    "dbpia_search_left_first_party",
                    "authentication_required",
                )
            self._bounded_page_content()
            return self._collect_search_results(max_results=max_results)
        except DbpiaBrowserOperationError:
            raise
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            if self._blocked_document:
                raise DbpiaBrowserOperationError(
                    "authentication_required",
                    "dbpia_search_redirected_outside_first_party",
                    "authentication_required",
                ) from None
            challenge = self._inspect_current_challenge()
            if challenge is not None:
                raise challenge from None
            raise DbpiaBrowserOperationError(
                "retryable_error",
                "dbpia_search_ui_failed",
                "browser_error",
            ) from None

    def open_article(self, url: str, *, timeout_ms: int) -> DbpiaPageSnapshot:
        self._ensure_started()
        self._require_owner_thread()
        assert self._page is not None
        self._blocked_document = False
        self._dialog_error = None
        try:
            response = self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self._raise_http_failure(response)
            if self._login_with_configured_credentials(timeout_ms=timeout_ms):
                self._blocked_document = False
                response = self._page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                self._raise_http_failure(response)
            else:
                self._dismiss_org_auth_complete_modal(timeout_ms=timeout_ms)
            self._raise_current_challenge()
            self._verify_required_institution()
            document = self._bounded_page_content()
            final_url = self._page.url
            buttons = self._collect_download_buttons()
        except DbpiaBrowserOperationError:
            raise
        except Exception:
            if self._blocked_document:
                raise DbpiaBrowserOperationError(
                    "authentication_required",
                    "dbpia_session_redirected_outside_first_party",
                    "authentication_required",
                ) from None
            raise DbpiaBrowserOperationError(
                "retryable_error",
                "dbpia_landing_navigation_failed",
                "browser_error",
            ) from None
        return DbpiaPageSnapshot(
            final_url=final_url,
            document=document,
            buttons=buttons,
            http_status=(
                int(response.status) if response is not None else None
            ),
        )

    def _collect_search_results(
        self,
        *,
        max_results: int,
    ) -> tuple[DbpiaBrowserSearchResult, ...]:
        assert self._page is not None
        cards = self._page.locator(_SEARCH_CARD_SELECTOR)
        count = cards.count()
        if count > max_results:
            raise DbpiaBrowserOperationError(
                "identity_mismatch",
                "too_many_dbpia_search_results",
                "ambiguous",
            )
        if count == 0:
            no_data = self._page.locator(_SEARCH_NO_DATA_SELECTOR)
            if no_data.count() and no_data.is_visible():
                return ()
            raise DbpiaBrowserOperationError(
                "invalid_response",
                "dbpia_search_results_missing",
                "invalid_response",
            )

        results: list[DbpiaBrowserSearchResult] = []
        for index in range(count):
            links = cards.nth(index).locator(_SEARCH_RESULT_LINK_SELECTOR)
            if links.count() != 1:
                raise DbpiaBrowserOperationError(
                    "identity_mismatch",
                    "ambiguous_dbpia_search_card",
                    "ambiguous",
                )
            link = links.nth(0)
            if not link.is_visible():
                raise DbpiaBrowserOperationError(
                    "invalid_response",
                    "hidden_dbpia_search_result",
                    "invalid_response",
                )
            result_title = link.inner_text().strip()
            href = link.get_attribute("href")
            article_url = normalize_dbpia_article_url(href)
            if not result_title or len(result_title) > 4_096:
                raise DbpiaBrowserOperationError(
                    "invalid_response",
                    "unsafe_dbpia_search_result",
                    "unsafe_url",
                )
            # Search pages can mix article cards with journal/detail links.
            # Ignore only that exact observed first-party journal-card shape;
            # every malformed or external link still fails closed.
            if article_url is None:
                if _is_ignorable_dbpia_journal_search_href(href):
                    continue
                raise DbpiaBrowserOperationError(
                    "invalid_response",
                    "unsafe_dbpia_search_result",
                    "unsafe_url",
                )
            results.append(
                DbpiaBrowserSearchResult(
                    title=result_title,
                    article_url=article_url,
                )
            )
        return tuple(results)

    def _login_with_configured_credentials(self, *, timeout_ms: int) -> bool:
        """Perform at most one ordinary DBpia login from the scoped header."""

        assert self._page is not None
        if self._config.login_id is None:
            return False
        # An active, explicitly required institution session already grants
        # the subscription context needed by this backend. A personal-login
        # link may remain visible in DBpia's header in that state; attempting
        # optional credentials merely because that link exists can replace a
        # valid institution session with an avoidable login failure.
        if self._required_institution_is_active():
            return False
        institution_login = self._visible_org_auth_complete_login()
        login_link = (
            None
            if institution_login is not None
            else self._visible_header_login_link()
        )
        login_control = (
            institution_login
            if institution_login is not None
            else login_link
        )
        if login_control is None:
            return False
        if self._login_attempted:
            raise DbpiaBrowserOperationError(
                "authentication_required",
                "dbpia_configured_login_failed",
                "authentication_required",
            )
        self._login_attempted = True
        try:
            login_control.click(timeout=timeout_ms)
            self._page.locator(_LOGIN_ID_SELECTOR).fill(
                self._config.login_id,
                timeout=timeout_ms,
            )
            self._page.locator(_LOGIN_PASSWORD_SELECTOR).fill(
                self._config.login_password,
                timeout=timeout_ms,
            )
            self._page.locator(_LOGIN_SUBMIT_SELECTOR).click(
                timeout=timeout_ms,
            )
            with contextlib.suppress(Exception):
                self._page.wait_for_selector(
                    _HEADER_LOGIN_SELECTOR,
                    state="hidden",
                    timeout=timeout_ms,
                )
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            challenge = self._inspect_current_challenge()
            if challenge is not None:
                raise challenge from None
            raise DbpiaBrowserOperationError(
                "authentication_required",
                "dbpia_configured_login_failed",
                "authentication_required",
            ) from None

        challenge = self._inspect_current_challenge()
        if challenge is not None:
            raise challenge
        if self._visible_header_login_link() is not None:
            raise DbpiaBrowserOperationError(
                "authentication_required",
                "dbpia_configured_login_failed",
                "authentication_required",
            )
        return True

    def _dismiss_org_auth_complete_modal(self, *, timeout_ms: int) -> bool:
        """Close the "기관인증 완료" dialog with its official continue button.

        The dialog appears once an institution session is active and no
        personal login is configured or wanted.  Its backdrop blocks pointer
        events, so leaving it open makes the search submit click time out.
        Only the exact ``바로 이용하기`` control is used; a missing or
        differently shaped dialog is left untouched and reported as not
        dismissed.
        """

        assert self._page is not None
        try:
            modal = self._page.locator(_ORG_AUTH_COMPLETE_MODAL_SELECTOR)
            if modal.count() != 1 or not modal.is_visible():
                return False
            controls = self._page.locator(_ORG_AUTH_COMPLETE_LATER_SELECTOR)
            if controls.count() != 1:
                return False
            control = controls.nth(0)
            if not control.is_visible() or not control.is_enabled():
                return False
            control.click(timeout=timeout_ms)
            with contextlib.suppress(Exception):
                modal.wait_for(state="hidden", timeout=timeout_ms)
            return True
        except Exception:
            # Optional UI; test seams and other page states need not expose it.
            return False

    def _visible_org_auth_complete_login(self) -> Any | None:
        """Use DBpia's official post-institution-auth login action if shown."""

        assert self._page is not None
        try:
            modal = self._page.locator(
                _ORG_AUTH_COMPLETE_MODAL_SELECTOR
            )
            if modal.count() != 1 or not modal.is_visible():
                return None
            controls = self._page.locator(
                _ORG_AUTH_COMPLETE_LOGIN_SELECTOR
            )
            if controls.count() != 1:
                raise DbpiaBrowserOperationError(
                    "invalid_response",
                    "ambiguous_dbpia_institution_login_control",
                    "ambiguous",
                )
            control = controls.nth(0)
            if not control.is_visible() or not control.is_enabled():
                raise DbpiaBrowserOperationError(
                    "authentication_required",
                    "dbpia_institution_login_control_unavailable",
                    "authentication_required",
                )
            return control
        except DbpiaBrowserOperationError:
            raise
        except Exception:
            # Test seams and ordinary pages may not implement or expose this
            # optional modal. The scoped header login remains the fallback.
            return None

    def _visible_header_login_link(self) -> Any | None:
        assert self._page is not None
        links = self._page.locator(_HEADER_LOGIN_SELECTOR)
        count = links.count()
        if count > 10:
            raise DbpiaBrowserOperationError(
                "invalid_response",
                "ambiguous_dbpia_login_control",
                "ambiguous",
            )
        unsafe_visible = False
        for index in range(count):
            link = links.nth(index)
            if not link.is_visible():
                continue
            href = link.get_attribute("href")
            if _is_exact_dbpia_login_href(href):
                return link
            unsafe_visible = True
        if unsafe_visible:
            raise DbpiaBrowserOperationError(
                "invalid_response",
                "unsafe_dbpia_login_control",
                "unsafe_url",
            )
        return None

    def _verify_required_institution(self) -> None:
        expected = self._config.required_institution_name
        if expected is None:
            return
        if self._required_institution_is_active():
            return
        raise DbpiaBrowserOperationError(
            "authentication_required",
            "required_dbpia_institution_not_active",
            "authentication_required",
        )

    def _required_institution_is_active(self) -> bool:
        """Return whether the configured institution is visibly active."""

        expected = self._config.required_institution_name
        if expected is None:
            return False
        assert self._page is not None
        expected_normalized = normalize_title(expected)
        institutions = self._page.locator(_INSTITUTION_SELECTOR)
        for index in range(institutions.count()):
            institution = institutions.nth(index)
            if (
                institution.is_visible()
                and normalize_title(institution.inner_text())
                == expected_normalized
            ):
                return True
        return False

    def _bounded_page_content(self) -> str:
        assert self._page is not None
        document = self._page.content()
        if len(document.encode("utf-8")) > self._config.max_html_bytes:
            raise DbpiaBrowserOperationError(
                "invalid_response",
                "dbpia_page_too_large",
                "too_large",
            )
        return document

    def _raise_current_challenge(
        self,
        *,
        include_access_gates: bool = False,
    ) -> None:
        challenge = self._inspect_current_challenge(
            include_access_gates=include_access_gates,
        )
        if challenge is not None:
            raise challenge

    def _inspect_current_challenge(
        self,
        *,
        include_access_gates: bool = False,
    ) -> DbpiaBrowserOperationError | None:
        assert self._page is not None
        visible = self._visible_challenge(
            self._page,
            include_access_gates=include_access_gates,
        )
        if visible is not None:
            return visible
        try:
            snapshot = DbpiaPageSnapshot(
                final_url=self._page.url,
                document=self._bounded_page_content(),
                buttons=(),
            )
        except DbpiaBrowserOperationError:
            raise
        except Exception:
            return None
        return _page_challenge(snapshot)

    @staticmethod
    def _visible_challenge(
        page: Any,
        *,
        include_access_gates: bool = False,
    ) -> DbpiaBrowserOperationError | None:
        """Classify rendered gates in the operation context that activates them.

        DBpia embeds dormant dialogs throughout ordinary pages.  CAPTCHA and
        authentication gates are meaningful before an action, but purchase and
        preview layers are considered only after the official download control
        failed to emit a native browser download.
        """

        challenge_groups = [
            (
                _VISIBLE_CAPTCHA_SELECTORS,
                "captcha_required",
                "dbpia_captcha_requires_manual_action",
                "captcha_required",
            ),
            (
                _VISIBLE_AUTH_SELECTORS,
                "authentication_required",
                "dbpia_authentication_requires_manual_refresh",
                "authentication_required",
            ),
        ]
        if include_access_gates:
            challenge_groups.append(
                (
                    _VISIBLE_ACCESS_GATE_SELECTORS,
                    "access_denied",
                    "dbpia_subscription_or_access_gate_visible",
                    "access_denied",
                )
            )
        for selectors, status, reason, error_code in challenge_groups:
            for selector in selectors:
                try:
                    locators = page.locator(selector)
                    count = min(locators.count(), 10)
                    if any(
                        locators.nth(index).is_visible()
                        for index in range(count)
                    ):
                        return DbpiaBrowserOperationError(
                            status,
                            reason,
                            error_code,
                        )
                except Exception:
                    continue
        return None

    @staticmethod
    def _raise_http_failure(response: Any) -> None:
        status = int(response.status) if response is not None else None
        failure = _page_http_failure(status)
        if failure is not None:
            raise failure

    def download_button(
        self,
        button: DbpiaBrowserButton,
        *,
        expected_node_id: str,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        self._require_owner_thread()
        assert self._page is not None
        locator = self._page.locator("[onclick]").nth(button.dom_index)
        self._blocked_document = False
        try:
            onclick = locator.get_attribute("onclick") or ""
            node_ids = tuple(
                match.group("node").upper()
                for match in _DOWNLOAD_BUTTON_RE.finditer(onclick)
            )
            if (
                node_ids != (expected_node_id,)
                or not locator.is_visible()
                or not locator.is_enabled()
            ):
                raise DbpiaBrowserOperationError(
                    "identity_mismatch",
                    "dbpia_download_control_changed",
                    "identity_mismatch",
                )
            self._dialog_error = None
            self._dismiss_org_auth_complete_modal(timeout_ms=timeout_ms)
            self._raise_current_challenge()
            with self._page.expect_download(timeout=timeout_ms) as download_info:
                locator.click(timeout=timeout_ms)
            download = download_info.value
        except DbpiaBrowserOperationError:
            raise
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            if self._blocked_document:
                raise DbpiaBrowserOperationError(
                    "authentication_required",
                    "dbpia_download_redirected_outside_first_party",
                    "authentication_required",
                ) from None
            challenge = self._current_page_challenge()
            if challenge is not None:
                raise challenge from None
            raise DbpiaBrowserOperationError(
                "retryable_error",
                "dbpia_native_download_not_emitted",
                "download_timeout",
            ) from None

        try:
            if not _is_allowed_download_url(download.url):
                raise DbpiaBrowserOperationError(
                    "invalid_response",
                    "unexpected_dbpia_download_origin",
                    "unsafe_url",
                )
            failure = download.failure()
            if failure is not None:
                raise DbpiaBrowserOperationError(
                    "retryable_error",
                    "dbpia_native_download_failed",
                    "browser_error",
                )
            path_value = download.path()
            if path_value is None:
                raise DbpiaBrowserOperationError(
                    "retryable_error",
                    "dbpia_native_download_missing",
                    "browser_error",
                )
            content = _read_download_without_symlink(
                Path(path_value),
                max_pdf_bytes,
            )
            if not content.startswith(b"%PDF-"):
                raise DbpiaBrowserOperationError(
                    "invalid_response",
                    "dbpia_download_is_not_pdf",
                    "invalid_pdf",
                )
            return content
        finally:
            with contextlib.suppress(Exception):
                download.delete()

    def close(self) -> None:
        if self._owner_thread is not None:
            self._require_owner_thread()
        if self._context is not None:
            with contextlib.suppress(Exception):
                self._context.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                self._playwright.stop()
        self._context = None
        self._page = None
        self._playwright = None
        self._owner_thread = None
        self._profile_lock.release()

    def _ensure_started(self) -> None:
        if self._context is not None:
            return
        self._profile_lock.acquire()
        playwright: Any = None
        context: Any = None
        try:
            _configure_user_playwright_libraries()
            sync_playwright = _load_sync_playwright()
            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                headless=self._config.headless,
                accept_downloads=True,
                service_workers="block",
            )
            context.route("**/*", self._route_first_party_only)
            pages = context.pages
            page = pages[0] if pages else context.new_page()
            page.on("dialog", self._handle_dialog)
            self._playwright = playwright
            self._context = context
            self._page = page
            self._owner_thread = threading.get_ident()
        except DbpiaBrowserOperationError:
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if playwright is not None:
                with contextlib.suppress(Exception):
                    playwright.stop()
            self._profile_lock.release()
            raise
        except Exception as exc:
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if playwright is not None:
                with contextlib.suppress(Exception):
                    playwright.stop()
            self._profile_lock.release()
            code = _launch_error_code(exc)
            raise DbpiaBrowserOperationError(
                "configuration_error",
                code,
                code,
            ) from None

    def _collect_download_buttons(self) -> tuple[DbpiaBrowserButton, ...]:
        assert self._page is not None
        controls = self._page.locator("[onclick]")
        count = controls.count()
        if count > MAX_ONCLICK_ELEMENTS:
            raise DbpiaBrowserOperationError(
                "invalid_response",
                "too_many_dbpia_controls",
                "ambiguous",
            )
        values: list[DbpiaBrowserButton] = []
        for index in range(count):
            locator = controls.nth(index)
            onclick = locator.get_attribute("onclick") or ""
            nodes = tuple(
                match.group("node").upper()
                for match in _DOWNLOAD_BUTTON_RE.finditer(onclick)
            )
            if not nodes:
                continue
            values.append(
                DbpiaBrowserButton(
                    dom_index=index,
                    node_ids=nodes,
                    visible=bool(locator.is_visible()),
                    enabled=bool(locator.is_enabled()),
                )
            )
        return tuple(values)

    def _current_page_challenge(self) -> DbpiaBrowserOperationError | None:
        """Classify a post-click challenge without retaining page details."""

        assert self._page is not None
        challenge = self._inspect_current_challenge(
            include_access_gates=True,
        )
        if challenge is not None:
            return challenge
        if not is_exact_dbpia_article_url(self._page.url):
            return DbpiaBrowserOperationError(
                "authentication_required",
                "dbpia_session_left_exact_article_page",
                "authentication_required",
            )
        return None

    def _route_first_party_only(self, route: Any) -> None:
        request = route.request
        if _is_allowed_first_party_url(request.url):
            route.continue_()
            return
        is_main_frame = False
        if self._page is not None:
            with contextlib.suppress(Exception):
                is_main_frame = request.frame == self._page.main_frame
        if (
            bool(request.is_navigation_request())
            and request.resource_type == "document"
            and is_main_frame
        ):
            self._blocked_document = True
        route.abort(error_code="blockedbyclient")

    def _handle_dialog(self, dialog: Any) -> None:
        message = str(dialog.message or "").casefold()
        if any(marker in message for marker in _CAPTCHA_MARKERS):
            self._dialog_error = DbpiaBrowserOperationError(
                "captcha_required",
                "dbpia_captcha_requires_manual_action",
                "captcha_required",
            )
        elif any(marker in message for marker in _AUTH_DIALOG_MARKERS):
            self._dialog_error = DbpiaBrowserOperationError(
                "authentication_required",
                "dbpia_authentication_requires_manual_refresh",
                "authentication_required",
            )
        else:
            self._dialog_error = DbpiaBrowserOperationError(
                "access_denied",
                "dbpia_download_dialog_rejected",
                "access_denied",
            )
        with contextlib.suppress(Exception):
            dialog.dismiss()

    def _require_owner_thread(self) -> None:
        if self._owner_thread is not None and self._owner_thread != threading.get_ident():
            raise RuntimeError("DBpia browser session used from a different thread")


class _ProfileLock:
    def __init__(self, profile_dir: Path) -> None:
        self._path = profile_dir / ".forensics-paper-session.lock"
        self._handle: Any = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        if self._path.is_symlink():
            raise DbpiaBrowserOperationError(
                "configuration_error",
                "invalid_browser_profile",
                "invalid_browser_profile",
            )
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        handle: Any = None
        try:
            descriptor = os.open(self._path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "a+", encoding="ascii")
            descriptor = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.close()
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise DbpiaBrowserOperationError(
                "configuration_error",
                "dbpia_browser_profile_in_use",
                "profile_in_use",
            ) from None
        except OSError:
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.close()
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise DbpiaBrowserOperationError(
                "configuration_error",
                "invalid_browser_profile",
                "invalid_browser_profile",
            ) from None
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _validate_config(config: DbpiaBrowserConfig) -> None:
    if config.permission_granted and config.profile_dir is None:
        raise ValueError("profile_dir is required for DBpia browser download")
    if (config.login_id is None) != (config.login_password is None):
        raise ValueError("login_id and login_password must be provided together")
    if config.login_id is not None:
        if (
            not isinstance(config.login_id, str)
            or not config.login_id.strip()
            or not isinstance(config.login_password, str)
            or not config.login_password
        ):
            raise ValueError("DBpia login credentials must be non-empty strings")
    if config.required_institution_name is not None and (
        not isinstance(config.required_institution_name, str)
        or normalize_title(config.required_institution_name) is None
    ):
        raise ValueError("required_institution_name must be a non-empty string")
    for name, value in (
        ("navigation_timeout_seconds", config.navigation_timeout_seconds),
        ("download_timeout_seconds", config.download_timeout_seconds),
        ("min_interval_seconds", config.min_interval_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be positive")
    if config.max_html_bytes < 1 or config.max_pdf_bytes < 1:
        raise ValueError("browser response bounds must be positive")
    if not 1 <= config.max_attempts_per_run <= 1_000:
        raise ValueError("max_attempts_per_run must be between 1 and 1000")
    if (
        isinstance(config.max_search_results, bool)
        or not isinstance(config.max_search_results, int)
        or not 1 <= config.max_search_results <= 100
    ):
        raise ValueError("max_search_results must be between 1 and 100")


def _compact_search_title(value: object) -> str:
    """Return the audited spacing/punctuation-insensitive search key."""

    normalized = normalize_title(value if isinstance(value, str) else None)
    if normalized is None:
        return ""
    return "".join(character for character in normalized if character.isalnum())


def _doi_less_search_metadata_ready(paper: PaperCandidate) -> bool:
    """Require every field used by the DOI-less landing identity chain."""

    return bool(
        paper.publication_year is not None
        and normalize_title(paper.institution_id or "")
        and normalize_title(paper.journal_id or "")
        and re.fullmatch(
            r"(?:[0-9]{1,6}_[0-9]{1,6}|[0-9]{1,6})",
            (paper.issue_id or "").strip(),
        )
        and (paper.first_page or "").strip()
        and (paper.final_page or "").strip()
        and bool(paper.authors)
        and all(normalize_title(author) for author in paper.authors)
    )


def _has_complete_search_bibliography(
    provenance: tuple[str, ...],
) -> bool:
    """Accept an exact volume/issue pair or a safely bounded issue-only row."""

    observed = set(provenance)
    return bool(
        _SEARCH_BIBLIOGRAPHY_CORE_PROVENANCE.issubset(observed)
        and _SEARCH_BIBLIOGRAPHY_ISSUE_PROVENANCE.intersection(observed)
    )


def _bind_article_references(
    requested: DbpiaArticleReference,
    observed: DbpiaArticleReference,
    buttons: tuple[DbpiaBrowserButton, ...],
) -> str:
    actionable_nodes = {
        node
        for button in buttons
        if button.visible and button.enabled
        for node in button.node_ids
    }
    explicit = {
        node for node in (requested.node_id, observed.node_id) if node is not None
    }
    if len(explicit) > 1:
        raise DbpiaBrowserOperationError(
            "identity_mismatch",
            "dbpia_article_redirect_node_mismatch",
            "identity_mismatch",
        )
    if explicit:
        expected = next(iter(explicit))
        # Dormant controls for related content can be present in the DOM.
        # Bind only controls a user could actually activate.  With no active
        # control, carry the URL-bound node into _select_download_button so it
        # reports an unavailable download instead of a false node mismatch.
        if actionable_nodes and actionable_nodes != {expected}:
            raise DbpiaBrowserOperationError(
                "identity_mismatch",
                "dbpia_download_button_node_mismatch",
                "identity_mismatch",
            )
        return expected
    if not actionable_nodes:
        raise DbpiaBrowserOperationError(
            "access_denied",
            "dbpia_download_control_unavailable",
            "access_denied",
        )
    if len(actionable_nodes) != 1:
        raise DbpiaBrowserOperationError(
            "identity_mismatch",
            "ambiguous_dbpia_download_node",
            "ambiguous",
        )
    return next(iter(actionable_nodes))


def _select_download_button(
    buttons: tuple[DbpiaBrowserButton, ...],
    expected_node: str,
) -> DbpiaBrowserButton:
    candidates = [
        button
        for button in buttons
        if button.node_ids == (expected_node,)
        and button.visible
        and button.enabled
    ]
    if len(candidates) != 1:
        raise DbpiaBrowserOperationError(
            "access_denied" if not candidates else "identity_mismatch",
            (
                "dbpia_download_control_unavailable"
                if not candidates
                else "ambiguous_dbpia_download_controls"
            ),
            "access_denied" if not candidates else "ambiguous",
        )
    return candidates[0]


def _page_challenge(
    snapshot: DbpiaPageSnapshot,
) -> DbpiaBrowserOperationError | None:
    # DBpia ships hidden CAPTCHA, login, MFA, preview, and purchase dialogs in
    # every ordinary article document.  Static HTML cannot establish whether
    # any of those gates is active, so only the top-level navigation target is
    # classified here.  The Playwright adapter separately inspects visibility.
    try:
        path = urlsplit(snapshot.final_url).path.casefold()
    except ValueError:
        path = ""
    if any(
        marker in path
        for marker in ("/login", "/signin", "/member/login")
    ):
        return DbpiaBrowserOperationError(
            "authentication_required",
            "dbpia_authentication_requires_manual_refresh",
            "authentication_required",
        )
    return None


def _page_http_failure(
    status: int | None,
) -> DbpiaBrowserOperationError | None:
    if status is None or 200 <= status < 400:
        return None
    if status == 401:
        return DbpiaBrowserOperationError(
            "authentication_required",
            "dbpia_article_authentication_required",
            "authentication_required",
        )
    if status == 403:
        return DbpiaBrowserOperationError(
            "access_denied",
            "dbpia_article_access_denied",
            "access_denied",
        )
    if status == 429:
        return DbpiaBrowserOperationError(
            "retryable_error",
            "dbpia_article_rate_limited",
            "retryable_error",
        )
    if status in {408, 425} or 500 <= status <= 599:
        return DbpiaBrowserOperationError(
            "retryable_error",
            "dbpia_article_retryable_http_error",
            "retryable_error",
        )
    if status in {404, 410}:
        return DbpiaBrowserOperationError(
            "not_found",
            "dbpia_article_not_found",
            "not_found",
        )
    return DbpiaBrowserOperationError(
        "invalid_response",
        "dbpia_article_http_error",
        "http_status",
    )


def _is_allowed_first_party_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    return bool(
        parsed.scheme.casefold() == "https"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and (host == "dbpia.co.kr" or host.endswith(".dbpia.co.kr"))
    )


def _is_exact_dbpia_login_href(href: object) -> bool:
    if not isinstance(href, str):
        return False
    raw = href.strip()
    if not raw or len(raw) > 2_048 or any(ord(char) < 0x20 for char in raw):
        return False
    if raw.startswith("/") and not raw.startswith("//"):
        candidate = urljoin(DBPIA_HOME_URL, raw)
    else:
        candidate = raw
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == "www.dbpia.co.kr"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.casefold() == "/member/login"
        and not parsed.query
        and not parsed.fragment
    )


def _is_allowed_download_url(url: str) -> bool:
    if not _is_allowed_first_party_url(url) or len(url) > 8_192:
        return False
    try:
        parsed = urlsplit(url)
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if parsed.fragment:
        return False
    if host == "download.dbpia.co.kr":
        return bool(
            parsed.path.casefold() == "/file/download"
            and len(pairs) == 1
            and pairs[0][0] == "key"
            and re.fullmatch(r"[0-9]{1,40}", pairs[0][1])
        )
    if host == "downloadcf.dbpia.co.kr":
        values = {name: value for name, value in pairs}
        return bool(
            _DBPIA_CDN_PATH_RE.fullmatch(parsed.path)
            and len(values) == len(pairs)
            and set(values).issubset({"jwt", "filename"})
            and "jwt" in values
            and 1 <= len(values["jwt"]) <= 4_096
            and len(values.get("filename", "")) <= 2_048
        )
    return False


def _read_download_without_symlink(path: Path, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise DbpiaBrowserOperationError(
            "invalid_response",
            "dbpia_download_file_unsafe",
            "invalid_pdf",
        ) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DbpiaBrowserOperationError(
                "invalid_response",
                "dbpia_download_file_unsafe",
                "invalid_pdf",
            )
        if info.st_size > max_bytes:
            raise DbpiaBrowserOperationError(
                "too_large",
                "dbpia_pdf_exceeds_size_limit",
                "too_large",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(content) > max_bytes:
        raise DbpiaBrowserOperationError(
            "too_large",
            "dbpia_pdf_exceeds_size_limit",
            "too_large",
        )
    return content


def _load_sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise DbpiaBrowserOperationError(
            "configuration_error",
            "browser_dependency_missing",
            "browser_dependency_missing",
        ) from None
    return sync_playwright


def _launch_error_code(exc: BaseException) -> str:
    lowered = str(exc).casefold()
    if "executable doesn't exist" in lowered or "playwright install" in lowered:
        return "browser_not_installed"
    return "browser_launch_failed"


def _require_private_owned_path(path: Path, *, directory: bool) -> None:
    info = path.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("DBpia browser profile must be owned by the current user")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise ValueError("DBpia browser profile permissions are too broad")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError("DBpia browser profile is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError("DBpia browser profile marker is not a regular file")


def _looks_like_default_browser_profile(path: Path) -> bool:
    normalized = path.as_posix().casefold().rstrip("/")
    markers = (
        "/.config/google-chrome",
        "/.config/chromium",
        "/.config/microsoft-edge",
        "/library/application support/google/chrome",
        "/appdata/local/google/chrome/user data",
        "/appdata/local/microsoft/edge/user data",
    )
    return any(normalized.endswith(marker) for marker in markers)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _seconds_to_ms(value: float) -> int:
    return max(1, int(value * 1_000))
