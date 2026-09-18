#!/usr/bin/env bash
# Automated live update, meant to run on a schedule (cron) during the event
# window (16-27 Sep 2026): re-syncs real results from chess-results.com for
# both 2026 sections, re-forecasts the remaining rounds, refreshes docs/
# (the GitHub Pages source), and pushes only if something actually changed.
#
# One unchanging cron entry is meant to run this year-round, every day, at
# a fixed interval (10 min currently) -- it does NOT need per-day schedule
# changes for the rest day (22 Sep) or round 11's earlier start time; that
# part is handled inside chessolympiad.data.sync (a round already fully
# decided is never re-fetched, see sync.py's module docstring).
#
# Every cycle still probes for the next not-yet-known round regardless of
# what time it is or when that round is officially scheduled to start --
# chess-results.com routinely publishes a round's pairings hours (or a
# day) ahead of its start, and gating that probe on the official start
# time (an earlier version of this script did) just meant those pairings
# went unnoticed for hours. That "is there anything new yet" probe is
# cheap (one empty request when there isn't) but, unlike a results-only
# gate would, it runs all day rather than only during a ~7h/day live
# window, so idle time is no longer nearly free the way it would be with
# such a gate: roughly 2-3 round requests/section/cycle around the clock
# (current round + next-round probe) plus the team list.
#
# Cadence choice, accounting for that: ~600/day at 15 min (~30% of the
# 2000/day cap), ~860/day at 10 min (~43%), ~1700/day at 5 min (~86% --
# not recommended, too little margin left for the daily full-refresh pass
# or manual checks on top). 10 min is a reasonable balance.
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
