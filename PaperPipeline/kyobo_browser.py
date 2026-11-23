"""Opt-in Kyobo Scholar downloads through its rendered, authenticated UI.

This module is intentionally narrow.  It handles one identity-bound
``PaperCandidate`` at a time, uses a dedicated Playwright profile, and clicks
the visible official ``.down`` (원문저장) control.  It never constructs or
replays ``/file/downs`` requests, article-file identifiers, cookies, or signed
URLs.  Only the browser's native download event is consumed, and transport
URLs are not representable in the returned audit objects.

The operator must have permission to access and download every requested
article and remains responsible for the applicable subscription terms.  The
resolver is disabled unless ``permission_granted`` is explicitly enabled.
CAPTCHAs, authentication prompts, access dialogs, ambiguous results, and DOM
changes stop collection instead of being bypassed.
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
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlsplit

from PaperPipeline.db_source import InvalidDoiError, normalize_doi
from PaperPipeline.identifier_resolver import normalize_title, paper_title_queries
from PaperPipeline.records import PaperCandidate
from PaperPipeline.storage import write_json_atomic


KYOBO_BROWSER_RESOLVER_NAME = "kyobo-authenticated-browser"
KYOBO_HOME_URL = "https://scholar.kyobobook.co.kr/main"
KYOBO_LOGIN_URL = "https://scholar.kyobobook.co.kr/user/loginView"
DEFAULT_PROFILE_MARKER = ".forensics-paper-kyobo-profile.json"
PROFILE_SCHEMA_VERSION = 1
PROFILE_PURPOSE = "forensics-paper-kyobo-scholar-browser"
DEFAULT_MAX_HTML_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_PDF_BYTES = 50 * 1024 * 1024
TRANSIENT_CIRCUIT_THRESHOLD = 3

_KYOBO_HOST = "scholar.kyobobook.co.kr"
_KYOBO_CONTENT_HOST = "contents.kyobobook.co.kr"
_DOI_HOST = "doi.org"
_ARTICLE_PATH_RE = re.compile(
    r"\A/article/detail/(?P<article>[1-9][0-9]{7,19})\Z",
    re.IGNORECASE,
)
_ARTICLE_CODE_RE = re.compile(r"\A[1-9][0-9]{7,19}\Z")
_YEAR_RE = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")
_ISSUE_ID_RE = re.compile(
    r"\A(?P<volume>[0-9]{1,6})(?:_(?P<issue>[0-9]{1,6}))?\Z"
)
_PAGE_RE = re.compile(r"\A[0-9A-Za-z가-힣.-]{1,32}\Z")
_SEARCH_QUERY_MAX_CHARS = 1_000
_MAX_DOWNLOAD_CONTROLS = 20

_SEARCH_TARGET_SELECTOR = "#searchTarget"
_SEARCH_INPUT_SELECTOR = "#mainSearch"
_SEARCH_SUBMIT_SELECTOR = "#mainSearchBtn"
_SEARCH_CARD_SELECTOR = "#SEARCH_BODY_ID .i_item"
_SEARCH_READY_SELECTOR = (
    f"{_SEARCH_CARD_SELECTOR}:visible, .nodata_type02:visible"
)
_SEARCH_TITLE_SELECTOR = ".prdTitle > a"
_SEARCH_CODE_SELECTOR = "input[name='checks'], input#articleBuy"
# The article page duplicates the save action in a floating responsive footer.
# Bind only the desktop article-detail action inside ARTICLE_ID; this is the
# same visible official control, while retaining ambiguity checks within that
# narrow container.
_DOWNLOAD_SELECTOR = (
    "#ARTICLE_ID .prdDetail_type01 .rBox.window_box a.down"
)

_CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "로봇이 아닙니다",
    "자동입력 방지",
    "자동 입력 방지",
)
_AUTH_MARKERS = (
    "로그인이 필요",
    "로그인 후",
    "login required",
    "authentication required",
    "기관 인증",
    "인증이 필요",
    "세션이 만료",
)
_ACCESS_MARKERS = (
    "구매하기",
    "구독이 필요",
    "이용 권한",
    "원문 이용",
    "access denied",
    "subscription required",
)
_DIALOG_SELECTORS = (
    "[role='dialog']:visible",
    ".popup_container:visible",
    ".popup_wrap:visible",
    ".layer_popup:visible",
    ".layerPopup:visible",
    ".modal:visible",
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
_IMMEDIATE_CIRCUIT_REASONS = frozenset({"kyobo_article_rate_limited"})
_BIBLIOGRAPHY_CORE_PROVENANCE = frozenset(
    {
        "kyobo_landing_journal_exact",
        "kyobo_landing_authors_exact",
    }
)
# Volume/issue evidence: exact metadata, or a landing that publishes none at all
# and is bound by the exact page range instead.
_BIBLIOGRAPHY_VOLUME_PROVENANCE = frozenset(
    {
        "kyobo_landing_volume_issue_exact",
        "kyobo_landing_volume_issue_absent_page_range_exact",
    }
)
_BIBLIOGRAPHY_PUBLISHER_PROVENANCE = frozenset(
    {
        "kyobo_landing_publisher_exact",
        "kyobo_landing_publisher_qualified_generic_exact",
        "kyobo_landing_publisher_legal_form_exact",
    }
)
_BIBLIOGRAPHY_PAGE_PROVENANCE = frozenset(
    {
        "kyobo_landing_page_range_exact",
        "kyobo_landing_final_page_off_by_one",
    }
)


@dataclass(frozen=True, slots=True)
class KyoboBrowserConfig:
    """Explicit authorization and conservative browser-operation bounds."""

    permission_granted: bool = False
    profile_dir: Path | None = field(default=None, repr=False, compare=False)
    repository_root: Path | None = field(default=None, repr=False, compare=False)
    headless: bool = True
    navigation_timeout_seconds: float = 60.0
    download_timeout_seconds: float = 90.0
    min_interval_seconds: float = 10.0
    max_attempts_per_run: int = 25
    max_search_results: int = 50
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES
    max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES


@dataclass(frozen=True, slots=True)
class KyoboBrowserAttempt:
    """A URL-, cookie-, token-, and credential-free audit event."""

    stage: str
    status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class KyoboBrowserResolution:
    """A resolution whose sensitive browser transport cannot be serialized."""

    status: str
    reason: str
    paper: PaperCandidate
    resolver: str = KYOBO_BROWSER_RESOLVER_NAME
    content: bytes | None = field(default=None, repr=False)
    content_type: str | None = None
    matched_title: str | None = None
    matched_doi: str | None = None
    landing_first_page: str | None = None
    landing_final_page: str | None = None
    landing_page_count: int | None = None
    identity_provenance: tuple[str, ...] = ()
    attempts: tuple[KyoboBrowserAttempt, ...] = ()
    error_code: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.content is not None


@dataclass(frozen=True, slots=True)
class KyoboBrowserSearchResult:
    """One rendered search row and its official article-page reference."""

    title: str
    article_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class KyoboDownloadControl:
    """One rendered ``a.down`` control; no file identifier is retained."""

    dom_index: int = field(repr=False)
    class_tokens: tuple[str, ...]
    label: str
    data_lang: str | None
    visible: bool
    enabled: bool


@dataclass(frozen=True, slots=True)
class KyoboPageSnapshot:
    """The bounded identity document and download controls for one article."""

    final_url: str = field(repr=False)
    document: str = field(repr=False)
    controls: tuple[KyoboDownloadControl, ...] = field(repr=False)
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class KyoboLandingIdentity:
    title: str
    doi: str | None
    year: int | None
    first_page: str | None
    final_page: str | None
    page_count: int | None
    provenance: tuple[str, ...]


class KyoboBrowserSession(Protocol):
    """Small dependency-injection seam; Playwright is optional at import."""

    def discover_by_doi(
        self,
        doi: str,
        *,
        timeout_ms: int,
    ) -> str | None:
        """Return an exact Kyobo article page or ``None`` for another host."""

    def search_articles(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_results: int,
    ) -> tuple[KyoboBrowserSearchResult, ...]:
        """Use the rendered title-search form and return its visible rows."""

    def open_article(self, url: str, *, timeout_ms: int) -> KyoboPageSnapshot:
        """Open one already-discovered exact official article page."""

    def download_control(
        self,
        control: KyoboDownloadControl,
        *,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        """Click that same native control and consume its download event."""

    def close(self) -> None:
        """Close browser resources on their owning thread."""


class KyoboBrowserOperationError(RuntimeError):
    """Sanitized browser failure that never embeds URLs or local paths."""

    def __init__(self, status: str, reason: str, error_code: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error_code = error_code


class KyoboLandingIdentityError(RuntimeError):
    """Sanitized identity failure raised by the pure landing validator."""

    def __init__(self, status: str, reason: str, error_code: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error_code = error_code


def default_kyobo_profile_dir() -> Path:
    """Return a dedicated profile path outside the source repository."""

    configured = os.getenv("XDG_DATA_HOME")
    base = (
        Path(configured).expanduser()
        if configured and configured.strip()
        else Path.home() / ".local" / "share"
    )
    return (base / "forensics-paper" / "kyobo-scholar-browser-profile").resolve()


def normalize_kyobo_article_url(value: object) -> str | None:
    """Accept only the canonical HTTPS Kyobo article-detail URL shape."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 2_048 or any(ord(char) < 0x20 for char in raw):
        return None
    if raw.startswith("/") and not raw.startswith("//"):
        raw = urljoin(KYOBO_HOME_URL, raw)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    match = _ARTICLE_PATH_RE.fullmatch(parsed.path)
    if not (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == _KYOBO_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and match is not None
    ):
        return None
    return f"https://{_KYOBO_HOST}/article/detail/{match.group('article')}"


