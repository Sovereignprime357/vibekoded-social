"""
post_schedule.py — the daily posting WINDOW scheduler (SPEC-v8 §I-PACE).

Ported from the operator's prior NullChadAI `WindowScheduler` (design reused, not
copied): define N posting WINDOWS, pick a RANDOM time inside each window, sort them,
and REGENERATE the whole schedule at daily reset. The behavior we want, adapted to a
STATELESS, short-lived tick model (no daemon, no node-cron):

  - The bot has no long-running process — every post is a ~15-min heartbeat tick that
    wakes, does one thing, and exits. So instead of scheduling cron jobs in memory, we
    generate the day's post times DETERMINISTICALLY (RNG seeded from the local date, so
    every tick within a day agrees on the same schedule) and persist which slots have
    fired to a small state file. Each tick asks: "is a slot due and not yet fired?"

  - I-PACE: posts land inside defined windows at a RANDOMIZED time within each window,
    and the schedule regenerates daily. The bot must NEVER post at the same clock time
    every day (a machine signature) — two consecutive days yield different schedules
    because the RNG seed is the date. Max one post per tick; the day's cadence is bounded
    by the window COUNT, not the heartbeat frequency.

  - Missed slots fire LATE, not skipped: a slot whose time has passed but hasn't fired
    (heartbeat gap) is still "due" on the next tick. A lagging tick is not a lost post.

  - A window that spans midnight belongs to the day it STARTED (SPEC edge case): its
    post time can be after 24:00 local, and the day's schedule stays authoritative until
    that post-midnight tail has elapsed, even though the calendar date has already rolled.

This module owns ONLY the schedule (pure, deterministic, network-free). post_tick.py
consumes it to gate posting. Marking a slot fired is the caller's job, AFTER a real post.
"""

from __future__ import annotations

import json
import os
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCHEDULE_PATH = os.path.join(HERE, "post-schedule.json")

# Local timezone the windows are expressed in. Override with POST_TZ (a repo var).
# ET matches where the operator's audience is (the old fixed crons were ET-shaped).
DEFAULT_TZ = "America/New_York"

# The posting WINDOWS (config constant): one randomized post time PER window, so the
# day's cadence == len(WINDOWS). Times are LOCAL "HH:MM"; an end past "24:00" means the
# window crosses midnight and its post is owned by the day it started (see module doc).
# 5 windows ≈ the prior "~3-5 posts/day", spread morning → late-night US.
WINDOWS: List[Dict[str, str]] = [
    {"name": "morning",   "start": "08:00", "end": "11:00"},
    {"name": "midday",    "start": "11:30", "end": "14:30"},
    {"name": "afternoon", "start": "15:00", "end": "18:00"},
    {"name": "evening",   "start": "18:30", "end": "21:30"},
    {"name": "late",      "start": "22:00", "end": "24:30"},  # crosses midnight
]


def _tz(tz_name: Optional[str] = None) -> ZoneInfo:
    name = tz_name or os.environ.get("POST_TZ", "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — bad/absent tz must never crash the tick
        return ZoneInfo("UTC")


def _parse_hhmm(s: str) -> int:
    """'HH:MM' -> minutes from local midnight. Hours >= 24 (midnight-crossing) allowed."""
    h, m = (int(x) for x in str(s).split(":"))
    return h * 60 + m


def _clock_str(minutes: int) -> str:
    """Human clock label for a slot; a post-midnight slot shows its next-day clock + (+1d)."""
    day_over = minutes >= 24 * 60
    m = minutes % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}" + (" (+1d)" if day_over else "")


def _seed_for_date(d: date) -> int:
    # Seed from the LOCAL date only -> same day is idempotent across ticks, and two
    # different days produce different schedules (I-PACE: no fixed clock signature).
    return int(d.strftime("%Y%m%d"))


