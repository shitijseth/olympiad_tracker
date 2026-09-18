#!/usr/bin/env bash
# Automated live update, meant to run on a schedule (cron) during the event
# window (16-27 Sep 2026): re-syncs real results from chess-results.com for
# both 2026 sections, re-forecasts the remaining rounds, refreshes docs/
# (the GitHub Pages source), and pushes only if something actually changed.
#
# One unchanging cron entry is meant to run this year-round, every day,
# at a fixed interval (5 min recommended -- see below) -- it does NOT
# need per-day schedule changes for the rest day (22 Sep) or round 11's
# earlier start time. All of that is handled inside chessolympiad.data.sync
# itself: a tick outside any round's live window (chessolympiad.data.schedule)
# costs just the team-list check (~2 requests) and does nothing else, and
# a tick during a round only re-fetches that one round until it's
# complete, never re-checking anything already settled. See sync.py's
# module docstring for the full request-volume design.
#
# Cadence choice: with the three gates above, idle time is nearly free, so
# the cadence is really just "how stale can a live result be" traded
# against request-budget margin. At 5 min: ~1000 requests/day (~50% of
# the 2000/day cap), 5 min worst-case lag behind chess-results.com. 3 min
# is viable too (~70% of cap) if lower latency matters more than margin;
# 2 min or tighter isn't recommended (>90% of cap leaves no room for
# manual checks or an unusually long round on top).
set -uo pipefail
cd "$(dirname "$0")/.."

LOCK=/tmp/olympiad_auto_update.lock
if [ -e "$LOCK" ]; then
  echo "$(date -u +%FT%TZ) already running (lock present) -- skipping this run"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT
touch "$LOCK"

PY=.venv/bin/python
echo "=== $(date -u +%FT%TZ) starting auto-update ==="

# Two things dominate this script's chess-results.com request volume and
# are both almost entirely wasted work on a 10-15 min cadence: re-fetching
# every team's roster (rarely changes mid-event) and re-fetching rounds
# already recorded locally as fully decided (can't change short of a rare
# post-hoc correction). Do a full refresh only once a day (persisted here
# since a cron process starts fresh each tick) -- the still-live round is
# always re-synced on every run regardless.
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
  $PY -m chessolympiad.cli live-update "$section" --iterations 5000 $REFRESH_FLAG || echo "WARN: live-update $section failed, continuing"
done

if ! $PY -m chessolympiad.report.export_artifact_data; then
  echo "ERROR: export_artifact_data failed -- aborting this run"
  exit 1
fi

./scripts/sync_docs.sh > /dev/null

if [ -n "$(git status --porcelain docs data/artifact_export reports)" ]; then
  git add docs data/artifact_export reports
  git commit -m "Automated live update: $(date -u +%FT%TZ)" > /dev/null
  if git push; then
    echo "=== $(date -u +%FT%TZ) pushed update ==="
  else
    echo "ERROR: git push failed -- changes are committed locally, will retry pushing next run"
  fi
else
  echo "=== $(date -u +%FT%TZ) no changes, nothing to push ==="
fi
