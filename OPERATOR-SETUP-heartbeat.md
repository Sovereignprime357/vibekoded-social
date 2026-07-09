# Operator Setup — the Reliable Heartbeat (SPEC-v5)

Make the DETECTION ticks (scout-tick = scanning for opportunities, notify-tick =
catching replies to the bot) run on a **dependable ~15-minute cadence** instead of
GitHub's flaky cron (which was lagging 2–4 hours). A free scheduler (cron-job.org)
pings your existing Vercel deployment every 15 min; the webhook fires a GitHub
`repository_dispatch` that runs both ticks.

**~10 minutes.** Nothing here loosens a gate — the heartbeat only *wakes* detection;
every triage/guard/human-gate still runs (I-TRIGGER-NOT-ACTION). The GitHub cron
stays as a fallback, so if the scheduler ever hiccups you're back to "slow," never
"dead."

You'll do two things: **(A)** add one Vercel env var, **(B)** create one cron-job.org job.

---

## Why cron-job.org (not Vercel Cron)

Vercel Cron on the **Hobby (free) plan is limited to once per DAY** — a `*/15` (or even
hourly) schedule *fails deployment*. So it can't do this cadence without a paid Pro
upgrade. **cron-job.org** is free, reliable, fine-grained (per-minute), and can send a
custom `Authorization` header — exactly what we need. Your GitHub token never goes
there; cron-job.org only holds a low-value wake secret.

## A. Add the Vercel env var

This reuses the **same Vercel deployment + GitHub PAT** you set up for the Slack event
trigger (SPEC-v4). Only ONE new var:

1. Make up a long random secret (e.g. `openssl rand -hex 32`, or any 40+ random chars).
   This is `HEARTBEAT_SECRET`.
2. Vercel → your project → **Settings → Environment Variables** → add:
   - `HEARTBEAT_SECRET` = (that random value)
3. **Redeploy** (Vercel → Deployments → ⋯ → Redeploy) so the function picks it up.

> The GitHub PAT (`GITHUB_DISPATCH_TOKEN`) and `GITHUB_REPO` are already set from the
> Slack-trigger setup and are reused as-is. If you haven't done that setup yet, do
> `OPERATOR-SETUP-event-trigger.md` first (it establishes the deployment + PAT).

Your **heartbeat URL** is your deployment domain + `/api/heartbeat`, e.g.
`https://vibekoded-social.vercel.app/api/heartbeat`.

## B. Create the cron-job.org job

1. Sign up (free) at **cron-job.org** → **Create cronjob**.
2. **Title:** `vibekoded heartbeat`
3. **URL:** your heartbeat URL from above (`https://…/api/heartbeat`).
4. **Schedule:** every **15 minutes** — set it to run at minutes
   `0, 15, 30, 45` of every hour (the "Every 15 minutes" preset, or a custom
   selection). *(Keep it at 15 min — 96 runs/day fits the free tier and keeps triage
   cost sane. Don't go below 10 min.)*
5. **Request method:** **POST**.
6. **Headers** (advanced/expand the headers section) → add one:
   - Key: `Authorization`
   - Value: `Bearer <HEARTBEAT_SECRET>`  ← the exact secret from step A, prefixed with `Bearer `
7. Save / enable the job.

That's it. cron-job.org's execution history will show `200 OK` on each run once it's
working.

---

## Verify it works

1. Wait for the next 15-min tick (or hit **"Run now"** in cron-job.org).
2. **GitHub → Actions**: within ~1 min you should see **scout-tick** AND
   **notify-tick** runs with trigger **`repository_dispatch`** (event `heartbeat`).
3. cron-job.org's history shows `200`.

If it doesn't fire:
- **cron-job.org history** shows the HTTP status. `401` = the `Authorization` header /
  `HEARTBEAT_SECRET` don't match (re-check step A value == step B6 value, and that you
  redeployed). Non-200 other = check the Vercel function logs.
- **Vercel → project → Logs**: a valid ping logs
  `dispatched heartbeat -> github status 204`. `401` = bad token. `github dispatch
  HTTP 4xx` = the GitHub PAT is wrong/expired.
- The GitHub `schedule:` cron still runs as a **fallback**, so detection still happens
  even if the heartbeat is misconfigured — just on GitHub's slow, best-effort timing.

---

## What the heartbeat does and does NOT do (safety)

- **Does:** on each valid 15-min ping, fire one `repository_dispatch` that wakes
  scout-tick + notify-tick. That's it.
- **Does NOT:** decide or perform any Bluesky action, and grants NO new authority.
  Scanning still only surfaces; replies still get triaged + privacy-guarded +
  human-gated exactly as before (**I-TRIGGER-NOT-ACTION**). A spoofed or repeated
  heartbeat can at worst cause a wasted detection run — never an unapproved action.
- **Secret safety:** cron-job.org holds only `HEARTBEAT_SECRET` (a low-value wake
  token). Your GitHub PAT stays in Vercel and is never exposed to the scheduler. A
  missing/wrong token → the webhook returns `401` and dispatches nothing.