def _random_minute(rng: random.Random, start_min: int, end_min: int) -> int:
    if end_min <= start_min:
        return start_min  # degenerate/zero-width window -> the start instant
    return rng.randrange(start_min, end_min)  # [start, end), mirrors the JS reference


def generate_schedule(
    local_date: date,
    windows: Optional[List[Dict[str, str]]] = None,
    tz_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the schedule for `local_date`: one randomized post time per window, sorted by
    time, seeded from the date (deterministic + idempotent within a day). fired=False.
    """
    windows = windows or WINDOWS
    rng = random.Random(_seed_for_date(local_date))
    slots: List[Dict[str, Any]] = []
    for w in windows:
        minutes = _random_minute(rng, _parse_hhmm(w["start"]), _parse_hhmm(w["end"]))
        slots.append({
            "window": w["name"],
            "minutes": minutes,
            "time": _clock_str(minutes),
            "fired": False,
            "fired_at": None,
        })
    slots.sort(key=lambda s: s["minutes"])
    return {
        "date": local_date.isoformat(),
        "tz": tz_name or os.environ.get("POST_TZ", "").strip() or DEFAULT_TZ,
        "slots": slots,
    }


def _slot_datetime(date_str: str, minutes: int, tz: ZoneInfo) -> datetime:
    """Absolute local datetime of a slot. minutes >= 1440 rolls to the next clock day,
    but the slot stays OWNED by date_str (the day the window started)."""
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime(y, m, d, tzinfo=tz) + timedelta(minutes=minutes)


def _latest_slot_minutes(state: Dict[str, Any]) -> int:
    return max((s["minutes"] for s in state.get("slots", [])), default=0)


def load_state(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    target = path or DEFAULT_SCHEDULE_PATH
    if not os.path.exists(target):
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("date") and isinstance(data.get("slots"), list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def save_state(state: Dict[str, Any], path: Optional[str] = None) -> None:
    with open(path or DEFAULT_SCHEDULE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def current_state(
    now_utc: Optional[datetime] = None,
    path: Optional[str] = None,
    windows: Optional[List[Dict[str, str]]] = None,
    tz_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    The schedule that governs RIGHT NOW. Reuse the saved schedule while its owned span
    (including any post-midnight tail) is still current; otherwise regenerate for the
    current local day (the daily reset). Does NOT persist a regeneration — it's
    deterministic, so an unsaved regen costs nothing and avoids state churn until a slot
    actually fires (the caller saves after marking one fired).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)

    saved = load_state(path)
    if saved:
        sched_date = date.fromisoformat(saved["date"])
        latest = _slot_datetime(saved["date"], _latest_slot_minutes(saved), tz)
        # Stay on the saved schedule if it's the same calendar day OR we're still inside
        # its owned span (the midnight-crossing tail). Only roll over once it's spent.
        if now_local.date() == sched_date or now_local <= latest:
            return saved
    return generate_schedule(now_local.date(), windows=windows, tz_name=tz_name)


def due_unfired_slot(
    state: Dict[str, Any],
    now_utc: Optional[datetime] = None,
    tz_name: Optional[str] = None,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """
    The EARLIEST slot that has come due (its local time <= now) and hasn't fired, as
    (index, slot). None if nothing is due. Earliest-first so a heartbeat gap that left
    several slots due drains them one-per-tick in order (missed slots fire late).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = _tz(tz_name)
    best: Optional[Tuple[int, Dict[str, Any]]] = None
    for i, slot in enumerate(state.get("slots", [])):
        if slot.get("fired"):
            continue
        if _slot_datetime(state["date"], slot["minutes"], tz) <= now_utc.astimezone(tz):
            if best is None or slot["minutes"] < best[1]["minutes"]:
                best = (i, slot)
    return best


def mark_fired(
    state: Dict[str, Any],
    index: int,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Mark slot `index` fired (in place) and return the state. Caller persists it."""
    now_utc = now_utc or datetime.now(timezone.utc)
    state["slots"][index]["fired"] = True
    state["slots"][index]["fired_at"] = now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return state
