"""#809 actual offline route() decisions, not only sizing helper assertions."""
import json
import sqlite3

import pytest

from office import dispatch, executor_slates, route_policy, routing


def candidate(model, effort, score, *, harness="codex", available=True, remaining=100):
    return {"model_id": model, "invocation_model_id": model,
            "harness": harness, "harness_version": "1.0",
            "effort": effort, "capabilities": ["builder"],
            "invocation_source": "local-evidence: test fixture",
            "benchmark_indexes": ({executor_slates.INDEX: score} if score is not None else {}),
            "quota": {"status": "ok", "tightest_remaining_percent": remaining},
            "route_status": "available" if available else "untried",
            "price_fields": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}


def decide(size, models, *, gear="XL", preference=None, user_policies=None, discovery=None,
           discovery_input=None, policy_overrides=None):
    return routing.route({
        "role": "worker",
        "playbook": "Change",
        "policy": {"required_capabilities": ["builder"],
                   "floor": {"min_effort": "none",
                             "allowed_sources": ["local-evidence:"],
                             "min_benchmark_index": {
                                 "index_name": executor_slates.INDEX, "min_score": 31}},
                   "quota_reserve_percent": 5},
        "context": {"gear": gear, "size_class": size},
        "allow_free_models": True,
        "cost_policy": "balanced",
        "routing_seed": "issue-809-fixture",
        "adaptive_config": {"exploration": {"rate": 0.0}, "competitive_band": 0.0},
        "preferred_seed": preference or executor_slates.ideal_seed(size),
        "candidates": models,
        **({"user_policies": user_policies} if user_policies else {}),
        **({"discovery": discovery} if discovery else {}),
        **({"discovery_input": discovery_input} if discovery_input else {}),
        **({"policy_overrides": policy_overrides} if policy_overrides else {}),
    })


@pytest.mark.parametrize("size,effort", [("L", "high")])
def test_real_sonnet_harness_self_fallback_and_unavailable_first_route(size, effort):
    score = 41 if size == "M" else 47
    claude = candidate("claude-sonnet-5-5", effort, score, harness="claude")
    agy = candidate("claude-sonnet-5-5", effort, score, harness="agy")
    terra = candidate("gpt-6.1-terra", "medium" if size == "M" else "high",
                      41 if size == "M" else 44)
    preferred = executor_slates.ideal_seed(size)
    selected = decide(size, [claude, agy, terra], preference=preferred)
    if selected["candidate"]["harness"] == "claude":
        assert selected["selection_disclosure"]["slate"][1]["route"].startswith("agy@"), selected
    fallback = decide(size, [dict(claude, route_status="untried"), agy, terra], preference=preferred)
    assert fallback["candidate"]["harness"] == "agy", fallback
    quota = decide(size, [dict(claude, quota={"status": "ok", "tightest_remaining_percent": 0}),
                          agy, terra], preference=preferred)
    assert quota["candidate"]["harness"] == "agy", quota
    retried = decide(size, [agy, terra], preference=preferred)
    assert retried["candidate"]["harness"] == "agy", retried


def test_m_l_planner_preserves_all_three_fallbacks_when_slate_has_four():
    from office import adaptive
    routes = ["claude@2/claude-sonnet-5-5@medium",
              "agy@1/claude-sonnet-5-5@medium",
              "codex@0/gpt-6.1-terra@medium",
              "opencode@1/glm-5.3-flash@max"]
    audit = {"slate": [{"route": route} for route in routes],
             "candidates": [{"route": route, "label": route, "utility": 0.80} for route in routes]}
    automatic = adaptive.apply_planner_choice(audit, None)
    assert automatic["primary"] == routes[0] and automatic["fallbacks"] == routes[1:]
    explicit = adaptive.apply_planner_choice(audit, {"routes": routes, "why": "qualified exact routes"})
    assert explicit["fallbacks"] == routes[1:]
    assert len(explicit["fallbacks"]) == 3


def test_issue861_unconfirmed_haiku_uses_real_terra_then_sonnet_fallback():
    haiku = candidate("claude-haiku-5-5", "high", 38, harness="claude", available=False)
    terra = candidate("gpt-6.1-terra", "medium", 41)
    sonnet = candidate("claude-sonnet-5-5", "medium", 41, harness="claude")
    first = decide("M", [haiku, terra, sonnet])
    assert first["candidate"]["model_id"] == "gpt-6.1-terra", first
    assert any("haiku" in x["candidate"] and x.get("category") == "untried"
               for x in first["rejected"]), first
    second = decide("M", [haiku, sonnet])
    assert second["candidate"]["model_id"] == "claude-sonnet-5-5", second
    assert "Temporary Sonnet Medium M fallback" in second["selection_disclosure"]["operational_fallback"]


