"""
bluesky.py — AT Protocol client (app-password auth), session-cached.

Per SPEC.md EDGE CASES: "Login/API failure -> reuse the cached session
token; only alert if createSession itself fails (login cap ~300/day)." The
real rate limit that matters here is LOGIN (createSession), not posting
volume — so this module caches the accessJwt/refreshJwt pair to a local
file (.session.json) and only calls createSession when there is no valid
cached session or the cached one has expired/been rejected.

No network calls happen at import time or in any test — every function
here that hits the network takes explicit arguments and is only invoked
by the entrypoint scripts or smoke_test.py (opt-in, real credentials only).
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://bsky.social/xrpc"

SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".session.json")

CREATE_SESSION_EP = f"{BASE_URL}/com.atproto.server.createSession"
REFRESH_SESSION_EP = f"{BASE_URL}/com.atproto.server.refreshSession"
CREATE_RECORD_EP = f"{BASE_URL}/com.atproto.repo.createRecord"
GET_RECORD_EP = f"{BASE_URL}/com.atproto.repo.getRecord"
PUT_RECORD_EP = f"{BASE_URL}/com.atproto.repo.putRecord"
LIST_NOTIFICATIONS_EP = f"{BASE_URL}/app.bsky.notification.listNotifications"
SEARCH_POSTS_EP = f"{BASE_URL}/app.bsky.feed.searchPosts"


class BlueskyError(Exception):
    """Raised on unrecoverable Bluesky API failures (after any retry/refresh)."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    try:
        resp = requests.request(method, url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise BlueskyError(f"network error calling {method} {url}: {exc}") from exc

    if resp.status_code >= 400:
        raise BlueskyError(f"HTTP {resp.status_code} from {method} {url}: {resp.text}")

    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise BlueskyError(f"non-JSON response from {method} {url}: {resp.text[:200]!r}") from exc


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------


def _load_cached_session(session_path: str = SESSION_PATH) -> Optional[Dict[str, Any]]:
    if not os.path.exists(session_path):
        return None
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        required = {"accessJwt", "refreshJwt", "did", "handle"}
        if not required.issubset(data.keys()):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _save_session(session: Dict[str, Any], session_path: str = SESSION_PATH) -> None:
    to_write = {
        "accessJwt": session.get("accessJwt"),
        "refreshJwt": session.get("refreshJwt"),
        "did": session.get("did"),
        "handle": session.get("handle"),
        "cachedAt": time.time(),
    }
    with open(session_path, "w", encoding="utf-8") as f:
        json.dump(to_write, f, indent=2)


def create_session(
    handle: Optional[str] = None,
    app_password: Optional[str] = None,
    force: bool = False,
    session_path: str = SESSION_PATH,
) -> Dict[str, Any]:
    """
    Return a valid session dict with at least accessJwt/refreshJwt/did/handle.

    Order of operations (this is the login-cap-conscious path):
      1. Unless force=True, try the cached session file first.
      2. If a cached session exists, try refreshSession (refresh does NOT
         count against the createSession login cap in the same way — it's
         a distinct, higher-volume-tolerant endpoint).
      3. Only if there's no cache, or refresh fails, call createSession
         (the ~300/day-capped login) with handle + app_password.

    Raises BlueskyError if createSession itself fails (per SPEC: "only
    alert if createSession itself fails").
    """
    handle = handle or os.environ.get("BSKY_HANDLE", "").strip()
    app_password = app_password or os.environ.get("BSKY_APP_PASSWORD", "").strip()

    if not force:
        cached = _load_cached_session(session_path)
        if cached:
            refreshed = _try_refresh(cached, session_path)
            if refreshed:
                return refreshed
            # Refresh failed (expired refresh token, revoked, etc.) — fall
            # through to a fresh login below.

    if not handle or not app_password:
        raise BlueskyError(
            "BSKY_HANDLE / BSKY_APP_PASSWORD not set and no usable cached session; "
            "cannot create a Bluesky session."
        )

    payload = {"identifier": handle, "password": app_password}
    try:
        result = _request("POST", CREATE_SESSION_EP, payload=payload)
    except BlueskyError as exc:
        raise BlueskyError(f"createSession failed (login cap ~300/day — check usage): {exc}") from exc

    session = {
        "accessJwt": result.get("accessJwt"),
        "refreshJwt": result.get("refreshJwt"),
        "did": result.get("did"),
        "handle": result.get("handle", handle),
    }
    if not session["accessJwt"] or not session["refreshJwt"]:
        raise BlueskyError(f"createSession returned an unexpected shape: {result!r}")

    _save_session(session, session_path)
    return session


def _try_refresh(cached: Dict[str, Any], session_path: str) -> Optional[Dict[str, Any]]:
    refresh_jwt = cached.get("refreshJwt")
    if not refresh_jwt:
        return None
    try:
        result = _request(
            "POST",
            REFRESH_SESSION_EP,
            headers={"Authorization": f"Bearer {refresh_jwt}"},
        )
    except BlueskyError:
        return None

    session = {
        "accessJwt": result.get("accessJwt"),
        "refreshJwt": result.get("refreshJwt", refresh_jwt),
        "did": result.get("did", cached.get("did")),
        "handle": result.get("handle", cached.get("handle")),
    }
    if not session["accessJwt"]:
        return None

    _save_session(session, session_path)
    return session


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def _auth_headers(session: Dict[str, Any]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {session['accessJwt']}"}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def post(
    text: str,
    session: Optional[Dict[str, Any]] = None,
    reply_ref: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Post `text` as a new record on app.bsky.feed.post.

    session: a session dict as returned by create_session(). If omitted,
             create_session() is called with env credentials.
    reply_ref: optional AT Protocol reply reference, shaped:
               { "root": {"uri": ..., "cid": ...}, "parent": {"uri": ..., "cid": ...} }
               Pass this to post a threaded reply (used by banter.py).

    Returns the createRecord response (contains "uri" and "cid" of the new
    post — callers should log these for dedup / thread-tracking purposes).

    This function makes a real network call. It is never invoked by any
    test in tests/ — only by post_tick.py, banter.py, and smoke_test.py.
    """
    session = session or create_session()

    record: Dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": _now_iso(),
    }
    if reply_ref:
        record["reply"] = reply_ref

    payload = {
        "repo": session["did"],
        "collection": "app.bsky.feed.post",
        "record": record,
    }

    return _request("POST", CREATE_RECORD_EP, headers=_auth_headers(session), payload=payload)


def build_reply_ref(root_uri: str, root_cid: str, parent_uri: str, parent_cid: str) -> Dict[str, Any]:
    """Small helper so callers don't hand-build the AT Protocol reply shape."""
    return {
        "root": {"uri": root_uri, "cid": root_cid},
        "parent": {"uri": parent_uri, "cid": parent_cid},
    }


def reply(
    text: str,
    root_uri: str,
    root_cid: str,
    parent_uri: str,
    parent_cid: str,
    session: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convenience wrapper: post() with a reply_ref built from the four URI/CID values."""
    ref = build_reply_ref(root_uri, root_cid, parent_uri, parent_cid)
    return post(text, session=session, reply_ref=ref)


# ---------------------------------------------------------------------------
# Engagement actions (the ACT layer, SPEC-v2 T1.5 / SPEC-v3).
#
# like / repost / follow are createRecord calls in three collections. They take
# NO model — Claude is never touched for these (only reply/quote drafting is).
# Every one of these is a WRITE and is only ever reached from act_tick.py AFTER
# an explicit operator thumbsup in Slack (I-HUMAN-GATE). None is invoked by any
# test in tests/ — the tests assert the PAYLOAD SHAPE via build helpers, never
# the network.
# ---------------------------------------------------------------------------

LIKE_COLLECTION = "app.bsky.feed.like"
REPOST_COLLECTION = "app.bsky.feed.repost"
FOLLOW_COLLECTION = "app.bsky.graph.follow"


def _create_record(collection: str, record: Dict[str, Any], session: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "repo": session["did"],
        "collection": collection,
        "record": record,
    }
    return _request("POST", CREATE_RECORD_EP, headers=_auth_headers(session), payload=payload)


def build_like_record(subject_uri: str, subject_cid: str) -> Dict[str, Any]:
    """The app.bsky.feed.like record shape. Split out so tests can assert it without a network call."""
    return {
        "$type": LIKE_COLLECTION,
        "subject": {"uri": subject_uri, "cid": subject_cid},
        "createdAt": _now_iso(),
    }


def build_repost_record(subject_uri: str, subject_cid: str) -> Dict[str, Any]:
    """The app.bsky.feed.repost record shape."""
    return {
        "$type": REPOST_COLLECTION,
        "subject": {"uri": subject_uri, "cid": subject_cid},
        "createdAt": _now_iso(),
    }


def build_follow_record(subject_did: str) -> Dict[str, Any]:
    """The app.bsky.graph.follow record shape (subject is a bare DID string, not a strong ref)."""
    return {
        "$type": FOLLOW_COLLECTION,
        "subject": subject_did,
        "createdAt": _now_iso(),
    }


def like(subject_uri: str, subject_cid: str, session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Like a post: createRecord on app.bsky.feed.like with a strong ref to the
    subject (uri + cid). Returns the createRecord response (uri/cid of OUR like
    record — log it so an unlike is possible later). Real network call.
    """
    session = session or create_session()
    return _create_record(LIKE_COLLECTION, build_like_record(subject_uri, subject_cid), session)


def repost(subject_uri: str, subject_cid: str, session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Repost a post: createRecord on app.bsky.feed.repost (strong ref subject). Real network call."""
    session = session or create_session()
    return _create_record(REPOST_COLLECTION, build_repost_record(subject_uri, subject_cid), session)


def follow(subject_did: str, session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Follow an account: createRecord on app.bsky.graph.follow (subject is the DID). Real network call."""
    session = session or create_session()
    return _create_record(FOLLOW_COLLECTION, build_follow_record(subject_did), session)


# ---------------------------------------------------------------------------
# Profile self-label — the I-BOT-DISCLOSED safety basis (SPEC-v3).
#
# Bluesky supports a global `bot` self-label on the profile record. It lives
# under app.bsky.actor.profile.labels as com.atproto.label.defs#selfLabels with
# val "bot". This is the disclosure that makes autonomous engagement honest.
# ---------------------------------------------------------------------------

PROFILE_COLLECTION = "app.bsky.actor.profile"
SELF_LABELS_TYPE = "com.atproto.label.defs#selfLabels"
BOT_LABEL_VAL = "bot"


def has_bot_label(profile_record: Dict[str, Any]) -> bool:
    """
    True if the given profile record already carries the `bot` self-label. Pure
    (no network) so the merge logic is unit-testable. Tolerant of a missing or
    differently-shaped `labels` field.
    """
    labels = (profile_record or {}).get("labels") or {}
    values = labels.get("values") if isinstance(labels, dict) else None
    if not isinstance(values, list):
        return False
    return any(isinstance(v, dict) and v.get("val") == BOT_LABEL_VAL for v in values)


def add_bot_label(profile_record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a copy of the profile record with the `bot` self-label added,
    PRESERVING every existing field (displayName, description, avatar, banner,
    other self-labels). Pure — no network. Idempotent: if `bot` is already
    present the record is returned unchanged in meaning.
    """
    record = dict(profile_record or {})
    labels = record.get("labels")
    values: List[Dict[str, Any]] = []
    if isinstance(labels, dict) and isinstance(labels.get("values"), list):
        values = [v for v in labels["values"] if isinstance(v, dict)]
    if not any(v.get("val") == BOT_LABEL_VAL for v in values):
        values = values + [{"val": BOT_LABEL_VAL}]
    record["labels"] = {"$type": SELF_LABELS_TYPE, "values": values}
    return record


def get_profile_record(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Read our own app.bsky.actor.profile / self record. Returns the getRecord
    response {uri, cid, value} or None if no profile record exists yet (a fresh
    account may not have one). Real network call.
    """
    did = session["did"]
    url = (
        f"{GET_RECORD_EP}?repo={urllib.parse.quote(did, safe=':')}"
        f"&collection={PROFILE_COLLECTION}&rkey=self"
    )
    try:
        return _request("GET", url, headers=_auth_headers(session))
    except BlueskyError as exc:
        # A record-not-found reads as an error; treat as "no profile yet".
        if "could not locate record" in str(exc).lower() or "RecordNotFound" in str(exc):
            return None
        raise


def ensure_bot_label(session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Idempotently ensure the `bot` self-label is set on our profile (I-BOT-DISCLOSED,
    SPEC-v3). Reads the current profile record, adds the label if missing, and
    putRecord's it back preserving all other fields. Safe to call every run.

    Returns {"status": "already_set" | "set" | "created", "cid": <new cid?>}.
    Real network call — never invoked by any test (the merge logic is tested via
    has_bot_label / add_bot_label instead).
    """
    session = session or create_session()
    did = session["did"]
    existing = get_profile_record(session)

    if existing is not None:
        record = existing.get("value") or {}
        if has_bot_label(record):
            return {"status": "already_set"}
        new_record = add_bot_label(record)
        payload = {
            "repo": did,
            "collection": PROFILE_COLLECTION,
            "rkey": "self",
            "record": new_record,
        }
        swap = existing.get("cid")
        if swap:
            payload["swapRecord"] = swap  # optimistic concurrency
        res = _request("POST", PUT_RECORD_EP, headers=_auth_headers(session), payload=payload)
        return {"status": "set", "cid": res.get("cid")}

    # No profile record yet: create a minimal one carrying just the label.
    new_record = add_bot_label({"$type": PROFILE_COLLECTION})
    payload = {"repo": did, "collection": PROFILE_COLLECTION, "rkey": "self", "record": new_record}
    res = _request("POST", PUT_RECORD_EP, headers=_auth_headers(session), payload=payload)
    return {"status": "created", "cid": res.get("cid")}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def get_notifications(
    session: Optional[Dict[str, Any]] = None,
    limit: int = 50,
    seen_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Wraps app.bsky.notification.listNotifications. Returns the raw list of
    notification objects (each has at minimum: uri, cid, author, reason,
    record, isRead, indexedAt). Filtering (own-account vs stranger, new vs
    already-handled) is banter.py's job, not this module's — this stays a
    thin transport wrapper.
    """
    session = session or create_session()

    url = f"{LIST_NOTIFICATIONS_EP}?limit={int(limit)}"
    if seen_at:
        url += f"&seenAt={seen_at}"

    result = _request("GET", url, headers=_auth_headers(session))
    return result.get("notifications", [])


def get_post_thread(uri: str, session: Optional[Dict[str, Any]] = None, depth: int = 1) -> Dict[str, Any]:
    """
    Wraps app.bsky.feed.getPostThread — used by banter.py to resolve a
    notification's parent/root URIs+CIDs before replying (a notification
    gives you the reply itself, not necessarily the full thread context).
    """
    session = session or create_session()
    url = f"{BASE_URL}/app.bsky.feed.getPostThread?uri={urllib.parse.quote(uri, safe=':/')}&depth={int(depth)}"
    return _request("GET", url, headers=_auth_headers(session))


# ---------------------------------------------------------------------------
# Search (the SEE step's eyes — read-only, no writes, cheap)
# ---------------------------------------------------------------------------


def search_posts(
    q: str,
    session: Optional[Dict[str, Any]] = None,
    limit: int = 25,
    sort: str = "latest",
    lang: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tags: Optional[List[str]] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Wraps app.bsky.feed.searchPosts — the scout's discovery primitive.

    This is a READ. Reads don't cost write-points; the only ceiling is the
    per-IP HTTP budget (3000 req / 5 min), which an hourly scan never
    approaches. Authenticated (bsky.social proxies app.bsky.* reads to the
    appview using the session's Bearer token).

    Params mirror the endpoint:
      q      — required query string (supports Bluesky search operators).
      sort   — "latest" (chronological, default here) or "top" (engagement).
      lang   — BCP-47 code, e.g. "en" (filters by detected post language).
      since  — ISO date / datetime; only posts AT OR AFTER this (inclusive).
               This is the incremental-scan lever: pass the last scan time so
               each run only sees new posts, never re-chewing the feed.
      until  — ISO date / datetime; only posts BEFORE this (exclusive).
      tags   — list of hashtags (no '#'); multiple = AND match.
      cursor — pagination cursor from a prior response.

    Returns the raw response dict: {"posts": [...], "cursor": <optional>}.
    Candidate shaping / dedup / own-account filtering is scout.py's job — this
    stays a thin transport wrapper, consistent with the rest of this module.
    """
    session = session or create_session()

    params: List[tuple] = [("q", q), ("limit", str(int(limit)))]
    if sort:
        params.append(("sort", sort))
    if lang:
        params.append(("lang", lang))
    if since:
        params.append(("since", since))
    if until:
        params.append(("until", until))
    if cursor:
        params.append(("cursor", cursor))
    for t in tags or []:
        # tag is a repeatable query param (AND-matched by the server).
        params.append(("tag", t))

    url = f"{SEARCH_POSTS_EP}?{urllib.parse.urlencode(params)}"
    return _request("GET", url, headers=_auth_headers(session))