def is_exact_kyobo_article_url(value: object) -> bool:
    return normalize_kyobo_article_url(value) is not None


def validate_kyobo_profile_dir(
    profile_dir: Path,
    *,
    repository_root: Path | None,
    require_marker: bool = True,
) -> Path:
    """Require a private, owned, marked profile outside the repository."""

    raw = Path(profile_dir).expanduser()
    if not raw.is_absolute():
        raise ValueError("Kyobo browser profile path must be absolute")
    if raw.is_symlink():
        raise ValueError("Kyobo browser profile must not be a symlink")
    resolved = raw.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError("Kyobo browser profile path is too broad")
    if repository_root is not None and _is_relative_to(
        resolved,
        Path(repository_root).resolve(),
    ):
        raise ValueError("Kyobo browser profile must be outside the repository")
    if _looks_like_default_browser_profile(resolved):
        raise ValueError("default personal browser profiles are not supported")
    if not resolved.exists() or not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("Kyobo browser profile has not been initialized")
    _require_private_owned_path(resolved, directory=True)

    marker = resolved / DEFAULT_PROFILE_MARKER
    if require_marker:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("Kyobo browser profile marker is missing")
        _require_private_owned_path(marker, directory=False)
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Kyobo browser profile marker is invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != PROFILE_SCHEMA_VERSION
            or value.get("purpose") != PROFILE_PURPOSE
            or set(value) != {"schema_version", "purpose"}
        ):
            raise ValueError("Kyobo browser profile marker is invalid")
    return resolved


def initialize_kyobo_profile_dir(
    profile_dir: Path,
    *,
    repository_root: Path | None,
) -> Path:
    """Create or reopen one explicitly dedicated Playwright profile."""

    raw = Path(profile_dir).expanduser()
    if not raw.is_absolute():
        raise ValueError("Kyobo browser profile path must be absolute")
    if raw.is_symlink():
        raise ValueError("Kyobo browser profile must not be a symlink")
    resolved = raw.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError("Kyobo browser profile path is too broad")
    if repository_root is not None and _is_relative_to(
        resolved,
        Path(repository_root).resolve(),
    ):
        raise ValueError("Kyobo browser profile must be outside the repository")
    if _looks_like_default_browser_profile(resolved):
        raise ValueError("default personal browser profiles are not supported")

    marker = resolved / DEFAULT_PROFILE_MARKER
    if resolved.exists():
        if not resolved.is_dir() or resolved.is_symlink():
            raise ValueError("Kyobo browser profile path is not a safe directory")
        existing = [child for child in resolved.iterdir() if child.name != marker.name]
        if existing and not marker.is_file():
            raise ValueError("refusing to reuse an unmarked browser profile")
    else:
        resolved.mkdir(parents=True, mode=0o700)
    os.chmod(resolved, 0o700)
    _require_private_owned_path(resolved, directory=True)

    if marker.is_symlink():
        raise ValueError("Kyobo browser profile marker must not be a symlink")
    if not marker.exists():
        write_json_atomic(
            marker,
            {
                "schema_version": PROFILE_SCHEMA_VERSION,
                "purpose": PROFILE_PURPOSE,
            },
        )
        os.chmod(marker, 0o600)
    return validate_kyobo_profile_dir(
        resolved,
        repository_root=repository_root,
        require_marker=True,
    )


