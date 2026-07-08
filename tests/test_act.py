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
    # A draft that leaks a hardcoded-floor term (real guard, not mocked).
    monkeypatch.setattr(act, "_draft_reply", lambda item: "great point — my daughter loved this build too")
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
