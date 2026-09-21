#!/usr/bin/env bash
# Single entry point for the live 2026 cron pipeline -- replaces the old
# sync_data.sh (every 2 min, unconditional) + auto_update.sh (every 10 min,
# unconditional) split with one script invoked every minute, whose actual
# cadence chessolympiad.cli.tick decides internally from the fixed round
# calendar (chessolympiad/schedule.py) plus each section's real completion
# state:
#   - between rounds (before a round starts, the rest day, or after a
#     round finishes/times out): chess-results.com only, every 2h --
#     pairings/board order for the next round, plus an occasional roster
#     full-refresh.
#   - inside a round's active window: lichess every tick (~1 min), plus a
#     5000-iteration/4-worker resimulation and a per-section
#     newly-completed-round deep run (20000 iters, 1 worker) every 5 min.
# An active window ends either when both sections' round data is complete,
# or after 1 hour with no new lichess results (stuck/disputed game) -- see
# tick()'s docstring in chessolympiad/cli.py for the exact rule.
set -uo pipefail
set -m  # job control: the backgrounded tick below gets its own process group, so a timeout kill can take its children (e.g. ProcessPoolExecutor sim workers) down with it, not just the tick process itself
cd "$(dirname "$0")/.."

LOCK=/tmp/olympiad_scheduler.lock
if [ -e "$LOCK" ]; then
  echo "$(date -u +%FT%TZ) already running (lock present) -- skipping this tick"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT
touch "$LOCK"

PY=.venv/bin/python

# A tick should never legitimately run anywhere near this long -- the
# worst normal case is both sections needing their 5-min resimulation AND
# their once-per-round 20000-iteration/1-worker deep run in the same
# cycle, comfortably under 15 minutes even on a loaded machine. This
# exists purely to recover from a genuine hang (seen once in production:
# a lichess network call stalled indefinitely under host memory pressure)
# rather than to bound normal operation -- without it, a hung tick holds
# LOCK forever and silently freezes the whole pipeline, since every later
# cron invocation just sees the lock and skips.
TICK_TIMEOUT=1800
TICK_OUT=$(mktemp)
$PY -m chessolympiad.cli tick > "$TICK_OUT" 2>&1 &
TICK_PID=$!
waited=0
while kill -0 "$TICK_PID" 2>/dev/null; do
  if [ "$waited" -ge "$TICK_TIMEOUT" ]; then
    echo "$(date -u +%FT%TZ) ERROR: tick exceeded ${TICK_TIMEOUT}s (pid $TICK_PID) -- killing its process group and skipping this cycle's publish" >> "$TICK_OUT"
    kill -TERM -- -"$TICK_PID" 2>/dev/null
    sleep 3
    kill -KILL -- -"$TICK_PID" 2>/dev/null
    break
  fi
  sleep 5
  waited=$((waited + 5))
done
wait "$TICK_PID" 2>/dev/null
OUT=$(cat "$TICK_OUT")
rm -f "$TICK_OUT"
echo "$OUT"

if ! echo "$OUT" | grep -q "^PUBLISH=1$"; then
  exit 0
fi

if ! $PY -m chessolympiad.report.export_artifact_data; then
  echo "ERROR: export_artifact_data failed -- aborting this tick's publish"
  exit 1
fi

./scripts/sync_docs.sh > /dev/null

if [ -n "$(git status --porcelain docs data/artifact_export reports)" ]; then
  git add docs data/artifact_export reports
  git commit -m "Automated live update: $(date -u +%FT%TZ)" > /dev/null
  if git push; then
    echo "=== $(date -u +%FT%TZ) pushed update ==="
  else
    echo "ERROR: git push failed -- changes are committed locally, will retry pushing next tick"
  fi
else
  echo "=== $(date -u +%FT%TZ) no changes, nothing to push ==="
fi
