"""Shipped policy data drives the production router; no test here names a model.

Every expectation is read from the shipped policy files (`executor_slates.POLICY`,
`route_policy.SEED_ROLE_RULES`, the shipped catalog) or from a hand-written arithmetic
fact about them, never from the selector under test. Changing a floor, a slate, a
restriction, an exception or a role rule in data keeps these passing; a router that stops
following the data fails them.
"""
from __future__ import annotations

import pytest

from office import candidates, config, executor_slates as slates, route_policy, routing, shipped_catalog

POLICY = slates.POLICY
INDEX = POLICY["benchmark_index"]
SIZES = tuple(POLICY["floors"])
CLEAR = max(POLICY["floors"].values()) + 10
HARNESSES = ("claude", "agy", "codex", "opencode")


def _route(model, effort, harness, score, *, status="available"):
    return {
        "model_id": model, "invocation_model_id": model, "harness": harness, "harness_version": "1.0",
        "effort": effort, "capabilities": ["builder"], "invocation_source": "local-evidence: contract fixture",
        "benchmark_indexes": {} if score is None else {INDEX: score},
        "quota": {"status": "ok", "tightest_remaining_percent": 100},
        "route_status": status, "price_fields": {"input_per_mtok": 1, "output_per_mtok": 2},
    }


def _decide(pool, size, *, seed=None, role="worker"):
    request = {
        "role": role, "playbook": "Change", "allow_free_models": True, "cost_policy": "balanced",
        "routing_seed": "policy-data-contract", "adaptive_config": {"exploration": {"rate": 0.0}},
        "policy": {"required_capabilities": ["builder"],
                   "floor": {"min_effort": "none", "allowed_sources": ["local-evidence:"]}},
        "context": {"gear": size, "size_class": size} if size else {},
        "candidates": pool,
    }
    if seed is not None:
        request.update(preferred_seed=seed, slate_anchor=True)
    return routing.route(request)


def _categories(decision):
    return {row.get("category") for row in decision["rejected"]}


def _identity(route):
    return route["model_id"], route["effort"], route["harness"]


def _seed_routes(rows):
    return [(row["model_id"], row["effort"], row.get("harness") or "claude") for row in rows]


@pytest.mark.parametrize("size", SIZES)
def test_a_size_floor_is_enforced_exactly_at_its_value(size):
    floor = POLICY["floors"][size]
    at_floor = _decide([_route("synthetic-a", "medium", "fake", floor)], size)
    assert at_floor["status"] == "selected", at_floor
    below = _decide([_route("synthetic-a", "medium", "fake", floor - 0.5)], size)
    assert below["status"] == "no_qualifying_candidate"
    assert "subtask-intelligence-floor" in _categories(below)
    unscored = _decide([_route("synthetic-a", "medium", "fake", None)], size)
    assert unscored["status"] == "no_qualifying_candidate"
    assert "subtask-intelligence-floor" in _categories(unscored)
    other_index = {**_route("synthetic-a", "medium", "fake", None),
                   "benchmark_indexes": {INDEX + " (other edition)": floor + 50}}
    assert slates.benchmark_gate(other_index, size) is not None
    assert slates.benchmark_gate({**other_index, "benchmark_indexes": {INDEX: None}}, size) is not None
    # A run with no task size keeps its role floor, so the size floor never applies.
    assert slates.benchmark_gate(_route("synthetic-a", "medium", "fake", None), None) is None
    assert _decide([_route("synthetic-a", "medium", "fake", None)], None)["status"] == "selected"


@pytest.mark.parametrize("size", SIZES)
def test_the_router_follows_the_preferred_slate_order_and_falls_through_it(size):
    rows = _seed_routes(POLICY["executor_preferred"][size])
    seed = slates.ideal_seed(size)
    for tried in range(len(rows)):
        pool = [_route(model, effort, harness, CLEAR, status="untried" if i < tried else "available")
                for i, (model, effort, harness) in enumerate(rows)]
        chosen = _decide(pool, size, seed=seed)
        assert chosen["status"] == "selected", (size, tried, chosen)
        assert _identity(chosen["candidate"]) == rows[tried], (size, tried)
    exhausted = _decide([_route(m, e, h, CLEAR, status="untried") for m, e, h in rows], size, seed=seed)
    assert exhausted["status"] == "no_qualifying_candidate"


