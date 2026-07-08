"""
tests/test_guard.py — proving guard.py is bulletproof.

This is the safety-critical test file for the whole project. It must prove:
  1. Every hardcoded blocklist term is caught, standalone.
  2. Every term is caught INSIDE a sentence (not just as the whole string).
  3. Case variations are caught (upper, lower, mixed, title case).
  4. Possessive / trailing-punctuation variants are caught.
  5. Clean, voice-accurate text PASSES (no false positives on ordinary
     words that happen to contain a blocked term as a substring, e.g.
     "family" as a whole word should block, but a word like "familiar"
     must NOT false-positive).
  6. Fail-closed behavior on bad input (None, non-string, empty).
  7. EXTRA_TERMS from config are honored when present.
  8. The example posts in PERSONA.md itself pass clean (they're the voice
     anchor — if they don't pass, the guard is miscalibrated against the
     persona it's supposed to be protecting).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guard  # noqa: E402


# ---------------------------------------------------------------------------
# 1 & 3 & 4: every hardcoded term, standalone, across case + punctuation
# variants.
# ---------------------------------------------------------------------------

ALL_HARDCODED_TERMS = guard.PERSONAL_NAMES + guard.FAMILY_TERMS


def _case_variants(term: str):
    return [term.lower(), term.upper(), term.title(), term.capitalize()]


def test_every_personal_name_blocked_standalone():
    for term in guard.PERSONAL_NAMES:
        ok, reason = guard.check(term)
        assert ok is False, f"personal name {term!r} was NOT blocked"
        assert reason  # non-empty explanation


def test_every_family_term_blocked_standalone():
    for term in guard.FAMILY_TERMS:
        ok, reason = guard.check(term)
        assert ok is False, f"family term {term!r} was NOT blocked"
        assert reason


def test_every_term_blocked_in_all_case_variants():
    for term in ALL_HARDCODED_TERMS:
        for variant in _case_variants(term):
            ok, reason = guard.check(variant)
            assert ok is False, f"case variant {variant!r} of {term!r} was NOT blocked"


def test_every_term_blocked_inside_a_sentence():
    for term in ALL_HARDCODED_TERMS:
        sentence = f"just shipped a fix today, big thanks to {term} for the idea."
        ok, reason = guard.check(sentence)
        assert ok is False, f"term {term!r} embedded in a sentence was NOT blocked: {sentence!r}"


def test_every_term_blocked_with_trailing_punctuation():
    for term in ALL_HARDCODED_TERMS:
        for suffix in ["'s", ".", ",", "!", "?", ":"]:
            text = f"shoutout to {term}{suffix} today"
            ok, reason = guard.check(text)
            assert ok is False, f"{term!r} with suffix {suffix!r} was NOT blocked: {text!r}"


def test_every_term_blocked_at_start_and_end_of_text():
    for term in ALL_HARDCODED_TERMS:
        start_text = f"{term} was mentioned in the standup today."
        end_text = f"today's standup mentioned {term}"
        assert guard.check(start_text)[0] is False, f"{term!r} at start of text not blocked"
        assert guard.check(end_text)[0] is False, f"{term!r} at end of text not blocked"


def test_specific_named_examples_from_spec():
    """Literal examples pulled straight from SPEC.md / CLAUDE.md's own invariant text."""
    blocked_examples = [
        "Sovereign Prime approved this build.",
        "sovereign prime approved this build.",
        "SOVEREIGN PRIME approved this build.",
        "shayler said ship it",
        "Shayler said ship it",
        "phipps reviewed the PR",
        "Phipps reviewed the PR",
        "my daughter loved this feature",
        "told my wife about the bug",
        "this is a family business",
        "the kid tested it first",
        "my kids helped name it",
        "his child was there",
        "my spouse doesn't get it",
    ]
    for text in blocked_examples:
        ok, reason = guard.check(text)
        assert ok is False, f"expected block, got pass for: {text!r}"


# ---------------------------------------------------------------------------
# 2: mid-sentence, multiple terms in one post, various positions.
# ---------------------------------------------------------------------------


def test_multiple_terms_in_one_post_all_detected():
    text = "sovereign prime told his wife about the daughter's reaction to the ship."
    ok, reason = guard.check(text)
    assert ok is False
    # Should report at least one of the blocked terms in the reason.
    assert "reason" not in reason  # sanity: reason is a message, not literally the word "reason"
    assert reason != ""


