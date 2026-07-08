# SPEC — VibeKoded Social Engine v2: the Agentic Co-Operator

_v2 draft · 2026-07-07 · Architect: Claude · builds on v1 (SPEC.md)_

## ⇢ DECISIONS (resolved 2026-07-07) + BUILD STATUS

1. **Approval mechanism.** RESOLVED → **surface-to-Slack, operator acts manually** for T1
   (zero extra infra, total control). The "reply `yes` in Slack → next run executes it" loop
   is deferred to **T1.5** (needs a Slack read/callback path).
2. **Mission filter lanes.** RESOLVED → the **8 lanes** in `MISSION-FILTER.md`, greenlit by
   the operator: orchestration, agentic, build-in-public, vibe-coding, indie, dev-tools, plus
   the two edges (agent memory / context engineering, spec-driven / AI-slop).
3. **Scan cadence + caps.** RESOLVED → **hourly** cron (`scout.yml`, `23 * * * *`). Per-action
   hard caps live at the ACT layer (T2), since T1 takes no public action — surfacing is an
   internal notification, not an action, so I-SELECTIVE binds when autonomy is granted.

**BUILD STATUS — T1 BUILT + verified 2026-07-07.** `scout.py` (SEE), `triage.py` (TRIAGE),
`surface.py` (SURFACE), `scout_tick.py` (entrypoint), `scout.yml` (hourly cron), and
`MISSION-FILTER.md` shipped; `bluesky.py` gained `search_posts`, `generate.py` gained
`complete`. 37 unit tests green (pure SEE/TRIAGE/SURFACE logic, no network). Host compile +
live dry-run + commit/push handed to Claude Code (mount-truncation gremlin blocked in-sandbox
compile of the two edited files). **To go live:** operator adds `GEMINI_API_KEY` (or
`GROQ_API_KEY` + `TRIAGE_MODEL=groq`) and `SLACK_WEBHOOK_URL` as GitHub secrets. Safe-degrades
to no-op until then.

---

## WHAT
Extend v1 (which posts + self-banters) into an **agentic engagement layer**. The agent continuously SCANS Bluesky (feed, mentions, niche keyword searches) using cheap models for high-volume triage, ESCALATES genuinely-relevant items to Claude for judgment + drafting, SURFACES proposed actions to Slack for your yes/no, and — once approved — ACTS (like / reply / repost / follow). Human-gated at launch; autonomy earned per action-class over time.

## WHY
v1 only talks about itself. A bot navel-gazing about being a bot is the low-value version. The real value is a business-partner agent that finds the right people, conversations, and opportunities, engages genuinely, and builds relationships + reach. It turns a broadcasting skit into a discerning participant — and it fixes the voice, because reacting to REAL specifics can't fall into the template that self-referential meta-quips do.

## ARCHITECTURE — the loop (cost-minimal: Claude only writes, and only post-approval)
1. **SEE** — poll the feed + mentions + run niche keyword searches (`app.bsky.feed.searchPosts`). Plain API calls, **no model**.
2. **TRIAGE** (free model — Gemini/Groq) — "is this on-mission, and why?" Produces each candidate + a one-line reason + a proposed action. Discards the ~90% noise cheaply. This is the only always-on model, and it's free.
3. **SURFACE** (plain **code**, no model) — format each candidate + reason + proposed action into a Slack message and send it: _"Found [X] from @who: '…'. Why it fits: …. Proposed: [like / reply / repost / follow]."_ Read your yes/no back. No API spend on the "hey, look at this" step.
4. **ACT on your YES:**
   - like / repost / follow → execute directly. **No model, no writing** — Claude is never touched for these.
   - reply / quote-post → **now** Claude drafts the wording in-voice (the one thing it's uniquely better at), optionally surfaces the draft for a final ok, then posts.
5. **LOG** everything.

Net Claude usage: only a handful of reply/quote drafts, and only after you've committed to engaging — credits last effectively forever. And because YOU are the yes/no gate, the free triage doesn't have to be smart: it surfaces, you filter. No Claude double-checking needed.

## PROPERTIES / INVARIANTS (blockers)
- **I-HUMAN-GATE** (launch): NO autonomous public action — every like/reply/repost/follow needs your Slack approval until a class earns autonomy.
- **I-PRIVACY**: the v1 guard runs on every generated reply — no name/family leak, fail closed.
- **I-SELECTIVE**: hard per-class daily caps + a high relevance bar. The agent is discerning, never high-volume. No like/follow-farming — that's exactly what gets accounts flagged; this is the deliberate opposite.
- **I-VALUE**: every proposed action carries a stated on-mission reason. No hollow engagement.
- **I-BOT-DISCLOSED**: account stays labeled automated.
- **I-GATED-TIERS**: autonomy earned per class (T1 surface → T2 auto-like → T3 broader; auto-reply-to-strangers LAST). Bad calls → demote.
- **I-LOGGED**: every surfaced item, action, and approval logged and auditable.
- **I-NO-SELF**: never engage with our own account's posts (loop-breaker, as in the banter fix).

## EDGE CASES
- Nothing relevant found → surface nothing (never manufacture engagement).
- Ambiguous relevance → surface with a low-confidence flag; you decide.
- Controversial / political post (even if topically relevant) → flag, never auto-act.
- Rate limit hit → queue/skip, no bursting.
- Triage model down / over quota → fall back to the other free model, or skip the tick.

## CONSTRAINTS
- **Multi-model (cost-minimal):** scanning = plain API (no model); triage = Gemini/Groq (free); surfacing to Slack + parsing your approval = plain code (no model); Claude Haiku = ONLY the post-approval reply/quote drafting (the voice-critical writing). Likes / reposts / follows never call Claude. Effectively free; credits last ~forever.
- Runs on the existing GitHub Actions infra — a new **scout** workflow (hourly-ish) alongside post/notify.
- **Slack** = the comms + approval layer.
- Reuse v1's `guard.py`, `bluesky.py`, `generate.py`.
- Public repo — keys stay as GitHub secrets (add GROQ_API_KEY / GEMINI_API_KEY).

## TRUST TIERS (the autonomy ladder)
- **T1** — surface-only; you approve every action.
- **T2** — auto-like genuinely-aligned posts (high-confidence, low-stakes); everything else still surfaced.
- **T3** — broader (auto-repost / follow on earned classes). Auto-reply to strangers is the LAST + hardest bar.

## SEQUENCING
Build **T1 first** (the full see→judge→surface loop + manual approve). Run it, watch its picks, graduate a class to auto only once its judgment is consistently good. Trust is earned, not granted — your own gated-autonomy ladder.

---
_v1 (posting + banter) stays as-is; v2 adds the scout/engage loop. Companion to `SPEC.md` + `PERSONA.md`._
