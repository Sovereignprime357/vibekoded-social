"""
act.py — the ACT layer (SPEC-v2 T1.5 / SPEC-v3): execute a surfaced action once
the operator has approved it with a 👍 in Slack.

The operator's thumbsup reaction IS the approval (I-HUMAN-GATE). This module
holds the pure, testable pieces:

  - operator_thumbsup(reactions, operator_id)  — parse a Slack reactions list and
    decide whether the OPERATOR (not just anyone) thumbs-upped the message.
  - within_cap(action, counts, caps)           — per-class daily cap (I-SELECTIVE).
  - is_self(item, own_did)                      — never act on our own posts (I-NO-SELF).
  - execute_action(item, session, ...)          — dispatch like/repost/follow/reply;
    replies are drafted in-voice and run through the fail-closed privacy guard
    BEFORE posting (I-PRIVACY). A guard block drops the reply, never sanitizes.

Network transport (Slack reads, Bluesky writes) lives here too but every function
that touches the network takes explicit args and is never invoked by any test —
the tests exercise the pure logic and monkeypatch the Bluesky/generate calls.

act_tick.py is the entrypoint that wires these together on the scout-act cron.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

import bluesky
import generate
import guard

# Slack reaction names for a thumbsup. Slack appends "::skin-tone-N" to
# skin-toned variants; we match on the base name, so all tones count.
THUMBSUP_NAMES = {"+1", "thumbsup"}

SLACK_REACTIONS_GET_EP = "https://slack.com/api/reactions.get"

# Per-class daily caps (I-SELECTIVE): discerning, low-volume, never farming.
# Overridable per class via ACT_CAP_LIKE / ACT_CAP_REPOST / ACT_CAP_FOLLOW /
# ACT_CAP_REPLY repo variables without a code change.
DEFAULT_CAPS = {"like": 20, "repost": 10, "follow": 10, "reply": 20}

# Burst protection (2026-07-08 mass-fire fix), independent of the daily caps:
#   - ACT_PACING_SECONDS: minimum gap between two EXECUTED public actions in one
#     tick, so multiple approvals can never fire within milliseconds.
#   - ACT_MAX_PER_TICK: hard ceiling on executed public actions per tick — a
#     belt so no bug can ever mass-act again; the remainder defers to next tick.
DEFAULT_PACING_SECONDS = 60.0
DEFAULT_MAX_PER_TICK = 3

# Terminal statuses: the item is done and must never fire again (I-LOGGED, no
# double-fire). Transient statuses (awaiting thumbsup, cap reached) are NOT
# written to the act log, so they're retried on a later tick.
TERMINAL_STATUSES = {"executed", "guard_blocked", "skipped_self", "empty_draft", "error"}


# ---------------------------------------------------------------------------
# Reaction parsing + operator filter (pure)
# ---------------------------------------------------------------------------


def _base_emoji(name: str) -> str:
    """Strip Slack's "::skin-tone-N" suffix so all thumbsup tones match."""
    return str(name or "").split("::", 1)[0]


def operator_thumbsup(reactions: Iterable[Dict[str, Any]], operator_id: Optional[str] = None) -> bool:
    """
    Decide whether the message carries an approving thumbsup FROM THE OPERATOR.

    reactions: the Slack `message.reactions` list, each item shaped
               {"name": "+1", "users": ["U123", ...], "count": N}.
    operator_id: the operator's Slack user id. Approval requires a thumbsup
                 whose `users` includes THIS id — a reaction from anyone or
                 anything else (a teammate, Slackbot, an integration's
                 auto-👍) is ignored (I-HUMAN-GATE).

    FAIL-CLOSED on identity (the 2026-07-08 mass-fire fix): if operator_id is
    not provided, we CANNOT verify the operator approved, so we return False —
    act on nothing — rather than accepting "any 👍 from anyone". The old
    permissive fallback is exactly what let auto-reactions approve everything.
    Callers (act_tick) must refuse to run without an operator id.

    Never raises on a malformed reactions blob — a shape it can't read means
    "no approval", which fails safe (nothing gets acted on).
    """
    if not operator_id:
        # Can't confirm it was the operator -> not approved. Never guess.
        return False
    try:
        for r in reactions or []:
            if _base_emoji(r.get("name")) not in THUMBSUP_NAMES:
                continue
            if operator_id in (r.get("users") or []):
                return True
    except Exception:  # noqa: BLE001 — unreadable blob => no approval (fail safe)
        return False
    return False


# ---------------------------------------------------------------------------
# Caps + self-check (pure)
# ---------------------------------------------------------------------------


