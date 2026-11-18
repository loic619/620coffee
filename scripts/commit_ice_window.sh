#!/usr/bin/env bash
# Gate and commit whatever the ICE fetch has published so far.
#
#   scripts/commit_ice_window.sh <label>
#
# Called once per market by fetch-ice-certified-stocks.yml: after the arabica
# pass, and again after the robusta pass. Committing per market is the point —
# a robusta pass that later overruns can no longer take arabica down with it
# (run 44052146050, 2026-11-17: cancelled at the 150-min timeout, nothing
# published, arabica's three good snapshots discarded).
#
# The publish gates run HERE, in the same step as the commit, so a commit step
# that has to run after a failed earlier step can never push an ungated payload.
set -euo pipefail

label="${1:?usage: commit_ice_window.sh <label>}"

python3 scripts/validate_catalog.py --update
python3 scripts/check_allowlist.py
python3 scripts/check_payload_window.py
python3 scripts/check_no_secrets.py

git config user.name  "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
# The hits log is added alongside the payload: it is learned state, and a run
# that captures a new publish second has to write that back or tier 0 is empty
# for that date on the next run. `git add` on a missing path exits 128 and
# would kill the step under `bash -e`, so it is added only if the fetcher
# actually wrote it (619 hit exactly this — run 32955091040 fetched
# successfully and threw it away).
git add data/ice catalog.json CATALOG.md
HITS=fetch/scraper/sources/ice_certified_stocks/stock_report_hits.json
if [ -f "$HITS" ]; then git add "$HITS"; fi
if git diff --cached --quiet; then
  echo "No change in the published window ($label)."
  exit 0
fi
git commit -m "data: ICE certified stocks window ($label) [skip ci]"
for attempt in 1 2 3; do
  if git pull --rebase --autostash origin "${GITHUB_REF_NAME:?}" && git push; then
    echo "Pushed the $label window."
    exit 0
  fi
  echo "push attempt $attempt failed; retrying"
  sleep $((RANDOM % 5 + 3))
done
echo "::error::could not push the ICE window ($label)"
exit 1
