"""
tests/test_voice.py — the operator voice profile (env-loaded, safe-degrade) + the
MECHANICAL anti-sycophancy gate, and its enforcement inside generate().

No network: generate's model call is monkeypatched. The gate itself is pure.
The VOICE_PROFILE secret is never written to disk here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
import voice  # noqa: E402


# --- the gate: banned punctuation --------------------------------------------

def test_em_dash_rejected():
    ok, why = voice.check_voice("local search kills the latency tax — and keeps the data yours.")
    assert ok is False and "dash" in why.lower()


def test_en_dash_rejected():
    ok, _ = voice.check_voice("range was 3–5 minutes on the old path.")
    assert ok is False


def test_plain_hyphen_is_allowed():
    ok, _ = voice.check_voice("we run spec-first, flat-file memory. what are you on?")
    assert ok is True


# --- the gate: praise-openers ------------------------------------------------

def test_every_banned_opener_rejected_at_start():
    for op in voice.BANNED_OPENERS:
        ok, why = voice.check_voice(f"{op}. here's the actual thing we did.")
        assert ok is False, f"opener {op!r} not caught"
        assert "praise-opener" in why


def test_opener_case_insensitive_and_leading_noise():
    assert voice.check_voice("That's The Move. we shipped it.")[0] is False
    assert voice.check_voice('> "great question" about memory')[0] is False
    assert voice.check_voice("**you're absolutely right** here")[0] is False


def test_curly_apostrophe_opener_rejected():
    assert voice.check_voice("you’re right, the index is the trick.")[0] is False  # you’re


def test_opener_mid_sentence_is_allowed():
    # Start-anchored: a praise phrase NOT at the opening isn't the sycophancy tell.
    ok, _ = voice.check_voice("we shipped the flat-file memory today. curious what you run.")
    assert ok is True


def test_facts_boundary_not_a_false_positive():
    assert voice.check_voice("facts. the deep folder tree lost.")[0] is False   # bare "facts" opener
    assert voice.check_voice("factsheet generation is the feature we cut.")[0] is True  # not the opener


def test_clean_engaging_text_passes():
    ok, why = voice.check_voice("we run a flat memory file plus an index. how are you handling dedup across sessions?")
    assert ok is True and why == ""


# --- profile load + safe-degrade ---------------------------------------------

def test_voice_profile_block_empty_when_secret_absent(monkeypatch):
    monkeypatch.delenv("VOICE_PROFILE", raising=False)
    assert voice.load_voice_profile() == ""
    assert voice.voice_profile_block() == ""   # safe-degrade -> no injection


def test_voice_profile_block_includes_profile_when_present(monkeypatch):
    monkeypatch.setenv("VOICE_PROFILE", "blunt, lowercase, no praise, no em-dashes.")
    block = voice.voice_profile_block()
    assert "blunt, lowercase" in block and "OPERATOR VOICE PROFILE" in block


# --- enforcement inside generate(): retry-then-drop --------------------------

def _live(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("GEN_MODEL", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(generate.time, "sleep", lambda s: None)


def test_generate_drops_when_output_always_sycophantic(monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(generate, "_call_anthropic", lambda prompt, api_key, **k: "that's the move. do X.")
    out = generate.generate({"raw": "someone said something", "type": "moment"}, kind="draft_reply", retries=2)
    assert out == ""   # gate rejected every attempt -> DROPPED, not posted


def test_generate_drops_on_em_dash(monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(generate, "_call_anthropic", lambda prompt, api_key, **k: "keeps the data yours — nice.")
    assert generate.generate({"raw": "x", "type": "moment"}, kind="draft_reply") == ""


def test_generate_retries_then_succeeds(monkeypatch):
    _live(monkeypatch)
    calls = {"n": 0}
    def fake(prompt, api_key, **k):
        calls["n"] += 1
        return "great question, here." if calls["n"] == 1 else "we run a flat file plus an index. you?"
    monkeypatch.setattr(generate, "_call_anthropic", fake)
    out = generate.generate({"raw": "x", "type": "moment"}, kind="draft_reply", retries=2)
    assert out == "we run a flat file plus an index. you?"   # 2nd attempt passed the gate
    assert calls["n"] == 2


def test_generate_returns_clean_first_time(monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(generate, "_call_anthropic", lambda prompt, api_key, **k: "shipped the memory scaffold. what are you using?")
    out = generate.generate({"raw": "x", "type": "moment"}, kind="post")
    assert out == "shipped the memory scaffold. what are you using?"


def test_generate_safe_degrades_without_secret(monkeypatch):
    # No VOICE_PROFILE + DRY_RUN -> deterministic stub, no crash, no injection.
    monkeypatch.delenv("VOICE_PROFILE", raising=False)
    monkeypatch.setenv("DRY_RUN", "1")
    out = generate.generate({"raw": "hello world", "type": "moment"}, kind="post")
    assert out and "hello world" in out   # stub returned, pipeline unbroken


def test_build_prompt_injects_profile_when_present(monkeypatch):
    monkeypatch.setenv("VOICE_PROFILE", "write like the operator: terse, no hype.")
    p = generate.build_prompt({"raw": "x", "type": "moment"}, kind="draft_reply")
    assert "write like the operator" in p and "OPERATOR VOICE PROFILE" in p


def test_build_prompt_no_profile_no_injection(monkeypatch):
    monkeypatch.delenv("VOICE_PROFILE", raising=False)
    p = generate.build_prompt({"raw": "x", "type": "moment"}, kind="draft_reply")
    assert "OPERATOR VOICE PROFILE" not in p   # safe-degrade