def load_caps() -> Dict[str, int]:
    """Per-class caps from env (ACT_CAP_*) falling back to DEFAULT_CAPS."""
    caps = dict(DEFAULT_CAPS)
    for action in list(caps.keys()):
        val = os.environ.get(f"ACT_CAP_{action.upper()}", "").strip()
        if val:
            try:
                caps[action] = int(val)
            except ValueError:
                pass
    return caps


def within_cap(action: str, counts: Dict[str, int], caps: Optional[Dict[str, int]] = None) -> bool:
    """True if executing one more `action` today stays within its daily cap."""
    caps = caps if caps is not None else load_caps()
    cap = caps.get(action, 0)
    return counts.get(action, 0) < cap


def load_pacing_seconds() -> float:
    """Minimum gap (s) between executed public actions in a tick (ACT_PACING_SECONDS)."""
    val = os.environ.get("ACT_PACING_SECONDS", "").strip()
    if val:
        try:
            return max(0.0, float(val))
        except ValueError:
            pass
    return DEFAULT_PACING_SECONDS


def load_max_per_tick() -> int:
    """Hard ceiling on executed public actions per tick (ACT_MAX_PER_TICK)."""
    val = os.environ.get("ACT_MAX_PER_TICK", "").strip()
    if val:
        try:
            return max(0, int(val))
        except ValueError:
            pass
    return DEFAULT_MAX_PER_TICK


# The per-class autonomy map (SPEC-v3 AUTONOMY LADDER). AUTO_ACT_CLASSES names
# ACTION types that execute WITHOUT an operator 👍. FOLLOW graduated 2026-07-08;
# like/repost/reply stay gated by default. A class graduates by config, no rebuild.
DEFAULT_AUTO_ACT_CLASSES = {"follow"}


def load_auto_act_classes() -> set:
    """
    Which ACTION types may execute autonomously (no 👍). Default {"follow"} —
    the first graduated class. Values:
      - unset                 -> {"follow"} (the shipped default)
      - "off"/"none"          -> {} (everything gated — full manual)
      - "all"/"*"             -> {"*"} (every action type; use with care)
      - "follow,like"         -> that explicit allowlist
    Still bounded, always: privacy guard + daily caps + pacing + per-tick cap.
    """
    raw = os.environ.get("AUTO_ACT_CLASSES")
    if raw is None:
        return set(DEFAULT_AUTO_ACT_CLASSES)
    raw = raw.strip().lower()
    if raw in ("off", "none", "0", "false"):
        return set()
    if raw in ("all", "*"):
        return {"*"}
    return {c.strip() for c in raw.split(",") if c.strip()}


def load_auto_reply_classes() -> set:
    """
    Finer-grained ladder for conversation reply-backs (AUTO_REPLY_BACK): which
    reply CLASSES may post WITHOUT a 👍. Default OFF (empty). Values:
      - ""/"off"/"none"/"0"/"false"  -> {} (gated; the safe default)
      - "all"/"*"                    -> {"*"} (every converse class auto-posts)
      - "appreciation,question"      -> that explicit allowlist of classes
    """
    raw = os.environ.get("AUTO_REPLY_BACK", "").strip().lower()
    if not raw or raw in ("off", "none", "0", "false"):
        return set()
    if raw in ("all", "*"):
        return {"*"}
    return {c.strip() for c in raw.split(",") if c.strip()}


def auto_eligible(item: Dict[str, Any], auto_act_classes: set, auto_reply_classes: set) -> bool:
    """
    True if this item may execute autonomously (no 👍), per the AUTONOMY LADDER.

      - follow / like / repost  -> eligible iff the action type is in
        AUTO_ACT_CLASSES (default {"follow"}). "*" enables all.
      - reply / quote           -> ONLY conversation reply-backs graduate, and
        only via the finer AUTO_REPLY_BACK class allowlist (default off). A
        scout reply is never autonomous.
    """
    action = str(item.get("action", "")).strip().lower()
    star_acts = "*" in auto_act_classes

    if action in ("like", "repost", "follow"):
        return star_acts or action in auto_act_classes

    if action in ("reply", "quote"):
        if str(item.get("source", "")).lower() != "converse":
            return False  # scout replies never graduate
        if not auto_reply_classes:
            return False
        if "*" in auto_reply_classes:
            return True
        return str(item.get("reply_class", "")).lower() in auto_reply_classes

    return False


def is_self(item: Dict[str, Any], own_did: Optional[str]) -> bool:
    """I-NO-SELF: never engage with our own account's posts (loop-breaker)."""
    if not own_did:
        return False
    return str(item.get("author_did") or "") == str(own_did)


# ---------------------------------------------------------------------------
# Slack read transport (reaction poll)
# ---------------------------------------------------------------------------


