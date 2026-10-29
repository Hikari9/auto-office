"""The production router follows mutable preference data and stable tie rules."""
from office import executor_slates, routing
import copy


def _seed(model):
    return [{"model_id": model, "effort": "low", "harness": "fake"}]


def test_preference_mutation_changes_the_runtime_winner_without_test_edits(synthetic_candidate, route_contract):
    a, b = synthetic_candidate("fixture-a"), synthetic_candidate("fixture-b")
    first = route_contract([a, b], seed=_seed("fixture-a"))
    second = route_contract([a, b], seed=_seed("fixture-b"))
    assert first["status"] == second["status"] == "selected"
    assert first["selected"].endswith("fixture-a@low")
    assert second["selected"].endswith("fixture-b@low")


def test_reversing_only_preference_data_reverses_selection(synthetic_candidate, route_contract):
    rows = [synthetic_candidate("fixture-a"), synthetic_candidate("fixture-b")]
    actual = [route_contract(rows, seed=_seed(model))["selected"] for model in ("fixture-a", "fixture-b")]
    assert actual[0].endswith("fixture-a@low") and actual[1].endswith("fixture-b@low")


def test_input_permutation_preserves_the_unique_preferred_winner(synthetic_candidate, route_contract):
    a, b = synthetic_candidate("fixture-a"), synthetic_candidate("fixture-b")
    left = route_contract([a, b], seed=_seed("fixture-b"))["selected"]
    right = route_contract([b, a], seed=_seed("fixture-b"))["selected"]
    assert left == right and left.endswith("fixture-b@low")


def test_equal_candidates_use_stable_tie_breaking_after_input_permutation(synthetic_candidate, route_contract):
    a, b = synthetic_candidate("fixture-a"), synthetic_candidate("fixture-b")
    left = route_contract([a, b])["selected"]
    right = route_contract([b, a])["selected"]
    assert left == right and left.endswith("fixture-a@low")


def test_ineligible_route_cannot_change_a_valid_winner(synthetic_candidate, route_contract):
    eligible = synthetic_candidate("fixture-a")
    ineligible = synthetic_candidate("fixture-c", capabilities=["builder"])
    baseline = route_contract([eligible], seed=_seed("fixture-a"))["selected"]
    changed = route_contract([ineligible, eligible], seed=_seed("fixture-a"))["selected"]
    assert baseline == changed and changed.endswith("fixture-a@low")


def test_required_capabilities_are_checked_before_selection(synthetic_candidate, route_contract):
    result = route_contract([synthetic_candidate(capabilities=["builder"])])
    assert result["selected"] is None
    assert any("missing capabilities" in row["reason"] for row in result["rejected"])


def test_role_deny_is_reported_as_an_explicit_rejection(synthetic_candidate, route_contract, user_policy):
    policy = user_policy({"role": "code_reviewer", "route": "fake/fixture-a@low", "state": "deny"})
    result = route_contract([synthetic_candidate()], policies=[policy])
    assert result["selected"] is None
    assert any(row.get("category") == "role-deny" for row in result["rejected"])


def test_missing_historical_trust_does_not_veto_neutral_route(monkeypatch, synthetic_candidate, route_contract):
    from office import scoring
    monkeypatch.setattr(scoring, "evaluate_trust_state",
                        lambda *_: (_ for _ in ()).throw(AssertionError("legacy trust read")))
    assert route_contract([synthetic_candidate("not-yet-listed")])["status"] == "selected"


def test_quota_ordering_selects_safe_fallback(synthetic_candidate, route_contract):
    result = route_contract([synthetic_candidate("fixture-a", quota=0), synthetic_candidate("fixture-b", quota=80)])
    assert result["status"] == "selected" and result["selected"].endswith("fixture-b@low")


def test_preference_cannot_make_a_quota_unsafe_candidate_eligible(synthetic_candidate, route_contract):
    preferred_but_unsafe = synthetic_candidate("fixture-a", quota=0)
    available = synthetic_candidate("fixture-b")
    result = route_contract([preferred_but_unsafe, available], seed=_seed("fixture-a"))
    assert result["status"] == "selected" and result["selected"].endswith("fixture-b@low")


def test_changing_the_configured_floor_changes_only_that_candidate_gate(synthetic_candidate):
    policy = copy.deepcopy(executor_slates.POLICY)
    policy["floors"] = {**policy["floors"], "M": 56}
    other_size = "S"
    other_score = policy["floors"][other_size]
    other_candidate = synthetic_candidate(score=other_score)
    other_candidate["benchmark_indexes"] = {policy["benchmark_index"]: other_score}
    assert executor_slates.benchmark_gate(other_candidate, other_size, policy) is None
    candidate = synthetic_candidate(score=55)
    candidate["benchmark_indexes"] = {policy["benchmark_index"]: 55}
    assert executor_slates.benchmark_gate(candidate, "M", policy) is not None
    policy["floors"] = {**policy["floors"], "M": 54}
    assert executor_slates.benchmark_gate(candidate, "M", policy) is None
    assert executor_slates.benchmark_gate(other_candidate, other_size, policy) is None
