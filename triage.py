"""
triage.py — the TRIAGE step of the agentic loop (SPEC-v2.md).

Takes scout.py's candidates and asks a FREE model (Gemini/Groq via
generate.complete) one question per candidate: "given the mission filter, is
this on-mission, and if so what's the proposed action + why + confidence?"
Keeps only the on-mission, actionable ones and hands them to SURFACE.

Cost note: this is the only always-on model in the loop, and it's free.
Claude is never called here — the paid model only ever writes a reply, and
only after the operator approves one. See SPEC-v2.md ARCHITECTURE.

DRY_RUN / no free key: generate.complete() returns "", so triage falls back
to a transparent deterministic stub classifier — enough to exercise the
SURFACE step end-to-end without any credentials, and clearly labelled
"[DRY_RUN]" so a stub verdict is never mistaken for a real one.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import generate

HERE = os.path.dirname(os.path.abspath(__file__))
MISSION_PATH = os.path.join(HERE, "MISSION-FILTER.md")

VALID_ACTIONS = {"like", "reply", "repost", "follow", "none"}
VALID_CONFIDENCE = {"high", "med", "low"}

# Triage wants determinism + reliable structured JSON. Default ANTHROPIC (Haiku):
# the free models proved unusable for a scanner — Gemini's free tier is 20 req/day,
# and Groq's gpt-oss-120b burns its budget on hidden reasoning (empty output) while
# the 8k tokens/minute cap 429s us. Haiku doesn't reason-bloat and has no free-tier
# TPM wall; batching (rubric once per batch) keeps it ~$2/mo. TRIAGE_MODEL overrides
# back to gemini|groq if ever needed.
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "anthropic")

# Batch size for classification. The rubric (~1.8k tokens) is the expensive part
# of a triage prompt; sending it ONCE per batch of N posts instead of once PER
# post cuts tokens ~15-25x, which is what keeps us under Groq's free-tier 8k
# tokens/minute ceiling. ~15 covers a full 60-candidate tick in ~4 calls.
TRIAGE_BATCH_SIZE = int(os.environ.get("SCOUT_TRIAGE_BATCH_SIZE", "15"))
# Small pause between batches to spread token spend across the TPM window. The
# real safety net is generate.complete()'s 429 backoff; this just reduces how
# often we hit it.
TRIAGE_BATCH_PAUSE_S = float(os.environ.get("SCOUT_TRIAGE_BATCH_PAUSE_S", "2"))


def _key_env_for(model: str) -> str:
    return {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(
        model, "GROQ_API_KEY"
    )


def _no_model_reason(model: str) -> Optional[str]:
    """
    If a real model call would be skipped (DRY_RUN or the key isn't set), return
    a truthful human-readable reason; else None. Mirrors generate.complete()'s
    early "" gate so triage knows a "" came from "no model" and not a swallowed
    API error (those now raise, and are labelled separately).
    """
    if generate._is_dry_run():
        return "DRY_RUN"
    if not os.environ.get(_key_env_for(model), "").strip():
        return f"no {_key_env_for(model)} set"
    return None


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------

_rubric_cache: Optional[str] = None


def load_rubric(mission_path: str = MISSION_PATH) -> str:
    """Read MISSION-FILTER.md verbatim — it's the triage model's entire rulebook."""
    global _rubric_cache
    if _rubric_cache is not None:
        return _rubric_cache
    if not os.path.exists(mission_path):
        raise FileNotFoundError(f"MISSION-FILTER.md not found at {mission_path}")
    with open(mission_path, "r", encoding="utf-8") as f:
        _rubric_cache = f.read()
    return _rubric_cache


# ---------------------------------------------------------------------------
# Prompt + parsing
# ---------------------------------------------------------------------------


def build_triage_prompt(candidate: Dict[str, Any], rubric: str) -> str:
    text = str(candidate.get("text", "")).strip()
    handle = candidate.get("author_handle", "")
    lane_label = candidate.get("lane_label", "")
    return (
        "You are the triage filter for a build-in-public Bluesky account that engages "
        "genuinely with builders. Apply the MISSION FILTER below strictly.\n\n"
        "<MISSION FILTER>\n"
        f"{rubric}\n"
        "</MISSION FILTER>\n\n"
        "Classify this single post:\n"
        f"author: @{handle}\n"
        f"lane it matched in search: {lane_label}\n"
        f'text: """{text}"""\n\n'
        "Respond with ONLY a JSON object — no prose, no markdown fence:\n"
        '{"on_mission": true|false, "lane": "<lane id or empty>", '
        '"action": "like|reply|repost|follow|none", '
        '"why": "<one concrete line, max 140 chars>", '
        '"confidence": "high|med|low"}\n\n'
        "Rules:\n"
        "- If it trips ANY hard-no (politics, drama, vendor flame wars, spam/shill, NSFW), "
        'set on_mission=false and action="none".\n'
        '- action must be "none" whenever on_mission is false.\n'
        '- Only propose "reply" when there is a real, specific thing to contribute; '
        'otherwise prefer "like".\n'
    )


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _normalize_verdict(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Coerce one parsed verdict object into the canonical shape, clamping invalid
    fields (unknown action -> none, bad confidence -> low, not-on-mission can
    never carry an action). Returns None if `obj` isn't a dict.
    """
    if not isinstance(obj, dict):
        return None

    action = str(obj.get("action", "none")).strip().lower()
    if action not in VALID_ACTIONS:
        action = "none"
    confidence = str(obj.get("confidence", "low")).strip().lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    on_mission = obj.get("on_mission", False)
    if isinstance(on_mission, str):
        on_mission = on_mission.strip().lower() in ("true", "yes", "1")
    on_mission = bool(on_mission)

    # Integrity: not-on-mission can never carry an action.
    if not on_mission:
        action = "none"

    return {
        "on_mission": on_mission,
        "lane": str(obj.get("lane", "")).strip(),
        "action": action,
        "why": str(obj.get("why", "")).strip()[:200],
        "confidence": confidence,
    }


def parse_verdict(raw: str) -> Optional[Dict[str, Any]]:
    """
    Extract + validate the JSON verdict from raw model text. Tolerates a
    ```json fence or leading/trailing prose by grabbing the first {...} span.
    Returns a normalized dict, or None if nothing parseable/valid.
    """
    if not raw or not raw.strip():
        return None
    m = _JSON_OBJ_RE.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return _normalize_verdict(obj)


# ---------------------------------------------------------------------------
# Batch prompt + parsing (the token fix)
# ---------------------------------------------------------------------------


def build_batch_prompt(candidates: List[Dict[str, Any]], rubric: str) -> str:
    """
    One prompt classifying N posts at once. The rubric is included ONCE (not once
    per post), which is the whole point — it's the ~1.8k-token part of a triage
    call, so sending it per-post is what blew the free-tier TPM budget.
    """
    lines = []
    for i, cand in enumerate(candidates):
        text = str(cand.get("text", "")).strip()
        handle = cand.get("author_handle", "")
        lane_label = cand.get("lane_label", "")
        lines.append(
            f"[{i}] author=@{handle} | matched_lane={lane_label}\n"
            f'    text: """{text}"""'
        )
    posts_block = "\n".join(lines)
    return (
        "You are the triage filter for a build-in-public Bluesky account that engages "
        "genuinely with builders. Apply the MISSION FILTER below strictly.\n\n"
        "<MISSION FILTER>\n"
        f"{rubric}\n"
        "</MISSION FILTER>\n\n"
        f"Classify EACH of the following {len(candidates)} posts:\n\n"
        f"{posts_block}\n\n"
        "Respond with ONLY a JSON array of objects — no prose, no markdown fence. One object "
        "per post, in the SAME ORDER, each including its index:\n"
        '[{"index": 0, "on_mission": true|false, "lane": "<lane id or empty>", '
        '"action": "like|reply|repost|follow|none", '
        '"why": "<one concrete line, max 140 chars>", "confidence": "high|med|low"}, ...]\n\n'
        "Rules:\n"
        "- If a post trips ANY hard-no (politics, drama, vendor flame wars, spam/shill, NSFW), "
        'set on_mission=false and action="none".\n'
        '- action must be "none" whenever on_mission is false.\n'
        '- Only propose "reply" when there is a real, specific thing to contribute; '
        'otherwise prefer "like".\n'
        "- Return exactly one object per post, all indices present.\n"
    )


_JSON_ARR_RE = re.compile(r"\[.*\]", re.DOTALL)


def parse_batch_verdicts(raw: str, n: int) -> List[Optional[Dict[str, Any]]]:
    """
    Extract N verdicts from a batch response. Returns a list of length n; each
    slot is a normalized verdict dict or None if that ONE post's object was
    missing/malformed. Defensive by design: a single bad object never discards
    the whole batch (the user's explicit requirement).
    """
    out: List[Optional[Dict[str, Any]]] = [None] * n
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

    # Map by explicit "index" when present, else fall back to position.
    for pos, item in enumerate(arr):
        try:
            if not isinstance(item, dict):
                continue
            idx = item.get("index", pos)
            idx = int(idx) if isinstance(idx, (int, float, str)) and str(idx).lstrip("-").isdigit() else pos
            if not (0 <= idx < n):
                continue
            verdict = _normalize_verdict(item)
            if verdict is not None:
                out[idx] = verdict
        except Exception:  # noqa: BLE001 — one bad item must never kill the batch
            continue
    return out


# ---------------------------------------------------------------------------
# Deterministic stub (DRY_RUN / no-key path)
# ---------------------------------------------------------------------------


def _stub_verdict(candidate: Dict[str, Any], index: int, reason: str = "no model ran") -> Dict[str, Any]:
    """
    Credential-free stand-in so the loop is testable WITHOUT a key (DRY_RUN or
    no API key). Deterministic: surfaces the first two candidates (one reply-ish
    if it contains a question, else a like), drops the rest. The `reason` is the
    TRUTH about why no model ran — never the old "[DRY_RUN stub]" lie that hid a
    live rate-limit. This path is only reached when there is genuinely no model
    to call; a real API failure raises and is labelled separately.
    """
    text = str(candidate.get("text", ""))
    surface = index < 2
    action = "reply" if ("?" in text and surface) else ("like" if surface else "none")
    return {
        "on_mission": surface,
        "lane": candidate.get("lane_id", ""),
        "action": action,
        "why": f"[stub: {reason}] matched lane {candidate.get('lane_label','')}",
        "confidence": "low",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_one(
    candidate: Dict[str, Any],
    rubric: Optional[str] = None,
    model: Optional[str] = None,
    index: int = 0,
) -> Dict[str, Any]:
    """
    Classify a single candidate. Returns the candidate merged with the triage
    verdict fields (on_mission, lane, action, why, confidence). Falls back to
    the stub verdict when no model runs (DRY_RUN or missing free key) or when
    the model's output can't be parsed.
    """
    model = model or TRIAGE_MODEL

    reason = _no_model_reason(model)
    if reason:
        verdict = _stub_verdict(candidate, index, reason=reason)
    else:
        rubric = rubric if rubric is not None else load_rubric()
        try:
            raw = generate.complete(
                build_triage_prompt(candidate, rubric),
                model=model,
                temperature=0.1,
                max_tokens=256,
            )
            verdict = parse_verdict(raw)
        except generate.GenerationError as exc:
            # A REAL failure — tell the truth, don't surface a fake verdict.
            print(f"[triage failed: {exc}]")
            verdict = None
        if verdict is None:
            # Model ran but returned nothing parseable — non-surfacing, honest label.
            verdict = {
                "on_mission": False,
                "lane": candidate.get("lane_id", ""),
                "action": "none",
                "why": "[triage: no usable verdict from model]",
                "confidence": "low",
            }

    merged = dict(candidate)
    merged.update(verdict)
    return merged


def classify_all(
    candidates: List[Dict[str, Any]],
    rubric: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Triage every candidate; return only the ones worth SURFACING (on_mission
    AND action != "none"), each merged with its verdict. Order preserved.

    Live path batches candidates (rubric sent ONCE per batch) to fit the free
    model's tokens/minute ceiling, with 429 backoff inside generate.complete().
    On a real API failure the batch surfaces NOTHING and logs the true cause —
    no more stub garbage mislabeled as a dry-run.
    """
    if not candidates:
        return []
    model = model or TRIAGE_MODEL
    reason = _no_model_reason(model)
    key_env = _key_env_for(model)

    print(
        f"[triage] model={model} key_present={bool(os.environ.get(key_env, '').strip())} "
        f"dry_run={generate._is_dry_run()} candidates={len(candidates)} "
        f"batch_size={TRIAGE_BATCH_SIZE} reason={reason or 'live'}"
    )

    surfaced: List[Dict[str, Any]] = []

    # No model available (DRY_RUN or no key): deterministic per-index stub so the
    # pipeline stays testable without credentials. Never reached when a real key
    # is set — a live API error raises and is handled in the batch loop below.
    if reason:
        for i, cand in enumerate(candidates):
            verdict = _stub_verdict(cand, i, reason=reason)
            if verdict.get("on_mission") and verdict.get("action") != "none":
                surfaced.append({**cand, **verdict})
        print(f"[triage] {len(candidates)} candidate(s) -> {len(surfaced)} on-mission (stub: {reason})")
        return surfaced

    # Live: classify in batches, rubric included once per batch.
    rubric = rubric if rubric is not None else load_rubric()
    batches = [candidates[i : i + TRIAGE_BATCH_SIZE] for i in range(0, len(candidates), TRIAGE_BATCH_SIZE)]
    for bi, chunk in enumerate(batches):
        try:
            raw = generate.complete(
                build_batch_prompt(chunk, rubric),
                model=model,
                temperature=0.1,
                max_tokens=min(len(chunk) * 80 + 120, 2048),
            )
        except generate.GenerationError as exc:
            # The truthful line the whole night was missing. Surface nothing.
            print(f"[triage failed: {exc}]  (batch {bi + 1}/{len(batches)}, {len(chunk)} post(s) not surfaced)")
            continue

        verdicts = parse_batch_verdicts(raw, len(chunk))
        for cand, vd in zip(chunk, verdicts):
            if vd is None:
                print(f"[triage] unparseable verdict for {cand.get('uri')}; skipped")
                continue
            if vd.get("on_mission") and vd.get("action") != "none":
                surfaced.append({**cand, **vd})

        if bi < len(batches) - 1 and TRIAGE_BATCH_PAUSE_S > 0:
            time.sleep(TRIAGE_BATCH_PAUSE_S)

    print(f"[triage] {len(candidates)} candidate(s) -> {len(surfaced)} on-mission for surfacing")
    return surfaced


if __name__ == "__main__":
    # Manual smoke: feed a fake candidate through triage (uses the free model
    # if a key is set, else the stub).
    demo = {
        "uri": "at://x/app.bsky.feed.post/1",
        "cid": "c1",
        "author_handle": "someone.bsky.social",
        "author_did": "did:plc:x",
        "text": "how is everyone handling agents losing all context between sessions?",
        "lane_id": "memory",
        "lane_label": "Agent memory / context engineering",
        "url": "https://bsky.app/profile/someone.bsky.social/post/1",
    }
    print(json.dumps(classify_one(demo), indent=2))
