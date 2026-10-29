"""Role policy, explicit authority and scope produce the documented outcomes."""
from office import route_policy

def rule(role, route, state, **fields):
    return {"role": role, "route": route, "state": state, **fields}


def test_executor_deny_does_not_deny_independent_reviewer(synthetic_candidate, route_contract, user_policy):
    candidate = synthetic_candidate()
    policy = user_policy(rule("executor", "fake/fixture-a@low", "deny"))
    assert route_contract([candidate], role="executor", policies=[policy])["status"] == "no_qualifying_candidate"
    assert route_contract([candidate], role="code_reviewer", policies=[policy])["status"] == "selected"


def test_reversing_role_scope_reverses_only_the_targeted_effect(synthetic_candidate, route_contract, user_policy):
    candidate = synthetic_candidate()
    policy = user_policy(rule("code_reviewer", "fake/fixture-a@low", "deny"))
    assert route_contract([candidate], role="plan_reviewer", policies=[policy])["status"] == "selected"
    assert route_contract([candidate], role="code_reviewer", policies=[policy])["status"] == "no_qualifying_candidate"


def test_deny_is_effort_exact(synthetic_candidate, route_contract, user_policy):
    policy = user_policy(rule("code_reviewer", "fake/fixture-a@low", "deny"))
    assert route_contract([synthetic_candidate(effort="low")], policies=[policy])["status"] == "no_qualifying_candidate"
    assert route_contract([synthetic_candidate(effort="high")], policies=[policy])["status"] == "selected"


def test_deny_is_size_scoped(synthetic_candidate, route_contract, user_policy):
    policy = user_policy(rule("code_reviewer", "fake/fixture-a@low", "deny", sizes=["S"]))
    assert route_contract([synthetic_candidate()], policies=[policy], context={"gear": "M", "size_class": "M"})["status"] == "selected"
    assert route_contract([synthetic_candidate()], policies=[policy], context={"gear": "S", "size_class": "S"})["status"] == "no_qualifying_candidate"


def test_explicit_wildcard_is_broader_than_exact_effort(synthetic_candidate, route_contract, user_policy):
    policy = user_policy(rule("code_reviewer", "fake/fixture-a", "deny", wildcard=True))
    assert route_contract([synthetic_candidate(effort="high")], policies=[policy])["status"] == "no_qualifying_candidate"


def test_exact_allow_overrides_a_wildcard_deny(synthetic_candidate, route_contract, user_policy):
    policy = user_policy(rule("code_reviewer", "fake/fixture-a", "deny", wildcard=True),
                         rule("code_reviewer", "fake/fixture-a@low", "allow"))
    assert route_contract([synthetic_candidate()], policies=[policy])["status"] == "selected"
    assert route_contract([synthetic_candidate(effort="high")], policies=[policy])["status"] == "no_qualifying_candidate"


def test_unlisted_candidate_remains_neutral_and_selectable(synthetic_candidate, route_contract):
    candidate = synthetic_candidate("new-synthetic-model")
    assert route_policy.effective_role_state(candidate, "code_reviewer", [])["state"] == "neutral"
    assert route_contract([candidate])["status"] == "selected"


def test_quota_failure_is_availability_not_a_policy_deny(synthetic_candidate, route_contract):
    blocked = synthetic_candidate("fixture-a", quota=0)
    available = synthetic_candidate("fixture-b")
    result = route_contract([blocked, available])
    assert result["status"] == "selected"
    assert route_policy.effective_role_state(blocked, "code_reviewer", [])["state"] != "deny"
    assert any("quota" in row["reason"] for row in result["rejected"])
