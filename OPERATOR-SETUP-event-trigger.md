# Operator Setup — the Slack 👍 Event Trigger (SPEC-v4)

Wire a Slack 👍 to fire `scout-act` in **seconds** instead of waiting on GitHub's
unreliable `*/5` cron. Flow: your 👍 → Slack Events API → a Vercel function
(`api/slack_events.py`, already in this repo) → GitHub `repository_dispatch` →
`scout-act` runs.

**You need ~15 minutes.** Nothing here loosens a gate: the webhook only *wakes*
`act_tick`; every approval/cap/guard check still runs inside it (I-TRIGGER-NOT-ACTION).

You'll do three things: **(A)** deploy the function to Vercel, **(B)** set 4 env
vars (2 are secrets; a 5th is optional), **(C)** point your Slack app at it.

---

## Before you start — collect 4 required values (+1 optional)

| Value | Required? | Where to get it |
|---|---|---|
| `SLACK_SIGNING_SECRET` | **required** | api.slack.com/apps → your app → **Basic Information** → *App Credentials* → **Signing Secret** (Show). |
| `GITHUB_DISPATCH_TOKEN` | **required** | A **fine-grained** GitHub PAT — created in step B2 below. |
| `GITHUB_REPO` | **required** | `Sovereignprime357/vibekoded-social` |
| `SLACK_OPERATOR_USER_ID` | **required** | Your Slack `U…` id (same as the repo secret). In Slack: your profile → ⋯ → *Copy member ID*. |
| `SLACK_CHANNEL_ID` | **optional** | Your `C…` misc/activity channel. **No longer required by the webhook** — the 4 routed channels (likes/replies/reposts/converse) are baked-in defaults. Set this ONLY if you also want a 👍 in the misc channel to fire the trigger. |

> **Multi-channel note (SPEC-v3.1):** the webhook accepts a 👍 from any of the 4
> routed channels out of the box — no channel env var needed. To change that set,
> set `SLACK_TRIGGER_CHANNELS` (comma-separated channel IDs); it replaces the
> default set entirely.

---

## A. Deploy the function to Vercel

The function lives at `api/slack_events.py` in this repo. Deploying the repo to
Vercel serves it at `https://<your-deployment>/api/slack_events` — zero config
(Vercel auto-detects `api/*.py` as a Python serverless function).

1. Go to **vercel.com → Add New… → Project → Import Git Repository**, and pick
   `Sovereignprime357/vibekoded-social`.
2. Framework Preset: **Other** (there's no site to build — only the API function).
   Leave build/output settings empty. Click **Deploy**. *(The repo ships a
   `pyproject.toml` that configures the Python entrypoint + zero function deps —
   no manual Vercel build config needed.)*
3. When it finishes, note the domain, e.g. `https://vibekoded-social.vercel.app`.
   Your **Request URL** is that + `/api/slack_events`, e.g.
   `https://vibekoded-social.vercel.app/api/slack_events`.

> Keeping this as its **own** Vercel project (separate from vibekoded.com) is the
> clean choice — it isolates the trigger and its secrets. If you'd rather host it
> under the vibekoded.com project instead, that also works; the route is still
> `/api/slack_events`.

## B. Set the environment variables (Vercel → Project → Settings → Environment Variables)

Add the **4 required** vars (Production scope). The first two are **secrets** —
never commit them. `SLACK_CHANNEL_ID` is now optional (see the note above).

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
5. *(optional)* `SLACK_CHANNEL_ID` = your misc `C…` id — only to also fire on a 👍
   there. The 4 routed channels already fire without it.

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
