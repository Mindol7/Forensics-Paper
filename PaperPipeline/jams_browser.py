"""Opt-in authenticated KDFS JAMS PDF downloads through Playwright.

This backend is deliberately disabled by default.  An operator must first
confirm that their account and the KDFS/JAMS terms permit automated internal
research downloads, then complete JAMS authentication manually in a dedicated
headed browser profile.  The program never accepts a password, emailed
authentication key, cookie, or bearer token.  Browser-managed authentication
state stays inside that private, marked profile and is never copied to config,
logs, results, or audit records.

The resolver is intentionally narrow: only KCI records for
``한국디지털포렌식학회`` / ``디지털포렌식연구`` are applicable.  It opens an
exact official detail reference returned by :mod:`PaperPipeline.kdfs_resolver`
or finds that reference through the rendered official title-search form.  It
never constructs a file identifier or download URL.  After exact article
identity validation, it clicks exactly one visible, enabled DOM control that
advertises a PDF/original-text download and accepts only a native browser
download from ``kdfs.jams.or.kr``.  CAPTCHA, MFA/authentication gates,
ambiguous controls, and changed DOM contracts fail closed; no challenge bypass
or broad crawl is attempted.
"""

from __future__ import annotations

import contextlib
import fcntl
import html
import json
import math
import os
import re
import stat
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from PaperPipeline import kdfs_resolver as public_kdfs
from PaperPipeline.identifier_resolver import normalize_title
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import write_json_atomic


JAMS_BROWSER_RESOLVER_NAME = "kdfs-jams-authenticated-browser"
DEFAULT_PROFILE_MARKER = ".forensics-paper-kdfs-jams-profile.json"
PROFILE_SCHEMA_VERSION = 1
PROFILE_PURPOSE = "forensics-paper-kdfs-jams-browser"
DEFAULT_MAX_HTML_BYTES = 4 * 1024 * 1024
TRANSIENT_CIRCUIT_THRESHOLD = 3
MAX_DOM_CONTROLS = 2_000

KDFS_RENDERED_SEARCH_URL = (
    "https://kdfs.jams.or.kr/co/com/EgovMenu.kci?"
    "s_url=%2Fpo%2Fsearch%2FpoArtiSear.kci&"
    "s_MenuId=MENU-000000000051000&accnId=AC0000000013"
)
_SEARCH_MENU_PATH = "/co/com/EgovMenu.kci"
_SEARCH_MENU_PAIRS = (
    ("s_url", "/po/search/poArtiSear.kci"),
    ("s_MenuId", "MENU-000000000051000"),
    ("accnId", "AC0000000013"),
)
_SEARCH_FORM_SELECTOR = "form#poSearchVo"
_SEARCH_SUBMIT_HANDLER_RE = re.compile(
    r"\A(?:javascript\s*:\s*)?goSearch\(\s*\)\s*;?"
    r"(?:\s*return\s+false\s*;?)?\s*\Z",
    re.IGNORECASE,
)

_DETAIL_ARTICLE_RE = re.compile(r"\ASJ[0-9]{10}\Z")
_SUBMISSION_COUNT_RE = re.compile(r"\A[1-9][0-9]{0,3}\Z")
_FILE_ID_RE = re.compile(r"\AKCI_(?:FI|ORTE)[0-9]{8,16}\Z", re.IGNORECASE)
_IFRAME_HANDLER_RE = re.compile(
    r"\A(?:javascript\s*:\s*)?fncFileIframe\(\s*"
    r"(?P<quote>['\"])(?P<file>KCI_(?:FI|ORTE)[0-9]{8,16})(?P=quote)"
    r"\s*\)\s*;?\s*\Z",
    re.IGNORECASE,
)
_FILE_CHECK_HANDLER_RE = re.compile(
    r"\A(?:javascript\s*:\s*)?fncFileIdChk\(\s*"
    r"(?P<arguments>[^()]*)\s*\)\s*;?\s*\Z",
    re.IGNORECASE,
)
_SAFE_HANDLER_ARGUMENT_RE = re.compile(
    r"(?:['\"][A-Za-z0-9_.:@/+-]{1,256}['\"]|[0-9]{1,20})"
)
_DOWNLOAD_LABEL_RE = re.compile(
    r"(?:^|\s)(?:원문(?:보기|저장|다운로드)?|pdf(?:\s*(?:보기|다운로드))?|"
    r"다운로드|download(?:\s*pdf)?|view\s*pdf|full\s*text)(?:$|\s)",
    re.IGNORECASE,
)
_CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "로봇이 아닙니다",
    "자동입력 방지",
    "자동 입력 방지",
)
_AUTH_MARKERS = (
    "로그인",
    "login",
    "인증키",
    "authentication key",
    "mfa",
    "일회용",
    "세션이 만료",
)
_VISIBLE_CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "[id*='captcha' i]",
    "[class*='captcha' i]",
)
_VISIBLE_AUTH_SELECTORS = (
    "input[type='password']",
    "input[name*='authKey' i]",
    "input[id*='authKey' i]",
    "[role='dialog'] input[name*='login' i]",
)
_IMMEDIATE_CIRCUIT_CODES = frozenset(
    {
        "authentication_required",
        "captcha_required",
        "browser_dependency_missing",
        "browser_not_installed",
        "browser_launch_failed",
        "invalid_browser_profile",
        "profile_in_use",
        "dom_changed",
        "ambiguous",
        "interactive_login_cancelled",
        "interactive_login_failed",
        "interactive_login_acknowledgement_rejected",
    }
)


@dataclass(frozen=True, slots=True)
class JamsArticleReference:
    """One exact detail reference, with transport fields hidden from repr."""

    article_id: str = field(repr=False)
    submission_count: str = field(repr=False)
    detail_url: str = field(repr=False)
    provenance: str
    author_verified_by_search: bool = False


