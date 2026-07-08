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

import os
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

# ---------------------------------------------------------------------------
# EXTRA (operator-added) TERMS — client/project-confidential words.
#
# CRITICAL (SPEC-v3 I-PRIVACY): this repo is PUBLIC. Client names, project
# code-names, and any confidential term the brain knows must NEVER be committed
# here — putting them in a tracked file would leak the very thing the guard
# exists to protect. So the extra terms load ONLY from non-committed sources:
#
#   1. GUARD_EXTRA_TERMS  — env var (a GitHub *secret* in CI), comma- OR
#                           newline-separated. This is the primary channel.
#   2. a gitignored FILE  — default guard-extra-terms.txt beside this module
#                           (path overridable via GUARD_EXTRA_TERMS_FILE), one
#                           term per line, '#' comments allowed. For local runs.
#   3. config.EXTRA_TERMS — legacy in-repo list, kept for backward compat. It
#                           should stay EMPTY in this public repo; real terms go
#                           in the two channels above.
#
# All three are merged. Every loader is best-effort and NEVER raises — a broken
# source degrades to "that source contributes nothing", never to "no guard".
# The hardcoded floor above always applies regardless.
# ---------------------------------------------------------------------------

_DEFAULT_EXTRA_TERMS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "guard-extra-terms.txt"
)


def _split_terms(blob: str) -> List[str]:
    """Split a comma- or newline-separated blob into cleaned, non-empty terms."""
    parts = re.split(r"[,\n\r]+", blob or "")
    out: List[str] = []
    for p in parts:
        t = p.strip()
        if t and not t.startswith("#"):
            out.append(t)
    return out


def _load_extra_terms_env() -> List[str]:
    """Terms from the GUARD_EXTRA_TERMS env var (the CI secret). Never raises."""
    try:
        return _split_terms(os.environ.get("GUARD_EXTRA_TERMS", ""))
    except Exception:
        print("[guard] WARNING: GUARD_EXTRA_TERMS failed to parse; ignoring that source.")
        return []


def _load_extra_terms_file() -> List[str]:
    """Terms from the gitignored guard-extra-terms.txt (local dev). Never raises."""
    path = os.environ.get("GUARD_EXTRA_TERMS_FILE", "").strip() or _DEFAULT_EXTRA_TERMS_FILE
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return _split_terms(f.read())
    except Exception:
        print(f"[guard] WARNING: extra-terms file {path!r} failed to load; ignoring that source.")
        return []


def _load_extra_terms_config() -> List[str]:
    """Legacy in-repo config.EXTRA_TERMS (should be empty in this public repo)."""
    try:
        from config import EXTRA_TERMS  # type: ignore

        if isinstance(EXTRA_TERMS, (list, tuple)):
            return [str(t).strip() for t in EXTRA_TERMS if str(t).strip()]
        return []
    except ImportError:
        return []
    except Exception:
        print(
            "[guard] WARNING: EXTRA_TERMS in config.py failed to load cleanly; "
            "continuing without that source."
        )
        return []


def _load_extra_terms() -> List[str]:
    """
    Merge all non-committed extra-term sources (env secret + gitignored file +
    legacy config). De-duped case-insensitively, order preserved. Never raises.
    """
    merged = _load_extra_terms_env() + _load_extra_terms_file() + _load_extra_terms_config()
    seen: set = set()
    out: List[str] = []
    for t in merged:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


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
