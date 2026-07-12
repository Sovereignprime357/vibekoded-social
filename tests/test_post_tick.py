"""
tests/test_post_tick.py — post_tick's I-PACE window gate (SPEC-v8).

Frozen clock via run_tick(now_utc=...). DRY_RUN=1 so no Bluesky network. QUEUE_PATH and
the posted/skipped/schedule paths are all redirected to tmp so no real state is touched.
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import content_queue  # noqa: E402
import post_schedule  # noqa: E402
import post_tick  # noqa: E402

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def _setup(tmp_path, monkeypatch, entries):
    """DRY_RUN tick against a tmp queue + tmp logs + tmp schedule. `entries` = list of
    (pillar, final_text)."""
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("POST_FORCE", raising=False)
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    sched = str(tmp_path / "sched.json")
    monkeypatch.setenv("SCHEDULE_PATH", sched)
    monkeypatch.setattr(post_tick, "POSTED_LOG", str(tmp_path / "posted.jsonl"))
    monkeypatch.setattr(post_tick, "SKIPPED_LOG", str(tmp_path / "skipped.jsonl"))
    for pillar, text in entries:
        content_queue.append_entry(raw=text, type="moment", pillar=pillar,
                                   final_text=text, path=queue)
    return queue, sched


def _posted_count(tmp_path):
    p = str(tmp_path / "posted.jsonl")
    if not os.path.exists(p):
        return 0
    return sum(1 for line in open(p, encoding="utf-8") if line.strip())


def test_no_slot_due_is_noop(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [("showcase", "we shipped the window scheduler today. how do you pace posts?")])
    # 06:00 ET is before the earliest window -> nothing due -> no post, no crash.
    rc = post_tick.run_tick(now_utc=_et(2026, 7, 12, 6, 0))
    assert rc == 0
    assert _posted_count(tmp_path) == 0
    assert content_queue.get_next_unused(str(tmp_path / "queue.jsonl")) is not None  # entry still unused


def test_due_slot_posts_one_and_marks_slot_fired(tmp_path, monkeypatch):
    _, sched = _setup(tmp_path, monkeypatch, [("showcase", "we shipped the window scheduler today. how do you pace posts?")])
    # 23:59 ET: every non-post-midnight slot is due -> earliest fires; exactly ONE post.
    rc = post_tick.run_tick(now_utc=_et(2026, 7, 12, 23, 59))
    assert rc == 0
    assert _posted_count(tmp_path) == 1
    state = json.load(open(sched, encoding="utf-8"))
    assert sum(1 for s in state["slots"] if s["fired"]) == 1   # exactly one slot marked


def test_second_tick_same_instant_does_not_double_post(tmp_path, monkeypatch):
    # Two eligible entries (distinct pillars so rotation allows the 2nd).
    _, sched = _setup(tmp_path, monkeypatch, [
        ("showcase", "we shipped the window scheduler today. how do you pace posts?"),
        ("operator", "spec the invariants first, then let the model fill them in. what is your rule?"),
    ])
    now = _et(2026, 7, 12, 23, 59)
    assert post_tick.run_tick(now_utc=now) == 0
    assert post_tick.run_tick(now_utc=now) == 0
    # At a single instant several slots are due, so two ticks fire two DIFFERENT slots,
    # but never more than one per tick.
    assert _posted_count(tmp_path) == 2
    state = json.load(open(sched, encoding="utf-8"))
    assert sum(1 for s in state["slots"] if s["fired"]) == 2


def test_empty_queue_due_slot_is_noop_slot_stays_due(tmp_path, monkeypatch):
    _, sched = _setup(tmp_path, monkeypatch, [])   # empty queue
    rc = post_tick.run_tick(now_utc=_et(2026, 7, 12, 23, 59))
    assert rc == 0
    assert _posted_count(tmp_path) == 0
    # No schedule persisted / no slot burned: a due slot with nothing to post stays due,
    # so the post lands late once the queue has material (I-PACE: missed != lost).
    if os.path.exists(sched):
        state = json.load(open(sched, encoding="utf-8"))
        assert all(not s["fired"] for s in state["slots"])


def test_force_bypasses_the_window_gate(tmp_path, monkeypatch):
    _, sched = _setup(tmp_path, monkeypatch, [("showcase", "we shipped the window scheduler today. how do you pace posts?")])
    monkeypatch.setenv("POST_FORCE", "1")
    # 06:00 ET: no slot due, but a manual/forced run posts anyway and touches NO schedule.
    rc = post_tick.run_tick(now_utc=_et(2026, 7, 12, 6, 0))
    assert rc == 0
    assert _posted_count(tmp_path) == 1
    assert not os.path.exists(sched)   # forced path never reads/writes the schedule
