"""
tests/test_content_refill.py — the content refill loop (SPEC-content-refill-v1).

No network: generate.generate + act.get_reactions are monkeypatched, the privacy
guard is REAL, ledgers/queue are tmp files.
"""

import inspect
import json
import os
import sys

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


def test_enqueue_on_operator_thumbsup(tmp_path, monkeypatch):
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
    ledger = _surfaced_ledger(tmp_path)
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [])   # no reaction
    n = cr.poll_and_enqueue(token="xoxb", channel="C_REFILL", operator_id="U_OP",
                            dry_run=False, surfaced_path=ledger)
    assert n == 0 and not os.path.exists(queue)   # un-👍'd never enqueues


def test_enqueue_fail_closed_without_operator_id(tmp_path, monkeypatch):
    ledger = _surfaced_ledger(tmp_path)
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    # Even with a thumbsup present, no operator id -> nothing enqueues (fail-closed).
    monkeypatch.setattr(act, "get_reactions", lambda ch, ts, tok: [{"name": "+1", "users": ["U_OP"], "count": 1}])
    n = cr.poll_and_enqueue(token="xoxb", channel="C_REFILL", operator_id=None,
                            dry_run=False, surfaced_path=ledger)
    assert n == 0 and not os.path.exists(queue)


# --- post_tick posts the approved final_text VERBATIM (no re-generation) ----

def test_post_tick_posts_final_text_verbatim(tmp_path, monkeypatch):
    queue = str(tmp_path / "queue.jsonl")
    monkeypatch.setenv("QUEUE_PATH", queue)
    monkeypatch.setenv("DRY_RUN", "1")
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
