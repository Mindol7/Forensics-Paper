from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import main
import pytest
from Collector.kci_metadata import PipelineResult
from Scheduler import job


def _result(value: int = 1, path: Path | None = None) -> PipelineResult:
    return PipelineResult(value, value, value, value, value, path)


def _fake_run_result(stages: tuple[str, ...] = (), failed: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        stages=[SimpleNamespace(name=name, status="ok") for name in stages],
        ledger_path=Path("reports/pipeline_runs/test.json"),
        failed=failed,
    )


def test_main_without_arguments_runs_the_full_pipeline(monkeypatch) -> None:
    from Scheduler.pipeline import DEFAULT_STAGES

    captured: dict[str, object] = {}

    def fake_run(options, *, run_date=None, settings=None):
        captured["options"] = options
        captured["run_date"] = run_date
        return _fake_run_result(options.stages)

    monkeypatch.setattr(main, "run_full_pipeline", fake_run)

    assert main.main([]) == 0
    options = captured["options"]
    assert options.stages == DEFAULT_STAGES
    assert "filter" not in options.stages
    assert options.collect_pdfs is True
    assert options.browser_login == "auto"
    assert options.interactive is None
    assert captured["run_date"] is None


def test_main_stage_flags_run_only_those_stages_in_pipeline_order(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        main,
        "run_full_pipeline",
        lambda options, **kwargs: captured.update(options=options, **kwargs) or _fake_run_result(options.stages),
    )

    assert (
        main.main(
            [
                "-graph", "-all", "-derive", "-filter",
                "--browser-login", "always", "--non-interactive",
                "--analysis-backend", "claude-code", "--analysis-limit", "2",
                "--browser-providers", "dbpia,kyobo", "--skip-export", "--run-date", "2026-09-16",
            ]
        )
        == 0
    )
    options = captured["options"]
    assert options.stages == ("metadata", "filter", "type", "keywords", "jev", "results", "graphs")
    assert options.browser_login == "always"
    assert options.interactive is False
    assert options.analysis_backend == "claude-code"
    assert options.analysis_limit == 2
    assert options.browser_providers == ("dbpia", "kyobo")
    assert options.skip_export is True
    assert captured["run_date"] == date(2026, 9, 16)

    # Every stage flag maps to a real pipeline stage.
    from Scheduler.pipeline import STAGES

    covered = {stage for _short, _long, stages, _help in main.STAGE_OPTIONS for stage in stages}
    assert covered == set(STAGES)


