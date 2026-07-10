# SPEC — Content Refill Loop (daily queue top-up + 👍-gated rotation)
## Project: vibekoded-social (Bluesky bot)
## Phase: SPEC-content-refill-v1

## WHAT
A scheduled morning loop (~8am local) that generates N pillar-rotated candidates (start: 5) from approved sources, posts them to Slack channel C0BGDT13CN7 for operator review, and on the operator's 👍 promotes each approved candidate into content-queue.jsonl so post-tick keeps posting. Reuses generate.build_prompt (pillar-aware), the content-engine rotation rules, and the same Slack 👍-poll pattern scout-act already uses (new action type: enqueue to content queue).

## WHY
The queue has no refill mechanism. Starter seeds were finite; once used, post_tick returns "success" on an empty queue and the bot silently stops posting (went quiet ~17h undetected). This keeps the queue fed with vetted, on-brand content, human-approved before it reaches the public timeline.

## INPUTS
- Sources, priority order: (1) real build activity — operator's actual shipped work; (2) evergreen pillar content across the 6 pillars — renewable floor; (3) graduated intel — insights that cleared the weekly dream + Council. NEVER raw/ungraduated ops intel.
- Pillar rotation state. Operator 👍 in C0BGDT13CN7.

## OUTPUTS
- N daily candidates → chat.postMessage (bot token) to C0BGDT13CN7, each with slack_ts + pillar + provenance, awaiting 👍.
- On 👍 → candidate appended to content-queue.jsonl with provenance + approval trail.
- Queue-empty alert → loud Slack message when unused queue entries hit 0.

## INVARIANTS
- I-HUMAN-GATE-CONTENT — no candidate enters the queue without the operator's explicit 👍 in C0BGDT13CN7.
- I-NO-RAW-INTEL — raw/ungraduated ops intel MUST NOT become a post; only graduated knowledge is eligible.
- I-GUARDED — every candidate passes brand-voice + privacy guards (incl. client terms) FAIL-CLOSED before it's even surfaced to Slack.
- I-PILLAR-ROTATION — no 2 consecutive same-pillar; META ≤ 1-in-5.
- I-PROVENANCE — every candidate + queued post carries source + pillar + approval trail.
- I-SEPARATION — distinct from the intel distill/dream loops; the dream's graduated output is an INPUT only; never modify the intel pipeline.
- I-CADENCE-EARNED — default 5/day.

## EDGE CASES
- Operator doesn't 👍 → nothing enqueues; queue-empty alert fires; generate a small surplus (5-7) so a buffer can build over time (v1: lean on the alert).
- Thin source day → leans evergreen → repetition risk; carry a low-freshness note in the Slack batch.
- Duplicate vs posted.jsonl → dedup before surfacing.
- Slack token missing → safe-degrade: log + skip; NEVER auto-post ungated content.
- Invariant-violating draft → guard fail-closed drops it before Slack.
- State-commit contention → reuse atomic pattern (--autostash, merge=union).

## OUT OF SCOPE
- Fully autonomous no-review posting (graduation target after v1). Cadence >5/day. Multi-account fleet. Reworking the distill/dream. Any raw-intel→post path.

## ACCEPTANCE CRITERIA
- ~8am job posts 5 guarded, pillar-rotated candidates to C0BGDT13CN7 with slack_ts + pillar + provenance.
- A 👍 promotes exactly that candidate into content-queue.jsonl within one poll cycle; un-👍'd candidates never enqueue.
- Raw ops intel provably never a source.
- post_tick draws from the refilled queue; no more silent drain.
- Queue-empty triggers a Slack alert.
- Guards fail-closed; invariant grep clean.
- Safe-degrade verified: no token → logs + skips, never posts ungated.
