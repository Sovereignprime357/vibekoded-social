"""
tests/test_bluesky.py — payload SHAPE of the ACT-layer engagement wrappers.

No network: we assert the record shape the builders produce, and (for like/
repost/follow) that the wrapper hands _request the correct collection + repo +
record. bluesky._request is monkeypatched to capture the call — it is never
actually invoked against the network in any test.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bluesky  # noqa: E402


SESSION = {"did": "did:plc:us", "accessJwt": "jwt", "refreshJwt": "r", "handle": "us.bsky.social"}


# --- record builders (pure) -------------------------------------------------

def test_build_like_record_shape():
    rec = bluesky.build_like_record("at://post/1", "cid1")
    assert rec["$type"] == "app.bsky.feed.like"
    assert rec["subject"] == {"uri": "at://post/1", "cid": "cid1"}
    assert rec["createdAt"]  # ISO timestamp present


def test_build_repost_record_shape():
    rec = bluesky.build_repost_record("at://post/2", "cid2")
    assert rec["$type"] == "app.bsky.feed.repost"
    assert rec["subject"] == {"uri": "at://post/2", "cid": "cid2"}
    assert rec["createdAt"]


def test_build_follow_record_shape():
    rec = bluesky.build_follow_record("did:plc:them")
    assert rec["$type"] == "app.bsky.graph.follow"
    # follow's subject is a BARE DID string, not a strong ref — the common bug.
    assert rec["subject"] == "did:plc:them"
    assert rec["createdAt"]


# --- wrappers build the correct createRecord call ---------------------------

def _capture_request(monkeypatch):
    calls = {}

    def fake_request(method, url, headers=None, payload=None, timeout=30):
        calls["method"] = method
        calls["url"] = url
        calls["headers"] = headers
        calls["payload"] = payload
        return {"uri": "at://ourrecord", "cid": "ourcid"}

    monkeypatch.setattr(bluesky, "_request", fake_request)
    return calls


def test_like_posts_to_like_collection(monkeypatch):
    calls = _capture_request(monkeypatch)
    out = bluesky.like("at://post/1", "cid1", session=SESSION)
    assert out["uri"] == "at://ourrecord"
    assert calls["url"] == bluesky.CREATE_RECORD_EP
    assert calls["payload"]["collection"] == "app.bsky.feed.like"
    assert calls["payload"]["repo"] == "did:plc:us"          # OUR did, not the author's
    assert calls["payload"]["record"]["subject"]["cid"] == "cid1"
    assert calls["headers"]["Authorization"] == "Bearer jwt"


def test_repost_posts_to_repost_collection(monkeypatch):
    calls = _capture_request(monkeypatch)
    bluesky.repost("at://post/2", "cid2", session=SESSION)
    assert calls["payload"]["collection"] == "app.bsky.feed.repost"
    assert calls["payload"]["record"]["subject"] == {"uri": "at://post/2", "cid": "cid2"}


def test_follow_posts_to_follow_collection(monkeypatch):
    calls = _capture_request(monkeypatch)
    bluesky.follow("did:plc:them", session=SESSION)
    assert calls["payload"]["collection"] == "app.bsky.graph.follow"
    assert calls["payload"]["record"]["subject"] == "did:plc:them"


# --- bot self-label (I-BOT-DISCLOSED) merge logic (pure) --------------------

def test_has_bot_label():
    assert bluesky.has_bot_label({"labels": {"$type": bluesky.SELF_LABELS_TYPE, "values": [{"val": "bot"}]}}) is True
    assert bluesky.has_bot_label({"labels": {"values": [{"val": "!no-unauthenticated"}]}}) is False
    assert bluesky.has_bot_label({}) is False
    assert bluesky.has_bot_label({"labels": None}) is False


def test_add_bot_label_is_idempotent_and_preserves_fields():
    prof = {"$type": "app.bsky.actor.profile", "displayName": "VibeKoded", "description": "build in public"}
    out = bluesky.add_bot_label(prof)
    assert out["displayName"] == "VibeKoded"           # preserved
    assert out["description"] == "build in public"     # preserved
    assert bluesky.has_bot_label(out) is True
    # Idempotent: adding again doesn't duplicate the value.
    out2 = bluesky.add_bot_label(out)
    vals = out2["labels"]["values"]
    assert [v for v in vals if v.get("val") == "bot"] == [{"val": "bot"}]


def test_add_bot_label_keeps_existing_self_labels():
    prof = {"labels": {"$type": bluesky.SELF_LABELS_TYPE, "values": [{"val": "!no-unauthenticated"}]}}
    out = bluesky.add_bot_label(prof)
    vals = {v["val"] for v in out["labels"]["values"]}
    assert vals == {"!no-unauthenticated", "bot"}      # both kept


def test_ensure_bot_label_already_set_is_noop(monkeypatch):
    monkeypatch.setattr(
        bluesky, "get_profile_record",
        lambda session: {"cid": "c", "value": {"labels": {"values": [{"val": "bot"}]}}},
    )
    # If putRecord were called it would hit the network; it must NOT be.
    monkeypatch.setattr(bluesky, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no put")))
    assert bluesky.ensure_bot_label(SESSION)["status"] == "already_set"


def test_ensure_bot_label_sets_when_missing(monkeypatch):
    monkeypatch.setattr(
        bluesky, "get_profile_record",
        lambda session: {"cid": "oldcid", "value": {"displayName": "VibeKoded"}},
    )
    captured = {}
    def fake_request(method, url, headers=None, payload=None, timeout=30):
        captured["url"] = url
        captured["payload"] = payload
        return {"cid": "newcid"}
    monkeypatch.setattr(bluesky, "_request", fake_request)
    res = bluesky.ensure_bot_label(SESSION)
    assert res["status"] == "set"
    assert captured["url"] == bluesky.PUT_RECORD_EP
    assert captured["payload"]["collection"] == "app.bsky.actor.profile"
    assert captured["payload"]["rkey"] == "self"
    assert captured["payload"]["swapRecord"] == "oldcid"   # optimistic concurrency
    assert bluesky.has_bot_label(captured["payload"]["record"]) is True
    assert captured["payload"]["record"]["displayName"] == "VibeKoded"  # preserved
