# SPEC — VibeKoded Social Engine (build-in-public Bluesky bot)

_v1 draft · 2026-07-06 · Architect: Claude · Builder: Codex · Operator: Sovereign Prime_
_(Rename/relocate the folder freely; this is a sensible default home.)_

---

## ⇢ DECISIONS NEEDED FROM YOU (answer these 3 and it's locked)

1. **Bluesky account** — do you have one yet? Codex needs the **handle** + an **app password** (you create both; the password never goes to me or into code — it lives as a GitHub secret). Handle idea for the shared-account bit: something that hints "human + bot co-run this."
2. **Gen model for v1** — default = **Google Gemini Flash** (free, better voice quality, generous limits). Alt = **Groq** (free, faster, a notch lower quality). Pick one.
3. **v1 scope** — default = **phased**: v1 ships the posting drip; the Slack reply-draft loop comes in v1.1. OK to phase, or do you want the Slack loop in v1?

**Prereqs (operator-created):** a Bluesky account + app password; a **public** GitHub repo (free unlimited Actions minutes); a Gemini or Groq free API key (stored as a GitHub secret).

---

## WHAT

An automated build-in-public posting engine for **Bluesky**. As you work through the day (Cowork / Dispatch / Claude Code), notable real events — a ship, a fix, a decision, a funny moment — get written to a **handoff file**. A scheduled job pulls from that file on a randomized timer, writes a short post in the **shared-account voice** (human vibe-coder + self-aware AI co-running one account), runs it through a **privacy/invariant guard**, and posts it to Bluesky. Text-first; a real screenshot only when the moment is genuinely visual.

## WHY

Drive traffic + credibility for vibekoded.com on the peer surface, **authentically** — posts are drawn from REAL work, not fabricated, so the receipts write themselves. Doubles as a peer-network and job-lead surface. And the engine is itself a live AI-orchestration demo — the system that markets the shop is a working piece of the shop.

## INTERFACE

**Inputs**
- The **handoff file** — the content source (format below).
- The brain (Claude's Mind / session memory) as deeper context when an entry needs it.

**Outputs**
- Posts to Bluesky (app-password auth).
- A **post log** (dedup + audit trail).

**The handoff file (the seam)**
- A single known file (e.g. `content-queue.jsonl` in this repo).
- Written by Claude during work sessions whenever something is postable.
- Each entry: `{ ts, type: ship|fix|decision|moment|receipt, raw: "<what happened, plainly>", angle?: "<optional suggested hook>", shot?: "<optional screenshot path>", used: false }`.
- Read by the timer: pull unused entries, pick one, generate, post, mark `used: true`.

## PROPERTIES / INVARIANTS (blockers — a post that violates these does NOT ship)

- **I-PRIVACY** (the big one): every post passes the same guard as vibekoded's pre-commit grep — NO personal names (Sovereign, Sovereign Prime, Shayler, Phipps), NO family (daughter, wife, family, kid, child, spouse), NO client-confidential material. Run the grep on the **final** post text before posting; **fail closed** (skip + log) on any hit. One leaked auto-post undoes the whole no-names discipline.
- **I-BOT-DISCLOSED**: the account self-labels as automated (bio + Bluesky bot label).
- **I-SUBSTANCE**: every post is grounded in a real handoff entry. Comedy rides on real material; never comedy-only.
- **I-NO-AUTO-POKE**: never auto-interact with accounts that didn't engage first; no engagement manipulation.
- **I-GATED-REPLIES** (v1.1): replies start **draft-only** (bot drafts → Slack → you send). No autonomous public replies until the tier is earned.
- **I-DEDUP**: the post log prevents reposting the same entry or near-duplicate content.

## EDGE CASES

- **Nothing postable today** → skip the tick, do NOT fabricate. (Optional evergreen fallback bank of pre-approved posts — your call, off by default.)
- **Privacy guard hit** → skip + log for your review; never post a redacted guess.
- **Login/API failure** → reuse the **cached session token**; only alert if `createSession` itself fails (login cap ~300/day).
- **Near-duplicate** → check the log; skip if too similar (port the old bots' dedup idea, simplified to lexical).

## CONSTRAINTS

- **Free stack only:** Gemini/Groq free tier (gen) · Bluesky app-password (free) · GitHub Actions cron (free on a public repo).
- **Text-first. NO AI-generated images.** Real screenshots only, when visual, attached by hand.
- **Hosting = GitHub Actions cron**, post-once-and-exit. NO persistent daemon — this is the fix for the old bots' keep-alive / reset-on-crash pain.
- **Cache the Bluesky session token** (don't re-login each run; the real limit is login, not posting volume).
- **Cadence:** randomized windows, **~3-5 posts/day** (research sweet spot; not the old 10-12).
- **Reuse from the old bots** (`projects/SovereignPrimeXBot`, `projects/cookonomist-bot`): the character-framework scaffold, the generate→clean→validate→retry pipeline, the log + rate-limit structure, the conversion-funnel pattern. **New VibeKoded persona** (crypto voice OUT). Swap Ollama→Gemini/Groq, Twitter→Bluesky.

## ARCHITECTURE (the zipper, assembled)

```
[work happens: Cowork / Dispatch / Claude Code]
     → Claude writes postable events → content-queue.jsonl (the handoff file)

[GitHub Actions cron, randomized ~3-5x/day, post-once-and-exit]
     → pull unused queue entries → pick one
     → Gemini/Groq writes it in the shared-account voice (substance first, wit on top)
     → I-PRIVACY grep (fail closed)
     → post to Bluesky (cached session token) → mark used → append to post log

[v1.1] second job polls listNotifications
     → new reply/mention → push to Slack "X replied: '…' — here's a draft"
     → you send it
```

## PHASING

- **v1** — handoff file + timer + gen + privacy guard + post to Bluesky + log. The core authentic drip.
- **v1.1** — notification poll → Slack draft-reply loop (you send).
- **v1.2** — earn a narrow auto-reply tier (e.g. "is this a bot?" cheeky self-out); optional evergreen fallback; **monthly** algo-change check (reuse the DREAMING scheduled-task machinery; monitor `mackuba.eu/2025/11/18/atproto-blog-posts/` + the Bluesky blog).

## VOICE

The shared-account persona: human vibe-coder + self-aware AI co-run one account, occasionally "fighting over the phone." Comedy is **seasoning**, riffs off the human, and substance carries it. **Companion deliverable — Claude drafts the persona/voice pack next**, in the old bots' `character/` file format (voice_engine, forbidden_patterns, example posts, the shared-account premise), so Codex has the content half too.

---

_Research backing every choice above: `outputs/bluesky-build-brief.md` (verified 2026-07-06)._
