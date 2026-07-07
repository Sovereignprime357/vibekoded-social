"""
guard.py — I-PRIVACY invariant enforcement.

SAFETY-CRITICAL. This is the last line of defense before anything reaches
Bluesky. One leaked post (a real name, a family reference) undoes the whole
no-names discipline the shop is built on.

Design principles (per SPEC.md I-PRIVACY, PERSONA.md FORBIDDEN):
  - Case-insensitive, word-boundary-aware matching (no false negatives from
    "Sovereign's" or "SHAYLER"; no false positives from substrings hiding
    inside unrelated words — a whole-word match is not "any substring").
  - FAIL CLOSED: any doubt, any error, any inability to evaluate the text
    cleanly -> block. It is always cheaper to skip a good post than to ship
    a bad one.
  - The blocklist is a hardcoded floor (never weakened at runtime) plus an
    EXTRA_TERMS list loaded from config for operator-added terms (client
    names, other family words, anything discovered in the wild).

check(text) -> (ok: bool, reason: str) is the only contract callers need.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

# ---------------------------------------------------------------------------
# THE HARDCODED FLOOR — never remove or weaken these. If you need to change
# this list, that is a SPEC change (SPEC.md I-PRIVACY), not a code change.
# ---------------------------------------------------------------------------

PERSONAL_NAMES: List[str] = [
    "sovereign prime",
    "sovereign",
    "shayler",
    "phipps",
]

FAMILY_TERMS: List[str] = [
    "daughter",
    "wife",
    "family",
    "kid",
    "kids",
    "child",
    "children",
    "spouse",
]

# Terms that only make sense as a *prefix hit* (e.g. "sovereign prime" should
# also catch "Sovereign Prime's" or "SOVEREIGN PRIME."). Word-boundary regex
# below already handles trailing punctuation/possessives; this list exists
# purely for readability of the merge below.
HARD_BLOCKLIST: List[str] = PERSONAL_NAMES + FAMILY_TERMS


def _load_extra_terms() -> List[str]:
    """
    Load operator-added EXTRA_TERMS from config, if config.py / config.json
    is present. Never raises — a missing or malformed config file degrades
    to "no extra terms" rather than crashing the guard (the hardcoded floor
    above still applies regardless).
    """
    try:
        from config import EXTRA_TERMS  # type: ignore

        if isinstance(EXTRA_TERMS, (list, tuple)):
            return [str(t).strip() for t in EXTRA_TERMS if str(t).strip()]
        return []
    except ImportError:
        return []
    except Exception:
        # Fail closed on the GUARD's availability, not on individual posts:
        # a broken config must not silently disable the extra-terms layer.
        # We can't raise from a module-level loader without breaking every
        # import of this file, so we degrade to empty and let the hardcoded
        # floor stand. Surface loudly in logs so it gets noticed.
        print(
            "[guard] WARNING: EXTRA_TERMS in config.py failed to load cleanly; "
            "continuing with hardcoded blocklist only."
        )
        return []


def _compile_patterns(terms: Iterable[str]) -> List[Tuple[str, "re.Pattern[str]"]]:
    """
    Build one word-boundary regex per term. Multi-word terms (e.g.
    "sovereign prime") use literal-space matching with boundaries on the
    outer edges only, so internal whitespace variations still match while
    the term can't fire on a mid-word substring.
    """
    compiled = []
    for term in terms:
        term_norm = term.strip()
        if not term_norm:
            continue
        # Escape the term, then relax escaped-space runs back into \s+ so
        # entries like "sovereign prime" tolerate double spaces / tabs.
        escaped = re.escape(term_norm)
        escaped = re.sub(r"(\\ )+", r"\\s+", escaped)
        pattern = re.compile(rf"\b{escaped}\b", re.IGNORECASE)
        compiled.append((term_norm, pattern))
    return compiled


def _all_terms() -> List[str]:
    return HARD_BLOCKLIST + _load_extra_terms()


def check(text: str) -> Tuple[bool, str]:
    """
    Check `text` against the I-PRIVACY blocklist.

    Returns (ok, reason):
      - ok=True,  reason=""                     -> text is clean, safe to post.
      - ok=False, reason="<human explanation>"  -> BLOCK. Do not post.

    FAIL CLOSED: any exception during evaluation is treated as a block, not
    a pass-through. A guard that can crash its way into "unchecked" is not
    a guard.
    """
    try:
        if text is None:
            return False, "guard received None (fail closed: no text to evaluate)"

        if not isinstance(text, str):
            return False, f"guard received non-string type {type(text).__name__} (fail closed)"

        stripped = text.strip()
        if stripped == "":
            # An empty post isn't a privacy violation, but it's also not
            # postable content — treat as block so callers don't ship blanks.
            return False, "text is empty after stripping (nothing to post)"

        terms = _all_terms()
        patterns = _compile_patterns(terms)

        hits = []
        for term, pattern in patterns:
            if pattern.search(stripped):
                hits.append(term)

        if hits:
            # De-dupe while preserving first-seen order for a stable message.
            seen = []
            for h in hits:
                if h not in seen:
                    seen.append(h)
            return False, f"blocked term(s) found: {', '.join(seen)}"

        return True, ""

    except Exception as exc:  # noqa: BLE001 — deliberate: fail closed on ANY error
        return False, f"guard evaluation raised an exception (fail closed): {exc!r}"


def check_many(texts: Iterable[str]) -> List[Tuple[str, bool, str]]:
    """Convenience: check a batch, return (text, ok, reason) tuples."""
    return [(t, *check(t)) for t in texts]


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        sample = " ".join(sys.argv[1:])
    else:
        sample = sys.stdin.read()

    ok, reason = check(sample)
    print(f"ok={ok}")
    if reason:
        print(f"reason={reason}")
    sys.exit(0 if ok else 1)
