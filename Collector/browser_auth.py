"""Browser-profile preflight and manual login for authenticated PDF providers.

DBpia, Kyobo Scholar and KDFS JAMS PDFs are collected by Playwright sessions
that reuse a dedicated, user-owned browser profile.  Nothing here ever handles
a password: the only way to authenticate is the headed browser window that the
existing ``run_interactive_*_login`` helpers open, where the operator logs in
by hand and then presses Enter in the terminal.

This module gives the scheduler three composable pieces:

* :func:`inspect_profile` - is the provider's profile present and valid?
* :func:`preflight_browser_providers` - decide, per provider, whether to crawl
  headlessly with the saved session, open the manual login first, or skip the
  provider (unattended run without a usable profile).
* :func:`authentication_failures` - after a collection run, read the download
  manifests of still-unresolved papers and report which providers hit a login
  or captcha challenge, so the operator can re-authenticate and retry.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PaperPipeline.dbpia_browser import (
    DbpiaBrowserOperationError,
    default_dbpia_profile_dir,
    run_interactive_dbpia_login,
    validate_dbpia_profile_dir,
)
from PaperPipeline.jams_browser import (
    JamsBrowserOperationError,
    default_jams_profile_dir,
    run_interactive_jams_login,
    validate_jams_profile_dir,
)
from PaperPipeline.kyobo_browser import (
    KyoboBrowserOperationError,
    default_kyobo_profile_dir,
    run_interactive_kyobo_login,
    validate_kyobo_profile_dir,
)
from PaperPipeline.storage import load_json_object


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BROWSER_PROVIDERS: tuple[str, ...] = ("dbpia", "kyobo", "jams")
# Sites whose login lives in a browser-session cookie: the session is gone as
# soon as the login window closes, so the operator must log in *inside* the
# collection session (the provider's own ``interactive_login`` option) rather
# than in a separate headed window beforehand.  DBpia and Kyobo institution
# sessions persist in the profile and can be prepared up front.
IN_SESSION_LOGIN_PROVIDERS: frozenset[str] = frozenset({"jams"})
KCI_BIBTEX_PROVIDER = "kci_bibtex"
KCI_BIBTEX_PROFILE_MARKER = ".forensics-paper-kci-bibtex-profile.json"
LOGIN_MODES: tuple[str, ...] = ("auto", "always", "never")
# Manifest codes that mean "the saved session is no longer authenticated".
LOGIN_CHALLENGE_CODES = frozenset({"authentication_required", "captcha_required"})
# Codes that mean the account is logged in but not entitled (subscription or
# access gate); a new login will not change them, so they are only reported.
ACCESS_DENIED_CODES = frozenset({"access_denied", "not_entitled"})
# Codes that mean the browser itself could not start; a login will not help.
BROWSER_SETUP_CODES = frozenset(
    {
        "browser_dependency_missing",
        "browser_not_installed",
        "browser_launch_failed",
        "invalid_browser_profile",
        "profile_in_use",
    }
)
_BROWSER_ERRORS = (
    DbpiaBrowserOperationError,
    KyoboBrowserOperationError,
    JamsBrowserOperationError,
)


class BrowserLoginError(RuntimeError):
    """The manual login could not be completed (cancelled, browser failure)."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider: str
    label: str
    environment_variable: str
    auth_script: str
    default_dir: Callable[[], Path]
    validate: Callable[..., Path]
    login: Callable[..., None]


_SPECS: dict[str, ProviderSpec] = {
    "dbpia": ProviderSpec(
        provider="dbpia",
        label="DBpia",
        environment_variable="DBPIA_BROWSER_PROFILE_DIR",
        auth_script="scripts/dbpia_browser_auth.py",
        default_dir=default_dbpia_profile_dir,
        validate=validate_dbpia_profile_dir,
        login=run_interactive_dbpia_login,
    ),
    "kyobo": ProviderSpec(
        provider="kyobo",
        label="교보 Scholar",
        environment_variable="KYOBO_BROWSER_PROFILE_DIR",
        auth_script="scripts/kyobo_browser_auth.py",
        default_dir=default_kyobo_profile_dir,
        validate=validate_kyobo_profile_dir,
        login=run_interactive_kyobo_login,
    ),
    "jams": ProviderSpec(
        provider="jams",
        label="KDFS JAMS",
        environment_variable="JAMS_BROWSER_PROFILE_DIR",
        auth_script="scripts/jams_browser_auth.py",
        default_dir=default_jams_profile_dir,
        validate=validate_jams_profile_dir,
        login=run_interactive_jams_login,
    ),
}


