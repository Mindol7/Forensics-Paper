"""Headless Claude Code calls (``claude -p``) shared by the LLM stages, with a cost ledger.

Every call runs the installed Claude Code CLI on the Max subscription:

    claude -p --model <full model id> --output-format json --json-schema <schema>
           --system-prompt-file <file> --tools "" --permission-mode bypassPermissions
           --max-turns N --no-session-persistence          (prompt on stdin)

* The model id is passed as is (``claude-opus-5``).  The CLI alias ``opus`` now
  resolves to a newer model, so aliases are never substituted.
* The system prompt goes through a file: Linux limits one argument to 128 KiB and
  the v5.2 prompts are close to that.
* Every attempt (success or failure) appends one JSON line to ``Cost/ledger.jsonl``
  with the CLI's own token counts and ``total_cost_usd`` (the API-equivalent
  estimate the CLI reports; the subscription itself is not billed per token).
  ``Analyzer/cost_report.py`` turns the ledger into the ``Cost/*.md`` tables.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from PaperPipeline.claude_analyzer import _cli_model, _parse_cli_json, find_claude_code_executable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COST_DIR = REPOSITORY_ROOT / "Cost"
LEDGER_PATH = COST_DIR / "ledger.jsonl"
DEFAULT_MODEL = "claude-opus-5"
RUN_ID = f"{datetime.now():%Y%m%dT%H%M%S}-{os.getpid()}"


class ClaudeCliError(RuntimeError):
    pass


class UsageLimitError(ClaudeCliError):
    """The subscription/rate limit was hit; the run should pause, not fail papers."""


class CostLedger:
    """Append-only JSONL ledger, safe across threads and concurrent processes."""

    def __init__(self, path: Path = LEDGER_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, entry: Mapping[str, Any]) -> None:
        line = (json.dumps(dict(entry), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                os.write(descriptor, line)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)


LEDGER = CostLedger()


def usage_summary(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Token counts and cost from one CLI JSON result (absent fields are None)."""

    payload = payload or {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    models = payload.get("modelUsage") if isinstance(payload.get("modelUsage"), dict) else {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_creation_1h_tokens": creation.get("ephemeral_1h_input_tokens"),
        "cache_creation_5m_tokens": creation.get("ephemeral_5m_input_tokens"),
        "duration_ms": payload.get("duration_ms"),
        "duration_api_ms": payload.get("duration_api_ms"),
        "num_turns": payload.get("num_turns"),
        "total_cost_usd": payload.get("total_cost_usd"),
        "model_usage": {
            str(name): {
                key: value.get(key)
                for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "costUSD")
            }
            for name, value in models.items()
            if isinstance(value, dict)
        },
    }


class ClaudeCli:
    """Headless ``claude -p`` with the prompt on stdin and schema-shaped output."""

    # e.g. "Claude usage limit reached", "You've hit your session limit · resets 6:30pm" — wait and retry, never a paper's failure.
    LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "overloaded", "429", "hit your", "session limit", "weekly limit")

    def __init__(
        self, model: str = DEFAULT_MODEL, timeout: float = 900.0, executable: str | None = None, *,
        effort: str | None = None, ledger: CostLedger | None = LEDGER,
    ) -> None:
        resolved = find_claude_code_executable(executable or os.getenv("CLAUDE_CODE_EXECUTABLE"))
        if not resolved:
            raise ClaudeCliError("Claude Code 실행 파일을 찾지 못했습니다 (CLAUDE_CODE_EXECUTABLE)")
        self.executable = resolved
        self.model = model
        self.timeout = timeout
        self.effort = effort
        self.ledger = ledger
        # A neutral working directory keeps project memory/CLAUDE.md out of the calls.
        self.workdir = Path(tempfile.mkdtemp(prefix="claude-cli-"))
        self._prompt_lock = threading.Lock()

    def _system_prompt_file(self, system_prompt: str) -> Path:
        digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:24]
        target = self.workdir / f"system-{digest}.txt"
        with self._prompt_lock:
            if not target.is_file():
                temporary = target.with_suffix(".tmp")
                temporary.write_text(system_prompt, encoding="utf-8")
                temporary.replace(target)
        return target

    def request(
        self, *, system_prompt: str, prompt: str, schema: Mapping[str, Any], tools: str = "",
        ledger_tags: Mapping[str, Any] | None = None, max_turns: int | None = None, validate=None,
    ) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
        """One call.  ``validate`` (optional) checks the structured answer before the ledger
        line is written, so an answer the caller rejects is recorded as ``invalid_output``."""

        command = [
            self.executable, "-p",
            "--model", self.model,
            "--output-format", "json",
            "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            "--system-prompt-file", str(self._system_prompt_file(system_prompt)),
            "--tools", tools,
            "--permission-mode", "bypassPermissions",
            "--max-turns", str(max_turns or (10 if tools else 6)),
            "--no-session-persistence",
        ]
        if self.effort:
            command[4:4] = ["--effort", self.effort]
        started = datetime.now()
        payload: dict[str, Any] | None = None
        status, error = "error", "interrupted or unexpected error"
        try:
            try:
                completed = subprocess.run(
                    command, input=prompt, capture_output=True, text=True, encoding="utf-8",
                    timeout=self.timeout, cwd=str(self.workdir), check=False,
                )
            except subprocess.TimeoutExpired as exc:
                status, error = "timeout", f"timeout after {self.timeout:.0f}s"
                raise ClaudeCliError(error) from exc
            except OSError as exc:  # e.g. E2BIG, missing executable
                status, error = "os_error", f"{type(exc).__name__}: {exc}"
                raise ClaudeCliError(f"CLI could not start: {error}") from exc
            payload = _parse_cli_json(completed.stdout)
            diagnostic = f"{completed.stderr or ''} {str(payload.get('result') if payload else completed.stdout)[:400]}".casefold()
            if payload is None or payload.get("is_error") or completed.returncode != 0:
                if any(marker in diagnostic for marker in self.LIMIT_MARKERS):
                    status, error = "usage_limit", diagnostic.strip()[:300]
                    raise UsageLimitError(error)
                status, error = "cli_error", f"exit {completed.returncode}: {diagnostic.strip()[:300]}"
                raise ClaudeCliError(f"CLI failure ({error})")
            structured = payload.get("structured_output")
            if not isinstance(structured, dict):
                result = payload.get("result")
                try:
                    structured = json.loads(result) if isinstance(result, str) else None
                except json.JSONDecodeError:
                    structured = None
            if not isinstance(structured, dict):
                status, error = "no_structured_output", "CLI returned no structured output"
                raise ClaudeCliError(error)
            if validate is not None:
                try:
                    structured = validate(structured)
                except Exception as exc:
                    status, error = "invalid_output", f"{type(exc).__name__}: {exc}"
                    raise
            usage = usage_summary(payload)
            status, error = "ok", ""
            return structured, _cli_model(payload), usage
        finally:
            if self.ledger is not None:
                self.ledger.append({
                    "ts": started.isoformat(timespec="seconds"), "run_id": RUN_ID,
                    **dict(ledger_tags or {}),
                    "requested_model": self.model, "model": _cli_model(payload) if payload else None,
                    "effort": self.effort, "status": status, "error": error[:300],
                    **usage_summary(payload),
                })


