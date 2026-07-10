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

# The actionable channels a 👍 may fire from (SPEC-v3.1 multi-channel routing).
# Baked as DEFAULTS so no new operator env var is required — the Vercel setup
# stays the same. These are the 4 routed channels (likes/replies/reposts/converse).
# SLACK_CHANNEL_ID (the misc/activity home) is added at load time when present,
# and the whole set is overridable via SLACK_TRIGGER_CHANNELS (comma-separated).
DEFAULT_TRIGGER_CHANNELS = (
    "C0BFZGBNFEH",  # likes
    "C0BG4NSKSQZ",  # replies
    "C0BGX5UHJ0G",  # reposts
    "C0BG2RQ7SF4",  # converse
)


def load_trigger_channels(trigger_channels_csv: Optional[str] = None, base_channel: Optional[str] = None) -> set:
    """
    Resolve the set of channels a 👍 may fire from.

      - SLACK_TRIGGER_CHANNELS set (comma-separated) -> exactly that set (override).
      - else -> the 4 baked routed channels + SLACK_CHANNEL_ID (if present).

    No new operator var is needed for the default case; SLACK_CHANNEL_ID is now
    OPTIONAL for the webhook (the routed channels are covered by the defaults).
    """
    if trigger_channels_csv and trigger_channels_csv.strip():
        return {c.strip() for c in trigger_channels_csv.split(",") if c.strip()}
    chans = set(DEFAULT_TRIGGER_CHANNELS)
    if base_channel and base_channel.strip():
        chans.add(base_channel.strip())
    return chans


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
        # Strip the stored signing secret: a trailing newline / stray space in the
        # env value (copy-paste artifact) would otherwise change the HMAC and 401
        # every request. Whitespace isn't secret entropy; the HMAC stays keyed on
        # the real secret and the final compare is still constant-time.
        signing_secret = signing_secret.strip() if signing_secret else signing_secret
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


def decide(body: Dict[str, Any], operator_id: Optional[str], target_channels: Any) -> Dict[str, Any]:
    """
    Decide what to do with a (already signature-verified) Slack payload.

    Returns one of:
      {"action": "challenge", "challenge": "<value>"}   -> echo it (setup)
      {"action": "dispatch",  "reason": "..."}          -> fire repository_dispatch
      {"action": "ignore",    "reason": "..."}          -> 200 ack, do nothing

    `target_channels` is the SET of actionable channels a 👍 may fire from
    (SPEC-v3.1 routing: the 4 routed channels + optionally SLACK_CHANNEL_ID). A
    single string is tolerated for back-compat.

    FAIL-CLOSED (I-OPERATOR-SCOPED): if operator_id is unset, or target_channels
    is empty, or any field doesn't match, the result is "ignore" — never "dispatch".
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

    channels = {target_channels} if isinstance(target_channels, str) else set(target_channels or [])

    # Fail-closed on missing config: without an operator id / any channel we can't
    # attribute the reaction, so we never dispatch.
    if not operator_id or not channels:
        return {"action": "ignore", "reason": "operator id or channels not configured"}

    if _base_emoji(event.get("reaction")) not in THUMBSUP_NAMES:
        return {"action": "ignore", "reason": f"not a thumbsup ({event.get('reaction')!r})"}
    if event.get("user") != operator_id:
        return {"action": "ignore", "reason": "reactor is not the operator"}

    channel = (event.get("item") or {}).get("channel")
    if channel not in channels:
        return {"action": "ignore", "reason": "reaction not in an actionable channel"}

    return {"action": "dispatch", "reason": "operator thumbsup in an actionable channel"}


def build_dispatch_payload(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    The GitHub repository_dispatch body. client_payload carries the reacted
    message's ts+channel as a TARGETING HINT (no secrets, no action authority —
    I-TRIGGER-NOT-ACTION): act_tick uses it to look up the ONE item, then STILL
    re-verifies the operator's 👍 via reactions.get before executing. A spoofed
    ts thus wastes a lookup, never acts.
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
