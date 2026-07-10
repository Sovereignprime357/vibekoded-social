# SPEC — v6: The Ops-Insight Harvest (review-only knowledge pipeline)
## Project: vibekoded-social
## Phase: v6 (a second triage lens for transferable build-with-AI knowledge)
## Last Updated: 2026-07-09
## Builds on: SPEC-v2/v3 (scout + converse streams), SPEC-v5 (heartbeat cadence)

## WHAT
A SECOND triage lens over the conversation data the bot ALREADY pulls — the scout
stream and especially the bot's own reply-threads / notify stream (where the deep
technical exchanges happen). When a conversation contains a genuine **OPS-INSIGHT**
— actionable AI-orchestration / vibe-coding / agentic-workflow knowledge that could
plausibly improve how the operator builds (SpecMesh, this bot, the builds) — the
harvest flags it, extracts a structured brief, and posts it to a **review-only**
Slack channel (`C0BGEB5FNGZ`). Pure FYI. Nothing is actioned, nothing is written to
any brain or memory.

Two-stage, cost-aware: a CHEAP flag first (batched, on already-pulled text), then a
DEEPER extract only on flagged items (a stronger-than-Haiku model is allowed, but
bounded by a hard per-tick cap).

## WHY
The bot sits on a stream of real practitioner conversation. Most of it is engagement
material (scout/converse handle that). But a thin slice is genuinely transferable
technique — how people actually build with AI — that could improve the operator's own
systems. Surfacing that, fact-checkably and review-only, turns the bot's existing
data pull into a knowledge feed WITHOUT any autonomous action and without touching the
brain (that graduation is a separate human decision).

## INPUTS
- **Scout candidates** — the full scanned set (already pulled by scout-tick; reused,
  no new scan). Each has text + author + permalink.
- **Notify stream** — new reply/mention notifications (already pulled by notify-tick /
  banter; reused). The deep technical exchanges (e.g. a prompt-injection thread) live
  here.
- **Env (non-secret defaults / secrets already present):** `SLACK_CHANNEL_OPS_INTEL`
  (default `C0BGEB5FNGZ`), `SLACK_BOT_TOKEN`, model keys, `OPS_INSIGHT_MODEL` (optional
  stronger extract model id, bounded), `OPS_INSIGHT_MAX_PER_TICK` (default 3),
  `OPS_INSIGHT_FLAG_MODEL` (cheap; default = TRIAGE_MODEL), `GUARD_EXTRA_TERMS`.

## OUTPUTS
- For each flagged, extracted, guard-passing insight: one Slack **brief** posted to the
  ops-intel channel via the bot token. The brief carries: the insight (what it is) ·
  why it applies to us · what it would do · why the bot thinks it improves our
  vibe-coded outcomes · **PROVENANCE (author handle + post permalink)**.
- A dedup ledger entry (`ops-insight-seen.jsonl`) per considered source post.
- NOTHING else. No brain write, no memory write, no action, no 👍 wiring.

## TRIAGE LENS (narrow — the definition is the whole game)
FLAG ONLY: a genuine, transferable technique/knowledge about building-with-AI
(orchestration, agentic workflows, prompt/spec technique, evals, memory, tooling) that
could plausibly improve the operator's OWN systems. **High bar. When in doubt, do not
flag.** NOT generic hot-takes, NOT opinions, NOT engagement bait, NOT news, NOT
marketing. Over-flagging = noise = the feature's failure mode, so the lens errs toward
silence.

## INVARIANTS
- **I-NO-AUTO-BRAIN (critical)** — the harvest NEVER writes to the operator's brain
  (Claude's Mind) or any memory/knowledge store. It ONLY posts a Slack brief (+ its own
  dedup ledger). Brain-graduation is a separate human+Council decision, fully out of
  scope. There is no code path here that writes anywhere but Slack + the seen ledger.
- **I-PROVENANCE** — every brief carries its source: author handle + a permalink to the
  originating post, so the operator can fact-check it. A brief without provenance is not
  posted.
