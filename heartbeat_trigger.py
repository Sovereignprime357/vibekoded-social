"""
heartbeat_trigger.py — pure, testable logic for the reliable-heartbeat trigger (SPEC-v5).

An external scheduler (cron-job.org) pings the Vercel endpoint on a dependable
cadence; the endpoint verifies a shared secret and fires a GitHub repository_dispatch
(event_type "heartbeat") that wakes scout-tick + notify-tick. This module holds the
security decision (token verify, fail-closed) and the dispatch payload — no network,
no third-party deps, so it is unit-tested and safe to vendor into the function.

I-SECURED-TRIGGER: fail-closed token check. I-TRIGGER-NOT-ACTION: the richest thing
here is a repository_dispatch payload — no action logic anywhere in this file.
"""

from __future__ import annotations

import hmac
from typing import Any, Dict, Optional

DISPATCH_EVENT_TYPE = "heartbeat"


def verify_token(provided: Optional[str], expected: Optional[str]) -> bool:
    """
    Verify the heartbeat shared secret, FAIL-CLOSED (I-SECURED-TRIGGER).

    `provided` is the request's Authorization header value; both a bare token and
    a "Bearer <token>" form are accepted. Returns True only if both the expected
    secret and a provided token are present and constant-time equal. Any missing
    input → False. Never raises.
    """
    if not expected or not provided:
        return False
    # Normalize BOTH sides' surrounding whitespace. The provided token was already
    # stripped; strip the stored secret too so a trailing newline / stray space in
    # the env value (classic copy-paste artifact) can't cause a spurious 401. This
    # does NOT weaken the check: whitespace isn't intended secret entropy, both
    # sides are normalized identically, and the compare stays constant-time.
    expected = expected.strip()
    token = provided.strip()
    if token[:7].lower() == "bearer ":
        token = token[7:].strip()
    if not expected or not token:
        return False
    try:
        return hmac.compare_digest(token, expected)
    except Exception:  # noqa: BLE001 — any comparison error is a fail-closed no
        return False


def build_dispatch_payload() -> Dict[str, Any]:
    """
    The GitHub repository_dispatch body. client_payload is trace-only (no secrets,
    no authority — I-TRIGGER-NOT-ACTION): scout/notify read NONE of it to decide
    what to do; they just wake and re-derive everything themselves.
    """
    return {
        "event_type": DISPATCH_EVENT_TYPE,
        "client_payload": {"source": "heartbeat"},
    }
