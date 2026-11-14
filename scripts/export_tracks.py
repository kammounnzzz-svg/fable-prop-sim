#!/usr/bin/env python3
"""Strategy-level export for the dashboard: writes ONE file, out/tracks.json.

    python3 scripts/export_tracks.py            # from the repository root, after the engine's own export

The engine keeps one simulated account per firm programme. This script adds nothing to the trading: it only READS the
saved state (state/portfolio_state.json) and summarises the whole strategy per track, so the dashboard can show a
"strategy focus" next to the single-account focus:

  track "challenge"     (A)  the accounts bought as evaluations, which the engine has to pass
  track "funded-start"  (B)  the same accounts handed over already funded, which the engine keeps alive and paid

READ ONLY. It never writes the state, never places, changes or cancels a trade, and is not part of the engine. If it
fails, the simulation run is unaffected (the driver records the failure and carries on). Paper trading only.

How the numbers are made
  * Strategy P&L of one account = everything its trades made or lost since the start, carried across phase resets
    and payouts: (trading result banked by every finished phase or attempt) + (rewards withdrawn) + (current equity
    - starting balance). A phase pass or a failed attempt puts the account's balance back to its starting size and a
    reward takes profit out of the account; neither is a trading loss or gain, so the curve does not jump there.
    A track's P&L is the sum over its accounts, each counted as many times as the firm allows identical copies.
  * One signal = the same entry (market, side, leg, entry date, entry price) taken by one or more accounts. Its
    result is the sum over those accounts; its R is that net result divided by the risk they took at entry. A signal
    counts as closed once no account still holds it. Win = net result above zero.
  * Drawdown is measured on the end-of-day strategy P&L curve of the track, from its highest point so far.
  * Everything that describes an account right now (room to the firm's loss limits, progress to the phase target,
    fees, rewards) is recomputed here with the engine's own formulas and compared with the engine's export of the
    same run (out/portfolio.json, out/psummary.json); any difference is reported in the selfCheck column.
"""
import argparse
import collections
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TRACKS = ["challenge", "funded-start"]
TRACK_LETTER = {"challenge": "A", "funded-start": "B"}
TRACK_LABEL = {"challenge": "Track A: bought as evaluations, the engine has to pass them",
               "funded-start": "Track B: passed by the trader, handed over already funded"}
# the engine's legs, in the order the dashboard lists them; a leg with no signals yet still gets its row
LEGS = [("intraday", "Hourly metals breakout"), ("mr", "Dip-buys (RSI-2)"), ("tom", "Turn of month"), ("crypto", "Crypto breakout")]
LEG_NAME = dict(LEGS)
LEG_NAME.update({"trend": "Daily trend breakout (retired)", "pullback": "Pullback (retired)"})

COLUMNS = [
    "key", "section", "track",
    # identity
    "label", "letter", "id", "name", "firm", "program", "copies", "size", "capital", "accounts", "programs", "firms",
    "startDate", "lastDate", "days", "date", "month", "n",
    # what was traded
    "kind", "symbol", "side", "leg", "strat", "legs", "setup", "entryDate", "exitDate", "holdDays", "entry", "exit", "price",
    "programsN", "accountsN", "riskUSD", "r", "result", "reason", "stage", "note",
    # profit and loss
    "pnlUSD", "pnlPct", "closedUSD", "floatUSD", "dayUSD", "dayPct", "phaseNetUSD", "netUSD",
    # signal statistics
    "signals", "wins", "losses", "winRatePct", "avgR", "avgWinR", "avgLossR", "payoff", "profitFactor", "expectancyUSD",
    "bestUSD", "worstUSD", "avgHoldDays", "fills",
    # risk
    "maxDdUSD", "maxDdPct", "ddUSD", "ddPct", "peakUSD", "bestDayUSD", "worstDayUSD", "upDays", "downDays",
    "openSignals", "openRiskUSD", "queuedSignals",
    # progress
    "phaseKey", "attempt", "fundedAccounts", "evalAccounts", "stepsDone", "stepsTotal", "progressPct",
    "bestProgressPct", "bestProgressName", "worstProgressPct", "worstProgressName",
    "cushionPct", "bufferPct", "bestCushionPct", "bestCushionName",
    "maxLeftPct", "maxLeftName", "dailyLeftPct", "dailyLeftName", "tradingDays", "minTradingDays", "daysMet",
    "phasesPassed", "attemptsFailed", "feesUSD", "payoutsUSD", "payoutsCount", "cashUSD",
    # events on the curve and the self-check
    "events", "eventKind", "checkUSD", "selfCheck",
]


def load_json(path):
    with open(path) as f:
        return json.load(f)


def r2(x):
    v = round(float(x), 2)
    return 0.0 if v == 0 else v          # never "-0.0"


def rn(x, n):
    v = round(float(x), n)
    return 0.0 if v == 0 else v


def pdate(s):
    return dt.date.fromisoformat(s[:10])


def track_of(acct):
    """Same rule as the engine's portfolio_rows."""
    return acct.get("track", "funded-start" if acct.get("startPhase") == "funded" else "challenge")


def phase_targets(cfg):
    t = cfg.get("phaseTargetsPct")
    if not t:
        t = [cfg["challengeTargetPct"], cfg["verificationTargetPct"]]
    return list(t)


