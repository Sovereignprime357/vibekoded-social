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
from typing import Any, Dict, List, Optional

import generate

HERE = os.path.dirname(os.path.abspath(__file__))
MISSION_PATH = os.path.join(HERE, "MISSION-FILTER.md")

VALID_ACTIONS = {"like", "reply", "repost", "follow", "none"}
VALID_CONFIDENCE = {"high", "med", "low"}

# Triage wants a free model + determinism, distinct from the anthropic
# post-writer. TRIAGE_MODEL overrides; default gemini.
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "gemini")


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


# ---------------------------------------------------------------------------
# Deterministic stub (DRY_RUN / no-key path)
# ---------------------------------------------------------------------------


def _stub_verdict(candidate: Dict[str, Any], index: int) -> Dict[str, Any]:
    """
    Credential-free stand-in so the loop is testable. Deterministic: surfaces
    the first two candidates (one reply-ish if it contains a question, else a
    like), drops the rest. Clearly labelled so it's never confused with a real
    model verdict.
    """
    text = str(candidate.get("text", ""))
    surface = index < 2
    action = "reply" if ("?" in text and surface) else ("like" if surface else "none")
    return {
        "on_mission": surface,
        "lane": candidate.get("lane_id", ""),
        "action": action,
        "why": f"[DRY_RUN stub] no model ran; matched lane {candidate.get('lane_label','')}",
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
    rubric = rubric if rubric is not None else load_rubric()
    model = model or TRIAGE_MODEL

    raw = generate.complete(
        build_triage_prompt(candidate, rubric),
        model=model,
        temperature=0.1,
        max_tokens=256,
    )
    verdict = parse_verdict(raw)
    if verdict is None:
        verdict = _stub_verdict(candidate, index)

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
    """
    if not candidates:
        return []
    rubric = rubric if rubric is not None else load_rubric()
    model = model or TRIAGE_MODEL

    surfaced: List[Dict[str, Any]] = []
    for i, cand in enumerate(candidates):
        result = classify_one(cand, rubric=rubric, model=model, index=i)
        if result.get("on_mission") and result.get("action") != "none":
            surfaced.append(result)

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
