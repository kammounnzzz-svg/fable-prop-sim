#!/usr/bin/env python3
"""Watchdog for the simulation's schedule. Paper trading only; this script runs no simulation code and changes nothing.

GitHub starts scheduled workflows late and sometimes not at all (18 Nov 2026: the 09:23 and 12:23 UTC runs never
started). The simulation is scheduled Monday to Saturday at 00:23, 03:23 ... 21:23 UTC (.github/workflows/sim.yml, with a
second chance at :41). Given when the simulation last started, this script works out the first scheduled slot after that
run and how long that slot has been waiting. The watchdog workflow (.github/workflows/watchdog.yml) starts the
simulation itself when the wait reaches the threshold.

    python3 scripts/sim_watchdog.py --last 2026-11-18T07:53:41Z [--now 2026-11-18T13:40:00Z] [--threshold-min 70]
    python3 scripts/sim_watchdog.py --selftest

Prints one line, "dueSlot=<ISO time> overdueMinutes=<whole minutes> start=<yes|no>", and exits 0. Anything it cannot
understand makes it print "start=no" with the reason: when in doubt the watchdog does nothing."""
import datetime as dt
import sys

UTC = dt.timezone.utc
SLOT_MINUTE = 23          # the first cron line of sim.yml: "23 */3 * * 1-6"
SLOT_EVERY_HOURS = 3
NO_RUN_WEEKDAY = 6        # Sunday (datetime.weekday(): Monday is 0)
DEFAULT_THRESHOLD_MIN = 70   # GitHub's own late starts have mostly been 25 to 80 minutes behind the slot


def parse(text):
    """An ISO time in UTC as GitHub prints it, e.g. 2026-11-18T07:53:41Z."""
    return dt.datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def first_due_slot(last):
    """The first scheduled slot strictly after the run that started at `last` (Sundays have no slots)."""
    t = last.replace(minute=SLOT_MINUTE, second=0, microsecond=0)
    t -= dt.timedelta(hours=t.hour % SLOT_EVERY_HOURS)
    while t <= last or t.weekday() == NO_RUN_WEEKDAY:
        t += dt.timedelta(hours=SLOT_EVERY_HOURS)
    return t


def overdue_minutes(last, now):
    """Whole minutes since the first slot after `last`; 0 while that slot is still in the future."""
    slot = first_due_slot(last)
    return slot, (int((now - slot).total_seconds() // 60) if slot <= now else 0)


def decide(last, now, threshold_min=DEFAULT_THRESHOLD_MIN):
    slot, minutes = overdue_minutes(last, now)
    return slot, minutes, minutes >= threshold_min


def selftest():
    cases = [
        # (newest run started, now, expected due slot, expected overdue minutes, expected start at 70 minutes)
        ("2026-11-18T07:53:41Z", "2026-11-18T08:07:00Z", "2026-11-18T09:23:00Z", 0, False),     # next slot not due yet
        ("2026-11-18T07:53:41Z", "2026-11-18T09:37:00Z", "2026-11-18T09:23:00Z", 14, False),    # 14 minutes late: GitHub's usual delay
        ("2026-11-18T07:53:41Z", "2026-11-18T10:32:59Z", "2026-11-18T09:23:00Z", 69, False),    # one second short of 70 minutes
        ("2026-11-18T07:53:41Z", "2026-11-18T10:33:00Z", "2026-11-18T09:23:00Z", 70, True),     # exactly 70 minutes
        ("2026-11-18T07:53:41Z", "2026-11-18T13:40:00Z", "2026-11-18T09:23:00Z", 257, True),    # the morning of 18 Nov 2026
        ("2026-11-18T09:23:00Z", "2026-11-18T10:40:00Z", "2026-11-18T12:23:00Z", 0, False),     # a run exactly on its slot covers that slot
        ("2026-11-18T09:22:59Z", "2026-11-18T10:40:00Z", "2026-11-18T09:23:00Z", 77, True),     # one second before the slot does not
        ("2026-11-18T12:31:10Z", "2026-11-18T13:37:00Z", "2026-11-18T15:23:00Z", 0, False),     # a very late 09:23 run counts for 12:23 as well
        ("2026-11-17T22:30:07Z", "2026-11-18T01:37:00Z", "2026-11-18T00:23:00Z", 74, True),     # across midnight
        ("2026-11-21T22:10:39Z", "2026-11-23T00:07:00Z", "2026-11-23T00:23:00Z", 0, False),     # Saturday night to Monday: Sunday has no slots
        ("2026-11-21T22:10:39Z", "2026-11-23T01:37:00Z", "2026-11-23T00:23:00Z", 74, True),     # Monday's first run missing
        ("2026-11-21T20:00:00Z", "2026-11-21T22:37:00Z", "2026-11-21T21:23:00Z", 74, True),     # Saturday's last slot missing
        ("2026-11-22T10:00:00Z", "2026-11-23T00:37:00Z", "2026-11-23T00:23:00Z", 14, False),    # a run started by hand on a Sunday
        ("2026-12-31T22:15:00Z", "2027-01-01T01:40:00Z", "2027-01-01T00:23:00Z", 77, True),     # across a year end (a Friday)
    ]
    bad = 0
    for last, now, want_slot, want_min, want_start in cases:
        slot, minutes, start = decide(parse(last), parse(now))
        ok = (slot == parse(want_slot)) and minutes == want_min and start == want_start
        bad += not ok
        print("%-4s last %s now %s -> slot %s overdue %4d min start %-5s (want %s, %d, %s)" % (
            "ok" if ok else "FAIL", last, now, slot.strftime("%Y-%m-%dT%H:%M:%SZ"), minutes, start, want_slot, want_min, want_start))
    # every slot the cron line "23 */3 * * 1-6" produces in three weeks is found, in order, and nothing else
    t = parse("2026-11-16T00:00:00Z"); end = parse("2026-12-07T00:00:00Z"); cron = []
    while t < end:
        if t.minute == 23 and t.hour % 3 == 0 and t.weekday() != 6:
            cron.append(t)
        t += dt.timedelta(minutes=1)
    walked = []; cur = parse("2026-11-15T23:59:59Z")
    while True:
        nxt = first_due_slot(cur)
        if nxt >= end:
            break
        walked.append(nxt); cur = nxt
    same = walked == cron
    bad += not same
    print("%-4s slots found by walking forward: %d, slots of the cron line over the same three weeks: %d" % ("ok" if same else "FAIL", len(walked), len(cron)))
    print("selftest: %d cases, %d not as expected" % (len(cases) + 1, bad))
    return 1 if bad else 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    try:
        args = dict(zip(argv[::2], argv[1::2]))
        last = parse(args["--last"])
        now = parse(args["--now"]) if "--now" in args else dt.datetime.now(UTC).replace(microsecond=0)
        threshold = int(args.get("--threshold-min", DEFAULT_THRESHOLD_MIN))
        slot, minutes, start = decide(last, now, threshold)
        print("dueSlot=%s overdueMinutes=%d start=%s" % (slot.strftime("%Y-%m-%dT%H:%M:%SZ"), minutes, "yes" if start else "no"))
    except Exception as e:                      # when in doubt, do nothing
        print("dueSlot=unknown overdueMinutes=0 start=no (could not work it out: %s)" % (str(e)[:120]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
