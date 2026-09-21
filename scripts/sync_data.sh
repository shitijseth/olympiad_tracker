#!/usr/bin/env bash
# SUPERSEDED for the live cron cadence by scripts/scheduler.sh (see that
# file), which varies sync frequency by whether a round is actually active
# instead of this script's unconditional 2-minute cadence. Kept only for
# manual/one-off use -- not in crontab.
#
# Data-fetch half of the live 2026 update pipeline -- runs far more often
# than scripts/auto_update.sh (2 min vs. 10) since re-syncing real results
# is cheap and worth doing frequently, but resimulating/publishing on every
# one of those cycles would just burn CPU on Monte Carlo runs that are
# almost always going to reuse the prior forecast anyway (see
# simulate_and_store's data_fingerprint skip). This script only ever
# touches the local data/olympiad.db -- no export, no docs/, no git.
#
# Board-level round results come primarily from lichess.org's broadcast of
# the event (chessolympiad.data.lichess_sync) -- faster than
# chess-results.com (no manual arbiter entry step) and explicitly welcomes
# automated polling, unlike chess-results.com's daily request cap that got
# this project's IP blocked once already. chess-results.com's remaining
# role -- team list (every cycle, cheap) and rosters (--full-refresh only,
# ~once/day) -- keeps its volume trivial regardless of this cadence.
set -uo pipefail
cd "$(dirname "$0")/.."

LOCK=/tmp/olympiad_sync_data.lock
if [ -e "$LOCK" ]; then
  echo "$(date -u +%FT%TZ) already running (lock present) -- skipping this run"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT
touch "$LOCK"

PY=.venv/bin/python
echo "=== $(date -u +%FT%TZ) starting data sync ==="

REFRESH_STATE=data/.last_full_refresh
REFRESH_FLAG=""
now_epoch=$(date -u +%s)
last_epoch=0
[ -f "$REFRESH_STATE" ] && last_epoch=$(cat "$REFRESH_STATE")
if [ $((now_epoch - last_epoch)) -ge 79200 ]; then # ~22h, drifts earlier each day rather than later
  REFRESH_FLAG="--full-refresh"
  echo "$now_epoch" > "$REFRESH_STATE"
fi

for section in open women; do
  $PY -m chessolympiad.cli sync-only "$section" $REFRESH_FLAG || echo "WARN: sync-only $section failed, continuing"
done

echo "=== $(date -u +%FT%TZ) sync done ==="
