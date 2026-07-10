"""
ops_insight.py — the Ops-Insight Harvest (SPEC-v6): a review-only knowledge lens.

A SECOND triage lens over data the bot ALREADY pulls (scout candidates + notify
replies). When a post carries a genuine, TRANSFERABLE build-with-AI technique that
could improve the operator's own systems, it: cheaply FLAGS it, deeply EXTRACTS a
structured brief (bounded), privacy-guards the brief, and posts it to a review-only
Slack channel with PROVENANCE. It NEVER acts and NEVER writes to any brain/memory.

Invariants (SPEC-v6):
  - I-NO-AUTO-BRAIN : only Slack + a dedup ledger; no brain/memory write anywhere.
  - I-PROVENANCE    : every brief carries author handle + permalink, or it isn't posted.
  - I-PRIVACY       : guard the whole rendered brief, fail-closed (drop, never sanitize).
  - I-DEDUP         : each source post surfaced at most once (ops-insight-seen.jsonl).
  - I-REVIEW-ONLY   : no 👍/action wiring; does NOT enter the act-layer ledger.
  - I-REUSE-ONLY    : reuses already-pulled data; adds no Bluesky scan.
  - I-COST-BOUNDED  : cheap batched flag first; deep extract only on flagged, hard-capped.

The lens is deliberately NARROW/high-bar — over-flagging is the failure mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

import generate
import guard

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_PATH = os.path.join(HERE, "ops-insight-seen.jsonl")
# The LOG BRIDGE (SPEC-v6.1): every posted-worthy brief is ALSO appended here so the
# operator's nightly distill (runs on his PC, can't see CI) can fetch it from the repo.
# Public repo -> fetchable via raw.githubusercontent.com with no auth.
LOG_PATH = os.path.join(HERE, "ops-intel-log.jsonl")

DEFAULT_OPS_CHANNEL = "C0BGEB5FNGZ"  # ops-intel review channel (not secret; overridable via env)
SLACK_POST_MESSAGE_EP = "https://slack.com/api/chat.postMessage"

DEFAULT_MAX_PER_TICK = 3   # hard cap on DEEP extracts per tick (cost bound)
DEFAULT_FLAG_LIMIT = 40    # max fresh items sent through the cheap flag per tick

# Terminal outcomes recorded in the seen ledger so an item is never reconsidered.
_TERMINAL = {"not_insight", "extract_empty", "no_provenance", "guard_blocked", "posted", "post_failed"}


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _flag_model() -> str:
    # Cheap model for the always-on flag pass (free/low-cost). Default = triage's.
    return (os.environ.get("OPS_INSIGHT_FLAG_MODEL") or os.environ.get("TRIAGE_MODEL") or "groq").strip().lower()


def _extract_model_id() -> Optional[str]:
    # Optional stronger-than-Haiku model for the DEEP extract (bounded by the cap).
    # Runs on the anthropic engine; overrides only the model name for this call.
    v = os.environ.get("OPS_INSIGHT_MODEL", "").strip()
    return v or None


def _max_per_tick() -> int:
    v = os.environ.get("OPS_INSIGHT_MAX_PER_TICK", "").strip()
    if v:
        try:
            return max(0, int(v))
        except ValueError:
            pass
    return DEFAULT_MAX_PER_TICK


def _ops_channel() -> str:
    return os.environ.get("SLACK_CHANNEL_OPS_INTEL", "").strip() or DEFAULT_OPS_CHANNEL


# ---------------------------------------------------------------------------
# Item normalization (reuse-only: shapes scout candidates + notify replies)
# ---------------------------------------------------------------------------


def _bsky_url(handle: str, uri: str) -> str:
    try:
        rkey = uri.rsplit("/", 1)[-1]
        return f"https://bsky.app/profile/{handle}/post/{rkey}"
    except Exception:  # noqa: BLE001
        return ""


def normalize_item(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Coerce a scout candidate OR a notify-derived dict into the lens's shape:
    {uri, text, author_handle, url}. Returns None if it lacks a uri or text.
    Builds the permalink from handle+uri when absent (I-PROVENANCE needs it).
    """
    if not isinstance(raw, dict):
        return None
    uri = raw.get("uri")
    text = str(raw.get("text", "")).strip()
    if not uri or not text:
        return None
    handle = raw.get("author_handle") or (raw.get("author") or {}).get("handle") or ""
    url = raw.get("url") or (_bsky_url(handle, uri) if handle else "")
    return {"uri": uri, "text": text, "author_handle": handle, "url": url}


