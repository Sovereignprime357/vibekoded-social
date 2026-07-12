# SPEC — v8: The Intel Pipeline (Fast Lane)
## Project: vibekoded-social (primary) + VibeKoded website (secondary sink)
## Phase: v8 — FAST LANE ONLY. Slow lane specified in §SLOW LANE, deliberately NOT built.
## Last Updated: 2026-07-12

---

## WHAT

One wire, two sinks.

The agent already harvests intel every day (2,657 insight records, 115 intel briefs, a
frontier watchlist) and already distills it nightly into themes. That distilled output
currently dies in Slack. This SPEC routes it into the two places it can earn: the
Bluesky posting queue and the website `/log` blog.

Concretely, v8 does three things:

1. **Kills the decoy.** The `daily-content-refill` scheduled task on the operator's PC
   generates high-freshness, provenance-carrying candidates every morning — and posts
   them as a single Slack digest that **nothing listens to**. Meanwhile the bot's own
   `content_refill.generate_candidates` produces low-value *evergreen* filler, and that
   filler is the only thing wired to the queue. v8 inverts this: the researched
   candidates become the ones the operator approves, and the evergreen generator becomes
   the fallback for days when research yields nothing.

2. **Ports the WindowScheduler.** `post.yml` is the only tick workflow with no heartbeat
   (it rides GitHub's built-in cron, which the repo's own comments admit lags 2–4h) and
   post_tick has **zero** pacing of its own. v8 moves posting onto the reliable heartbeat
   and adds posting windows with a randomized time inside each window, regenerated daily —
   ported from the operator's prior NullChadAI `WindowScheduler` (no waste, only reuse).

3. **Opens the second sink.** The same distilled themes feed the website's `/log`
   content engine, which already has an auto-ship pipeline, an I-CAP rate limit, and a
   revert command. Today nothing carries intel there at all.

---

## WHY

The gold is already being mined. It has nowhere to go.

The bot has been **silent for two days** because the queue drained to a single META item
and pillar-rotation correctly refused it. The rotation guard was right. The supply was
the problem — and the supply was the problem because the *good* supply was never wired in.

The operator's keystone rule: **no waste, only repurpose.** Every artifact has multiple
homes. Right now the intel has zero homes. This is the wire that gives it two.

Second-order: the operator's other keystone — **everything points to selling.** Content
that never ships sells nothing. A blog that never gets a post ranks for nothing. This
pipeline is the supply line for both lead-gen surfaces.

---

## INPUTS

- **Distilled intel themes** — produced by the `nightly-ops-intel-distill` scheduled task
  (01:04 daily) from `ops-intel-log.jsonl` + `ops-insight-seen.jsonl`. Validation: theme
  must carry provenance (which real build event it came from) or it is not eligible.
- **Real build activity** — commits, merges, incidents, and reversals from the last 24–48h
  across both repos. This is what makes a candidate HIGH freshness rather than evergreen.
- **The operator's 👍** — a Slack reaction on an individual candidate card. This is the
  human gate. Nothing enqueues or ships without it.
- **Rotation memory** — recent pillars from `posted.jsonl` (existing).
- **The voice profile** — `VOICE_PROFILE` secret (existing, live).

---

## OUTPUTS

- **Bluesky queue** — approved candidates appended to `content-queue.jsonl` with
  `final_text` + `provenance`, posted verbatim by `post_tick` on the window schedule.
- **Website `/log` draft** — the same distilled theme, expanded, written to
  `content/log/_drafts/` in the VibeKoded repo, entering the existing auto-ship pipeline
  (which already carries I-AUTOSHIP, I-CAP, I-REVERT).
- **Slack candidate cards** — one message **per candidate**, each with its own reaction
  target, each stating its pillar explicitly, each recorded in `refill-surfaced.jsonl`
  with its `slack_ts` so the existing `poll_and_enqueue` can map a 👍 back to a candidate.
- **The daily post schedule** — a state file holding today's randomized post times,
  regenerated at local-day rollover.

---

## INVARIANTS

- **I-GATE.** Nothing reaches a public surface without an explicit operator 👍 on an
  individual candidate. A digest with one reaction bar for five candidates is a
  **violation** — it makes the gate unmappable. One card, one candidate, one reaction target.

