"""The production router follows mutable preference data and stable tie rules."""
import copy

from office import candidates, executor_slates, routing
from test_helpers import route_contract, synthetic_candidate, user_policy


def _seed(model):
    return [{"model_id": model, "effort": "low", "harness": "fake"}]


def _configured_preference_route(rows, preferences, policy, route_contract):
    policy["executor_preferred"]["M"] = preferences
    seed = candidates._preferred_seed({}, {"gear": "M"}, "M", executor_policy=policy)
    assert seed == preferences
    return route_contract(rows, role="worker", required=["builder"], seed=seed,
                          context={"gear": "M", "size_class": "M"}, executor_policy=policy)


def test_preference_mutation_changes_the_runtime_winner_without_test_edits(synthetic_candidate, route_contract):
    policy = copy.deepcopy(executor_slates.POLICY)
    rows = [synthetic_candidate("fixture-a"), synthetic_candidate("fixture-b")]
    for candidate in rows:
        candidate["benchmark_indexes"] = {policy["benchmark_index"]: 55}
    first = _configured_preference_route(rows, _seed("fixture-a"), copy.deepcopy(policy), route_contract)
    second = _configured_preference_route(rows, _seed("fixture-b"), copy.deepcopy(policy), route_contract)
    assert first["status"] == second["status"] == "selected"
    assert first["selected"].endswith("fixture-a@low")
    assert second["selected"].endswith("fixture-b@low")


def test_reversing_only_preference_data_reverses_selection(synthetic_candidate, route_contract):
    policy = copy.deepcopy(executor_slates.POLICY)
    rows = [synthetic_candidate("fixture-a"), synthetic_candidate("fixture-b")]
    for candidate in rows:
        candidate["benchmark_indexes"] = {policy["benchmark_index"]: 55}
    first = _configured_preference_route(rows, [_seed("fixture-a")[0], _seed("fixture-b")[0]],
                                         copy.deepcopy(policy), route_contract)
    second = _configured_preference_route(rows, [_seed("fixture-b")[0], _seed("fixture-a")[0]],
                                          copy.deepcopy(policy), route_contract)
    assert first["selected"].endswith("fixture-a@low")
    assert second["selected"].endswith("fixture-b@low")


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


def test_changing_the_configured_floor_changes_only_that_candidate_gate(synthetic_candidate, route_contract):
    policy = copy.deepcopy(executor_slates.POLICY)
    policy["floors"] = {**policy["floors"], "M": 56}
    other_candidate = synthetic_candidate("other-size", score=policy["floors"]["S"])
    other_candidate["benchmark_indexes"] = {policy["benchmark_index"]: policy["floors"]["S"]}
    candidate = synthetic_candidate("target-size", score=55)
    candidate["benchmark_indexes"] = {policy["benchmark_index"]: 55}
    target = route_contract([candidate], role="worker", required=["builder"],
                            context={"gear": "M", "size_class": "M"}, executor_policy=policy)
    other = route_contract([other_candidate], role="worker", required=["builder"],
                           context={"gear": "S", "size_class": "S"}, executor_policy=policy)
    assert target["status"] == "no_qualifying_candidate"
    assert any(row.get("category") == "subtask-intelligence-floor" for row in target["rejected"])
    assert other["status"] == "selected"
    policy["floors"] = {**policy["floors"], "M": 54}
    lowered = route_contract([candidate], role="worker", required=["builder"],
                             context={"gear": "M", "size_class": "M"}, executor_policy=policy)
    assert lowered["status"] == "selected"
