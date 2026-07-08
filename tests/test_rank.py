"""
tests/test_rank.py — opportunity-value ranking (SPEC-v3).

Pure: a fixed now_epoch makes recency deterministic. Proves the score is an
explainable sum of components and that items sort best-first.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rank  # noqa: E402

# A fixed "now" so recency is deterministic. 2026-07-08T12:00:00Z-ish epoch.
NOW = 1_783_512_000.0


def _item(**over):
    base = {
        "confidence": "med", "author_followers": 0, "indexed_at": "",
        "like_count": 0, "reply_count": 0, "repost_count": 0, "lane_id": "memory",
    }
    base.update(over)
    return base


def test_components_sum_to_score():
    score, comp = rank.score_item(_item(confidence="high", author_followers=999, like_count=10), now_epoch=NOW)
    assert abs(sum(comp.values()) - score) < 1e-6


def test_higher_confidence_scores_higher():
    hi, _ = rank.score_item(_item(confidence="high"), now_epoch=NOW)
    lo, _ = rank.score_item(_item(confidence="low"), now_epoch=NOW)
    assert hi > lo


def test_more_reach_and_engagement_scores_higher():
    big, _ = rank.score_item(_item(author_followers=100000, like_count=500, reply_count=50), now_epoch=NOW)
    small, _ = rank.score_item(_item(author_followers=1, like_count=0, reply_count=0), now_epoch=NOW)
    assert big > small


def test_recency_decays():
    fresh, _ = rank.score_item(_item(indexed_at="2026-07-08T11:30:00Z"), now_epoch=NOW)   # ~30 min old
    stale, _ = rank.score_item(_item(indexed_at="2026-07-05T12:00:00Z"), now_epoch=NOW)   # 3 days old
    assert fresh > stale


def test_missing_signals_do_not_crash():
    score, comp = rank.score_item({}, now_epoch=NOW)  # everything absent
    assert isinstance(score, float)
    assert comp["reach"] == 0.0 and comp["engagement"] == 0.0 and comp["recency"] == 0.0


def test_rank_items_sorts_best_first_and_annotates():
    items = [
        _item(confidence="low", author_followers=0),                       # weak
        _item(confidence="high", author_followers=50000, like_count=200),  # strong
        _item(confidence="med", author_followers=10),                      # mid
    ]
    ranked = rank.rank_items(items, now_epoch=NOW)
    scores = [r["rank_score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)     # best-first
    assert ranked[0]["confidence"] == "high"          # the strong one leads
    assert "rank_components" in ranked[0]             # annotated for logging


def test_rank_items_stable_for_equal_scores():
    a = _item(confidence="med", author_followers=0, indexed_at="")
    b = _item(confidence="med", author_followers=0, indexed_at="")
    a["author_handle"], b["author_handle"] = "a", "b"
    ranked = rank.rank_items([a, b], now_epoch=NOW)
    assert [r["author_handle"] for r in ranked] == ["a", "b"]  # original order preserved
