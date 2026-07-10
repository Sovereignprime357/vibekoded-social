"""
rank.py — opportunity-value ranking for surfaced items (SPEC-v3).

The operator's replies are the scarce resource; sort surfaced items best-first so
that scarce attention lands on the highest-leverage rooms. The score is a simple,
EXPLAINABLE linear combination of cheap signals already in the triage/post data —
every component is logged on the item (item["rank_components"]) so a ranking is
never a black box.

Signals (each missing signal contributes 0, never crashes):
  - confidence : triage's high/med/low             -> 3 / 2 / 1
  - reach      : author follower count (log10)      -> log10(1 + followers)
  - recency    : how fresh the post is              -> 1.0 now → 0.0 at 48h+
  - engagement : likes + 2·replies + reposts (log)  -> log10(1 + weighted)
  - fit        : lane/wheelhouse matched at all      -> 1 if a lane is present

Weights are deliberately modest and readable; tune in WEIGHTS.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import frontier  # curated watchlist boosts (SPEC-v7); light module (no bluesky/act at import)

CONFIDENCE_POINTS = {"high": 3.0, "med": 2.0, "low": 1.0}

WEIGHTS = {
    "confidence": 2.0,
    "reach": 1.5,
    "recency": 2.0,
    "engagement": 1.5,
    "fit": 1.0,
}

# A post older than this contributes no recency value.
_RECENCY_HORIZON_H = 48.0


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _recency_score(indexed_at: str, now_epoch: Optional[float] = None) -> float:
    """1.0 for a just-now post decaying linearly to 0.0 at the horizon. 0 if unparseable."""
    if not indexed_at:
        return 0.0
    ts = str(indexed_at).strip().replace("Z", "+00:00")
    try:
        posted = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0
    now = now_epoch if now_epoch is not None else time.time()
    hours = max(0.0, (now - posted) / 3600.0)
    return max(0.0, 1.0 - hours / _RECENCY_HORIZON_H)


def score_item(item: Dict[str, Any], now_epoch: Optional[float] = None,
               watchlist: Optional[Dict[str, str]] = None) -> Tuple[float, Dict[str, float]]:
    """
    Return (score, components). Components are the WEIGHTED contributions, so
    they sum to the score and read as "where the score came from".

    `watchlist` (SPEC-v7): the frontier watchlist map; when the item's author is on
    it, a `frontier` boost is added (study_closely > high_signal) so pre-vetted
    frontier accounts rank higher. Passed in by rank_items (loaded once); defaults
    to the cached watchlist when called directly.
    """
    conf_raw = CONFIDENCE_POINTS.get(str(item.get("confidence", "low")).lower(), 1.0)
    reach_raw = math.log10(1.0 + _to_float(item.get("author_followers")))
    recency_raw = _recency_score(item.get("indexed_at", ""), now_epoch)
    engagement_raw = math.log10(
        1.0
        + _to_float(item.get("like_count"))
        + 2.0 * _to_float(item.get("reply_count"))
        + _to_float(item.get("repost_count"))
    )
    fit_raw = 1.0 if (item.get("lane_id") or item.get("lane_label")) else 0.0
    frontier_boost = frontier.boost_for(item.get("author_handle", ""), watchlist)

    components = {
        "confidence": round(WEIGHTS["confidence"] * conf_raw, 4),
        "reach": round(WEIGHTS["reach"] * reach_raw, 4),
        "recency": round(WEIGHTS["recency"] * recency_raw, 4),
        "engagement": round(WEIGHTS["engagement"] * engagement_raw, 4),
        "fit": round(WEIGHTS["fit"] * fit_raw, 4),
        "frontier": round(frontier_boost, 4),
    }
    score = round(sum(components.values()), 4)
    return score, components


def rank_items(items: List[Dict[str, Any]], now_epoch: Optional[float] = None) -> List[Dict[str, Any]]:
    """
    Return a NEW list of the items sorted best-first, each annotated with
    `rank_score` and `rank_components` (so the ordering is auditable and the
    score is persisted with the surfaced record). Stable for equal scores.
    """
    watchlist = frontier.get_watchlist()  # load once for the whole batch
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for i, item in enumerate(items):
        score, components = score_item(item, now_epoch, watchlist)
        enriched = dict(item)
        enriched["rank_score"] = score
        enriched["rank_components"] = components
        scored.append((score, i, enriched))
    # Sort by score desc; original index asc as the stable tiebreaker.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in scored]
