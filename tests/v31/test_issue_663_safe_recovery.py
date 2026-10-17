"""#663: a quoted re-pin clears roles file drift and evidence-blocked gates remain recoverable."""
from copy import deepcopy

from office import config as cfg, convergence, contract, db, state


def test_apply_routing_reconciles_a_stale_raw_roles_baseline(env):
    code, out = env.office("start", "visual review", "--planner", "inline")
    assert code == 0, out
    con = env.con()
    rid = con.execute("SELECT id FROM runs").fetchone()[0]
    run = state.get_run(con, rid)
    policy = deepcopy(run["policy"])
    assert cfg.FILE_BLOCKS_KEY in policy
    policy[cfg.FILE_BLOCKS_KEY].setdefault("user", {})["roles"] = {
        "visual_reviewer": {"preferred_seed": ["stale"]}}
    with db.transaction(con):
        state.update_run(con, rid, policy=policy)
    assert "roles differ" in cfg.config_drift(state.get_run(con, rid))
    code, out = env.office("config", "--run", rid[:8], "--apply-routing",
                           "--quote", "apply current visual reviewer routing")
    assert code == 0 and "re-pinned roles" in out, out
    assert cfg.config_drift(state.get_run(con, rid)) is None
    assert con.execute("SELECT 1 FROM events WHERE kind='run.routing_applied'").fetchone()


def test_visual_evidence_blocked_is_eligible_for_review_only_or_inspected_fallback(monkeypatch):
    run = {"id": "r"*32}
    gate = {"id": "G1", "kind": "visual", "status": "done",
            "review_status": contract.EVIDENCE_BLOCKED, "evidence_status": "CAPTURE_BLOCKED",
            "cycle": 1}
    monkeypatch.setattr(convergence.state, "get_run", lambda *a: run)
    monkeypatch.setattr(convergence, "scope_state",
                        lambda *a: {"commit": "a"*40, "cycle": 1, "status": "evidence_blocked"})
    monkeypatch.setattr(convergence, "_latest", lambda *a: {"visual": gate})
    monkeypatch.setattr(convergence, "_satisfied", lambda *a: False)
    assert convergence._unreviewed_gates(object(), run, "L-T1") == [gate]
    assert convergence.fallback_gate(object(), run, "L-T1", "visual") == gate


def test_visual_spec_defect_is_not_an_orchestrator_fallback(monkeypatch):
    run = {"id": "r"*32}
    gate = {"id": "G1", "kind": "visual", "status": "done",
            "review_status": contract.EVIDENCE_BLOCKED,
            "evidence_status": convergence.SPEC_DEFECT, "cycle": 1}
    monkeypatch.setattr(convergence.state, "get_run", lambda *a: run)
    monkeypatch.setattr(convergence, "scope_state",
                        lambda *a: {"commit": "a"*40, "cycle": 1, "status": "evidence_blocked"})
    monkeypatch.setattr(convergence, "_latest", lambda *a: {"visual": gate})
    monkeypatch.setattr(convergence, "_satisfied", lambda *a: False)
    assert convergence.fallback_gate(object(), run, "L-T1", "visual") is None
