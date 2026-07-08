"""
tests/test_surface.py — the SURFACE step's formatting + ledger + dry-run.

No network: dry-run prints, and the "no webhook set" path also prints. A real
Slack POST is never made in any test.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import surface  # noqa: E402


def _item(uri="at://d/app.bsky.feed.post/1", action="reply", **over):
    base = {
        "uri": uri, "cid": "c",
        "author_handle": "someone.bsky.social", "author_did": "did:plc:x",
        "text": "how is everyone handling agents losing context between sessions?",
        "lane_id": "memory", "lane_label": "Agent memory / context engineering",
        "action": action, "confidence": "high",
        "why": "our wheelhouse — flat-file+index answer",
        "url": "https://bsky.app/profile/someone.bsky.social/post/1",
    }
    base.update(over)
    return base


# --- format_item -----------------------------------------------------------

def test_format_item_contains_key_fields():
    out = surface.format_item(_item())
    assert "REPLY" in out
    assert "someone.bsky.social" in out
    assert "context between sessions" in out
    assert "our wheelhouse" in out
    assert "https://bsky.app/profile/someone.bsky.social/post/1" in out
    assert "Agent memory" in out


def test_format_item_low_confidence_flagged():
    out = surface.format_item(_item(confidence="low"))
    assert "⚠️" in out  # low confidence carries the warning mark


def test_format_item_truncates_long_text():
    out = surface.format_item(_item(text="x" * 800))
    assert "…" in out


def test_format_item_unknown_action_falls_back():
    out = surface.format_item(_item(action="weird"))
    assert "WEIRD" in out  # upper-cased fallback, never crashes


# --- ledger ----------------------------------------------------------------

def test_load_surfaced_uris(tmp_path):
    p = tmp_path / "surfaced.jsonl"
    p.write_text(
        json.dumps({"uri": "at://a"}) + "\n" + json.dumps({"uri": "at://b"}) + "\n",
        encoding="utf-8",
    )
    assert surface.load_surfaced_uris(str(p)) == {"at://a", "at://b"}


# --- surface_all -----------------------------------------------------------

def test_surface_all_dry_run_writes_no_ledger(tmp_path, monkeypatch, capsys):
    ledger = str(tmp_path / "surfaced.jsonl")
    monkeypatch.setattr(surface, "SURFACED_PATH", ledger)
    n = surface.surface_all([_item()], dry_run=True)
    assert n == 1
    assert not os.path.exists(ledger)  # side-effect-free preview
    printed = capsys.readouterr().out
    assert "DRY_RUN" in printed


def test_surface_all_live_no_webhook_prints_and_logs(tmp_path, monkeypatch):
    ledger = str(tmp_path / "surfaced.jsonl")
    monkeypatch.setattr(surface, "SURFACED_PATH", ledger)
    # dry_run=False + empty webhook -> print branch (NO network), ledger written
    n = surface.surface_all([_item(uri="at://d/app.bsky.feed.post/9")], webhook_url="", dry_run=False)
    assert n == 1
    assert os.path.exists(ledger)
    with open(ledger, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    assert rows[0]["uri"] == "at://d/app.bsky.feed.post/9"


def test_surface_all_skips_already_surfaced(tmp_path, monkeypatch):
    ledger = str(tmp_path / "surfaced.jsonl")
    with open(ledger, "w", encoding="utf-8") as f:
        f.write(json.dumps({"uri": "at://dupe"}) + "\n")
    monkeypatch.setattr(surface, "SURFACED_PATH", ledger)
    n = surface.surface_all([_item(uri="at://dupe")], webhook_url="", dry_run=False)
    assert n == 0  # deduped


def test_surface_all_empty_list():
    assert surface.surface_all([], dry_run=True) == 0


# --- bot-token (chat.postMessage) path captures ts for the ACT layer --------

def test_surface_all_bot_token_records_ts_and_act_fields(tmp_path, monkeypatch):
    ledger = str(tmp_path / "surfaced.jsonl")
    monkeypatch.setattr(surface, "SURFACED_PATH", ledger)
    # Pretend chat.postMessage succeeded and returned a message ts.
    monkeypatch.setattr(surface, "_post_slack_web", lambda text, token, channel, timeout=15: "1700000000.000100")

    n = surface.surface_all(
        [_item(uri="at://d/app.bsky.feed.post/42")],
        dry_run=False, bot_token="xoxb-test", channel="C123",
    )
    assert n == 1
    with open(ledger, encoding="utf-8") as f:
        rec = json.loads(f.readline())
    # Everything the ACT layer needs to act without re-querying triage:
    assert rec["slack_ts"] == "1700000000.000100"
    assert rec["slack_channel"] == "C123"
    assert rec["uri"] == "at://d/app.bsky.feed.post/42"
    assert rec["cid"] == "c"
    assert rec["author_did"] == "did:plc:x"
    assert rec["action"] == "reply"


def test_surface_all_bot_token_failure_logs_without_ts(tmp_path, monkeypatch):
    ledger = str(tmp_path / "surfaced.jsonl")
    monkeypatch.setattr(surface, "SURFACED_PATH", ledger)
    # chat.postMessage failed (returned None) -> logged, but not actionable.
    monkeypatch.setattr(surface, "_post_slack_web", lambda *a, **k: None)
    surface.surface_all([_item()], dry_run=False, bot_token="xoxb-test", channel="C123")
    with open(ledger, encoding="utf-8") as f:
        rec = json.loads(f.readline())
    assert rec["slack_ts"] is None
