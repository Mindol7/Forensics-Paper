from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from Collector.kci_metadata import KCI_CORPUS_PROGRESS_STATE_KEY
from Scheduler import pipeline
from Scheduler.pipeline import (
    DEFAULT_STAGES,
    STAGES,
    PipelineOptions,
    expand_stages,
    build_argument_parser,
    options_from_arguments,
    run_pipeline,
    target_months_from_database,
)
from Storage.db import DatabaseManager
from config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kci_open_api_key=None,
        database_url=f"sqlite:///{tmp_path / 'pipeline.db'}",
        export_dir=tmp_path / "reports",
    )


def _runner(name: str, status: str = "ok", log: list | None = None, error: Exception | None = None):
    def run(settings, options, run_date, logger):
        if log is not None:
            log.append(name)
        if error is not None:
            raise error
        return status, {"stage": name}

    return run


def test_stages_run_in_canonical_order_and_write_a_ledger(tmp_path: Path) -> None:
    log: list[str] = []
    runners = {name: _runner(name, log=log) for name in STAGES}
    options = PipelineOptions(stages=("md", "metadata", "type", "lv3", "keywords"))

    result = run_pipeline(
        options, settings=_settings(tmp_path), run_date=date(2026, 9, 16), runners=runners,
        ledger_dir=tmp_path / "ledger",
    )

    assert log == ["metadata", "type", "lv3", "keywords", "md"]
    assert [report.status for report in result.stages] == ["ok"] * 5
    assert result.failed is False
    ledger = json.loads(result.ledger_path.read_text(encoding="utf-8"))
    assert ledger["run_date"] == "2026-09-16"
    assert [stage["name"] for stage in ledger["stages"]] == log
    assert ledger["stages"][0]["details"] == {"stage": "metadata"}


def test_dependent_stages_are_skipped_after_a_failure(tmp_path: Path) -> None:
    log: list[str] = []
    runners = {name: _runner(name, log=log) for name in STAGES}
    runners["type"] = _runner("type", log=log, error=RuntimeError("stale evidence"))
    runners["oa"] = _runner("oa", status="partial", log=log)

    result = run_pipeline(
        PipelineOptions(), settings=_settings(tmp_path), run_date=date(2026, 9, 16), runners=runners,
        ledger_dir=tmp_path / "ledger",
    )

    by_name = {report.name: report for report in result.stages}
    assert by_name["type"].status == "failed"
    assert "stale evidence" in by_name["type"].error
    assert by_name["oa"].status == "partial"
    assert by_name["md"].status == "skipped" and by_name["md"].details["blocked_by"] == ["type"]
    assert by_name["graphs"].status == "skipped"
    # Independent stages still run after the failure: the jev stage reads the Jev AI results
    # itself, and the result workbooks report whatever is known.
    assert by_name["jev"].status == "ok"
    assert by_name["keywords"].status == "ok"
    assert by_name["results"].status == "ok"
    assert result.failed is True
    assert "keywords" in log and "jev" in log and "md" not in log
    assert "lv3" not in log and "level5" not in log   # the earlier Claude classification runs only on request


def test_stop_on_error_halts_the_run(tmp_path: Path) -> None:
    log: list[str] = []
    runners = {name: _runner(name, log=log) for name in STAGES}
    runners["metadata"] = _runner("metadata", status="failed", log=log)

    result = run_pipeline(
        PipelineOptions(stop_on_error=True), settings=_settings(tmp_path), run_date=date(2026, 9, 16),
        runners=runners, ledger_dir=tmp_path / "ledger",
    )
    assert [report.name for report in result.stages] == ["metadata"]
    assert log == ["metadata"]


