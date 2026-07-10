"""
tests/test_ops_insight.py — the Ops-Insight Harvest (SPEC-v6).

No network: generate.complete / post_brief are monkeypatched, the seen ledger is a
tmp file, the privacy guard is REAL (so the fail-closed test can't be faked).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ops_insight as oi  # noqa: E402


# --- normalize_item ---------------------------------------------------------

def test_normalize_builds_permalink_and_keeps_fields():
    it = oi.normalize_item({"uri": "at://did:plc:x/app.bsky.feed.post/rk", "text": "a technique",
                            "author_handle": "builder.bsky.social"})
    assert it["url"] == "https://bsky.app/profile/builder.bsky.social/post/rk"
    assert it["text"] == "a technique"


def test_normalize_drops_without_uri_or_text():
    assert oi.normalize_item({"text": "hi"}) is None
    assert oi.normalize_item({"uri": "at://x"}) is None
    assert oi.normalize_item({"uri": "at://x", "text": "  "}) is None


# --- flag parsing / no-model safe-degrade -----------------------------------

def test_parse_flag_batch_maps_indices_and_defaults_false():
    raw = '[{"index":0,"ops_insight":true},{"index":2,"ops_insight":true}]'
    assert oi.parse_flag_batch(raw, 3) == [True, False, True]


def test_parse_flag_batch_garbage_is_all_false():
    assert oi.parse_flag_batch("not json", 3) == [False, False, False]
    assert oi.parse_flag_batch("", 2) == [False, False]


def test_flag_items_no_model_flags_nothing(monkeypatch):
    # generate.complete returns "" when no key / dry-run -> flag nothing (safe-degrade).
    monkeypatch.setattr(oi.generate, "complete", lambda *a, **k: "")
    assert oi.flag_items([{"text": "x"}, {"text": "y"}]) == [False, False]


# --- extract parsing --------------------------------------------------------

def test_parse_brief_valid():
    raw = '{"insight":"use spec invariants","applies":"our framework","effect":"less slop","why_improves":"tighter"}'
    b = oi.parse_brief(raw)
    assert b["insight"] == "use spec invariants"
    assert b["applies"] == "our framework"


def test_parse_brief_empty_insight_is_decline():
    assert oi.parse_brief('{"insight":""}') is None   # the extractor declined
    assert oi.parse_brief("garbage") is None


# --- format_brief: PROVENANCE required (I-PROVENANCE) -----------------------

def _brief():
    return {"insight": "X", "applies": "Y", "effect": "Z", "why_improves": "W"}


def test_format_brief_includes_provenance():
    out = oi.format_brief(_brief(), {"author_handle": "b.bsky.social", "url": "https://bsky.app/x"})
    assert "b.bsky.social" in out and "https://bsky.app/x" in out
    assert "OPS-INSIGHT" in out


def test_format_brief_without_provenance_is_none():
    assert oi.format_brief(_brief(), {"author_handle": "", "url": "https://x"}) is None
    assert oi.format_brief(_brief(), {"author_handle": "b", "url": ""}) is None


# --- harvest orchestration --------------------------------------------------

def _item(uri, text="a real agentic technique", handle="b.bsky.social"):
    return {"uri": uri, "text": text, "author_handle": handle,
            "url": f"https://bsky.app/profile/{handle}/post/{uri[-3:]}"}


def _wire_harvest(monkeypatch, flags, brief=None):
    posted = []
    monkeypatch.setattr(oi, "flag_items", lambda items, model=None: flags)
    monkeypatch.setattr(oi, "extract_brief", lambda it, mid=None: (brief if brief is not None else _brief()))
    monkeypatch.setattr(oi, "post_brief", lambda text, token, channel, timeout=15: (posted.append((channel, text)) or True))
    return posted


def test_harvest_posts_only_flagged(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl")
    posted = _wire_harvest(monkeypatch, flags=[True, False])
    n = oi.harvest([_item("at://a1"), _item("at://a2")], dry_run=False,
                   token="xoxb", channel="C_OPS", seen_path=seen, log_path=str(tmp_path / "log.jsonl"))
    assert n == 1
    assert len(posted) == 1 and posted[0][0] == "C_OPS"
    rows = {json.loads(l)["uri"]: json.loads(l)["status"] for l in open(seen, encoding="utf-8") if l.strip()}
    assert rows["at://a1"] == "posted"
    assert rows["at://a2"] == "not_insight"


def test_harvest_dedups_seen(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl")
    with open(seen, "w", encoding="utf-8") as f:
        f.write(json.dumps({"uri": "at://dupe", "status": "posted"}) + "\n")
    posted = _wire_harvest(monkeypatch, flags=[True])
    n = oi.harvest([_item("at://dupe")], dry_run=False, token="x", channel="C", seen_path=seen, log_path=str(tmp_path / "log.jsonl"))
    assert n == 0 and posted == []   # already seen -> never re-surfaced (I-DEDUP)


def test_harvest_respects_max_per_tick(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl")
    posted = _wire_harvest(monkeypatch, flags=[True, True, True, True])
    items = [_item(f"at://f{i}") for i in range(4)]
    n = oi.harvest(items, dry_run=False, token="x", channel="C", seen_path=seen, log_path=str(tmp_path / "log.jsonl"), max_per_tick=2)
    assert n == 2   # hard cap on deep extracts/posts this tick


def test_harvest_guard_blocks_leaking_brief(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl")
    # A brief whose insight leaks a hardcoded-floor term -> REAL guard blocks it.
    leaking = {"insight": "shayler said to cache prompts", "applies": "", "effect": "", "why_improves": ""}
    posted = _wire_harvest(monkeypatch, flags=[True], brief=leaking)
    n = oi.harvest([_item("at://leak")], dry_run=False, token="x", channel="C", seen_path=seen, log_path=str(tmp_path / "log.jsonl"))
    assert n == 0 and posted == []    # never posted (I-PRIVACY fail-closed)
    rows = [json.loads(l) for l in open(seen, encoding="utf-8") if l.strip()]
    assert rows[0]["status"] == "guard_blocked"


def test_harvest_extract_decline_marks_seen(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl")
    posted = _wire_harvest(monkeypatch, flags=[True], brief=None)  # extractor declined
    # extract_brief patched to return None
    monkeypatch.setattr(oi, "extract_brief", lambda it, mid=None: None)
    n = oi.harvest([_item("at://x")], dry_run=False, token="x", channel="C", seen_path=seen, log_path=str(tmp_path / "log.jsonl"))
    assert n == 0 and posted == []
    assert [json.loads(l) for l in open(seen, encoding="utf-8") if l.strip()][0]["status"] == "extract_empty"


def test_harvest_dry_run_writes_nothing(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl")
    posted = _wire_harvest(monkeypatch, flags=[True])
    n = oi.harvest([_item("at://d")], dry_run=True, token="x", channel="C", seen_path=seen, log_path=str(tmp_path / "log.jsonl"))
    assert n == 0
    assert posted == []                       # no post
    assert not os.path.exists(seen)           # side-effect-free preview (no ledger write)


def test_harvest_never_imports_brain_or_memory():
    # I-NO-AUTO-BRAIN, structural: the module IMPORTS nothing that could write a
    # brain/memory/vector store — only generate (models), guard (privacy),
    # requests (Slack), and stdlib. (We scan import statements, not string
    # content — the flag prompt legitimately mentions "agent memory" as a topic.)
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops_insight.py"), encoding="utf-8").read()
    import_lines = [ln.strip() for ln in src.splitlines() if ln.strip().startswith(("import ", "from "))]
    banned = ("brain", "memory", "mem0", "sqlite", "chromadb", "pinecone", "faiss", "mind")
    for ln in import_lines:
        low = ln.lower()
        assert not any(b in low for b in banned), f"ops_insight imports a store — violates I-NO-AUTO-BRAIN: {ln!r}"
    # Positive: the only imports are the expected safe set.
    assert "import generate" in src and "import guard" in src


def test_harvest_only_persistent_writes_are_repo_ledgers(monkeypatch, tmp_path):
    # I-NO-AUTO-BRAIN, functional: a full posted harvest writes ONLY the repo
    # ledgers we hand it (the seen ledger + the log bridge) — no brain/memory, no
    # other file anywhere.
    seen = str(tmp_path / "seen.jsonl")
    _wire_harvest(monkeypatch, flags=[True])
    oi.harvest([_item("at://only")], dry_run=False, token="x", channel="C", seen_path=seen, log_path=str(tmp_path / "log.jsonl"))
    assert set(os.listdir(tmp_path)) == {"seen.jsonl", "log.jsonl"}


# --- LOG BRIDGE (SPEC-v6.1): mirror briefs to ops-intel-log.jsonl -----------

def test_stable_id_is_deterministic():
    assert oi._stable_id("at://x") == oi._stable_id("at://x")
    assert oi._stable_id("at://x") != oi._stable_id("at://y")


def test_append_log_writes_full_brief_with_provenance(tmp_path):
    log = str(tmp_path / "log.jsonl")
    item = {"uri": "at://p1", "author_handle": "b.bsky.social", "url": "https://bsky.app/x"}
    assert oi.append_log(_brief(), item, log) is True
    rec = json.loads(open(log, encoding="utf-8").readline())
    assert rec["source_uri"] == "at://p1"
    assert rec["id"] == oi._stable_id("at://p1")
    assert rec["author_handle"] == "b.bsky.social" and rec["link"] == "https://bsky.app/x"
    assert rec["insight"] == "X" and rec["applies"] == "Y" and rec["effect"] == "Z" and rec["why_improves"] == "W"
    assert rec["ts"]


def test_append_log_dedups_by_source_uri(tmp_path):
    log = str(tmp_path / "log.jsonl")
    item = {"uri": "at://dupe", "author_handle": "b", "url": "https://x"}
    assert oi.append_log(_brief(), item, log) is True
    assert oi.append_log(_brief(), item, log) is False   # logged once
    rows = [l for l in open(log, encoding="utf-8") if l.strip()]
    assert len(rows) == 1


def test_harvest_logs_posted_brief(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl"); log = str(tmp_path / "log.jsonl")
    _wire_harvest(monkeypatch, flags=[True])
    oi.harvest([_item("at://logme")], dry_run=False, token="x", channel="C", seen_path=seen, log_path=log)
    rec = json.loads(open(log, encoding="utf-8").readline())
    assert rec["source_uri"] == "at://logme" and rec["insight"]


def test_harvest_logs_even_when_slack_post_fails(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl"); log = str(tmp_path / "log.jsonl")
    monkeypatch.setattr(oi, "flag_items", lambda items, model=None: [True])
    monkeypatch.setattr(oi, "extract_brief", lambda it, mid=None: _brief())
    monkeypatch.setattr(oi, "post_brief", lambda *a, **k: False)   # Slack rejects (bot not in channel)
    oi.harvest([_item("at://failpost")], dry_run=False, token="x", channel="C", seen_path=seen, log_path=log)
    # The intel is still captured for the nightly even though Slack failed.
    assert json.loads(open(log, encoding="utf-8").readline())["source_uri"] == "at://failpost"


def test_harvest_dry_run_does_not_log(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl"); log = str(tmp_path / "log.jsonl")
    _wire_harvest(monkeypatch, flags=[True])
    oi.harvest([_item("at://d")], dry_run=True, token="x", channel="C", seen_path=seen, log_path=log)
    assert not os.path.exists(log)   # side-effect-free preview


def test_harvest_guard_blocked_not_logged(monkeypatch, tmp_path):
    seen = str(tmp_path / "seen.jsonl"); log = str(tmp_path / "log.jsonl")
    leaking = {"insight": "shayler's trick", "applies": "", "effect": "", "why_improves": ""}
    _wire_harvest(monkeypatch, flags=[True], brief=leaking)
    oi.harvest([_item("at://leak2")], dry_run=False, token="x", channel="C", seen_path=seen, log_path=log)
    assert not os.path.exists(log)   # a guard-blocked brief is never logged