@dataclass(frozen=True, slots=True)
class JamsBrowserConfig:
    """Explicit authority, private profile, and hard operation bounds."""

    permission_granted: bool = False
    profile_dir: Path | None = field(default=None, repr=False, compare=False)
    repository_root: Path | None = field(default=None, repr=False, compare=False)
    headless: bool = True
    interactive_login: bool = False
    interactive_input_fn: Callable[[str], str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    navigation_timeout_seconds: float = 60.0
    download_timeout_seconds: float = 90.0
    min_interval_seconds: float = 10.0
    max_attempts_per_run: int = 25
    max_title_queries: int = 3
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES
    max_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 2_000
    max_pdf_text_chars: int = 2_000_000


@dataclass(frozen=True, slots=True)
class JamsBrowserAttempt:
    """A URL-, file-id-, and authentication-material-free audit event."""

    stage: str
    status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class JamsBrowserResolution:
    """A safe result which cannot expose a transient URL or DOM handler."""

    status: str
    reason: str
    paper: PaperCandidate = field(repr=False)
    resolver: str = JAMS_BROWSER_RESOLVER_NAME
    content: bytes | None = field(default=None, repr=False)
    content_type: str | None = None
    matched_title: str | None = None
    matched_doi: str | None = None
    identity_provenance: tuple[str, ...] = ()
    attempts: tuple[JamsBrowserAttempt, ...] = ()
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.content is not None


@dataclass(frozen=True, slots=True)
class JamsBrowserControl:
    """A live DOM control descriptor retained only between checks and click."""

    dom_index: int = field(repr=False)
    tag: str = field(repr=False)
    href: str | None = field(default=None, repr=False)
    onclick: str | None = field(default=None, repr=False)
    label: str = field(default="", repr=False)
    visible: bool = False
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class JamsSearchSnapshot:
    final_url: str = field(repr=False)
    document: str = field(repr=False)
    http_status: int | None = None
    challenge: str | None = None


@dataclass(frozen=True, slots=True)
class JamsDetailSnapshot:
    final_url: str = field(repr=False)
    document: str = field(repr=False)
    controls: tuple[JamsBrowserControl, ...] = field(default=(), repr=False)
    http_status: int | None = None
    challenge: str | None = None


class JamsBrowserSession(Protocol):
    """Small dependency seam; unit tests never need Playwright or network."""

    def search_title(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_html_bytes: int,
    ) -> JamsSearchSnapshot:
        """Use the rendered official title-search form and return its DOM."""

    def open_detail(
        self,
        url: str,
        *,
        timeout_ms: int,
        max_html_bytes: int,
    ) -> JamsDetailSnapshot:
        """Open an already validated official detail URL."""

    def download_control(
        self,
        control: JamsBrowserControl,
        *,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        """Revalidate and click one control, returning a native download."""

    def close(self) -> None:
        """Close resources on the owner thread."""


class JamsBrowserOperationError(RuntimeError):
    """Sanitized failure which never embeds a URL, profile, or auth value."""

    def __init__(self, status: str, reason: str, error_code: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error_code = error_code


def default_jams_profile_dir() -> Path:
    """Return a dedicated profile location outside the repository."""

    configured = os.getenv("XDG_DATA_HOME")
    base = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else Path.home() / ".local" / "share"
    )
    return (base / "forensics-paper" / "kdfs-jams-browser-profile").resolve()


def parse_kdfs_detail_reference(
    url: object,
    *,
    provenance: str,
    author_verified_by_search: bool = False,
) -> JamsArticleReference:
    """Accept only the exact fixed KDFS detail endpoint and public identifiers."""

    if not isinstance(url, str) or not url or len(url) > 2_048:
        raise ValueError("invalid KDFS JAMS detail reference")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError("invalid KDFS JAMS detail reference") from exc
    if not (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == public_kdfs.KDFS_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == public_kdfs.KDFS_DETAIL_PATH
        and not parsed.fragment
        and pairs
        == [
            ("soceId", public_kdfs.KDFS_SOCE_ID),
            ("artiId", pairs[1][1] if len(pairs) == 4 else ""),
            ("sereId", public_kdfs.KDFS_SERE_ID),
            ("submCnt", pairs[3][1] if len(pairs) == 4 else ""),
        ]
    ):
        raise ValueError("invalid KDFS JAMS detail reference")
    article_id = pairs[1][1]
    submission_count = pairs[3][1]
    if (
        _DETAIL_ARTICLE_RE.fullmatch(article_id) is None
        or _SUBMISSION_COUNT_RE.fullmatch(submission_count) is None
    ):
        raise ValueError("invalid KDFS JAMS detail reference")
    canonical = (
        f"{public_kdfs.KDFS_ORIGIN}{public_kdfs.KDFS_DETAIL_PATH}?"
        + urlencode(
            {
                "soceId": public_kdfs.KDFS_SOCE_ID,
                "artiId": article_id,
                "sereId": public_kdfs.KDFS_SERE_ID,
                "submCnt": submission_count,
            }
        )
    )
    return JamsArticleReference(
        article_id=article_id,
        submission_count=submission_count,
        detail_url=canonical,
        provenance=provenance,
        author_verified_by_search=author_verified_by_search,
    )


def reference_from_public_resolution(
    paper: PaperCandidate,
    resolution: public_kdfs.KdfsResolution,
) -> JamsArticleReference:
    """Bind a detail reference to a successful exact public KDFS decision.

    The anonymous resolver intentionally returns this state when the exact
    detail page is valid but its file control is visible only after login.
    References from failed identity/detail checks are never accepted.
    """

    expected, eligibility = public_kdfs._expected_identity(paper)
    if eligibility is not None or expected is None:
        raise ValueError("paper is not an exact KDFS JAMS candidate")
    expected = _with_safe_title_variants(expected, paper)
    if not isinstance(resolution, public_kdfs.KdfsResolution):
        raise ValueError("invalid public KDFS resolution")
    observed = resolution.paper
    if (
        observed.database_id != paper.database_id
        or observed.source != paper.source
        or observed.source_id != paper.source_id
        or resolution.reason
        not in {
            "kdfs_anonymous_download_not_advertised",
            "official_kdfs_jams_exact_pdf_identity",
        }
        or resolution.detail_url is None
        or resolution.jams_article_id is None
        or normalize_title(resolution.matched_title) not in {
            normalized for _title, normalized in expected.titles
        }
        or resolution.expected_doi != expected.doi
        or resolution.expected_year != expected.year
        or resolution.expected_volume != expected.volume
        or resolution.expected_issue != expected.issue
        or resolution.expected_first_page != expected.first_page
        or resolution.expected_final_page != expected.final_page
    ):
        raise ValueError("public KDFS resolution is not identity-bound")
    reference = parse_kdfs_detail_reference(
        resolution.detail_url,
        provenance="public-kdfs-exact-detail",
        author_verified_by_search=True,
    )
    if reference.article_id != resolution.jams_article_id:
        raise ValueError("public KDFS resolution article identity changed")
    return reference


def validate_jams_profile_dir(
    profile_dir: Path,
    *,
    repository_root: Path | None,
    require_marker: bool = True,
) -> Path:
    """Validate separation, ownership, permissions, marker, and symlinks."""

    raw = Path(profile_dir).expanduser()
    if not raw.is_absolute():
        raise ValueError("JAMS browser profile path must be absolute")
    if raw.is_symlink():
        raise ValueError("JAMS browser profile must not be a symlink")
    resolved = raw.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError("JAMS browser profile path is too broad")
    if repository_root is not None and _is_relative_to(
        resolved, Path(repository_root).resolve()
    ):
        raise ValueError("JAMS browser profile must be outside the repository")
    if _looks_like_default_browser_profile(resolved):
        raise ValueError("default personal browser profiles are not supported")
    if not resolved.exists() or not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("JAMS browser profile has not been initialized")
    _require_private_owned_path(resolved, directory=True)
    marker = resolved / DEFAULT_PROFILE_MARKER
    if require_marker:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("JAMS browser profile marker is missing")
        _require_private_owned_path(marker, directory=False)
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("JAMS browser profile marker is invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != PROFILE_SCHEMA_VERSION
            or value.get("purpose") != PROFILE_PURPOSE
        ):
            raise ValueError("JAMS browser profile marker is invalid")
    return resolved


def initialize_jams_profile_dir(
    profile_dir: Path,
    *,
    repository_root: Path | None,
) -> Path:
    """Create or reopen one explicitly dedicated private Playwright profile."""

    raw = Path(profile_dir).expanduser()
    if not raw.is_absolute():
        raise ValueError("JAMS browser profile path must be absolute")
    if raw.is_symlink():
        raise ValueError("JAMS browser profile must not be a symlink")
    resolved = raw.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError("JAMS browser profile path is too broad")
    if repository_root is not None and _is_relative_to(
        resolved, Path(repository_root).resolve()
    ):
        raise ValueError("JAMS browser profile must be outside the repository")
    if _looks_like_default_browser_profile(resolved):
        raise ValueError("default personal browser profiles are not supported")
    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink():
            raise ValueError("JAMS browser profile path is not a safe directory")
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
        raise ValueError("JAMS browser profile marker must not be a symlink")
    if not marker.exists():
        write_json_atomic(
            marker,
            {
                "schema_version": PROFILE_SCHEMA_VERSION,
                "purpose": PROFILE_PURPOSE,
            },
        )
        os.chmod(marker, 0o600)
    return validate_jams_profile_dir(
        resolved,
        repository_root=repository_root,
        require_marker=True,
    )


def run_interactive_jams_login(
    profile_dir: Path,
    *,
    repository_root: Path | None,
    input_fn: Callable[[str], str] = input,
) -> None:
    """Open a headed profile for manual login/email-key/MFA completion."""

    validated = initialize_jams_profile_dir(
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
        except Exception as exc:
            code = _launch_error_code(exc)
            raise JamsBrowserOperationError("configuration_error", code, code) from None
        pages = context.pages
        page = pages[0] if pages else context.new_page()
        try:
            page.goto(KDFS_RENDERED_SEARCH_URL, wait_until="domcontentloaded")
            input_fn(
                "브라우저에서 KDFS JAMS 로그인을 직접 완료하세요. 이메일 인증키와 "
                "비밀번호는 이 프로그램에 입력하지 않습니다. 완료 후 이 터미널에서 "
                "Enter를 누르세요: "
            )
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception:
            raise JamsBrowserOperationError(
                "retryable_error",
                "jams_interactive_login_failed",
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


class AuthenticatedKdfsJamsBrowserResolver:
    """Download one exact KDFS article from a manually authenticated UI."""

    def __init__(
        self,
        config: JamsBrowserConfig,
        *,
        session: JamsBrowserSession | None = None,
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
        public_resolution: public_kdfs.KdfsResolution | None = None,
    ) -> JamsBrowserResolution:
        if not self.config.permission_granted:
            return self._failure(
                paper,
                "permission_required",
                "jams_browser_collection_disabled",
                "permission_required",
                stage="jams-browser-precheck",
            )
        expected, eligibility = public_kdfs._expected_identity(paper)
        if eligibility is not None:
            return self._failure(
                paper,
                str(eligibility["status"]),
                str(eligibility["reason"]),
                str(eligibility["error_code"]),
                stage="jams-browser-applicability",
            )
        assert expected is not None
        expected = _with_safe_title_variants(expected, paper)

        with self._lock:
            if self._circuit_error is not None:
                status, reason, error_code = self._circuit_error
                return self._failure(
                    paper,
                    status,
                    reason,
                    error_code,
                    stage="jams-browser-circuit",
                )
            if self._attempts_started >= self.config.max_attempts_per_run:
                return self._failure(
                    paper,
                    "retryable_error",
                    "jams_browser_attempt_budget_exhausted",
                    "browser_attempt_limit_reached",
                    stage="jams-browser-budget",
                )
            self._attempts_started += 1
            operation_stage = "jams-browser"
            attempts: list[JamsBrowserAttempt] = []
            try:
                session = self._get_session()
                if public_resolution is not None:
                    operation_stage = "jams-public-reference"
                    try:
                        reference = reference_from_public_resolution(
                            paper, public_resolution
                        )
                    except ValueError:
                        raise JamsBrowserOperationError(
                            "invalid_response",
                            "invalid_public_kdfs_detail_reference",
                            "identity_mismatch",
                        ) from None
                    attempts.append(
                        JamsBrowserAttempt("jams-public-reference", "validated")
                    )
                else:
                    operation_stage = "jams-browser-search"
                    reference = self._search_exact_reference(
                        session, expected
                    )
                    attempts.append(
                        JamsBrowserAttempt("jams-browser-search", "validated")
                    )

                self._wait_for_interval()
                operation_stage = "jams-browser-detail"
                snapshot = session.open_detail(
                    reference.detail_url,
                    timeout_ms=_seconds_to_ms(
                        self.config.navigation_timeout_seconds
                    ),
                    max_html_bytes=self.config.max_html_bytes,
                )
                _raise_snapshot_challenge(snapshot.challenge)
                failure = _page_http_failure(snapshot.http_status, stage="detail")
                if failure is not None:
                    raise failure
                try:
                    observed_reference = parse_kdfs_detail_reference(
                        snapshot.final_url,
                        provenance="rendered-detail",
                    )
                except ValueError:
                    if _is_authentication_url(snapshot.final_url):
                        raise JamsBrowserOperationError(
                            "authentication_required",
                            "jams_authentication_requires_manual_refresh",
                            "authentication_required",
                        ) from None
                    raise JamsBrowserOperationError(
                        "invalid_response",
                        "jams_session_left_exact_detail_page",
                        "unsafe_url",
                    ) from None
                if (
                    observed_reference.article_id != reference.article_id
                    or observed_reference.submission_count
                    != reference.submission_count
                ):
                    raise JamsBrowserOperationError(
                        "identity_mismatch",
                        "jams_detail_redirect_identity_mismatch",
                        "identity_mismatch",
                    )

                matched_title, detail_provenance = _validate_detail_identity(
                    snapshot.document,
                    expected,
                    year_verified_by_exact_search=True,
                    author_verified_by_exact_search=(
                        reference.author_verified_by_search
                    ),
                )
                control = _select_download_control(snapshot.controls)
                attempts.append(
                    JamsBrowserAttempt("jams-browser-detail", "validated")
                )
                operation_stage = "jams-browser-download"
                content = session.download_control(
                    control,
                    timeout_ms=_seconds_to_ms(
                        self.config.download_timeout_seconds
                    ),
                    max_pdf_bytes=self.config.max_pdf_bytes,
                )
                _validate_pdf_identity(
                    content,
                    expected,
                    max_pages=self.config.max_pdf_pages,
                    max_text_chars=self.config.max_pdf_text_chars,
                )
                attempts.append(
                    JamsBrowserAttempt("jams-browser-download", "downloaded")
                )
            except JamsBrowserOperationError as exc:
                self._record_operation_failure(
                    exc.status, exc.reason, exc.error_code
                )
                return self._failure(
                    paper,
                    exc.status,
                    exc.reason,
                    exc.error_code,
                    stage=operation_stage,
                    prior_attempts=attempts,
                )
            except Exception:
                self._record_operation_failure(
                    "retryable_error",
                    "jams_browser_operation_failed",
                    "browser_error",
                )
                return self._failure(
                    paper,
                    "retryable_error",
                    "jams_browser_operation_failed",
                    "browser_error",
                    stage=operation_stage,
                    prior_attempts=attempts,
                )

            self._consecutive_retryable_errors = 0
            return JamsBrowserResolution(
                status="resolved",
                reason="authenticated_kdfs_jams_exact_pdf_identity",
                paper=paper,
                content=content,
                content_type="application/pdf",
                matched_title=matched_title,
                matched_doi=expected.doi,
                identity_provenance=(
                    f"jams_detail_reference_{reference.provenance}",
                    *detail_provenance,
                    "jams_download_control_exactly_one",
                    "native_browser_download_event",
                    "kdfs_pdf_identity_exact",
                ),
                attempts=tuple(attempts),
            )

    def _search_exact_reference(
        self,
        session: JamsBrowserSession,
        expected: Any,
    ) -> JamsArticleReference:
        matches: dict[tuple[str, str], JamsArticleReference] = {}
        for title, _normalized in expected.titles[: self.config.max_title_queries]:
            self._wait_for_interval()
            snapshot = session.search_title(
                title,
                timeout_ms=_seconds_to_ms(
                    self.config.navigation_timeout_seconds
                ),
                max_html_bytes=self.config.max_html_bytes,
            )
            _raise_snapshot_challenge(snapshot.challenge)
            failure = _page_http_failure(snapshot.http_status, stage="search")
            if failure is not None:
                raise failure
            if not _is_exact_search_result_url(snapshot.final_url):
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_search_left_official_form",
                    "unsafe_url",
                )
            parser = public_kdfs._SearchRowsParser()
            try:
                parser.feed(snapshot.document)
                parser.close()
            except (AssertionError, ValueError):
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_search_dom_changed",
                    "dom_changed",
                ) from None
            match, author_missing = _select_rendered_search_match(
                parser.rows,
                expected,
            )
            if match is None:
                continue
            try:
                reference = parse_kdfs_detail_reference(
                    match.detail_url,
                    provenance=(
                        "rendered-search-exact-detail-author"
                        if author_missing
                        else "rendered-search-exact"
                    ),
                    author_verified_by_search=not author_missing,
                )
            except ValueError:
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_search_advertised_unsafe_detail",
                    "unsafe_url",
                ) from None
            matches[(reference.article_id, reference.submission_count)] = reference
            if len(matches) > 1:
                raise JamsBrowserOperationError(
                    "ambiguous",
                    "multiple_exact_jams_search_results",
                    "ambiguous",
                )
            # One rendered row has already supplied the exact core identity.
            # If its author field was explicitly blank, the subsequent exact
            # detail check supplies that missing independent evidence.
            break
        if not matches:
            raise JamsBrowserOperationError(
                "not_found",
                "exact_jams_search_result_not_found",
                "not_found",
            )
        return next(iter(matches.values()))

    def _get_session(self) -> JamsBrowserSession:
        if self._session is not None:
            return self._session
        assert self.config.profile_dir is not None
        try:
            profile = validate_jams_profile_dir(
                self.config.profile_dir,
                repository_root=self.config.repository_root,
            )
        except (OSError, ValueError):
            raise JamsBrowserOperationError(
                "configuration_error",
                "invalid_jams_browser_profile",
                "invalid_browser_profile",
            ) from None
        self._session = _PlaywrightJamsSession(self.config, profile)
        return self._session

    def _wait_for_interval(self) -> None:
        now = self._clock()
        if now < self._next_allowed:
            self._sleep(self._next_allowed - now)
            now = self._clock()
        self._next_allowed = max(now, self._next_allowed) + self.config.min_interval_seconds

    def _record_operation_failure(
        self,
        status: str,
        reason: str,
        error_code: str,
    ) -> None:
        if error_code in _IMMEDIATE_CIRCUIT_CODES or reason == "jams_article_rate_limited":
            self._circuit_error = (status, reason, error_code)
            return
        if status != "retryable_error":
            self._consecutive_retryable_errors = 0
            return
        self._consecutive_retryable_errors += 1
        if self._consecutive_retryable_errors >= TRANSIENT_CIRCUIT_THRESHOLD:
            self._circuit_error = (
                "retryable_error",
                "jams_browser_transient_circuit_open",
                "consecutive_retryable_failures",
            )

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    @staticmethod
    def _failure(
        paper: PaperCandidate,
        status: str,
        reason: str,
        error_code: str,
        *,
        stage: str,
        prior_attempts: list[JamsBrowserAttempt] | None = None,
    ) -> JamsBrowserResolution:
        attempts = list(prior_attempts or ())
        attempts.append(JamsBrowserAttempt(stage, "failed", error_code))
        return JamsBrowserResolution(
            status=status,
            reason=reason,
            error_code=error_code,
            paper=paper,
            attempts=tuple(attempts),
        )


class _PlaywrightJamsSession:
    """Lazy synchronous adapter restricted to the official KDFS JAMS host."""

    def __init__(self, config: JamsBrowserConfig, profile_dir: Path) -> None:
        self._config = config
        self._profile_dir = profile_dir
        self._profile_lock = _ProfileLock(profile_dir)
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._owner_thread: int | None = None
        self._dialog_error: JamsBrowserOperationError | None = None
        self._blocked_document = False
        self._interactive_login_completed = False

    def search_title(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_html_bytes: int,
    ) -> JamsSearchSnapshot:
        self._require_owner_thread()
        self._ensure_started()
        assert self._page is not None
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise JamsBrowserOperationError(
                "missing_metadata", "invalid_jams_search_title", "missing_metadata"
            )
        self._blocked_document = False
        self._dialog_error = None
        try:
            response = self._page.goto(
                KDFS_RENDERED_SEARCH_URL,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self._raise_http_failure(response, stage="search")
            self._raise_current_challenge()
            inputs = self._page.locator("input[name='keywordList']")
            visible_inputs = [
                inputs.nth(index)
                for index in range(min(inputs.count(), 20))
                if inputs.nth(index).is_visible()
            ]
            if len(visible_inputs) != 1:
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_search_dom_changed",
                    "dom_changed",
                )
            fields = self._page.locator("select[name='fieldList']")
            visible_fields = [
                fields.nth(index)
                for index in range(min(fields.count(), 20))
                if fields.nth(index).is_visible()
            ]
            if len(visible_fields) > 1:
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_search_dom_changed",
                    "dom_changed",
                )
            if visible_fields:
                visible_fields[0].select_option("title_idx", timeout=timeout_ms)
            search_input = visible_inputs[0]
            search_input.fill(title.strip(), timeout=timeout_ms)
            forms = self._page.locator(_SEARCH_FORM_SELECTOR)
            if forms.count() != 1:
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_search_dom_changed",
                    "dom_changed",
                )
            # JAMS renders goSearch() immediately outside the form element in
            # the final DOM even though it submits #poSearchVo.  Bind the
            # operation to the unique validated form/input above and then
            # select the unique page control with the exact official handler.
            controls = self._page.locator(
                "button[onclick],input[onclick],a[onclick]"
            )
            submit_controls: list[Any] = []
            for index in range(min(controls.count(), 100)):
                control = controls.nth(index)
                if not control.is_visible() or not control.is_enabled():
                    continue
                tag = str(
                    control.evaluate("element => element.tagName.toLowerCase()")
                )
                label = _plain_text(
                    " ".join(
                        (
                            control.get_attribute("aria-label") or "",
                            control.get_attribute("title") or "",
                            control.get_attribute("value") or "",
                            control.inner_text() if tag in {"a", "button"} else "",
                        )
                    )
                )
                if _is_exact_search_submit_control(
                    tag=tag,
                    label=label,
                    onclick=control.get_attribute("onclick"),
                ):
                    submit_controls.append(control)
            if len(submit_controls) != 1:
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_search_dom_changed",
                    "dom_changed",
                )
            # Enter does not submit JAMS' JavaScript-backed paper-search form;
            # it leaves the default 436-result page in place.  Click only the
            # one control that advertises the exact goSearch() handler
            # observed beside the already validated official form.
            submit_controls[0].click(timeout=timeout_ms)
            self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            self._raise_current_challenge()
            document = self._bounded_page_content(max_html_bytes)
            return JamsSearchSnapshot(
                final_url=self._page.url,
                document=document,
                http_status=None,
            )
        except JamsBrowserOperationError:
            raise
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            if self._blocked_document:
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_search_left_official_host",
                    "unsafe_url",
                ) from None
            challenge = self._inspect_current_challenge()
            if challenge is not None:
                raise challenge from None
            raise JamsBrowserOperationError(
                "retryable_error",
                "jams_rendered_search_failed",
                "browser_error",
            ) from None

    def open_detail(
        self,
        url: str,
        *,
        timeout_ms: int,
        max_html_bytes: int,
    ) -> JamsDetailSnapshot:
        self._require_owner_thread()
        self._ensure_started()
        assert self._page is not None
        try:
            parse_kdfs_detail_reference(url, provenance="requested-detail")
        except ValueError:
            raise JamsBrowserOperationError(
                "invalid_response", "unsafe_jams_detail_reference", "unsafe_url"
            ) from None
        self._blocked_document = False
        self._dialog_error = None
        try:
            response = self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self._raise_http_failure(response, stage="detail")
            self._raise_current_challenge()
            document = self._bounded_page_content(max_html_bytes)
            controls = self._collect_controls()
            return JamsDetailSnapshot(
                final_url=self._page.url,
                document=document,
                controls=controls,
                http_status=None,
            )
        except JamsBrowserOperationError:
            raise
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            if self._blocked_document:
                raise JamsBrowserOperationError(
                    "authentication_required",
                    "jams_detail_redirected_outside_official_host",
                    "authentication_required",
                ) from None
            challenge = self._inspect_current_challenge()
            if challenge is not None:
                raise challenge from None
            raise JamsBrowserOperationError(
                "retryable_error", "jams_detail_navigation_failed", "browser_error"
            ) from None

    def download_control(
        self,
        control: JamsBrowserControl,
        *,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        self._require_owner_thread()
        assert self._page is not None
        controls = self._page.locator("a,button,input")
        if control.dom_index < 0 or control.dom_index >= controls.count():
            raise JamsBrowserOperationError(
                "invalid_response", "jams_download_control_changed", "dom_changed"
            )
        locator = controls.nth(control.dom_index)
        observed = self._snapshot_control(locator, control.dom_index)
        if observed != control or not _is_advertised_download_control(observed):
            raise JamsBrowserOperationError(
                "invalid_response", "jams_download_control_changed", "dom_changed"
            )
        self._blocked_document = False
        self._dialog_error = None
        try:
            self._raise_current_challenge()
            with self._page.expect_download(timeout=timeout_ms) as download_info:
                locator.click(timeout=timeout_ms)
            download = download_info.value
        except JamsBrowserOperationError:
            raise
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            challenge = self._inspect_current_challenge()
            if challenge is not None:
                raise challenge from None
            raise JamsBrowserOperationError(
                "retryable_error",
                "jams_native_download_not_emitted",
                "download_timeout",
            ) from None
        try:
            if not _is_exact_kdfs_download_url(download.url):
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "unexpected_jams_download_origin",
                    "unsafe_url",
                )
            if download.failure() is not None:
                raise JamsBrowserOperationError(
                    "retryable_error", "jams_native_download_failed", "browser_error"
                )
            path_value = download.path()
            if path_value is None:
                raise JamsBrowserOperationError(
                    "retryable_error", "jams_native_download_missing", "browser_error"
                )
            content = _read_download_without_symlink(Path(path_value), max_pdf_bytes)
            if not content.startswith(b"%PDF-"):
                raise JamsBrowserOperationError(
                    "invalid_pdf", "jams_download_is_not_pdf", "invalid_pdf"
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
        self._interactive_login_completed = False
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
                headless=(
                    False
                    if self._config.interactive_login
                    else self._config.headless
                ),
                accept_downloads=True,
                service_workers="block",
                args=(
                    ["--restore-last-session"]
                    if self._config.interactive_login
                    else []
                ),
            )
            pages = context.pages
            page = pages[0] if pages else context.new_page()
            page.on("dialog", self._handle_dialog)
            self._playwright = playwright
            self._context = context
            self._page = page
            self._owner_thread = threading.get_ident()
            # JAMS' manual email-key/login flow may use authentication
            # services outside the society subdomain.  Applying the
            # collection allowlist before the operator finishes that flow
            # makes otherwise legitimate authentication controls inert.
            # Keep the headed, operator-controlled login unrestricted, then
            # install the strict KDFS-only route on this same context before
            # any automated search/detail/download operation begins.
            if not self._config.interactive_login:
                context.route("**/*", self._route_official_only)
            self._perform_interactive_login_once()
            if self._config.interactive_login:
                context.route("**/*", self._route_official_only)
        except JamsBrowserOperationError:
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if playwright is not None:
                with contextlib.suppress(Exception):
                    playwright.stop()
            self._context = None
            self._page = None
            self._playwright = None
            self._owner_thread = None
            self._interactive_login_completed = False
            self._profile_lock.release()
            raise
        except Exception as exc:
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            if playwright is not None:
                with contextlib.suppress(Exception):
                    playwright.stop()
            self._context = None
            self._page = None
            self._playwright = None
            self._owner_thread = None
            self._interactive_login_completed = False
            self._profile_lock.release()
            code = _launch_error_code(exc)
            raise JamsBrowserOperationError("configuration_error", code, code) from None

    def _perform_interactive_login_once(self) -> None:
        """Pause once while the operator authenticates in this live context.

        Authentication material is entered only into the headed browser.  The
        terminal callback accepts an empty Enter acknowledgement and rejects
        every non-empty value without logging or persisting it.  The context,
        page, profile lock, and browser-managed cookies remain the exact same
        objects subsequently used for search/detail/download operations.
        """

        if (
            not self._config.interactive_login
            or self._interactive_login_completed
        ):
            return
        assert self._page is not None
        try:
            response = self._page.goto(
                KDFS_RENDERED_SEARCH_URL,
                wait_until="domcontentloaded",
                timeout=_seconds_to_ms(
                    self._config.navigation_timeout_seconds
                ),
            )
            self._raise_http_failure(response, stage="interactive-login")
            if not _is_official_kdfs_url(self._page.url):
                raise JamsBrowserOperationError(
                    "invalid_response",
                    "jams_interactive_login_left_official_host",
                    "unsafe_url",
                )
            input_fn = self._config.interactive_input_fn or input
            acknowledgement = input_fn(
                "열린 브라우저에서만 KDFS JAMS 로그인과 이메일 인증을 "
                "완료하세요. 비밀번호·인증키·쿠키를 이 터미널에 입력하지 "
                "마십시오. 완료 후 아무 값도 입력하지 말고 Enter만 누르세요: "
            )
        except JamsBrowserOperationError:
            raise
        except (KeyboardInterrupt, EOFError):
            raise JamsBrowserOperationError(
                "retryable_error",
                "jams_interactive_login_cancelled",
                "interactive_login_cancelled",
            ) from None
        except Exception:
            raise JamsBrowserOperationError(
                "retryable_error",
                "jams_interactive_login_failed",
                "interactive_login_failed",
            ) from None
        if not isinstance(acknowledgement, str) or acknowledgement != "":
            # Never include the supplied value in the exception, logs, or
            # resolver state.  Only a bare Enter is an accepted signal.
            del acknowledgement
            raise JamsBrowserOperationError(
                "configuration_error",
                "jams_interactive_login_requires_empty_enter",
                "interactive_login_acknowledgement_rejected",
            )
        del acknowledgement
        self._interactive_login_completed = True

    def _collect_controls(self) -> tuple[JamsBrowserControl, ...]:
        assert self._page is not None
        controls = self._page.locator("a,button,input")
        count = controls.count()
        if count > MAX_DOM_CONTROLS:
            raise JamsBrowserOperationError(
                "invalid_response", "too_many_jams_controls", "dom_changed"
            )
        return tuple(
            observed
            for index in range(count)
            if _is_potential_download_control(
                observed := self._snapshot_control(controls.nth(index), index)
            )
        )

    @staticmethod
    def _snapshot_control(locator: Any, index: int) -> JamsBrowserControl:
        tag = str(locator.evaluate("element => element.tagName.toLowerCase()"))
        label_parts = (
            locator.get_attribute("aria-label") or "",
            locator.get_attribute("title") or "",
            locator.get_attribute("value") or "",
            locator.inner_text() if tag in {"a", "button"} else "",
        )
        return JamsBrowserControl(
            dom_index=index,
            tag=tag,
            href=(locator.get_attribute("href") or "").strip() or None,
            onclick=(locator.get_attribute("onclick") or "").strip() or None,
            label=_plain_text(" ".join(label_parts)),
            visible=bool(locator.is_visible()),
            enabled=bool(locator.is_enabled()),
        )

    def _bounded_page_content(self, max_bytes: int) -> str:
        assert self._page is not None
        document = self._page.content()
        if len(document.encode("utf-8")) > max_bytes:
            raise JamsBrowserOperationError(
                "invalid_response", "jams_page_too_large", "too_large"
            )
        return document

    def _raise_current_challenge(self) -> None:
        challenge = self._inspect_current_challenge()
        if challenge is not None:
            raise challenge

    def _inspect_current_challenge(self) -> JamsBrowserOperationError | None:
        assert self._page is not None
        for selectors, status, reason, code in (
            (
                _VISIBLE_CAPTCHA_SELECTORS,
                "captcha_required",
                "jams_captcha_requires_manual_action",
                "captcha_required",
            ),
            (
                _VISIBLE_AUTH_SELECTORS,
                "authentication_required",
                "jams_authentication_requires_manual_refresh",
                "authentication_required",
            ),
        ):
            for selector in selectors:
                try:
                    values = self._page.locator(selector)
                    if any(
                        values.nth(index).is_visible()
                        for index in range(min(values.count(), 10))
                    ):
                        return JamsBrowserOperationError(status, reason, code)
                except Exception:
                    continue
        if _is_authentication_url(self._page.url):
            return JamsBrowserOperationError(
                "authentication_required",
                "jams_authentication_requires_manual_refresh",
                "authentication_required",
            )
        return None

    @staticmethod
    def _raise_http_failure(response: Any, *, stage: str) -> None:
        status = int(response.status) if response is not None else None
        failure = _page_http_failure(status, stage=stage)
        if failure is not None:
            raise failure

    def _route_official_only(self, route: Any) -> None:
        request = route.request
        if _is_official_kdfs_url(request.url):
            route.continue_()
            return
        if bool(request.is_navigation_request()) and request.resource_type == "document":
            self._blocked_document = True
        route.abort(error_code="blockedbyclient")

    def _handle_dialog(self, dialog: Any) -> None:
        message = str(dialog.message or "").casefold()
        if any(marker in message for marker in _CAPTCHA_MARKERS):
            self._dialog_error = JamsBrowserOperationError(
                "captcha_required",
                "jams_captcha_requires_manual_action",
                "captcha_required",
            )
        elif any(marker in message for marker in _AUTH_MARKERS):
            self._dialog_error = JamsBrowserOperationError(
                "authentication_required",
                "jams_authentication_requires_manual_refresh",
                "authentication_required",
            )
        else:
            self._dialog_error = JamsBrowserOperationError(
                "access_denied", "jams_download_dialog_rejected", "access_denied"
            )
        with contextlib.suppress(Exception):
            dialog.dismiss()

    def _require_owner_thread(self) -> None:
        if self._owner_thread is not None and self._owner_thread != threading.get_ident():
            raise RuntimeError("JAMS browser session used from a different thread")