def test_issue861_persisted_route_audit_retains_exact_haiku_receipt_and_fallback():
    haiku = candidate("claude-haiku-5-5", "high", 38, harness="claude")
    terra = candidate("gpt-6.1-terra", "medium", 41)
    sonnet = candidate("claude-sonnet-5-5", "medium", 41, harness="claude")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE routing_decisions(id TEXT, run_id TEXT, role TEXT,"
                " request_hash TEXT, selected_triple TEXT, decision_hash TEXT, created_at TEXT)")
    run = {"id": "run-861", "gear": "XL", "plan_version": None, "gates": {}}

    def persisted(choice):
        choice["request"] = {"role": "worker", "task_id": None,
                             "context": {"gear": "XL", "size_class": "M"}}
        dispatch._record_routing(con, run, choice)
        receipt = json.loads(con.execute(
            "SELECT disclosure_json FROM route_audit ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        saved = con.execute(
            "SELECT selected_triple, decision_hash FROM routing_decisions ORDER BY rowid DESC LIMIT 1").fetchone()
        assert saved == (choice["selected"], choice["decision_hash"])
        assert receipt["decision_hash"] == choice["decision_hash"]
        assert receipt["selected_route"] == choice["selected"]
        return receipt

    first = decide("M", [haiku, terra, sonnet])
    audit = persisted(first)
    waiver = audit["floor_exceptions"]
    assert len(waiver) == 1 and waiver[0]["candidate"] == first["selected"]
    assert waiver[0]["harness"] == "claude" and waiver[0]["effort"] == "high"
    assert waiver[0]["observed_score"] == 38
    assert audit["preferred_route"] == first["selected"]
    assert "gpt-6.1-terra@medium" in audit["fallback_routes"][0]

    fallback = decide("M", [dict(haiku, route_status="untried"), sonnet])
    stored = persisted(fallback)
    assert "floor_exceptions" not in stored
    assert stored["selected_route"] == fallback["selected"]
    assert "Temporary Sonnet Medium M fallback" in stored["operational_fallback"]
    assert stored["decision_hash"] == fallback["decision_hash"]
    con.close()


def test_issue861_legacy_pinned_routing_is_independent_of_new_m_policy():
    haiku = candidate("claude-haiku-5-5", "high", 38, harness="claude")
    sonnet = candidate("claude-sonnet-5-5", "medium", 41, harness="claude")
    historical_preference = [{"model_id": "claude-sonnet-5-5", "harness": "claude",
                              "effort": "medium"}]
    original = decide("M", [haiku, sonnet], gear="direct",
                      preference=historical_preference)
    replay = decide("XL", [haiku, sonnet], gear="direct",
                    preference=historical_preference)
    assert original["selected"] == replay["selected"], (original, replay)
    assert original["decision_hash"] == replay["decision_hash"]
    assert "floor_exceptions" not in original["routing"]
    assert original["candidate"]["model_id"] == "claude-sonnet-5-5"


def test_issue861_discovery_candidates_are_never_ordinary_dispatches():
    haiku = candidate("claude-haiku-5-5", "high", 38, harness="claude",
                      available=False)
    haiku.update({"route_status": "discovered-unconfirmed", "discovery": True,
                  "probe_key": "claude|1.0|claude-haiku-5-5|high|fixture|worker",
                  "price_fields": {"input_per_mtok": 0.2, "output_per_mtok": 0.2}})
    terra = candidate("gpt-6.1-terra", "medium", 41)
    fallback_id = routing.candidate_id(terra)
    discovery = {
        "settings": {**route_policy.DISCOVERY_DEFAULTS, "enabled": True,
                     "max_trial_percent_rolling_20": 100,
                     "trial_bounded_production_tasks": True},
        "allocation": {"probes": {"used": 0, "max": 2},
                       "trials": {"used": 0, "max": 1},
                       "rolling": {"used": 0, "max": 3}},
        "known_working": [fallback_id], "quarantined": [],
        "risk": {"size_class": "M", "blast_radius": "repo",
                 "irreversible": False},
    }
    decision = decide("M", [haiku, terra], discovery=discovery)
    assert decision["selected"] == fallback_id, decision
    assert decision["discovery"]["intent"] == "probe", decision
    assert decision["discovery"]["fallback"] == fallback_id
    assert decision["discovery"]["candidate"] == routing.candidate_id(haiku)
    assert "haiku" not in decision["selected"]
    assert all("haiku" not in row["route"] for row in decision["slate"])
    assert "floor_exceptions" not in decision["routing"]


def test_issue861_fresh_probe_trial_has_persisted_numeric_floor_receipt():
    haiku = candidate("claude-haiku-5-5", "high", 38, harness="claude", available=False)
    haiku.update({"route_status": "probe-passed", "discovery": True,
                  "probe_key": "claude|1.0|claude-haiku-5-5|high|fixture|worker",
                  "probe": {"result": "pass", "fresh": True,
                            "probed_at": "2026-10-28T00:00:00Z"},
                  "price_fields": {"input_per_mtok": 0.2, "output_per_mtok": 0.2}})
    terra = candidate("gpt-6.1-terra", "medium", 41)
    haiku_id, terra_id = routing.candidate_id(haiku), routing.candidate_id(terra)
    discovery = {
        "settings": {**route_policy.DISCOVERY_DEFAULTS, "enabled": True,
                     "max_trial_percent_rolling_20": 100,
                     "trial_bounded_production_tasks": True},
        "allocation": {"probes": {"used": 0, "max": 2},
                       "trials": {"used": 0, "max": 1},
                       "rolling": {"used": 0, "max": 3}},
        "known_working": [terra_id], "quarantined": [],
        "risk": {"size_class": "M", "blast_radius": "repo", "irreversible": False},
    }
    handle = {"candidate": haiku_id, "probe_key": haiku["probe_key"],
              "reservation_id": "R861", "attempt_id": "R861"}
    choice = decide("M", [haiku, terra], discovery=discovery, discovery_input=handle)
    assert choice["selected"] == haiku_id, choice
    assert choice["discovery"]["intent"] == "trial", choice
    assert choice["discovery"]["fallback"] == terra_id
    assert choice["selection_disclosure"]["trial"]["fallback"] == terra_id
    assert choice["selection_disclosure"]["harness"] == "claude"
    assert choice["selection_disclosure"]["effort"] == "high"
    waived = choice["routing"]["floor_exceptions"]
    assert len(waived) == 1 and waived[0]["candidate"] == haiku_id
    assert waived[0]["observed_score"] == 38 and waived[0]["size"] == "M"

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE routing_decisions(id TEXT, run_id TEXT, role TEXT,"
                " request_hash TEXT, selected_triple TEXT, decision_hash TEXT, created_at TEXT)")
    choice["request"] = {"role": "worker", "task_id": None}
    dispatch._record_routing(con, {"id": "run-861", "gear": "XL", "plan_version": None}, choice)
    saved = json.loads(con.execute("SELECT disclosure_json FROM route_audit").fetchone()[0])
    assert saved["floor_exceptions"] == waived
    assert saved["selected_route"] == haiku_id
    assert saved["discovery"]["attempt_id"] == "R861"
    assert saved["dispatch"]["dispatched"] == haiku_id
    con.close()


def test_issue861_authenticated_user_override_receipt_beats_role_deny_and_persists():
    haiku = candidate("claude-haiku-5-5", "high", 38, harness="claude")
    terra = candidate("gpt-6.1-terra", "medium", 41)
    deny = {"role_rules": [{"role": "worker", "route": "claude/claude-haiku-5-5@high",
                           "state": "deny", "sizes": ["M"]}],
            "sources": {"role_rules": "user"}}
    rid = routing.candidate_id(haiku)
    # The caller-facing candidates.route_role validates and claims this receipt;
    # raw routing.route intentionally only consumes the already validated handle.
    decision = decide("M", [haiku, terra], user_policies=[deny],
                      policy_overrides={rid: "validated-user-receipt-861"})
    assert decision["selected"] == rid, decision
    eligibility = next(row["eligibility"] for row in decision["routing"]["candidates"]
                       if row["route"] == rid)
    assert eligibility["policy"] == "allow"
    assert "validated-user-receipt-861" in eligibility["source"]
    assert decision["routing"]["floor_exceptions"][0]["candidate"] == rid
