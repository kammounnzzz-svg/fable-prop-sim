#!/usr/bin/env python3
"""Add track C to the portfolio pack: a fast-pass twin of every evaluation account.

    python3 scripts/add_fast_track.py state/portfolio_state.json

Track A ("challenge") holds the accounts bought as evaluations that the engine passes at its standard evaluation
risk (1.5% per trade in the Challenge, 1.125% in Verification, one daily-leg position at a time).
Track C ("fast-pass") holds the same evaluations pushed harder so they pass sooner: 2.0% per trade in the Challenge
and 1.5% in Verification, still with only ONE daily-leg position at a time plus the metals position. That is the
"concentrated 2%" push from the research of 21 Oct 2026 - the fastest setting that did not breach a firm rule in
either replay period. Nothing else differs: same signals, same firm rulebooks, same day guards and drawdown brakes,
and once an account is funded it runs exactly like tracks A and B (0.75% base risk and the profit-cushion ladder).

The engine itself is not changed: every setting below is a per-account override in the manifest, which the engine
already merges over its defaults on every run. Tracks A and B are not touched. Idempotent: twins that already exist
are left alone. The new states start from the sibling's last processed bars, so no history is replayed and track C
begins flat on the day it is added. Paper trading only."""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fable_prop_sim.engine import account_config, new_state, phase_value, iso, now_utc  # noqa: E402

TRACK = "fast-pass"
SUFFIX = "-fast"
EVAL_RISK = {"challenge": 2.0, "verification": 1.5}           # % of the starting balance per trade (track A: 1.5 / 1.125)
TRACK_LABELS = {"challenge": "A: bought as evaluations, the engine passes them",
                "funded-start": "B: passed by the trader, handed over funded",
                TRACK: "C: bought as evaluations, passed with the faster 2% push"}
NAME = ("Prop portfolio sim (Fable) - track A: evaluations the engine passes; track B: accounts passed by the trader and "
        "handed over funded; track C: evaluations passed with the faster 2% push")


def fast_config(acct):
    """The sibling's own overrides plus the evaluation push. The funded values are copied from what the sibling runs
    today, so the funded stage of track C is identical to tracks A and B whatever the engine's defaults become."""
    base = account_config(acct)                                # the engine's defaults merged with the sibling's overrides
    cfg = copy.deepcopy(acct.get("config", {}))
    funded_risk = phase_value(base["riskPct"], "funded")
    funded_budget = phase_value(base["maxOpenRiskPct"], "funded")
    funded_metals = phase_value(base["strategies"]["intraday"]["riskPct"], "funded")
    cfg["riskPct"] = {"challenge": EVAL_RISK["challenge"], "verification": EVAL_RISK["verification"], "funded": funded_risk}
    # the daily legs' combined open risk equals one trade's risk in the evaluation: one daily position at a time
    cfg["maxOpenRiskPct"] = {"challenge": EVAL_RISK["challenge"], "verification": EVAL_RISK["verification"], "funded": funded_budget}
    cfg.setdefault("strategies", {}).setdefault("intraday", {})["riskPct"] = {
        "challenge": EVAL_RISK["challenge"], "verification": EVAL_RISK["verification"], "funded": funded_metals}
    return cfg


def add_fast_track(pack, stamp=None):
    """Adds the missing fast-pass twins to the pack in place; returns the ids it added."""
    manifest = pack["manifest"]
    existing = {a["id"] for a in manifest["accounts"]}
    added = []
    for acct in list(manifest["accounts"]):
        if acct.get("track", "funded-start" if acct.get("startPhase") == "funded" else "challenge") != "challenge":
            continue
        twin_id = acct["id"] + SUFFIX
        if twin_id in existing:
            continue
        twin = copy.deepcopy(acct)
        twin["id"] = twin_id
        twin["track"] = TRACK
        twin.pop("startPhase", None)
        twin["config"] = fast_config(acct)
        twin["product"] = acct.get("product", acct["id"]) + " - fast-pass evaluation (track C)"
        twin["note"] = ("Track C twin of %s: the same evaluation at 2.0%% per trade in the Challenge and 1.5%% in Verification, one "
                        "daily-leg position at a time (track A: 1.5%% / 1.125%%); the funded stage is identical. %s" % (acct["id"], acct.get("note", ""))).strip()
        manifest["accounts"].append(twin)
        state = new_state(account_config(twin))
        sibling = pack["states"][acct["id"]]
        state["meta"]["lastProcessed"] = copy.deepcopy(sibling["meta"].get("lastProcessed", {}))
        state["meta"]["lastProcessedHourly"] = copy.deepcopy(sibling["meta"].get("lastProcessedHourly", {}))
        if "lastCloses" in sibling["meta"]:
            state["meta"]["lastCloses"] = copy.deepcopy(sibling["meta"]["lastCloses"])
        state["meta"]["note"] = "created %s as the fast-pass twin of %s" % (stamp or iso(now_utc()), acct["id"])
        pack["states"][twin_id] = state
        added.append(twin_id)
    if added or TRACK in {a.get("track") for a in manifest["accounts"]}:
        manifest["name"] = NAME
        labels = dict(manifest.get("tracks") or {})
        for k, v in TRACK_LABELS.items():
            labels.setdefault(k, v)
        labels[TRACK] = TRACK_LABELS[TRACK]
        manifest["tracks"] = {k: labels[k] for k in ["challenge", "funded-start", TRACK] if k in labels}
        manifest["tracks"].update({k: v for k, v in labels.items() if k not in manifest["tracks"]})
    return added


def main(path):
    with open(path) as f:
        pack = json.load(f)
    added = add_fast_track(pack)
    for twin_id in added:
        acct = next(a for a in pack["manifest"]["accounts"] if a["id"] == twin_id)
        cfg = pack["states"][twin_id]["config"]
        print("added %-24s %s %.0f x%d, evaluation risk %.2f%% / %.2f%%" % (twin_id, acct.get("firm", ""), cfg["initial"], acct.get("copies", 1),
              cfg["riskPct"]["challenge"], cfg["riskPct"].get("verification", 0.0)))
    if added:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(pack, f, indent=1)
        os.replace(tmp, path)
    print("%d twin(s) added; %d accounts in the manifest" % (len(added), len(pack["manifest"]["accounts"])))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "state", "portfolio_state.json"))
