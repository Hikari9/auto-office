"""#254: an executor that lost its env is never bound as the orchestrator, and
config edits that cannot reach a running run are named instead of ignored."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, task_row
from office import candidates, dispatch

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
SESSION = {"OFFICE_HARNESS": "claude", "OFFICE_SESSION": "sess-exec"}


def _dispatch_external(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    t = task_row(env)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    con.execute("DELETE FROM session_bindings")  # `office start` bound the test's own session
    return d, Path(d["worktree"])


def _agent_env_command(env, d):
    ddir = env.state / "runs" / d["run_id"] / "dispatches" / d["id"]
    return f". {ddir}/agent.env && cd {d['worktree']} && office submit"


def _bindings(env):
    return env.con().execute("SELECT COUNT(*) FROM session_bindings").fetchone()[0]


# ------------------------------------------------------------------ A

@pytest.mark.approved
def test_executor_without_env_in_its_task_worktree_gets_dispatch_scoped_behavior(env):
    d, wt = _dispatch_external(env)
    # A second active run makes the repository ambiguous: only the worktree names the run.
    env.office("start", "second", "--planner", "inline", check=0)
    env.con().execute("DELETE FROM session_bindings")
    assert env.office("status", env={"HERDR_PANE_ID": ""})[0] == 3
    code, out = env.office("status", cwd=wt)
    assert code == 0 and out.startswith("T1 "), out
    (wt / "calc.py").write_text(GOOD_ADD)
    code, out = env.office("submit", cwd=wt, env={"OFFICE_JOBS": "manual"})
    assert code == 0 and "captured" in out, out
    assert _bindings(env) == 0


@pytest.mark.approved
def test_resume_from_a_task_worktree_is_refused_and_writes_no_binding(env):
    d, wt = _dispatch_external(env)
    code, out = env.office("resume", d["run_id"][:8], cwd=wt, env=SESSION)
    assert code == 4 and "executor-lost-binding" in out, out
    assert _agent_env_command(env, d) in out, out
    assert _bindings(env) == 0


@pytest.mark.approved
def test_resume_by_a_session_recorded_on_an_open_executor_dispatch_is_refused(env):
    d, _ = _dispatch_external(env)
    con = env.con()
    con.execute("UPDATE dispatches SET session_id='sess-exec' WHERE id=?", (d["id"],))
    code, out = env.office("resume", d["run_id"][:8], env=SESSION)
    assert code == 4 and "executor-lost-binding" in out and _agent_env_command(env, d) in out, out
    assert _bindings(env) == 0


@pytest.mark.approved
def test_orchestrator_submit_without_a_draft_lists_each_open_executor_recovery(env):
    d, _ = _dispatch_external(env)
    for p in (env.repo / ".office").glob("plans/*/PLAN.md"):
        p.unlink()
    code, out = env.office("submit")
    assert code == 4 and "executor-lost-binding" in out, out
    assert _agent_env_command(env, d) in out and "PLAN.md" not in out.split("next:")[0], out


@pytest.mark.approved
def test_orchestrator_resume_still_binds(env):
    d, _ = _dispatch_external(env)
    code, out = env.office("resume", d["run_id"][:8], env=SESSION | {"OFFICE_SESSION": "sess-orch"})
    assert code == 0, out
    assert env.con().execute("SELECT COUNT(*) FROM session_bindings WHERE session_id='sess-orch'").fetchone()[0] == 1


# ------------------------------------------------------------------ B

def _write_reserve(path: Path, percent: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"quota:\n  reserve_percent: {percent}\n")


def _blocked_reviewer_routing(monkeypatch):
    real = candidates.route_role

    def route(con, config, run, role, **kw):
        if role == "code_reviewer" and not kw.get("exact"):
            return {"status": "protected_quota_would_be_consumed", "rejected": [], "skipped": []}
        return real(con, config, run, role, **kw)

    monkeypatch.setattr(candidates, "route_role", route)


@pytest.mark.approved
def test_protected_quota_gate_names_review_as_and_the_pinned_reserve(env, monkeypatch):
    d, wt = _dispatch_external(env)
    _blocked_reviewer_routing(monkeypatch)
    (wt / "calc.py").write_text(GOOD_ADD)
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1", "OFFICE_ROLE": "executor"}
    env.office("submit", cwd=wt, env=wenv, check=0)
    t = task_row(env)
    assert t["status"] == "blocked", t
    reason = t["pause_reason"]
    assert "protected_quota_would_be_consumed" in reason, reason
    assert "office dispatch T1 --review-as <harness>/<model>[@effort]" in reason, reason
    pinned = json.loads(env.con().execute("SELECT policy_json FROM runs").fetchone()[0])["quota"]["reserve_percent"]
    assert f"pinned at {float(pinned):g}%" in reason and "config edits do not apply" in reason, reason


def test_route_next_for_protected_quota_names_the_remedy():
    run = {"policy": {"quota": {"reserve_percent": 9}}}
    text = dispatch._route_next({"status": "protected_quota_would_be_consumed"}, "T4", run)
    assert "office dispatch T4 --as" in text and "--review-as" in text and "pinned at 9%" in text, text
    assert "explicit user authority" in text


@pytest.mark.parametrize("where", ["user", "repo"])
def test_status_warns_when_quota_config_drifted_since_run_start(env, where):
    user = env.tmp / "user-config.yaml"
    repo = env.repo / ".auto-office" / "config.yaml"
    _write_reserve(user, 7)
    approved_run(env)
    code, out = env.office("status")
    assert code == 0 and "config edited" not in out, out
    _write_reserve(user if where == "user" else repo, 3)
    code, out = env.office("status")
    assert "config edited since run start; not applied to running run" in out, out
    assert "quota.reserve_percent pinned 7" in out and "live 3" in out, out
    code, out = env.office("doctor")
    assert "config edited since run start" in out, out


# ------------------------------------------------------------------ review follow-ups

@pytest.mark.approved
def test_stale_orchestrator_binding_of_an_executor_session_is_repaired_not_rebound(env):
    d, wt = _dispatch_external(env)
    env.office("start", "second", "--planner", "inline", check=0)
    # What an older `office resume` left behind: the executor's session bound as orchestrator.
    con = env.con()
    con.execute("DELETE FROM session_bindings")
    con.execute("INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) VALUES(?,?,?,?,?)",
                ("claude", "sess-exec", d["run_id"], "2026-01-01T00:00:00Z", "resume"))
    code, out = env.office("status", cwd=wt, env=SESSION)
    assert code == 0 and out.startswith("T1 "), out
    assert con.execute("SELECT COUNT(*) FROM session_bindings WHERE session_id='sess-exec' AND ended_at IS NULL").fetchone()[0] == 0
    # A session id recorded on the open dispatch wins from any cwd, too.
    con.execute("UPDATE dispatches SET session_id='sess-exec' WHERE id=?", (d["id"],))
    con.execute("INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) VALUES(?,?,?,?,?) "
                "ON CONFLICT(harness, session_id) DO UPDATE SET ended_at=NULL",
                ("claude", "sess-exec", d["run_id"], "2026-01-01T00:00:00Z", "resume"))
    code, out = env.office("status", env=SESSION)
    assert code == 0 and out.startswith("T1 "), out
    assert con.execute("SELECT COUNT(*) FROM session_bindings WHERE session_id='sess-exec' AND ended_at IS NULL").fetchone()[0] == 0


def test_start_overrides_are_not_drift_but_file_edits_are(env):
    user = env.tmp / "user-config.yaml"
    _write_reserve(user, 15)
    env.trust()
    env.script()
    env.office("start", "g", "--planner", "inline", "--set", "quota.reserve_percent=7", check=0)
    code, out = env.office("status")
    assert code == 0 and "config edited" not in out, out
    code, out = env.office("doctor")
    assert "config edited" not in out, out
    _write_reserve(user, 20)
    code, out = env.office("status")
    assert "config edited since run start" in out, out


def test_runs_without_recorded_file_blocks_never_warn(env):
    _write_reserve(env.tmp / "user-config.yaml", 7)
    approved_run(env)
    con = env.con()
    pol = json.loads(con.execute("SELECT policy_json FROM runs").fetchone()[0])
    pol.pop("_file_blocks_at_start")
    con.execute("UPDATE runs SET policy_json=?", (json.dumps(pol),))
    _write_reserve(env.tmp / "user-config.yaml", 3)
    code, out = env.office("status")
    assert code == 0 and "config edited" not in out, out
