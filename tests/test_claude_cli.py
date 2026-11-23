from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from Analyzer.claude_cli import ClaudeCli, ClaudeCliError, CostLedger, RetryingCaller, UsageLimitError
from Analyzer import cost_report

FAKE = """#!{python}
import json, sys
args = sys.argv[1:]
log = {log!r}
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"args": args, "stdin": sys.stdin.read()}}) + "\\n")
model = args[args.index("--model") + 1]
system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
mode = {mode!r}
usage = {{"input_tokens": 2, "output_tokens": 100, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 2000,
          "cache_creation": {{"ephemeral_1h_input_tokens": 2000, "ephemeral_5m_input_tokens": 0}}}}
models = {{model: {{"inputTokens": 2, "outputTokens": 100, "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 2000, "costUSD": 0.0228}}}}
if mode == "safeguard" and model == "claude-opus-5":
    print(json.dumps({{"is_error": True, "result": "API Error: Opus 5's safeguards flagged this message", "usage": usage, "modelUsage": models, "total_cost_usd": 0.0228}}))
    sys.exit(1)
if mode == "limit":
    print(json.dumps({{"is_error": True, "result": "You've hit your limit"}}))
    sys.exit(1)
if mode == "session":
    print(json.dumps({{"is_error": True, "result": "You've hit your session limit · resets 6:30pm (Asia/Seoul)"}}))
    sys.exit(1)
print(json.dumps({{"is_error": False, "structured_output": {{"system": system, "model": model}}, "usage": usage, "modelUsage": models,
                  "total_cost_usd": 0.0228, "num_turns": 2, "duration_ms": 1500, "duration_api_ms": 1400}}))
"""


def _fake(tmp_path: Path, mode: str = "ok") -> tuple[Path, Path]:
    log = tmp_path / "calls.jsonl"
    executable = tmp_path / "claude"
    executable.write_text(FAKE.format(python=sys.executable, log=str(log), mode=mode), encoding="utf-8")
    executable.chmod(0o755)
    return executable, log


def test_a_call_passes_the_model_as_is_and_the_system_prompt_through_a_file(tmp_path: Path) -> None:
    executable, log = _fake(tmp_path)
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    cli = ClaudeCli("claude-opus-5", 60, str(executable), ledger=ledger)
    big = "규칙 " * 40_000                                              # larger than one Linux argument may be
    structured, model, usage = cli.request(system_prompt=big, prompt="논문", schema={"type": "object"},
                                           ledger_tags={"stage": "lv_mapping", "task": "lv_case1", "source_id": "ART1"})
    assert structured == {"system": big, "model": "claude-opus-5"} and model == "claude-opus-5"
    call = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert call["stdin"] == "논문" and "--system-prompt" not in call["args"] and call["args"][call["args"].index("--model") + 1] == "claude-opus-5"
    assert usage["total_cost_usd"] == 0.0228 and usage["cache_creation_1h_tokens"] == 2000 and usage["num_turns"] == 2
    entry = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert (entry["stage"], entry["task"], entry["source_id"], entry["status"], entry["total_cost_usd"]) == ("lv_mapping", "lv_case1", "ART1", "ok", 0.0228)


def test_failures_are_ledgered_and_a_usage_limit_is_its_own_error(tmp_path: Path) -> None:
    executable, _log = _fake(tmp_path, "limit")
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    with pytest.raises(UsageLimitError):
        ClaudeCli("claude-opus-5", 60, str(executable), ledger=ledger).request(system_prompt="s", prompt="p", schema={})
    assert json.loads(ledger.path.read_text(encoding="utf-8"))["status"] == "usage_limit"
    # The session limit of the subscription is a usage limit too: the caller waits, the paper is not failed.
    executable, _log = _fake(tmp_path, "session")
    with pytest.raises(UsageLimitError):
        ClaudeCli("claude-opus-5", 60, str(executable), ledger=None).request(system_prompt="s", prompt="p", schema={})


