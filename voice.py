"""
voice.py — the operator's voice profile (injected) + a MECHANICAL anti-sycophancy gate.

SECURITY (critical): the voice profile is the operator's MOAT and this repo is PUBLIC.
It arrives ONLY as the `VOICE_PROFILE` env var (a GitHub secret) — it is NEVER committed,
NEVER written to disk in the repo, and NEVER logged/echoed. This module reads it from the
environment and hands it to the prompt builder in-memory; nothing here prints it.

THE THESIS (why the gate is code, not a prompt):
  You cannot prompt sycophancy out of a model — the praise-opener reflex ("that's the
  move", "great question") and the em-dash tic are trained in and LEAK past instructions.
  So the guard is EXTERNAL and MECHANICAL, and — unlike a prompt — it can actually FAIL a
  generation. check_voice() is that hard gate: it runs post-generation, before anything is
  surfaced or posted, and a violation is rejected (regenerate, then DROP), never shipped.
"""

from __future__ import annotations

import os
import re
from typing import List, Tuple

# --- Banned punctuation: any em-dash or en-dash is a hard fail. Plain hyphen-minus
# ("-", as in "spec-first") is fine — only these two are the machine tell.
_BANNED_DASHES = ("—", "–")  # — em-dash, – en-dash

# --- Banned praise-openers / validation stamps (maintainable constant). Matched
# case-insensitively at/near the START of the text (after any leading quotes/markdown/
# punctuation). This is the sycophancy reflex the operator flagged.
BANNED_OPENERS: List[str] = [
    "that's the move",
    "great question",
    "good question",
    "you're absolutely right",
    "you're right",
    "exactly this",
    "spot on",
    "nailed it",
    "so true",
    "this is the way",
    "love this",
    "great point",
    "well said",
    "couldn't agree more",
    "100%",
    "facts",
]

# Leading noise we skip before checking for an opener: whitespace, quotes (straight +
# curly), markdown emphasis, blockquote/list markers, leading dots/dashes.
_LEAD = r"[\s\"'“”‘’`*_>\-.]*"


def _compile_opener(op: str) -> "re.Pattern[str]":
    # Anchored at start (after lead noise); trailing (?![a-z0-9]) so "facts" doesn't
    # match "factsheet" and "100%" is a clean token. Case-insensitive.
    return re.compile(r"^" + _LEAD + re.escape(op) + r"(?![a-z0-9])", re.IGNORECASE)


_OPENER_PATTERNS = [(op, _compile_opener(op)) for op in BANNED_OPENERS]


def load_voice_profile() -> str:
    """The operator's voice profile from the VOICE_PROFILE secret, or '' if absent."""
    return os.environ.get("VOICE_PROFILE", "").strip()


def voice_profile_block() -> str:
    """
    A prompt fragment injecting the voice profile — or '' when the secret is absent
    (SAFE-DEGRADE: the caller's prompt is unchanged and generation continues on the
    base persona). Never logged; only returned for in-memory prompt assembly.
    """
    profile = load_voice_profile()
    if not profile:
        return ""
    return (
        "\n\n---\n\nOPERATOR VOICE PROFILE (match this voice precisely — it OVERRIDES any "
        "generic assistant style; write as this person writes):\n"
        f"{profile}\n"
        "Hard voice rules: no em-dashes or en-dashes (use a period or comma). Do NOT open "
        "with praise or validation (no \"that's the move\", \"great question\", \"you're "
        "right\", etc.). Engage with the actual substance; don't flex jargon to sound smart."
    )


def check_voice(text: str) -> Tuple[bool, str]:
    """
    The MECHANICAL gate (see THE THESIS above). Returns (ok, reason).

      - ok=False if the text contains ANY em-dash/en-dash (banned punctuation), or
        opens (at/near the start) with a praise-opener / validation stamp.
      - ok=True otherwise.

    Reason strings never include the generated text or the voice profile — only the
    generic rule that tripped — so logging a rejection can't leak either.
    """
    if not text or not text.strip():
        return False, "empty output"

    for d in _BANNED_DASHES:
        if d in text:
            return False, "contains an em-dash/en-dash (banned punctuation)"

    # Normalize curly apostrophes so "you're" and "you’re" both match.
    head = text.replace("’", "'")
    for op, pattern in _OPENER_PATTERNS:
        if pattern.search(head):
            return False, f"opens with a banned praise-opener: {op!r}"

    return True, ""
