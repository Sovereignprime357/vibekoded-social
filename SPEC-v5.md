# SPEC — v5: The Reliable Heartbeat (kill cron flakiness for detection)
## Project: vibekoded-social
## Phase: v5 (dependable detection cadence)
## Last Updated: 2026-07-09
## Builds on: SPEC-v4 (event trigger for 👍→act), SPEC-v2/v3 (scout/converse loops)

## WHAT
An external, reliable scheduler (cron-job.org) pings a secured Vercel endpoint on a
dependable ~15-minute cadence; the endpoint fires a GitHub `repository_dispatch`
(`event_type: "heartbeat"`) that runs BOTH detection ticks — **scout-tick**
(scanning Bluesky for opportunities) and **notify-tick** (catching replies to the
bot). This replaces reliance on GitHub Actions' best-effort `schedule:` cron, which
was observed lagging **2–4 hours** between runs that should be ~10–15 min apart.

The GitHub `schedule:` triggers stay in place as a FALLBACK; the heartbeat is
additive. Existing `concurrency` guards on both workflows keep heartbeat-fired and
cron-fired runs from racing on their state ledgers.

## WHY
SPEC-v4 already fixed 👍→act latency (Slack → Vercel → `repository_dispatch` →
scout-act in seconds). What stayed flaky is DETECTION: scout/notify still rode
GitHub cron, so opportunities and inbound replies could go unseen for hours. The
value of the whole system is that it notices things promptly; a dependable heartbeat
restores that without depending on a scheduler GitHub explicitly documents as
best-effort.

## INPUTS
- **Scheduled ping** — cron-job.org POSTs to `https://<deployment>/api/heartbeat`
  every ~15 min with `Authorization: Bearer <HEARTBEAT_SECRET>`.
- **Host env (Vercel, secrets never in repo):** `HEARTBEAT_SECRET` (shared with
  cron-job.org — a LOW-value wake token), plus the already-present
  `GITHUB_DISPATCH_TOKEN` (fine-grained PAT, this repo) + `GITHUB_REPO`.

## OUTPUTS
- On a valid ping: one `POST /repos/{owner}/{repo}/dispatches`
  (`event_type: "heartbeat"`) → GitHub runs scout-tick + notify-tick.
- On an invalid/missing token: `401`, NO dispatch.

## INVARIANTS
- **I-DEPENDABLE-CADENCE** — detection runs on a bounded, reliable cadence
  (~15 min) driven by an external scheduler, independent of GitHub cron. No
  multi-hour gaps. Cadence is a config value (the cron-job.org schedule), not code.
- **I-TRIGGER-NOT-ACTION (reaffirmed, critical)** — the heartbeat only WAKES the
  detection ticks. It grants NO new authority: scout still only surfaces (never
  auto-acts beyond the SPEC-v3 autonomy ladder), notify/converse still triage +
  guard + human-gate. A spoofed or duplicated heartbeat can at worst cause a wasted
  detection run — never an unapproved public action. The endpoint fires
  `repository_dispatch` and nothing else.
- **I-SECURED-TRIGGER (fail-closed)** — the heartbeat endpoint verifies a shared
  secret (constant-time compare) before dispatching; missing/wrong/absent →
  `401`, no dispatch. The GitHub PAT stays server-side in Vercel env and is NEVER
  exposed to the scheduler — cron-job.org only holds the low-value `HEARTBEAT_SECRET`
  (worst case if it leaks: someone triggers extra detection runs, bounded by
  concurrency + triage cost, never a write to the repo).
- **I-CRON-FALLBACK** — the GitHub `schedule:` triggers remain as a fallback so a
  scheduler outage degrades to "slow" (old behavior), never to "dead". The heartbeat
  is purely additive.
- **I-CONCURRENCY** — scout.yml and notify.yml keep their `concurrency` groups
  (`cancel-in-progress: false`), so a heartbeat-fired run and a cron-fired run (or a
  burst) queue serially rather than racing the state commit-back. Append-only `.jsonl`
  ledgers use `merge=union` (`.gitattributes`) so concurrent appends auto-merge on
  rebase instead of conflicting.

## EDGE CASES
- **Scheduler outage** — cron-job.org down → GitHub cron fallback still runs
  (laggy but alive). I-CRON-FALLBACK.
- **Duplicate / burst pings** — multiple heartbeats close together → runs queue
  (I-CONCURRENCY); union-merge ledgers absorb concurrent appends. Harmless.
- **Wrong / missing secret** — `401`, no dispatch (I-SECURED-TRIGGER).
- **GitHub dispatch fails (PAT expired / GitHub down)** — endpoint logs it and still
  `200`-acks the scheduler (retrying won't fix a bad PAT); the next heartbeat + the
  cron fallback retry. Operator rotates the PAT.
- **Cost creep** — cadence too aggressive would multiply Haiku triage. Fixed at
  ~15 min (96 pings/day → 96 scout + 96 notify runs/day); scans are incremental so
  total triaged candidates/day is roughly cadence-independent.

## OUT OF SCOPE (v5)
- Replacing the GitHub cron (kept as fallback).
- Any action logic in the endpoint (only dispatch — I-TRIGGER-NOT-ACTION).
- A paid scheduler (Vercel Cron Hobby is once-per-day — unusable here; cron-job.org
  free covers the cadence).
- Per-tick sub-15-min frequency (dependability is the goal, not high frequency).

## ACCEPTANCE CRITERIA
- A validly-signed heartbeat POST produces exactly one `repository_dispatch`
  (`heartbeat`) → scout-tick + notify-tick both run within a minute.
- An invalid/missing token returns `401` and dispatches nothing (tested).
- scout.yml + notify.yml run on `repository_dispatch: [heartbeat]` AND still on
  their `schedule:` + `workflow_dispatch`; concurrency groups intact.
- The GitHub PAT is never placed in the scheduler; only `HEARTBEAT_SECRET` is.
- Detection cadence is dependable ~15 min with no multi-hour gaps.

---
_Companion to SPEC.md + SPEC-v2/v3/v4 + PERSONA.md. v5 makes detection dependable
without loosening a single gate and without paying for a scheduler._