def test_a_safety_refusal_goes_to_the_fallback_model_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    executable, log = _fake(tmp_path, "safeguard")
    ledger = CostLedger(tmp_path / "ledger.jsonl")
    caller = RetryingCaller(ClaudeCli("claude-opus-5", 60, str(executable), ledger=ledger), retries=3)
    outcome = caller.call(system_prompt="s", prompt="p", schema={}, validate=lambda value: value, tags={"task": "extract"})
    assert outcome["fallback_for"] == "claude-opus-5" and outcome["result"]["model"] == "claude-sonnet-5"
    models = [json.loads(line)["args"][json.loads(line)["args"].index("--model") + 1] for line in log.read_text(encoding="utf-8").splitlines()]
    assert models == ["claude-opus-5", "claude-sonnet-5"]                 # no paid retries of a refusal
    statuses = [json.loads(line)["status"] for line in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert statuses == ["cli_error", "ok"]

    def invalid(_value):
        raise ClaudeCliError("schema mismatch")

    (tmp_path / "x").mkdir()
    plain, _ = _fake(tmp_path / "x", "ok")
    with pytest.raises(ClaudeCliError, match="failed after 2 attempts"):
        RetryingCaller(ClaudeCli("claude-opus-5", 60, str(plain), ledger=None), retries=2).call(
            system_prompt="s", prompt="p", schema={}, validate=invalid)


def test_cost_tables_split_reading_and_writing_and_add_up(tmp_path: Path, monkeypatch) -> None:
    def entry(task: str, source_id: str, status: str = "ok", cost: float = 0.0228, model: str = "claude-opus-5") -> dict:
        return {"ts": "2026-10-04T21:00:00", "task": task, "source_id": source_id, "status": status, "requested_model": model,
                "total_cost_usd": cost, "cache_creation_input_tokens": 2000, "cache_creation_1h_tokens": 2000, "duration_ms": 1000,
                "model_usage": {model: {"inputTokens": 2, "outputTokens": 100, "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 2000, "costUSD": cost}}}

    totals = cost_report.Totals()
    totals.add(entry("lv_case1", "ART1"))
    # 2*5 + 1000*0.5 + 2000*10 = 20,510 / 1e6 reading; 100*25 = 2,500 / 1e6 writing = the CLI's 0.02301 (rounded in the fake)
    assert round(totals.input_side, 6) == 0.02051 and round(totals.output_side, 6) == 0.0025

    monkeypatch.setattr(cost_report, "SUMMARY_CACHE", tmp_path / "none")
    entries = [entry("lv_case1", "ART1"), entry("lv_case1", "ART2", "cli_error"), entry("extract", "ART1"), entry("l5_map", "ART1")]
    written = cost_report.write_reports(entries, tmp_path)
    assert {path.name for path in written.values()} == {"lv_mapping.md", "md_wrting.md", "pdf_understand.md", "total.md"}
    total = (tmp_path / "total.md").read_text(encoding="utf-8")
    assert "| **전체 합계** |  | 1 | 4 | 1 |" in total and "$0.09" in total   # a failed call counts as a call, not as a paper done
    assert "안전 필터" in (tmp_path / "lv_mapping.md").read_text(encoding="utf-8") or "기타 오류" in (tmp_path / "lv_mapping.md").read_text(encoding="utf-8")


def test_judge_calls_have_their_own_section_and_an_unpriced_model_is_split_by_the_usual_ratios(tmp_path: Path, monkeypatch) -> None:
    def entry(task: str, model: str, cost: float) -> dict:
        return {"ts": "2026-10-05T21:00:00", "task": task, "source_id": "ART1", "status": "ok", "requested_model": model,
                "total_cost_usd": cost, "cache_creation_input_tokens": 0, "duration_ms": 1000,
                "model_usage": {model: {"inputTokens": 1000, "outputTokens": 200, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": cost}}}

    totals = cost_report.Totals()
    totals.add(entry("lv_judge_B", "claude-fable-9", 0.2))       # no list price: 1000 reading vs 200*5 writing -> half and half
    assert round(totals.input_side, 6) == 0.1 and round(totals.output_side, 6) == 0.1

    monkeypatch.setattr(cost_report, "SUMMARY_CACHE", tmp_path / "none")
    cost_report.write_reports([entry("lv_case1", "claude-opus-5", 0.3), entry("lv_judge_A", "claude-opus-5", 0.5),
                               entry("lv_judge_B", "claude-fable-9", 0.2)], tmp_path)
    mapping = (tmp_path / "lv_mapping.md").read_text(encoding="utf-8")
    assert "## 3-1. Level 비교 심사(compare-lv)만" in mapping and "+ 비교 심사 $0.70" in mapping
    assert "기타(분류되지 않은 호출)" not in (tmp_path / "total.md").read_text(encoding="utf-8")