def phase_names(cfg):
    return ["challenge", "verification", "funded"] if len(phase_targets(cfg)) >= 2 else ["challenge", "funded"]


def signal_key(x):
    """The same entry in different accounts: market, side, leg, entry date and entry price."""
    return (x["symbol"], x["side"], x.get("strat", "trend"), x["entryDate"], "%.8g" % float(x["entry"]))


def modal(values):
    """Most common value; ties go to the one seen first."""
    c = collections.Counter(values)
    best = max(c.values())
    for v in values:
        if c[v] == best:
            return v


# ----------------------------------------------------------------------------------------------------------------
# one account
# ----------------------------------------------------------------------------------------------------------------
def account_view(acct, state):
    """Everything this export needs from one account's saved state. Reads only."""
    cfg, acc = state["config"], state["account"]
    initial = float(cfg["initial"])
    copies = int(acct.get("copies", 1))
    hist = acc.get("phaseHistory", [])
    payouts = acc.get("payouts", [])
    points = state.get("equity", [])

    # trading result banked by the phases/attempts that ended before each (attempt, phase) segment began
    seg_base = {}
    run = 0.0
    for h in hist:
        seg_base.setdefault((h["attempt"], h["phase"]), run)
        run += float(h["balance"]) - initial
    base_all = run
    paid_all = sum(float(p["profit"]) for p in payouts)

    # walk the equity points in the order the engine wrote them; several points can carry the same date (bars of one
    # session arrive at different times) and a late bar can carry an earlier date, so a point belongs to the latest
    # date seen so far and the last point of a date is that date's end-of-day value
    tagged = sum(1 for p in points if p.get("event") == "payout")
    by_tag = tagged == len(payouts)
    paid = 0.0
    pi = 0
    cur = ""
    series = collections.OrderedDict()            # date -> (strategy P&L with open trades, closed P&L)
    for p in points:
        if p["date"] > cur:
            cur = p["date"]
        base = seg_base.get((p["attempt"], p["phase"]), base_all)
        if not by_tag:                            # never seen with engine 0.8.0; kept so a curve is still drawn
            paid = sum(float(x["profit"]) for x in payouts if x["date"] < cur)
        series[cur] = (base + float(p["equity"]) - initial + paid, base + float(p["balance"]) - initial + paid)
        if by_tag and p.get("event") == "payout" and pi < len(payouts):
            paid += float(payouts[pi]["profit"])
            pi += 1
    s_now = base_all + r2(acc["lastEquity"]) - initial + paid_all     # cents, like the engine's own equity points
    b_now = base_all + r2(acc["balance"]) - initial + paid_all
    # the newest value is the account as it stands now (hourly bars move equity between daily points)
    hourly_date = (acc.get("lastHourlyBar") or "")[:10]
    live = max([d for d in (cur, hourly_date) if d] or [""])
    if live:
        series[live] = (s_now, b_now)

    events = []
    for h in hist:
        events.append((h["end"], "pass" if h["result"] == "passed" else "fail", "%s %s" % (h["result"], h["phase"])))
    for p in payouts:
        events.append((p["date"], "payout", "paid a reward of %s" % format(float(p["trader"]), ",.0f")))

    trades = state.get("trades", [])
    positions = state.get("positions", [])
    closed_net = sum(float(t["netUSD"]) for t in trades)
    floating = r2(acc["lastEquity"]) - r2(acc["balance"])
    open_comm = sum(float(p.get("commUSD", 0.0)) for p in positions)
    check = s_now - (closed_net + floating - open_comm)
    tol = 0.02 * (len(trades) + len(hist) + len(payouts) + 1)

    # where the account stands, with the engine's own formulas (compared with the engine's export further down)
    names = phase_names(cfg)
    targets = phase_targets(cfg)
    phase = acc["phase"]
    funded = phase == "funded"
    step = names.index(phase) if phase in names else 0
    balance, equity = float(acc["balance"]), float(acc["lastEquity"])
    if funded:
        target = None
        progress = None
    else:
        target = initial * (1 + targets[min(step, len(targets) - 1)] / 100)
        progress = round((balance - initial) / (target - initial) * 100, 1)
    max_allow = initial * float(cfg["maxLossPct"]) / 100
    max_room = equity - (initial - max_allow)
    if cfg.get("maxDailyLossPct") is None:
        daily_allow = None
        daily_room = None
    else:
        daily_allow = initial * float(cfg["maxDailyLossPct"]) / 100
        daily_room = equity - (float(acc["dayStartBalance"]) - daily_allow)
    thr = initial * cfg.get("profitableDayPct", 0.5) / 100
    profitable_days = sum(1 for v in acc.get("realizedByDate", {}).values() if v >= thr)
    days_met = len(acc.get("tradingDays", [])) >= cfg.get("minTradingDays", 0) and profitable_days >= cfg.get("minProfitableDays", 0)
    lad = cfg.get("fundedLadder") or {}
    buffer_pct = float(lad.get("keepBufferPct", 0.0)) if lad.get("enabled") else 0.0
    if len(targets) >= 2:
        stage = {"challenge": "Evaluation step 1 of 2", "verification": "Evaluation step 2 of 2", "funded": "Funded"}.get(phase, phase)
    else:
        stage = {"challenge": "Evaluation (single step)", "funded": "Funded"}.get(phase, phase)

    # closed signals of this account alone (for its row in the accounts table)
    open_keys = set(signal_key(p) for p in positions)
    own = collections.OrderedDict()
    for t in trades:
        k = signal_key(t)
        s = own.setdefault(k, {"net": 0.0, "risk": 0.0})
        s["net"] += float(t["netUSD"])
        s["risk"] += float(t.get("riskUSD") or 0.0)
    own_closed = [{"netUSD": s["net"], "r": (s["net"] / s["risk"]) if s["risk"] else 0.0} for k, s in own.items() if k not in open_keys]

    # the account's own worst peak-to-trough fall of its strategy P&L, as a share of its size
    peak = 0.0
    maxdd = 0.0
    for v, _ in series.values():
        peak = max(peak, v)
        maxdd = min(maxdd, v - peak)

    return {
        "id": acct["id"], "acct": acct, "state": state, "copies": copies, "initial": initial, "capital": initial * copies,
        "firm": acct.get("firm", ""), "program": acct.get("program", ""), "name": ("%s %s" % (acct.get("firm", ""), acct.get("program", ""))).strip(),
        "series": series, "sNow": s_now, "bNow": b_now, "events": events, "check": check, "checkTol": tol, "payoutTags": by_tag,
        "trades": trades, "positions": positions, "orders": state.get("orders", []), "closedNet": closed_net,
        "phase": phase, "attempt": int(acc["attempt"]), "funded": funded, "step": step, "steps": len(targets), "stage": stage,
        "balance": balance, "equity": equity, "progress": progress,
        "maxRoom": max_room, "maxLeftPct": max(0.0, min(100.0, max_room / max_allow * 100)) if max_allow else None,
        "dailyRoom": daily_room, "dailyLeftPct": None if daily_allow is None else max(0.0, min(100.0, daily_room / daily_allow * 100)),
        "tradingDays": len(acc.get("tradingDays", [])), "minTradingDays": cfg.get("minTradingDays", 0), "daysMet": days_met,
        "cushionPct": (balance - initial) / initial * 100, "bufferPct": buffer_pct,
        "passed": len([h for h in hist if h["result"] == "passed"]), "failed": len([h for h in hist if h["result"] == "failed"]),
        "feesUSD": float(acc.get("feesPaidEUR", 0.0)) * float(acct.get("feeToUSD", 1.0)) * copies,
        "payoutsUSD": sum(float(p["trader"]) for p in payouts) * copies, "payoutsCount": len(payouts) * copies,
        "openRisk": sum(float(p["riskUSD"]) for p in positions if not p.get("beDone")),
        "ownClosed": own_closed, "maxDdPct": maxdd / initial * 100,
    }