def get_reactions(channel: str, ts: str, token: str, timeout: int = 15) -> List[Dict[str, Any]]:
    """
    Fetch the reactions on one message via Slack reactions.get. Returns the
    reactions list (possibly empty). Never raises — a Slack error degrades to
    "no reactions", which means "not approved yet" (fail safe, we just don't act).
    """
    try:
        resp = requests.get(
            SLACK_REACTIONS_GET_EP,
            headers={"Authorization": f"Bearer {token}"},
            params={"channel": channel, "timestamp": ts},
            timeout=timeout,
        )
        data = resp.json() if resp.content else {}
    except (requests.RequestException, ValueError) as exc:
        print(f"[act] reactions.get error (non-fatal): {exc}")
        return []
    if not data.get("ok"):
        print(f"[act] reactions.get rejected for ts={ts}: {data.get('error')}")
        return []
    return (data.get("message") or {}).get("reactions", []) or []


# ---------------------------------------------------------------------------
# Dispatch (per action type; guard fail-closed on reply)
# ---------------------------------------------------------------------------


def _draft_reply(item: Dict[str, Any]) -> str:
    """Draft an in-voice reply to the surfaced post (PERSONA voice, via generate)."""
    pseudo_entry = {
        "raw": str(item.get("text", "")).strip(),
        "type": "moment",
        "angle": item.get("why", ""),
    }
    return generate.generate(pseudo_entry, kind="draft_reply")


def execute_action(
    item: Dict[str, Any],
    session: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Execute one approved action. Returns a result dict {action, status, ...}.

    like / repost -> createRecord with the post's (uri, cid). No model.
    follow        -> createRecord on the author's DID. No model.
    reply / quote -> draft in PERSONA voice, run the fail-closed privacy guard
                     (incl. extended client/project terms), and ONLY post if it
                     passes. A guard block returns status="guard_blocked" and
                     posts NOTHING — dropped, never sanitized-and-hoped (I-PRIVACY).

    For a reply we use the surfaced post as both the reply root and parent. Scout
    surfaces standalone search hits, for which the post IS the thread root; this
    is the correct, network-free default for the human-gated MVP.

    dry_run: compute everything (incl. the draft + guard verdict for replies) but
    make NO Bluesky write. Returns status prefixed "dry_run_".
    """
    action = str(item.get("action", "")).strip().lower()
    uri = item.get("uri")
    cid = item.get("cid")
    author_did = item.get("author_did")

    base = {"action": action, "uri": uri}

    if action in ("like", "repost"):
        if not uri or not cid:
            return {**base, "status": "error", "detail": "missing uri/cid for like/repost"}
        if dry_run:
            return {**base, "status": f"dry_run_{action}"}
        fn = bluesky.like if action == "like" else bluesky.repost
        res = fn(uri, cid, session=session)
        return {**base, "status": "executed", "record_uri": res.get("uri")}

    if action == "follow":
        if not author_did:
            return {**base, "status": "error", "detail": "missing author_did for follow"}
        if dry_run:
            return {**base, "status": "dry_run_follow", "subject_did": author_did}
        res = bluesky.follow(author_did, session=session)
        return {**base, "status": "executed", "record_uri": res.get("uri"), "subject_did": author_did}

    if action in ("reply", "quote"):
        # Thread targets: converse items carry explicit parent/root refs so the
        # reply-back threads correctly under the stranger's reply. Scout items
        # carry none -> fall back to the surfaced post as both root and parent
        # (correct for the standalone hits scout surfaces).
        parent_uri = item.get("parent_uri") or uri
        parent_cid = item.get("parent_cid") or cid
        root_uri = item.get("root_uri") or parent_uri
        root_cid = item.get("root_cid") or parent_cid
        if not parent_uri or not parent_cid:
            return {**base, "status": "error", "detail": "missing uri/cid for reply"}
        # A converse item was drafted + guarded at surface time; use that exact
        # text (re-guarded below as the safety net). Scout items draft fresh now.
        draft = item.get("draft_text") or _draft_reply(item)
        if not draft or not draft.strip():
            return {**base, "status": "empty_draft", "detail": "no draft text"}
        ok, reason = guard.check(draft)
        if not ok:
            # I-PRIVACY: drop it. Never post, never sanitize. Log for review.
            return {**base, "status": "guard_blocked", "reason": reason, "draft": draft}
        if dry_run:
            return {**base, "status": "dry_run_reply", "draft": draft}
        res = bluesky.reply(
            draft,
            root_uri=root_uri, root_cid=root_cid,
            parent_uri=parent_uri, parent_cid=parent_cid,
            session=session,
        )
        return {**base, "status": "executed", "record_uri": res.get("uri"), "posted_text": draft}

    return {**base, "status": "error", "detail": f"unknown action {action!r}"}
