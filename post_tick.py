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
            "text": final_text,
            "dry_run": _is_dry_run(),
            "uri": (post_result or {}).get("uri"),
            "cid": (post_result or {}).get("cid"),
        },
    )


def run_tick() -> int:
    """
    Execute one posting tick. Returns a process exit code (0 = clean run,
    1 = failure). Side effects (log writes, mark_used, real post) only
    happen along the paths described in the module docstring above.
    """
    entry = content_queue.get_next_unused()

    if entry is None:
        print("[post_tick] no unused queue entries — nothing to do. (I-SUBSTANCE: never fabricate.)")
        return 0

    print(f"[post_tick] picked entry ts={entry.get('ts')} type={entry.get('type')}")

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