# ----------------------------------------------------------------------------------------------------------------
# signals
# ----------------------------------------------------------------------------------------------------------------
def collect_signals(views):
    """Group every trade and open position of a track's accounts by the entry they share."""
    sigs = collections.OrderedDict()

    def get(x):
        k = signal_key(x)
        if k not in sigs:
            sigs[k] = {"key": k, "symbol": x["symbol"], "side": x["side"], "strat": x.get("strat", "trend"), "setup": x.get("setup", ""),
                       "entryDate": x["entryDate"], "entry": float(x["entry"]), "pieces": [], "open": [], "programs": [], "net": 0.0, "risk": 0.0, "fills": 0}
        return sigs[k]

    for v in views:
        for t in v["trades"]:
            s = get(t)
            s["pieces"].append((v, t))
            s["net"] += v["copies"] * float(t["netUSD"])
            s["risk"] += v["copies"] * float(t.get("riskUSD") or 0.0)
            s["fills"] += v["copies"]
            if v["id"] not in s["programs"]:
                s["programs"].append(v["id"])
        for p in v["positions"]:
            s = get(p)
            s["open"].append((v, p))
            s["risk"] += v["copies"] * float(p.get("riskUSD") or 0.0)
            if v["id"] not in s["programs"]:
                s["programs"].append(v["id"])
    out = []
    for s in sigs.values():
        s["closed"] = bool(s["pieces"]) and not s["open"]
        s["r"] = (s["net"] / s["risk"]) if s["risk"] else 0.0
        if s["pieces"]:
            finals = [t for _, t in s["pieces"] if not str(t.get("id", "")).endswith("a")] or [t for _, t in s["pieces"]]
            s["exitDate"] = max(t["exitDate"] for _, t in s["pieces"])
            s["exit"] = float(modal([t["exit"] for t in finals]))
            reasons = [t.get("reason", "") for t in finals]
            top = modal(reasons)
            others = len(set(reasons)) - 1
            s["reason"] = top + (" (and %d other exit%s)" % (others, "" if others == 1 else "s") if others > 0 else "")
            s["holdDays"] = (pdate(s["exitDate"]) - pdate(s["entryDate"])).days
        out.append(s)
    return out


