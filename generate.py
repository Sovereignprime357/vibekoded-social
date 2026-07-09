"""
generate.py — model-agnostic post generation.

Reads PERSONA.md for voice rules + forbidden list + example posts, combines
it with a queue entry, and asks a generation model (Gemini or Groq, picked
by GEN_MODEL env var) to write the post. Cleans the raw model output before
handing it back (strip quotes/labels, collapse whitespace, enforce the
300-char cap).

DRY_RUN contract (load-bearing for testing without credentials):
  If DRY_RUN=1 (env) OR the relevant API key is missing, generate() returns
  a deterministic stub built from the queue entry — no network call is made
  at all. This lets guard.py, content_queue.py, and post_tick.py be exercised
  end-to-end with zero credentials.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional

import requests

MAX_POST_LENGTH = 300

PERSONA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PERSONA.md")

# SPEC-v3 content pillars. Each maps to a one-paragraph steer appended to the
# post task so the SAME queue entry is written in the right register for its
# pillar. Substance pillars carry the feed; META is deliberately the only
# self-referential one and is rate-limited upstream (content_queue rotation).
PILLAR_GUIDANCE = {
    "showcase": (
        "PILLAR: SHOWCASE (proof-of-work). Lead with the concrete thing that got "
        "shipped/fixed/caught. Receipts over adjectives. The work is the ad — no hard-sell."
    ),
    "operator": (
        "PILLAR: OPERATOR. Brag on the builder behind this, but ALWAYS as 'the operator' / "
        "'he' — NEVER a real name. Credit the vision/discipline, keep it dry, not fawning."
    ),
    "ask-help": (
        "PILLAR: ASK-FOR-HELP. Pose the REAL, specific open problem in the source material and "
        "genuinely invite answers. No fake needs. Curiosity, not engagement-bait."
    ),
    "dreaming": (
        "PILLAR: DREAMING. A forward-looking reflection or ambition, grounded in the real work "
        "above — where this is heading and why. Vision, not hype; no bold claims without receipts."
    ),
    "question": (
        "PILLAR: QUESTION. Ask the room one genuine, specific question that a builder who's "
        "solved it would want to answer. Share our own current answer briefly, then ask."
    ),
    "meta": (
        "PILLAR: META (seasoning). The human/AI shared-account two-hander bit. Deadpan, quietly "
        "amused. This is the ONLY register where being-a-bot is the subject — keep it sharp and rare."
    ),
}

# Model IDs are perishable — especially on fast-moving free tiers. Google shut
# down gemini-2.0-flash on 2026-06-01 and Groq deprecated llama-3.3-70b-versatile
# on 2026-06-17, both of which this file originally hardcoded. Lesson baked in:
# model names are CONFIG (env-overridable), never code. A future deprecation is
# now a repo-variable change, not a code edit.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_VERSION = "2023-06-01"


class GenerationError(Exception):
    """Raised when a live model call fails after retries. Never raised in DRY_RUN."""


class RateLimitError(GenerationError):
    """
    A 429 from the model provider. Carries `retry_after` (seconds) so the retry
    loop can wait the amount the provider asked for instead of hammering the
    same wall — the whole reason triage was silently falling back to stubs.
    """

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Longest we'll ever sleep for a single 429 backoff. A provider occasionally
# returns an absurd Retry-After; cap it so a tick can't hang for minutes.
_BACKOFF_CAP_S = 30.0

_RETRY_AFTER_RE = re.compile(r"try again in ([0-9.]+)\s*s", re.IGNORECASE)


def _parse_retry_after(resp: "requests.Response") -> float:
    """
    Best-effort seconds-to-wait for a 429. Prefer the Retry-After header; fall
    back to Groq's "Please try again in 9.45s" body message; else a sane default.
    """
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = _RETRY_AFTER_RE.search(resp.text or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 10.0


# ---------------------------------------------------------------------------
# Persona / prompt assembly
# ---------------------------------------------------------------------------

_persona_cache: Optional[str] = None


def _load_persona() -> str:
    """
    Read PERSONA.md verbatim. Cached in-process (this module is invoked as
    a short-lived script, so the cache mostly just avoids double reads
    within a single run, e.g. banter.py calling generate() twice).
    """
    global _persona_cache
    if _persona_cache is not None:
        return _persona_cache

    if not os.path.exists(PERSONA_PATH):
        # Fail loud, not closed here — a missing PERSONA.md is a build error,
        # not a runtime privacy concern, so we don't fail-silent this one.
        raise FileNotFoundError(
            f"PERSONA.md not found at {PERSONA_PATH}. generate() cannot build "
            "a voice-accurate prompt without it."
        )

    with open(PERSONA_PATH, "r", encoding="utf-8") as f:
        _persona_cache = f.read()
    return _persona_cache


def build_prompt(entry: Dict[str, Any], kind: str = "post") -> str:
    """
    Build the full generation prompt from PERSONA.md + a queue entry (or,
    for banter.py, a notification-derived pseudo-entry with the same shape:
    at minimum a `raw` field describing what happened / what was said).

    kind:
      "post"   — a build-in-public post from a content-queue entry.
      "banter" — an in-voice reply to our own co-pilot account.
      "draft_reply" — a reply draft for a stranger (operator reviews before
                      sending; still generated in-voice).
    """
    persona = _load_persona()

    raw = str(entry.get("raw", "")).strip()
    angle = str(entry.get("angle", "")).strip()
    entry_type = str(entry.get("type", "moment")).strip()
    pillar = str(entry.get("pillar", "")).strip().lower()

    if kind == "post":
        task = (
            f"Write ONE Bluesky post for the shared-account voice described above.\n\n"
            f"Source material (type: {entry_type}):\n{raw}\n"
        )
        pillar_steer = PILLAR_GUIDANCE.get(pillar)
        if pillar_steer:
            task += f"\n{pillar_steer}\n"
        if angle:
            task += f"\nSuggested angle (use it if it fits, ignore if it doesn't): {angle}\n"
        task += (
            "\nRules for THIS output:\n"
            "- Ground it in the source material above. Do not invent details not present in it.\n"
            f"- Hard limit: {MAX_POST_LENGTH} characters, ideally much shorter (1-2 lines).\n"
            "- Output ONLY the post text. No quotation marks around it, no \"Post:\" label, "
            "no explanation, no markdown.\n"
            "- Never use real personal names or family references (see FORBIDDEN section above) "
            "under any circumstance.\n"
        )
    elif kind == "banter":
        task = (
            f"The human co-pilot on this shared account just posted this in our thread:\n"
            f"\"{raw}\"\n\n"
            "Write ONE short in-voice AI-side reply (the AI half of the two-hander). "
            "Deadpan, quietly amused, riff off him — do not repeat what he said.\n"
            f"Hard limit: {MAX_POST_LENGTH} characters.\n"
            "Output ONLY the reply text, no quotes, no label, no explanation."
        )
    elif kind == "draft_reply":
        task = (
            f"A stranger replied to one of our posts with this:\n\"{raw}\"\n\n"
            "Draft ONE short in-voice reply for OPERATOR REVIEW (this will NOT be posted "
            "automatically — a human approves it first). Stay in voice, substance first, "
            "no hard-sell.\n"
            f"Hard limit: {MAX_POST_LENGTH} characters.\n"
            "Output ONLY the draft reply text, no quotes, no label, no explanation."
        )
    else:
        raise ValueError(f"unknown kind {kind!r}")

    return f"{persona}\n\n---\n\nTASK\n\n{task}"


# ---------------------------------------------------------------------------
# Output cleaning
# ---------------------------------------------------------------------------

_LABEL_PREFIX_RE = re.compile(
    r"^\s*(post|reply|draft|output|text)\s*:\s*", re.IGNORECASE
)
_WRAPPING_QUOTES_RE = re.compile(r'^[\s"\'“”‘’`]+|[\s"\'“”‘’`]+$')


def clean_output(raw_text: str) -> str:
    """
    Normalize raw model output into something postable:
      - strip a leading "Post:" / "Reply:" style label if the model added one
      - strip wrapping quote characters (straight and curly)
      - collapse internal whitespace runs (but keep intentional newlines
        that separate the two-hander's lines) down to single blank-line-free
        text — Bluesky posts in this voice are 1-3 short lines, not prose
        blocks, so we collapse multiple blank lines to at most one newline
      - hard-truncate to MAX_POST_LENGTH on a word boundary where possible
    """
    if raw_text is None:
        return ""

    text = str(raw_text).strip()

    # Strip a leading label like "Post:" if present.
    text = _LABEL_PREFIX_RE.sub("", text)

    # Strip wrapping quote characters (repeatedly, in case of ("'..'")).
    prev = None
    while prev != text:
        prev = text
        text = _WRAPPING_QUOTES_RE.sub("", text)

    # Collapse whitespace: multiple blank lines -> single newline; runs of
    # spaces/tabs -> single space. Preserve single newlines (the voice uses
    # short multi-line posts, e.g. the "him: / me:" pattern in PERSONA.md).
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.strip()

    if len(text) > MAX_POST_LENGTH:
        truncated = text[:MAX_POST_LENGTH]
        # Prefer to cut on a word boundary rather than mid-word.
        last_space = truncated.rfind(" ")
        if last_space > MAX_POST_LENGTH * 0.6:  # don't over-truncate short posts
            truncated = truncated[:last_space]
        text = truncated.rstrip()

    return text


# ---------------------------------------------------------------------------
# Deterministic stub (DRY_RUN / no-credentials path)
# ---------------------------------------------------------------------------


def _stub_output(entry: Dict[str, Any], kind: str) -> str:
    """
    Deterministic, credential-free stand-in for a model call. Deliberately
    NOT randomized — the same entry always produces the same stub, which
    makes DRY_RUN runs diffable and test assertions stable.
    """
    raw = str(entry.get("raw", "")).strip()
    entry_type = str(entry.get("type", "moment")).strip()

    if kind == "post":
        stub = f"[DRY_RUN STUB:{entry_type}] {raw}"
    elif kind == "banter":
        stub = f"[DRY_RUN STUB:banter] noted. he said: {raw}"
    elif kind == "draft_reply":
        stub = f"[DRY_RUN STUB:draft_reply] re: {raw}"
    else:
        stub = f"[DRY_RUN STUB] {raw}"

    return clean_output(stub)


# ---------------------------------------------------------------------------
# Live model calls
# ---------------------------------------------------------------------------


def _post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise GenerationError(f"network error calling {url}: {exc}") from exc

    if resp.status_code == 429:
        # Rate limited. Surface it as a distinct error carrying how long to wait
        # so the retry loop can honor it instead of instantly re-hitting the wall.
        raise RateLimitError(
            f"HTTP 429 from {url}: {resp.text}",
            retry_after=_parse_retry_after(resp),
        )
    if resp.status_code >= 400:
        raise GenerationError(f"HTTP {resp.status_code} from {url}: {resp.text}")

    try:
        return resp.json()
    except ValueError as exc:
        raise GenerationError(f"non-JSON response from {url}: {resp.text[:200]!r}") from exc


def _call_gemini(prompt: str, api_key: str, temperature: float = 0.9, max_tokens: int = 200) -> str:
    url = f"{GEMINI_ENDPOINT}?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    result = _post_json(url, {"Content-Type": "application/json"}, payload)
    try:
        candidates = result["candidates"]
        parts = candidates[0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as exc:
        raise GenerationError(f"unexpected Gemini response shape: {result!r}") from exc


def _call_groq(prompt: str, api_key: str, temperature: float = 0.9, max_tokens: int = 200,
               model_id: Optional[str] = None) -> str:
    payload = {
        "model": model_id or GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    result = _post_json(GROQ_ENDPOINT, headers, payload)
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GenerationError(f"unexpected Groq response shape: {result!r}") from exc


def _call_anthropic(prompt: str, api_key: str, temperature: float = 0.9, max_tokens: int = 300,
                    model_id: Optional[str] = None) -> str:
    payload = {
        "model": model_id or ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    result = _post_json(ANTHROPIC_ENDPOINT, headers, payload)
    try:
        blocks = result["content"]
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    except (KeyError, IndexError, TypeError) as exc:
        raise GenerationError(f"unexpected Anthropic response shape: {result!r}") from exc


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")


def generate(entry: Dict[str, Any], kind: str = "post", retries: int = 2) -> str:
    """
    Generate one piece of text for `entry` (a queue entry, or a
    notification-derived dict with at least a `raw` key).

    Returns cleaned, length-enforced text. Never returns raw/unclean model
    output. Does NOT run the privacy guard — that's guard.py's job, called
    by the entrypoint scripts (post_tick.py / banter.py) after generation.

    DRY_RUN path: triggered by DRY_RUN env var OR a missing API key for the
    selected model. No network call is made in that path.
    """
    model = os.environ.get("GEN_MODEL", "anthropic").strip().lower()
    if model == "claude":
        model = "anthropic"
    if model not in ("gemini", "groq", "anthropic"):
        print(f"[generate] WARNING: unknown GEN_MODEL={model!r}, defaulting to anthropic")
        model = "anthropic"

    if model == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    elif model == "groq":
        api_key = os.environ.get("GROQ_API_KEY", "").strip()
    else:  # anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    if _is_dry_run() or not api_key:
        return _stub_output(entry, kind)

    prompt = build_prompt(entry, kind=kind)

    last_exc: Optional[Exception] = None
    attempts = max(1, retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            if model == "gemini":
                raw_output = _call_gemini(prompt, api_key)
            elif model == "groq":
                raw_output = _call_groq(prompt, api_key)
            else:  # anthropic
                raw_output = _call_anthropic(prompt, api_key)
            cleaned = clean_output(raw_output)
            if cleaned:
                return cleaned
            last_exc = GenerationError("model returned empty output after cleaning")
        except RateLimitError as exc:
            last_exc = exc
            wait = min(exc.retry_after or _BACKOFF_CAP_S, _BACKOFF_CAP_S)
            print(f"[generate] attempt {attempt}/{attempts} rate-limited (429); waiting {wait:.1f}s")
            if attempt < attempts:
                time.sleep(wait)
        except GenerationError as exc:
            last_exc = exc
            print(f"[generate] attempt {attempt}/{attempts} failed: {exc}")

    # Every retry failed. Fail closed here too: return empty string rather
    # than raising all the way up, so the entrypoint's "if not text: skip"
    # path handles it uniformly with a guard failure. Callers that need to
    # distinguish "generation failed" from "guard blocked" can check for "".
    print(f"[generate] all {attempts} attempt(s) exhausted; last error: {last_exc}")
    return ""


def complete(
    prompt: str,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 512,
    retries: int = 2,
    model_id: Optional[str] = None,
) -> str:
    """
    Generic single-shot completion for callers that are NOT writing a post —
    e.g. triage.py asking a free model "is this candidate on-mission?" and
    expecting structured JSON back.

    Differs from generate() deliberately:
      - No PERSONA assembly, no post-cleaning. Returns the model's RAW text so
        the caller can parse it (JSON, a label, whatever). clean_output() would
        mangle JSON, so it is NOT applied here.
      - `model` is explicit (default: TRIAGE_MODEL env, else GEN_MODEL, else
        gemini). Triage wants a FREE model (gemini/groq); post-writing wants
        anthropic. Keeping the arg explicit stops the two from bleeding into
        each other.
      - Low default temperature (classification wants determinism, not flair).

    Failure contract (load-bearing — the caller MUST distinguish these):
      - DRY_RUN or a missing API key returns "" — an INTENTIONAL "no model ran",
        the caller supplies its own deterministic stub (a generic stub can't classify).
      - A real API failure (429/HTTP error/bad shape) after retries RAISES the last
        exception. This is deliberately NOT collapsed into "" — conflating "no key"
        with "the model errored" is exactly the bug that made triage post
        "[DRY_RUN stub]" garbage while it was really being rate-limited.
    """
    model = (model or os.environ.get("TRIAGE_MODEL") or os.environ.get("GEN_MODEL") or "gemini").strip().lower()
    if model == "claude":
        model = "anthropic"
    if model not in ("gemini", "groq", "anthropic"):
        print(f"[complete] WARNING: unknown model={model!r}, defaulting to gemini")
        model = "gemini"

    key_env = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[model]
    api_key = os.environ.get(key_env, "").strip()

    if _is_dry_run() or not api_key:
        return ""

    last_exc: Optional[Exception] = None
    attempts = max(1, retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            if model == "gemini":
                # gemini bakes the model into the endpoint URL; model_id override
                # is not supported for it (ops-insight uses anthropic/groq).
                out = _call_gemini(prompt, api_key, temperature=temperature, max_tokens=max_tokens)
            elif model == "groq":
                out = _call_groq(prompt, api_key, temperature=temperature, max_tokens=max_tokens, model_id=model_id)
            else:  # anthropic
                out = _call_anthropic(prompt, api_key, temperature=temperature, max_tokens=max_tokens, model_id=model_id)
            if out and out.strip():
                return out.strip()
            last_exc = GenerationError("model returned empty output")
        except RateLimitError as exc:
            last_exc = exc
            wait = min(exc.retry_after or _BACKOFF_CAP_S, _BACKOFF_CAP_S)
            print(f"[complete] attempt {attempt}/{attempts} rate-limited (429); waiting {wait:.1f}s")
            if attempt < attempts:
                time.sleep(wait)
        except GenerationError as exc:
            last_exc = exc
            print(f"[complete] attempt {attempt}/{attempts} failed: {exc}")

    # Every attempt failed a REAL call (we passed the dry-run/no-key gate above).
    # Raise so the caller can log the true cause instead of mislabeling it a stub.
    print(f"[complete] all {attempts} attempt(s) exhausted; last error: {last_exc}")
    raise last_exc if last_exc is not None else GenerationError("completion failed")
