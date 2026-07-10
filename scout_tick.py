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
import rank
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

    # Frontier auto-follow (SPEC-v7): follow the curated watchlist within the SHARED
    # daily follow cap + pacing (pre-vetted -> fills the budget preferentially).
    # Autonomous follow, gated on the confirmed bot self-label; wrapped so it can
    # NEVER break the scout tick.
    try:
        import frontier
        n_f = frontier.follow_watchlist(session, dry_run=dry, own_did=session.get("did"))
        if n_f:
            print(f"[scout_tick] frontier: auto-followed {n_f} watchlist account(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"[scout_tick] frontier auto-follow errored (non-fatal): {exc!r}")

    candidates = scout.scan(session=session, persist=not dry)
    if not candidates:
        print("[scout_tick] no new candidates this tick; done")
        return 0

    # Ops-Insight Harvest (SPEC-v6): a SECOND, review-only lens over the SAME
    # candidates (reuse-only — no new scan). Runs on ALL scanned candidates,
    # independent of the mission triage below (an off-mission post can still carry
    # a transferable technique). Wrapped so it can NEVER break the scout tick.
    try:
        import ops_insight
        n_ins = ops_insight.harvest(candidates, dry_run=dry)
        if n_ins:
            print(f"[scout_tick] ops-insight: posted {n_ins} brief(s) to the review channel")
    except Exception as exc:  # noqa: BLE001
        print(f"[scout_tick] ops-insight harvest errored (non-fatal): {exc!r}")

    surfaced_items = triage.classify_all(candidates)

    # Frontier monitoring feed (SPEC-v7, review-only): post watchlist activity to the
    # frontier channel — study_closely = EVERY post; high_signal = notable/on-mission.
    # Runs REGARDLESS of whether mission-triage surfaced anything (study_closely must
    # feed even off-mission). Wrapped; takes no action, writes no brain/memory.
    try:
        import frontier
        on_mission_uris = {it.get("uri") for it in surfaced_items if it.get("uri")}
        n_fr = frontier.feed_candidates(candidates, on_mission_uris=on_mission_uris, dry_run=dry)
        if n_fr:
            print(f"[scout_tick] frontier: posted {n_fr} monitoring card(s) to the frontier channel")
    except Exception as exc:  # noqa: BLE001
        print(f"[scout_tick] frontier feed errored (non-fatal): {exc!r}")

    if not surfaced_items:
        print("[scout_tick] triage found nothing on-mission this tick; done")
        return 0

    # Rank best-first so the operator's scarce 👍 lands on the highest-leverage
    # rooms (SPEC-v3). Score components are logged on each item.
    surfaced_items = rank.rank_items(surfaced_items)
    print("[scout_tick] ranked (best-first): "
          + ", ".join(f"{it.get('author_handle')}={it.get('rank_score')}" for it in surfaced_items[:8]))

    n = surface.surface_all(surfaced_items, dry_run=dry)
    print(f"[scout_tick] done; surfaced {n} item(s) for your review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
