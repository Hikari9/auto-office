"""A reviewer routes from the run's pinned policy as stored now, not the snapshot the
calling job loaded (2026-11-02): one convergence job runs every self-review subagent in
turn, so a re-pin made meanwhile (office config --apply-routing) must reach the next one."""
import json

from test_convergence_contract import APPROVED, _start

from office import candidates, gates, state


def test_run_reviewer_routes_from_the_current_pin_not_the_jobs_snapshot(env, monkeypatch):
    _start(env, gear="S", executor=[], plan_reviewer=[{"reply": APPROVED}])
    con = env.con()
    rid = con.execute("SELECT id FROM runs").fetchone()[0]
    stale = state.get_run(con, rid)
    policy = dict(stale["policy"])
    roles = json.loads(json.dumps(policy.get("roles") or {}))
    roles.setdefault("code_reviewer", {})["preferred_seed"] = [
        {"harness": "codex", "model_id": "gpt-6-luna", "effort": "high"}]
    policy["roles"] = roles
    with con:
        state.update_run(con, rid, policy=policy)
    seen = []

    def capture(con_, config, run, role, **kw):
        seen.append(((run.get("policy") or {}).get("roles") or {}).get("code_reviewer", {}).get("preferred_seed"))
        return {"status": "no_candidate", "rejected": [], "skipped": []}

    monkeypatch.setattr(candidates, "route_role", capture)
    gates.run_reviewer(con, stale, {"id": "Gtest0001", "task_id": None}, "code_reviewer", "brief", cwd=env.repo)
    assert seen and seen[0] == roles["code_reviewer"]["preferred_seed"], seen
