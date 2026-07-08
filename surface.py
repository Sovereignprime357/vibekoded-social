"""
surface.py — the SURFACE step of the agentic loop (SPEC-v2.md).

Takes triage.py's on-mission items and posts each one to Slack for the
operator's yes/no. This is PLAIN CODE — no model runs here. An HTTP POST out,
that's it. The "hey, look at this" step costs nothing.

Each surfaced item is formatted so the operator can act from their phone:
what the post is, who said it, which lane, why it fits, the proposed action,
a confidence flag, and a tap-through link. The operator does the action
manually in T1 (tap like / write the reply / repost); autonomy is earned per
class later (SPEC-v2.md TRUST TIERS).

DRY_RUN: prints the exact Slack payload instead of sending it — this is the
sample the operator reviews before any key or webhook goes live.

Dedup: scout.py already marks every scanned post as seen, so a post is only
ever surfaced once. surface.py additionally keeps its own audit log
(scout-surfaced.jsonl, I-LOGGED) and skips any URI already in it as a belt-
and-suspenders guard.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Set

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SURFACED_PATH = os.path.join(HERE, "scout-surfaced.jsonl")

ACTION_LABEL = {
    "like": "👍 LIKE",
    "reply": "💬 REPLY",
    "repost": "🔁 REPOST",
    "follow": "➕ FOLLOW",
}
CONFIDENCE_MARK = {"high": "high", "med": "med", "low": "low ⚠️"}


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


# ---------------------------------------------------------------------------
# Formatting (pure, testable)
# ---------------------------------------------------------------------------


def format_item(item: Dict[str, Any]) -> str:
    """Render one surfaced item as Slack mrkdwn text."""
    action = str(item.get("action", "")).lower()
    action_lbl = ACTION_LABEL.get(action, action.upper() or "—")
    conf = CONFIDENCE_MARK.get(str(item.get("confidence", "low")).lower(), "low ⚠️")
    lane = item.get("lane_label") or item.get("lane") or ""
    handle = item.get("author_handle", "")
    text = str(item.get("text", "")).strip()
    why = str(item.get("why", "")).strip()
    url = item.get("url", "")

    # Keep the quoted post from blowing up the message.
    if len(text) > 400:
        text = text[:397] + "…"

    # Conversation-continuation items show the incoming reply as context and,
    # crucially, the PROPOSED reply-back the operator is 👍-approving — so the
    # 👍 approves the actual text, not a blind "reply to this".
    source = str(item.get("source", "")).lower()
    if source == "converse":
        header = f"*💬 REPLY-BACK*  ·  class: {item.get('reply_class', '?')}  ·  @{handle} replied:"
        lines = [header, f"> {text}"]
        if why:
            lines.append(f"_why:_ {why}")
        draft = str(item.get("draft_text", "")).strip()
        if draft:
            lines.append(f"*proposed reply:* {draft}")
        if url:
            lines.append(f"→ {url}")
        return "\n".join(lines)

    lines = [
        f"*{action_lbl}*  ·  confidence: {conf}  ·  lane: {lane}",
        f"@{handle}:",
        f"> {text}",
    ]
    if why:
        lines.append(f"_why:_ {why}")
    if url:
        lines.append(f"→ {url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Surfaced-audit ledger (dedup + I-LOGGED)
# ---------------------------------------------------------------------------


def load_surfaced_uris(path: str = SURFACED_PATH) -> Set[str]:
    seen: Set[str] = set()
    if not os.path.exists(path):
        return seen
    try:
        with open(path, "r", encoding="utf-8") as f:
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


def _append_surfaced(
    item: Dict[str, Any],
    path: str = SURFACED_PATH,
    slack_ts: Optional[str] = None,
    slack_channel: Optional[str] = None,
) -> None:
    """
    Append one audit record (I-LOGGED). SPEC-v2 T1.5 requires enough here for the
    ACT layer to execute later WITHOUT re-querying triage: the Bluesky post's
    uri+cid and the author DID (for like/repost/follow), the proposed action, and
    — critically — the Slack message `ts` + channel, which are the handle
    act_tick.py polls for the operator's thumbsup reaction.
    """
    rec = {
        "uri": item.get("uri"),
        "cid": item.get("cid"),
        "author_handle": item.get("author_handle"),
        "author_did": item.get("author_did"),
        "lane_id": item.get("lane_id"),
        "action": item.get("action"),
        "confidence": item.get("confidence"),
        "why": item.get("why"),
        "text": item.get("text"),
        "url": item.get("url"),
        "slack_ts": slack_ts,
        "slack_channel": slack_channel,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Conversation-continuation items (converse.py) carry a pre-drafted,
    # pre-guarded reply-back plus explicit thread refs so act_tick can post the
    # exact approved text, threaded correctly. Persist them when present.
    for k in ("source", "reply_class", "draft_text", "parent_uri", "parent_cid", "root_uri", "root_cid",
              "rank_score", "rank_components"):
        if item.get(k) is not None:
            rec[k] = item.get(k)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Slack transport
# ---------------------------------------------------------------------------


SLACK_POST_MESSAGE_EP = "https://slack.com/api/chat.postMessage"


def _post_slack(text: str, webhook_url: str, timeout: int = 15) -> bool:
    """
    Best-effort POST to a Slack incoming webhook (legacy fallback). Returns
    success; never raises. This path captures NO message ts, so items surfaced
    via the webhook can't be acted on by act_tick — it's the safe-degrade rung
    for when SLACK_BOT_TOKEN isn't set yet.
    """
    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=timeout)
        if resp.status_code >= 400:
            print(f"[surface] Slack POST failed: HTTP {resp.status_code} {resp.text[:120]}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"[surface] Slack POST error (non-fatal): {exc}")
        return False


def _post_slack_web(text: str, token: str, channel: str, timeout: int = 15) -> Optional[str]:
    """
    Post via the Slack Web API (chat.postMessage) with a bot token, so we get the
    message `ts` back — the anchor act_tick.py polls for the operator's thumbsup
    (SPEC-v2 T1.5). Returns the message ts on success, or None on any failure
    (never raises — a Slack hiccup must not crash the scout tick).
    """
    try:
        resp = requests.post(
            SLACK_POST_MESSAGE_EP,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json={"channel": channel, "text": text},
            timeout=timeout,
        )
        data = resp.json() if resp.content else {}
    except (requests.RequestException, ValueError) as exc:
        print(f"[surface] Slack chat.postMessage error (non-fatal): {exc}")
        return None
    if not data.get("ok"):
        # Slack returns 200 with ok:false + an error string (e.g. not_in_channel).
        print(f"[surface] Slack chat.postMessage rejected: {data.get('error', resp.text[:120])}")
        return None
    return data.get("ts")


def batch_summary(items: List[Dict[str, Any]]) -> str:
    """
    One scannable header line for a batch: "N finds: X replies, Y likes, Z reposts,
    W follows". Pure/testable. Only the action types actually present are listed,
    in a stable order.
    """
    order = [("reply", "replies"), ("like", "likes"), ("repost", "reposts"), ("follow", "follows")]
    counts: Dict[str, int] = {}
    for it in items:
        a = str(it.get("action", "")).strip().lower()
        counts[a] = counts.get(a, 0) + 1
    parts = [f"{counts[a]} {label}" for a, label in order if counts.get(a)]
    # Any other/unknown action types, appended so nothing is silently dropped.
    known = {a for a, _ in order}
    other = sum(v for k, v in counts.items() if k not in known)
    if other:
        parts.append(f"{other} other")
    n = len(items)
    return f"*📥 {n} find{'s' if n != 1 else ''}:* " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def surface_all(
    items: List[Dict[str, Any]],
    webhook_url: Optional[str] = None,
    dry_run: Optional[bool] = None,
    surfaced_path: Optional[str] = None,
    bot_token: Optional[str] = None,
    channel: Optional[str] = None,
) -> int:
    """
    Surface each item to Slack (or print in DRY_RUN). Skips any URI already in
    the audit ledger. Returns the number of items actually surfaced this call.

    Transport ladder (first that applies wins), per SPEC-v2 T1.5:
      1. DRY_RUN            -> print only, no ledger write (repeatable preview).
      2. bot_token+channel  -> chat.postMessage; capture the message `ts` so the
                               ACT layer can poll for the operator's thumbsup.
      3. webhook_url        -> legacy incoming webhook (NO ts; not actionable).
      4. nothing configured -> print to the run log (still logged to the ledger).

    surfaced_path resolves at CALL time (default: the module SURFACED_PATH) and
    is threaded explicitly into the ledger helpers — never relying on their
    def-time default args, so the ledger the dedup-check reads is always the
    same one the append writes to.
    """
    if not items:
        print("[surface] nothing to surface")
        return 0

    dry_run = _is_dry_run() if dry_run is None else dry_run
    webhook_url = webhook_url if webhook_url is not None else os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    bot_token = bot_token if bot_token is not None else os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = channel if channel is not None else os.environ.get("SLACK_CHANNEL_ID", "").strip()
    surfaced_path = surfaced_path or SURFACED_PATH
    already = load_surfaced_uris(surfaced_path)

    # Items that will actually be surfaced this call (skip already-surfaced dupes),
    # in the order given — scout_tick passes them ranked best-first (SPEC-v3).
    to_surface = [it for it in items if not (it.get("uri") and it.get("uri") in already)]

    # A scannable batch header so the firehose is legible on mobile: how many
    # finds and the action-type mix, posted BEFORE the ranked cards. Only for a
    # real batch (>1) — a lone item (e.g. a converse reply-back) needs no header.
    if len(to_surface) > 1:
        summary = batch_summary(to_surface)
        if dry_run:
            print(f"---- [DRY_RUN] batch header ----\n{summary}")
        elif bot_token and channel:
            _post_slack_web(summary, bot_token, channel)
        elif webhook_url:
            _post_slack(summary, webhook_url)
        else:
            print(summary)

    count = 0
    for item in to_surface:
        uri = item.get("uri")

        text = format_item(item)
        slack_ts: Optional[str] = None
        slack_channel: Optional[str] = None

        if dry_run:
            print("---- [DRY_RUN] would surface to Slack ----")
            print(text)
        elif bot_token and channel:
            slack_ts = _post_slack_web(text, bot_token, channel)
            slack_channel = channel
            if slack_ts:
                print(f"[surface] posted to Slack channel {channel} ts={slack_ts} (actionable)")
            else:
                print("[surface] chat.postMessage failed; item logged without a ts (not actionable)")
        elif webhook_url:
            _post_slack(text, webhook_url)
        else:
            # Nothing configured but not dry-run: print + still log so we don't
            # silently drop, and the operator sees it in the run output.
            print("[surface] no SLACK_BOT_TOKEN / SLACK_WEBHOOK_URL set; printing instead:")
            print(text)

        # Dry-run is side-effect-free: no ledger write, so the operator can
        # re-run the preview as many times as they like without "consuming"
        # posts (scout also skips state/seen persistence in dry-run).
        if not dry_run:
            _append_surfaced(item, surfaced_path, slack_ts=slack_ts, slack_channel=slack_channel)
        if uri:
            already.add(uri)
        count += 1

    print(f"[surface] surfaced {count} item(s)" + (" (dry-run)" if dry_run else ""))
    return count


if __name__ == "__main__":
    demo = [{
        "uri": "at://x/app.bsky.feed.post/1",
        "author_handle": "someone.bsky.social",
        "text": "how is everyone handling agents losing all context between sessions?",
        "lane_id": "memory",
        "lane_label": "Agent memory / context engineering",
        "action": "reply",
        "confidence": "high",
        "why": "our exact wheelhouse — flat-file+index answer, ask what they run",
        "url": "https://bsky.app/profile/someone.bsky.social/post/1",
    }]
    surface_all(demo, dry_run=True)
