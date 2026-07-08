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
