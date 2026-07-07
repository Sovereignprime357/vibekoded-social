# vibekoded-social

Automated build-in-public posting engine for Bluesky. Full contract in
`SPEC.md`; voice contract in `PERSONA.md`. Read both before touching code —
this README is the RUNBOOK for going live, not a design doc.

Architecture in one line: a handoff file (`content-queue.jsonl`) gets
appended to during real work sessions; a GitHub Actions cron picks the
oldest unused entry, generates a post in the shared-account voice, runs it
through a privacy guard, and posts it — all as a short-lived script that
exits, never a persistent process.

---

## RUNBOOK — going live, in order

### 0. Prerequisites

- A Bluesky account you're willing to dedicate to this (see step 1 — do
  NOT reuse a personal account's login password).
- A GitHub account, willing to host this as a **public** repo (public repos
  get unlimited free GitHub Actions minutes; private repos have a limited
  free tier that a 20-min-cadence notification cron will burn through
  quickly).
- A free Gemini API key ([Google AI Studio](https://aistudio.google.com/apikey))
  or a free Groq API key ([console.groq.com](https://console.groq.com/keys)).
  Pick one — `GEN_MODEL` env var controls which.

### 1. Create the Bluesky account + app password

1. Go to [bsky.app](https://bsky.app) and create the account. Pick a handle
   that hints "human + bot co-run this" (per SPEC.md's suggestion) if you
   want that visible in the identity itself.
2. Fill in the bio to disclose automation up front — **I-BOT-DISCLOSED** is
   a hard invariant, not optional polish. Something like: "automated
   build-in-public account, co-run by a vibe coder and the AI that writes
   the code. real receipts, no filler." Set the account's self-label /
   automation flag in Bluesky's settings if the app version you're on
   exposes one (Settings -> Account -> this varies by app version).
3. Go to **Settings -> App Passwords -> Add App Password**. Name it
   something identifiable (e.g. `vibekoded-social-bot`). Copy the password
   **immediately** — Bluesky only shows it once. This is what goes into
   `BSKY_APP_PASSWORD`, never the account's real login password.

### 2. Create the GitHub repo

1. Create a **new, public** GitHub repository (empty, no README/license
   auto-init — this project already has its own).
2. **Note:** there is already an empty, uncommitted `.git/` folder in this
   project (a build-environment artifact — the sandbox that built this
   attempted a throwaway `git init` while diagnosing a mount issue; it has
   zero commits and zero objects, so it's harmless). `git init` below will
   just reinitialize over it cleanly — no special cleanup needed, but if
   you'd rather start pristine, delete the `.git` folder by hand first.
3. From this folder (`C:\Users\zenit\projects\vibekoded-social`):
   ```
   git init
   git add .
   git commit -m "chore: initial vibekoded-social v1 scaffold"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo-name>.git
   git push -u origin main
   ```
4. Double-check nothing secret went in: `.env`, `.session.json`, and any
   real credentials should never appear in `git status` as staged (they're
   in `.gitignore`). If you ever DID commit a real secret, rotate it
   immediately (new app password, new API key) — removing it from a future
   commit does not remove it from git history.

### 3. Set the GitHub Secrets and (optional) Variables

Repo -> **Settings -> Secrets and variables -> Actions**.

**Secrets** (Secrets tab — encrypted, never visible again after saving):
| Name | Value |
|---|---|
| `BSKY_HANDLE` | your bot's Bluesky handle, e.g. `yourbot.bsky.social` |
| `BSKY_APP_PASSWORD` | the app password from step 1.3 |
| `GEMINI_API_KEY` | your Gemini key (if `GEN_MODEL=gemini`, the default) |
| `GROQ_API_KEY` | your Groq key (if `GEN_MODEL=groq`) |

**Variables** (Variables tab — plain text, fine for non-secret toggles):
| Name | Value | Purpose |
|---|---|---|
| `GEN_MODEL` | `gemini` or `groq` | which model API to call. Omit to default to `gemini`. |
| `DRY_RUN` | `1` or `0` | `1` = pipeline runs fully but never actually posts/replies to Bluesky (prints what it would do instead). Leave at `0` (or unset) once you're ready to go live. **Start with `1`.** |

### 4. Run the dry-run test (no credentials needed for THIS step)

Locally, before touching GitHub Actions at all:

```
cd vibekoded-social
pip install -r requirements.txt
python -m pytest
```

Both `tests/test_guard.py` and `tests/test_content_queue.py` should pass with
zero credentials, zero network calls, zero setup beyond `pip install`.

Then run the full pipeline end-to-end in dry-run:

```
DRY_RUN=1 python post_tick.py
```

(On Windows PowerShell: `$env:DRY_RUN=1; python post_tick.py`.)

This should print the exact text it WOULD post to Bluesky, using the
seeded entry in `content-queue.jsonl`, and mark that entry `used: true`
without making any network call at all. If you want to run it again,
either add a new entry (see step 6) or flip that entry's `used` back to
`false` by hand.

Once you're happy locally, push with `DRY_RUN=1` still set as the repo
Variable (step 3) and manually trigger both workflows from the **Actions**
tab (`workflow_dispatch`) to confirm they run clean in GitHub's environment
too — check the run logs for the same "would post" output, and confirm the
commit-back step runs (it should show `content-queue.jsonl` / `posted.jsonl`
changes committed, even in dry-run — dry-run still marks entries used and
logs to `posted.jsonl` so the whole state-tracking path gets exercised,
it just skips the literal network call to Bluesky).

### 5. Go live: first real post

1. Flip the `DRY_RUN` repo Variable to `0` (or delete it — `0` is the
   default when unset).
2. Trigger `post-tick` manually from the Actions tab (`workflow_dispatch`),
   OR just wait for the next scheduled cron tick (see `.github/workflows/post.yml`
   for the fixed UTC times).
3. Check the Bluesky account — your first real, automated, in-voice post
   should be live. Check `posted.jsonl` in the repo (the workflow commits
   it back) — it should have one new line with the `uri`/`cid` of the post.
4. Consider running `python smoke_test.py` locally first (opt-in, makes
   real API calls) if you want to sanity-check login + notification-fetch
   BEFORE the first scheduled post — `python smoke_test.py --post` will
   additionally publish one clearly-labeled real test post so you can
   confirm the account's posting path works before the drip starts.

### 6. Seed the content queue (ongoing)

The queue is the fuel. Nothing gets posted without a real entry in
`content-queue.jsonl` (I-SUBSTANCE — the engine never fabricates). Two ways
to add entries:

**By hand** — append a line directly:
```json
{"ts": "2026-07-07T00:00:00Z", "type": "fix", "raw": "plainly describe what actually happened here", "angle": "optional suggested hook", "used": false}
```
`type` must be one of: `ship`, `fix`, `decision`, `moment`, `receipt`.

**Via Python** (from a Claude session / Claude Code, or any script with
this repo checked out):
```python
import content_queue
content_queue.append_entry(
    raw="found a race condition in the queue module, fixed it in twenty minutes",
    type="fix",
    angle="the boring discipline angle",  # optional
)
```

Commit and push after adding entries (or let the next scheduled workflow
run pick them up if they're already pushed — `post_tick.py` reads whatever
is in the repo at checkout time).

---

### Housekeeping note: two retired stub files

During the build, the content-queue module was originally named `queue.py`,
which turned out to shadow Python's own stdlib `queue` module (used
internally by `requests`) and broke at runtime. It was renamed to
`content_queue.py`. The build environment used to create this project could
not delete files on its mount, so `queue.py` and `tests/test_queue.py` were
left behind as empty, clearly-labeled stub files instead of being removed.
They are inert (nothing imports them, pytest collects `tests/test_queue.py`
but it has zero tests in it) — feel free to `rm queue.py tests/test_queue.py`
whenever convenient; there's no rush and no risk either way.

---

## What's in this repo

| File | Purpose |
|---|---|
| `guard.py` | I-PRIVACY enforcement. `check(text) -> (ok, reason)`. Fails closed. |
| `content_queue.py` | Read/write `content-queue.jsonl`. `append_entry`, `get_next_unused`, `mark_used`. (Named `content_queue.py`, not `queue.py`, to avoid shadowing Python's stdlib `queue` module — see the module docstring.) |
| `generate.py` | Builds the prompt from `PERSONA.md` + a queue entry, calls Gemini/Groq, cleans output. Has a DRY_RUN / no-key stub path. |
| `bluesky.py` | AT Protocol client (app-password auth), cached session (`.session.json`), `post`, `reply`, `get_notifications`. |
| `post_tick.py` | Scheduled posting entrypoint: `get_next_unused -> generate -> guard.check -> post -> mark_used`. |
| `banter.py` | Notification-poll entrypoint: own-account reply -> auto-post; stranger reply -> draft for approval. |
| `smoke_test.py` | OPT-IN live check against the real API. Never run by `pytest`. |
| `config.py` | `EXTRA_TERMS` — operator-added guard terms on top of the hardcoded floor. |
| `tests/test_guard.py` | Proves the guard blocks every term (all cases, mid-sentence, with punctuation) and passes clean text, including every `PERSONA.md` example post. |
| `tests/test_content_queue.py` | Proves append/read/mark_used/dedup behavior. |
| `.github/workflows/post.yml` | Cron: run `post_tick.py` a few times/day, commit state back. |
| `.github/workflows/notify.yml` | Cron: run `banter.py` every ~20 min, commit state back. |
| `queue.py`, `tests/test_queue.py` | RETIRED stubs — see note below. Safe to delete by hand. |

## Local development

```
pip install -r requirements.txt
python -m pytest              # guard + content_queue unit tests, no credentials needed
DRY_RUN=1 python post_tick.py  # full pipeline, no network calls
DRY_RUN=1 python banter.py     # full pipeline, no network calls (needs a
                                # cached .session.json or real creds to reach
                                # the notification-fetch step — see below)
```

Note: `banter.py`'s DRY_RUN still calls `bluesky.get_notifications()` to
fetch what's new (there's no stub for reading Bluesky's own state — only
the generation and posting steps have a credential-free path). Running it
without real credentials will fail at the login step, which is expected
and fine locally; the GitHub Actions workflow always has real secrets
available. If you want to exercise `banter.py`'s classify/generate/guard
logic without any Bluesky account at all, call `classify()`,
`process_own_account()`, or `process_stranger()` directly with a
hand-built notification dict, the way you would in a REPL.

## Invariants (do not weaken these — see SPEC.md for the authoritative list)

- **I-PRIVACY** — `guard.py` blocks personal names + family terms, fails
  closed. Covered by `tests/test_guard.py`.
- **I-BOT-DISCLOSED** — set at the account level (bio + bot label), not
  enforced in code. Verify manually per step 1.2 above.
- **I-SUBSTANCE** — `post_tick.py` never fabricates; if the queue is empty,
  it skips the tick and logs nothing was posted.
- **I-NO-AUTO-POKE** — `banter.py` never initiates with a stranger; it only
  reacts to notifications that already exist (someone else engaged first).
- **I-GATED-REPLIES** — `banter.py` never auto-posts to a stranger; it
  writes a draft to `drafts.jsonl` for operator review instead.
- **I-DEDUP** — `content_queue.py`'s `used` flag prevents reposting the same entry;
  `banter.py`'s `handled.jsonl` prevents reprocessing the same notification.

## What still needs YOUR real credentials to verify

See the final section of the delivery report for the full list — in short:
Bluesky account creation + app password, GitHub secrets, and the actual
live `smoke_test.py` run (login, notification fetch, and optionally one
real test post) can only be verified with real credentials, which this
build deliberately never had access to.
