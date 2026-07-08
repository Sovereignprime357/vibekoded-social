# SPEC — v3: From Broadcaster to Participant
## Project: vibekoded-social
## Phase: v3 (content pillars + active engagement)
## Last Updated: 2026-07-07
## Builds on: SPEC.md (v1 posting/banter) + SPEC-v2.md (scout SEE→TRIAGE→SURFACE→ACT)

## WHAT
Two coordinated upgrades that turn the account from a navel-gazing broadcaster into a
discerning participant.
1. **Brain-fed content engine** — replace the stale seeded meta-queue. The poster pulls REAL
   material from the brain (`Claude's Mind` + project files) and rotates across content
   PILLARS instead of rewording one bit: SHOWCASE, OPERATOR, ASK-FOR-HELP, DREAMING,
   QUESTIONS, and META (seasoning only).
2. **Active-engagement ACT layer** — the scout goes from surface-only to acting
   (reply / repost / follow, and eventually DM) via the Slack-approve loop, gated per class
   on the earned-autonomy ladder.
Plus a weekly **DREAMING** scheduled pass that feeds the content (reflection → insight →
ask-for-help / dreaming posts).

## WHY
The live account is 8+ near-identical "i built a bot that…" posts — the exact navel-gaze we
flagged, and it's a SOURCE problem (a stale seeded queue of meta lines), not a voice problem.
A bot that only talks about itself and only passively likes generates no real signal and
drives no attention. The fix: draw from real work (proof-of-work showcase + real operator
needs) and actively participate (reply, ask for help, build relationships), everything
soft-funneling to vibekoded.com. Showing real builds and asking for real help are engagement
magnets a meta-bit-on-loop can never be.

## INPUTS
- **The BRAIN** — `Claude's Mind` + project files: real projects (ASE Detect, products, this
  bot), shipped work, wins, lessons, OPEN PROBLEMS / NEEDS, dreaming-pass insights. Source of
  truth for all content. Every draw passes the privacy guard before it can become a post.
- **Scout candidates** — on-mission Bluesky posts from SPEC-v2's SEE/TRIAGE (targets to
  engage with).
- **Weekly DREAMING output** — reflection insights → content seeds (esp. ASK-FOR-HELP).
- **Operator Slack approvals** — the human gate for every engagement action.

## OUTPUTS
- **Varied posts** across the 6 pillars → Bluesky (via existing post_tick + guard), each
  soft-funneling to the site. Queue entries tagged by pillar for rotation.
- **Gated engagement actions** — replies / reposts / follows (later DMs) executed ONLY after
  Slack approval; logged.
- **A brain-sourced content queue** replacing the stale seed.

## INVARIANTS
- **I-PRIVACY (extended, critical)** — the guard runs on EVERY generated post/reply/DM and
  fails closed. The term list covers personal names + family (Sovereign/Shayler/Phipps/
  daughter/wife/etc.) AND **client/project-confidential terms** (e.g. Asphalt Solutions,
  Bob, Justin, and any client the brain names). The brain holds confidential material; a
  showcase post must never leak a client identity. If a post can't be made public-safe, it
  is dropped, not sanitized-and-hoped.
- **I-BRAND-VOICE** — brand-first (VibeKoded / "the operator"), no real names, no vendor
  names in public chrome. Bragging on the operator is done as "the operator" / the builder
  behind this, never the personal name.
- **I-REAL** — every ask-for-help, showcase, need, and win is REAL (drawn from the brain),
  never invented. No fabricated needs, no fake receipts.
- **I-PILLAR-MIX** — META two-hander bit is SEASONING, ~1-in-5 max; substance pillars carry.
  No two consecutive posts from the same pillar (rotation enforced).
