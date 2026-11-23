#!/usr/bin/env python3
"""Token use and API-equivalent cost of the LLM stages -> ``Cost/*.md`` tables.

Every ``claude -p`` call of the LLM stages appends one line to ``Cost/ledger.jsonl``
(``Analyzer/claude_cli.py``): the CLI's own token counts per model and its
``total_cost_usd``, which is an estimate at API list prices.  The runs are made on
the Claude Max subscription, so this is the API-equivalent value of the work, not
an invoice.

    Cost/lv_mapping.md      Level 1·2·3 classification (PDF/Abstract/Summary-Based) and Level-5 mapping
    Cost/md_wrting.md       MD writing: tags and target-application extraction (v5.2), rendering
    Cost/pdf_understand.md  understanding the original PDF: summaries, OCR, and the reading side of every call
    Cost/total.md           all of the above in one table

    python -m Analyzer.cost_report
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COST_DIR = REPOSITORY_ROOT / "Cost"
LEDGER = COST_DIR / "ledger.jsonl"
EXTRACT_CACHE = REPOSITORY_ROOT / "Analyzer" / "Application" / "cache" / "extract"
MAP_CACHE = REPOSITORY_ROOT / "Analyzer" / "Application" / "cache" / "level5"
SUMMARY_CACHE = REPOSITORY_ROOT / "Analyzer" / "LV" / "cache" / "case_3_summary"
LEVELS_JSON = REPOSITORY_ROOT / "Analyzer" / "LV" / "levels.json"

# USD per million tokens (API list prices; checked against the CLI's costUSD per call).
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.50},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write_5m": 2.50, "cache_write_1h": 4.0, "cache_read": 0.20},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.10},
}
TASK_LABELS = {
    "lv_case1": ("Level 1·2·3 분류 — PDF-Based", "원문 PDF 본문 전체를 읽고 분류"),
    "lv_case2": ("Level 1·2·3 분류 — Abstract-Based", "국문·영문 초록만 읽고 분류"),
    "lv_case3": ("Level 1·2·3 분류 — Summary-Based", "원문 요약만 읽고 분류"),
    "lv_case1_min1": ("Level 재분석(선택 규칙) — PDF-Based", "확정 Level이 없을 때 최소 1개(선택 옵션)"),
    "lv_case2_min1": ("Level 재분석(선택 규칙) — Abstract-Based", "확정 Level이 없을 때 최소 1개(선택 옵션)"),
    "lv_case3_min1": ("Level 재분석(선택 규칙) — Summary-Based", "확정 Level이 없을 때 최소 1개(선택 옵션)"),
    "l5_resolve": ("Level-5 등록 확인(이름 판정)", "정확한 규칙으로 못 찾은 이름이 등록 제품과 같은지 판정"),
    "l5_map": ("Level-5 매핑 — PDF-Based", "원문 PDF 본문(원문이 없으면 초록)과 후보 노드를 읽고 Level-5 식별자 선택 — MD에 쓰는 결과"),
    "l5_map_abstract": ("Level-5 매핑 — Abstract-Based", "초록만 읽고 Level-5 식별자 선택(입력별 비교용)"),
    "l5_map_summary": ("Level-5 매핑 — Summary-Based", "원문 요약만 읽고 Level-5 식별자 선택(입력별 비교용)"),
    "l5_reason": ("공란 사유 한 줄 요약", "등록됐지만 맞는 노드가 없는 애플리케이션의 사유를 짧게 정리(공란 목록)"),
    "lv_judge_A": ("Level 비교 심사 — 심사자 A", "세 파일의 Level-3가 다른 논문에서 본문 전체를 읽고 후보마다 맞음/애매/틀림 판정(compare-lv)"),
    "lv_judge_B": ("Level 비교 심사 — 심사자 B", "다른 모델로 같은 판정을 독립적으로 한 번 더(compare-lv)"),
    "lv_screen_A": ("미분류 논문 포렌식 판정 — 심사자 A", "1차 매칭에서 세 입력 모두 Level이 없는 논문이 포렌식 논문인지 판정(Mapping-Try)"),
    "lv_screen_B": ("미분류 논문 포렌식 판정 — 심사자 B", "다른 모델로 같은 판정을 독립적으로 한 번 더(Mapping-Try)"),
    "lv_case1_retry": ("Level 2차 매칭 — PDF-Based", "1차 판단을 보여 주고 같은 규칙으로 원문 본문을 다시 분류(Mapping-Try)"),
    "lv_case2_retry": ("Level 2차 매칭 — Abstract-Based", "1차 판단을 보여 주고 같은 규칙으로 초록을 다시 분류(Mapping-Try)"),
    "lv_case3_retry": ("Level 2차 매칭 — Summary-Based", "1차 판단을 보여 주고 같은 규칙으로 요약을 다시 분류(Mapping-Try)"),
    "lv_case1_force": ("Level 2차 매칭 배정 — PDF-Based", "재검토에서 관련·부분 관련인데 후보가 없는 입력에 가장 가까운 Level-3 선택(Mapping-Try)"),
    "lv_case2_force": ("Level 2차 매칭 배정 — Abstract-Based", "재검토에서 관련·부분 관련인데 후보가 없는 입력에 가장 가까운 Level-3 선택(Mapping-Try)"),
    "lv_case3_force": ("Level 2차 매칭 배정 — Summary-Based", "재검토에서 관련·부분 관련인데 후보가 없는 입력에 가장 가까운 Level-3 선택(Mapping-Try)"),
    "extract": ("태그·대상 애플리케이션 추출 (Step 1)", "원문(없으면 초록)과 v5.2 추출 어휘로 MD의 tags·4장 내용 추출"),
    "lv_summary": ("원문 요약 생성 (Summary-Based 입력)", "원문 전체를 읽고 구조화 요약 작성"),
    "ocr": ("이미지 PDF 전사(OCR)", "글자 층이 없는 PDF를 페이지 이미지로 읽어 전사"),
}
LEVEL_TASKS = ("lv_case1", "lv_case2", "lv_case3", "lv_case1_min1", "lv_case2_min1", "lv_case3_min1")
LEVEL5_TASKS = ("l5_resolve", "l5_map", "l5_map_abstract", "l5_map_summary", "l5_reason")
JUDGE_TASKS = ("lv_judge_A", "lv_judge_B")
RETRY_TASKS = ("lv_screen_A", "lv_screen_B", "lv_case1_retry", "lv_case2_retry", "lv_case3_retry",
               "lv_case1_force", "lv_case2_force", "lv_case3_force")
CATEGORIES = {
    "lv_mapping": LEVEL_TASKS + LEVEL5_TASKS + JUDGE_TASKS + RETRY_TASKS,
    "md_writing": ("extract",),
    "pdf_understand": ("lv_summary", "ocr"),
}
# Calls whose input contains the original PDF text (for the "reading the PDF" view).
PDF_READING_TASKS = ("lv_case1", "lv_case1_min1", "extract", "l5_map", "lv_summary", "ocr", "lv_judge_A", "lv_judge_B", "lv_case1_retry",
                     "lv_case1_force")


def _price(model: str) -> dict[str, float] | None:
    for name, price in PRICES.items():
        if model == name or model.startswith(name):
            return price
    return None


@dataclass(slots=True)
class Totals:
    calls: int = 0
    failed_calls: int = 0
    papers: set[str] = field(default_factory=set)
    input_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    failed_cost: float = 0.0
    input_side: float = 0.0     # reading: input + cache write + cache read
    output_side: float = 0.0    # writing: output tokens
    duration_ms: int = 0

    def add(self, entry: Mapping[str, Any]) -> None:
        self.calls += 1
        ok = entry.get("status") == "ok"
        cost = float(entry.get("total_cost_usd") or 0.0)
        self.cost += cost
        if not ok:
            self.failed_calls += 1
            self.failed_cost += cost
        elif entry.get("source_id"):
            for source_id in str(entry["source_id"]).split(","):
                self.papers.add(source_id)
        self.duration_ms += int(entry.get("duration_ms") or 0)
        usage = entry.get("model_usage") or {}
        creation_1h = int(entry.get("cache_creation_1h_tokens") or 0)
        creation_total = int(entry.get("cache_creation_input_tokens") or 0)
        share_1h = creation_1h / creation_total if creation_total else 1.0
        for model, counts in usage.items():
            price = _price(model)
            tokens_in = int(counts.get("inputTokens") or 0)
            tokens_out = int(counts.get("outputTokens") or 0)
            read = int(counts.get("cacheReadInputTokens") or 0)
            write = int(counts.get("cacheCreationInputTokens") or 0)
            self.input_tokens += tokens_in
            self.output_tokens += tokens_out
            self.cache_read += read
            self.cache_write += write
            if price is None:
                # No list price here (e.g. a newer model): split the CLI's own cost for this model with the
                # usual ratios (output 5x input, cache write 1.25x/2x, cache read 0.1x).
                cost_usd = float(counts.get("costUSD") or 0.0)
                reading = tokens_in + read * 0.1 + write * (share_1h * 2.0 + (1 - share_1h) * 1.25)
                weight = reading + tokens_out * 5.0
                if cost_usd and weight:
                    self.input_side += cost_usd * reading / weight
                    self.output_side += cost_usd * tokens_out * 5.0 / weight
                continue
            write_price = share_1h * price["cache_write_1h"] + (1 - share_1h) * price["cache_write_5m"]
            self.input_side += (tokens_in * price["input"] + read * price["cache_read"] + write * write_price) / 1e6
            self.output_side += tokens_out * price["output"] / 1e6

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cache_write + self.cache_read + self.output_tokens


def read_ledger(path: Path = LEDGER) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def _basis(task: str, source_id: str) -> str:
    """PDF-Based / Abstract-Based context of an extraction or mapping call."""

    for directory in (EXTRACT_CACHE, MAP_CACHE):
        path = directory / f"{source_id}.json"
        if path.is_file():
            try:
                basis = json.loads(path.read_text(encoding="utf-8")).get("basis")
            except ValueError:
                continue
            if basis:
                return f"{basis}-Based"
    return ""


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _int(value: int) -> str:
    return f"{value:,}"


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]], align: Sequence[str] | None = None) -> str:
    align = align or ["---"] + ["---:"] * (len(header) - 1)
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _usage_row(label: str, note: str, totals: Totals) -> list[str]:
    papers = len(totals.papers)
    return [
        label, note, _int(papers), _int(totals.calls), _int(totals.failed_calls),
        _int(totals.input_tokens), _int(totals.cache_write), _int(totals.cache_read), _int(totals.output_tokens),
        _int(totals.total_tokens), _money(totals.cost), _money(totals.cost / papers) if papers else "-",
    ]


USAGE_HEADER = (
    "작업", "내용", "논문 수", "호출 수", "그중 실패 호출", "입력 토큰(캐시 미적용)", "캐시 쓰기 토큰", "캐시 읽기 토큰",
    "출력 토큰", "토큰 합계", "비용(USD)", "논문당 평균",
)
USAGE_ALIGN = ["---", "---"] + ["---:"] * 10


def _header(title: str, entries: Sequence[Mapping[str, Any]], scope: str) -> str:
    stamps = sorted(entry.get("ts", "") for entry in entries if entry.get("ts"))
    period = f"{stamps[0].replace('T', ' ')} ~ {stamps[-1].replace('T', ' ')}" if stamps else "-"
    price_rows = [
        [model, f"${price['input']:.2f}", f"${price['output']:.2f}", f"${price['cache_write_5m']:.2f}", f"${price['cache_write_1h']:.2f}", f"${price['cache_read']:.2f}"]
        for model, price in PRICES.items()
    ]
    return "\n".join([
        f"# {title}",
        "",
        f"- 범위: {scope}",
        "- 기준: Claude Code CLI(`claude -p`)가 호출마다 보고한 토큰 수와 `total_cost_usd`(API 정가로 환산한 추정 금액). "
        "이번 작업은 Claude Max 구독으로 실행했으므로 **실제로 청구된 금액은 아니며**, 같은 일을 API로 했을 때의 비용입니다(구독 사용 한도에서 차감).",
        "- 모델: `claude-opus-5`. Opus 5의 안전 필터가 막은 논문(보안·공격 기법을 자세히 다룬 논문)은 같은 프롬프트로 `claude-sonnet-5`가 대신 처리했고, "
        "CLI가 내부적으로 쓰는 작은 보조 호출(`claude-haiku-4-5`)도 비용에 포함됩니다.",
        "- 실패한 호출(안전 필터 거절, 시간 초과 등)도 토큰을 쓰므로 합계에 넣고 따로 표시합니다.",
        "- 토큰 열: '입력(캐시 미적용)'은 거의 전부 CLI 보조 모델(haiku)의 입력입니다. 논문 본문·분류체계 같은 큰 입력은 프롬프트 캐시로 들어가 "
        "'캐시 쓰기'(처음 읽을 때)·'캐시 읽기'(같은 앞부분을 다시 읽을 때)로 집계됩니다.",
        f"- 기간: {period} · 원장: `Cost/ledger.jsonl` · 생성: {datetime.now():%Y-%m-%d %H:%M} (`python -m Analyzer.cost_report`)",
        "",
        "단가(100만 토큰당 USD, 호출별 CLI 금액으로 검산함):",
        "",
        _table(("모델", "입력", "출력", "캐시 쓰기(5분)", "캐시 쓰기(1시간)", "캐시 읽기"), price_rows),
        "",
    ])


def _by_task(entries: Sequence[Mapping[str, Any]]) -> dict[str, Totals]:
    totals: dict[str, Totals] = defaultdict(Totals)
    for entry in entries:
        totals[str(entry.get("task") or "unattributed")].add(entry)
    return totals


def _by_model(entries: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    rows: dict[str, Counter] = defaultdict(Counter)
    for entry in entries:
        for model, counts in (entry.get("model_usage") or {}).items():
            row = rows[model]
            row["calls"] += 1
            row["input"] += int(counts.get("inputTokens") or 0)
            row["write"] += int(counts.get("cacheCreationInputTokens") or 0)
            row["read"] += int(counts.get("cacheReadInputTokens") or 0)
            row["output"] += int(counts.get("outputTokens") or 0)
            row["cost"] += float(counts.get("costUSD") or 0.0)
    return [
        [model, _int(row["calls"]), _int(row["input"]), _int(row["write"]), _int(row["read"]), _int(row["output"]), _money(row["cost"])]
        for model, row in sorted(rows.items(), key=lambda item: -item[1]["cost"])
    ]


def _split_rows(totals: Mapping[str, Totals], tasks: Sequence[str]) -> list[list[str]]:
    rows = []
    for task in tasks:
        item = totals.get(task)
        if not item or not item.calls:
            continue
        share = item.input_side / (item.input_side + item.output_side) if (item.input_side + item.output_side) else 0
        rows.append([TASK_LABELS.get(task, (task, ""))[0], _money(item.input_side), _money(item.output_side), f"{share:.0%}", _money(item.cost)])
    return rows


def _historical_summaries(before: str = "") -> dict[str, Any]:
    """Tokens of the case-3 summaries made before the ledger started (``before`` = its first timestamp).

    Summaries made by a ledgered run are already in the ledger and are not counted here again.
    """

    current: set[str] = set()
    try:
        current = set(json.loads(LEVELS_JSON.read_text(encoding="utf-8")).get("papers", {}))
    except (OSError, ValueError):
        pass
    tokens = Counter()
    count = 0
    created: list[str] = []
    for path in SUMMARY_CACHE.glob("*.json"):
        if current and path.stem not in current:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if record.get("status") != "ok":
            continue
        if before and str(record.get("created_at", "")) >= before:
            continue
        usage = record.get("usage") or {}
        count += 1
        created.append(str(record.get("created_at", "")))
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            tokens[key] += int(usage.get(key) or 0)
    price = PRICES["claude-opus-5"]
    base = (tokens["input_tokens"] * price["input"] + tokens["output_tokens"] * price["output"] + tokens["cache_read_input_tokens"] * price["cache_read"]) / 1e6
    return {
        "count": count, "tokens": tokens,
        "cost_5m": base + tokens["cache_creation_input_tokens"] * price["cache_write_5m"] / 1e6,
        "cost_1h": base + tokens["cache_creation_input_tokens"] * price["cache_write_1h"] / 1e6,
        "created": (min(created)[:10], max(created)[:10]) if created else ("", ""),
    }


def _text_cache_counts() -> tuple[int, int, str]:
    """Current papers with a text file, how many of them came from OCR, and the files' date range."""

    text_dir = REPOSITORY_ROOT / "Analyzer" / "LV" / "cache" / "text"
    try:
        current = set(json.loads(LEVELS_JSON.read_text(encoding="utf-8")).get("papers", {}))
    except (OSError, ValueError):
        current = set()
    files = [path for path in text_dir.glob("*.txt") if not current or path.stem in current]
    ocr = sum(1 for path in files if path.with_suffix(".ocr").is_file())
    stamps = sorted(datetime.fromtimestamp(path.stat().st_mtime).date().isoformat() for path in files)
    return len(files), ocr, (f"{stamps[0]} ~ {stamps[-1]}" if stamps and stamps[0] != stamps[-1] else (stamps[0] if stamps else "-"))


