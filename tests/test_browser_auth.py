from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from Collector import browser_auth
from Collector.browser_auth import (
    BrowserLoginError,
    ProfileStatus,
    authentication_failures,
    inspect_profile,
    is_interactive_session,
    manual_login_instructions,
    preflight_browser_providers,
)
from PaperPipeline.dbpia_browser import initialize_dbpia_profile_dir


def _status(provider: str, valid: bool, exists: bool | None = None) -> ProfileStatus:
    return ProfileStatus(
        provider=provider,
        profile_dir=Path(f"/profiles/{provider}"),
        exists=valid if exists is None else exists,
        valid=valid,
        detail="" if valid else "profile directory is missing",
    )


def test_inspect_profile_reports_missing_invalid_and_valid_profiles(tmp_path: Path) -> None:
    missing = inspect_profile("dbpia", tmp_path / "absent")
    assert (missing.exists, missing.valid) == (False, False)
    assert "missing" in missing.detail

    # A directory without the project marker is not a usable profile.
    bare = tmp_path / "bare"
    bare.mkdir(mode=0o700)
    without_marker = inspect_profile("dbpia", bare)
    assert (without_marker.exists, without_marker.valid) == (True, False)

    # The provider's own initializer creates a valid one.
    initialize_dbpia_profile_dir(tmp_path / "dbpia", repository_root=tmp_path / "repo")
    valid = inspect_profile("dbpia", tmp_path / "dbpia")
    assert valid.valid is True and valid.detail == ""

    kci = inspect_profile("kci_bibtex", tmp_path / "kci")
    assert kci.valid is False
    (tmp_path / "kci").mkdir()
    (tmp_path / "kci" / ".forensics-paper-kci-bibtex-profile.json").write_text("{}")
    assert inspect_profile("kci_bibtex", tmp_path / "kci").valid is True

    with pytest.raises(ValueError, match="unknown browser provider"):
        inspect_profile("scihub")


def test_interactive_detection_requires_a_tty_and_a_display() -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    class Pipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    assert is_interactive_session(stdin=Tty(), environ={"DISPLAY": ":0"}, platform="linux") is True
    assert is_interactive_session(stdin=Tty(), environ={"WAYLAND_DISPLAY": "wayland-0"}, platform="linux") is True
    assert is_interactive_session(stdin=Tty(), environ={}, platform="linux") is False
    assert is_interactive_session(stdin=Pipe(), environ={"DISPLAY": ":0"}, platform="linux") is False
    assert is_interactive_session(stdin=Tty(), environ={}, platform="win32") is True
    assert is_interactive_session(stdin=None, environ={}, platform="linux") in (True, False)


def test_preflight_uses_saved_profiles_without_opening_a_browser() -> None:
    logins: list[str] = []
    result = preflight_browser_providers(
        ["dbpia", "kyobo", "jams"],
        login="auto",
        interactive=True,
        login_runner=logins.append,
        inspector=lambda provider: _status(provider, valid=True),
    )
    assert result.enabled == ("dbpia", "kyobo", "jams")
    assert result.logged_in == () and logins == [] and result.skipped == {}


def test_preflight_opens_the_login_only_for_missing_profiles_when_interactive() -> None:
    logins: list[str] = []
    states = {"dbpia": True, "kyobo": False, "jams": False}

    def inspector(provider: str) -> ProfileStatus:
        return _status(provider, valid=states[provider])

    def login(provider: str) -> None:
        logins.append(provider)
        if provider == "kyobo":
            raise BrowserLoginError("교보 Scholar 수동 로그인이 취소되었습니다")
        states[provider] = True  # the operator logged in; the profile now validates

    messages: list[str] = []
    result = preflight_browser_providers(
        ["dbpia", "kyobo", "jams"],
        login="auto",
        interactive=True,
        login_runner=login,
        inspector=inspector,
        notify=messages.append,
    )
    # Kyobo uses a separate headed window (cancelled here); JAMS keeps a
    # session cookie only, so it logs in inside the collection run instead.
    assert logins == ["kyobo"]
    assert result.enabled == ("dbpia", "jams")
    assert result.logged_in == ()
    assert result.in_session_login == ("jams",)
    assert result.provider_options() == {"jams": {"interactive_login": True}}
    assert "취소" in result.skipped["kyobo"]
    assert any("Enter" in message for message in messages)
    assert result.to_dict()["in_session_login"] == ["jams"]


def test_in_session_providers_never_use_a_separate_login_window(tmp_path: Path) -> None:
    with pytest.raises(BrowserLoginError, match="세션 쿠키"):
        browser_auth.run_manual_login("jams", tmp_path / "profile")
    # Unattended: a JAMS profile without a live session is skipped with instructions.
    result = preflight_browser_providers(
        ["jams"], login="auto", interactive=False,
        inspector=lambda provider: _status(provider, valid=False),
    )
    assert result.enabled == () and "jams" in result.skipped


