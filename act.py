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
DEFAULT_CAPS = {"like": 20, "repost": 10, "follow": 10, "reply": 5}

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
    operator_id: the operator's Slack user id. If set (recommended), ONLY a
                 thumbsup whose `users` includes that id counts — a reaction
                 from anyone else in the channel is ignored (I-HUMAN-GATE). If
                 None (single-operator channel, id not configured), ANY thumbsup
                 counts.

    Never raises on a malformed reactions blob — a shape it can't read means
    "no approval", which fails safe (nothing gets acted on).
    """
    try:
        for r in reactions or []:
            if _base_emoji(r.get("name")) not in THUMBSUP_NAMES:
                continue
            if operator_id:
                if operator_id in (r.get("users") or []):
                    return True
            else:
                if (r.get("count") or 0) >= 1 or (r.get("users") or []):
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
        if not uri or not cid:
            return {**base, "status": "error", "detail": "missing uri/cid for reply"}
        draft = _draft_reply(item)
        if not draft or not draft.strip():
            return {**base, "status": "empty_draft", "detail": "generation returned empty"}
        ok, reason = guard.check(draft)
        if not ok:
            # I-PRIVACY: drop it. Never post, never sanitize. Log for review.
            return {**base, "status": "guard_blocked", "reason": reason, "draft": draft}
        if dry_run:
            return {**base, "status": "dry_run_reply", "draft": draft}
        res = bluesky.reply(
            draft,
            root_uri=uri, root_cid=cid,
            parent_uri=uri, parent_cid=cid,
            session=session,
        )
        return {**base, "status": "executed", "record_uri": res.get("uri"), "posted_text": draft}

    return {**base, "status": "error", "detail": f"unknown action {action!r}"}
