#!/usr/bin/env bash
# Run the idempotent monthly pipeline through the main entry point:
#   python3 main.py  ->  KCI metadata (full or incremental) -> filtering -> PDFs
#   -> Claude analysis -> OA/Special-Issue/reference collection
#   -> type/Level/keyword derivation -> MD profiles -> knowledge graphs.
#
# Cron example (replace the path with this repository's absolute path):
#   0 2 1 * * /absolute/path/to/Forensics-Paper/run_monthly.sh --non-interactive
#
# The pipeline is checkpoint-driven: it fetches every publication month the
# corpus still owes (never skipping a month when a run was missed) and
# re-checks the two most recent completed months, so it may run on any day.
# Every argument is passed to main.py: --dry-run prints the plan, stage flags
# such as -all / -pdf / -md run a subset (see `python3 main.py -h`).
# Set PIPELINE_ENTRYPOINT=Scheduler.pipeline to use the stage-name CLI instead
# (same options, stages chosen with --stages/--skip-<stage>).
#
# WSL cron is not started automatically on every installation. Enable the cron
# service or invoke this script from Windows Task Scheduler.

set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPOSITORY_ROOT/.venv/bin/python}"
LOG_PATH="${KCI_MONTHLY_LOG:-$REPOSITORY_ROOT/reports/kci_monthly.log}"
ENTRYPOINT="${PIPELINE_ENTRYPOINT:-main.py}"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[run_monthly] executable Python not found: $PYTHON_BIN" >&2
    exit 127
fi

mkdir -p "$(dirname "$LOG_PATH")"
cd "$REPOSITORY_ROOT"

if [[ "$ENTRYPOINT" == *.py ]]; then
    COMMAND=("$PYTHON_BIN" -u "$ENTRYPOINT")
else
    COMMAND=("$PYTHON_BIN" -u -m "$ENTRYPOINT")
fi

echo "[run_monthly] START $(date '+%F %T') entrypoint=$ENTRYPOINT" | tee -a "$LOG_PATH"
set +e
"${COMMAND[@]}" "$@" 2>&1 | tee -a "$LOG_PATH"
JOB_STATUS=${PIPESTATUS[0]}
set -e

if [[ "$JOB_STATUS" -eq 0 ]]; then
    echo "[run_monthly] DONE  $(date '+%F %T')" | tee -a "$LOG_PATH"
else
    echo "[run_monthly] FAILED status=$JOB_STATUS $(date '+%F %T')" | tee -a "$LOG_PATH" >&2
fi
exit "$JOB_STATUS"
