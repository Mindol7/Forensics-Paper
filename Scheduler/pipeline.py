"""End-to-end monthly pipeline: collect -> analyze -> derive -> profiles -> graphs.

``python3 main.py`` (or ``python -m Scheduler.pipeline``) runs, in order:

1. ``metadata`` - KCI corpus collection for every month the checkpoint says is
   still pending (the whole configured window on an empty database, never
   skipping a month, re-checking the two most recent) and the offline
   blacklist/keyword match of the new rows.  ``filter`` (on demand) re-applies
   the current rules to the whole corpus without dropping selected papers.
   ``pdf`` - PDF collection for papers without a durable PDF, provider by
   provider (public sources first, then the authenticated browsers).
2. ``analyze``  - Claude analysis of every indexed PDF without a current
   analysis record; writes Structure documents, Level-3 manifest, type
   decision, analysis record and ``INDEX.md``.
3. ``oa`` / ``special`` / ``ref`` - Open-Access, Special-Issue and reference
   collection for every DB paper, publishing the canonical workbook even when
   some papers stay unresolved (they render as ``N/A`` downstream).
4. ``type`` / ``keywords`` - exporters over the analyzed corpus.
   ``jev`` - the paper classification of chapter III (Jev AI): Level-3, tags and
   target applications with their DF-Insight Level-5 ids, from the Jev AI results
   delivered into ``Analyzer/JevAI`` (what the profiles show); ``results`` - the
   dated result workbooks.  ``lv3`` (Level 1-3 by Claude, three inputs per paper)
   and ``level5`` (tags and Level-5 mapping by Claude) are the earlier method,
   kept for comparison and run only on request.
5. ``md``       - bilingual paper profiles.
6. ``graphs``   - the three knowledge graphs plus the DF-Insight web build.

Each stage records ``ok`` / ``partial`` / ``skipped`` / ``failed`` in a run
ledger under ``<EXPORT_DIR>/pipeline_runs/``; a failed stage never hides the
stages after it unless they depend on its output.  This module only composes
existing services and never implements collection, analysis or export logic.
"""

from __future__ import annotations

import argparse
import json
import re
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from Collector.browser_auth import (
    BROWSER_PROVIDERS,
    KCI_BIBTEX_PROVIDER,
    LOGIN_MODES,
    inspect_profile,
    is_interactive_session,
    manual_login_instructions,
)
from Collector.kci_metadata import load_corpus_done_months
from Collector.kci_pdf import default_manifest_dir, default_papers_dir
from Scheduler.job import JobResult, database_target_months, run_kci_job
from Storage.db import DatabaseManager
from Util.logger import ProgressLogger
from config import Settings, load_settings


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPOSITORY_ROOT / "web" / "DF-Insight"
# The passes of DF-Insight's linker (scripts/link-identifiers.mjs) the graphs stage runs before
# `npm run build:only`: B fills reference ids (the citation edges need them), D adds our papers to the
# target documents of the applications they link.  Pass A, which links a blank-id application row by
# its name, is left out: a blank id is the Jev AI classification's decision that no Level-5 node fits
# the application in that paper (chapter III), so the profile's table must reach the site as it is
# (``web_sync.sync_profiles(profile_rows_win=True)``).
WEB_LINK_PASSES = "B,D"
# What the graphs stage (and the build it runs) changes in the web repository: the paths of its data commit.
WEB_DATA_PATHS = ("data/references_md/", "data/targets_md/")
STAGES: tuple[str, ...] = (
    "metadata",
    "filter",
    "pdf",
    "renumber",
    "analyze",
    "oa",
    "special",
    "ref",
    "type",
    "lv3",
    "keywords",
    "level5",
    "jev",
    "results",
    "md",
    "graphs",
)
# ``filter`` re-applies the selection rules to the whole corpus; it is only run
# on demand (after the blacklist/keyword sets changed), never by default.
# ``lv3`` and ``level5`` are the earlier Claude classification, superseded by the Jev AI
# results (chapter III, the ``jev`` stage); they are kept for comparison, on request only.
ON_DEMAND_STAGES: tuple[str, ...] = ("filter", "lv3", "level5")
DEFAULT_STAGES: tuple[str, ...] = tuple(stage for stage in STAGES if stage not in ON_DEMAND_STAGES)
# Friendly names that expand to several stages.
STAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "collect": ("metadata", "pdf"),
    "derive": ("type", "keywords", "jev", "results"),
    # Re-sort the numbers and rebuild everything that shows them.
    "renumber-all": ("renumber", "type", "keywords", "jev", "results", "md", "graphs"),
    "graph": ("graphs",),
    "default": DEFAULT_STAGES,
}
# A stage is skipped when one of these stages *failed* in the same run.
STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    # An interrupted renumbering leaves the number stores half rewritten; nothing may read them until it is finished.
    "analyze": ("renumber",),
    "type": ("renumber",),
    "lv3": ("renumber",),
    "keywords": (),
    "level5": ("renumber",),
    "jev": (),
    "results": ("renumber",),
    "md": ("renumber", "type", "keywords", "jev"),
    "graphs": ("md",),
}


