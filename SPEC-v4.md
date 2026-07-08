# SPEC — v4: The Event Trigger (kill the cron-lag latency)
## Project: vibekoded-social
## Phase: v4 (event-driven acting)
## Last Updated: 2026-07-08
## Builds on: SPEC-v2 (SEE→TRIAGE→SURFACE→ACT), SPEC-v3 (pillars + active engagement + autonomy ladder)

## WHAT
A Slack 👍 (`reaction_added`) from the operator fires the `scout-act` workflow in
**seconds**, via: Slack Events API → a serverless webhook (Vercel) → GitHub
`repository_dispatch`. This replaces the dependency on GitHub's `*/5` cron, which
in production fired sparsely and unpredictably (observed: one scheduled run with a
154-minute gap), so an operator 👍 could sit unprocessed for 25–50+ minutes.

The webhook does exactly ONE thing on a valid operator 👍: `POST` a
`repository_dispatch` with `event_type: "slack-thumbsup"`. It contains **no action
logic**. `scout-act.yml` gains `on: repository_dispatch: types: [slack-thumbsup]`
alongside the existing schedule (now a best-effort fallback) and `workflow_dispatch`.

## WHY
GitHub Actions scheduled workflows are best-effort and heavily throttled; `*/5` is
aspirational, not real. The whole value of the ACT layer is that a 👍 acts quickly.
An event trigger makes latency seconds instead of tens of minutes, WITHOUT granting
any new authority — the webhook only wakes the same, fully-gated `act_tick`.

## INPUTS
- **Slack event** — a `reaction_added` event POSTed by Slack to the Request URL,
  with `X-Slack-Signature` + `X-Slack-Request-Timestamp` headers and a JSON body.
- **Slack URL-verification challenge** — a one-time `{type:"url_verification",
  challenge:"…"}` POST during setup; the function echoes the challenge.
- **Host env (secrets, never in repo):** `SLACK_SIGNING_SECRET`, `GITHUB_DISPATCH_TOKEN`
  (fine-grained PAT, this repo only, `repository_dispatch` write), `GITHUB_REPO`
  (`owner/repo`), `SLACK_OPERATOR_USER_ID`, `SLACK_CHANNEL_ID`.

## OUTPUTS
- On a **valid operator 👍**: one `POST /repos/{owner}/{repo}/dispatches`
  (`event_type:"slack-thumbsup"`), then a `200` ack to Slack.
- On the **challenge**: `200` echoing `challenge`.
- On **anything else** (wrong emoji / non-operator / other channel / non-reaction
  event): `200` ack, NO dispatch.
- On **invalid/missing/stale signature**: `401`, NO dispatch, NO body.