def run_interactive_kyobo_login(
    profile_dir: Path,
    *,
    repository_root: Path | None,
    input_fn: Callable[[str], str] = input,
) -> None:
    """Open the dedicated profile headed; authentication stays manual."""

    validated = initialize_kyobo_profile_dir(
        profile_dir,
        repository_root=repository_root,
    )
    lock = _KyoboProfileLock(validated)
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
            raise KyoboBrowserOperationError(
                "configuration_error",
                code,
                code,
            ) from None
        pages = context.pages
        page = pages[0] if pages else context.new_page()
        page.goto(KYOBO_HOME_URL, wait_until="domcontentloaded")
        input_fn(
            "브라우저에서 교보 Scholar/기관 인증을 직접 완료한 뒤 이 "
            "터미널에서 Enter를 누르세요. 비밀번호는 이 프로그램에 "
            "입력하지 않습니다: "
        )
    except (KeyboardInterrupt, EOFError):
        raise
    except KyoboBrowserOperationError:
        raise
    except Exception:
        raise KyoboBrowserOperationError(
            "retryable_error",
            "kyobo_interactive_login_failed",
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


class _KyoboMetadataParser(HTMLParser):
    """Extract only bounded citation metadata used for identity binding."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.canonical_urls: list[str] = []
        self._capture_id: str | None = None
        self._capture_depth = 0
        self._capture_parts: list[str] = []
        self.element_text: dict[str, list[str]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {name.casefold(): value for name, value in attrs}
        if tag.casefold() == "meta":
            name = (values.get("name") or "").strip().casefold()
            content = (values.get("content") or "").strip()
            if name.startswith("citation_") and content:
                self.meta.setdefault(name, []).append(content)
        elif tag.casefold() == "link":
            rel = (values.get("rel") or "").strip().casefold().split()
            href = (values.get("href") or "").strip()
            if "canonical" in rel and href:
                self.canonical_urls.append(href)

        element_id = (values.get("id") or "").strip()
        if self._capture_id is None and element_id in {
            "academyTitle",
            "journalTitle",
            "volumeTitle",
            "pubTitle",
        }:
            self._capture_id = element_id
            self._capture_depth = 1
            self._capture_parts = []
        elif self._capture_id is not None:
            self._capture_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if self._capture_id is not None:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._capture_id is not None:
            self._capture_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        del tag
        if self._capture_id is None:
            return
        self._capture_depth -= 1
        if self._capture_depth == 0:
            value = " ".join("".join(self._capture_parts).split())
            if value:
                self.element_text.setdefault(self._capture_id, []).append(value)
            self._capture_id = None
            self._capture_parts = []

    def values(self, name: str) -> tuple[str, ...]:
        return tuple(self.meta.get(name.casefold(), ()))


def validate_kyobo_landing_document(
    document: str,
    paper: PaperCandidate,
    *,
    article_url: str,
) -> KyoboLandingIdentity:
    """Bind a rendered Kyobo landing page to exactly one candidate.

    DOI-bearing rows require exact normalized title and DOI.  Every supplied
    year and usable bibliographic field must also agree.  DOI-less rows are
    accepted only with title, year, publisher, journal, volume/issue, pages,
    and complete author-set evidence.
    """

    if not isinstance(document, str) or not document:
        raise KyoboLandingIdentityError(
            "invalid_response",
            "empty_kyobo_landing_document",
            "invalid_html",
        )
    if len(document.encode("utf-8")) > DEFAULT_MAX_HTML_BYTES * 4:
        # The runtime adapter applies the configured tighter bound.  This pure
        # guard prevents accidental unbounded standalone parser use.
        raise KyoboLandingIdentityError(
            "invalid_response",
            "kyobo_landing_document_too_large",
            "too_large",
        )
    canonical_article = normalize_kyobo_article_url(article_url)
    if canonical_article is None:
        raise KyoboLandingIdentityError(
            "invalid_response",
            "unsafe_kyobo_article_reference",
            "unsafe_url",
        )

    parser = _KyoboMetadataParser()
    try:
        parser.feed(document)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise KyoboLandingIdentityError(
            "invalid_response",
            "invalid_kyobo_landing_html",
            "invalid_html",
        ) from exc

    canonical_urls = {
        normalized
        for raw in parser.canonical_urls
        if (normalized := normalize_kyobo_article_url(raw)) is not None
    }
    if len(parser.canonical_urls) != 1 or canonical_urls != {canonical_article}:
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "kyobo_landing_canonical_mismatch",
            "identity_mismatch",
        )

    known_titles = {
        normalized
        for _raw, normalized in paper_title_queries(paper)
        if normalized
    }
    title_values = _unique_plain_values(parser.values("citation_title"))
    normalized_titles = {
        normalized
        for value in title_values
        if (normalized := normalize_title(value)) is not None
    }
    if not known_titles:
        raise KyoboLandingIdentityError(
            "missing_metadata",
            "missing_known_title",
            "missing_metadata",
        )
    if len(normalized_titles) != 1 or not title_values:
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "kyobo_landing_title_mismatch",
            "identity_mismatch",
        )
    landing_normalized_title = next(iter(normalized_titles))
    matched_title = sorted(title_values, key=lambda value: (len(value), value))[0]
    title_normalized_exact = landing_normalized_title in known_titles
    provenance: list[str] = []

    expected_doi = _paper_doi(paper)
    landing_dois: set[str] = set()
    raw_landing_dois = _unique_plain_values(parser.values("citation_doi"))
    malformed_landing_doi = False
    for value in raw_landing_dois:
        try:
            normalized = normalize_doi(value)
        except InvalidDoiError:
            if expected_doi is not None:
                raise KyoboLandingIdentityError(
                    "invalid_response",
                    "invalid_kyobo_landing_doi",
                    "invalid_doi",
                ) from None
            malformed_landing_doi = True
            continue
        if normalized:
            landing_dois.add(normalized.casefold())
    if (
        len(raw_landing_dois) > 1
        or len(landing_dois) > 1
        or (landing_dois and malformed_landing_doi)
    ):
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "ambiguous_kyobo_landing_doi",
            "ambiguous",
        )
    matched_doi = next(iter(landing_dois), None)
    doi_exact = False
    if expected_doi is not None:
        if matched_doi != expected_doi:
            raise KyoboLandingIdentityError(
                "identity_mismatch",
                "kyobo_landing_doi_mismatch",
                "identity_mismatch",
            )
        doi_exact = True
        provenance.append("kyobo_landing_doi_exact")
    elif matched_doi is not None:
        # KCI occasionally omits a DOI that is present on the exact publisher
        # article page.  Do not mutate the KCI record or treat this value as an
        # independently resolved DOI.  It is retained only as one part of the
        # complete publisher bibliography below.
        provenance.append("kyobo_landing_doi_unique_observed")

    publisher_doi_evidence = bool(doi_exact or matched_doi is not None)

    years = {
        int(match.group(0))
        for value in parser.values("citation_publication_date")
        for match in _YEAR_RE.finditer(value)
    }
    if len(years) > 1:
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "ambiguous_kyobo_landing_year",
            "ambiguous",
        )
    landing_year = next(iter(years), None)
    year_exact = False
    if paper.publication_year is not None:
        if landing_year != paper.publication_year:
            raise KyoboLandingIdentityError(
                "identity_mismatch",
                "kyobo_landing_year_mismatch",
                "identity_mismatch",
            )
        year_exact = True
        provenance.append("kyobo_landing_year_exact")

    bibliography = _validate_kyobo_bibliography(
        parser,
        paper,
        publisher_doi_evidence=publisher_doi_evidence,
    )
    provenance.extend(bibliography)
    complete_bibliographic_chain = _has_complete_kyobo_bibliography(
        tuple(bibliography)
    )
    exceptional_chain_ready = bool(
        publisher_doi_evidence
        and year_exact
        and complete_bibliographic_chain
    )
    if (
        "kyobo_landing_publisher_qualified_generic_exact" in bibliography
        and not exceptional_chain_ready
    ):
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "kyobo_landing_publisher_mismatch",
            "identity_mismatch",
        )
    if (
        "kyobo_landing_publisher_legal_form_exact" in bibliography
        and not exceptional_chain_ready
    ):
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "kyobo_landing_publisher_mismatch",
            "identity_mismatch",
        )
    if (
        "kyobo_landing_final_page_off_by_one" in bibliography
        and not exceptional_chain_ready
    ):
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "kyobo_landing_page_range_mismatch",
            "identity_mismatch",
        )
    compact_title_exact = _compact_identity_title(
        landing_normalized_title
    ) in {
        _compact_identity_title(title)
        for title in known_titles
    }
    main_title_exact = landing_normalized_title in (
        _kyobo_main_title_variants(paper_title_queries(paper))
    )
    bibliographic_title_equivalent = _bibliographically_equivalent_title(
        landing_normalized_title,
        known_titles,
    )
    if (
        _authors_use_affiliation_annotations(paper.authors)
        and not (
            exceptional_chain_ready
            or (
                expected_doi is None
                and (title_normalized_exact or compact_title_exact)
                and year_exact
                and complete_bibliographic_chain
                and "kyobo_landing_publisher_exact" in bibliography
                and "kyobo_landing_page_range_exact" in bibliography
            )
        )
    ):
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "kyobo_landing_authors_mismatch",
            "identity_mismatch",
        )
    if malformed_landing_doi:
        if not (
            expected_doi is None
            and title_normalized_exact
            and year_exact
            and complete_bibliographic_chain
            and "kyobo_landing_publisher_exact" in bibliography
            and "kyobo_landing_page_range_exact" in bibliography
        ):
            raise KyoboLandingIdentityError(
                "invalid_response",
                "invalid_kyobo_landing_doi",
                "invalid_doi",
            )
        # Do not repair or adopt the malformed value.  The DOI-less identity
        # is bound solely by the otherwise exact canonical bibliography.
        provenance.append("kyobo_landing_malformed_doi_ignored")
    exact_canonical_bibliographic_chain = bool(
        year_exact
        and complete_bibliographic_chain
        and "kyobo_landing_publisher_exact" in bibliography
        and "kyobo_landing_page_range_exact" in bibliography
    )
    if title_normalized_exact:
        provenance.insert(0, "kyobo_landing_title_normalized_exact")
    elif compact_title_exact and exact_canonical_bibliographic_chain:
        provenance.insert(0, "kyobo_landing_title_compact_exact")
    elif (
        publisher_doi_evidence
        and year_exact
        and complete_bibliographic_chain
        and (
            compact_title_exact
            or main_title_exact
            or bibliographic_title_equivalent
        )
    ):
        provenance.insert(
            0,
            (
                "kyobo_landing_title_compact_exact"
                if compact_title_exact
                else "kyobo_landing_title_doi_bibliographic_equivalent"
                if doi_exact and bibliographic_title_equivalent
                else "kyobo_landing_title_main_exact"
                if main_title_exact
                else "kyobo_landing_title_doi_bibliographic_equivalent"
            ),
        )
    else:
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "kyobo_landing_title_mismatch",
            "identity_mismatch",
        )
    if expected_doi is None:
        if matched_doi is not None:
            # A DOI observed only on the publisher page is corroborating
            # evidence, not an independent match to KCI.  Permit at most one
            # substantive KCI/publisher variance; punctuation-only compact
            # title equality is not counted as a variance.
            provider_variances = {
                name
                for name, present in (
                    (
                        "title",
                        provenance[0]
                        in {
                            "kyobo_landing_title_main_exact",
                            "kyobo_landing_title_doi_bibliographic_equivalent",
                        },
                    ),
                    (
                        "publisher",
                        "kyobo_landing_publisher_qualified_generic_exact"
                        in provenance,
                    ),
                    (
                        "pages",
                        "kyobo_landing_final_page_off_by_one" in provenance,
                    ),
                )
                if present
            }
            if len(provider_variances) > 1:
                raise KyoboLandingIdentityError(
                    "identity_mismatch",
                    "kyobo_landing_multiple_bibliographic_variances",
                    "identity_mismatch",
                )
        required_core = {
            "kyobo_landing_year_exact",
            "kyobo_landing_journal_exact",
            "kyobo_landing_volume_issue_exact",
            "kyobo_landing_authors_exact",
        }
        publisher_evidence = {
            "kyobo_landing_publisher_exact",
            "kyobo_landing_publisher_qualified_generic_exact",
        }
        page_evidence = {
            "kyobo_landing_page_range_exact",
            "kyobo_landing_final_page_off_by_one",
        }
        complete_provider_doi_chain = bool(
            matched_doi is not None
            and "kyobo_landing_doi_unique_observed" in provenance
            and required_core.issubset(provenance)
            and publisher_evidence.intersection(provenance)
            and page_evidence.intersection(provenance)
        )
        # The strict DOI-less chain may stand on a landing that publishes no
        # volume/issue metadata: every other field, including both page
        # numbers, must then be exact (never the off-by-one page allowance).
        volume_absent_core = (required_core - {"kyobo_landing_volume_issue_exact"}) | {
            "kyobo_landing_volume_issue_absent_page_range_exact"
        }
        strict_doi_less_chain = bool(
            (required_core.issubset(provenance) or volume_absent_core.issubset(provenance))
            and "kyobo_landing_publisher_exact" in provenance
            and "kyobo_landing_page_range_exact" in provenance
        )
        if not (complete_provider_doi_chain or strict_doi_less_chain):
            raise KyoboLandingIdentityError(
                "missing_metadata",
                "missing_kyobo_doi_less_bibliographic_identity",
                "missing_metadata",
            )
        if complete_provider_doi_chain:
            provenance.append(
                "kyobo_landing_provider_doi_bibliographic_identity"
            )

    landing_first_page, landing_final_page = _landing_page_range(parser)
    landing_page_count = _inclusive_numeric_page_count(
        landing_first_page,
        landing_final_page,
    )

    return KyoboLandingIdentity(
        title=matched_title,
        doi=matched_doi,
        year=landing_year,
        first_page=landing_first_page,
        final_page=landing_final_page,
        page_count=landing_page_count,
        provenance=tuple(provenance),
    )


def _validate_kyobo_bibliography(
    parser: _KyoboMetadataParser,
    paper: PaperCandidate,
    *,
    publisher_doi_evidence: bool,
) -> tuple[str, ...]:
    provenance: list[str] = []

    expected_journal = normalize_title(paper.journal_id or "")
    if expected_journal:
        observed = _normalized_identity_values(
            (*parser.values("citation_journal_title"), *parser.element_text.get("journalTitle", ()))
        )
        if observed != {expected_journal}:
            raise KyoboLandingIdentityError(
                "identity_mismatch",
                "kyobo_landing_journal_mismatch",
                "identity_mismatch",
            )
        provenance.append("kyobo_landing_journal_exact")

    expected_publisher = normalize_title(paper.institution_id or "")
    if expected_publisher:
        observed = _normalized_identity_values(
            parser.element_text.get("academyTitle", ())
        )
        if observed == {expected_publisher}:
            provenance.append("kyobo_landing_publisher_exact")
        elif (
            publisher_doi_evidence
            and len(observed) == 1
            and _without_legal_form(paper.institution_id or "")
            == _without_legal_form(next(iter(parser.element_text.get("academyTitle", ())), ""))
            != ""
        ):
            # KCI and the publisher platform disagree only on a legal-form prefix
            # ("사단법인 한국국가법학회" vs "한국국가법학회").  The organisation name
            # itself is still compared exactly, and only after the landing DOI matched.
            provenance.append("kyobo_landing_publisher_legal_form_exact")
        elif (
            publisher_doi_evidence
            and expected_publisher == normalize_title("법학연구소")
            and len(observed) == 1
            and next(iter(observed)).endswith(expected_publisher)
        ):
            # KCI sometimes stores only the generic institute type while the
            # publisher landing page qualifies it with a university/journal
            # name.  This suffix evidence is never enough for DOI-less rows;
            # it is accepted only after the landing DOI matched exactly.
            provenance.append(
                "kyobo_landing_publisher_qualified_generic_exact"
            )
        else:
            raise KyoboLandingIdentityError(
                "identity_mismatch",
                "kyobo_landing_publisher_mismatch",
                "identity_mismatch",
            )

    volume_issue_absent = False
    expected_issue = _parse_issue_id(paper.issue_id)
    if paper.issue_id and expected_issue is None:
        raise KyoboLandingIdentityError(
            "missing_metadata",
            "invalid_kyobo_candidate_issue_id",
            "missing_metadata",
        )
    if expected_issue is not None:
        volumes = _unique_digits(parser.values("citation_volume"))
        issues = _unique_digits(parser.values("citation_issue"))
        expected_volume, expected_number = expected_issue
        if expected_number is not None:
            exact = volumes == {expected_volume} and issues == {expected_number}
        else:
            exact = bool(
                (volumes == {expected_volume} and not issues)
                or (issues == {expected_volume} and not volumes)
            )
        if exact:
            provenance.append("kyobo_landing_volume_issue_exact")
        elif not volumes and not issues:
            # Some landing pages publish no volume/issue metadata at all.  That is
            # missing evidence, not contradicting evidence: the exact title and
            # journal checked above must then be joined by an exact page range.
            volume_issue_absent = True
        else:
            raise KyoboLandingIdentityError(
                "identity_mismatch",
                "kyobo_landing_volume_issue_mismatch",
                "identity_mismatch",
            )

    first_page = _normalized_page(paper.first_page)
    final_page = _normalized_page(paper.final_page)
    if first_page or final_page:
        observed_first = {
            value
            for raw in parser.values("citation_firstpage")
            if (value := _normalized_page(raw)) is not None
        }
        observed_last = {
            value
            for raw in parser.values("citation_lastpage")
            if (value := _normalized_page(raw)) is not None
        }
        exact_first = first_page is not None and observed_first == {first_page}
        exact_last = final_page is not None and observed_last == {final_page}
        if exact_first and exact_last:
            provenance.append("kyobo_landing_page_range_exact")
            if volume_issue_absent:
                provenance.append("kyobo_landing_volume_issue_absent_page_range_exact")
                volume_issue_absent = False
        elif (
            publisher_doi_evidence
            and not volume_issue_absent
            and exact_first
            and final_page is not None
            and final_page.isdigit()
            and len(observed_last) == 1
            and next(iter(observed_last)).isdigit()
            and abs(int(next(iter(observed_last))) - int(final_page)) == 1
        ):
            # A small number of KCI rows count the terminal page one past the
            # publisher's citation metadata.  Preserve the disagreement as
            # provenance and permit only the exact-DOI, exact-first-page case.
            provenance.append("kyobo_landing_final_page_off_by_one")
        else:
            raise KyoboLandingIdentityError(
                "identity_mismatch",
                "kyobo_landing_page_range_mismatch",
                "identity_mismatch",
            )

    if volume_issue_absent:
        # No volume/issue on the landing and no exact page range to stand in for it.
        raise KyoboLandingIdentityError(
            "identity_mismatch",
            "kyobo_landing_volume_issue_mismatch",
            "identity_mismatch",
        )

    expected_authors = _normalized_author_set(paper.authors)
    if paper.authors and expected_authors is None:
        raise KyoboLandingIdentityError(
            "missing_metadata",
            "invalid_kyobo_candidate_authors",
            "missing_metadata",
        )
    if expected_authors:
        observed_authors = _normalized_author_set(
            parser.values("citation_author")
        )
        if observed_authors != expected_authors:
            raise KyoboLandingIdentityError(
                "identity_mismatch",
                "kyobo_landing_authors_mismatch",
                "identity_mismatch",
            )
        provenance.append("kyobo_landing_authors_exact")
    return tuple(provenance)


_LEGAL_FORM_PREFIX_RE = re.compile(r"^\s*(?:\(\s*[사재학]\s*\)|사단법인|재단법인|학교법인)\s*")


def _without_legal_form(value: str) -> str:
    """Organisation name without a leading legal-form marker, normalised for comparison."""

    return normalize_title(_LEGAL_FORM_PREFIX_RE.sub("", value or "", count=1))


class AuthenticatedKyoboBrowserResolver:
    """Resolve one exact paper through Kyobo's official rendered controls."""

    def __init__(
        self,
        config: KyoboBrowserConfig,
        *,
        session: KyoboBrowserSession | None = None,
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
    ) -> KyoboBrowserResolution:
        """Attempt exactly one candidate, with no cross-paper browser state."""

        if not self.config.permission_granted:
            return self._failure(
                paper,
                "permission_required",
                "kyobo_browser_collection_disabled",
                "permission_required",
            )
        known_titles = paper_title_queries(paper)
        if not known_titles:
            return self._failure(
                paper,
                "missing_metadata",
                "missing_kyobo_search_title",
                "missing_metadata",
                stage="browser-precheck",
            )
        expected_doi: str | None
        try:
            expected_doi = _paper_doi(paper)
        except KyoboLandingIdentityError as exc:
            return self._failure(
                paper,
                exc.status,
                exc.reason,
                exc.error_code,
                stage="browser-precheck",
            )
        article_url: str | None = None
        if landing_url is not None:
            article_url = normalize_kyobo_article_url(landing_url)
            if article_url is None:
                return self._failure(
                    paper,
                    "invalid_response",
                    "unsafe_kyobo_article_reference",
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
                    "kyobo_browser_attempt_budget_exhausted",
                    "browser_attempt_limit_reached",
                    stage="browser-budget",
                )
            self._attempts_started += 1
            operation_stage = "kyobo-browser"
            attempts: list[KyoboBrowserAttempt] = []
            discovery = "supplied"
            try:
                session = self._get_session()
                if article_url is None and expected_doi is not None:
                    operation_stage = "kyobo-browser-doi"
                    self._wait_for_interval()
                    article_url = session.discover_by_doi(
                        expected_doi,
                        timeout_ms=_seconds_to_ms(
                            self.config.navigation_timeout_seconds
                        ),
                    )
                    if article_url is not None:
                        normalized = normalize_kyobo_article_url(article_url)
                        if normalized is None:
                            raise KyoboBrowserOperationError(
                                "invalid_response",
                                "unsafe_kyobo_doi_article_reference",
                                "unsafe_url",
                            )
                        article_url = normalized
                        discovery = "doi"
                        attempts.append(
                            KyoboBrowserAttempt("kyobo-browser-doi", "ok")
                        )
                    else:
                        attempts.append(
                            KyoboBrowserAttempt(
                                "kyobo-browser-doi",
                                "not_found",
                                "not_found",
                            )
                        )

                if article_url is None:
                    operation_stage = "kyobo-browser-search"
                    article_url = self._search_exact_article(
                        session,
                        known_titles,
                    )
                    discovery = "title_search"
                    attempts.append(
                        KyoboBrowserAttempt("kyobo-browser-search", "ok")
                    )

                self._wait_for_interval()
                operation_stage = "kyobo-browser-landing"
                snapshot = session.open_article(
                    article_url,
                    timeout_ms=_seconds_to_ms(
                        self.config.navigation_timeout_seconds
                    ),
                )
                challenge = _page_challenge(snapshot.final_url)
                if challenge is not None:
                    raise challenge
                http_failure = _page_http_failure(snapshot.http_status)
                if http_failure is not None:
                    raise http_failure
                final_url = normalize_kyobo_article_url(snapshot.final_url)
                if final_url is None:
                    raise KyoboBrowserOperationError(
                        "authentication_required",
                        "kyobo_session_left_exact_article_page",
                        "authentication_required",
                    )
                if final_url != article_url:
                    raise KyoboBrowserOperationError(
                        "identity_mismatch",
                        "kyobo_article_redirect_identity_mismatch",
                        "identity_mismatch",
                    )
                if len(snapshot.document.encode("utf-8")) > self.config.max_html_bytes:
                    raise KyoboBrowserOperationError(
                        "invalid_response",
                        "kyobo_page_too_large",
                        "too_large",
                    )
                identity = validate_kyobo_landing_document(
                    snapshot.document,
                    paper,
                    article_url=final_url,
                )
                control = _select_download_control(snapshot.controls)

                self._wait_for_interval()
                operation_stage = "kyobo-browser-download"
                content = session.download_control(
                    control,
                    timeout_ms=_seconds_to_ms(
                        self.config.download_timeout_seconds
                    ),
                    max_pdf_bytes=self.config.max_pdf_bytes,
                )
                _validate_pdf_bytes(content, self.config.max_pdf_bytes)
            except KyoboLandingIdentityError as exc:
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
                    stage="kyobo-landing-identity",
                    prior_attempts=attempts,
                )
            except KyoboBrowserOperationError as exc:
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
                    prior_attempts=attempts,
                )
            except Exception:
                self._record_operation_failure(
                    "retryable_error",
                    "kyobo_browser_operation_failed",
                    "browser_error",
                )
                return self._failure(
                    paper,
                    "retryable_error",
                    "kyobo_browser_operation_failed",
                    "browser_error",
                    stage=operation_stage,
                    prior_attempts=attempts,
                )

            self._consecutive_retryable_errors = 0
            return KyoboBrowserResolution(
                status="resolved",
                reason="authenticated_official_download_control",
                paper=paper,
                content=content,
                content_type="application/pdf",
                matched_title=identity.title,
                matched_doi=identity.doi,
                landing_first_page=identity.first_page,
                landing_final_page=identity.final_page,
                landing_page_count=identity.page_count,
                identity_provenance=(
                    *identity.provenance,
                    "kyobo_landing_canonical_article_exact",
                    (
                        "kyobo_doi_redirect_article_exact"
                        if discovery == "doi"
                        else "kyobo_web_title_search_exact"
                        if discovery == "title_search"
                        else "kyobo_supplied_article_reference_exact"
                    ),
                    "kyobo_visible_down_control_exact",
                    "native_browser_download_event",
                ),
                attempts=(
                    *attempts,
                    KyoboBrowserAttempt("kyobo-browser-landing", "ok"),
                    KyoboBrowserAttempt(
                        "kyobo-browser-download",
                        "downloaded",
                    ),
                ),
            )

    def _search_exact_article(
        self,
        session: KyoboBrowserSession,
        title_queries: tuple[tuple[str, str], ...],
    ) -> str:
        expected_titles = frozenset(
            normalized for _raw, normalized in title_queries
        )
        matches: dict[str, KyoboBrowserSearchResult] = {}
        for query_title in _kyobo_search_query_variants(title_queries):
            normalized_query_title = normalize_title(query_title)
            self._wait_for_interval()
            results = session.search_articles(
                query_title,
                timeout_ms=_seconds_to_ms(
                    self.config.navigation_timeout_seconds
                ),
                max_results=self.config.max_search_results,
            )
            for result in results:
                article_url = normalize_kyobo_article_url(result.article_url)
                if article_url is None:
                    raise KyoboBrowserOperationError(
                        "invalid_response",
                        "unsafe_kyobo_search_article_reference",
                        "unsafe_url",
                    )
                if not _is_kyobo_search_title_shortlist(
                    result.title,
                    expected_titles,
                    normalized_query_title=normalized_query_title,
                ):
                    continue
                matches.setdefault(article_url, result)
            if len(matches) > 1:
                raise KyoboBrowserOperationError(
                    "identity_mismatch",
                    "ambiguous_exact_kyobo_search_results",
                    "ambiguous",
                )
            if matches:
                break
        if not matches:
            raise KyoboBrowserOperationError(
                "not_found",
                "exact_kyobo_search_result_not_found",
                "not_found",
            )
        return next(iter(matches))

    def _record_operation_failure(
        self,
        status: str,
        reason: str,
        error_code: str,
    ) -> None:
        if error_code in _CIRCUIT_CODES or reason in _IMMEDIATE_CIRCUIT_REASONS:
            self._circuit_error = (status, reason, error_code)
            return
        if status != "retryable_error":
            self._consecutive_retryable_errors = 0
            return
        self._consecutive_retryable_errors += 1
        if self._consecutive_retryable_errors >= TRANSIENT_CIRCUIT_THRESHOLD:
            self._circuit_error = (
                "retryable_error",
                "kyobo_browser_transient_circuit_open",
                "consecutive_retryable_failures",
            )

    def _get_session(self) -> KyoboBrowserSession:
        if self._session is not None:
            return self._session
        assert self.config.profile_dir is not None
        self._session = _PlaywrightKyoboSession(self.config)
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
        stage: str = "kyobo-browser",
        prior_attempts: list[KyoboBrowserAttempt] | None = None,
    ) -> KyoboBrowserResolution:
        return KyoboBrowserResolution(
            status=status,
            reason=reason,
            paper=paper,
            error_code=error_code,
            attempts=(
                *(prior_attempts or ()),
                KyoboBrowserAttempt(stage, "failed", error_code),
            ),
        )


