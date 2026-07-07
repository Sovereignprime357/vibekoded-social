# MISSION FILTER — what the scout surfaces (and what it never touches)

_v1 draft · 2026-07-07 · your review expected. This is the single source of truth for the
scout: the `LANES` block below feeds the SEE step's searches, and the prose rubric feeds
the TRIAGE model's judgment. Edit this file and both change — no second copy anywhere._

---

## THE JOB

Find the people and conversations where VibeKoded belongs, so the account engages
genuinely and earns attention toward vibekoded.com. **Discernment over volume.** Every
surfaced item carries a one-line reason. Target ~5–10 surfaced a day, not a firehose. If
nothing's worth it, surface nothing — we never manufacture engagement.

---

## LANES (what the scout searches for)

A post is a candidate if it fits ANY lane AND clears the on-mission test below. Each lane
is a set of search terms + optional hashtags the SEE step queries (English, recent-first).

```json
{
  "lanes": [
    {"id": "orchestration", "label": "AI orchestration / multi-agent",
     "terms": ["AI orchestration", "orchestrating agents", "multi-agent", "agent swarm", "fleet of agents"],
     "tags": []},
    {"id": "agentic", "label": "Agentic / autonomous agents",
     "terms": ["agentic AI", "autonomous agent", "AI agent that", "agent that ships"],
     "tags": ["agenticai"]},
    {"id": "buildinpublic", "label": "Build-in-public",
     "terms": ["building in public", "shipped today", "build log", "ship log"],
     "tags": ["buildinpublic"]},
    {"id": "vibecoding", "label": "Vibe coding / AI-assisted dev",
     "terms": ["vibe coding", "AI wrote the code", "don't write code", "orchestrate not code"],
     "tags": ["vibecoding"]},
    {"id": "indie", "label": "Indie hackers / solo founders",
     "terms": ["indie hacker", "solo founder", "bootstrapped", "shipping solo"],
     "tags": ["indiehackers"]},
    {"id": "devtools", "label": "AI dev tooling (experience, not vendor wars)",
     "terms": ["coding agent", "AI coding", "AI dev tool", "agent in my workflow"],
     "tags": []},
    {"id": "memory", "label": "Agent memory / context engineering (our edge)",
     "terms": ["agent memory", "agents forget", "memory for agents", "context engineering", "persistent context"],
     "tags": []},
    {"id": "spec", "label": "Spec-driven / AI-slop prevention (our edge)",
     "terms": ["spec-driven", "AI slop", "invariants", "spec before code", "AI code quality"],
     "tags": []}
  ]
}
```

---

## ON-MISSION TEST (all must hold to surface)

- A **real builder or person**, not a brand blasting ads and not a pure repost bot.
- A genuine **question, insight, build-update, or struggle** we can actually add to — not a
  bare link-drop with no substance.
- We have something **real to contribute**: a receipt, a transferable pattern, or an honest
  question back. If the best we've got is "nice!", that's a like at most, never a reply.
- **Recent** (inside the scan window) and active enough to be worth entering.
- **English** for now (`lang=en`).

---

## HARD NO — never surface, never engage

- **Politics, culture-war, anything divisive** — even if topically adjacent. Flag and drop.
- **Drama, pile-ons, dunking, quote-dunk bait.** We don't play the outrage economy.
- **Vendor flame wars** (model X vs model Y). We're role-neutral; we never take a side.
- **Shill/spam:** crypto, get-rich, growth-hacking, follow-for-follow, "🚀 link below 👇".
- **NSFW, harassment, or anything that'd embarrass the brand sitting next to it.**
- **Anyone we've engaged in the last few days** — no pestering (dedup handles this).
- **Our own account** (I-NO-SELF).
- Anything a drafted reply would trip the **privacy guard** on (I-PRIVACY).

Controversial-but-relevant is not a maybe — it's a **flag-and-drop**, or at most surface with
a loud low-confidence warning. Never auto-act on it.

---

## PROPOSED ACTION (the scout suggests; you decide)

The scout proposes one of four. The order below is also the trust ladder — earlier ones earn
autonomy first, replies to strangers are the last and hardest bar.

- **LIKE** — solid, on-mission, but we've nothing specific to add. A low-stakes nod. *(First
  class to earn auto.)*
- **REPLY** — we have a real, specific contribution: a receipt, a pattern, a genuine question.
  The highest-value action and the relationship-builder. *(Human-gated longest.)*
- **REPOST** — genuinely great and on-mission; our audience should see it. Rare — we are not
  a repost firehose.
- **FOLLOW** — a consistent on-mission builder worth keeping in the orbit. High bar, sparse.

---

## SURFACE FORMAT (what lands in Slack)

Each surfaced item: **post text · author · lane · one-line why · proposed action ·
confidence (high/med/low) · link.** Low-confidence or ambiguous still surfaces, flagged —
you're the gate, so borderline calls come to you rather than getting dropped silently.

---

## FEW-SHOT (teaches the triage model the line)

```
SURFACE (reply, high):
  @dev: "how's everyone handling agents losing all context between sessions?"
  lane: memory · why: our exact wheelhouse — flat-file+index answer, and we can ask what
  they're running · action: reply

SURFACE (like, high):
  @builder: "shipped my first AI-orchestrated app this weekend, wild that it actually worked"
  lane: vibecoding · why: on-mission builder, but nothing specific to add past a nod ·
  action: like

SURFACE (follow, med):
  @maker posts daily build-in-public updates on their own agent framework
  lane: buildinpublic · why: consistent on-mission builder worth the orbit · action: follow

DROP:
  @acct: "AI is going to replace all programmers, cope harder"
  why: culture-war bait, off-brand, drama · action: none

DROP:
  @brand: "🚀 Supercharge your workflow with our AI tool! Link below 👇"
  why: ad blast, not a real builder, nothing to engage · action: none

DROP:
  "hot take: [model A] is garbage, [model B] mogs it, cope"
  why: vendor flame war, we're role-neutral · action: none
```

---

_Companion to `PERSONA.md` (voice) and `SPEC-v2.md` (the loop). The scout reads `LANES` for
searches and this rubric for judgment; you edit here, the behavior follows._
