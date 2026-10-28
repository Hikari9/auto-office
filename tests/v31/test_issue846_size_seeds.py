"""#846: the #809 size slates are the default executor routing, and an old plan re-routes onto them."""
import pytest

from office import candidates, dispatch, executor_slates, routing

from test_issue809_real_routes import candidate

PROFILE = {"XS": "lean", "S": "balanced", "M": "balanced", "L": "deep", "XL": "deep"}

# The slate primary at its nominal benchmark score (#809 final matrix).
PRIMARY_SCORE = {"XS": 34, "S": 40, "M": 41, "L": 47, "XL": 51}
FLAT_SEED = [{"model_id": "claude-sonnet-5-5", "harness": "claude", "effort": "high"}]


# Catalog list prices (USD per Mtok in/out) so a cheap slate primary is not out-scored on price alone.
PRICE = {"claude-haiku-5-5": (0.1, 0.5), "gemini-3.8-flash": (0.75, 3.75), "claude-sonnet-5-5": (2.0, 10.0),
         "gpt-6.1-terra": (1.0, 5.0), "claude-opus-5-5": (5.0, 25.0)}
FLOORS_OK = {"XS": 33, "S": 38, "M": 41, "L": 47, "XL": 51}


def priced(model, effort, score, harness):
    c = candidate(model, effort, score, harness=harness)
    low, high = PRICE.get(model, (1.0, 5.0))
    return {**c, "price_fields": {"input_per_mtok": low, "output_per_mtok": high}}


def decide(size, models):
    """An offline executor decision for a task of `size` on a sized run, with the dispatch profile classify() gives it."""
    return routing.route({
        "role": "worker",
        "playbook": "Change",
        "policy": {"required_capabilities": ["builder"],
                   "floor": {"min_effort": "none", "allowed_sources": ["local-evidence:"],
                             "min_benchmark_index": {"index_name": executor_slates.INDEX, "min_score": 31}},
                   "quota_reserve_percent": 5},
        "context": {"gear": size, "size_class": size, "dispatch_profile": PROFILE[size],
                    "dispatch_profile_version": "task-dispatch-profile-1"},
        "allow_free_models": True,
        "cost_policy": "balanced",
        "routing_seed": "issue-846-fixture",
        "adaptive_config": {"exploration": {"rate": 0.0}},
        "preferred_seed": executor_slates.ideal_seed(size),
        "slate_anchor": True,  # what route_role sets for a size-slate seed
        "candidates": models,
    })


def _slate_pool(size):
    """Every slate route at a score that clears the floor."""
    return [priced(model, effort, PRIMARY_SCORE[size] if i == 0 else FLOORS_OK[size], harness or "agy")
            for i, (model, effort, harness) in enumerate(executor_slates.IDEAL[size])]


@pytest.mark.parametrize("size", list(executor_slates.FLOORS))
def test_each_size_selects_its_slate_primary_when_allowed(size):
    model, effort, harness = executor_slates.IDEAL[size][0]
    pool = _slate_pool(size)[:1] + [priced("claude-sonnet-5-5", "high", 47, "claude")]
    out = decide(size, pool)
    assert out["status"] == "selected", out
    chosen = out["candidate"]
    assert (chosen["model_id"], chosen["effort"], chosen["harness"]) == (model, effort, harness or "agy"), out


@pytest.mark.parametrize("size", list(executor_slates.FLOORS))
def test_each_size_falls_to_the_next_slate_route_when_the_primary_cannot_run(size):
    pool = _slate_pool(size)[:2] + [priced("claude-sonnet-5-5", "high", 47, "claude")]
    pool[0] = dict(pool[0], route_status="untried")
    model, effort, harness = executor_slates.IDEAL[size][1]
    out = decide(size, pool)
    assert out["status"] == "selected", out
    assert (out["candidate"]["model_id"], out["candidate"]["effort"]) == (model, effort), out


