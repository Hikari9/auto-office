"""Quota routing keeps unknown candidates and discounts them in adaptive ranking."""
from office import routing


def _candidate(harness, model, *, quota="unknown", remaining=None, caps=("builder",)):
    q = ({"status": "ok", "tightest_remaining_percent": remaining} if quota == "ok"
         else {"status": "unknown", "tightest_remaining_percent": None})
    return {
        "harness": harness, "harness_version": "2.0", "model_id": model,
        "invocation_model_id": model, "effort": "medium", "capabilities": list(caps),
        "benchmark_indexes": {"Artificial Analysis Intelligence Index v4.3.2": 45},
        "price_fields": {"output_per_mtok": 2.0, "input_per_mtok": 0.5},
        "speed_fields": {"output_tok_per_s": 100.0, "ttft_ms": 1000},
        "cost": {"money_estimate": 2.0}, "quota": q,
    }


def _request(candidates, **extra):
    return {"role": "worker", "playbook": "Change", "candidates": candidates,
            "policy": {"cost_policy": "balanced", "required_capabilities": ["builder"]},
            "evidence": {"routes": {}, "pooling": {"prior_strength": 8}},
            "routing_seed": "quota-routing-test", "adaptive_config": {"exploration": {"rate": 0}},
            **extra}


def test_unknown_quota_stays_in_the_slate_and_the_old_flag_has_no_effect():
    codex = _candidate("codex", "gpt-6", quota="ok", remaining=76)
    claude = _candidate("claude", "opus")
    request = _request([codex, claude], preferred_seed=[{"model_id": "opus"}],
                       adaptive_config={"competitive_band": 0})
    without_flag = routing.route(request)
    with_flag = routing.route({**request, "allow_unknown_quota_with_safe_alternative": True})

    assert without_flag["status"] == "selected"
    assert {row["route"] for row in without_flag["routing"]["candidates"]} == {
        routing.candidate_id(codex), routing.candidate_id(claude)}
    unknown = next(row for row in without_flag["routing"]["candidates"] if row["route"] == routing.candidate_id(claude))
    assert unknown["quota"] == {"score": 0.35, "state": "unknown"}
    assert [row["route"] for row in without_flag["slate"]] == [row["route"] for row in with_flag["slate"]]
    assert routing.candidate_id(claude) in {row["route"] for row in without_flag["slate"]}


def test_known_safe_route_outranks_unknown_with_equal_other_inputs():
    safe = _candidate("codex", "gpt-6", quota="ok", remaining=76)
    unknown = _candidate("claude", "opus")
    decision = routing.route(_request([unknown, safe], adaptive_config={"competitive_band": 0}))
    rows = {row["route"]: row for row in decision["routing"]["candidates"]}

    assert rows[routing.candidate_id(safe)]["utility"] > rows[routing.candidate_id(unknown)]["utility"]
    assert rows[routing.candidate_id(safe)]["rank"] < rows[routing.candidate_id(unknown)]["rank"]
    assert decision["slate"][0]["route"] == routing.candidate_id(safe)


def test_nonadaptive_routing_keeps_unknown_alongside_known_safe():
    safe = _candidate("codex", "gpt-6", quota="ok", remaining=76, caps=("review",))
    unknown = _candidate("claude", "opus", caps=("review",))
    decision = routing.route({"role": "plan_reviewer", "candidates": [safe, unknown],
                              "policy": {"required_capabilities": ["review"]},
                              "preferred_seed": [{"model_id": "opus"}]})

    assert decision["status"] == "selected"
    assert decision["selected"] == routing.candidate_id(unknown)
    assert not any(item["candidate"] == routing.candidate_id(unknown) and item["stage"] == 6
                    for item in decision["rejected"])


def test_all_known_unsafe_candidates_keep_the_protected_quota_stop():
    first = _candidate("codex", "gpt-6", quota="ok", remaining=2)
    second = _candidate("claude", "opus", quota="ok", remaining=4)
    decision = routing.route(_request([first, second]))

    assert decision["status"] == "protected_quota_would_be_consumed"
    assert decision["selected"] is None


def _reviewer_request(role, luna, deepseek):
    return {"role": role, "candidates": [luna, deepseek],
            "policy": {"required_capabilities": ["review"], "quota_reserve_percent": 5.0, "strict_seed": True},
            "preferred_seed": [{"model_id": "luna"}, {"model_id": "deepseek"}]}


import pytest
from office import candidates


def test_protected_luna_falls_through_to_the_next_seeded_plan_reviewer():
    luna = _candidate("codex", "luna", quota="ok", remaining=0, caps=("review",))
    deepseek = _candidate("opencode", "deepseek", caps=("review",))
    decision = routing.route(_reviewer_request("plan_reviewer", luna, deepseek))

    assert decision["status"] == "selected"
    assert decision["selected"] == routing.candidate_id(deepseek)


def test_protected_luna_falls_through_to_a_proven_deepseek_code_reviewer(tmp_path):
    from office import db, scoring
    luna = _candidate("codex", "luna", quota="ok", remaining=0, caps=("review",))
    deepseek = _candidate("opencode", "deepseek", caps=("review",))
    path = tmp_path / "runs.db"
    request = {**_reviewer_request("code_reviewer", luna, deepseek), "runs_db": str(path)}
    scoring.record_trust_acts(path, [routing.candidate_id(luna), routing.candidate_id(deepseek)], "proven", "user", "test trust act for the fallthrough")
    con = db.connect(path)

    decision = routing.route(request, connection=con)

    assert decision["status"] == "selected"
    assert decision["selected"] == routing.candidate_id(deepseek)


@pytest.mark.parametrize("role", ["plan_reviewer", "code_reviewer"])
def test_protected_stop_names_the_seeded_fallback_cut_by_the_trust_floor(role):
    luna = _candidate("codex", "luna", quota="ok", remaining=0, caps=("review",))
    deepseek = _candidate("opencode", "deepseek", caps=("review",))
    deepseek["adapter_trust"] = "valid-unverified"
    request = _reviewer_request(role, luna, deepseek)
    decision = routing.route(request)
    decision["request"] = request
    decision["rejected"] = list(decision.get("rejected") or []) + [
        {"candidate": routing.candidate_id(deepseek), "stage": 2,
         "reason": "adapter trust is derived as 'valid-unverified', not proven for normal mutable/gate authority"}]

    text = candidates.seeded_fallback_blockers(decision)

    assert routing.candidate_id(deepseek) in text and "valid-unverified" in text
    assert f"office approve trust {routing.candidate_id(deepseek)}" in text
