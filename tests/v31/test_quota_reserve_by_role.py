"""The protected quota reserve is read per role: executor family 20, reviewer family 5, else quota.reserve_percent."""
from datetime import datetime, timezone

import pytest

from office import candidates, config, db, routing, scheduler


def _candidate(harness, model, *, remaining, caps=("review",)):
    return {
        "harness": harness, "harness_version": "2.0", "model_id": model,
        "invocation_model_id": model, "effort": "medium", "capabilities": list(caps),
        "benchmark_indexes": {"Artificial Analysis Intelligence Index v4.3.2": 45},
        "price_fields": {"output_per_mtok": 2.0, "input_per_mtok": 0.5},
        "speed_fields": {"output_tok_per_s": 100.0, "ttft_ms": 1000},
        "cost": {"money_estimate": 2.0},
        "quota": {"status": "ok", "tightest_remaining_percent": remaining},
    }


def _route(role, candidate, reserve=None):
    """Route one role with the reserve the shipped config resolves for it, as candidates.route_role pins it."""
    reserve = config.quota_reserve_percent(_shipped(), role) if reserve is None else reserve
    return routing.route({"role": role, "candidates": [candidate],
                          "policy": {"quota_reserve_percent": reserve},
                          "evidence": {"routes": {}, "pooling": {"prior_strength": 8}},
                          "routing_seed": "quota-reserve-by-role", "adaptive_config": {"exploration": {"rate": 0}}})


def _shipped():
    return config.load_yaml(config.default_config_path())


def test_a_13_percent_route_is_skipped_for_an_executor_and_eligible_for_a_reviewer():
    route = _candidate("codex", "gpt-6", remaining=13)

    executor = _route("executor", route)
    reviewer = _route("plan_reviewer", route)
    executor_at_base_reserve = _route("executor", route, reserve=5)

    assert executor["status"] == "protected_quota_would_be_consumed", executor
    assert executor_at_base_reserve["status"] == "selected", executor_at_base_reserve
    assert reviewer["status"] == "selected" and reviewer["selected"] == routing.candidate_id(route), reviewer


def test_worker_is_in_the_executor_family_and_code_reviewer_in_the_reviewer_family():
    shipped = _shipped()

    assert config.quota_reserve_percent(shipped, "worker") == 20
    assert config.quota_reserve_percent(shipped, "executor") == 20
    assert config.quota_reserve_percent(shipped, "plan_reviewer") == 5
    assert config.quota_reserve_percent(shipped, "code_reviewer") == 5


def test_a_role_not_listed_falls_back_to_quota_reserve_percent():
    shipped = _shipped()

    assert config.quota_reserve_percent(shipped, "visual_reviewer") == shipped["quota"]["reserve_percent"] == 5
    assert config.quota_reserve_percent(shipped, None) == 5


def test_a_missing_role_key_falls_back_to_quota_reserve_percent():
    no_role_keys = {"quota": {"reserve_percent": 7}}
    partial = {"quota": {"reserve_percent": 7, "reserve_percent_by_role": {"reviewer": 3}}}

    assert config.quota_reserve_percent(no_role_keys, "executor") == 7
    assert config.quota_reserve_percent(no_role_keys, "plan_reviewer") == 7
    assert config.quota_reserve_percent(partial, "executor") == 7
    assert config.quota_reserve_percent(partial, "code_reviewer") == 3


def test_the_shipped_default_resolves_and_is_pinned_with_the_quota_block():
    resolved, _warnings = config.resolve(None, files={"user": None, "repo": None})

    assert resolved["quota"]["reserve_percent_by_role"] == {"executor": 20, "reviewer": 5}
    assert resolved["quota"]["reserve_percent"] == 5


def test_a_user_tier_role_key_overrides_only_its_family():
    files = {"user": "quota:\n  reserve_percent: 7\n  reserve_percent_by_role:\n    executor: 10\n", "repo": None}
    resolved, _warnings = config.resolve(None, files=files)

    assert config.quota_reserve_percent(resolved, "executor") == 10
    assert config.quota_reserve_percent(resolved, "plan_reviewer") == 5
    assert config.quota_reserve_percent(resolved, "code_reviewer") == 5


def test_a_non_mapping_role_block_falls_back_to_quota_reserve_percent():
    assert config.quota_reserve_percent({"quota": {"reserve_percent": 7, "reserve_percent_by_role": 20}}, "executor") == 7
    assert config.quota_reserve_percent({"quota": {"reserve_percent": 7, "reserve_percent_by_role": ["executor"]}},
                                        "executor") == 7


def test_route_role_hands_each_role_its_own_reserve_to_the_router(tmp_path, monkeypatch):
    """Production wiring: candidates.route_role builds the router's policy from the config's per-role reserve."""
    con = db.connect(tmp_path / "runs.db")
    con.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('R1','Change','open','{}')")
    shipped, _warnings = config.resolve(None, files={"user": None, "repo": None})
    seen = {}
    monkeypatch.setattr(candidates, "build_candidates", lambda *a, **kw: ([], []))
    monkeypatch.setattr("office.benchmarks.apply", lambda run, cands: {})

    def capture(request):
        seen[request["role"]] = request
        return {"status": "no_candidate"}

    monkeypatch.setattr("office.routing.route", capture)
    try:
        for role in ("executor", "plan_reviewer"):
            candidates.route_role(con, shipped, {"id": "R1"}, role, probe=False)
    finally:
        con.close()

    assert seen["executor"]["policy"]["quota_reserve_percent"] == 20
    assert seen["plan_reviewer"]["policy"]["quota_reserve_percent"] == 5


