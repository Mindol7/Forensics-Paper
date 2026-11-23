from __future__ import annotations

from pathlib import Path

import pytest

from PaperPipeline.jams_browser import JamsBrowserOperationError
from PaperPipeline.kyobo_browser import KyoboBrowserOperationError
from scripts import jams_browser_auth, kyobo_browser_auth


@pytest.mark.parametrize(
    ("module", "environment_name", "configured"),
    [
        (kyobo_browser_auth, "KYOBO_BROWSER_PROFILE_DIR", "/tmp/kyobo-auth"),
        (jams_browser_auth, "JAMS_BROWSER_PROFILE_DIR", "/tmp/jams-auth"),
    ],
)
def test_profile_dir_comes_from_the_site_specific_environment(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    environment_name: str,
    configured: str,
) -> None:
    monkeypatch.setenv(environment_name, configured)

    args = module.build_parser().parse_args([])  # type: ignore[attr-defined]

    assert args.profile_dir == configured


@pytest.mark.parametrize("module", [kyobo_browser_auth, jams_browser_auth])
@pytest.mark.parametrize(
    "forbidden_argument",
    ["--username", "--password", "--cookie", "--token"],
)
def test_parser_does_not_accept_authentication_secrets(
    module: object,
    forbidden_argument: str,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        module.build_parser().parse_args(  # type: ignore[attr-defined]
            [forbidden_argument, "secret"]
        )

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("module", "runner_name"),
    [
        (kyobo_browser_auth, "run_interactive_kyobo_login"),
        (jams_browser_auth, "run_interactive_jams_login"),
    ],
)
def test_main_passes_repository_boundary_to_interactive_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: object,
    runner_name: str,
) -> None:
    calls: list[tuple[Path, Path | None]] = []

    def fake_runner(
        profile_dir: Path,
        *,
        repository_root: Path | None,
    ) -> None:
        calls.append((profile_dir, repository_root))

    monkeypatch.setattr(module, runner_name, fake_runner)

    assert module.main(["--profile-dir", str(tmp_path)]) == 0  # type: ignore[attr-defined]
    assert calls == [(tmp_path, module.REPOSITORY_ROOT)]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("module", "runner_name"),
    [
        (kyobo_browser_auth, "run_interactive_kyobo_login"),
        (jams_browser_auth, "run_interactive_jams_login"),
    ],
)
def test_main_returns_130_when_manual_login_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: object,
    runner_name: str,
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(module, runner_name, interrupted)

    assert module.main(["--profile-dir", str(tmp_path)]) == 130  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("module", "runner_name", "operation_error"),
    [
        (
            kyobo_browser_auth,
            "run_interactive_kyobo_login",
            KyoboBrowserOperationError,
        ),
        (
            jams_browser_auth,
            "run_interactive_jams_login",
            JamsBrowserOperationError,
        ),
    ],
)
def test_main_reports_only_sanitized_browser_operation_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    module: object,
    runner_name: str,
    operation_error: type[RuntimeError],
) -> None:
    def failed(*args: object, **kwargs: object) -> None:
        raise operation_error(
            "configuration_error",
            "browser_dependency_missing",
            "dependency_missing",
        )

    monkeypatch.setattr(module, runner_name, failed)

    with pytest.raises(SystemExit) as exc_info:
        module.main(["--profile-dir", str(tmp_path)])  # type: ignore[attr-defined]

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "browser_dependency_missing" in stderr
    assert "secret" not in stderr
