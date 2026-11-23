#!/usr/bin/env python3
"""Mapping-Try: second matching attempt for the papers no Level was confirmed for in any input.

Step 1  First try: the regular Level classification (``lv_experiment``: PDF-, Abstract- and
        Summary-Based, caches ``cache/case_{1,2,3}``); its workbooks are kept in
        ``Analyzer/LV/Mapping-Try/First-Try``.
Step 2  Mapping failure list: the papers whose ``all_lv3`` is empty in all three First-Try
        workbooks -> ``Mapping-Try/(YYMMDD)-Mapping-Failed-List.xlsx``.
Step 3  Second matching attempt (re-review): each input of a listed paper is classified again under
        the first try's rules (taxonomy v5.2, the three criteria and their bands), with the first
        try's judgement for that input shown for reference (``cache/case_N_retry``).  Its relevance
        verdicts decide the paper: 비관련 when every input it has says 비관련, else 관련 (an input
        says 관련) or 부분 관련.
Step 4  비관련 -> ``Mapping-Try/Remove-List/(YYMMDD)-Remove-List.xlsx``, a list for
        ``Collector/paper_removal.py`` (nothing is removed here).  관련 / 부분 관련 -> mapped in every
        input the paper has, by the user's decision of 2026-10-05 even below the 0.80 confirmation
        bar, one Level-3 per input: a Level the re-review confirmed (0.80+ with a verbatim quote),
        else the re-review's top candidate, else the closest Level-3 of a forced pass
        (``lv_experiment``'s minimum-one prompt, ``cache/case_N_force``).  How each Level was
        assigned, with its confidence, is in the failure list.
Step 5  ``Mapping-Try/Second-Try``: the First-Try files for every paper but the 비관련 ones (the listed
        papers with their new Levels, every other paper as in the first try) and compare-lv, whose
        two judges also rate every newly assigned Level from the full text.

The first try, ``levels.json`` and the profiles do not change.

    python -m Analyzer.LV.lv_retry --date 261005
    python -m Analyzer.LV.lv_retry --date 261005 --export-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from Analyzer.Application.application_list import keep_text  # noqa: E402
from Analyzer.claude_cli import ClaudeCli, RetryingCaller, UsageLimitError  # noqa: E402
from Analyzer.LV import lv_compare  # noqa: E402
from Analyzer.LV import lv_experiment as lv  # noqa: E402

MAPPING_TRY_DIR = lv.LV_DIR / "Mapping-Try"
FIRST_TRY_DIR = MAPPING_TRY_DIR / "First-Try"
SECOND_TRY_DIR = MAPPING_TRY_DIR / "Second-Try"
REMOVE_LIST_DIR = MAPPING_TRY_DIR / "Remove-List"
RETRY_PROMPT_VERSION = "lv-retry-2"
FORCE_PROMPT_VERSION = "lv-force-1"
CASES = (1, 2, 3)
CASE_NAMES = {1: "PDF", 2: "Abstract", 3: "Summary"}
RELATED, PARTLY, UNRELATED = "관련", "부분 관련", "비관련"
METHOD_CONFIRMED = "확정(0.80 이상)"
METHOD_TOP = "재검토 최상위 후보"
METHOD_FORCED = "가장 가까운 Level-3 배정"
NOT_APPLIED = "미반영 — 목록만 작성(말뭉치·MD·DF-Insight에는 그대로 있음)"


class RetryError(lv.ExperimentError):
    pass


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- #
# Step 2: the papers no Level was confirmed for
# --------------------------------------------------------------------------- #

def _normal_title(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def first_try_workbook(case: int, directory: Path) -> Path:
    found = sorted(directory.glob(f"(*)-LV-{lv.CASE_FILE_LABELS[case]}.xlsx"))
    if not found:
        raise RetryError(f"no First-Try workbook '(YYMMDD)-LV-{lv.CASE_FILE_LABELS[case]}.xlsx' in {directory}")
    return found[-1]


def read_first_try(directory: Path) -> dict[int, list[dict[str, Any]]]:
    """case -> rows of the First-Try workbook (sheet '논문별 결과')."""

    from openpyxl import load_workbook

    rows: dict[int, list[dict[str, Any]]] = {}
    for case in CASES:
        path = first_try_workbook(case, directory)
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            data = list(workbook["논문별 결과"].iter_rows(values_only=True))
        finally:
            workbook.close()
        header = tuple(str(value or "") for value in data[0][: len(lv.RESULT_COLUMNS)]) if data else ()
        if header != lv.RESULT_COLUMNS:
            raise RetryError(f"{path}: unexpected columns {header}")
        rows[case] = [dict(zip(lv.RESULT_COLUMNS, row)) for row in data[1:] if row and row[1]]
    return rows


def _ids(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _excluded_titles(excluded: frozenset[str]) -> list[dict[str, Any]]:
    if not excluded:
        return []
    from Collector.paper_removal import load_excluded_papers

    return [entry for source_id, entry in load_excluded_papers().items() if source_id in excluded]


def unmatched_papers(
    papers: Sequence[lv.Paper], first_try: Mapping[int, Sequence[Mapping[str, Any]]], excluded: frozenset[str] = frozenset(),
) -> list[lv.Paper]:
    """Papers whose ``all_lv3`` is empty in all three First-Try workbooks, in paper order.

    A row is the paper with that record id if the titles agree, else the one paper with that
    title (record ids move when papers are removed and renumbered); a row of a paper removed
    since (``excluded``) is skipped.  Every row must still be what the first-try cache gives:
    the second try reads that cache, and the Second-Try files copy it for the other papers.
    """

    by_record = {paper.record_id: paper for paper in papers}
    by_title: dict[str, list[lv.Paper]] = {}
    for paper in papers:
        by_title.setdefault(_normal_title(paper.display_title), []).append(paper)
    rows: dict[str, dict[int, Mapping[str, Any]]] = {}
    for case in CASES:
        for row in first_try[case]:
            paper = by_record.get(str(row["record_id"]))
            if paper is None or _normal_title(paper.display_title) != _normal_title(row["title"]):
                matches = by_title.get(_normal_title(row["title"]), [])
                paper = matches[0] if len(matches) == 1 else None
            if paper is None:
                if any(_normal_title(entry.get("title")) == _normal_title(row["title"]) for entry in _excluded_titles(excluded)):
                    continue
                raise RetryError(f"{row['record_id']} '{row['title']}': no single paper of the corpus matches this First-Try row")
            rows.setdefault(paper.source_id, {})[case] = row
    papers_by_id = {paper.source_id: paper for paper in papers}
    targets: list[lv.Paper] = []
    for source_id, by_case in rows.items():
        paper = papers_by_id[source_id]
        if set(by_case) != set(CASES):
            raise RetryError(f"{paper.record_id}: not in all three First-Try workbooks")
        for case in CASES:
            record = lv.load_case_record(case, source_id)
            if record is None or record.get("status") not in ("ok", "input_unavailable"):
                raise RetryError(f"{paper.record_id} case {case}: no current first-try record (pending, failed or another taxonomy)")
            if _ids(by_case[case]["all_lv3"]) != [item["path_id"] for item in lv.final_levels(case, paper)[0]]:
                raise RetryError(f"{paper.record_id} case {case}: the First-Try workbook and the first-try cache differ (re-run since?)")
        if not any(_ids(by_case[case]["all_lv3"]) for case in CASES):
            targets.append(paper)
    return sorted(targets, key=lambda paper: (paper.paper_index is None, paper.paper_index or 0, paper.source_id))


def summary_text(paper: lv.Paper) -> str | None:
    """The first try's structured summary of the PDF text (``cache/case_3_summary``)."""

    record = _read_json(lv.CACHE_DIR / "case_3_summary" / f"{paper.source_id}.json") or {}
    if record.get("status") != "ok" or record.get("prompt_version") != lv.SUMMARY_PROMPT_VERSION:
        return None
    return str(record.get("summary_text") or "").strip() or None


