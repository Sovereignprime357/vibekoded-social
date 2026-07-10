# SPEC — v7: Frontier Scout-Weighting (curated watchlist)
## Project: vibekoded-social
## Phase: v7 (a curated frontier watchlist: auto-follow + weight + monitoring feed)
## Last Updated: 2026-07-09
## Builds on: SPEC-v3 (autonomy ladder / follow), SPEC-v6 (review-only feeds), SPEC-v5 (heartbeat)

## WHAT
A curated **frontier watchlist** (`frontier-watchlist.json`, two tiers) of high-signal
accounts the operator wants to track closely. For those accounts the system:
1. **Auto-follows** them (resolve handle→DID; reuse the autonomous-follow with its
   daily cap + pacing + dedup + I-NO-SELF + I-LOGGED — they're pre-vetted, so they
   fill the follow budget preferentially).
2. **Weights** their posts higher in scout ranking (rank boost; study_closely > high_signal).
3. Feeds a **review-only monitoring stream** to the frontier Slack channel `C0BGH90UKM2`:
   - **STUDY-CLOSELY** tier (`0coceo.bsky.social`) → **EVERY** post (full monitoring).
   - **HIGH-SIGNAL** tier → their notable / on-mission posts.
No auto-action on the feed (review-only, like ops-intel). All existing gates preserved.

## WHY
The operator has a genre-twin (`0coceo` — an AI running a company in public) worth
studying move-for-move, and a seed set of frontier practitioners whose signal is high
enough to pre-vet. Auto-following + weighting + a dedicated monitoring channel turns a
"hope the scan catches them" into deliberate, dependable frontier awareness — without
loosening any engagement gate.

## INPUTS
- **`frontier-watchlist.json`** — `tiers.study_closely.handles[]` + `tiers.high_signal.handles[]`.
  Extensible: add handles over time; no code change.
- **Scout candidates** — already pulled by scout-tick (reuse-only for the feed + weight).
- **Env / non-secret defaults:** `SLACK_CHANNEL_FRONTIER` (default `C0BGH90UKM2`),
  `SLACK_BOT_TOKEN`, `ACT_CAP_FOLLOW` + `ACT_PACING_SECONDS` (shared follow budget/pacing).

## OUTPUTS
- **Follows** of watchlist accounts (createRecord graph.follow), logged to `act-log.jsonl`
  (shared follow budget) + deduped in `frontier-followed.jsonl`.
- **Ranking boost** on watchlist authors' items (higher surface priority for engagement
  + ops-insight).
- **Monitoring cards** posted to the frontier channel (review-only), deduped in
  `frontier-seen.jsonl`, guard-checked.
- NOTHING actioned from the feed; NO brain/memory write.

## INVARIANTS
- **I-FOLLOW-BUDGET** — watchlist auto-follows draw on the SAME daily follow cap
  (`ACT_CAP_FOLLOW`) and pacing as the existing autonomous-follow, counted from
  `act-log.jsonl`. Pre-vetted watchlist follows fill that budget preferentially. Never
  exceed the cap.
- **I-BOT-DISCLOSED (safety basis)** — autonomous follow requires the `bot` self-label
  confirmed first (fail-closed: if it can't be confirmed, no watchlist follows this tick).
- **I-NO-SELF** — never follow our own account.
- **I-DEDUP** — a watchlist account is followed at most once (`frontier-followed.jsonl`);
  a post is monitored at most once (`frontier-seen.jsonl`).
- **I-REVIEW-ONLY (feed)** — the frontier monitoring feed is FYI: no 👍 wiring, no action,
  does NOT enter the act-layer surfaced ledger. It only informs.
- **I-PRIVACY** — every frontier card is privacy-guarded (fail-closed) before posting;
  a card that trips the guard is dropped, never posted.
- **I-HUMAN-GATE (unchanged)** — weighting only changes ORDER/priority of what's surfaced
  for the operator's 👍; it grants no new action authority. Actual public engagement
  (like/reply/repost) is still human-gated exactly as before.
- **I-LOGGED** — every follow logged (act-log + frontier ledger); every monitored post
  recorded.
- **I-REUSE-ONLY (feed + weight)** — the feed and weighting reuse already-pulled scout
  candidates; only the auto-follow adds network calls (handle→DID resolve + follow),
  which are bounded by the follow cap.

## EDGE CASES
- **Handle won't resolve (renamed/deleted)** — skip it, log, move on; never crash the tick.
- **Already following / already in ledger** — skip (no re-follow, no resolve call).
- **Follow cap reached today** — stop following; remaining watchlist accounts wait for
  tomorrow's budget (preferential ordering: study_closely first).
- **Self-label unconfirmed** — no watchlist follows this tick (fail-closed); feed + weight
  still run (they take no public action).
- **Frontier card trips the guard** — dropped, marked seen, never posted.
- **Bot not in the frontier channel** — post fails; logged; marked seen (bot IS already in
  `C0BGH90UKM2` per setup).
- **Watchlist file missing/malformed** — degrade to an empty watchlist (no boost, no feed,
  no follow); never crash.
- **Frontier step errors** — wrapped so it can NEVER break the scout tick.

## OUT OF SCOPE (v7)
- Any auto-action from the frontier feed (review-only).
- Brain/memory writes (I-NO-AUTO-BRAIN from SPEC-v6 still holds).
- Unfollowing / list pruning (manual for now).
- Fetching a watchlist account's full timeline (feed uses the posts scout already pulls;
  study_closely "every post" = every one that appears in the scan stream).

## ACCEPTANCE CRITERIA
- Watchlist accounts are auto-followed within the follow cap + pacing, deduped, logged;
  our own account is never followed.
- Watchlist authors' posts rank higher (study_closely > high_signal > others), tested.
- STUDY-CLOSELY posts surface to the frontier channel regardless of mission-triage;
  HIGH-SIGNAL posts surface when notable/on-mission; both deduped + guard-checked.
- The feed takes no action and writes no brain/memory.
- All prior invariants hold; weighting changes only priority, not authority.

---
_Companion to SPEC.md + SPEC-v2…v6 + PERSONA.md. v7 makes frontier awareness deliberate:
follow the right people, weight their signal, watch the genre-twin closely — all without
loosening a gate._