def write_reports(entries: Sequence[Mapping[str, Any]] | None = None, directory: Path = COST_DIR) -> dict[str, Path]:
    entries = list(entries if entries is not None else read_ledger())
    totals = _by_task(entries)
    written: dict[str, Path] = {}
    first_ts = min((str(entry.get("ts")) for entry in entries if entry.get("ts")), default="")
    summary_calls = totals["lv_summary"].calls if "lv_summary" in totals else 0

    def usage_table(tasks: Sequence[str], total_label: str) -> tuple[str, Totals]:
        rows, combined = [], Totals()
        for task in tasks:
            item = totals.get(task)
            if not item or not item.calls:
                continue
            label, note = TASK_LABELS.get(task, (task, ""))
            rows.append(_usage_row(label, note, item))
        for entry in entries:
            if entry.get("task") in tasks:
                combined.add(entry)
        rows.append(_usage_row(f"**{total_label}**", "", combined))
        return _table(USAGE_HEADER, rows, USAGE_ALIGN), combined

    def failures(tasks: Sequence[str]) -> str:
        reasons: Counter = Counter()
        costs: Counter = Counter()
        for entry in entries:
            if entry.get("task") in tasks and entry.get("status") != "ok":
                error = str(entry.get("error") or entry.get("status"))
                reason = "안전 필터(safeguards) 거절 → 대체 모델로 처리" if "safeguard" in error else (
                    "사용량 제한(잠시 대기 후 재시도)" if entry.get("status") == "usage_limit" else f"기타 오류: {error[:60]}")
                reasons[(TASK_LABELS.get(entry['task'], (entry['task'], ''))[0], reason)] += 1
                costs[(TASK_LABELS.get(entry['task'], (entry['task'], ''))[0], reason)] += float(entry.get("total_cost_usd") or 0)
        if not reasons:
            return "실패한 호출이 없습니다."
        return _table(("작업", "사유", "호출 수", "쓴 비용(USD)"),
                      [[task, reason, _int(count), _money(costs[(task, reason)])] for (task, reason), count in reasons.most_common()],
                      ["---", "---", "---:", "---:"])

    def fallback_papers(tasks: Sequence[str]) -> int:
        papers = set()
        for entry in entries:
            if entry.get("task") in tasks and entry.get("status") == "ok" and str(entry.get("requested_model", "")).startswith("claude-sonnet"):
                papers.add((entry.get("task"), entry.get("source_id")))
        return len(papers)

    # ---- Level mapping -------------------------------------------------------
    tasks = CATEGORIES["lv_mapping"]
    category_entries = [entry for entry in entries if entry.get("task") in tasks]
    table, combined = usage_table(tasks, "Level 매핑 합계")
    level_only, _ = usage_table(LEVEL_TASKS, "Level 1·2·3 소계")
    level5_only, _ = usage_table(LEVEL5_TASKS, "Level-5 소계")
    judge_only, _ = usage_table(JUDGE_TASKS, "비교 심사 소계")
    judged = any(task in totals and totals[task].calls for task in JUDGE_TASKS)
    retry_only, _ = usage_table(RETRY_TASKS, "2차 매칭 소계")
    retried = any(task in totals and totals[task].calls for task in RETRY_TASKS)
    text = [
        _header("Level 매핑 비용", category_entries, "Level 1·2·3 분류(v5.2 분류체계, PDF-Based·Abstract-Based·Summary-Based 세 가지)와 대상 애플리케이션의 Level-5 매핑(등록 확인 + 매핑)"),
        "## 1. 작업별 합계", "", table, "",
        f"- 합계 {_money(combined.cost)} = Level 1·2·3 {_money(sum(totals[t].cost for t in LEVEL_TASKS if t in totals))} + Level-5 {_money(sum(totals[t].cost for t in LEVEL5_TASKS if t in totals))}"
        + (f" + 비교 심사 {_money(sum(totals[t].cost for t in JUDGE_TASKS if t in totals))}" if judged else "")
        + (f" + 2차 매칭 {_money(sum(totals[t].cost for t in RETRY_TASKS if t in totals))}" if retried else ""),
        ("- Summary-Based 분류의 입력인 '원문 요약'은 원장 기간에 새로 만들지 않고 그 전에 만든 요약을 재사용했습니다(`pdf_understand.md` 2절)."
         if not summary_calls else
         f"- Summary-Based 분류의 입력인 '원문 요약' 생성 호출 {summary_calls}건은 `pdf_understand.md` 1절에 있습니다(나머지는 그 전에 만든 요약 재사용)."),
        f"- 안전 필터 때문에 `claude-sonnet-5`가 대신 처리한 경우: {fallback_papers(tasks)}건(작업·논문 기준).",
        "", "## 2. Level 1·2·3 분류만", "", level_only, "",
        "## 3. Level-5 매핑만", "", level5_only, "",
        "- 등록 확인은 정확한 규칙(제목·별칭·한글 이름)으로 먼저 하고, 그것으로 못 찾은 이름만 LLM이 판정했습니다. 매핑은 등록된 애플리케이션이 있는 논문에서만 호출했습니다.",
        "- Level-5 매핑은 입력을 바꿔 세 번 했습니다: PDF-Based(MD에 쓰는 결과), Abstract-Based, Summary-Based(입력별 비교용, `application_list.xlsx`). "
        "Abstract·Summary-Based는 원문 PDF가 있는 논문만 따로 호출했고, 원문이 없는 논문은 초록으로 한 번만 매핑했습니다(PDF-Based 행에 포함).",
        *(["", "## 3-1. Level 비교 심사(compare-lv)만", "", judge_only, "",
           "- 세 파일(PDF·Abstract·Summary-Based)의 Level-3가 서로 다른 논문마다 심사자 2명이 본문 전체를 읽고 후보마다 맞음/애매/틀림을 판정했습니다"
           "(`(YYMMDD)-compare-lv.xlsx`). 분류 결과(Level) 자체는 바꾸지 않는 검수용 비용입니다."] if judged else []),
        *(["", "## 3-2. 1차 매핑 실패 논문의 2차 매칭(Mapping-Try)만", "", retry_only, "",
           "- 1차 매칭에서 세 입력 모두 Level이 없는 논문만 대상입니다: 입력별 재검토(관련·부분 관련·비관련 판정), 관련·부분 관련인데 후보가 없는 입력의 "
           "가장 가까운 Level-3 선택(`Analyzer/LV/Mapping-Try`). '포렌식 판정' 행은 이전 방식(2026-10-05 오후, 폐기)의 비용입니다. "
           "1차 결과·`levels.json`·MD는 바꾸지 않습니다."] if retried else []),
        "", "## 4. 읽기(입력) 대 쓰기(출력) 비용", "",
        "입력 측 = 논문·분류체계·후보 목록을 읽는 비용(입력+캐시 쓰기+캐시 읽기), 출력 측 = 판단과 근거를 쓰는 비용(출력 토큰, 추론 포함).", "",
        _table(("작업", "입력 측(USD)", "출력 측(USD)", "입력 비중", "합계(USD)"), _split_rows(totals, tasks), ["---", "---:", "---:", "---:", "---:"]),
        "", "## 5. 모델별", "",
        _table(("모델", "호출 수", "입력", "캐시 쓰기", "캐시 읽기", "출력", "비용(USD)"), _by_model(category_entries)),
        "", "## 6. 실패한 호출", "", failures(tasks), "",
    ]
    written["lv_mapping"] = directory / "lv_mapping.md"
    written["lv_mapping"].write_text("\n".join(text), encoding="utf-8")

    # ---- MD writing ----------------------------------------------------------
    tasks = CATEGORIES["md_writing"]
    category_entries = [entry for entry in entries if entry.get("task") in tasks]
    table, combined = usage_table(tasks, "MD 작성 합계")
    by_basis: dict[str, Totals] = defaultdict(Totals)
    for entry in category_entries:
        by_basis[_basis("extract", str(entry.get("source_id") or "")) or "(미확인)"].add(entry)
    basis_rows = [_usage_row(f"태그·대상 애플리케이션 추출 — {basis}", "", item) for basis, item in sorted(by_basis.items())]
    text = [
        _header("MD 파일 작성 비용", category_entries, "논문별 MD(ko.md·en.md)를 v5.2로 다시 쓰는 데 든 LLM 비용 — 태그(5개 축)와 대상 애플리케이션 추출(Step 1). Level 1·2·3과 Level-5 ID는 `lv_mapping.md`."),
        "## 1. 작업별 합계", "", table, "",
        "- MD 파일 자체를 채우는 일(`Analyzer/MD/paper_md.py`: 서지정보·초록·키워드·참고문헌·Level·태그·대상 애플리케이션을 템플릿에 넣기)은 LLM을 쓰지 않아 비용이 0입니다.",
        (f"- 원문 PDF가 없는 {len(by_basis['Abstract-Based'].papers)}편은 초록으로 추출했고(Abstract-Based), 이 논문들은 MD가 만들어지지 않지만 "
         "`application_list.xlsx`에는 들어갑니다." if "Abstract-Based" in by_basis else "- 초록으로만 추출한(Abstract-Based) 논문은 없습니다."),
        f"- 안전 필터 때문에 `claude-sonnet-5`가 대신 처리한 논문: {fallback_papers(tasks)}편.",
        "", "## 2. 입력 근거별", "", _table(USAGE_HEADER, basis_rows, USAGE_ALIGN), "",
        "## 3. 읽기(입력) 대 쓰기(출력) 비용", "",
        _table(("작업", "입력 측(USD)", "출력 측(USD)", "입력 비중", "합계(USD)"), _split_rows(totals, tasks), ["---", "---:", "---:", "---:", "---:"]),
        "", "## 4. 모델별", "",
        _table(("모델", "호출 수", "입력", "캐시 쓰기", "캐시 읽기", "출력", "비용(USD)"), _by_model(category_entries)),
        "", "## 5. 실패한 호출", "", failures(tasks), "",
    ]
    written["md_writing"] = directory / "md_wrting.md"
    written["md_writing"].write_text("\n".join(text), encoding="utf-8")

    # ---- PDF understanding ----------------------------------------------------
    tasks = CATEGORIES["pdf_understand"]
    category_entries = [entry for entry in entries if entry.get("task") in tasks]
    history = _historical_summaries(first_ts)
    text_count, ocr_count, text_dates = _text_cache_counts()
    reading: dict[str, Totals] = defaultdict(Totals)
    for entry in entries:
        task = str(entry.get("task") or "")
        if task in PDF_READING_TASKS:
            basis = _basis(task, str(entry.get("source_id") or "")) if task in ("extract", "l5_map") else "PDF-Based"
            if basis == "PDF-Based":
                reading[task].add(entry)
    reading_rows = []
    reading_total = 0.0
    for task in PDF_READING_TASKS:
        item = reading.get(task)
        if not item or not item.calls:
            continue
        reading_total += item.input_side
        reading_rows.append([TASK_LABELS.get(task, (task, ""))[0], _int(len(item.papers)), _int(item.calls),
                             _int(item.input_tokens + item.cache_write + item.cache_read), _money(item.input_side)])
    reading_rows.append(["**합계**", "", "", "", _money(reading_total)])
    this_run = (
        _table(USAGE_HEADER, [_usage_row(*TASK_LABELS[task], totals[task]) for task in tasks if task in totals and totals[task].calls], USAGE_ALIGN)
        if any(task in totals for task in tasks) else "이번 작업에서는 원문 이해 전용 호출이 없었습니다(아래 2의 기존 요약과 OCR 전사본을 재사용)."
    )
    tokens = history["tokens"]
    text = [
        _header("원문 PDF 이해 비용", category_entries or entries, "원문 PDF를 읽고 이해하는 데 든 비용 — 원문 요약 생성·OCR(전용 호출)과, 원문 본문을 입력으로 읽은 모든 호출의 읽기 비용"),
        "## 1. 이번 작업의 원문 이해 전용 호출", "", this_run, "",
        "## 2. 재사용한 기존 산출물 (이번 비용 0)", "",
        _table(("산출물", "논문 수", "만든 날", "입력 토큰", "캐시 쓰기", "캐시 읽기", "출력 토큰", "당시 추정 비용(USD)"), [[
            "원문 요약(Summary-Based 분류의 입력, `Analyzer/LV/cache/case_3_summary`)", _int(history["count"]),
            " ~ ".join(sorted(set(history["created"]))), _int(tokens["input_tokens"]), _int(tokens["cache_creation_input_tokens"]),
            _int(tokens["cache_read_input_tokens"]), _int(tokens["output_tokens"]),
            f"{_money(history['cost_5m'])} ~ {_money(history['cost_1h'])}",
        ], ["원문 텍스트·OCR 전사본(`Analyzer/LV/cache/text`)", _int(text_count), text_dates, "-", "-", "-", "-",
            f"텍스트 추출은 LLM 미사용, OCR {ocr_count}편은 원장 이전이면 기록 없음"]],
            ["---", "---:", "---", "---:", "---:", "---:", "---:", "---:"]),
        "",
        "- 당시 요약 기록에는 금액이 저장되지 않아 토큰 수에 Opus 5 단가를 곱해 추정했습니다(캐시 쓰기 보존 시간 5분~1시간에 따라 범위로 표시).",
        "", "## 3. 원문 본문을 읽은 비용 (다른 표와 겹치는 교차 보기)", "",
        "원문 PDF 본문을 입력으로 받은 호출에서 '읽기' 쪽(입력+캐시 쓰기+캐시 읽기 토큰)만 모은 금액입니다. "
        "**이 금액은 `lv_mapping.md`·`md_wrting.md`의 합계에 이미 들어 있으므로 더하면 안 됩니다.** 프롬프트(분류체계·어휘·후보 목록)를 읽은 토큰도 포함됩니다.", "",
        _table(("작업", "논문 수", "호출 수", "읽은 토큰", "읽기 비용(USD)"), reading_rows, ["---", "---:", "---:", "---:", "---:"]),
        "",
    ]
    written["pdf_understand"] = directory / "pdf_understand.md"
    written["pdf_understand"].write_text("\n".join(text), encoding="utf-8")

    # ---- Total -----------------------------------------------------------------
    rows = []
    grand = Totals()
    for key, label in (("lv_mapping", "Level 매핑 (lv_mapping.md)"), ("md_writing", "MD 파일 작성 (md_wrting.md)"), ("pdf_understand", "원문 PDF 이해 전용 (pdf_understand.md)")):
        item = Totals()
        for entry in entries:
            if entry.get("task") in CATEGORIES[key]:
                item.add(entry)
                grand.add(entry)
        rows.append(_usage_row(label, "", item))
    other = Totals()
    for entry in entries:
        if not any(entry.get("task") in tasks for tasks in CATEGORIES.values()):
            other.add(entry)
            grand.add(entry)
    if other.calls:
        rows.append(_usage_row("기타(분류되지 않은 호출)", "", other))
    rows.append(_usage_row("**전체 합계**", "", grand))
    hours = grand.duration_ms / 3_600_000
    text = [
        _header("LLM 토큰 비용 합계", entries, "`Cost/ledger.jsonl`에 기록된 LLM 호출 전체(2026-10-04 v5.2 MD 재작성·Level-5 매핑 작업부터, 시험 실행 포함)"),
        _table(USAGE_HEADER, rows, USAGE_ALIGN), "",
        f"- 호출 시간의 합: {hours:,.1f}시간(동시에 여러 개를 실행했으므로 실제 경과 시간은 이보다 짧음).",
        f"- 재사용한 기존 원문 요약 {history['count']}편의 당시 추정 비용 {_money(history['cost_5m'])} ~ {_money(history['cost_1h'])}은 위 합계에 들어 있지 않습니다(`pdf_understand.md` 2절).",
        "- 세부 내역: `lv_mapping.md`(Level 1·2·3·Level-5), `md_wrting.md`(태그·대상 애플리케이션 추출), `pdf_understand.md`(원문 이해).",
        "",
    ]
    written["total"] = directory / "total.md"
    written["total"].write_text("\n".join(text), encoding="utf-8")
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 토큰 비용 표(Cost/*.md) 생성")
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args(argv)
    written = write_reports(read_ledger(args.ledger))
    for path in written.values():
        print(f"wrote {path.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
