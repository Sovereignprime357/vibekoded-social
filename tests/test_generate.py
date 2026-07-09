"""
tests/test_generate.py — generate.complete()'s failure contract, no network.

The whole point of these: prove complete() (1) honors a 429 by backing off and
retrying instead of hammering the wall, (2) RAISES on a real exhausted failure
so the caller can tell the truth, and (3) still returns "" for the intentional
no-model cases (DRY_RUN / missing key). That distinction is what stops a live
rate-limit from being mislabeled a dry-run stub.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402


class _FakeResp:
    def __init__(self, status_code, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.content = b"x"  # non-empty so _post_json calls .json()
        self.headers = headers or {}

    def json(self):
        return self._json


def _groq_ok(content="OK"):
    return _FakeResp(200, json_data={"choices": [{"message": {"content": content}}]})


def _groq_429():
    # No Retry-After header; the wait is parsed from the body message instead.
    return _FakeResp(429, text='{"error":{"message":"Please try again in 0.01s"}}')


# --- intentional no-model cases return "" (NOT raise) -----------------------

def test_complete_dry_run_returns_empty(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert generate.complete("hi", model="groq") == ""


def test_complete_missing_key_returns_empty(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert generate.complete("hi", model="groq") == ""


# --- 429 backoff then success ----------------------------------------------

def test_complete_backs_off_on_429_then_succeeds(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")

    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=30):
        calls["n"] += 1
        return _groq_429() if calls["n"] == 1 else _groq_ok("real verdict")

    slept = []
    monkeypatch.setattr(generate.requests, "post", fake_post)
    monkeypatch.setattr(generate.time, "sleep", lambda s: slept.append(s))

    out = generate.complete("hi", model="groq")
    assert out == "real verdict"
    assert calls["n"] == 2          # retried after the 429
    assert slept and slept[0] > 0   # actually backed off (didn't hammer)


# --- exhausted real failure RAISES (does not return "") --------------------

def test_complete_exhausted_429_raises(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")

    monkeypatch.setattr(generate.requests, "post", lambda *a, **k: _groq_429())
    monkeypatch.setattr(generate.time, "sleep", lambda s: None)

    with pytest.raises(generate.RateLimitError):
        generate.complete("hi", model="groq", retries=1)


# --- model_id override (SPEC-v6 bounded stronger extract) -------------------

def test_complete_model_id_overrides_payload_model(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=30):
        captured["model"] = (json or {}).get("model")
        return _FakeResp(200, json_data={"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(generate.requests, "post", fake_post)
    out = generate.complete("hi", model="anthropic", model_id="claude-sonnet-5")
    assert out == "ok"
    assert captured["model"] == "claude-sonnet-5"   # override used, not ANTHROPIC_MODEL default


def test_complete_no_model_id_uses_default(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    captured = {}
    monkeypatch.setattr(generate.requests, "post",
                        lambda url, json=None, headers=None, timeout=30: (
                            captured.update(model=(json or {}).get("model")),
                            _FakeResp(200, json_data={"content": [{"type": "text", "text": "ok"}]}))[1])
    generate.complete("hi", model="anthropic")
    assert captured["model"] == generate.ANTHROPIC_MODEL   # falls back to the module default


# --- SPEC-v3 pillar-aware prompt assembly -----------------------------------

def test_build_prompt_injects_pillar_steer():
    entry = {"raw": "shipped the scout loop", "type": "ship", "pillar": "showcase"}
    prompt = generate.build_prompt(entry, kind="post")
    assert "SHOWCASE" in prompt          # the pillar steer is present
    assert "proof-of-work" in prompt


def test_build_prompt_meta_pillar_steer():
    entry = {"raw": "the two-hander bit", "type": "moment", "pillar": "meta"}
    prompt = generate.build_prompt(entry, kind="post")
    assert "META" in prompt
    assert "seasoning" in prompt


def test_build_prompt_untagged_pillar_has_no_steer():
    entry = {"raw": "legacy entry", "type": "ship"}  # no pillar
    prompt = generate.build_prompt(entry, kind="post")
    # None of the pillar headers should appear for an untagged entry.
    assert "PILLAR:" not in prompt