@pytest.mark.parametrize("size", SIZES)
def test_the_preference_summary_reports_each_slate_route_once(size):
    rows = _seed_routes(POLICY["executor_preferred"][size])
    seed = slates.ideal_seed(size)
    lead = _route(*rows[0][:2], rows[0][2], CLEAR)
    selected = _decide([lead], size, seed=seed)
    summary = slates.route_summary(seed, [lead], selected["rejected"], selected["slate"])
    assert [row["status"] for row in summary] == ["selected"] + ["not-in-catalog"] * (len(rows) - 1)
    assert len(slates.skip_lines(summary)) == len(rows) - 1
    untried = {**lead, "route_status": "untried"}
    blocked = _decide([untried], size, seed=seed)
    summary = slates.route_summary(seed, [untried], blocked["rejected"], blocked.get("slate", []))
    assert [row["status"] for row in summary][0] == "unqualified"


@pytest.mark.parametrize("gear", [g for g in config.RUN_GEARS if g in POLICY["planner_preferred"]])
def test_a_planner_follows_its_gear_table_in_data_order(gear):
    flat = [{"model_id": "synthetic-flat", "effort": "medium", "harness": "claude"}]
    seed = candidates._preferred_seed({"preferred_seed": flat}, {"gear": gear}, "S", role="planner")
    assert [(row["model_id"], row["effort"]) for row in seed] == [
        (row["model_id"], row["effort"]) for row in POLICY["planner_preferred"][gear]]


@pytest.mark.parametrize("gear", [None, *config.LEGACY_GEARS,
                                  *[g for g in config.RUN_GEARS if g not in POLICY["planner_preferred"]]])
def test_a_gear_without_a_planner_table_keeps_the_flat_seed(gear):
    flat = [{"model_id": "synthetic-flat", "effort": "medium", "harness": "claude"}]
    assert candidates._preferred_seed({"preferred_seed": flat}, {"gear": gear}, None, role="planner") == flat


@pytest.mark.parametrize("restriction", POLICY["model_restrictions"], ids=lambda r: r["model_id"])
def test_model_restrictions_bind_exactly_the_sizes_and_harnesses_in_data(restriction):
    model = restriction["model_id"]
    for size in SIZES:
        for harness in HARNESSES:
            allowed = (size in restriction.get("allowed_sizes", SIZES)
                       and harness in restriction.get("allowed_harnesses", HARNESSES))
            route = _route(model, "medium", harness, CLEAR)
            assert (slates.model_restriction(route, size) is None) is allowed, (size, harness)
            alias = {**route, "model_id": "synthetic-alias", "alias_resolved_to": model}
            assert (slates.model_restriction(alias, size) is None) is allowed, (size, harness)
            decision = _decide([route], size)
            assert ("size-model-restriction" in _categories(decision)) is (not allowed), (size, harness, decision)
            probe = {**route, "route_status": "untried"}
            gate = routing._pool_gate(probe, {"builder"}, None, "Change", set(), policy_size=size)
            assert (gate is None) is allowed, (size, harness, gate)
    assert slates.model_restriction(_route(model, "medium", "claude", CLEAR), None) is None


def test_a_probe_is_gated_by_the_size_floor_like_a_dispatch():
    for size, floor in POLICY["floors"].items():
        below = _route("synthetic-a", "medium", "fake", floor - 1, status="untried")
        gate = routing._pool_gate(below, {"builder"}, None, "Change", set(), policy_size=size)
        assert gate and gate[0] == 4 and f">= {floor}" in gate[1], (size, gate)
        meets = _route("synthetic-a", "medium", "fake", floor, status="untried")
        assert routing._pool_gate(meets, {"builder"}, None, "Change", set(), policy_size=size) is None
        # Discovery may probe a route whose score is unknown, but a dispatch still fails closed.
        unknown = _route("synthetic-a", "medium", "fake", None, status="untried")
        assert routing._pool_gate(unknown, {"builder"}, None, "Change", set(), policy_size=size) is None
        assert _decide([{**unknown, "route_status": "available"}], size)["status"] == "no_qualifying_candidate"


