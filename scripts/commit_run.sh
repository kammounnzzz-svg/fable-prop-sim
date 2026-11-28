#!/usr/bin/env bash
# The simulation workflow's commit step (.github/workflows/sim.yml): commit the new state and exports (state/ and
# out/) and push them to the branch. Paper trading only.
#
# A push can fail for two reasons.
#   1. GitHub refuses it although nothing else reached the branch (4 Nov 2026: HTTP 500 three times in 30 seconds).
#      The step waits longer and longer (10, 20, 30, 60, 90 s) and pushes again.
#   2. Another commit reached the branch while this run was working. If that commit changed nothing under state/ or
#      out/ (code or notes), this run's commit is put on top of it and pushed. If it did (another simulation run
#      saved its result first), this run's result is out of date: it is thrown away, the branch's newest commit is
#      checked out and the simulation runs again on it (at most MAX_RERUNS times), so no run's result is lost and no
#      run works from an older state. Until 28 Nov 2026 the step only retried the push, which cannot succeed in this
#      case (26 Nov 2026 19:15 UTC).
# Exits 0 when the result is pushed or there is nothing to commit, 1 when it gives up (the next run catches up).
#
# Environment, all optional: BRANCH (default: the branch checked out), REMOTE (default origin), SIM_CMD (default
# "python3 scripts/run_sim.py"), PUSH_ATTEMPTS (default 6), MAX_RERUNS (default 2), RETRY_SLEEPS (seconds to wait
# before each new attempt, default "10 20 30 60 90"; the last value repeats).
set -u
cd "$(dirname "$0")/.." || exit 1
BRANCH=${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}
REMOTE=${REMOTE:-origin}
SIM_CMD=${SIM_CMD:-python3 scripts/run_sim.py}
PUSH_ATTEMPTS=${PUSH_ATTEMPTS:-6}
MAX_RERUNS=${MAX_RERUNS:-2}
read -r -a SLEEPS <<< "${RETRY_SLEEPS:-10 20 30 60 90}"
if [ ${#SLEEPS[@]} -eq 0 ]; then SLEEPS=(10); fi
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "no branch to push to (detached HEAD and BRANCH not set)" >&2
  exit 1
fi
git config user.name "fable-prop-sim bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

commit_result() {   # stage state/ and out/ and commit them; returns 1 when nothing changed
  git add state out
  if git diff --cached --quiet; then return 1; fi
  git commit -q -m "sim run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

if ! commit_result; then
  echo "nothing to commit"
  exit 0
fi
reruns=0
for ((attempt = 1; attempt <= PUSH_ATTEMPTS; attempt++)); do
  if git push -q "$REMOTE" "HEAD:refs/heads/$BRANCH"; then
    echo "pushed $(git rev-parse --short HEAD) to $BRANCH (attempt $attempt)"
    exit 0
  fi
  echo "push failed (attempt $attempt of $PUSH_ATTEMPTS)"
  if [ "$attempt" -eq "$PUSH_ATTEMPTS" ]; then break; fi
  i=$((attempt - 1)); if [ $i -ge ${#SLEEPS[@]} ]; then i=$((${#SLEEPS[@]} - 1)); fi
  sleep "${SLEEPS[$i]}"
  if ! git fetch -q "$REMOTE" "$BRANCH"; then
    echo "could not fetch $REMOTE/$BRANCH; trying the push again"
    continue
  fi
  remote_head=$(git rev-parse FETCH_HEAD)
  if git merge-base --is-ancestor "$remote_head" HEAD; then
    echo "nothing else reached $BRANCH: GitHub refused the push itself; trying again"
    continue
  fi
  base=$(git merge-base HEAD "$remote_head" 2>/dev/null)
  if [ -n "$base" ] && [ -z "$(git diff --name-only "$base" "$remote_head" -- state out)" ] && git rebase -q "$remote_head"; then
    echo "another commit reached $BRANCH first ($(git rev-parse --short "$remote_head")) without touching state/ or out/: this run's commit now sits on top of it"
    continue
  fi
  git rebase --abort >/dev/null 2>&1
  if [ "$reruns" -ge "$MAX_RERUNS" ]; then
    echo "another simulation run saved its result first again; giving up after $reruns re-run(s) (the next run catches up)" >&2
    exit 1
  fi
  reruns=$((reruns + 1))
  echo "another simulation run saved its result first ($(git rev-parse --short "$remote_head")): this run's result is out of date; running the simulation again on that commit (re-run $reruns of at most $MAX_RERUNS)"
  git reset -q --hard "$remote_head"
  $SIM_CMD
  if ! commit_result; then
    echo "nothing new to commit after the re-run"
    exit 0
  fi
done
echo "could not push after $PUSH_ATTEMPTS attempts (the next run catches up)" >&2
exit 1
