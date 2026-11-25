#!/usr/bin/env python3
"""Hourly metals leg: take new entries at any hour, on every account of the portfolio pack.

    python3 scripts/set_metals_hours.py state/portfolio_state.json

Until this change the hourly gold and silver leg queued a new entry only when the signal bar fell outside US hours
(21:00-12:59 UTC). That filter was adopted on 19 Oct 2026 from published work on gold's returns by session and a
two-year test. Sixteen years of hourly data (2011-2026, research notes section 18) showed it was wrong for this rule:
with the filter the leg made nothing over the thirteen years before its design window and about +53R over the sixteen;
without it about +120R, the filter throwing away the leg's best signals (the US data releases and the New York open).
The user approved removing it on 17 Nov 2026 (Sydney time).

The engine itself is not changed: the setting is a per-account override in the manifest
(config.strategies.intraday.entryHoursUTC = null, which the engine reads as "no hours filter") and the engine already
merges the manifest over its defaults on every run. Nothing else differs: the same breakout rule, trend filter, stops,
trailing, sizes, one metal position at a time, firm rulebooks, day guards and drawdown brakes; no short side.

Idempotent: accounts that already carry the override are left alone. Each changed account gets one line in its event
list so the dashboard shows when the setting changed. Paper trading only."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fable_prop_sim.engine import account_config, record_event, iso, now_utc  # noqa: E402

EVENT_TEXT = ("Setting changed: the hourly gold and silver leg now takes new entries at any hour. The rule that skipped "
              "US hours (13:00-20:59 UTC) was removed after a 16-year test showed it cost the leg more than half of its "
              "profit. Nothing else changed.")


def set_any_hour(pack, stamp=None):
    """Sets the override on every manifest row that does not carry it yet, in place; returns the ids it changed."""
    changed = []
    for acct in pack["manifest"]["accounts"]:
        intraday = acct.setdefault("config", {}).setdefault("strategies", {}).setdefault("intraday", {})
        if "entryHoursUTC" in intraday and intraday["entryHoursUTC"] is None:
            continue
        intraday["entryHoursUTC"] = None
        changed.append(acct["id"])
        state = pack["states"].get(acct["id"])
        if state is not None:
            state["config"] = account_config(acct)             # what the next run would set anyway: the manifest is the source of truth
            record_event(state, (stamp or iso(now_utc()))[:10], "config", EVENT_TEXT)
    return changed


def main(path):
    with open(path) as f:
        pack = json.load(f)
    changed = set_any_hour(pack)
    for acct in pack["manifest"]["accounts"]:
        hours = account_config(acct)["strategies"]["intraday"]["entryHoursUTC"]
        print("%-26s %-13s entry hours: %s%s" % (acct["id"], acct.get("track", ""), "any" if not hours else hours, "  (changed now)" if acct["id"] in changed else ""))
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(pack, f, indent=1)
        os.replace(tmp, path)
    print("%d account(s) changed; %d accounts in the manifest" % (len(changed), len(pack["manifest"]["accounts"])))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "state", "portfolio_state.json"))
