"""
converse.py — the conversation-continuation loop (SPEC-v3 active engagement).

When a STRANGER replies to / mentions our account, we can reply back to keep the
thread going — but GATED behind the operator's 👍, never autonomous by default.
Auto-replying to strangers is the single riskiest action; this is the earn-it
ladder in code, not a launch-with-it feature.

Flow (invoked from banter.py's stranger branch, on the notify cron):
  incoming stranger reply
    -> I-NO-SELF: never our own account (infinite-loop guard)
    -> dedup: banter's handled.jsonl means each incoming reply is seen once
    -> thread-depth cap: stop a thread from spiraling (CONVERSE_MAX_THREAD_DEPTH)
    -> TRIAGE (Haiku): worth responding to? (genuine Q / substance / appreciation)
       vs skip (spam / hostile / nothing to add / off-brand)
    -> DRAFT a reply-back in PERSONA voice
    -> GUARD (fail-closed, incl. client/project terms) — a leaking draft is
       dropped, never surfaced
    -> SURFACE to Slack via the bot token (captures the ts), tagged source=
       "converse" so act_tick posts it on the operator's 👍 through the SAME
       act gate (operator-scoped, paced, capped, logged).

Autonomy is a config FLAG consumed by act_tick (AUTO_REPLY_BACK, default OFF):
specific reply-classes can graduate to auto-posting LATER without a rebuild.

Everything here is I-LOGGED via handled.jsonl (with converse_* fields).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set

import generate
import guard

HERE = os.path.dirname(os.path.abspath(__file__))
HANDLED_LOG = os.path.join(HERE, "handled.jsonl")

# Notification reasons that are someone talking TO us (a thread to continue).
INCOMING_REASONS = {"reply", "mention"}

# Triage verdict classes. Anything that isn't "skip" is worth a reply-back.
# These class names are also what AUTO_REPLY_BACK allowlists graduate (later).
WORTH_CLASSES = {"question", "substantive", "appreciation"}
SKIP_CLASS = "skip"
VALID_CLASSES = WORTH_CLASSES | {SKIP_CLASS}

# Triage on the paid model (voice/judgment matter more than cost here, and it's
# low-volume — only actual inbound replies). Override via CONVERSE_TRIAGE_MODEL.
def _triage_model() -> str:
    return (os.environ.get("CONVERSE_TRIAGE_MODEL") or os.environ.get("GEN_MODEL") or "anthropic").strip().lower()


def _max_thread_depth() -> int:
    """Max reply-backs we'll propose in a single thread before we stop (anti-spiral)."""
    val = os.environ.get("CONVERSE_MAX_THREAD_DEPTH", "").strip()
    if val:
        try:
            return max(0, int(val))
        except ValueError:
            pass
    return 3


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


# ---------------------------------------------------------------------------
# I-NO-SELF (infinite-loop guard)
# ---------------------------------------------------------------------------


def is_own_reply(notification: Dict[str, Any], our_did: Optional[str]) -> bool:
    """
    True if the triggering post was authored by OUR OWN account. We must NEVER
    generate a conversation reply-back to ourselves — that's the infinite loop
    (our reply -> notification -> another reply -> ...). Fail safe: if we can't
    read the author, treat as self (skip) rather than risk the loop.
    """
    if not our_did:
        return False
    author = notification.get("author") or {}
    did = author.get("did")
    if did is None:
        return True  # unknown author -> don't risk it
    return did == our_did


# ---------------------------------------------------------------------------
# Thread refs (so the reply-back threads correctly, not as a new root)
# ---------------------------------------------------------------------------


