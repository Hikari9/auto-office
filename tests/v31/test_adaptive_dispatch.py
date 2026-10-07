"""#300 end to end: the plan's route slate, planner choice, dispatch fallback on fresh
quota, slate exhaustion and reroute, sticky rerun, and the inspect surfaces."""
import json

import pytest
from jsonschema import Draft202012Validator

from conftest import PLAN_ONE, ROOT, start_inline

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
PRIMARY, FB1, FB2 = "claude/claude-opus-5-5@medium", "codex/gpt-6-astra@low", "claude/claude-opus-5-5@high"
PLANNED = PLAN_ONE.replace("visual: none", f"visual: none\nroute: {PRIMARY}, {FB1}, {FB2}", 1)


def _quota(env, **remaining):
    from office import candidates
    candidates._QUOTA_CACHE.clear()
    return {**EXTERNAL, "OFFICE_QUOTA_FIXTURE": json.dumps(remaining)}


def _approve(env, plan=PLANNED):
    env.trust()
    out = start_inline(env, plan=plan)
    env.office("approve", "plan", "--quote", "approved", check=0)
    return out


def test_plan_shows_the_planner_slate_and_dispatch_follows_its_primary(env):
    out = _approve(env)
    assert "ROUTING  (planner choice)" in out and PRIMARY in out, out
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0 and "executor/claude@1/claude-opus-5-5@medium" in out, out
    con = env.con()
    row = con.execute("SELECT phase, primary_route, dispatched_route, disclosure_json FROM route_audit "
                      "WHERE task_id='T1' ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row["phase"] == "dispatch" and row["dispatched_route"] == "claude@1/claude-opus-5-5@medium"
    assert json.loads(row["disclosure_json"])["dispatch"]["source"] == "plan"


def test_fresh_quota_forces_the_recorded_fallback_and_says_why(env):
    _approve(env)
    code, out = env.office("dispatch", "T1", env=_quota(env, claude=1))
    assert code == 0 and "executor/codex@" in out and "gpt-6-astra@low" in out, out
    assert "planned fallback 1" in out and "quota" in out, out
    code, out = env.office("inspect", "route", "T1")
    assert "after fallback: claude@1/claude-opus-5-5@medium" in out, out


def test_an_exhausted_slate_returns_for_reroute_and_never_picks_an_unplanned_route(env):
    _approve(env)
    code, out = env.office("dispatch", "T1", env=_quota(env, claude=1, codex=1))
    assert code != 0 and "every planned route is unavailable" in out and "--reroute" in out, out
    code, out = env.office("dispatch", "T1", "--reroute", env=_quota(env, claude=1, codex=1))
    assert code == 0 and "rerouted from current evidence" in out, out
    assert "claude@" not in out.split("executor/")[1].split()[0] and "codex@" not in out.split("executor/")[1].split()[0]


def test_a_primary_far_from_the_best_needs_route_why(env):
    plan = PLAN_ONE.replace("visual: none", "visual: none\nroute: codex/gpt-5.6-terra@high", 1)
    out = _approve(env, plan=plan)
    assert "T1 route: " in out and "needs route_why" in out and "the ranked slate stands" in out, out
    plan = plan.replace("route: codex/gpt-5.6-terra@high",
                        "route: codex/gpt-5.6-terra@high\nroute_why: terra is the only route with the repo's VPN", 1)
    env.write_plan(plan)
    code, out = env.office("submit")
    assert code == 0, out
    code, out = env.office("inspect", "route", "T1")
    assert "planner choice: terra is the only route" in out and "planner reason:" in out, out


def test_inspect_route_json_carries_the_complete_audit(env):
    _approve(env)
    code, data = env.ojson("inspect", "route", "T1")
    assert code == 0
    audit = data["data"]["audits"][-1]["disclosure"]
    schema = json.loads((ROOT / "schemas" / "routing-decision.schema.json").read_text())
    Draft202012Validator(schema).validate(audit)
    assert audit["planner"]["chooser"] == "planner" and len(audit["slate"]) == 3
    assert {"clincher", "exploration", "evidence_digest", "numeric_order", "weights"} <= set(audit)
    assert audit["rejected"]
    assert all({"candidate", "stage", "reason"} <= set(item) for item in audit["rejected"])
    code, out = env.office("inspect", "route", "T1")
    assert out.count("PRIMARY") == 1 and "evidence (adaptive-1" in out, out


def test_legacy_single_route_records_stay_readable(env):
    _approve(env, plan=PLAN_ONE)
    con = env.con()
    con.execute("DELETE FROM route_audit")
    con.execute("UPDATE plans SET preview_json=NULL")
    con.commit()
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out
    con.execute("DELETE FROM route_audit")
    con.execute("UPDATE dispatches SET route_json=? WHERE task_id='T1'",
                (json.dumps({"selection_disclosure": {"reason": "won the balanced cost and local-evidence comparison"}}),))
    con.commit()
    code, out = env.office("inspect", "route", "T1")
    assert "single-route record" in out and "won the balanced cost" in out, out


@pytest.mark.parametrize("phase", ["closed"])
def test_learner_refresh_runs_inside_close_and_inspect_learner_reads(env, phase):
    _approve(env)
    from office import lifecycle, state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    with con:
        lifecycle._learn(con, run)
    assert not con.execute("SELECT 1 FROM events WHERE kind='learner.refresh_failed'").fetchone()
    code, out = env.office("inspect", "learner")
    assert code == 0 and out.startswith("learner route-learner-1"), out
