"""
tests/test_post_schedule.py — the daily posting WINDOW scheduler (SPEC-v8 §I-PACE).

Pure + deterministic: everything is driven by an injected frozen clock (`now_utc`) and
a tmp state file, no network, no wall-clock. Times are reasoned about in ET (the default
POST_TZ); July is EDT (UTC-4).
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import post_schedule as ps  # noqa: E402

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def _times(state):
    return [s["minutes"] for s in state["slots"]]


# --- determinism + I-PACE ----------------------------------------------------

def test_same_day_is_deterministic():
    from datetime import date
    a = ps.generate_schedule(date(2026, 7, 12))
    b = ps.generate_schedule(date(2026, 7, 12))
    assert _times(a) == _times(b)   # idempotent across ticks within a day


def test_two_consecutive_days_differ():
    # I-PACE: the bot must NEVER post at the same clock time every day.
    from datetime import date
    d1 = ps.generate_schedule(date(2026, 7, 12))
    d2 = ps.generate_schedule(date(2026, 7, 13))
    assert _times(d1) != _times(d2)


def test_posts_land_inside_their_windows():
    from datetime import date
    sched = ps.generate_schedule(date(2026, 7, 12))
    by_name = {w["name"]: w for w in ps.WINDOWS}
    assert len(sched["slots"]) == len(ps.WINDOWS)
    for slot in sched["slots"]:
        w = by_name[slot["window"]]
        start, end = ps._parse_hhmm(w["start"]), ps._parse_hhmm(w["end"])
        assert start <= slot["minutes"] < end, slot


def test_slots_sorted_by_time():
    from datetime import date
    sched = ps.generate_schedule(date(2026, 7, 12))
    mins = _times(sched)
    assert mins == sorted(mins)


# --- due / fire semantics ----------------------------------------------------

def test_nothing_due_before_first_slot():
    from datetime import date
    sched = ps.generate_schedule(date(2026, 7, 12))
    # 06:00 ET is before the earliest window (morning starts 08:00).
    assert ps.due_unfired_slot(sched, now_utc=_et(2026, 7, 12, 6, 0)) is None


def test_one_per_tick_each_slot_fires_once_across_a_day(tmp_path):
    path = str(tmp_path / "sched.json")
    fires = 0
    fired_windows = []
    per_tick_counts = []
    # Tick every 15 min from 07:00 ET through 01:00 ET the NEXT day (covers a
    # possibly-post-midnight 'late' slot before the day rolls over).
    t = _et(2026, 7, 12, 7, 0)
    end = _et(2026, 7, 13, 1, 0)
    while t <= end:
        state = ps.current_state(now_utc=t, path=path)
        # only stay on day-12's schedule (don't count day-13 regen)
        if state["date"] != "2026-07-12":
            break
        due = ps.due_unfired_slot(state, now_utc=t)
        n = 0
        if due is not None:
            idx, slot = due
            ps.mark_fired(state, idx, now_utc=t)
            ps.save_state(state, path)
            fires += 1
            fired_windows.append(slot["window"])
            n = 1
        per_tick_counts.append(n)
        t += timedelta(minutes=15)
    assert fires == len(ps.WINDOWS)                 # every slot fired
    assert max(per_tick_counts) == 1                # never more than one per tick
    assert sorted(fired_windows) == sorted(w["name"] for w in ps.WINDOWS)  # each once


def test_heartbeat_gap_missed_slot_fires_late(tmp_path):
    from datetime import date
    sched = ps.generate_schedule(date(2026, 7, 12))
    first, second = sched["slots"][0], sched["slots"][1]
    # A tick that arrives AFTER the 2nd slot's time, having missed the 1st entirely
    # (heartbeat gap). due_unfired_slot must return the EARLIEST unfired (the 1st),
    # so a lagging tick fires the missed post late rather than skipping it.
    late_min = second["minutes"] + 1
    now = _et(2026, 7, 12, 0, 0) + timedelta(minutes=late_min)
    due = ps.due_unfired_slot(sched, now_utc=now)
    assert due is not None and due[1]["window"] == first["window"]
    ps.mark_fired(sched, due[0], now_utc=now)
    # Next tick (same instant) now yields the SECOND slot — drained in order, one/tick.
    due2 = ps.due_unfired_slot(sched, now_utc=now)
    assert due2 is not None and due2[1]["window"] == second["window"]


# --- rollover ----------------------------------------------------------------

def test_current_state_regenerates_on_day_rollover(tmp_path):
    path = str(tmp_path / "sched.json")
    day1 = ps.current_state(now_utc=_et(2026, 7, 12, 9, 0), path=path)
    ps.save_state(day1, path)
    assert day1["date"] == "2026-07-12"
    # Well into the next day (past any midnight tail) -> regenerate for the new day.
    day2 = ps.current_state(now_utc=_et(2026, 7, 13, 9, 0), path=path)
    assert day2["date"] == "2026-07-13"
    assert _times(day1) != _times(day2)


def test_midnight_crossing_slot_owned_by_start_day(tmp_path):
    # A window that spans midnight belongs to the day it STARTED: while now is inside
    # that post-midnight tail, current_state must NOT roll over to the new calendar day.
    from datetime import date
    path = str(tmp_path / "sched.json")
    # Force a schedule whose latest slot is after midnight (25 min past), owned by 7/12.
    state = ps.generate_schedule(date(2026, 7, 12))
    state["slots"][-1]["minutes"] = 24 * 60 + 25   # 00:25 next day, owned by 7/12
    state["date"] = "2026-07-12"
    ps.save_state(state, path)
    # 00:10 ET on 7/13 is before the tail (00:25) -> still on 7/12's schedule.
    kept = ps.current_state(now_utc=_et(2026, 7, 13, 0, 10), path=path)
    assert kept["date"] == "2026-07-12"
    # With the earlier slots already fired, the post-midnight tail is the one still
    # due at 00:26 next day — it fires late, owned by 7/12, before the day is abandoned.
    for i in range(len(kept["slots"]) - 1):
        ps.mark_fired(kept, i, now_utc=_et(2026, 7, 12, 23, 0))
    due = ps.due_unfired_slot(kept, now_utc=_et(2026, 7, 13, 0, 26))
    assert due is not None and due[1]["window"] == ps.WINDOWS[-1]["name"]
    # ...and it is NOT yet due at 00:10 (before the tail time).
    assert ps.due_unfired_slot(kept, now_utc=_et(2026, 7, 13, 0, 10)) is None