- **I-HUMAN-GATE** — no autonomous public action UNTIL a class earns autonomy (SPEC-v2
  gated ladder). Every action starts 👍-gated; a class graduates to autonomous only with a
  stated safety basis. See **AUTONOMY LADDER** below for the current graduated set. As of
  2026-07-08: FOLLOW is autonomous (capped + paced + dedup'd + self-labelled); like / reply /
  repost / DM remain 👍-gated.
- **I-BOT-DISCLOSED (enforced)** — the account carries the Bluesky `bot` self-label on its
  profile record (`app.bsky.actor.profile.labels` → `com.atproto.label.defs#selfLabels`,
  val `"bot"`), set programmatically + idempotently. This is the disclosure safety basis that
  makes autonomous engagement (e.g. auto-follow) honest rather than covert.
- **I-DM-WARM** — DMs are the LAST tier, only to accounts that have ALREADY engaged
  (replied / followed / mentioned us), NEVER cold, ALWAYS human-approved. Cold-DM is a
  ban vector and forbidden.
- **I-SELECTIVE / I-NO-SPAM** — discerning, low-volume, high-relevance; hard per-class daily
  caps; no like/follow/DM-farming.
- **I-FUNNEL** — content + engagement soft-funnel to vibekoded.com (value-led, link in bio +
  value-led in-post; never hard-sell in body).
- **I-BOT-DISCLOSED** — account stays labeled automated.
- **I-LOGGED** — every post, surfaced item, action, and approval logged + auditable.

## EDGE CASES
- No fresh brain material for a pillar this cycle → skip it, don't manufacture (I-REAL).
- Ask-for-help with no real current need → rotate to another pillar, never post a fake ask.
- Brain material contains a name/client term → guard scrubs or the post is dropped (fail closed).
- Controversial / political even if on-topic → skip, never engage.
- A warm contact turns hostile / thread sours → stop engaging that thread; no DM.
- Rate / spam limits approached → back off, queue, no bursting.
- Operator rejects a surfaced action in Slack → log + learn, never auto-retry.
- Brain empty / unreadable → fall back to a small set of evergreen substance posts, never to
  the meta-loop.

## OUT OF SCOPE (v3)
- Fully autonomous public replies/DMs to strangers (that's the LAST earned tier, not v3 launch).
- Cold outreach / mass DMs (never — forbidden by I-DM-WARM).
- Paid promotion / ads.
- AI image generation for posts (still cut — text-native + real screenshots only).
- Multi-platform (Bluesky only for now).

## ACCEPTANCE CRITERIA
- Content queue is populated FROM THE BRAIN, tagged by pillar; the poster rotates (no >1-in-5
  meta, no two same-pillar in a row).
- ≥4 of the 6 pillars produce real posts from real brain material in a test run.
- The guard BLOCKS any post/reply/DM containing a personal, family, or client term (tested,
  fail-closed).
- The ACT layer surfaces a proposed reply/repost/follow to Slack, executes ONLY after
  approval, and logs it.
- No DM path fires to a non-warm contact or without approval (tested).
- The weekly DREAMING scheduled task runs and drops content seeds into the queue.
- All SPEC.md / SPEC-v2.md invariants still hold; name-grep on public surfaces returns zero.

## AUTONOMY LADDER — graduated classes (the earn-it ladder, live state)
Trust is earned per action-class, never granted wholesale. Each row is a class and whether it
may act WITHOUT an operator 👍. A class graduates only when its safety basis is in place.

| Class            | Autonomous? | Safety basis / gate |
|------------------|-------------|---------------------|
| **follow**       | **YES** (2026-07-08) | Daily cap (10/day) + pacing + dedup + I-NO-SELF + I-LOGGED, on top of the `bot` self-label (I-BOT-DISCLOSED). Autonomous follows post a lightweight Slack FYI **digest** (not gated) so the operator keeps visibility without action. |
| like             | no — 👍-gated | low-stakes but not yet graduated; next candidate. |
| repost           | no — 👍-gated | amplification = higher stakes; stays gated. |
| reply / quote    | no — 👍-gated | voice-critical + stranger-facing; governed by `AUTO_REPLY_BACK` (default off). The LAST/hardest bar. |
| DM               | no — forbidden until warm-contact tier | I-DM-WARM; out of scope here. |

Mechanism: a per-class autonomy map in code (`AUTO_ACT_CLASSES`, default `follow`; converse
reply-backs additionally governed by `AUTO_REPLY_BACK`). A class graduates by config, not a
rebuild. Autonomy NEVER bypasses the privacy guard, the daily caps, pacing, or the per-tick cap.
A bad call → demote the class back to gated.

## SEQUENCING (build order)
1. **Content-engine v3** — brain-fed queue + pillar rotation + the 6 pillar generators. Kill
   the meta-loop FIRST (biggest visible win, ends the embarrassment).
2. **DREAMING weekly schedule** — feeds ask-for-help + dreaming pillars.
3. **ACT layer T1.5** — Slack-approve → execute reply/repost/follow (the active engagement).
   Needs the Slack read/callback path.
4. **Earn autonomy per class** — FOLLOW graduated to autonomous 2026-07-08 (see AUTONOMY
   LADDER). Like/repost next candidates; auto-reply (AUTO_REPLY_BACK) last + hardest.
5. **DM tier LAST** — warm-contact detection + human-approved DM path, only after the above
   prove out.

---
_Companion to SPEC.md + SPEC-v2.md + PERSONA.md + MISSION-FILTER.md. v3 makes the posting
brain-fed and the engagement active, without ever going ungated or off-brand._
