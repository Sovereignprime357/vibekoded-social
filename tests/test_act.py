"""
tests/test_act.py — the ACT layer's decision + dispatch logic (SPEC-v2 T1.5).

No network: reaction parsing, operator filter, caps, and self-check are pure;
execute_action's dispatch is tested with the Bluesky wrappers + generate
monkeypatched. The privacy guard is NOT mocked — the fail-closed reply test
exercises the REAL guard so a leak can't slip through a stubbed check.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import act  # noqa: E402


# ---------------------------------------------------------------------------
# operator_thumbsup — reaction parse + operator-only filter (I-HUMAN-GATE)
# ---------------------------------------------------------------------------

def _reactions(name="+1", users=("U_OP",)):
    return [{"name": name, "users": list(users), "count": len(users)}]


def test_thumbsup_from_operator_counts():
    assert act.operator_thumbsup(_reactions(users=("U_OP",)), operator_id="U_OP") is True


def test_thumbsup_from_someone_else_ignored_when_operator_set():
    # A thumbsup exists, but not from the operator -> not approved.
    assert act.operator_thumbsup(_reactions(users=("U_RANDO",)), operator_id="U_OP") is False


def test_thumbsup_fail_closed_when_no_operator_configured():
    # 2026-07-08 mass-fire fix: without an operator id we CANNOT verify who
    # reacted, so approval fails closed — even a real thumbsup does NOT approve.
    assert act.operator_thumbsup(_reactions(users=("U_RANDO",)), operator_id=None) is False
    assert act.operator_thumbsup(_reactions(users=("U_OP",)), operator_id=None) is False
    assert act.operator_thumbsup(_reactions(users=("U_OP",)), operator_id="") is False


def test_non_thumbsup_reaction_ignored():
    assert act.operator_thumbsup([{"name": "eyes", "users": ["U_OP"], "count": 1}], operator_id="U_OP") is False


def test_thumbsup_skin_tone_variant_counts():
    assert act.operator_thumbsup(_reactions(name="+1::skin-tone-3", users=("U_OP",)), operator_id="U_OP") is True


def test_thumbsup_alias_name_counts():
    assert act.operator_thumbsup(_reactions(name="thumbsup", users=("U_OP",)), operator_id="U_OP") is True


def test_no_reactions_is_not_approved():
    assert act.operator_thumbsup([], operator_id="U_OP") is False
    assert act.operator_thumbsup(None, operator_id="U_OP") is False


def test_malformed_reactions_fail_safe():
    # A shape it can't read must mean "not approved", never crash.
    assert act.operator_thumbsup(["not-a-dict"], operator_id="U_OP") is False


# ---------------------------------------------------------------------------
# caps (I-SELECTIVE) + self-check (I-NO-SELF)
# ---------------------------------------------------------------------------

def test_within_cap_true_below_and_false_at_limit():
    caps = {"like": 2}
    assert act.within_cap("like", {"like": 1}, caps) is True
    assert act.within_cap("like", {"like": 2}, caps) is False


def test_load_caps_env_override(monkeypatch):
    monkeypatch.setenv("ACT_CAP_LIKE", "3")
    caps = act.load_caps()
    assert caps["like"] == 3
    assert caps["reply"] == act.DEFAULT_CAPS["reply"]  # untouched keys keep defaults


def test_is_self_blocks_own_did():
    assert act.is_self({"author_did": "did:plc:us"}, "did:plc:us") is True
    assert act.is_self({"author_did": "did:plc:them"}, "did:plc:us") is False
    assert act.is_self({"author_did": "did:plc:them"}, None) is False


# ---------------------------------------------------------------------------
# execute_action — dispatch per action type
# ---------------------------------------------------------------------------

def _item(action, **over):
    base = {
        "action": action,
        "uri": "at://post/1",
        "cid": "cid1",
        "author_did": "did:plc:them",
        "text": "how do you persist agent memory across sessions?",
        "why": "our wheelhouse",
    }
    base.update(over)
    return base


def _patch_bluesky(monkeypatch):
    calls = []
    monkeypatch.setattr(act.bluesky, "like", lambda u, c, session=None: (calls.append(("like", u, c)) or {"uri": "at://like"}))
    monkeypatch.setattr(act.bluesky, "repost", lambda u, c, session=None: (calls.append(("repost", u, c)) or {"uri": "at://repost"}))
    monkeypatch.setattr(act.bluesky, "follow", lambda d, session=None: (calls.append(("follow", d)) or {"uri": "at://follow"}))
    monkeypatch.setattr(
        act.bluesky, "reply",
        lambda text, root_uri, root_cid, parent_uri, parent_cid, session=None: (
            calls.append(("reply", text, parent_uri)) or {"uri": "at://reply"}
        ),
    )
    return calls


def test_execute_like(monkeypatch):
    calls = _patch_bluesky(monkeypatch)
    res = act.execute_action(_item("like"))
    assert res["status"] == "executed"
    assert calls == [("like", "at://post/1", "cid1")]


def test_execute_repost(monkeypatch):
    calls = _patch_bluesky(monkeypatch)
    res = act.execute_action(_item("repost"))
    assert res["status"] == "executed"
    assert calls[0][0] == "repost"


def test_execute_follow_uses_author_did(monkeypatch):
    calls = _patch_bluesky(monkeypatch)
    res = act.execute_action(_item("follow"))
    assert res["status"] == "executed"
    assert calls == [("follow", "did:plc:them")]


def test_execute_like_missing_cid_errors(monkeypatch):
    _patch_bluesky(monkeypatch)
    res = act.execute_action(_item("like", cid=None))
    assert res["status"] == "error"


def test_dry_run_makes_no_write(monkeypatch):
    calls = _patch_bluesky(monkeypatch)
    res = act.execute_action(_item("like"), dry_run=True)
    assert res["status"] == "dry_run_like"
    assert calls == []  # no network write in dry-run


# ---------------------------------------------------------------------------
# execute_action reply — draft in voice, guard fail-closed (I-PRIVACY)
# ---------------------------------------------------------------------------

def test_reply_drafts_and_posts_when_clean(monkeypatch):
    calls = _patch_bluesky(monkeypatch)
    monkeypatch.setattr(act, "_draft_reply", lambda item: "we run a flat memory file plus an index. holds up well.")
    res = act.execute_action(_item("reply"))
    assert res["status"] == "executed"
    assert calls and calls[0][0] == "reply"
    assert calls[0][2] == "at://post/1"  # replied to the surfaced post


def test_reply_guard_blocks_and_posts_nothing(monkeypatch):
    """The critical safety test: a draft that trips the guard must NOT be posted."""
    calls = _patch_bluesky(monkeypatch)
    # A draft that leaks a hardcoded-floor term (real guard, not mocked). Kept
    # voice-clean (no dash, no praise-opener) so it clears the voice gate and the
    # PRIVACY guard is what blocks it.
    monkeypatch.setattr(act, "_draft_reply", lambda item: "my daughter loved this build too, honestly")
    res = act.execute_action(_item("reply"))
    assert res["status"] == "guard_blocked"
    assert res["reason"]                      # explains what was blocked
    assert calls == []                        # NOTHING posted (fail-closed)


def test_reply_empty_draft_does_not_post(monkeypatch):
    calls = _patch_bluesky(monkeypatch)
    monkeypatch.setattr(act, "_draft_reply", lambda item: "")
    res = act.execute_action(_item("reply"))
    assert res["status"] == "empty_draft"
    assert calls == []


def test_reply_dry_run_runs_guard_but_no_post(monkeypatch):
    calls = _patch_bluesky(monkeypatch)
    monkeypatch.setattr(act, "_draft_reply", lambda item: "clean in-voice reply about shipping")
    res = act.execute_action(_item("reply"), dry_run=True)
    assert res["status"] == "dry_run_reply"
    assert res["draft"]
    assert calls == []


# --- converse reply items: use pre-drafted text + explicit thread refs -------

def test_reply_uses_pre_drafted_text_and_thread_refs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        act.bluesky, "reply",
        lambda text, root_uri, root_cid, parent_uri, parent_cid, session=None: (
            calls.append((text, root_uri, parent_uri)) or {"uri": "at://reply"}
        ),
    )
    # If _draft_reply is called, that's a bug — the draft is already provided.
    monkeypatch.setattr(act, "_draft_reply", lambda item: (_ for _ in ()).throw(AssertionError("should not re-draft")))

    item = _item(
        "reply",
        source="converse",
        draft_text="we run a flat memory file plus an index.",
        parent_uri="at://them/reply", parent_cid="pcid",
        root_uri="at://root/1", root_cid="rootcid",
        uri="at://them/reply", cid="pcid",
    )
    res = act.execute_action(item)
    assert res["status"] == "executed"
    assert res["posted_text"] == "we run a flat memory file plus an index."
    # Threaded correctly: root from the item, parent = the stranger's reply.
    assert calls == [("we run a flat memory file plus an index.", "at://root/1", "at://them/reply")]


def test_reply_pre_drafted_text_still_guarded(monkeypatch):
    """Even a pre-drafted converse reply is re-guarded at act time (safety net).

    The draft is voice-clean (no dash, no praise-opener) so it clears the voice
    gate and reaches the PRIVACY guard — proving the privacy guard still fires and
    still wins on a stored draft after the voice gate was added ahead of it.
    """
    calls = _patch_bluesky(monkeypatch)
    # If the voice gate mistakenly tripped, it would regenerate via _draft_reply;
    # make that a hard failure so this test proves privacy blocked it directly.
    monkeypatch.setattr(act, "_draft_reply", lambda item: (_ for _ in ()).throw(AssertionError("should not re-draft: voice-clean")))
    item = _item("reply", source="converse", draft_text="thanks, my daughter loved it")
    res = act.execute_action(item)
    assert res["status"] == "guard_blocked"   # privacy fired and won
    assert calls == []


# --- STALE-DRAFT voice hole: the voice gate runs at the execute boundary --------
# A card can sit in Slack having been drafted BEFORE the VOICE_PROFILE secret
# existed. Thumbing it must NOT post the old sycophantic bytes verbatim: the gate
# runs here on the exact text about to hit Bluesky (voice -> privacy -> post).

def test_stale_bad_draft_regenerates_clean_and_posts_that(monkeypatch):
    """Stored draft trips the voice gate -> regenerated fresh; the CLEAN regen posts."""
    calls = _patch_bluesky(monkeypatch)
    clean = "we run a flat file plus an index. how are you handling it?"
    # _draft_reply stands in for generate() (profile-injected + gated) -> clean text.
    monkeypatch.setattr(act, "_draft_reply", lambda item: clean)
    item = _item("reply", source="converse",
                 draft_text="that's the move — keeps the data yours.")  # opener + em-dash
    res = act.execute_action(item)
    assert res["status"] == "executed"
    assert res["posted_text"] == clean                 # the regenerated text, not the stale bytes
    assert calls == [("reply", clean, "at://post/1")]  # posted exactly the clean regen


def test_stale_bad_draft_regen_drops_returns_voice_blocked(monkeypatch):
    """Stored bad draft + a model that stays sycophantic -> generate() drops -> no post."""
    calls = _patch_bluesky(monkeypatch)
    # generate() exhausted its own retries and dropped -> _draft_reply returns "".
    monkeypatch.setattr(act, "_draft_reply", lambda item: "")
    item = _item("reply", source="converse",
                 draft_text="great question — here's the thing.")
    res = act.execute_action(item)
    assert res["status"] == "voice_blocked"
    assert res["reason"] and "draft" not in res        # rule only, never the body
    assert calls == []                                 # bluesky.reply NEVER called


def test_stale_clean_draft_posts_unchanged_no_regen(monkeypatch):
    """A voice-clean stored draft posts verbatim and is NOT regenerated (no token burn)."""
    calls = _patch_bluesky(monkeypatch)
    monkeypatch.setattr(act, "_draft_reply", lambda item: (_ for _ in ()).throw(AssertionError("should not re-draft a clean stored draft")))
    clean = "shipped the memory scaffold today. what are you using?"
    item = _item("reply", source="converse", draft_text=clean)
    res = act.execute_action(item)
    assert res["status"] == "executed"
    assert res["posted_text"] == clean
    assert calls == [("reply", clean, "at://post/1")]


def test_scout_item_no_draft_text_drafts_fresh_and_gated(monkeypatch):
    """No stored draft -> fresh draft via generate() (already gated); clean -> posts."""
    calls = _patch_bluesky(monkeypatch)
    monkeypatch.setattr(act, "_draft_reply", lambda item: "curious how you shard the index. we keep it flat.")
    res = act.execute_action(_item("reply"))   # no draft_text
    assert res["status"] == "executed"
    assert calls and calls[0][0] == "reply"


def test_fresh_draft_failing_voice_gate_is_voice_blocked(monkeypatch):
    """Defense-in-depth: if a FRESH draft somehow returns dirty text, drop it (voice_blocked)."""
    calls = _patch_bluesky(monkeypatch)
    # A fresh draft that still carries an em-dash (generate's own gate bypassed here).
    monkeypatch.setattr(act, "_draft_reply", lambda item: "keeps the data yours — nice.")
    res = act.execute_action(_item("reply"))   # no draft_text -> fresh
    assert res["status"] == "voice_blocked"
    assert calls == []