class RetryingCaller:
    """Retries, usage-limit pauses and the safety-filter fallback around ``ClaudeCli``.

    * A validation failure or CLI error is retried (``retries`` attempts in total).
    * A usage-limit response does not use up an attempt: the thread sleeps and tries
      again; after ``stop_after_limits`` limit responses (all threads) the run stops.
    * A safety-filter refusal ("safeguards flagged") is not retried on the same model:
      the call goes straight to ``fallback_model`` once, and the result records it.
    """

    def __init__(
        self, cli: ClaudeCli, *, fallback_model: str | None = "claude-sonnet-5", retries: int = 3,
        stop_after_limits: int = 40,
    ) -> None:
        self.cli = cli
        self.fallback = (
            ClaudeCli(fallback_model, cli.timeout, cli.executable, effort=cli.effort, ledger=cli.ledger)
            if fallback_model and fallback_model != cli.model else None
        )
        self.retries = retries
        self.stop_after_limits = stop_after_limits
        self.limit_hits = 0
        self._lock = threading.Lock()
        self.stop = threading.Event()

    def call(
        self, *, system_prompt: str, prompt: str, schema: Mapping[str, Any], validate,
        tags: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``{"result", "model", "usage", "attempts", "fallback_for"}`` for one validated answer."""

        try:
            return self._call_with(self.cli, system_prompt, prompt, schema, validate, tags)
        except ClaudeCliError as exc:
            if isinstance(exc, UsageLimitError) or self.fallback is None or "safeguard" not in str(exc).casefold():
                raise
            outcome = self._call_with(self.fallback, system_prompt, prompt, schema, validate, tags)
            return {**outcome, "fallback_for": self.cli.model}

    def _call_with(self, cli: ClaudeCli, system_prompt, prompt, schema, validate, tags) -> dict[str, Any]:
        import time

        last: Exception | None = None
        attempt = 0
        while attempt < self.retries:
            if self.stop.is_set():
                raise UsageLimitError("run stopped after repeated usage-limit responses")
            attempt += 1
            try:
                result, model, usage = cli.request(
                    system_prompt=system_prompt, prompt=prompt, schema=schema,
                    ledger_tags={**dict(tags or {}), "attempt": attempt}, validate=validate,
                )
                return {"result": result, "model": model, "usage": usage, "attempts": attempt, "fallback_for": None}
            except UsageLimitError as exc:
                last = exc
                with self._lock:
                    self.limit_hits += 1
                    hits = self.limit_hits
                if hits >= self.stop_after_limits:
                    self.stop.set()
                    raise
                attempt -= 1  # a limit response is not the paper's fault
                time.sleep(min(900, 60 * min(hits, 15)))
            except ClaudeCliError as exc:
                last = exc
                if "safeguard" in str(exc).casefold():
                    raise
                time.sleep(3 * attempt)
        raise ClaudeCliError(f"failed after {self.retries} attempts: {last}")
