"""
frontier.py — the curated frontier watchlist (SPEC-v7).

A two-tier watchlist (frontier-watchlist.json) of high-signal accounts:
  - AUTO-FOLLOW them (reusing the autonomous-follow's shared daily cap + pacing).
  - WEIGHT their posts higher in scout ranking (rank.py reads the boosts here).
  - FEED a review-only monitoring stream to the frontier Slack channel:
      study_closely -> EVERY post; high_signal -> notable/on-mission posts.

Design: the top of this module stays LIGHT (no bluesky/act import) so rank.py can
import it just for the tier lookup + boosts. The follow path lazy-imports bluesky.
Everything is fail-safe: a missing/malformed watchlist degrades to empty, and the
feed/follow steps are meant to be wrapped by their caller so they never break a tick.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Set

import guard

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(HERE, "frontier-watchlist.json")
FOLLOWED_PATH = os.path.join(HERE, "frontier-followed.jsonl")
SEEN_PATH = os.path.join(HERE, "frontier-seen.jsonl")
ACT_LOG = os.path.join(HERE, "act-log.jsonl")  # shared follow-budget ledger (with act_tick)

DEFAULT_FRONTIER_CHANNEL = "C0BGH90UKM2"
SLACK_POST_MESSAGE_EP = "https://slack.com/api/chat.postMessage"

STUDY_CLOSELY = "study_closely"
HIGH_SIGNAL = "high_signal"

# Ranking boosts (SPEC-v7 weight) — added by rank.py. study_closely > high_signal.
STUDY_CLOSELY_BOOST = 6.0
HIGH_SIGNAL_BOOST = 3.0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _norm(handle: str) -> str:
    return str(handle or "").strip().lstrip("@").lower()


# ---------------------------------------------------------------------------
# Watchlist load (fail-safe) + tier lookup
# ---------------------------------------------------------------------------

_cache: Optional[Dict[str, str]] = None


def load_watchlist(path: Optional[str] = None) -> Dict[str, str]:
    """
    Return {normalized_handle: tier}. Malformed/missing file -> {} (degrade, never crash).
    """
    target = path or WATCHLIST_PATH
    if not os.path.exists(target):
        return {}
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        tiers = (data or {}).get("tiers") or {}
        out: Dict[str, str] = {}
        for tier_name in (STUDY_CLOSELY, HIGH_SIGNAL):
            for h in (tiers.get(tier_name) or {}).get("handles", []) or []:
                nh = _norm(h)
                if nh:
                    out[nh] = tier_name  # study_closely listed first wins if duplicated
        return out
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        print(f"[frontier] watchlist load failed ({exc}); using empty watchlist.")
        return {}


def get_watchlist() -> Dict[str, str]:
    """Cached watchlist (loaded once per process) for the hot ranking path."""
    global _cache
    if _cache is None:
        _cache = load_watchlist()
    return _cache


def tier_of(handle: str, watchlist: Optional[Dict[str, str]] = None) -> Optional[str]:
    wl = watchlist if watchlist is not None else get_watchlist()
    return wl.get(_norm(handle))


def is_watchlisted(handle: str, watchlist: Optional[Dict[str, str]] = None) -> bool:
    return tier_of(handle, watchlist) is not None


def boost_for(handle: str, watchlist: Optional[Dict[str, str]] = None) -> float:
    """Ranking boost for a handle's tier (0 if not watchlisted)."""
    t = tier_of(handle, watchlist)
    if t == STUDY_CLOSELY:
        return STUDY_CLOSELY_BOOST
    if t == HIGH_SIGNAL:
        return HIGH_SIGNAL_BOOST
    return 0.0


# ---------------------------------------------------------------------------
# Auto-follow (shared follow budget + pacing; fail-closed on the self-label)
# ---------------------------------------------------------------------------


def _followed_handles(path: Optional[str] = None) -> Set[str]:
    target = path or FOLLOWED_PATH
    seen: Set[str] = set()
    if not os.path.exists(target):
        return seen
    try:
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("handle"):
                        seen.add(_norm(rec["handle"]))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return seen