def sig_stats(sigs):
    """Statistics of a list of closed signals (the engine's own convention: a result of exactly zero counts as a loss)."""
    n = len(sigs)
    out = {"signals": n, "wins": 0, "losses": 0, "winRatePct": None, "avgR": None, "avgWinR": None, "avgLossR": None, "payoff": None,
           "profitFactor": None, "netUSD": 0.0, "expectancyUSD": None, "bestUSD": None, "worstUSD": None, "avgHoldDays": None}
    if not n:
        return out
    wins = [s for s in sigs if s["net"] > 0]
    losses = [s for s in sigs if s["net"] <= 0]
    gw = sum(s["net"] for s in wins)
    gl = -sum(s["net"] for s in losses)
    net = sum(s["net"] for s in sigs)
    out.update({"wins": len(wins), "losses": len(losses), "winRatePct": rn(len(wins) / n * 100, 1), "avgR": rn(sum(s["r"] for s in sigs) / n, 2),
                "netUSD": r2(net), "expectancyUSD": r2(net / n), "bestUSD": r2(max(s["net"] for s in sigs)), "worstUSD": r2(min(s["net"] for s in sigs))})
    if wins:
        out["avgWinR"] = rn(sum(s["r"] for s in wins) / len(wins), 2)
    if losses:
        out["avgLossR"] = rn(sum(s["r"] for s in losses) / len(losses), 2)
    if wins and losses and sum(s["r"] for s in losses) < 0:
        out["payoff"] = rn((sum(s["r"] for s in wins) / len(wins)) / abs(sum(s["r"] for s in losses) / len(losses)), 2)
    if gl > 0:
        out["profitFactor"] = rn(gw / gl, 2)
    held = [s["holdDays"] for s in sigs if s.get("holdDays") is not None]
    if held:
        out["avgHoldDays"] = rn(sum(held) / len(held), 1)
    return out


# ----------------------------------------------------------------------------------------------------------------
# open positions: the engine's own read-only marks (optional)
# ----------------------------------------------------------------------------------------------------------------
def engine_marks(views, root, use_engine=True):
    """{account id: {position id: the engine's positions_rows row}} marked at the last closes the engine processed,
    or (None, reason) when the engine module cannot be used. Nothing is written; the states are only read."""
    if not use_engine:
        return None, "engine marks switched off"
    if not any(v["positions"] for v in views):
        return {}, ""
    try:
        if root not in sys.path:
            sys.path.insert(0, root)
        from fable_prop_sim import engine as E
        out = {}
        for v in views:
            if not v["positions"]:
                continue
            closes = {s: c["c"] for s, c in v["state"].get("meta", {}).get("lastCloses", {}).items() if isinstance(c, dict) and "c" in c}
            out[v["id"]] = {r["id"]: r for r in E.positions_rows(v["state"], closes)}
        return out, ""
    except Exception as e:  # noqa
        return None, "open-position marks unavailable (%s: %s)" % (type(e).__name__, str(e)[:120])


