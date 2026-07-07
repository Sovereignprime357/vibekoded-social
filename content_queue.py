"""
content_queue.py — read/write the handoff file (content-queue.jsonl).

NOTE ON THE MODULE NAME: this file is deliberately named `content_queue.py`,
NOT `queue.py`. Python's stdlib has its own `queue` module (used internally
by `urllib3`/`requests` for connection pooling), and because the current
working directory / script directory is on `sys.path`, a local `queue.py`
sitting next to it would shadow the stdlib module for every import in the
whole process — including inside `requests`, which breaks in ways that have
nothing to do with this file's logic. Renaming avoids the collision.

Per SPEC.md INTERFACE "the handoff file (the seam)":
  Each entry: { ts, type: ship|fix|decision|moment|receipt, raw: "<what
  happened, plainly>", angle?: "<optional suggested hook>",
  shot?: "<optional screenshot path>", used: false }

This module owns reading/writing that file. It does NOT generate content
and does NOT know about Bluesky — it is pure file I/O plus the small amount
of logic needed to pick "the next unused entry" and to mark one used.

File format: JSON Lines (one JSON object per line). This is deliberate —
appends are O(1) and don't require rewriting the whole file, and partial
corruption of one line doesn't take down the others (a malformed line is
skipped with a warning, not a crash).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

VALID_TYPES = {"ship", "fix", "decision", "moment", "receipt"}

DEFAULT_QUEUE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "content-queue.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queue_path(path: Optional[str] = None) -> str:
    return path or os.environ.get("QUEUE_PATH") or DEFAULT_QUEUE_PATH


def append_entry(
    raw: str,
    type: str = "moment",  # noqa: A002 — mirrors the SPEC's field name deliberately
    angle: Optional[str] = None,
    shot: Optional[str] = None,
    ts: Optional[str] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Append one entry to the handoff file. Returns the entry as written.

    Validates `type` against VALID_TYPES from the SPEC; raises ValueError on
    an unrecognized type rather than silently writing bad data (this file
    is read by a generation pipeline downstream — garbage in is a real cost
    there, not just here).
    """
    if not raw or not str(raw).strip():
        raise ValueError("append_entry requires non-empty `raw` content")

    if type not in VALID_TYPES:
        raise ValueError(f"type must be one of {sorted(VALID_TYPES)}, got {type!r}")

    entry: Dict[str, Any] = {
        "ts": ts or _now_iso(),
        "type": type,
        "raw": str(raw).strip(),
        "used": False,
    }
    if angle:
        entry["angle"] = str(angle).strip()
    if shot:
        entry["shot"] = str(shot).strip()

    target = _queue_path(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


def _read_all_lines(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Read every valid line from the queue file. Malformed lines are skipped
    with a stderr warning (index preserved via enumerate so a fix is easy),
    never raised — one bad line must not block the whole pipeline.
    """
    target = _queue_path(path)
    if not os.path.exists(target):
        return []

    entries: List[Dict[str, Any]] = []
    with open(target, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("line is not a JSON object")
                entries.append(obj)
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"[content_queue] WARNING: skipping malformed line {i} in {target}: {exc}")
                continue
    return entries


def get_next_unused(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Return the oldest unused entry (first `used: false` in file order), or
    None if there is nothing postable. This is a deliberate FIFO, not a
    priority queue — the SPEC's "pull unused entries, pick one" doesn't
    specify ordering beyond that, and FIFO is the simplest thing that keeps
    old entries from being buried forever by newer ones.
    """
    for entry in _read_all_lines(path):
        if entry.get("used") is False or entry.get("used") is None:
            return entry
    return None


def get_all_unused(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return every unused entry, in file order."""
    return [
        e
        for e in _read_all_lines(path)
        if e.get("used") is False or e.get("used") is None
    ]


def mark_used(entry_ts: str, path: Optional[str] = None) -> bool:
    """
    Mark the entry with this `ts` as used: true. `ts` is the natural key
    here since entries don't carry a separate id — SPEC's shape has no id
    field, and ts is written at append time, so it's stable and unique in
    practice (two entries at the exact same microsecond is the only failure
    mode, which we treat as "mark the first match" — acceptable given this
    is a low-volume, single-writer file).

    Returns True if an entry was found and updated, False otherwise. This
    rewrites the whole file (JSONL doesn't support in-place line edits);
    fine at this volume (a handful of entries/day).
    """
    target = _queue_path(path)
    entries = _read_all_lines(target)

    found = False
    for entry in entries:
        if entry.get("ts") == entry_ts and not found:
            entry["used"] = True
            found = True

    if not found:
        return False

    with open(target, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return True


def count_unused(path: Optional[str] = None) -> int:
    return len(get_all_unused(path))
