# SPEC-v3 — Bluesky Bot Rewire: Chatbot → Brain-Improvement MINER
*Captured 2026-08-07. Repurposes the live vibekoded-social bot from a build-in-public chatbot into a read-only intel miner that feeds the brain's self-improvement loop. SpecMesh discipline — read before building.*

## WHAT
Rewire the live bot (@vibekoded.bsky.social) from an auto-poster / banter / reply-draft chatbot into a **read-only brain-improvement MINER.** It scans credible AI/agent sources, filters for real techniques that improve the operator's "brain" system — memory architecture, recall, writing / filtering / searching memory, automations, agent orchestration and governance — dedupes, and surfaces ONE batched digest feeding two sinks: (1) the brain self-improvement loop, and (2) content / newsletter raw material.

## WHY
The chatbot behaviors (auto-posting, the two-hander banter, stranger-reply-drafts, any liking/following) are noise, API waste, and carry a real failure mode — the operator watched two bots get stuck in a multi-day sycophantic reply-loop. The SAME infrastructure (SEE → TRIAGE → SURFACE, the multi-model split, the cloud cron) is far more valuable pointed at MINING: it becomes the missing slow-lane SUPPLY for brain self-improvement plus a content source. It also fixes the notification spam (currently ~8 pings per 30-minute cron).

## SCOPE
**IN:** repoint the existing SEE → TRIAGE → EXTRACT → SURFACE loop to mine brain-improvement techniques from a curated set of CREDIBLE sources; batched digest to a single sink; two tagged outputs; retire the outbound behaviors.
**OUT (v3):** all outbound posting / replying / liking / following (killed); multi-platform mining beyond Bluesky (Phase 2 — arXiv / GitHub / HN / blogs); auto-adding anything to the brain (the Council + operator gate that).

## KILL (retire these behaviors and their crons)
- The auto-poster (build-in-public posts) — the `post.yml` / `post_tick.py` posting path.
- The banter two-hander — `banter.py` / `notify.yml`.
- Stranger-reply-drafts, and all ENGAGEMENT actions (post / reply / like / repost / banter).
**Exception — FOLLOW is allowed** (curation, not engagement): the miner may follow credible sources to build and maintain the mine-from list; following generates no content and no reply-loop, so it carries none of the risks above.
Keep the code archived in the repo, but the miner does none of the engagement behaviors. **No posting/replying = no sycophancy-loop, no API waste, no reply-notification spam.**

## KEEP / REPURPOSE (the engine)
- **SEE (scan):** pull posts from the curated CREDIBLE-SOURCE list (the miner's FOLLOW list IS the source list) + relevant feeds/keywords. Mine BOTH new posts AND a source's PAST content — when a source is newly added, backfill its history (`app.bsky.feed.getAuthorFeed`) for techniques if the API allows; a fresh credible source's back-catalog is high-signal. Plain API, no model.
- **TRIAGE (cheap model — free Gemini/Groq, or Haiku):** first-pass "is this a real, substantive brain-improvement technique from a credible source?" This is the MISSION FILTER, repointed from engagement to improvement.
- **EXTRACT (Claude Haiku, only on passers):** turn a passing post into a structured CANDIDATE — `{technique, why-it-matters, source + credibility, link, tags: improvement | content}`. Dedupe against a ledger (semantic — don't resurface the same technique).
- **SURFACE:** ONE batched digest per cycle, not per-item pings.
- Reuse: the privacy guard, the GitHub Actions cloud cron, the cached session token, the semantic-dedupe ledger pattern.

## ARCHITECTURE (multi-model, cost-lean — reuse v2's split)
SCAN = plain API (no model). TRIAGE = free cheap model (volume filter). EXTRACT = Claude Haiku (only the few passers). SURFACE = plain code (assemble digest, post to sink). No model spent scanning or surfacing; Claude touches only what passes triage, so cost ≈ nil.

## THE TWO SINKS
1. **Improvement candidates** → a `brain-improvement-candidates` queue → the **Council** reviews (the slow lane) → operator decides what becomes a skill / pattern / upgrade. The miner PROPOSES; it never writes to the brain. **Retiring the post/banter crons frees a daily scheduled-task slot — repurpose it to run the scheduled COUNCIL REVIEW of this queue.** That closes the loop end-to-end: miner surfaces → scheduled Council review → operator-gated additions. The miner and the review are the two halves of the self-improvement loop, both on a schedule.
2. **Content material** → items tagged `content` become raw material for a `/log` post, a newsletter item, or material for the brothers.

## INVARIANTS (blockers)
- **I-NO-ENGAGEMENT** — the miner never posts, replies, likes, reposts, or banters (no content output, no conversation — this kills the sycophancy-loop, API waste, and notification spam). It MAY FOLLOW credible sources as a curation action to build the mine-from list.
- **I-CREDIBLE-SOURCE** — mine only from the curated credible-source list (+ relevance feeds); every surfaced item carries its source + a one-line credibility note. No random vibe-coders.
- **I-BATCHED** — one digest per cycle, never per-item pings. Cadence daily (or twice-daily), not every 30 minutes.
- **I-DEDUPE** — never resurface a technique already surfaced (semantic dedupe against the ledger).
- **I-PROPOSE-ONLY** — the miner surfaces candidates; it NEVER modifies the brain. The Council + operator gate every addition. (Read-only autonomy tier.)
- **I-COST-CAP** — cheap-model triage on volume; Claude only on passers; hard per-cycle caps on items scanned + Claude calls.

## BUILD ORDER
1. Curate the credible-source list (accounts + feeds) — AI-memory / agent-orchestration / agent-governance practitioners; seed from the existing growth-map's bullseye packs, filtered to CREDIBLE, not just in-lane.
2. Repoint the triage prompt (mission filter) to "brain-improvement technique from a credible source."
3. Build EXTRACT → structured candidate + the dedupe ledger.
4. Build the batched digest → the sink; tag each item `improvement | content`.
5. Retire the posting / banter / reply crons; add the miner cron (daily / twice-daily).
6. Wire sink 1 into the Council-review step (the slow lane).

## ACCEPTANCE
- One clean digest lands per cycle (batched, not per-item), with N credible-sourced candidates, each `{technique, why, source + credibility, link, tags}`, deduped.
- Zero outbound actions occur (verify: no posts / replies / likes in the account activity).
- Notification load drops to one digest per cycle.
- At least one surfaced candidate is genuinely worth Council review (signal check).

## OPEN DECISIONS (yours)
1. **Sink surface:** a Slack digest, or write it into the brain's command-center queue (the informant already reads that), or both?
2. **Cadence:** once a day, or twice?
3. **Credible-source list:** who's on it? (I can draft a seed list of proven AI-memory / agent-orchestration voices for you to prune.)
4. **Platform scope:** Bluesky-only for v3 (reuse the infra), or fold in a couple of high-signal off-Bluesky sources (arXiv / GitHub trending / HN) from the start?
