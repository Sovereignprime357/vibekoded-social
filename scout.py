"""
scout.py — the SEE step of the agentic loop (SPEC-v2.md).

Reads the LANES block from MISSION-FILTER.md, runs app.bsky.feed.searchPosts
for each lane's terms + tags (English, recent-first, incremental via `since`),
and returns shaped candidate posts with the noise already stripped:
  - our own account removed        (I-NO-SELF)
  - posts we've already seen removed (dedup, scout-seen.jsonl)
  - within-run duplicates collapsed

This module is READ-ONLY. It never writes a record to Bluesky, so it can't
cost write-points and can't take a public action. Judgment (TRIAGE) and
contact (SURFACE/ACT) are other modules' jobs.

Testability contract (same as the rest of the codebase): no network call
happens at import or in any test. The network lives in `scan()`; all the
filtering/shaping logic lives in `scan_from_results()`, which is pure and
fully unit-testable with canned search responses.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Set

import bluesky

HERE = os.path.dirname(os.path.abspath(__file__))
MISSION_PATH = os.path.join(HERE, "MISSION-FILTER.md")
STATE_PATH = os.path.join(HERE, "scout-state.json")
SEEN_PATH = os.path.join(HERE, "scout-seen.jsonl")

# On the very first run there's no last-scan marker; seed the window this far
# back so we get a useful first batch without dredging ancient posts.
DEFAULT_SEED_HOURS = int(os.environ.get("SCOUT_SEED_HOURS", "24"))
# Per (lane, term) search page size.
PER_TERM_LIMIT = int(os.environ.get("SCOUT_PER_TERM_LIMIT", "20"))
# Hard ceiling on candidates handed to TRIAGE per run (keeps triage cheap).
MAX_CANDIDATES = int(os.environ.get("SCOUT_MAX_CANDIDATES", "60"))
# Keep the dedup ledger from growing forever.
SEEN_RETENTION_DAYS = int(os.environ.get("SCOUT_SEEN_RETENTION_DAYS", "14"))


# ---------------------------------------------------------------------------
# Mission filter (lanes)
# ---------------------------------------------------------------------------

_LANES_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def load_lanes(mission_path: str = MISSION_PATH) -> List[Dict[str, Any]]:
    """
    Extract the `lanes` list from the first ```json block in MISSION-FILTER.md.

    Single source of truth: the operator edits MISSION-FILTER.md and both the
    searches (here) and the triage rubric (triage.py) follow — no second copy.
    A malformed or missing block is a build error worth failing loud on, not a
    runtime privacy risk, so we raise rather than fail-closed.
    """
    if not os.path.exists(mission_path):
        raise FileNotFoundError(f"MISSION-FILTER.md not found at {mission_path}")

    with open(mission_path, "r", encoding="utf-8") as f:
        text = f.read()

    m = _LANES_BLOCK_RE.search(text)
    if not m:
        raise ValueError("no ```json lanes block found in MISSION-FILTER.md")

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"lanes block in MISSION-FILTER.md is not valid JSON: {exc}") from exc

    lanes = data.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise ValueError("lanes block must contain a non-empty 'lanes' list")
    return lanes


# ---------------------------------------------------------------------------
# State: last-scan marker + seen-URI dedup ledger
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hours_ago_iso(hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))