- **I-PRIVACY (fail-closed)** — the privacy guard runs on the ENTIRE rendered brief
  (incl. extended client/project terms) before it is posted. A brief that trips the
  guard is DROPPED, never sanitized-and-posted.
- **I-DEDUP** — an insight is surfaced at most once (keyed on source post uri via the
  seen ledger). Considered-but-dropped items are also recorded so they aren't
  re-flagged (cost).
- **I-REVIEW-ONLY** — no 👍-action is wired, no autonomy, no downstream execution. The
  brief is FYI. (It does not enter the act-layer surfaced ledger.)
- **I-REUSE-ONLY** — runs on the existing scout/notify (heartbeat-driven) ticks and
  reuses already-pulled data. It adds NO new Bluesky scan/fetch — only model calls +
  the Slack post.
- **I-COST-BOUNDED** — cheap flag first (batched); the deeper extract runs ONLY on
  flagged items and is hard-capped at `OPS_INSIGHT_MAX_PER_TICK` per tick.

## EDGE CASES
- **No model / DRY_RUN** — flag returns nothing (no model to judge) → harvest no-ops.
  Safe-degrade; the core scout/notify ticks are untouched.
- **Over-flagging risk** — the lens is deliberately narrow + high-bar; the flag prompt
  instructs "when in doubt, skip", and the per-tick extract cap bounds blast radius.
- **Guard trips on a brief** — dropped, marked seen, never posted (I-PRIVACY).
- **Extract returns nothing usable / missing provenance** — dropped, marked seen.
- **Bot not invited to the ops-intel channel** — the post fails `not_in_channel`;
  logged clearly; item marked seen (cost-bounded, no infinite retry). Operator must
  invite the bot to `C0BGEB5FNGZ` (setup step).
- **Harvest itself errors** — wrapped so it can NEVER break the scout/notify tick
  (review-only, additive).
- **Same insight in both scout + notify** — the shared seen ledger dedups it.

## LOG BRIDGE (v6.1 — the harvest→nightly input seam)
The nightly distill runs on the operator's PC; the harvest runs in CI and can't write
his machine. So every post-worthy brief is ALSO mirrored to `ops-intel-log.jsonl` in the
repo (committed + pushed with the other ledgers), which the nightly fetches.
- **I-LOG-MIRROR** — each brief that passes the guard is appended to `ops-intel-log.jsonl`
  BEFORE the Slack attempt (so the intel is captured even if Slack rejects), as the full
  brief (insight / applies / effect / why_improves) + provenance (author + link) + `ts` +
  a stable `id` (sha1 of the source uri). Deduped by source uri — logged at most once. A
  dry-run or guard-blocked brief is NOT logged. This is still I-NO-AUTO-BRAIN: a repo
  ledger, not the brain.
- **Fetch path** — the repo is PUBLIC, so the nightly reads the log with NO auth at:
  `https://raw.githubusercontent.com/Sovereignprime357/vibekoded-social/main/ops-intel-log.jsonl`.

## OUT OF SCOPE (v6)
- Any brain/memory write (I-NO-AUTO-BRAIN — separate human+Council decision).
- Any action / 👍 / autonomy on a brief (review-only).
- New scanning/fetching (reuse-only).
- Thread-level multi-message synthesis (v6 judges the surfaced message/post text; deeper
  thread stitching is a later refinement).

## ACCEPTANCE CRITERIA
- A post containing a genuine transferable technique is flagged, extracted into a brief
  with provenance, guard-checked, and posted to `C0BGEB5FNGZ`.
- A generic opinion / hot-take / engagement-bait post is NOT flagged (high bar).
- A brief that trips the privacy guard is never posted (tested, fail-closed).
- The same source post is never surfaced twice (tested).
- The deeper extract never exceeds the per-tick cap (tested).
- No code path writes to any brain/memory (I-NO-AUTO-BRAIN) — only Slack + the seen ledger.
- Harvest failure never breaks the scout/notify tick.

---
_Companion to SPEC.md + SPEC-v2/v3/v4/v5 + PERSONA.md. v6 adds a knowledge lens that
observes and reports — never acts, never writes to the brain._
