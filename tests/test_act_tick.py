"""
tests/test_act_tick.py — orchestration-level regression tests for the ACT tick.

The headline is the 2026-07-08 mass-fire regression: surface 3 items, let ONLY
item #2 carry the operator's thumbsup, and prove ONLY item #2 is acted on — the
other two are never touched. Also covers the burst-protection belts (per-tick
cap + pacing) and the fail-closed operator-id guard.

No network: Slack reads (get_reactions) and Bluesky writes (execute_action) are
monkeypatched; the act log is redirected to a tmp file.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import act  # noqa: E402
import act_tick  # noqa: E402


def _surfaced(uri, ts, action="reply"):
    return {
        "uri": uri, "cid": f"cid-{uri}", "author_did": f"did:plc:{uri}",
        "action": action, "text": "how do you keep agents on-mission?",
        "why": "wheelhouse", "slack_ts": ts, "slack_channel": "C1",
    }


def _wire(monkeypatch, tmp_path, items, reacted_ts, *, operator="U_OP",
          caps_env=None, pacing="0", max_per_tick="3"):
    """Common harness: set env, redirect log, stub Slack + Bluesky + session."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    if operator is None:
        monkeypatch.delenv("SLACK_OPERATOR_USER_ID", raising=False)
    else:
        monkeypatch.setenv("SLACK_OPERATOR_USER_ID", operator)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("ACT_PACING_SECONDS", pacing)
    monkeypatch.setenv("ACT_MAX_PER_TICK", max_per_tick)
    for k, v in (caps_env or {}).items():
        monkeypatch.setenv(k, v)

    monkeypatch.setattr(act_tick, "ACT_LOG", str(tmp_path / "act-log.jsonl"))
    monkeypatch.setattr(act_tick, "load_actionable", lambda path=None: list(items))
    monkeypatch.setattr(act_tick, "load_acted", lambda path=None: (set(), {}))
    monkeypatch.setattr(act_tick, "bluesky_session", lambda: {"did": "did:plc:us"})

    # Only messages whose ts is in reacted_ts carry the operator's 👍.
    def fake_reactions(channel, ts, token, timeout=15):
        if ts in reacted_ts:
            return [{"name": "+1", "users": ["U_OP"], "count": 1}]
        return []
    monkeypatch.setattr(act, "get_reactions", fake_reactions)

    executed = []

    def fake_execute(item, session=None, dry_run=False):
        executed.append(item["uri"])
        return {"action": item["action"], "status": "executed", "uri": item["uri"]}
    monkeypatch.setattr(act, "execute_action", fake_execute)

    slept = []
    monkeypatch.setattr(act_tick.time, "sleep", lambda s: slept.append(s))
    return executed, slept


# --- THE regression test ----------------------------------------------------

def test_only_the_thumbsupped_item_is_acted(monkeypatch, tmp_path):
    items = [
        _surfaced("post1", "111.1"),
        _surfaced("post2", "222.2"),
        _surfaced("post3", "333.3"),
    ]
    # ONLY item #2 (ts 222.2) has the operator's thumbsup.
    executed, _ = _wire(monkeypatch, tmp_path, items, reacted_ts={"222.2"})

    rc = act_tick.run_tick()
    assert rc == 0
    assert executed == ["post2"]          # exactly one, and the right one
    assert "post1" not in executed
    assert "post3" not in executed


# --- burst protection -------------------------------------------------------

def test_per_tick_cap_stops_mass_fire(monkeypatch, tmp_path):
    # All 5 approved, but the per-tick cap is 2 -> only 2 fire this tick.
    items = [_surfaced(f"p{i}", f"{i}.0", action="like") for i in range(5)]
    reacted = {f"{i}.0" for i in range(5)}
    executed, _ = _wire(monkeypatch, tmp_path, items, reacted_ts=reacted, max_per_tick="2")

    act_tick.run_tick()
    assert len(executed) == 2             # hard ceiling held


def test_pacing_sleeps_between_executed_actions(monkeypatch, tmp_path):
    items = [_surfaced(f"p{i}", f"{i}.0", action="like") for i in range(3)]
    reacted = {f"{i}.0" for i in range(3)}
    executed, slept = _wire(
        monkeypatch, tmp_path, items, reacted_ts=reacted, pacing="30", max_per_tick="5"
    )
    act_tick.run_tick()
    assert len(executed) == 3
    # Paced before the 2nd and 3rd action only (not before the 1st).
    assert slept == [30.0, 30.0]


