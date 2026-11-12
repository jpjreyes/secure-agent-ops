#!/usr/bin/env bash
# Run an ITBench-AA eval to the end without losing work.
#
#   scripts/run_eval.sh --setup C --split dev --repeats 3 [other evals.run_itbench args]
#
# - Usage stop (80% of the 5-hour or weekly window): waits until that window resets, then resumes the same command.
#   Waits longer than MAX_WAIT_S (default 6 h) end the script; rerun it later and it picks up where it stopped.
# - Paid extra usage or an unclean environment: stops for good (exit 2).
# - Crash: makes sure the checkpointer database is up and resumes; gives up after 3 crashes in a row without a new
#   saved run.
# Interrupted team episodes resume from their last finished step (Postgres checkpoint), so a stop costs at most the
# agent calls that were in flight.
set -uo pipefail
cd "$(dirname "$0")/.."
MAX_WAIT_S="${MAX_WAIT_S:-21600}"
LOG="runs/itbench/run_eval_$(date +%Y%m%d_%H%M%S).log"
mkdir -p runs/itbench

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
saved() { find runs/itbench -name 'rep*.json' | wc -l | tr -d ' '; }

crashes=0
while true; do
  nc -z 127.0.0.1 55436 2>/dev/null || { log "checkpointer database down: starting it"; scripts/dev_db.sh up >>"$LOG" 2>&1; }
  before=$(saved)
  log "start/resume ($before results saved): $*"
  uv run python -m evals.run_itbench "$@" 2>&1 | tee -a "$LOG"
  code=${PIPESTATUS[0]}
  after=$(saved)
  case "$code" in
    0) log "finished"; exit 0 ;;
    2) log "stopped for good (extra usage or unclean environment); see $LOG"; exit 2 ;;
    75)
      retry_at=$(grep -o 'EVAL_RETRY_AT=[0-9]*' "$LOG" | tail -1 | cut -d= -f2)
      wait_s=$(( retry_at - $(date +%s) + 120 ))
      if [ "$wait_s" -gt "$MAX_WAIT_S" ]; then
        log "usage stop; window resets $(date -r "$retry_at"), too far to wait. Rerun this script after that."
        exit 75
      fi
      log "usage stop at $after results; waiting $(( wait_s / 60 )) min"
      sleep "$(( wait_s > 0 ? wait_s : 60 ))"
      crashes=0 ;;
    *)
      if [ "$after" -gt "$before" ]; then crashes=0; else crashes=$((crashes + 1)); fi
      [ "$crashes" -ge 3 ] && { log "3 crashes in a row without progress (exit $code); giving up"; exit "$code"; }
      log "crash (exit $code); restarting in 60 s"
      sleep 60 ;;
  esac
done