@dataclass(frozen=True, slots=True)
class ProfileStatus:
    provider: str
    profile_dir: Path
    exists: bool
    valid: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "profile_dir": str(self.profile_dir),
            "exists": self.exists,
            "valid": self.valid,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Which browser providers a collection run may use, and why not others."""

    login_mode: str
    interactive: bool
    enabled: tuple[str, ...] = ()
    logged_in: tuple[str, ...] = ()
    # Providers that must show their login window inside the collection run
    # (``provider_options[provider]["interactive_login"] = True``).
    in_session_login: tuple[str, ...] = ()
    skipped: Mapping[str, str] = field(default_factory=dict)
    profiles: Mapping[str, ProfileStatus] = field(default_factory=dict)

    def provider_options(self) -> dict[str, dict[str, Any]]:
        """Collector ``provider_options`` implied by this preflight."""

        return {provider: {"interactive_login": True} for provider in self.in_session_login}

    def to_dict(self) -> dict[str, Any]:
        return {
            "login_mode": self.login_mode,
            "interactive": self.interactive,
            "enabled": list(self.enabled),
            "logged_in": list(self.logged_in),
            "in_session_login": list(self.in_session_login),
            "skipped": dict(self.skipped),
            "profiles": {name: status.to_dict() for name, status in self.profiles.items()},
        }


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #

def _xdg_data_home() -> Path:
    configured = os.getenv("XDG_DATA_HOME")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share"


def kci_bibtex_profile_dir() -> Path:
    """Profile used by ``Analyzer/Special-Issue/special_issue.py`` (KCI login)."""

    configured = os.getenv("KCI_BIBTEX_BROWSER_PROFILE_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser().resolve()
    return (_xdg_data_home() / "forensics-paper" / "kci-bibtex-browser-profile").resolve()


def profile_dir_for(provider: str, override: Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    if provider == KCI_BIBTEX_PROVIDER:
        return kci_bibtex_profile_dir()
    spec = _spec(provider)
    configured = os.getenv(spec.environment_variable)
    if configured and configured.strip():
        return Path(configured).expanduser()
    return spec.default_dir()


def _spec(provider: str) -> ProviderSpec:
    try:
        return _SPECS[provider]
    except KeyError:
        raise ValueError(f"unknown browser provider: {provider!r}") from None


def inspect_profile(
    provider: str,
    profile_dir: Path | None = None,
    *,
    repository_root: Path | None = REPOSITORY_ROOT,
) -> ProfileStatus:
    """Report whether the dedicated profile exists and passes the provider's checks.

    ``valid`` means the provider would accept the profile for a headless run
    (ownership, mode, marker, repository separation).  It does not prove the
    site session is still logged in; that is only known after an attempt.
    """

    directory = profile_dir_for(provider, profile_dir)
    exists = directory.is_dir() and not directory.is_symlink()
    if provider == KCI_BIBTEX_PROVIDER:
        marker = directory / KCI_BIBTEX_PROFILE_MARKER
        valid = exists and marker.is_file() and not marker.is_symlink()
        detail = "" if valid else ("profile marker is missing" if exists else "profile directory is missing")
        return ProfileStatus(provider, directory, exists, valid, detail)
    spec = _spec(provider)
    if not exists:
        return ProfileStatus(provider, directory, False, False, "profile directory is missing")
    try:
        spec.validate(directory, repository_root=repository_root, require_marker=True)
    except (ValueError, OSError) as exc:
        return ProfileStatus(provider, directory, True, False, str(exc))
    return ProfileStatus(provider, directory, True, True, "")


def is_interactive_session(
    *,
    stdin: Any | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> bool:
    """True when a human can see a browser window and press Enter.

    A terminal on stdin is required; a graphical display is required on Linux
    (``DISPLAY`` or ``WAYLAND_DISPLAY``, both present under WSLg).
    """

    stream = stdin if stdin is not None else sys.stdin
    environment = environ if environ is not None else os.environ
    system = platform or sys.platform
    try:
        if stream is None or not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if system.startswith(("win", "darwin")):
        return True
    return bool(environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY"))


def run_manual_login(
    provider: str,
    profile_dir: Path | None = None,
    *,
    repository_root: Path | None = REPOSITORY_ROOT,
    input_fn: Callable[[str], str] = input,
) -> None:
    """Open the provider's headed browser and wait for the operator's Enter.

    The provider helper creates the profile marker when needed; credentials are
    typed only into the browser window.
    """

    spec = _spec(provider)
    directory = profile_dir_for(provider, profile_dir)
    if provider in IN_SESSION_LOGIN_PROVIDERS:
        raise BrowserLoginError(
            f"{spec.label} 로그인은 세션 쿠키라 별도 창에서는 유지되지 않습니다; "
            "수집 실행 안에서 interactive_login으로 로그인하세요"
        )
    try:
        spec.login(directory, repository_root=repository_root, input_fn=input_fn)
    except (KeyboardInterrupt, EOFError) as exc:
        raise BrowserLoginError(f"{spec.label} 수동 로그인이 취소되었습니다") from exc
    except _BROWSER_ERRORS as exc:
        raise BrowserLoginError(
            f"{spec.label} 수동 로그인 실패: {getattr(exc, 'error_code', None) or exc}"
        ) from exc
    except (ValueError, OSError) as exc:
        raise BrowserLoginError(f"{spec.label} 프로필을 사용할 수 없습니다: {exc}") from exc


def manual_login_instructions(providers: Iterable[str]) -> str:
    parts = []
    for provider in providers:
        if provider == KCI_BIBTEX_PROVIDER:
            parts.append(
                "KCI(Special Issue): python3 main.py -special --special-interactive-login"
            )
            continue
        spec = _spec(provider)
        parts.append(f"{spec.label}: python {spec.auth_script}")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

def preflight_browser_providers(
    providers: Sequence[str],
    *,
    login: str = "auto",
    interactive: bool | None = None,
    login_runner: Callable[[str], None] | None = None,
    inspector: Callable[[str], ProfileStatus] = inspect_profile,
    notify: Callable[[str], None] | None = None,
) -> PreflightResult:
    """Decide how each requested browser provider participates in this run.

    * ``never``  - use providers whose profile is valid; never open a browser
      for login.  Providers without a profile are skipped with instructions.
    * ``auto``   - like ``never``, but when the session is interactive a
      missing/invalid profile triggers the manual login first.
    * ``always`` - open the manual login for every requested provider before
      crawling (requires an interactive session; otherwise behaves as ``never``).
    """

    if login not in LOGIN_MODES:
        raise ValueError(f"login must be one of {LOGIN_MODES}, got {login!r}")
    requested = list(dict.fromkeys(providers))
    unknown = [provider for provider in requested if provider not in _SPECS]
    if unknown:
        raise ValueError(f"unknown browser providers: {unknown}")
    effective_interactive = is_interactive_session() if interactive is None else interactive
    runner = login_runner or run_manual_login
    say = notify or (lambda message: None)
    enabled: list[str] = []
    logged_in: list[str] = []
    in_session: list[str] = []
    skipped: dict[str, str] = {}
    profiles: dict[str, ProfileStatus] = {}
    for provider in requested:
        status = inspector(provider)
        profiles[provider] = status
        label = _spec(provider).label
        wants_login = (login == "always") or (login == "auto" and not status.valid)
        if wants_login and effective_interactive and provider in IN_SESSION_LOGIN_PROVIDERS:
            # The session cookie would not survive a separate login window;
            # the provider opens its own headed window during collection.
            say(f"{label}: 수집 중 열리는 브라우저 창에서 로그인한 뒤 터미널에서 Enter를 누르세요")
            in_session.append(provider)
            enabled.append(provider)
            continue
        if wants_login and effective_interactive:
            say(f"{label}: 브라우저 창에서 로그인한 뒤 터미널에서 Enter를 누르세요")
            try:
                runner(provider)
            except BrowserLoginError as exc:
                skipped[provider] = str(exc)
                say(f"{label}: {exc} -> 이번 실행에서 제외")
                continue
            refreshed = inspector(provider)
            profiles[provider] = refreshed
            if not refreshed.valid:
                skipped[provider] = f"로그인 후에도 프로필이 유효하지 않음: {refreshed.detail}"
                say(f"{label}: {skipped[provider]}")
                continue
            logged_in.append(provider)
            enabled.append(provider)
            continue
        if status.valid:
            enabled.append(provider)
            say(f"{label}: 저장된 프로필로 headless 수집")
            continue
        skipped[provider] = (
            f"프로필이 없거나 유효하지 않음({status.detail}); 대화형 터미널에서 "
            f"`{manual_login_instructions([provider])}` 로 먼저 로그인하세요"
        )
        say(f"{label}: {skipped[provider]}")
    return PreflightResult(
        login_mode=login,
        interactive=effective_interactive,
        enabled=tuple(enabled),
        logged_in=tuple(logged_in),
        in_session_login=tuple(in_session),
        skipped=skipped,
        profiles=profiles,
    )


# --------------------------------------------------------------------------- #
# Post-run diagnosis
# --------------------------------------------------------------------------- #

def _attempt_provider(attempt: Mapping[str, Any]) -> str | None:
    platform = attempt.get("platform")
    if isinstance(platform, str):
        lowered = platform.casefold()
        if lowered in BROWSER_PROVIDERS:
            return lowered
        if lowered == "kdfs":
            return "jams"
    resolver = str(attempt.get("resolver") or "").casefold()
    for provider in BROWSER_PROVIDERS:
        if resolver.startswith(f"{provider}-") or f":{provider}-" in resolver:
            return provider
    if resolver.startswith("kdfs-") and "browser" in resolver:
        return "jams"
    return None


def _attempt_codes(attempt: Mapping[str, Any]) -> set[str]:
    codes: set[str] = set()
    for key in ("error_code", "resolution_status", "status", "reason"):
        value = attempt.get(key)
        if isinstance(value, str) and value:
            codes.add(value.casefold())
    return codes


@dataclass(frozen=True, slots=True)
class AuthenticationDiagnosis:
    """Per-provider papers grouped by the kind of browser failure they hit.

    ``login_required`` is the actionable bucket (log in again, then retry).
    ``access_denied`` means the session works but the subscription does not
    cover the paper; ``browser_setup_failed`` means Chromium could not run.
    """

    login_required: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    access_denied: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    browser_setup_failed: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def providers_needing_login(self) -> tuple[str, ...]:
        return tuple(sorted(self.login_required))

    def to_dict(self) -> dict[str, Any]:
        return {
            "login_required": {k: list(v) for k, v in self.login_required.items()},
            "access_denied": {k: list(v) for k, v in self.access_denied.items()},
            "browser_setup_failed": {k: list(v) for k, v in self.browser_setup_failed.items()},
        }


def authentication_failures(
    manifest_dir: Path,
    source_ids: Iterable[str],
    *,
    providers: Sequence[str] = BROWSER_PROVIDERS,
) -> AuthenticationDiagnosis:
    """Read download manifests and group unresolved papers by the challenge hit.

    Only attempts made by the given browser providers count.  A paper whose
    latest manifest ended in ``authentication_required`` or a captcha for a
    provider means that provider's saved session expired; a browser setup code
    means the environment (not the login) is the problem.
    """

    wanted = set(providers)
    login_required: dict[str, list[str]] = {}
    access_denied: dict[str, list[str]] = {}
    setup_failed: dict[str, list[str]] = {}
    for source_id in source_ids:
        manifest = load_json_object(Path(manifest_dir) / f"{source_id}.download.json")
        if manifest is None or manifest.get("status") == "downloaded":
            continue
        attempts = manifest.get("attempts")
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            provider = _attempt_provider(attempt)
            if provider is None or provider not in wanted:
                continue
            codes = _attempt_codes(attempt)
            if codes & LOGIN_CHALLENGE_CODES:
                login_required.setdefault(provider, []).append(source_id)
            elif codes & ACCESS_DENIED_CODES:
                access_denied.setdefault(provider, []).append(source_id)
            elif codes & BROWSER_SETUP_CODES:
                setup_failed.setdefault(provider, []).append(source_id)
    return AuthenticationDiagnosis(
        login_required={k: tuple(dict.fromkeys(v)) for k, v in login_required.items()},
        access_denied={k: tuple(dict.fromkeys(v)) for k, v in access_denied.items()},
        browser_setup_failed={k: tuple(dict.fromkeys(v)) for k, v in setup_failed.items()},
    )


__all__ = [
    "AuthenticationDiagnosis",
    "BROWSER_PROVIDERS",
    "BrowserLoginError",
    "IN_SESSION_LOGIN_PROVIDERS",
    "KCI_BIBTEX_PROVIDER",
    "LOGIN_MODES",
    "PreflightResult",
    "ProfileStatus",
    "authentication_failures",
    "inspect_profile",
    "is_interactive_session",
    "kci_bibtex_profile_dir",
    "manual_login_instructions",
    "preflight_browser_providers",
    "profile_dir_for",
    "run_manual_login",
]