- **I-APPROVABLE.** A candidate that is surfaced for approval MUST be postable at the time
  it is surfaced. Never surface a candidate that current pillar-rotation would reject. A 👍
  that the system cannot honor is worse than no candidate at all — it is the exact defect
  that produced two days of silence.

- **I-NO-DECOY.** Exactly ONE system surfaces content candidates for approval. Two systems
  posting approvable-looking cards into the same channel, where only one is wired, is
  forbidden. If the researched generator produces nothing, the fallback generator may
  surface — but they must never both surface in the same cycle.

- **I-PROVENANCE.** Every candidate carries the real build event it came from. No
  fabrication, no evergreen dressed as fresh. If it can't be traced to something that
  actually happened, it doesn't ship. (Mirrors the website's I-AUTOSHIP anchor rule.)

- **I-ROTATION (preserved).** Pillar rotation and the META 1-in-5 cap are NOT weakened.
  They protected the brand voice for two days while everything else was broken. The fix
  goes in the supply, never in the guard.

- **I-VOICE (preserved).** All generated text passes the existing mechanical voice gate
  (em-dashes, praise-openers, trailing validation stamps). The gate drops rather than posts.

- **I-PACE.** Posts go out inside defined windows at a randomized time within each window,
  and the schedule regenerates daily. The bot must never post at the same clock time every
  day — that is a machine signature. Max one post per tick; the day's cadence is bounded by
  the window count, not by the heartbeat frequency.

- **I-REVERSIBLE.** Everything in the fast lane is revertible. Bluesky posts can be deleted;
  `/log` posts have `/revert-auto-ship`. This is *why* the fast lane is allowed to be fast.

---

## EDGE CASES

- **Every candidate is rotation-blocked.** Surface nothing. Fire the existing
  ROTATION-BLOCKED alert instead. Never surface an empty batch silently.
- **The research yields no fresh material** (a quiet build day). Fall back to the evergreen
  generator, and **label it evergreen on the card** so the operator knows what he's approving.
  Never fabricate freshness.
- **The operator 👍s nothing for N days.** The queue drains. The QUEUE-EMPTY alert already
  exists and fires. Do not auto-post to compensate. Silence is preferable to an ungated post.
- **Two ticks race on the same commit-back push.** Existing concurrency group
  (`vibekoded-social-state`) already serializes this. Do not regress it.
- **A 👍 lands on a stale card** (surfaced before a rotation change). Re-check eligibility at
  enqueue time, not just at surface time. If it's now blocked, hold it in the queue rather
  than dropping it — it becomes eligible again on the next rotation.
- **The website sink is unavailable** (deploy cap, repo lock). The Bluesky sink must still
  work. The two sinks are independent; one failing never blocks the other.
- **Day rollover mid-window.** Schedule regeneration is keyed to local calendar day. A post
  window that spans midnight belongs to the day it started.
- **Duplicate content across sinks.** The same theme may legitimately produce both a Bluesky
  post and a `/log` post — they're different lengths for different audiences. But the same
  theme must not produce two Bluesky posts. Dedup on theme id against `posted.jsonl`.

---

## OUT OF SCOPE

- **Lessons and courses.** That is the masterclass, it has a human buyer waiting and a date
  to be set, and it must not be absorbed into a pipeline build. Separate track.
- **The SLOW LANE** (below). Specified, deliberately not built in v8.
- **Weakening pillar rotation, the META cap, the voice gate, or the human 👍 gate.** None of
  these are on the table.
- **Moving bot state off git.** Real smell, real fix, not this SPEC. (Vercel Git is now
  disconnected, so the push storm costs nothing urgent.)
- **Multi-vertical replication.** The engine is copyable; the voice is not. Not now.

---

## SLOW LANE (specified, NOT built in v8)

Same source, different smelter. The distinction is **reversibility**:

    intel → weekly dream → the Council → operator approval → the agent changes itself

Content is cheap and revertible, so it gets a fast gate (one 👍). **Self-improvement is not
revertible in the same way** — an agent that changes its own behavior on bad intel carries
that change forward into everything it does next. That earns the ceremony: the weekly dream
consolidation, adversarial review by the Council, and an explicit operator approval before
any behavioral change lands.

Building this in v8 would put a Council review in front of every blog post, and nothing
would ever ship. The lanes stay separate. The fast lane pays; the slow lane compounds.

