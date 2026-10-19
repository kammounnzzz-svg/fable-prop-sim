#!/usr/bin/env python3
"""
FTMO 2-Step challenge simulator -- mechanical daily-bar engine.
PAPER TRADING ONLY. Nothing here touches a broker or real money.

Commands (all: python3 engine.py <cmd> ...):
  fetch   --out bars            download daily OHLC bars for every instrument (Yahoo chart feed)
  init    --state state.json    create a fresh US$100,000 Challenge account
  run     --state state.json --bars bars [--mode live|backtest] [--no-orders]
                                process every new completed bar: fills, stops, trailing, swaps,
                                FTMO rule checks, phase changes, then queue orders for the next session
  cancel  --state s --id O3 --note "..."   cancel a pending order (Claude's only discretionary action)
  journal --state s --title "..." --body "..."
  export  --state s --out out   write the dashboard dataset files
  status  --state s             print a plain-text summary
  backtest --bars bars --start YYYY-MM-DD [--end YYYY-MM-DD]   replay the rules over history
"""
import argparse, copy, datetime as dt, json, math, os, sys, time, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
PRAGUE = ZoneInfo("Europe/Prague")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# ----------------------------------------------------------------------------------------------
# Instruments (FTMO-style CFD specs, approximated). P&L = units * price change * quote->USD rate.
#   lot      units per 1.00 lot (how the dashboard shows size)
#   spread   full bid/ask spread in price units (stocks: fraction of price)
#   comm     commission per unit per side, USD (stocks: per share)
#   swap     fx: pips charged per night (both sides); others: annual financing spread over benchmark
#   session  how to tell a Yahoo daily bar is complete
# ----------------------------------------------------------------------------------------------
INSTRUMENTS = {
    "EURUSD": dict(y="EURUSD=X", cls="fx", quote="USD", usd="quote", lot=100000, pip=0.0001, dp=5, spread=0.00012, comm=0.00003, swap=0.6, margin=30, group="fx", session="fx"),
    "GBPUSD": dict(y="GBPUSD=X", cls="fx", quote="USD", usd="quote", lot=100000, pip=0.0001, dp=5, spread=0.00015, comm=0.00003, swap=0.6, margin=30, group="fx", session="fx"),
    "AUDUSD": dict(y="AUDUSD=X", cls="fx", quote="USD", usd="quote", lot=100000, pip=0.0001, dp=5, spread=0.00013, comm=0.00003, swap=0.6, margin=30, group="fx", session="fx"),
    "NZDUSD": dict(y="NZDUSD=X", cls="fx", quote="USD", usd="quote", lot=100000, pip=0.0001, dp=5, spread=0.00018, comm=0.00003, swap=0.6, margin=30, group="fx", session="fx"),
    "USDJPY": dict(y="USDJPY=X", cls="fx", quote="JPY", usd="base", lot=100000, pip=0.01, dp=3, spread=0.013, comm=0.00003, swap=0.6, margin=30, group="fx", session="fx"),
    "USDCAD": dict(y="USDCAD=X", cls="fx", quote="CAD", usd="base", lot=100000, pip=0.0001, dp=5, spread=0.00018, comm=0.00003, swap=0.6, margin=30, group="fx", session="fx"),
    "XAUUSD": dict(y="GC=F", cls="metal", quote="USD", usd="quote", lot=100, pip=0.01, dp=2, spread=0.30, comm=0.0, swap=0.025, margin=20, group="metal", session="gc", note="gold futures (GC=F) used as the spot proxy"),
    "US500": dict(y="^GSPC", cls="index", quote="USD", usd=None, lot=1, pip=0.1, dp=1, spread=0.6, comm=0.0, swap=0.025, margin=20, group="us_index", session="ny_index"),
    "US100": dict(y="^NDX", cls="index", quote="USD", usd=None, lot=1, pip=0.1, dp=1, spread=1.5, comm=0.0, swap=0.025, margin=20, group="us_index", session="ny_index"),
    "US30": dict(y="^DJI", cls="index", quote="USD", usd=None, lot=1, pip=1, dp=0, spread=3.0, comm=0.0, swap=0.025, margin=20, group="us_index", session="ny_index"),
    "GER40": dict(y="^GDAXI", cls="index", quote="EUR", usd=None, lot=1, pip=0.1, dp=1, spread=1.6, comm=0.0, swap=0.025, margin=20, group="eu_index", session="xetra"),
    "UK100": dict(y="^FTSE", cls="index", quote="GBP", usd=None, lot=1, pip=0.1, dp=1, spread=1.2, comm=0.0, swap=0.025, margin=20, group="eu_index", session="lse"),
    "EU50": dict(y="^STOXX50E", cls="index", quote="EUR", usd=None, lot=1, pip=0.1, dp=1, spread=1.5, comm=0.0, swap=0.025, margin=20, group="eu_index", session="xetra"),
    "JP225": dict(y="^N225", cls="index", quote="JPY", usd=None, lot=1, pip=1, dp=0, spread=8.0, comm=0.0, swap=0.025, margin=20, group="asia_index", session="tse"),
    "AUS200": dict(y="^AXJO", cls="index", quote="AUD", usd=None, lot=1, pip=0.1, dp=1, spread=1.5, comm=0.0, swap=0.025, margin=20, group="asia_index", session="asx"),
    "XAGUSD": dict(y="SI=F", cls="metal", quote="USD", usd="quote", lot=5000, pip=0.001, dp=3, spread=0.025, comm=0.0, swap=0.025, margin=20, group="metal", session="gc", note="silver futures (SI=F) used as the spot proxy"),
    "NVDA": dict(y="NVDA", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0004, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "AAPL": dict(y="AAPL", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0003, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "MSFT": dict(y="MSFT", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0003, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "AMZN": dict(y="AMZN", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0004, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "TSLA": dict(y="TSLA", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0006, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "GOOGL": dict(y="GOOGL", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0003, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "META": dict(y="META", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0004, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "AVGO": dict(y="AVGO", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0005, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "JPM": dict(y="JPM", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0003, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "NFLX": dict(y="NFLX", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0005, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "LLY": dict(y="LLY", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0005, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    "COST": dict(y="COST", cls="stock", quote="USD", usd=None, lot=1, pip=0.01, dp=2, spread=0.0004, comm=0.005, swap=0.025, margin=5, group="stock", session="ny_equity"),
    # crypto CFDs: spread as a fraction of price, financing spread wide (FTMO-style crypto swaps), 24/7 UTC-day bars
    "BTCUSD": dict(y="BTC-USD", cls="crypto", quote="USD", usd=None, lot=1, pip=1.0, dp=1, spread=0.0008, comm=0.0, swap=0.16, margin=50, group="crypto", session="crypto"),
    "ETHUSD": dict(y="ETH-USD", cls="crypto", quote="USD", usd=None, lot=1, pip=0.1, dp=2, spread=0.0012, comm=0.0, swap=0.16, margin=50, group="crypto", session="crypto"),
}
SESSION_HOURS = {"fx": 24.0, "gc": 17.0, "ny_index": 6.75, "ny_equity": 6.75, "xetra": 8.75, "lse": 8.75, "tse": 6.75, "asx": 6.25, "crypto": 24.0}
BENCHMARK_RATE = 0.040   # USD overnight benchmark used for CFD financing (approximation)

CONFIG_VERSION = 6   # bump when DEFAULT_CONFIG changes; a running state is upgraded to it on the next run

DEFAULT_CONFIG = {
    "configVersion": CONFIG_VERSION,
    "accountId": "ftmo-100k", "firm": "FTMO", "program": "2-Step Swing",
    "product": "FTMO 2-Step Challenge, Swing account (hold over weekends and news)",
    "currency": "USD",
    "initial": 100000.0,
    "fee": 540.0, "feeCurrency": "EUR", "feeEUR": 540.0,   # feeEUR kept for older states
    "phaseTargetsPct": [10.0, 5.0],   # one entry per evaluation phase: 2-step = Challenge then Verification; a 1-step firm has one entry
    "challengeTargetPct": 10.0,
    "verificationTargetPct": 5.0,
    "maxDailyLossPct": 5.0,           # None = the firm has no daily limit
    "dailyLossBasis": "equity",       # "equity": floating losses count (FTMO); "balance": only closed P&L counts (Fintokei, CTI)
    "maxLossPct": 10.0,
    "maxLossBasis": "equity",
    "minTradingDays": 4,
    "minProfitableDays": 0, "profitableDayPct": 0.5,   # The5ers / CTI style: days with closed profit >= 0.5% of initial
    "excludeClasses": [],             # instrument classes the firm does not offer, e.g. ["stock", "crypto"]
    "volScale": {"enabled": False, "lookback": 252, "trigger": 1.25, "floor": 0.5},   # scale a trade's risk down when the instrument's ATR% is > trigger x its 1-year median (never up)
    "riskScale": 1.0,                 # multiplies every risk % (0.8 for firms with an 8% max loss)
    "rewardSplitPct": 80.0,
    "rewardWaitDays": 14,
    "fundedLadder": {"enabled": True, "cushionPct": [0.0, 2.0, 4.0], "riskPct": [0.5, 0.75, 1.0], "keepBufferPct": 4.0},   # funded: risk steps up with closed profit kept on the account
    "riskPct": {"challenge": 1.00, "verification": 0.75, "funded": 0.50},   # % of initial balance per trade
    "maxOpenRiskPct": 3.75,           # daily components' combined open risk; the intraday leg has its own reserved slice (worst case 4.75% < 5%)
    "maxPositions": 6,
    "maxNewPerRun": 2,
    "groupCaps": {"fx": 2, "metal": 1, "us_index": 1, "eu_index": 1, "asia_index": 1, "stock": 2, "crypto": 1},
    "clusterCaps": {"equity": 3},          # us_index + eu_index + asia_index + stock together
    "clusters": {"us_index": "equity", "eu_index": "equity", "asia_index": "equity", "stock": "equity"},
    "maxSameUsdDirection": 2,
    "dayGuardNoNewPct": -2.5,      # day P&L (of initial) at/below which no new trades are queued
    "dayGuardFlattenPct": -3.5,    # day P&L at/below which every position is closed at the next open
    "ddHalfRiskPct": -6.0,         # equity drawdown at/below which risk halves and positions cap at 2
    "ddQuarterRiskPct": -8.0,      # ... risk to a third, one position
    "lossStreakCut": 3,            # after this many consecutive losses, risk x0.67 until a winner
    "atrLen": 14, "emaFast": 20, "emaSlow": 50, "donchian": 20, "smaLong": 200, "smaExit": 5, "rsiLen": 2,
    "gapSkipAtr": 1.0,
    "passBufferPct": 0.25,
    "strategies": {
        # v5 (2026-10): the daily index breakout leg is OFF - it lost money net of financing over 2011-2026 (PF 0.81) and
        # out of sample in the engine itself (2021-11..2024-12). Replaced by three legs that held up over 15 years.
        "trend": {"enabled": False, "classes": ["index"], "entries": ["breakout"], "adxMin": 25.0,
                  "stopAtr": 3.0, "trailAtr": 3.5, "trailAtR": 1.0, "breakeven": False, "emaExit": False, "shorts": False},
        "mr": {"enabled": True, "classes": ["index", "stock"], "rsiBuy": 15.0, "rsiSell": 90.0, "stopAtr": 3.0, "maxBars": 8, "shorts": False,
               "panicDropPct": 2.5, "panicRangeAtr": 2.5, "riskMult": 0.75},
        # pullback-in-uptrend: close dips under the 20-day EMA (but within 2 ATR of it) with a rising 50-day EMA and above the 200-day average;
        # exit on a close back above the 20-day EMA or after maxBars
        "pullback": {"enabled": False, "classes": ["index"], "stopAtr": 2.5, "maxBars": 10, "riskMult": 0.75},   # off: lost money in both engine windows (2021-24, 2025-26)
        # turn-of-month: buy indices at the open of the month's last trading day, hold through the first three sessions of the new month
        "tom": {"enabled": True, "classes": ["index"], "stopAtr": 3.0, "holdBars": 3, "riskMult": 0.75},
        # crypto breakout sleeve: 20-day high with price above the 200-day average, 2.5 ATR stop, 3 ATR trail after +1R, half the usual risk
        "crypto": {"enabled": True, "symbols": ["BTCUSD", "ETHUSD"], "stopAtr": 2.5, "trailAtr": 3.0, "trailAtR": 1.0, "riskMult": 0.5},
        "intraday": {"enabled": True, "symbols": ["XAUUSD", "XAGUSD"], "donchian": 48, "atrLen": 14, "minRangeAtr": 0.5,
                     "stopAtr": 2.5, "trailAtr": 3.0, "trailAtR": 1.0, "riskPct": 1.0, "gapSkipAtr": 1.0, "yahooRange": "60d",
                     "scaleOutAtR": {"challenge": None, "verification": 2.0, "funded": 2.0},   # bank half at +2R once past the Challenge
                     "entryHoursUTC": [21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]},   # v6: no new metal entries during US hours (13-20 UTC); gold's return accrues in Asian/London hours and the US session subtracts (same profit, fewer trades, smaller drawdown in the 2-year test)
    },
}

# ----------------------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------------------
def now_utc():
    return dt.datetime.now(UTC)

def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)

def r2(x):
    return round(x + 0.0, 2)

def pdate(s):
    return dt.date.fromisoformat(s)

# ----------------------------------------------------------------------------------------------
# data: Yahoo chart feed
# ----------------------------------------------------------------------------------------------
def fetch_yahoo(ysym, rng="2y"):
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (urllib.parse.quote(ysym), rng)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception as e:  # noqa
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("fetch failed for %s: %s" % (ysym, last))

def clean_bars(spec, payload, now_epoch):
    res = payload["chart"]["result"][0]
    tz = ZoneInfo(res["meta"]["exchangeTimezoneName"])
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    hours = SESSION_HOURS[spec["session"]]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        if t + hours * 3600 > now_epoch:
            continue                                  # session still running
        if i == len(ts) - 1 and o == h == l == c:
            continue                                  # trailing quote pseudo-bar
        if i > 0 and t - ts[i - 1] < 12 * 3600:
            continue                                  # intraday pseudo-bar
        d = dt.datetime.fromtimestamp(t, tz).date().isoformat()
        if bars and bars[-1]["d"] == d:
            continue
        bars.append({"d": d, "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    return bars

def fetch_yahoo_hourly(ysym, rng):
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1h" % (urllib.parse.quote(ysym), rng)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("hourly fetch failed for %s: %s" % (ysym, last))

def clean_hourly(payload, now_epoch):
    res = payload["chart"]["result"][0]
    tz = ZoneInfo(res["meta"]["exchangeTimezoneName"])
    ts = res["timestamp"]; q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        if t + 3600 > now_epoch:
            continue                                  # bar still forming
        if i == len(ts) - 1 and o == h == l == c:
            continue
        if bars and t - bars[-1]["t"] < 1800:
            continue
        local = dt.datetime.fromtimestamp(t, tz)
        bars.append({"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "date": local.date().isoformat(),
                     "pday": dt.datetime.fromtimestamp(t, PRAGUE).date().isoformat()})
    return bars

def cmd_fetch(a):
    os.makedirs(a.out, exist_ok=True)
    now_epoch = now_utc().timestamp()
    allbars, failures = {}, {}
    icfg = DEFAULT_CONFIG["strategies"]["intraday"]
    hourly = {}
    if icfg["enabled"] and not a.no_hourly:
        for sym in icfg["symbols"]:
            try:
                hb = clean_hourly(fetch_yahoo_hourly(INSTRUMENTS[sym]["y"], a.hourly_range), now_epoch)
                if len(hb) < 80:
                    raise RuntimeError("only %d hourly bars" % len(hb))
                hourly[sym] = hb
                print("%-7s %4d hourly bars, last %s close %s" % (sym, len(hb), dt.datetime.fromtimestamp(hb[-1]["t"], UTC).strftime("%Y-%m-%d %H:%M UTC"), hb[-1]["c"]))
            except Exception as e:  # noqa
                failures[sym + "@1h"] = str(e)
                print("%-7s hourly FAILED: %s" % (sym, e))
            time.sleep(0.6)
        save_json(os.path.join(a.out, "hourly.json"), {"fetchedAt": iso(now_utc()), "bars": hourly})
    for sym, spec in INSTRUMENTS.items():
        try:
            payload = fetch_yahoo(spec["y"], a.range)
            bars = clean_bars(spec, payload, now_epoch)
            if len(bars) < 80:
                raise RuntimeError("only %d bars" % len(bars))
            allbars[sym] = bars
            print("%-7s %4d bars, last %s close %s" % (sym, len(bars), bars[-1]["d"], bars[-1]["c"]))
        except Exception as e:  # noqa
            failures[sym] = str(e)
            print("%-7s FAILED: %s" % (sym, e))
        time.sleep(0.6)
    save_json(os.path.join(a.out, "all.json"), {"fetchedAt": iso(now_utc()), "bars": allbars, "failures": failures})
    print("saved %s (%d ok, %d failed)" % (os.path.join(a.out, "all.json"), len(allbars), len(failures)))
    return 0 if not failures else 2

def cmd_import_av(a):
    """Fallback: add Alpha Vantage daily CSV (timestamp,open,high,low,close[,volume]) for one symbol."""
    path = os.path.join(a.bars, "all.json")
    data = load_json(path)
    rows = []
    text = open(a.csv, encoding="utf-8").read()
    if text.lstrip().startswith("{"):            # Alpha Vantage gold/silver history: {"data": [{"date", "price"}]}
        for r in json.loads(text).get("data", []):
            try:
                p = float(r["price"]); rows.append({"d": r["date"][:10], "o": p, "h": p, "l": p, "c": p})
            except (KeyError, ValueError, TypeError):
                continue
    else:
        for line in text.splitlines()[1:]:
            p = line.strip().split(",")
            if len(p) < 5:
                continue
            try:
                rows.append({"d": p[0][:10], "o": float(p[1]), "h": float(p[2]), "l": float(p[3]), "c": float(p[4])})
            except ValueError:
                continue
    rows.sort(key=lambda b: b["d"])
    today = now_utc().date().isoformat()
    rows = [b for b in rows if b["d"] < today]
    data["bars"][a.symbol] = rows
    data["failures"].pop(a.symbol, None)
    save_json(path, data)
    print("imported %d bars for %s (last %s)" % (len(rows), a.symbol, rows[-1]["d"] if rows else None))

# ----------------------------------------------------------------------------------------------
# indicators
# ----------------------------------------------------------------------------------------------
def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    s = sum(vals[:n]) / n
    out[n - 1] = s
    for i in range(n, len(vals)):
        s = vals[i] * k + s * (1 - k)
        out[i] = s
    return out

def wilder(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    s = sum(vals[:n])
    out[n - 1] = s / n
    for i in range(n, len(vals)):
        s = s - s / n + vals[i]
        out[i] = s / n
    return out

def indicators(bars, cfg):
    n = len(bars)
    c = [b["c"] for b in bars]; h = [b["h"] for b in bars]; l = [b["l"] for b in bars]
    tr, pdm, mdm = [], [], []
    for i in range(n):
        if i == 0:
            tr.append(h[0] - l[0]); pdm.append(0.0); mdm.append(0.0); continue
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    L = cfg["atrLen"]
    atr = wilder(tr, L)
    trs, pds, mds = wilder(tr, L), wilder(pdm, L), wilder(mdm, L)
    dx = [None] * n
    for i in range(n):
        if trs[i] and trs[i] > 0:
            pdi, mdi = 100 * pds[i] / trs[i], 100 * mds[i] / trs[i]
            dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
    adx = [None] * n
    first = [i for i in range(n) if dx[i] is not None]
    if len(first) >= L:
        s = sum(dx[i] for i in first[:L]) / L
        adx[first[L - 1]] = s
        for i in first[L:]:
            s = (s * (L - 1) + dx[i]) / L
            adx[i] = s
    ef, es = ema(c, cfg["emaFast"]), ema(c, cfg["emaSlow"])
    D = cfg["donchian"]
    dhi, dlo = [None] * n, [None] * n
    for i in range(D, n):
        dhi[i] = max(h[i - D:i]); dlo[i] = min(l[i - D:i])
    return {"atr": atr, "adx": adx, "emaF": ef, "emaS": es, "dhi": dhi, "dlo": dlo,
            "smaL": sma(c, cfg["smaLong"]), "smaX": sma(c, cfg["smaExit"]), "rsi": rsi(c, cfg["rsiLen"])}

def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out

def rsi(vals, n):
    out = [None] * len(vals)
    if len(vals) <= n:
        return out
    gains = [max(vals[i] - vals[i - 1], 0.0) for i in range(1, len(vals))]
    losses = [max(vals[i - 1] - vals[i], 0.0) for i in range(1, len(vals))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n; al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

# ----------------------------------------------------------------------------------------------
# account mechanics
# ----------------------------------------------------------------------------------------------
def conv_rate(sym, closes):
    """quote currency -> USD multiplier using the latest known closes of the conversion pairs."""
    q = INSTRUMENTS[sym]["quote"]
    if q == "USD":
        return 1.0
    if q == "JPY":
        return 1.0 / closes["USDJPY"]
    if q == "CAD":
        return 1.0 / closes["USDCAD"]
    if q == "EUR":
        return closes["EURUSD"]
    if q == "GBP":
        return closes["GBPUSD"]
    if q == "AUD":
        return closes["AUDUSD"]
    raise ValueError(q)

def half_spread(sym, price):
    s = INSTRUMENTS[sym]["spread"]
    return price * s / 2 if INSTRUMENTS[sym]["cls"] in ("stock", "crypto") else s / 2

def commission(sym, units, price):
    return INSTRUMENTS[sym]["comm"] * units

def lots_of(sym, units):
    return units / INSTRUMENTS[sym]["lot"]

def round_units(sym, units):
    cls = INSTRUMENTS[sym]["cls"]
    if cls == "fx":
        return math.floor(units / 1000) * 1000           # 0.01 lot steps
    if cls == "metal":
        return math.floor(units)                           # 1 oz = 0.01 lot
    if cls == "index":
        return math.floor(units * 10) / 10                 # $0.10 per point
    if cls == "crypto":
        return math.floor(units * 1000) / 1000             # 0.001 coin steps
    return math.floor(units)                               # whole shares

def pos_value(pos, price, conv):
    sign = 1 if pos["side"] == "long" else -1
    return sign * (price - pos["entry"]) * pos["units"] * conv

def risk_to_stop(pos, conv):
    sign = 1 if pos["side"] == "long" else -1
    return max(0.0, sign * (pos["entry"] - pos["stop"]) * pos["units"] * conv)

def usd_direction(sym, side):
    u = INSTRUMENTS[sym].get("usd")
    if u is None:
        return None
    long_usd = (u == "base" and side == "long") or (u == "quote" and side == "short")
    return "usd_long" if long_usd else "usd_short"

def new_state(cfg):
    return {
        "meta": {"version": 2, "createdAt": iso(now_utc()), "lastRunAt": None, "lastProcessed": {}, "lastProcessedHourly": {}, "nextId": 1, "runs": 0},
        "config": cfg,
        "account": {
            "attempt": 1, "phase": "challenge", "phaseStart": None, "balance": cfg["initial"], "dayStartBalance": cfg["initial"],
            "peakBalance": cfg["initial"], "tradingDays": [], "profitableDays": 0, "feesPaidEUR": cfg_fee(cfg), "phaseHistory": [],
            "payouts": [], "lastPayoutDate": None, "lossStreak": 0, "flattenNextOpen": False, "status": "active",
            "lastDate": None, "lastEquity": cfg["initial"], "lastWorst": cfg["initial"], "lastDayPnl": 0.0, "pragueDay": None, "lastHourlyBar": None,
        },
        "positions": [], "orders": [], "trades": [], "equity": [], "journal": [], "watch": [], "events": [],
    }

def nid(state, prefix):
    i = state["meta"]["nextId"]; state["meta"]["nextId"] = i + 1
    return "%s%d" % (prefix, i)

def phase_targets(cfg):
    t = cfg.get("phaseTargetsPct")
    if not t:
        t = [cfg["challengeTargetPct"], cfg["verificationTargetPct"]]
    return list(t)

def phase_names(cfg):
    return ["challenge", "verification", "funded"] if len(phase_targets(cfg)) >= 2 else ["challenge", "funded"]

def next_phase(cfg, phase):
    names = phase_names(cfg)
    return names[min(names.index(phase) + 1, len(names) - 1)]

def phase_target(state):
    cfg, acc = state["config"], state["account"]
    if acc["phase"] == "funded":
        return None
    names = phase_names(cfg); targets = phase_targets(cfg)
    i = names.index(acc["phase"]) if acc["phase"] in names else 0
    return cfg["initial"] * (1 + targets[min(i, len(targets) - 1)] / 100)

def phase_target_pct(state):
    cfg, acc = state["config"], state["account"]
    if acc["phase"] == "funded":
        return None
    names = phase_names(cfg); targets = phase_targets(cfg)
    return targets[min(names.index(acc["phase"]), len(targets) - 1)]

def cfg_fee(cfg):
    return cfg.get("fee", cfg.get("feeEUR", 0.0))

def floors(state):
    """(daily floor or None, max-loss floor) in account currency."""
    cfg, acc = state["config"], state["account"]
    daily = None if cfg.get("maxDailyLossPct") is None else acc["dayStartBalance"] - cfg["initial"] * cfg["maxDailyLossPct"] / 100
    return daily, cfg["initial"] * (1 - cfg["maxLossPct"] / 100)

def breach_check(state, worst_equity, balance, where=""):
    """Firm rules against the worst-case equity of the period (equity basis) or the closed balance (balance basis)."""
    cfg = state["config"]
    daily_floor, max_floor = floors(state)
    m_val = worst_equity if cfg.get("maxLossBasis", "equity") == "equity" else balance
    if m_val < max_floor:
        return "Maximum Loss breached%s: %s %.0f below %.0f" % (where, "worst-case equity" if cfg.get("maxLossBasis", "equity") == "equity" else "balance", m_val, max_floor)
    if daily_floor is not None:
        d_val = worst_equity if cfg.get("dailyLossBasis", "equity") == "equity" else balance
        if d_val < daily_floor:
            return "Maximum Daily Loss breached%s: %s %.0f below the day's floor %.0f" % (where, "worst-case equity" if cfg.get("dailyLossBasis", "equity") == "equity" else "balance", d_val, daily_floor)
    return None

def profitable_days(state):
    cfg, acc = state["config"], state["account"]
    thr = cfg["initial"] * cfg.get("profitableDayPct", 0.5) / 100
    return sum(1 for v in acc.get("realizedByDate", {}).values() if v >= thr)

def min_days_met(state):
    cfg, acc = state["config"], state["account"]
    if len(acc["tradingDays"]) < cfg["minTradingDays"]:
        return False
    return profitable_days(state) >= cfg.get("minProfitableDays", 0)

def reset_account(acc, cfg, phase, attempt, fee_add=0.0, last_payout=None):
    acc.update({"attempt": attempt, "phase": phase, "phaseStart": None, "balance": cfg["initial"], "dayStartBalance": cfg["initial"], "peakBalance": cfg["initial"],
                "tradingDays": [], "profitableDays": 0, "realizedByDate": {}, "lossStreak": 0, "lossStreakIntraday": 0, "flattenNextOpen": False, "lastEquity": cfg["initial"], "lastWorst": cfg["initial"],
                "lastDayPnl": 0.0, "lastPayoutDate": last_payout})
    if fee_add:
        acc["feesPaidEUR"] = acc.get("feesPaidEUR", 0.0) + fee_add

def phase_risk_pct(state):
    cfg, acc = state["config"], state["account"]
    pct = cfg["riskPct"][acc["phase"]]
    lad = cfg.get("fundedLadder")
    if acc["phase"] == "funded" and lad and lad.get("enabled"):
        cushion = (acc["balance"] - cfg["initial"]) / cfg["initial"] * 100
        for c, r in zip(lad["cushionPct"], lad["riskPct"]):
            if cushion >= c:
                pct = r
    return pct

def risk_per_trade(state, strat="daily"):
    cfg, acc = state["config"], state["account"]
    base = phase_risk_pct(state)
    pct = min(cfg["strategies"]["intraday"]["riskPct"], base) if strat == "intraday" else base
    pct *= cfg.get("riskScale", 1.0)
    mult = 1.0
    dd = (acc["lastEquity"] - cfg["initial"]) / cfg["initial"] * 100
    if dd <= cfg["ddQuarterRiskPct"]:
        mult *= 0.33
    elif dd <= cfg["ddHalfRiskPct"]:
        mult *= 0.5
    streak = acc.get("lossStreakIntraday", 0) if strat == "intraday" else acc["lossStreak"]
    if streak >= cfg["lossStreakCut"]:
        mult *= 0.67
    return cfg["initial"] * pct / 100 * mult, mult

def max_positions(state):
    cfg, acc = state["config"], state["account"]
    dd = (acc["lastEquity"] - cfg["initial"]) / cfg["initial"] * 100
    if dd <= cfg["ddQuarterRiskPct"]:
        return 1
    if dd <= cfg["ddHalfRiskPct"]:
        return 2
    return cfg["maxPositions"]

def record_event(state, date, kind, text):
    state["events"].append({"date": date, "kind": kind, "text": text})
    if len(state["events"]) > 400:
        state["events"] = state["events"][-400:]

def open_position(state, order, fill, date, closes):
    sym = order["symbol"]; side = order["side"]
    conv = conv_rate(sym, closes)
    units = round_units(sym, order["riskUSD"] / (order["stopDist"] * conv))
    if units <= 0:
        return None
    stop = fill - order["stopDist"] if side == "long" else fill + order["stopDist"]
    comm = commission(sym, units, fill)
    pos = {
        "id": nid(state, "T"), "orderId": order["id"], "symbol": sym, "side": side, "units": units, "lots": r2(lots_of(sym, units)) if INSTRUMENTS[sym]["cls"] in ("fx", "metal") else units,
        "entry": fill, "entryDate": date, "stop": stop, "initialStop": stop, "stopDist": order["stopDist"],
        "riskUSD": r2(order["stopDist"] * units * conv), "atr": order["atr"], "setup": order["setup"], "reason": order["reason"],
        "extreme": fill, "beDone": False, "trailOn": False, "swapUSD": 0.0, "commUSD": r2(comm), "bars": 0, "lastDate": date,
        "exitNextOpen": None, "phase": state["account"]["phase"], "attempt": state["account"]["attempt"], "strat": order.get("strat", "trend"), "tf": order.get("tf", "1d"),
    }
    state["account"]["balance"] -= comm
    return pos

def close_position(state, pos, price, date, reason, closes, day):
    sym = pos["symbol"]
    conv = conv_rate(sym, closes)
    gross = pos_value(pos, price, conv)
    comm = commission(sym, pos["units"], price)
    net = gross - comm + pos["swapUSD"] - pos["commUSD"]      # swapUSD is negative when a cost
    state["account"]["balance"] += gross - comm + pos["swapUSD"]
    r = (gross - comm + pos["swapUSD"] - pos["commUSD"]) / pos["riskUSD"] if pos["riskUSD"] else 0.0
    trade = {
        "id": pos["id"], "symbol": sym, "side": pos["side"], "units": pos["units"], "lots": pos["lots"], "entry": pos["entry"], "exit": price,
        "entryDate": pos["entryDate"], "exitDate": date, "bars": pos["bars"], "grossUSD": r2(gross), "swapUSD": r2(pos["swapUSD"]),
        "commUSD": r2(pos["commUSD"] + comm), "netUSD": r2(net), "r": round(r, 2), "riskUSD": pos["riskUSD"], "setup": pos["setup"],
        "reason": reason, "thesis": pos["reason"], "phase": pos["phase"], "attempt": pos["attempt"], "strat": pos.get("strat", "trend"),
    }
    state["trades"].append(trade)
    day["closes"].append(trade)
    day["realized"] += net
    rb = state["account"].setdefault("realizedByDate", {}); rb[date] = rb.get(date, 0.0) + net
    key = "lossStreakIntraday" if pos.get("strat") == "intraday" else "lossStreak"
    if net < 0:
        day["realizedLoss"] += -net
        state["account"][key] = state["account"].get(key, 0) + 1
    else:
        state["account"][key] = 0
    record_event(state, date, "close", "%s %s %s closed at %s (%s): %+.2f USD, %+.2fR" % (pos["id"], pos["side"], sym, fmt_price(sym, price), reason, net, r))
    return trade

def partial_close(state, pos, price, date, fraction, reason, closes, day):
    """Close a fraction of a position at price; books it as its own trade and shrinks the remainder."""
    sym = pos["symbol"]; conv = conv_rate(sym, closes)
    units = round_units(sym, pos["units"] * fraction)
    if units <= 0 or units >= pos["units"]:
        return None
    part = dict(pos); part["units"] = units; part["riskUSD"] = r2(pos["riskUSD"] * units / pos["units"])
    part["swapUSD"] = pos["swapUSD"] * units / pos["units"]; part["commUSD"] = pos["commUSD"] * units / pos["units"]
    part["id"] = pos["id"] + "a"; part["lots"] = r2(lots_of(sym, units)) if INSTRUMENTS[sym]["cls"] in ("fx", "metal") else units
    close_position(state, part, price, date, reason, closes, day)
    pos["units"] -= units; pos["riskUSD"] = r2(pos["riskUSD"] - part["riskUSD"]); pos["swapUSD"] -= part["swapUSD"]; pos["commUSD"] -= part["commUSD"]
    pos["lots"] = r2(lots_of(sym, pos["units"])) if INSTRUMENTS[sym]["cls"] in ("fx", "metal") else pos["units"]
    pos["scaled"] = True
    return part

def fmt_price(sym, p):
    return ("%." + str(INSTRUMENTS[sym]["dp"]) + "f") % p

def update_trailing(state, pos, bar, atr):
    strat = pos.get("strat", "trend")
    t = state["config"]["strategies"].get(strat) or state["config"]["strategies"]["trend"]
    if "trailAtr" not in t:
        t = state["config"]["strategies"]["trend"]
    t = dict({"breakeven": False, "trailAtR": 1.0}, **t)
    R = pos["stopDist"]
    if pos["side"] == "long":
        pos["extreme"] = max(pos["extreme"], bar["c"])
        if t["breakeven"] and bar["c"] >= pos["entry"] + R and not pos["beDone"]:
            pos["stop"] = max(pos["stop"], pos["entry"] + 0.1 * R); pos["beDone"] = True
        if bar["c"] >= pos["entry"] + t["trailAtR"] * R:
            pos["trailOn"] = True
        if pos["trailOn"]:
            pos["stop"] = max(pos["stop"], pos["extreme"] - t["trailAtr"] * atr)
    else:
        pos["extreme"] = min(pos["extreme"], bar["c"])
        if t["breakeven"] and bar["c"] <= pos["entry"] - R and not pos["beDone"]:
            pos["stop"] = min(pos["stop"], pos["entry"] - 0.1 * R); pos["beDone"] = True
        if bar["c"] <= pos["entry"] - t["trailAtR"] * R:
            pos["trailOn"] = True
        if pos["trailOn"]:
            pos["stop"] = min(pos["stop"], pos["extreme"] + t["trailAtr"] * atr)

def swap_charge(sym, pos, nights, closes):
    spec = INSTRUMENTS[sym]
    conv = conv_rate(sym, closes)
    if spec["cls"] == "fx":
        return -spec["swap"] * spec["pip"] * pos["units"] * conv * nights
    notional = pos["units"] * closes[sym] * conv
    rate = (BENCHMARK_RATE + spec["swap"]) if pos["side"] == "long" else -(BENCHMARK_RATE - spec["swap"])
    return -notional * rate / 365 * nights

# ----------------------------------------------------------------------------------------------
# signals
# ----------------------------------------------------------------------------------------------
def signals_at(sym, bars, ind, i, cfg):
    """All candidate entries at bar i: list of dicts {side, setup, strat, stopDist, score, atr}."""
    out = []
    atr, adx, ef, es = ind["atr"][i], ind["adx"][i], ind["emaF"][i], ind["emaS"][i]
    if None in (atr, adx, ef, es) or i < cfg["emaSlow"] + 6 or ind["emaS"][i - 5] is None or ind["dhi"][i] is None or atr <= 0:
        return out
    b = bars[i]; c, o, h, l = b["c"], b["o"], b["h"], b["l"]
    cls = INSTRUMENTS[sym]["cls"]
    tcfg = cfg["strategies"]["trend"]
    if tcfg["enabled"] and cls in tcfg["classes"] and adx >= tcfg["adxMin"]:
        up = c > es and ef > es and es > ind["emaS"][i - 5]
        dn = c < es and ef < es and es < ind["emaS"][i - 5] and tcfg["shorts"]
        if up or dn:
            side = "long" if up else "short"
            ext = (c - ef) / atr if up else (ef - c) / atr
            setup = None
            if "breakout" in tcfg["entries"]:
                if up and c > ind["dhi"][i] and c > o and (h - l) >= 0.6 * atr and ext <= 3.5:
                    setup = "breakout"
                if dn and c < ind["dlo"][i] and c < o and (h - l) >= 0.6 * atr and ext <= 3.5:
                    setup = "breakout"
            if setup is None and "pullback" in tcfg["entries"]:
                if up and any(bars[j]["l"] <= ind["emaF"][j] + 0.25 * atr for j in range(i - 4, i + 1)) and c > bars[i - 1]["h"] and c > ef and ext <= 2.5:
                    setup = "pullback"
                if dn and any(bars[j]["h"] >= ind["emaF"][j] - 0.25 * atr for j in range(i - 4, i + 1)) and c < bars[i - 1]["l"] and c < ef and ext <= 2.5:
                    setup = "pullback"
            if setup:
                out.append({"side": side, "setup": setup, "strat": "trend", "stopDist": tcfg["stopAtr"] * atr, "atr": atr,
                            "score": adx + (10 if setup == "pullback" else 0) - 3 * ext})
    mcfg = cfg["strategies"]["mr"]
    smaL, smaX, rv = ind["smaL"][i], ind["smaX"][i], ind["rsi"][i]
    if mcfg["enabled"] and cls in mcfg["classes"] and None not in (smaL, smaX, rv):
        panic = (bars[i - 1]["c"] - c) / c * 100 > mcfg["panicDropPct"] or (h - l) > mcfg["panicRangeAtr"] * atr
        if c > smaL and rv < mcfg["rsiBuy"] and c < smaX and not panic:
            out.append({"side": "long", "setup": "dip", "strat": "mr", "stopDist": mcfg["stopAtr"] * atr, "atr": atr, "score": 100 - rv})
        if mcfg["shorts"] and c < smaL and rv > mcfg["rsiSell"] and c > smaX:
            out.append({"side": "short", "setup": "spike", "strat": "mr", "stopDist": mcfg["stopAtr"] * atr, "atr": atr, "score": rv})
    pcfg = cfg["strategies"].get("pullback", {"enabled": False})
    if pcfg["enabled"] and cls in pcfg["classes"] and smaL is not None:
        if c > smaL and es > ind["emaS"][i - 5] and c < ef and c > ef - 2.0 * atr and c > o:
            out.append({"side": "long", "setup": "pullback", "strat": "pullback", "stopDist": pcfg["stopAtr"] * atr, "atr": atr, "score": 60 + 10 * (ef - c) / atr})
    tcfg2 = cfg["strategies"].get("tom", {"enabled": False})
    if tcfg2["enabled"] and cls in tcfg2["classes"] and is_turn_of_month_signal_day(b["d"]):
        out.append({"side": "long", "setup": "turn of month", "strat": "tom", "stopDist": tcfg2["stopAtr"] * atr, "atr": atr, "score": 55})
    ccfg = cfg["strategies"].get("crypto", {"enabled": False})
    if ccfg["enabled"] and sym in ccfg["symbols"] and smaL is not None:
        if c > smaL and c > ind["dhi"][i] and c > o:
            out.append({"side": "long", "setup": "breakout", "strat": "crypto", "stopDist": ccfg["stopAtr"] * atr, "atr": atr, "score": 50})
    return out

def vol_scale(cfg, bars, ind, i):
    """Risk multiplier <= 1: shrinks when today's ATR (as % of price) is well above its trailing median."""
    v = cfg.get("volScale") or {}
    if not v.get("enabled") or i < 60 or not ind["atr"][i]:
        return 1.0
    lb = v.get("lookback", 252); hist = []
    for j in range(max(14, i - lb), i):
        a = ind["atr"][j]
        if a:
            hist.append(a / bars[j]["c"])
    if len(hist) < 40:
        return 1.0
    hist.sort(); med = hist[len(hist) // 2]
    now = ind["atr"][i] / bars[i]["c"]
    if now <= v.get("trigger", 1.25) * med:
        return 1.0
    return max(v.get("floor", 0.5), v.get("trigger", 1.25) * med / now)

def next_weekday(d):
    d = d + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d = d + dt.timedelta(days=1)
    return d

def is_turn_of_month_signal_day(date_str):
    """True on the session before the month's last weekday session (the order then fills at that last session's open)."""
    d = pdate(date_str)
    n1 = next_weekday(d)
    n2 = next_weekday(n1)
    return n1.month == d.month and n2.month != d.month

def watch_row(sym, bars, ind, i):
    atr, adx, ef, es = ind["atr"][i], ind["adx"][i], ind["emaF"][i], ind["emaS"][i]
    b = bars[i]
    regime = "none"
    if None not in (ef, es, adx) and ind["emaS"][i - 5] is not None:
        if b["c"] > es and ef > es and es > ind["emaS"][i - 5]:
            regime = "up"
        elif b["c"] < es and ef < es and es < ind["emaS"][i - 5]:
            regime = "down"
    return {
        "symbol": sym, "date": b["d"], "close": b["c"], "regime": regime, "adx": round(adx, 1) if adx else None,
        "atrPct": round(atr / b["c"] * 100, 2) if atr else None, "distEma20Atr": round((b["c"] - ef) / atr, 2) if (atr and ef) else None,
        "chg5Pct": round((b["c"] / bars[i - 5]["c"] - 1) * 100, 2) if i >= 5 else None,
        "chg20Pct": round((b["c"] / bars[i - 20]["c"] - 1) * 100, 2) if i >= 20 else None,
        "ema20": round(ef, INSTRUMENTS[sym]["dp"]) if ef else None, "ema50": round(es, INSTRUMENTS[sym]["dp"]) if es else None,
        "rsi2": round(ind["rsi"][i], 1) if ind["rsi"][i] is not None else None,
        "aboveSma200": (b["c"] > ind["smaL"][i]) if ind["smaL"][i] is not None else None,
        "donchianHi": ind["dhi"][i], "donchianLo": ind["dlo"][i],
    }

# ----------------------------------------------------------------------------------------------
# the daily processing loop
# ----------------------------------------------------------------------------------------------
_IND_CACHE = {}
def cached_indicators(allbars, cfg):
    """Indicators and date indexes for a bar set; cached so a per-date backtest does not recompute 30 instruments each day."""
    key = (id(allbars), cfg["atrLen"], cfg["emaFast"], cfg["emaSlow"], cfg["donchian"], cfg["smaLong"], cfg["smaExit"], cfg["rsiLen"], sum(len(b) for b in allbars.values()))
    hit = _IND_CACHE.get(key)
    if hit is None:
        if len(_IND_CACHE) > 4:
            _IND_CACHE.clear()
        hit = ({sym: indicators(bars, cfg) for sym, bars in allbars.items()}, {sym: {b["d"]: i for i, b in enumerate(bars)} for sym, bars in allbars.items()})
        _IND_CACHE[key] = hit
    return hit

def process(state, allbars, mode="live", no_orders=False, start=None, end=None, verbose=True, hourly=None):
    cfg, acc = state["config"], state["account"]
    ind, idx = cached_indicators(allbars, cfg)
    lastp = state["meta"]["lastProcessed"]
    newdates = set(); last_new = {}
    for sym, bars in allbars.items():
        lp = lastp.get(sym) or (start and (pdate(start) - dt.timedelta(days=1)).isoformat()) or bars[-1]["d"]
        for b in bars:
            if b["d"] > lp and (end is None or b["d"] <= end):
                newdates.add(b["d"]); last_new[sym] = b["d"]
    dates = sorted(newdates)
    closes = {}
    for sym, bars in allbars.items():         # latest known closes up to the first new date
        prior = [b for b in bars if not dates or b["d"] < dates[0]]
        if prior:
            closes[sym] = prior[-1]["c"]
    log = []
    for d in dates:
        day = {"date": d, "realized": 0.0, "realizedLoss": 0.0, "fills": [], "closes": [], "adverse": 0.0, "orders": []}
        day_start = acc["balance"]
        if acc["phaseStart"] is None:
            acc["phaseStart"] = d
        traded_syms = [sym for sym in allbars if d in idx[sym]]
        for sym in traded_syms:
            closes[sym] = allbars[sym][idx[sym][d]]["c"]
        for sym in traded_syms:
            i = idx[sym][d]; bar = allbars[sym][i]; atr = ind[sym]["atr"][i]
            conv = conv_rate(sym, closes)
            # 1. pending orders -> fills at the open
            for order in [o for o in state["orders"] if o["symbol"] == sym]:
                if order["createdDate"] >= d:
                    continue
                state["orders"].remove(order)
                gap_away = (bar["o"] > order["signalClose"] + cfg["gapSkipAtr"] * order["atr"]) if order["side"] == "long" else (bar["o"] < order["signalClose"] - cfg["gapSkipAtr"] * order["atr"])
                if gap_away:
                    record_event(state, d, "cancel", "%s %s %s not filled: opened more than %.1f ATR away from the signal close" % (order["id"], order["side"], sym, cfg["gapSkipAtr"]))
                    continue
                if acc["flattenNextOpen"]:
                    record_event(state, d, "cancel", "%s %s %s cancelled: day guard flattening" % (order["id"], order["side"], sym))
                    continue
                fill = bar["o"] + half_spread(sym, bar["o"]) * (1 if order["side"] == "long" else -1)
                pos = open_position(state, order, fill, d, closes)
                if pos is None:
                    continue
                state["positions"].append(pos)
                day["fills"].append(pos)
                record_event(state, d, "open", "%s %s %s %s at %s, stop %s, risk %.0f USD (%s)" % (pos["id"], pos["side"], fmt_lots(sym, pos), sym, fmt_price(sym, fill), fmt_price(sym, pos["stop"]), pos["riskUSD"], pos["setup"]))
            # 2. open position on this symbol: exits (hourly-timeframe positions are managed by the hourly loop)
            for pos in [p for p in state["positions"] if p["symbol"] == sym and p.get("tf", "1d") == "1d"]:
                pos["bars"] += 1
                nights = (pdate(d) - pdate(pos["lastDate"])).days
                if nights > 0 and pos["entryDate"] != d:
                    if INSTRUMENTS[sym]["cls"] == "fx" and pdate(d).weekday() == 3:
                        nights += 2        # Wednesday-night triple swap on FX, booked on the Thursday bar
                    pos["swapUSD"] += swap_charge(sym, pos, nights, closes)
                pos["lastDate"] = d
                exit_price, reason = None, None
                if acc["flattenNextOpen"] or pos.get("exitNextOpen"):
                    exit_price = bar["o"] - half_spread(sym, bar["o"]) * (1 if pos["side"] == "long" else -1)
                    reason = "day guard flatten" if acc["flattenNextOpen"] else pos["exitNextOpen"]
                elif pos["side"] == "long":
                    if bar["o"] <= pos["stop"]:
                        exit_price, reason = bar["o"] - half_spread(sym, bar["o"]), "stop (gap)"
                    elif bar["l"] <= pos["stop"]:
                        exit_price, reason = pos["stop"] - half_spread(sym, pos["stop"]), "stop"
                else:
                    if bar["o"] >= pos["stop"]:
                        exit_price, reason = bar["o"] + half_spread(sym, bar["o"]), "stop (gap)"
                    elif bar["h"] >= pos["stop"]:
                        exit_price, reason = pos["stop"] + half_spread(sym, pos["stop"]), "stop"
                # adverse excursion for the worst-case equity check
                adverse_px = bar["l"] if pos["side"] == "long" else bar["h"]
                if exit_price is not None and reason.startswith("stop"):
                    adverse_px = exit_price
                day["adverse"] += min(0.0, pos_value(pos, adverse_px, conv))
                if exit_price is not None:
                    state["positions"].remove(pos)
                    close_position(state, pos, exit_price, d, reason, closes, day)
                    continue
                strat = pos.get("strat", "trend")
                if strat == "mr":
                    smaX = ind[sym]["smaX"][i]; m = cfg["strategies"]["mr"]
                    if smaX is not None and ((pos["side"] == "long" and bar["c"] > smaX) or (pos["side"] == "short" and bar["c"] < smaX)):
                        pos["exitNextOpen"] = "mean reached (close back through the 5-day average)"
                    elif pos["bars"] >= m["maxBars"]:
                        pos["exitNextOpen"] = "time stop (%d bars)" % m["maxBars"]
                elif strat == "pullback":
                    ef = ind[sym]["emaF"][i]; pc = cfg["strategies"]["pullback"]
                    if ef is not None and bar["c"] > ef:
                        pos["exitNextOpen"] = "pullback resolved (close back above the 20-day EMA)"
                    elif pos["bars"] >= pc["maxBars"]:
                        pos["exitNextOpen"] = "time stop (%d bars)" % pc["maxBars"]
                elif strat == "tom":
                    tc = cfg["strategies"]["tom"]
                    if pos["bars"] >= tc["holdBars"]:
                        pos["exitNextOpen"] = "turn-of-month window over (%d sessions held)" % tc["holdBars"]
                else:
                    update_trailing(state, pos, bar, atr)
                    es = ind[sym]["emaS"][i]
                    if cfg["strategies"]["trend"]["emaExit"] and es is not None and ((pos["side"] == "long" and bar["c"] < es) or (pos["side"] == "short" and bar["c"] > es)):
                        pos["exitNextOpen"] = "trend lost (close through EMA50)"
        if acc["flattenNextOpen"] and not state["positions"]:
            acc["flattenNextOpen"] = False
        # 3. end of day: equity, worst case, rules
        floating = sum(pos_value(p, p.get("lastPrice") or closes[p["symbol"]], conv_rate(p["symbol"], closes)) + p["swapUSD"] for p in state["positions"])
        equity = acc["balance"] + floating
        worst = day_start - day["realizedLoss"] + day["adverse"] + sum(p["swapUSD"] for p in state["positions"])
        worst = min(worst, equity)
        day_pnl = equity - day_start
        acc["lastDate"], acc["lastEquity"], acc["lastWorst"], acc["lastDayPnl"] = d, equity, worst, day_pnl
        acc["peakBalance"] = max(acc["peakBalance"], acc["balance"])
        if day["fills"] or day["closes"]:
            if d not in acc["tradingDays"]:
                acc["tradingDays"].append(d)
        breach = breach_check(state, worst, acc["balance"])
        point = {"date": d, "balance": r2(acc["balance"]), "equity": r2(equity), "worst": r2(worst), "phase": acc["phase"], "attempt": acc["attempt"], "dayPnl": r2(day_pnl)}
        if breach:
            for pos in list(state["positions"]):
                state["positions"].remove(pos)
                px = closes[pos["symbol"]]
                close_position(state, pos, px, d, "account failed", closes, day)
            state["orders"] = []
            acc["phaseHistory"].append({"attempt": acc["attempt"], "phase": acc["phase"], "start": acc["phaseStart"], "end": d, "result": "failed", "reason": breach, "balance": r2(acc["balance"]), "tradingDays": len(acc["tradingDays"])})
            record_event(state, d, "fail", breach + " -> attempt %d ends; new evaluation starts (fee %s %.0f)" % (acc["attempt"], cfg.get("feeCurrency", "EUR"), cfg_fee(cfg)))
            point["event"] = "failed"
            reset_account(acc, cfg, "challenge", acc["attempt"] + 1, fee_add=cfg_fee(cfg))
            state["equity"].append(point)
            log.append("%s FAILED: %s" % (d, breach))
            for sym in traded_syms:
                lastp[sym] = d
            continue
        # phase pass / payouts
        target = phase_target(state)
        if target is not None and min_days_met(state):
            if state["positions"] and equity >= target * (1 + cfg["passBufferPct"] / 100):
                for pos in list(state["positions"]):
                    state["positions"].remove(pos)
                    px = closes[pos["symbol"]] - half_spread(pos["symbol"], closes[pos["symbol"]]) * (1 if pos["side"] == "long" else -1)
                    close_position(state, pos, px, d, "locking the phase target", closes, day)
                state["orders"] = []
                floating = 0.0; equity = acc["balance"]
                point["balance"], point["equity"] = r2(acc["balance"]), r2(equity)
            if not state["positions"] and acc["balance"] >= target:
                old = acc["phase"]
                acc["phaseHistory"].append({"attempt": acc["attempt"], "phase": old, "start": acc["phaseStart"], "end": d, "result": "passed", "balance": r2(acc["balance"]), "tradingDays": len(acc["tradingDays"])})
                nxt = next_phase(cfg, old)
                record_event(state, d, "pass", "%s passed with balance %.2f -> %s starts" % (old, acc["balance"], nxt))
                point["event"] = "passed " + old
                state["orders"] = []
                reset_account(acc, cfg, nxt, acc["attempt"], last_payout=d)
                equity = cfg["initial"]
                log.append("%s PASSED %s" % (d, old))
        if acc["phase"] == "funded" and acc["phaseStart"]:
            since = pdate(acc["lastPayoutDate"] or acc["phaseStart"])
            lad = cfg.get("fundedLadder") or {}
            keep = cfg["initial"] * (1 + (lad.get("keepBufferPct", 0.0) if lad.get("enabled") else 0.0) / 100)
            if (pdate(d) - since).days >= cfg["rewardWaitDays"] and acc["balance"] > keep + 100:
                profit = acc["balance"] - keep
                share = profit * cfg["rewardSplitPct"] / 100
                acc["payouts"].append({"date": d, "profit": r2(profit), "trader": r2(share), "firm": r2(profit - share)})
                acc["balance"] = keep; acc["lastPayoutDate"] = d; acc["peakBalance"] = keep
                record_event(state, d, "payout", "Reward claimed: profit %.2f above the kept buffer, trader share %.2f; balance back to %.0f" % (profit, share, keep))
                point["event"] = "payout"
                equity = acc["balance"] + floating
        state["equity"].append(point)
        acc["lastEquity"] = equity
        nxt = (pdate(d) + dt.timedelta(days=1)).isoformat()
        if acc["pragueDay"] is None or nxt > acc["pragueDay"]:
            acc["pragueDay"] = nxt
            acc["dayStartBalance"] = acc["balance"]
        # 4. day guards and new orders
        day_pnl_pct = day_pnl / cfg["initial"] * 100
        if day_pnl_pct <= cfg["dayGuardFlattenPct"] and state["positions"]:
            acc["flattenNextOpen"] = True
            record_event(state, d, "guard", "Day P&L %.2f%% of initial: every position will be closed at the next open" % day_pnl_pct)
        # live: a symbol's signal is queued on the last new bar that symbol has (so Friday's index signals are not lost when a weekend crypto bar is the last date)
        elig = traded_syms if mode == "backtest" else [sym for sym in traded_syms if last_new.get(sym) == d]
        want_orders = (not no_orders) and bool(elig) and not acc["flattenNextOpen"] and day_pnl_pct > cfg["dayGuardNoNewPct"]
        if (not want_orders) and elig and day_pnl_pct <= cfg["dayGuardNoNewPct"] and not no_orders:
            record_event(state, d, "guard", "Day P&L %.2f%% of initial: no new trades queued" % day_pnl_pct)
        if want_orders:
            queue_orders(state, allbars, ind, idx, d, closes, day, elig)
        for sym in traded_syms:
            lastp[sym] = d
        if verbose:
            log.append("%s %s a%d bal %.0f eq %.0f worst %.0f day %+.0f | fills %d closes %d orders %d | pos %d" % (
                d, acc["phase"][:4], acc["attempt"], acc["balance"], equity, worst, day_pnl, len(day["fills"]), len(day["closes"]), len(day["orders"]), len(state["positions"])))
    if hourly:
        for line in process_hourly(state, hourly, allbars, closes, mode=mode, no_orders=no_orders, start=start, end=end):
            log.append(line)
    # watchlist snapshot from the latest bar of each instrument
    state["watch"] = []
    for sym, bars in allbars.items():
        i = len(bars) - 1
        if i > cfg["emaSlow"] + 6:
            state["watch"].append(watch_row(sym, bars, ind[sym], i))
    state["meta"]["lastRunAt"] = iso(now_utc())
    state["meta"]["runs"] += 1
    return dates, log


# ----------------------------------------------------------------------------------------------
# intraday component: hourly-bar breakouts in the metals, same account, same rules
# ----------------------------------------------------------------------------------------------
def hourly_indicators(bars, cfg):
    i = cfg["strategies"]["intraday"]
    c = [b["c"] for b in bars]; h = [b["h"] for b in bars]; l = [b["l"] for b in bars]
    tr = [h[0] - l[0]] + [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1])) for k in range(1, len(bars))]
    atr = wilder(tr, i["atrLen"])
    D = i["donchian"]
    don = [None] * len(bars)
    for k in range(D, len(bars)):
        don[k] = max(h[k - D:k])
    return {"atr": atr, "don": don}

def daily_trend_ok(daily_bars, date):
    """Previous completed daily close above its 200- and 50-day averages (long-only filter)."""
    prior = [b["c"] for b in daily_bars if b["d"] < date]
    if len(prior) < 200:
        return False
    s200 = sum(prior[-200:]) / 200; s50 = sum(prior[-50:]) / 50
    return prior[-1] > s200 and prior[-1] > s50

def process_hourly(state, hourly, allbars, closes, mode="live", no_orders=False, start=None, end=None):
    cfg, acc = state["config"], state["account"]
    icfg = cfg["strategies"]["intraday"]
    if not icfg["enabled"] or not hourly:
        return []
    lastp = state["meta"].setdefault("lastProcessedHourly", {})
    hkey = ("hourly", id(hourly), icfg["donchian"], icfg["atrLen"], sum(len(b) for b in hourly.values()))
    hit = _IND_CACHE.get(hkey)
    if hit is None:
        if len(_IND_CACHE) > 4:
            _IND_CACHE.clear()
        hit = ({sym: hourly_indicators(bars, cfg) for sym, bars in hourly.items()}, {sym: {b["t"]: k for k, b in enumerate(bars)} for sym, bars in hourly.items()})
        _IND_CACHE[hkey] = hit
    ind, idx = hit
    events = []
    times = set()
    for sym, bars in hourly.items():
        lp = lastp.get(sym)
        for b in bars:
            if (lp is None and (start is None or b["date"] >= start) and (mode == "backtest" or b is bars[-1])) or (lp is not None and b["t"] > lp):
                if end is None or b["date"] <= end:
                    times.add(b["t"])
    if not times:
        return events
    for sym, bars in hourly.items():
        prior = [b for b in bars if b["t"] < min(times)]
        if prior:
            closes[sym] = prior[-1]["c"]
    day = {"date": None, "realized": 0.0, "realizedLoss": 0.0, "fills": [], "closes": [], "adverse": 0.0, "orders": []}
    for t in sorted(times):
        for sym in hourly:
            k = idx[sym].get(t)
            if k is None:
                continue
            bar = hourly[sym][k]; atr = ind[sym]["atr"][k]
            if atr is None:
                lastp[sym] = t; continue
            closes[sym] = bar["c"]
            if acc["pragueDay"] is None or bar["pday"] > acc["pragueDay"]:
                acc["pragueDay"] = bar["pday"]; acc["dayStartBalance"] = acc["balance"]
            if acc["phaseStart"] is None:
                acc["phaseStart"] = bar["pday"]
            if day["date"] != bar["pday"]:
                day = {"date": bar["pday"], "realized": 0.0, "realizedLoss": 0.0, "fills": [], "closes": [], "adverse": 0.0, "orders": []}
            conv = conv_rate(sym, closes)
            # pending hourly orders fill at this bar's open
            for order in [o for o in state["orders"] if o["symbol"] == sym and o.get("tf") == "1h"]:
                if order["createdT"] >= t:
                    continue
                state["orders"].remove(order)
                if bar["o"] > order["signalClose"] + icfg["gapSkipAtr"] * order["atr"]:
                    record_event(state, bar["pday"], "cancel", "%s long %s not filled: opened more than %.1f ATR above the signal close" % (order["id"], sym, icfg["gapSkipAtr"])); continue
                if acc["flattenNextOpen"]:
                    record_event(state, bar["pday"], "cancel", "%s long %s cancelled: day guard flattening" % (order["id"], sym)); continue
                fill = bar["o"] + half_spread(sym, bar["o"])
                pos = open_position(state, order, fill, bar["date"], closes)
                if pos is None:
                    continue
                pos["tf"] = "1h"; pos["entryT"] = t; pos["lastPrice"] = bar["c"]; pos["entryTime"] = dt.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M UTC")
                state["positions"].append(pos); day["fills"].append(pos)
                if bar["pday"] not in acc["tradingDays"]:
                    acc["tradingDays"].append(bar["pday"])
                record_event(state, bar["pday"], "open", "%s long %s %s at %s (%s), stop %s, risk %.0f USD (hourly breakout)" % (pos["id"], fmt_lots(sym, pos), sym, fmt_price(sym, fill), pos["entryTime"], fmt_price(sym, pos["stop"]), pos["riskUSD"]))
            # manage the hourly position
            for pos in [p for p in state["positions"] if p["symbol"] == sym and p.get("tf") == "1h"]:
                if pos.get("entryT") == t:
                    continue
                pos["bars"] += 1
                if pos["lastDate"] != bar["date"]:
                    nights = (pdate(bar["date"]) - pdate(pos["lastDate"])).days
                    if nights > 0:
                        pos["swapUSD"] += swap_charge(sym, pos, nights, closes)
                    pos["lastDate"] = bar["date"]
                exit_price, reason = None, None
                if acc["flattenNextOpen"] or pos.get("exitNextOpen"):
                    exit_price, reason = bar["o"] - half_spread(sym, bar["o"]), ("day guard flatten" if acc["flattenNextOpen"] else pos["exitNextOpen"])
                elif bar["o"] <= pos["stop"]:
                    exit_price, reason = bar["o"] - half_spread(sym, bar["o"]), "stop (gap)"
                elif bar["l"] <= pos["stop"]:
                    exit_price, reason = pos["stop"] - half_spread(sym, pos["stop"]), "stop"
                adverse_px = exit_price if (exit_price is not None and reason.startswith("stop")) else bar["l"]
                day["adverse"] = min(day["adverse"], pos_value(pos, adverse_px, conv))   # worst single excursion this day for this position
                if exit_price is not None:
                    state["positions"].remove(pos)
                    close_position(state, pos, exit_price, bar["date"], reason, closes, day)
                    if bar["pday"] not in acc["tradingDays"]:
                        acc["tradingDays"].append(bar["pday"])
                    continue
                pos["lastPrice"] = bar["c"]
                pos["extreme"] = max(pos["extreme"], bar["c"])
                if bar["c"] >= pos["entry"] + icfg["trailAtR"] * pos["stopDist"]:
                    pos["trailOn"] = True
                if pos["trailOn"]:
                    pos["stop"] = max(pos["stop"], pos["extreme"] - icfg["trailAtr"] * atr)
                so = (icfg.get("scaleOutAtR") or {}).get(acc["phase"])
                if so and not pos.get("scaled") and bar["c"] >= pos["entry"] + so * pos["stopDist"]:
                    px = bar["c"] - half_spread(sym, bar["c"])
                    if partial_close(state, pos, px, bar["date"], 0.5, "half banked at +%.0fR" % so, closes, day):
                        pos["stop"] = max(pos["stop"], pos["entry"] + 0.1 * pos["stopDist"])
                        record_event(state, bar["pday"], "scale", "%s: half of the %s position banked at %s (+%.0fR); stop on the rest moved to %s" % (pos["id"], sym, fmt_price(sym, px), so, fmt_price(sym, pos["stop"])))
            lastp[sym] = t
            acc["lastHourlyBar"] = dt.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M UTC")
            # account check at this bar
            floating = sum(pos_value(p, p.get("lastPrice") or closes[p["symbol"]], conv_rate(p["symbol"], closes)) + p["swapUSD"] for p in state["positions"])
            equity = acc["balance"] + floating
            worst = min(equity, acc["balance"] + floating + day["adverse"] - max(0.0, pos_value_now(state, sym, closes)))
            acc["lastEquity"] = equity; acc["lastWorst"] = min(acc["lastWorst"], worst) if acc.get("lastDate") == bar["pday"] else worst
            acc["lastDayPnl"] = equity - acc["dayStartBalance"]
            breach = breach_check(state, worst, acc["balance"], " intraday")
            if breach:
                fail_attempt(state, bar["pday"], breach, closes, day)
                events.append("%s FAILED (intraday): %s" % (bar["pday"], breach))
                continue
            if maybe_pass(state, bar["pday"], equity, closes, day):
                events.append("%s PASSED (intraday)" % bar["pday"])
                continue
            day_pnl_pct = (equity - acc["dayStartBalance"]) / cfg["initial"] * 100
            if day_pnl_pct <= cfg["dayGuardFlattenPct"] and state["positions"] and not acc["flattenNextOpen"]:
                acc["flattenNextOpen"] = True
                record_event(state, bar["pday"], "guard", "Day P&L %.2f%% of initial (intraday): every position will be closed at the next open" % day_pnl_pct)
            # signal on this bar -> order for the next bar
            if no_orders or acc["flattenNextOpen"] or day_pnl_pct <= cfg["dayGuardNoNewPct"]:
                continue
            if any(p["symbol"] == sym for p in state["positions"]) or any(o["symbol"] == sym for o in state["orders"]):
                continue
            don = ind[sym]["don"][k]
            if don is None or not daily_trend_ok(allbars.get(sym, []), bar["date"]) or INSTRUMENTS[sym]["cls"] in cfg.get("excludeClasses", []):
                continue
            if bar["c"] > don and bar["c"] > bar["o"] and (bar["h"] - bar["l"]) >= icfg["minRangeAtr"] * atr:
                hrs = icfg.get("entryHoursUTC")
                if hrs and dt.datetime.fromtimestamp(t, UTC).hour not in hrs:
                    continue
                if not intraday_slot_free(state, sym, closes):
                    continue
                risk, mult = risk_per_trade(state, "intraday")
                order = {"id": nid(state, "O"), "symbol": sym, "side": "long", "setup": "hourly breakout", "strat": "intraday", "tf": "1h",
                         "stopDist": icfg["stopAtr"] * atr, "riskUSD": r2(risk), "atr": atr, "signalClose": bar["c"], "createdDate": bar["pday"], "createdT": t,
                         "score": 0.0, "riskMult": mult,
                         "reason": "long %s: close above the prior %d-hour high at %s with the daily trend up; stop %.1f ATR(1h) below, trail %.1f ATR after +1R" % (sym, icfg["donchian"], dt.datetime.fromtimestamp(t, UTC).strftime("%H:%M UTC"), icfg["stopAtr"], icfg["trailAtr"])}
                state["orders"].append(order); day["orders"].append(order)
                record_event(state, bar["pday"], "order", "%s queued: long %s at the next hourly open, stop %s away (hourly breakout, risk %.0f USD)" % (order["id"], sym, fmt_price(sym, order["stopDist"]), risk))
    return events

def pos_value_now(state, sym, closes):
    return 0.0

def intraday_slot_free(state, sym, closes):
    cfg = state["config"]
    if len(state["positions"]) + len(state["orders"]) >= max_positions(state):
        return False
    g = INSTRUMENTS[sym]["group"]
    if sum(1 for p in state["positions"] + state["orders"] if INSTRUMENTS[p["symbol"]]["group"] == g) >= cfg["groupCaps"][g]:
        return False
    u = usd_direction(sym, "long")
    if u and sum(1 for p in state["positions"] + state["orders"] if usd_direction(p["symbol"], p["side"]) == u) >= cfg["maxSameUsdDirection"]:
        return False
    open_risk_1h = sum(risk_to_stop(p, conv_rate(p["symbol"], closes)) for p in state["positions"] if p.get("tf") == "1h") + sum(o["riskUSD"] for o in state["orders"] if o.get("tf") == "1h")
    slice_pct = min(cfg["strategies"]["intraday"]["riskPct"], phase_risk_pct(state))
    return open_risk_1h + cfg["initial"] * slice_pct / 100 <= cfg["initial"] * slice_pct / 100 + 1e-6

def fail_attempt(state, d, breach, closes, day):
    cfg, acc = state["config"], state["account"]
    for pos in list(state["positions"]):
        state["positions"].remove(pos)
        close_position(state, pos, pos.get("lastPrice") or closes[pos["symbol"]], d, "account failed", closes, day)
    state["orders"] = []
    acc["phaseHistory"].append({"attempt": acc["attempt"], "phase": acc["phase"], "start": acc["phaseStart"], "end": d, "result": "failed", "reason": breach, "balance": r2(acc["balance"]), "tradingDays": len(acc["tradingDays"])})
    record_event(state, d, "fail", breach + " -> attempt %d ends; new evaluation starts (fee %s %.0f)" % (acc["attempt"], cfg.get("feeCurrency", "EUR"), cfg_fee(cfg)))
    state["equity"].append({"date": d, "balance": r2(acc["balance"]), "equity": r2(acc["balance"]), "worst": r2(acc["balance"]), "phase": acc["phase"], "attempt": acc["attempt"], "dayPnl": r2(acc["balance"] - acc["dayStartBalance"]), "event": "failed"})
    reset_account(acc, cfg, "challenge", acc["attempt"] + 1, fee_add=cfg_fee(cfg))

def maybe_pass(state, d, equity, closes, day):
    cfg, acc = state["config"], state["account"]
    target = phase_target(state)
    if target is None or not min_days_met(state):
        return False
    if state["positions"] and equity >= target * (1 + cfg["passBufferPct"] / 100):
        for pos in list(state["positions"]):
            state["positions"].remove(pos)
            px = (pos.get("lastPrice") or closes[pos["symbol"]])
            px = px - half_spread(pos["symbol"], px) * (1 if pos["side"] == "long" else -1)
            close_position(state, pos, px, d, "locking the phase target", closes, day)
        state["orders"] = []
    if not state["positions"] and acc["balance"] >= target:
        old = acc["phase"]
        acc["phaseHistory"].append({"attempt": acc["attempt"], "phase": old, "start": acc["phaseStart"], "end": d, "result": "passed", "balance": r2(acc["balance"]), "tradingDays": len(acc["tradingDays"])})
        nxt = next_phase(cfg, old)
        record_event(state, d, "pass", "%s passed with balance %.2f -> %s starts" % (old, acc["balance"], nxt))
        state["equity"].append({"date": d, "balance": r2(acc["balance"]), "equity": r2(acc["balance"]), "worst": r2(acc["balance"]), "phase": old, "attempt": acc["attempt"], "dayPnl": r2(acc["balance"] - acc["dayStartBalance"]), "event": "passed " + old})
        state["orders"] = []
        reset_account(acc, cfg, nxt, acc["attempt"], last_payout=d)
        return True
    return False

def fmt_lots(sym, pos):
    cls = INSTRUMENTS[sym]["cls"]
    if cls in ("fx", "metal"):
        return "%.2f lots" % lots_of(sym, pos["units"])
    if cls == "index":
        return "$%.1f/pt" % pos["units"]
    return "%d sh" % pos["units"]

def queue_orders(state, allbars, ind, idx, d, closes, day, traded_syms):
    cfg, acc = state["config"], state["account"]
    cands = []
    isyms = set(cfg["strategies"]["intraday"]["symbols"]) if cfg["strategies"]["intraday"]["enabled"] else set()
    excl = set(cfg.get("excludeClasses", []))
    for sym in traded_syms:
        if sym in isyms or INSTRUMENTS[sym]["cls"] in excl:
            continue
        if any(p["symbol"] == sym for p in state["positions"]) or any(o["symbol"] == sym for o in state["orders"]):
            continue
        for sig in signals_at(sym, allbars[sym], ind[sym], idx[sym][d], cfg):
            cands.append((sig["score"], sym, sig))
    cands.sort(key=lambda x: -x[0])
    risk, mult = risk_per_trade(state)
    maxpos = max_positions(state)
    open_risk = sum(risk_to_stop(p, conv_rate(p["symbol"], closes)) for p in state["positions"] if p.get("tf", "1d") == "1d") + sum(o["riskUSD"] for o in state["orders"] if o.get("tf", "1d") == "1d")
    slots = maxpos - len(state["positions"]) - len(state["orders"])
    group_count, cluster_count = {}, {}
    usd_count = {"usd_long": 0, "usd_short": 0}
    for p in state["positions"] + state["orders"]:
        g = INSTRUMENTS[p["symbol"]]["group"]; group_count[g] = group_count.get(g, 0) + 1
        cl = cfg["clusters"].get(g)
        if cl:
            cluster_count[cl] = cluster_count.get(cl, 0) + 1
        u = usd_direction(p["symbol"], p["side"])
        if u:
            usd_count[u] += 1
    added = 0
    for score, sym, sig in cands:
        side, setup, stop_dist, atr, strat = sig["side"], sig["setup"], sig["stopDist"], sig["atr"], sig["strat"]
        if added >= cfg["maxNewPerRun"] or slots <= 0:
            break
        if any(o["symbol"] == sym for o in state["orders"]):
            continue
        g = INSTRUMENTS[sym]["group"]
        if group_count.get(g, 0) >= cfg["groupCaps"][g]:
            continue
        cl = cfg["clusters"].get(g)
        if cl and cluster_count.get(cl, 0) >= cfg["clusterCaps"][cl]:
            continue
        u = usd_direction(sym, side)
        if u and usd_count[u] >= cfg["maxSameUsdDirection"]:
            continue
        if open_risk + risk > cfg["initial"] * cfg["maxOpenRiskPct"] / 100 + 1e-6:
            continue
        b = allbars[sym][idx[sym][d]]
        vsc = vol_scale(cfg, allbars[sym], ind[sym], idx[sym][d])
        trade_risk = risk * cfg["strategies"].get(strat, {}).get("riskMult", 1.0) * vsc
        if open_risk + trade_risk > cfg["initial"] * cfg["maxOpenRiskPct"] / 100 + 1e-6:
            continue
        ii = idx[sym][d]
        if strat == "trend":
            why = "%s %s: 20-day %s breakout in a %strend (ADX %.0f), stop %.1f ATR away" % (side, sym, "high" if side == "long" else "low", "up" if side == "long" else "down", ind[sym]["adx"][ii], stop_dist / atr)
        elif strat == "mr":
            why = "%s %s: RSI(2) %.0f dip above the 200-day average; exit on a close back above the 5-day average, disaster stop %.1f ATR away" % (side, sym, ind[sym]["rsi"][ii], stop_dist / atr)
        elif strat == "pullback":
            why = "long %s: pullback %.1f ATR under the 20-day EMA with a rising 50-day EMA, above the 200-day average; exit on a close back above the 20-day EMA or after %d bars, stop %.1f ATR away" % (sym, (ind[sym]["emaF"][ii] - b["c"]) / atr, cfg["strategies"]["pullback"]["maxBars"], stop_dist / atr)
        elif strat == "tom":
            why = "long %s: turn-of-month seasonality - buy the open of the month's last session, hold %d sessions into the new month, stop %.1f ATR away" % (sym, cfg["strategies"]["tom"]["holdBars"], stop_dist / atr)
        else:
            why = "long %s: 20-day high above the 200-day average (crypto sleeve, half risk); 2.5 ATR stop, 3 ATR trail after +1R" % sym
        if vsc < 1.0:
            why += " (risk scaled x%.2f: volatility well above its 1-year norm)" % vsc
        order = {"id": nid(state, "O"), "symbol": sym, "side": side, "setup": setup, "strat": strat, "stopDist": stop_dist, "riskUSD": r2(trade_risk), "atr": atr,
                 "signalClose": b["c"], "createdDate": d, "score": round(score, 1), "riskMult": mult * vsc, "reason": why}
        state["orders"].append(order); day["orders"].append(order)
        record_event(state, d, "order", "%s queued: %s %s at next open, stop %s away (%s, score %.0f, risk %.0f USD)" % (order["id"], side, sym, fmt_price(sym, stop_dist), setup, score, trade_risk))
        added += 1; slots -= 1; open_risk += trade_risk
        group_count[g] = group_count.get(g, 0) + 1
        if cl:
            cluster_count[cl] = cluster_count.get(cl, 0) + 1
        if u:
            usd_count[u] += 1

# ----------------------------------------------------------------------------------------------
# reporting / export
# ----------------------------------------------------------------------------------------------
def stats(trades):
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t["netUSD"] > 0]; losses = [t for t in trades if t["netUSD"] <= 0]
    gw = sum(t["netUSD"] for t in wins); gl = -sum(t["netUSD"] for t in losses)
    return {"n": len(trades), "wins": len(wins), "losses": len(losses), "winRatePct": r2(len(wins) / len(trades) * 100),
            "avgR": round(sum(t["r"] for t in trades) / len(trades), 2), "profitFactor": round(gw / gl, 2) if gl > 0 else None,
            "netUSD": r2(sum(t["netUSD"] for t in trades)), "best": max(t["netUSD"] for t in trades), "worst": min(t["netUSD"] for t in trades),
            "avgBars": round(sum(t["bars"] for t in trades) / len(trades), 1)}

def account_row(state, closes=None):
    cfg, acc = state["config"], state["account"]
    target = phase_target(state)
    equity = acc["lastEquity"]
    cur_trades = [t for t in state["trades"] if t["attempt"] == acc["attempt"] and t["phase"] == acc["phase"]]
    st = stats(cur_trades)
    allst = stats(state["trades"])
    daily_floor, max_floor = floors(state)
    risk, mult = risk_per_trade(state)
    dd_pct = (equity - cfg["initial"]) / cfg["initial"] * 100
    firm = cfg.get("firm", "FTMO")
    names = {"challenge": "%s Challenge" % firm if len(phase_names(cfg)) == 3 else "%s Evaluation" % firm, "verification": "Verification", "funded": "%s funded account" % firm}
    eq_pts = [p for p in state["equity"] if p["attempt"] == acc["attempt"] and p["phase"] == acc["phase"]]
    peak = cfg["initial"]; maxdd = 0.0
    for p in eq_pts:
        peak = max(peak, p["equity"]); maxdd = max(maxdd, peak - p["worst"])
    return {
        "asOf": state["meta"]["lastRunAt"], "lastBarDate": acc["lastDate"], "product": cfg["product"], "currency": cfg["currency"],
        "phase": names[acc["phase"]], "firm": firm, "program": cfg.get("program", ""), "accountId": cfg.get("accountId", "main"),
        "phaseKey": acc["phase"], "attempt": acc["attempt"], "phaseStart": acc["phaseStart"], "initial": cfg["initial"],
        "balance": r2(acc["balance"]), "equity": r2(equity), "floating": r2(equity - acc["balance"]), "dayPnl": r2(acc["lastDayPnl"]),
        "dayPnlPct": round(acc["lastDayPnl"] / cfg["initial"] * 100, 2), "worstEquityToday": r2(acc["lastWorst"]),
        "netPct": round(dd_pct, 2), "target": r2(target) if target else None, "targetPct": phase_target_pct(state),
        "toTarget": r2(target - acc["balance"]) if target else None, "progressPct": round((acc["balance"] - cfg["initial"]) / (target - cfg["initial"]) * 100, 1) if target else None,
        "dailyFloor": r2(daily_floor) if daily_floor is not None else None, "dailyRoom": r2(equity - daily_floor) if daily_floor is not None else None, "maxFloor": r2(max_floor), "maxRoom": r2(equity - max_floor),
        "dayStartBalance": r2(acc["dayStartBalance"]), "maxDailyLossPct": cfg.get("maxDailyLossPct"), "maxLossPct": cfg["maxLossPct"],
        "tradingDays": len(acc["tradingDays"]), "minTradingDays": cfg["minTradingDays"], "profitableDays": profitable_days(state), "minProfitableDays": cfg.get("minProfitableDays", 0),
        "openPositions": len(state["positions"]), "pendingOrders": len(state["orders"]),
        "openRisk": r2(sum(p["riskUSD"] if not p["beDone"] else 0 for p in state["positions"])),
        "riskPerTrade": r2(risk), "riskMult": mult, "riskPctPhase": phase_risk_pct(state), "maxPositionsNow": max_positions(state),
        "lossStreak": acc["lossStreak"], "feesPaidEUR": acc["feesPaidEUR"], "feesPaid": acc["feesPaidEUR"], "feeCurrency": cfg.get("feeCurrency", "EUR"), "payoutsTotal": r2(sum(p["trader"] for p in acc["payouts"])),
        "payoutsCount": len(acc["payouts"]), "maxDrawdownPhase": r2(maxdd),
        "phaseTradesN": st.get("n", 0), "phaseWinRatePct": st.get("winRatePct"), "phaseAvgR": st.get("avgR"), "phaseProfitFactor": st.get("profitFactor"), "phaseNetUSD": st.get("netUSD", 0.0),
        "allTradesN": allst.get("n", 0), "allWinRatePct": allst.get("winRatePct"), "allAvgR": allst.get("avgR"), "allProfitFactor": allst.get("profitFactor"), "allNetUSD": allst.get("netUSD", 0.0),
        "schedule": "Runs every 3 hours Mon-Sat (00:15, 03:15 ... 21:15 UTC); the US-close run at 00:15 UTC (11:15 Sydney) handles the daily system, every run handles the hourly metals component", "dataSource": "Daily OHLC (indices, US stocks, gold, silver, Bitcoin and Ether) plus hourly bars for gold and silver from the Yahoo Finance chart feed (GC=F/SI=F futures as spot proxies), Alpha Vantage as fallback",
        "lastHourlyBar": acc.get("lastHourlyBar"),
        "attemptsFailed": len([h for h in acc["phaseHistory"] if h["result"] == "failed"]), "phasesPassed": len([h for h in acc["phaseHistory"] if h["result"] == "passed"]),
        "runs": state["meta"]["runs"], "flattenNextOpen": acc["flattenNextOpen"], "status": acc["status"],
    }

def positions_rows(state, closes):
    rows = []
    for p in state["positions"]:
        sym = p["symbol"]; px = closes.get(sym, p["entry"]); conv = conv_rate(sym, closes)
        pnl = pos_value(p, px, conv) + p["swapUSD"] - p["commUSD"]
        if p.get("tf") == "1h" and p.get("lastPrice"):
            px = p["lastPrice"]; pnl = pos_value(p, px, conv) + p["swapUSD"] - p["commUSD"]
        rows.append({"id": p["id"], "symbol": sym, "side": p["side"], "size": fmt_lots(sym, p), "units": p["units"], "entry": p["entry"], "entryDate": p.get("entryTime") or p["entryDate"], "tf": p.get("tf", "1d"),
                     "price": px, "stop": round(p["stop"], INSTRUMENTS[sym]["dp"]), "initialStop": round(p["initialStop"], INSTRUMENTS[sym]["dp"]), "riskUSD": p["riskUSD"],
                     "riskNow": r2(risk_to_stop(p, conv)), "pnlUSD": r2(pnl), "r": round(pnl / p["riskUSD"], 2) if p["riskUSD"] else 0, "swapUSD": r2(p["swapUSD"]),
                     "bars": p["bars"], "setup": p["setup"], "stage": "trailing" if p["trailOn"] else ("breakeven" if p["beDone"] else "initial"), "thesis": p["reason"],
                     "exitPlan": p.get("exitNextOpen") or ""})
    return rows

def orders_rows(state):
    return [{"id": o["id"], "symbol": o["symbol"], "side": o["side"], "setup": o["setup"], "signalClose": o["signalClose"], "stopDist": round(o["stopDist"], INSTRUMENTS[o["symbol"]]["dp"]),
             "riskUSD": o["riskUSD"], "score": o["score"], "createdDate": o["createdDate"], "fills": "next hourly open" if o.get("tf") == "1h" else "next open", "reason": o["reason"]} for o in state["orders"]]

def rows_or_placeholder(rows, key):
    """The dashboard cannot read a zero-row file, so an empty list becomes one flagged placeholder row the page hides."""
    return rows if rows else [{key: "none", "empty": True}]

def export_state(state, bars_dir, out_dir):
    closes = latest_closes(bars_dir) if bars_dir else {}
    os.makedirs(out_dir, exist_ok=True)
    save_json(os.path.join(out_dir, "account.json"), [account_row(state)])
    save_json(os.path.join(out_dir, "positions.json"), rows_or_placeholder(positions_rows(state, closes) if closes else [], "id"))
    save_json(os.path.join(out_dir, "orders.json"), rows_or_placeholder(orders_rows(state), "id"))
    save_json(os.path.join(out_dir, "trades.json"), rows_or_placeholder(list(reversed(state["trades"])), "id"))
    save_json(os.path.join(out_dir, "equity.json"), state["equity"])
    save_json(os.path.join(out_dir, "journal.json"), rows_or_placeholder(list(reversed(state["journal"])), "at"))
    save_json(os.path.join(out_dir, "watchlist.json"), state["watch"])
    ev = state["events"][-120:]
    base = len(state["events"]) - len(ev)
    save_json(os.path.join(out_dir, "events.json"), rows_or_placeholder(list(reversed([dict(e, n=base + i + 1) for i, e in enumerate(ev)])), "n"))
    save_json(os.path.join(out_dir, "history.json"), rows_or_placeholder([dict(h, n=i + 1) for i, h in enumerate(reversed(state["account"]["phaseHistory"]))], "n"))

def cmd_export(a):
    state = load_json(a.state)
    export_state(state, a.bars, a.out)
    print("exported to", a.out)

# ----------------------------------------------------------------------------------------------
# portfolio: many accounts (one per firm/program/size), same engine and signals, each under its own rulebook
# ----------------------------------------------------------------------------------------------
def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out

def account_config(acct):
    cfg = deep_merge(DEFAULT_CONFIG, acct.get("config", {}))
    cfg["accountId"] = acct["id"]; cfg["firm"] = acct.get("firm", cfg.get("firm")); cfg["program"] = acct.get("program", cfg.get("program"))
    cfg["initial"] = float(acct.get("initial", cfg["initial"]))
    if "product" in acct:
        cfg["product"] = acct["product"]
    cfg["configVersion"] = CONFIG_VERSION
    return cfg

def load_pack(path):
    return load_json(path)

def pack_accounts(pack):
    return [a for a in pack["manifest"]["accounts"] if a.get("enabled", True)]

def cmd_portfolio_init(a):
    manifest = load_json(a.manifest)
    data = load_json(os.path.join(a.bars, "all.json"))
    hourly = load_hourly_bars(a.bars)
    pack = {"manifest": manifest, "states": {}, "createdAt": iso(now_utc())}
    for acct in manifest["accounts"]:
        cfg = account_config(acct)
        state = new_state(cfg)
        for sym, bars in data["bars"].items():
            if len(bars) >= 2:
                state["meta"]["lastProcessed"][sym] = bars[-2]["d"]
        for sym, hb in hourly.items():
            if len(hb) >= 2:
                state["meta"]["lastProcessedHourly"][sym] = hb[-2]["t"]
        pack["states"][acct["id"]] = state
        print("initialised %-16s %-28s %s %.0f x%d" % (acct["id"], acct.get("firm", "") + " " + acct.get("program", ""), cfg["currency"], cfg["initial"], acct.get("copies", 1)))
    save_json(a.out, pack)
    print("saved", a.out)

def cmd_portfolio_run(a):
    pack = load_pack(a.states)
    data = load_json(os.path.join(a.bars, "all.json"))
    if data.get("failures"):
        print("WARNING bars missing for:", ", ".join(data["failures"]))
    allbars = data["bars"]; hourly = load_hourly_bars(a.bars)
    save_json(a.states + ".bak", pack)
    changed = False
    for acct in pack_accounts(pack):
        state = pack["states"].get(acct["id"])
        if state is None:
            print("%-16s missing state; run portfolio-init for it" % acct["id"]); continue
        state["config"] = account_config(acct)          # the manifest is the source of truth for every account's rules
        before_h = dict(state["meta"].get("lastProcessedHourly", {}))
        dates, log = process(state, allbars, mode=a.mode, no_orders=a.no_orders, hourly=hourly, verbose=False)
        new_h = {s: t for s, t in state["meta"].get("lastProcessedHourly", {}).items() if before_h.get(s) != t}
        row = account_row(state)
        if dates or new_h:
            changed = True
        print("%-16s %-32s %-22s bal %10.0f eq %10.0f (%+.2f%%) day %+8.0f | pos %d ord %d | %s" % (
            acct["id"], (acct.get("firm", "") + " " + acct.get("program", ""))[:32], row["phase"][:22], row["balance"], row["equity"], row["netPct"], row["dayPnl"],
            row["openPositions"], row["pendingOrders"], ("%d date(s), hourly to %s" % (len(dates), acc_last_hourly(state))) if (dates or new_h) else "nothing new"))
        for line in log:
            if "FAILED" in line or "PASSED" in line:
                print("   ", line)
        for e in state["events"][-8:]:
            if e["kind"] in ("open", "close", "order", "cancel", "fail", "pass", "payout", "guard", "scale", "config"):
                if e["date"] in dates or (new_h and e["kind"] in ("open", "close", "order", "scale")):
                    print("    EVENT", e["date"], e["kind"], e["text"])
    save_json(a.states, pack)
    print("No new completed bars since the last run. Nothing changed." if not changed else "portfolio updated")

def acc_last_hourly(state):
    return state["account"].get("lastHourlyBar") or "-"

def portfolio_rows(pack, closes):
    rows = []; tot = {"accounts": 0, "capital": 0.0, "fees": 0.0, "payouts": 0.0, "netPnl": 0.0, "funded": 0, "fundedCapital": 0.0, "failed": 0, "equity": 0.0}
    for acct in pack_accounts(pack):
        state = pack["states"].get(acct["id"])
        if not state:
            continue
        r = account_row(state); cfg = state["config"]; copies = int(acct.get("copies", 1))
        fx = acct.get("feeToUSD", 1.0)
        fees_usd = r["feesPaid"] * fx * copies
        rows.append({"id": acct["id"], "firm": acct.get("firm", ""), "program": acct.get("program", ""), "copies": copies, "size": cfg["initial"], "capital": cfg["initial"] * copies,
                     "rules": "%s / daily %s / max %s%% %s" % ("+".join("%g%%" % t for t in phase_targets(cfg)), "none" if cfg.get("maxDailyLossPct") is None else "%g%%" % cfg["maxDailyLossPct"], cfg["maxLossPct"], cfg.get("maxLossBasis", "equity")),
                     "phase": r["phase"], "attempt": r["attempt"], "balance": r["balance"], "equity": r["equity"], "netPct": r["netPct"], "dayPnl": r["dayPnl"],
                     "toTarget": r["toTarget"], "progressPct": r["progressPct"], "tradingDays": r["tradingDays"], "minTradingDays": r["minTradingDays"],
                     "openPositions": r["openPositions"], "pendingOrders": r["pendingOrders"], "maxRoom": r["maxRoom"], "dailyRoom": r["dailyRoom"],
                     "feesPaid": r2(fees_usd), "payouts": r2(r["payoutsTotal"] * copies), "attemptsFailed": r["attemptsFailed"], "phasesPassed": r["phasesPassed"],
                     "status": acct.get("status", "simulated"), "note": acct.get("note", "")})
        tot["accounts"] += copies; tot["capital"] += cfg["initial"] * copies; tot["fees"] += fees_usd; tot["payouts"] += r["payoutsTotal"] * copies
        tot["netPnl"] += (r["equity"] - cfg["initial"]) * copies; tot["equity"] += r["equity"] * copies
        if r["phaseKey"] == "funded":
            tot["funded"] += copies; tot["fundedCapital"] += cfg["initial"] * copies
        tot["failed"] += r["attemptsFailed"] * copies
    summary = {"asOf": iso(now_utc()), "accounts": tot["accounts"], "firms": len(set(a.get("firm", "") for a in pack_accounts(pack))), "capital": r2(tot["capital"]), "feesPaidUSD": r2(tot["fees"]), "feesPaidAUD": r2(tot["fees"] * pack["manifest"].get("usdToAud", 1.445)),
               "payoutsUSD": r2(tot["payouts"]), "payoutsAUD": r2(tot["payouts"] * pack["manifest"].get("usdToAud", 1.445)), "netPnlUSD": r2(tot["netPnl"]), "netPct": round(tot["netPnl"] / tot["capital"] * 100, 2) if tot["capital"] else 0,
               "fundedAccounts": tot["funded"], "fundedCapital": r2(tot["fundedCapital"]), "attemptsFailed": tot["failed"], "equity": r2(tot["equity"])}
    return rows, summary

def cmd_portfolio_export(a):
    pack = load_pack(a.states)
    closes = latest_closes(a.bars) if a.bars else {}
    focus = a.focus or pack["manifest"].get("focus") or pack_accounts(pack)[0]["id"]
    export_state(pack["states"][focus], a.bars, a.out)
    rows, summary = portfolio_rows(pack, closes)
    save_json(os.path.join(a.out, "portfolio.json"), rows_or_placeholder(rows, "id"))
    save_json(os.path.join(a.out, "psummary.json"), [summary])
    print("exported focus account %s plus portfolio (%d accounts, %.0f capital) to %s" % (focus, summary["accounts"], summary["capital"], a.out))

def cmd_portfolio_status(a):
    pack = load_pack(a.states)
    rows, summary = portfolio_rows(pack, {})
    print("PORTFOLIO %d accounts, capital %.0f, funded %d (%.0f), fees paid USD %.0f, payouts USD %.0f, net P&L %.0f (%+.2f%%), failed attempts %d" % (
        summary["accounts"], summary["capital"], summary["fundedAccounts"], summary["fundedCapital"], summary["feesPaidUSD"], summary["payoutsUSD"], summary["netPnlUSD"], summary["netPct"], summary["attemptsFailed"]))
    for r in rows:
        print("  %-16s %-28s x%d %8.0f | %-24s a%d bal %9.0f eq %9.0f %+6.2f%% | days %d/%d | pos %d ord %d | fees %6.0f payouts %7.0f" % (
            r["id"], (r["firm"] + " " + r["program"])[:28], r["copies"], r["size"], r["phase"][:24], r["attempt"], r["balance"], r["equity"], r["netPct"], r["tradingDays"], r["minTradingDays"], r["openPositions"], r["pendingOrders"], r["feesPaid"], r["payouts"]))
    if a.account:
        st = pack["states"][a.account]
        cmd_status_state(st, a.bars)

def cmd_status_state(state, bars_dir):
    row = account_row(state)
    print("%s | attempt %d | balance %.2f | equity %.2f (%+.2f%%) | day %+.2f" % (row["phase"], row["attempt"], row["balance"], row["equity"], row["netPct"], row["dayPnl"]))
    closes = latest_closes(bars_dir) if bars_dir else {}
    for p in positions_rows(state, closes) if closes else []:
        print("POS %s %s %s %s entry %s now %s stop %s pnl %+.2f (%+.2fR) %s %s" % (p["id"], p["side"], p["size"], p["symbol"], p["entry"], p["price"], p["stop"], p["pnlUSD"], p["r"], p["stage"], p["exitPlan"]))
    for o in state["orders"]:
        print("ORDER %s %s %s at next open, stop %s away, risk %.0f, %s score %.0f" % (o["id"], o["side"], o["symbol"], fmt_price(o["symbol"], o["stopDist"]), o["riskUSD"], o["setup"], o["score"]))
    for e in state["events"][-10:]:
        print("EVENT", e["date"], e["kind"], e["text"])

def cmd_portfolio_journal(a):
    pack = load_pack(a.states)
    for acct in pack_accounts(pack):
        state = pack["states"][acct["id"]]
        state["journal"].append({"at": iso(now_utc()), "date": state["account"]["lastDate"], "title": a.title[:80], "body": a.body[:1200], "phase": state["account"]["phase"], "attempt": state["account"]["attempt"]})
        if len(state["journal"]) > 400:
            state["journal"] = state["journal"][-400:]
    save_json(a.states, pack); print("journal entry added to %d accounts" % len(pack_accounts(pack)))

def cmd_portfolio_cancel(a):
    pack = load_pack(a.states); n = 0
    for acct in pack_accounts(pack):
        state = pack["states"][acct["id"]]
        for o in list(state["orders"]):
            if o["symbol"] == a.symbol and o["side"] == a.side:
                state["orders"].remove(o); n += 1
                record_event(state, state["account"]["lastDate"] or now_utc().date().isoformat(), "cancel", "%s %s %s cancelled by Claude: %s" % (o["id"], o["side"], o["symbol"], a.note))
    save_json(a.states, pack); print("cancelled %d order(s) for %s %s across the portfolio" % (n, a.side, a.symbol))

def cmd_portfolio_backtest(a):
    manifest = load_json(a.manifest)
    data = load_json(os.path.join(a.bars, "all.json")); allbars = data["bars"]
    hourly = load_hourly_bars(a.bars) if not a.no_hourly else {}
    print("%-16s %-30s %8s | %s" % ("account", "firm/program", "size", "result"))
    for acct in manifest["accounts"]:
        if a.only and acct["id"] not in a.only.split(","):
            continue
        cfg = account_config(acct); state = new_state(cfg)
        if hourly:
            alld = sorted(set(b["d"] for bars in allbars.values() for b in bars if b["d"] >= a.start and (a.end is None or b["d"] <= a.end)))
            for d in alld:
                process(state, allbars, mode="backtest", start=a.start, end=d, verbose=False, hourly=hourly)
        else:
            process(state, allbars, mode="backtest", start=a.start, end=a.end, verbose=False)
        acc = state["account"]; st = stats(state["trades"])
        passed = [h for h in acc["phaseHistory"] if h["result"] == "passed"]; failed = [h for h in acc["phaseHistory"] if h["result"] == "failed"]
        worst_day = min([p["dayPnl"] for p in state["equity"]] + [0.0]); low = min([p["worst"] for p in state["equity"]] + [cfg["initial"]])
        paid = sum(p["trader"] for p in acc["payouts"])
        fund = [h for h in passed if h["phase"] == phase_names(cfg)[-2]]
        print("%-16s %-30s %8.0f | now %-12s a%d bal %9.0f | trades %3d win %4.0f%% PF %4s net %+8.0f | passed %d (funded %s) failed %d | paid %7.0f | worst day %+6.0f low %8.0f" % (
            acct["id"], (acct.get("firm", "") + " " + acct.get("program", ""))[:30], cfg["initial"], acc["phase"], acc["attempt"], acc["balance"], st.get("n", 0), st.get("winRatePct", 0) or 0, st.get("profitFactor"), st.get("netUSD", 0),
            len(passed), fund[0]["end"] if fund else "-", len(failed), paid, worst_day, low))

def latest_closes(bars_dir):
    data = load_json(os.path.join(bars_dir, "all.json"))
    return {sym: bars[-1]["c"] for sym, bars in data["bars"].items() if bars}

def cmd_status(a):
    state = load_json(a.state)
    row = account_row(state)
    print("%s | attempt %d | balance %.2f | equity %.2f (%+.2f%%) | day %+.2f" % (row["phase"], row["attempt"], row["balance"], row["equity"], row["netPct"], row["dayPnl"]))
    if row["target"]:
        print("target %.0f: %.0f to go (%.1f%% done) | trading days %d/%d" % (row["target"], row["toTarget"], row["progressPct"], row["tradingDays"], row["minTradingDays"]))
    print("daily-loss room %s (floor %s) | max-loss room %.0f (floor %.0f) | open risk %.0f | risk/trade %.0f (x%.2f) | max positions %d" % (
        "n/a" if row["dailyRoom"] is None else "%.0f" % row["dailyRoom"], "n/a" if row["dailyFloor"] is None else "%.0f" % row["dailyFloor"], row["maxRoom"], row["maxFloor"], row["openRisk"], row["riskPerTrade"], row["riskMult"], row["maxPositionsNow"]))
    closes = latest_closes(a.bars) if a.bars else {}
    for p in positions_rows(state, closes) if closes else []:
        print("POS %s %s %s %s entry %s now %s stop %s pnl %+.2f (%+.2fR) %s %s" % (p["id"], p["side"], p["size"], p["symbol"], p["entry"], p["price"], p["stop"], p["pnlUSD"], p["r"], p["stage"], p["exitPlan"]))
    for o in state["orders"]:
        print("ORDER %s %s %s at next open, stop %s away, risk %.0f, %s score %.0f" % (o["id"], o["side"], o["symbol"], fmt_price(o["symbol"], o["stopDist"]), o["riskUSD"], o["setup"], o["score"]))
    if row["phaseTradesN"]:
        print("phase trades %d, win %.0f%%, avg %.2fR, PF %s, net %+.2f" % (row["phaseTradesN"], row["phaseWinRatePct"], row["phaseAvgR"], row["phaseProfitFactor"], row["phaseNetUSD"]))
    for e in state["events"][-12:]:
        print("EVENT", e["date"], e["kind"], e["text"])

def cmd_init(a):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if a.initial:
        cfg["initial"] = float(a.initial)
    state = new_state(cfg)
    if a.bars:
        data = load_json(os.path.join(a.bars, "all.json"))
        for sym, bars in data["bars"].items():
            if len(bars) >= 2:
                state["meta"]["lastProcessed"][sym] = bars[-2]["d"]     # so the first run processes the latest bar only
        for sym, hb in load_hourly_bars(a.bars).items():
            if len(hb) >= 2:
                state["meta"]["lastProcessedHourly"][sym] = hb[-2]["t"]
    save_json(a.state, state)
    print("initialised %s: %s %.0f" % (a.state, cfg["currency"], cfg["initial"]))

def load_hourly_bars(bars_dir):
    p = os.path.join(bars_dir, "hourly.json")
    return load_json(p)["bars"] if os.path.exists(p) else {}

def upgrade_config(state):
    """Bring a running state's config up to this engine's DEFAULT_CONFIG (keeps the account, positions, orders and history)."""
    old = state["config"]
    if old.get("configVersion") == CONFIG_VERSION:
        return False
    new = copy.deepcopy(DEFAULT_CONFIG)
    for k in ("initial", "currency", "feeEUR", "fee", "feeCurrency", "accountId", "firm", "program", "product"):
        if k in old:
            new[k] = old[k]
    state["config"] = new
    record_event(state, state["account"].get("lastDate") or now_utc().date().isoformat(), "config",
                 "Engine upgraded to v%d: firm-specific rulebooks (phase targets, daily/max loss basis, profitable days, instrument exclusions); metal entries skipped during US hours" % CONFIG_VERSION)
    return True

def cmd_run(a):
    state = load_json(a.state)
    data = load_json(os.path.join(a.bars, "all.json"))
    if data.get("failures"):
        print("WARNING bars missing for:", ", ".join(data["failures"]))
    allbars = data["bars"]
    hourly = load_hourly_bars(a.bars)
    backup = a.state + ".bak"
    save_json(backup, state)
    if upgrade_config(state):
        print("Config upgraded to v%d" % CONFIG_VERSION)
    before_h = dict(state["meta"].get("lastProcessedHourly", {}))
    dates, log = process(state, allbars, mode=a.mode, no_orders=a.no_orders, hourly=hourly)
    save_json(a.state, state)
    new_h = {s: t for s, t in state["meta"].get("lastProcessedHourly", {}).items() if before_h.get(s) != t}
    if not dates and not new_h:
        print("No new completed bars since the last run. Nothing changed.")
        return
    if dates:
        print("Processed %d session date(s): %s .. %s" % (len(dates), dates[0], dates[-1]))
    if new_h:
        print("Processed hourly bars up to: " + ", ".join("%s %s" % (s, dt.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M UTC")) for s, t in new_h.items()))
    for line in log:
        print(line)
    cmd_status(argparse.Namespace(state=a.state, bars=a.bars))

def cmd_cancel(a):
    state = load_json(a.state)
    for o in list(state["orders"]):
        if o["id"] == a.id:
            state["orders"].remove(o)
            record_event(state, state["account"]["lastDate"] or now_utc().date().isoformat(), "cancel", "%s %s %s cancelled by Claude: %s" % (o["id"], o["side"], o["symbol"], a.note))
            save_json(a.state, state); print("cancelled", a.id); return
    print("no pending order", a.id); sys.exit(1)

def cmd_journal(a):
    state = load_json(a.state)
    state["journal"].append({"at": iso(now_utc()), "date": state["account"]["lastDate"], "title": a.title[:80], "body": a.body[:1200], "phase": state["account"]["phase"], "attempt": state["account"]["attempt"]})
    if len(state["journal"]) > 400:
        state["journal"] = state["journal"][-400:]
    save_json(a.state, state); print("journal entry added")

def cmd_backtest(a):
    data = load_json(os.path.join(a.bars, "all.json"))
    allbars = {s: b for s, b in data["bars"].items()}
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    state = new_state(cfg)
    hourly = load_hourly_bars(a.bars) if not a.no_hourly else {}
    if hourly:
        # interleave: one date at a time so the hourly component sees the account as it was on that day
        alld = sorted(set(b["d"] for bars in allbars.values() for b in bars if b["d"] >= a.start and (a.end is None or b["d"] <= a.end)))
        dates, log = [], []
        for d in alld:
            dd, ll = process(state, allbars, mode="backtest", start=a.start, end=d, verbose=a.verbose, hourly=hourly)
            dates += dd; log += ll
    else:
        dates, log = process(state, allbars, mode="backtest", start=a.start, end=a.end, verbose=a.verbose, hourly=hourly)
    acc = state["account"]
    print("Backtest %s..%s: %d sessions, %d trades, attempts %d, phases passed %d, failed %d" % (dates[0], dates[-1], len(dates), len(state["trades"]), acc["attempt"], len([h for h in acc["phaseHistory"] if h["result"] == "passed"]), len([h for h in acc["phaseHistory"] if h["result"] == "failed"])))
    for h in acc["phaseHistory"]:
        print("  attempt %d %-12s %s -> %s %s %s bal %.0f days %d" % (h["attempt"], h["phase"], h["start"], h["end"], h["result"], h.get("reason", "")[:70], h["balance"], h["tradingDays"]))
    print("  now: attempt %d %s balance %.0f equity %.0f, %d open" % (acc["attempt"], acc["phase"], acc["balance"], acc["lastEquity"], len(state["positions"])))
    st = stats(state["trades"])
    print("  trades:", json.dumps(st))
    by = {}
    for t in state["trades"]:
        k = t["symbol"]; by.setdefault(k, []).append(t)
    for k, ts in sorted(by.items()):
        s = stats(ts); print("  %-7s n %3d win %5.1f%% avgR %5.2f net %9.0f" % (k, s["n"], s["winRatePct"], s["avgR"], s["netUSD"]))
    by = {}
    for t in state["trades"]:
        k = t.get("strat", "trend") + "/" + t["setup"] + "/" + t["side"]; by.setdefault(k, []).append(t)
    for k, ts in sorted(by.items()):
        s = stats(ts); print("  %-15s n %3d win %5.1f%% avgR %5.2f net %9.0f" % (k, s["n"], s["winRatePct"], s["avgR"], s["netUSD"]))
    if a.verbose:
        for line in log:
            print(line)
    if a.save:
        save_json(a.save, state)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch"); p.add_argument("--out", default="bars"); p.add_argument("--range", default="2y"); p.add_argument("--hourly-range", default=DEFAULT_CONFIG["strategies"]["intraday"]["yahooRange"]); p.add_argument("--no-hourly", action="store_true"); p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("import-av"); p.add_argument("--bars", default="bars"); p.add_argument("--symbol", required=True); p.add_argument("--csv", required=True); p.set_defaults(fn=cmd_import_av)
    p = sub.add_parser("init"); p.add_argument("--state", default="state.json"); p.add_argument("--bars"); p.add_argument("--initial"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("run"); p.add_argument("--state", default="state.json"); p.add_argument("--bars", default="bars"); p.add_argument("--mode", default="live"); p.add_argument("--no-orders", action="store_true"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("cancel"); p.add_argument("--state", default="state.json"); p.add_argument("--id", required=True); p.add_argument("--note", required=True); p.set_defaults(fn=cmd_cancel)
    p = sub.add_parser("journal"); p.add_argument("--state", default="state.json"); p.add_argument("--title", required=True); p.add_argument("--body", required=True); p.set_defaults(fn=cmd_journal)
    p = sub.add_parser("export"); p.add_argument("--state", default="state.json"); p.add_argument("--bars", default="bars"); p.add_argument("--out", default="out"); p.set_defaults(fn=cmd_export)
    p = sub.add_parser("status"); p.add_argument("--state", default="state.json"); p.add_argument("--bars", default="bars"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("backtest"); p.add_argument("--bars", default="bars"); p.add_argument("--start", required=True); p.add_argument("--end"); p.add_argument("--verbose", action="store_true"); p.add_argument("--save"); p.add_argument("--no-hourly", action="store_true"); p.set_defaults(fn=cmd_backtest)
    p = sub.add_parser("portfolio-init"); p.add_argument("--manifest", required=True); p.add_argument("--bars", default="bars"); p.add_argument("--out", default="portfolio_state.json"); p.set_defaults(fn=cmd_portfolio_init)
    p = sub.add_parser("portfolio-run"); p.add_argument("--states", default="portfolio_state.json"); p.add_argument("--bars", default="bars"); p.add_argument("--mode", default="live"); p.add_argument("--no-orders", action="store_true"); p.set_defaults(fn=cmd_portfolio_run)
    p = sub.add_parser("portfolio-export"); p.add_argument("--states", default="portfolio_state.json"); p.add_argument("--bars", default="bars"); p.add_argument("--out", default="out"); p.add_argument("--focus"); p.set_defaults(fn=cmd_portfolio_export)
    p = sub.add_parser("portfolio-status"); p.add_argument("--states", default="portfolio_state.json"); p.add_argument("--bars", default="bars"); p.add_argument("--account"); p.set_defaults(fn=cmd_portfolio_status)
    p = sub.add_parser("portfolio-journal"); p.add_argument("--states", default="portfolio_state.json"); p.add_argument("--title", required=True); p.add_argument("--body", required=True); p.set_defaults(fn=cmd_portfolio_journal)
    p = sub.add_parser("portfolio-cancel"); p.add_argument("--states", default="portfolio_state.json"); p.add_argument("--symbol", required=True); p.add_argument("--side", default="long"); p.add_argument("--note", required=True); p.set_defaults(fn=cmd_portfolio_cancel)
    p = sub.add_parser("portfolio-backtest"); p.add_argument("--manifest", required=True); p.add_argument("--bars", default="bars"); p.add_argument("--start", required=True); p.add_argument("--end"); p.add_argument("--no-hourly", action="store_true"); p.add_argument("--only"); p.set_defaults(fn=cmd_portfolio_backtest)
    a = ap.parse_args()
    rc = a.fn(a)
    sys.exit(rc or 0)

if __name__ == "__main__":
    main()
