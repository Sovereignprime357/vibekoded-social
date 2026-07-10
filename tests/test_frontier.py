"""
tests/test_frontier.py — the frontier watchlist (SPEC-v7): load/tier, ranking boost,
auto-follow (cap/pacing/dedup/no-self/self-label), and the review-only monitoring feed.

No network: bluesky (resolve/follow/ensure_bot_label) is monkeypatched, the guard is
REAL, ledgers are tmp files.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bluesky  # noqa: E402
import frontier  # noqa: E402
import rank  # noqa: E402

WL = {"a.bsky.social": "study_closely", "b.bsky.social": "high_signal", "c.bsky.social": "high_signal"}


# --- load / tier -------------------------------------------------------------

def test_load_watchlist_parses_tiers_and_normalizes(tmp_path):
    p = tmp_path / "wl.json"
    p.write_text(json.dumps({"tiers": {
        "study_closely": {"handles": ["@0coceo.bsky.social"]},
        "high_signal": {"handles": ["SimonWillison.net", "howard.fm"]},
    }}), encoding="utf-8")
    wl = frontier.load_watchlist(str(p))
    assert wl["0coceo.bsky.social"] == "study_closely"   # @ stripped
    assert wl["simonwillison.net"] == "high_signal"       # lowercased
    assert wl["howard.fm"] == "high_signal"


def test_load_watchlist_missing_or_malformed_is_empty(tmp_path):
    assert frontier.load_watchlist(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert frontier.load_watchlist(str(bad)) == {}


def test_tier_and_boost_lookup():
    assert frontier.tier_of("A.bsky.social", WL) == "study_closely"   # case-insensitive
    assert frontier.is_watchlisted("b.bsky.social", WL) is True
    assert frontier.is_watchlisted("rando.bsky.social", WL) is False
    assert frontier.boost_for("a.bsky.social", WL) == frontier.STUDY_CLOSELY_BOOST
    assert frontier.boost_for("b.bsky.social", WL) == frontier.HIGH_SIGNAL_BOOST
    assert frontier.boost_for("rando", WL) == 0.0


# --- ranking boost (SPEC-v7 weight) -----------------------------------------

def _cand(handle, **over):
    base = {"uri": f"at://{handle}", "author_handle": handle, "confidence": "med",
            "author_followers": 100, "indexed_at": "", "lane_id": "x"}
    base.update(over)
    return base


def test_watchlist_author_scores_higher_and_tiers_ordered():
    NOW = 1_783_512_000.0
    study, _ = rank.score_item(_cand("a.bsky.social"), NOW, WL)
    high, _ = rank.score_item(_cand("b.bsky.social"), NOW, WL)
    none_, comp = rank.score_item(_cand("rando.bsky.social"), NOW, WL)
    assert study > high > none_               # study_closely > high_signal > non-watchlist
    assert comp["frontier"] == 0.0            # boost logged as a component


def test_rank_items_surfaces_watchlist_first():
    NOW = 1_783_512_000.0
    # A non-watchlist item with otherwise-identical signals ranks below the watchlist one.
    items = [_cand("rando.bsky.social"), _cand("a.bsky.social")]
    # score_item uses the cached watchlist in rank_items; force our WL via monkeypatch-free
    # path by scoring directly (rank_items uses frontier.get_watchlist()).
    ranked = sorted(items, key=lambda it: -rank.score_item(it, NOW, WL)[0])
    assert ranked[0]["author_handle"] == "a.bsky.social"


# --- auto-follow -------------------------------------------------------------

def _wire_follow(monkeypatch, resolved):
    """resolved: {handle: did}. Returns (follows list)."""
    monkeypatch.setattr(bluesky, "ensure_bot_label", lambda session: {"status": "already_set"})
    monkeypatch.setattr(bluesky, "resolve_handle", lambda h, session=None: resolved.get(frontier._norm(h)))
    follows = []
    monkeypatch.setattr(bluesky, "follow", lambda did, session=None: (follows.append(did) or {"uri": "at://f"}))
    monkeypatch.setattr(frontier.time, "sleep", lambda s: None)
    return follows


def test_follow_watchlist_follows_within_cap_and_dedups(monkeypatch, tmp_path):
    monkeypatch.setenv("ACT_CAP_FOLLOW", "10")
    resolved = {"a.bsky.social": "did:a", "b.bsky.social": "did:b", "c.bsky.social": "did:c"}
    follows = _wire_follow(monkeypatch, resolved)
    fp = str(tmp_path / "followed.jsonl"); al = str(tmp_path / "act-log.jsonl")

    n = frontier.follow_watchlist({"did": "did:us"}, WL, dry_run=False,
                                  followed_path=fp, act_log_path=al, own_did="did:us")
    assert n == 3
    assert set(follows) == {"did:a", "did:b", "did:c"}
    # study_closely first (preferential order)
    assert follows[0] == "did:a"
    # act-log recorded executed follows (shared budget)
    al_rows = [json.loads(l) for l in open(al, encoding="utf-8") if l.strip()]
    assert all(r["action"] == "follow" and r["status"] == "executed" for r in al_rows)

    # Second run: all already followed -> dedup -> no new follows, no resolve needed.
    follows.clear()
    n2 = frontier.follow_watchlist({"did": "did:us"}, WL, dry_run=False,
                                   followed_path=fp, act_log_path=al, own_did="did:us")
    assert n2 == 0 and follows == []


def test_follow_watchlist_skips_own_account(monkeypatch, tmp_path):
    resolved = {"a.bsky.social": "did:us", "b.bsky.social": "did:b", "c.bsky.social": "did:c"}
    follows = _wire_follow(monkeypatch, resolved)
    n = frontier.follow_watchlist({"did": "did:us"}, WL, dry_run=False,
                                  followed_path=str(tmp_path / "f.jsonl"),
                                  act_log_path=str(tmp_path / "a.jsonl"), own_did="did:us")
    assert "did:us" not in follows          # I-NO-SELF
    assert n == 2


def test_follow_watchlist_respects_daily_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("ACT_CAP_FOLLOW", "5")
    al = tmp_path / "act-log.jsonl"
    # Pre-seed 5 follows today -> budget already spent.
    today = frontier._today()
    al.write_text("\n".join(json.dumps({"ts": f"{today}T00:00:0{i}Z", "action": "follow", "status": "executed"})
                            for i in range(5)) + "\n", encoding="utf-8")
    follows = _wire_follow(monkeypatch, {"a.bsky.social": "did:a"})
    n = frontier.follow_watchlist({"did": "did:us"}, WL, dry_run=False,
                                  followed_path=str(tmp_path / "f.jsonl"), act_log_path=str(al), own_did="did:us")
    assert n == 0 and follows == []          # cap reached


def test_follow_watchlist_fail_closed_without_self_label(monkeypatch, tmp_path):
    monkeypatch.setattr(bluesky, "ensure_bot_label", lambda session: (_ for _ in ()).throw(RuntimeError("no label")))
    follows = []
    monkeypatch.setattr(bluesky, "resolve_handle", lambda h, session=None: "did:x")
    monkeypatch.setattr(bluesky, "follow", lambda did, session=None: follows.append(did))
    n = frontier.follow_watchlist({"did": "did:us"}, WL, dry_run=False,
                                  followed_path=str(tmp_path / "f.jsonl"), act_log_path=str(tmp_path / "a.jsonl"))
    assert n == 0 and follows == []          # self-label unconfirmed -> no follows


# --- monitoring feed (review-only) ------------------------------------------

def _fcand(handle, uri, text="a frontier post", on=False):
    return {"uri": uri, "author_handle": handle, "text": text,
            "url": f"https://bsky.app/profile/{handle}/post/{uri[-2:]}"}


def _wire_feed(monkeypatch):
    posted = []
    monkeypatch.setattr(frontier, "_post_slack", lambda text, token, channel, timeout=15: (posted.append((channel, text)) or True))
    return posted


def test_feed_study_closely_posts_every_post(monkeypatch, tmp_path):
    posted = _wire_feed(monkeypatch)
    cands = [_fcand("a.bsky.social", "at://s1"), _fcand("a.bsky.social", "at://s2")]
    n = frontier.feed_candidates(cands, WL, on_mission_uris=set(), dry_run=False,
                                 token="x", channel="C_FR", seen_path=str(tmp_path / "seen.jsonl"))
    assert n == 2 and len(posted) == 2       # study_closely: every post, even off-mission
    assert all(p[0] == "C_FR" for p in posted)


def test_feed_high_signal_only_notable(monkeypatch, tmp_path):
    posted = _wire_feed(monkeypatch)
    cands = [_fcand("b.bsky.social", "at://h1"), _fcand("b.bsky.social", "at://h2")]
    # only h1 is on-mission
    n = frontier.feed_candidates(cands, WL, on_mission_uris={"at://h1"}, dry_run=False,
                                 token="x", channel="C_FR", seen_path=str(tmp_path / "seen.jsonl"))
    assert n == 1
    assert "at://h1"[-2:] in posted[0][1] or "h1" in posted[0][1]


def test_feed_ignores_non_watchlist_and_dedups(monkeypatch, tmp_path):
    posted = _wire_feed(monkeypatch)
    seen = tmp_path / "seen.jsonl"
    seen.write_text(json.dumps({"uri": "at://dupe"}) + "\n", encoding="utf-8")
    cands = [_fcand("rando.bsky.social", "at://r1"),           # not watchlisted -> skip
             _fcand("a.bsky.social", "at://dupe")]             # already seen -> skip
    n = frontier.feed_candidates(cands, WL, dry_run=False, token="x", channel="C", seen_path=str(seen))
    assert n == 0 and posted == []


def test_feed_guard_blocks_leaking_card(monkeypatch, tmp_path):
    posted = _wire_feed(monkeypatch)
    # A watchlist post whose text leaks a hardcoded-floor term -> REAL guard blocks.
    cands = [_fcand("a.bsky.social", "at://leak", text="shayler taught me this trick")]
    n = frontier.feed_candidates(cands, WL, dry_run=False, token="x", channel="C", seen_path=str(tmp_path / "s.jsonl"))
    assert n == 0 and posted == []           # I-PRIVACY fail-closed
    rows = [json.loads(l) for l in open(tmp_path / "s.jsonl", encoding="utf-8") if l.strip()]
    assert rows[0]["status"] == "guard_blocked"


def test_feed_dry_run_writes_nothing(monkeypatch, tmp_path):
    posted = _wire_feed(monkeypatch)
    seen = str(tmp_path / "seen.jsonl")
    n = frontier.feed_candidates([_fcand("a.bsky.social", "at://d1")], WL, dry_run=True,
                                 token="x", channel="C", seen_path=seen)
    assert n == 0 and posted == []
    assert not os.path.exists(seen)          # side-effect-free preview
