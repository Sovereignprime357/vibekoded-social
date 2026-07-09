"""
tests/test_slack_trigger.py — the event-trigger security surface (SPEC-v4).

Pure, no network: signature verification (pass/fail/stale/missing), the
challenge echo, the operator/emoji/channel filter (fail-closed), and the
repository_dispatch payload shape.
"""

import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slack_trigger  # noqa: E402

SECRET = "s3cr3t-signing"
NOW = 1_783_512_000.0
OP = "U_OPERATOR"
CH = "C_TARGET"


def _sign(secret, ts, body):
    base = f"v0:{ts}:{body}".encode("utf-8")
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


# --- I-SIG-VERIFY -----------------------------------------------------------

def test_signature_valid_passes():
    ts = str(int(NOW))
    body = '{"type":"event_callback"}'
    sig = _sign(SECRET, ts, body)
    assert slack_trigger.verify_signature(SECRET, ts, body, sig, now=NOW) is True


def test_signature_wrong_secret_fails():
    ts = str(int(NOW))
    body = '{"x":1}'
    sig = _sign("other-secret", ts, body)
    assert slack_trigger.verify_signature(SECRET, ts, body, sig, now=NOW) is False


def test_signature_tampered_body_fails():
    ts = str(int(NOW))
    sig = _sign(SECRET, ts, '{"x":1}')
    assert slack_trigger.verify_signature(SECRET, ts, '{"x":2}', sig, now=NOW) is False


def test_signing_secret_whitespace_is_tolerated():
    # A trailing newline / space in the stored SLACK_SIGNING_SECRET (copy-paste)
    # must still verify: the signature was computed with the clean secret, and the
    # code strips the stored secret before keying the HMAC.
    ts = str(int(NOW))
    body = '{"type":"event_callback"}'
    sig = _sign(SECRET, ts, body)  # signed with the clean secret (as Slack does)
    assert slack_trigger.verify_signature(f"{SECRET}\n", ts, body, sig, now=NOW) is True
    assert slack_trigger.verify_signature(f"  {SECRET}  ", ts, body, sig, now=NOW) is True


def test_signature_stale_timestamp_fails():
    ts = str(int(NOW) - 10 * 60)  # 10 min old -> outside the 5-min window (replay)
    body = '{"x":1}'
    sig = _sign(SECRET, ts, body)
    assert slack_trigger.verify_signature(SECRET, ts, body, sig, now=NOW) is False


def test_signature_future_timestamp_fails():
    ts = str(int(NOW) + 10 * 60)
    body = '{"x":1}'
    sig = _sign(SECRET, ts, body)
    assert slack_trigger.verify_signature(SECRET, ts, body, sig, now=NOW) is False


def test_signature_missing_pieces_fail():
    ts = str(int(NOW))
    body = '{"x":1}'
    sig = _sign(SECRET, ts, body)
    assert slack_trigger.verify_signature("", ts, body, sig, now=NOW) is False        # no secret
    assert slack_trigger.verify_signature(SECRET, None, body, sig, now=NOW) is False   # no ts
    assert slack_trigger.verify_signature(SECRET, ts, body, None, now=NOW) is False    # no sig
    assert slack_trigger.verify_signature(SECRET, "notanumber", body, sig, now=NOW) is False


# --- decide: challenge ------------------------------------------------------

def test_challenge_is_echoed():
    d = slack_trigger.decide({"type": "url_verification", "challenge": "abc123"}, OP, CH)
    assert d == {"action": "challenge", "challenge": "abc123"}


# --- decide: operator/emoji/channel filter (fail-closed) --------------------

def _reaction(user=OP, reaction="+1", channel=CH):
    return {
        "type": "event_callback",
        "event": {"type": "reaction_added", "user": user, "reaction": reaction,
                  "item": {"type": "message", "channel": channel, "ts": "1700.1"}},
    }


def test_operator_thumbsup_in_channel_dispatches():
    assert slack_trigger.decide(_reaction(), OP, CH)["action"] == "dispatch"


def test_skin_tone_thumbsup_dispatches():
    assert slack_trigger.decide(_reaction(reaction="+1::skin-tone-4"), OP, CH)["action"] == "dispatch"
    assert slack_trigger.decide(_reaction(reaction="thumbsup"), OP, CH)["action"] == "dispatch"


