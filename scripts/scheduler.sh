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
cd "$(dirname "$0")/.."

LOCK=/tmp/olympiad_scheduler.lock
if [ -e "$LOCK" ]; then
  echo "$(date -u +%FT%TZ) already running (lock present) -- skipping this tick"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT
touch "$LOCK"

PY=.venv/bin/python
OUT=$($PY -m chessolympiad.cli tick 2>&1)
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
