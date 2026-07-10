"""
tests/test_act_tick.py — orchestration-level regression tests for the ACT tick.

The headline is the 2026-07-08 mass-fire regression: surface 3 items, let ONLY
item #2 carry the operator's thumbsup, and prove ONLY item #2 is acted on — the
other two are never touched. Also covers the burst-protection belts (per-tick
cap + pacing) and the fail-closed operator-id guard.

No network: Slack reads (get_reactions) and Bluesky writes (execute_action) are
monkeypatched; the act log is redirected to a tmp file.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import act  # noqa: E402
import act_tick  # noqa: E402
import surface  # noqa: E402


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
    monkeypatch.delenv("ACT_TARGET_TS", raising=False)       # default harness = poll mode
    monkeypatch.delenv("ACT_TARGET_CHANNEL", raising=False)
    monkeypatch.setenv("ACT_PACING_SECONDS", pacing)
    monkeypatch.setenv("ACT_MAX_PER_TICK", max_per_tick)
    for k, v in (caps_env or {}).items():
        monkeypatch.setenv(k, v)

    monkeypatch.setattr(act_tick, "ACT_LOG", str(tmp_path / "act-log.jsonl"))
    monkeypatch.setattr(act_tick, "load_actionable", lambda *a, **k: list(items))
    monkeypatch.setattr(act_tick, "load_acted", lambda path=None: (set(), {}))
    # Neutralize the file-reading expiry pass for the injected-item harness.
    monkeypatch.setattr(act_tick, "expire_stale", lambda acted_uris, now=None, **k: set())
    # Isolate execution-pacing tests from the reaction-poll sleep (tested separately).
    monkeypatch.setattr(act_tick, "REACTION_POLL_SLEEP_S", 0.0)
    monkeypatch.setattr(act_tick, "bluesky_session", lambda: {"did": "did:plc:us"})
    # Self-label ensure-step (safety basis) — stub so no network; report set.
    monkeypatch.setattr(act_tick.bluesky, "ensure_bot_label", lambda session: {"status": "already_set"})
    # Auto-follow digest transport — stub so no Slack call in the default harness.
    monkeypatch.setattr(surface, "_post_slack_web", lambda text, token, channel, timeout=15: "1.0")

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


def test_auto_act_classes_default_is_follow(monkeypatch):
    monkeypatch.delenv("AUTO_ACT_CLASSES", raising=False)
    assert act.load_auto_act_classes() == {"follow"}   # FOLLOW graduated
    monkeypatch.setenv("AUTO_ACT_CLASSES", "off")
    assert act.load_auto_act_classes() == set()
    monkeypatch.setenv("AUTO_ACT_CLASSES", "follow, like")
    assert act.load_auto_act_classes() == {"follow", "like"}


def test_auto_eligible_follow_autonomous_others_gated():
    ACT = {"follow"}          # the shipped default
    NO_REPLY = set()          # AUTO_REPLY_BACK off
    # FOLLOW graduated -> autonomous.
    assert act.auto_eligible({"action": "follow"}, ACT, NO_REPLY) is True
    # like / repost stay gated under the default map.
    assert act.auto_eligible({"action": "like"}, ACT, NO_REPLY) is False
    assert act.auto_eligible({"action": "repost"}, ACT, NO_REPLY) is False
    # scout reply never graduates; converse reply only via AUTO_REPLY_BACK.
    assert act.auto_eligible({"action": "reply", "source": "scout"}, ACT, {"*"}) is False
    assert act.auto_eligible({"action": "reply", "source": "converse", "reply_class": "appreciation"}, ACT, {"appreciation"}) is True
    # like graduates only if explicitly added to the act map.
    assert act.auto_eligible({"action": "like"}, {"follow", "like"}, NO_REPLY) is True


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


# --- FOLLOW graduated to autonomous (SPEC-v3) -------------------------------

def test_follow_auto_executes_without_thumbsup_and_digests(monkeypatch, tmp_path):
    items = [
        {"uri": "f1", "cid": "c1", "author_did": "did:plc:a", "author_handle": "alice.bsky.social",
         "action": "follow", "text": "x", "slack_ts": "1.0", "slack_channel": "C1", "source": "scout"},
        # A like is NOT autonomous under the default map -> stays gated, un-👍'd -> skipped.
        {"uri": "l1", "cid": "c2", "author_did": "did:plc:b", "author_handle": "bob.bsky.social",
         "action": "like", "text": "y", "slack_ts": "2.0", "slack_channel": "C1", "source": "scout"},
    ]
    executed, _ = _wire(monkeypatch, tmp_path, items, reacted_ts=set())  # nothing 👍'd
    monkeypatch.delenv("AUTO_ACT_CLASSES", raising=False)  # default {"follow"}
    # Capture the FYI digest.
    posts = []
    monkeypatch.setattr(surface, "_post_slack_web", lambda text, token, channel, timeout=15: posts.append(text) or "1.0")

    act_tick.run_tick()
    assert executed == ["f1"]                      # follow auto-executed; like NOT
    assert any("auto-followed" in p and "alice.bsky.social" in p for p in posts)  # digest posted


def test_follow_autonomy_disabled_when_self_label_unconfirmed(monkeypatch, tmp_path):
    # Fail-closed: if the bot self-label can't be confirmed, autonomy is OFF this
    # tick -> the un-👍'd follow does NOT fire.
    items = [
        {"uri": "f1", "cid": "c1", "author_did": "did:plc:a", "author_handle": "alice.bsky.social",
         "action": "follow", "text": "x", "slack_ts": "1.0", "slack_channel": "C1", "source": "scout"},
    ]
    executed, _ = _wire(monkeypatch, tmp_path, items, reacted_ts=set())
    monkeypatch.delenv("AUTO_ACT_CLASSES", raising=False)  # default {"follow"}

    def boom(session):
        raise RuntimeError("cannot reach profile record")
    monkeypatch.setattr(act_tick.bluesky, "ensure_bot_label", boom)

    act_tick.run_tick()
    assert executed == []  # self-label unconfirmed -> no autonomous follow


# --- backlog fix (2026-07-10): bounded poll set + expiry + rate-limit backoff ---

import time as _time  # noqa: E402


def _surfaced_file(tmp_path, rows):
    p = tmp_path / "surfaced.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p)


def _iso(epoch):
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(epoch))


def test_load_actionable_bounds_window_and_count(tmp_path):
    now = 1_783_000_000.0
    rows = []
    # 100 fresh items (within 48h) + 50 stale items (surfaced 5 days ago)
    for i in range(100):
        rows.append({"uri": f"at://fresh{i}", "cid": "c", "slack_ts": f"{i}.0",
                     "slack_channel": "C", "ts": _iso(now - i * 60)})  # spread over ~100 min
    for i in range(50):
        rows.append({"uri": f"at://old{i}", "cid": "c", "slack_ts": f"o{i}.0",
                     "slack_channel": "C", "ts": _iso(now - 5 * 24 * 3600)})
    path = _surfaced_file(tmp_path, rows)
    out = act_tick.load_actionable(path=path, window_hours=48, max_items=40, now=now)
    assert len(out) == 40                                   # capped
    assert all(u.startswith("at://fresh") for u in [r["uri"] for r in out])  # no stale
    # newest-first: fresh0 (now) leads
    assert out[0]["uri"] == "at://fresh0"


def test_load_actionable_excludes_stale_window(tmp_path):
    now = 1_783_000_000.0
    rows = [
        {"uri": "at://recent", "cid": "c", "slack_ts": "1.0", "slack_channel": "C", "ts": _iso(now - 3600)},
        {"uri": "at://old", "cid": "c", "slack_ts": "2.0", "slack_channel": "C", "ts": _iso(now - 100 * 3600)},
    ]
    path = _surfaced_file(tmp_path, rows)
    out = act_tick.load_actionable(path=path, window_hours=48, max_items=40, now=now)
    assert [r["uri"] for r in out] == ["at://recent"]


def test_expire_stale_marks_old_unacted_terminal(tmp_path):
    now = 1_783_000_000.0
    rows = [
        {"uri": "at://stale", "action": "like", "slack_ts": "1.0", "slack_channel": "C", "ts": _iso(now - 100 * 3600)},
        {"uri": "at://fresh", "action": "like", "slack_ts": "2.0", "slack_channel": "C", "ts": _iso(now - 3600)},
        {"uri": "at://acted", "action": "like", "slack_ts": "3.0", "slack_channel": "C", "ts": _iso(now - 100 * 3600)},
    ]
    path = _surfaced_file(tmp_path, rows)
    log = str(tmp_path / "act-log.jsonl")
    expired = act_tick.expire_stale({"at://acted"}, now=now, expire_hours=72, path=path, log_path=log)
    assert expired == {"at://stale"}                        # fresh kept, already-acted skipped
    rec = json.loads(open(log, encoding="utf-8").readline())
    assert rec["uri"] == "at://stale" and rec["status"] == "expired"
    assert "expired" in act.TERMINAL_STATUSES               # load_acted treats it terminal


def test_get_reactions_backs_off_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, data, headers=None):
            self.status_code = status; self._data = data
            self.content = b"x"; self.headers = headers or {}
        def json(self): return self._data

    def fake_get(url, headers=None, params=None, timeout=15):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "1"})
        return _Resp(200, {"ok": True, "message": {"reactions": [{"name": "+1", "users": ["U_OP"]}]}})

    slept = []
    monkeypatch.setattr(act.requests, "get", fake_get)
    monkeypatch.setattr(act.time, "sleep", lambda s: slept.append(s))
    out = act.get_reactions("C", "1.0", "tok", retries=1)
    assert out == [{"name": "+1", "users": ["U_OP"]}]        # retried after backoff
    assert calls["n"] == 2 and slept and slept[0] >= 1.0


def test_get_reactions_gives_up_after_backoff(monkeypatch):
    class _Resp:
        status_code = 429; content = b"x"; headers = {"Retry-After": "1"}
        def json(self): return {"ok": False, "error": "ratelimited"}
    monkeypatch.setattr(act.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    assert act.get_reactions("C", "1.0", "tok", retries=1) == []   # still limited -> [] (fail safe)


def test_recent_thumbsup_acted_despite_large_stale_backlog(monkeypatch, tmp_path):
    # End-to-end: a huge stale backlog + one FRESH 👍'd item -> the fresh item is
    # polled (bounded set) and executed; the backlog is expired out, not polled.
    now = _time.time()
    rows = [{"uri": f"at://old{i}", "action": "like", "cid": "c", "author_did": f"d{i}",
             "slack_ts": f"o{i}.0", "slack_channel": "C1", "ts": _iso(now - 200 * 3600)}
            for i in range(300)]
    rows.append({"uri": "at://freshpost", "action": "like", "cid": "cf", "author_did": "dfresh",
                 "slack_ts": "FRESH.ts", "slack_channel": "C1", "ts": _iso(now - 600)})
    surfaced = _surfaced_file(tmp_path, rows)

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb"); monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    monkeypatch.setenv("SLACK_OPERATOR_USER_ID", "U_OP"); monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(act_tick, "SURFACED_PATH", surfaced)
    monkeypatch.setattr(act_tick, "ACT_LOG", str(tmp_path / "act-log.jsonl"))
    monkeypatch.setattr(act_tick, "load_acted", lambda path=None: (set(), {}))
    monkeypatch.setattr(act_tick, "bluesky_session", lambda: {"did": "did:plc:us"})
    monkeypatch.setattr(act_tick.time, "sleep", lambda s: None)

    polled = []
    def fake_reactions(channel, ts, token, timeout=15, retries=1):
        polled.append(ts)
        return [{"name": "+1", "users": ["U_OP"], "count": 1}] if ts == "FRESH.ts" else []
    monkeypatch.setattr(act, "get_reactions", fake_reactions)
    executed = []
    monkeypatch.setattr(act, "execute_action", lambda item, session=None, dry_run=False:
                        (executed.append(item["uri"]) or {"action": item["action"], "status": "executed", "uri": item["uri"]}))

    rc = act_tick.run_tick()
    assert rc == 0
    assert executed == ["at://freshpost"]        # the fresh 👍'd item WAS read + acted
    assert len(polled) <= act_tick.POLL_MAX_ITEMS  # never polled the 300-item backlog
    assert "FRESH.ts" in polled


# --- event-driven targeting (SPEC-v4.1): reaction_added -> act on THAT item -----

def test_event_targeted_acts_on_the_reacted_item(monkeypatch, tmp_path):
    item = _surfaced("post_target", "TS.9", action="like")
    executed, _ = _wire(monkeypatch, tmp_path, items=[], reacted_ts={"TS.9"})
    monkeypatch.setenv("ACT_TARGET_TS", "TS.9")
    monkeypatch.setenv("ACT_TARGET_CHANNEL", "C1")
    # Targeted lookup returns the one reacted item (no backlog scan).
    monkeypatch.setattr(act_tick, "load_targeted", lambda ts, ch=None, path=None: [item] if ts == "TS.9" else [])
    # If the poll path were taken this would blow up (proves we used the targeted path).
    monkeypatch.setattr(act_tick, "load_actionable", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not poll in targeted mode")))
    rc = act_tick.run_tick()
    assert rc == 0
    assert executed == ["post_target"]   # acted on the exact reacted item, instantly


def test_event_targeted_idempotent_vs_poll(monkeypatch, tmp_path):
    item = _surfaced("dup", "TS.1", action="like")
    executed, _ = _wire(monkeypatch, tmp_path, items=[], reacted_ts={"TS.1"})
    monkeypatch.setenv("ACT_TARGET_TS", "TS.1")
    monkeypatch.setattr(act_tick, "load_targeted", lambda ts, ch=None, path=None: [item])
    # The poll already acted this uri (it's terminal in the act-log).
    monkeypatch.setattr(act_tick, "load_acted", lambda path=None: ({"dup"}, {}))
    act_tick.run_tick()
    assert executed == []   # already acted -> no-op; webhook + poll can't double-fire


def test_event_targeted_reverifies_operator_reaction(monkeypatch, tmp_path):
    # Targeted, but the message carries NO operator 👍 (spoofed dispatch / 👍 removed):
    # act_tick re-verifies via reactions.get and does NOT act.
    item = _surfaced("noreact", "TS.2", action="like")
    executed, _ = _wire(monkeypatch, tmp_path, items=[], reacted_ts=set())  # nothing reacted
    monkeypatch.setenv("ACT_TARGET_TS", "TS.2")
    monkeypatch.setattr(act_tick, "load_targeted", lambda ts, ch=None, path=None: [item])
    act_tick.run_tick()
    assert executed == []   # re-verify gate: no operator 👍 on the message -> no act


def test_event_targeted_no_matching_item_is_noop(monkeypatch, tmp_path):
    executed, _ = _wire(monkeypatch, tmp_path, items=[], reacted_ts={"TS.x"})
    monkeypatch.setenv("ACT_TARGET_TS", "TS.x")
    monkeypatch.setattr(act_tick, "load_targeted", lambda ts, ch=None, path=None: [])  # ts not a surfaced item
    rc = act_tick.run_tick()
    assert rc == 0 and executed == []


def test_load_targeted_matches_by_ts_and_channel(tmp_path):
    rows = [
        {"uri": "at://a", "slack_ts": "1.0", "slack_channel": "C1"},
        {"uri": "at://b", "slack_ts": "2.0", "slack_channel": "C1"},
        {"uri": "at://c", "slack_ts": "2.0", "slack_channel": "C2"},
    ]
    p = _surfaced_file(tmp_path, rows)
    assert [r["uri"] for r in act_tick.load_targeted("2.0", "C1", path=p)] == ["at://b"]
    assert sorted(r["uri"] for r in act_tick.load_targeted("2.0", None, path=p)) == ["at://b", "at://c"]
    assert act_tick.load_targeted("", None, path=p) == []
