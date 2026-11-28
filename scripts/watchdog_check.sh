#!/usr/bin/env bash
# The watchdog's check, shared by .github/workflows/watchdog.yml (started by GitHub's schedule) and
# .github/workflows/keeper.yml (started by its own previous run, after an environment's wait timer). Paper trading
# only: the most it does is start this repository's own simulation workflow, the same as the "Run workflow" button
# on the Actions page. It runs no simulation code and changes nothing in the repository.
#
# It reads the simulation workflow's newest runs. When the newest one is queued or running it does nothing.
# Otherwise it takes the newest run that SUCCEEDED (a failed run saved nothing, so it has not served its slot; until
# 28 Nov 2026 the watchdog counted it) and asks scripts/sim_watchdog.py whether the first scheduled slot after that
# run is 70 or more minutes overdue; if so, it starts the simulation. It never fails: when anything is unclear it
# starts nothing and says why.
#
# Environment: GH_TOKEN and REPO (owner/name), as the workflows set them. Optional: REF (the branch to start,
# default main); PRETEND_LAST (test only: treat this UTC time, like 2026-11-18T07:53:41Z, as the newest successful
# run); DRY_RUN=true (test only: decide and print, start nothing); THRESHOLD_MIN (default: sim_watchdog.py's 70);
# WATCHDOG_NOW (test only: the current UTC time).
set +e
cd "$(dirname "$0")/.." || exit 0
say() { echo "$1"; if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "$1" >> "$GITHUB_STEP_SUMMARY"; fi; }
TS='[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z'

runs=$(gh api "repos/$REPO/actions/workflows/sim.yml/runs?per_page=30" \
  --jq '.workflow_runs[] | "\(.status) \(.conclusion // "none") \(.created_at)"' 2>&1)
rc=$?
if [ $rc -ne 0 ] || [ -z "$runs" ] || printf '%s\n' "$runs" | grep -Evq "^[a-z_]+ [a-z_]+ $TS\$"; then
  say "Could not read the simulation's runs (${runs:0:200}). Nothing started."
  exit 0
fi
read -r status conclusion newest <<< "$(printf '%s\n' "$runs" | head -n 1)"
last=$(printf '%s\n' "$runs" | awk '$1 == "completed" && $2 == "success" { print $3; exit }')

if [ -n "${PRETEND_LAST:-}" ]; then
  say "TEST: pretending the newest successful simulation run started at $PRETEND_LAST (really: newest run $status/$conclusion, started $newest; newest successful ${last:-none of the last 30})."
  last="$PRETEND_LAST"
elif [ "$status" != "completed" ]; then
  say "A simulation run is already $status (started $newest). Nothing to do."
  exit 0
elif [ -z "$last" ]; then
  say "None of the simulation's last 30 runs succeeded (newest: $conclusion, started $newest). Nothing started: look at the Actions page."
  exit 0
elif [ "$last" != "$newest" ]; then
  say "The newest simulation run (started $newest) ended '$conclusion' and saved nothing, so it does not count as its slot's run."
fi

args=(--last "$last")
if [ -n "${THRESHOLD_MIN:-}" ]; then args+=(--threshold-min "$THRESHOLD_MIN"); fi
if [ -n "${WATCHDOG_NOW:-}" ]; then args+=(--now "$WATCHDOG_NOW"); fi
verdict=$(python3 scripts/sim_watchdog.py "${args[@]}" 2>&1)
say "Newest successful simulation run started $last. $verdict"
case "$verdict" in
  *"start=yes"*)
    if [ "${DRY_RUN:-}" = "true" ]; then
      say "TEST (dry run): the simulation would be started now."
    elif out=$(gh workflow run sim.yml -R "$REPO" --ref "${REF:-main}" 2>&1); then
      say "The scheduled run is overdue: started the simulation. ${out:0:200}"
    else
      say "The scheduled run is overdue, but the simulation could not be started: ${out:0:300}"
    fi
    ;;
  *)
    say "Nothing to do."
    ;;
esac
exit 0