def test_the_pinned_reserve_notice_names_the_role_and_its_value():
    run = {"policy": {"quota": {"reserve_percent": 5, "reserve_percent_by_role": {"executor": 20, "reviewer": 5}}}}

    notice = candidates.protected_quota_remedy(run, "executor", "T1")

    assert "the executor quota reserve is pinned at 20%" in notice, notice
    reviewer_notice = candidates.protected_quota_remedy(run, "plan_reviewer")
    assert "the plan_reviewer quota reserve is pinned at 5%" in reviewer_notice, reviewer_notice


def _stored_run(con, policy):
    """A run row as resume reads it: the pinned policy goes through the runs table's JSON column."""
    from office import state
    from office.util import dumps
    con.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('R2','Change','open','{}')")
    con.execute("UPDATE runs SET policy_json=? WHERE id='R2'", (dumps(policy),))
    return state._run(con.execute("SELECT * FROM runs WHERE id='R2'").fetchone())


def test_a_run_pinned_with_the_per_role_keys_keeps_them_across_resume(tmp_path):
    shipped, _warnings = config.resolve(None, files={"user": None, "repo": None})
    con = db.connect(tmp_path / "runs.db")
    try:
        run = _stored_run(con, {"quota": shipped["quota"]})
    finally:
        con.close()

    assert config.quota_reserve_percent(run["policy"], "executor") == 20
    assert config.quota_reserve_percent(run["policy"], "plan_reviewer") == 5


def test_a_run_pinned_before_the_key_existed_keeps_the_base_reserve(tmp_path):
    con = db.connect(tmp_path / "runs.db")
    try:
        run = _stored_run(con, {"quota": {"reserve_percent": 5}})
    finally:
        con.close()

    assert config.quota_reserve_percent(run["policy"], "executor") == 5


def test_an_existing_user_config_without_the_key_keeps_shipped_role_reserves_and_its_own_base():
    files = {"user": "quota:\n  reserve_percent: 7\n", "repo": None}
    resolved, warnings = config.resolve(None, files=files)

    assert warnings == []
    assert config.quota_reserve_percent(resolved, "executor") == 20
    assert config.quota_reserve_percent(resolved, "plan_reviewer") == 5
    assert config.quota_reserve_percent(resolved, "visual_reviewer") == 7


def test_queue_admission_holds_executor_work_at_13_percent_left_under_the_executor_reserve():
    """Scheduler admission reads the executor reserve: 13% left is under the shipped 20% and over the 5% base."""
    now = datetime.now(timezone.utc)
    queued = [{"id": "run-a", "priority": "normal", "enqueued_at": now.isoformat(), "provider": "codex"}]
    quota = {"codex": {"remaining_percent": 13}}
    unmeasured = {"cpu": {"status": "unavailable", "value": None}, "ram": {"status": "unavailable", "value": None}}

    shipped_entry = scheduler.plan_admission(queued, [], unmeasured, quota, _shipped(), now)["entries"][0]
    base_entry = scheduler.plan_admission(queued, [], unmeasured, quota, {"quota": {"reserve_percent": 5}}, now)["entries"][0]

    assert shipped_entry["decision"] == "hold" and shipped_entry["quota"] == "exhausted", shipped_entry
    assert "20% reserve" in shipped_entry["reason"], shipped_entry
    assert base_entry["decision"] == "admit" and base_entry["quota"] == "ok", base_entry


def test_a_blank_role_value_in_the_user_tier_keeps_the_shipped_reserve_for_its_family():
    files = {"user": "quota:\n  reserve_percent_by_role:\n    executor:\n", "repo": None}
    resolved, _warnings = config.resolve(None, files=files)

    assert config.quota_reserve_percent(resolved, "executor") == 20
    assert config.quota_reserve_percent(resolved, "plan_reviewer") == 5


def test_a_user_quoted_executor_route_at_13_percent_is_refused_at_the_executor_reserve(monkeypatch):
    """qualify_recorded_route reads the executor reserve: 13% left is under the shipped 20%, so the reuse is refused."""
    from office import adapters, dispatch, state
    shipped = _shipped()
    cand = _candidate("codex", "gpt-6", remaining=13)
    monkeypatch.setattr(state, "pinned_config", lambda run: shipped)
    monkeypatch.setattr(candidates, "declared_decision", lambda *a, **kw: {"candidate": dict(cand)})
    monkeypatch.setattr(adapters, "load_all", lambda: {"codex": {}})
    monkeypatch.setattr(candidates, "probe_quota", lambda adapter: {})
    monkeypatch.setattr(candidates, "quota_for_candidate", lambda c, probe: {
        "status": "ok", "tightest_remaining_percent": 13, "projected_burn_percent": 0})
    route = {"candidate": dict(cand), "route_authority": {"quote": "use codex for this fix"}}

    with pytest.raises(state.Refused, match="protected reserve"):
        dispatch.qualify_recorded_route(None, {"id": "R1", "repo_root": None}, {"id": "T1"}, route)
