#!/usr/bin/env bash
# Automated live update, meant to run on a schedule (cron) during the event
# window (16-27 Sep 2026): re-syncs real results for both 2026 sections,
# re-forecasts the remaining rounds, refreshes docs/ (the GitHub Pages
# source), and pushes only if something actually changed.
#
# Board-level round results (the frequent, per-cycle part) come primarily
# from lichess.org's broadcast of the event (chessolympiad.data.lichess_sync)
# -- faster than chess-results.com (no manual arbiter entry step) and
# explicitly welcomes automated polling, unlike chess-results.com's daily
# request cap that got this project's IP blocked once already (see git log
# around the incident). A not-yet-known round is always probed regardless
# of the official schedule on both sources (pairings routinely appear
# hours before a round's official start).
#
# chess-results.com's remaining role -- team list (every cycle, 1
# request/section) and rosters (--full-refresh only, ~once/day) -- keeps
# its volume trivially low now (well under 1% of its daily cap), so the
# cron cadence is no longer bound by that cap at all. It's bound instead
# by being a reasonable Lichess citizen: per cycle, board results cost
# roughly one request per sub-broadcast covering the currently-live round
# (up to 5 for Open, 4 for Women's) plus an occasional (~once/day) refresh
# of the round-ID cache when a new round appears -- modest at any cadence
# from 15 min down to a couple of minutes. One unchanging cron entry runs
# this year-round, every day, at a fixed interval (10 min currently) --
# it does NOT need per-day schedule changes for the rest day (22 Sep) or
# round 11's earlier start time (handled by "always probe" above).
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

# Rosters rarely change mid-event, and re-verifying chess-results.com's own
# board results (kept only as an occasional cross-check against Lichess,
# not the primary path -- see this file's header) is wasted work on a
# 10-min cadence. Do a full refresh only once a day (persisted here since
# a cron process starts fresh each tick) -- the still-live round is always
# re-synced from Lichess on every run regardless.
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
