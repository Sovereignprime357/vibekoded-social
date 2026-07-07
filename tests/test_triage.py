"""
tests/test_triage.py — the TRIAGE step's parsing + stub logic, no network.

parse_verdict() is the load-bearing bit (models return messy text), so it
gets the most cases. classify_* are exercised through the DRY_RUN stub path,
which needs no API key.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
import triage  # noqa: E402


# --- parse_verdict ---------------------------------------------------------

def test_parse_clean_json():
    v = triage.parse_verdict('{"on_mission": true, "lane": "memory", "action": "reply", "why": "wheelhouse", "confidence": "high"}')
    assert v["on_mission"] is True
    assert v["action"] == "reply"
    assert v["lane"] == "memory"
    assert v["confidence"] == "high"


def test_parse_json_in_code_fence():
    raw = "```json\n{\"on_mission\": true, \"action\": \"like\", \"confidence\": \"med\"}\n```"
    v = triage.parse_verdict(raw)
    assert v["action"] == "like"
    assert v["confidence"] == "med"


def test_parse_json_with_surrounding_prose():
    raw = 'Sure, here is my verdict: {"on_mission": false, "action": "none", "why": "spam"} hope that helps!'
    v = triage.parse_verdict(raw)
    assert v["on_mission"] is False
    assert v["action"] == "none"


def test_not_on_mission_forces_action_none():
    v = triage.parse_verdict('{"on_mission": false, "action": "reply", "confidence": "high"}')
    assert v["action"] == "none"


def test_invalid_action_coerced_to_none():
    v = triage.parse_verdict('{"on_mission": true, "action": "smash", "confidence": "high"}')
    assert v["action"] == "none"


def test_invalid_confidence_coerced_to_low():
    v = triage.parse_verdict('{"on_mission": true, "action": "like", "confidence": "extreme"}')
    assert v["confidence"] == "low"


def test_on_mission_string_true_is_boolean():
    v = triage.parse_verdict('{"on_mission": "true", "action": "like", "confidence": "low"}')
    assert v["on_mission"] is True


def test_parse_garbage_returns_none():
    assert triage.parse_verdict("no json here at all") is None
    assert triage.parse_verdict("") is None
    assert triage.parse_verdict(None) is None


def test_why_is_length_capped():
    long_why = "x" * 500
    v = triage.parse_verdict('{"on_mission": true, "action": "like", "why": "' + long_why + '", "confidence": "low"}')
    assert len(v["why"]) <= 200


# --- stub verdict ----------------------------------------------------------

def _cand(uri, text, lane_id="memory"):
    return {
        "uri": uri, "cid": "c", "author_handle": "u.bsky.social", "author_did": "did:plc:u",
        "text": text, "lane_id": lane_id, "lane_label": "Agent memory",
        "url": "https://bsky.app/profile/u.bsky.social/post/1",
    }


def test_stub_surfaces_first_two_and_reply_on_question():
    q = triage._stub_verdict(_cand("at://1", "how do you handle memory?"), index=0)
    assert q["on_mission"] is True
    assert q["action"] == "reply"  # has a question mark

    plain = triage._stub_verdict(_cand("at://2", "shipped a thing today"), index=1)
    assert plain["on_mission"] is True
    assert plain["action"] == "like"

    dropped = triage._stub_verdict(_cand("at://3", "another post"), index=2)
    assert dropped["on_mission"] is False
    assert dropped["action"] == "none"


def test_stub_is_deterministic():
    a = triage._stub_verdict(_cand("at://1", "same text?"), index=0)
    b = triage._stub_verdict(_cand("at://1", "same text?"), index=0)
    assert a == b


# --- classify_* via DRY_RUN stub (no key needed) ---------------------------

def test_classify_one_uses_stub_in_dry_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    result = triage.classify_one(_cand("at://1", "how to persist context?"), rubric="(test rubric)", index=0)
    assert result["on_mission"] is True
    assert result["action"] == "reply"
    assert result["uri"] == "at://1"  # candidate fields preserved


def test_classify_all_filters_to_surfaceable(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    cands = [
        _cand("at://1", "question one?"),
        _cand("at://2", "statement two"),
        _cand("at://3", "third one drops"),
    ]
    surfaced = triage.classify_all(cands, rubric="(test rubric)")
    # stub surfaces index 0 and 1, drops index 2
    assert len(surfaced) == 2
    assert {s["uri"] for s in surfaced} == {"at://1", "at://2"}


# --- parse_batch_verdicts (the defensive batch parser) ---------------------

def test_parse_batch_clean_array():
    raw = json.dumps([
        {"index": 0, "on_mission": True, "action": "like", "confidence": "high"},
        {"index": 1, "on_mission": False, "action": "none", "confidence": "low"},
    ])
    out = triage.parse_batch_verdicts(raw, 2)
    assert out[0]["action"] == "like"
    assert out[1]["on_mission"] is False


def test_parse_batch_one_malformed_item_only_that_slot_none():
    # A non-dict element in the middle must null ONLY its slot, never the batch.
    raw = ('[{"index":0,"on_mission":true,"action":"like","confidence":"high"}, '
           '"BROKEN", '
           '{"index":2,"on_mission":true,"action":"reply","confidence":"med"}]')
    out = triage.parse_batch_verdicts(raw, 3)
    assert out[0] is not None and out[0]["action"] == "like"
    assert out[1] is None
    assert out[2] is not None and out[2]["action"] == "reply"


def test_parse_batch_short_array_pads_none():
    raw = json.dumps([{"index": 0, "on_mission": True, "action": "like", "confidence": "low"}])
    out = triage.parse_batch_verdicts(raw, 3)
    assert out[0] is not None
    assert out[1] is None and out[2] is None


def test_parse_batch_garbage_all_none():
    assert triage.parse_batch_verdicts("not json at all", 2) == [None, None]
    assert triage.parse_batch_verdicts("", 2) == [None, None]
    # a JSON object (not array) is not a batch -> all None
    assert triage.parse_batch_verdicts('{"index":0}', 2) == [None, None]


def test_parse_batch_tolerates_code_fence():
    raw = "```json\n[{\"index\":0,\"on_mission\":true,\"action\":\"like\",\"confidence\":\"low\"}]\n```"
    out = triage.parse_batch_verdicts(raw, 1)
    assert out[0]["action"] == "like"


def test_parse_batch_maps_by_explicit_index_out_of_order():
    raw = json.dumps([
        {"index": 1, "on_mission": True, "action": "reply", "confidence": "high"},
        {"index": 0, "on_mission": True, "action": "like", "confidence": "low"},
    ])
    out = triage.parse_batch_verdicts(raw, 2)
    assert out[0]["action"] == "like"
    assert out[1]["action"] == "reply"


# --- classify_all live batch path (monkeypatched model, no network) --------

def _live_env(monkeypatch):
    """Force the live batch path: not dry-run, key present, no real sleeping."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(triage, "TRIAGE_BATCH_PAUSE_S", 0)
    monkeypatch.setattr(time, "sleep", lambda s: None)


