#!/usr/bin/env bash
# Resimulate + publish half of the live 2026 update pipeline -- runs every
# 10 min, independently of scripts/sync_data.sh's 2-min data-fetch cadence
# (see that script's header for why they're split). Reads whatever the
# most recent sync left in data/olympiad.db, re-forecasts the remaining
# rounds, refreshes docs/ (the GitHub Pages source), and pushes only if
# something actually changed -- simulate_and_store's own data_fingerprint
# check already skips the Monte Carlo run entirely when nothing did.
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
echo "=== $(date -u +%FT%TZ) starting resimulate+publish ==="

for section in open women; do
  $PY -m chessolympiad.cli simulate "$section" --iterations 5000 || echo "WARN: simulate $section failed, continuing"
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