def load_state(state_path: str = STATE_PATH) -> Dict[str, Any]:
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: Dict[str, Any], state_path: str = STATE_PATH) -> None:
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_seen(seen_path: str = SEEN_PATH) -> Set[str]:
    """Return the set of already-seen post URIs (dedup across runs)."""
    seen: Set[str] = set()
    if not os.path.exists(seen_path):
        return seen
    try:
        with open(seen_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    uri = rec.get("uri")
                    if uri:
                        seen.add(uri)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return seen


def append_seen(uris: List[str], seen_path: str = SEEN_PATH) -> None:
    """Append newly-seen URIs to the ledger, then prune entries older than retention."""
    if not uris:
        return
    now = _now_iso()
    with open(seen_path, "a", encoding="utf-8") as f:
        for uri in uris:
            f.write(json.dumps({"uri": uri, "ts": now}) + "\n")
    _prune_seen(seen_path)


def _prune_seen(seen_path: str = SEEN_PATH) -> None:
    """Drop seen-ledger rows older than SEEN_RETENTION_DAYS (best-effort)."""
    if not os.path.exists(seen_path):
        return
    cutoff = time.time() - SEEN_RETENTION_DAYS * 86400
    kept: List[str] = []
    try:
        with open(seen_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                keep = True
                if ts:
                    try:
                        row_epoch = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
                        keep = row_epoch >= cutoff
                    except (ValueError, OverflowError):
                        keep = True
                if keep:
                    kept.append(line)
    except OSError:
        return
    with open(seen_path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))


# ---------------------------------------------------------------------------
# Pure shaping / filtering (fully testable, no network)
# ---------------------------------------------------------------------------


def post_url(handle: str, uri: str) -> str:
    """Build the human bsky.app URL from an at:// post URI + author handle."""
    rkey = uri.rstrip("/").split("/")[-1] if uri else ""
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def shape_candidate(post: Dict[str, Any], lane: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Turn a raw searchPosts 'post view' into a lean candidate dict, or None if
    it's missing the fields we need (defensive — the appview occasionally
    returns partial views for deleted/blocked content).
    """
    uri = post.get("uri")
    cid = post.get("cid")
    author = post.get("author") or {}
    record = post.get("record") or {}
    handle = author.get("handle")
    text = record.get("text")
    if not uri or not cid or not handle or text is None:
        return None
    return {
        "uri": uri,
        "cid": cid,
        "author_handle": handle,
        "author_did": author.get("did", ""),
        "author_name": author.get("displayName", ""),
        "text": text,
        "lane_id": lane.get("id", ""),
        "lane_label": lane.get("label", ""),
        "indexed_at": post.get("indexedAt", ""),
        "url": post_url(handle, uri),
    }


def scan_from_results(
    results_by_lane: List[Dict[str, Any]],
    seen: Set[str],
    own_did: str = "",
    own_handle: str = "",
    max_candidates: int = MAX_CANDIDATES,
) -> List[Dict[str, Any]]:
    """
    Pure SEE logic: given fetched search results, produce the filtered,
    deduped, own-account-excluded candidate list. No network here so tests
    can drive the whole discovery pipeline with canned data.

    results_by_lane: list of {"lane": <lane dict>, "posts": [<post view>, ...]}
    seen: set of already-seen URIs (cross-run dedup)
    own_did / own_handle: our identity, for I-NO-SELF
    """
    own_handle_l = (own_handle or "").lower().lstrip("@")
    candidates: List[Dict[str, Any]] = []
    seen_this_run: Set[str] = set()

    for bucket in results_by_lane:
        lane = bucket.get("lane") or {}
        for post in bucket.get("posts") or []:
            cand = shape_candidate(post, lane)
            if cand is None:
                continue
            uri = cand["uri"]

            # I-NO-SELF: never our own posts.
            if own_did and cand["author_did"] == own_did:
                continue
            if own_handle_l and cand["author_handle"].lower().lstrip("@") == own_handle_l:
                continue

            # Dedup: seen in a prior run, or already collected this run.
            if uri in seen or uri in seen_this_run:
                continue

            seen_this_run.add(uri)
            candidates.append(cand)
            if len(candidates) >= max_candidates:
                return candidates

    return candidates


# ---------------------------------------------------------------------------
# Network scan (the only part that hits Bluesky)
# ---------------------------------------------------------------------------


def _fetch_lane(
    lane: Dict[str, Any],
    session: Dict[str, Any],
    since: str,
) -> List[Dict[str, Any]]:
    """
    Run searchPosts for every term in a lane (plus a tag-only search if the
    lane declares tags) and merge the post views. One failing term is logged
    and skipped — it never aborts the lane or the run.
    """
    posts: List[Dict[str, Any]] = []
    terms = list(lane.get("terms") or [])
    tags = list(lane.get("tags") or [])

    for term in terms:
        try:
            resp = bluesky.search_posts(
                q=term,
                session=session,
                limit=PER_TERM_LIMIT,
                sort="latest",
                lang="en",
                since=since,
            )
            posts.extend(resp.get("posts") or [])
        except bluesky.BlueskyError as exc:
            print(f"[scout] lane={lane.get('id')} term={term!r} search failed: {exc}")

    if tags:
        # A single tag-scoped search using the first term as the text anchor
        # (searchPosts requires a non-empty q; tags AND-filter on top).
        anchor = terms[0] if terms else lane.get("label", "")
        try:
            resp = bluesky.search_posts(
                q=anchor,
                session=session,
                limit=PER_TERM_LIMIT,
                sort="latest",
                lang="en",
                since=since,
                tags=tags,
            )
            posts.extend(resp.get("posts") or [])
        except bluesky.BlueskyError as exc:
            print(f"[scout] lane={lane.get('id')} tags={tags} search failed: {exc}")

    return posts


def scan(
    session: Optional[Dict[str, Any]] = None,
    mission_path: str = MISSION_PATH,
    persist: bool = True,
) -> List[Dict[str, Any]]:
    """
    Full SEE step: load lanes, search each, filter/shape/dedup, and (unless
    persist=False) advance the last-scan marker + record seen URIs.

    Returns the candidate list for TRIAGE. An empty list is a normal outcome
    (nothing new on-lane) — never an error.
    """
    session = session or bluesky.create_session()
    lanes = load_lanes(mission_path)

    state = load_state()
    since = state.get("last_scan") or _hours_ago_iso(DEFAULT_SEED_HOURS)
    seen = load_seen()

    own_did = session.get("did", "")
    own_handle = session.get("handle", "") or os.environ.get("BSKY_HANDLE", "")

    scan_started = _now_iso()
    results_by_lane: List[Dict[str, Any]] = []
    for lane in lanes:
        posts = _fetch_lane(lane, session, since)
        results_by_lane.append({"lane": lane, "posts": posts})

    candidates = scan_from_results(results_by_lane, seen, own_did, own_handle)

    if persist:
        append_seen([c["uri"] for c in candidates])
        state["last_scan"] = scan_started
        state["last_run_candidates"] = len(candidates)
        save_state(state)

    print(f"[scout] scanned {len(lanes)} lanes since {since} -> {len(candidates)} new candidate(s)")
    return candidates


if __name__ == "__main__":
    # Manual smoke run (needs real BSKY creds; prints candidates, persists state).
    for c in scan():
        print(f"  [{c['lane_id']}] @{c['author_handle']}: {c['text'][:80]!r}")