# --- fail-closed operator-id guard ------------------------------------------

def test_no_operator_id_acts_on_nothing(monkeypatch, tmp_path):
    items = [_surfaced("post1", "111.1"), _surfaced("post2", "222.2")]
    # Even though both messages are 👍'd, no SLACK_OPERATOR_USER_ID -> act on nothing.
    executed, _ = _wire(monkeypatch, tmp_path, items, reacted_ts={"111.1", "222.2"}, operator=None)

    rc = act_tick.run_tick()
    assert rc == 0
    assert executed == []                 # fail-closed: nothing acted


# --- loaders ----------------------------------------------------------------

def test_load_pacing_and_max_env(monkeypatch):
    monkeypatch.setenv("ACT_PACING_SECONDS", "12.5")
    monkeypatch.setenv("ACT_MAX_PER_TICK", "7")
    assert act.load_pacing_seconds() == 12.5
    assert act.load_max_per_tick() == 7


def test_load_pacing_and_max_defaults(monkeypatch):
    monkeypatch.delenv("ACT_PACING_SECONDS", raising=False)
    monkeypatch.delenv("ACT_MAX_PER_TICK", raising=False)
    assert act.load_pacing_seconds() == act.DEFAULT_PACING_SECONDS
    assert act.load_max_per_tick() == act.DEFAULT_MAX_PER_TICK


# --- AUTO_REPLY_BACK earn-it ladder -----------------------------------------

def test_auto_reply_classes_parsing(monkeypatch):
    monkeypatch.setenv("AUTO_REPLY_BACK", "off")
    assert act.load_auto_reply_classes() == set()
    monkeypatch.setenv("AUTO_REPLY_BACK", "all")
    assert act.load_auto_reply_classes() == {"*"}
    monkeypatch.setenv("AUTO_REPLY_BACK", "appreciation, question")
    assert act.load_auto_reply_classes() == {"appreciation", "question"}


def test_auto_eligible_only_converse_and_allowlisted():
    # Scout actions NEVER graduate, even with "*".
    assert act.auto_eligible({"source": "scout", "reply_class": "question"}, {"*"}) is False
    # Converse item, class allowlisted -> eligible.
    assert act.auto_eligible({"source": "converse", "reply_class": "appreciation"}, {"appreciation"}) is True
    # Converse item, class NOT allowlisted -> not eligible.
    assert act.auto_eligible({"source": "converse", "reply_class": "question"}, {"appreciation"}) is False
    # Empty allowlist (default) -> nothing eligible.
    assert act.auto_eligible({"source": "converse", "reply_class": "question"}, set()) is False


def test_auto_reply_back_posts_converse_without_thumbsup(monkeypatch, tmp_path):
    # A converse reply-back, NOT 👍'd, but its class is in the AUTO_REPLY_BACK
    # allowlist -> it posts autonomously. A scout item (also un-👍'd) does NOT.
    items = [
        {"uri": "conv1", "cid": "c1", "author_did": "did:plc:a", "action": "reply",
         "text": "thanks!", "slack_ts": "1.0", "slack_channel": "C1",
         "source": "converse", "reply_class": "appreciation", "draft_text": "glad it landed."},
        {"uri": "scout1", "cid": "c2", "author_did": "did:plc:b", "action": "like",
         "text": "x", "slack_ts": "2.0", "slack_channel": "C1", "source": "scout"},
    ]
    executed, _ = _wire(monkeypatch, tmp_path, items, reacted_ts=set())  # nothing 👍'd
    monkeypatch.setenv("AUTO_REPLY_BACK", "appreciation")

    act_tick.run_tick()
    assert executed == ["conv1"]   # converse appreciation auto-posted; scout NOT


def test_default_no_auto_nothing_fires_without_thumbsup(monkeypatch, tmp_path):
    items = [
        {"uri": "conv1", "cid": "c1", "author_did": "did:plc:a", "action": "reply",
         "text": "thanks!", "slack_ts": "1.0", "slack_channel": "C1",
         "source": "converse", "reply_class": "appreciation", "draft_text": "glad it landed."},
    ]
    executed, _ = _wire(monkeypatch, tmp_path, items, reacted_ts=set())
    monkeypatch.setenv("AUTO_REPLY_BACK", "off")  # default
    act_tick.run_tick()
    assert executed == []  # gated: no 👍, no auto -> nothing
