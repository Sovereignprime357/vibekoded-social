"""
slack_trigger.py — pure, testable logic for the Slack→GitHub event trigger (SPEC-v4).

This module has NO third-party dependencies and does NO network I/O, so it is
unit-testable and safe to vendor into the serverless (Vercel) function. The thin
HTTP adapter lives in api/slack_events.py; ALL the security-relevant decisions
(signature verification, operator/emoji/channel filtering, dispatch payload) live
here so they are covered by the repo's pytest suite.

Design invariants (see SPEC-v4):
  - I-SIG-VERIFY: fail-closed HMAC verification + ±window timestamp (replay guard).
  - I-OPERATOR-SCOPED: dispatch only on the operator's 👍 in the target channel.
  - I-TRIGGER-NOT-ACTION: the richest thing produced here is a repository_dispatch
    payload — there is no action logic anywhere in this file.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, Optional

# Slack reaction names that count as a thumbsup (skin-tone variants tolerated).
THUMBSUP_NAMES = {"+1", "thumbsup"}

# Timestamp freshness window (seconds). Slack recommends 5 minutes; anything
# outside is treated as a replay / clock-skew and rejected.
DEFAULT_WINDOW_S = 300

DISPATCH_EVENT_TYPE = "slack-thumbsup"


def _base_emoji(name: str) -> str:
    """Strip Slack's '::skin-tone-N' suffix so all thumbsup tones match."""
    return str(name or "").split("::", 1)[0]


def verify_signature(
    signing_secret: str,
    timestamp: Optional[str],
    raw_body: str,
    slack_signature: Optional[str],
    now: Optional[float] = None,
    window: int = DEFAULT_WINDOW_S,
) -> bool:
    """
    Verify a Slack request signature (v0 scheme), FAIL-CLOSED (I-SIG-VERIFY).

    Returns True only if ALL hold:
      - signing_secret, timestamp, and slack_signature are present,
      - the timestamp is within ±`window` seconds of `now` (replay guard),
      - hmac_sha256(secret, "v0:{timestamp}:{raw_body}") == slack_signature,
        constant-time compared.
    Any missing input, unparadseable timestamp, stale window, or mismatch → False.
    Never raises — an exception path returns False (still fail-closed).
    """
    try:
        if not signing_secret or not timestamp or slack_signature is None:
            return False
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False

    now = time.time() if now is None else now
    if abs(now - ts) > window:
        return False  # stale / future-dated → replay guard trips

    try:
        basestring = f"v0:{timestamp}:{raw_body}".encode("utf-8")
        digest = hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
        expected = f"v0={digest}"
        return hmac.compare_digest(expected, slack_signature)
    except Exception:  # noqa: BLE001 — any crypto/encoding error is a fail-closed no
        return False


def decide(body: Dict[str, Any], operator_id: Optional[str], target_channel: Optional[str]) -> Dict[str, Any]:
    """
    Decide what to do with a (already signature-verified) Slack payload.

    Returns one of:
      {"action": "challenge", "challenge": "<value>"}   -> echo it (setup)
      {"action": "dispatch",  "reason": "..."}          -> fire repository_dispatch
      {"action": "ignore",    "reason": "..."}          -> 200 ack, do nothing

    FAIL-CLOSED (I-OPERATOR-SCOPED): if operator_id or target_channel is unset, or
    any field doesn't match exactly, the result is "ignore" — never "dispatch".
    """
    if not isinstance(body, dict):
        return {"action": "ignore", "reason": "body not an object"}

    btype = body.get("type")
    if btype == "url_verification":
        return {"action": "challenge", "challenge": str(body.get("challenge", ""))}

    if btype != "event_callback":
        return {"action": "ignore", "reason": f"unhandled top-level type {btype!r}"}

    event = body.get("event") or {}
    if event.get("type") != "reaction_added":
        return {"action": "ignore", "reason": f"not a reaction_added ({event.get('type')!r})"}

    # Fail-closed on missing config: without an operator id / channel we can't
    # attribute the reaction, so we never dispatch.
    if not operator_id or not target_channel:
        return {"action": "ignore", "reason": "operator id or channel not configured"}

    if _base_emoji(event.get("reaction")) not in THUMBSUP_NAMES:
        return {"action": "ignore", "reason": f"not a thumbsup ({event.get('reaction')!r})"}
    if event.get("user") != operator_id:
        return {"action": "ignore", "reason": "reactor is not the operator"}

    channel = (event.get("item") or {}).get("channel")
    if channel != target_channel:
        return {"action": "ignore", "reason": "reaction not in the target channel"}

    return {"action": "dispatch", "reason": "operator thumbsup in target channel"}


def build_dispatch_payload(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    The GitHub repository_dispatch body. client_payload is trace-only metadata
    (no secrets, no action authority — I-TRIGGER-NOT-ACTION). scout-act reads
    NONE of it to decide what to do; act_tick re-derives everything from Slack.
    """
    event = (body or {}).get("event") or {}
    item = event.get("item") or {}
    return {
        "event_type": DISPATCH_EVENT_TYPE,
        "client_payload": {
            "source": "slack-reaction",
            "reacted_channel": item.get("channel"),
            "reacted_ts": item.get("ts"),
        },
    }