def first_records(paper: lv.Paper) -> dict[int, dict[str, Any] | None]:
    return {case: lv.load_case_record(case, paper.source_id) for case in CASES}


def case_input(paper: lv.Paper, case: int) -> tuple[str, str, str] | None:
    """(source text, prompt body, input kind) — exactly the first try's input for the case."""

    if case == 2:
        body = lv.abstract_block(paper)
        return (body, body, "abstract") if body else None
    if paper.pdf_path is None:
        return None
    if case == 1:
        text = lv.full_text(paper)
        return (text, f"[논문 본문]\n{text}", "full_text") if text else None
    summary = summary_text(paper)
    return (summary, f"[논문 본문 요약]\n{summary}", "summary") if summary else None


# --------------------------------------------------------------------------- #
# Step 3: re-review
# --------------------------------------------------------------------------- #

RETRY_NOTE = """
재검토 안내
- 사용자 메시지 끝의 '[같은 입력에 대한 이전 판단(참고)]'은 같은 입력을 읽은 이전 분류의 판단입니다. 입력을 처음부터 다시 읽고 독립적으로 판단합니다. 이전 판단을 따를 의무도, 뒤집을 의무도 없습니다.
- 분류체계의 모든 Level-2·Level-3을 훑어, 논문이 직접 수행·제안·검토한 활동에 맞는 Level-3이 이전 판단에서 빠졌는지 확인합니다.
- 이전 판단과 다른 기준값(기여 직접성·정의 부합·근거 명시성)을 주거나 이전 판단에 없던 Level-3을 배정하려면, 이전 판단이 놓쳤거나 잘못 읽은 입력 구절을 evidence_quote로 글자 그대로 인용하고 rationale_ko를 '이전 판단과 다른 점: '으로 시작해 무엇을 놓쳤거나 잘못 읽었는지 씁니다. 같은 구절·같은 사실을 두고 정의 해석만 달리하는 것은 기준값을 바꾸는 근거가 아닙니다.
- evidence_quote는 논문 입력에서만 인용합니다(이전 판단의 문장은 인용하지 않습니다). 기준과 조합표는 위와 같고, 그런 근거가 없으면 이전 판단과 같은 결론(빈 배열 포함)이 맞는 답입니다.
"""


