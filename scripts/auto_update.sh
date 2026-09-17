#!/usr/bin/env bash
# Automated live update, meant to run on a schedule (cron) during the event
# window (16-27 Sep 2026): re-syncs real results from chess-results.com for
# both 2026 sections, re-forecasts the remaining rounds, refreshes docs/
# (the GitHub Pages source), and pushes only if something actually changed.
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

# Rosters rarely change mid-event (only rare substitutions), but fetching
# all ~200 of them per section is ~98% of this script's chess-results.com
# request volume. Only do it once a day (persisted across runs here, since
# a cron process starts fresh each tick) -- results/pairings are still
# re-synced on every run regardless.
ROSTER_STATE=data/.last_roster_refresh
REFRESH_FLAG=""
now_epoch=$(date -u +%s)
last_epoch=0
[ -f "$ROSTER_STATE" ] && last_epoch=$(cat "$ROSTER_STATE")
if [ $((now_epoch - last_epoch)) -ge 79200 ]; then # ~22h, drifts earlier each day rather than later
  REFRESH_FLAG="--refresh-rosters"
  echo "$now_epoch" > "$ROSTER_STATE"
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