**Do not build until the fast lane has been shipping for at least two weeks.**

---

## ACCEPTANCE CRITERIA

- [ ] Exactly one system surfaces candidates. The Slack digest decoy is gone or rewired.
- [ ] Each candidate is its own Slack message with its own reaction target and an explicit
      `PILLAR:` label on the card.
- [ ] No rotation-blocked candidate is ever surfaced. Verified by test: last-posted=META ⇒
      the META candidate is withheld and the other four are surfaced.
- [ ] A 👍 on a researched candidate enqueues it, and it posts on the next scheduled window.
      End-to-end, verified live on the real account.
- [ ] `post.yml` fires on the heartbeat, and `post_tick` posts at most one entry per tick and
      only inside a window, at that window's randomized time. Verified by test with a frozen
      clock across a simulated day.
- [ ] The day's post times differ from the previous day's. Verified by test across two
      simulated day-rollovers.
- [ ] A distilled theme produces a `/log` draft in the VibeKoded repo that enters the existing
      auto-ship pipeline and passes its pre-commit validators unchanged.
- [ ] The Bluesky sink still works with the website sink failing, and vice versa.
- [ ] All existing tests pass. The voice gate, the rotation rules, and the 👍 gate are
      untouched except where this SPEC explicitly extends them.
- [ ] The bot posts something. It has been silent since 2026-07-09.

---

## CARD CONTRACT (v1) — the Slack bus (added by PR B, I-NO-DECOY)

Slack IS the shared state. `poll_and_enqueue` enqueues ANY operator-👍'd candidate card
in the review channel (`C0BGDT13CN7`), no matter who posted it — the bot, or the
operator's PC-side research task (which has no push creds and so cannot write
`refill-surfaced.jsonl`). A card is recognized by ONE machine-readable envelope line; the
rest of the message is human sugar and may be anything.

### The envelope line (the ONLY thing the parser reads)

Somewhere in the message text, on its own line:

    VKS-CANDIDATE-V1: {"id":"<id>","pillar":"<pillar>","freshness":"<freshness>","final_text":"<verbatim post>","provenance":{"source":"<real build event>"}}

- **Marker**: literally `VKS-CANDIDATE-V1:` followed by a space, then a single-line,
  compact JSON object. Match is `VKS-CANDIDATE-V1:\s*(\{.*\})`. Emit the JSON on ONE line.
- **`id`** — stable, unique string. The durable dedup key: an id that has ever been
  enqueued (it persists in `content-queue.jsonl`) never enqueues again, even across
  restarts or a lost ledger. Reuse the same id if you re-post the same candidate.
- **`pillar`** — exactly one of: `showcase`, `operator`, `ask-help`, `dreaming`,
  `question`, `meta`.
- **`freshness`** — `"evergreen"` marks low-value filler (the bot's fallback). ANY other
  value (e.g. `"fresh"`) marks a researched card; a researched card in the channel this
  cycle SUPPRESSES the bot's evergreen fallback (I-NO-DECOY). Never dress evergreen as fresh.
- **`final_text`** — the EXACT post body, posted verbatim to Bluesky. JSON-escaped, so
  it may contain newlines/quotes. Must pass the voice gate (no em/en-dashes, no
  praise-opener, no trailing validation stamp) and the privacy guard.
- **`provenance`** — object; MUST carry a non-empty `"source"` naming the real build
  event it came from (commit/merge/incident/reversal). I-PROVENANCE: no source ⇒ rejected.
  `type` (optional): one of `ship|fix|decision|moment|receipt` (defaults to `moment`).

### Rules

- **One card = one message = one reaction target** (I-GATE). Never a digest with one
  reaction bar for many candidates.
- The human-facing part should state the pillar plainly (the bot renders `*PILLAR: <p>*`).
- **Verification at ENQUEUE time** (external text never saw generation): fields present →
  provenance present → voice gate → privacy guard → rotation eligibility. A card that
  can't be parsed or can't be verified does NOT enqueue and logs the tripped rule (never
  the body). A card that is valid but currently rotation-blocked is HELD, not dropped — it
  enqueues on a later poll once rotation clears (I-APPROVABLE edge case).
- The bot's own surfaced cards carry the identical envelope, so one parser + one dedup
  covers both sources. The `refill-surfaced.jsonl` ledger is now an optimization for older
  bot cards, not a requirement.
