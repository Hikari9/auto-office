"""#913: next: names office prompt (or states delivered) while the worker is live, never office rerun.

ACCEPT:
- With the owner's worker live, `office disposition` and `office status` name
  `office prompt <T> -- "<message>"` (or state the finding is delivered to the live worker),
  never `office rerun`.
- Every printed next: in that state is a command the runtime accepts; test runs it.
"""
from __future__ import annotations

import json
import os
import shlex
import uuid

import pytest
from conftest import approved_run
from test_herdr_agent_launch import BUSY, EMPTY, _calls, _fake, _live_dispatch


def _live_herdr_worker(env, monkeypatch, *, reads=None, status="running", agent="working"):
    """Set up a herdr-hosted live worker for T1."""
    if reads is None:
        reads = [EMPTY, BUSY, BUSY, BUSY]
    state_file = _fake(env, monkeypatch, gets=[agent], reads=reads)
    for k, v in {"OFFICE_HERDR_LAND_TIMEOUT": "0", "OFFICE_HERDR_ENTER_WAIT": "0"}.items():
        monkeypatch.setenv(k, v)
    run, d = _live_dispatch(env, monkeypatch)
    con = env.con()
    try:
        con.execute("UPDATE dispatches SET launcher='herdr', pane_id='w1:p7', status=? WHERE id=?", (status, d["id"]))
        con.commit()
    finally:
        con.close()
    from office import dispatch
    held = json.loads(state_file.read_text())
    held["pane_agents"], held["pane_kinds"] = {"w1:p7": dispatch.herdr_agent_name(d["id"])}, {"w1:p7": d["harness"]}
    state_file.write_text(json.dumps(held))
    return state_file, run, d


def _add_nonblocking_finding(con, run_id, task_id="T1", scope="L-T1", code="F1"):
    gate_id = "G-" + uuid.uuid4().hex[:8]
    con.execute(
        "INSERT INTO gates(id, run_id, subject, task_id, scope, kind, input_key, status, verdict, round, created_at) "
        "VALUES(?, ?, 'lane', ?, ?, 'convergence_review', ?, 'closed', 'APPROVED', 1, datetime('now'))",
        (gate_id, run_id, task_id, scope, f"k-{uuid.uuid4().hex[:6]}"))
    con.execute(
        "INSERT INTO findings(id, run_id, task_id, gate_id, scope, code, state, level, severity, location, summary, "
        "created_at, contract, gate_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'),'convergence-v1','convergence_review')",
        ("find-" + uuid.uuid4().hex[:8], run_id, task_id, gate_id, scope, code, "nonblocking", "medium", "medium",
         "calc.py:10", "cleanup comment"))
    con.commit()


@pytest.mark.approved
def test_disposition_fix_with_live_herdr_worker_names_prompt_never_rerun(env, monkeypatch):
    """With owner's worker live, office disposition fix names prompt and delivers finding, never rerun."""
    state_file, run, d = _live_herdr_worker(env, monkeypatch)
    con = env.con()
    try:
        # Task T1 is accepted; worker is still live
        con.execute("UPDATE tasks SET status='accepted', accepted_revision_id='rev-1', current_revision_id='rev-1' WHERE id='T1'")
        _add_nonblocking_finding(con, run["id"], "T1", "L-T1", "F1")
    finally:
        con.close()

    code, out = env.office("disposition", "L-T1:F1", "fix", "--", "fix comment wording", check=0)
    assert code == 0, out
    assert "office rerun" not in out, f"output must never name office rerun while worker is live: {out}"
    assert "office prompt T1" in out, f"output must name office prompt: {out}"
    assert "delivered to the live worker" in out, f"output must state finding delivered: {out}"

    nxt_line = next((line for line in out.splitlines() if line.startswith("next:")), None)
    assert nxt_line, f"missing next: line in {out}"
    nxt_cmd = nxt_line.removeprefix("next:").strip()
    assert "office rerun" not in nxt_cmd
    assert nxt_cmd.startswith("office prompt T1")

    # Every printed next: in that state is a command the runtime accepts; test runs it.
    args = shlex.split(nxt_cmd)[1:]  # strip leading 'office'
    cmd_code, cmd_out = env.office(*args, check=0)
    assert cmd_code == 0, cmd_out


