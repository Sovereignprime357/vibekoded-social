"""
post_tick.py — scheduled POSTING entrypoint.

Invoked by .github/workflows/post.yml on a cron (a few times/day). Also
runnable manually: `python post_tick.py`.

Flow (per SPEC.md ARCHITECTURE):
  get_next_unused -> generate -> guard.check
    -> if guard fails: SKIP, log to skipped.jsonl, do NOT post
    -> if guard passes: post to Bluesky -> mark_used -> log to posted.jsonl

DRY_RUN=1 (or a missing GEN_MODEL API key, which generate.py already
handles): does everything except the real network post. Prints exactly
what it WOULD post and does not call bluesky.post() at all — so the whole
pipeline is exercisable with zero credentials and zero side effects beyond
local log files.

Exit codes:
  0 — ran cleanly (whether that means "posted", "skipped: nothing queued",
      or "skipped: guard blocked" — all are successful *runs* of the tick,
      not failures of the script).
  1 — an actual failure (generation raised, Bluesky API call failed, etc.)
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, Optional

import guard
import content_queue
import generate

STATE_DIR = os.path.dirname(os.path.abspath(__file__))
POSTED_LOG = os.path.join(STATE_DIR, "posted.jsonl")
SKIPPED_LOG = os.path.join(STATE_DIR, "skipped.jsonl")


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_jsonl(path: str, record: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _log_skip(entry: Optional[Dict[str, Any]], generated_text: str, reason: str) -> None:
    _append_jsonl(
        SKIPPED_LOG,
        {
            "ts": _now_iso(),
            "entry_ts": entry.get("ts") if entry else None,
            "generated_text": generated_text,
            "reason": reason,
        },
    )


def _log_posted(entry: Dict[str, Any], final_text: str, post_result: Optional[Dict[str, Any]]) -> None:
    _append_jsonl(
        POSTED_LOG,
        {
            "ts": _now_iso(),
            "entry_ts": entry.get("ts"),
            "entry_type": entry.get("type"),
            "pillar": entry.get("pillar"),
            "text": final_text,
            "dry_run": _is_dry_run(),
            "uri": (post_result or {}).get("uri"),
            "cid": (post_result or {}).get("cid"),
        },
    )


def _recent_pillars(n: int = content_queue.META_WINDOW) -> list:
    """
    Read the pillars of the last `n` posted entries, MOST RECENT FIRST, from
    posted.jsonl. This is the rotation memory that feeds
    content_queue.get_next_rotated — a post from the same pillar as the last
    one, or a META within the meta-cap window, gets skipped.

    Legacy posted records (pre-v3) have no `pillar`; they read back as "" and
    simply don't constrain rotation, which is the intended graceful behavior.
    """
    if not os.path.exists(POSTED_LOG):
        return []
    rows: list = []
    try:
        with open(POSTED_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    pillars = [str(r.get("pillar") or "").strip().lower() for r in rows]
    return list(reversed(pillars))[:n]


def run_tick() -> int:
    """
    Execute one posting tick. Returns a process exit code (0 = clean run,
    1 = failure). Side effects (log writes, mark_used, real post) only
    happen along the paths described in the module docstring above.
    """
    recent = _recent_pillars()
    entry = content_queue.get_next_rotated(recent_pillars=recent)

    if entry is None:
        print("[post_tick] no postable queue entry under the pillar-rotation rules — nothing to do. (I-SUBSTANCE: never fabricate; I-PILLAR-MIX: never burst META.)")
        return 0

    print(
        f"[post_tick] picked entry ts={entry.get('ts')} type={entry.get('type')} "
        f"pillar={entry.get('pillar') or '(untagged)'} (recent pillars: {recent or 'none'})"
    )

    # SPEC-content-refill: a content-refill entry carries a pre-generated,
    # operator-approved `final_text` — post it VERBATIM (the exact text they 👍'd),
    # skipping generation. The privacy guard below still runs on it (I-GUARDED
    # safety net). Legacy entries (raw only) generate as before.
    final_text = str(entry.get("final_text", "")).strip()
    if final_text:
        print("[post_tick] using pre-approved final_text (content-refill); skipping generation.")
        text = final_text
    else:
        try:
            text = generate.generate(entry, kind="post")
        except Exception as exc:  # noqa: BLE001 — a generation crash must not crash the tick
            print(f"[post_tick] generation raised an exception: {exc!r}")
            _log_skip(entry, "", f"generation exception: {exc!r}")
            return 1

    if not text:
        print("[post_tick] generation returned empty output — skipping this tick, entry stays unused for next run.")
        _log_skip(entry, "", "generation returned empty output")
        return 0

    ok, reason = guard.check(text)
    if not ok:
        print(f"[post_tick] GUARD BLOCKED this post: {reason}")
        print(f"[post_tick] blocked text was: {text!r}")
        _log_skip(entry, text, f"guard blocked: {reason}")
        # Deliberately do NOT mark_used here — SPEC's I-PRIVACY edge case
        # says "skip + log for your review; never post a redacted guess."
        # Leaving it unused lets the operator fix the source entry (or the
        # guard's EXTRA_TERMS) and let it flow through next time, rather
        # than silently burning the entry.
        return 0

    if _is_dry_run():
        print("=" * 60)
        print("[post_tick] DRY_RUN — would post the following to Bluesky:")
        print(text)
        print("=" * 60)
        content_queue.mark_used(entry["ts"])
        _log_posted(entry, text, post_result=None)
        return 0

    try:
        import bluesky

        session = bluesky.create_session()
        result = bluesky.post(text, session=session)
    except Exception as exc:  # noqa: BLE001
        print(f"[post_tick] Bluesky post failed: {exc!r}")
        _log_skip(entry, text, f"bluesky post failed: {exc!r}")
        return 1

    print(f"[post_tick] posted: uri={result.get('uri')}")
    content_queue.mark_used(entry["ts"])
    _log_posted(entry, text, result)
    return 0


if __name__ == "__main__":
    sys.exit(run_tick())
