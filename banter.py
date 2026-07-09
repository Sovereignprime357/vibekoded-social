"""
banter.py — reply/coverage entrypoint.

Invoked by .github/workflows/notify.yml on a cron (every ~20 min). Also
runnable manually: `python banter.py`.

Flow (per SPEC.md ARCHITECTURE + INVARIANTS I-NO-AUTO-POKE / I-GATED-REPLIES):
  poll notifications -> for each NEW reply/mention:
    (a) authored by OUR OWN account, but NOT one of our own scheduled posts
        (i.e. same handle/DID as us, and the post id is NOT in posted.jsonl)
        -> this is the human co-pilot logging in and posting from the same
           account. Safe to auto-reply (it's our own account, no stranger
           engagement involved) -> generate an in-voice banter reply and
           POST it.
    (b) authored by a STRANGER -> hand off to converse.py: triage (Haiku),
        draft a reply-back in voice, guard it, and SURFACE it to Slack for the
        operator's 👍 (posted by act_tick through the same gate). Do NOT
        auto-post to strangers — human-gated by default (SPEC-v3).

  Every notification id is tracked in handled.jsonl to dedup across runs
  (cron fires every ~20 min; without dedup the same notification would be
  re-processed every tick until it ages out of listNotifications).

  Every generated reply (banter AND draft) is guard-checked before it is
  posted OR written to drafts.jsonl — a draft the operator reviews is still
  something that could get copy-pasted and sent without a second look, so
  it gets the same I-PRIVACY treatment as a live post.

Classification detail (case (a) vs (b)):
  A notification's `author.did` / `author.handle` tells us who wrote the
  triggering post. If that matches OUR OWN did/handle, it's technically
  "us" — but a notification of type "reply"/"mention" only fires for
  interactions on OUR threads that come from the timeline, which in the
  shared-account setup includes the human co-pilot logging into the same
  account and replying to the bot's own scheduled post. We distinguish
  "our own scheduled post continuing the thread" (nothing to react to)
  from "the human logged in and said something new in-thread" by checking
  whether the notification's post URI appears in posted.jsonl: if the
  triggering post's `record` text/URI isn't something WE scheduled and
  logged via post_tick.py, but the author IS us, it's the human's
  manual keystrokes on the shared login -> that's the co-pilot signal.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Set

import guard
import generate
import converse

STATE_DIR = os.path.dirname(os.path.abspath(__file__))
HANDLED_LOG = os.path.join(STATE_DIR, "handled.jsonl")
POSTED_LOG = os.path.join(STATE_DIR, "posted.jsonl")
SKIPPED_LOG = os.path.join(STATE_DIR, "skipped.jsonl")

REPLY_REASONS = {"reply", "mention", "quote"}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def _append_jsonl(path: str, record: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _load_handled_ids() -> Set[str]:
    return {r.get("notification_uri") for r in _read_jsonl(HANDLED_LOG) if r.get("notification_uri")}


def _load_our_own_post_uris() -> Set[str]:
    """
    URIs of posts WE scheduled and shipped via post_tick.py. Used to tell
    "human replied in-thread on the shared account" apart from "this is our
    own scheduled post notifying about itself" (the latter shouldn't happen
    via listNotifications for our own outbound posts, but the check costs
    nothing and makes the classification explicit rather than assumed).
    """
    return {r.get("uri") for r in _read_jsonl(POSTED_LOG) if r.get("uri")}


def _recent_posted(limit: int = 10) -> List[Dict[str, Any]]:
    """Last `limit` posts we shipped (file order = chronological), for thread polling."""
    rows = [r for r in _read_jsonl(POSTED_LOG) if r.get("uri")]
    return rows[-limit:]


def _mark_handled(notification: Dict[str, Any], action: str) -> None:
    _append_jsonl(
        HANDLED_LOG,
        {
            "ts": _now_iso(),
            "notification_uri": notification.get("uri"),
            "reason": notification.get("reason"),
            "author_handle": (notification.get("author") or {}).get("handle"),
            "action": action,
        },
    )


def _extract_text(notification: Dict[str, Any]) -> str:
    record = notification.get("record") or {}
    return str(record.get("text", "")).strip()


def _log_guard_block(context: str, notification: Dict[str, Any], text: str, reason: str) -> None:
    _append_jsonl(
        SKIPPED_LOG,
        {
            "ts": _now_iso(),
            "context": context,
            "notification_uri": notification.get("uri"),
            "generated_text": text,
            "reason": f"guard blocked: {reason}",
        },
    )


def classify(notification: Dict[str, Any], our_did: str, our_own_post_uris: Set[str]) -> str:
    """
    Returns "own_account", "stranger", or "ignore".

    "ignore" covers notification reasons we don't act on at all here (likes,
    follows, reposts, etc.) — banter.py only reacts to reply/mention/quote
    reasons; everything else is out of scope for this entrypoint.
    """
    reason = notification.get("reason")
    if reason not in REPLY_REASONS:
        return "ignore"

    author = notification.get("author") or {}
    author_did = author.get("did")

    if author_did == our_did:
        # Our own account authored the triggering post. Per the module
        # docstring: this is the human co-pilot, UNLESS this exact post URI
        # is one we scheduled ourselves (which would be an odd case where
        # our own scheduled post shows up as a "reply" notification about
        # itself — guard against double-replying to our own drip).
        subject_uri = notification.get("uri")
        if subject_uri in our_own_post_uris:
            return "ignore"
        return "own_account"

    return "stranger"


def process_own_account(notification: Dict[str, Any], bluesky_module, session: Dict[str, Any]) -> None:
    """
    Case (a): the human co-pilot posted from our shared account. Generate
    an in-voice banter reply and POST it — this is safe because it's our
    own account replying to itself, not an auto-interaction with a stranger
    (I-NO-AUTO-POKE is about not initiating with accounts that didn't
    engage first; this is literally us continuing our own thread).
    """
    text_in = _extract_text(notification)
    pseudo_entry = {"raw": text_in, "type": "moment"}

    try:
        reply_text = generate.generate(pseudo_entry, kind="banter")
    except Exception as exc:  # noqa: BLE001
        print(f"[banter] generation exception for own_account reply: {exc!r}")
        _mark_handled(notification, "generation_error")
        return

    if not reply_text:
        print("[banter] empty generation for own_account reply — skipping.")
        _mark_handled(notification, "empty_generation")
        return

    ok, reason = guard.check(reply_text)
    if not ok:
        print(f"[banter] GUARD BLOCKED own_account reply: {reason}")
        _log_guard_block("own_account_reply", notification, reply_text, reason)
        _mark_handled(notification, "guard_blocked")
        return

    record = notification.get("record") or {}
    reply_ref = record.get("reply")  # AT proto reply object, if the notification itself was a reply
    root = (reply_ref or {}).get("root") or {"uri": notification.get("uri"), "cid": notification.get("cid")}
    parent = {"uri": notification.get("uri"), "cid": notification.get("cid")}

    if _is_dry_run():
        print("=" * 60)
        print(f"[banter] DRY_RUN — would REPLY (own_account) with:\n{reply_text}")
        print("=" * 60)
        _mark_handled(notification, "dry_run_would_post")
        return

    try:
        result = bluesky_module.reply(
            reply_text,
            root_uri=root.get("uri"),
            root_cid=root.get("cid"),
            parent_uri=parent.get("uri"),
            parent_cid=parent.get("cid"),
            session=session,
        )
        print(f"[banter] posted own_account reply: uri={result.get('uri')}")
        _mark_handled(notification, "posted")
        # Loop-breaker: record OUR OWN reply's URI as handled so the thread-poll
        # path (poll_own_threads) never treats it as a fresh human comment on the
        # next tick — otherwise we'd banter-reply to our own banter forever.
        if result.get("uri"):
            _append_jsonl(
                HANDLED_LOG,
                {
                    "ts": _now_iso(),
                    "notification_uri": result.get("uri"),
                    "reason": "self_banter_output",
                    "author_handle": session.get("handle"),
                    "action": "own_reply_posted",
                },
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[banter] Bluesky reply failed for own_account case: {exc!r}")
        _mark_handled(notification, "post_failed")


def poll_own_threads(
    bluesky_module,
    session: Dict[str, Any],
    our_did: str,
    handled_ids: Set[str],
    our_own_post_uris: Set[str],
) -> int:
    """
    Second detection path: catch the co-pilot's manual comments, which are
    authored BY our own account and therefore never surface in
    listNotifications (Bluesky doesn't notify you about your own replies).

    Poll the threads of our recent posts; a direct reply authored by OUR OWN
    DID that is neither one of our scheduled posts nor already handled is a
    human manual comment -> banter-reply to it via process_own_account.
    Strangers (other DIDs) are skipped here — they're handled by the
    notifications flow (converse.handle_incoming_reply), so we don't double-process them.
    """
    processed = 0
    for row in _recent_posted(limit=10):
        post_uri = row.get("uri")
        if not post_uri:
            continue
        try:
            thread = (
                bluesky_module.get_post_thread(post_uri, session=session, depth=1) or {}
            ).get("thread") or {}
        except Exception as exc:  # noqa: BLE001 — one bad/deleted post must not kill the tick
            print(f"[banter] get_post_thread failed for {post_uri}: {exc!r}")
            continue

        for node in (thread.get("replies") or []):
            post = node.get("post")  # absent for #notFoundPost / #blockedPost nodes
            if not post:
                continue
            reply_uri = post.get("uri")
            author_did = (post.get("author") or {}).get("did")
            if not reply_uri or author_did != our_did:
                continue  # stranger reply -> handled by the notifications path
            if reply_uri in our_own_post_uris or reply_uri in handled_ids:
                continue  # our own scheduled post, or already handled

            pseudo = {
                "uri": reply_uri,
                "cid": post.get("cid"),
                "reason": "self_reply",
                "author": post.get("author") or {},
                "record": post.get("record") or {},  # carries text + reply.{root,parent}
            }
            process_own_account(pseudo, bluesky_module, session)
            handled_ids.add(reply_uri)  # guard against re-processing within this tick
            processed += 1

    return processed


def run_banter() -> int:
    """
    Execute one notification-poll tick. Returns a process exit code
    (0 = clean run, including "nothing new"; 1 = an actual failure such as
    the notification fetch itself erroring).
    """
    try:
        import bluesky

        session = bluesky.create_session()
        notifications = bluesky.get_notifications(session=session)
    except Exception as exc:  # noqa: BLE001
        print(f"[banter] failed to fetch notifications: {exc!r}")
        return 1

    our_did = session.get("did")
    handled_ids = _load_handled_ids()
    our_own_post_uris = _load_our_own_post_uris()

    new_count = 0
    insight_items: List[Dict[str, Any]] = []  # reply/mention text for the ops-insight harvest (reuse-only)
    for notification in notifications:
        uri = notification.get("uri")
        if not uri or uri in handled_ids:
            continue

        category = classify(notification, our_did, our_own_post_uris)
        if category == "ignore":
            continue

        new_count += 1
        # Collect the (already-pulled) reply/mention for the ops-insight lens. This
        # is the deep-technical-exchange stream; the harvest has its own dedup + a
        # high bar, so including all reasons here is safe.
        author = notification.get("author") or {}
        insight_items.append({
            "uri": uri, "cid": notification.get("cid"),
            "text": _extract_text(notification),
            "author_handle": author.get("handle", ""), "author_did": author.get("did", ""),
        })
        if category == "own_account":
            process_own_account(notification, bluesky, session)
        elif category == "stranger":
            # Conversation-continuation (SPEC-v3): triage the inbound reply,
            # draft a reply-back in voice, guard it, and SURFACE it to Slack for
            # the operator's 👍 (posted by act_tick through the same gate).
            # Supersedes the old draft-to-drafts.jsonl path. converse owns its
            # own handled.jsonl marking + thread-depth + I-NO-SELF loop guard.
            converse.handle_incoming_reply(notification, our_did)

    if new_count == 0:
        print("[banter] no new reply/mention notifications this tick.")
    else:
        print(f"[banter] processed {new_count} new notification(s) this tick.")

    # Ops-Insight Harvest (SPEC-v6): a SECOND, review-only lens over the reply/
    # mention stream we ALREADY pulled (reuse-only). Wrapped so it can NEVER break
    # the notify tick. Never acts, never writes to the brain — only a Slack brief.
    try:
        import ops_insight
        n_ins = ops_insight.harvest(insight_items, dry_run=_is_dry_run())
        if n_ins:
            print(f"[banter] ops-insight: posted {n_ins} brief(s) to the review channel")
    except Exception as exc:  # noqa: BLE001
        print(f"[banter] ops-insight harvest errored (non-fatal): {exc!r}")

    # Second pass: poll our own recent threads for the co-pilot's manual
    # comments, which never appear in listNotifications (Bluesky doesn't notify
    # an account about its own replies). Keep the notifications pass above for
    # strangers; this pass only acts on self-authored replies.
    try:
        thread_count = poll_own_threads(
            bluesky, session, our_did, handled_ids, our_own_post_uris
        )
    except Exception as exc:  # noqa: BLE001 — thread poll must not fail an otherwise-clean tick
        print(f"[banter] thread-poll pass errored (non-fatal): {exc!r}")
        thread_count = 0

    if thread_count:
        print(f"[banter] posted {thread_count} in-thread banter reply(ies) from self-reply poll.")
    else:
        print("[banter] no new self-replies found in own threads this tick.")

    return 0


if __name__ == "__main__":
    sys.exit(run_banter())
