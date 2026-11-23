"""Monthly KCI incremental collection job.

Schedule :func:`run_kci_job` for the first day of each month.  It rechecks the
two most recent completed publication months (capturing late KCI
registrations), appends new corpus rows idempotently, classifies only corpus
rows beyond the stored match checkpoint, and retries only missing PDF artifacts.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from Collector.browser_auth import (
    IN_SESSION_LOGIN_PROVIDERS,
    LOGIN_MODES,
    AuthenticationDiagnosis,
    BrowserLoginError,
    PreflightResult,
    authentication_failures,
    preflight_browser_providers,
    run_manual_login,
    is_interactive_session,
)
from Collector.kci_metadata import PipelineResult, run_kci_metadata_pipeline
from Collector.located_pdf import located_source_ids, run as run_located_pdf_download
from Collector.kci_pdf import (
    KciPdfCollectionResult,
    PaperPipelineProviderRunner,
    PdfProvider,
    ProviderRunner,
    default_manifest_dir,
    default_papers_dir,
    run_kci_pdf_collection,
)
from Storage.db import DatabaseManager, sqlite_database_path
from Util.debugger import PdfCollectionDebugger
from Util.exporter import export_pdf_statistics
from Util.logger import ProgressLogger, log
from config import Settings, load_settings


RECENT_COMPLETED_MONTHS = 2


@dataclass(frozen=True)
class JobResult:
    target_months: tuple[str, ...]
    corpus: PipelineResult
    incremental_match: PipelineResult
    pdf: KciPdfCollectionResult | None = None
    pdf_report_path: Path | None = None
    # Authenticated browser providers: what the preflight decided, which
    # providers hit a login challenge, and the result of the post-login retry.
    browser_preflight: PreflightResult | None = None
    browser_auth: AuthenticationDiagnosis | None = None
    pdf_retry: KciPdfCollectionResult | None = None
    # Papers the automatic providers could not find, fetched from the official
    # pages recorded in ``Processor/refs/located_pdf_sources.json``.
    located_pdf: dict[str, Any] | None = None

    @property
    def raw_count(self) -> int:
        return self.corpus.raw_count

    @property
    def normalized_count(self) -> int:
        return self.corpus.normalized_count

    @property
    def deduplicated_count(self) -> int:
        return self.corpus.deduplicated_count

    @property
    def relevant_count(self) -> int:
        return self.incremental_match.relevant_count

    @property
    def stored_count(self) -> int:
        return self.incremental_match.stored_count

    @property
    def export_path(self) -> Path | None:
        return self.incremental_match.export_path


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    absolute = year * 12 + (month - 1) + offset
    shifted_year, shifted_month_zero_based = divmod(absolute, 12)
    return shifted_year, shifted_month_zero_based + 1


def recent_completed_months(
    run_date: date,
    *,
    count: int = RECENT_COMPLETED_MONTHS,
) -> tuple[str, ...]:
    """Return the latest completed ``YYYYMM`` publication months, oldest first."""

    if count < 1:
        raise ValueError("count must be at least 1")
    months = [
        _shift_month(run_date.year, run_date.month, -offset)
        for offset in range(count, 0, -1)
    ]
    return tuple(f"{year:04d}{month:02d}" for year, month in months)


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}{month:02d}"


def _parse_month_key(value: str) -> tuple[int, int]:
    if len(value) != 6 or not value.isdigit():
        raise ValueError(f"invalid YYYYMM month key: {value!r}")
    year, month = int(value[:4]), int(value[4:])
    if not 1 <= month <= 12:
        raise ValueError(f"invalid YYYYMM month key: {value!r}")
    return year, month


def database_target_months(
    done_months: Iterable[str],
    run_date: date,
    *,
    recheck_count: int = RECENT_COMPLETED_MONTHS,
    earliest_month: str | None = None,
) -> tuple[str, ...]:
    """Choose the months an increment must fetch from what the corpus holds.

    ``done_months`` are the ``YYYYMM`` keys the corpus builder has marked
    complete.  The result runs from the month after the last completed one up
    to the last *completed* calendar month (the running month is never fetched
    because KCI is still registering it), and it also re-fetches the
    ``recheck_count`` most recent completed months so late registrations are
    picked up.  Unlike a run-date window this never skips a month when the job
    was not run for a while, and it is a no-op only when nothing is pending.
    """

    if recheck_count < 0:
        raise ValueError("recheck_count must be non-negative")
    completed = sorted({month for month in done_months if month})
    for month in completed:
        _parse_month_key(month)
    last_completed_year, last_completed_month = _shift_month(
        run_date.year, run_date.month, -1
    )
    upper = _month_key(last_completed_year, last_completed_month)
    if completed:
        year, month = _parse_month_key(completed[-1])
        recheck = completed[-recheck_count:] if recheck_count else []
        start_year, start_month = _shift_month(year, month, 1)
    else:
        if earliest_month is None:
            raise ValueError("earliest_month is required when no month is complete")
        recheck = []
        start_year, start_month = _parse_month_key(earliest_month)
    pending: list[str] = []
    year, month = start_year, start_month
    while _month_key(year, month) <= upper:
        pending.append(_month_key(year, month))
        year, month = _shift_month(year, month, 1)
    return tuple(sorted({*recheck, *pending}))


def run_kci_job(
    *,
    settings: Settings | None = None,
    run_date: date | None = None,
    require_first_day: bool = True,
    recent_month_count: int = RECENT_COMPLETED_MONTHS,
    target_months: Sequence[str] | None = None,
    skip_export: bool = False,
    collect_pdfs: bool = True,
    pdf_runners: Mapping[PdfProvider | str, ProviderRunner] | None = None,
    papers_dir: Path | None = None,
    manifest_dir: Path | None = None,
    compatibility_papers_dirs: tuple[Path, ...] = (),
    enable_dbpia: bool = False,
    enable_kyobo: bool = False,
    enable_jams: bool = False,
    enable_sciencedirect: bool = False,
    dry_run_pdf: bool = False,
    pdf_provider_options: Mapping[str, Mapping[str, Any]] | None = None,
    browser_login: str = "never",
    interactive: bool | None = None,
    collect_metadata: bool = True,
) -> JobResult:
    """Collect recent metadata, incrementally match it, then fill missing PDFs.

    ``require_first_day`` protects the scheduled contract.  Operators doing a
    deliberate catch-up run can set it to ``False`` while retaining the same
    completed-month calculation.  ``target_months`` overrides the run-date
    window with an explicit ``YYYYMM`` list; the end-to-end pipeline derives it
    from the corpus checkpoints through :func:`database_target_months`.

    ``browser_login`` governs the authenticated browser providers (DBpia,
    Kyobo, JAMS) that were enabled: ``never`` uses only saved profiles,
    ``auto`` opens the manual login window when a profile is missing or the
    saved session turns out to be expired, ``always`` opens it before crawling.
    Logins need an interactive terminal with a display; ``interactive`` forces
    that detection.  After a login the affected providers are retried once.
    ``collect_metadata=False`` skips the KCI corpus increment and match and
    only runs the PDF stages (with the same browser preflight and retry).
    """

    effective_date = run_date or date.today()
    if require_first_day and effective_date.day != 1:
        raise ValueError("the monthly KCI job must run on the first day of the month")
    if not collect_metadata:
        # PDF-only run: no publication month is fetched, so none is reported.
        target_months = ()
    elif target_months is None:
        target_months = recent_completed_months(
            effective_date,
            count=recent_month_count,
        )
    else:
        target_months = tuple(target_months)
        for month in target_months:
            _parse_month_key(month)
        if not target_months:
            raise ValueError("target_months must not be empty")
    month_ranges = [(month, month) for month in target_months]

    if settings is None:
        settings = load_settings(validate=True)
    database = DatabaseManager(settings.database_url)
    database.create_tables()

    try:
        if collect_metadata:
            log(
                "S1",
                f"[1/2] KCI 논문 정보 수집 시작 — 받을 달: {', '.join(target_months)} (기준일 {effective_date.isoformat()})",
            )
            corpus_result = run_kci_metadata_pipeline(
                settings=settings,
                db=database,
                build_corpus=True,
                sweep_months_override=month_ranges,
                skip_export=True,
            )
            log("S2", "[2/2] 새로 받은 논문 정보에 블랙리스트·키워드 필터 적용 시작")
            match_result = run_kci_metadata_pipeline(
                settings=settings,
                db=database,
                match_corpus=True,
                match_incremental=True,
                skip_export=skip_export,
            )
        else:
            log("S1", "논문 정보 수집은 건너뜁니다 (원문 PDF 수집만 실행)")
            corpus_result = PipelineResult(0, 0, 0, 0, 0, None)
            match_result = PipelineResult(0, 0, 0, 0, 0, None)
    finally:
        close = getattr(database, "close", None)
        if callable(close):
            close()

    pdf_result: KciPdfCollectionResult | None = None
    pdf_report_path: Path | None = None
    preflight: PreflightResult | None = None
    diagnosis: AuthenticationDiagnosis | None = None
    retry_result: KciPdfCollectionResult | None = None
    located_report: dict[str, Any] | None = None
    if browser_login not in LOGIN_MODES:
        raise ValueError(f"browser_login must be one of {LOGIN_MODES}")
    if collect_pdfs:
        effective_papers_dir = papers_dir or default_papers_dir()
        effective_manifest_dir = manifest_dir or default_manifest_dir()
        readable_compatibility_dirs = tuple(
            path for path in compatibility_papers_dirs if path.is_dir()
        )
        browser_flags = {"dbpia": enable_dbpia, "kyobo": enable_kyobo, "jams": enable_jams}
        requested_browser = [name for name, flag in browser_flags.items() if flag]
        if requested_browser and not dry_run_pdf:
            log(
                "S3",
                f"로그인 브라우저 점검 — 대상 사이트: {', '.join(requested_browser)} (로그인 창 정책: {browser_login})",
            )
            preflight = preflight_browser_providers(
                requested_browser,
                login=browser_login,
                interactive=interactive,
                notify=lambda message: log("S3", message),
            )
            enable_dbpia = "dbpia" in preflight.enabled
            enable_kyobo = "kyobo" in preflight.enabled
            enable_jams = "jams" in preflight.enabled
            if preflight.in_session_login:
                merged: dict[str, dict[str, Any]] = {
                    name: dict(value) for name, value in (pdf_provider_options or {}).items()
                }
                for name, value in preflight.provider_options().items():
                    merged.setdefault(name, {}).update(value)
                pdf_provider_options = merged
        # The prosecutors' office publishes its journals on a public board that KCI
        # only previews; the board resolver verifies every PDF against the paper.
        with_spo: dict[str, dict[str, Any]] = {name: dict(value) for name, value in (pdf_provider_options or {}).items()}
        with_spo.setdefault("kci", {}).setdefault("enable_spo_download", True)
        pdf_provider_options = with_spo
        pdf_result = run_kci_pdf_collection(
            runners=pdf_runners,
            database_url=settings.database_url,
            papers_dir=effective_papers_dir,
            manifest_dir=effective_manifest_dir,
            compatibility_papers_dirs=readable_compatibility_dirs,
            enable_dbpia=enable_dbpia,
            enable_kyobo=enable_kyobo,
            enable_jams=enable_jams,
            enable_sciencedirect=enable_sciencedirect,
            dry_run=dry_run_pdf,
            provider_options=pdf_provider_options,
            debugger=PdfCollectionDebugger(
                effective_manifest_dir / ".pdf-collection-stages.jsonl"
            ),
            logger=ProgressLogger(),
        )
        if preflight is not None and preflight.enabled:
            diagnosis = authentication_failures(
                effective_manifest_dir,
                pdf_result.unresolved_source_ids,
                providers=preflight.enabled,
            )
            relogin = list(diagnosis.providers_needing_login)
            if relogin:
                log(
                    "S3",
                    "로그인 세션이 만료된 사이트가 있습니다 — "
                    + ", ".join(
                        f"{provider}={len(diagnosis.login_required[provider])}편"
                        for provider in relogin
                    ),
                )
            if relogin and browser_login != "never" and preflight.interactive:
                retried: list[str] = []
                retry_options: dict[str, dict[str, Any]] = {
                    name: dict(value) for name, value in (pdf_provider_options or {}).items()
                }
                for provider in relogin:
                    if provider in IN_SESSION_LOGIN_PROVIDERS:
                        # Log in inside the retry session; the window opens there.
                        retry_options.setdefault(provider, {})["interactive_login"] = True
                        retried.append(provider)
                        continue
                    log("S3", f"{provider}: 브라우저 창에서 다시 로그인한 뒤 Enter를 누르세요")
                    try:
                        run_manual_login(provider)
                    except BrowserLoginError as exc:
                        log("S3", f"{provider}: {exc}")
                        continue
                    retried.append(provider)
                if retried:
                    # After the first challenge the provider's circuit breaker
                    # fails every remaining paper, so retry all unresolved ones.
                    retry_ids = tuple(pdf_result.unresolved_source_ids)
                    log(
                        "S3",
                        f"다시 로그인한 사이트({', '.join(retried)})에서 아직 못 받은 논문 {len(retry_ids)}편을 재시도합니다",
                    )
                    retry_result = run_kci_pdf_collection(
                        runners={
                            PdfProvider(provider): PaperPipelineProviderRunner(
                                PdfProvider(provider)
                            )
                            for provider in retried
                        },
                        database_url=settings.database_url,
                        papers_dir=effective_papers_dir,
                        manifest_dir=effective_manifest_dir,
                        compatibility_papers_dirs=readable_compatibility_dirs,
                        enable_dbpia="dbpia" in retried,
                        enable_kyobo="kyobo" in retried,
                        enable_jams="jams" in retried,
                        provider_options=retry_options,
                        source_ids=retry_ids,
                        debugger=PdfCollectionDebugger(
                            effective_manifest_dir / ".pdf-collection-stages.jsonl"
                        ),
                        logger=ProgressLogger(),
                    )
            elif relogin:
                log(
                    "S3",
                    "Browser re-login required (non-interactive run) | run interactively: "
                    "python3 main.py -pdf --browser-login always",
                )
        if not dry_run_pdf:
            unresolved = set((retry_result or pdf_result).unresolved_source_ids)
            located = unresolved & located_source_ids()
            if located:
                can_show_window = browser_login != "never" and (
                    interactive if interactive is not None else is_interactive_session()
                )
                log("S4", f"확인된 제공처에서 받기 시작 — 표준 제공처가 못 찾은 논문 {len(located)}편 "
                          f"(브라우저 창 {'사용 가능' if can_show_window else '사용 안 함'})")
                located_report = run_located_pdf_download(
                    only=located, allow_window=can_show_window,
                    papers_dir=effective_papers_dir, manifest_dir=effective_manifest_dir,
                    database_url=settings.database_url,
                )
                log(
                    "S4",
                    f"확인된 제공처에서 받기 완료 — 새로 확보 {len(located_report['downloaded'])}편 · "
                    f"실패 {len(located_report['failed'])}편",
                )
        if not skip_export:
            report = export_pdf_statistics(
                sqlite_database_path(settings.database_url),
                effective_papers_dir,
                manifest_dir=effective_manifest_dir,
                output_dir=settings.export_dir,
                compatibility_papers_dirs=readable_compatibility_dirs,
            )
            pdf_report_path = Path(report["output"])
    log(
        "S9",
        f"수집 작업 종료 — 새로 저장한 논문 정보 {corpus_result.stored_count:,}건 · 이번에 선별된 논문 {match_result.relevant_count:,}편 · "
        f"PDF 보유 {pdf_result.current_count if pdf_result else '(PDF 단계 실행 안 함)'}편",
    )
    return JobResult(
        target_months=target_months,
        corpus=corpus_result,
        incremental_match=match_result,
        pdf=pdf_result,
        pdf_report_path=pdf_report_path,
        browser_preflight=preflight,
        browser_auth=diagnosis,
        pdf_retry=retry_result,
        located_pdf=located_report,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the first-day KCI corpus increment and incremental match job."
        )
    )
    parser.add_argument(
        "--allow-non-first-day",
        action="store_true",
        help="Allow an explicit operator catch-up run outside the first day.",
    )
    parser.add_argument(
        "--skip-export",
        action="store_true",
        help="Skip regeneration of the accumulated metadata report.",
    )
    pdf_group = parser.add_mutually_exclusive_group()
    pdf_group.add_argument(
        "--skip-pdf",
        action="store_true",
        help="Skip PDF collection for this scheduled run.",
    )
    pdf_group.add_argument(
        "--dry-run-pdf",
        action="store_true",
        help="Audit PDF targets without making provider requests.",
    )
    parser.add_argument(
        "--enable-dbpia",
        action="store_true",
        help="Enable the permission-gated authenticated DBpia stage.",
    )
    parser.add_argument(
        "--enable-kyobo",
        action="store_true",
        help="Enable the permission-gated authenticated Kyobo Scholar stage.",
    )
    parser.add_argument(
        "--enable-jams",
        action="store_true",
        help="Enable the permission-gated KDFS JAMS stage.",
    )
    parser.add_argument(
        "--enable-sciencedirect",
        action="store_true",
        help="Enable the Elsevier Article Retrieval API stage.",
    )
    parser.add_argument(
        "--browser-login",
        choices=LOGIN_MODES,
        default="never",
        help=(
            "Manual login policy for --enable-dbpia/--enable-kyobo/--enable-jams: "
            "never (saved profiles only), auto (open the login window when a profile "
            "is missing or the session expired; interactive terminal required), "
            "always (log in before crawling)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.skip_pdf and any(
        (
            args.enable_dbpia,
            args.enable_kyobo,
            args.enable_jams,
            args.enable_sciencedirect,
        )
    ):
        parser.error("PDF provider flags cannot be used with --skip-pdf")
    try:
        result = run_kci_job(
            require_first_day=not args.allow_non_first_day,
            skip_export=args.skip_export,
            collect_pdfs=not args.skip_pdf,
            enable_dbpia=args.enable_dbpia,
            enable_kyobo=args.enable_kyobo,
            enable_jams=args.enable_jams,
            enable_sciencedirect=args.enable_sciencedirect,
            dry_run_pdf=args.dry_run_pdf,
            browser_login=args.browser_login,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        "monthly-kci: "
        f"months={','.join(result.target_months)} "
        f"corpus_stored={result.corpus.stored_count} "
        f"matched={result.incremental_match.relevant_count} "
        f"stored={result.incremental_match.stored_count} "
        f"metadata_export={result.export_path} "
        f"pdf_current={result.pdf.current_count if result.pdf else 'skipped'} "
        f"pdf_export={result.pdf_report_path}"
    )
    return 0


__all__ = [
    "JobResult",
    "RECENT_COMPLETED_MONTHS",
    "build_argument_parser",
    "database_target_months",
    "main",
    "recent_completed_months",
    "run_kci_job",
]


if __name__ == "__main__":
    raise SystemExit(main())