def first_try_block(result: Mapping[str, Any], techniques: Mapping[str, lv.Technique]) -> str:
    """The earlier judgement for one input, as the model reads it: criteria and reasons, no numbers and no verdict."""

    def name(path_id: str) -> str:
        technique = techniques.get(path_id)
        return f"{path_id} {technique.level_3}" if technique else path_id

    lines = [f"포렌식 관련성: {result.get('forensic_relevance', '')} — {result.get('relevance_reason_ko', '')}"]
    mappings = result.get("mappings") or []
    if mappings:
        lines.append("이전 판단의 후보와 세 기준(기여 직접성 / 정의 부합 / 근거 명시성):")
        lines += [
            f"- {name(item['path_id'])} ({item.get('directness', '')} / {item.get('definition_fit', '')} / {item.get('evidence_explicitness', '')}): {item.get('rationale_ko', '')}"
            for item in mappings
        ]
    rejected = result.get("rejected_candidates") or []
    if rejected:
        lines.append("검토했으나 배정하지 않은 기술:")
        lines += [f"- {name(item['path_id'])}: {item.get('reason_ko', '')}" for item in rejected]
    if result.get("no_mapping_reason_ko"):
        lines.append(f"배정하지 않은 이유: {result['no_mapping_reason_ko']}")
    return "\n".join(lines)