class _PlaywrightKyoboSession:
    """Lazy same-thread Playwright adapter with first-party isolation."""

    def __init__(self, config: KyoboBrowserConfig) -> None:
        assert config.profile_dir is not None
        self._config = config
        try:
            self._profile_dir = validate_kyobo_profile_dir(
                config.profile_dir,
                repository_root=config.repository_root,
            )
        except (OSError, ValueError):
            raise KyoboBrowserOperationError(
                "configuration_error",
                "invalid_browser_profile",
                "invalid_browser_profile",
            ) from None
        self._profile_lock = _KyoboProfileLock(self._profile_dir)
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._owner_thread: int | None = None
        self._dialog_error: KyoboBrowserOperationError | None = None
        self._blocked_document = False
        self._doi_navigation = False

    def discover_by_doi(
        self,
        doi: str,
        *,
        timeout_ms: int,
    ) -> str | None:
        self._ensure_started()
        self._require_owner_thread()
        assert self._page is not None
        try:
            normalized = normalize_doi(doi)
        except InvalidDoiError:
            raise KyoboBrowserOperationError(
                "invalid_response",
                "invalid_kyobo_candidate_doi",
                "invalid_doi",
            ) from None
        if normalized is None:
            return None
        doi_url = f"https://{_DOI_HOST}/{quote(normalized, safe='/')}"
        self._blocked_document = False
        self._dialog_error = None
        self._doi_navigation = True
        try:
            response = self._page.goto(
                doi_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
        except Exception:
            if self._blocked_document:
                return None
            raise KyoboBrowserOperationError(
                "retryable_error",
                "kyobo_doi_navigation_failed",
                "browser_error",
            ) from None
        finally:
            self._doi_navigation = False
        failure = _page_http_failure(
            int(response.status) if response is not None else None
        )
        if failure is not None:
            if failure.status == "not_found":
                return None
            raise failure
        challenge = self._inspect_visible_challenge()
        if challenge is not None:
            raise challenge
        return normalize_kyobo_article_url(self._page.url)

    def search_articles(
        self,
        title: str,
        *,
        timeout_ms: int,
        max_results: int,
    ) -> tuple[KyoboBrowserSearchResult, ...]:
        self._ensure_started()
        self._require_owner_thread()
        assert self._page is not None
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title.strip()) > _SEARCH_QUERY_MAX_CHARS
        ):
            raise KyoboBrowserOperationError(
                "invalid_response",
                "invalid_kyobo_search_title",
                "identity_mismatch",
            )
        if not 1 <= max_results <= 100:
            raise KyoboBrowserOperationError(
                "configuration_error",
                "invalid_kyobo_search_result_limit",
                "invalid_browser_profile",
            )
        self._blocked_document = False
        self._dialog_error = None
        try:
            response = self._page.goto(
                KYOBO_HOME_URL,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self._raise_http_failure(response)
            self._raise_visible_challenge()
            selected = self._page.locator(
                _SEARCH_TARGET_SELECTOR
            ).select_option(
                "productNm",
                force=True,
                timeout=timeout_ms,
            )
            if selected != ["productNm"]:
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "kyobo_title_search_scope_not_selected",
                    "identity_mismatch",
                )
            self._page.locator(_SEARCH_INPUT_SELECTOR).fill(
                title.strip(),
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
            self._raise_visible_challenge()
            if not _is_allowed_kyobo_page_url(self._page.url):
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "kyobo_search_left_first_party",
                    "unsafe_url",
                )
            self._bounded_page_content()
            return self._collect_search_results(max_results=max_results)
        except KyoboBrowserOperationError:
            raise
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            if self._blocked_document:
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "kyobo_search_redirected_outside_first_party",
                    "unsafe_url",
                ) from None
            challenge = self._inspect_visible_challenge()
            if challenge is not None:
                raise challenge from None
            raise KyoboBrowserOperationError(
                "retryable_error",
                "kyobo_search_ui_failed",
                "browser_error",
            ) from None

    def open_article(self, url: str, *, timeout_ms: int) -> KyoboPageSnapshot:
        self._ensure_started()
        self._require_owner_thread()
        assert self._page is not None
        normalized = normalize_kyobo_article_url(url)
        if normalized is None:
            raise KyoboBrowserOperationError(
                "invalid_response",
                "unsafe_kyobo_article_reference",
                "unsafe_url",
            )
        self._blocked_document = False
        self._dialog_error = None
        try:
            response = self._page.goto(
                normalized,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self._raise_http_failure(response)
            self._raise_visible_challenge()
            document = self._bounded_page_content()
            controls = self._collect_download_controls()
            final_url = self._page.url
        except KyoboBrowserOperationError:
            raise
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            if self._blocked_document:
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "kyobo_landing_redirected_outside_first_party",
                    "unsafe_url",
                ) from None
            challenge = self._inspect_visible_challenge()
            if challenge is not None:
                raise challenge from None
            raise KyoboBrowserOperationError(
                "retryable_error",
                "kyobo_landing_navigation_failed",
                "browser_error",
            ) from None
        return KyoboPageSnapshot(
            final_url=final_url,
            document=document,
            controls=controls,
            http_status=(int(response.status) if response is not None else None),
        )

    def download_control(
        self,
        control: KyoboDownloadControl,
        *,
        timeout_ms: int,
        max_pdf_bytes: int,
    ) -> bytes:
        self._ensure_started()
        self._require_owner_thread()
        assert self._page is not None
        controls = self._page.locator(_DOWNLOAD_SELECTOR)
        if control.dom_index < 0 or control.dom_index >= controls.count():
            raise KyoboBrowserOperationError(
                "identity_mismatch",
                "kyobo_download_control_changed",
                "identity_mismatch",
            )
        locator = controls.nth(control.dom_index)
        current = self._control_snapshot(locator, control.dom_index)
        if current != control or not _is_official_download_control(current):
            raise KyoboBrowserOperationError(
                "identity_mismatch",
                "kyobo_download_control_changed",
                "identity_mismatch",
            )

        self._dialog_error = None
        self._blocked_document = False
        self._raise_visible_challenge()
        download: Any = None
        try:
            with self._page.expect_download(timeout=timeout_ms) as download_info:
                locator.click(timeout=timeout_ms)
            download = download_info.value
        except Exception:
            if self._dialog_error is not None:
                raise self._dialog_error from None
            challenge = self._inspect_visible_challenge(include_access=True)
            if challenge is not None:
                raise challenge from None
            if self._blocked_document:
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "kyobo_download_left_first_party",
                    "unsafe_url",
                ) from None
            raise KyoboBrowserOperationError(
                "retryable_error",
                "kyobo_native_download_not_emitted",
                "download_timeout",
            ) from None

        try:
            if not _is_allowed_native_download_url(download.url):
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "unexpected_kyobo_download_origin",
                    "unsafe_url",
                )
            if download.failure() is not None:
                raise KyoboBrowserOperationError(
                    "retryable_error",
                    "kyobo_native_download_failed",
                    "browser_error",
                )
            path_value = download.path()
            if path_value is None:
                raise KyoboBrowserOperationError(
                    "retryable_error",
                    "kyobo_native_download_missing",
                    "browser_error",
                )
            content = _read_download_without_symlink(
                Path(path_value),
                max_pdf_bytes,
            )
            _validate_pdf_bytes(content, max_pdf_bytes)
            return content
        finally:
            if download is not None:
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
        except KyoboBrowserOperationError:
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
            raise KyoboBrowserOperationError(
                "configuration_error",
                code,
                code,
            ) from None

    def _collect_search_results(
        self,
        *,
        max_results: int,
    ) -> tuple[KyoboBrowserSearchResult, ...]:
        assert self._page is not None
        cards = self._page.locator(_SEARCH_CARD_SELECTOR)
        count = cards.count()
        total = self._visible_search_total()
        if count > max_results or (total is not None and total > max_results):
            raise KyoboBrowserOperationError(
                "identity_mismatch",
                "too_many_kyobo_search_results",
                "ambiguous",
            )
        if count == 0:
            no_data = self._page.locator(".nodata_type02")
            if no_data.count() and no_data.nth(0).is_visible():
                return ()
            raise KyoboBrowserOperationError(
                "invalid_response",
                "kyobo_search_results_missing",
                "invalid_response",
            )

        results: list[KyoboBrowserSearchResult] = []
        for index in range(count):
            card = cards.nth(index)
            links = card.locator(_SEARCH_TITLE_SELECTOR)
            if links.count() != 1:
                raise KyoboBrowserOperationError(
                    "identity_mismatch",
                    "ambiguous_kyobo_search_card",
                    "ambiguous",
                )
            link = links.nth(0)
            if not link.is_visible():
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "hidden_kyobo_search_result",
                    "invalid_response",
                )
            title = " ".join(link.inner_text().split())
            if not title or len(title) > 4_096:
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "invalid_kyobo_search_result_title",
                    "identity_mismatch",
                )
            codes: set[str] = set()
            code_controls = card.locator(_SEARCH_CODE_SELECTOR)
            for code_index in range(code_controls.count()):
                value = (code_controls.nth(code_index).get_attribute("value") or "").strip()
                if value:
                    codes.add(value)
            if len(codes) != 1:
                raise KyoboBrowserOperationError(
                    "identity_mismatch",
                    "ambiguous_kyobo_search_article_code",
                    "ambiguous",
                )
            article_code = next(iter(codes))
            if _ARTICLE_CODE_RE.fullmatch(article_code) is None:
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "unsafe_kyobo_search_article_code",
                    "unsafe_url",
                )
            # This code is read from the rendered official result row and is
            # used only for its public article route.  It is never passed to
            # or replayed against any file/download endpoint.
            article_url = normalize_kyobo_article_url(
                f"https://{_KYOBO_HOST}/article/detail/{article_code}"
            )
            if article_url is None:
                raise KyoboBrowserOperationError(
                    "invalid_response",
                    "unsafe_kyobo_search_article_reference",
                    "unsafe_url",
                )
            results.append(
                KyoboBrowserSearchResult(title=title, article_url=article_url)
            )
        return tuple(results)

    def _visible_search_total(self) -> int | None:
        assert self._page is not None
        totals = self._page.locator("#totalCount")
        if totals.count() != 1 or not totals.nth(0).is_visible():
            return None
        raw = totals.nth(0).inner_text()
        digits = "".join(character for character in raw if character.isdigit())
        return int(digits) if digits else None

    def _collect_download_controls(self) -> tuple[KyoboDownloadControl, ...]:
        assert self._page is not None
        controls = self._page.locator(_DOWNLOAD_SELECTOR)
        count = controls.count()
        if count > _MAX_DOWNLOAD_CONTROLS:
            raise KyoboBrowserOperationError(
                "invalid_response",
                "too_many_kyobo_download_controls",
                "ambiguous",
            )
        return tuple(
            self._control_snapshot(controls.nth(index), index)
            for index in range(count)
        )

    @staticmethod
    def _control_snapshot(locator: Any, dom_index: int) -> KyoboDownloadControl:
        class_tokens = tuple(
            sorted(
                token
                for token in (locator.get_attribute("class") or "").split()
                if token
            )
        )
        return KyoboDownloadControl(
            dom_index=dom_index,
            class_tokens=class_tokens,
            label=" ".join(locator.inner_text().split()),
            data_lang=(locator.get_attribute("data-lang") or "").strip() or None,
            visible=bool(locator.is_visible()),
            enabled=bool(locator.is_enabled()),
        )

    def _bounded_page_content(self) -> str:
        assert self._page is not None
        document = self._page.content()
        if len(document.encode("utf-8")) > self._config.max_html_bytes:
            raise KyoboBrowserOperationError(
                "invalid_response",
                "kyobo_page_too_large",
                "too_large",
            )
        return document

    def _raise_visible_challenge(self, *, include_access: bool = False) -> None:
        challenge = self._inspect_visible_challenge(include_access=include_access)
        if challenge is not None:
            raise challenge

    def _inspect_visible_challenge(
        self,
        *,
        include_access: bool = False,
    ) -> KyoboBrowserOperationError | None:
        assert self._page is not None
        path_error = _page_challenge(self._page.url)
        if path_error is not None:
            return path_error
        for selector in _DIALOG_SELECTORS:
            try:
                dialogs = self._page.locator(selector)
                for index in range(min(dialogs.count(), 10)):
                    dialog = dialogs.nth(index)
                    if not dialog.is_visible():
                        continue
                    text = " ".join(dialog.inner_text().split()).casefold()
                    if any(marker in text for marker in _CAPTCHA_MARKERS):
                        return KyoboBrowserOperationError(
                            "captcha_required",
                            "kyobo_captcha_requires_manual_action",
                            "captcha_required",
                        )
                    if any(marker in text for marker in _AUTH_MARKERS):
                        return KyoboBrowserOperationError(
                            "authentication_required",
                            "kyobo_authentication_requires_manual_refresh",
                            "authentication_required",
                        )
                    if include_access and any(
                        marker in text for marker in _ACCESS_MARKERS
                    ):
                        return KyoboBrowserOperationError(
                            "access_denied",
                            "kyobo_subscription_or_access_gate_visible",
                            "access_denied",
                        )
            except KyoboBrowserOperationError:
                raise
            except Exception:
                continue
        return None

    def _raise_http_failure(self, response: Any) -> None:
        failure = _page_http_failure(
            int(response.status) if response is not None else None
        )
        if failure is not None:
            raise failure

    def _route_first_party_only(self, route: Any, request: Any) -> None:
        url = str(request.url)
        if _is_allowed_kyobo_resource_url(url):
            route.continue_()
            return
        try:
            parsed = urlsplit(url)
            main_frame = self._page is None or request.frame == self._page.main_frame
            doi_allowed = bool(
                self._doi_navigation
                and request.is_navigation_request()
                and request.resource_type == "document"
                and main_frame
                and parsed.scheme.casefold() == "https"
                and (parsed.hostname or "").casefold() == _DOI_HOST
                and parsed.port in {None, 443}
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
        except (TypeError, ValueError):
            doi_allowed = False
            main_frame = False
        if doi_allowed:
            route.continue_()
            return
        if (
            request.is_navigation_request()
            and request.resource_type == "document"
            and main_frame
        ):
            self._blocked_document = True
        route.abort(error_code="blockedbyclient")

    def _handle_dialog(self, dialog: Any) -> None:
        message = " ".join(str(dialog.message or "").split()).casefold()
        if any(marker in message for marker in _CAPTCHA_MARKERS):
            self._dialog_error = KyoboBrowserOperationError(
                "captcha_required",
                "kyobo_captcha_requires_manual_action",
                "captcha_required",
            )
        elif any(marker in message for marker in _AUTH_MARKERS):
            self._dialog_error = KyoboBrowserOperationError(
                "authentication_required",
                "kyobo_authentication_requires_manual_refresh",
                "authentication_required",
            )
        elif any(marker in message for marker in _ACCESS_MARKERS):
            self._dialog_error = KyoboBrowserOperationError(
                "access_denied",
                "kyobo_subscription_or_access_gate_visible",
                "access_denied",
            )
        else:
            self._dialog_error = KyoboBrowserOperationError(
                "access_denied",
                "kyobo_download_dialog_rejected",
                "access_denied",
            )
        with contextlib.suppress(Exception):
            dialog.dismiss()

    def _require_owner_thread(self) -> None:
        if (
            self._owner_thread is not None
            and self._owner_thread != threading.get_ident()
        ):
            raise RuntimeError("Kyobo browser session used from a different thread")


class _KyoboProfileLock:
    def __init__(self, profile_dir: Path) -> None:
        self._path = profile_dir / ".forensics-paper-session.lock"
        self._handle: Any = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        if self._path.is_symlink():
            raise KyoboBrowserOperationError(
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
            raise KyoboBrowserOperationError(
                "configuration_error",
                "kyobo_browser_profile_in_use",
                "profile_in_use",
            ) from None
        except OSError:
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.close()
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise KyoboBrowserOperationError(
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


def _validate_config(config: KyoboBrowserConfig) -> None:
    if config.permission_granted and config.profile_dir is None:
        raise ValueError("profile_dir is required for Kyobo browser download")
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


def _select_download_control(
    controls: tuple[KyoboDownloadControl, ...],
) -> KyoboDownloadControl:
    candidates = [control for control in controls if _is_official_download_control(control)]
    if len(candidates) != 1:
        raise KyoboBrowserOperationError(
            "access_denied" if not candidates else "identity_mismatch",
            (
                "kyobo_download_control_unavailable"
                if not candidates
                else "ambiguous_kyobo_download_controls"
            ),
            "access_denied" if not candidates else "ambiguous",
        )
    return candidates[0]


def _is_official_download_control(control: KyoboDownloadControl) -> bool:
    label = normalize_title(control.label)
    return bool(
        control.visible
        and control.enabled
        and "down" in control.class_tokens
        and (
            label == normalize_title("원문저장")
            or control.data_lang == "article_btn_save"
        )
    )


def _paper_doi(paper: PaperCandidate) -> str | None:
    value = paper.doi or paper.doi_original
    try:
        normalized = normalize_doi(value)
    except InvalidDoiError:
        raise KyoboLandingIdentityError(
            "invalid_response",
            "invalid_kyobo_candidate_doi",
            "invalid_doi",
        ) from None
    return normalized.casefold() if normalized else None


def _page_challenge(url: str) -> KyoboBrowserOperationError | None:
    try:
        parsed = urlsplit(url)
        path = parsed.path.casefold()
    except ValueError:
        path = ""
    if any(marker in path for marker in ("/login", "/signin", "/user/loginview")):
        return KyoboBrowserOperationError(
            "authentication_required",
            "kyobo_authentication_requires_manual_refresh",
            "authentication_required",
        )
    if "captcha" in path:
        return KyoboBrowserOperationError(
            "captcha_required",
            "kyobo_captcha_requires_manual_action",
            "captcha_required",
        )
    return None


def _page_http_failure(status: int | None) -> KyoboBrowserOperationError | None:
    if status is None or 200 <= status < 400:
        return None
    if status == 401:
        return KyoboBrowserOperationError(
            "authentication_required",
            "kyobo_article_authentication_required",
            "authentication_required",
        )
    if status == 403:
        return KyoboBrowserOperationError(
            "access_denied",
            "kyobo_article_access_denied",
            "access_denied",
        )
    if status == 429:
        return KyoboBrowserOperationError(
            "retryable_error",
            "kyobo_article_rate_limited",
            "retryable_error",
        )
    if status in {408, 425} or 500 <= status <= 599:
        return KyoboBrowserOperationError(
            "retryable_error",
            "kyobo_article_retryable_http_error",
            "retryable_error",
        )
    if status in {404, 410}:
        return KyoboBrowserOperationError(
            "not_found",
            "kyobo_article_not_found",
            "not_found",
        )
    return KyoboBrowserOperationError(
        "invalid_response",
        "kyobo_article_http_error",
        "http_status",
    )


def _is_allowed_kyobo_resource_url(url: str) -> bool:
    if not isinstance(url, str) or not url or len(url) > 8_192:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold()
        in {_KYOBO_HOST, _KYOBO_CONTENT_HOST}
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _is_allowed_kyobo_page_url(url: str) -> bool:
    if not _is_allowed_kyobo_resource_url(url):
        return False
    parsed = urlsplit(url)
    if (parsed.hostname or "").casefold() != _KYOBO_HOST:
        return False
    return bool(
        parsed.path
        in {
            "/",
            "/main",
            "/search/article",
            # The rendered title-search form scopes the route by appending
            # the selected official option to the path.
            "/search/article/productNm",
        }
        or _ARTICLE_PATH_RE.fullmatch(parsed.path)
    )


def _is_allowed_native_download_url(url: object) -> bool:
    """Validate only the emitted origin; never retain or replay its token."""

    if not isinstance(url, str) or not url or len(url) > 8_192:
        return False
    if url.startswith("blob:"):
        inner = url[5:]
        try:
            parsed = urlsplit(inner)
            port = parsed.port
        except ValueError:
            return False
        return bool(
            parsed.scheme.casefold() == "https"
            and (parsed.hostname or "").casefold() == _KYOBO_HOST
            and port in {None, 443}
            and parsed.username is None
            and parsed.password is None
        )
    return _is_allowed_kyobo_resource_url(url)


def _validate_pdf_bytes(content: object, max_pdf_bytes: int) -> None:
    if not isinstance(content, bytes):
        raise KyoboBrowserOperationError(
            "invalid_response",
            "kyobo_download_is_not_bytes",
            "invalid_pdf",
        )
    if len(content) > max_pdf_bytes:
        raise KyoboBrowserOperationError(
            "too_large",
            "kyobo_pdf_exceeds_size_limit",
            "too_large",
        )
    if not content.startswith(b"%PDF-"):
        raise KyoboBrowserOperationError(
            "invalid_response",
            "kyobo_download_is_not_pdf",
            "invalid_pdf",
        )


def _read_download_without_symlink(path: Path, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise KyoboBrowserOperationError(
            "invalid_response",
            "kyobo_download_file_unsafe",
            "invalid_pdf",
        ) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise KyoboBrowserOperationError(
                "invalid_response",
                "kyobo_download_file_unsafe",
                "invalid_pdf",
            )
        if info.st_size > max_bytes:
            raise KyoboBrowserOperationError(
                "too_large",
                "kyobo_pdf_exceeds_size_limit",
                "too_large",
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(content) > max_bytes:
        raise KyoboBrowserOperationError(
            "too_large",
            "kyobo_pdf_exceeds_size_limit",
            "too_large",
        )
    return content


def _unique_plain_values(values: tuple[str, ...]) -> set[str]:
    return {
        plain
        for value in values
        if (plain := " ".join(html.unescape(value).split()))
    }


def _normalized_identity_values(values: tuple[str, ...]) -> set[str]:
    return {
        normalized
        for value in values
        if (normalized := normalize_title(value)) is not None
    }


def _unique_digits(values: tuple[str, ...]) -> set[str]:
    result: set[str] = set()
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if re.fullmatch(r"[0-9]{1,6}", value) is None:
            return {"__invalid__"}
        result.add(str(int(value)))
    return result


def _parse_issue_id(value: str | None) -> tuple[str, str | None] | None:
    if value is None or not value.strip():
        return None
    match = _ISSUE_ID_RE.fullmatch(value.strip())
    if match is None:
        return None
    return (
        str(int(match.group("volume"))),
        str(int(match.group("issue"))) if match.group("issue") else None,
    )


def _normalized_page(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = html.unescape(value).strip().casefold().replace(" ", "")
    if not normalized or _PAGE_RE.fullmatch(normalized) is None:
        return None
    if normalized.isdigit():
        return str(int(normalized))
    return normalized


def _landing_page_range(
    parser: _KyoboMetadataParser,
) -> tuple[str | None, str | None]:
    first_values = {
        value
        for raw in parser.values("citation_firstpage")
        if (value := _normalized_page(raw)) is not None
    }
    final_values = {
        value
        for raw in parser.values("citation_lastpage")
        if (value := _normalized_page(raw)) is not None
    }
    return (
        next(iter(first_values)) if len(first_values) == 1 else None,
        next(iter(final_values)) if len(final_values) == 1 else None,
    )


def _inclusive_numeric_page_count(
    first_page: str | None,
    final_page: str | None,
) -> int | None:
    if not (
        isinstance(first_page, str)
        and isinstance(final_page, str)
        and first_page.isdigit()
        and final_page.isdigit()
    ):
        return None
    first = int(first_page)
    final = int(final_page)
    if final < first:
        return None
    return final - first + 1


def _normalized_author_set(values: tuple[str, ...]) -> frozenset[str] | None:
    if not values:
        return frozenset()
    normalized: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            return None
        # KCI's author projection commonly appends an affiliation in trailing
        # parentheses (for example ``홍길동(대학교)``), whereas citation_author
        # contains only the name.  Remove only one or more complete trailing
        # parenthesized annotations; never split or fuzzy-match the name.
        author_name = _strip_trailing_author_affiliations(raw)
        value = normalize_title(author_name)
        if value is None:
            return None
        compact = "".join(character for character in value if character.isalnum())
        if not compact:
            return None
        normalized.add(compact)
    return frozenset(normalized)


def _authors_use_affiliation_annotations(values: tuple[str, ...]) -> bool:
    return any(
        isinstance(value, str)
        and _strip_trailing_author_affiliations(value) != value.strip()
        for value in values
    )


def _strip_trailing_author_affiliations(value: str) -> str:
    """Remove complete trailing parenthesized groups, including nesting."""

    current = value.strip()
    while current.endswith((")", "）")):
        depth = 0
        opening_index: int | None = None
        for index in range(len(current) - 1, -1, -1):
            character = current[index]
            if character in ")）":
                depth += 1
            elif character in "(（":
                depth -= 1
                if depth == 0:
                    opening_index = index
                    break
                if depth < 0:
                    return value.strip()
        if opening_index is None:
            return value.strip()
        prefix = current[:opening_index].rstrip()
        annotation = current[opening_index + 1 : -1].strip()
        if not prefix or not annotation:
            return value.strip()
        current = prefix
    return current


def _has_complete_kyobo_bibliography(
    provenance: tuple[str, ...],
) -> bool:
    observed = set(provenance)
    return bool(
        _BIBLIOGRAPHY_CORE_PROVENANCE.issubset(observed)
        and _BIBLIOGRAPHY_VOLUME_PROVENANCE.intersection(observed)
        and _BIBLIOGRAPHY_PUBLISHER_PROVENANCE.intersection(observed)
        and _BIBLIOGRAPHY_PAGE_PROVENANCE.intersection(observed)
    )


def _compact_identity_title(value: str) -> str:
    normalized = normalize_title(value)
    if normalized is None:
        return ""
    return "".join(character for character in normalized if character.isalnum())


def _kyobo_search_query_variants(
    title_queries: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Return rendered-form queries; every result is still landing-validated."""

    values: list[str] = []
    seen: set[str] = set()
    for raw_title, _normalized in title_queries:
        title = " ".join(raw_title.split())
        variants = [title]
        prefix = re.split(
            r"(?:\s+[-—―:：]\s*|\s+-\s*|\s*[:：]\s+)",
            title,
            maxsplit=1,
        )[0].strip()
        if len(_compact_identity_title(prefix)) >= 12:
            variants.append(prefix)
        punctuation_spaced = " ".join(
            "".join(
                character if character.isalnum() else " "
                for character in title
            ).split()
        )
        if len(_compact_identity_title(punctuation_spaced)) >= 12:
            variants.append(punctuation_spaced)
        for variant in variants:
            key = normalize_title(variant)
            if (
                key is None
                or key in seen
                or len(variant) > _SEARCH_QUERY_MAX_CHARS
            ):
                continue
            seen.add(key)
            values.append(variant)
    return tuple(values)


def _kyobo_main_title_variants(
    title_queries: tuple[tuple[str, str], ...],
) -> frozenset[str]:
    """Return only exact, explicitly delimited main-title projections."""

    values: set[str] = set()
    for raw_title, _normalized in title_queries:
        title = " ".join(raw_title.split())
        prefix = re.split(
            r"(?:\s+[-—―:：]\s*|\s+-\s*|\s*[:：]\s+)",
            title,
            maxsplit=1,
        )[0].strip()
        normalized = normalize_title(prefix)
        if (
            normalized is not None
            and len(_compact_identity_title(normalized)) >= 12
            and normalized != normalize_title(title)
        ):
            values.add(normalized)
    return frozenset(values)


def _is_kyobo_search_title_shortlist(
    result_title: str,
    expected_titles: frozenset[str],
    *,
    normalized_query_title: str | None,
) -> bool:
    normalized = normalize_title(result_title)
    if normalized is None:
        return False
    # The query variants are deterministically derived from the candidate.  An
    # exact rendered-card match to one such query is safe for shortlisting;
    # the canonical landing page must still pass the complete identity chain.
    if normalized_query_title is not None and normalized == normalized_query_title:
        return True
    if normalized in expected_titles:
        return True
    observed = _compact_identity_title(normalized)
    if len(observed) < 12:
        return False
    for expected_title in expected_titles:
        expected = _compact_identity_title(expected_title)
        if observed == expected:
            return True
        shorter, longer = sorted((observed, expected), key=len)
        if (
            len(shorter) >= 12
            and len(shorter) / len(longer) >= 0.45
            and longer.startswith(shorter)
        ):
            return True
    return False


def _bibliographically_equivalent_title(
    observed_title: str,
    expected_titles: set[str],
) -> bool:
    """Accept only conservative publisher/KCI title presentation variance.

    This helper is called only after exact DOI, year, journal, publisher,
    volume/issue, page, and author evidence.  It covers punctuation-only
    changes, a publisher omitting a long subtitle/case citation, and a small
    wording correction.  Unrelated short titles remain rejected.
    """

    observed = _compact_identity_title(observed_title)
    if len(observed) < 12:
        return False
    for expected_title in expected_titles:
        expected = _compact_identity_title(expected_title)
        if len(expected) < 12:
            continue
        if observed == expected:
            return True
        shorter, longer = sorted((observed, expected), key=len)
        if (
            len(shorter) >= 20
            and len(shorter) / len(longer) >= 0.45
            and (longer.startswith(shorter) or shorter in longer)
        ):
            return True
        if SequenceMatcher(
            None,
            observed,
            expected,
            autojunk=False,
        ).ratio() >= 0.88:
            return True
    return False


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
        raise KyoboBrowserOperationError(
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
        raise ValueError("Kyobo browser profile must be owned by the current user")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise ValueError("Kyobo browser profile permissions are too broad")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError("Kyobo browser profile is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError("Kyobo browser profile marker is not a regular file")


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
