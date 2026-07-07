"""
tests/test_scout.py — the SEE step's pure logic, no network.

Drives scan_from_results() with canned searchPosts views so the whole
filter/shape/dedup/own-account pipeline is proven without a live Bluesky call.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scout  # noqa: E402


def _postview(uri, handle, text, did="did:plc:other", cid="cid1"):
    return {
        "uri": uri,
        "cid": cid,
        "author": {"did": did, "handle": handle, "displayName": handle.split(".")[0]},
        "record": {"text": text, "createdAt": "2026-07-07T00:00:00Z"},
        "indexedAt": "2026-07-07T00:00:00Z",
    }


LANE = {"id": "memory", "label": "Agent memory", "terms": ["agent memory"], "tags": []}


# --- load_lanes ------------------------------------------------------------

def test_load_lanes_parses_json_block(tmp_path):
    p = tmp_path / "MISSION-FILTER.md"
    p.write_text(
        "# Mission\n\n```json\n"
        '{"lanes": [{"id": "a", "label": "A", "terms": ["x"], "tags": []}]}\n'
        "```\n\nsome prose after\n",
        encoding="utf-8",
    )
    lanes = scout.load_lanes(str(p))
    assert len(lanes) == 1
    assert lanes[0]["id"] == "a"


def test_load_lanes_missing_file_raises(tmp_path):
    try:
        scout.load_lanes(str(tmp_path / "nope.md"))
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_load_lanes_no_block_raises(tmp_path):
    p = tmp_path / "MISSION-FILTER.md"
    p.write_text("# Mission\n\nno json block here\n", encoding="utf-8")
    try:
        scout.load_lanes(str(p))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_repo_mission_filter_lanes_load():
    """The real shipped MISSION-FILTER.md must parse and declare lanes."""
    lanes = scout.load_lanes()
    assert isinstance(lanes, list) and len(lanes) >= 1
    for lane in lanes:
        assert "id" in lane and "terms" in lane


# --- post_url --------------------------------------------------------------

def test_post_url_builds_from_rkey():
    url = scout.post_url("alice.bsky.social", "at://did:plc:x/app.bsky.feed.post/abc123")
    assert url == "https://bsky.app/profile/alice.bsky.social/post/abc123"


# --- shape_candidate -------------------------------------------------------

def test_shape_candidate_happy_path():
    pv = _postview("at://d/app.bsky.feed.post/1", "bob.bsky.social", "hi there")
    cand = scout.shape_candidate(pv, LANE)
    assert cand["author_handle"] == "bob.bsky.social"
    assert cand["text"] == "hi there"
    assert cand["lane_id"] == "memory"
    assert cand["url"].endswith("/post/1")


def test_shape_candidate_missing_fields_returns_none():
    assert scout.shape_candidate({"uri": "at://d/x/1"}, LANE) is None  # no author/record
    assert scout.shape_candidate({}, LANE) is None


# --- scan_from_results -----------------------------------------------------

def test_scan_filters_own_account_by_did():
    results = [{"lane": LANE, "posts": [
        _postview("at://d/app.bsky.feed.post/1", "me.bsky.social", "mine", did="did:plc:me"),
        _postview("at://d/app.bsky.feed.post/2", "stranger.bsky.social", "theirs", did="did:plc:them"),
    ]}]
    cands = scout.scan_from_results(results, seen=set(), own_did="did:plc:me")
    uris = [c["uri"] for c in cands]
    assert "at://d/app.bsky.feed.post/1" not in uris
    assert "at://d/app.bsky.feed.post/2" in uris


def test_scan_filters_own_account_by_handle():
    results = [{"lane": LANE, "posts": [
        _postview("at://d/app.bsky.feed.post/1", "vibekoded.bsky.social", "mine", did="did:plc:me"),
    ]}]
    cands = scout.scan_from_results(results, seen=set(), own_handle="vibekoded.bsky.social")
    assert cands == []


def test_scan_dedup_against_seen():
    results = [{"lane": LANE, "posts": [
        _postview("at://d/app.bsky.feed.post/1", "a.bsky.social", "one"),
        _postview("at://d/app.bsky.feed.post/2", "b.bsky.social", "two"),
    ]}]
    cands = scout.scan_from_results(results, seen={"at://d/app.bsky.feed.post/1"})
    assert [c["uri"] for c in cands] == ["at://d/app.bsky.feed.post/2"]


def test_scan_dedup_within_run():
    dup = _postview("at://d/app.bsky.feed.post/1", "a.bsky.social", "one")
    results = [
        {"lane": LANE, "posts": [dup]},
        {"lane": {"id": "b", "label": "B"}, "posts": [dup]},  # same post, different lane
    ]
    cands = scout.scan_from_results(results, seen=set())
    assert len(cands) == 1


def test_scan_respects_max_candidates():
    posts = [_postview(f"at://d/app.bsky.feed.post/{i}", f"u{i}.bsky.social", f"t{i}") for i in range(10)]
    results = [{"lane": LANE, "posts": posts}]
    cands = scout.scan_from_results(results, seen=set(), max_candidates=3)
    assert len(cands) == 3


# --- seen ledger round-trip ------------------------------------------------

def test_seen_ledger_roundtrip(tmp_path):
    p = str(tmp_path / "seen.jsonl")
    scout.append_seen(["at://d/x/1", "at://d/x/2"], seen_path=p)
    seen = scout.load_seen(p)
    assert seen == {"at://d/x/1", "at://d/x/2"}


def test_load_seen_missing_file_is_empty(tmp_path):
    assert scout.load_seen(str(tmp_path / "nope.jsonl")) == set()


def test_state_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    scout.save_state({"last_scan": "2026-07-07T00:00:00Z"}, p)
    assert scout.load_state(p)["last_scan"] == "2026-07-07T00:00:00Z"