def test_dry_run_reports_the_plan_without_running_stages(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANALYZER_MANIFEST_DIR", str(tmp_path / "Manifest"))
    monkeypatch.setenv("ANALYZER_PAPER_DIR", str(tmp_path / "Paper"))
    settings = _settings(tmp_path)
    database = DatabaseManager(settings.database_url)
    database.create_tables()
    database.set_state(KCI_CORPUS_PROGRESS_STATE_KEY, json.dumps(["202605", "202606"]))
    database.close()
    runners = {name: _runner(name, error=AssertionError("must not run")) for name in STAGES}

    result = run_pipeline(
        PipelineOptions(dry_run=True), settings=settings, run_date=date(2026, 9, 16), runners=runners,
        ledger_dir=tmp_path / "ledger",
    )

    assert all(report.status == "skipped" for report in result.stages)
    plan = result.stages[0].details["plan"]
    assert plan["target_months"] == ["202605", "202606", "202607", "202608"]
    assert plan["analysis_pending"] == []
    assert plan["claude_credentials"] in (True, False)
    assert json.loads(result.ledger_path.read_text(encoding="utf-8"))["dry_run"] is True


def test_target_months_follow_the_corpus_checkpoint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = DatabaseManager(settings.database_url)
    database.create_tables()
    database.set_state(KCI_CORPUS_PROGRESS_STATE_KEY, json.dumps(["202604", "202605", "202606"]))
    database.close()
    assert target_months_from_database(settings, date(2026, 9, 16)) == (
        "202605", "202606", "202607", "202608",
    )
    # An empty checkpoint starts from the configured first year.
    empty = Settings(kci_open_api_key=None, database_url=f"sqlite:///{tmp_path / 'empty.db'}", kci_start_year=2026)
    months = target_months_from_database(empty, date(2026, 3, 1))
    assert months == ("202601", "202602")


def test_cli_options_select_and_skip_stages() -> None:
    parser = build_argument_parser()
    options = options_from_arguments(parser.parse_args(["--stages", "lv3,type,md", "--skip-md", "--analysis-limit", "3"]))
    assert options.stages == ("type", "lv3")
    assert options.analysis_limit == 3
    with pytest.raises(ValueError, match="unknown stages"):
        options_from_arguments(parser.parse_args(["--stages", "nope"]))
    with pytest.raises(ValueError, match="no stages left"):
        options_from_arguments(parser.parse_args(["--stages", "md", "--skip-md"]))


def test_default_stages_skip_the_on_demand_filter_and_aliases_expand() -> None:
    # The user's required order: 수집 -> 필터 -> PDF -> 분석 -> OA/Special/참고문헌 -> 유형·Level·태그 -> MD -> 그래프.
    assert STAGES == (
        "metadata", "filter", "pdf", "renumber", "analyze", "oa", "special", "ref", "type", "lv3", "keywords", "level5", "jev",
        "results", "md", "graphs",
    )
    # Level·태그·대상 애플리케이션 come from the Jev AI results (jev); the earlier Claude stages run on request only.
    assert DEFAULT_STAGES == tuple(stage for stage in STAGES if stage not in ("filter", "lv3", "level5"))
    assert expand_stages(["graph", "collect", "derive"]) == ("metadata", "pdf", "type", "keywords", "jev", "results", "graphs")
    assert expand_stages(["default", "filter"]) == tuple(stage for stage in STAGES if stage not in ("lv3", "level5"))
    assert expand_stages(["lv3", "level5"]) == ("lv3", "level5")
    with pytest.raises(ValueError, match="unknown stages"):
        expand_stages(["all"])  # 'all' is main.py's "metadata only" flag, not a pipeline alias
    parser = build_argument_parser()
    options = options_from_arguments(parser.parse_args(["--stages", "collect,derive", "--skip-pdf"]))
    assert options.stages == ("metadata", "type", "keywords", "jev", "results")
    assert options.collect_pdfs is False
    assert options_from_arguments(parser.parse_args(["--skip-collect"])).stages == DEFAULT_STAGES[2:]
    assert options_from_arguments(parser.parse_args([])).stages == DEFAULT_STAGES


def test_metadata_and_pdf_stages_split_the_monthly_job(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    calls: list[dict] = []

    def fake_job(**kwargs):
        calls.append(kwargs)
        match = SimpleNamespace(relevant_count=2, stored_count=2, new_source_ids=("ART1", "ART2"))
        pdf = SimpleNamespace(current_count=1, selected_count=2, unresolved_source_ids=("ART2",))
        return SimpleNamespace(
            target_months=("202605",), corpus=SimpleNamespace(raw_count=10, stored_count=10),
            incremental_match=match, export_path=None, pdf=pdf if kwargs["collect_pdfs"] else None,
            pdf_report_path=None, browser_preflight=None, browser_auth=None, pdf_retry=None,
        )

    monkeypatch.setattr(pipeline, "run_kci_job", fake_job)
    monkeypatch.setattr(pipeline, "target_months_from_database", lambda settings, run_date: ("202605",))
    monkeypatch.setattr(pipeline, "browser_providers_for", lambda options: ("dbpia",))
    settings = _settings(tmp_path)
    logger = pipeline.ProgressLogger()

    status, details = pipeline._metadata_stage(settings, PipelineOptions(), date(2026, 9, 16), logger)
    assert status == "ok" and details["new_source_ids"] == ["ART1", "ART2"] and "pdf_current" not in details
    assert calls[-1]["collect_metadata"] is True and calls[-1]["collect_pdfs"] is False
    assert calls[-1]["enable_dbpia"] is False  # no browser crawling without PDFs

    status, details = pipeline._pdf_stage(settings, PipelineOptions(browser_login="always"), date(2026, 9, 16), logger)
    assert status == "ok" and details["pdf_current"] == 1 and details["pdf_unresolved_source_ids"] == ["ART2"]
    assert "new_source_ids" not in details
    assert calls[-1]["collect_metadata"] is False and calls[-1]["collect_pdfs"] is True
    assert calls[-1]["enable_dbpia"] is True and calls[-1]["browser_login"] == "always"

    status, details = pipeline._pdf_stage(settings, PipelineOptions(collect_pdfs=False), date(2026, 9, 16), logger)
    assert status == "skipped" and details == {"reason": "--skip-pdf"}

    # Nothing to fetch: the stage says so instead of silently fetching the recent months.
    monkeypatch.setattr(pipeline, "target_months_from_database", lambda settings, run_date: ())
    before = len(calls)
    status, details = pipeline._metadata_stage(settings, PipelineOptions(), date(2026, 9, 16), logger)
    assert status == "skipped" and details["target_months"] == [] and len(calls) == before


def test_pdf_stage_reports_expired_sessions_and_retries(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from Collector.browser_auth import AuthenticationDiagnosis, PreflightResult

    state: dict = {"retry": None}

    def fake_job(**kwargs):
        pdf = SimpleNamespace(current_count=3, selected_count=5, unresolved_source_ids=("ART2", "ART3"))
        return SimpleNamespace(
            target_months=(), corpus=None, incremental_match=None, export_path=None, pdf=pdf,
            pdf_report_path=tmp_path / "pdf.xlsx",
            browser_preflight=PreflightResult(login_mode="auto", interactive=False),
            browser_auth=AuthenticationDiagnosis(login_required={"dbpia": ("ART2", "ART3")}),
            pdf_retry=state["retry"],
        )

    monkeypatch.setattr(pipeline, "run_kci_job", fake_job)
    monkeypatch.setattr(pipeline, "browser_providers_for", lambda options: ("dbpia",))
    settings = _settings(tmp_path)
    logger = pipeline.ProgressLogger()

    # Unattended run: the session expired and nobody could log in -> partial + instructions.
    status, details = pipeline._pdf_stage(settings, PipelineOptions(interactive=False), date(2026, 9, 16), logger)
    assert status == "partial"
    assert "python3 main.py -pdf --browser-login always" in details["action_required"]
    assert details["browser_auth"]["login_required"] == {"dbpia": ["ART2", "ART3"]}
    assert details["pdf_unresolved_source_ids"] == ["ART2", "ART3"]

    # Interactive run: the operator logged in again and the retry recovered one paper.
    state["retry"] = SimpleNamespace(current_count=4, unresolved_source_ids=("ART3",))
    status, details = pipeline._pdf_stage(settings, PipelineOptions(interactive=True), date(2026, 9, 16), logger)
    assert status == "ok" and "action_required" not in details
    assert details["pdf_retry_current"] == 4 and details["pdf_retry_unresolved_source_ids"] == ["ART3"]


def test_filter_stage_reapplies_rules_additively(monkeypatch, tmp_path: Path) -> None:
    from Collector import kci_metadata

    captured: dict = {}

    def fake_match(**kwargs):
        captured.update(kwargs)
        return kci_metadata.PipelineResult(
            100, 100, 100, 40, 40, tmp_path / "kci_papers_accumulated.xlsx",
            new_source_ids=("ART9",), stale_source_ids=("ART1",), outside_year_window_source_ids=("ART0",),
        )

    monkeypatch.setattr(kci_metadata, "run_kci_metadata_pipeline", fake_match)
    status, details = pipeline._filter_stage(
        _settings(tmp_path), PipelineOptions(), date(2026, 9, 16), pipeline.ProgressLogger()
    )
    assert status == "ok"
    assert captured["match_corpus"] is True and captured["match_additive"] is True
    assert "match_incremental" not in captured
    assert details["new_source_ids"] == ["ART9"] and details["stale_source_ids"] == ["ART1"]
    assert details["outside_year_window_source_ids"] == ["ART0"]
    assert details["relevant"] == 40


def test_cli_rejects_provider_flags_without_pdf_collection() -> None:
    with pytest.raises(SystemExit) as excinfo:
        pipeline.main(["--skip-pdf", "--enable-dbpia"])
    assert excinfo.value.code == 2


def test_unknown_stage_names_are_rejected(tmp_path: Path) -> None:
    # A non-empty runner table keeps this a test run: no real stage and not the repository's run lock.
    with pytest.raises(ValueError, match="unknown pipeline stages"):
        run_pipeline(
            PipelineOptions(stages=("metadata", "bogus")), settings=_settings(tmp_path),
            run_date=date(2026, 9, 16), runners={"metadata": _runner("metadata")}, ledger_dir=tmp_path,
        )


def test_browser_providers_follow_saved_profiles_and_explicit_flags(monkeypatch) -> None:
    from Collector.browser_auth import ProfileStatus

    present = {"dbpia": True, "kyobo": False, "jams": True}
    monkeypatch.setattr(
        pipeline,
        "inspect_profile",
        lambda provider: ProfileStatus(provider, Path("/p") / provider, present[provider], present[provider], ""),
    )
    # auto: every provider with a saved profile
    assert pipeline.browser_providers_for(PipelineOptions()) == ("dbpia", "jams")
    # explicit enable flags force a provider in, even without a profile
    assert pipeline.browser_providers_for(PipelineOptions(enable_kyobo=True)) == ("dbpia", "kyobo", "jams")
    # an explicit list wins over auto-detection and over the enable flags
    assert pipeline.browser_providers_for(PipelineOptions(browser_providers=("kyobo",))) == ("kyobo",)
    assert pipeline.browser_providers_for(PipelineOptions(browser_providers=())) == ()
    assert pipeline.browser_providers_for(PipelineOptions(browser_providers=(), enable_dbpia=True)) == ()


def test_cli_parses_browser_flags() -> None:
    parser = build_argument_parser()
    options = options_from_arguments(parser.parse_args(["--browser-providers", "dbpia,jams", "--browser-login", "always", "--non-interactive"]))
    assert options.browser_providers == ("dbpia", "jams")
    assert options.browser_login == "always"
    assert options.interactive is False
    assert options_from_arguments(parser.parse_args(["--browser-providers", "none"])).browser_providers == ()
    assert options_from_arguments(parser.parse_args([])).browser_providers is None
    assert options_from_arguments(parser.parse_args(["--interactive"])).interactive is True
    with pytest.raises(ValueError, match="unknown browser providers"):
        options_from_arguments(parser.parse_args(["--browser-providers", "scihub"]))


def test_special_stage_skips_without_a_kci_profile_when_unattended(monkeypatch, tmp_path: Path) -> None:
    from Collector.browser_auth import ProfileStatus

    monkeypatch.setattr(
        pipeline, "inspect_profile",
        lambda provider: ProfileStatus(provider, tmp_path / provider, False, False, "profile directory is missing"),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(pipeline, "_run_subprocess", lambda command, **kwargs: (calls.append(list(command)), (0, "ok"))[1])

    status, details = pipeline._special_stage(
        _settings(tmp_path), PipelineOptions(interactive=False), date(2026, 9, 16), pipeline.ProgressLogger()
    )
    assert status == "skipped" and "action_required" in details and calls == []

    # Interactive first run: the headed KCI login is requested automatically.
    status, details = pipeline._special_stage(
        _settings(tmp_path), PipelineOptions(interactive=True), date(2026, 9, 16), pipeline.ProgressLogger()
    )
    assert status == "ok" and "--interactive-login" in calls[-1] and details["interactive_login"] is True

    # never: no login window, so the stage is skipped with instructions.
    status, _details = pipeline._special_stage(
        _settings(tmp_path), PipelineOptions(interactive=True, browser_login="never"), date(2026, 9, 16), pipeline.ProgressLogger()
    )
    assert status == "skipped"


def test_stage_logs_say_what_runs_how_far_and_what_came_out(tmp_path: Path) -> None:
    import io

    stream = io.StringIO()
    logger = pipeline.ProgressLogger(stream=stream)
    runners = {name: _runner(name) for name in STAGES}
    runners["pdf"] = lambda settings, options, run_date, log: ("partial", {
        "pdf_current": 549, "pdf_selected": 555, "pdf_unresolved_source_ids": ["A"] * 6,
        "located_pdf_downloaded": ["B", "C"], "action_required": "대화형 터미널에서 `python3 main.py -pdf` 실행",
    })
    runners["jev"] = _runner("jev", error=RuntimeError("taxonomy mismatch"))
    run_pipeline(
        PipelineOptions(stages=("pdf", "type", "jev", "md")), settings=_settings(tmp_path), run_date=date(2026, 9, 20),
        runners=runners, logger=logger, ledger_dir=tmp_path / "ledger",
    )
    text = stream.getvalue()
    assert "파이프라인 시작 — 총 4단계: 원문 PDF 수집 → 논문 유형 정리 → Jev AI 분류 반영 → 논문별 MD 생성" in text
    assert "━━ [1/4단계] 원문 PDF 수집 시작 ━━" in text and "하는 일: PDF가 없는 선별 논문을" in text
    assert "[1/4단계] 원문 PDF 수집 일부 완료" in text and "PDF 보유 549/555편 · 아직 없는 논문 6편 · 확인된 제공처에서 추가 확보 2편" in text
    assert "▶ 해야 할 일: 대화형 터미널에서" in text
    assert "전체 진행률 1/4단계 (25%)" in text and "전체 진행률 4/4단계 (100%)" in text
    assert "[3/4단계] Jev AI 분류 반영 실패" in text and "taxonomy mismatch" in text
    assert "[4/4단계] 논문별 MD 생성 건너뜀 — 앞 단계(Jev AI 분류 반영)가 실패" in text
    assert "━━ 파이프라인 종료 — 단계별 결과 ━━" in text and "Jev AI 분류 반영: 실패" in text


def test_plan_and_helpers_read_as_sentences() -> None:
    assert pipeline.format_duration(4) == "4초" and pipeline.format_duration(133) == "2분 13초"
    assert pipeline.format_duration(3 * 3600 + 120) == "3시간 2분"
    lines = pipeline.describe_plan({
        "stages": ["metadata", "pdf", "analyze"], "target_months": ["202605", "202606"], "kci_api_key": True,
        "browser_providers": ["dbpia", "kyobo"], "browser_login": "auto", "interactive": False,
        "analysis_pending": ["ART1", "ART2"], "analysis_up_to_date": 534, "analysis_backend": "claude-code", "claude_credentials": True,
    })
    text = "\n".join(lines)
    assert "실행할 단계: 논문 정보 수집·필터링 → 원문 PDF 수집 → 논문 분석(Claude)" in text
    assert "논문 정보를 받을 달: 202605, 202606" in text and "분석 대기 논문 2편" in text and "dbpia, kyobo" in text
    assert pipeline.stage_summary("md", {"paper_count": 534, "markdown_count": 1068, "pending_source_ids": ["x"] * 15,
                                          "oa_unconfirmed": 1, "special_unconfirmed": 6}).startswith("논문 534편 → MD 1068개 생성")
    assert set(pipeline.STAGE_INFO) == set(STAGES)


def test_subprocess_relay_hides_repeated_noise(tmp_path: Path) -> None:
    import io
    import sys as _sys

    script = tmp_path / "noisy.py"
    script.write_text(
        "import sys\nsys.stdout.reconfigure(encoding='utf-8')\nprint('> tool banner')\nfor i in range(300): print('  ⚠ data/x.json has no title.ko')\n"
        "print('Ignoring wrong pointing object 1 0 (offset 0)')\nprint('Checked 197 nodes')\nprint('✓ All checks passed.')\n",
        encoding="utf-8",
    )
    stream = io.StringIO()
    code, tail = pipeline._run_subprocess([_sys.executable, str(script)], cwd=tmp_path, logger=pipeline.ProgressLogger(stream=stream), label="npm:validate")
    text = stream.getvalue()
    assert code == 0 and "⚠" not in text and "Ignoring wrong" not in text
    assert "Checked 197 nodes" in text and "✓ All checks passed." in text
    assert "반복되는 경고·안내 302줄은 화면에서 생략했습니다" in text
    assert tail.splitlines() == ["Checked 197 nodes", "✓ All checks passed."]


def test_level_stage_reports_an_unfinished_classification(tmp_path: Path, monkeypatch) -> None:
    """Exit code 3 of the classifier means "not every paper is classified": partial, with the reason and what to do."""

    import json as _json

    levels = tmp_path / "Analyzer" / "LV" / "levels.json"
    levels.parent.mkdir(parents=True)
    levels.write_text(_json.dumps({"papers": {
        "ART1": {"status": "classified", "basis": "PDF", "lv3": ["EXAM-04-01"]},
        "ART2": {"status": "pending", "basis": "PDF", "lv3": []},
    }}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "REPOSITORY_ROOT", tmp_path)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        return 3, "case 1 done: {'ok': 1, 'failed': 1}\nLEVEL INCOMPLETE: 사용량 제한으로 중단 — 같은 명령을 다시 실행하면 남은 논문만 이어서 분류합니다."

    monkeypatch.setattr(pipeline, "_run_subprocess", fake_run)
    status, details = pipeline._lv3_stage(_settings(tmp_path), PipelineOptions(), date(2026, 9, 1), pipeline.ProgressLogger())

    assert status == "partial" and details["pending_level_count"] == 1
    assert details["reason"].startswith("사용량 제한으로 중단") and "-derive" in details["action_required"]
    assert commands[0][-2:] == ["--date", "260901"]  # the run date names the workbooks, like the other result workbooks
    assert pipeline.stage_summary("lv3", details).startswith("사용량 제한으로 중단")

    monkeypatch.setattr(pipeline, "_run_subprocess", lambda command, **_kwargs: (1, "Traceback ...\nValueError: boom"))
    status, details = pipeline._lv3_stage(_settings(tmp_path), PipelineOptions(), date(2026, 9, 1), pipeline.ProgressLogger())
    assert status == "failed" and details["reason"] == "ValueError: boom"


def test_level5_stage_reports_partial_and_failed_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "REPOSITORY_ROOT", tmp_path)
    targets = tmp_path / "Analyzer" / "Application" / "targets.json"
    targets.parent.mkdir(parents=True)
    targets.write_text(json.dumps({"papers": {
        "ART000000001": {"status": "ok", "applications": [
            {"status": "mapped", "level5": [{"id": "EXAM-04-01-02-003"}]}, {"status": "unregistered", "level5": []}]},
        "ART000000002": {"status": "pending", "applications": []},
    }}), encoding="utf-8")
    commands: list[list[str]] = []

    summaries = {}
    for code, expected in ((0, "ok"), (3, "partial"), (1, "failed")):
        monkeypatch.setattr(pipeline, "_run_subprocess", lambda command, code=code, **_kwargs: (commands.append(command), (code, "Traceback\nRuntimeError: boom"))[1])
        status, details = pipeline._level5_stage(_settings(tmp_path), PipelineOptions(), date(2026, 10, 4), pipeline.ProgressLogger())
        assert status == expected, code
        assert (details["paper_count"], details["papers_with_applications"], details["mapped_applications"], details["empty_rows"], details["pending"]) == (2, 1, 1, 1, 1)
        summaries[expected] = pipeline.stage_summary("level5", details)
    assert commands[0][2:5] == ["-m", "Analyzer.Application.level5", "--extract"]
    assert "Level-5를 붙인 애플리케이션 1건" in summaries["ok"] and "아직 끝나지 않은 논문 1편" in summaries["ok"]
    assert summaries["failed"] == "RuntimeError: boom"

    # The earlier Level-5 mapping is a comparison now: the profiles do not wait for it.
    log: list[str] = []
    runners = {name: _runner(name, log=log) for name in STAGES}
    runners["level5"] = _runner("level5", log=log, error=RuntimeError("registry"))
    result = run_pipeline(PipelineOptions(stages=("level5", "jev", "results", "md", "graphs")), settings=_settings(tmp_path),
                          run_date=date(2026, 10, 4), runners=runners, ledger_dir=tmp_path / "ledger")
    by_name = {report.name: report for report in result.stages}
    assert by_name["level5"].status == "failed"
    assert by_name["md"].status == "ok" and by_name["graphs"].status == "ok" and by_name["results"].status == "ok"


def test_level5_stage_says_when_only_the_comparison_mappings_are_unfinished(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "REPOSITORY_ROOT", tmp_path)
    targets = tmp_path / "Analyzer" / "Application" / "targets.json"
    targets.parent.mkdir(parents=True)
    by_basis = {"PDF": {"status": "mapped", "ids": ["EXAM-04-01-02-003"]}, "Abstract": {"status": "pending", "ids": []},
                "Summary": {"status": "no_fit", "ids": []}}
    targets.write_text(json.dumps({"papers": {"ART000000001": {"status": "ok", "applications": [
        {"status": "mapped", "level5": [{"id": "EXAM-04-01-02-003"}], "level5_by_basis": by_basis},
        {"status": "no_fit", "level5": []}]}}}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_run_subprocess", lambda command, **_kwargs: (3, "usage limit"))
    status, details = pipeline._level5_stage(_settings(tmp_path), PipelineOptions(), date(2026, 10, 5), pipeline.ProgressLogger())
    assert status == "partial" and details["pending"] == 0 and details["comparison_pending"] == 1
    assert "비교용" in details["reason"] and "-level5" in details["action_required"]
    finished = {key: value for key, value in details.items() if key not in ("reason", "action_required")}
    assert "공란 목록: 미등록 0건, 등록됐지만 맞는 노드 없음 1건" in pipeline.stage_summary("level5", finished)


def test_jev_stage_writes_the_profile_inputs_and_says_what_is_missing(tmp_path: Path, monkeypatch) -> None:
    from Analyzer.JevAI import jev_fields

    pending: list[str] = []
    databases: list[Path] = []

    def fake_export(*, database: Path, **_kwargs):
        databases.append(database)
        return jev_fields.ExportResult(
            levels=tmp_path / "levels.json", targets=tmp_path / "targets.json", paper_count=3, scored=3 - len(pending),
            pending=list(pending), not_selected=[], with_level3=2, with_tags=1, with_applications=1, level5_rows=2,
            blank_rows=1, metadata_only=0, runs=[], database="results_kci-(1026-0132).db", checked=3 - len(pending),
        )

    monkeypatch.setattr(jev_fields, "export", fake_export)
    status, details = pipeline._jev_stage(_settings(tmp_path), PipelineOptions(), date(2026, 10, 26), pipeline.ProgressLogger())
    assert status == "ok" and databases == [tmp_path / "pipeline.db"]       # the selection is read from the pipeline DB
    summary = pipeline.stage_summary("jev", details)
    assert summary.startswith("선별 논문 3편 중 Jev 결과 3편") and "Level-5 행 2건, ID 공란 행 1건" in summary
    assert "results_kci-(1026-0132).db와 3편 일치" in summary

    pending.append("ART000000003")
    status, details = pipeline._jev_stage(_settings(tmp_path), PipelineOptions(), date(2026, 10, 26), pipeline.ProgressLogger())
    assert status == "partial" and "ART000000003" in details["reason"] and "-jev -md -graph" in details["action_required"]
    assert pipeline.stage_summary("jev", details) == details["reason"]

    # A failed jev stage holds the profiles back (they would show a stale classification); the workbooks still run.
    log: list[str] = []
    runners = {name: _runner(name, log=log) for name in STAGES}
    runners["jev"] = _runner("jev", log=log, error=RuntimeError("the Jev decisions differ from the results DB"))
    result = run_pipeline(PipelineOptions(stages=expand_stages(["renumber-all"])), settings=_settings(tmp_path),
                          run_date=date(2026, 10, 26), runners=runners, ledger_dir=tmp_path / "ledger")
    by_name = {report.name: report for report in result.stages}
    assert by_name["jev"].status == "failed" and by_name["md"].details["blocked_by"] == ["jev"]
    assert by_name["results"].status == "ok" and by_name["graphs"].status == "skipped"
    assert "lv3" not in log and "level5" not in log
    # Papers without a type decision yet get their profile (type N/A); the md summary says how many.
    md = pipeline.stage_summary("md", {"paper_count": 545, "markdown_count": 1090, "pending_source_ids": [], "untyped_source_ids": ["x"] * 34,
                                       "oa_unconfirmed": 0, "special_unconfirmed": 0})
    assert md.startswith("논문 545편 → MD 1090개 생성 · 유형 미확인(분석 대기, 유형 칸 N/A) 34편")


def test_numbers_are_checked_before_anything_reads_them(tmp_path: Path) -> None:
    """``renumber`` runs right after the PDFs are known; a failed renumbering blocks every stage that reads numbers."""

    assert DEFAULT_STAGES.index("pdf") < DEFAULT_STAGES.index("renumber") < DEFAULT_STAGES.index("analyze")
    assert expand_stages(["renumber-all"]) == ("renumber", "type", "keywords", "jev", "results", "md", "graphs")

    log: list[str] = []
    runners = {name: _runner(name, log=log) for name in STAGES}
    runners["renumber"] = _runner("renumber", log=log, error=RuntimeError("stale pin"))
    result = run_pipeline(
        PipelineOptions(), settings=_settings(tmp_path), run_date=date(2026, 9, 20), runners=runners, ledger_dir=tmp_path / "ledger",
    )
    by_name = {report.name: report for report in result.stages}
    for blocked in ("analyze", "type", "results", "md", "graphs"):
        assert by_name[blocked].status == "skipped", blocked
    # They do not read paper numbers (the Jev results are keyed by KCI article id).
    assert by_name["oa"].status == "ok" and by_name["keywords"].status == "ok" and by_name["jev"].status == "ok"


def test_renumber_stage_tells_the_user_when_the_results_are_not_rebuilt_in_the_same_run(tmp_path: Path, monkeypatch) -> None:
    from Analyzer import numbering

    moved = {"in_order": False, "paper_count": 10, "changed_count": 3, "new_count": 2, "moved_count": 1, "profiles_cleared": 1,
             "first_changes": [{"source_id": "ART000000009", "from": None, "to": 9}, {"source_id": "ART000000003", "from": 4, "to": 3}]}
    monkeypatch.setattr(numbering, "renumber", lambda **_kwargs: dict(moved))
    import io

    stream = io.StringIO()
    status, details = pipeline._renumber_stage(_settings(tmp_path), PipelineOptions(stages=("pdf", "renumber")), date(2026, 10, 1), pipeline.ProgressLogger(stream=stream))
    assert status == "ok" and "-renumber" in details["action_required"]
    assert "ART000000009: 새 논문 → 9번" in stream.getvalue() and "ART000000003: 4번 → 3번" in stream.getvalue()
    assert "새로 번호를 받은 논문 2편 · 번호가 바뀐 논문 1편" in pipeline.stage_summary("renumber", details)

    for stages in (DEFAULT_STAGES, expand_stages(["renumber-all"])):  # everything that shows numbers is rebuilt in the same run
        _status, details = pipeline._renumber_stage(_settings(tmp_path), PipelineOptions(stages=stages), date(2026, 10, 1), pipeline.ProgressLogger(stream=io.StringIO()))
        assert "action_required" not in details
    monkeypatch.setattr(numbering, "renumber", lambda **_kwargs: {"in_order": True, "paper_count": 10, "with_pdf": 9, "without_pdf": 1})
    _status, details = pipeline._renumber_stage(_settings(tmp_path), PipelineOptions(stages=("renumber",)), date(2026, 10, 1), pipeline.ProgressLogger(stream=io.StringIO()))
    assert "action_required" not in details and "이미 정렬" in pipeline.stage_summary("renumber", details)


def test_a_pending_renumbering_is_finished_before_stages_that_read_numbers(tmp_path: Path) -> None:
    journal = tmp_path / ".renumber-journal.json"
    options = PipelineOptions(stages=("md", "graphs"))
    assert pipeline._with_pending_renumber(options, journal).stages == ("md", "graphs")     # nothing pending
    journal.write_text("{}", encoding="utf-8")
    assert pipeline._with_pending_renumber(options, journal).stages == ("renumber", "md", "graphs")
    assert pipeline._with_pending_renumber(PipelineOptions(stages=("oa", "ref")), journal).stages == ("oa", "ref")  # they do not read numbers
    assert pipeline._with_pending_renumber(PipelineOptions(stages=("md",), dry_run=True), journal).stages == ("md",)


def test_graphs_summary_reports_sync_counts_and_what_is_left_to_commit() -> None:
    details = {"build_author_graph.py": {}, "build_institution_graph.py": {}, "build_reference_graph.py": {},
               "web_sync": {"added": 2, "updated": 118, "removed": 1}, "web_build": "ok", "web_repository_changes": 0}
    assert pipeline.stage_summary("graphs", details) == "그래프 3종 생성 · DF-Insight 프로필 추가 2·갱신 118·삭제 1편 · 웹 빌드 완료 · 웹 저장소에 커밋할 문서 0개"
    assert pipeline.stage_summary("graphs", {"build_author_graph.py": {}, "web_sync": "checkout missing", "web_build": "skipped"}) == (
        "그래프 1종 생성 · DF-Insight 동기화 안 됨 · 웹 빌드 생략")
    # Papers whose web table was replaced by the profile's are worth a word, tables already equal are not.
    details["web_sync"] = {"added": 0, "updated": 59, "removed": 0, "application_links_kept": 167, "application_links_rebuilt": 2}
    details["web_repository_changes"] = 4
    assert pipeline.stage_summary("graphs", details) == (
        "그래프 3종 생성 · DF-Insight 프로필 추가 0·갱신 59·삭제 0편 · 대상 애플리케이션 표를 바꾼 논문 2편 · 웹 빌드 완료 · 웹 저장소에 커밋할 문서 4개")
    # Rows renumbered in DF-Insight's own documents, and uncommitted files the stage did not make, are told apart.
    details["web_sync"]["renumbered_rows"] = 32
    details["web_repository_changes"], details["web_repository_other_change_count"] = 1126, 11
    assert pipeline.stage_summary("graphs", details) == (
        "그래프 3종 생성 · DF-Insight 프로필 추가 0·갱신 59·삭제 0편 · 대상 애플리케이션 표를 바꾼 논문 2편 · 옛 번호를 새 번호로 고친 DF-Insight 문서 행 32개 · "
        "웹 빌드 완료 · 웹 저장소에 커밋할 문서 1115개(그 밖에 이 단계와 무관한 미커밋 파일 11개)")
    # A builder that failed is recorded with its return code; it produced no graph.
    assert pipeline.stage_summary("graphs", {"build_author_graph.py": {"returncode": 0}, "build_institution_graph.py": {"returncode": 1}}) == (
        "그래프 1종 생성 · DF-Insight 동기화 안 됨 · 웹 빌드 안 됨")


KCI_URL = "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId=ART000000001"
LINKED_ROW, PLAIN_ROW = "| EXAM-04-01-01-01 | Chrome |", "|  | Google Chrome |"


def _profile_document(rows: str, date_: str, references: str = "") -> str:
    return (f'last_updated: "{date_}"\n\n## 1. 기본 정보\n| 항목 | 내용 |\n|---|---|\n| URL | {KCI_URL} |\n\n'
            f"## 4. 대상 애플리케이션\n| ID | 이름 |\n|---|---|\n{rows}\n\n## 5. 참고문헌\n| ID | REF |\n|---|---|\n{references}")


class _GraphsSite:
    """A one-paper profile tree and DF-Insight checkout wired into ``_graphs_stage``: no real repository, record or npm."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from Analyzer import web_sync

        self.web_sync = web_sync
        self.tmp_path = tmp_path
        self.profiles, self.site, self.state = tmp_path / "MD", tmp_path / "site", tmp_path / "Manifest" / "web_sync_state.json"
        self.references = self.site / "data" / "references_md"
        (self.site / "scripts").mkdir(parents=True)
        (self.site / "scripts" / "build-graphs.mjs").write_text("// upstream build", encoding="utf-8")
        self.write(self.profiles, PLAIN_ROW, "2026-10-03")
        self.write(self.references, LINKED_ROW, "2026-09-28")    # an earlier build linked the plain name
        self.committed = self.web_copy("ko.md")
        self.messages: list[str] = []
        self.commands: list[list[str]] = []
        self.pending: list[int | None] = []          # what git reports after the build
        self.paths: list[str] | None = None          # ... and which paths those are
        self.link = lambda: 0                         # what `npm run link:write -- --only=B,D` does and returns
        self.build = lambda: 0                        # what `npm run build:only` does and returns
        self.builder_code = 0
        real_sync = web_sync.sync_profiles
        monkeypatch.setattr(pipeline, "WEB_DIR", self.site)
        monkeypatch.setattr(pipeline, "_run_subprocess", self._run)
        monkeypatch.setattr(pipeline, "_web_repository_changes", lambda: self.pending.pop(0) if self.pending else 0)
        monkeypatch.setattr(pipeline, "_web_repository_paths", lambda: self.paths)
        monkeypatch.setattr(pipeline.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(web_sync, "DEFAULT_STATE", self.state)
        monkeypatch.setattr(web_sync, "sync_profiles", lambda **options: real_sync(profiles=self.profiles, **options))

    def write(self, root: Path, rows: str, date_: str, names: tuple[str, ...] = ("ko.md", "en.md"), references: str = "") -> None:
        (root / "KNOW-DOMP-0000001").mkdir(parents=True, exist_ok=True)
        for name in names:
            (root / "KNOW-DOMP-0000001" / name).write_text(_profile_document(rows, date_, references), encoding="utf-8")

    def web_copy(self, name: str) -> str:
        return (self.references / "KNOW-DOMP-0000001" / name).read_text(encoding="utf-8")

    def emit(self, component: str, message: str, **_: object) -> None:
        self.messages.append(message)

    def _run(self, command, **_: object) -> tuple[int, str]:
        self.commands.append([str(part) for part in command])
        if list(command[1:3]) == ["run", "link:write"]:
            return self.link(), "link output"
        if list(command[1:]) == ["run", "build:only"]:
            return self.build(), "build output"
        assert list(command[1:]) != ["run", "build"], "the full build would link blank rows by name (pass A)"
        return self.builder_code, "builder output"

    def run(self, **options: object) -> tuple[str, dict]:
        self.messages.clear()
        self.commands.clear()
        return pipeline._graphs_stage(_settings(self.tmp_path), PipelineOptions(stages=("graphs",), **options), date(2026, 10, 3), self)


@pytest.fixture()
def graphs_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _GraphsSite:
    return _GraphsSite(tmp_path, monkeypatch)


def test_graphs_stage_gives_the_web_copy_the_profile_table_and_never_links_by_name(graphs_site: _GraphsSite) -> None:
    status, details = graphs_site.run()

    assert status == "ok" and details["web_build"] == "ok" and details["web_repository_changes"] == 0 and "action_required" not in details
    # The blank id is the Jev AI classification's decision: the name link of an earlier build is replaced, not adopted.
    assert details["web_sync"]["state_found"] is False and details["web_sync"]["application_links_adopted"] == 0
    assert details["web_sync"]["application_links_rebuilt_reasons"] == {"KNOW-DOMP-0000001": graphs_site.web_sync.WEB_ROWS_DIFFER}
    assert PLAIN_ROW in graphs_site.web_copy("ko.md") and PLAIN_ROW in graphs_site.web_copy("en.md") and LINKED_ROW not in graphs_site.web_copy("ko.md")
    assert json.loads(graphs_site.state.read_text(encoding="utf-8"))["papers"] == {"ART000000001": {"applications": ["Google Chrome"]}}
    assert any("기록을 새로 만들었습니다" in message for message in graphs_site.messages) and "web_sync_warning" not in details
    assert any("표 교체: KNOW-DOMP-0000001" in message for message in graphs_site.messages)
    # Three builders, then DF-Insight's build without its name-linking pass: link:write with B and D only, then build:only.
    assert [Path(command[-1]).name for command in graphs_site.commands[:3]] == [
        "build_author_graph.py", "build_institution_graph.py", "build_reference_graph.py"]
    assert [command[1:] for command in graphs_site.commands[3:]] == [["run", "link:write", "--", "--only=B,D"], ["run", "build:only"]]
    assert pipeline.WEB_LINK_PASSES == "B,D"

    status, details = graphs_site.run(run_web_build=False)
    assert status == "ok" and details["web_build"] == "skipped" and "action_required" not in details      # nothing was rewritten
    assert details["web_sync"]["state_found"] is True and details["web_sync"]["application_links_kept"] == 1 and details["web_sync"]["updated"] == 0
    assert any("웹 사본이 이미 같은 논문 1편 · 웹 사본의 표를 바꾼 논문 0편" in message for message in graphs_site.messages)


def test_graphs_stage_restores_what_the_build_reproduces_and_says_what_is_left_to_commit(graphs_site: _GraphsSite) -> None:
    graphs_site.run()
    cited = "Kim, A. 2020. A cited domestic paper of this corpus. J. 1(1)."
    graphs_site.write(graphs_site.profiles, PLAIN_ROW, "2026-10-03", references=f"|  | {cited} |\n")
    graphs_site.write(graphs_site.references, PLAIN_ROW, "2026-09-28", references=f"| KNOW-DOMP-0000002 | {cited} |\n")   # pass B filled our number
    committed = graphs_site.web_copy("ko.md")

    def build() -> int:                                                           # pass B resolves the citation again
        for name in ("ko.md", "en.md"):
            assert f"|  | {cited} |" in graphs_site.web_copy(name)                 # a KNOW-DOMP number is always resolved again
            graphs_site.write(graphs_site.references, PLAIN_ROW, "2026-10-03", names=(name,), references=f"| KNOW-DOMP-0000002 | {cited} |\n")
        return 0

    graphs_site.link = build
    graphs_site.pending = [3]
    graphs_site.paths = ["data/references_md/KNOW-DOMP-0000001/ko.md", "README.md", "js/main.js"]
    status, details = graphs_site.run()

    assert status == "ok" and details["web_sync"]["restored_after_build"] == 2 and details["web_sync"]["application_links_rebuilt"] == 0
    assert graphs_site.web_copy("ko.md") == committed == graphs_site.web_copy("en.md")          # old bytes, old date
    assert details["web_repository_changes"] == 3 and "문서 3개가 마지막 커밋과 다릅니다" in details["action_required"]
    # Only what the stage writes goes into the data commit; other uncommitted work there is named, never swept in.
    assert "git -C web/DF-Insight add data/references_md data/targets_md" in details["action_required"]
    assert "git add -A 는 쓰지 마세요" in details["action_required"] and "git add -A 후" not in details["action_required"]
    assert "그 밖에 커밋되지 않은 파일 2개(README.md, js/main.js)" in details["action_required"]
    assert details["web_repository_other_changes"] == ["README.md", "js/main.js"]
    assert pipeline.stage_summary("graphs", details).endswith("웹 저장소에 커밋할 문서 1개(그 밖에 이 단계와 무관한 미커밋 파일 2개)")


def test_graphs_stage_says_what_to_do_when_the_web_build_does_not_finish(graphs_site: _GraphsSite, monkeypatch: pytest.MonkeyPatch) -> None:
    graphs_site.run()
    graphs_site.write(graphs_site.profiles, "|  | Telegram |", "2026-10-03")
    status, details = graphs_site.run(run_web_build=False)
    assert status == "ok" and details["web_build"] == "skipped" and "커밋하지 말고" in details["action_required"]

    graphs_site.build = lambda: 1
    status, details = graphs_site.run()
    assert status == "failed" and details["npm:build"]["returncode"] == 1 and "웹 빌드가 실패했습니다" in details["action_required"]
    assert "web_repository_changes" not in details

    graphs_site.build, graphs_site.link = (lambda: 0), (lambda: 1)
    status, details = graphs_site.run()
    assert status == "failed" and details["npm:link"]["returncode"] == 1 and "npm:build" not in details   # build:only never runs after it
    assert graphs_site.commands[-1][1:3] == ["run", "link:write"]

    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    status, details = graphs_site.run()
    assert status == "partial" and details["web_build"] == "npm not found" and "python3 main.py -graph" in details["action_required"]


def test_graphs_stage_stops_before_the_sync_when_it_cannot_mirror(graphs_site: _GraphsSite) -> None:
    graphs_site.builder_code = 1                                                  # a graph builder fails: nothing is handed over
    status, details = graphs_site.run()
    assert status == "failed" and "web_sync" not in details and not graphs_site.state.exists()
    assert pipeline.stage_summary("graphs", details).startswith("그래프 0종 생성")

    graphs_site.builder_code = 0
    (graphs_site.profiles / "KNOW-DOMP-0000001" / "en.md").unlink()               # a half-written profile
    status, details = graphs_site.run()
    assert status == "partial" and "넘기지 못했습니다" in details["action_required"] and "KNOW-DOMP-0000001" in details["web_sync"]
    assert graphs_site.web_copy("ko.md") == graphs_site.committed and not graphs_site.state.exists()


def test_graphs_stage_reports_web_tables_it_cannot_read(graphs_site: _GraphsSite) -> None:
    broken = graphs_site.web_copy("en.md").replace("## 4. 대상 애플리케이션", "## 4. 대상")
    (graphs_site.references / "KNOW-DOMP-0000001" / "en.md").write_text(broken, encoding="utf-8")
    graphs_site.pending = [2]
    status, details = graphs_site.run()
    assert details["web_sync_unreadable"] == ["KNOW-DOMP-0000001"] and "web_sync_warning" not in details
    assert "KNOW-DOMP-0000001" in details["action_required"] and "손으로 고친 내용" in details["action_required"]
    assert PLAIN_ROW in graphs_site.web_copy("ko.md") and PLAIN_ROW in graphs_site.web_copy("en.md")


def test_graphs_stage_renumbers_df_insights_own_rows_and_reports_the_ones_it_cannot_place(graphs_site: _GraphsSite) -> None:
    title = "A sufficiently long title of the only paper"
    for root in (graphs_site.profiles, graphs_site.references):
        graphs_site.write(root, PLAIN_ROW, "2026-10-03")
        for name in ("ko.md", "en.md"):
            path = root / "KNOW-DOMP-0000001" / name
            path.write_text(f'subject: "{title}"\n' + path.read_text(encoding="utf-8"), encoding="utf-8")
    # The paper moves from number 1 to number 2 (an older paper took number 1, which has no profile yet here).
    (graphs_site.profiles / "KNOW-DOMP-0000001").rename(graphs_site.profiles / "KNOW-DOMP-0000002")
    target = graphs_site.site / "data" / "targets_md" / "TAGT-0000001"
    target.mkdir(parents=True)
    (target / "ko.md").write_text(f"## 관련 참고문헌\n| ID | 제목 |\n|---|---|\n| KNOW-DOMP-0000001 | {title} |\n| KNOW-DOMP-0000001 | 다른 문헌 |\n", encoding="utf-8")

    status, details = graphs_site.run()
    assert status == "ok" and details["web_sync"]["renumbered_rows"] == 1 and details["web_sync"]["renumbered_documents_first"] == ["targets_md/TAGT-0000001/ko.md"]
    assert (target / "ko.md").read_text(encoding="utf-8").split("\n")[3:5] == [f"| KNOW-DOMP-0000002 | {title} |", "| KNOW-DOMP-0000001 | 다른 문헌 |"]
    assert details["web_sync_unverified_rows"] == ["targets_md/TAGT-0000001/ko.md: KNOW-DOMP-0000001"] and "그대로 둔 행 1개" in details["web_sync_warning"]
    assert any("행 1개를 새 번호로 고쳤습니다(문서 1개)" in message for message in graphs_site.messages)
    assert "옛 번호를 새 번호로 고친 DF-Insight 문서 행 1개" in pipeline.stage_summary("graphs", details)


def test_graphs_stage_reports_application_ids_df_insight_no_longer_has(graphs_site: _GraphsSite) -> None:
    graphs_site.write(graphs_site.profiles, LINKED_ROW, "2026-10-03")            # the profile names a Level-5 node ...
    (graphs_site.site / "data" / "techniques").mkdir()
    (graphs_site.site / "data" / "techniques" / "EXAM-04-01-01-09.json").write_text("{}", encoding="utf-8")   # ... removed upstream since
    status, details = graphs_site.run()
    assert status == "ok" and details["web_sync_unknown_application_ids"] == {"KNOW-DOMP-0000001": ["EXAM-04-01-01-01"]}
    assert "DF-Insight에 없는 노드 ID" in details["action_required"] and "KNOW-DOMP-0000001(EXAM-04-01-01-01)" in details["action_required"]
    assert "python3 main.py -jev -md -graph" in details["action_required"]

    (graphs_site.site / "data" / "techniques" / "EXAM-04-01-01-01.json").write_text("{}", encoding="utf-8")
    status, details = graphs_site.run()
    assert "web_sync_unknown_application_ids" not in details and "action_required" not in details


def test_web_repository_paths_reads_porcelain_z_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(pipeline, "WEB_DIR", tmp_path)
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list[list[str]] = []
    output = b" M data/references_md/KNOW-DOMP-0000001/ko.md\0R  js/new name.js\0js/old.js\0?? scripts/lib/people.mjs\0"

    def run(command, **_):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    assert pipeline._web_repository_paths() == ["data/references_md/KNOW-DOMP-0000001/ko.md", "js/new name.js", "scripts/lib/people.mjs"]
    assert pipeline._web_repository_changes() == 3
    assert "--no-optional-locks" in seen[0] and "-z" in seen[0]          # read only: git never refreshes the web repository's index
    (tmp_path / ".git").rmdir()
    assert pipeline._web_repository_paths() is None and pipeline._web_repository_changes() is None
