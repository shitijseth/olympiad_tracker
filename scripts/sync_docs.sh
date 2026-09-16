#!/usr/bin/env bash
# Syncs the GitHub Pages source (docs/) from the working copies of the
# dashboard and its exported forecast data. Run this after any dashboard.html
# edit or after re-running `python -m chessolympiad.report.export_artifact_data`,
# then commit and push docs/ to publish the update.
set -euo pipefail
cd "$(dirname "$0")/.."

cp ui/artifact/dashboard.html docs/index.html
cp ui/artifact/assets/*.png docs/assets/
cp data/artifact_export/*.json docs/data/

echo "docs/ synced. Review with 'git status', then:"
echo "  git add docs && git commit -m 'Update dashboard data' && git push"