def _follows_today(act_log_path: Optional[str] = None) -> int:
    """Count executed follows recorded today (shared budget with act_tick)."""
    target = act_log_path or ACT_LOG
    if not os.path.exists(target):
        return 0
    today = _today()
    n = 0
    with open(target, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "executed" and rec.get("action") == "follow" \
                    and str(rec.get("ts", "")).startswith(today):
                n += 1
    return n


def _append_jsonl(path: str, record: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _follow_cap() -> int:
    try:
        return int(os.environ.get("ACT_CAP_FOLLOW", "10") or "10")
    except ValueError:
        return 10


def _pacing() -> float:
    try:
        return max(0.0, float(os.environ.get("ACT_PACING_SECONDS", "20") or "20"))
    except ValueError:
        return 20.0


def follow_watchlist(
    session: Dict[str, Any],
    watchlist: Optional[Dict[str, str]] = None,
    dry_run: bool = False,
    followed_path: Optional[str] = None,
    act_log_path: Optional[str] = None,
    own_did: Optional[str] = None,
) -> int:
    """
    Follow not-yet-followed watchlist accounts, preferential order (study_closely first),
    within the SHARED daily follow cap + pacing. Returns the number followed this tick.

    Fail-closed on I-BOT-DISCLOSED: confirm the bot self-label first; if it can't be
    confirmed, follow NOTHING this tick. I-NO-SELF: never follow our own DID. I-DEDUP +
    I-LOGGED: each follow recorded in act-log (shared budget) + frontier-followed.jsonl.
    Never raises on the normal paths.
    """
    wl = watchlist if watchlist is not None else get_watchlist()
    if not wl:
        return 0
    followed_path = followed_path or FOLLOWED_PATH
    act_log_path = act_log_path or ACT_LOG

    import bluesky  # lazy — keeps the module (and rank.py) light

    own_did = own_did if own_did is not None else (session or {}).get("did")

    # Safety basis for autonomous follow (SPEC-v3): confirm the bot self-label.
    if not dry_run:
        try:
            res = bluesky.ensure_bot_label(session)
            print(f"[frontier] I-BOT-DISCLOSED: bot self-label {res.get('status')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[frontier] could NOT confirm bot self-label ({exc!r}); no watchlist follows this tick (fail-closed).")
            return 0

    already = _followed_handles(followed_path)
    remaining = _follow_cap() - _follows_today(act_log_path)
    if remaining <= 0:
        print("[frontier] daily follow cap already reached; deferring watchlist follows.")
        return 0

    # Preferential order: study_closely first, then high_signal.
    pending = [h for h, t in wl.items() if t == STUDY_CLOSELY and h not in already]
    pending += [h for h, t in wl.items() if t == HIGH_SIGNAL and h not in already]

    followed = 0
    pacing = _pacing()
    for handle in pending:
        if followed >= remaining:
            print(f"[frontier] hit remaining follow budget ({remaining}); rest deferred to next tick.")
            break
        did = bluesky.resolve_handle(handle, session=session)
        if not did:
            # Unresolvable (renamed/deleted) — record so we don't retry endlessly.
            if not dry_run:
                _append_jsonl(followed_path, {"handle": handle, "did": None, "status": "unresolved", "ts": _now_iso()})
            continue
        if own_did and did == own_did:
            print(f"[frontier] skip (I-NO-SELF): {handle}")
            if not dry_run:
                _append_jsonl(followed_path, {"handle": handle, "did": did, "status": "self_skip", "ts": _now_iso()})
            continue

        if dry_run:
            print(f"[frontier] DRY_RUN would follow @{handle} ({did})")
            followed += 1
            continue

        if followed > 0 and pacing > 0:
            time.sleep(pacing)
        try:
            bluesky.follow(did, session=session)
        except Exception as exc:  # noqa: BLE001 — one bad follow must not crash the tick
            print(f"[frontier] follow failed for @{handle}: {exc!r}")
            continue
        # Shared follow-budget ledger (act-log) + frontier dedup ledger.
        _append_jsonl(act_log_path, {
            "ts": _now_iso(), "action": "follow", "status": "executed",
            "author_did": did, "source": "frontier", "approval_mode": "auto:frontier-watchlist",
        })
        _append_jsonl(followed_path, {"handle": handle, "did": did, "status": "followed", "ts": _now_iso()})
        followed += 1
        print(f"[frontier] followed @{handle} ({did})")

    return followed


# ---------------------------------------------------------------------------
# Monitoring feed (review-only) — study_closely: all; high_signal: notable/on-mission
# ---------------------------------------------------------------------------

_TIER_LABEL = {STUDY_CLOSELY: "STUDY-CLOSELY", HIGH_SIGNAL: "HIGH-SIGNAL"}


def format_card(tier: str, item: Dict[str, Any]) -> Optional[str]:
    """Render a monitoring card. None if no permalink (nothing to link to)."""
    handle = _norm(item.get("author_handle", ""))
    url = str(item.get("url", "")).strip()
    text = str(item.get("text", "")).strip()
    if not handle or not url:
        return None
    if len(text) > 500:
        text = text[:497] + "…"
    return (
        f"*🔭 FRONTIER · {_TIER_LABEL.get(tier, tier)}* · _review-only monitoring_\n"
        f"@{handle}:\n> {text}\n→ {url}"
    )


def load_seen(path: Optional[str] = None) -> Set[str]:
    target = path or SEEN_PATH
    seen: Set[str] = set()
    if not os.path.exists(target):
        return seen
    try:
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("uri"):
                        seen.add(rec["uri"])
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return seen


def _mark_seen(item: Dict[str, Any], status: str, path: str) -> None:
    _append_jsonl(path, {"uri": item.get("uri"), "author_handle": _norm(item.get("author_handle", "")),
                         "status": status, "ts": _now_iso()})


def _post_slack(text: str, token: str, channel: str, timeout: int = 15) -> bool:
    import requests  # lazy
    try:
        resp = requests.post(
            SLACK_POST_MESSAGE_EP,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json={"channel": channel, "text": text},
            timeout=timeout,
        )
        data = resp.json() if resp.content else {}
    except Exception as exc:  # noqa: BLE001
        print(f"[frontier] Slack post error (non-fatal): {exc}")
        return False
    if not data.get("ok"):
        print(f"[frontier] Slack post rejected for channel {channel}: {data.get('error')}")
        return False
    return True


def feed_candidates(
    candidates: List[Dict[str, Any]],
    watchlist: Optional[Dict[str, str]] = None,
    on_mission_uris: Optional[Set[str]] = None,
    dry_run: bool = False,
    token: Optional[str] = None,
    channel: Optional[str] = None,
    seen_path: Optional[str] = None,
) -> int:
    """
    Post watchlist activity to the frontier channel (REVIEW-ONLY). study_closely -> EVERY
    post; high_signal -> only notable/on-mission (uri in on_mission_uris). Deduped +
    guard-checked (fail-closed). Returns the number posted. Never raises on normal paths;
    takes NO action and writes NO brain/memory.
    """
    wl = watchlist if watchlist is not None else get_watchlist()
    if not wl or not candidates:
        return 0
    on_mission_uris = on_mission_uris or set()
    token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = channel or os.environ.get("SLACK_CHANNEL_FRONTIER", "").strip() or DEFAULT_FRONTIER_CHANNEL
    seen_path = seen_path or SEEN_PATH

    seen = load_seen(seen_path)
    posted = 0
    for cand in candidates:
        uri = cand.get("uri")
        if not uri or uri in seen:
            continue
        tier = tier_of(cand.get("author_handle", ""), wl)
        if tier is None:
            continue
        # study_closely: every post. high_signal: only notable/on-mission.
        if tier == HIGH_SIGNAL and uri not in on_mission_uris:
            continue

        card = format_card(tier, cand)
        if card is None:
            if not dry_run:
                _mark_seen(cand, "no_permalink", seen_path)
            continue
        # I-PRIVACY: guard the card fail-closed (their post could mention our terms).
        ok, reason = guard.check(card)
        if not ok:
            print(f"[frontier] GUARD BLOCKED frontier card from {uri}: {reason}")
            if not dry_run:
                _mark_seen(cand, "guard_blocked", seen_path)
            continue

        if dry_run:
            print("---- [DRY_RUN] frontier monitoring card ----")
            print(card)
            continue

        if token and channel and _post_slack(card, token, channel):
            _mark_seen(cand, "posted", seen_path)
            posted += 1
        else:
            _mark_seen(cand, "post_failed", seen_path)
    return posted
