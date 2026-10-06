"""#254: an executor that lost its env is never bound as the orchestrator, and
config edits that cannot reach a running run are named instead of ignored."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, PLAN_TWO, approved_run, task_row
from office import candidates, config as cfg, dispatch

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
SESSION = {"OFFICE_HARNESS": "claude", "OFFICE_SESSION": "sess-exec"}


def _dispatch_external(env):
    approved_run(env, executor=[{}], convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
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
    con.execute("UPDATE dispatches SET harness='claude', session_id='sess-exec' WHERE id=?", (d["id"],))
    code, out = env.office("resume", d["run_id"][:8], env=SESSION)
    assert code == 4 and "executor-lost-binding" in out and _agent_env_command(env, d) in out, out
    assert _bindings(env) == 0


def test_orchestrator_submit_without_a_draft_lists_each_open_executor_recovery(env):
    approved_run(env, plan=PLAN_TWO, executor=[{}, {}])
    env.office("dispatch", "T1", "T2", "--parallel", env=EXTERNAL, check=0)
    con = env.con()
    ds = [dict(con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env, t)["current_dispatch_id"],)).fetchone())
          for t in ("T1", "T2")]
    assert all(d["status"] in ("launching", "running") for d in ds), ds
    con.execute("DELETE FROM session_bindings")
    for p in (env.repo / ".office").glob("plans/*/PLAN.md"):
        p.unlink()
    code, out = env.office("submit")
    assert code == 4 and "executor-lost-binding" in out, out
    assert "PLAN.md" not in out.split("next:")[0], out
    lines = out.splitlines()
    for d in ds:
        # One labelled line per dispatch, holding exactly that dispatch's command.
        mine = [ln for ln in lines if _agent_env_command(env, d) in ln]
        assert len(mine) == 1 and mine[0].strip() == f"{d['task_id']} ({d['id']}): {_agent_env_command(env, d)}", out
    # Nothing chains the commands: no `;` anywhere in the recovery block.
    assert not any(";" in ln for ln in lines if "office submit" in ln), out


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


@pytest.mark.review_contract("v3.1")  # a per-task code review gate exists only under v3.1 (no shared snapshot)
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
    assert "office dispatch T4 --as <harness>/<model>[@effort]" in text and "pinned at 9%" in text, text
    assert "--review-as" not in text and "explicit user authority" in text, text


def test_protected_quota_remedy_fits_the_role():
    run = {"policy": {"quota": {"reserve_percent": 9}}}
    code = candidates.protected_quota_remedy(run, "code_reviewer", "T4")
    assert "office dispatch T4 --review-as <harness>/<model>[@effort]" in code and "pinned at 9%" in code, code
    visual = candidates.protected_quota_remedy(run, "visual_reviewer", "T4")
    assert "office approve visual T4 --by" in visual and "--review-as" not in visual, visual
    for role in ("plan_reviewer", "integration_reviewer"):
        text = candidates.protected_quota_remedy(run, role, None)
        assert "office dispatch" not in text and "--review-as" not in text, text
        assert "wait for quota" in text and "pinned at 9%" in text and "config edits do not apply" in text, text


def test_protected_quota_plan_gate_gives_quota_guidance_not_review_as(env, monkeypatch):
    real = candidates.route_role

    def route(con, config, run, role, **kw):
        if role == "plan_reviewer":
            return {"status": "protected_quota_would_be_consumed", "rejected": [], "skipped": []}
        return real(con, config, run, role, **kw)

    monkeypatch.setattr(candidates, "route_role", route)
    env.trust()
    env.script()
    code, out = env.office("start", "g", "--gear", "express", "--planner", "inline")
    assert code == 0, out
    env.write_plan(PLAN_ONE)
    env.office("submit", check=0)
    con = env.con()
    rows = [dict(r) for r in con.execute("SELECT verdict, review_status, summary FROM gates WHERE subject='plan'").fetchall()]
    # #337: an unavailable reviewer is runtime status, never a verdict.
    assert rows and rows[-1]["verdict"] is None and rows[-1]["review_status"] == "UNAVAILABLE", rows
    summary = rows[-1]["summary"]
    pinned = json.loads(con.execute("SELECT policy_json FROM runs").fetchone()[0])["quota"]["reserve_percent"]
    assert "protected_quota_would_be_consumed" in summary and f"pinned at {float(pinned):g}%" in summary, summary
    assert "wait for quota" in summary and "--review-as" not in summary and "office dispatch" not in summary, summary


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
    live = "SELECT COUNT(*) FROM session_bindings WHERE session_id='sess-exec' AND ended_at IS NULL"
    code, out = env.office("status", cwd=wt, env=SESSION)
    assert code == 0 and out.startswith("T1 "), out
    # Nothing proves this binding is the executor's (no session recorded on the dispatch): it stays.
    assert con.execute(live).fetchone()[0] == 1
    # The dispatch's recorded harness and session id prove it: the worktree command ends it.
    con.execute("UPDATE dispatches SET harness='claude', session_id='sess-exec' WHERE id=?", (d["id"],))
    code, out = env.office("status", cwd=wt, env=SESSION)
    assert code == 0 and out.startswith("T1 "), out
    assert con.execute(live).fetchone()[0] == 0
    # A session id recorded on the open dispatch wins from any cwd, too.
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


def _make_legacy(env):
    con = env.con()
    pol = json.loads(con.execute("SELECT policy_json FROM runs").fetchone()[0])
    pol.pop("_file_blocks_at_start")
    con.execute("UPDATE runs SET policy_json=?", (json.dumps(pol),))


LEGACY_CAUSE_UNKNOWN = ("pinned values (pinned before Office recorded a baseline, so the cause is unknown: "
                        "a file edit, a --set at start, or a changed default)")


def test_legacy_run_with_an_edited_file_warns_without_claiming_a_cause(env):
    _write_reserve(env.tmp / "user-config.yaml", 7)
    approved_run(env)
    _make_legacy(env)
    _write_reserve(env.tmp / "user-config.yaml", 3)
    code, out = env.office("status")
    assert "config differs from run" in out and LEGACY_CAUSE_UNKNOWN in out, out
    assert "not applied to this run" in out and "quota.reserve_percent pinned 7, live 3" in out, out
    assert "config edited" not in out, out


def test_legacy_run_with_unchanged_config_does_not_warn(env):
    _write_reserve(env.tmp / "user-config.yaml", 7)
    approved_run(env)
    _make_legacy(env)
    code, out = env.office("status")
    assert code == 0 and "config edited" not in out and "config differs" not in out, out


def test_new_run_ignores_a_change_in_shipped_defaults_but_not_a_file_edit(env, monkeypatch):
    approved_run(env)
    changed = env.tmp / "default-config.yaml"
    text = cfg.default_config_path().read_text(encoding="utf-8")
    assert "reserve_percent" in text
    changed.write_text(re.sub(r"reserve_percent:\s*\S+", "reserve_percent: 41", text, count=1), encoding="utf-8")
    monkeypatch.setattr(cfg, "default_config_path", lambda: changed)
    code, out = env.office("status")
    assert code == 0 and "config edited" not in out and "config differs" not in out, out
    _write_reserve(env.tmp / "user-config.yaml", 3)
    code, out = env.office("status")
    assert "config edited since run start" in out, out


def test_session_id_collision_across_harnesses_is_not_an_executor(env):
    d, _ = _dispatch_external(env)
    env.con().execute("UPDATE dispatches SET harness='claude', session_id='sess-exec' WHERE id=?", (d["id"],))
    codex = {"OFFICE_HARNESS": "codex", "OFFICE_SESSION": "sess-exec"}
    code, out = env.office("resume", d["run_id"][:8], env=codex)
    assert code == 0, out
    code, out = env.office("status", env=codex)
    assert code == 0 and not out.startswith("T1 "), out


@pytest.mark.approved
def test_recovery_keeps_the_sessions_binding_to_another_run(env):
    d, wt = _dispatch_external(env)
    env.office("start", "second", "--planner", "inline", check=0)
    con = env.con()
    other = con.execute("SELECT id FROM runs WHERE id<>?", (d["run_id"],)).fetchone()[0]
    con.execute("DELETE FROM session_bindings")
    con.execute("INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) VALUES(?,?,?,?,?)",
                ("claude", "sess-exec", other, "2026-01-01T00:00:00Z", "resume"))
    con.execute("UPDATE dispatches SET harness='claude' WHERE id=?", (d["id"],))
    code, out = env.office("status", cwd=wt, env=SESSION)
    assert code == 0 and out.startswith("T1 "), out
    row = con.execute("SELECT run_id, ended_at FROM session_bindings WHERE session_id='sess-exec'").fetchone()
    assert row["run_id"] == other and row["ended_at"] is None, dict(row)


def test_legacy_run_with_a_changed_default_warns_without_claiming_an_edit(env, monkeypatch):
    approved_run(env)
    _make_legacy(env)
    pinned = json.loads(env.con().execute("SELECT policy_json FROM runs").fetchone()[0])["quota"]["reserve_percent"]
    changed = env.tmp / "default-config.yaml"
    text = cfg.default_config_path().read_text(encoding="utf-8")
    changed.write_text(re.sub(r"reserve_percent:\s*\S+", "reserve_percent: 41", text, count=1), encoding="utf-8")
    monkeypatch.setattr(cfg, "default_config_path", lambda: changed)
    code, out = env.office("status")
    assert LEGACY_CAUSE_UNKNOWN in out and "not applied to this run" in out, out
    assert f"quota.reserve_percent pinned {pinned}, live 41" in out and "config edited" not in out, out
    code, out = env.office("doctor")
    assert LEGACY_CAUSE_UNKNOWN in out and "config edited" not in out, out


def test_legacy_run_with_a_removed_pinned_key_warns(env):
    user = env.tmp / "user-config.yaml"
    _write_reserve(user, 7)
    approved_run(env)
    _make_legacy(env)
    user.write_text("{}\n")
    live = cfg.load_yaml(cfg.default_config_path())["quota"]["reserve_percent"]
    assert live != 7
    code, out = env.office("status")
    assert LEGACY_CAUSE_UNKNOWN in out and f"quota.reserve_percent pinned 7, live {live}" in out, out
    assert "config edited" not in out, out


def test_start_reads_each_config_file_once_so_pin_and_baseline_agree(env, monkeypatch):
    """An edit landing between two reads during `office start` must not pin
    one value and record another as the drift baseline."""
    user = env.tmp / "user-config.yaml"
    _write_reserve(user, 7)
    real = Path.read_text
    reads = []

    def read_text(self, *a, **kw):
        # Count the config module's reads; runs_db and new_runs lookups read
        # other keys of the same file.
        if self == user and sys._getframe(1).f_globals.get("__name__") == "office.config":
            reads.append(1)
            if len(reads) > 1:  # the edit lands after the first read
                return "quota:\n  reserve_percent: 3\n"
        return real(self, *a, **kw)

    env.trust()
    env.script()
    monkeypatch.setattr(Path, "read_text", read_text)
    code, out = env.office("start", "g", "--planner", "inline")
    monkeypatch.setattr(Path, "read_text", real)
    assert code == 0, out
    assert len(reads) == 1, reads
    pol = json.loads(env.con().execute("SELECT policy_json FROM runs").fetchone()[0])
    assert pol["quota"]["reserve_percent"] == 7
    assert pol[cfg.FILE_BLOCKS_KEY]["user"]["quota"]["reserve_percent"] == 7


@pytest.mark.approved
def test_orchestrator_keeps_its_binding_after_status_from_an_executor_worktree(env):
    d, wt = _dispatch_external(env)
    env.office("start", "second", "--planner", "inline", check=0)
    con = env.con()
    con.execute("UPDATE dispatches SET harness='claude', session_id='sess-exec' WHERE id=?", (d["id"],))
    con.execute("DELETE FROM session_bindings")
    orch = SESSION | {"OFFICE_SESSION": "sess-orch"}
    code, out = env.office("resume", d["run_id"][:8], env=orch)
    assert code == 0, out
    code, out = env.office("status", cwd=wt, env=orch)
    assert code == 0 and out.startswith("T1 "), out
    row = con.execute("SELECT run_id, ended_at FROM session_bindings WHERE session_id='sess-orch'").fetchone()
    assert row["run_id"] == d["run_id"] and row["ended_at"] is None, dict(row)
    # Back in the main checkout, with two active runs, the binding still names the run.
    code, out = env.office("status", env=orch)
    assert code == 0 and not out.startswith("T1 "), out


def test_unreadable_live_config_is_reported_not_treated_as_no_drift(env):
    user = env.tmp / "user-config.yaml"
    _write_reserve(user, 7)
    approved_run(env)
    user.write_text("quota: [unclosed\n")
    code, out = env.office("status")
    assert "live config could not be read or parsed" in out and str(user) in out, out
    assert "Error" in out.split(str(user), 1)[1] and "stays on its pinned values" in out, out
    assert "config edited" not in out and "config differs" not in out, out