def extract_refs(notification: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Build the AT-proto reply targets for a reply-back:
      - parent = the incoming reply itself (we reply UNDER it).
      - root   = the thread's real root, taken from the incoming reply's
                 record.reply.root; falls back to the parent when the incoming
                 reply is itself a top-level post (mention on our post).
    """
    parent_uri = notification.get("uri")
    parent_cid = notification.get("cid")
    record = notification.get("record") or {}
    reply = record.get("reply") or {}
    root = reply.get("root") or {}
    root_uri = root.get("uri") or parent_uri
    root_cid = root.get("cid") or parent_cid
    return {
        "parent_uri": parent_uri, "parent_cid": parent_cid,
        "root_uri": root_uri, "root_cid": root_cid,
    }


# ---------------------------------------------------------------------------
# TRIAGE (Haiku): is this incoming reply worth responding to?
# ---------------------------------------------------------------------------


def build_triage_prompt(incoming_text: str, our_post_text: str = "") -> str:
    ctx = f'\nOur post they replied to:\n"""{our_post_text.strip()}"""\n' if our_post_text.strip() else ""
    return (
        "You triage inbound replies to a dry, build-in-public Bluesky account that engages "
        "genuinely with builders. Decide if THIS reply is worth responding to.\n"
        f"{ctx}"
        f'\nTheir reply:\n"""{incoming_text.strip()}"""\n\n'
        "Respond with ONLY a JSON object — no prose, no fence:\n"
        '{"worth_responding": true|false, '
        '"class": "question|substantive|appreciation|skip", '
        '"why": "<one concrete line, max 140 chars>"}\n\n'
        "Rules:\n"
        "- worth_responding=true ONLY if we can add something genuine: answer a real question, "
        "build on a substantive point, or warmly acknowledge clear appreciation.\n"
        '- class="skip" (worth_responding=false) for spam, shilling, hostility, bait, '
        "off-brand/political, or anything we'd add nothing to.\n"
        "- When in doubt, skip. Silence is fine; a hollow reply is not.\n"
    )


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_triage(raw: str) -> Optional[Dict[str, Any]]:
    """Extract + normalize the triage verdict. None if nothing parseable."""
    if not raw or not raw.strip():
        return None
    m = _JSON_OBJ_RE.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    cls = str(obj.get("class", "")).strip().lower()
    if cls not in VALID_CLASSES:
        cls = SKIP_CLASS
    worth = obj.get("worth_responding", False)
    if isinstance(worth, str):
        worth = worth.strip().lower() in ("true", "yes", "1")
    worth = bool(worth)
    # Integrity: skip class is never "worth"; a worth verdict can't be skip-class.
    if cls == SKIP_CLASS:
        worth = False
    elif not worth:
        cls = SKIP_CLASS
    return {"worth_responding": worth, "class": cls, "why": str(obj.get("why", "")).strip()[:200]}


def _stub_triage(incoming_text: str) -> Dict[str, Any]:
    """
    Deterministic, credential-free verdict for DRY_RUN / no-key runs so the loop
    is testable without a model. A question ('?') is treated as worth a reply;
    everything else skips (conservative — matches "when in doubt, skip").
    """
    if "?" in (incoming_text or ""):
        return {"worth_responding": True, "class": "question", "why": "[stub] looks like a question"}
    return {"worth_responding": False, "class": SKIP_CLASS, "why": "[stub] no question detected"}


def triage_incoming(incoming_text: str, our_post_text: str = "", model: Optional[str] = None) -> Dict[str, Any]:
    """
    Triage one inbound reply. Returns {worth_responding, class, why}. Falls back
    to the deterministic stub when no model runs (DRY_RUN / missing key) or the
    model output can't be parsed — never invents a "worth" verdict on failure.
    """
    model = model or _triage_model()
    key_env = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(
        model, "ANTHROPIC_API_KEY"
    )
    if _is_dry_run() or not os.environ.get(key_env, "").strip():
        return _stub_triage(incoming_text)
    try:
        raw = generate.complete(build_triage_prompt(incoming_text, our_post_text), model=model, temperature=0.1, max_tokens=200)
    except generate.GenerationError as exc:
        print(f"[converse] triage failed ({exc}); skipping (fail safe).")
        return {"worth_responding": False, "class": SKIP_CLASS, "why": "[triage error]"}
    verdict = parse_triage(raw)
    if verdict is None:
        return {"worth_responding": False, "class": SKIP_CLASS, "why": "[triage: no usable verdict]"}
    return verdict


# ---------------------------------------------------------------------------
# handled.jsonl helpers (dedup + I-LOGGED + thread-depth)
# ---------------------------------------------------------------------------


def _append_handled(record: Dict[str, Any], path: Optional[str] = None) -> None:
    target = path or HANDLED_LOG
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _mark(notification: Dict[str, Any], status: str, *, root_uri: Optional[str] = None,
          cls: Optional[str] = None, path: Optional[str] = None) -> None:
    _append_handled(
        {
            "ts": _now_iso(),
            "notification_uri": notification.get("uri"),
            "reason": notification.get("reason"),
            "author_handle": (notification.get("author") or {}).get("handle"),
            "action": f"converse:{status}",
            "converse_action": status,
            "converse_root": root_uri,
            "converse_class": cls,
        },
        path,
    )


def thread_depth(root_uri: Optional[str], path: Optional[str] = None) -> int:
    """
    How many reply-backs we've already SURFACED for this thread root — the
    anti-spiral counter. Reads handled.jsonl for converse records that reached
    'surfaced' (a proposed reply) sharing this root.
    """
    if not root_uri:
        return 0
    target = path or HANDLED_LOG
    if not os.path.exists(target):
        return 0
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
            if rec.get("converse_action") == "surfaced" and rec.get("converse_root") == root_uri:
                n += 1
    return n


# ---------------------------------------------------------------------------
# Item builder + orchestration
# ---------------------------------------------------------------------------


def _bsky_url(handle: str, uri: str) -> str:
    # at://did/app.bsky.feed.post/RKEY -> https://bsky.app/profile/<handle>/post/<rkey>
    try:
        rkey = uri.rsplit("/", 1)[-1]
        return f"https://bsky.app/profile/{handle}/post/{rkey}"
    except Exception:  # noqa: BLE001
        return ""


def make_surfaced_item(notification: Dict[str, Any], verdict: Dict[str, Any], draft_text: str) -> Dict[str, Any]:
    author = notification.get("author") or {}
    refs = extract_refs(notification)
    handle = author.get("handle", "")
    return {
        "uri": notification.get("uri"),
        "cid": notification.get("cid"),
        "author_handle": handle,
        "author_did": author.get("did"),
        "text": (notification.get("record") or {}).get("text", ""),
        "action": "reply",
        "confidence": "med",
        "why": verdict.get("why", ""),
        "url": _bsky_url(handle, notification.get("uri", "")),
        "source": "converse",
        "reply_class": verdict.get("class"),
        "draft_text": draft_text,
        **refs,
    }


def handle_incoming_reply(
    notification: Dict[str, Any],
    our_did: Optional[str],
    our_post_text: str = "",
    surface_fn: Optional[Callable[[List[Dict[str, Any]]], int]] = None,
    handled_path: Optional[str] = None,
) -> str:
    """
    Orchestrate one inbound stranger reply end-to-end. Returns a status string
    (also written to handled.jsonl). Never raises on the normal paths — a
    failure degrades to a skip+log, never to an un-gated post.

    surface_fn: injectable for tests; defaults to surface.surface_all (bot-token
    transport, captures the Slack ts so act_tick can gate on the 👍).
    """
    # I-NO-SELF — the infinite-loop guard. Never respond to our own account.
    if is_own_reply(notification, our_did):
        _mark(notification, "skipped_self", path=handled_path)
        return "skipped_self"

    if notification.get("reason") not in INCOMING_REASONS:
        _mark(notification, "skipped_reason", path=handled_path)
        return "skipped_reason"

    incoming_text = str((notification.get("record") or {}).get("text", "")).strip()

    verdict = triage_incoming(incoming_text, our_post_text)
    if not verdict.get("worth_responding"):
        _mark(notification, f"skipped_{verdict.get('class', 'skip')}", cls=verdict.get("class"), path=handled_path)
        return "skipped_not_worth"

    refs = extract_refs(notification)
    root_uri = refs.get("root_uri")

    # Anti-spiral: cap reply-backs per thread.
    depth = thread_depth(root_uri, path=handled_path)
    if depth >= _max_thread_depth():
        _mark(notification, "thread_depth_exceeded", root_uri=root_uri, cls=verdict.get("class"), path=handled_path)
        return "thread_depth_exceeded"

    # Draft the reply-back in PERSONA voice.
    try:
        draft = generate.generate({"raw": incoming_text, "type": "moment", "angle": verdict.get("why", "")}, kind="draft_reply")
    except Exception as exc:  # noqa: BLE001
        print(f"[converse] draft generation error: {exc!r}")
        _mark(notification, "draft_error", root_uri=root_uri, cls=verdict.get("class"), path=handled_path)
        return "draft_error"
    if not draft or not draft.strip():
        _mark(notification, "empty_draft", root_uri=root_uri, cls=verdict.get("class"), path=handled_path)
        return "empty_draft"

    # GUARD fail-closed (incl. client/project terms). A leaking draft is dropped
    # here and NEVER surfaced — the operator can't 👍 what he never sees.
    ok, reason = guard.check(draft)
    if not ok:
        print(f"[converse] GUARD BLOCKED draft reply-back: {reason}")
        _mark(notification, "guard_blocked", root_uri=root_uri, cls=verdict.get("class"), path=handled_path)
        return "guard_blocked"

    item = make_surfaced_item(notification, verdict, draft)
    fn = surface_fn if surface_fn is not None else _default_surface
    try:
        fn([item])
    except Exception as exc:  # noqa: BLE001 — a Slack hiccup must not crash the notify tick
        print(f"[converse] surface error (non-fatal): {exc!r}")
        _mark(notification, "surface_error", root_uri=root_uri, cls=verdict.get("class"), path=handled_path)
        return "surface_error"

    _mark(notification, "surfaced", root_uri=root_uri, cls=verdict.get("class"), path=handled_path)
    print(f"[converse] surfaced reply-back to @{item['author_handle']} (class={verdict.get('class')}) for operator 👍")
    return "surfaced"


def _default_surface(items: List[Dict[str, Any]]) -> int:
    # Imported lazily so tests that inject surface_fn don't pull the transport.
    import surface
    return surface.surface_all(items)
