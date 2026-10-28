"""#837 controlling role policy, superseding historic adapter-trust admission."""
import pytest
from office import config, route_policy, routing


def candidate(model="claude-opus-5-5", effort="low", harness="claude", capabilities=None, quota=80):
    return {"harness": harness, "harness_version": "2.1.311" if harness == "claude" else "0.157.1",
            "model_id": model, "invocation_model_id": model, "effort": effort,
            "capabilities": capabilities if capabilities is not None else ["review", "builder"],
            "quota": {"status": "ok", "tightest_remaining_percent": quota},
            "cost": {"money_estimate": 1, "quota_burn": 1, "wall_clock_seconds": 1},
            "route_status": "available"}


def user_rules(*rules):
    return {"role_rules": list(rules), "denied": [], "overkill": [], "ceilings": {}, "sources": {"role_rules": "user"}}


def rule(role, route, state, **kw):
    return {"role": role, "route": route, "state": state, **kw}


def route(role, rows, *policies, seed=None, required=None, strict=False):
    return routing.route({"role": role, "candidates": rows,
        "preferred_seed": seed, "policy": {"floor": None, "strict_seed": strict,
            "required_capabilities": required or ["review"]},
        "user_policies": list(policies), "runs_db": None})


def test_shipped_known_allow_and_new_default_neutral_are_separate_from_execution():
    assert route_policy.effective_role_state(candidate(), "code_reviewer", [])["state"] == "allow"
    assert route_policy.effective_role_state(candidate(), "executor", [])["state"] == "neutral"
    assert route_policy.effective_role_state(candidate("deepseek-v4.1"), "code_reviewer", [])["state"] == "neutral"


def test_user_deny_is_effort_exact_and_role_specific():
    pol = user_rules(rule("executor", "claude/claude-opus-5-5@low", "deny"),
                     rule("code_reviewer", "claude/claude-opus-5-5@low", "allow"),
                     rule("code_reviewer", "claude/claude-opus-5-5@high", "deny"))
    assert route("executor", [candidate()], pol)["status"] == "no_qualifying_candidate"
    assert route("code_reviewer", [candidate()], pol)["status"] == "selected"
    assert route("code_reviewer", [candidate(effort="high")], pol)["status"] == "no_qualifying_candidate"
    # #850: the shipped policy denies Opus above low as a reviewer, and the user's allow at low does not widen it.
    assert route("code_reviewer", [candidate(effort="medium")], pol)["status"] == "no_qualifying_candidate"


def test_neutral_role_can_run_without_legacy_proven_approval(monkeypatch):
    from office import scoring
    monkeypatch.setattr(scoring, "evaluate_trust_state", lambda *_: (_ for _ in ()).throw(AssertionError("legacy trust read")))
    result = route("code_reviewer", [candidate("deepseek-v4.1")])
    assert result["status"] == "selected", result


def test_strict_seed_without_user_authority_does_not_block_neutral_fallback():
    seed = [{"model_id": "gpt-6-luna", "effort": "high"}]
    result = route("code_reviewer", [candidate("deepseek-v4.1")], seed=seed, strict=True)
    assert result["status"] == "selected", result


def test_quota_exhaustion_affects_availability_not_policy_and_fallback_advances():
    rows = [candidate("gpt-6-luna", "high", "codex", quota=1), candidate("deepseek-v4.1", quota=80)]
    result = route("code_reviewer", rows)
    assert result["status"] == "selected" and result["selected"].endswith("deepseek-v4.1@low")
    assert any("quota" in r["reason"] for r in result["rejected"])
    assert route_policy.effective_role_state(rows[0], "code_reviewer", [])["state"] != "deny"


def test_role_deny_does_not_bypass_required_capabilities():
    pol = user_rules(rule("code_reviewer", "claude/claude-opus-5-5@low", "allow"))
    result = route("code_reviewer", [candidate(capabilities=["builder"])], pol)
    assert result["selected"] is None
    assert any("missing capabilities" in r["reason"] for r in result["rejected"])


def test_user_role_allow_supersedes_broad_legacy_deny():
    pol = user_rules(rule("code_reviewer", "claude/claude-opus-5-5@low", "allow"))
    pol["denied"] = ["harness:claude"]
    assert route("code_reviewer", [candidate()], pol)["status"] == "selected"
    assert route("executor", [candidate()], pol)["status"] == "no_qualifying_candidate"


def test_unlisted_or_explicit_neutral_is_not_a_deny():
    pol = user_rules(rule("code_reviewer", "claude/claude-opus-5-5@low", "neutral"))
    assert route_policy.effective_role_state(candidate(), "code_reviewer", [pol])["state"] == "neutral"
    assert route("code_reviewer", [candidate()], pol)["status"] == "selected"


@pytest.mark.parametrize("bad", [
    {"role": "code_reviewer", "route": "claude/opus", "state": "deny"},
    {"role": "code_reviewer", "route": "claude/opus@low", "state": "bad"},
    {"role": "unlisted", "route": "claude/opus@low", "state": "deny"},
    {"role": "*", "route": "claude/opus@low", "state": "deny"},
    {"role": "code_reviewer", "route": None, "state": "deny"},
])
def test_invalid_rule_fails_validation(bad):
    assert route_policy.validate_user_policy({"routing": {"user_policy": {"role_rules": [bad]}}})


def test_explicit_wildcard_is_allowed_and_an_exact_override_has_priority():
    pol = user_rules(rule("code_reviewer", "claude/claude-opus-5-5", "deny", wildcard=True),
                     rule("code_reviewer", "claude/claude-opus-5-5@low", "allow"))
    assert route_policy.effective_role_state(candidate(), "code_reviewer", [pol])["state"] == "allow"
    assert route_policy.effective_role_state(candidate(effort="high"), "code_reviewer", [pol])["state"] == "deny"


def test_repository_cannot_silently_mutate_role_policy():
    with pytest.raises(ValueError, match="repository routing.user_policy.role_rules"):
        config.resolve(None, files={"user": None,
            "repo": "routing:\n  user_policy:\n    role_rules:\n      - {role: code_reviewer, route: claude/opus@high, state: allow}\n"})


def test_legacy_denial_stays_denied_until_explicit_user_allow():
    old = {"denied": ["harness:claude"], "ceilings": {}, "overkill": [], "sources": {"denied_models": "user"}}
    assert route_policy.effective_role_state(candidate(), "code_reviewer", [old])["state"] == "deny"


def test_fake_override_without_db_authorization_fails_closed():
    c = candidate()
    req = {"role": "code_reviewer", "candidates": [c], "policy": {},
           "allow_override": True, "recorded_override": {"authorized_by": "user", "bypass_stage": 2}}
    res = routing.route(req)
    assert res["status"] == "override_not_authorized"


def test_run_set_cannot_impersonate_global_user_policy():
    with pytest.raises(ValueError, match="user config"):
        config.resolve(None, sets=["routing.user_policy.role_rules=[]"], files={"user": None, "repo": None})
    with pytest.raises(ValueError, match="user config"):
        config.resolve(None, sets=["routing.user_policy.denied_models=[]"], files={"user": None, "repo": None})
