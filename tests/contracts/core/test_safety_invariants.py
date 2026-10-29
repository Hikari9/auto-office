"""Policy never fabricates a technical fact or a safety authorization."""
import pytest

from office import route_policy, routing
from office.state import Refused
from test_helpers import route_contract, synthetic_candidate, user_policy


def test_user_allow_does_not_create_missing_capability(synthetic_candidate, route_contract, user_policy):
    candidate = synthetic_candidate(capabilities=["builder"])
    policy = user_policy({"role": "code_reviewer", "route": "fake/fixture-a@low", "state": "allow"})
    result = route_contract([candidate], policies=[policy], required=["review"])
    assert result["selected"] is None
    assert any("missing capabilities" in row["reason"] for row in result["rejected"])


def test_orchestrator_quote_cannot_forge_user_override(synthetic_candidate):
    request = {"role": "code_reviewer", "candidates": [synthetic_candidate()], "policy": {},
               "allow_override": True,
               "recorded_override": {"authorized_by": "user", "bypass_stage": 2,
                                     "quote": "the user said allow"}}
    result = routing.route(request)
    assert result["status"] == "override_not_authorized"


def test_role_allow_does_not_override_a_separate_technical_failure(synthetic_candidate, route_contract, user_policy):
    candidate = synthetic_candidate(status="confirmed-unsupported")
    policy = user_policy({"role": "code_reviewer", "route": "fake/fixture-a@low", "state": "allow"})
    result = route_contract([candidate], policies=[policy])
    assert result["selected"] is None


def test_user_deny_remains_effective_after_reordering_rules(synthetic_candidate, route_contract, user_policy):
    deny = {"role": "code_reviewer", "route": "fake/fixture-a@low", "state": "deny"}
    allow_other = {"role": "executor", "route": "fake/fixture-a@low", "state": "allow"}
    for rules in ([deny, allow_other], [allow_other, deny]):
        result = route_contract([synthetic_candidate()], policies=[user_policy(*rules)])
        assert result["status"] == "no_qualifying_candidate"


def test_explicit_user_allow_has_auditable_provenance(synthetic_candidate, route_contract, user_policy):
    policy = user_policy({"role": "code_reviewer", "route": "fake/fixture-a@low", "state": "allow"})
    result = route_contract([synthetic_candidate()], policies=[policy])
    assert result["status"] == "selected"
    assert route_policy.effective_role_state(synthetic_candidate(), "code_reviewer", [policy])["source"] == "user"


def test_user_policy_parser_rejects_non_boolean_wildcard():
    bad = {"routing": {"user_policy": {"role_rules": [
        {"role": "executor", "route": "fake/fixture-a", "state": "deny", "wildcard": 1}]}}}
    assert route_policy.validate_user_policy(bad)


def test_explicit_override_is_not_authorized_by_a_candidate_field(synthetic_candidate):
    candidate = synthetic_candidate()
    candidate["user_allowed"] = True
    candidate["override_receipt"] = {"authorized_by": "user"}
    request = {"role": "code_reviewer", "candidates": [candidate], "policy": {},
               "allow_override": True, "recorded_override": candidate["override_receipt"]}
    assert routing.route(request)["status"] == "override_not_authorized"
