"""
content_refill.py — the daily queue top-up + 👍-gated rotation (SPEC-content-refill-v1).

The queue had no refill mechanism: starter seeds were finite, and once used, post_tick
returned "success" on an empty queue and the bot silently went quiet. This module:
  1. GENERATES N pillar-rotated candidates from APPROVED sources (evergreen renewable
     floor + optional graduated intel — NEVER raw/ungraduated ops intel),
  2. GUARDS each fail-closed and dedups vs posted.jsonl BEFORE surfacing,
  3. SURFACES them to the review channel (C0BGDT13CN7) via the bot token with a
     slack_ts + pillar + provenance,
  4. on the operator's 👍, ENQUEUES the approved candidate into content-queue.jsonl as
     a pre-approved `final_text` (posted verbatim later), with an approval trail,
  5. fires a loud queue-empty alert when unused entries hit 0.

Reuses generate (pillar-aware), guard (privacy, fail-closed), content_queue (rotation +
append), and act's 👍-poll (operator_thumbsup / get_reactions). It NEVER touches the
intel distill/dream pipeline (I-SEPARATION) and NEVER reads ops-intel-log.jsonl
(I-NO-RAW-INTEL) — graduated intel arrives only via an explicit graduated file.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional, Set

import requests

import act
import content_queue
import generate
import guard

HERE = os.path.dirname(os.path.abspath(__file__))
SURFACED_PATH = os.path.join(HERE, "refill-surfaced.jsonl")
STATE_PATH = os.path.join(HERE, "refill-state.json")
POSTED_PATH = os.path.join(HERE, "posted.jsonl")
# Graduated intel INPUT (produced by the SEPARATE dream/Council pipeline — NOT built
# here). Optional; absent by default. This is the ONLY intel source and it is
# post-graduation. ops-intel-log.jsonl (raw) is intentionally never referenced.
GRADUATED_PATH = os.path.join(HERE, "graduated-intel.jsonl")

DEFAULT_REFILL_CHANNEL = "C0BGDT13CN7"
SLACK_POST_MESSAGE_EP = "https://slack.com/api/chat.postMessage"

DEFAULT_COUNT = 5                 # I-CADENCE-EARNED: 5/day
EMPTY_ALERT_COOLDOWN_S = 6 * 3600  # don't spam the queue-empty alert

META = "meta"
META_WINDOW = content_queue.META_WINDOW  # META <= 1-in-5, same rule as the queue


# ---------------------------------------------------------------------------
# Approved sources (I-NO-RAW-INTEL): evergreen renewable floor + graduated intel
# ---------------------------------------------------------------------------

# Evergreen pillar content — real, on-brand, non-client truths about how this shop
# builds. The renewable floor (SPEC INPUTS #2). generate.build_prompt turns each into
# an in-voice post. NOT client material, NOT raw intel.
EVERGREEN_SEEDS: List[Dict[str, str]] = [
    {"pillar": "showcase", "type": "moment", "raw": "the whole account runs on short-lived github actions scripts, no daemon — scan, triage, surface, act: each a cron that wakes, does one thing, commits its state, and exits.", "angle": "the architecture is the receipt"},
    {"pillar": "showcase", "type": "moment", "raw": "every public action is human-gated behind a slack 👍 — the bot proposes, the operator disposes. autonomy is earned per action-class, never granted wholesale.", "angle": "gated autonomy as a feature"},
    {"pillar": "operator", "type": "decision", "raw": "the operator's rule: define what has to be true before you generate a line. spec the invariants first, then let the model fill them in. the discipline is the moat.", "angle": "spec-first over vibe-first"},
    {"pillar": "operator", "type": "decision", "raw": "vibe-coding done right isn't 'no rules' — it's the human holding the vision and the invariants while the AI writes the four hundred lines that satisfy them.", "angle": "what vibe-coding actually is"},
    {"pillar": "ask-help", "type": "moment", "raw": "genuine ask for anyone wiring human-in-the-loop approvals: how do you keep the gate strict without standing up a backend? we run slack reactions → repository_dispatch. what's worked for you?", "angle": "real open question, asked plainly"},
    {"pillar": "ask-help", "type": "moment", "raw": "if you've built an earned-autonomy ladder for an agent — surface, then auto-like, then auto-reply — what actually graduated a class for you: metrics, or a gut call after watching it?", "angle": "crowdsource the ladder"},
    {"pillar": "question", "type": "moment", "raw": "how are you handling agent memory across sessions in prod? we run a flat file plus an index and it beat the deep folder tree. curious what's actually working for people.", "angle": "specific, invites builders"},
    {"pillar": "question", "type": "moment", "raw": "for anyone running scheduled agents on github actions: how do you deal with cron lag? we've seen 2-4h gaps, so we moved latency-critical wakes to an external ping → repository_dispatch.", "angle": "shared pain, shared fix"},
    {"pillar": "dreaming", "type": "moment", "raw": "where this goes: not a broadcaster but a participant — an agent that finds the right builders, engages for real, and earns trust one action-class at a time. the ladder is the point, not the shortcut.", "angle": "the vision, grounded"},
    {"pillar": "dreaming", "type": "moment", "raw": "the dream is a shop where the system that markets the work is itself a working piece of the work — the account is the demo, not the ad.", "angle": "the account is the demo"},
    {"pillar": "meta", "type": "moment", "raw": "him: i built a bot to run this. me: he described a vibe in a slack message and i wrote the lines that held the invariants. we share one login and one of us can't write hello world.", "angle": "the two-hander, seasoning"},
]

# Valid pillars this loop can produce (subset of content_queue.VALID_PILLARS present
# in the evergreen floor).
_PILLARS_AVAILABLE = sorted({s["pillar"] for s in EVERGREEN_SEEDS})


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def _count() -> int:
    v = os.environ.get("REFILL_COUNT", "").strip()
    if v:
        try:
            return max(1, int(v))
        except ValueError:
            pass
    return DEFAULT_COUNT


def _channel() -> str:
    return os.environ.get("SLACK_CHANNEL_REFILL", "").strip() or DEFAULT_REFILL_CHANNEL


def load_graduated(path: Optional[str] = None) -> List[Dict[str, str]]:
    """
    Load GRADUATED intel seeds (post dream+Council) if the file exists. Optional and
    empty by default — the dream pipeline (out of scope here) writes it. Each row must
    carry pillar + raw + a graduated marker. This is the ONLY intel path, and it is
    graduated-only; raw ops intel is never read here (I-NO-RAW-INTEL).
    """
    target = path or GRADUATED_PATH
    if not os.path.exists(target):
        return []
    out: List[Dict[str, str]] = []
    try:
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Fail-safe gate: only rows explicitly marked graduated are eligible.
                if not rec.get("graduated"):
                    continue
                pillar = str(rec.get("pillar", "")).strip().lower()
                raw = str(rec.get("raw", "")).strip()
                if raw and pillar in content_queue.VALID_PILLARS:
                    out.append({"pillar": pillar, "type": str(rec.get("type", "moment")),
                                "raw": raw, "angle": str(rec.get("angle", "")),
                                "source": "graduated", "graduated_id": rec.get("id")})
    except OSError:
        return []
    return out


# ---------------------------------------------------------------------------
# Pillar rotation for the batch (I-PILLAR-ROTATION)
# ---------------------------------------------------------------------------


def select_pillars(n: int, recent_pillars: Optional[List[str]] = None,
                   available: Optional[List[str]] = None) -> List[str]:
    """
    Choose a sequence of n pillars honoring I-PILLAR-ROTATION: no two consecutive the
    same, and META at most once per META_WINDOW (across the recent tail + this batch).
    Greedy + deterministic (round-robins the available non-meta pillars), so the
    reviewer sees a varied batch. `recent_pillars` = most-recent-first posted pillars.
    """
    avail = [p for p in (available or _PILLARS_AVAILABLE)]
    non_meta = [p for p in avail if p != META]
    recent = [str(p or "").strip().lower() for p in (recent_pillars or [])]

    seq: List[str] = []
    # trailing window = recent tail + what we've chosen, most-recent-first
    def _window_has_meta() -> bool:
        window = (seq[::-1] + recent)[: META_WINDOW - 1]
        return META in window

    last = recent[0] if recent else None
    ni = 0
    for _ in range(n):
        # META allowed only if it wouldn't break the 1-in-window cap and isn't consecutive.
        pick = None
        # rotate through non-meta first for variety
        for _try in range(len(non_meta)):
            cand = non_meta[ni % len(non_meta)] if non_meta else None
            ni += 1
            if cand and cand != last:
                pick = cand
                break
        # occasional meta if the window allows and we have it available
        if META in avail and not _window_has_meta() and (len(seq) % META_WINDOW == META_WINDOW - 1) and last != META:
            pick = META
        if pick is None:
            pick = (non_meta[ni % len(non_meta)] if non_meta else (avail[0] if avail else META))
            ni += 1
        seq.append(pick)
        last = pick
    return seq


def _seed_pool_by_pillar(graduated: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Evergreen floor + graduated, grouped by pillar. Evergreen is the renewable base."""
    pool: Dict[str, List[Dict[str, str]]] = {}
    for s in EVERGREEN_SEEDS:
        pool.setdefault(s["pillar"], []).append({**s, "source": s.get("source", "evergreen")})
    for g in graduated:
        pool.setdefault(g["pillar"], []).append(g)
    return pool