# What each stage is called and what it does, for the person watching the run.
STAGE_INFO: dict[str, tuple[str, str]] = {
    "metadata": ("논문 정보 수집·필터링",
                 "KCI API에서 아직 받지 않은 달의 논문 정보를 받아 저장하고, 새 논문에 블랙리스트·키워드 필터를 적용해 디지털 포렌식 논문만 고릅니다."),
    "filter": ("필터 규칙 전체 재적용",
               "바뀐 블랙리스트·키워드 규칙을 저장된 전체 논문 정보에 다시 적용합니다. 이미 고른 논문은 지우지 않습니다."),
    "pdf": ("원문 PDF 수집",
            "PDF가 없는 선별 논문을 KCI → 공개 저장소 → DBpia → 교보 → JAMS → 확인된 제공처 순으로 내려받습니다. 로그인이 필요하면 브라우저 창이 뜹니다."),
    "analyze": ("논문 분석(Claude)",
                "아직 분석하지 않은 PDF를 Claude가 읽고 구조 요약, Level·유형·태그 판정 근거를 만듭니다."),
    "oa": ("Open Access 여부 수집", "선별 논문이 무료 공개(Open Access)인지 KCI 공개 정보로 확인합니다."),
    "special": ("Special Issue 여부 수집", "선별 논문이 특집호 논문인지 KCI 서지정보로 확인합니다(KCI 로그인 필요)."),
    "ref": ("참고문헌 수집", "선별 논문의 참고문헌 목록을 KCI API로 받고, 응답의 referenceInfo XML을 논문별로 보관합니다(MD의 참고문헌 출처)."),
    "type": ("논문 유형 정리", "분석 결과와 PDF 근거를 검증해 논문 유형 워크북을 만듭니다."),
    "renumber": ("논문 번호 정렬", "논문 번호(KNOW-DOMP-번호)를 규칙대로 맞춥니다: 원문 PDF가 있는 논문을 KCI 논문 ID 순으로 1번부터, 그 뒤에 PDF가 없는 논문. 이미 맞으면 아무것도 바꾸지 않습니다."),
    "lv3": ("Level 1·2·3 분류", "논문마다 본문·초록·요약 세 가지 입력으로 Level을 분류하고(confidence 0.80 이상만 확정), 세 워크북과 levels.json을 만듭니다. 이미 분류한 논문은 다시 묻지 않습니다. "
                               "(이전 방식, 비교용 — 요청할 때만 실행하며 MD에는 Jev AI 분류를 씁니다)"),
    "results": ("결과 워크북 작성", "전체 논문의 OA·Special Issue·Type·Tags·참고문헌 결과를 날짜가 붙은 워크북((YYMMDD)-….xlsx)으로 각 폴더에 저장합니다."),
    "keywords": ("키워드 워크북 정리", "분석 결과의 기기·운영체제·애플리케이션 키워드를 모아 키워드 워크북을 만듭니다(참고 자료 — MD의 태그는 Jev AI 분류 반영 단계의 결과를 씁니다)."),
    "level5": ("대상 애플리케이션·Level-5 매핑",
               "논문마다 v5.2 추출 어휘로 태그와 대상 애플리케이션을 뽑고(원문 PDF, PDF가 없으면 초록), 시스템(DF-Insight)에 등록된 애플리케이션에는 "
               "논문 맥락에 맞는 Level-5 식별자를 붙입니다(원문 PDF·초록·본문 요약 세 입력으로 각각 매핑해 비교). "
               "등록되지 않았거나 맞는 노드가 없는 애플리케이션은 공란 목록(applicationempty_level5.xlsx)에 남기고, "
               "나중에 등록되면 다시 확인해 매핑합니다. 이미 처리한 논문은 다시 묻지 않습니다. "
               "(이전 방식, 비교용 — 요청할 때만 실행하며 MD에는 Jev AI 분류를 씁니다)"),
    "jev": ("Jev AI 분류 반영",
            "III장 '논문 분류'의 Jev AI 결과(Analyzer/JevAI의 mapping_scores_*.jsonl·results_kci-*.db)에서 논문마다 기술분류(Level-3)·태그·"
            "대상 애플리케이션(DF-Insight Level-5 식별자)을 정리해 MD 입력(Analyzer/JevAI/levels.json·targets.json)을 만듭니다. "
            "같은 논문을 여러 번 채점했으면 가장 나중 실행의 결과를 쓰고, 결과 DB와 판정이 하나라도 다르면 멈춥니다. 네트워크·Claude는 쓰지 않습니다."),
    "md": ("논문별 MD 생성", "위 워크북들과 Jev AI 분류(Level-3·태그·대상 애플리케이션 Level-5)를 합쳐 논문마다 국문·영문 프로필(MD)을 만듭니다. "
                          "원문 분석 전이라 유형을 아직 모르는 논문은 유형 칸을 N/A로 둡니다."),
    "graphs": ("지식 그래프 생성", "저자·기관·참고문헌 그래프 3종을 만들어 프로필을 검증하고, 프로필을 웹 화면(DF-Insight)에 넘긴 뒤 DF-Insight의 빌드로 웹 데이터를 다시 만듭니다. "
                               "대상 애플리케이션 표는 프로필(Jev AI 분류)의 표 그대로 넘기고 웹 빌드도 이름으로 Level-5를 붙이지 않으며, "
                               "번호가 바뀐 논문을 옛 번호로 가리키던 DF-Insight 문서의 행은 새 번호로 고칩니다."),
}
STATUS_LABELS_KO = {"ok": "완료", "partial": "일부 완료", "skipped": "건너뜀", "failed": "실패", "running": "진행 중"}
# Lines that repeat thousands of times and tell the operator nothing new.
NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*⚠ "),
    re.compile(r"Ignoring wrong pointing object"),
    re.compile(r"Advanced encoding .* not implemented"),
    re.compile(r"^> "),
)


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def stage_summary(stage: str, details: Mapping[str, Any]) -> str:
    """One plain sentence about what the stage achieved, from its ledger details."""

    def count(key: str) -> int:
        value = details.get(key)
        return len(value) if isinstance(value, (list, tuple, dict)) else int(value or 0)

    if details.get("reason"):
        return str(details["reason"])
    if details.get("blocked_by"):
        return f"앞 단계 실패로 실행하지 않음: {', '.join(details['blocked_by'])}"
    if stage == "metadata":
        months = details.get("target_months") or []
        return (f"대상 월 {months[0]}~{months[-1]}({len(months)}개월)" if months else "대상 월 없음") + (
            f" · 새로 저장한 논문 정보 {count('corpus_stored'):,}건 · 새로 선별된 논문 {count('new_source_ids')}편"
        )
    if stage == "filter":
        return (f"검사 {count('processed'):,}건 · 현재 규칙으로 선별 {count('relevant'):,}편 · 새로 추가 {count('new_source_ids')}편 · "
                f"규칙에서 빠졌지만 유지한 논문 {count('stale_source_ids')}편")
    if stage == "pdf":
        text = f"PDF 보유 {details.get('pdf_current', '?')}/{details.get('pdf_selected', '?')}편 · 아직 없는 논문 {count('pdf_unresolved_source_ids')}편"
        if details.get("located_pdf_downloaded"):
            text += f" · 확인된 제공처에서 추가 확보 {count('located_pdf_downloaded')}편"
        return text
    if stage == "analyze":
        return (f"이번에 분석 {count('analyzed_count')}편 · 실패 {count('failed_count')}편 · 이미 분석된 논문 {count('up_to_date_count')}편 · "
                f"분석 대기 {count('pending_source_ids')}편")
    pending = count("pending_source_ids")
    waiting = f" · 분석 대기(아직 포함되지 않은) 논문 {pending}편" if pending else ""
    if stage == "type":
        return f"논문 {count('paper_count')}편의 유형 정리{waiting} · 결과: {Path(str(details.get('output', ''))).name}"
    if stage == "lv3":
        return (f"논문 {count('paper_count')}편 중 Level이 확정된 논문 {count('classified_count')}편 (Level-3 {count('mapping_count')}건, "
                f"PDF가 없어 초록으로 분류한 논문 {count('abstract_basis_count')}편) · 결과: Analyzer/LV/의 세 워크북과 levels.json")
    if stage == "keywords":
        return f"논문 {count('row_count')}편의 태그 정리 · 결과: {Path(str(details.get('output', ''))).name}"
    if stage == "level5":
        return (f"논문 {count('paper_count')}편 · 대상 애플리케이션이 있는 논문 {count('papers_with_applications')}편 · "
                f"Level-5를 붙인 애플리케이션 {count('mapped_applications')}건 · 공란 목록: 미등록 {count('empty_rows')}건, "
                f"등록됐지만 맞는 노드 없음 {count('empty_no_fit')}건"
                + (f" · 아직 끝나지 않은 논문 {count('pending')}편" if count('pending') else "")
                + " · 결과: Analyzer/Application/application_list.xlsx, applicationempty_level5.xlsx")
    if stage == "jev":
        return (f"선별 논문 {count('paper_count')}편 중 Jev 결과 {count('scored_count')}편 · Level-3가 있는 논문 {count('papers_with_level3')}편 · "
                f"태그가 있는 논문 {count('papers_with_tags')}편 · 대상 애플리케이션이 있는 논문 {count('papers_with_applications')}편"
                f"(Level-5 행 {count('level5_rows')}건, ID 공란 행 {count('blank_rows')}건) · 결과 DB {details.get('results_db') or '?'}와 "
                f"{count('db_checked')}편 일치 · 결과: Analyzer/JevAI/levels.json, targets.json")
    if stage == "renumber":
        if details.get("in_order"):
            return f"논문 {count('paper_count')}편의 번호가 이미 정렬되어 있습니다 (1~{count('with_pdf')}번: PDF 있음, 그 뒤 {count('without_pdf')}편: PDF 없음)"
        if details.get("housekeeping_only"):
            return f"번호는 그대로 · 번호 기록만 정리했습니다 (논문 {count('paper_count')}편)"
        return (f"논문 {count('paper_count')}편 · 새로 번호를 받은 논문 {count('new_count')}편 · 번호가 바뀐 논문 {count('moved_count')}편 · "
                f"번호가 바뀌어 다시 만들 MD {count('profiles_cleared')}개 · 바뀐 기록: Analyzer/Manifest/renumber_history.json")
    if stage == "results":
        written = details.get("workbooks") if isinstance(details.get("workbooks"), list) else []
        return f"논문 {count('paper_count')}편 · 워크북 {len(written)}개 저장: " + ", ".join(Path(str(item)).name for item in written)
    if stage == "md":
        untyped = count("untyped_source_ids")
        waiting = f" · MD를 아직 만들지 않은 논문 {pending}편(유형·Jev 결과 대기)" if pending else ""
        return (f"논문 {count('paper_count')}편 → MD {count('markdown_count')}개 생성{waiting} · "
                + (f"유형 미확인(분석 대기, 유형 칸 N/A) {untyped}편 · " if untyped else "")
                + f"OA 미확인 {count('oa_unconfirmed')}편 · Special Issue 미확인 {count('special_unconfirmed')}편")
    if stage == "graphs":
        # A builder that failed is recorded too (with its return code); it did not produce a graph.
        built = [name for name, value in details.items() if name.startswith("build_")
                 and not (isinstance(value, Mapping) and value.get("returncode") not in (None, 0))]
        sync = details.get("web_sync")
        synced = (f"DF-Insight 프로필 추가 {sync.get('added', 0)}·갱신 {sync.get('updated', 0)}·삭제 {sync.get('removed', 0)}편"
                  if isinstance(sync, Mapping) else "DF-Insight 동기화 안 됨")
        rebuilt = sync.get("application_links_rebuilt") if isinstance(sync, Mapping) else None
        relinked = f" · 대상 애플리케이션 표를 바꾼 논문 {rebuilt}편" if isinstance(rebuilt, int) and rebuilt else ""
        renumbered = sync.get("renumbered_rows") if isinstance(sync, Mapping) else None
        if isinstance(renumbered, int) and renumbered:
            relinked += f" · 옛 번호를 새 번호로 고친 DF-Insight 문서 행 {renumbered}개"
        web = details.get("web_build")
        pending = details.get("web_repository_changes")
        other = int(details.get("web_repository_other_change_count") or 0)
        remaining = (f" · 웹 저장소에 커밋할 문서 {pending - other}개" + (f"(그 밖에 이 단계와 무관한 미커밋 파일 {other}개)" if other else "")
                     if isinstance(pending, int) else "")
        return f"그래프 {len(built)}종 생성 · {synced}{relinked} · 웹 빌드 " + ("완료" if web == "ok" else "생략" if web == "skipped" else "안 됨") + remaining
    tail = str(details.get("output_tail") or "").strip().splitlines()
    meaningful = [line for line in tail if line.strip() and not any(p.search(line) for p in NOISE_PATTERNS)]
    return meaningful[-1][:160] if meaningful else ""


