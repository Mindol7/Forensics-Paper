#!/usr/bin/env bash
#
# Monthly incremental update for the KCI corpus.
#
#   1) --build-corpus       : fetch only new months (the corpus checkpoint skips
#                             already-done months; the most recent months are
#                             re-checked for late-registered papers). Network.
#   2) --match-corpus --incremental
#                           : classify ONLY the newly-added corpus rows and
#                             APPEND to kci_papers, then regenerate the
#                             accumulated Excel (reports/kci_papers_accumulated.xlsx).
#                             Offline, fast.
#
# One-time prerequisite: run a FULL match once to set the incremental baseline:
#     .venv/bin/python -u main.py --source kci --match-corpus
# (also re-run the full match whenever you change the keyword set / blacklist —
#  --incremental does NOT apply keyword changes retroactively.)
#
# Scheduling: run this on the last day of each month however you like. WSL cron
# does NOT run by default — either start it (`sudo service cron start`, and enable
# it to autostart), or trigger this script from Windows Task Scheduler via
# `wsl.exe`. A "last day of month" cron needs a guard, e.g.:
#     0 23 28-31 * *  [ "$(date -d tomorrow +\%d)" = "01" ] && /home/mindol/Paper-Collector/run_monthly.sh
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/.venv/bin/python"
LOG="${KCI_MONTHLY_LOG:-$HOME/kci_monthly.log}"

cd "$REPO"
echo "[run_monthly] START $(date '+%F %T')" | tee -a "$LOG"

"$PY" -u main.py --source kci --build-corpus                 2>&1 | tee -a "$LOG"
"$PY" -u main.py --source kci --match-corpus --incremental   2>&1 | tee -a "$LOG"

echo "[run_monthly] DONE  $(date '+%F %T')" | tee -a "$LOG"
