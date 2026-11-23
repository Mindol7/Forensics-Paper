#!/usr/bin/env python3
"""Forensic-KCI-Paper Collector 실행 진입점.

``python3 main.py``만 실행하면 전체 파이프라인이 순서대로 돕니다.

    KCI API 논문 정보 수집(전수 또는 증분) → 블랙리스트·키워드 필터링
    → 원문 PDF 수집 → 논문 분석(Claude) → Open Access·Special Issue·참고문헌 수집
    → 유형 정리·Jev AI 분류(Level 1/2/3·태그·대상 애플리케이션) 반영 → 논문별 MD 생성 → 지식 그래프 생성

단계별 옵션(``-all``, ``-pdf``, ``-md`` 등)을 주면 그 단계만 실행합니다.  이 파일은
옵션을 ``Scheduler.pipeline``의 단계 이름으로 바꿔 넘기는 얇은 진입점이며, 실제
동작은 ``Scheduler/pipeline.py``와 각 서비스 모듈에 있습니다.  처음 실행(빈 DB)의
전수 수집도 같은 ``metadata`` 단계가 체크포인트를 보고 수행합니다.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date

from Scheduler import pipeline as pipeline_module
from Scheduler.pipeline import PipelineOptions, PipelineRunResult, expand_stages
from config import Settings, load_settings


# 단계 선택 옵션: (짧은 이름, 긴 이름, 실행할 파이프라인 단계, 설명)
STAGE_OPTIONS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("-all", "--all", ("metadata",),
     "KCI API로 논문 정보 수집 (수집 기록이 없으면 KCI_START_YEAR부터 전수, 있으면 아직 수집하지 않은 달만 + 최근 2개월 재확인) "
     "+ 신규 논문 블랙리스트·키워드 필터링"),
    ("-filter", "--filter", ("filter",),
     "블랙리스트·키워드 필터링을 전체 논문 정보에 다시 적용 (Processor/refs/의 규칙 파일을 바꾼 뒤 사용; 기존 선별 논문은 유지)"),
    ("-pdf", "--pdf", ("pdf",),
     "원문 PDF 수집 (KCI → Unpaywall → OpenAlex → Semantic Scholar → DBpia → 교보 → JAMS → ScienceDirect 순)"),
    ("-analyze", "--analyze", ("analyze",),
     "새 PDF를 Claude로 분석해 태그·Level 도출 근거(Structure/Lv3/유형) 생성"),
    ("-oa", "--oa", ("oa",), "Open Access 여부 수집"),
    ("-special", "--special", ("special",), "Special Issue 여부 수집 (KCI 로그인 필요)"),
    ("-ref", "--ref", ("ref",), "서지정보(참고문헌) 수집"),
    ("-derive", "--derive", pipeline_module.STAGE_ALIASES["derive"],
     "논문 유형·키워드 정리 + Jev AI 분류(Level-3·태그·대상 애플리케이션 Level-5) 반영 + 결과 워크북"
     "((YYMMDD)-OA·Special-Issue·Type·Tags·ref_list.xlsx) 저장"),
    ("-jev", "--jev", ("jev",),
     "Jev AI 결과(Analyzer/JevAI의 mapping_scores_*.jsonl·results_kci-*.db)를 MD 입력(Analyzer/JevAI/levels.json·targets.json)으로 정리 "
     "(새 Jev 결과를 받은 뒤 -jev -md -graph)"),
    ("-lv3", "--lv3", ("lv3",),
     "(이전 방식, 비교용) Claude로 Level 1·2·3 분류(본문·초록·요약)만 실행 — MD에는 Jev AI 분류를 씁니다"),
    ("-level5", "--level5", ("level5",),
     "(이전 방식, 비교용) 대상 애플리케이션 추출(v5.2 어휘)과 Level-5 매핑만 실행 — MD에는 Jev AI 분류를 씁니다"),
    ("-md", "--md", ("md",), "논문별 MD 파일 생성"),
    ("-renumber", "--renumber", pipeline_module.STAGE_ALIASES["renumber-all"],
     "논문 번호(KNOW-DOMP-번호)를 다시 정렬(PDF가 있는 논문을 KCI 논문 ID 순으로 1번부터)하고 워크북·MD·그래프를 새 번호로 다시 생성 "
     "(논문을 제외한 뒤 사용; 전체 실행에서는 자동으로 확인)"),
    ("-graph", "--graph", ("graphs",), "지식 그래프 3종 생성 (저자·기관·참고문헌) + 프로필을 DF-Insight에 동기화하고 웹 빌드 "
     "(대상 애플리케이션 표는 프로필(Jev AI) 그대로, 이름으로 Level-5를 붙이는 단계는 빼고 실행)"),
)


class _ExactOptionParser(argparse.ArgumentParser):
    """argparse without option-prefix matching.

    The stage flags are single-dash words (``-md``, ``-all``), which argparse
    would otherwise also accept as ``-m`` or ``-al``; a typo must not start a
    real stage, so only exact option names are recognised.
    """

    def _get_option_tuples(self, option_string: str) -> list:  # noqa: D401 - argparse hook
        return []


class _KoreanHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep the hand-written epilog layout and widen the option column."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=30, width=100)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = _ExactOptionParser(
        prog="python3 main.py",
        formatter_class=_KoreanHelpFormatter,
        allow_abbrev=False,
        description=(
            "디지털 포렌식 KCI 논문 수집·분석 파이프라인.\n"
            "옵션 없이 실행하면 전체 파이프라인을 순서대로 실행합니다:\n"
            "  논문 정보 수집(전수/증분) → 필터링 → 원문 PDF 수집 → 논문 분석\n"
            "  → OA·Special Issue·참고문헌 수집 → 유형·Level·태그 도출 → MD 생성 → 지식 그래프 생성"
        ),
        epilog=(
            "단계 선택 옵션은 여러 개를 함께 줄 수 있으며, 항상 파이프라인 순서대로 실행됩니다.\n"
            "옵션 이름은 정확히 입력해야 합니다 (-m 같은 줄임 표기는 받지 않습니다).\n\n"
            "예시:\n"
            "  python3 main.py                      전체 파이프라인\n"
            "  python3 main.py -all                 논문 정보만 수집(증분)\n"
            "  python3 main.py -pdf --browser-providers dbpia,kyobo,jams --browser-login always\n"
            "                                       처음 한 번: 로그인 창을 띄워 사이트별 프로필 생성 후 원문 PDF 수집\n"
            "  python3 main.py -pdf --browser-login always\n"
            "                                       세션 만료 시 다시 로그인한 뒤 원문 PDF 수집\n"
            "  python3 main.py -derive -md -graph   도출·MD·그래프만 다시 생성\n"
            "  python3 main.py -jev -md -graph      새 Jev AI 결과를 MD·그래프에 반영\n"
            "  python3 main.py -renumber            논문 번호를 다시 정렬하고 워크북·MD·그래프를 새 번호로 생성\n"
            "  python3 main.py --dry-run            수집·다운로드·분석 없이 계획만 출력\n"
        ),
    )
    stage_group = parser.add_argument_group("단계 선택")
    for short, long, _stages, description in STAGE_OPTIONS:
        stage_group.add_argument(short, long, action="store_true", help=description)

    common = parser.add_argument_group("공통 옵션")
    common.add_argument("--dry-run", action="store_true", help="수집·다운로드·분석 없이 계획(대상 월, 분석 대기 논문 등)만 출력 (실행 기록 파일은 남음)")
    common.add_argument("--stop-on-error", action="store_true", help="한 단계가 실패하면 즉시 중단 (기본: 의존하지 않는 단계는 계속)")
    common.add_argument("--run-date", default=None, help="오늘 날짜 대신 사용할 날짜 (YYYY-MM-DD)")
    common.add_argument("--skip-export", action="store_true", help="Excel 보고서(metadata·PDF 통계) 생성 생략")

    collect = parser.add_argument_group("수집 옵션")
    collect.add_argument("--dry-run-pdf", action="store_true", help="PDF 대상만 점검하고 다운로드하지 않음")
    collect.add_argument(
        "--browser-providers", default="auto",
        help=(
            "인증 브라우저 크롤링 대상: auto(로그인 프로필이 이미 있는 사이트 전부), none, "
            "또는 dbpia,kyobo,jams 목록 (처음 로그인할 사이트는 목록으로 지정)"
        ),
    )
    collect.add_argument(
        "--browser-login", choices=("auto", "always", "never"), default="auto",
        help="브라우저 로그인 정책: auto(프로필이 없거나 세션이 만료되면 창을 띄움), always(수집 전 항상 로그인), never",
    )
    interactivity = collect.add_mutually_exclusive_group()
    interactivity.add_argument("--interactive", action="store_true", help="사람이 로그인할 수 있는 환경으로 간주")
    interactivity.add_argument("--non-interactive", action="store_true", help="로그인 창을 절대 띄우지 않음 (cron용)")
    collect.add_argument("--enable-sciencedirect", action="store_true", help="Elsevier ScienceDirect API 단계 활성화 (API 키 필요)")
    collect.add_argument("--special-interactive-login", action="store_true", help="Special Issue 수집 전에 KCI 로그인 창을 띄움")

    analysis = parser.add_argument_group("분석 옵션")
    analysis.add_argument(
        "--analysis-backend", choices=("auto", "api", "claude-code"), default=None,
        help="논문 분석 방식: claude-code(로그인된 Claude Code, 구독), api(ANTHROPIC_API_KEY), auto(API 키가 있으면 api)",
    )
    analysis.add_argument("--analysis-model", default=None, help="분석 모델 지정 (기본 claude-opus-5)")
    analysis.add_argument("--analysis-limit", type=int, default=None, help="이번 실행에서 분석할 최대 논문 수")
    analysis.add_argument("--analysis-force", action="store_true", help="이미 분석한 논문도 다시 분석")
    analysis.add_argument("--skip-web-build", action="store_true", help="그래프 단계에서 웹(DF-Insight) 검증·빌드 생략 (Node.js/npm이 없을 때)")
    return parser


def options_from_arguments(args: argparse.Namespace) -> PipelineOptions:
    """Translate the friendly flags into pipeline stages and options."""

    selected: list[str] = []
    for _short, long, stages, _description in STAGE_OPTIONS:
        if getattr(args, long.lstrip("-").replace("-", "_")):
            selected.extend(stages)
    stages = expand_stages(selected) if selected else pipeline_module.DEFAULT_STAGES

    raw_browser = (args.browser_providers or "auto").strip().casefold()
    if raw_browser == "auto":
        browser_providers: tuple[str, ...] | None = None
    elif raw_browser == "none":
        browser_providers = ()
    else:
        browser_providers = tuple(part.strip() for part in raw_browser.split(",") if part.strip())
        unknown = [name for name in browser_providers if name not in pipeline_module.BROWSER_PROVIDERS]
        if unknown:
            raise ValueError(f"알 수 없는 브라우저 provider: {unknown} (가능: dbpia, kyobo, jams)")
    interactive: bool | None = True if args.interactive else False if args.non_interactive else None
    return PipelineOptions(
        stages=stages,
        dry_run=args.dry_run,
        stop_on_error=args.stop_on_error,
        collect_pdfs=True,
        dry_run_pdf=args.dry_run_pdf,
        skip_export=args.skip_export,
        enable_sciencedirect=args.enable_sciencedirect,
        analysis_limit=args.analysis_limit,
        analysis_force=args.analysis_force,
        analysis_model=args.analysis_model,
        analysis_backend=args.analysis_backend,
        special_interactive_login=args.special_interactive_login,
        run_web_build=not args.skip_web_build,
        browser_providers=browser_providers,
        browser_login=args.browser_login,
        interactive=interactive,
    )


def run_full_pipeline(
    options: PipelineOptions,
    *,
    settings: Settings | None = None,
    run_date: date | None = None,
) -> PipelineRunResult:
    """Run the selected stages through :mod:`Scheduler.pipeline`."""

    if settings is None:
        # Only the KCI collection needs the API key; every other stage may run without it.
        settings = load_settings(validate="metadata" in options.stages and not options.dry_run)
    return pipeline_module.run_pipeline(options, settings=settings, run_date=run_date)


def _summary_line(result: PipelineRunResult) -> str:
    labels = pipeline_module.STATUS_LABELS_KO
    titles = pipeline_module.STAGE_INFO
    parts = [f"{titles.get(report.name, (report.name, ''))[0]} {labels.get(report.status, report.status)}" for report in result.stages]
    return "결과: " + " / ".join(parts) + f" | 실행 기록: {result.ledger_path}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        options = options_from_arguments(args)
        run_date = date.fromisoformat(args.run_date) if args.run_date else None
    except ValueError as exc:
        parser.error(str(exc))
    from Util.run_lock import AlreadyRunningError

    try:
        result = run_full_pipeline(options, run_date=run_date)
    except AlreadyRunningError as exc:
        print(str(exc))
        return 2
    print(_summary_line(result))
    return 1 if result.failed else 0


__all__ = [
    "STAGE_OPTIONS",
    "build_argument_parser",
    "main",
    "options_from_arguments",
    "run_full_pipeline",
]


if __name__ == "__main__":
    raise SystemExit(main())
