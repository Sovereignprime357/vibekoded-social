"""
tests/test_content_refill.py — the content refill loop (SPEC-content-refill-v1).

No network: generate.generate + act.get_reactions are monkeypatched, the privacy
guard is REAL, ledgers/queue are tmp files.
"""

import inspect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import act  # noqa: E402
import content_queue  # noqa: E402
import content_refill as cr  # noqa: E402
import post_tick  # noqa: E402


# --- I-PILLAR-ROTATION: batch pillar selection ------------------------------

def test_select_pillars_no_two_consecutive():
    seq = cr.select_pillars(6, recent_pillars=[])
    assert all(seq[i] != seq[i + 1] for i in range(len(seq) - 1)), seq


def test_select_pillars_meta_capped_and_not_after_recent_meta():
    seq = cr.select_pillars(5, recent_pillars=["meta"])
    # META already 1-ago -> must not appear early in a 5-window
    assert seq[0] != "meta"
    assert seq.count("meta") <= 1


# --- I-NO-RAW-INTEL: only graduated intel, never raw ops-intel --------------

def test_load_graduated_requires_graduated_marker(tmp_path):
    p = tmp_path / "grad.jsonl"
    p.write_text(
        json.dumps({"pillar": "showcase", "raw": "graduated insight", "graduated": True}) + "\n" +
        json.dumps({"pillar": "showcase", "raw": "RAW ungraduated insight"}) + "\n",  # no marker
        encoding="utf-8",
    )
    seeds = cr.load_graduated(str(p))
    assert len(seeds) == 1
    assert seeds[0]["raw"] == "graduated insight" and seeds[0]["source"] == "graduated"


def test_load_graduated_missing_file_is_empty(tmp_path):
    assert cr.load_graduated(str(tmp_path / "nope.jsonl")) == []


def test_source_selection_code_never_references_raw_intel_log():
    # Structural proof of I-NO-RAW-INTEL: the source-selection functions contain no
    # reference to the raw ops-intel log — raw intel can't be a source, by construction.
    for fn in (cr.load_graduated, cr._seed_pool_by_pillar, cr.generate_candidates):
        src = inspect.getsource(fn)
        assert "ops-intel-log" not in src and "ops_insight" not in src
    # The only intel path is the graduated file.
    assert cr.GRADUATED_PATH.endswith("graduated-intel.jsonl")


# --- generation: guard fail-closed + dedup ----------------------------------

def test_generate_candidates_drops_guard_blocked(monkeypatch):
    # Generation returns a leaking draft -> the REAL guard blocks it -> dropped.
    monkeypatch.setattr(cr.generate, "generate", lambda entry, kind="post": "great tip from shayler today")
    out = cr.generate_candidates(n=3, recent_pillars=[], graduated=[], already_posted=set())
    assert out == []   # nothing survives the guard


def test_generate_candidates_dedups_vs_posted(monkeypatch):
    monkeypatch.setattr(cr.generate, "generate", lambda entry, kind="post": "a clean evergreen post about specs")
    posted = {cr._normalize("a clean evergreen post about specs")}
    out = cr.generate_candidates(n=3, recent_pillars=[], graduated=[], already_posted=posted)
    assert out == []   # exact dup of a posted text -> dropped


def test_generate_candidates_produces_guarded_records(monkeypatch):
    # Distinct clean text per call so dedup doesn't collapse the batch.
    calls = {"n": 0}
    def fake_gen(entry, kind="post"):
        calls["n"] += 1
        return f"clean post number {calls['n']} about building with ai"
    monkeypatch.setattr(cr.generate, "generate", fake_gen)
    out = cr.generate_candidates(n=3, recent_pillars=[], graduated=[], already_posted=set())
    assert len(out) == 3
    for rec in out:
        assert rec["text"] and rec["pillar"] and rec["id"]
        assert rec["provenance"]["source"] in ("evergreen", "graduated")   # I-PROVENANCE


# --- surface: safe-degrade (no token -> never posts, no ledger) -------------

