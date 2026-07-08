# Operator Setup — the Slack 👍 Event Trigger (SPEC-v4)

Wire a Slack 👍 to fire `scout-act` in **seconds** instead of waiting on GitHub's
unreliable `*/5` cron. Flow: your 👍 → Slack Events API → a Vercel function
(`api/slack_events.py`, already in this repo) → GitHub `repository_dispatch` →
`scout-act` runs.

**You need ~15 minutes.** Nothing here loosens a gate: the webhook only *wakes*
`act_tick`; every approval/cap/guard check still runs inside it (I-TRIGGER-NOT-ACTION).

You'll do three things: **(A)** deploy the function to Vercel, **(B)** set 5 env
vars (2 are secrets), **(C)** point your Slack app at it.

---

## Before you start — collect 5 values

| Value | Where to get it |
|---|---|
| `SLACK_SIGNING_SECRET` | api.slack.com/apps → your app → **Basic Information** → *App Credentials* → **Signing Secret** (Show). |
| `GITHUB_DISPATCH_TOKEN` | A **fine-grained** GitHub PAT — created in step B2 below. |
| `GITHUB_REPO` | `Sovereignprime357/vibekoded-social` |
| `SLACK_OPERATOR_USER_ID` | Same value already set as the repo secret (your Slack `U…` id). In Slack: your profile → ⋯ → *Copy member ID*. |
| `SLACK_CHANNEL_ID` | Same `C…` value as the repo secret (channel → *View channel details* → bottom). |

---

## A. Deploy the function to Vercel

The function lives at `api/slack_events.py` in this repo. Deploying the repo to
Vercel serves it at `https://<your-deployment>/api/slack_events` — zero config
(Vercel auto-detects `api/*.py` as a Python serverless function).

1. Go to **vercel.com → Add New… → Project → Import Git Repository**, and pick
   `Sovereignprime357/vibekoded-social`.
2. Framework Preset: **Other** (there's no site to build — only the API function).
   Leave build/output settings empty. Click **Deploy**.
3. When it finishes, note the domain, e.g. `https://vibekoded-social.vercel.app`.
   Your **Request URL** is that + `/api/slack_events`, e.g.
   `https://vibekoded-social.vercel.app/api/slack_events`.

> Keeping this as its **own** Vercel project (separate from vibekoded.com) is the
> clean choice — it isolates the trigger and its secrets. If you'd rather host it
> under the vibekoded.com project instead, that also works; the route is still
> `/api/slack_events`.

## B. Set the environment variables (Vercel → Project → Settings → Environment Variables)

Add all five (Production scope). The first two are **secrets** — never commit them.

1. `SLACK_SIGNING_SECRET` = (from the table above)
2. `GITHUB_DISPATCH_TOKEN` — create it now:
   - github.com → **Settings → Developer settings → Personal access tokens →
     Fine-grained tokens → Generate new token**.
   - **Resource owner:** your account. **Repository access:** *Only select
     repositories* → `vibekoded-social`.
   - **Repository permissions → Contents: Read and write** (this is what the
     `repository_dispatch` API requires; Metadata read is included automatically).
     Nothing else.
   - Expiration: your call (shorter = safer; set a reminder to rotate).
   - Generate, copy the `github_pat_…` value, paste as this env var.
3. `GITHUB_REPO` = `Sovereignprime357/vibekoded-social`
4. `SLACK_OPERATOR_USER_ID` = your `U…` id
5. `SLACK_CHANNEL_ID` = your `C…` id

**Redeploy** after adding env vars (Vercel → Deployments → ⋯ → Redeploy) so the
function picks them up.

## C. Configure the Slack app

Use the **same** Slack app that already has your bot token.

1. api.slack.com/apps → your app → **OAuth & Permissions** → *Scopes → Bot Token
   Scopes*: make sure **`reactions:read`** is present (add it if not — it may
   already be there from the act-layer setup).
2. **Event Subscriptions** → toggle **On**.
3. **Request URL:** paste your Vercel URL from A3
   (`https://…/api/slack_events`). Slack immediately sends a verification
   challenge; the function echoes it and Slack shows **Verified ✓**.
   *(If it fails: re-check the URL, that you redeployed after setting
   `SLACK_SIGNING_SECRET`, and that the deploy is live.)*
4. **Subscribe to bot events** → **Add Bot User Event** → `reaction_added`. Save.
5. Slack will prompt to **reinstall the app** (scope/event changes require it) →
   **Reinstall to Workspace** → allow.
6. Make sure the bot is **in the target channel** (invite it if needed) so it
   receives reactions there.

---

## Verify it works

1. In the target channel, find a surfaced item and add a 👍.
2. Within a few seconds: **GitHub → Actions → scout-act** shows a new run with
   trigger **`repository_dispatch`** (event `slack-thumbsup`).
3. That run's log shows the normal `act_tick` gate output and executes your
   approved item(s).

If the run doesn't appear:
- **Vercel → your project → Logs**: a valid 👍 logs
  `dispatched slack-thumbsup -> github status 204`. A `401` = signature/secret
  mismatch; `ignored: …` = the reactor/emoji/channel filter (expected for
  non-operator or non-👍). `github dispatch HTTP 4xx` = PAT wrong/expired.
- The `*/5` cron still runs as a **fallback**, so acting still happens eventually
  even if the trigger is misconfigured — you just lose the speed.

---

## What the trigger does and does NOT do (safety)

- **Does:** on *your* 👍 in *your* channel, fire one `repository_dispatch` that
  wakes `scout-act`. That's it.
- **Does NOT:** decide or perform any Bluesky action. Every gate still runs inside
  `act_tick` — your 👍 is re-checked against Slack, per-class caps, pacing,
  self-label confirmation, the privacy guard, and I-NO-SELF all still apply
  (**I-TRIGGER-NOT-ACTION**). A spoofed or duplicated trigger can at worst waste a
  workflow run; it can never cause an unapproved action.
- **Secrets** (`SLACK_SIGNING_SECRET`, `GITHUB_DISPATCH_TOKEN`) live only in Vercel
  env, never in the repo. The PAT is scoped to this one repo. Requests without a
  valid, fresh Slack signature are rejected `401` (**I-SIG-VERIFY**).
