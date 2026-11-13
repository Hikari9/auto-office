"""Hard Claude Fable 5.2 planner-only gate: role allows are not authorization."""
import pytest

from office import candidates, route_policy


FABLE = "claude-fable-5-2"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROLES = ("executor", "worker", "code_reviewer", "plan_reviewer",
         "visual_reviewer", "integration_reviewer", "closeout_verifier",
         "browser_verifier")


@pytest.mark.parametrize("effort", EFFORTS)
def test_fable_is_disabled_even_for_planner_until_local_qualification(effort):
    cand = {"harness": "claude", "model_id": FABLE,
            "invocation_model_id": FABLE, "effort": effort}
    assert route_policy.fable_role_block(cand, "planner")
    assert route_policy.effective_role_state(cand, "planner", [])["state"] == "deny"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("effort", EFFORTS)
def test_fable_non_planner_roles_are_hard_denied_despite_user_allow(role, effort):
    cand = {"harness": "claude", "model_id": FABLE, "effort": effort,
            "invocation_model_id": FABLE}
    pol = route_policy.preferred_seed_policy(role, [
        {"harness": "claude", "model_id": FABLE, "effort": effort}])
    assert route_policy.effective_role_state(cand, role, [pol])["state"] == "deny"
    assert route_policy.user_allows(cand, role, [pol]) is None


@pytest.mark.parametrize("effort", EFFORTS)
def test_qualified_planner_flag_alone_does_not_authorize_executor(effort, monkeypatch):
    monkeypatch.setattr(route_policy, "FABLE_PLANNER_QUALIFIED_EFFORTS", frozenset({effort}))
    cand = {"harness": "claude", "model_id": FABLE, "effort": effort,
            "invocation_model_id": FABLE}
    assert route_policy.fable_role_block(cand, "planner") is None
    for role in ROLES:
        assert route_policy.fable_role_block(cand, role)


def test_alias_and_invocation_model_identity_cannot_escape_boundary():
    for key in ("model_id", "invocation_model_id", "alias_resolved_to"):
        cand = {key: FABLE, "effort": "high"}
        assert route_policy.fable_role_block(cand, "executor")
        assert route_policy.fable_role_block(cand, "planner")
    assert route_policy.fable_role_block(
        {"lineage_key": "anthropic/claude-fable", "effort": "high"}, "executor")


def test_catalog_registration_is_unqualified_and_not_in_executor_seeds():
    from office import shipped_catalog, executor_slates
    rows = [r for r in shipped_catalog.load()["models"] if r["model_id"] == FABLE]
    assert {r["effort"] for r in rows} == set(EFFORTS)
    assert all(r["dispatchable"] is False for r in rows)
    assert not route_policy.FABLE_PLANNER_QUALIFIED_EFFORTS
    for size in ("XS", "S", "M", "L", "XL"):
        assert FABLE not in str(executor_slates.ideal_seed(size))


def test_discovery_does_not_turn_fable_into_a_worker(monkeypatch):
    rows = [r for r in candidates.catalog_rows() if r["model_id"] == FABLE]
    assert len(rows) == 5
    for row in rows:
        assert route_policy.fable_role_block(row, "worker")