def test_main_help_exits_cleanly_and_bad_options_fail(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        main,
        "run_full_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("pipeline called")),
    )
    with pytest.raises(SystemExit) as exc_info:
        main.main(["-h"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    for short, _long, _stages, _help in main.STAGE_OPTIONS:
        assert short in help_text

    with pytest.raises(SystemExit) as exc_info:
        main.main(["--browser-providers", "scihub"])
    assert exc_info.value.code == 2
    with pytest.raises(SystemExit) as exc_info:
        main.main(["--run-date", "yesterday"])
    assert exc_info.value.code == 2

    # Abbreviated option names must not start a stage (argparse prefix matching is off).
    for argv in (["-m"], ["-al"], ["--al"], ["-g"], ["-s"], ["--dry-ru"]):
        with pytest.raises(SystemExit) as exc_info:
            main.main(argv)
        assert exc_info.value.code == 2, argv


@pytest.mark.parametrize(
    ("argv", "field", "expected"),
    [
        (["--dry-run"], "dry_run", True),
        (["--stop-on-error"], "stop_on_error", True),
        (["--dry-run-pdf"], "dry_run_pdf", True),
        (["--skip-export"], "skip_export", True),
        (["--enable-sciencedirect"], "enable_sciencedirect", True),
        (["--special-interactive-login"], "special_interactive_login", True),
        (["--analysis-model", "claude-sonnet-5"], "analysis_model", "claude-sonnet-5"),
        (["--analysis-force"], "analysis_force", True),
        (["--analysis-backend", "api"], "analysis_backend", "api"),
        (["--skip-web-build"], "run_web_build", False),
        (["--interactive"], "interactive", True),
        (["--non-interactive"], "interactive", False),
        ([], "interactive", None),
        (["--browser-providers", "none"], "browser_providers", ()),
        (["--browser-providers", "auto"], "browser_providers", None),
        (["--browser-providers", "jams"], "browser_providers", ("jams",)),
        (["--browser-login", "never"], "browser_login", "never"),
        ([], "browser_login", "auto"),
        ([], "collect_pdfs", True),
    ],
)
def test_main_flags_map_onto_pipeline_options(argv, field, expected) -> None:
    options = main.options_from_arguments(main.build_argument_parser().parse_args(argv))
    assert getattr(options, field) == expected


def test_run_full_pipeline_only_requires_the_kci_key_for_metadata(monkeypatch) -> None:
    from Scheduler.pipeline import PipelineOptions

    validations: list[bool] = []
    runs: list[object] = []
    monkeypatch.setattr(main, "load_settings", lambda validate: validations.append(validate) or SimpleNamespace(validate=validate))
    monkeypatch.setattr(
        main.pipeline_module, "run_pipeline",
        lambda options, settings=None, run_date=None: runs.append((options, settings, run_date)) or _fake_run_result(options.stages),
    )
    main.run_full_pipeline(PipelineOptions(stages=("metadata", "pdf")))
    main.run_full_pipeline(PipelineOptions(stages=("pdf",)))
    main.run_full_pipeline(PipelineOptions(stages=("metadata",), dry_run=True))
    main.run_full_pipeline(PipelineOptions(stages=("md",)), run_date=date(2026, 9, 1))
    assert validations == [True, False, False, False]
    assert runs[-1][2] == date(2026, 9, 1)
    # An explicit settings object is passed through untouched.
    explicit = SimpleNamespace(validate="explicit")
    main.run_full_pipeline(PipelineOptions(stages=("md",)), settings=explicit)
    assert runs[-1][1] is explicit and len(validations) == 4


def test_main_reports_failure_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(
        main, "run_full_pipeline", lambda options, **_kwargs: _fake_run_result(options.stages, failed=True)
    )
    assert main.main(["-md"]) == 1


def test_monthly_job_can_skip_metadata_and_only_collect_pdfs(monkeypatch, tmp_path: Path) -> None:
    settings = SimpleNamespace(database_url="sqlite:///pipeline.db", export_dir=tmp_path / "reports")
    database = SimpleNamespace(create_tables=lambda: None)
    events: list[str] = []
    monkeypatch.setattr(job, "DatabaseManager", lambda _url: database)
    monkeypatch.setattr(
        job,
        "run_kci_metadata_pipeline",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("metadata must not run")),
    )
    monkeypatch.setattr(job, "run_kci_pdf_collection", lambda **kwargs: events.append("pdf") or SimpleNamespace(current_count=1, unresolved_source_ids=()))
    monkeypatch.setattr(job, "export_pdf_statistics", lambda *args, **kwargs: {"output": tmp_path / "pdf.xlsx"})
    monkeypatch.setattr(job, "sqlite_database_path", lambda _url: tmp_path / "pipeline.db")

    result = job.run_kci_job(
        settings=settings, run_date=date(2026, 8, 1), collect_metadata=False,
        papers_dir=tmp_path / "Paper", manifest_dir=tmp_path / "Manifest",
    )

    assert events == ["pdf"]
    assert result.corpus.raw_count == 0 and result.incremental_match.stored_count == 0
    assert result.pdf.current_count == 1
    assert result.target_months == ()  # nothing was fetched, so nothing is reported


def test_recent_completed_months_crosses_year_boundary() -> None:
    assert job.recent_completed_months(date(2026, 1, 1)) == (
        "202511",
        "202512",
    )


def test_database_target_months_follows_the_corpus_checkpoint() -> None:
    done = [f"2026{month:02d}" for month in range(1, 7)]

    # Everything after the last completed month up to the last full calendar
    # month, plus the two most recent completed months for late registrations.
    assert job.database_target_months(done, date(2026, 9, 16)) == (
        "202605", "202606", "202607", "202608",
    )
    # A skipped quarter is caught up in full instead of being lost.
    assert job.database_target_months(done, date(2027, 1, 1)) == tuple(
        f"2026{month:02d}" for month in range(5, 13)
    )
    # Nothing new: only the recheck window remains, so the job stays idempotent.
    assert job.database_target_months(done + ["202607", "202608"], date(2026, 9, 16)) == (
        "202607", "202608",
    )
    # The running month is never fetched.
    assert job.database_target_months(done + ["202607", "202608"], date(2026, 9, 1)) == (
        "202607", "202608",
    )
    # An empty corpus needs an explicit starting month.
    assert job.database_target_months([], date(2026, 3, 1), earliest_month="202601") == (
        "202601", "202602",
    )
    with pytest.raises(ValueError):
        job.database_target_months([], date(2026, 3, 1))
    with pytest.raises(ValueError):
        job.database_target_months(["2026-06"], date(2026, 9, 1))


def test_monthly_job_accepts_explicit_target_months(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_pipeline(**kwargs):
        calls.append(kwargs)
        return PipelineResult(1, 1, 1, 1, 1, None)

    class FakeDatabase:
        def __init__(self, url: str) -> None:
            self.url = url

        def create_tables(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(job, "run_kci_metadata_pipeline", fake_pipeline)
    monkeypatch.setattr(job, "DatabaseManager", FakeDatabase)
    settings = SimpleNamespace(database_url="sqlite:///:memory:")

    result = job.run_kci_job(
        settings=settings,
        run_date=date(2026, 9, 16),
        require_first_day=False,
        target_months=("202605", "202606", "202607", "202608"),
        collect_pdfs=False,
        skip_export=True,
    )

    assert result.target_months == ("202605", "202606", "202607", "202608")
    assert calls[0]["sweep_months_override"] == [
        ("202605", "202605"), ("202606", "202606"), ("202607", "202607"), ("202608", "202608"),
    ]
    with pytest.raises(ValueError):
        job.run_kci_job(
            settings=settings, run_date=date(2026, 9, 16), require_first_day=False,
            target_months=(), collect_pdfs=False,
        )


def test_monthly_job_builds_recent_months_then_incrementally_matches(monkeypatch) -> None:
    settings = SimpleNamespace(database_url="sqlite://")
    database = SimpleNamespace(create_tables=lambda: None)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(job, "DatabaseManager", lambda _url: database)

    def fake_metadata(**kwargs):
        calls.append(kwargs)
        return _result(len(calls))

    monkeypatch.setattr(job, "run_kci_metadata_pipeline", fake_metadata)

    result = job.run_kci_job(
        settings=settings,
        run_date=date(2026, 8, 1),
        collect_pdfs=False,
    )

    assert result.target_months == ("202606", "202607")
    assert calls[0]["build_corpus"] is True
    assert calls[0]["sweep_months_override"] == [
        ("202606", "202606"),
        ("202607", "202607"),
    ]
    assert calls[1]["match_corpus"] is True
    assert calls[1]["match_incremental"] is True
    assert "incremental_count" not in job.JobResult.__dataclass_fields__


def test_monthly_job_composes_metadata_pdf_then_report(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    captured_pdf: dict[str, object] = {}
    captured_report: dict[str, object] = {}
    settings = SimpleNamespace(
        database_url="sqlite:///pipeline.db",
        export_dir=tmp_path / "reports",
    )
    database = SimpleNamespace(create_tables=lambda: None)
    papers_dir = tmp_path / "Analyzer" / "Paper"
    manifest_dir = tmp_path / "Analyzer" / "Manifest"
    legacy_dir = tmp_path / "Papers"
    legacy_dir.mkdir()
    database_path = tmp_path / "pipeline.db"
    pdf_result = SimpleNamespace(current_count=5)

    monkeypatch.setattr(job, "DatabaseManager", lambda _url: database)

    def fake_metadata(**kwargs):
        if kwargs.get("build_corpus"):
            events.append("metadata-corpus")
            return _result(2)
        events.append("metadata-match")
        return _result(3, tmp_path / "metadata.xlsx")

    def fake_pdf(**kwargs):
        events.append("pdf")
        captured_pdf.update(kwargs)
        return pdf_result

    def fake_report(database_file, artifact_dir, **kwargs):
        events.append("report")
        captured_report.update(
            database=database_file,
            papers_dir=artifact_dir,
            **kwargs,
        )
        return {"output": tmp_path / "reports" / "pdf-statistics.xlsx"}

    monkeypatch.setattr(job, "run_kci_metadata_pipeline", fake_metadata)
    monkeypatch.setattr(job, "run_kci_pdf_collection", fake_pdf)
    monkeypatch.setattr(job, "export_pdf_statistics", fake_report)
    monkeypatch.setattr(job, "sqlite_database_path", lambda _url: database_path)

    result = job.run_kci_job(
        settings=settings,
        run_date=date(2026, 8, 1),
        papers_dir=papers_dir,
        manifest_dir=manifest_dir,
        compatibility_papers_dirs=(legacy_dir,),
        enable_dbpia=True,
        enable_kyobo=True,
        enable_jams=True,
        enable_sciencedirect=True,
        dry_run_pdf=True,
        pdf_provider_options={"semantic_scholar": {"enabled": False}},
    )

    assert events == ["metadata-corpus", "metadata-match", "pdf", "report"]
    assert captured_pdf["database_url"] == settings.database_url
    assert captured_pdf["papers_dir"] == papers_dir
    assert captured_pdf["manifest_dir"] == manifest_dir
    assert captured_pdf["compatibility_papers_dirs"] == (legacy_dir,)
    assert captured_pdf["enable_dbpia"] is True
    assert captured_pdf["enable_kyobo"] is True
    assert captured_pdf["enable_jams"] is True
    assert captured_pdf["enable_sciencedirect"] is True
    assert captured_pdf["dry_run"] is True
    assert captured_pdf["provider_options"] == {
        "semantic_scholar": {"enabled": False},
        # the prosecutors' office public board is part of every automatic run
        "kci": {"enable_spo_download": True},
    }
    assert captured_report == {
        "database": database_path,
        "papers_dir": papers_dir,
        "manifest_dir": manifest_dir,
        "output_dir": settings.export_dir,
        "compatibility_papers_dirs": (legacy_dir,),
    }
    assert result.pdf is pdf_result
    assert result.pdf_report_path == tmp_path / "reports" / "pdf-statistics.xlsx"


def test_monthly_job_rejects_non_first_day_before_database_access(monkeypatch) -> None:
    monkeypatch.setattr(
        job,
        "DatabaseManager",
        lambda _url: (_ for _ in ()).throw(AssertionError("database accessed")),
    )

    with pytest.raises(ValueError, match="first day"):
        job.run_kci_job(
            settings=SimpleNamespace(database_url="sqlite://"),
            run_date=date(2026, 8, 2),
        )


def test_scheduler_cli_exposes_explicit_catch_up_mode(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_job(**kwargs):
        captured.update(kwargs)
        return job.JobResult(
            target_months=("202606", "202607"),
            corpus=_result(2),
            incremental_match=_result(3),
        )

    monkeypatch.setattr(job, "run_kci_job", fake_job)

    assert (
        job.main(
            [
                "--allow-non-first-day",
                "--skip-export",
                "--dry-run-pdf",
                "--enable-dbpia",
                "--enable-kyobo",
                "--enable-jams",
                "--enable-sciencedirect",
            ]
        )
        == 0
    )
    assert captured == {
        "require_first_day": False,
        "skip_export": True,
        "collect_pdfs": True,
        "enable_dbpia": True,
        "enable_kyobo": True,
        "enable_jams": True,
        "enable_sciencedirect": True,
        "dry_run_pdf": True,
        "browser_login": "never",
    }
    assert "months=202606,202607" in capsys.readouterr().out


def test_scheduler_cli_rejects_dbpia_when_pdf_collection_is_skipped(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        job,
        "run_kci_job",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("job called")),
    )

    with pytest.raises(SystemExit) as exc_info:
        job.main(["--skip-pdf", "--enable-dbpia"])

    assert exc_info.value.code == 2


def test_monthly_job_logs_in_again_and_retries_after_an_expired_browser_session(
    monkeypatch, tmp_path: Path
) -> None:
    from Collector.browser_auth import AuthenticationDiagnosis, PreflightResult

    settings = SimpleNamespace(database_url="sqlite:///pipeline.db", export_dir=tmp_path)
    database = SimpleNamespace(create_tables=lambda: None, close=lambda: None)
    monkeypatch.setattr(job, "DatabaseManager", lambda _url: database)
    monkeypatch.setattr(job, "run_kci_metadata_pipeline", lambda **_kwargs: _result())

    preflights: list[dict] = []
    monkeypatch.setattr(
        job,
        "preflight_browser_providers",
        lambda providers, **kwargs: (
            preflights.append({"providers": list(providers), **{k: v for k, v in kwargs.items() if k != "notify"}}),
            PreflightResult(login_mode=kwargs["login"], interactive=True, enabled=tuple(providers)),
        )[1],
    )
    collections: list[dict] = []

    def fake_collection(**kwargs):
        collections.append(kwargs)
        unresolved = ("ART000000001", "ART000000002") if len(collections) == 1 else ("ART000000002",)
        return SimpleNamespace(
            current_count=10,
            selected_count=12,
            unresolved_source_ids=unresolved,
        )

    monkeypatch.setattr(job, "run_kci_pdf_collection", fake_collection)
    monkeypatch.setattr(
        job,
        "authentication_failures",
        lambda manifest_dir, source_ids, providers: AuthenticationDiagnosis(
            login_required={"dbpia": ("ART000000001",)} if len(collections) == 1 else {}
        ),
    )
    logins: list[str] = []
    monkeypatch.setattr(job, "run_manual_login", logins.append)

    result = job.run_kci_job(
        settings=settings,
        run_date=date(2026, 9, 1),
        skip_export=True,
        enable_dbpia=True,
        enable_kyobo=True,
        browser_login="auto",
        interactive=True,
        papers_dir=tmp_path / "Paper",
        manifest_dir=tmp_path / "Manifest",
    )

    assert preflights == [{"providers": ["dbpia", "kyobo"], "login": "auto", "interactive": True}]
    assert logins == ["dbpia"]
    assert len(collections) == 2
    first, retry = collections
    assert first["enable_dbpia"] is True and first["enable_kyobo"] is True
    # The retry runs only the re-authenticated provider over every unresolved paper.
    assert set(retry["runners"]) == {job.PdfProvider.DBPIA}
    assert retry["source_ids"] == ("ART000000001", "ART000000002")
    assert retry["enable_dbpia"] is True and retry["enable_kyobo"] is False
    assert result.pdf_retry is not None and result.pdf_retry.unresolved_source_ids == ("ART000000002",)
    assert result.browser_auth.login_required == {"dbpia": ("ART000000001",)}


def test_monthly_job_skips_browser_providers_without_profiles_when_unattended(monkeypatch, tmp_path: Path) -> None:
    from Collector.browser_auth import AuthenticationDiagnosis, PreflightResult

    settings = SimpleNamespace(database_url="sqlite:///pipeline.db", export_dir=tmp_path)
    database = SimpleNamespace(create_tables=lambda: None, close=lambda: None)
    monkeypatch.setattr(job, "DatabaseManager", lambda _url: database)
    monkeypatch.setattr(job, "run_kci_metadata_pipeline", lambda **_kwargs: _result())
    monkeypatch.setattr(
        job,
        "preflight_browser_providers",
        lambda providers, **kwargs: PreflightResult(
            login_mode="never", interactive=False, enabled=(), skipped={"jams": "profile missing"}
        ),
    )
    collections: list[dict] = []
    monkeypatch.setattr(
        job,
        "run_kci_pdf_collection",
        lambda **kwargs: (collections.append(kwargs), SimpleNamespace(current_count=1, selected_count=1, unresolved_source_ids=()))[1],
    )
    monkeypatch.setattr(job, "authentication_failures", lambda *a, **k: AuthenticationDiagnosis())
    monkeypatch.setattr(job, "run_manual_login", lambda *_a: pytest.fail("no login in unattended mode"))

    result = job.run_kci_job(
        settings=settings, run_date=date(2026, 9, 1), skip_export=True, enable_jams=True,
        browser_login="never", interactive=False,
        papers_dir=tmp_path / "Paper", manifest_dir=tmp_path / "Manifest",
    )
    assert collections[0]["enable_jams"] is False
    assert result.browser_preflight.skipped == {"jams": "profile missing"}
    assert result.pdf_retry is None


def test_monthly_job_fetches_located_sources_for_papers_the_providers_missed(monkeypatch, tmp_path: Path) -> None:
    settings = SimpleNamespace(database_url="sqlite:///pipeline.db", export_dir=tmp_path / "reports")
    monkeypatch.setattr(job, "DatabaseManager", lambda _url: SimpleNamespace(create_tables=lambda: None))
    monkeypatch.setattr(job, "run_kci_metadata_pipeline", lambda **_kwargs: _result())
    monkeypatch.setattr(
        job, "run_kci_pdf_collection",
        lambda **_kwargs: SimpleNamespace(current_count=1, unresolved_source_ids=("ART1", "ART2", "ART3")),
    )
    monkeypatch.setattr(job, "export_pdf_statistics", lambda *args, **kwargs: {"output": tmp_path / "pdf.xlsx"})
    monkeypatch.setattr(job, "sqlite_database_path", lambda _url: tmp_path / "pipeline.db")
    monkeypatch.setattr(job, "authentication_failures", lambda *args, **kwargs: job.AuthenticationDiagnosis())
    monkeypatch.setattr(job, "located_source_ids", lambda: {"ART2", "ART3", "ART9"})
    calls: list[dict[str, object]] = []

    def fake_located(**kwargs):
        calls.append(kwargs)
        return {"downloaded": ["ART2"], "already": [], "failed": {"ART3": "window_required: KISS"}}

    monkeypatch.setattr(job, "run_located_pdf_download", fake_located)

    unattended = job.run_kci_job(
        settings=settings, run_date=date(2026, 8, 1), papers_dir=tmp_path / "Paper", manifest_dir=tmp_path / "Manifest",
        interactive=False,
    )
    # Only papers that are both unresolved and in the table are requested; no window when unattended.
    assert calls[0]["only"] == {"ART2", "ART3"} and calls[0]["allow_window"] is False
    assert unattended.located_pdf["downloaded"] == ["ART2"]

    job.run_kci_job(
        settings=settings, run_date=date(2026, 8, 1), papers_dir=tmp_path / "Paper", manifest_dir=tmp_path / "Manifest",
        interactive=True, browser_login="auto",
    )
    assert calls[1]["allow_window"] is True
    job.run_kci_job(
        settings=settings, run_date=date(2026, 8, 1), papers_dir=tmp_path / "Paper", manifest_dir=tmp_path / "Manifest",
        interactive=True, browser_login="never",
    )
    assert calls[2]["allow_window"] is False
    # A dry-run never downloads.
    job.run_kci_job(
        settings=settings, run_date=date(2026, 8, 1), papers_dir=tmp_path / "Paper", manifest_dir=tmp_path / "Manifest",
        dry_run_pdf=True,
    )
    assert len(calls) == 3


def test_renumber_flag_rebuilds_everything_that_shows_numbers() -> None:
    from Scheduler.pipeline import expand_stages

    options = main.options_from_arguments(main.build_argument_parser().parse_args(["-renumber"]))
    assert options.stages == expand_stages(["renumber-all"]) == ("renumber", "type", "keywords", "jev", "results", "md", "graphs")
    # The earlier Claude classification stays reachable on request; the Jev AI results feed the profiles.
    assert main.options_from_arguments(main.build_argument_parser().parse_args(["-lv3", "-level5"])).stages == ("lv3", "level5")
    assert main.options_from_arguments(main.build_argument_parser().parse_args(["-md", "-jev"])).stages == ("jev", "md")
