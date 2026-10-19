#!/usr/bin/env python3
"""Add track B to the portfolio pack: a funded-from-day-one twin of every evaluation account.

    python3 scripts/add_funded_track.py state/portfolio_state.json

Track A ("challenge") is the existing set of accounts bought as evaluations that the engine has to pass.
Track B ("funded-start") assumes the trader passed each evaluation by hand and handed the funded account to
the engine, which only has to keep it alive and paid. Both tracks trade the same signals, so the dashboard
shows side by side how the engine does before and after the evaluation. Idempotent: twins that already exist
are left alone. The new states start from the sibling's last processed bars, so no history is replayed.
Paper trading only."""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fable_prop_sim.engine import account_config, new_state, start_in_funded, iso, now_utc  # noqa: E402


def main(path):
    with open(path) as f:
        pack = json.load(f)
    manifest = pack["manifest"]
    existing = {a["id"] for a in manifest["accounts"]}
    added = 0
    for acct in list(manifest["accounts"]):
        if acct.get("track", "challenge") != "challenge":
            continue
        acct.setdefault("track", "challenge")
        twin_id = acct["id"] + "-funded"
        if twin_id in existing:
            continue
        twin = copy.deepcopy(acct)
        twin["id"] = twin_id
        twin["track"] = "funded-start"
        twin["startPhase"] = "funded"
        twin["product"] = acct.get("product", acct["id"]) + " - handed over already funded (track B)"
        twin["note"] = "Track B twin of %s: the trader passes the evaluation by hand, the engine runs the funded stage. %s" % (acct["id"], acct.get("note", ""))
        manifest["accounts"].append(twin)
        cfg = account_config(twin)
        state = new_state(cfg)
        start_in_funded(state, twin)
        sibling = pack["states"][acct["id"]]
        state["meta"]["lastProcessed"] = copy.deepcopy(sibling["meta"].get("lastProcessed", {}))
        state["meta"]["lastProcessedHourly"] = copy.deepcopy(sibling["meta"].get("lastProcessedHourly", {}))
        state["meta"]["note"] = "created %s as the funded-start twin of %s" % (iso(now_utc()), acct["id"])
        pack["states"][twin_id] = state
        added += 1
        print("added %-24s %s %.0f x%d, funded from day one" % (twin_id, twin.get("firm", ""), cfg["initial"], twin.get("copies", 1)))
    manifest["name"] = "Prop portfolio sim (Fable) - track A: evaluations the engine passes; track B: accounts passed by the trader and handed over funded"
    manifest["tracks"] = {"challenge": "A: bought as evaluations, the engine passes them", "funded-start": "B: passed by the trader, handed over funded"}
    with open(path, "w") as f:
        json.dump(pack, f, indent=1)
    print("%d twin(s) added; %d accounts in the manifest" % (added, len(manifest["accounts"])))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "state", "portfolio_state.json"))