# ----------------------------------------------------------------------------------------------------------------
# one track
# ----------------------------------------------------------------------------------------------------------------
def track_rows(track, views, marks, engine_rows, engine_summary):
    letter = TRACK_LETTER.get(track, track)
    capital = sum(v["capital"] for v in views)
    accounts = sum(v["copies"] for v in views)
    rows = []
    notes = []

    def key(section, *parts):
        return ":".join([section, track] + [str(p) for p in parts])

    # ---- curve -------------------------------------------------------------------------------------------------
    dates = sorted(set(d for v in views for d in v["series"]))
    ev_by_date = collections.OrderedDict()
    for v in views:
        for d, kind, text in v["events"]:
            ev_by_date.setdefault(d, []).append((kind, "%s%s %s%s" % (v["name"], " x%d" % v["copies"] if v["copies"] > 1 else "", text, " each" if kind == "payout" and v["copies"] > 1 else "")))
    last = {v["id"]: (0.0, 0.0) for v in views}
    curve = []
    peak = 0.0
    prev = 0.0
    for d in dates:
        tot = 0.0
        closed = 0.0
        for v in views:
            if d in v["series"]:
                last[v["id"]] = v["series"][d]
            s, b = last[v["id"]]
            tot += v["copies"] * s
            closed += v["copies"] * b
        peak = max(peak, tot)
        curve.append({"date": d, "pnl": tot, "closed": closed, "day": tot - prev, "dd": tot - peak})
        prev = tot
    # events dated between two curve dates land on the next curve date (or the last one)
    ev_on = collections.OrderedDict()
    for d, items in ev_by_date.items():
        target = next((c["date"] for c in curve if c["date"] >= d), curve[-1]["date"] if curve else d)
        ev_on.setdefault(target, []).extend(items)
    for c in curve:
        items = ev_on.get(c["date"], [])
        kinds = [k for k, _ in items]
        texts = [t for _, t in items]
        counts = collections.Counter(texts)                     # the same event more than once on a date is written once, with its count
        texts = ["%s%s" % (t, " (%d times)" % counts[t] if counts[t] > 1 else "") for t in collections.OrderedDict.fromkeys(texts)]
        row = {"key": key("equity", c["date"]), "section": "equity", "track": track, "date": c["date"], "pnlUSD": r2(c["pnl"]),
               "pnlPct": rn(c["pnl"] / capital * 100, 2) if capital else 0.0, "closedUSD": r2(c["closed"]), "dayUSD": r2(c["day"]),
               "ddUSD": r2(c["dd"]), "ddPct": rn(c["dd"] / capital * 100, 2) if capital else 0.0,
               "events": ("; ".join(texts[:4]) + ("; and %d more" % (len(texts) - 4) if len(texts) > 4 else "")) if texts else None,
               "eventKind": ("fail" if "fail" in kinds else "pass" if "pass" in kinds else "payout") if kinds else None}
        rows.append(row)

    # ---- signals -----------------------------------------------------------------------------------------------
    sigs = collect_signals(views)
    by_id = {v["id"]: v for v in views}
    for s in sigs:
        s["accountsN"] = sum(by_id[i]["copies"] for i in s["programs"])
    closed_sigs = sorted([s for s in sigs if s["closed"]], key=lambda s: (s["exitDate"], s["entryDate"], s["symbol"], s["side"], s["strat"], s["entry"]))
    open_sigs = [s for s in sigs if s["open"]]
    st = sig_stats(closed_sigs)
    for n, s in enumerate(closed_sigs, 1):
        rows.append({"key": key("signal", n), "section": "signal", "track": track, "n": n, "symbol": s["symbol"], "side": s["side"],
                     "leg": LEG_NAME.get(s["strat"], s["strat"]), "strat": s["strat"], "setup": s["setup"], "entryDate": s["entryDate"], "exitDate": s["exitDate"],
                     "holdDays": s["holdDays"], "entry": s["entry"], "exit": s["exit"], "programsN": len(s["programs"]), "accountsN": s["accountsN"],
                     "riskUSD": r2(s["risk"]), "netUSD": r2(s["net"]), "r": rn(s["r"], 2), "result": "win" if s["net"] > 0 else "loss", "reason": s["reason"]})

    # ---- legs, markets, months -----------------------------------------------------------------------------------
    strats = [k for k, _ in LEGS] + sorted(set(s["strat"] for s in sigs) - set(k for k, _ in LEGS))
    for k in strats:
        ls = sig_stats([s for s in closed_sigs if s["strat"] == k])
        row = {"key": key("leg", k), "section": "leg", "track": track, "leg": LEG_NAME.get(k, k), "strat": k,
               "openSignals": len([s for s in open_sigs if s["strat"] == k])}
        row.update({f: ls[f] for f in ("signals", "wins", "losses", "winRatePct", "avgR", "netUSD", "profitFactor", "bestUSD", "worstUSD", "avgHoldDays")})
        rows.append(row)
    for sym in sorted(set(s["symbol"] for s in closed_sigs)):
        own = [s for s in closed_sigs if s["symbol"] == sym]
        ms = sig_stats(own)
        row = {"key": key("symbol", sym), "section": "symbol", "track": track, "symbol": sym,
               "legs": ", ".join(LEG_NAME.get(k, k) for k in sorted(set(s["strat"] for s in own), key=lambda k: strats.index(k)))}
        row.update({f: ms[f] for f in ("signals", "wins", "losses", "winRatePct", "avgR", "netUSD", "profitFactor")})
        rows.append(row)
    months = collections.OrderedDict()
    prev_end = 0.0
    for c in curve:
        m = months.setdefault(c["date"][:7], {"days": [], "start": prev_end})
        m["days"].append(c)
        prev_end = c["pnl"]
    for mkey, m in months.items():
        ms = sig_stats([s for s in closed_sigs if s["exitDate"][:7] == mkey])
        pnl = m["days"][-1]["pnl"] - m["start"]
        row = {"key": key("month", mkey), "section": "month", "track": track, "month": mkey, "pnlUSD": r2(pnl),
               "pnlPct": rn(pnl / capital * 100, 2) if capital else 0.0, "days": len(m["days"]),
               "bestDayUSD": r2(max(c["day"] for c in m["days"])), "worstDayUSD": r2(min(c["day"] for c in m["days"]))}
        row.update({f: ms[f] for f in ("signals", "wins", "losses", "winRatePct", "netUSD")})
        rows.append(row)

    # ---- open now ----------------------------------------------------------------------------------------------
    n = 0
    for s in sorted(open_sigs, key=lambda s: (s["entryDate"], s["symbol"], s["side"], s["strat"], s["entry"])):
        n += 1
        risk_open = sum(v["copies"] * float(p.get("riskUSD") or 0.0) for v, p in s["open"])
        stages = ["trailing" if p.get("trailOn") else ("breakeven" if p.get("beDone") else "initial") for _, p in s["open"]]
        fl = None
        px = None
        if marks is not None:
            got = [(v, marks.get(v["id"], {}).get(p["id"])) for v, p in s["open"]]
            if all(m is not None for _, m in got):
                fl = sum(v["copies"] * float(m["pnlUSD"]) for v, m in got)
                px = float(modal([m["price"] for _, m in got]))
        held = sorted(set(v["id"] for v, _ in s["open"]))
        rows.append({"key": key("open", n), "section": "open", "track": track, "n": n, "kind": "position", "symbol": s["symbol"], "side": s["side"],
                     "leg": LEG_NAME.get(s["strat"], s["strat"]), "strat": s["strat"], "setup": s["setup"],
                     "entryDate": modal([p.get("entryTime") or p["entryDate"] for _, p in s["open"]]), "entry": s["entry"], "price": px,
                     "programsN": len(held), "accountsN": sum(by_id[i]["copies"] for i in held), "riskUSD": r2(risk_open),
                     "floatUSD": None if fl is None else r2(fl), "r": None if fl is None or not risk_open else rn(fl / risk_open, 2),
                     "closedUSD": r2(s["net"]), "stage": modal(stages), "note": modal([p.get("exitNextOpen") or "" for _, p in s["open"]]) or None})
    orders = collections.OrderedDict()
    for v in views:
        for o in v["orders"]:
            k = (o["symbol"], o["side"], o.get("strat", "trend"), o.get("createdDate", ""))
            g = orders.setdefault(k, {"o": o, "programs": [], "risk": 0.0})
            if v["id"] not in g["programs"]:
                g["programs"].append(v["id"])
            g["risk"] += v["copies"] * float(o.get("riskUSD") or 0.0)
    for k in sorted(orders):
        g = orders[k]
        o = g["o"]
        n += 1
        rows.append({"key": key("open", n), "section": "open", "track": track, "n": n, "kind": "order", "symbol": o["symbol"], "side": o["side"],
                     "leg": LEG_NAME.get(o.get("strat", "trend"), o.get("strat", "trend")), "strat": o.get("strat", "trend"), "setup": o.get("setup", ""),
                     "entryDate": o.get("createdDate"), "entry": o.get("signalClose"), "price": None, "programsN": len(g["programs"]),
                     "accountsN": sum(by_id[i]["copies"] for i in g["programs"]), "riskUSD": r2(g["risk"]), "floatUSD": None, "r": None, "closedUSD": None,
                     "stage": "queued", "note": "fills at the next hourly open" if o.get("tf") == "1h" else "fills at the next open"})

    # ---- accounts ----------------------------------------------------------------------------------------------
    eng = {r.get("id"): r for r in (engine_rows or []) if isinstance(r, dict)}
    for v in views:
        os_ = sig_stats([{"net": s["netUSD"], "r": s["r"], "holdDays": None} for s in v["ownClosed"]])
        rows.append({"key": key("account", v["id"]), "section": "account", "track": track, "id": v["id"], "name": v["name"], "firm": v["firm"], "program": v["program"],
                     "copies": v["copies"], "size": v["initial"], "capital": v["capital"], "stage": v["stage"], "phaseKey": v["phase"], "attempt": v["attempt"],
                     "pnlUSD": r2(v["copies"] * v["sNow"]), "pnlPct": rn(v["sNow"] / v["initial"] * 100, 2),
                     "signals": os_["signals"], "wins": os_["wins"], "losses": os_["losses"], "winRatePct": os_["winRatePct"], "avgR": os_["avgR"],
                     "netUSD": r2(v["copies"] * v["closedNet"]), "maxDdPct": rn(v["maxDdPct"], 2), "progressPct": v["progress"],
                     "cushionPct": rn(v["cushionPct"], 2) if v["funded"] else None,
                     "maxLeftPct": None if v["maxLeftPct"] is None else rn(v["maxLeftPct"], 1), "dailyLeftPct": None if v["dailyLeftPct"] is None else rn(v["dailyLeftPct"], 1),
                     "tradingDays": v["tradingDays"], "minTradingDays": v["minTradingDays"], "phasesPassed": v["passed"], "attemptsFailed": v["failed"],
                     "feesUSD": r2(v["feesUSD"]), "payoutsUSD": r2(v["payoutsUSD"]), "openSignals": len(v["positions"])})
        # compare with the engine's own export of the same run, where it is available
        e = eng.get(v["id"])
        if e is not None:
            for mine, theirs, tol, what in ((v["balance"], e.get("balance"), 0.011, "balance"), (v["equity"], e.get("equity"), 0.011, "equity"),
                                            (v["maxRoom"], e.get("maxRoom"), 0.011, "max-loss room"), (v["dailyRoom"], e.get("dailyRoom"), 0.011, "daily-loss room"),
                                            (v["progress"], e.get("progressPct"), 0.051, "progress"), (v["tradingDays"], e.get("tradingDays"), 0, "trading days"),
                                            (v["feesUSD"], e.get("feesPaid"), 0.02 * v["copies"], "fees"), (v["payoutsUSD"], e.get("payouts"), 0.02 * v["copies"], "rewards"),
                                            (v["failed"], e.get("attemptsFailed"), 0, "failed attempts"), (v["passed"], e.get("phasesPassed"), 0, "phases passed")):
                if (mine is None) != (theirs is None) or (mine is not None and abs(float(mine) - float(theirs)) > tol):
                    notes.append("%s of %s differs from the engine's export (%s vs %s)" % (what, v["id"], "none" if mine is None else "%.2f" % mine, "none" if theirs is None else "%.2f" % float(theirs)))
        if abs(v["check"]) > v["checkTol"]:
            notes.append("curve and trade list of %s differ by %.2f" % (v["id"], v["check"]))
        if not v["payoutTags"]:
            notes.append("reward markers of %s do not match its reward list" % v["id"])

    # ---- the track's one row of statistics and progress ----------------------------------------------------------
    ev = [v for v in views if not v["funded"]]
    fu = [v for v in views if v["funded"]]
    ev_cap = sum(v["capital"] for v in ev)
    fu_cap = sum(v["capital"] for v in fu)
    best = max(ev, key=lambda v: v["progress"]) if ev else None
    worst = min(ev, key=lambda v: v["progress"]) if ev else None
    bestc = max(fu, key=lambda v: v["cushionPct"]) if fu else None
    tight = min([v for v in views if v["maxLeftPct"] is not None], key=lambda v: v["maxLeftPct"], default=None)
    tightd = min([v for v in views if v["dailyLeftPct"] is not None], key=lambda v: v["dailyLeftPct"], default=None)
    pnl_now = curve[-1]["pnl"] if curve else 0.0
    closed_now = curve[-1]["closed"] if curve else 0.0
    phase_net = sum(v["copies"] * (r2(v["equity"]) - v["initial"]) for v in views)
    check = sum(v["copies"] * v["check"] for v in views)
    fees = sum(v["feesUSD"] for v in views)
    paid = sum(v["payoutsUSD"] for v in views)
    stats = {"key": key("stats"), "section": "stats", "track": track, "label": TRACK_LABEL.get(track, track), "letter": letter,
             "accounts": accounts, "programs": len(views), "firms": len(set(v["firm"] for v in views)), "capital": r2(capital),
             "startDate": curve[0]["date"] if curve else None, "lastDate": curve[-1]["date"] if curve else None, "days": len(curve),
             "pnlUSD": r2(pnl_now), "pnlPct": rn(pnl_now / capital * 100, 2) if capital else 0.0, "closedUSD": r2(closed_now), "floatUSD": r2(pnl_now - closed_now),
             "dayUSD": r2(curve[-1]["day"]) if curve else 0.0, "dayPct": rn(curve[-1]["day"] / capital * 100, 2) if curve and capital else 0.0,
             "phaseNetUSD": r2(phase_net),
             "fills": sum(s["fills"] for s in sigs),
             "maxDdUSD": r2(min([c["dd"] for c in curve] or [0.0])), "maxDdPct": rn(min([c["dd"] for c in curve] or [0.0]) / capital * 100, 2) if capital else 0.0,
             "ddUSD": r2(curve[-1]["dd"]) if curve else 0.0, "ddPct": rn(curve[-1]["dd"] / capital * 100, 2) if curve and capital else 0.0,
             "peakUSD": r2(max([c["pnl"] for c in curve] + [0.0])), "bestDayUSD": r2(max([c["day"] for c in curve] or [0.0])), "worstDayUSD": r2(min([c["day"] for c in curve] or [0.0])),
             "upDays": len([c for c in curve if c["day"] > 0.005]), "downDays": len([c for c in curve if c["day"] < -0.005]),
             "openSignals": len(open_sigs), "openRiskUSD": r2(sum(v["copies"] * v["openRisk"] for v in views)), "queuedSignals": len(orders),
             "fundedAccounts": sum(v["copies"] for v in fu), "evalAccounts": sum(v["copies"] for v in ev),
             "stepsDone": sum(v["copies"] * min(v["step"], v["steps"]) for v in views), "stepsTotal": sum(v["copies"] * v["steps"] for v in views),
             "progressPct": rn(sum(v["capital"] * v["progress"] for v in ev) / ev_cap, 1) if ev_cap else None,
             "bestProgressPct": best["progress"] if best else None, "bestProgressName": best["name"] if best else None,
             "worstProgressPct": worst["progress"] if worst else None, "worstProgressName": worst["name"] if worst else None,
             "cushionPct": rn(sum(v["capital"] * v["cushionPct"] for v in fu) / fu_cap, 2) if fu_cap else None,
             "bufferPct": rn(sum(v["capital"] * v["bufferPct"] for v in fu) / fu_cap, 2) if fu_cap else None,
             "bestCushionPct": rn(bestc["cushionPct"], 2) if bestc else None, "bestCushionName": bestc["name"] if bestc else None,
             "maxLeftPct": rn(tight["maxLeftPct"], 1) if tight else None, "maxLeftName": tight["name"] if tight else None,
             "dailyLeftPct": rn(tightd["dailyLeftPct"], 1) if tightd else None, "dailyLeftName": tightd["name"] if tightd else None,
             "daysMet": sum(v["copies"] for v in ev if v["daysMet"]),
             "phasesPassed": sum(v["copies"] * v["passed"] for v in views), "attemptsFailed": sum(v["copies"] * v["failed"] for v in views),
             "feesUSD": r2(fees), "payoutsUSD": r2(paid), "payoutsCount": sum(v["payoutsCount"] for v in views), "cashUSD": r2(paid - fees),
             "checkUSD": r2(check)}
    stats.update(st)
    if engine_summary is not None:
        for mine, theirs, tol, what in ((stats["phaseNetUSD"], engine_summary.get("netPnlUSD"), 0.05, "net P&L of the current phases"),
                                        (stats["accounts"], engine_summary.get("accounts"), 0, "number of accounts"), (stats["capital"], engine_summary.get("capital"), 0.05, "capital"),
                                        (stats["fundedAccounts"], engine_summary.get("fundedAccounts"), 0, "funded accounts"), (stats["feesUSD"], engine_summary.get("feesPaidUSD"), 0.5, "fees"),
                                        (stats["payoutsUSD"], engine_summary.get("payoutsUSD"), 0.5, "rewards"), (stats["attemptsFailed"], engine_summary.get("attemptsFailed"), 0, "failed attempts")):
            if theirs is None or abs(float(mine) - float(theirs)) > tol:
                notes.append("%s differs from the engine's summary (%s vs %s)" % (what, mine, theirs))
    stats["selfCheck"] = "ok" if not notes else "; ".join(notes[:6]) + ("; and %d more" % (len(notes) - 6) if len(notes) > 6 else "")
    return [stats] + rows, stats