def test_surface_no_token_writes_no_ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    ledger = str(tmp_path / "surfaced.jsonl")
    rec = {"id": "c1", "text": "x", "pillar": "showcase", "type": "moment", "source": "evergreen"}
    n = cr.surface_candidates([rec], token="", channel="C", dry_run=False, surfaced_path=ledger)
    assert n == 0 and not os.path.exists(ledger)   # never surfaced, no ledger, never auto-posted


def test_surface_writes_ledger_with_slack_ts(tmp_path, monkeypatch):
    ledger = str(tmp_path / "surfaced.jsonl")
    monkeypatch.setattr(cr, "_post_slack_web", lambda text, token, channel, timeout=15: "1700.5")
    rec = {"id": "c1", "text": "a clean post", "pillar": "showcase", "type": "moment",
           "source": "evergreen", "provenance": {"source": "evergreen"}}
    n = cr.surface_candidates([rec], token="xoxb", channel="C_REFILL", dry_run=False, surfaced_path=ledger)
    assert n == 1
    row = json.loads(open(ledger, encoding="utf-8").readline())
    assert row["slack_ts"] == "1700.5" and row["status"] == "surfaced" and row["slack_channel"] == "C_REFILL"


# --- ROTATION-AWARE surfacing (the funnel fix) ------------------------------
# We must not surface a candidate rotation would reject RIGHT NOW: a 👍 has to mean
# "this will post". The trap was surfacing a blocked META and stacking it at the
# bottom (easiest to react to), which quietly stalled the feed for days.

def _cand(cid, pillar, text=None):
    return {"id": cid, "text": text or f"a clean post {cid} about building with ai",
            "pillar": pillar, "type": "moment", "source": "evergreen",
            "provenance": {"source": "evergreen", "pillar": pillar}}


def _capture_posts(monkeypatch):
    """Fake Slack transport: 1 call per message, a distinct slack_ts each time."""
    posts = []
    def fake(text, token, channel, timeout=15):
        posts.append(text)
        return f"1700.{len(posts)}"
    monkeypatch.setattr(cr, "_post_slack_web", fake)
    return posts