class _ProfileLock:
    def __init__(self, profile_dir: Path) -> None:
        self._path = profile_dir / ".forensics-paper-session.lock"
        self._handle: Any = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        if self._path.is_symlink():
            raise JamsBrowserOperationError(
                "configuration_error", "invalid_jams_browser_profile", "invalid_browser_profile"
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
            raise JamsBrowserOperationError(
                "configuration_error", "jams_browser_profile_in_use", "profile_in_use"
            ) from None
        except OSError:
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.close()
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise JamsBrowserOperationError(
                "configuration_error", "invalid_jams_browser_profile", "invalid_browser_profile"
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


def _validate_config(config: JamsBrowserConfig) -> None:
    if config.permission_granted and config.profile_dir is None:
        raise ValueError("profile_dir is required for JAMS browser download")
    if not isinstance(config.interactive_login, bool):
        raise ValueError("interactive_login must be a boolean")
    if (
        config.interactive_input_fn is not None
        and not callable(config.interactive_input_fn)
    ):
        raise ValueError("interactive_input_fn must be callable")
    for name, value in (
        ("navigation_timeout_seconds", config.navigation_timeout_seconds),
        ("download_timeout_seconds", config.download_timeout_seconds),
        ("min_interval_seconds", config.min_interval_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be positive")
    for name in ("max_html_bytes", "max_pdf_bytes", "max_pdf_pages", "max_pdf_text_chars"):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if (
        isinstance(config.max_attempts_per_run, bool)
        or not isinstance(config.max_attempts_per_run, int)
        or not 1 <= config.max_attempts_per_run <= 1_000
    ):
        raise ValueError("max_attempts_per_run must be between 1 and 1000")
    if (
        isinstance(config.max_title_queries, bool)
        or not isinstance(config.max_title_queries, int)
        or not 1 <= config.max_title_queries <= 10
    ):
        raise ValueError("max_title_queries must be between 1 and 10")


def _with_safe_title_variants(expected: Any, paper: PaperCandidate) -> Any:
    """Add only provable adjacent-duplicate repairs to the exact title set.

    One KCI row currently repeats the complete Korean title twice with no
    separator.  This is not fuzzy matching: a variant is admitted only when
    the NFKC-normalized source string is exactly ``half + half``.  The detail
    page must still satisfy the full independent bibliography chain.
    """

    variants: list[tuple[str, str]] = []
    observed_normalized: set[str] = set()
    for title, normalized in expected.titles:
        if normalized not in observed_normalized:
            variants.append((title, normalized))
            observed_normalized.add(normalized)
        value = unicodedata.normalize("NFKC", title).strip()
        if len(value) < 16 or len(value) % 2:
            continue
        midpoint = len(value) // 2
        if value[:midpoint] != value[midpoint:]:
            continue
        repaired = value[:midpoint].strip()
        repaired_normalized = normalize_title(repaired)
        if (
            repaired_normalized is not None
            and repaired_normalized not in observed_normalized
        ):
            variants.append((repaired, repaired_normalized))
            observed_normalized.add(repaired_normalized)
    if tuple(variants) == expected.titles:
        return expected
    return replace(expected, titles=tuple(variants))


def _select_rendered_search_match(
    rows: tuple[Any, ...] | list[Any],
    expected: Any,
) -> tuple[Any | None, bool]:
    """Select one exact row, deferring only an explicitly blank author.

    Some official JAMS search rows render the ``저자`` label with no value.
    In that single case the row may shortlist an article when title, KDFS
    identifiers, journal, year, volume/issue, and pages are all exact.  The
    authenticated detail page must then provide the exact first author before
    a download control can be clicked.  A non-empty conflicting author is
    never deferred.
    """

    known_titles = {normalized: title for title, normalized in expected.titles}
    valid: dict[str, tuple[Any, bool]] = {}
    saw_title = False
    saw_mismatch = False
    for row in rows:
        for href, label in row.anchors:
            action = public_kdfs._DETAIL_ACTION_RE.fullmatch(
                html.unescape(href).strip()
            )
            if action is None:
                continue
            candidate_title = public_kdfs._LEADING_RESULT_NUMBER_RE.sub(
                "", label
            ).strip()
            normalized = normalize_title(candidate_title)
            if normalized not in known_titles:
                continue
            saw_title = True
            if (
                action.group("soce") != public_kdfs.KDFS_SOCE_ID
                or action.group("sere") != public_kdfs.KDFS_SERE_ID
                or _DETAIL_ARTICLE_RE.fullmatch(action.group("article")) is None
                or _SUBMISSION_COUNT_RE.fullmatch(action.group("count")) is None
            ):
                saw_mismatch = True
                continue
            token = public_kdfs._identity_token(row.text)
            core_exact = bool(
                public_kdfs._identity_token(public_kdfs.KDFS_JOURNAL) in token
                and public_kdfs._year_present(row.text, expected.year)
                and public_kdfs._volume_issue_present(
                    row.text, expected.volume, expected.issue
                )
                and public_kdfs._page_range_present(
                    row.text, expected.first_page, expected.final_page
                )
            )
            if not core_exact:
                saw_mismatch = True
                continue
            author_exact = public_kdfs._identity_token(expected.authors[0]) in token
            author_missing = not author_exact and _search_row_has_blank_author(
                row.text
            )
            if not author_exact and not author_missing:
                saw_mismatch = True
                continue
            detail_url = public_kdfs._detail_url(
                article_id=action.group("article"),
                submission_count=action.group("count"),
            )
            valid[detail_url] = (
                public_kdfs._SearchMatch(
                    title=known_titles[normalized],
                    article_id=action.group("article"),
                    submission_count=action.group("count"),
                    detail_url=detail_url,
                    controls=row.controls,
                ),
                author_missing,
            )

    if len(valid) == 1:
        return next(iter(valid.values()))
    if len(valid) > 1:
        raise JamsBrowserOperationError(
            "ambiguous",
            "multiple_exact_kdfs_article_matches",
            "ambiguous",
        )
    if saw_title or saw_mismatch:
        raise JamsBrowserOperationError(
            "identity_mismatch",
            "kdfs_search_bibliographic_identity_mismatch",
            "identity_mismatch",
        )
    return None, False


def _search_row_has_blank_author(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text)
    match = re.search(
        r"(?:^|\s)저자\s*(?P<value>.*?)\s*학술지(?:명)?(?:\s|$)",
        normalized,
        re.DOTALL,
    )
    return bool(
        match is not None
        and not public_kdfs._identity_token(match.group("value"))
    )


def _is_exact_search_submit_control(
    *,
    tag: str,
    label: str,
    onclick: str | None,
) -> bool:
    if tag not in {"a", "button", "input"} or onclick is None:
        return False
    label_tokens = [token for token in label.split() if token]
    return bool(
        label_tokens
        and set(label_tokens) == {"검색"}
        and _SEARCH_SUBMIT_HANDLER_RE.fullmatch(html.unescape(onclick).strip())
    )


def _validate_detail_identity(
    document: str,
    expected: Any,
    *,
    year_verified_by_exact_search: bool = False,
    author_verified_by_exact_search: bool = False,
) -> tuple[str, tuple[str, ...]]:
    parser = public_kdfs._DetailParser(document)
    try:
        parser.feed(document)
        parser.close()
    except (AssertionError, ValueError):
        raise JamsBrowserOperationError(
            "invalid_response", "jams_detail_dom_changed", "dom_changed"
        ) from None
    detail = parser.document()
    known_titles = {normalized: title for title, normalized in expected.titles}
    matched_title: str | None = None
    for paragraph in detail.paragraphs:
        normalized = normalize_title(public_kdfs._strip_detail_label(paragraph))
        if normalized in known_titles:
            matched_title = known_titles[normalized]
            break
    if matched_title is None:
        raise JamsBrowserOperationError(
            "identity_mismatch", "jams_detail_title_mismatch", "identity_mismatch"
        )

    raw_text = html.unescape(document)
    if expected.doi is not None and expected.doi.casefold() in public_kdfs._normalized_dois(raw_text):
        return matched_title, ("jams_detail_title_exact", "jams_detail_doi_exact")

    info = detail.info_text
    token = public_kdfs._identity_token(info)
    year_exact = public_kdfs._year_present(info, expected.year)
    if year_exact:
        year_provenance = "jams_detail_year_exact"
    else:
        observed_years = {
            int(value)
            for value in re.findall(
                r"(?<![0-9])((?:19|20)[0-9]{2}|2100)(?![0-9])",
                unicodedata.normalize("NFKC", info),
            )
        }
        if observed_years or not year_verified_by_exact_search:
            raise JamsBrowserOperationError(
                "identity_mismatch",
                "jams_detail_year_mismatch",
                "identity_mismatch",
            )
        year_provenance = "jams_detail_year_from_exact_search"
    checks = (
        (public_kdfs._identity_token(public_kdfs.KDFS_JOURNAL) in token, "journal"),
        (
            public_kdfs._volume_issue_present(info, expected.volume, expected.issue),
            "volume_issue",
        ),
        (
            public_kdfs._page_range_present(info, expected.first_page, expected.final_page),
            "page_range",
        ),
    )
    failed = next((name for passed, name in checks if not passed), None)
    if failed is not None:
        raise JamsBrowserOperationError(
            "identity_mismatch",
            f"jams_detail_{failed}_mismatch",
            "identity_mismatch",
        )
    expected_author = public_kdfs._identity_token(expected.authors[0])
    if expected_author in token:
        author_provenance = "jams_detail_author_exact"
    else:
        author_paragraphs = tuple(
            paragraph
            for paragraph in detail.paragraphs
            if re.match(r"\s*저자(?:\s|:|：|$)", paragraph)
        )
        explicitly_blank = bool(
            len(author_paragraphs) == 1
            and not public_kdfs._identity_token(
                re.sub(
                    r"^\s*저자\s*(?::|：)?\s*",
                    "",
                    author_paragraphs[0],
                )
            )
        )
        if not explicitly_blank:
            raise JamsBrowserOperationError(
                "identity_mismatch",
                "jams_detail_author_mismatch",
                "identity_mismatch",
            )
        author_provenance = (
            "jams_detail_author_from_exact_search"
            if author_verified_by_exact_search
            else "jams_detail_author_deferred_to_pdf"
        )
    return matched_title, (
        "jams_detail_title_exact",
        "jams_detail_journal_exact",
        year_provenance,
        "jams_detail_volume_issue_exact",
        "jams_detail_page_range_exact",
        author_provenance,
    )


def _validate_pdf_identity(
    content: bytes,
    expected: Any,
    *,
    max_pages: int,
    max_text_chars: int,
) -> None:
    if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
        raise JamsBrowserOperationError(
            "invalid_pdf", "jams_download_is_not_pdf", "invalid_pdf"
        )
    try:
        document = public_kdfs._read_pdf_document(
            content,
            max_pages=max_pages,
            max_text_chars=max_text_chars,
        )
    except ValueError:
        raise JamsBrowserOperationError(
            "invalid_pdf", "jams_attachment_is_not_a_safe_parseable_pdf", "invalid_pdf"
        ) from None
    failure = public_kdfs._pdf_identity_failure(document, expected)
    if failure is not None:
        raise JamsBrowserOperationError(
            failure.status, failure.reason, failure.error_code
        )


def _select_download_control(
    controls: tuple[JamsBrowserControl, ...],
) -> JamsBrowserControl:
    suspicious_invalid = any(
        _is_potential_download_control(control)
        and not _is_advertised_download_control(control)
        for control in controls
    )
    candidates = [
        control
        for control in controls
        if control.visible and control.enabled and _is_advertised_download_control(control)
    ]
    if len(candidates) > 1:
        raise JamsBrowserOperationError(
            "ambiguous", "multiple_jams_download_controls", "ambiguous"
        )
    if suspicious_invalid:
        raise JamsBrowserOperationError(
            "invalid_response", "unsafe_jams_download_control", "dom_changed"
        )
    if not candidates:
        raise JamsBrowserOperationError(
            "authentication_required",
            "jams_authenticated_download_control_unavailable",
            "authentication_required",
        )
    return candidates[0]


def _is_potential_download_control(control: JamsBrowserControl) -> bool:
    combined = " ".join(
        value for value in (control.href, control.onclick, control.label) if value
    ).casefold()
    return any(
        marker in combined
        for marker in (
            "fncfileiframe",
            "fncfileidchk",
            "podownload.kci",
            "원문",
            "pdf",
            "다운로드",
            "download",
            "full text",
        )
    )


def _is_advertised_download_control(control: JamsBrowserControl) -> bool:
    if control.tag not in {"a", "button", "input"}:
        return False
    onclick_handler = _parse_download_handler(control.onclick)
    href_handler = _parse_download_handler(control.href)
    handler_ok = onclick_handler is not None or href_handler is not None
    href_ok = control.href is not None and _is_exact_kdfs_download_url(control.href)
    label_ok = _download_label_present(control.label)
    if not (handler_ok or href_ok or label_ok):
        return False
    if control.onclick is not None and onclick_handler is None:
        return False
    if (
        control.href is not None
        and not href_ok
        and href_handler is None
        and not _is_inert_control_href(control.href)
    ):
        return False
    return True


def _parse_download_handler(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    raw = html.unescape(value).strip()
    iframe = _IFRAME_HANDLER_RE.fullmatch(raw)
    if iframe is not None:
        return ("fncFileIframe", iframe.group("file").upper())
    checked = _FILE_CHECK_HANDLER_RE.fullmatch(raw)
    if checked is None:
        return None
    argument_text = checked.group("arguments").strip()
    if not argument_text:
        return None
    parts = [part.strip() for part in argument_text.split(",")]
    if not 1 <= len(parts) <= 8 or any(
        _SAFE_HANDLER_ARGUMENT_RE.fullmatch(part) is None for part in parts
    ):
        return None
    return ("fncFileIdChk", *parts)


def _download_label_present(value: str) -> bool:
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", value).strip()
    return _DOWNLOAD_LABEL_RE.search(normalized) is not None


def _is_inert_control_href(value: str) -> bool:
    return value.strip().casefold() in {
        "#",
        "javascript:;",
        "javascript:void(0)",
        "javascript:void(0);",
    }


def _is_exact_kdfs_download_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    raw = url.strip()
    candidate = (
        urljoin(public_kdfs.KDFS_ORIGIN, raw)
        if raw.startswith("/") and not raw.startswith("//")
        else raw
    )
    return public_kdfs._is_exact_official_url(candidate, kind="download")


def _is_official_kdfs_url(url: object) -> bool:
    if not isinstance(url, str) or not url or len(url) > 8_192:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == public_kdfs.KDFS_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _is_exact_search_result_url(url: object) -> bool:
    if not _is_official_kdfs_url(url):
        return False
    assert isinstance(url, str)
    try:
        parsed = urlsplit(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    return bool(
        (parsed.path == public_kdfs.KDFS_SEARCH_PATH and not pairs and not parsed.fragment)
        or (
            parsed.path == _SEARCH_MENU_PATH
            and len(pairs) == len(_SEARCH_MENU_PAIRS)
            and len({name for name, _value in pairs}) == len(pairs)
            and dict(pairs) == dict(_SEARCH_MENU_PAIRS)
            and not parsed.fragment
        )
    )


def _is_authentication_url(url: object) -> bool:
    if not _is_official_kdfs_url(url):
        return False
    assert isinstance(url, str)
    path = urlsplit(url).path.casefold()
    return any(marker in path for marker in ("/login", "/signin", "/auth"))


def _raise_snapshot_challenge(challenge: str | None) -> None:
    if challenge is None:
        return
    if challenge == "captcha_required":
        raise JamsBrowserOperationError(
            "captcha_required", "jams_captcha_requires_manual_action", "captcha_required"
        )
    if challenge == "authentication_required":
        raise JamsBrowserOperationError(
            "authentication_required",
            "jams_authentication_requires_manual_refresh",
            "authentication_required",
        )
    raise JamsBrowserOperationError(
        "invalid_response", "unknown_jams_browser_challenge", "dom_changed"
    )


def _page_http_failure(
    status: int | None,
    *,
    stage: str,
) -> JamsBrowserOperationError | None:
    if status is None or 200 <= status < 400:
        return None
    if status == 401:
        return JamsBrowserOperationError(
            "authentication_required",
            f"jams_{stage}_authentication_required",
            "authentication_required",
        )
    if status == 403:
        return JamsBrowserOperationError(
            "access_denied", f"jams_{stage}_access_denied", "access_denied"
        )
    if status == 429:
        return JamsBrowserOperationError(
            "retryable_error", "jams_article_rate_limited", "retryable_error"
        )
    if status in {408, 425} or 500 <= status <= 599:
        return JamsBrowserOperationError(
            "retryable_error",
            f"jams_{stage}_retryable_http_error",
            "retryable_error",
        )
    if status in {404, 410}:
        return JamsBrowserOperationError(
            "not_found", f"jams_{stage}_not_found", "not_found"
        )
    return JamsBrowserOperationError(
        "invalid_response", f"jams_{stage}_http_error", "http_status"
    )


def _read_download_without_symlink(path: Path, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise JamsBrowserOperationError(
            "invalid_pdf", "jams_download_file_unsafe", "invalid_pdf"
        ) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise JamsBrowserOperationError(
                "invalid_pdf", "jams_download_file_unsafe", "invalid_pdf"
            )
        if info.st_size > max_bytes:
            raise JamsBrowserOperationError(
                "too_large", "jams_pdf_exceeds_size_limit", "too_large"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(content) > max_bytes:
        raise JamsBrowserOperationError(
            "too_large", "jams_pdf_exceeds_size_limit", "too_large"
        )
    return content


def _configure_user_playwright_libraries() -> None:
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


def _load_sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise JamsBrowserOperationError(
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
        raise ValueError("JAMS browser profile must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("JAMS browser profile permissions are too broad")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError("JAMS browser profile is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError("JAMS browser profile marker is not a regular file")


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
    return any(
        normalized.endswith(marker) or f"{marker}/" in normalized
        for marker in markers
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _plain_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


def _seconds_to_ms(value: float) -> int:
    return max(1, int(value * 1_000))