def test_preflight_never_opens_a_browser_when_unattended() -> None:
    logins: list[str] = []
    result = preflight_browser_providers(
        ["dbpia", "kyobo"],
        login="auto",
        interactive=False,
        login_runner=logins.append,
        inspector=lambda provider: _status(provider, valid=provider == "dbpia"),
    )
    assert logins == []
    assert result.enabled == ("dbpia",)
    assert "scripts/kyobo_browser_auth.py" in result.skipped["kyobo"]
    # ``always`` degrades to saved profiles when nobody can log in.
    unattended = preflight_browser_providers(
        ["dbpia"], login="always", interactive=False, login_runner=logins.append,
        inspector=lambda provider: _status(provider, valid=True),
    )
    assert unattended.enabled == ("dbpia",) and logins == []


def test_preflight_always_logs_in_before_crawling_and_never_mode_skips_logins() -> None:
    logins: list[str] = []
    result = preflight_browser_providers(
        ["jams", "dbpia"],
        login="always",
        interactive=True,
        login_runner=logins.append,
        inspector=lambda provider: _status(provider, valid=True),
    )
    assert logins == ["dbpia"]
    assert result.logged_in == ("dbpia",)
    assert result.in_session_login == ("jams",)

    never = preflight_browser_providers(
        ["dbpia"], login="never", interactive=True, login_runner=logins.append,
        inspector=lambda provider: _status(provider, valid=False),
    )
    assert logins == ["dbpia"] and never.enabled == () and "dbpia" in never.skipped
    with pytest.raises(ValueError, match="login must be one of"):
        preflight_browser_providers(["dbpia"], login="sometimes", interactive=True)
    with pytest.raises(ValueError, match="unknown browser providers"):
        preflight_browser_providers(["scihub"], interactive=True)


def _manifest(path: Path, source_id: str, attempts: list[dict], status: str = "authentication_required") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{source_id}.download.json").write_text(
        json.dumps({"schema_version": "1.0", "status": status, "attempts": attempts}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_authentication_failures_group_expired_sessions_by_provider(tmp_path: Path) -> None:
    _manifest(tmp_path, "ART000000001", [
        {"resolver": "dbpia-authenticated-browser:dbpia-browser-search", "status": "failed",
         "error_code": "authentication_required"},
        {"resolver": "dbpia-authenticated-browser", "platform": "dbpia", "status": "failed",
         "resolution_status": "authentication_required", "reason": "dbpia_authentication_requires_manual_refresh"},
    ])
    _manifest(tmp_path, "ART000000002", [
        {"resolver": "kyobo-authenticated-browser", "platform": "kyobo", "status": "failed",
         "resolution_status": "captcha_required"},
    ], status="access_denied")
    _manifest(tmp_path, "ART000000006", [
        {"resolver": "kyobo-authenticated-browser", "platform": "kyobo", "status": "failed",
         "resolution_status": "access_denied", "reason": "kyobo_subscription_or_access_gate_visible"},
    ], status="access_denied")
    _manifest(tmp_path, "ART000000003", [
        {"resolver": "kdfs-jams-authenticated-browser", "platform": "jams", "status": "failed",
         "resolution_status": "configuration_error", "error_code": "browser_launch_failed"},
    ], status="configuration_error")
    _manifest(tmp_path, "ART000000004", [
        {"resolver": "semantic-scholar-metadata", "status": "skipped", "resolution_status": "identity_mismatch"},
    ], status="not_found")
    _manifest(tmp_path, "ART000000005", [
        {"resolver": "dbpia-authenticated-browser", "platform": "dbpia", "status": "ok"},
    ], status="downloaded")

    diagnosis = authentication_failures(
        tmp_path, ["ART000000001", "ART000000002", "ART000000003", "ART000000004", "ART000000005", "ART000000006", "ART000000009"]
    )
    assert diagnosis.login_required == {"dbpia": ("ART000000001",), "kyobo": ("ART000000002",)}
    assert diagnosis.access_denied == {"kyobo": ("ART000000006",)}
    assert diagnosis.browser_setup_failed == {"jams": ("ART000000003",)}
    assert diagnosis.providers_needing_login == ("dbpia", "kyobo")
    # Restricting to the providers actually enabled hides the others.
    assert authentication_failures(tmp_path, ["ART000000001", "ART000000002"], providers=["kyobo"]).login_required == {
        "kyobo": ("ART000000002",)
    }
    assert diagnosis.to_dict()["login_required"]["dbpia"] == ["ART000000001"]


def test_manual_login_wraps_provider_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def cancelled(profile_dir, *, repository_root, input_fn):
        raise KeyboardInterrupt

    monkeypatch.setitem(
        browser_auth._SPECS, "dbpia", replace(browser_auth._SPECS["dbpia"], login=cancelled)
    )
    with pytest.raises(BrowserLoginError, match="취소"):
        browser_auth.run_manual_login("dbpia", tmp_path / "profile")
    assert "scripts/dbpia_browser_auth.py" in manual_login_instructions(["dbpia"])
    assert "--special-interactive-login" in manual_login_instructions(["kci_bibtex"])