@pytest.mark.approved
def test_disposition_fix_with_live_process_worker_names_prompt(env):
    """With a live process (headless) worker, office disposition fix queues prompt, never rerun."""
    approved_run(env)
    con = env.con()
    try:
        run_row = con.execute("SELECT id FROM runs").fetchone()
        run_id = run_row[0]
        # Dispatch with launcher=process and our current pid so it is live
        disp_id = "D" + uuid.uuid4().hex[:8]
        con.execute(
            "INSERT INTO dispatches(id, run_id, task_id, role, launcher, pid, status, started_at) "
            "VALUES(?,?,?,?,?,?,?,datetime('now'))",
            (disp_id, run_id, "T1", "executor", "process", os.getpid(), "running"))
        con.execute("UPDATE tasks SET current_dispatch_id=?, status='accepted' WHERE id='T1'", (disp_id,))
        _add_nonblocking_finding(con, run_id, "T1", "L-T1", "F1")
    finally:
        con.close()

    code, out = env.office("disposition", "L-T1:F1", "fix", "--", "clean up style", check=0)
    assert code == 0, out
    assert "office rerun" not in out
    assert "office prompt T1" in out
    assert "delivered to the live worker" in out

    nxt_line = next(line for line in out.splitlines() if line.startswith("next:"))
    nxt_cmd = nxt_line.removeprefix("next:").strip()
    assert nxt_cmd.startswith("office prompt T1")
    assert "office rerun" not in nxt_cmd

    # Runtime accepts the printed next: command
    args = shlex.split(nxt_cmd)[1:]
    cmd_code, cmd_out = env.office(*args, check=0)
    assert cmd_code == 0, cmd_out


@pytest.mark.approved
def test_status_with_live_worker_in_changes_required_names_prompt_never_rerun(env, monkeypatch):
    """With owner's worker live, office status names prompt and states finding delivered, never rerun."""
    state_file, run, d = _live_herdr_worker(env, monkeypatch)
    con = env.con()
    try:
        con.execute("UPDATE tasks SET status='changes_required', pause_reason='repair needed' WHERE id='T1'")
        con.commit()
    finally:
        con.close()

    code, out = env.office("status", check=0)
    assert code == 0, out
    assert "office rerun" not in out, f"status output must never name office rerun while worker is live: {out}"
    assert "office prompt T1" in out, f"status output must name office prompt: {out}"
    assert "delivered to the live worker" in out, f"status output must state finding delivered: {out}"

    nxt_line = next(line for line in out.splitlines() if line.startswith("next:"))
    nxt_cmd = nxt_line.removeprefix("next:").strip()
    assert "office rerun" not in nxt_cmd
    assert nxt_cmd.startswith("office prompt T1")

    # Test runs it
    args = shlex.split(nxt_cmd)[1:]
    cmd_code, cmd_out = env.office(*args, check=0)
    assert cmd_code == 0, cmd_out


@pytest.mark.approved
def test_convergence_recheck_status_with_live_worker_names_prompt(env, monkeypatch):
    """In convergence RECHECK, with live worker, status names office prompt, never office rerun."""
    state_file, run, d = _live_herdr_worker(env, monkeypatch)
    con = env.con()
    try:
        con.execute("UPDATE tasks SET status='changes_required', pause_reason='L-T1 RECHECK: repair' WHERE id='T1'")
        con.execute(
            "INSERT INTO gates(id, run_id, subject, task_id, revision_id, kind, input_key, round, status, created_at) "
            "VALUES('gate-1', ?, 'task', 'T1', 'rev-1', 'convergence_review', 'k1', 1, 'changes_required', datetime('now'))",
            (run["id"],))
        con.execute(
            "UPDATE runs SET landing_json=? WHERE id=?",
            (json.dumps({"convergence": {"L-T1": {"id": "L-T1", "tasks": ["T1"], "status": "recheck", "round": 2}}}),
             run["id"]))
        con.commit()
    finally:
        con.close()

    code, out = env.office("status", check=0)
    assert code == 0, out
    assert "office rerun" not in out
    assert "office prompt T1" in out

    nxt_line = next(line for line in out.splitlines() if line.startswith("next:"))
    nxt_cmd = nxt_line.removeprefix("next:").strip()
    assert "office rerun" not in nxt_cmd
    assert nxt_cmd.startswith("office prompt T1")

    # Test runs it
    args = shlex.split(nxt_cmd)[1:]
    cmd_code, cmd_out = env.office(*args, check=0)
    assert cmd_code == 0, cmd_out


@pytest.mark.approved
def test_ended_worker_still_names_rerun(env):
    """When the worker has ended, office disposition and office status continue to name rerun."""
    approved_run(env)
    con = env.con()
    try:
        run_row = con.execute("SELECT id FROM runs").fetchone()
        run_id = run_row[0]
        # Dispatch that has ended
        disp_id = "D" + uuid.uuid4().hex[:8]
        con.execute(
            "INSERT INTO dispatches(id, run_id, task_id, role, launcher, status, started_at, ended_at) "
            "VALUES(?,?,?,?,?,'exited',datetime('now'),datetime('now'))",
            (disp_id, run_id, "T1", "executor", "process"))
        con.execute("UPDATE tasks SET current_dispatch_id=?, status='accepted' WHERE id='T1'", (disp_id,))
        _add_nonblocking_finding(con, run_id, "T1", "L-T1", "F1")
    finally:
        con.close()

    code, out = env.office("disposition", "L-T1:F1", "fix", "--", "fix it", check=0)
    assert code == 0, out
    assert "office rerun T1" in out
    assert "office prompt" not in out
