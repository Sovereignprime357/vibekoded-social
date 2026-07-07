"""
scout_tick.py — entrypoint for the agentic SEE -> TRIAGE -> SURFACE loop.

Short-lived, run-once-and-exit (same shape as post_tick.py): a GitHub Actions
cron invokes it on a schedule; it scans, triages, surfaces to Slack, and
exits. No daemon, no persistent process.

Cost profile per run:
  SEE     — plain Bluesky reads, no model.
  TRIAGE  — one free-model call per candidate (Gemini/Groq). The only spend,
            and it's free.
  SURFACE — plain HTTP POST to Slack, no model.
Claude (the paid model) is NOT in this path at all — it only ever writes a
reply, and only after the operator approves one (a later tier).

DRY_RUN=1:
  - scout does NOT persist state/seen (posts aren't "consumed").
  - triage falls back to its labelled stub if no free key is set.
  - surface PRINTS the Slack payload instead of sending it, and writes no
    ledger — so the whole thing is a repeatable, side-effect-free preview of
    exactly what would land in Slack.
  (A live scan still needs BSKY creds even in dry-run — searchPosts is a real
  read. Unit tests, which need zero creds, exercise the pure functions.)
"""

from __future__ import annotations

import os
import sys

import bluesky
import scout
import surface
import triage


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def main() -> int:
    dry = _is_dry_run()
    print(f"[scout_tick] starting (dry_run={dry})")

    try:
        session = bluesky.create_session()
    except bluesky.BlueskyError as exc:
        # No usable session -> we can't scan. Surface the reason and exit
        # non-zero so a broken login is visible in the Actions run, but don't
        # traceback-crash (keeps the workflow log clean).
        print(f"[scout_tick] cannot create Bluesky session: {exc}")
        return 1

    candidates = scout.scan(session=session, persist=not dry)
    if not candidates:
        print("[scout_tick] no new candidates this tick; done")
        return 0

    surfaced_items = triage.classify_all(candidates)
    if not surfaced_items:
        print("[scout_tick] triage found nothing on-mission this tick; done")
        return 0

    n = surface.surface_all(surfaced_items, dry_run=dry)
    print(f"[scout_tick] done; surfaced {n} item(s) for your review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
