#!/usr/bin/env python3
"""Tests for the scripts behind the simulation's backup starts and its commit step (28 Nov 2026). Paper trading only.

    python3 tests/test_backup_scripts.py            # from the repository root; needs bash, git, jq and python3

- scripts/watchdog_check.sh and scripts/keeper_next.sh run against a stand-in for the GitHub CLI (`gh`) that answers
  each API path with a fixed JSON body (filtered by the real jq, as `gh --jq` does) and records every call, so a test
  can see exactly what would have been started.
- scripts/commit_run.sh runs in throwaway local git repositories: a bare "origin", the run's own clone and a second
  clone that plays the other run or a code commit. A pre-receive hook on origin can refuse pushes, as GitHub did on
  4 Nov 2026. A stand-in simulation records which state it worked from.
Nothing here touches GitHub, the network or the real repository's history.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "kammounnzzz-svg/fable-prop-sim"
FAILS = []

GH_STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
scen = json.load(open(os.environ["GH_STUB_SCENARIO"]))
with open(os.environ["GH_STUB_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if a[:1] == ["api"]:
    path, jq = a[1], (a[a.index("--jq") + 1] if "--jq" in a else ".")
    ans = scen.get("api", {}).get(path)
    if ans is None:
        sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
    if ans.get("rc", 0) != 0:
        sys.stderr.write(ans.get("stderr", "gh: Server Error (HTTP 500)") + "\n"); sys.exit(ans["rc"])
    if "raw" in ans:
        sys.stdout.write(ans["raw"]); sys.exit(0)
    p = subprocess.run(["jq", "-r", jq], input=json.dumps(ans["body"]), capture_output=True, text=True)
    sys.stdout.write(p.stdout); sys.stderr.write(p.stderr); sys.exit(p.returncode)
if a[:2] == ["workflow", "run"]:
    wf = a[2]
    seq = scen.get("run", {}).get(wf, [0])
    cnt_path = os.environ["GH_STUB_LOG"] + "." + wf + ".count"
    n = int(open(cnt_path).read()) if os.path.exists(cnt_path) else 0
    open(cnt_path, "w").write(str(n + 1))
    rc = seq[n] if n < len(seq) else seq[-1]
    if rc == 0:
        print("Created workflow_dispatch event for %s at %s" % (wf, a[a.index("--ref") + 1] if "--ref" in a else "?")); sys.exit(0)
    sys.stderr.write("could not create workflow dispatch event: HTTP 500: Server Error\n"); sys.exit(1)
sys.stderr.write("stub gh: unexpected call %r\n" % a); sys.exit(2)
'''


def check(name, cond, detail=""):
    print("%-4s %s%s" % ("ok" if cond else "FAIL", name, "" if cond else "   <-- " + detail))
    if not cond:
        FAILS.append(name)


class GhStub:
    def __init__(self, tmp):
        self.dir = os.path.join(tmp, "bin")
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, "gh")
        with open(path, "w") as f:
            f.write(GH_STUB)
        os.chmod(path, 0o755)
        self.tmp = tmp

    def run(self, script, scenario, env_extra):
        scen = os.path.join(self.tmp, "scenario.json")
        log = os.path.join(self.tmp, "gh.log")
        for p in [log] + [os.path.join(self.tmp, x) for x in os.listdir(self.tmp) if x.startswith("gh.log.")]:
            if os.path.exists(p):
                os.remove(p)
        with open(scen, "w") as f:
            json.dump(scenario, f)
        env = dict(os.environ, PATH=self.dir + os.pathsep + os.environ["PATH"], GH_STUB_SCENARIO=scen, GH_STUB_LOG=log,
                   REPO=REPO, GH_TOKEN="stub")
        env.pop("GITHUB_STEP_SUMMARY", None)
        env.update(env_extra)
        p = subprocess.run(["bash", os.path.join(ROOT, "scripts", script)], env=env, capture_output=True, text=True, timeout=120)
        calls = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
        return p.returncode, p.stdout + p.stderr, calls


def sim_runs(*rows):
    return {"api": {"repos/%s/actions/workflows/sim.yml/runs?per_page=30" % REPO: {"body": {"workflow_runs": [
        {"status": s, "conclusion": c, "created_at": t} for s, c, t in rows]}}}}


def started(calls, wf):
    return [c for c in calls if c[:3] == ["workflow", "run", wf]]


def test_watchdog_check(gh):
    print("== scripts/watchdog_check.sh")
    S, F = ("completed", "success"), ("completed", "failure")
    cases = [
        # name, scenario, env, expect a start of sim.yml?, text the output must contain
        ("a run is in progress: nothing", sim_runs(("in_progress", None, "2026-11-18T10:40:00Z"), S + ("2026-11-18T07:53:41Z",)),
         {"WATCHDOG_NOW": "2026-11-18T13:40:00Z"}, False, "already in_progress"),
        ("a run is queued: nothing", sim_runs(("queued", None, "2026-11-18T10:40:00Z"), S + ("2026-11-18T07:53:41Z",)),
         {"WATCHDOG_NOW": "2026-11-18T13:40:00Z"}, False, "already queued"),
        ("next slot not due yet: nothing", sim_runs(S + ("2026-11-18T07:53:41Z",)), {"WATCHDOG_NOW": "2026-11-18T08:07:00Z"}, False, "start=no"),
        ("69 minutes overdue: nothing", sim_runs(S + ("2026-11-18T07:53:41Z",)), {"WATCHDOG_NOW": "2026-11-18T10:32:59Z"}, False, "overdueMinutes=69 start=no"),
        ("70 minutes overdue: starts the simulation on main", sim_runs(S + ("2026-11-18T07:53:41Z",)), {"WATCHDOG_NOW": "2026-11-18T10:33:00Z"},
         True, "started the simulation. Created workflow_dispatch event for sim.yml at main"),
        ("newest run failed: counts from the newest successful run and starts",
         sim_runs(F + ("2026-11-18T09:40:00Z",), S + ("2026-11-18T07:53:41Z",)), {"WATCHDOG_NOW": "2026-11-18T10:40:00Z"},
         True, "ended 'failure' and saved nothing"),
        ("newest run failed but the slot after the last success is not due: nothing",
         sim_runs(F + ("2026-11-18T10:00:00Z",), S + ("2026-11-18T09:25:00Z",)), {"WATCHDOG_NOW": "2026-11-18T11:00:00Z"}, False, "dueSlot=2026-11-18T12:23:00Z"),
        ("newest run cancelled: counts from the newest successful run",
         sim_runs(("completed", "cancelled", "2026-11-18T09:40:00Z"), S + ("2026-11-18T07:53:41Z",)), {"WATCHDOG_NOW": "2026-11-18T10:40:00Z"},
         True, "ended 'cancelled'"),
        ("no success among the last 30: nothing", sim_runs(*[F + ("2026-11-18T%02d:40:00Z" % h,) for h in range(12, 2, -1)]),
         {"WATCHDOG_NOW": "2026-11-18T13:40:00Z"}, False, "None of the simulation's last 30 runs succeeded"),
        ("API error: nothing", {"api": {"repos/%s/actions/workflows/sim.yml/runs?per_page=30" % REPO: {"rc": 1, "stderr": "HTTP 502"}}},
         {"WATCHDOG_NOW": "2026-11-18T13:40:00Z"}, False, "Could not read the simulation's runs (HTTP 502"),
        ("garbled answer: nothing", {"api": {"repos/%s/actions/workflows/sim.yml/runs?per_page=30" % REPO: {"raw": "completed success yesterday\n"}}},
         {"WATCHDOG_NOW": "2026-11-18T13:40:00Z"}, False, "Could not read the simulation's runs"),
        ("empty run list: nothing", sim_runs(), {"WATCHDOG_NOW": "2026-11-18T13:40:00Z"}, False, "Could not read the simulation's runs"),
        ("start refused: says so", dict(sim_runs(S + ("2026-11-18T07:53:41Z",)), run={"sim.yml": [1]}), {"WATCHDOG_NOW": "2026-11-18T13:40:00Z"},
         True, "could not be started: could not create workflow dispatch event"),
        ("dry run: decides, starts nothing", sim_runs(S + ("2026-11-18T07:53:41Z",)), {"WATCHDOG_NOW": "2026-11-18T13:40:00Z", "DRY_RUN": "true"},
         False, "TEST (dry run): the simulation would be started now."),
        ("pretend time while a run is in progress (test): decides from the pretend time",
         sim_runs(("in_progress", None, "2026-11-18T13:30:00Z")), {"WATCHDOG_NOW": "2026-11-18T13:40:00Z", "DRY_RUN": "true", "PRETEND_LAST": "2026-11-18T07:53:41Z"},
         False, "TEST (dry run): the simulation would be started now."),
        ("lower threshold (THRESHOLD_MIN=30)", sim_runs(S + ("2026-11-18T07:53:41Z",)), {"WATCHDOG_NOW": "2026-11-18T09:53:00Z", "THRESHOLD_MIN": "30"},
         True, "overdueMinutes=30 start=yes"),
        ("Sunday: nothing until Monday's first slot", sim_runs(S + ("2026-11-21T22:10:39Z",)), {"WATCHDOG_NOW": "2026-11-22T18:00:00Z"}, False, "dueSlot=2026-11-23T00:23:00Z"),
        ("Monday's first slot missing: starts", sim_runs(S + ("2026-11-21T22:10:39Z",)), {"WATCHDOG_NOW": "2026-11-23T01:37:00Z"}, True, "overdueMinutes=74 start=yes"),
        ("the 26 Nov duplicate: newest failed 30 s after a success, slot not due: nothing",
         sim_runs(F + ("2026-11-26T19:15:34Z",), S + ("2026-11-26T19:15:04Z",)), {"WATCHDOG_NOW": "2026-11-26T20:30:00Z"}, False, "dueSlot=2026-11-26T21:23:00Z"),
    ]
    for name, scen, env, want_start, text in cases:
        rc, out, calls = gh.run("watchdog_check.sh", scen, env)
        s = started(calls, "sim.yml")
        ok = rc == 0 and bool(s) == want_start and text in out and all(c[-2:] == ["--ref", "main"] for c in s)
        check("watchdog: " + name, ok, "rc %d, starts %s, output: %s" % (rc, s, out.strip()[-400:]))


def keeper_scen(created, runs, list_rc=0, own_rc=0, start_seq=(0,)):
    api = {"repos/%s/actions/runs/500" % REPO: ({"body": {"id": 500, "created_at": created}} if own_rc == 0 else {"rc": own_rc}),
           "repos/%s/actions/workflows/keeper.yml/runs?per_page=30" % REPO: (
               {"body": {"workflow_runs": [{"id": i, "status": s, "display_title": t} for i, s, t in runs]}} if list_rc == 0 else {"rc": list_rc})}
    return {"api": api, "run": {"keeper.yml": list(start_seq)}}


def test_keeper_next(gh):
    print("== scripts/keeper_next.sh")
    base = {"RUN_ID": "500", "REF": "main", "KEEPER_NOW": "2026-11-28T10:00:00Z", "KEEPER_RETRY_SLEEP": "0"}
    me = (500, "in_progress", "Keeper")
    cases = [
        # name, scenario, env changes, expected starts of keeper.yml, text
        ("waited 30 minutes, alone: starts the next run on main", keeper_scen("2026-11-28T09:30:00Z", [me]), {}, 1, "Started the next keeper run"),
        ("test run: never starts another (and asks GitHub nothing)", keeper_scen("2026-11-28T09:30:00Z", [me]), {"TEST_RUN": "true"}, 0, "Test run"),
        ("waited 1 minute (timer missing): does not restart", keeper_scen("2026-11-28T09:59:00Z", [me]), {}, 0, "wait timer of the environment 'sim-keeper' is missing"),
        ("waited 9 minutes (timer too short): does not restart", keeper_scen("2026-11-28T09:51:00Z", [me]), {}, 0, "less than 10"),
        ("waited exactly 10 minutes: restarts", keeper_scen("2026-11-28T09:50:00Z", [me]), {}, 1, "waited 10 minutes"),
        ("a newer keeper run is waiting: this one stops", keeper_scen("2026-11-28T09:30:00Z", [(510, "waiting", "Keeper (restarted by the schedule)"), me]), {}, 0,
         "A newer keeper run carries on (510 waiting"),
        ("a newer keeper run is queued: this one stops", keeper_scen("2026-11-28T09:30:00Z", [(511, "queued", "Keeper"), me]), {}, 0, "511 queued"),
        ("a newer TEST run is waiting: ignored, restarts", keeper_scen("2026-11-28T09:30:00Z", [(512, "waiting", "Keeper test run"), me]), {}, 1, "Started the next keeper run"),
        ("an older keeper run is waiting: ignored, restarts", keeper_scen("2026-11-28T09:30:00Z", [me, (490, "waiting", "Keeper")]), {}, 1, "Started the next keeper run"),
        ("a newer run already completed: ignored, restarts", keeper_scen("2026-11-28T09:30:00Z", [(513, "completed", "Keeper"), me]), {}, 1, "Started the next keeper run"),
        ("run list unreadable: restarts anyway and says so", keeper_scen("2026-11-28T09:30:00Z", [me], list_rc=1), {}, 1, "Could not list the keeper's runs"),
        ("own run unreadable: does not restart", keeper_scen("2026-11-28T09:30:00Z", [me], own_rc=1), {}, 0, "Could not tell when this run was created"),
        ("start refused twice, then accepted: restarts on the third try", keeper_scen("2026-11-28T09:30:00Z", [me], start_seq=(1, 1, 0)), {}, 3, "Started the next keeper run"),
        ("start always refused: gives up after three tries and says so", keeper_scen("2026-11-28T09:30:00Z", [me], start_seq=(1,)), {}, 3, "Could not start the next keeper run"),
        ("run id missing: does not restart", keeper_scen("2026-11-28T09:30:00Z", [me]), {"RUN_ID": ""}, 0, "missing or not a number"),
        ("run id not a number: does not restart", keeper_scen("2026-11-28T09:30:00Z", [me]), {"RUN_ID": "1 or true"}, 0, "missing or not a number"),
    ]
    for name, scen, env, want, text in cases:
        rc, out, calls = gh.run("keeper_next.sh", scen, dict(base, **env))
        s = started(calls, "keeper.yml")
        ok = rc == 0 and len(s) == want and text in out and all(c[-2:] == ["--ref", "main"] for c in s)
        if name.startswith("test run"):
            ok = ok and calls == []
        check("keeper: " + name, ok, "rc %d, %d start(s), calls %s, output: %s" % (rc, len(s), calls, out.strip()[-400:]))


FAKE_SIM = r'''#!/usr/bin/env python3
# stand-in for scripts/run_sim.py: works from the state it finds and records which state that was
import hashlib, json, os, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sp = os.path.join(root, "state", "portfolio_state.json")
raw = open(sp, "rb").read()
st = json.loads(raw)
st["runs"] = st.get("runs", 0) + 1
st["basedOn"] = hashlib.sha256(raw).hexdigest()[:12]
st["by"] = os.environ.get("SIM_NAME", "?")
open(sp, "w").write(json.dumps(st, indent=1) + "\n")
open(os.path.join(root, "out", "run_meta.json"), "w").write(json.dumps({"ok": True, "basedOn": st["basedOn"], "by": st["by"]}, indent=1) + "\n")
log = os.environ.get("SIM_LOG")
if log:
    open(log, "a").write("%s %s\n" % (st["by"], st["basedOn"]))
'''


# the commit step of .github/workflows/sim.yml until 28 Nov 2026, word for word (sleep shortened), as a control
OLD_STEP = r'''cd "$(dirname "$0")/.." || exit 1
set -e
git config user.name "fable-prop-sim bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add state out
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "sim run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
for attempt in 1 2 3; do
  if git push; then
    exit 0
  fi
  echo "push failed (attempt $attempt), rebasing on the remote and retrying"
  git pull --rebase --autostash || true
  sleep 0
done
echo "could not push after 3 attempts" >&2
exit 1
'''


def git(cwd, *args, check_rc=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check_rc and p.returncode != 0:
        raise RuntimeError("git %s failed in %s: %s" % (" ".join(args), cwd, p.stderr))
    return p.stdout.strip()


class Repos:
    """origin (bare), seeded with state/, out/, a README and the scripts under test; clones made on demand."""
    def __init__(self, tmp):
        self.tmp = tmp
        self.origin = os.path.join(tmp, "origin.git")
        git(tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:   # refuses the next N pushes when the file fail_next holds N > 0
            f.write('#!/bin/sh\nn=$(cat "$GIT_DIR/fail_next" 2>/dev/null || echo 0)\n'
                    'if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$GIT_DIR/fail_next"; echo "refused (test)" >&2; exit 1; fi\nexit 0\n')
        os.chmod(hook, 0o755)
        seed = self.clone("seed")
        os.makedirs(os.path.join(seed, "state")); os.makedirs(os.path.join(seed, "out")); os.makedirs(os.path.join(seed, "scripts"))
        with open(os.path.join(seed, "state", "portfolio_state.json"), "w") as f:
            f.write(json.dumps({"runs": 0, "basedOn": None, "by": "seed"}, indent=1) + "\n")
        with open(os.path.join(seed, "out", "run_meta.json"), "w") as f:
            f.write("{}\n")
        with open(os.path.join(seed, "README.md"), "w") as f:
            f.write("seed\n")
        shutil.copy(os.path.join(ROOT, "scripts", "commit_run.sh"), os.path.join(seed, "scripts", "commit_run.sh"))
        with open(os.path.join(seed, "scripts", "fake_sim.py"), "w") as f:
            f.write(FAKE_SIM)
        git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "seed"); git(seed, "push", "-q", "origin", "main")

    def clone(self, name):
        d = os.path.join(self.tmp, name)
        git(self.tmp, "clone", "-q", self.origin, d)
        git(d, "config", "user.name", "test"); git(d, "config", "user.email", "test@example.invalid")
        return d

    def fail_next(self, n):
        with open(os.path.join(self.origin, "fail_next"), "w") as f:
            f.write(str(n))

    def head(self):
        return git(self.origin, "rev-parse", "main")

    def log(self):
        return git(self.origin, "log", "--format=%s", "main").splitlines()

    def state_at_head(self):
        return json.loads(git(self.origin, "show", "main:state/portfolio_state.json"))


def sim(clone, name, log=None):
    env = dict(os.environ, SIM_NAME=name)
    if log:
        env["SIM_LOG"] = log
    subprocess.run([sys.executable, "scripts/fake_sim.py"], cwd=clone, env=env, check=True)


def commit_run(clone, name, log, **env_extra):
    env = dict(os.environ, SIM_CMD="python3 scripts/fake_sim.py", SIM_NAME=name, SIM_LOG=log, RETRY_SLEEPS="0", BRANCH="main")
    env.update({k: str(v) for k, v in env_extra.items()})
    p = subprocess.run(["bash", "scripts/commit_run.sh"], cwd=clone, env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def sim_log(log):
    return open(log).read().split() if os.path.exists(log) else []


def test_commit_run(tmp_root):
    print("== scripts/commit_run.sh")
    n = [0]

    def fresh():
        n[0] += 1
        d = os.path.join(tmp_root, "repos%d" % n[0]); os.makedirs(d)
        return Repos(d), os.path.join(d, "sim.log")

    # 1. normal run
    r, log = fresh(); a = r.clone("a"); base = r.head()
    sim(a, "A"); rc, out = commit_run(a, "A", log)
    st = r.state_at_head()
    check("commit: normal run is pushed as one commit", rc == 0 and len(r.log()) == 2 and git(r.origin, "rev-parse", "main~1") == base
          and st["by"] == "A" and sim_log(log) == [], "rc %d log %s out %s" % (rc, r.log(), out))

    # 2. nothing changed
    r, log = fresh(); a = r.clone("a"); base = r.head()
    rc, out = commit_run(a, "A", log)
    check("commit: nothing to commit, nothing pushed", rc == 0 and "nothing to commit" in out and r.head() == base, out)

    # 3. GitHub refuses two pushes (as on 4 Nov 2026), the third goes through
    r, log = fresh(); a = r.clone("a"); base = r.head()
    sim(a, "A"); r.fail_next(2); rc, out = commit_run(a, "A", log)
    check("commit: two refused pushes, pushed on the third, one commit, no re-run",
          rc == 0 and "attempt 3" in out and "GitHub refused the push itself" in out and len(r.log()) == 2 and sim_log(log) == [], out)

    # 4. the 26 Nov 2026 19:15 UTC case: another run saved its result while this one worked from the older commit
    r, log = fresh(); a = r.clone("a"); b = r.clone("b")
    sim(a, "A-stale")                              # A works from the seed state
    sim(b, "B"); git(b, "commit", "-q", "-am", "sim run B"); git(b, "push", "-q", "origin", "main")
    b_head = r.head()
    raw_b = git(r.origin, "show", "main:state/portfolio_state.json") + "\n"
    b_state_hash = hashlib.sha256(raw_b.encode()).hexdigest()[:12]
    rc, out = commit_run(a, "A", log)
    st = r.state_at_head()
    check("commit: 26 Nov case: A's out-of-date result is replaced by a re-run on B's commit, then pushed",
          rc == 0 and git(r.origin, "rev-parse", "main~1") == b_head and st["runs"] == 2 and st["basedOn"] == b_state_hash
          and st["by"] == "A" and sim_log(log) == ["A", b_state_hash] and "re-run 1" in out,
          "rc %d state %s simlog %s out %s" % (rc, st, sim_log(log), out))

    # 5. a code commit (README only) reached the branch first: A's commit goes on top, no re-run
    r, log = fresh(); a = r.clone("a"); b = r.clone("b")
    sim(a, "A")
    with open(os.path.join(b, "README.md"), "a") as f:
        f.write("a code change\n")
    git(b, "commit", "-q", "-am", "docs: code change"); git(b, "push", "-q", "origin", "main"); b_head = r.head()
    rc, out = commit_run(a, "A", log)
    st = r.state_at_head()
    check("commit: a code-only commit first: A's commit is put on top of it, A's result kept, no re-run",
          rc == 0 and git(r.origin, "rev-parse", "main~1") == b_head and st["by"] == "A" and st["runs"] == 1 and sim_log(log) == []
          and "without touching state/ or out/" in out and git(r.origin, "show", "main:README.md").endswith("a code change"), out)

    # 6. a commit that changes both code and the state (as a settings script would): treated as a newer state, re-run
    r, log = fresh(); a = r.clone("a"); b = r.clone("b")
    sim(a, "A")
    with open(os.path.join(b, "README.md"), "a") as f:
        f.write("settings note\n")
    stb = json.load(open(os.path.join(b, "state", "portfolio_state.json"))); stb["setting"] = "changed"
    with open(os.path.join(b, "state", "portfolio_state.json"), "w") as f:
        f.write(json.dumps(stb, indent=1) + "\n")
    git(b, "commit", "-q", "-am", "settings change"); git(b, "push", "-q", "origin", "main"); b_head = r.head()
    rc, out = commit_run(a, "A", log)
    st = r.state_at_head()
    check("commit: a settings commit that touches state/ first: A re-runs on it, the setting survives",
          rc == 0 and git(r.origin, "rev-parse", "main~1") == b_head and st.get("setting") == "changed" and st["runs"] == 1
          and len(sim_log(log)) == 2 and "re-run 1" in out, "rc %d state %s out %s" % (rc, st, out))

    # 7. another run wins every time: gives up after two re-runs, exit 1, nothing of A's pushed
    r, log = fresh(); a = r.clone("a"); b = r.clone("b")
    sim(a, "A")
    sim(b, "B1"); git(b, "commit", "-q", "-am", "sim run B1"); git(b, "push", "-q", "origin", "main")
    racer = os.path.join(r.tmp, "racer.sh")      # every re-run of A loses the race again
    with open(racer, "w") as f:
        f.write('#!/bin/sh\npython3 scripts/fake_sim.py\ncd "%s" && git pull -q --rebase origin main && SIM_NAME=Bx python3 scripts/fake_sim.py '
                '&& git commit -q -am "sim run Bx" && git push -q origin main\n' % b)
    os.chmod(racer, 0o755)
    rc, out = commit_run(a, "A", log, SIM_CMD="sh " + racer)
    st = r.state_at_head()
    check("commit: loses the race every time: gives up after 2 re-runs (exit 1), the other run's result stands",
          rc == 1 and "giving up after 2 re-run(s)" in out and st["by"] == "Bx", "rc %d state %s out %s" % (rc, st, out))

    # 8. GitHub refuses every push: gives up after PUSH_ATTEMPTS, exit 1
    r, log = fresh(); a = r.clone("a"); base = r.head()
    sim(a, "A"); r.fail_next(100); rc, out = commit_run(a, "A", log, PUSH_ATTEMPTS=4)
    check("commit: every push refused: gives up after 4 attempts (exit 1), nothing pushed",
          rc == 1 and "could not push after 4 attempts" in out and r.head() == base, out)

    # 9. another run's commit first, then GitHub refuses the push made after the re-run: waits, pushes again
    r, log = fresh(); a = r.clone("a"); b = r.clone("b")
    sim(a, "A")
    sim(b, "B"); git(b, "commit", "-q", "-am", "sim run B"); git(b, "push", "-q", "origin", "main"); b_head = r.head()
    r.fail_next(1)      # A's first push is turned away by git itself ("fetch first"), so the refusal hits the one after the re-run
    rc, out = commit_run(a, "A", log)
    st = r.state_at_head()
    check("commit: another run first, then a refused push after the re-run: pushed on attempt 3",
          rc == 0 and git(r.origin, "rev-parse", "main~1") == b_head and st["runs"] == 2 and "re-run 1" in out
          and "GitHub refused the push itself" in out and "attempt 3" in out, out)

    # 9b. control: the commit step used until 28 Nov 2026 (sim.yml's inline script) loses A's run in the 26 Nov case
    r, log = fresh(); a = r.clone("a"); b = r.clone("b")
    with open(os.path.join(a, "scripts", "old_commit.sh"), "w") as f:
        f.write(OLD_STEP)
    sim(a, "A-stale")
    sim(b, "B"); git(b, "commit", "-q", "-am", "sim run B"); git(b, "push", "-q", "origin", "main"); b_head = r.head()
    p = subprocess.run(["bash", "scripts/old_commit.sh"], cwd=a, capture_output=True, text=True, timeout=120)
    out = p.stdout + p.stderr
    check("control: the old commit step fails in the 26 Nov case exactly as on GitHub (conflict, 'could not push after 3 attempts')",
          p.returncode == 1 and "could not push after 3 attempts" in out and "CONFLICT" in out and r.head() == b_head, out[-600:])

    # 10. detached HEAD without BRANCH: refuses to guess
    r, log = fresh(); a = r.clone("a"); git(a, "checkout", "-q", "--detach")
    sim(a, "A")
    env = dict(os.environ); env.pop("BRANCH", None)
    p = subprocess.run(["bash", "scripts/commit_run.sh"], cwd=a, env=env, capture_output=True, text=True)
    check("commit: detached HEAD and no BRANCH: exits 1 without pushing", p.returncode == 1 and "no branch to push to" in p.stderr, p.stdout + p.stderr)


def main():
    for tool in ("bash", "git", "jq"):
        if not shutil.which(tool):
            print("missing tool: %s" % tool)
            return 2
    tmp = tempfile.mkdtemp(prefix="backup-tests-")
    try:
        gh = GhStub(os.path.join(tmp, "gh"))
        test_watchdog_check(gh)
        test_keeper_next(gh)
        test_commit_run(os.path.join(tmp, "git"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d failed" % len(FAILS) if FAILS else "all passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.makedirs(tempfile.gettempdir(), exist_ok=True)
    sys.exit(main())