## INVARIANTS
- **I-SIG-VERIFY (fail-closed)** — every request (except it can't even be parsed)
  is authenticated by recomputing the Slack v0 HMAC-SHA256 signature over
  `v0:{timestamp}:{raw_body}` with the signing secret, constant-time compared, AND
  the timestamp must be within a ±5-minute window (replay guard). Invalid, missing,
  or stale → `401`, no dispatch. No signature, no action. Ever.
- **I-TRIGGER-NOT-ACTION (critical)** — the trigger ONLY wakes `act_tick`; it grants
  NO action authority. Every existing gate still runs unchanged inside `act_tick`:
  operator-👍 match (re-checked against Slack reactions), per-class daily caps,
  pacing, per-tick cap, self-label-confirmed, privacy guard, I-NO-SELF. A spoofed or
  duplicated trigger can at worst waste a workflow run — NEVER cause an unapproved
  action. The webhook performs `repository_dispatch` and nothing else.
- **I-OPERATOR-SCOPED (fail-closed)** — dispatch only when `event.user ==
  SLACK_OPERATOR_USER_ID` AND `reaction ∈ {+1, thumbsup}` (skin-tone tolerated) AND
  `event.item.channel == SLACK_CHANNEL_ID`. Any mismatch or missing field → no
  dispatch. If the operator id or channel env is unset, dispatch nothing (fail-closed).
- **I-SECRET-SAFE** — the PAT and signing secret live ONLY in the host (Vercel) env,
  never committed. The PAT is fine-grained: this repo only, `repository_dispatch`
  write, nothing else. Compromise of the webhook can trigger runs, not read code or
  act elsewhere.
- **I-CONCURRENCY** — `scout-act` carries a `concurrency` group
  (`vibekoded-social-act-state`, `cancel-in-progress: false`) so event-fired runs
  QUEUE serially instead of racing on the act-log/ledger. Combined with act-log
  dedup, duplicate/concurrent triggers are no-ops, never double-acts.
- **I-FAST-ACK** — Slack retries a delivery it doesn't see acked within 3s. The
  function verifies + dispatches (a sub-second `urllib` call) then acks `200` well
  inside the window; a Slack retry that slips through is harmless (idempotent
  downstream via I-TRIGGER-NOT-ACTION + I-CONCURRENCY + act-log dedup).

## EDGE CASES
- **Replay attack** — an attacker re-sends a previously-valid signed body. Blocked by
  the ±5-min timestamp window (I-SIG-VERIFY); an old timestamp fails even with a
  valid-looking sig.
- **Stale timestamp / clock skew** — timestamp older/newer than the window → `401`.
- **👍-storm** — many reactions in a burst → many dispatches → runs QUEUE
  (I-CONCURRENCY) → act-log dedup makes repeats no-ops. Bounded, harmless.
- **Slack 3s-retry** — a slow ack → Slack re-delivers → at most a duplicate dispatch,
  which is idempotent downstream. We still ack fast to avoid it.
- **Invalid signature** — `401`, no dispatch (I-SIG-VERIFY).
- **Non-operator reactor** — a teammate/Slackbot 👍 → `200` ack, no dispatch
  (I-OPERATOR-SCOPED). Even if one slipped through, `act_tick` re-checks the operator
  id against the message's reactions, so it still wouldn't act.
- **Wrong emoji / wrong channel / non-reaction event (message, reaction_removed…)**
  → `200` ack, no dispatch.
- **GitHub dispatch fails (PAT expired / GitHub down)** — log server-side, still ack
  `200` to Slack (retrying Slack won't fix a bad PAT); the `*/5` cron remains the
  fallback so acting still happens, just slower. Operator fixes the PAT.
- **Env unset (PAT / signing secret / operator id / channel)** — fail-closed: verify
  fails or the filter dispatches nothing. The webhook never acts on a half-config.

## OUT OF SCOPE (v4)
- Any action logic in the webhook (it only dispatches — I-TRIGGER-NOT-ACTION).
- Replacing the cron (kept as a best-effort fallback, not removed).
- Slack interactivity / buttons / slash commands (reactions only).
- Bidirectional Slack posting from the webhook (the bot still posts via the workflow).
- A persistent server / queue (serverless function only; GitHub concurrency is the queue).

## ACCEPTANCE CRITERIA
- Slack URL-verification challenge is echoed (setup succeeds).
- A validly-signed operator 👍 in the target channel produces exactly one
  `repository_dispatch` (`slack-thumbsup`) and `scout-act` fires within seconds.
- An invalid/missing/stale signature returns `401` and dispatches nothing (tested).
- A non-operator reactor, wrong emoji, or wrong channel dispatches nothing (tested).
- `scout-act.yml` runs on `repository_dispatch: slack-thumbsup` and still on schedule
  + `workflow_dispatch`; the concurrency group serializes runs.
- All prior invariants (SPEC/v2/v3) hold; the trigger adds NO action authority.

---
_Companion to SPEC.md + SPEC-v2.md + SPEC-v3.md + PERSONA.md + MISSION-FILTER.md.
v4 makes acting event-driven and low-latency without loosening a single gate._
