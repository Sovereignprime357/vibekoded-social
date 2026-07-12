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
#
# WHY THESE LISTS GROW (and that's the point): sycophancy is a PRESSURE, not a fixed
# phrase — ban it at the front and it comes out the back, ban one stamp and the model
# reaches for a synonym. So this is a mechanical guard that is EXPECTED to accrete new
# entries as the model finds new ways to validate; that maintenance cost is the price
# of a guard that can actually FAIL a generation, and it's cheaper than trusting a
# prompt the trained-in reflex leaks straight past. (Live proof: "that's the play"
# shipped because it wasn't listed — and it's in the operator's own VOICE_PROFILE, so
# the profile itself coached the bypass. We don't edit the profile from here; we grow
# the gate.)
BANNED_OPENERS: List[str] = [
    # -- original 16 --
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
    # -- demonstrative-validation family (added v2 after "that's the play" leaked) --
    "that's the play",
    "that's it",
    "this is it",
    "that's exactly it",
    "exactly right",
    "that's the whole thing",
    "that's the point",
    "that's the one",
    "you get it",
    "you nailed it",
]

# --- Banned CLOSERS / trailing validation stamps (maintainable constant). Sycophancy
# migrates to the TAIL when the head is guarded (live proof: a reply that opened clean
# still closed on "you're building for the real world."). Matched case-insensitively on
# the LAST clause only — a stamp phrase MID-text is left alone (don't over-reject).
BANNED_CLOSERS: List[str] = [
    "you're building for the real world",
    "you're doing it right",
    "you get it",
    "you nailed it",
    "you're on the right track",
    "that's the right call",
    "keep going",
    "you're already there",
    "smart move",
    "well played",
]

# Leading noise we skip before an opener: whitespace, quotes (straight + curly),
# markdown emphasis, blockquote/list markers, leading dots/dashes.
_LEAD = r"[\s\"'“”‘’`*_>\-.]*"
# Trailing noise we allow AFTER a closer (before end-of-text): whitespace, quotes,
# markdown, and sentence-final punctuation. Mirror of _LEAD for the tail.
_TRAIL = r"[\s\"'“”‘’`*_.!?,;:)\]]*"


def _phrase_pattern(phrase: str, at: str) -> "re.Pattern[str]":
    """
    ONE compiler for both ends so the head- and tail-matchers can't drift apart.

      at="head" -> anchored at start after lead-noise; a trailing (?![a-z0-9]) keeps
                   "facts" from matching "factsheet" and makes "100%" a clean token.
      at="tail" -> a leading (?<![a-z0-9]) token boundary, then the phrase, then only
                   trailing noise/punctuation, then end-of-text -> matches the LAST
                   clause only (a mid-text stamp has real words after it, so it fails).

    Both are case-insensitive; callers normalize curly apostrophes first.
    """
    esc = re.escape(phrase)
    if at == "head":
        return re.compile(r"^" + _LEAD + esc + r"(?![a-z0-9])", re.IGNORECASE)
    return re.compile(r"(?<![a-z0-9])" + esc + _TRAIL + r"$", re.IGNORECASE)


_OPENER_PATTERNS = [(op, _phrase_pattern(op, "head")) for op in BANNED_OPENERS]
_CLOSER_PATTERNS = [(cl, _phrase_pattern(cl, "tail")) for cl in BANNED_CLOSERS]


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
    The MECHANICAL gate (see THE THESIS above). Returns (ok, reason). It guards
    BOTH ends of the text, because sycophancy is a pressure that migrates:

      - ok=False if the text contains ANY em-dash/en-dash (banned punctuation), or
        OPENS (at/near the start) with a praise-opener / validation stamp, or
        CLOSES (on its last clause) with a trailing validation stamp.
      - ok=True otherwise.

    Reason strings never include the generated text or the voice profile — only the
    generic rule that tripped — so logging a rejection can't leak either.
    """
    if not text or not text.strip():
        return False, "empty output"

    for d in _BANNED_DASHES:
        if d in text:
            return False, "contains an em-dash/en-dash (banned punctuation)"

    # Normalize curly apostrophes once so "you're"/"you’re" match at either end.
    norm = text.replace("’", "'").strip()
    for op, pattern in _OPENER_PATTERNS:
        if pattern.search(norm):
            return False, f"opens with a banned praise-opener: {op!r}"
    for cl, pattern in _CLOSER_PATTERNS:
        if pattern.search(norm):
            return False, f"closes with a banned validation stamp: {cl!r}"

    return True, ""
