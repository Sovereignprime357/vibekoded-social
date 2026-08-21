# MISSION FILTER — what the miner harvests (and what it never touches)

_v2 · rewritten 2026-08-21 under SPEC-vibekoded-social-miner (BRAIN2.0/blueprints/). The
broadcast half of this system is retired on the operator's 2026-08-21 ruling; this account
mines. This file is still the single source of truth: the `LANES` block feeds the SEE step's
searches, and the prose bar below feeds the ops-insight lens and the weekly distill. Edit
here and the behavior follows — no second copy anywhere._

_v1 (the engagement-era filter) is preserved in git history at tag-commit `dcfbf21` and
earlier — nothing deleted._

---

## THE JOB

Mine Bluesky for the best of the best material on **agent memory, context engineering, and
agentic workflow / agent architecture** — named mechanisms, primary research, real
engineering writeups — and harvest it into `ops-intel-log.jsonl` for the weekly distill.
The customer is the operator's own system (BRAIN2.0), not an audience.

**This account does not engage.** No posts, no replies, no likes, no reposts. The only
outward action that remains is the capped frontier auto-follow (curation, not conversation).
Discernment over volume: the lens's failure mode is over-flagging. **A tick that harvests
nothing is a pass, not a failure.**

---

## THE SOURCE DOCTRINE (tiers, not firehose)

The mine is the **source list**, not the keyword firehose. Measured 2026-08-21: the top 25
brief producers in the log were ALL aggregator/content-marketing bots; zero were watchlist
handles. So the weighting is inverted:

1. **Tier 1 — the frontier watchlist** (`frontier-watchlist.json`, every handle verified
   live): the primary lode. Watchlist authors get extract PRIORITY in the lens.
2. **Tier 2 — the lanes below, demoted to discovery:** lane hits from unknown authors face
   the same bar but mainly serve to NOMINATE new watchlist candidates — an unknown author
   who keeps clearing the bar is a watchlist candidate for the weekly distill to propose.
3. **The off-platform lode (Phase 2, not fetched yet):** much of the canon is not on
   Bluesky at all — see the SOURCE MAP at the bottom. The weekly distill may propose
   promoting one of those to a fetched source once Phase 1 shows its survivor rate.

---

## LANES (what the scout searches for)

Four mission lanes only — the engagement-era lanes (buildinpublic, indie, devtools,
vibecoding) are retired. Each lane is a set of search terms the SEE step queries (English,
recent-first).

```json
{
  "lanes": [
    {"id": "memory", "label": "Agent memory / context engineering (the core lane)",
     "terms": ["agent memory", "memory architecture", "context engineering", "context rot", "memory consolidation", "temporal knowledge graph"],
     "tags": []},
    {"id": "agentic", "label": "Agent architecture / harnesses / long-horizon agents",
     "terms": ["agent harness", "coding agent", "agent architecture", "long-horizon agent", "agent evals"],
     "tags": []},
    {"id": "orchestration", "label": "Orchestration / multi-agent / context isolation",
     "terms": ["multi-agent", "AI orchestration", "subagent", "agent orchestration", "context isolation"],
     "tags": []},
    {"id": "spec", "label": "Spec-driven / evals / verification (our discipline)",
     "terms": ["spec-driven", "spec before code", "invariants", "eval harness", "verification loop"],
     "tags": []}
  ]
}
```

---

## THE BAR (all four must hold — counters and traceability, not vibes)

A post is harvest-worthy ONLY if it would survive this. The lens applies 1 and 3 at flag
time; the weekly distill applies all four before anything becomes a proposal:

1. **Mechanism named.** It says or links HOW, not that. An outcome claim with no mechanism
   is dropped.
2. **Maps to a named file or loop in the operator's system.** (Distill-time test — the lens
   records; the distill targets.)
3. **Checkable from outside.** Primary source, reproducible claim, or named evidence. An
   unsourced percentage ("agents lose 83% of context") is a drop ON SIGHT — that exact
   pattern is how content-marketing bots write.
4. **New here.** Already-known or already-ruled-on is dropped silently.

---

## HARD NO — never harvest, never surface

- **Aggregator, news-roundup, link-drop, and trend-bot accounts** — the measured noise
  floor of this system. A real practitioner writes their own mechanism; a bot recycles
  someone else's summary.
- **Content marketing and product announcements** (a technical writeup by the builder of
  the thing is fine; a promo thread is not).
- **Unsourced statistics** used as hooks.
- **Politics, culture-war, drama, dunking, vendor flame wars** — unchanged from v1.
- **Shill/spam: crypto, get-rich, growth-hacking.**
- **NSFW, harassment, anything that would embarrass the brand.**
- **Our own account** (I-NO-SELF).
- Anything the **privacy guard** trips on (I-PRIVACY, fail-closed).

---

## WHAT THE MINER PRODUCES

- **Briefs** → `ops-intel-log.jsonl` (append-only, deduped, provenance-carrying: author +
  permalink or it is not logged). Slack ops-intel/frontier channels remain OPTIONAL raw
  taps; nothing depends on them being read.
- **Weekly distill** (Sunday, on the operator's PC) fetches the week's briefs, applies the
  four-test bar, and writes 0-3 PROPOSED improvement blocks into the brain + one pointer
  line on the morning board. Zero survivors is a stated pass. >3 survivors/week means the
  filter is broken — fix the filter, never widen the operator's attention.

---

## SOURCE MAP — the off-platform lode (Phase 2; do NOT build fetchers yet)

Verified 2026-08-21. The best mechanism material mostly is NOT on Bluesky; these are the
canonical homes, kept here so the distill can nominate one for promotion once Phase 1 shows
a survivor rate:

- **Letta blog** — letta.com/blog (memory models, sleep-time compute, git-based context
  repositories; ~monthly). Founders' Bluesky handles ARE watchlisted.
- **Anthropic engineering** — anthropic.com/engineering (context engineering canon;
  irregular, every post load-bearing).
- **Chroma research** — research.trychroma.com (context rot + replication toolkit).
- **arXiv** — "agent memory" / "context engineering" in cs.CL, cs.AI (the 2026 wave:
  hierarchical/graph agentic memory, memory contamination, forgetting studies).
- **rlancemartin.github.io** (Lance Martin — agent design patterns, context engineering
  series) · **lilianweng.github.io** (canonical surveys, slow) · **hamel.dev** + evals.info
  (evals) · **eugeneyan.com** (applied patterns) · **philschmid.de** (context engineering
  series) · **cognition.com/blog** + **manus.im/blog** (the production context-engineering
  school) · **blog.cloudflare.com** (Code Mode / agents series) · **registerspill** (Thorsten
  Ball, weekly agent internals) · **latent.space** (swyx; curation with substance).

These people's Bluesky accounts are mostly dead (verified) — do not re-add handles for
them; their blogs are the channel.

---

_Companion to `SPEC-vibekoded-social-miner.md` (BRAIN2.0, the governing spec), `PERSONA.md`
(historical — the broadcast voice, dormant), and `SPEC-v2.md` (the original loop). The scout
reads `LANES` for searches; the ops-insight lens and weekly distill read the bar above._