@pytest.mark.parametrize("size", list(executor_slates.FLOORS))
def test_a_flat_executor_seed_does_not_shadow_the_size_slate(size):
    run = {"gear": size, "risk": {"size_class": size}}
    assert candidates._preferred_seed({"preferred_seed": FLAT_SEED}, run, size) == executor_slates.ideal_seed(size)
    assert candidates._preferred_seed({"preferred_seed": FLAT_SEED}, run, "S") == executor_slates.ideal_seed("S")


def test_a_per_size_seed_still_wins_and_a_legacy_run_keeps_the_flat_seed():
    by_size = {"S": [{"model_id": "luna"}]}
    run = {"gear": "S"}
    assert candidates._preferred_seed({"preferred_seed_by_size": by_size, "preferred_seed": FLAT_SEED},
                                      run, "S") == by_size["S"]
    assert candidates._preferred_seed({"preferred_seed": FLAT_SEED}, {"gear": "express"}, "S") == FLAT_SEED
    assert candidates._preferred_seed({"preferred_seed": FLAT_SEED}, {"gear": "S"}, None) == FLAT_SEED
    assert candidates._preferred_seed({"preferred_seed": FLAT_SEED}, {"gear": "S"}, "S", role="code_reviewer") \
        == FLAT_SEED


def test_the_size_slate_anchors_routing_but_a_users_own_seed_does_not():
    run = {"gear": "S"}
    assert candidates._uses_size_slate({}, run, "S", "executor")
    assert candidates._uses_size_slate({"preferred_seed": FLAT_SEED}, run, "S", "executor")
    # A per-size seed is the user's, even when it copies the ideal slate.
    copied = {"preferred_seed_by_size": {"S": executor_slates.ideal_seed("S")}}
    assert not candidates._uses_size_slate(copied, run, "S", "executor")
    assert candidates._uses_size_slate(copied, run, "M", "executor")
    assert not candidates._uses_size_slate({}, {"gear": "express"}, "S", "executor")
    assert not candidates._uses_size_slate({}, run, "S", "code_reviewer")
    assert not candidates._uses_size_slate({}, run, None, "executor")


@pytest.mark.parametrize("size", list(executor_slates.FLOORS))
def test_routes_outside_the_slate_stay_behind_it_as_fallbacks(size):
    outsider = (priced("gpt-6.1-terra", "xhigh", 45, "codex") if size == "L"
                else priced("claude-sonnet-5-5", "high", 47, "claude"))
    pool = _slate_pool(size)[:1] + [outsider]
    out = decide(size, pool)
    routes = [e["route"] for e in out["routing"]["slate"]]
    assert routes == [routing.candidate_id(pool[0]), routing.candidate_id(outsider)], routes
    assert out["routing"]["slate_anchor"]


def test_with_no_slate_route_available_an_off_slate_route_still_runs():
    pool = [dict(c, route_status="untried") for c in _slate_pool("S")] + [priced("claude-sonnet-5-5", "high", 47, "claude")]
    out = decide("S", pool)
    assert out["status"] == "selected" and out["candidate"]["model_id"] == "claude-sonnet-5-5", out


def _fresh(size, *, status="selected"):
    pool = _slate_pool(size)
    return {"status": status, "request": {"preferred_seed": executor_slates.ideal_seed(size), "candidates": pool,
                                          "slate_anchor": True}}


def test_an_s_plan_that_drew_sonnet_high_reroutes_at_dispatch():
    old = {"chooser": "router", "primary": "claude@1.0/claude-sonnet-5-5@high"}
    fresh = _fresh("S")
    fresh["request"]["candidates"].append(priced("claude-sonnet-5-5", "high", 47, "claude"))
    why = dispatch._outdated_slate(old, fresh)
    assert why and "claude-sonnet-5-5@high" in why and "current policy" in why


