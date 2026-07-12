"""
refill_tick.py — entrypoint for the content-refill loop (SPEC-content-refill-v1).

Two phases in one run:
  - poll+enqueue (ALWAYS): pick up the operator's 👍 on previously-surfaced candidates
    and promote them into content-queue.jsonl (posts verbatim later). Also fires the
    queue-empty alert. This runs on every tick (heartbeat-driven) so a 👍 lands within
    ~15 min, like the rest of the system.
  - generate+surface (MORNING / dispatch): generate N pillar-rotated candidates from
    approved sources, guard them fail-closed, dedup vs posted.jsonl, and surface to the
    review channel for the operator's 👍. Gated to the schedule/workflow_dispatch run
    via REFILL_GENERATE so the heartbeat poll doesn't over-generate.

Safe-degrade: no SLACK_BOT_TOKEN -> log + skip; NEVER auto-posts ungated content.
"""

from __future__ import annotations

import os
import sys

import content_refill
import post_tick  # reuse _recent_pillars (reads posted.jsonl)


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def _should_generate() -> bool:
    return os.environ.get("REFILL_GENERATE", "").strip() in ("1", "true", "True", "yes")


def run_tick() -> int:
    dry = _is_dry_run()
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL_REFILL", "").strip() or content_refill.DEFAULT_REFILL_CHANNEL
    operator_id = os.environ.get("SLACK_OPERATOR_USER_ID", "").strip() or None

    # Safe-degrade: without a bot token we can neither surface for review nor poll for
    # the 👍, and we must NEVER auto-post ungated content. Log and skip.
    if not token and not dry:
        print("[refill_tick] no SLACK_BOT_TOKEN, skipping (content refill idle — never auto-posts ungated).")
        return 0

    print(f"[refill_tick] starting (dry_run={dry}, generate={_should_generate()})")

    # Phase 1 — always: promote any operator-approved candidates.
    try:
        enq = content_refill.poll_and_enqueue(token, channel, operator_id, dry_run=dry)
        print(f"[refill_tick] enqueued {enq} approved candidate(s) this tick.")
    except Exception as exc:  # noqa: BLE001 — never crash the tick
        print(f"[refill_tick] poll_and_enqueue errored (non-fatal): {exc!r}")

    # Phase 2 — morning/dispatch: generate + surface a fresh batch for review.
    # I-NO-DECOY: the bot's evergreen generator is a FALLBACK. If the operator's PC-side
    # research task already posted researched cards to the channel this cycle, THOSE are
    # the surface — the bot must NOT also surface evergreen (never both in one cycle).
    if _should_generate():
        try:
            if content_refill.researched_cards_present(token, channel):
                print("[refill_tick] researched candidate cards present this cycle; "
                      "suppressing the evergreen fallback (I-NO-DECOY).")
            else:
                recent = post_tick._recent_pillars(n=content_refill.META_WINDOW)
                candidates = content_refill.generate_candidates(recent_pillars=recent)
                # Pass the SAME recent-pillar history so surface only shows currently-
                # postable candidates (a 👍 must mean "this will post") — not the funnel.
                n = content_refill.surface_candidates(candidates, token, channel, dry_run=dry,
                                                      recent_pillars=recent)
                print(f"[refill_tick] surfaced {n}/{len(candidates)} evergreen fallback candidate(s) "
                      f"to {channel} for review.")
        except Exception as exc:  # noqa: BLE001
            print(f"[refill_tick] generate/surface errored (non-fatal): {exc!r}")

    # Queue-health guardrails (the silent-failure fixes):
    #  - queue EMPTY (0 unused) -> the silent-drain alert.
    #  - queue NON-empty but rotation blocks every item (get_next_rotated -> None)
    #    -> the silent-DEADLOCK alert (today's bug: only a META item queued, rotation
    #    correctly won't post it, and it was invisible). recent_pillars drives the
    #    same rotation check post_tick uses.
    try:
        # PR C: retire stale rotation-stranded entries FIRST so the alerts below reflect
        # the real queue (a day-old lone META must not fake a non-empty, healthy queue).
        content_refill.expire_stale_queue(dry_run=dry)
        fired_empty = content_refill.queue_empty_alert(token, channel, dry_run=dry)
        if not fired_empty:
            recent = post_tick._recent_pillars(n=content_refill.META_WINDOW)
            content_refill.queue_rotation_blocked_alert(recent, token, channel, dry_run=dry)
    except Exception as exc:  # noqa: BLE001
        print(f"[refill_tick] queue-health alert errored (non-fatal): {exc!r}")

    print("[refill_tick] done.")
    return 0


if __name__ == "__main__":
    sys.exit(run_tick())