# ----------------------------------------------------------------------------------------------------------------
# the file
# ----------------------------------------------------------------------------------------------------------------
SECTION_ORDER = ["stats", "equity", "month", "leg", "symbol", "signal", "open", "account"]


def build(pack, engine_rows=None, engine_summaries=None, root=ROOT, use_engine=True):
    manifest = pack["manifest"]
    by_track = collections.OrderedDict((t, []) for t in TRACKS)
    for acct in manifest["accounts"]:
        if not acct.get("enabled", True):
            continue
        state = pack["states"].get(acct["id"])
        if not state:
            continue
        by_track.setdefault(track_of(acct), []).append(account_view(acct, state))
    all_views = [v for vs in by_track.values() for v in vs]
    marks, marks_note = engine_marks(all_views, root, use_engine)
    if marks is None and any(v["positions"] for v in all_views):
        print("note: %s; open positions are listed without their floating result" % marks_note)
    summaries = {s.get("track"): s for s in (engine_summaries or []) if isinstance(s, dict)}
    rows = []
    stats = []
    for track, views in by_track.items():
        if not views:
            continue
        tr, st = track_rows(track, views, marks, engine_rows, summaries.get(track) if engine_summaries is not None else None)
        rows.extend(tr)
        stats.append(st)
    rows.sort(key=lambda r: SECTION_ORDER.index(r["section"]))          # stable: tracks and natural order are kept inside a section
    unknown = sorted(set(k for r in rows for k in r) - set(COLUMNS))
    if unknown:
        raise ValueError("columns missing from COLUMNS: %s" % unknown)
    return rows, stats