def _exception_candidate(rule, **override):
    route = _route(rule["model_id"], rule["effort"], rule.get("harness", "claude"),
                   rule["minimum_score"] if rule["score_evidence"] == "numeric-below-floor" else None)
    return {**route, **override}


@pytest.mark.parametrize("rule", POLICY["floor_exceptions"], ids=lambda r: r["name"])
def test_a_floor_exception_waives_only_its_exact_scope(rule):
    size, role = rule["size"], rule["role"]
    floor = POLICY["floors"][size]
    exact = _exception_candidate(rule)
    assert slates.benchmark_gate(exact, size), "the exception never lowers the floor itself"
    for caller in (role, "worker"):
        applied = slates.floor_exception(exact, size, caller)
        assert applied and applied["exception"] == rule["name"] and applied["size"] == size
        assert applied["not_waived"] == rule["not_waived"]
    pool = _decide([exact], size)
    assert pool["status"] == "selected", pool
    assert [e["exception"] for e in pool["routing"]["floor_exceptions"]] == [rule["name"]]
    assert routing._pool_gate({**exact, "route_status": "untried"}, {"builder"}, None, "Change", set(),
                              policy_size=size, role=role) is None

    misses = [(exact, other, role) for other in SIZES if other != size]
    misses += [(exact, size, "code_reviewer"), (exact, None, role),
               ({**exact, "model_id": "synthetic-other"}, size, role),
               ({**exact, "effort": "synthetic-effort"}, size, role)]
    if "harness" in rule:
        misses.append(({**exact, "harness": next(h for h in HARNESSES if h != rule["harness"])}, size, role))
    published = floor - 1 if rule["score_evidence"] == "missing" else rule["minimum_score"] - 1
    misses.append(({**exact, "benchmark_indexes": {INDEX: published}}, size, role))
    misses.append(({**exact, "benchmark_indexes": {INDEX: floor}}, size, role))
    for candidate, where, caller in misses:
        assert slates.floor_exception(candidate, where, caller) is None, (candidate, where, caller)
    assert _decide([misses[-2][0]], size)["status"] == "no_qualifying_candidate"
    for malformed in ([], "malformed"):
        assert slates.floor_exception({**exact, "benchmark_indexes": malformed}, size, role) is None


@pytest.mark.parametrize("gear", config.RUN_GEARS)
def test_run_gears_hand_their_task_size_to_the_size_policy(gear):
    for size in SIZES:
        assert slates.new_gear_size({"context": {"gear": gear, "size_class": size}}) == size


@pytest.mark.parametrize("gear", [None, "", *config.LEGACY_GEARS])
def test_historical_gears_and_a_missing_gear_never_inherit_a_size_policy(gear):
    assert slates.new_gear_size({"context": {"gear": gear, "size_class": "S"}}) is None
    assert slates.new_gear_size({"context": {"gear": gear, "size_class": "XL"}}) is None
    assert slates.new_gear_size({"context": {"size_class": "L"}}) is None
    assert slates.new_gear_size({"size_class": "XL"}) is None


def _rule_candidate(rule):
    spec, rest = rule["route"].split("/", 1)
    model, effort = rest.rsplit("@", 1)
    harness, _, major = spec.split("|")[0].partition("@")
    return {"harness": harness, "harness_version": f"{major}.0.0" if major else None,
            "model_id": model, "invocation_model_id": model, "effort": effort}


def _state(rule, role, size):
    return route_policy.effective_role_state(_rule_candidate(rule), role, [], size)["state"]


def test_every_shipped_role_rule_holds_at_its_own_scope():
    rules = route_policy.SEED_ROLE_RULES
    wrong = []
    for rule in rules:
        for size in rule.get("sizes") or [None]:
            overlapping = [r for r in rules if r is not rule and r["role"] == rule["role"]
                           and r["route"] == rule["route"] and size in (r.get("sizes") or [size])
                           and r.get("sizes") is not None and rule.get("sizes") is None]
            if overlapping:
                continue
            if _state(rule, rule["role"], size) != rule["state"]:
                wrong.append((rule["role"], rule["route"], size, rule["state"]))
    assert not wrong, wrong