def test_classify_all_live_batch_surfaces_on_mission(monkeypatch):
    _live_env(monkeypatch)
    cands = [_cand("at://1", "q one?"), _cand("at://2", "two"), _cand("at://3", "three")]
    arr = json.dumps([
        {"index": 0, "on_mission": True, "action": "reply", "why": "real", "confidence": "high"},
        {"index": 1, "on_mission": False, "action": "none", "why": "off", "confidence": "low"},
        {"index": 2, "on_mission": True, "action": "like", "why": "nod", "confidence": "med"},
    ])
    monkeypatch.setattr(generate, "complete", lambda *a, **k: arr)
    surfaced = triage.classify_all(cands, rubric="(r)")
    assert {s["uri"] for s in surfaced} == {"at://1", "at://3"}


def test_classify_all_live_api_failure_surfaces_nothing(monkeypatch):
    _live_env(monkeypatch)
    cands = [_cand("at://1", "q one?"), _cand("at://2", "two")]

    def boom(*a, **k):
        raise generate.RateLimitError("HTTP 429 from groq: rate_limit_exceeded", retry_after=1.0)

    monkeypatch.setattr(generate, "complete", boom)
    # Must not raise, and must surface nothing rather than stub garbage.
    surfaced = triage.classify_all(cands, rubric="(r)")
    assert surfaced == []


def test_classify_all_live_one_bad_item_skipped_rest_surface(monkeypatch):
    _live_env(monkeypatch)
    cands = [_cand("at://1", "one"), _cand("at://2", "two"), _cand("at://3", "three")]
    raw = ('[{"index":0,"on_mission":true,"action":"like","confidence":"high"}, '
           '"BROKEN", '
           '{"index":2,"on_mission":true,"action":"like","confidence":"med"}]')
    monkeypatch.setattr(generate, "complete", lambda *a, **k: raw)
    surfaced = triage.classify_all(cands, rubric="(r)")
    # at://2's verdict was malformed -> skipped; the other two surface.
    assert {s["uri"] for s in surfaced} == {"at://1", "at://3"}
