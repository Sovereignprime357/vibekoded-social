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

import calendar
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import act
import bluesky

HERE = os.path.dirname(os.path.abspath(__file__))
SURFACED_PATH = os.path.join(HERE, "scout-surfaced.jsonl")
ACT_LOG = os.path.join(HERE, "act-log.jsonl")

# --- Poll-set bounds (2026-07-10 backlog fix) ------------------------------
# scout-surfaced.jsonl grows forever; polling EVERY un-acted item (~515) blew
# Slack's reactions.get Tier-3 rate limit (~50/min) -> everything "ratelimited"
# -> no 👍 ever read -> executed 0. So we only poll RECENT items, cap the count,
# expire stale ones out of the ledger, and pace the polls. Env-overridable.
POLL_WINDOW_HOURS = float(os.environ.get("ACT_POLL_WINDOW_HOURS", "48") or "48")
POLL_MAX_ITEMS = int(os.environ.get("ACT_POLL_MAX_ITEMS", "40") or "40")
EXPIRE_HOURS = float(os.environ.get("ACT_EXPIRE_HOURS", "72") or "72")
REACTION_POLL_SLEEP_S = float(os.environ.get("ACT_REACTION_POLL_SLEEP", "0.7") or "0.7")


def _parse_iso_epoch(ts: str) -> Optional[float]:
    """Parse a surfaced 'YYYY-MM-DDThh:mm:ssZ' (UTC) timestamp to epoch seconds, or None."""
    if not ts:
        return None
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------