class Retrier:
    def __init__(self, techniques: Mapping[str, lv.Technique], taxonomy_sha256: str, *, model: str, timeout: float, retries: int) -> None:
        self.techniques = techniques
        self.taxonomy_sha256 = taxonomy_sha256
        self.model = model
        self.caller = RetryingCaller(ClaudeCli(model, timeout), retries=retries)
        self.schema = lv.classification_schema(techniques)
        self.system_prompts = {case: lv.classification_system_prompt(techniques, case) + RETRY_NOTE for case in CASES}

    def prompt(self, paper: lv.Paper, case: int) -> tuple[str, str, str, str] | None:
        """(source text, prompt, input kind, first-try input hash); None when the first try had no input either.

        A first-try record that is missing or not current is an error, not 'no input'.
        """

        first = lv.load_case_record(case, paper.source_id)
        if first is None or first.get("status") not in ("ok", "input_unavailable"):
            raise RetryError(f"{paper.source_id} case {case}: no current first-try record")
        if first["status"] == "input_unavailable":
            return None
        prepared = case_input(paper, case)
        if prepared is None:
            raise RetryError(f"{paper.source_id} case {case}: the first try's input is not available any more")
        source_text, body, kind = prepared
        prompt = f"{lv.title_block(paper)}\n\n{body}\n\n[같은 입력에 대한 이전 판단(참고)]\n{first_try_block(first['result'], self.techniques)}"
        return source_text, prompt, kind, str(first.get("input_sha256") or "")

    def classify(self, paper: lv.Paper, case: int) -> dict[str, Any]:
        path = lv.CACHE_DIR / f"case_{case}_retry" / f"{paper.source_id}.json"
        prepared = self.prompt(paper, case)
        if prepared is None:
            existing = _read_json(path)
            if existing and existing.get("status") == "ok":
                raise RetryError(f"{paper.source_id} case {case}: has a second-try answer but no input now")
            record = {"schema_version": 1, "prompt_version": RETRY_PROMPT_VERSION, "status": "input_unavailable", "case": case,
                      "source_id": paper.source_id, "paper_index": paper.paper_index,
                      "reason": "이 입력이 없습니다(원문 PDF·요약 없음) — 1차에서도 분류하지 않음"}
            lv._write_json(path, record)
            return record
        source_text, prompt, kind, first_sha = prepared
        system_prompt = self.system_prompts[case]
        input_sha256 = lv._sha(system_prompt + "\x1f" + prompt)
        cached = lv._read_cache(path, input_sha256=input_sha256, model=self.model, prompt_version=RETRY_PROMPT_VERSION)
        if cached and cached.get("taxonomy_sha256") == self.taxonomy_sha256:
            return cached
        outcome = self.caller.call(
            system_prompt=system_prompt, prompt=prompt, schema=self.schema,
            validate=lambda structured: lv.normalize_classification(structured, self.techniques, source_text),
            tags={"stage": "lv_mapping", "task": f"lv_case{case}_retry", "case": case, "source_id": paper.source_id},
        )
        record = {
            "schema_version": 1, "prompt_version": RETRY_PROMPT_VERSION, "status": "ok", "case": case,
            "source_id": paper.source_id, "paper_index": paper.paper_index,
            "requested_model": self.model, "model": outcome["model"], "fallback_for": outcome["fallback_for"],
            "taxonomy_sha256": self.taxonomy_sha256, "input_kind": kind, "input_chars": len(source_text),
            "input_sha256": input_sha256, "first_try_input_sha256": first_sha,
            "result": outcome["result"], "usage": outcome["usage"], "attempts": outcome["attempts"],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        lv._write_json(path, record)
        return record


def _load_versioned(directory: str, case: int, source_id: str, version: str) -> dict[str, Any] | None:
    record = _read_json(lv.CACHE_DIR / f"case_{case}_{directory}" / f"{source_id}.json")
    if not record or record.get("prompt_version") != version:
        return None
    if record.get("status") == "ok" and record.get("taxonomy_sha256") != lv.current_taxonomy_sha256():
        return None
    return record


def load_retry_record(case: int, source_id: str) -> dict[str, Any] | None:
    """The re-review's record for the case, if it belongs to the current prompt and taxonomy."""

    return _load_versioned("retry", case, source_id, RETRY_PROMPT_VERSION)


def confirmed(record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Re-review Levels that pass the first try's bar: 0.80 or more, with a quote found verbatim in the input."""

    if not record or record.get("status") != "ok":
        return []
    return [item for item in record["result"]["mappings"] if item["confidence"] >= lv.CONFIRMED_CONFIDENCE and item.get("quote_verified")]


# --------------------------------------------------------------------------- #
# Step 4: the closest Level-3 for a related paper the re-review left blank in an input
# --------------------------------------------------------------------------- #

FORCE_NOTE = """
이번 배정의 전제
- 이 논문은 1차 분류와 재검토에서 확정된 Level-3이 없었지만, 재검토에서 디지털 포렌식 관련(또는 부분 관련) 논문으로 판정되었습니다. 이런 논문에는 확신도가 낮더라도 가장 가까운 Level-3을 배정하기로 했습니다(사용자 결정).
- 사용자 메시지 끝의 '[이전 분류의 판단]'은 같은 입력에 대한 재검토 결과입니다. 그 판단이 이 입력을 비관련으로 보았더라도, 이 입력에 드러난 대상·활동에 가장 가까운 Level-3을 고릅니다.
"""


class Forcer:
    """The closest Level-3 of one input (lv_experiment's minimum-one prompt), for a related paper left blank."""

    def __init__(self, techniques: Mapping[str, lv.Technique], taxonomy_sha256: str, *, model: str, timeout: float, retries: int) -> None:
        self.techniques = techniques
        self.taxonomy_sha256 = taxonomy_sha256
        self.model = model
        self.caller = RetryingCaller(ClaudeCli(model, timeout), retries=retries)
        self.schema = lv.minimum_one_schema(techniques)
        self.system_prompts = {case: lv.minimum_one_system_prompt(techniques, case) + FORCE_NOTE for case in CASES}

    def prompt(self, paper: lv.Paper, case: int, retry_record: Mapping[str, Any]) -> tuple[str, str, str]:
        prepared = case_input(paper, case)
        if prepared is None or retry_record.get("status") != "ok":
            raise RetryError(f"{paper.source_id} case {case}: no input or no re-review to start from")
        source_text, body, kind = prepared
        result = retry_record["result"]

        def name(path_id: str) -> str:
            technique = self.techniques.get(path_id)
            return f"{path_id} {technique.level_3}" if technique else path_id

        considered = [f"- {name(item['path_id'])}: {item.get('rationale_ko', '')}" for item in result.get("mappings") or []]
        considered += [f"- {name(item['path_id'])}: {item.get('reason_ko', '')}" for item in result.get("rejected_candidates") or []]
        block = (f"포렌식 관련성: {result.get('forensic_relevance', '')} — {result.get('relevance_reason_ko', '')}\n"
                 f"배정하지 않은 이유: {result.get('no_mapping_reason_ko') or '기재 없음'}\n검토했던 후보:\n" + ("\n".join(considered) or "- 없음"))
        return source_text, f"{lv.title_block(paper)}\n\n{body}\n\n[이전 분류의 판단]\n{block}", kind

    def classify(self, paper: lv.Paper, case: int, retry_record: Mapping[str, Any]) -> dict[str, Any]:
        path = lv.CACHE_DIR / f"case_{case}_force" / f"{paper.source_id}.json"
        source_text, prompt, kind = self.prompt(paper, case, retry_record)
        system_prompt = self.system_prompts[case]
        input_sha256 = lv._sha(system_prompt + "\x1f" + prompt)
        cached = lv._read_cache(path, input_sha256=input_sha256, model=self.model, prompt_version=FORCE_PROMPT_VERSION)
        if cached and cached.get("taxonomy_sha256") == self.taxonomy_sha256:
            return cached
        outcome = self.caller.call(
            system_prompt=system_prompt, prompt=prompt, schema=self.schema,
            validate=lambda structured: lv.normalize_minimum_one(structured, self.techniques, source_text),
            tags={"stage": "lv_mapping", "task": f"lv_case{case}_force", "case": case, "source_id": paper.source_id},
        )
        record = {
            "schema_version": 1, "prompt_version": FORCE_PROMPT_VERSION, "status": "ok", "case": case,
            "source_id": paper.source_id, "paper_index": paper.paper_index,
            "requested_model": self.model, "model": outcome["model"], "fallback_for": outcome["fallback_for"],
            "taxonomy_sha256": self.taxonomy_sha256, "input_kind": kind, "input_chars": len(source_text), "input_sha256": input_sha256,
            "retry_input_sha256": retry_record.get("input_sha256"), "result": outcome["result"], "usage": outcome["usage"],
            "attempts": outcome["attempts"], "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        lv._write_json(path, record)
        return record


def load_force_record(case: int, source_id: str, retry_record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The forced pick for the case, if it was made from today's re-review answer."""

    record = _load_versioned("force", case, source_id, FORCE_PROMPT_VERSION)
    if record and retry_record and record.get("retry_input_sha256") != retry_record.get("input_sha256"):
        return None
    return record


@dataclass(slots=True)
class Outcome:
    """One listed paper after the re-review (and the forced picks where needed)."""

    paper: lv.Paper
    retry: dict[int, dict[str, Any] | None]
    forced: dict[int, dict[str, Any] | None] = field(default_factory=dict)
    judgements: dict[str, dict[str, Any]] = field(default_factory=dict)   # path_id -> compare-lv consensus

    def available(self, case: int) -> bool:
        record = self.retry.get(case)
        return bool(record) and record.get("status") == "ok"

    @property
    def reviewed(self) -> bool:
        return all(self.retry.get(case) is not None and self.retry[case].get("status") in ("ok", "input_unavailable") for case in CASES)

    @property
    def verdicts(self) -> dict[int, str]:
        return {case: self.retry[case]["result"]["forensic_relevance"] for case in CASES if self.available(case)}

    @property
    def relevance(self) -> str:
        """비관련 only when every input the paper has says 비관련; 관련 when one says 관련."""

        if not self.reviewed:
            return "미완료"
        verdicts = list(self.verdicts.values())
        if not verdicts or all(verdict == UNRELATED for verdict in verdicts):
            return UNRELATED
        return RELATED if RELATED in verdicts else PARTLY

    def needs_force(self, case: int) -> bool:
        return (self.relevance in (RELATED, PARTLY) and self.available(case)
                and not confirmed(self.retry[case]) and not self.retry[case]["result"]["mappings"])

    def assigned(self, case: int) -> tuple[list[dict[str, Any]], str]:
        """(the Levels of this input, how they were assigned) — empty for 비관련 or a missing input."""

        if self.relevance not in (RELATED, PARTLY) or not self.available(case):
            return [], ""
        found = confirmed(self.retry[case])
        if found:
            return found, METHOD_CONFIRMED
        mappings = self.retry[case]["result"]["mappings"]
        if mappings:
            return mappings[:1], METHOD_TOP
        forced = self.forced.get(case)
        if forced and forced.get("status") == "ok":
            return forced["result"]["mappings"][:1], METHOD_FORCED
        return [], "미완료"

    @property
    def complete(self) -> bool:
        return self.reviewed and all(self.assigned(case)[1] != "미완료" for case in CASES)


# --------------------------------------------------------------------------- #
# Workbooks
# --------------------------------------------------------------------------- #

def _sheet(workbook, title: str, header: Sequence[str], rows: Sequence[Sequence[Any]], widths: Mapping[str, int], *, first: bool = False):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    sheet = workbook.active if first else workbook.create_sheet()
    sheet.title = title
    sheet.append(list(header))
    for row in rows:
        sheet.append([lv._cell(value) for value in row])
    for cell in sheet[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="305496")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    keep_text(sheet)
    for position, name in enumerate(header, 1):
        sheet.column_dimensions[get_column_letter(position)].width = widths.get(name, 14)
    sheet.freeze_panes = "D2"
    sheet.auto_filter.ref = sheet.dimensions
    return sheet


def _summary_sheet(workbook, rows: Sequence[Sequence[Any]]) -> None:
    from openpyxl.styles import Alignment, Font

    sheet = workbook.create_sheet("요약")
    for row in rows:
        sheet.append([lv._cell(value) for value in row])
        if len(row) == 1:
            sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    keep_text(sheet)
    sheet.column_dimensions["A"].width = 30
    sheet.column_dimensions["B"].width = 110


def _save(workbook, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.tmp.xlsx")
    workbook.save(temporary)
    temporary.replace(target)
    return target


def _identity(paper: lv.Paper) -> list[Any]:
    return [paper.paper_index if paper.paper_index is not None else "", paper.record_id, paper.display_title, paper.journal,
            paper.publication_year, paper.source_id]


IDENTITY = ("paper_index", "record_id", "title", "journal", "publication_year", "source_id")
WIDTHS = {"record_id": 20, "title": 50, "journal": 22, "source_id": 15, "1차 최근접 후보": 40, "1차 분류기의 설명": 60, "제거 사유": 50,
          "PDF 배정": 44, "Abstract 배정": 44, "Summary 배정": 44, "본문 심사(배정 Level)": 40, "처리": 22, "반영 상태": 30,
          "재검토 관련성(PDF / Abstract / Summary)": 30, "1차 관련성(PDF / Abstract / Summary)": 30}


def _relevance_cells(records: Mapping[int, Mapping[str, Any] | None]) -> str:
    return " / ".join(
        (record["result"]["forensic_relevance"] if record and record.get("status") == "ok" else "입력 없음") for record in
        (records.get(case) for case in CASES)
    )


def _closest(records: Mapping[int, Mapping[str, Any] | None], techniques: Mapping[str, lv.Technique]) -> tuple[str, str]:
    """('ID 이름 · 입력 확신도' of the best unconfirmed candidate over the inputs, its explanation)."""

    best: tuple[float, str, str] | None = None
    for case in CASES:
        record = records.get(case)
        top, explanation = lv_compare.closest_candidate(record["result"] if record and record.get("status") == "ok" else None)
        if top and (best is None or float(top.get("confidence") or 0) > best[0]):
            technique = techniques.get(top["path_id"])
            label = f"{top['path_id']} {technique.level_3 if technique else ''} · {CASE_NAMES[case]} {float(top.get('confidence') or 0):.2f}"
            best = (float(top.get("confidence") or 0), label, explanation)
    return (best[1], best[2]) if best else ("—", "")


def _assigned_cell(outcome: Outcome, case: int, techniques: Mapping[str, lv.Technique]) -> str:
    if not outcome.available(case):
        return "입력 없음"
    if outcome.relevance == UNRELATED:
        return "비관련(제거 목록)"
    levels, method = outcome.assigned(case)
    if not levels:
        return method or "미완료"
    item = levels[0]
    technique = techniques.get(item["path_id"])
    return (f"{item['path_id']} {technique.level_3 if technique else ''} · {method} · 확신도 {float(item['confidence']):.2f} "
            f"({item.get('directness', '')}/{item.get('definition_fit', '')}/{item.get('evidence_explicitness', '')})")


def _judged(outcome: Outcome) -> str:
    if outcome.relevance not in (RELATED, PARTLY):
        return ""
    if not outcome.judgements:
        return "본문 심사 없음(원문 없음 또는 미완료)"
    return "\n".join(f"{path_id}: {item['verdict']} (A {item['verdict_A']} / B {item['verdict_B']})" for path_id, item in outcome.judgements.items())


def export_failure_list(outcomes: Sequence[Outcome], techniques: Mapping[str, lv.Technique], target: Path) -> Path:
    """Step 2's list (the papers no Level was confirmed for) and what Steps 3-4 did with each."""

    from openpyxl import Workbook

    listed = (*IDENTITY, "PDF all_lv3", "Abstract all_lv3", "Summary all_lv3", "1차 관련성(PDF / Abstract / Summary)",
              "1차 최근접 후보", "1차 분류기의 설명")
    results = (*IDENTITY, "재검토 관련성(PDF / Abstract / Summary)", "논문 판정", "처리", "PDF 배정", "Abstract 배정", "Summary 배정",
               "본문 심사(배정 Level)")
    list_rows, result_rows = [], []
    for outcome in outcomes:
        first = first_records(outcome.paper)
        closest, explanation = _closest(first, techniques)
        list_rows.append([*_identity(outcome.paper), "", "", "", _relevance_cells(first), closest, explanation])
        relevance = outcome.relevance
        action = {UNRELATED: "제거 목록", RELATED: "Level 배정", PARTLY: "Level 배정"}.get(relevance, "미완료")
        result_rows.append([*_identity(outcome.paper), _relevance_cells(outcome.retry), relevance, action,
                            *(_assigned_cell(outcome, case, techniques) for case in CASES), _judged(outcome)])
    workbook = Workbook()
    _sheet(workbook, "매핑 실패 목록", listed, list_rows, WIDTHS, first=True)
    _sheet(workbook, "2차 매핑 결과", results, result_rows, WIDTHS)
    counts = Counter(outcome.relevance for outcome in outcomes)
    methods = Counter(outcome.assigned(case)[1] for outcome in outcomes for case in CASES if outcome.assigned(case)[0])
    verdicts = Counter(item["verdict"] for outcome in outcomes for item in outcome.judgements.values())
    _summary_sheet(workbook, [
        ("Level 2차 매핑(Mapping-Try)",),
        ("Step 1", "PDF·Abstract·Summary-Based로 1차 Level 매핑(First-Try 폴더)."),
        ("Step 2 매핑 실패 목록", f"세 파일 모두 Level이 비어 있는 논문 {len(outcomes)}편 — 시트 '매핑 실패 목록'."),
        ("Step 3 재검토", "입력별로 1차와 같은 규칙(분류체계 v5.2, 세 기준과 조합표)으로 다시 분류. 같은 입력에 대한 1차 판단을 참고로 보여 주되, 기준값은 "
                        "1차가 놓쳤거나 잘못 읽은 입력 구절을 인용할 때만 바꿈. 논문 판정: 입력이 모두 '비관련'이면 비관련, 하나라도 '관련'이면 관련, 그 밖은 부분 관련."),
        ("논문 판정 결과", ", ".join(f"{name} {count}편" for name, count in counts.most_common())),
        ("Step 4 처리", f"비관련 → Remove-List. 관련·부분 관련 → 입력마다 Level-3 1개를 배정(사용자 결정, 확신도 0.80 미만 포함): "
                       f"'{METHOD_CONFIRMED}' → 없으면 '{METHOD_TOP}' → 후보도 없으면 '{METHOD_FORCED}'(가장 가까운 Level-3을 고르게 한 별도 호출)."),
        ("배정 방식별 건수(입력 단위)", ", ".join(f"{name} {count}건" for name, count in methods.most_common()) or "없음"),
        ("본문 심사", "배정한 Level마다 compare-lv 심사자 2명(claude-opus-5, claude-fable-5-1)이 본문 전체를 읽고 맞음/애매/틀림으로 다시 판정 — "
                     + (", ".join(f"{name} {count}건" for name, count in verdicts.most_common()) or "없음")),
        ("Step 5", "최종 결과: Second-Try 폴더(1차와 같은 형식의 세 워크북 + compare-lv). levels.json·MD에는 아직 반영하지 않았습니다."),
        ("생성일", date.today().isoformat()),
        ("프롬프트 버전", f"재검토 {RETRY_PROMPT_VERSION}, 가장 가까운 Level-3 배정 {FORCE_PROMPT_VERSION}"),
    ])
    return _save(workbook, target)


def export_remove_list(outcomes: Sequence[Outcome], techniques: Mapping[str, lv.Technique], target: Path) -> Path:
    from openpyxl import Workbook

    header = (*IDENTITY, "제거 단계", "제거 사유", "재검토 관련성(PDF / Abstract / Summary)", "1차 관련성(PDF / Abstract / Summary)",
              "1차 최근접 후보", "반영 상태")
    rows = []
    for outcome in outcomes:
        if outcome.relevance != UNRELATED:
            continue
        reason = next((outcome.retry[case]["result"]["relevance_reason_ko"] for case in CASES if outcome.available(case)), "")
        first = first_records(outcome.paper)
        rows.append([*_identity(outcome.paper), "Level 2차 매핑(재검토) — 비관련", f"디지털 포렌식 비관련 — {reason}",
                     _relevance_cells(outcome.retry), _relevance_cells(first), _closest(first, techniques)[0], NOT_APPLIED])
    workbook = Workbook()
    _sheet(workbook, "논문별 결과", header, rows, WIDTHS, first=True)
    _summary_sheet(workbook, [
        ("Level 2차 매핑에서 비관련으로 판정된 논문",),
        ("대상", f"1차 매핑 실패 논문 가운데 재검토에서 모든 입력이 '비관련'(디지털 포렌식을 실질적으로 다루지 않음)인 논문 {len(rows)}편"),
        ("판정 기준", "재검토는 1차와 같은 관련성 정의: 관련 = 디지털 증거의 식별·보전·수집·조사·분석·보고 또는 그 기반 기술·제도가 중심 / 부분 관련 = 실질적인 일부 쟁점 / "
                     "비관련 = 디지털 포렌식을 실질적으로 다루지 않음(단어만 언급, 다른 분야의 forensic 등)."),
        ("반영", "목록만 작성했습니다. 실제 제거(말뭉치·MD·DF-Insight)는 검토 후 Collector/paper_removal.py로 합니다: 제목 칸을 노란색으로 칠한 뒤 "
                "python3 -m Collector.paper_removal --from-workbook \"이 파일\" --reason \"Level 2차 매핑 재검토: 디지털 포렌식 비관련으로 제외\" (먼저 --dry-run), "
                "끝나면 python3 main.py -renumber."),
        ("생성일", date.today().isoformat()),
    ])
    return _save(workbook, target)


def export_second_try(
    papers: Sequence[lv.Paper], outcomes: Mapping[str, Outcome], techniques: Mapping[str, lv.Technique], stamp: str, directory: Path,
) -> list[Path]:
    """The three First-Try result files again: the listed papers with their new Levels."""

    written = []
    directory.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        def levels_of(paper: lv.Paper, case: int = case) -> list[dict[str, Any]]:
            if paper.source_id in outcomes:
                return outcomes[paper.source_id].assigned(case)[0]
            return lv.final_levels(case, paper)[0]

        target = directory / f"({stamp})-LV-{lv.CASE_FILE_LABELS[case]}.xlsx"
        written.append(lv.export_result(case, papers, techniques, stamp, target=target, levels_of=levels_of))
    return written


def second_try_overrides(outcomes: Mapping[str, Outcome]) -> dict[tuple[int, str], dict[str, Any]]:
    """The re-review records for lv_compare, with the Levels each input was given."""

    overrides: dict[tuple[int, str], dict[str, Any]] = {}
    for source_id, outcome in outcomes.items():
        for case in CASES:
            record = outcome.retry.get(case)
            overrides[(case, source_id)] = {**record, "confirmed": outcome.assigned(case)[0]} if record else {"status": "pending"}
    return overrides


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def _run_parallel(jobs: Sequence[tuple[Any, ...]], function, workers: int, label: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    failures: dict[str, str] = {}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(function, *job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            key = ":".join(str(getattr(part, "source_id", part)) for part in job if not isinstance(part, dict))
            try:
                future.result()
                counts["ok"] += 1
            except UsageLimitError as exc:
                counts["usage_limit"] += 1
                failures[key] = f"UsageLimitError: {exc}"
            except Exception as exc:  # noqa: BLE001 - report per job, keep going
                counts["failed"] += 1
                failures[key] = f"{type(exc).__name__}: {exc}"
            if done % 10 == 0 or done == len(futures):
                print(f"[{datetime.now():%H:%M:%S}] {label} {done}/{len(futures)}건 · 실패 {counts['failed']} · 경과 {(time.monotonic() - started) / 60:.1f}분", flush=True)
    for key, message in failures.items():
        print(f"  실패 {key}: {message[:200]}", flush=True)
    return dict(counts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="1차 Level 매핑 실패 논문의 재검토·2차 매핑 (Mapping-Try)")
    parser.add_argument("--date", default=None, help="파일명 날짜 YYMMDD (기본: 오늘)")
    parser.add_argument("--first-try", type=Path, default=FIRST_TRY_DIR)
    parser.add_argument("--mapping-try", type=Path, default=MAPPING_TRY_DIR, help="매핑 실패 목록을 쓸 폴더")
    parser.add_argument("--second-try", type=Path, default=SECOND_TRY_DIR)
    parser.add_argument("--remove-list", type=Path, default=REMOVE_LIST_DIR)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--model", default=lv.DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--export-only", action="store_true", help="LLM 호출 없이 캐시로 파일만 다시 생성")
    parser.add_argument("--no-judges", action="store_true", help="배정한 Level의 본문 심사 호출을 하지 않음")
    args = parser.parse_args(argv)
    stamp = args.date or date.today().strftime("%y%m%d")
    if not re.fullmatch(r"\d{6}", stamp):
        parser.error("--date는 YYMMDD 형식이어야 합니다")
    first_dir, list_dir, second_dir, remove_dir = (
        path.expanduser().resolve() for path in (args.first_try, args.mapping_try, args.second_try, args.remove_list))

    from Collector.paper_removal import load_excluded_source_ids

    techniques, taxonomy_sha256 = lv.load_techniques()
    papers = lv.load_corpus(lv.DEFAULT_DATABASE, techniques)
    targets = unmatched_papers(papers, read_first_try(first_dir), load_excluded_source_ids())
    print(f"Step 2 매핑 실패(세 파일 모두 Level 없음): {len(targets)}편", flush=True)
    summary: dict[str, Any] = {"targets": len(targets)}
    stopped = False

    if not args.export_only:
        retrier = Retrier(techniques, taxonomy_sha256, model=args.model, timeout=args.timeout, retries=args.retries)
        summary["retry"] = _run_parallel([(paper, case) for paper in targets for case in CASES], retrier.classify, args.workers, "Step 3 재검토")
        stopped = retrier.caller.stop.is_set()
    outcomes = {paper.source_id: Outcome(paper, {case: load_retry_record(case, paper.source_id) for case in CASES}) for paper in targets}

    force_jobs = [(outcome.paper, case, outcome.retry[case]) for outcome in outcomes.values() for case in CASES if outcome.needs_force(case)]
    if not args.export_only and not stopped and force_jobs:
        forcer = Forcer(techniques, taxonomy_sha256, model=args.model, timeout=args.timeout, retries=args.retries)
        summary["forced"] = _run_parallel(force_jobs, forcer.classify, args.workers, "Step 4 가장 가까운 Level-3 배정")
        stopped = forcer.caller.stop.is_set()
    for outcome in outcomes.values():
        outcome.forced = {case: load_force_record(case, outcome.paper.source_id, outcome.retry.get(case)) for case in CASES if outcome.needs_force(case)}

    removed = {source_id for source_id, outcome in outcomes.items() if outcome.relevance == UNRELATED}
    remaining = [paper for paper in papers if paper.source_id not in removed]
    kept = {source_id: outcome for source_id, outcome in outcomes.items() if source_id not in removed}
    comparisons = lv_compare.load_comparisons(remaining, second_try_overrides(kept))
    by_id = {comparison.paper.source_id: comparison for comparison in comparisons}
    mapped = [by_id[source_id] for source_id in kept if by_id[source_id].comparable and by_id[source_id].union]
    if not args.export_only and not args.no_judges and not stopped and mapped:
        # Every newly assigned Level is rated from the full text by the compare-lv judges, even when the three inputs agree.
        summary["judges"] = lv_compare.run_judges(mapped, techniques, workers=args.workers, timeout=args.timeout, include_identical=True,
                                                  failures_path=lv.CACHE_DIR / "judge_failures_second_try.json")
    prompts = {judge: lv_compare.judge_system_prompt(techniques, judge) for judge in lv_compare.JUDGES}
    for comparison in mapped:
        kept[comparison.paper.source_id].judgements = lv_compare.consensus(comparison, lv_compare.expected_hashes(comparison, techniques, prompts))
    unjudged = sum(1 for comparison in comparisons
                   if comparison.comparable and comparison.union and (not comparison.identical or comparison.paper.source_id in kept)
                   and not lv_compare.consensus(comparison, lv_compare.expected_hashes(comparison, techniques, prompts)))

    ordered = list(outcomes.values())
    written = [export_failure_list(ordered, techniques, list_dir / f"({stamp})-Mapping-Failed-List.xlsx"),
               export_remove_list(ordered, techniques, remove_dir / f"({stamp})-Remove-List.xlsx")]
    written += export_second_try(remaining, kept, techniques, stamp, second_dir)
    incomplete = sum(not outcome.complete for outcome in outcomes.values())
    if incomplete:
        print(f"끝나지 않은 재검토·배정 {incomplete}편: Second-Try compare-lv는 쓰지 않았습니다(다시 실행하면 이어서 합니다)", flush=True)
    else:
        written.append(lv_compare.export(comparisons, techniques, stamp, second_dir))

    summary.update({
        "relevance": dict(Counter(outcome.relevance for outcome in outcomes.values())),
        "removed": len(removed), "mapped_papers": len(kept),
        "methods": dict(Counter(outcome.assigned(case)[1] for outcome in kept.values() for case in CASES if outcome.assigned(case)[0])),
        "incomplete": incomplete, "unjudged_comparisons": unjudged, "stopped_on_usage_limit": stopped,
        "written": [os.path.relpath(path, REPOSITORY_ROOT) for path in written],
    })
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 3 if incomplete or unjudged or stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