def expand_stages(names: Sequence[str]) -> tuple[str, ...]:
    """Resolve aliases and return the requested stages in canonical order."""

    chosen: set[str] = set()
    unknown: list[str] = []
    for name in names:
        key = name.strip().casefold()
        if not key:
            continue
        if key in STAGE_ALIASES:
            chosen.update(STAGE_ALIASES[key])
        elif key in STAGES:
            chosen.add(key)
        else:
            unknown.append(name)
    if unknown:
        raise ValueError(f"unknown stages: {unknown}")
    return tuple(stage for stage in STAGES if stage in chosen)
COMPONENT = "PIPELINE"
LEDGER_SCHEMA_VERSION = 1
# Open-Access verdicts change rarely.  The collector's own defaults (30 days
# for Y, 7 days for N) force a multi-hour OAI re-harvest of the whole corpus
# on every monthly run; the pipeline keeps verdicts for these periods instead
# so a monthly run only harvests papers that are new or whose entry expired.
OA_OPEN_ACCESS_TTL_DAYS = 180
OA_NOT_OPEN_ACCESS_TTL_DAYS = 60
# "KCI's OAI repository has no record of this paper" is the answer of a complete
# harvest of that repository (hours of requests).  Asking again on every run
# would repeat the whole harvest for a handful of papers, so it is re-checked
# on the same schedule as a "Not Open Access" answer.
OA_UNRESOLVED_TTL_DAYS = 60


@dataclass(frozen=True, slots=True)
class PipelineOptions:
    stages: tuple[str, ...] = DEFAULT_STAGES
    dry_run: bool = False
    stop_on_error: bool = False
    collect_pdfs: bool = True
    dry_run_pdf: bool = False
    skip_export: bool = False
    enable_dbpia: bool = False
    enable_kyobo: bool = False
    enable_jams: bool = False
    enable_sciencedirect: bool = False
    analysis_limit: int | None = None
    analysis_force: bool = False
    analysis_model: str | None = None
    analysis_backend: str | None = None
    special_interactive_login: bool = False
    run_web_build: bool = True
    python_executable: str = sys.executable
    # Authenticated browser crawling (DBpia / Kyobo / JAMS).  ``None`` means
    # "auto": every provider whose dedicated profile exists, plus any provider
    # forced on with enable_*.  ``browser_login`` follows Collector.browser_auth:
    # never | auto (log in when a profile is missing or the session expired) |
    # always.  ``interactive`` forces the terminal/display detection.
    browser_providers: tuple[str, ...] | None = None
    browser_login: str = "auto"
    interactive: bool | None = None


@dataclass
class StageReport:
    name: str
    status: str  # "ok" | "partial" | "skipped" | "failed"
    started_at: str
    finished_at: str | None = None
    duration_seconds: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class PipelineRunResult:
    run_id: str
    run_date: str
    started_at: str
    finished_at: str | None
    dry_run: bool
    stages: list[StageReport]
    ledger_path: Path | None

    @property
    def failed(self) -> bool:
        return any(report.status == "failed" for report in self.stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "run_id": self.run_id,
            "run_date": self.run_date,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "stages": [asdict(report) for report in self.stages],
        }


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def target_months_from_database(settings: Settings, run_date: date) -> tuple[str, ...]:
    """Months the corpus checkpoint still owes, oldest first."""

    database = DatabaseManager(settings.database_url)
    database.create_tables()
    try:
        done = load_corpus_done_months(database)
    finally:
        database.close()
    return database_target_months(
        done, run_date, earliest_month=f"{settings.kci_start_year:04d}01"
    )


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _serialize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if hasattr(value, "to_dict"):
        return _serialize(value.to_dict())
    return value


# --------------------------------------------------------------------------- #
# Default stage runners (composition only)
# --------------------------------------------------------------------------- #

def browser_providers_for(options: PipelineOptions) -> tuple[str, ...]:
    """Browser providers this run should try, in canonical order.

    ``auto`` (``browser_providers is None``) keeps every provider whose
    dedicated profile is present (the operator logged in at least once) and
    adds any provider forced on with an ``enable_*`` flag.  An explicit list
    is used verbatim, so ``()`` disables browser crawling even when an
    ``enable_*`` flag is set.
    """

    forced = {
        name
        for name, flag in (
            ("dbpia", options.enable_dbpia),
            ("kyobo", options.enable_kyobo),
            ("jams", options.enable_jams),
        )
        if flag
    }
    if options.browser_providers is not None:
        chosen = set(options.browser_providers)
    else:
        chosen = forced | {
            provider for provider in BROWSER_PROVIDERS if inspect_profile(provider).exists
        }
    return tuple(provider for provider in BROWSER_PROVIDERS if provider in chosen)


def _run_collection(
    settings: Settings,
    options: PipelineOptions,
    run_date: date,
    logger: ProgressLogger,
    *,
    collect_metadata: bool,
    collect_pdfs: bool,
) -> tuple[str, dict[str, Any]]:
    """Shared body of the ``metadata`` and ``pdf`` stages."""

    months = target_months_from_database(settings, run_date) if collect_metadata else ()
    if collect_metadata:
        logger.emit(COMPONENT, f"수집 대상 월: {', '.join(months) or '없음(모두 완료)'}")
        if not months:
            return "skipped", {"reason": "no pending publication months", "target_months": []}
    browser = browser_providers_for(options) if collect_pdfs else ()
    if collect_pdfs:
        logger.emit(
            COMPONENT,
            f"브라우저 크롤링 provider: {', '.join(browser) or '없음'} (login={options.browser_login})",
        )
    result: JobResult = run_kci_job(
        settings=settings,
        run_date=run_date,
        require_first_day=False,
        target_months=months or None,
        skip_export=options.skip_export,
        collect_pdfs=collect_pdfs,
        collect_metadata=collect_metadata,
        enable_dbpia="dbpia" in browser,
        enable_kyobo="kyobo" in browser,
        enable_jams="jams" in browser,
        enable_sciencedirect=options.enable_sciencedirect,
        dry_run_pdf=options.dry_run_pdf,
        browser_login=options.browser_login,
        interactive=options.interactive,
    )
    details: dict[str, Any] = {}
    if collect_metadata:
        details.update(
            {
                "target_months": list(result.target_months),
                "corpus_raw": result.corpus.raw_count,
                "corpus_stored": result.corpus.stored_count,
                "matched": result.incremental_match.relevant_count,
                "stored": result.incremental_match.stored_count,
                "new_source_ids": list(getattr(result.incremental_match, "new_source_ids", ())),
                "metadata_export": str(result.export_path) if result.export_path else None,
            }
        )
    status = "ok"
    if collect_pdfs:
        details["pdf_current"] = result.pdf.current_count if result.pdf else None
        details["pdf_report"] = str(result.pdf_report_path) if result.pdf_report_path else None
        if result.pdf is not None:
            details["pdf_selected"] = result.pdf.selected_count
            details["pdf_unresolved_source_ids"] = list(result.pdf.unresolved_source_ids)
        details["browser_providers"] = list(browser)
        if result.browser_preflight is not None:
            details["browser_preflight"] = result.browser_preflight.to_dict()
        if result.browser_auth is not None:
            details["browser_auth"] = result.browser_auth.to_dict()
            if result.browser_auth.login_required and result.pdf_retry is None:
                # The saved session expired and no human was there to log in again.
                status = "partial"
                details["action_required"] = (
                    "브라우저 세션 만료: 대화형 터미널에서 "
                    "`python3 main.py -pdf --browser-login always` 실행"
                )
        located = getattr(result, "located_pdf", None)
        if located:
            details["located_pdf_downloaded"] = list(located.get("downloaded", []))
            details["located_pdf_failed"] = dict(located.get("failed", {}))
            remaining = [sid for sid in details.get("pdf_unresolved_source_ids", []) if sid not in set(located.get("downloaded", []))]
            details["pdf_unresolved_source_ids"] = remaining
            if any(str(reason).startswith("window_required") for reason in located.get("failed", {}).values()):
                status = "partial"
                details.setdefault("action_required", "브라우저 창이 필요한 제공처가 있습니다: 대화형 터미널에서 `python3 main.py -pdf` 실행")
        if result.pdf_retry is not None:
            details["pdf_retry_current"] = result.pdf_retry.current_count
            details["pdf_retry_unresolved_source_ids"] = list(result.pdf_retry.unresolved_source_ids)
    return status, details


def _metadata_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    return _run_collection(settings, options, run_date, logger, collect_metadata=True, collect_pdfs=False)


def _pdf_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    if not options.collect_pdfs:
        return "skipped", {"reason": "--skip-pdf"}
    return _run_collection(settings, options, run_date, logger, collect_metadata=False, collect_pdfs=True)