def _iter_surfaced(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """All surfaced records carrying a pollable Slack ts+channel+uri (unbounded)."""
    path = path or SURFACED_PATH  # resolve at CALL time (monkeypatch-safe, no def-time bind)
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


def load_actionable(
    path: Optional[str] = None,
    window_hours: Optional[float] = None,
    max_items: Optional[int] = None,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    RECENT surfaced records to poll this tick — BOUNDED so reactions.get calls/tick
    stay well under Slack's Tier-3 rate limit. Only items surfaced within
    `window_hours` (default POLL_WINDOW_HOURS), newest-first, capped at `max_items`
    (default POLL_MAX_ITEMS). A 👍 lands within a poll cycle or two of surfacing, so
    a bounded recent window loses nothing real while making the poll O(1) not O(all).

    Items with an unparseable surfaced-ts are treated as fresh (kept), so a format
    hiccup never silently drops a live item — the cap still bounds them.
    """
    window_hours = POLL_WINDOW_HOURS if window_hours is None else window_hours
    max_items = POLL_MAX_ITEMS if max_items is None else max_items
    now = time.time() if now is None else now
    cutoff = now - window_hours * 3600.0

    recent: List[Tuple[float, Dict[str, Any]]] = []
    for rec in _iter_surfaced(path):
        epoch = _parse_iso_epoch(rec.get("ts", ""))
        sort_key = epoch if epoch is not None else now  # unparseable => treat as fresh
        if epoch is not None and epoch < cutoff:
            continue  # too old for this poll window (expire_stale retires it)
        recent.append((sort_key, rec))
    recent.sort(key=lambda t: t[0], reverse=True)  # newest first
    return [rec for _, rec in recent[:max_items]]


def load_targeted(target_ts: str, target_channel: Optional[str] = None,
                  path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    TARGETED (event-driven) mode: the specific surfaced item(s) the operator just
    👍'd, looked up by the reacted message's slack_ts (+ channel if given). Reads
    the full ledger but matches by ts, so it's O(1) intent — one item, one
    reactions.get to re-verify — no backlog scan, no rate-limit exposure.
    """
    target_ts = str(target_ts or "").strip()
    if not target_ts:
        return []
    out: List[Dict[str, Any]] = []
    for rec in _iter_surfaced(path):
        if rec.get("slack_ts") != target_ts:
            continue
        if target_channel and rec.get("slack_channel") != target_channel:
            continue
        out.append(rec)
    return out


def expire_stale(
    acted_uris: Set[str],
    now: Optional[float] = None,
    expire_hours: Optional[float] = None,
    path: Optional[str] = None,
    log_path: Optional[str] = None,
) -> Set[str]:
    """
    Permanently retire surfaced items older than `expire_hours` (default EXPIRE_HOURS)
    that were never acted: append an "expired" (terminal) record to the act log so they
    drop out of the poll set forever. Stops the unbounded backlog growth that tripped
    the rate limit. Stale engagement opportunities; abandoning them is fine (SPEC-ok).
    Returns the set of uris expired this call.
    """
    now = time.time() if now is None else now
    expire_hours = EXPIRE_HOURS if expire_hours is None else expire_hours
    cutoff = now - expire_hours * 3600.0
    expired: Set[str] = set()
    for rec in _iter_surfaced(path):
        uri = rec.get("uri")
        if not uri or uri in acted_uris or uri in expired:
            continue
        epoch = _parse_iso_epoch(rec.get("ts", ""))
        if epoch is None or epoch >= cutoff:
            continue  # unparseable or still within TTL -> not expired
        _log_act({"ts": _now_iso(), "uri": uri, "action": rec.get("action"),
                  "status": "expired", "reason": f"un-acted >{expire_hours:.0f}h since surfaced"},
                 path=log_path)
        expired.add(uri)
    if expired:
        print(f"[act_tick] expired {len(expired)} stale un-acted item(s) (>{expire_hours:.0f}h old).")
    return expired


def load_acted(path: Optional[str] = None) -> Tuple[Set[str], Dict[str, int]]:
    """
    Read the act log. Returns:
      - acted_uris: URIs that reached a TERMINAL status (never fire again).
      - today_counts: {action: n executed today (UTC)} for the daily caps.
    """
    path = path or ACT_LOG  # resolve at CALL time (monkeypatch-safe)
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


def _post_auto_follow_digest(handles: List[str], token: str, channel: str, dry: bool) -> None:
    """
    One lightweight Slack FYI per tick listing the accounts we auto-followed
    (SPEC-v3 AUTONOMY LADDER: follows are autonomous but stay visible). Not
    gated, not per-follow. Best-effort — a Slack hiccup never fails the tick.
    """
    if not handles:
        return
    msg = "🔗 auto-followed: " + ", ".join(f"@{h}" for h in handles)
    if dry:
        print(f"[act_tick] DRY_RUN digest: {msg}")
        return
    try:
        import surface
        surface._post_slack_web(msg, token, channel)
    except Exception as exc:  # noqa: BLE001
        print(f"[act_tick] auto-follow digest post failed (non-fatal): {exc!r}")


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

    now = time.time()
    acted_uris, today_counts = load_acted()

    # EVENT-DRIVEN targeting (SPEC-v4.1): when woken by a Slack reaction_added event,
    # scout-act passes the reacted message's ts+channel (client_payload) as
    # ACT_TARGET_TS/ACT_TARGET_CHANNEL. Act on THAT one item instantly — no backlog
    # scan, one reactions.get to re-verify. Absent (cron/heartbeat fallback) -> the
    # bounded recent poll. Either way the SAME gate/execute loop below runs, and
    # idempotency is via acted_uris (act-log) so the webhook + the poll can't
    # double-act the same item.
    target_ts = os.environ.get("ACT_TARGET_TS", "").strip()
    target_channel = os.environ.get("ACT_TARGET_CHANNEL", "").strip() or None
    if target_ts:
        actionable = load_targeted(target_ts, target_channel)
        print(f"[act_tick] EVENT-TARGETED mode: ts={target_ts} channel={target_channel} "
              f"-> {len(actionable)} matching surfaced item(s)")
        if not actionable:
            print("[act_tick] no surfaced item matches the reacted ts (not an actionable message); done.")
            return 0
    else:
        # Retire stale un-acted items so the poll set can't grow unbounded (the backlog
        # that tripped Slack's reactions.get rate limit), then poll only the RECENT,
        # capped window — keeps reactions.get calls/tick well under Slack's ~50/min.
        acted_uris |= expire_stale(acted_uris, now=now)
        actionable = load_actionable(now=now)
        if not actionable:
            print("[act_tick] no recent actionable surfaced items to poll this tick; done.")
            return 0

    caps = act.load_caps()
    pacing_seconds = act.load_pacing_seconds()
    max_per_tick = act.load_max_per_tick()
    auto_act_classes = act.load_auto_act_classes()      # AUTO_ACT_CLASSES (default {"follow"})
    auto_reply_classes = act.load_auto_reply_classes()  # AUTO_REPLY_BACK (default empty)

    # A Bluesky session is required for the writes AND for our own DID (I-NO-SELF).
    try:
        session = bluesky_session()
    except Exception as exc:  # noqa: BLE001
        print(f"[act_tick] cannot create Bluesky session: {exc}")
        return 1
    own_did = (session or {}).get("did")

    # I-BOT-DISCLOSED is the safety basis for ALL autonomy (SPEC-v3). If any class
    # can act without a 👍, confirm the `bot` self-label is set first; if we can't
    # confirm it, disable autonomy for this tick (fail-closed) — 👍-gated actions
    # are unaffected. Idempotent: a no-op once the label is set.
    if auto_act_classes or auto_reply_classes:
        try:
            res = bluesky.ensure_bot_label(session)
            print(f"[act_tick] I-BOT-DISCLOSED: bot self-label {res.get('status')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[act_tick] could NOT confirm bot self-label ({exc!r}); disabling ALL autonomy this "
                  "tick (fail-closed). 👍-gated actions still run.")
            auto_act_classes = set()
            auto_reply_classes = set()

    pending = [it for it in actionable if it.get("uri") not in acted_uris]
    print(f"[act_tick] {len(pending)} un-acted actionable item(s) in the recent window "
          f"(<= {POLL_MAX_ITEMS} polled/tick); dry_run={dry} "
          f"max_per_tick={max_per_tick} pacing={pacing_seconds}s operator={operator_id}")

    executed = 0
    polled = 0
    auto_followed: list = []  # handles auto-followed this tick, for the Slack FYI digest
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

        # Pace the reaction polls so a burst can't trip Slack's rate limit (belt on
        # top of the bounded poll set). Sleep between polls, never before the first.
        if polled > 0 and REACTION_POLL_SLEEP_S > 0 and not dry:
            time.sleep(REACTION_POLL_SLEEP_S)
        reactions = act.get_reactions(item["slack_channel"], item["slack_ts"], token)
        polled += 1
        approved = act.operator_thumbsup(reactions, operator_id)
        # Autonomy only where a class has EARNED it (AUTONOMY LADDER): follow via
        # AUTO_ACT_CLASSES (default), converse reply-backs via AUTO_REPLY_BACK
        # (default off). An operator 👍 always takes precedence.
        auto = (not approved) and act.auto_eligible(item, auto_act_classes, auto_reply_classes)
        if not (approved or auto):
            # Not approved (no operator 👍 on THIS message, not auto-eligible).
            # Leave it — a later tick re-polls. Not logged. Strict per-item gate.
            continue
        approval_mode = "operator_thumbsup" if approved else f"auto:{action}"

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
              + (f" ({result.get('reason')})" if status in ("guard_blocked", "voice_blocked") else ""))

        if status == "executed":
            today_counts[action] = today_counts.get(action, 0) + 1
            executed += 1
            # Autonomous follows get a lightweight FYI digest (not gated, not
            # per-follow) so the operator keeps visibility without action.
            if action == "follow" and not approved:
                handle = item.get("author_handle") or item.get("author_did") or "?"
                auto_followed.append(handle)
        # dry_run_* statuses are non-terminal: nothing was actually done, so we
        # deliberately do NOT block a real run later. They ARE logged above for
        # the audit trail but their uri is not in TERMINAL_STATUSES.

    _post_auto_follow_digest(auto_followed, token, channel_default, dry)
    print(f"[act_tick] done; executed {executed} action(s) this tick.")
    return 0


def bluesky_session() -> Dict[str, Any]:
    """Import bluesky lazily so a missing dependency can't break the no-token safe-degrade path."""
    import bluesky
    return bluesky.create_session()


if __name__ == "__main__":
    sys.exit(run_tick())
