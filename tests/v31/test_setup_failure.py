"""A worktree setup that fails the same way every time stops the dispatch before the executor starts.

Exit 127 (command not found) repeats in every new worktree, so launching the agent without its
dependencies only burns a session. Every other setup failure keeps the notice and still launches.

Also here: `office doctor` and the SessionStart hook on `.office/active/<run>` pointers whose run is
missing or already terminal."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import GOOD_ADD, approved_run, start_inline
from office import worktree_setup

pytestmark = pytest.mark.integration

EXECUTOR = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]
REVIEW = [{"reply": "VERDICT: APPROVED\nNEXT proceed"}]
EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _config(env, setup):
    (env.repo / ".gitignore").write_text("node_modules/\n.office/\n")
    env.git("add", ".gitignore")
    env.git("commit", "-qm", "ignore deps")
    (env.repo / ".auto-office").mkdir(exist_ok=True)
    (env.repo / ".auto-office" / "config.yaml").write_text(f"worktree:\n  setup: {json.dumps(setup)}\n")


def _rows(env, sql):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def test_command_not_found_stops_the_dispatch_before_the_executor_starts(env):
    _config(env, "office-no-such-tool --install")
    approved_run(env, executor=EXECUTOR, convergence_reviewer=REVIEW)
    code, out = env.office("dispatch", "T1", env=EXTERNAL)
    task = _rows(env, "SELECT status, pause_reason FROM tasks WHERE id='T1'")[0]
    assert task["status"] == "blocked", (task, out)
    reason = task["pause_reason"]
    assert "office-no-such-tool --install" in reason and "exited 127" in reason, reason
    assert "office doctor" in reason and "not started" in reason, reason
    dispatches = _rows(env, "SELECT status, terminal_classification, ended_at FROM dispatches WHERE kind='executor'")
    assert len(dispatches) == 1, dispatches  # stopped at once: no relaunch of a failure that repeats
    d = dispatches[0]
    assert d["ended_at"] and d["terminal_classification"] == "setup_failed" and d["status"] == "failed", d
    assert not _rows(env, "SELECT 1 FROM events WHERE kind='task.relaunch'")
    # No agent was launched: neither the brief nor a launch record exists.
    ddir = next(env.state.glob("**/dispatches/*"))
    assert not (ddir / "brief.md").exists() and not (ddir / "launch.json").exists()
    assert json.loads((ddir / "setup.json").read_text())["exit"] == 127
    blocked = [e["summary"] for e in _rows(env, "SELECT summary FROM events WHERE kind='task.blocked'")]
    assert len(blocked) == 1 and "office-no-such-tool --install" in blocked[0] and "was not started" in blocked[0], blocked
    assert "start a new run" in reason  # worktree.setup is pinned to the run


def test_other_setup_failures_still_launch_with_the_notice(env):
    _config(env, "echo boom-line; exit 3")
    approved_run(env, executor=EXECUTOR, convergence_reviewer=REVIEW)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    assert _rows(env, "SELECT status FROM tasks WHERE id='T1'")[0]["status"] != "blocked"
    ddir = next(env.state.glob("**/dispatches/*"))
    assert (ddir / "brief.md").exists()
    assert [e for e in _rows(env, "SELECT summary FROM events WHERE kind='launch'") if "worktree setup failed" in e["summary"]]


def test_only_exit_127_without_a_timeout_is_deterministic():
    assert worktree_setup.deterministic_failure({"exit": 127, "timed_out": False})
    assert not worktree_setup.deterministic_failure({"exit": 126})
    assert not worktree_setup.deterministic_failure({"exit": 1})
    assert not worktree_setup.deterministic_failure({"exit": 127, "timed_out": True})


# ---- stale active-run pointers

GHOST = "deadbeef-00000000000000000000000000000000"


def _hook(env):
    payload = json.dumps({"session_id": "s1", "cwd": str(env.repo), "source": "startup"})
    proc = subprocess.run([sys.executable, "-m", "office", "hook", "SessionStart", "--harness", "claude", "--office-managed"],
                          input=payload, capture_output=True, text=True, cwd=env.repo)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _run_ids(env):
    return [r["id"] for r in _rows(env, "SELECT id FROM runs ORDER BY created_at")]


def _pointer(env, run_id, phase="planning"):
    path = env.repo / ".office" / "active" / run_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{phase}\tgoal\n")
    return path


def test_doctor_reports_pointers_whose_run_is_missing_or_terminal(env):
    start_inline(env)
    live, = _run_ids(env)
    assert (env.repo / ".office" / "active" / live).exists()
    ghost = _pointer(env, GHOST)
    ended = _pointer(env, "feedface-11111111111111111111111111111111")
    con = env.con()
    con.execute("INSERT INTO runs(id, family_id, created_at, status, office_version, goal, phase, state_dir, policy_json, "
                "gates_json, requirements_version, plan_version, routing_version, terminal_at, terminal_reason, updated_at) "
                "SELECT ?, family_id, created_at, 'closed', office_version, goal, 'closed', state_dir, policy_json, gates_json, "
                "requirements_version, plan_version, routing_version, created_at, 'done', updated_at FROM runs WHERE id=?",
                ("feedface-11111111111111111111111111111111", live))
    con.commit()
    code, out = env.office("doctor")
    assert f"active run pointer deadbeef: stale (run missing)" in out, out
    assert "active run pointer feedface: stale (run closed)" in out, out
    assert f"active run pointer {live[:8]}" not in out
    from office import doctor, hooks
    con = env.con()
    assert len(hooks.stale_pointers(con, env.repo)) == 2
    assert ghost.exists() and ended.exists()  # reporting alone removes nothing
    code, out = env.office("doctor", "--fix")
    assert "stale (run missing) - removed" in out, out
    assert not ghost.exists() and not ended.exists() and (env.repo / ".office" / "active" / live).exists()
    code, out = env.office("doctor")
    assert "active run pointer" not in out, out


def test_session_start_ignores_stale_pointers_when_counting_active_runs(env):
    start_inline(env)
    live, = _run_ids(env)
    for p in (env.repo / ".office" / "sessions").glob("*.json"):
        p.unlink()
    assert f"Active Office run {live[:8]}" in _hook(env)
    _pointer(env, GHOST)
    _pointer(env, "feedface-11111111111111111111111111111111", phase="closed")
    out = _hook(env)
    assert f"Active Office run {live[:8]}" in out and "active Office runs here" not in out, out


def test_session_start_says_nothing_when_every_pointer_is_stale(env):
    start_inline(env)
    live, = _run_ids(env)
    for p in (env.repo / ".office" / "sessions").glob("*.json"):
        p.unlink()
    (env.repo / ".office" / "active" / live).unlink()
    _pointer(env, GHOST)
    assert _hook(env) == ""
    con = env.con()
    con.execute("UPDATE runs SET phase='abandoned' WHERE id=?", (live,))  # the db says it ended; the pointer lags
    con.commit()
    _pointer(env, live)
    assert _hook(env) == ""


def test_doctor_counts_a_stale_pointer_as_a_problem_until_fixed(env):
    from office import doctor
    from office.result import Result
    start_inline(env)
    _pointer(env, GHOST)
    assert doctor._stale_active_pointers(Result(), env.repo, fix=False) == 1
    assert doctor._stale_active_pointers(Result(), env.repo, fix=True) == 0
    assert doctor._stale_active_pointers(Result(), env.repo, fix=False) == 0


def test_doctor_fix_touches_only_pointers(env, tmp_path):
    """A stray file, a symlink, or a symlinked `active` directory is not a pointer and is never removed."""
    start_inline(env)
    active = env.repo / ".office" / "active"
    stray = active / "notes.txt"
    stray.write_text("mine\n")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "deadbeef-00000000000000000000000000000001").write_text("keep\n")
    (active / "feedface-22222222222222222222222222222222").symlink_to(victim / "deadbeef-00000000000000000000000000000001")
    env.office("doctor", "--fix")
    assert stray.exists() and (victim / "deadbeef-00000000000000000000000000000001").exists()
    real = active
    moved = env.repo / ".office" / "active-real"
    real.rename(moved)
    real.symlink_to(victim)
    env.office("doctor", "--fix")
    assert (victim / "deadbeef-00000000000000000000000000000001").exists()


def test_doctor_does_not_believe_a_missing_run_in_an_empty_db(env):
    """An empty runs.db is more likely another data home than a repository without runs."""
    from office import doctor
    from office.result import Result
    start_inline(env)
    live, = _run_ids(env)
    con = env.con()
    con.execute("DELETE FROM runs")
    con.commit()
    assert (env.repo / ".office" / "active" / live).exists()
    res = Result()
    assert doctor._stale_active_pointers(res, env.repo, fix=True) == 1
    assert (env.repo / ".office" / "active" / live).exists() and "left alone" in res.lines[0], res.lines


def test_session_start_keeps_a_legacy_run_and_fails_open_on_an_unreadable_db(env):
    start_inline(env)
    live, = _run_ids(env)
    for p in (env.repo / ".office" / "sessions").glob("*.json"):
        p.unlink()
    legacy = env.tmp / "legacy-run"
    legacy.mkdir()
    (legacy / "state.json").write_text(json.dumps({"phase": "executing"}))
    refs = env.repo / ".office" / "runs"
    refs.mkdir()
    (refs / "abcd1234.ref").write_text(str(legacy))
    _pointer(env, GHOST)
    out = _hook(env)
    assert "2 active Office runs here" in out, out  # the live run and the legacy one; the ghost is ignored
    (env.data / "runs.db").write_bytes(b"not a database")
    out = _hook(env)
    assert "3 active Office runs here" in out, out  # unverifiable: every pointer is kept
    (env.repo / ".office" / "active" / live).write_text("closed\tgoal\n")
    assert "2 active Office runs here" in _hook(env)  # a pointer that says closed is ignored without the db