def _ledger_pillars(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    return [r["pillar"] for r in rows]


def test_surface_skips_meta_when_last_posted_meta(tmp_path, monkeypatch):
    # Last post was META -> META is 1-in-5-capped -> a META candidate is NOT surfaced;
    # the four non-META candidates ARE.
    posts = _capture_posts(monkeypatch)
    ledger = str(tmp_path / "s.jsonl")
    cands = [_cand("m", "meta"), _cand("a", "showcase"), _cand("b", "operator"),
             _cand("c", "ask-help"), _cand("d", "dreaming")]
    n = cr.surface_candidates(cands, token="xoxb", channel="C", dry_run=False,
                              surfaced_path=ledger, recent_pillars=["meta"])
    assert n == 4
    pillars = _ledger_pillars(ledger)
    assert "meta" not in pillars
    assert set(pillars) == {"showcase", "operator", "ask-help", "dreaming"}


def test_surface_skips_same_pillar_as_last(tmp_path, monkeypatch):
    # Last post was showcase -> no two-in-a-row -> a showcase candidate is NOT surfaced.
    _capture_posts(monkeypatch)
    ledger = str(tmp_path / "s.jsonl")
    cands = [_cand("a", "showcase"), _cand("b", "operator"), _cand("c", "question")]
    n = cr.surface_candidates(cands, token="xoxb", channel="C", dry_run=False,
                              surfaced_path=ledger, recent_pillars=["showcase"])
    assert n == 2
    assert set(_ledger_pillars(ledger)) == {"operator", "question"}


def test_surface_all_blocked_fires_alert_and_surfaces_nothing(tmp_path, monkeypatch):
    # Every candidate shares the last pillar -> all blocked. Surface NOTHING, but do
    # NOT go silent: fire the existing queue-health alert (empty first, else blocked).
    posts = _capture_posts(monkeypatch)
    ledger = str(tmp_path / "s.jsonl")
    fired = []
    monkeypatch.setattr(cr, "queue_empty_alert", lambda **k: fired.append("empty") or False)
    monkeypatch.setattr(cr, "queue_rotation_blocked_alert", lambda **k: fired.append("blocked") or True)
    cands = [_cand("a", "showcase"), _cand("b", "showcase")]
    n = cr.surface_candidates(cands, token="xoxb", channel="C", dry_run=False,
                              surfaced_path=ledger, recent_pillars=["showcase"])
    assert n == 0
    assert not os.path.exists(ledger)   # nothing surfaced, no ledger
    assert posts == []                  # never posted a candidate card
    assert fired == ["empty", "blocked"]  # empty checked first, then rotation-blocked


def test_surface_never_leaves_meta_last_in_stack(tmp_path, monkeypatch):
    # META (eligible here) must not be the BOTTOM card of the collapsed stack.
    posts = _capture_posts(monkeypatch)
    ledger = str(tmp_path / "s.jsonl")
    # generation order puts meta LAST (the old trap); recent=[] so nothing is blocked.
    cands = [_cand("a", "showcase"), _cand("b", "operator"), _cand("m", "meta")]
    n = cr.surface_candidates(cands, token="xoxb", channel="C", dry_run=False,
                              surfaced_path=ledger, recent_pillars=[])
    assert n == 3
    pillars = _ledger_pillars(ledger)
    assert pillars[-1] != "meta"        # META never at the bottom of the stack
    assert pillars[0] == "meta"         # floated to the top (deterministic)


def test_eligible_card_states_its_pillar_on_its_own_line():
    card = cr.format_candidate({"pillar": "ask-help", "source": "evergreen",
                                "text": "how are you wiring approvals?"})
    assert "PILLAR: ask-help" in card   # collapsed-stack ambiguity is readable


def test_surface_one_slack_message_each_distinct_ts(tmp_path, monkeypatch):
    # Don't regress: each surfaced candidate is its OWN Slack message with a distinct ts.
    posts = _capture_posts(monkeypatch)
    ledger = str(tmp_path / "s.jsonl")
    cands = [_cand("a", "showcase"), _cand("b", "operator"), _cand("c", "question")]
    n = cr.surface_candidates(cands, token="xoxb", channel="C", dry_run=False,
                              surfaced_path=ledger, recent_pillars=[])
    assert n == 3
    assert len(posts) == 3                              # one message per candidate
    rows = [json.loads(l) for l in open(ledger, encoding="utf-8") if l.strip()]
    tss = [r["slack_ts"] for r in rows]
    assert len(tss) == 3 and len(set(tss)) == 3         # distinct slack_ts each


# --- 👍-gated enqueue (I-HUMAN-GATE-CONTENT) --------------------------------

def _surfaced_ledger(tmp_path, cid="c1", ts="1700.5"):
    p = tmp_path / "surfaced.jsonl"
    p.write_text(json.dumps({
        "id": cid, "status": "surfaced", "text": "a clean approved post about specs",
        "pillar": "operator", "type": "decision", "source": "evergreen",
        "provenance": {"source": "evergreen", "pillar": "operator"},
        "slack_ts": ts, "slack_channel": "C_REFILL",
    }) + "\n", encoding="utf-8")
    return str(p)


def _offline(monkeypatch, tmp_path):
    # Keep poll_and_enqueue offline + deterministic: no real channel scan, and an empty
    # posted history so rotation blocks nothing (the ledger path is what's under test).
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [])
    monkeypatch.setattr(cr, "POSTED_PATH", str(tmp_path / "posted.jsonl"))