# ---------------------------------------------------------------------------
# Stage 1 — cheap batched FLAG (narrow, high bar)
# ---------------------------------------------------------------------------

_FLAG_LENS = (
    "You screen builder posts for ONE thing: a genuine, TRANSFERABLE technique or piece of "
    "knowledge about BUILDING WITH AI — AI orchestration, agentic workflows, prompt/spec "
    "technique, evals, agent memory, tooling — that could plausibly improve how an AI-building "
    "operator builds their own systems.\n"
    "FLAG (ops_insight=true) ONLY for that. Do NOT flag: generic hot-takes, opinions, "
    "engagement bait, news, announcements, marketing, vague enthusiasm. The bar is HIGH — "
    "when in doubt, do NOT flag. Most posts are NOT insights."
)


def build_flag_batch_prompt(texts: List[str]) -> str:
    lines = []
    for i, t in enumerate(texts):
        snippet = t.strip().replace("\n", " ")
        if len(snippet) > 500:
            snippet = snippet[:500] + "…"
        lines.append(f'[{i}] """{snippet}"""')
    block = "\n".join(lines)
    return (
        f"{_FLAG_LENS}\n\n"
        f"Classify EACH of these {len(texts)} posts. Respond with ONLY a JSON array — no prose, "
        "no fence — one object per post in the SAME ORDER, each: "
        '{"index": <i>, "ops_insight": true|false}\n\n'
        f"{block}\n"
    )


_JSON_ARR_RE = re.compile(r"\[.*\]", re.DOTALL)


def parse_flag_batch(raw: str, n: int) -> List[bool]:
    """Return a length-n list of bools. Anything unparseable/missing => False (no flag)."""
    out = [False] * n
    if not raw or not raw.strip():
        return out
    m = _JSON_ARR_RE.search(raw)
    if not m:
        return out
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return out
    if not isinstance(arr, list):
        return out
    for pos, item in enumerate(arr):
        try:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("index", pos))
            if not (0 <= idx < n):
                continue
            val = item.get("ops_insight", False)
            if isinstance(val, str):
                val = val.strip().lower() in ("true", "yes", "1")
            out[idx] = bool(val)
        except Exception:  # noqa: BLE001 — one bad element never flips others to True
            continue
    return out


def flag_items(items: List[Dict[str, Any]], model: Optional[str] = None) -> List[bool]:
    """
    Cheap batched flag over `items`. Returns a bool per item. No model available
    (DRY_RUN / missing key) => all False (safe-degrade: nothing flagged, no noise).
    A real API error also degrades to all-False here (the flag is best-effort;
    an insight missed this tick is cheaper than crashing the tick).
    """
    if not items:
        return []
    model = model or _flag_model()
    try:
        raw = generate.complete(build_flag_batch_prompt([it["text"] for it in items]),
                                model=model, temperature=0.0, max_tokens=min(len(items) * 20 + 60, 1200))
    except generate.GenerationError as exc:
        print(f"[ops_insight] flag pass failed ({exc}); flagging nothing this tick.")
        return [False] * len(items)
    return parse_flag_batch(raw, len(items))


# ---------------------------------------------------------------------------
# Stage 2 — deep EXTRACT (bounded, stronger model allowed)
# ---------------------------------------------------------------------------


def build_extract_prompt(text: str, author_handle: str) -> str:
    return (
        "A builder posted the text below. It appears to contain a transferable technique for "
        "building with AI. Extract a tight brief for an operator who builds AI-orchestrated "
        "software (a spec-driven framework, an autonomous social bot, client builds).\n\n"
        f'Author: @{author_handle}\nPost:\n"""{text.strip()}"""\n\n'
        "Respond with ONLY a JSON object — no prose, no fence:\n"
        "{\n"
        '  "insight": "<the technique/knowledge, concretely, 1-2 sentences>",\n'
        '  "applies": "<why it applies to our systems, specific>",\n'
        '  "effect": "<what adopting it would actually do>",\n'
        '  "why_improves": "<why it would improve our vibe-coded outcomes>"\n'
        "}\n"
        "If on reflection there is NO genuinely transferable technique here (just opinion / "
        'hype / vagueness), return {"insight": ""} and nothing else. Be honest — a false '
        "positive wastes the operator's attention."
    )


