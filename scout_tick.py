"""
scout_tick.py — entrypoint for the MINER loop: SEE -> HARVEST -> (frontier feed).

Rewired 2026-08-21 under SPEC-vibekoded-social-miner (BRAIN2.0/blueprints/): the
broadcast/engagement half of this system is retired (post/notify/scout-act/refill
workflows disabled — not deleted). This tick no longer proposes likes, replies, or
reposts and posts NO engagement cards to Slack. What remains, and is now the point:

  SEE      — scout.scan(): keyword-lane search (MISSION-FILTER.md, 4 mining lanes).
  HARVEST  — ops_insight.harvest(): the mechanism lens. Flags high-bar material
             (watchlist authors get extract priority), extracts bounded briefs,
             appends them to ops-intel-log.jsonl (the weekly distill's feedstock).
  FRONTIER — follow_watchlist(): capped curation-follow of the verified watchlist;
             feed_candidates(): optional review-only monitoring cards (study_closely
             = every post; high_signal = lens-flagged posts).

Short-lived, run-once-and-exit. Cadence: the workflow fires on GitHub cron (hourly,
lagging) and/or the external heartbeat; a MIN-INTERVAL GATE below (I-COST-BOUND,
default 50 min, env SCOUT_MIN_INTERVAL_MINUTES) makes ~hourly a mechanism rather
than a hope — a faster trigger cadence cannot make the scan spend more.

DRY_RUN=1: no state persisted, no Slack sent, preview-only (unchanged).
"""

from __future__ import annotations

import calendar
import os
import sys
import time

import bluesky
import scout


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def _min_interval_minutes() -> int:
    v = os.environ.get("SCOUT_MIN_INTERVAL_MINUTES", "").strip()
    try:
        return max(0, int(v)) if v else 50
    except ValueError:
        return 50


def _cadence_gate_open(dry: bool) -> bool:
    """
    I-COST-BOUND: skip the scan if the last one is fresher than the min interval.
    Fail-open on any parse problem (a broken marker must never silence the miner),
    and always open in dry-run (previews are side-effect-free anyway).
    """
    if dry:
        return True
    min_min = _min_interval_minutes()
    if min_min <= 0:
        return True
    last = scout.load_state().get("last_scan")
    if not last:
        return True
    try:
        last_epoch = calendar.timegm(time.strptime(str(last), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return True
    age_min = (time.time() - last_epoch) / 60.0
    if age_min < min_min:
        print(f"[scout_tick] cadence gate: last scan {age_min:.0f}m ago "
              f"(< {min_min}m, SCOUT_MIN_INTERVAL_MINUTES); skipping this wake.")
        return False
    return True


def main() -> int:
    dry = _is_dry_run()
    print(f"[scout_tick] starting MINER tick (dry_run={dry})")

    if not _cadence_gate_open(dry):
        return 0

    try:
        session = bluesky.create_session()
    except bluesky.BlueskyError as exc:
        # No usable session -> we can't scan. Surface the reason and exit
        # non-zero so a broken login is visible in the Actions run (loud failure).
        print(f"[scout_tick] cannot create Bluesky session: {exc}")
        return 1

    # Frontier auto-follow (SPEC-v7, kept by the miner spec as CURATION, capped
    # 10/day shared budget): follow verified watchlist accounts. The one outward
    # action that survives the repurpose. Wrapped so it can NEVER break the tick.
    try:
        import frontier
        n_f = frontier.follow_watchlist(session, dry_run=dry, own_did=session.get("did"))
        if n_f:
            print(f"[scout_tick] frontier: auto-followed {n_f} watchlist account(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"[scout_tick] frontier auto-follow errored (non-fatal): {exc!r}")

    candidates = scout.scan(session=session, persist=not dry)
    if not candidates:
        print("[scout_tick] no new candidates this tick; done (an empty tick is a pass)")
        return 0

    # THE HARVEST — the miner's product. Flags mechanisms, extracts bounded briefs,
    # appends to ops-intel-log.jsonl (log bridge BEFORE Slack — repo is the record).
    # flagged_uris doubles as the "notable" signal for the frontier feed below.
    flagged_uris: set = set()
    try:
        import ops_insight
        n_ins = ops_insight.harvest(candidates, dry_run=dry, flagged_out=flagged_uris)
        if n_ins:
            print(f"[scout_tick] ops-insight: harvested {n_ins} brief(s)")
        else:
            print("[scout_tick] ops-insight: 0 briefs this tick (a pass, not a failure)")
    except Exception as exc:  # noqa: BLE001
        print(f"[scout_tick] ops-insight harvest errored (non-fatal): {exc!r}")

    # Frontier monitoring feed (review-only, an OPTIONAL raw tap — nothing depends
    # on the channel being read): study_closely = every post; high_signal = posts
    # the lens flagged. Takes no action, writes no brain/memory.
    try:
        import frontier
        n_fr = frontier.feed_candidates(candidates, on_mission_uris=flagged_uris, dry_run=dry)
        if n_fr:
            print(f"[scout_tick] frontier: posted {n_fr} monitoring card(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"[scout_tick] frontier feed errored (non-fatal): {exc!r}")

    print(f"[scout_tick] done; {len(candidates)} candidate(s) scanned, "
          f"{len(flagged_uris)} flagged by the lens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