def render(rows):
    how = (__doc__ or "").split("How the numbers are made", 1)[-1].strip()
    how = " ".join(line.strip() for line in how.splitlines()).replace("  ", " ")
    meta = [{"label": "Made by", "value": "scripts/export_tracks.py in the simulation's repository, run by the GitHub workflow right after every engine run. It only reads the engine's saved state; it places, changes or cancels nothing."},
            {"label": "Input", "value": "state/portfolio_state.json (the engine's saved state, read only), compared with out/portfolio.json and out/psummary.json of the same run"},
            {"label": "How to read it", "value": "One table with a section column: stats (one row of statistics and progress per track), equity (the strategy P&L curve by day), month, leg, symbol, signal (one row per closed signal), open (positions and queued orders now) and account (one row per firm programme). Track challenge is A, funded-start is B. Amounts are US dollars summed over a track's accounts, identical copies included."},
            {"label": "How the numbers are made", "value": how}]
    body = ",\n".join("  " + json.dumps(r, allow_nan=False) for r in rows)
    text = "{\n \"columns\": %s,\n \"rows\": [\n%s\n ],\n \"meta\": %s\n}\n" % (json.dumps(COLUMNS), body, json.dumps(meta, indent=1, allow_nan=False))
    json.loads(text)                                                        # must be valid JSON before it replaces the old file
    return text