def test_enqueue_on_operator_thumbsup(tmp_path, monkeypatch):
    _offline(monkeypatch, tmp_path)
    ledger = _surfaced_ledger(tmp_path)
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)   # content_queue.append_entry writes here
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [{"name": "+1", "users": ["U_OP"], "count": 1}])
    n = cr.poll_and_enqueue(token="xoxb", channel="C_REFILL", operator_id="U_OP",
                            dry_run=False, surfaced_path=ledger)
    assert n == 1
    entry = json.loads(open(queue, encoding="utf-8").readline())
    assert entry["final_text"] == "a clean approved post about specs"   # posts verbatim
    assert entry["pillar"] == "operator" and entry["used"] is False
    assert entry["provenance"]["approved_by"] == "U_OP"                 # approval trail (I-PROVENANCE)
    # marked enqueued -> a second poll does NOT double-enqueue
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [{"name": "+1", "users": ["U_OP"], "count": 1}])
    n2 = cr.poll_and_enqueue(token="xoxb", channel="C_REFILL", operator_id="U_OP",
                             dry_run=False, surfaced_path=ledger)
    assert n2 == 0
    assert sum(1 for _ in open(queue, encoding="utf-8")) == 1


def test_no_thumbsup_never_enqueues(tmp_path, monkeypatch):
    _offline(monkeypatch, tmp_path)
    ledger = _surfaced_ledger(tmp_path)
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [])   # no reaction
    n = cr.poll_and_enqueue(token="xoxb", channel="C_REFILL", operator_id="U_OP",
                            dry_run=False, surfaced_path=ledger)
    assert n == 0 and not os.path.exists(queue)   # un-👍'd never enqueues


def test_enqueue_fail_closed_without_operator_id(tmp_path, monkeypatch):
    _offline(monkeypatch, tmp_path)
    ledger = _surfaced_ledger(tmp_path)
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    # Even with a thumbsup present, no operator id -> nothing enqueues (fail-closed).
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [{"name": "+1", "users": ["U_OP"], "count": 1}])
    n = cr.poll_and_enqueue(token="xoxb", channel="C_REFILL", operator_id=None,
                            dry_run=False, surfaced_path=ledger)
    assert n == 0 and not os.path.exists(queue)


# --- SPEC-v8 I-NO-DECOY: Slack is the bus (channel-scan enqueue + card contract) ---

def _ext_rec(cid="ext1", pillar="showcase", text="we wired the intel bus into slack today. how do you route approvals?",
             freshness="fresh", source="research", prov_source="commit abc123: wired the slack bus"):
    return {"id": cid, "pillar": pillar, "type": "moment", "text": text, "source": source,
            "freshness": freshness, "provenance": {"source": prov_source, "pillar": pillar}}


def _channel_msg(rec, ts="1800.1", reacted=True, human_prefix="here's a candidate for today:\n"):
    # A card as the OPERATOR's PC task would post it: human text + the machine envelope.
    text = human_prefix + f"> {rec['text']}\n" + cr.build_card_envelope(rec)
    reactions = [{"name": "+1", "users": ["U_OP"], "count": 1}] if reacted else []
    return {"ts": ts, "text": text, "reactions": reactions}


def test_card_envelope_roundtrips_through_parse():
    rec = _ext_rec(cid="z1", pillar="operator", text="spec the invariants first. what is your rule?",
                   prov_source="merge #42")
    card = cr.parse_card(cr.format_candidate(rec))   # bot card carries the envelope too
    assert card["id"] == "z1" and card["pillar"] == "operator"
    assert card["final_text"] == "spec the invariants first. what is your rule?"
    assert card["provenance"]["source"] == "merge #42"


def test_external_card_with_thumbsup_enqueues(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setattr(cr, "POSTED_PATH", str(tmp_path / "posted.jsonl"))   # empty -> nothing blocked
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [])         # ledger path empty
    rec = _ext_rec()
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [_channel_msg(rec)])
    n = cr.poll_and_enqueue(token="xoxb", channel="C_REFILL", operator_id="U_OP",
                            dry_run=False, surfaced_path=str(tmp_path / "s.jsonl"))
    assert n == 1
    entry = json.loads(open(queue, encoding="utf-8").readline())
    assert entry["final_text"] == rec["text"] and entry["pillar"] == "showcase"
    assert entry["provenance"]["approved_by"] == "U_OP"
    assert entry["provenance"]["candidate_id"] == "ext1"


