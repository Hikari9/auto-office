"""#809 R11: who executes a task. XS inline by default; S only on a measured cost-to-success estimate that beats
dedicated dispatch; M only when single-task with a measured cost advantage; L and XL never. The owner, reasoning, estimated savings and
outcome are persisted on the task and shown. An inline task keeps its lease, scope, self-review subagents,
convergence review and resume."""
import json
import subprocess
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, PLAN_TWO, start_inline, write_clean_ledger
from test_convergence_contract import APPROVED, _gates, _q, _roles

from office import briefs, state, task_descriptors, task_sizing

LOW = {"classification": "low"}
EST = "inline=14 dedicated=22 minutes: reuses the loaded session and skips an eight minute brief and spin-up"


def _desc(size="XS", **extra):
    return {"task_size": size, **extra}


# ------------------------------------------------------------------ the decision

def test_xs_is_inline_by_default_for_low_risk_bounded_work():
    mode = task_sizing.execution_mode("XS", _desc("XS"), LOW)
    assert (mode["mode"], mode["owner"]) == ("inline", "orchestrator") and mode["why"]
    assert task_sizing.execution_mode("XS", _desc("S"), LOW)["mode"] == "inline"
    assert task_sizing.execution_mode("XS", _desc("M"), LOW)["mode"] == "delegated"
    assert task_sizing.execution_mode("XS", _desc("XS"), {"classification": "elevated"})["mode"] == "delegated"


def test_s_is_inline_only_on_a_measured_estimate_that_beats_dedicated_dispatch():
    assert task_sizing.execution_mode("S", _desc("S"), LOW)["mode"] == "delegated"
    assert task_sizing.execution_mode("S", _desc("S", inline_advantage="it is much cheaper"), LOW)["mode"] == "delegated"
    mode = task_sizing.execution_mode("S", _desc("S", inline_advantage=EST), LOW)
    assert mode["mode"] == "inline" and mode["estimated_savings"]["savings"] == 8 and "14" in mode["why"]
    worse = "inline=30 dedicated=22 minutes: retries and a second review round dominate the total"
    refused = task_sizing.execution_mode("S", _desc("S", inline_advantage=worse), LOW)
    assert refused["mode"] == "delegated" and "does not beat" in refused["why"]
    assert task_sizing.execution_mode("S", _desc("S", inline_advantage=EST), {"classification": "elevated"})["mode"] == "delegated"


@pytest.mark.parametrize("gear", ["S", "M"])
def test_equal_or_dearer_inline_cost_never_runs_inline(gear):
    for est in ("inline=22 dedicated=22 minutes: tie", "inline=22.00001 dedicated=22 minutes: dearer",
                "inline=1.00001 dedicated=1.00002 minutes: a saving that rounds to zero"):
        mode = task_sizing.execution_mode(gear, _desc("S", inline_advantage=est), LOW, task_count=1)
        assert mode["mode"] == "delegated", est


def test_m_is_inline_only_for_a_single_task_run_and_an_estimate_is_not_enough():
    assert task_sizing.execution_mode("M", _desc("M"), LOW, task_count=1)["mode"] == "delegated"
    good = task_sizing.execution_mode("M", _desc("M", inline_advantage=EST), LOW, task_count=1)
    assert good["mode"] == "inline" and good["estimated_savings"]["savings"] == 8
    bad = "inline=24 dedicated=22 minutes: measured retry overhead"
    assert task_sizing.execution_mode("M", _desc("M", inline_advantage=bad), LOW, task_count=1)["mode"] == "delegated"
    assert task_sizing.execution_mode("M", _desc("M", inline_advantage="not measured"), LOW, task_count=1)["mode"] == "delegated"
    assert task_sizing.execution_mode("M", _desc("M"), LOW, task_count=2)["mode"] == "delegated"
    assert task_sizing.execution_mode("M", _desc("M"), LOW)["mode"] == "delegated"
    assert task_sizing.execution_mode("M", _desc("M", inline_advantage=EST), LOW, task_count=2)["mode"] == "delegated"


@pytest.mark.parametrize("gear", ["L", "XL", "express", None])
def test_l_xl_and_legacy_gears_never_run_inline(gear):
    assert task_sizing.execution_mode(gear, _desc("XS", inline_advantage=EST), LOW, task_count=1)["mode"] == "delegated"


def test_the_estimate_must_be_a_structured_measurement():
    assert task_sizing.inline_estimate(EST)["savings"] == 8
    for bad in (None, "", "cheaper", "inline=1 dedicated=2", "inline=a dedicated=2 min: x",
                "inline=0 dedicated=22 minutes: free", "inline=0.0 dedicated=22 minutes: free",
                "inline=5 dedicated=0 minutes: free", "inline=-1 dedicated=22 minutes: negative",
                "inline=1e1 dedicated=22 minutes: exponent", "inline=NaN dedicated=22 minutes: nan",
                f"inline={'9' * 400} dedicated={'9' * 400} minutes: both overflow to inf",
                f"inline=5 dedicated={'9' * 400} minutes: overflow to inf"):
        assert task_sizing.inline_estimate(bad) is None, bad
    assert task_sizing.inline_estimate("inline=0.5 dedicated=22 minutes: a measured half minute")["savings"] == 21.5
    assert task_descriptors.parse_field("inline_advantage", EST) == EST
    for bad in ("it is cheaper", EST + "\x1b[31m"):
        with pytest.raises(ValueError):
            task_descriptors.parse_field("inline_advantage", bad)