def parse_brief(raw: str) -> Optional[Dict[str, str]]:
    """Extract the brief object. None if unparseable OR if `insight` is empty (a decline)."""
    if not raw or not raw.strip():
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    insight = str(obj.get("insight", "")).strip()
    if not insight:
        return None  # the extractor declined — respect the high bar
    return {
        "insight": insight,
        "applies": str(obj.get("applies", "")).strip(),
        "effect": str(obj.get("effect", "")).strip(),
        "why_improves": str(obj.get("why_improves", "")).strip(),
    }


def extract_brief(item: Dict[str, Any], model_id: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Deep extract on ONE flagged item (stronger model allowed via model_id). None on decline/failure."""
    try:
        raw = generate.complete(
            build_extract_prompt(item["text"], item.get("author_handle", "")),
            model="anthropic",
            temperature=0.2,
            max_tokens=500,
            model_id=model_id if model_id is not None else _extract_model_id(),
        )
    except generate.GenerationError as exc:
        print(f"[ops_insight] extract failed for {item.get('uri')}: {exc}")
        return None
    return parse_brief(raw)


# ---------------------------------------------------------------------------
# Render (with PROVENANCE) — I-PROVENANCE
# ---------------------------------------------------------------------------


def format_brief(brief: Dict[str, str], item: Dict[str, Any]) -> Optional[str]:
    """
    Render the Slack brief. Returns None if provenance is missing (author + link) —
    I-PROVENANCE: a brief that can't be fact-checked is not posted.
    """
    handle = str(item.get("author_handle", "")).strip()
    url = str(item.get("url", "")).strip()
    if not handle or not url:
        return None
    lines = [
        "*🧠 OPS-INSIGHT* · _review-only — nothing is actioned or saved to the brain_",
        f"*Insight:* {brief['insight']}",
    ]
    if brief.get("applies"):
        lines.append(f"*Why it applies to us:* {brief['applies']}")
    if brief.get("effect"):
        lines.append(f"*What it would do:* {brief['effect']}")
    if brief.get("why_improves"):
        lines.append(f"*Why it improves our vibe-coded outcomes:* {brief['why_improves']}")
    lines.append(f"*Source:* @{handle} — {url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dedup ledger (I-DEDUP) + Slack transport
# ---------------------------------------------------------------------------


def load_seen(path: Optional[str] = None) -> set:
    target = path or SEEN_PATH
    seen: set = set()
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


def mark_seen(item: Dict[str, Any], status: str, path: Optional[str] = None) -> None:
    target = path or SEEN_PATH
    rec = {"uri": item.get("uri"), "author_handle": item.get("author_handle"),
           "status": status, "ts": _now_iso()}
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# The LOG BRIDGE (SPEC-v6.1): mirror briefs to a repo file for the nightly distill
# ---------------------------------------------------------------------------


def _stable_id(source_uri: str) -> str:
    """Deterministic short id from the source post uri — a stable key for the nightly."""
    return hashlib.sha1(str(source_uri or "").encode("utf-8")).hexdigest()[:12]


def load_logged_uris(path: Optional[str] = None) -> set:
    """Source URIs already in the log — dedup so a brief is logged at most once."""
    target = path or LOG_PATH
    out: set = set()
    if not os.path.exists(target):
        return out
    try:
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("source_uri"):
                        out.add(rec["source_uri"])
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def append_log(brief: Dict[str, str], item: Dict[str, Any], path: Optional[str] = None) -> bool:
    """
    Append one full brief to ops-intel-log.jsonl for the nightly distill. Deduped by
    source uri (logged once). Returns True if written, False if already present.
    Each entry carries the full brief + provenance (author + link) + ts + a stable id.
    """
    target = path or LOG_PATH
    uri = item.get("uri")
    if uri and uri in load_logged_uris(target):
        return False
    rec = {
        "id": _stable_id(uri),
        "source_uri": uri,
        "ts": _now_iso(),
        "author_handle": item.get("author_handle"),
        "link": item.get("url"),
        "insight": brief.get("insight", ""),
        "applies": brief.get("applies", ""),
        "effect": brief.get("effect", ""),
        "why_improves": brief.get("why_improves", ""),
    }
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def post_brief(text: str, token: str, channel: str, timeout: int = 15) -> bool:
    """Post the brief to the ops-intel channel. Never raises; returns success."""
    try:
        resp = requests.post(
            SLACK_POST_MESSAGE_EP,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json={"channel": channel, "text": text},
            timeout=timeout,
        )
        data = resp.json() if resp.content else {}
    except (requests.RequestException, ValueError) as exc:
        print(f"[ops_insight] Slack post error (non-fatal): {exc}")
        return False
    if not data.get("ok"):
        print(f"[ops_insight] Slack post rejected for channel {channel}: {data.get('error')}")
        return False
    return True


# ---------------------------------------------------------------------------
# Orchestration — the harvest
# ---------------------------------------------------------------------------


def harvest(
    items: List[Dict[str, Any]],
    dry_run: Optional[bool] = None,
    token: Optional[str] = None,
    channel: Optional[str] = None,
    seen_path: Optional[str] = None,
    flag_model: Optional[str] = None,
    extract_model_id: Optional[str] = None,
    max_per_tick: Optional[int] = None,
    log_path: Optional[str] = None,
) -> int:
    """
    Run the harvest over already-pulled `items` (scout candidates / notify replies).
    Returns the number of briefs posted. Never raises on the normal paths — a
    failure degrades to "surfaced nothing", never breaks the caller's tick.
    """
    dry_run = _is_dry_run() if dry_run is None else dry_run
    token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = channel or _ops_channel()
    seen_path = seen_path or SEEN_PATH
    log_path = log_path or LOG_PATH
    max_per_tick = _max_per_tick() if max_per_tick is None else max_per_tick

    normalized = [n for n in (normalize_item(it) for it in (items or [])) if n]
    if not normalized:
        return 0

    seen = load_seen(seen_path)
    # Fresh + de-duped within this batch, capped for the cheap flag pass.
    fresh: List[Dict[str, Any]] = []
    batch_uris: set = set()
    for it in normalized:
        u = it["uri"]
        if u in seen or u in batch_uris:
            continue
        batch_uris.add(u)
        fresh.append(it)
        if len(fresh) >= DEFAULT_FLAG_LIMIT:
            break
    if not fresh:
        return 0

    flags = flag_items(fresh, flag_model)

    posted = 0
    extracted = 0
    for it, is_flag in zip(fresh, flags):
        if not is_flag:
            mark_seen(it, "not_insight", seen_path)
            continue
        if extracted >= max_per_tick:
            # Hard per-tick cap on the DEEP model. Remaining flagged items are left
            # UNSEEN so a later tick re-considers them (cheap re-flag) — bounded.
            print(f"[ops_insight] per-tick extract cap reached ({extracted}/{max_per_tick}); deferring the rest.")
            break
        extracted += 1

        brief = extract_brief(it, extract_model_id)
        if not brief:
            mark_seen(it, "extract_empty", seen_path)
            continue

        text = format_brief(brief, it)
        if text is None:
            mark_seen(it, "no_provenance", seen_path)  # I-PROVENANCE
            continue

        # I-PRIVACY: guard the WHOLE brief, fail-closed. A leak is dropped, never posted.
        ok, reason = guard.check(text)
        if not ok:
            print(f"[ops_insight] GUARD BLOCKED brief from {it.get('uri')}: {reason}")
            mark_seen(it, "guard_blocked", seen_path)
            continue

        if dry_run:
            print("---- [DRY_RUN] ops-insight brief ----")
            print(text)
            # side-effect-free preview: no ledger write, no log write, no post.
            continue

        # LOG BRIDGE: record the post-worthy brief for the nightly distill BEFORE the
        # Slack attempt, so the intel is captured even if Slack rejects (bot not in
        # channel). Deduped by source uri (logged once).
        append_log(brief, it, log_path)

        if token and channel and post_brief(text, token, channel):
            mark_seen(it, "posted", seen_path)
            posted += 1
            print(f"[ops_insight] posted brief to {channel} (source {it.get('uri')})")
        else:
            # Post failed (e.g. bot not invited to the channel) — mark seen to bound
            # cost (no infinite re-extract). Operator must invite the bot (setup step).
            mark_seen(it, "post_failed", seen_path)
            print(f"[ops_insight] brief NOT posted (no token or channel rejected) for {it.get('uri')}; marked seen.")

    return posted