def test_same_id_thumbsup_twice_enqueues_once(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setattr(cr, "POSTED_PATH", str(tmp_path / "posted.jsonl"))
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [])
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [_channel_msg(_ext_rec())])
    a = cr.poll_and_enqueue(token="xoxb", channel="C", operator_id="U_OP", dry_run=False,
                            surfaced_path=str(tmp_path / "s.jsonl"))
    # A second poll (simulating a later tick / restart) sees the id already in the queue.
    b = cr.poll_and_enqueue(token="xoxb", channel="C", operator_id="U_OP", dry_run=False,
                            surfaced_path=str(tmp_path / "s.jsonl"))
    assert a == 1 and b == 0
    assert sum(1 for _ in open(queue, encoding="utf-8")) == 1


def test_rotation_blocked_external_card_is_held_not_enqueued(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    # Last posted pillar == showcase -> a showcase card is rotation-blocked right now.
    posted = tmp_path / "posted.jsonl"
    posted.write_text(json.dumps({"pillar": "showcase", "text": "prev"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(cr, "POSTED_PATH", str(posted))
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [])
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [_channel_msg(_ext_rec(pillar="showcase"))])
    n = cr.poll_and_enqueue(token="xoxb", channel="C", operator_id="U_OP", dry_run=False,
                            surfaced_path=str(tmp_path / "s.jsonl"))
    assert n == 0 and not os.path.exists(queue)   # held (not dropped), never enqueued


def test_malformed_card_does_not_enqueue_and_logs(tmp_path, monkeypatch, capsys):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setattr(cr, "POSTED_PATH", str(tmp_path / "posted.jsonl"))
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [])
    bad = {"ts": "1800.9", "text": cr.CARD_MARKER + " {not valid json",
           "reactions": [{"name": "+1", "users": ["U_OP"], "count": 1}]}
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [bad])
    n = cr.poll_and_enqueue(token="xoxb", channel="C", operator_id="U_OP", dry_run=False,
                            surfaced_path=str(tmp_path / "s.jsonl"))
    assert n == 0 and not os.path.exists(queue)
    assert "malformed" in capsys.readouterr().out.lower()


def test_external_card_missing_provenance_rejected(tmp_path, monkeypatch, capsys):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setattr(cr, "POSTED_PATH", str(tmp_path / "posted.jsonl"))
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [])
    rec = _ext_rec()
    rec["provenance"] = {}   # I-PROVENANCE: no source -> fail closed
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [_channel_msg(rec)])
    n = cr.poll_and_enqueue(token="xoxb", channel="C", operator_id="U_OP", dry_run=False,
                            surfaced_path=str(tmp_path / "s.jsonl"))
    assert n == 0 and not os.path.exists(queue)
    assert "provenance" in capsys.readouterr().out.lower()


def test_external_card_failing_voice_gate_rejected(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setattr(cr, "POSTED_PATH", str(tmp_path / "posted.jsonl"))
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [])
    # An externally-authored card with an em-dash never saw generation's voice gate.
    rec = _ext_rec(text="we wired the bus — and it just works.")
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [_channel_msg(rec)])
    n = cr.poll_and_enqueue(token="xoxb", channel="C", operator_id="U_OP", dry_run=False,
                            surfaced_path=str(tmp_path / "s.jsonl"))
    assert n == 0 and not os.path.exists(queue)   # I-VOICE enforced at enqueue too


def test_researched_cards_suppress_evergreen_fallback(monkeypatch):
    fresh = {"ts": str(time.time()),
             "text": cr.build_card_envelope(_ext_rec(cid="r1", freshness="fresh"))}
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [fresh])
    assert cr.researched_cards_present(token="xoxb", channel="C") is True   # -> evergreen suppressed


def test_evergreen_only_channel_allows_fallback(monkeypatch):
    ever = {"ts": str(time.time()),
            "text": cr.build_card_envelope(_ext_rec(cid="e1", freshness="evergreen", prov_source="evergreen"))}
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [ever])
    assert cr.researched_cards_present(token="xoxb", channel="C") is False  # fallback may surface


