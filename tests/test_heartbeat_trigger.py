"""
tests/test_heartbeat_trigger.py — the heartbeat trigger's security surface (SPEC-v5).

Pure, no network: fail-closed shared-secret verification (the thing standing
between a rando and spamming repository_dispatch) + the dispatch payload shape.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heartbeat_trigger as ht  # noqa: E402

SECRET = "hb-shared-secret-xyz"


# --- I-SECURED-TRIGGER: verify_token fail-closed ----------------------------

def test_bare_token_matches():
    assert ht.verify_token(SECRET, SECRET) is True


def test_bearer_token_matches():
    assert ht.verify_token(f"Bearer {SECRET}", SECRET) is True
    assert ht.verify_token(f"bearer {SECRET}", SECRET) is True  # case-insensitive scheme


def test_wrong_token_rejected():
    assert ht.verify_token("Bearer not-the-secret", SECRET) is False
    assert ht.verify_token("totally-wrong", SECRET) is False


def test_missing_pieces_fail_closed():
    assert ht.verify_token(None, SECRET) is False          # no header
    assert ht.verify_token("", SECRET) is False            # empty header
    assert ht.verify_token(SECRET, None) is False          # secret not configured
    assert ht.verify_token(SECRET, "") is False            # secret empty
    assert ht.verify_token("Bearer ", SECRET) is False     # bearer with empty token


def test_no_secret_configured_never_authorizes():
    # If HEARTBEAT_SECRET is unset, NOTHING authorizes (fail-closed).
    assert ht.verify_token("Bearer anything", "") is False
    assert ht.verify_token("anything", None) is False


# --- dispatch payload -------------------------------------------------------

def test_build_dispatch_payload_shape():
    p = ht.build_dispatch_payload()
    assert p["event_type"] == "heartbeat"
    assert p["client_payload"]["source"] == "heartbeat"
    # trace-only: no secrets / no authority in the payload (I-TRIGGER-NOT-ACTION)
    assert set(p["client_payload"].keys()) == {"source"}
