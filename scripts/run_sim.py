#!/usr/bin/env python3
"""One scheduled simulation run, driven inside GitHub Actions (or by hand).

    python3 scripts/run_sim.py            # from the repository root

Steps: fetch bars from Yahoo (retry once), run every account in the portfolio pack
(state/portfolio_state.json), export the dashboard datasets to out/, and always write
out/run_meta.json + out/run_log.txt describing what happened. The script never raises
and always exits 0 so that the workflow's commit step runs and the meta file is kept
even when a step failed. Paper trading only: no broker, no real orders.
"""
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state", "portfolio_state.json")
BARS = os.path.join(ROOT, "bars")
OUT = os.path.join(ROOT, "out")
ENGINE = [sys.executable, "-m", "fable_prop_sim"]
ENV = dict(os.environ, PYTHONPATH=ROOT)
NOTHING_NEW = "No new completed bars since the last run"


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def sh(args, timeout=900):
    p = subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=timeout)
    out = p.stdout
    if p.stderr.strip():
        out += "\n[stderr]\n" + p.stderr
    return p.returncode, out


def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=False)
    os.replace(tmp, path)


def main():
    started = now()
    logs = []
    meta = {
        "startedAt": started, "finishedAt": None, "ok": False, "stage": "start", "error": "",
        "nothingNew": None, "fetchFailures": {}, "hourlySymbols": [], "dailySymbols": 0,
        "events": [], "accounts": [], "passFail": [], "focus": None,
        "runId": os.environ.get("GITHUB_RUN_ID", ""), "runNumber": os.environ.get("GITHUB_RUN_NUMBER", ""),
        "baseCommit": os.environ.get("GITHUB_SHA", ""), "runner": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "manual",
    }
    try:
        from fable_prop_sim import __version__  # noqa
        meta["engineVersion"] = __version__
    except Exception:  # noqa
        meta["engineVersion"] = "unknown"
    meta["engineSha256"] = sha256(os.path.join(ROOT, "fable_prop_sim", "engine.py"))
    try:
        os.makedirs(OUT, exist_ok=True)
        pack = load_json(STATE)
        focus = pack["manifest"].get("focus") or pack["manifest"]["accounts"][0]["id"]
        meta["focus"] = focus
        meta["stateSha256Before"] = sha256(STATE)

        # 1. prices (Yahoo chart feed); a partial failure is retried once, the engine skips symbols without new bars
        meta["stage"] = "fetch"
        fetch_cmd = ENGINE + ["fetch", "--out", BARS, "--range", "2y", "--hourly-range", "60d"]
        rc, log = sh(fetch_cmd)
        logs.append("### fetch (exit %d)\n%s" % (rc, log))
        if rc != 0:
            time.sleep(30)
            rc, log = sh(fetch_cmd)
            logs.append("### fetch retry (exit %d)\n%s" % (rc, log))
        allj = load_json(os.path.join(BARS, "all.json"))
        meta["fetchFailures"] = allj.get("failures", {})
        meta["dailySymbols"] = len(allj.get("bars", {}))
        hp = os.path.join(BARS, "hourly.json")
        if os.path.exists(hp):
            meta["hourlySymbols"] = sorted(load_json(hp).get("bars", {}).keys())
        if meta["dailySymbols"] == 0 and not meta["hourlySymbols"]:
            raise RuntimeError("no price bars could be fetched at all: %s" % json.dumps(meta["fetchFailures"])[:800])

        # 2. run every account's engine on the new bars
        meta["stage"] = "run"
        before = {aid: (len(st.get("events", [])), len(st.get("trades", [])), st["account"].get("phase"), st["account"].get("attempt"))
                  for aid, st in pack["states"].items()}
        rc, log = sh(ENGINE + ["portfolio-run", "--states", STATE, "--bars", BARS])
        logs.append("### portfolio-run (exit %d)\n%s" % (rc, log))
        if rc != 0:
            raise RuntimeError("portfolio-run exited %d: %s" % (rc, log[-1500:]))
        lines = log.splitlines()
        meta["nothingNew"] = any(NOTHING_NEW in l for l in lines)
        meta["accounts"] = [l.rstrip() for l in lines if " bal " in l and " eq " in l]
        meta["passFail"] = [l.strip() for l in lines if ("PASSED" in l or "FAILED" in l) and not l.strip().startswith("EVENT")]
        # events and closed trades that this run added, per account (the engine's EVENT lines repeat older order events)
        after = load_json(STATE)
        new_events, new_trades, phase_changes = [], [], []
        for aid, st in after["states"].items():
            n_ev, n_tr, ph, att = before.get(aid, (0, 0, None, None))
            for e in st.get("events", [])[n_ev:]:
                new_events.append({"account": aid, "date": e.get("date"), "kind": e.get("kind"), "text": e.get("text")})
            for t in st.get("trades", [])[n_tr:]:
                new_trades.append(dict(t, account=aid))
            if (st["account"].get("phase"), st["account"].get("attempt")) != (ph, att):
                phase_changes.append({"account": aid, "from": [ph, att], "to": [st["account"].get("phase"), st["account"].get("attempt")]})
        meta["events"] = new_events
        meta["newTrades"] = new_trades
        meta["phaseChanges"] = phase_changes
        meta["lastDate"] = after["states"][focus]["account"].get("lastDate")
        meta["lastHourlyBar"] = after["states"][focus]["account"].get("lastHourlyBar")

        # 3. status (human-readable) and 4. export (dashboard datasets)
        meta["stage"] = "status"
        rc, log = sh(ENGINE + ["portfolio-status", "--states", STATE, "--bars", BARS, "--account", focus])
        logs.append("### portfolio-status (exit %d)\n%s" % (rc, log))
        meta["stage"] = "export"
        rc, log = sh(ENGINE + ["portfolio-export", "--states", STATE, "--bars", BARS, "--out", OUT, "--focus", focus])
        logs.append("### portfolio-export (exit %d)\n%s" % (rc, log))
        if rc != 0:
            raise RuntimeError("portfolio-export exited %d: %s" % (rc, log[-1500:]))
        meta["ok"] = True
        meta["stage"] = "done"
    except Exception as e:  # noqa
        meta["error"] = ("%s: %s" % (type(e).__name__, e))[:2000]
        logs.append("### ERROR at stage %s\n%s" % (meta["stage"], meta["error"]))
    finally:
        meta["finishedAt"] = now()
        try:
            meta["stateSha256"] = sha256(STATE)
        except Exception:  # noqa
            meta["stateSha256"] = None
        try:
            bak = STATE + ".bak"
            if os.path.exists(bak):
                os.remove(bak)
        except Exception:  # noqa
            pass
        os.makedirs(OUT, exist_ok=True)
        save_json(os.path.join(OUT, "run_meta.json"), meta)
        with open(os.path.join(OUT, "run_log.txt"), "w") as f:
            f.write("run started %s, finished %s, ok=%s\n\n" % (meta["startedAt"], meta["finishedAt"], meta["ok"]))
            f.write("\n\n".join(logs))
        # rolling history of run summaries (no logs), newest last
        hist_path = os.path.join(OUT, "run_history.json")
        hist = []
        if os.path.exists(hist_path):
            try:
                hist = load_json(hist_path)
            except Exception:  # noqa
                hist = []
        hist.append({k: meta[k] for k in ("startedAt", "finishedAt", "ok", "stage", "error", "nothingNew", "runId", "engineVersion") if k in meta}
                    | {"events": len(meta["events"]), "fetchFailures": sorted(meta["fetchFailures"].keys())})
        save_json(hist_path, hist[-400:])
        print(json.dumps({k: meta[k] for k in ("startedAt", "finishedAt", "ok", "stage", "error", "nothingNew", "dailySymbols", "hourlySymbols")}, indent=1))
        for l in meta["accounts"]:
            print(l)
        for e in meta["events"]:
            print("EVENT", e.get("account"), e.get("date"), e.get("kind"), e.get("text"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