def test_stale_researched_card_outside_window_does_not_count(monkeypatch):
    old = {"ts": str(time.time() - 48 * 3600),
           "text": cr.build_card_envelope(_ext_rec(cid="old1", freshness="fresh"))}
    monkeypatch.setattr(cr, "_conversations_history", lambda *a, **k: [old])
    assert cr.researched_cards_present(token="xoxb", channel="C", within_hours=18) is False


# --- PR C: expire stale rotation-stranded queue entries ---------------------

def test_expire_stale_queue_wrapper_expires_when_live(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    content_queue.append_entry("old meta", type="moment", pillar="meta",
                               ts="2026-07-01T00:00:00+00:00", path=queue)
    n = cr.expire_stale_queue(max_age_hours=24, dry_run=False)
    assert n == 1 and content_queue.count_unused(queue) == 0


def test_expire_stale_queue_wrapper_skips_in_dry_run(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    content_queue.append_entry("old meta", type="moment", pillar="meta",
                               ts="2026-07-01T00:00:00+00:00", path=queue)
    n = cr.expire_stale_queue(max_age_hours=24, dry_run=True)
    assert n == 0 and content_queue.count_unused(queue) == 1   # dry-run mutates nothing


# --- post_tick posts the approved final_text VERBATIM (no re-generation) ----

def test_post_tick_posts_final_text_verbatim(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("POST_FORCE", "1")   # bypass the I-PACE window gate (SPEC-v8); this test is about verbatim posting, not pacing
    content_queue.append_entry(raw="seed source", type="decision", pillar="operator",
                               final_text="the EXACT approved post text", path=queue)
    monkeypatch.setattr(post_tick, "POSTED_LOG", str(tmp_path / "posted.jsonl"))
    monkeypatch.setattr(post_tick, "SKIPPED_LOG", str(tmp_path / "skipped.jsonl"))
    # If post_tick tried to generate, this would raise — proving it used final_text.
    monkeypatch.setattr(post_tick.generate, "generate",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not generate")))
    rc = post_tick.run_tick()
    assert rc == 0
    logged = json.loads(open(tmp_path / "posted.jsonl", encoding="utf-8").readline())
    assert logged["text"] == "the EXACT approved post text"   # verbatim


# --- queue-empty alert -------------------------------------------------------

def test_queue_empty_alert_fires_when_zero(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")   # empty -> 0 unused
    monkeypatch.setenv("QUEUE_PATH", queue)
    state = str(tmp_path / "state.json")
    posts = []
    monkeypatch.setattr(cr, "_post_slack_web", lambda text, token, channel, timeout=15: (posts.append(text) or "1"))
    fired = cr.queue_empty_alert(token="xoxb", channel="C", dry_run=False, state_path=state, now=1000.0)
    assert fired is True and any("QUEUE EMPTY" in p for p in posts)
    # cooldown: a second immediate call does NOT re-alert
    assert cr.queue_empty_alert(token="xoxb", channel="C", dry_run=False, state_path=state, now=1001.0) is False


def test_queue_empty_alert_silent_when_queue_has_entries(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    content_queue.append_entry(raw="something", type="moment", path=queue)  # 1 unused
    assert cr.queue_empty_alert(token="xoxb", channel="C", dry_run=False,
                                state_path=str(tmp_path / "s.json"), now=1000.0) is False


# --- SPEC-content-refill v1.1: rotation-blocked (silent-deadlock) alert ------

def _queue_with(monkeypatch, tmp_path, entries):
    q = str(tmp_path / "q.jsonl")
    monkeypatch.setenv("QUEUE_PATH", q)
    for e in entries:
        content_queue.append_entry(raw=e["raw"], type=e.get("type", "moment"),
                                   pillar=e["pillar"], final_text=e["raw"], path=q)
    return q


def test_rotation_blocked_alert_fires_when_only_meta_queued(monkeypatch, tmp_path):
    # Today's bug: the only unused entry is META, last post was meta -> get_next_rotated
    # returns None -> post_tick skips silently. Now it alerts.
    _queue_with(monkeypatch, tmp_path, [{"raw": "a meta bit", "pillar": "meta"}])
    posts = []
    monkeypatch.setattr(cr, "_post_slack_web", lambda t, tok, ch, timeout=15: (posts.append(t) or "1"))
    fired = cr.queue_rotation_blocked_alert(["meta"], token="x", channel="C",
                                            dry_run=False, state_path=str(tmp_path / "s.json"), now=1000.0)
    assert fired is True
    assert "ROTATION-BLOCKED" in posts[0]
    assert "meta" in posts[0].lower() and "non-META" in posts[0]   # names the blocked pillar + the fix


def test_rotation_alert_silent_when_something_postable(monkeypatch, tmp_path):
    _queue_with(monkeypatch, tmp_path, [{"raw": "a showcase", "pillar": "showcase"}])
    posts = []
    monkeypatch.setattr(cr, "_post_slack_web", lambda *a, **k: (posts.append(1) or "1"))
    fired = cr.queue_rotation_blocked_alert(["meta"], token="x", channel="C",
                                            dry_run=False, state_path=str(tmp_path / "s.json"), now=1000.0)
    assert fired is False and posts == []   # showcase IS postable after a meta post -> healthy


def test_rotation_alert_silent_when_queue_empty(monkeypatch, tmp_path):
    _queue_with(monkeypatch, tmp_path, [])   # empty -> queue_empty_alert owns it
    fired = cr.queue_rotation_blocked_alert([], token="x", channel="C",
                                            dry_run=False, state_path=str(tmp_path / "s.json"), now=1000.0)
    assert fired is False


def test_rotation_alert_no_repeat_spam(monkeypatch, tmp_path):
    _queue_with(monkeypatch, tmp_path, [{"raw": "meta bit", "pillar": "meta"}])
    posts = []
    monkeypatch.setattr(cr, "_post_slack_web", lambda *a, **k: (posts.append(1) or "1"))
    s = str(tmp_path / "s.json")
    assert cr.queue_rotation_blocked_alert(["meta"], "x", "C", dry_run=False, state_path=s, now=1000.0) is True
    # same blocked state, within cooldown -> NO re-alert (not every heartbeat)
    assert cr.queue_rotation_blocked_alert(["meta"], "x", "C", dry_run=False, state_path=s, now=1500.0) is False
    assert len(posts) == 1
    # cooldown elapsed -> re-nag (don't let a week vanish)
    later = 1000.0 + cr.EMPTY_ALERT_COOLDOWN_S + 1
    assert cr.queue_rotation_blocked_alert(["meta"], "x", "C", dry_run=False, state_path=s, now=later) is True
    assert len(posts) == 2


def test_rotation_alert_healthy_clears_marker(monkeypatch, tmp_path):
    s = str(tmp_path / "s.json")
    q = _queue_with(monkeypatch, tmp_path, [{"raw": "meta bit", "pillar": "meta"}])
    monkeypatch.setattr(cr, "_post_slack_web", lambda *a, **k: "1")
    cr.queue_rotation_blocked_alert(["meta"], "x", "C", dry_run=False, state_path=s, now=1000.0)
    assert "last_blocked_sig" in json.load(open(s, encoding="utf-8"))
    # add a substance item -> now postable -> the healthy branch clears the marker
    content_queue.append_entry(raw="a showcase", type="moment", pillar="showcase", final_text="a showcase", path=q)
    cr.queue_rotation_blocked_alert(["meta"], "x", "C", dry_run=False, state_path=s, now=1200.0)
    assert "last_blocked_sig" not in json.load(open(s, encoding="utf-8"))


# --- #2 refill-side bias: batch is <=1 META + >=4 substance (already enforced) ---

def test_batch_is_at_most_one_meta_four_substance():
    for recent in ([], ["meta"], ["showcase"], ["meta", "showcase", "question", "operator"], ["showcase", "meta"]):
        seq = cr.select_pillars(5, recent)
        assert seq.count("meta") <= 1, (recent, seq)
        assert sum(1 for p in seq if p != "meta") >= 4, (recent, seq)
