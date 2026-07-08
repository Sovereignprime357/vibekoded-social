"""
tests/test_converse.py — the conversation-continuation loop (SPEC-v3).

No network, no model: triage falls back to its deterministic stub (no key),
generate/guard are real or monkeypatched, and surface is injected so we capture
the surfaced item instead of hitting Slack. handled.jsonl is redirected to tmp.

Covers the loop-breakers that matter most: I-NO-SELF (never respond to our own
account), worth-it gating, guard fail-closed on the draft, thread-depth cap, and
dedup-via-handled — plus that a worth-it clean reply is surfaced (not posted).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import converse  # noqa: E402


def _notif(uri="at://did:plc:them/app.bsky.feed.post/r1", did="did:plc:them",
           text="how do you keep the agent on-mission?", reason="reply", root=None):
    record = {"text": text}
    if root:
        record["reply"] = {"root": {"uri": root[0], "cid": root[1]},
                           "parent": {"uri": uri, "cid": "pcid"}}
    return {
        "uri": uri, "cid": "rcid", "reason": reason,
        "author": {"did": did, "handle": "them.bsky.social"},
        "record": record,
    }


def _no_model(monkeypatch):
    """Force triage's deterministic stub + generate's stub (no keys, not dry)."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


# --- I-NO-SELF (infinite-loop guard) ----------------------------------------

def test_is_own_reply_true_for_our_did():
    assert converse.is_own_reply(_notif(did="did:plc:us"), "did:plc:us") is True
    assert converse.is_own_reply(_notif(did="did:plc:them"), "did:plc:us") is False


def test_is_own_reply_unknown_author_fails_safe():
    n = {"author": {}, "record": {"text": "hi"}}
    assert converse.is_own_reply(n, "did:plc:us") is True  # unknown -> treat as self, skip


# --- triage parse + stub ----------------------------------------------------

def test_parse_triage_normalizes_skip():
    v = converse.parse_triage('{"worth_responding": true, "class": "skip", "why": "x"}')
    assert v["worth_responding"] is False  # skip can never be "worth"
    assert v["class"] == "skip"


def test_parse_triage_worth_without_class_becomes_skip():
    v = converse.parse_triage('{"worth_responding": false, "class": "question", "why": "x"}')
    assert v["class"] == "skip"


def test_parse_triage_garbage_returns_none():
    assert converse.parse_triage("not json") is None


def test_stub_triage_question_is_worth(monkeypatch):
    _no_model(monkeypatch)
    v = converse.triage_incoming("what stack are you on?")
    assert v["worth_responding"] is True and v["class"] == "question"


def test_stub_triage_statement_skips(monkeypatch):
    _no_model(monkeypatch)
    v = converse.triage_incoming("cool project")
    assert v["worth_responding"] is False


# --- thread refs ------------------------------------------------------------

def test_extract_refs_uses_thread_root():
    n = _notif(root=("at://root/1", "rootcid"))
    refs = converse.extract_refs(n)
    assert refs["root_uri"] == "at://root/1"
    assert refs["root_cid"] == "rootcid"
    assert refs["parent_uri"] == n["uri"]


def test_extract_refs_falls_back_to_parent_when_no_root():
    n = _notif()  # no reply.root -> root == parent (a mention on our post)
    refs = converse.extract_refs(n)
    assert refs["root_uri"] == n["uri"]
    assert refs["parent_uri"] == n["uri"]


# --- thread-depth ------------------------------------------------------------

def test_thread_depth_counts_surfaced_for_root(tmp_path):
    p = tmp_path / "handled.jsonl"
    p.write_text(
        json.dumps({"converse_action": "surfaced", "converse_root": "at://root/1"}) + "\n" +
        json.dumps({"converse_action": "surfaced", "converse_root": "at://root/1"}) + "\n" +
        json.dumps({"converse_action": "skipped_skip", "converse_root": "at://root/1"}) + "\n" +
        json.dumps({"converse_action": "surfaced", "converse_root": "at://other"}) + "\n",
        encoding="utf-8",
    )
    assert converse.thread_depth("at://root/1", path=str(p)) == 2
    assert converse.thread_depth("at://other", path=str(p)) == 1
    assert converse.thread_depth(None, path=str(p)) == 0


# --- handle_incoming_reply orchestration ------------------------------------

def _capture_surface():
    captured = []
    def fn(items):
        captured.extend(items)
        return len(items)
    return captured, fn


def test_handle_skips_our_own_account(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    captured, fn = _capture_surface()
    status = converse.handle_incoming_reply(
        _notif(did="did:plc:us"), our_did="did:plc:us",
        surface_fn=fn, handled_path=str(tmp_path / "h.jsonl"),
    )
    assert status == "skipped_self"
    assert captured == []  # never surfaced


def test_handle_skips_not_worth(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    captured, fn = _capture_surface()
    status = converse.handle_incoming_reply(
        _notif(text="nice one"), our_did="did:plc:us",  # no '?' -> stub skips
        surface_fn=fn, handled_path=str(tmp_path / "h.jsonl"),
    )
    assert status == "skipped_not_worth"
    assert captured == []


def test_handle_worth_it_clean_is_surfaced(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    captured, fn = _capture_surface()
    status = converse.handle_incoming_reply(
        _notif(text="what memory setup do you run?", root=("at://root/1", "rc")),
        our_did="did:plc:us", surface_fn=fn, handled_path=str(tmp_path / "h.jsonl"),
    )
    assert status == "surfaced"
    assert len(captured) == 1
    item = captured[0]
    assert item["source"] == "converse"
    assert item["action"] == "reply"
    assert item["draft_text"]                    # a pre-drafted reply-back
    assert item["root_uri"] == "at://root/1"     # threads correctly
    assert item["author_did"] == "did:plc:them"


def test_handle_guard_blocks_draft_and_does_not_surface(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    captured, fn = _capture_surface()
    # Force a draft that leaks a hardcoded-floor term (real guard, not mocked).
    monkeypatch.setattr(converse.generate, "generate", lambda *a, **k: "sure — my wife loved it")
    status = converse.handle_incoming_reply(
        _notif(text="is this real?"), our_did="did:plc:us",
        surface_fn=fn, handled_path=str(tmp_path / "h.jsonl"),
    )
    assert status == "guard_blocked"
    assert captured == []  # a leaking draft is NEVER surfaced


def test_handle_thread_depth_exceeded(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    monkeypatch.setenv("CONVERSE_MAX_THREAD_DEPTH", "1")
    hp = tmp_path / "h.jsonl"
    # Pre-seed one surfaced reply-back for this root -> depth is already at cap.
    hp.write_text(json.dumps({"converse_action": "surfaced", "converse_root": "at://root/9"}) + "\n", encoding="utf-8")
    captured, fn = _capture_surface()
    status = converse.handle_incoming_reply(
        _notif(text="another question?", root=("at://root/9", "rc")),
        our_did="did:plc:us", surface_fn=fn, handled_path=str(hp),
    )
    assert status == "thread_depth_exceeded"
    assert captured == []


def test_handle_marks_handled(tmp_path, monkeypatch):
    _no_model(monkeypatch)
    captured, fn = _capture_surface()
    hp = tmp_path / "h.jsonl"
    converse.handle_incoming_reply(
        _notif(text="what stack?"), our_did="did:plc:us", surface_fn=fn, handled_path=str(hp),
    )
    rows = [json.loads(l) for l in open(hp, encoding="utf-8") if l.strip()]
    assert rows and rows[-1]["converse_action"] == "surfaced"
    assert rows[-1]["notification_uri"]  # dedup key recorded (once-per-incoming-reply)