def test_a_size_scoped_shipped_rule_does_not_leak_to_other_sizes():
    rules = route_policy.SEED_ROLE_RULES
    for rule in rules:
        if rule.get("sizes") is None:
            continue
        siblings = [r for r in rules if r["role"] == rule["role"] and r["route"] == rule["route"]
                    and r["state"] == rule["state"]]
        if any(r.get("sizes") is None for r in siblings):
            continue
        covered = {size for r in siblings for size in r["sizes"]}
        for size in set(SIZES) - covered:
            assert _state(rule, rule["role"], size) != rule["state"], (rule, size)


def test_a_shipped_role_rule_never_leaks_into_a_role_it_does_not_name():
    rules = route_policy.SEED_ROLE_RULES
    for rule in rules:
        named = {r["role"] for r in rules if r["route"] == rule["route"]}
        for role in ("planner", "unlisted-role", *{r["role"] for r in rules} - named):
            for size in (None, *SIZES):
                assert _state(rule, role, size) == "neutral", (rule["route"], role, size)


def test_size_scoped_shipped_rules_do_not_apply_when_the_size_is_unknown():
    rules = route_policy.SEED_ROLE_RULES
    for rule in rules:
        if rule.get("sizes") is None:
            continue
        sibling_unsized = [r for r in rules if r["role"] == rule["role"] and r["route"] == rule["route"]
                           and r.get("sizes") is None]
        expected = sibling_unsized[0]["state"] if sibling_unsized else "neutral"
        assert _state(rule, rule["role"], None) == expected, rule


def test_the_router_rejects_every_shipped_deny_in_its_role_and_scope():
    deny = [r for r in route_policy.SEED_ROLE_RULES if r["state"] == "deny"]
    assert deny
    for rule in deny:
        candidate = {**_rule_candidate(rule), "capabilities": ["builder", "review"], "route_status": "available",
                     "quota": {"status": "ok", "tightest_remaining_percent": 80},
                     "cost": {"money_estimate": 1, "quota_burn": 1, "wall_clock_seconds": 1}}
        for size in rule.get("sizes") or [None]:
            request = {"role": rule["role"], "candidates": [candidate], "preferred_seed": None,
                       "user_policies": [], "runs_db": None,
                       "policy": {"floor": None, "strict_seed": False, "required_capabilities": []}}
            if size:
                request.update(run_gear=size, size_class=size)
            decision = routing.route(request)
            assert decision["status"] == "no_qualifying_candidate", (rule, size)
            assert "role-deny" in _categories(decision), (rule, size, decision["rejected"])


def test_built_executor_candidates_never_include_an_unscoped_shipped_deny(tmp_path, monkeypatch):
    from office import db
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.chdir(tmp_path)
    con = db.connect()
    try:
        built, _ = candidates.build_candidates(con, "executor", probe=False,
                                               user_policies=[{"role_rules": [], "denied": [], "overkill": [], "ceilings": {},
                                                       "sources": {"role_rules": "user"}}])
    finally:
        con.close()
    built_ids = {routing.candidate_id(c) for c in built}
    unscoped = [r for r in route_policy.SEED_ROLE_RULES
                if r["role"] == "executor" and r["state"] == "deny" and r.get("sizes") is None]
    assert unscoped
    for rule in unscoped:
        spec, rest = rule["route"].split("/", 1)
        harnesses = [alt.partition("@")[0] for alt in spec.split("|")]
        assert not [i for i in built_ids if i.partition("@")[0] in harnesses and i.endswith("/" + rest)], rule


def test_every_shipped_route_is_either_dispatchable_or_a_documented_discovery_candidate():
    rows = shipped_catalog.load()["models"]
    assert rows
    for row in rows:
        status = route_policy.row_status(row)
        if row.get("dispatchable") is False:
            assert status["status"] == "discovered-unconfirmed", shipped_catalog.key(row)
        if row.get("discovery") == "eligible":
            assert row["dispatchable"] is False, shipped_catalog.key(row)
            assert row["discovery_reason"] and row["invocation_model_id"], shipped_catalog.key(row)