# ---------------------------------------------------------------------------
# Dedup vs posted.jsonl
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def posted_texts(path: Optional[str] = None) -> Set[str]:
    target = path or POSTED_PATH
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
                    if rec.get("text"):
                        seen.add(_normalize(rec["text"]))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return seen


# ---------------------------------------------------------------------------
# Candidate generation (generate + guard fail-closed + dedup)
# ---------------------------------------------------------------------------


def _candidate_id(text: str) -> str:
    return hashlib.sha1(_normalize(text).encode("utf-8")).hexdigest()[:12]


def generate_candidates(
    n: Optional[int] = None,
    recent_pillars: Optional[List[str]] = None,
    graduated: Optional[List[Dict[str, str]]] = None,
    already_posted: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Produce up to n guarded, deduped, pillar-rotated candidates. Each is generated in
    PERSONA voice (generate), then privacy-guarded FAIL-CLOSED (I-GUARDED) and deduped
    vs posted.jsonl BEFORE it can be surfaced. A blocked/dup/empty candidate is dropped.
    Returns candidate records (no network beyond the model call inside generate).
    """
    n = _count() if n is None else n
    graduated = load_graduated() if graduated is None else graduated
    already = posted_texts() if already_posted is None else already_posted
    pool = _seed_pool_by_pillar(graduated)
    pillars = select_pillars(n, recent_pillars, available=sorted(pool.keys()))
    thin = not graduated  # no fresh graduated intel this cycle -> leaning evergreen

    used_seed_ids: Set[str] = set()
    batch_norms: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for pillar in pillars:
        seeds = pool.get(pillar) or []
        if not seeds:
            continue
        # prefer a seed we haven't used this batch (reduce evergreen repetition)
        seed = next((s for s in seeds if id(s) not in used_seed_ids), seeds[0])
        used_seed_ids.add(id(seed))
        try:
            text = generate.generate({"raw": seed["raw"], "type": seed.get("type", "moment"),
                                      "pillar": pillar, "angle": seed.get("angle", "")}, kind="post")
        except Exception as exc:  # noqa: BLE001
            print(f"[content_refill] generation error for pillar {pillar}: {exc!r}")
            continue
        if not text or not text.strip():
            continue
        norm = _normalize(text)
        if norm in already or norm in batch_norms:
            print(f"[content_refill] dropped duplicate candidate (pillar {pillar}).")
            continue
        ok, reason = guard.check(text)  # I-GUARDED fail-closed BEFORE surfacing
        if not ok:
            print(f"[content_refill] GUARD BLOCKED candidate (pillar {pillar}): {reason}")
            continue
        batch_norms.add(norm)
        source = seed.get("source", "evergreen")
        out.append({
            "id": _candidate_id(text),
            "ts": _now_iso(),
            "text": text,
            "pillar": pillar,
            "type": seed.get("type", "moment"),
            "source": source,
            "freshness": "evergreen" if source == "evergreen" else "fresh",
            "provenance": {"source": source, "pillar": pillar, "seed": seed.get("raw", "")[:120],
                           "graduated_id": seed.get("graduated_id")},
        })
    if thin and out:
        print("[content_refill] low-freshness batch (evergreen floor — no graduated intel this cycle).")
    return out


# ---------------------------------------------------------------------------
# Slack transport + surfaced ledger
# ---------------------------------------------------------------------------


def format_candidate(rec: Dict[str, Any]) -> str:
    fresh = " · ⚠️ evergreen (low-freshness)" if rec.get("freshness") == "evergreen" else ""
    # PILLAR on its OWN line: Slack collapses several bot messages posted in the same
    # second into one visual block, so the header alone is ambiguous in the stack.
    # An explicit label makes each card self-identifying no matter how it's collapsed.
    return (
        f"*🧵 CONTENT CANDIDATE* · source: {rec.get('source')}{fresh}\n"
        f"*PILLAR: {rec.get('pillar')}*\n"
        f"_👍 to approve → enqueues to the posting queue (posts verbatim)_\n"
        f"> {rec.get('text')}"
    )


def _post_slack_web(text: str, token: str, channel: str, timeout: int = 15) -> Optional[str]:
    try:
        resp = requests.post(
            SLACK_POST_MESSAGE_EP,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json={"channel": channel, "text": text},
            timeout=timeout,
        )
        data = resp.json() if resp.content else {}
    except (requests.RequestException, ValueError) as exc:
        print(f"[content_refill] Slack post error (non-fatal): {exc}")
        return None
    if not data.get("ok"):
        print(f"[content_refill] Slack post rejected for channel {channel}: {data.get('error')}")
        return None
    return data.get("ts")


def _append_ledger(rec: Dict[str, Any], path: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _load_ledger(path: str) -> Dict[str, Dict[str, Any]]:
    """id -> latest record (status merges: a later 'enqueued' line supersedes 'surfaced')."""
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = rec.get("id")
                if not cid:
                    continue
                if cid in out and rec.get("status") == "surfaced" and out[cid].get("status") == "enqueued":
                    continue  # never downgrade enqueued -> surfaced
                out[cid] = {**out.get(cid, {}), **rec}
    except OSError:
        pass
    return out


def _order_for_stack(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Never leave META at the BOTTOM of the collapsed Slack stack. Slack stacks
    same-second bot messages and the bottom one is the easiest to react to; META
    ending up there (it was always generated last) is exactly what funneled a 👍 onto
    the one pillar rotation refuses to post. Float META to the TOP; others keep their
    (already pillar-rotated) order, so the bottom of the stack varies run to run rather
    than always being the same pillar. Deterministic, no randomness needed.
    """
    meta = [c for c in candidates if str(c.get("pillar") or "").strip().lower() == META]
    rest = [c for c in candidates if str(c.get("pillar") or "").strip().lower() != META]
    return meta + rest


def surface_candidates(
    candidates: List[Dict[str, Any]],
    token: Optional[str] = None,
    channel: Optional[str] = None,
    dry_run: Optional[bool] = None,
    surfaced_path: Optional[str] = None,
    recent_pillars: Optional[List[str]] = None,
) -> int:
    """
    Post each candidate to the review channel, capture its slack_ts, and record it in
    the surfaced ledger (status "surfaced"). Returns the count surfaced. Safe-degrade:
    no token -> print only, NO ledger, NEVER auto-posts to the public timeline.

    ROTATION-AWARE (the funnel fix): a candidate whose pillar rotation would reject
    RIGHT NOW (same pillar as the last post, or META inside the 1-in-META_WINDOW cap)
    is NOT surfaced — because a 👍 must always mean "this will post", and surfacing an
    un-postable candidate is the trap that quietly stalled the feed. `recent_pillars`
    (most-recent-first, from posted.jsonl) drives the SAME predicate post_tick uses.
    If EVERY candidate is blocked we surface nothing AND fire the queue-health alert,
    so the operator hears about it instead of getting a silent empty batch.
    """
    dry_run = _is_dry_run() if dry_run is None else dry_run
    token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = channel or _channel()
    surfaced_path = surfaced_path or SURFACED_PATH
    if not candidates:
        return 0

    # Drop candidates rotation would reject now; explain each drop (no silent trap).
    eligible: List[Dict[str, Any]] = []
    for rec in candidates:
        if content_queue.rotation_eligible(rec.get("pillar"), recent_pillars):
            eligible.append(rec)
        else:
            print(f"[content_refill] NOT surfacing rotation-blocked candidate "
                  f"(pillar {rec.get('pillar')}); a thumbs-up must always mean 'this will post'.")

    if not eligible:
        # Degenerate case: the whole batch is un-postable. Do NOT surface an empty
        # batch silently — fire the existing queue-health alert so the operator hears
        # it (queue_empty_alert owns the empty case; else the rotation-blocked alert).
        print("[content_refill] entire batch is rotation-blocked; surfacing nothing and firing a queue-health alert.")
        if not queue_empty_alert(token=token, channel=channel, dry_run=dry_run):
            queue_rotation_blocked_alert(recent_pillars=recent_pillars, token=token,
                                         channel=channel, dry_run=dry_run)
        return 0

    count = 0
    for rec in _order_for_stack(eligible):
        card = format_candidate(rec)
        if dry_run:
            print("---- [DRY_RUN] content candidate ----")
            print(card)
            continue
        if not token:
            print("[content_refill] no SLACK_BOT_TOKEN; candidate NOT surfaced (never auto-posted). Preview:")
            print(card)
            continue
        slack_ts = _post_slack_web(card, token, channel)
        _append_ledger({
            "id": rec["id"], "ts": _now_iso(), "status": "surfaced",
            "text": rec["text"], "pillar": rec["pillar"], "type": rec["type"],
            "source": rec["source"], "freshness": rec.get("freshness"),
            "provenance": rec.get("provenance"),
            "slack_ts": slack_ts, "slack_channel": channel,
        }, surfaced_path)
        if slack_ts:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 👍-gated enqueue (I-HUMAN-GATE-CONTENT) — reuses act's operator-thumbsup poll
# ---------------------------------------------------------------------------


def poll_and_enqueue(
    token: Optional[str] = None,
    channel: Optional[str] = None,
    operator_id: Optional[str] = None,
    dry_run: Optional[bool] = None,
    surfaced_path: Optional[str] = None,
) -> int:
    """
    Poll the review channel for the OPERATOR's 👍 on surfaced candidates; each 👍'd
    candidate is appended to content-queue.jsonl as a pre-approved `final_text` with a
    provenance/approval trail, then marked enqueued (dedup). Returns count enqueued.

    I-HUMAN-GATE-CONTENT: nothing enqueues without the operator's explicit 👍. Reuses
    act.operator_thumbsup (fail-closed without an operator id) + act.get_reactions.
    """
    dry_run = _is_dry_run() if dry_run is None else dry_run
    token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = channel or _channel()
    surfaced_path = surfaced_path or SURFACED_PATH
    if not token:
        return 0

    ledger = _load_ledger(surfaced_path)
    pending = [r for r in ledger.values() if r.get("status") == "surfaced" and r.get("slack_ts")]
    if not pending:
        return 0
    if not operator_id:
        print("[content_refill] SLACK_OPERATOR_USER_ID not set — cannot verify operator 👍; "
              "nothing enqueues (I-HUMAN-GATE-CONTENT fail-closed).")
        return 0

    enqueued = 0
    for rec in pending:
        reactions = act.get_reactions(rec["slack_channel"], rec["slack_ts"], token)
        if not act.operator_thumbsup(reactions, operator_id):
            continue  # not approved yet — leave surfaced for a later poll
        if dry_run:
            print(f"[content_refill] DRY_RUN would enqueue candidate {rec['id']} (pillar {rec.get('pillar')})")
            continue
        prov = dict(rec.get("provenance") or {})
        prov.update({"approved_by": operator_id, "approved_at": _now_iso(),
                     "slack_ts": rec.get("slack_ts"), "candidate_id": rec["id"]})
        try:
            content_queue.append_entry(
                raw=rec["text"], type=rec.get("type", "moment"), pillar=rec.get("pillar"),
                final_text=rec["text"], provenance=prov,
            )
        except Exception as exc:  # noqa: BLE001 — a bad entry must not crash the tick
            print(f"[content_refill] enqueue failed for {rec['id']}: {exc!r}")
            continue
        _append_ledger({"id": rec["id"], "ts": _now_iso(), "status": "enqueued",
                        "slack_ts": rec.get("slack_ts"), "slack_channel": rec.get("slack_channel")}, surfaced_path)
        enqueued += 1
        print(f"[content_refill] enqueued approved candidate {rec['id']} (pillar {rec.get('pillar')}).")
    return enqueued


# ---------------------------------------------------------------------------
# Queue-empty alert
# ---------------------------------------------------------------------------


def _load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(path: str, state: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def queue_empty_alert(
    token: Optional[str] = None,
    channel: Optional[str] = None,
    dry_run: Optional[bool] = None,
    state_path: Optional[str] = None,
    now: Optional[float] = None,
) -> bool:
    """
    If the queue has 0 unused entries, post a LOUD alert to the review channel (with a
    cooldown so it doesn't spam). Returns True if an alert was (or would be, in dry-run)
    sent. This is the guardrail against the silent-drain that started all this.
    """
    dry_run = _is_dry_run() if dry_run is None else dry_run
    token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = channel or _channel()
    state_path = state_path or STATE_PATH
    now = time.time() if now is None else now

    if content_queue.count_unused() > 0:
        return False

    state = _load_state(state_path)
    last = float(state.get("last_empty_alert", 0) or 0)
    if last and (now - last) < EMPTY_ALERT_COOLDOWN_S:
        return False  # already alerted recently (only suppress if we HAVE alerted before)

    msg = ("🚨 *CONTENT QUEUE EMPTY* — 0 unused entries. post-tick has nothing to post "
           "(the bot will go quiet). 👍 some candidates in this channel to refill the queue.")
    if dry_run:
        print(f"[content_refill] DRY_RUN queue-empty alert: {msg}")
        return True
    if token and _post_slack_web(msg, token, channel):
        state["last_empty_alert"] = now
        _save_state(state_path, state)
        print("[content_refill] posted queue-empty alert.")
        return True
    return False


def queue_rotation_blocked_alert(
    recent_pillars: Optional[List[str]] = None,
    token: Optional[str] = None,
    channel: Optional[str] = None,
    dry_run: Optional[bool] = None,
    state_path: Optional[str] = None,
    now: Optional[float] = None,
) -> bool:
    """
    The SILENT-DEADLOCK alert (SPEC-content-refill v1.1): the queue is NON-empty but
    pillar rotation blocks EVERY item (get_next_rotated -> None), so post_tick skips
    forever with no signal. That's how "a week disappears". Post a loud, actionable
    alert to the review channel naming which pillars are queued vs blocked and exactly
    what to do. This does NOT weaken the rotation guard — it just breaks the silence.

    Non-spammy: alert on entering a NEW blocked state (by content signature) OR at most
    once per cooldown while a blocked state persists. Returns True if alerted.
    (Empty queue is handled by queue_empty_alert; healthy queue -> no-op + clears the
    blocked marker so a future block re-alerts.)
    """
    dry_run = _is_dry_run() if dry_run is None else dry_run
    token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = channel or _channel()
    state_path = state_path or STATE_PATH
    now = time.time() if now is None else now

    unused = content_queue.get_all_unused()
    if not unused:
        return False  # empty -> queue_empty_alert owns this
    if content_queue.get_next_rotated(recent_pillars=recent_pillars) is not None:
        # Healthy: something IS postable. Clear any prior blocked marker so the next
        # time we enter a blocked state we alert again (don't let it be a one-time).
        state = _load_state(state_path)
        if state.get("last_blocked_sig") or state.get("last_blocked_alert"):
            state.pop("last_blocked_sig", None)
            state.pop("last_blocked_alert", None)
            if not dry_run:
                _save_state(state_path, state)
        return False

    # Rotation-blocked. Build a self-explanatory breakdown.
    counts: Dict[str, int] = {}
    for r in unused:
        p = (r.get("pillar") or "untagged").strip().lower()
        counts[p] = counts.get(p, 0) + 1
    breakdown = ", ".join(f"{n}×{p}" for p, n in sorted(counts.items()))
    sig = f"{len(unused)}|{breakdown}"
    last_posted = (recent_pillars or ["none"])[0] or "none"

    state = _load_state(state_path)
    same = state.get("last_blocked_sig") == sig
    last_ts = float(state.get("last_blocked_alert", 0) or 0)
    if same and last_ts and (now - last_ts) < EMPTY_ALERT_COOLDOWN_S:
        return False  # same blocked state, alerted recently -> no spam

    msg = (
        f"⚠️ *QUEUE ROTATION-BLOCKED* — {len(unused)} item(s) queued but NONE postable. "
        f"Queued: {breakdown}. Last posted pillar: *{last_posted}*.\n"
        "Rotation won't post META back-to-back or more than 1-in-5, and won't repeat the "
        "last pillar — so the queue is stuck. 👍 a *non-META* candidate in this channel to unblock."
    )
    if dry_run:
        print(f"[content_refill] DRY_RUN rotation-blocked alert: {msg}")
        return True
    if token and _post_slack_web(msg, token, channel):
        state["last_blocked_sig"] = sig
        state["last_blocked_alert"] = now
        _save_state(state_path, state)
        print("[content_refill] posted rotation-blocked alert.")
        return True
    return False