def test_non_operator_is_ignored():
    assert slack_trigger.decide(_reaction(user="U_RANDO"), OP, CH)["action"] == "ignore"


def test_wrong_emoji_is_ignored():
    assert slack_trigger.decide(_reaction(reaction="eyes"), OP, CH)["action"] == "ignore"


def test_wrong_channel_is_ignored():
    assert slack_trigger.decide(_reaction(channel="C_OTHER"), OP, CH)["action"] == "ignore"


def test_non_reaction_event_ignored():
    body = {"type": "event_callback", "event": {"type": "message", "user": OP}}
    assert slack_trigger.decide(body, OP, CH)["action"] == "ignore"


def test_reaction_removed_ignored():
    body = _reaction()
    body["event"]["type"] = "reaction_removed"
    assert slack_trigger.decide(body, OP, CH)["action"] == "ignore"


def test_unconfigured_operator_or_channel_fails_closed():
    # Missing operator id or an empty channel set -> never dispatch.
    assert slack_trigger.decide(_reaction(), "", {CH})["action"] == "ignore"
    assert slack_trigger.decide(_reaction(), OP, set())["action"] == "ignore"


def test_unknown_top_level_type_ignored():
    assert slack_trigger.decide({"type": "something_else"}, OP, CH)["action"] == "ignore"


# --- multi-channel trigger (SPEC-v3.1): 👍 fires from ANY actionable channel ---

def test_load_trigger_channels_defaults_are_the_four_routed():
    chans = slack_trigger.load_trigger_channels(None, "")
    assert chans == set(slack_trigger.DEFAULT_TRIGGER_CHANNELS)
    assert chans == {"C0BFZGBNFEH", "C0BG4NSKSQZ", "C0BGX5UHJ0G", "C0BG2RQ7SF4"}


def test_load_trigger_channels_adds_base_when_present():
    chans = slack_trigger.load_trigger_channels(None, "C_BASE")
    assert "C_BASE" in chans
    assert set(slack_trigger.DEFAULT_TRIGGER_CHANNELS).issubset(chans)


def test_load_trigger_channels_override_wins():
    chans = slack_trigger.load_trigger_channels("A1, B2 , C3", "C_BASE")
    assert chans == {"A1", "B2", "C3"}  # explicit override replaces the whole set


def test_thumbsup_in_each_routed_channel_dispatches():
    chans = slack_trigger.load_trigger_channels(None, "")  # the 4 baked defaults, no base
    for cid in slack_trigger.DEFAULT_TRIGGER_CHANNELS:
        d = slack_trigger.decide(_reaction(channel=cid), OP, chans)
        assert d["action"] == "dispatch", f"👍 in {cid} should fire"


def test_thumbsup_in_non_actionable_channel_ignored():
    chans = slack_trigger.load_trigger_channels(None, "")
    assert slack_trigger.decide(_reaction(channel="C_NOT_ROUTED"), OP, chans)["action"] == "ignore"


def test_non_operator_ignored_even_in_routed_channel():
    chans = slack_trigger.load_trigger_channels(None, "")
    d = slack_trigger.decide(_reaction(user="U_RANDO", channel="C0BFZGBNFEH"), OP, chans)
    assert d["action"] == "ignore"


def test_non_thumbsup_ignored_even_in_routed_channel():
    chans = slack_trigger.load_trigger_channels(None, "")
    d = slack_trigger.decide(_reaction(reaction="eyes", channel="C0BG4NSKSQZ"), OP, chans)
    assert d["action"] == "ignore"


# --- dispatch payload -------------------------------------------------------

def test_build_dispatch_payload_shape():
    p = slack_trigger.build_dispatch_payload(_reaction())
    assert p["event_type"] == "slack-thumbsup"
    assert p["client_payload"]["source"] == "slack-reaction"
    assert p["client_payload"]["reacted_channel"] == CH
    assert p["client_payload"]["reacted_ts"] == "1700.1"


def test_build_dispatch_payload_no_body_is_safe():
    p = slack_trigger.build_dispatch_payload()
    assert p["event_type"] == "slack-thumbsup"
    assert "client_payload" in p