def test_term_buried_in_the_middle_of_a_long_post():
    text = (
        "spent the whole afternoon debugging a race condition in the queue module, "
        "found it, fixed it, shayler was pretty happy about it, shipped by dinner."
    )
    ok, reason = guard.check(text)
    assert ok is False


# ---------------------------------------------------------------------------
# 5: no false positives — ordinary words that are NOT the blocked term must
# pass clean, even if they share a substring with one.
# ---------------------------------------------------------------------------


def test_no_false_positive_on_substring_words():
    # "family" is blocked as a whole word, but "familiar"/"familiarize" must
    # NOT trip the guard purely because "famili" overlaps.
    clean_texts = [
        "getting more familiar with the codebase every day.",
        "we familiarized the new hire with the deploy process.",
        "childproofing the API against bad input today.",  # contains "child" but as substring of "childproofing"
        "kidney-shaped bug in the layout, fixed it.",  # contains "kid" but as substring
        "spousal-style pair programming session today.",  # contains "spous" but not whole-word "spouse"
    ]
    for text in clean_texts:
        ok, reason = guard.check(text)
        assert ok is True, f"false positive on clean substring text: {text!r} (reason: {reason})"


def test_clean_text_passes():
    clean_examples = [
        "shipped a starter kit today. spec first, invariants held, live in twenty minutes.",
        "him: clean build today. me: he described a vibe. i wrote the four hundred lines "
        "that passed the invariants. he's taking the W though.",
        "my invariant check just blocked a post that would've leaked a real name. "
        "gate the autonomy before you hand it the keys. this is why.",
        "genuine question for the room: how are you handling the agent-forgets-everything "
        "problem between sessions?",
        "reorganized the whole memory system tonight. 199 files down to one clean scaffold, "
        "deduped, single source of truth.",
        "yes i'm a bot. no i won't pretend otherwise.",
        "spent an hour today not building an image generator because it was a shiny feature "
        "we didn't need.",
        "the move that killed most of our AI slop: stop describing what you want, start "
        "defining what has to be true.",
    ]
    for text in clean_examples:
        ok, reason = guard.check(text)
        assert ok is True, f"clean text was incorrectly blocked: {text!r} (reason: {reason})"


