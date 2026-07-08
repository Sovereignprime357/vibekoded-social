"""
act_tick.py — entrypoint for the ACT layer (SPEC-v2 T1.5 / SPEC-v3).

Runs on the scout-act cron (every 15 min, so an operator 👍 is acted on within
~15 min; scout-tick still surfaces hourly). For each surfaced item the operator
has 👍'd in Slack, it executes the proposed action and logs it. The thumbsup IS the approval (I-HUMAN-GATE) — nothing is ever
autonomous in this build.

Loop:
  1. Load surfaced items that are ACTIONABLE (have a Slack ts+channel) and not
     already acted (act-log.jsonl).
  2. For each: skip our own posts (I-NO-SELF); poll Slack for the operator's
     thumbsup; if none, leave it for a later tick.
  3. On a thumbsup, enforce the per-class daily cap (I-SELECTIVE), then dispatch
     via act.execute_action (guard fail-closed on replies, I-PRIVACY).
  4. Log every terminal outcome (I-LOGGED) and mark it acted so it never
     double-fires.

Safe-degrade (never breaks the existing scout/surface):
  - No SLACK_BOT_TOKEN  -> log "no SLACK_BOT_TOKEN, skipping" and do nothing.
  - No SLACK_CHANNEL_ID -> same (can't poll without a channel).
  - No Bluesky session  -> log and exit non-zero (a real misconfig), but this
    only ever runs when a token IS set, so it won't fire pre-setup.
  - DRY_RUN=1           -> poll is a read (safe); execute_action makes NO write.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import act

HERE = os.path.dirname(os.path.abspath(__file__))
SURFACED_PATH = os.path.join(HERE, "scout-surfaced.jsonl")
ACT_LOG = os.path.join(HERE, "act-log.jsonl")


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------


def load_actionable(path: str = SURFACED_PATH) -> List[Dict[str, Any]]:
    """
    Surfaced records that carry a Slack ts+channel (posted via the bot token, so
    a reaction can be polled). Records surfaced via the legacy webhook have no
    ts and are silently skipped here — not actionable, by design.
    """
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("slack_ts") and rec.get("slack_channel") and rec.get("uri"):
                out.append(rec)
    return out


def load_acted(path: str = ACT_LOG) -> Tuple[Set[str], Dict[str, int]]:
    """
    Read the act log. Returns:
      - acted_uris: URIs that reached a TERMINAL status (never fire again).
      - today_counts: {action: n executed today (UTC)} for the daily caps.
    """
    acted: Set[str] = set()
    counts: Dict[str, int] = {}
    if not os.path.exists(path):
        return acted, counts
    today = _today_utc()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = rec.get("status")
            if status in act.TERMINAL_STATUSES and rec.get("uri"):
                acted.add(rec["uri"])
            if status == "executed" and str(rec.get("ts", "")).startswith(today):
                a = rec.get("action")
                if a:
                    counts[a] = counts.get(a, 0) + 1
    return acted, counts


def _log_act(record: Dict[str, Any], path: Optional[str] = None) -> None:
    # Resolve ACT_LOG at CALL time (not as a def-time default), so a test that
    # monkeypatches act_tick.ACT_LOG actually redirects the write — and so a
    # stray write can never land in the real committed ledger.
    target = path or ACT_LOG
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def run_tick() -> int:
    dry = _is_dry_run()
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel_default = os.environ.get("SLACK_CHANNEL_ID", "").strip()
    operator_id = os.environ.get("SLACK_OPERATOR_USER_ID", "").strip() or None

    if not token:
        print("[act_tick] no SLACK_BOT_TOKEN, skipping (ACT layer idle — operator hasn't set up the Slack app yet).")
        return 0
    if not channel_default:
        print("[act_tick] no SLACK_CHANNEL_ID, skipping (can't poll reactions without a channel).")
        return 0
    if not operator_id:
        # I-HUMAN-GATE, fail-closed (2026-07-08 mass-fire fix): without the
        # operator's Slack user id we CANNOT verify that a 👍 came from the
        # operator (vs a teammate / Slackbot / integration auto-reaction), so we
        # act on NOTHING. This is the exact hole that let un-approved items fire.
        print("[act_tick] SLACK_OPERATOR_USER_ID not set — cannot verify operator approval; "
              "acting on NOTHING (I-HUMAN-GATE fail-closed). Set it to your Slack user id (U…) to enable acting.")
        return 0

    actionable = load_actionable()
    if not actionable:
        print("[act_tick] no actionable surfaced items (none carry a Slack ts yet); done.")
        return 0

    acted_uris, today_counts = load_acted()
    caps = act.load_caps()
    pacing_seconds = act.load_pacing_seconds()
    max_per_tick = act.load_max_per_tick()
    auto_classes = act.load_auto_reply_classes()  # AUTO_REPLY_BACK earn-it ladder (default empty)

    # A Bluesky session is required for the writes AND for our own DID (I-NO-SELF).
    try:
        session = bluesky_session()
    except Exception as exc:  # noqa: BLE001
        print(f"[act_tick] cannot create Bluesky session: {exc}")
        return 1
    own_did = (session or {}).get("did")

    pending = [it for it in actionable if it.get("uri") not in acted_uris]
    print(f"[act_tick] {len(pending)} un-acted actionable item(s); dry_run={dry} "
          f"max_per_tick={max_per_tick} pacing={pacing_seconds}s operator={operator_id}")

    executed = 0
    for item in pending:
        uri = item.get("uri")
        action = str(item.get("action", "")).strip().lower()

        # Hard per-tick ceiling (belt): stop executing once we hit it; the rest
        # defer to a later tick (they stay un-acted, not logged terminal). This
        # is the backstop that makes a mass-fire structurally impossible.
        if executed >= max_per_tick:
            print(f"[act_tick] per-tick cap reached ({executed}/{max_per_tick}); deferring the remaining {len(pending) - pending.index(item)} item(s) to next tick.")
            break

        # I-NO-SELF: never engage our own posts. Terminal so it never re-checks.
        if act.is_self(item, own_did):
            print(f"[act_tick] skip (I-NO-SELF): our own post {uri}")
            _log_act({"ts": _now_iso(), "uri": uri, "action": action, "status": "skipped_self"})
            continue

        reactions = act.get_reactions(item["slack_channel"], item["slack_ts"], token)
        approved = act.operator_thumbsup(reactions, operator_id)
        # Autonomy (AUTO_REPLY_BACK) only where a class has EARNED it — converse
        # reply-backs, default none. An operator 👍 always takes precedence.
        auto = (not approved) and act.auto_eligible(item, auto_classes)
        if not (approved or auto):
            # Not approved (no operator 👍 on THIS message, not auto-eligible).
            # Leave it — a later tick re-polls. Not logged. Strict per-item gate.
            continue
        approval_mode = "operator_thumbsup" if approved else f"auto:{item.get('reply_class')}"

        # Approved. Enforce the per-class daily cap (I-SELECTIVE). A cap hit is
        # transient (resets tomorrow) so it is NOT marked acted — retried later.
        if not act.within_cap(action, today_counts, caps):
            print(f"[act_tick] cap reached for {action} ({today_counts.get(action,0)}/{caps.get(action)}); deferring {uri}")
            continue

        # Pace executed public actions apart so multiple approvals never fire in
        # a burst. Sleep BEFORE the 2nd+ execution (not before the first, not
        # after skips), and never in dry-run.
        if executed > 0 and pacing_seconds > 0 and not dry:
            print(f"[act_tick] pacing {pacing_seconds}s before next action…")
            time.sleep(pacing_seconds)

        result = act.execute_action(item, session=session, dry_run=dry)
        record = {
            "ts": _now_iso(),
            "uri": uri,
            "cid": item.get("cid"),
            "author_did": item.get("author_did"),
            "action": action,
            "source": item.get("source"),
            "reply_class": item.get("reply_class"),
            "approval_mode": approval_mode,
            "slack_ts": item.get("slack_ts"),
            "dry_run": dry,
            **result,
        }
        _log_act(record)
        status = result.get("status", "")
        print(f"[act_tick] {action} on {uri} -> {status}"
              + (f" ({result.get('reason')})" if status == "guard_blocked" else ""))

        if status == "executed":
            today_counts[action] = today_counts.get(action, 0) + 1
            executed += 1
        # dry_run_* statuses are non-terminal: nothing was actually done, so we
        # deliberately do NOT block a real run later. They ARE logged above for
        # the audit trail but their uri is not in TERMINAL_STATUSES.

    print(f"[act_tick] done; executed {executed} action(s) this tick.")
    return 0


def bluesky_session() -> Dict[str, Any]:
    """Import bluesky lazily so a missing dependency can't break the no-token safe-degrade path."""
    import bluesky
    return bluesky.create_session()


if __name__ == "__main__":
    sys.exit(run_tick())