def main():
    ap = argparse.ArgumentParser(description="Strategy-level (per track) export for the dashboard. Read only.")
    ap.add_argument("--states", default=os.path.join(ROOT, "state", "portfolio_state.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    ap.add_argument("--no-engine", action="store_true", help="do not ask the engine module for open-position marks")
    ap.add_argument("--no-compare", action="store_true", help="do not compare with out/portfolio.json and out/psummary.json")
    a = ap.parse_args()
    pack = load_json(a.states)
    engine_rows = engine_summaries = None
    if not a.no_compare:
        try:
            engine_rows = load_json(os.path.join(a.out, "portfolio.json"))
            engine_summaries = load_json(os.path.join(a.out, "psummary.json"))
        except Exception as e:  # noqa
            print("no engine export to compare with (%s)" % e)
            engine_rows = engine_summaries = None
    rows, stats = build(pack, engine_rows, engine_summaries, root=ROOT, use_engine=not a.no_engine)
    text = render(rows)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "tracks.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    for s in stats:
        print("track %s: %d accounts, strategy P&L %+.2f (%+.2f%%), %d closed signals, win rate %s, avg R %s, max drawdown %.2f%%, self-check: %s" % (
            s["letter"], s["accounts"], s["pnlUSD"], s["pnlPct"], s["signals"], s["winRatePct"], s["avgR"], s["maxDdPct"], s["selfCheck"]))
    print("wrote %s (%d rows, %d bytes)" % (path, len(rows), len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