def _filter_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    """Re-apply the blacklist/keyword rules to the whole corpus (additive)."""

    from Collector.kci_metadata import run_kci_metadata_pipeline

    database = DatabaseManager(settings.database_url)
    database.create_tables()
    try:
        result = run_kci_metadata_pipeline(
            settings=settings,
            db=database,
            match_corpus=True,
            match_additive=True,
            skip_export=options.skip_export,
        )
    finally:
        database.close()
    return "ok", {
        "processed": result.raw_count,
        "relevant": result.relevant_count,
        "new_source_ids": list(result.new_source_ids),
        "stale_source_ids": list(result.stale_source_ids),
        "outside_year_window_source_ids": list(result.outside_year_window_source_ids),
        "metadata_export": str(result.export_path) if result.export_path else None,
    }


def _analyze_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    from Analyzer.analysis_stage import run_analysis_stage
    from PaperPipeline.claude_analyzer import ClaudeConfig

    overrides: dict[str, Any] = {"dry_run": options.dry_run}
    if options.analysis_model:
        overrides["model"] = options.analysis_model
    if options.analysis_backend:
        overrides["backend"] = options.analysis_backend
    config = ClaudeConfig.from_environment(**overrides)
    result = run_analysis_stage(
        database_url=settings.database_url,
        papers_dir=default_papers_dir(),
        manifest_dir=default_manifest_dir(),
        config=config,
        limit=options.analysis_limit,
        force=options.analysis_force,
        logger=logger,
    )
    return result.status, result.to_dict()