def test_the_executor_brief_states_what_the_runtime_reviews():
    run = {"gear": "S", "gates": {"gear_model_version": "run-gear-1"}}
    assert "one independent self-review subagent" in " ".join(briefs.runtime_review_lines(run))
    assert "up to four independent specialist" in " ".join(briefs.runtime_review_lines({**run, "gear": "XL"}))
    assert briefs.runtime_review_lines({**run, "gear": "XS"}) == []
    assert briefs.runtime_review_lines({"gear": "S", "gates": {}}) == []


# ------------------------------------------------------------------ through dispatch

def _xs_plan():
    return PLAN_ONE.replace("visual: none", "visual: none\ntask_size: XS\n"
                            "task_size_rationale: bounded local change with no external interfaces\n", 1)


def _m_plan(estimate=EST):
    return PLAN_ONE.replace("visual: none", "visual: none\ntask_size: M\n"
                            "task_size_rationale: bounded one-task implementation with no shared interfaces\n"
                            f"inline_advantage: {estimate}\n", 1)


def _row(env, tid="T1"):
    con = env.con()
    try:
        return dict(con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    finally:
        con.close()


def _mode(env):
    return json.loads(_row(env)["route_json"])["execution_mode"]


def _approved(env, plan, gear):
    env.trust()
    start_inline(env, plan=plan, gear=gear)
    env.office("approve", "plan", "--quote", "approved", check=0)


def _inline_xs(env):
    _approved(env, _xs_plan(), "XS")
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    return out


def test_xs_dispatch_prepares_a_fenced_inline_worker_and_persists_the_record(env):
    out = _inline_xs(env)
    assert "orchestrator-inline lease/worktree prepared" in out and "no model launched" in out
    assert "EXECUTION T1: inline (owner orchestrator)" in out
    con = env.con()
    d = con.execute("SELECT * FROM dispatches WHERE role='executor'").fetchone()
    assert json.loads(d["route_json"])["execution_mode"]["mode"] == "inline"
    lease = con.execute("SELECT * FROM leases WHERE dispatch_id=?", (d["id"],)).fetchone()
    assert lease is not None and lease["released_at"] is None, "an inline task holds its lease"
    assert json.loads(d["override_json"])["inline"] is True
    mode = _mode(env)
    assert mode["owner"] == "orchestrator" and mode["why"] and "outcome" not in mode
    assert "executor" not in _roles(env), "no executor model is launched"


def test_the_diagram_shows_owner_reason_and_outcome(env):
    _inline_xs(env)
    code, out = env.office("inspect", "plan")
    assert code == 0 and "execution: inline (owner orchestrator)" in out
    assert "orchestrator-inline (no model launched)" in out
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    state.update_task(con, run["id"], "T1", status="accepted")
    con.commit()
    mode = _mode(env)
    assert mode["outcome"]["status"] == "accepted" and mode["owner"] == "orchestrator"
    code, out = env.office("inspect", "plan")
    assert "outcome accepted" in out


def test_an_s_run_with_a_measured_estimate_dispatches_inline_and_shows_the_savings(env):
    body = PLAN_ONE.replace("visual: none", "visual: none\ntask_size: S\ntask_size_rationale: one bounded file, no interfaces\n"
                            f"inline_advantage: {EST}\n", 1)
    _approved(env, body, "S")
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "EXECUTION T1: inline" in out and "estimated savings 8 minutes" in out, out
    assert _mode(env)["estimated_savings"]["savings"] == 8


def test_a_single_task_s_run_without_an_estimate_is_delegated(env):
    _approved(env, PLAN_ONE, "S")
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "orchestrator-inline" not in out
    assert _mode(env)["mode"] == "delegated"


def test_m_without_measured_advantage_delegates_at_dispatch(env):
    _approved(env, PLAN_ONE, "M")
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "orchestrator-inline lease/worktree prepared" not in out, out
    assert _mode(env)["mode"] == "delegated" and "measured" in _mode(env)["why"]


def test_m_with_measured_advantage_records_savings_at_dispatch(env):
    _approved(env, _m_plan(), "M")
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "EXECUTION T1: inline" in out and "estimated savings 8 minutes" in out, out
    assert _mode(env)["estimated_savings"]["savings"] == 8


def test_m_with_a_non_beneficial_estimate_delegates_at_dispatch(env):
    _approved(env, _m_plan("inline=24 dedicated=22 minutes: retries and a second review round cost more than a launch"), "M")
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "orchestrator-inline lease/worktree prepared" not in out, out
    assert _mode(env)["mode"] == "delegated" and "does not beat" in _mode(env)["why"]


@pytest.mark.parametrize("bad", ["inline=0 dedicated=22 minutes: free", "inline=-3 dedicated=22 minutes: negative",
                                 "cheaper than dedicated", "inline=14 dedicated=22"])
def test_a_plan_whose_m_estimate_is_unmeasured_is_refused_and_never_runs_inline(env, bad):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", "M", "--planner", "inline")
    assert code == 0, out
    env.write_plan(_m_plan(bad))
    code, out = env.office("submit")
    assert code != 0 and "inline_advantage" in out, out
    assert _q(env, "SELECT COUNT(*) n FROM dispatches")[0]["n"] == 0


def test_a_relaunch_of_an_m_inline_task_keeps_its_measured_estimate(env):
    _approved(env, _m_plan(), "M")
    env.office("dispatch", "T1", check=0)
    before = _mode(env)
    env.office("revoke", "T1", check=0)  # the first inline worker ends; the relaunch gets a new fenced lease
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "orchestrator-inline lease/worktree prepared" in out, out
    after = _mode(env)
    assert after["mode"] == "inline" and after["why"] == before["why"]
    assert after["estimated_savings"] == before["estimated_savings"] and after["estimated_savings"]["savings"] == 8


def test_a_relaunch_of_a_delegated_m_task_never_becomes_inline(env):
    _approved(env, PLAN_ONE, "M")
    env.office("dispatch", "T1", check=0)
    assert _mode(env)["mode"] == "delegated"
    env.office("revoke", "T1", check=0)
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "orchestrator-inline" not in out, out
    assert _mode(env)["mode"] == "delegated"


def test_m_with_two_tasks_is_delegated(env):
    _approved(env, PLAN_TWO, "M")
    env.office("dispatch", "T1", check=0)
    assert _mode(env)["mode"] == "delegated" and "single-task" in _mode(env)["why"]


def test_an_explicit_launch_override_beats_inline(env):
    _approved(env, _xs_plan(), "XS")
    code, out = env.office("dispatch", "T1", "--external")
    assert code == 0 and "orchestrator-inline" not in out
    assert _mode(env)["mode"] == "delegated" and "override" in _mode(env)["why"]


def test_an_inline_task_keeps_checks_self_review_convergence_and_the_lease(env):
    env.script(convergence_reviewer=[{"reply": APPROVED}], code_reviewer=[{"reply": APPROVED}])
    _approved(env, _m_plan(), "M")  # one task with measured cost advantage
    env.office("dispatch", "T1", check=0)
    assert _mode(env)["mode"] == "inline"
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE role='executor'").fetchone())
    wt = d["worktree"]
    (Path(wt) / "calc.py").write_text(GOOD_ADD)
    subprocess.run(["git", "-C", wt, "add", "calc.py"], check=True)
    subprocess.run(["git", "-C", wt, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "work"], check=True)
    write_clean_ledger(wt)
    code, out = env.office("submit", cwd=wt, env={"OFFICE_RUN_ID": d["run_id"], "OFFICE_TASK_ID": "T1",
                                                  "OFFICE_DISPATCH_ID": d["id"]})
    assert code == 0, out
    roles = _roles(env)
    assert roles.count("code_reviewer") == 1, "the M gear's self-review subagent runs for an inline task too"
    assert roles.count("convergence_reviewer") == 1 and "executor" not in roles
    (conv,) = _gates(env, "convergence_review")
    assert conv["verdict"] == "APPROVED"
    assert _mode(env)["outcome"]["status"] == "accepted"


def test_a_scope_violation_by_an_inline_worker_is_still_refused(env):
    _approved(env, _m_plan(), "M")
    env.office("dispatch", "T1", check=0)
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE role='executor'").fetchone())
    wt = d["worktree"]
    (Path(wt) / "calc.py").write_text(GOOD_ADD)
    (Path(wt) / "other.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", wt, "add", "-A"], check=True)
    subprocess.run(["git", "-C", wt, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "work"], check=True)
    write_clean_ledger(wt)
    code, out = env.office("submit", cwd=wt, env={"OFFICE_RUN_ID": d["run_id"], "OFFICE_TASK_ID": "T1",
                                                  "OFFICE_DISPATCH_ID": d["id"]})
    assert code != 0 and "other.py" in out, out
    assert _row(env)["status"] not in ("accepted",)


def test_a_relaunch_of_an_inline_task_stays_inline_with_its_reason(env):
    _inline_xs(env)
    before = _mode(env)
    env.office("revoke", "T1", check=0)  # the first inline worker ends; the relaunch gets a new fenced lease
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "orchestrator-inline lease/worktree prepared" in out, out
    after = _mode(env)
    assert after["mode"] == "inline" and after["why"] == before["why"] and after["owner"] == "orchestrator"
