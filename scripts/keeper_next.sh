#!/usr/bin/env bash
# The keeper's last step (.github/workflows/keeper.yml): start the next keeper run, so that the keeper does not
# depend on GitHub's schedule. Paper trading only: it starts this repository's own keeper workflow and nothing else.
#
# It starts the next run unless
#   - this is a test run (TEST_RUN=true): test runs never start another;
#   - this run started less than MIN_WAIT_MIN minutes after it was created: then the wait timer of the environment
#     "sim-keeper" is missing or too short, and restarting would loop without a pause (GitHub creates a missing
#     environment by itself, without a timer, the first time a workflow names it);
#   - a newer keeper run that is not a test run is already queued, waiting or running: that run carries on, so two
#     keepers (for example one restarted by the schedule) merge into one instead of multiplying.
# It never fails: when anything is unclear it says why. A keeper that stops is restarted by the keeper's schedule.
#
# Environment: GH_TOKEN, REPO (owner/name), RUN_ID (this run's id), REF (branch, default main). Optional: TEST_RUN,
# MIN_WAIT_MIN (default 10); for tests only: KEEPER_NOW (the current UTC time), KEEPER_RETRY_SLEEP (default 15).
set +e
say() { echo "$1"; if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "$1" >> "$GITHUB_STEP_SUMMARY"; fi; }
MIN_WAIT_MIN=${MIN_WAIT_MIN:-10}

if [ "${TEST_RUN:-}" = "true" ]; then
  say "Test run: the keeper does not start its next run."
  exit 0
fi
if ! [[ "${RUN_ID:-}" =~ ^[0-9]+$ && "$MIN_WAIT_MIN" =~ ^[0-9]+$ ]]; then
  say "This run's id or the minimum wait is missing or not a number ('${RUN_ID:-}', '$MIN_WAIT_MIN'). Not starting the next run."
  exit 0
fi

created=$(gh api "repos/$REPO/actions/runs/$RUN_ID" --jq '.created_at' 2>&1)
waited=$(python3 - "$created" "${KEEPER_NOW:-}" 2>/dev/null <<'PY'
import datetime as dt, sys
p = lambda s: dt.datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
now = p(sys.argv[2]) if sys.argv[2] else dt.datetime.now(dt.timezone.utc)
print(int((now - p(sys.argv[1])).total_seconds() // 60))
PY
)
if ! [[ "$waited" =~ ^-?[0-9]+$ ]]; then
  say "Could not tell when this run was created (${created:0:200}). Not starting the next run; the keeper's schedule restarts it later."
  exit 0
fi
if [ "$waited" -lt "$MIN_WAIT_MIN" ]; then
  say "This run started $waited minute(s) after it was created, less than $MIN_WAIT_MIN: the wait timer of the environment 'sim-keeper' is missing or too short (Settings > Environments > sim-keeper > Wait timer). Not starting the next run, so the keeper cannot loop without a pause."
  exit 0
fi

newer=$(gh api "repos/$REPO/actions/workflows/keeper.yml/runs?per_page=30" \
  --jq ".workflow_runs[] | select(.status != \"completed\" and .id > $RUN_ID and ((.display_title // \"\") | test(\"test\"; \"i\") | not)) | \"\(.id) \(.status)\"" 2>&1)
rc=$?
if [ $rc -eq 0 ] && [ -n "$newer" ]; then
  say "This run waited $waited minutes. A newer keeper run carries on ($(printf '%s' "$newer" | tr '\n' ' ')); this one stops here."
  exit 0
fi
if [ $rc -ne 0 ]; then
  say "Could not list the keeper's runs (${newer:0:200}); starting the next run anyway (a second keeper would stop by itself at its end)."
fi

for attempt in 1 2 3; do
  if out=$(gh workflow run keeper.yml -R "$REPO" --ref "${REF:-main}" 2>&1); then
    say "This run waited $waited minutes. Started the next keeper run. ${out:0:200}"
    exit 0
  fi
  if [ $attempt -lt 3 ]; then sleep "${KEEPER_RETRY_SLEEP:-15}"; fi
done
say "Could not start the next keeper run (${out:0:300}). The keeper's schedule restarts it later."
exit 0