def test_a_planned_primary_inside_the_current_slate_is_kept():
    fresh = _fresh("S")
    primary = routing.candidate_id(fresh["request"]["candidates"][0])
    assert dispatch._outdated_slate({"chooser": "router", "primary": primary}, fresh) is None


@pytest.mark.parametrize("chooser", ["planner", "declared", "recorded"])
def test_a_deliberate_planned_choice_is_never_rerouted(chooser):
    old = {"chooser": chooser, "primary": "claude@1.0/claude-sonnet-5-5@high"}
    assert dispatch._outdated_slate(old, _fresh("S")) is None


def test_no_reroute_for_a_legacy_run_or_a_user_seed():
    """Without `slate_anchor` (legacy-pinned run, or a seed the user set) the plan stands."""
    old = {"chooser": "router", "primary": "claude@1.0/claude-sonnet-5-5@high"}
    fresh = _fresh("S")
    del fresh["request"]["slate_anchor"]
    assert dispatch._outdated_slate(old, fresh) is None


def test_no_reroute_without_a_seed_or_a_selected_fresh_decision():
    old = {"chooser": "router", "primary": "claude@1.0/claude-sonnet-5-5@high"}
    no_seed = _fresh("S")
    no_seed["request"]["preferred_seed"] = None
    assert dispatch._outdated_slate(old, no_seed) is None
    assert dispatch._outdated_slate(old, _fresh("S", status="no_qualifying_candidate")) is None


# ------------------------------------------------------------------ #850 §4: the orchestrator plans


@pytest.mark.parametrize("gear,effort", [("M", "low"), ("L", "medium"), ("XL", "high")])
def test_an_explicit_planner_follows_the_run_gear_table(gear, effort):
    run = {"gear": gear}
    seed = candidates._preferred_seed({"preferred_seed": FLAT_SEED}, run, "S", role="planner")
    assert [(e["model_id"], e["effort"]) for e in seed] == [
        ("claude-opus-5-5", effort), ("gpt-6-astra", effort), ("gemini-4-argon", effort)]
    assert seed[0]["harness"] == "claude" and seed[1]["harness"] == "codex"


@pytest.mark.parametrize("gear", ["XS", "S", "express", None])
def test_xs_s_and_legacy_gears_have_no_gear_planner_table(gear):
    assert candidates._preferred_seed({"preferred_seed": FLAT_SEED}, {"gear": gear}, None, role="planner") \
        == FLAT_SEED


def test_no_new_gear_queues_a_dedicated_planner_by_default():
    from office import config
    cfg = config.load_yaml(config.default_config_path())
    for gear in ("XS", "S", "M", "L", "XL"):
        risk = config.resolve_risk(cfg, "repo", None, False)
        risk["size_class"] = gear
        assert config.resolve_gates(gear, risk, cfg)["dedicated_planner"] is False, gear


@pytest.mark.parametrize("size", ["S", "M"])
def test_exploration_never_lifts_a_route_outside_the_slate_over_it(size):
    outsider = priced("gpt-6.1-terra", "xhigh", 45, "codex")
    pool = _slate_pool(size)[:1] + [outsider]
    request = {"role": "worker", "playbook": "Change", "preferred_seed": executor_slates.ideal_seed(size),
               "slate_anchor": True, "routing_seed": "issue-846-explore",
               "policy": {"required_capabilities": ["builder"], "quota_reserve_percent": 5,
                          "floor": {"min_effort": "none", "allowed_sources": ["local-evidence:"]}},
               "context": {"gear": size, "size_class": size}, "cost_policy": "balanced", "allow_free_models": True,
               "adaptive_config": {"exploration": {"rate": 1.0, "margin": 1.0, "max_cost_vs_primary_percent": 1000}},
               "candidates": pool}
    for seed in range(40):
        out = routing.route({**request, "routing_seed": f"issue-846-explore-{seed}"})
        assert out["candidate"]["model_id"] == pool[0]["model_id"], (seed, out["routing"]["exploration"])
        assert out["routing"]["exploration"]["picked"] is None