def _run_subprocess(command: Sequence[str], *, cwd: Path, logger: ProgressLogger, label: str) -> tuple[int, str]:
    """Run a stage CLI, relaying its output live so long stages stay observable.

    Returns the exit code and the last 40 output lines for the ledger.
    """

    readable = " ".join(Path(part).name if part.startswith("/") else part for part in command)
    logger.emit(COMPONENT, f"  실행: {readable}")
    process = subprocess.Popen(
        list(command),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    suppressed = 0
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            if any(pattern.search(line) for pattern in NOISE_PATTERNS):
                suppressed += 1  # kept out of the screen and the ledger tail alike
                continue
            lines.append(line)
            if line.strip():
                logger.emit(label, line)
    finally:
        process.stdout.close()
        returncode = process.wait()
    if suppressed:
        logger.emit(label, f"(반복되는 경고·안내 {suppressed:,}줄은 화면에서 생략했습니다)")
    tail = [line for line in lines if line.strip()][-40:]
    return returncode, "\n".join(tail)


def _oa_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    command = [
        options.python_executable,
        "-m",
        "Analyzer.OA.paper_oa",
        "--canonical-when-unresolved",
        "--open-access-ttl-days",
        str(OA_OPEN_ACCESS_TTL_DAYS),
        "--not-open-access-ttl-days",
        str(OA_NOT_OPEN_ACCESS_TTL_DAYS),
        "--unresolved-ttl-hours",
        str(OA_UNRESOLVED_TTL_DAYS * 24),
    ]
    code, tail = _run_subprocess(command, cwd=REPOSITORY_ROOT, logger=logger, label="KCI-OA")
    status = "ok" if code == 0 else "partial" if code == 3 else "failed"
    return status, {"returncode": code, "output_tail": tail}


def _special_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    profile = inspect_profile(KCI_BIBTEX_PROVIDER)
    interactive = (
        is_interactive_session() if options.interactive is None else options.interactive
    )
    login = options.special_interactive_login or options.browser_login == "always"
    if not profile.valid and options.browser_login != "never" and interactive:
        # First run: the KCI BibTeX export needs a logged-in browser profile.
        login = True
    if not profile.valid and not login:
        return "skipped", {
            "reason": "KCI 로그인 브라우저 프로필이 없습니다",
            "profile": profile.to_dict(),
            "action_required": manual_login_instructions([KCI_BIBTEX_PROVIDER]),
        }
    command = [
        options.python_executable,
        str(REPOSITORY_ROOT / "Analyzer" / "Special-Issue" / "special_issue.py"),
        "--allow-authenticated-collection",
        "--canonical-when-unresolved",
    ]
    if login:
        # The window is watched for the signed-in state, so nobody has to press
        # Enter on a terminal (the run may not have one).
        command += ["--interactive-login", "--login-wait-seconds", "1200"]
    code, tail = _run_subprocess(command, cwd=REPOSITORY_ROOT, logger=logger, label="KCI-SPECIAL-ISSUE")
    status = "ok" if code == 0 else "partial" if code == 1 else "failed"
    return status, {"returncode": code, "output_tail": tail, "interactive_login": login}


def _ref_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    command = [options.python_executable, "-m", "Analyzer.Ref.ref_collect", "--canonical-when-unresolved"]
    code, tail = _run_subprocess(command, cwd=REPOSITORY_ROOT, logger=logger, label="KCI-REFERENCE")
    # ref_collect exits 2 both for a partial result and for a hard failure; a
    # partial run still prints its completion line.
    if code == 0:
        status = "ok"
    elif code == 2 and "[KCI-REFERENCE] 완료" in tail:
        status = "partial"
    else:
        status = "failed"
    return status, {"returncode": code, "output_tail": tail}


def _type_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    from Analyzer.Type.paper_type import export_type_list

    result = export_type_list(progress=logger)
    return "ok", {
        "output": str(result.output),
        "paper_count": result.paper_count,
        "subtype_counts": dict(result.counts),
        "pending_source_ids": list(result.pending_source_ids),
        "migrated_count": result.migrated_count,
    }


def _lv3_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    """Level 1·2·3: classify every paper from its full text, abstract and summary.

    ``Analyzer/LV/lv_experiment.py`` keeps a per-paper cache, so only papers
    that are new (or whose input changed) are sent to Claude.  It writes the
    three dated workbooks and ``Analyzer/LV/levels.json`` (what the profiles use).
    """

    command = [
        options.python_executable, "-u", "-m", "Analyzer.LV.lv_experiment", "--cases", "1,2,3", "--workers", "6",
        "--ocr-missing", "--date", run_date.strftime("%y%m%d"),
    ]
    code, tail = _run_subprocess(command, cwd=REPOSITORY_ROOT, logger=logger, label="LEVEL")
    levels_path = REPOSITORY_ROOT / "Analyzer" / "LV" / "levels.json"
    details: dict[str, Any] = {"returncode": code, "output_tail": tail, "output": str(levels_path)}
    if levels_path.is_file():
        papers = json.loads(levels_path.read_text(encoding="utf-8")).get("papers", {})
        details.update({
            "paper_count": len(papers),
            "classified_count": sum(1 for record in papers.values() if record.get("lv3")),
            "mapping_count": sum(len(record.get("lv3", [])) for record in papers.values()),
            "abstract_basis_count": sum(1 for record in papers.values() if record.get("basis") == "Abstract"),
            "pending_level_count": sum(1 for record in papers.values() if record.get("status") == "pending"),
        })
    if code == 3:
        # The classifier ran but could not finish every paper (usage limit, failed calls).
        reason = next((line for line in reversed(str(tail).splitlines()) if "LEVEL INCOMPLETE" in line), "")
        details["reason"] = reason.split("LEVEL INCOMPLETE:", 1)[-1].strip()
        details["action_required"] = "python3 main.py -derive 를 다시 실행하면 남은 논문만 이어서 분류합니다 (MD는 모든 논문의 Level이 끝난 뒤에 만들어집니다)."
        return "partial", details
    if code != 0:
        details["reason"] = next((line.strip() for line in reversed(str(tail).splitlines()) if line.strip()), "")[:300]
        return "failed", details
    return "ok", details


def _level5_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    """Tags and target applications (v5.2 vocabulary) and their Level-5 ids.

    ``Analyzer/Application/level5.py --extract`` runs Step 1 (extraction) for papers
    without a cached result, checks every application against DF-Insight's Level-5
    nodes, maps the registered ones and writes ``targets.json`` (what the profiles
    use), ``application_list.xlsx`` and ``applicationempty_level5.xlsx``.  Because the
    registry is read on every run, applications DF-Insight registered since the last
    run are found and mapped here (Step 5).
    """

    command = [options.python_executable, "-u", "-m", "Analyzer.Application.level5", "--extract", "--workers", "6"]
    code, tail = _run_subprocess(command, cwd=REPOSITORY_ROOT, logger=logger, label="LEVEL5")
    targets_path = REPOSITORY_ROOT / "Analyzer" / "Application" / "targets.json"
    details: dict[str, Any] = {"returncode": code, "output_tail": tail, "output": str(targets_path)}
    if targets_path.is_file():
        papers = json.loads(targets_path.read_text(encoding="utf-8")).get("papers", {})
        applications = [item for record in papers.values() for item in record.get("applications", [])]
        details.update({
            "paper_count": len(papers),
            "papers_with_applications": sum(1 for record in papers.values() if record.get("applications")),
            "mapped_applications": sum(1 for item in applications if item.get("level5")),
            "empty_rows": sum(1 for item in applications if item.get("status") in ("unregistered", "identity_mismatch")),
            "empty_no_fit": sum(1 for item in applications if item.get("status") == "no_fit"),
            # Abstract-/Summary-Based mappings (comparison only; the profiles use the PDF-Based result).
            "comparison_pending": sum(1 for item in applications for basis in ("Abstract", "Summary")
                                      if (item.get("level5_by_basis") or {}).get(basis, {}).get("status") == "pending"),
            "pending": sum(1 for record in papers.values() if record.get("status") != "ok"),
        })
    if code == 3:
        if details.get("pending") == 0 and details.get("comparison_pending"):
            details["reason"] = ("입력별 비교용 Level-5 매핑(Abstract·Summary-Based) 일부가 끝나지 않았습니다(사용량 제한·실패). "
                                 "MD에 쓰는 PDF-Based 결과는 모두 끝났습니다.")
            details["action_required"] = "python3 main.py -level5 를 다시 실행하면 남은 비교 매핑만 이어서 처리합니다."
        else:
            details["reason"] = "일부 논문의 대상 추출 또는 Level-5 매핑이 끝나지 않았습니다(사용량 제한·실패)."
            details["action_required"] = "python3 main.py -derive 를 다시 실행하면 남은 논문만 이어서 처리합니다 (MD는 모든 논문이 끝난 뒤에 만들어집니다)."
        return "partial", details
    if code != 0:
        details["reason"] = next((line.strip() for line in reversed(str(tail).splitlines()) if line.strip()), "")[:300]
        return "failed", details
    return "ok", details


def _jev_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    """Classification, tags and target applications from the Jev AI results (chapter III '논문 분류').

    ``Analyzer/JevAI/jev_fields.py`` merges every Jev run of ``Analyzer/JevAI`` per paper (the
    latest run wins), checks the decisions against the latest results DB (a difference stops
    the stage) and writes ``Analyzer/JevAI/levels.json`` and ``targets.json``, which the
    profiles use.  Offline: no network, no Claude.
    """

    from Analyzer.JevAI.jev_fields import export
    from Storage.db import sqlite_database_path

    result = export(database=Path(sqlite_database_path(settings.database_url)))
    details = result.to_dict()
    if result.not_selected:
        logger.emit(COMPONENT, f"  선별 목록에 없어 쓰지 않은 Jev 결과 {len(result.not_selected)}편: {', '.join(result.not_selected[:5])}")
    if result.pending:
        details["reason"] = (f"선별 논문 {len(result.pending)}편은 Jev AI 결과가 아직 없습니다({', '.join(result.pending[:5])}"
                             f"{' 외' if len(result.pending) > 5 else ''}) — 나머지 {result.scored}편은 반영했습니다")
        details["action_required"] = (
            "그 논문들의 Jev AI 결과(mapping_scores_*.jsonl, results_kci-*.db)를 Analyzer/JevAI에 넣고 "
            "`python3 main.py -jev -md -graph`를 다시 실행하세요 (결과가 없는 논문의 MD는 그 뒤에 만들어집니다)."
        )
        return "partial", details
    return "ok", details


def _keywords_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    from Analyzer.keyword_workbook import build_keyword_workbooks

    result = build_keyword_workbooks()
    return "ok", {
        "output": str(result.path),
        "row_count": result.row_count,
        "legacy_count": result.legacy_count,
        "analysis_count": result.analysis_count,
    }


NUMBER_READING_STAGES = frozenset({"analyze", "type", "lv3", "level5", "results", "md", "graphs"})


def _with_pending_renumber(options: PipelineOptions, journal: Path | None = None) -> PipelineOptions:
    """An interrupted renumbering leaves the number stores half rewritten: finish it before any stage reads them."""

    from dataclasses import replace

    pending = journal or REPOSITORY_ROOT / "Analyzer" / "Manifest" / ".renumber-journal.json"
    if options.dry_run or "renumber" in options.stages or not pending.is_file() or not NUMBER_READING_STAGES & set(options.stages):
        return options
    return replace(options, stages=expand_stages([*options.stages, "renumber"]))


def _renumber_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    """Keep the paper numbers sorted and without gaps (``Analyzer/numbering.py``); a no-op when they already are."""

    from Analyzer.numbering import renumber
    from Storage.db import sqlite_database_path

    report = renumber(database=Path(sqlite_database_path(settings.database_url)))
    if not report["in_order"] and not report.get("housekeeping_only"):
        for change in report.get("first_changes", [])[:5]:
            origin = "새 논문" if change["from"] is None else f"{change['from']}번"
            logger.emit(COMPONENT, f"  {change['source_id']}: {origin} → {change['to']}번")
        downstream = {"type", "keywords", "jev", "results", "md", "graphs"}
        if not downstream <= set(options.stages):
            report["action_required"] = "번호가 바뀌었습니다. `python3 main.py -renumber`(또는 -derive -md -graph)로 워크북·MD·그래프를 새 번호로 다시 만드세요."
    return "ok", report


def _results_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    """OA · Special Issue · Type · Tags · ref_list: one dated workbook each, for every paper of the corpus."""

    from Analyzer.deliverables import export_all

    results = export_all(run_date.strftime("%y%m%d"))
    for item in results:
        logger.emit(COMPONENT, f"  {item.path.relative_to(REPOSITORY_ROOT)} — 논문 {item.paper_count}편 중 값이 있는 논문 {item.filled_count}편")
    return "ok", {
        "paper_count": results[0].paper_count if results else 0,
        "workbooks": [str(item.path) for item in results],
        "filled": {item.name: item.filled_count for item in results},
    }


def _update_collection_tracker(run_date: date, logger: ProgressLogger) -> None:
    """Rewrite ``Analyzer/KCI-Collector.json``; a tracker problem must never fail the run."""

    try:
        from Analyzer.collection_status import TRACKER_PATH, update_tracker

        status = update_tracker(today=run_date)
    except Exception as exc:  # noqa: BLE001 - reporting only
        logger.emit(COMPONENT, f"수집 현황판을 갱신하지 못했습니다: {type(exc).__name__}: {exc}")
        return
    summary = status["요약"]
    logger.emit(
        COMPONENT,
        f"수집 현황판 갱신: {TRACKER_PATH.relative_to(REPOSITORY_ROOT)} — 총 {summary['현재 수집된 KCI 포렌식 논문 수'] + summary['제거된 포렌식 논문 수']}편 = "
        f"현재 {summary['현재 수집된 KCI 포렌식 논문 수']}편 + 제거 {summary['제거된 포렌식 논문 수']}편 · 원문 PDF {summary['원문 PDF 수집']} · MD {summary['MD 파일 생성']}",
    )


def _md_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    from Analyzer.MD.paper_md import PaperMarkdownError, generate

    try:
        summary = generate(updated_on=run_date.isoformat())
    except PaperMarkdownError as exc:
        if "without an indexed PDF" in str(exc) or "unexpected" in str(exc).casefold():
            # The usual cause: papers were removed (or renumbered) and the workbooks the profiles are built from are older.
            raise PaperMarkdownError(f"{exc} — 논문을 제외했거나 번호가 바뀐 뒤라면 `python3 main.py -renumber`를 먼저 실행하세요") from exc
        raise
    return "ok", {
        "paper_count": summary.paper_count,
        "markdown_count": summary.markdown_count,
        "oa_unconfirmed": summary.oa_unconfirmed,
        "special_unconfirmed": summary.special_unconfirmed,
        "reference_unavailable": summary.reference_unavailable_count,
        "pending_source_ids": list(summary.pending_source_ids),
        "untyped_source_ids": list(summary.untyped_source_ids),
    }


def _graphs_stage(settings: Settings, options: PipelineOptions, run_date: date, logger: ProgressLogger) -> tuple[str, dict[str, Any]]:
    """Knowledge graphs.

    1. The pipeline's own three graphs (author, institution, reference) are built
       into ``Analyzer/Graph/out/`` with their audits: they cross-check every
       profile against the database and the citation evidence.
    2. The profiles are handed to the DF-Insight web app (``web/DF-Insight``,
       ``data/references_md/KNOW-DOMP-*``), which is where the deployed site
       reads them (``Analyzer/web_sync.py``).  The application table of every
       copy is the profile's, i.e. the Jev AI classification (chapter III), and a
       row of DF-Insight's own documents that names a renumbered paper by its old
       number gets the new one.
    3. DF-Insight's build regenerates its manifest, graphs and trends, so the
       local site shows the new profiles: its linker first, with the passes in
       ``WEB_LINK_PASSES`` only, then ``npm run build:only``.
       Committing/pushing the web repository stays a human step.
    """

    from Analyzer import web_sync   # attributes are read at call time, so a test can redirect the state file

    details: dict[str, Any] = {}
    for script in ("build_author_graph.py", "build_institution_graph.py", "build_reference_graph.py"):
        code, tail = _run_subprocess(
            [options.python_executable, str(REPOSITORY_ROOT / "Analyzer" / "Graph" / script)],
            cwd=REPOSITORY_ROOT,
            logger=logger,
            label=script.removesuffix(".py"),
        )
        details[script] = {"returncode": code, "output_tail": tail}
        if code != 0:
            return "failed", details
    try:
        report = web_sync.sync_profiles(web=WEB_DIR, state=web_sync.DEFAULT_STATE, profile_rows_win=True)
    except web_sync.WebSyncError as exc:
        details["web_sync"] = str(exc)
        details["action_required"] = (
            f"프로필을 web/DF-Insight 에 넘기지 못했습니다 — {exc} "
            "(저장소가 없으면 git clone https://github.com/dfrc-korea/DF-Insight.git web/DF-Insight)"
        )
        return "partial", details
    details["web_sync"] = report.as_dict()
    logger.emit(COMPONENT, f"  DF-Insight 프로필 동기화: 추가 {len(report.added)}편 · 갱신 {len(report.updated)}편 · 삭제 {len(report.removed)}편 · 그대로 {report.unchanged}편")
    logger.emit(COMPONENT, f"  대상 애플리케이션 표는 프로필(Jev AI 분류)의 표 그대로: 웹 사본이 이미 같은 논문 {report.links_kept}편 · 웹 사본의 표를 바꾼 논문 {len(report.relinked)}편 "
                           "(ID가 빈 행은 Jev AI가 맞는 Level-5 노드를 찾지 못한 애플리케이션 — 웹 빌드도 이름으로 연결하지 않음)")
    for record_id, reason in list(report.relinked.items())[:10]:
        logger.emit(COMPONENT, f"    표 교체: {record_id} — {reason}")
    if len(report.relinked) > 10:
        logger.emit(COMPONENT, f"    … 외 {len(report.relinked) - 10}편 (실행 기록의 web_sync 항목 참고)")
    if report.renumbered_rows or report.unlinked_rows:
        logger.emit(COMPONENT, f"  번호가 바뀐 논문을 옛 번호로 가리키던 DF-Insight 문서(대상 문서·다른 문헌의 참고문헌)의 행 {report.renumbered_rows}개를 새 번호로 고쳤습니다"
                               f"(문서 {len(report.renumbered_documents)}개)"
                               + (f" · 프로필이 없어진 논문의 번호를 지운 행 {report.unlinked_rows}개" if report.unlinked_rows else ""))
    if report.unverified_rows:
        details["web_sync_unverified_rows"] = report.unverified_rows[:20]
        warning = (
            f"번호가 바뀐 논문의 옛 번호가 적힌 행인데 제목·DOI로 그 논문인지 확인하지 못해 그대로 둔 행 {len(report.unverified_rows)}개: "
            f"{', '.join(report.unverified_rows[:5])}{' 외' if len(report.unverified_rows) > 5 else ''} — web/DF-Insight/data/<문서>에서 그 행의 번호가 맞는지 확인하세요."
        )
        logger.emit(COMPONENT, f"  주의: {warning}")
        details["web_sync_warning"] = warning
    if not report.state_found:
        state_path = web_sync.DEFAULT_STATE
        record = state_path.relative_to(REPOSITORY_ROOT) if state_path.is_relative_to(REPOSITORY_ROOT) else state_path
        logger.emit(COMPONENT, f"  동기화 기록이 없어 기록을 새로 만들었습니다 ({record})")
    if report.unreadable:
        details["web_sync_unreadable"] = list(report.unreadable)
        logger.emit(COMPONENT, f"  주의: 웹 사본의 표를 읽을 수 없어(표 손상·파일 누락·중복) 프로필 내용으로 다시 쓴 논문 {len(report.unreadable)}편: {', '.join(report.unreadable[:10])}")
    if not options.run_web_build:
        details["web_build"] = "skipped"
        if report.changed or report.renumbered_documents:
            details["action_required"] = _join_actions(details.get("action_required"), (
                "웹 빌드를 생략했습니다. 다시 쓴 프로필 사본의 참고문헌 ID와 대상 문서가 아직 연결되지 않은 상태이니 web/DF-Insight 를 커밋하지 말고, "
                "`python3 main.py -graph` 를 (생략 옵션 없이) 다시 실행해 빌드까지 마치세요."
            ))
        return "ok", details
    npm = shutil.which("npm")
    if npm is None:
        details["web_build"] = "npm not found"
        details["action_required"] = _join_actions(details.get("action_required"), (
            "Node.js/npm이 없어 웹 빌드를 하지 못했습니다. 설치한 뒤 `python3 main.py -graph` 를 다시 실행하세요 "
            "(그 전에는 web/DF-Insight 를 커밋하지 마세요: 프로필 사본의 식별자가 아직 연결되지 않았습니다)."
        ))
        return "partial", details
    # DF-Insight's `npm run build` is `link:write && build:only`; the linker runs with the passes the profiles allow.
    for label, command in (("npm:link", [npm, "run", "link:write", "--", f"--only={WEB_LINK_PASSES}"]),
                           ("npm:build", [npm, "run", "build:only"])):
        code, tail = _run_subprocess(command, cwd=WEB_DIR, logger=logger, label=label)
        details[label] = {"returncode": code, "output_tail": tail}
        if code != 0:
            details["action_required"] = _join_actions(details.get("action_required"), (
                "웹 빌드가 실패했습니다. 원인을 고친 뒤 `python3 main.py -graph` 를 다시 실행하세요 "
                "(그 전에는 web/DF-Insight 를 커밋하지 마세요: 프로필 사본의 식별자가 아직 연결되지 않았을 수 있습니다)."
            ))
            return "failed", details
    details["web_build"] = "ok"
    # The build fills the reference ids our export leaves blank; copies it brought back to their previous content keep their old date.
    restored = web_sync.restore_unchanged_after_build(report)
    details["web_sync"] = report.as_dict()
    if restored:
        logger.emit(COMPONENT, f"  빌드 뒤 내용이 그대로인 문서 {restored}개는 이전 사본을 유지했습니다")
    # DF-Insight's build rewrites the copies it links (and adds a newly linked paper to its target documents),
    # so only git knows what differs from the last commit.
    pending = _web_repository_changes()
    details["web_repository_changes"] = pending
    if pending:
        # Only the documents this stage writes belong in the data commit; anything else uncommitted there is somebody's work.
        elsewhere = [path for path in (_web_repository_paths() or []) if not path.startswith(WEB_DATA_PATHS)]
        details["action_required"] = _join_actions(details.get("action_required"), (
            f"web/DF-Insight 의 문서 {pending}개가 마지막 커밋과 다릅니다. git -C web/DF-Insight status --short 와 git -C web/DF-Insight diff 로 "
            "바뀐 내용을 확인한 뒤, 배포 사이트에 올리려면 이 단계가 쓴 문서만 커밋·푸시하세요: "
            f"git -C web/DF-Insight add {' '.join(path.rstrip('/') for path in WEB_DATA_PATHS)} 다음 git -C web/DF-Insight commit (git add -A 는 쓰지 마세요)."
        ))
        if elsewhere:
            details["web_repository_other_changes"] = elsewhere[:20]
            details["web_repository_other_change_count"] = len(elsewhere)
            details["action_required"] += (
                f" 그 밖에 커밋되지 않은 파일 {len(elsewhere)}개({', '.join(elsewhere[:5])}{' 외' if len(elsewhere) > 5 else ''})는 "
                "이 단계가 만든 것이 아니니 이 커밋에 넣지 마세요."
            )
        if report.unreadable:
            details["action_required"] += (
                f" 웹 사본의 표를 읽을 수 없어 프로필 내용으로 다시 쓴 논문이 있습니다({', '.join(report.unreadable[:10])}): "
                "웹 쪽에서 손으로 고친 내용이 없었는지 diff로 꼭 확인하세요."
            )
    # DF-Insight's validation does not look at these ids: a node removed there (after the jev/md stages checked
    # the ids against the registry) would silently drop the application from the site.
    unknown = web_sync.unknown_application_ids(WEB_DIR)
    if unknown:
        details["web_sync_unknown_application_ids"] = dict(list(unknown.items())[:20])
        listed = ", ".join(f"{record_id}({' '.join(ids[:3])})" for record_id, ids in list(unknown.items())[:5])
        warning = (
            f"웹 사본의 대상 애플리케이션 표에 DF-Insight에 없는 노드 ID가 있는 논문 {len(unknown)}편: {listed}"
            f"{' 외' if len(unknown) > 5 else ''}. DF-Insight에서 그 노드가 없어졌거나 번호가 바뀐 것이니 "
            "`python3 main.py -jev -md -graph` 로 다시 만드세요 (Jev AI 결과의 ID가 DF-Insight에 없으면 jev 단계가 멈추고 알려 줍니다)."
        )
        logger.emit(COMPONENT, f"  주의: {warning}")
        details["action_required"] = _join_actions(details.get("action_required"), warning)
    return "ok", details


def _join_actions(first: str | None, second: str) -> str:
    return f"{first} {second}" if first else second


def _web_repository_paths() -> list[str] | None:
    """Paths in the web repository that differ from its last commit, new files included (build output is
    git-ignored there); None when the checkout is not a git repository.  Read only: no index refresh."""

    git = shutil.which("git")
    if git is None or not (WEB_DIR / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            [git, "--no-optional-locks", "-C", str(WEB_DIR), "status", "--porcelain", "-z", "--untracked-files=all"],
            capture_output=True, check=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    paths: list[str] = []
    entries = iter(completed.stdout.decode("utf-8", "replace").split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":
            next(entries, None)   # a rename or copy names its source path in the next entry
    return paths


def _web_repository_changes() -> int | None:
    """Number of files in the web repository that differ from its last commit (``_web_repository_paths``)."""

    paths = _web_repository_paths()
    return None if paths is None else len(paths)


StageRunner = Callable[[Settings, PipelineOptions, date, ProgressLogger], tuple[str, dict[str, Any]]]
DEFAULT_RUNNERS: dict[str, StageRunner] = {
    "metadata": _metadata_stage,
    "filter": _filter_stage,
    "pdf": _pdf_stage,
    "renumber": _renumber_stage,
    "analyze": _analyze_stage,
    "oa": _oa_stage,
    "special": _special_stage,
    "ref": _ref_stage,
    "type": _type_stage,
    "lv3": _lv3_stage,
    "keywords": _keywords_stage,
    "level5": _level5_stage,
    "jev": _jev_stage,
    "results": _results_stage,
    "md": _md_stage,
    "graphs": _graphs_stage,
}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def write_ledger(result: PipelineRunResult, ledger_dir: Path) -> Path:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / f"{result.run_id}.json"
    temporary = path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(_serialize(result.to_dict()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _analysis_config(options: PipelineOptions) -> Any:
    from PaperPipeline.claude_analyzer import ClaudeConfig

    overrides: dict[str, Any] = {}
    if options.analysis_model:
        overrides["model"] = options.analysis_model
    if options.analysis_backend:
        overrides["backend"] = options.analysis_backend
    return ClaudeConfig.from_environment(**overrides)


def plan(settings: Settings, options: PipelineOptions, run_date: date) -> dict[str, Any]:
    """Describe what a run would do without touching the network or disk."""

    from Analyzer.analysis_stage import has_claude_credentials, select_targets
    from PaperPipeline.paper_files import PAPER_FILE_INDEX_NAME, load_paper_file_index

    months = target_months_from_database(settings, run_date) if "metadata" in options.stages else ()
    manifest_dir = default_manifest_dir()
    pending: list[str] = []
    up_to_date = 0
    index_path = manifest_dir / PAPER_FILE_INDEX_NAME
    if index_path.is_file():
        records = load_paper_file_index(index_path)
        targets, current, _missing = select_targets(
            records,
            papers_dir=default_papers_dir(),
            manifest_dir=manifest_dir,
            analysis_dir=REPOSITORY_ROOT / "Analyzer" / "Manifest" / "Analysis",
            lv3_dir=REPOSITORY_ROOT / "Analyzer" / "Manifest" / "Lv3",
            structure_dir=REPOSITORY_ROOT / "Analyzer" / "Structure",
        )
        pending = [target.record.source_id for target in targets]
        up_to_date = len(current)
    return {
        "run_date": run_date.isoformat(),
        "stages": list(options.stages),
        "target_months": list(months),
        "analysis_pending": pending,
        "analysis_up_to_date": up_to_date,
        "claude_credentials": has_claude_credentials(),
        "analysis_backend": _analysis_config(options).resolved_backend(),
        "kci_api_key": bool(settings.has_kci_key),
        "browser_providers": list(browser_providers_for(options)),
        "browser_login": options.browser_login,
        "interactive": (
            is_interactive_session() if options.interactive is None else options.interactive
        ),
        "browser_profiles": {
            provider: inspect_profile(provider).to_dict()
            for provider in (*BROWSER_PROVIDERS, KCI_BIBTEX_PROVIDER)
        },
    }


def describe_plan(description: Mapping[str, Any]) -> list[str]:
    """The dry-run plan as sentences instead of a JSON blob."""

    lines = ["실행 계획 (실제로는 아무것도 수집·다운로드·분석하지 않았습니다)"]
    stages = description.get("stages") or []
    lines.append("  실행할 단계: " + " → ".join(STAGE_INFO.get(stage, (stage, ""))[0] for stage in stages))
    months = description.get("target_months") or []
    if "metadata" in stages:
        lines.append(f"  논문 정보를 받을 달: {', '.join(months) if months else '없음(모두 받음)'}"
                     + ("" if description.get("kci_api_key") else " — 주의: KCI_OPEN_API_KEY가 설정되지 않았습니다"))
    if "pdf" in stages:
        providers = description.get("browser_providers") or []
        lines.append(f"  로그인 브라우저로 크롤링할 사이트: {', '.join(providers) if providers else '없음'} "
                     f"(로그인 창 정책 {description.get('browser_login')}, 사람이 있는 실행: {'예' if description.get('interactive') else '아니오'})")
    if "analyze" in stages:
        pending = description.get("analysis_pending") or []
        lines.append(f"  분석 대기 논문 {len(pending)}편 · 이미 분석된 논문 {description.get('analysis_up_to_date', 0)}편 · "
                     f"분석 방식 {description.get('analysis_backend')} · 사용 가능: {'예' if description.get('claude_credentials') else '아니오'}")
    return lines


def _run_pipeline_unlocked(
    options: PipelineOptions,
    *,
    settings: Settings | None = None,
    run_date: date | None = None,
    runners: Mapping[str, StageRunner] | None = None,
    logger: ProgressLogger | None = None,
    ledger_dir: Path | None = None,
) -> PipelineRunResult:
    """Run the selected stages in canonical order and persist the ledger (the caller holds the run lock)."""

    logger = logger or ProgressLogger()
    settings = settings or load_settings(validate=False)
    effective_date = run_date or date.today()
    # The collection tracker describes the real repository, so only a real run (no injected runners) rewrites it.
    real_run = not runners
    runners = {**DEFAULT_RUNNERS, **(runners or {})}
    unknown = [stage for stage in options.stages if stage not in STAGES]
    if unknown:
        raise ValueError(f"unknown pipeline stages: {unknown}")
    selected = [stage for stage in STAGES if stage in set(options.stages)]
    started = _now()
    run_id = started.strftime("%Y%m%dT%H%M%S")
    result = PipelineRunResult(
        run_id=run_id,
        run_date=effective_date.isoformat(),
        started_at=started.isoformat(),
        finished_at=None,
        dry_run=options.dry_run,
        stages=[],
        ledger_path=None,
    )
    ledger_dir = ledger_dir or (Path(settings.export_dir) / "pipeline_runs")
    # pypdf reports every malformed object and unsupported font encoding of every PDF (even at ERROR
    # level); none of it is actionable here and it buries the progress lines.
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    titles = " → ".join(STAGE_INFO.get(stage, (stage, ""))[0] for stage in selected)
    logger.emit(COMPONENT, f"파이프라인 시작 — 총 {len(selected)}단계: {titles}")
    result.ledger_path = write_ledger(result, ledger_dir)
    logger.emit(COMPONENT, f"실행 기록 파일: {result.ledger_path} (단계가 끝날 때마다 갱신)")

    if options.dry_run:
        description = plan(settings, options, effective_date)
        for stage in selected:
            result.stages.append(
                StageReport(
                    name=stage,
                    status="skipped",
                    started_at=started.isoformat(),
                    finished_at=started.isoformat(),
                    duration_seconds=0.0,
                    details={"dry_run": True, **({"plan": description} if stage == selected[0] else {})},
                )
            )
        result.finished_at = _now().isoformat()
        result.ledger_path = write_ledger(result, ledger_dir)
        for line in describe_plan(description):
            logger.emit(COMPONENT, line)
        return result

    failed_stages: set[str] = set()
    unavailable: set[str] = set()  # failed, or skipped because a dependency failed
    for position, stage in enumerate(selected, start=1):
        title, purpose = STAGE_INFO.get(stage, (stage, ""))
        head = f"[{position}/{len(selected)}단계] {title}"
        stage_started = _now()
        report = StageReport(name=stage, status="skipped", started_at=stage_started.isoformat())
        blocked = [dependency for dependency in STAGE_DEPENDENCIES.get(stage, ()) if dependency in unavailable]
        if blocked:
            report.details = {"blocked_by": blocked}
            unavailable.add(stage)
            names = ", ".join(STAGE_INFO.get(name, (name, ""))[0] for name in blocked)
            logger.emit(COMPONENT, f"━━ {head} 건너뜀 — 앞 단계({names})가 실패해 실행할 수 없습니다")
        else:
            logger.emit(COMPONENT, f"━━ {head} 시작 ━━")
            if purpose:
                logger.emit(COMPONENT, f"  하는 일: {purpose}")
            report.status = "running"
            result.stages.append(report)
            write_ledger(result, ledger_dir)
            result.stages.pop()
            report.status = "skipped"
            clock = time.monotonic()
            try:
                status, details = runners[stage](settings, options, effective_date, logger)
                report.status = status
                report.details = _serialize(details)
            except Exception as exc:  # a stage failure must reach the ledger
                report.status = "failed"
                report.error = f"{type(exc).__name__}: {exc}"
                report.details = {"traceback": traceback.format_exc().splitlines()[-12:]}
                failed_stages.add(stage)
                unavailable.add(stage)
                logger.emit(COMPONENT, f"━━ {head} 실패 ({format_duration(time.monotonic() - clock)}) — {report.error}")
            else:
                if status == "failed":
                    failed_stages.add(stage)
                    unavailable.add(stage)
                summary_text = stage_summary(stage, report.details if isinstance(report.details, Mapping) else {})
                logger.emit(
                    COMPONENT,
                    f"━━ {head} {STATUS_LABELS_KO.get(status, status)} ({format_duration(time.monotonic() - clock)})"
                    + (f" — {summary_text}" if summary_text else ""),
                )
                action = report.details.get("action_required") if isinstance(report.details, Mapping) else None
                if action:
                    logger.emit(COMPONENT, f"  ▶ 해야 할 일: {action}")
        logger.emit(COMPONENT, f"  전체 진행률 {position}/{len(selected)}단계 ({position / len(selected):.0%})")
        finished = _now()
        report.finished_at = finished.isoformat()
        report.duration_seconds = (finished - stage_started).total_seconds()
        result.stages.append(report)
        # Persist after every stage so a killed run still leaves its ledger.
        result.ledger_path = write_ledger(result, ledger_dir)
        if report.status == "failed" and options.stop_on_error:
            logger.emit(COMPONENT, "stop-on-error: 남은 단계를 실행하지 않습니다")
            break
    result.finished_at = _now().isoformat()
    result.ledger_path = write_ledger(result, ledger_dir)
    if real_run:
        _update_collection_tracker(effective_date, logger)
    logger.emit(COMPONENT, "━━ 파이프라인 종료 — 단계별 결과 ━━")
    for report in result.stages:
        title = STAGE_INFO.get(report.name, (report.name, ""))[0]
        logger.emit(
            COMPONENT,
            f"  {title}: {STATUS_LABELS_KO.get(report.status, report.status)} ({format_duration(report.duration_seconds or 0)})"
            + (f" — {report.error}" if report.error else ""),
        )
    logger.emit(COMPONENT, f"실행 기록: {result.ledger_path}")
    return result


def run_pipeline(
    options: PipelineOptions,
    *,
    settings: Settings | None = None,
    run_date: date | None = None,
    runners: Mapping[str, StageRunner] | None = None,
    logger: ProgressLogger | None = None,
    ledger_dir: Path | None = None,
) -> PipelineRunResult:
    """Run the selected stages; a real run holds the repository's run lock for its whole duration."""

    from Util.run_lock import exclusive_run

    options = _with_pending_renumber(options)
    if runners or options.dry_run:  # injected runners (tests) and dry runs change nothing in the repository
        return _run_pipeline_unlocked(options, settings=settings, run_date=run_date, runners=runners, logger=logger, ledger_dir=ledger_dir)
    with exclusive_run("python3 main.py (파이프라인)"):
        return _run_pipeline_unlocked(options, settings=settings, run_date=run_date, runners=runners, logger=logger, ledger_dir=ledger_dir)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_argument_parser() -> argparse.ArgumentParser:
    alias_help = ", ".join(
        f"{alias}={'+'.join(members)}" for alias, members in STAGE_ALIASES.items() if alias != "default"
    )
    parser = argparse.ArgumentParser(
        prog="python -m Scheduler.pipeline",
        description="Run the incremental collect→analyze→derive→MD→graph pipeline.",
    )
    parser.add_argument(
        "--stages",
        default=",".join(DEFAULT_STAGES),
        help=(
            "Comma-separated stages to run in canonical order "
            f"(default: {','.join(DEFAULT_STAGES)}; also: filter; aliases: {alias_help})."
        ),
    )
    for stage in (*STAGES, *(alias for alias in STAGE_ALIASES if alias != "default")):
        if stage == "pdf":
            continue  # defined below, mutually exclusive with --dry-run-pdf
        parser.add_argument(f"--skip-{stage}", action="store_true", help=f"Skip the {stage} stage.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; touch nothing.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop at the first failed stage.")
    parser.add_argument("--run-date", default=None, help="Override today's date (YYYY-MM-DD).")
    parser.add_argument(
        "--allow-non-first-day",
        action="store_true",
        help="Accepted for compatibility with Scheduler.job; the pipeline is checkpoint-driven.",
    )
    pdf_group = parser.add_mutually_exclusive_group()
    pdf_group.add_argument("--skip-pdf", action="store_true", help="Skip the pdf stage (PDF collection).")
    pdf_group.add_argument("--dry-run-pdf", action="store_true", help="Audit PDF targets without requests.")
    parser.add_argument("--skip-export", action="store_true", help="Skip the metadata/PDF reports.")
    parser.add_argument("--enable-dbpia", action="store_true")
    parser.add_argument("--enable-kyobo", action="store_true")
    parser.add_argument("--enable-jams", action="store_true")
    parser.add_argument("--enable-sciencedirect", action="store_true")
    parser.add_argument("--analysis-limit", type=int, default=None, help="Analyze at most N papers this run.")
    parser.add_argument("--analysis-force", action="store_true", help="Re-analyze papers that already have a record.")
    parser.add_argument("--analysis-model", default=None, help="Override CLAUDE_ANALYSIS_MODEL.")
    parser.add_argument(
        "--analysis-backend",
        choices=("auto", "api", "claude-code"),
        default=None,
        help=(
            "How papers are analyzed: api (Anthropic API key), claude-code (headless Claude Code CLI "
            "under the operator's subscription), or auto (API key if set, else the CLI). "
            "Overrides CLAUDE_ANALYSIS_BACKEND."
        ),
    )
    parser.add_argument(
        "--special-interactive-login",
        action="store_true",
        help="Open the headed KCI browser for a manual login before Special-Issue collection.",
    )
    parser.add_argument("--skip-web-build", action="store_true", help="Build graphs but skip the npm web build.")
    parser.add_argument(
        "--browser-providers",
        default="auto",
        help=(
            "Authenticated browser PDF providers: 'auto' (every provider with a saved "
            "profile), 'none', or a comma list of dbpia,kyobo,jams."
        ),
    )
    parser.add_argument(
        "--browser-login",
        choices=LOGIN_MODES,
        default="auto",
        help=(
            "never: saved profiles only; auto: open the login window when a profile is "
            "missing or the session expired (interactive terminal required); "
            "always: log in before crawling."
        ),
    )
    interactivity = parser.add_mutually_exclusive_group()
    interactivity.add_argument("--interactive", action="store_true", help="Assume a human can log in (force).")
    interactivity.add_argument("--non-interactive", action="store_true", help="Never open a login window (cron).")
    return parser


def options_from_arguments(args: argparse.Namespace) -> PipelineOptions:
    requested = expand_stages([stage for stage in args.stages.split(",") if stage.strip()])
    skipped = set(
        expand_stages(
            [
                name
                for name in (*STAGES, *(alias for alias in STAGE_ALIASES if alias != "default"))
                if getattr(args, f"skip_{name}", False)
            ]
        )
    )
    stages = tuple(stage for stage in requested if stage not in skipped)
    if not stages:
        raise ValueError("no stages left to run")
    raw_browser = (args.browser_providers or "auto").strip().casefold()
    if raw_browser == "auto":
        browser_providers: tuple[str, ...] | None = None
    elif raw_browser == "none":
        browser_providers = ()
    else:
        browser_providers = tuple(part.strip() for part in raw_browser.split(",") if part.strip())
        unknown_browser = [name for name in browser_providers if name not in BROWSER_PROVIDERS]
        if unknown_browser:
            raise ValueError(f"unknown browser providers: {unknown_browser}")
    interactive: bool | None = True if args.interactive else False if args.non_interactive else None
    return PipelineOptions(
        stages=stages,
        dry_run=args.dry_run,
        stop_on_error=args.stop_on_error,
        collect_pdfs=not args.skip_pdf,
        dry_run_pdf=args.dry_run_pdf,
        skip_export=args.skip_export,
        enable_dbpia=args.enable_dbpia,
        enable_kyobo=args.enable_kyobo,
        enable_jams=args.enable_jams,
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.skip_pdf and any((args.enable_dbpia, args.enable_kyobo, args.enable_jams, args.enable_sciencedirect)):
        parser.error("PDF provider flags cannot be used with --skip-pdf")
    try:
        options = options_from_arguments(args)
        run_date = date.fromisoformat(args.run_date) if args.run_date else None
    except ValueError as exc:
        parser.error(str(exc))
    settings = load_settings(validate="metadata" in options.stages and not options.dry_run)
    result = run_pipeline(options, settings=settings, run_date=run_date)
    print(
        "pipeline: "
        + " ".join(f"{report.name}={report.status}" for report in result.stages)
        + f" ledger={result.ledger_path}"
    )
    return 1 if result.failed else 0


__all__ = [
    "DEFAULT_RUNNERS",
    "DEFAULT_STAGES",
    "STAGE_ALIASES",
    "expand_stages",
    "PipelineOptions",
    "PipelineRunResult",
    "STAGES",
    "StageReport",
    "build_argument_parser",
    "main",
    "options_from_arguments",
    "plan",
    "run_pipeline",
    "target_months_from_database",
    "write_ledger",
]


if __name__ == "__main__":
    raise SystemExit(main())