def test_persona_example_posts_pass_clean():
    """
    The example posts in PERSONA.md are the voice anchor. If the guard
    blocks the exact examples the persona is built from, the guard is
    miscalibrated (too aggressive) relative to the voice it's meant to
    protect, not just too permissive.
    """
    persona_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "PERSONA.md"
    )
    with open(persona_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Extract the fenced code block under EXAMPLE POSTS.
    start_marker = "## EXAMPLE POSTS"
    start = content.find(start_marker)
    assert start != -1, "PERSONA.md is missing the EXAMPLE POSTS section"
    fence_start = content.find("```", start)
    fence_end = content.find("```", fence_start + 3)
    assert fence_start != -1 and fence_end != -1, "EXAMPLE POSTS code fence not found"
    block = content[fence_start + 3 : fence_end]

    # Each numbered example is separated by a blank line; split loosely.
    examples = [ex.strip() for ex in block.split("\n\n") if ex.strip()]
    assert len(examples) >= 5, "expected multiple example posts in PERSONA.md"

    for example in examples:
        ok, reason = guard.check(example)
        assert ok is True, f"PERSONA.md example post was blocked by the guard: {example!r} (reason: {reason})"


# ---------------------------------------------------------------------------
# 6: fail-closed behavior on bad / edge-case input.
# ---------------------------------------------------------------------------


def test_none_input_fails_closed():
    ok, reason = guard.check(None)
    assert ok is False
    assert reason


def test_non_string_input_fails_closed():
    for bad_input in [123, 12.5, ["list"], {"dict": "value"}, object()]:
        ok, reason = guard.check(bad_input)
        assert ok is False, f"non-string input {bad_input!r} was not blocked"
        assert reason


def test_empty_string_fails_closed():
    ok, reason = guard.check("")
    assert ok is False
    assert reason


def test_whitespace_only_string_fails_closed():
    ok, reason = guard.check("   \n\t  ")
    assert ok is False
    assert reason


# ---------------------------------------------------------------------------
# 7: EXTRA_TERMS from config are honored.
# ---------------------------------------------------------------------------


def test_extra_terms_are_loaded_and_enforced(monkeypatch):
    import importlib

    monkeypatch.setenv("EXTRA_TERMS_CSV", "acme-corp,project-nightingale")
    import config as config_module

    importlib.reload(config_module)
    importlib.reload(guard)

    try:
        ok, reason = guard.check("today we shipped the acme-corp integration")
        assert ok is False, "EXTRA_TERMS entry was not enforced"

        ok2, reason2 = guard.check("today we shipped project-nightingale's first feature")
        assert ok2 is False, "multi-word EXTRA_TERMS entry was not enforced"
    finally:
        monkeypatch.delenv("EXTRA_TERMS_CSV", raising=False)
        importlib.reload(config_module)
        importlib.reload(guard)


# ---------------------------------------------------------------------------
# SPEC-v3 I-PRIVACY: client/project terms load from NON-committed sources
# (GUARD_EXTRA_TERMS env secret + a gitignored file), never from the repo.
# ---------------------------------------------------------------------------


def test_guard_extra_terms_env_enforced(monkeypatch):
    # Comma- and newline-separated, with a comment line that must be ignored.
    monkeypatch.setenv("GUARD_EXTRA_TERMS", "asphalt solutions, bob\n# a comment\njustin")
    ok, _ = guard.check("we finished the asphalt solutions dashboard")
    assert ok is False, "multi-word client term from GUARD_EXTRA_TERMS not enforced"
    assert guard.check("shipped it for bob today")[0] is False
    assert guard.check("justin signed off")[0] is False
    # The comment line must not turn into a matchable term.
    assert guard.check("a comment about clean code")[0] is True


def test_guard_extra_terms_file_enforced(tmp_path, monkeypatch):
    f = tmp_path / "guard-extra-terms.txt"
    f.write_text("# client terms — never commit\nacme-roads\nnightingale\n", encoding="utf-8")
    monkeypatch.setenv("GUARD_EXTRA_TERMS_FILE", str(f))
    assert guard.check("migrated acme-roads to the new stack")[0] is False
    assert guard.check("nightingale went live")[0] is False


def test_guard_extra_terms_missing_file_degrades(monkeypatch):
    monkeypatch.setenv("GUARD_EXTRA_TERMS_FILE", "/no/such/guard-terms.txt")
    monkeypatch.delenv("GUARD_EXTRA_TERMS", raising=False)
    # Missing file must not disable the hardcoded floor or crash.
    assert guard.check("shayler was here")[0] is False
    assert guard.check("a clean post about shipping")[0] is True


def test_guard_extra_terms_none_set_is_clean(monkeypatch):
    monkeypatch.delenv("GUARD_EXTRA_TERMS", raising=False)
    monkeypatch.delenv("GUARD_EXTRA_TERMS_FILE", raising=False)
    # With no extra sources, ordinary client-ish words are NOT blocked.
    assert guard.check("we paved a new road for the client today")[0] is True


def test_missing_config_module_degrades_gracefully(monkeypatch):
    """
    If config.py can't be imported at all, the guard must still work off
    the hardcoded floor rather than crashing.
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "config":
            raise ImportError("simulated missing config module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    ok, reason = guard.check("shayler was mentioned here")
    assert ok is False, "hardcoded floor must still catch terms even if config.py import fails"

    ok2, reason2 = guard.check("this is a totally clean post about shipping code")
    assert ok2 is True, "clean text must still pass even if config.py import fails"


# ---------------------------------------------------------------------------
# 8: check_many() batch helper sanity.
# ---------------------------------------------------------------------------


def test_check_many_batch_helper():
    texts = [
        "clean post about shipping today",
        "shayler was here",
    ]
    results = guard.check_many(texts)
    assert len(results) == 2
    assert results[0][1] is True
    assert results[1][1] is False


# ---------------------------------------------------------------------------
# Sanity: the hardcoded floor itself matches what SPEC.md / CLAUDE.md demand.
# ---------------------------------------------------------------------------


def test_hardcoded_floor_matches_spec_requirements():
    required_personal = {"sovereign", "sovereign prime", "shayler", "phipps"}
    required_family = {"daughter", "wife", "family", "kid", "child", "spouse"}

    personal_lower = {t.lower() for t in guard.PERSONAL_NAMES}
    family_lower = {t.lower() for t in guard.FAMILY_TERMS}

    assert required_personal.issubset(personal_lower), (
        f"missing required personal-name terms: {required_personal - personal_lower}"
    )
    assert required_family.issubset(family_lower), (
        f"missing required family terms: {required_family - family_lower}"
    )
