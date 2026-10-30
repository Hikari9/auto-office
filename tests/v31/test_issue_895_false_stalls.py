"""#895: no false stalls for a gate owned by a running self-review subagent, or for an executor waiting on a live child.

Process tables, agent identities and pane activity are scripted fakes, so no real process or herdr pane is read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from test_wait import _go

from office import dispatch, guide, rerun, root_wake, state, watchdog
from office import questions as questions_mod

AGENT = 4242
START = "Fri Jan 2 03:04:05 2026"
IDLE_TEXT = "› Ready for input\n"
IDLE_HASH = "h-idle"


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


MCP = "node /opt/mcp/context-mode/start.mjs"
WAIT = "office wait --timeout 580"


def _row(pid, ppid, command, state="S", began=START):
    """A ps-shaped row: (pid, ppid, pgid, state, start, command)."""
    return (pid, ppid, pid, state, began, command)


def _table(*, in_flight=False, agent_start=START, extra=()):
    """The agent's process table. An MCP server child is always present and never counts as work in flight.
    `in_flight` adds the agent's shell running `office wait`."""
    rows = [_row(AGENT, 1, "claude", began=agent_start), _row(5100, AGENT, MCP)]
    if in_flight:
        rows += [_row(5001, AGENT, f"/bin/zsh -c eval '{WAIT}'"), _row(5002, 5001, WAIT)]
    return rows + list(extra)


def _script_agent(monkeypatch, table, *, identity=True):
    """The agent is pid AGENT, verified by its start time, and the process table is `table`."""
    rec = {"pid": AGENT, "c_start": START} if identity else None
    monkeypatch.setattr(dispatch, "_recorded_identity", lambda run, dispatch_id, which: rec if which == "agent" else None)
    monkeypatch.setattr(dispatch, "_process_table", lambda: table)


def _idle_executor(env, monkeypatch, *, idle_for=600.0):
    """T1's executor is a herdr pane agent that has sat idle for `idle_for` seconds."""
    _go(env)
    monkeypatch.setenv("OFFICE_EXECUTOR_IDLE_STALL_S", "60")
    monkeypatch.setenv("OFFICE_EXECUTOR_PROGRESS_STALL_S", "60")
    con = env.con()
    con.execute("UPDATE tasks SET status='running' WHERE id='T1'")
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='w1:p1', status='running', ended_at=NULL, "
                "started_at=?, idle_since=?, idle_hash=?, watchdog_progress_since=? WHERE task_id='T1' AND role='executor'",
                (_ago(idle_for + 60), _ago(idle_for), IDLE_HASH, _ago(idle_for)))
    con.commit()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    did = con.execute("SELECT id FROM dispatches WHERE task_id='T1' AND role='executor'").fetchone()[0]
    return con, run, did


def _idle_act(text=IDLE_TEXT):
    return {"alive": True, "busy": False, "hash": IDLE_HASH, "text": text, "status": "idle", "question": None}


def _executor_row(con, did) -> dict:
    return dict(con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())


# --- Gate ownership: a running self-review subagent is a live owner -------------------------------------------------


def _reviewer_gate(env, monkeypatch, *, kind, stale_for=7200.0, started_for=120.0):
    """A gate of `kind` whose reviewer dispatch runs in a herdr pane and has made no progress for `stale_for` seconds.

    The T1 executor is ended, so the only running dispatch is the reviewer. The reviewer's stored progress sample
    matches its pane, so only elapsed time separates it from a stall."""
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_NO_PROGRESS_S", "60")
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    gate_id = f"G895{kind[:4].upper()}"
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, plan_version, kind, input_key, status, "
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (gate_id, run["id"], "task", "T1", None, run.get("plan_version"), kind, "k895", "queued", _ago(stale_for)))
    did = con.execute("SELECT id FROM dispatches WHERE task_id='T1' AND role='executor'").fetchone()[0]
    con.execute("UPDATE dispatches SET role='code_reviewer', kind='reviewer', gate_id=?, launcher='herdr', pane_id='w1:p2', "
                "status='running', ended_at=NULL, started_at=?, watchdog_progress_since=?, watchdog_progress_hash=NULL "
                "WHERE id=?", (gate_id, _ago(started_for), _ago(stale_for), did))
    con.commit()
    act = {"alive": True, "busy": False, "hash": "h-review", "text": "Reviewing the diff.\n", "status": "idle", "question": None}
    monkeypatch.setattr(rerun, "agent_activity", lambda d: act)
    con.execute("UPDATE dispatches SET watchdog_progress_hash=? WHERE id=?",
                (watchdog.progress(_executor_row(con, did), act), did))
    con.commit()
    return con, run, gate_id, did


def _gate_lines(con, run, gate_id, did):
    return [line for line in guide.stalls(con, run) if gate_id in line or did in line]


def test_running_self_review_subagent_owns_its_gate(env, monkeypatch):
    con, run, gate_id, did = _reviewer_gate(env, monkeypatch, kind="self_review_subagent")
    assert watchdog.reviewer_stalls(con, run) == []
    assert _gate_lines(con, run, gate_id, did) == []


def test_a_running_self_review_subagent_past_its_wall_budget_is_still_a_stall(env, monkeypatch):
    """A1: the owned exemption covers the no-progress clock only, never the review_wall hard budget."""
    monkeypatch.setenv("OFFICE_REVIEW_WALL_S", "3600")
    con, run, gate_id, did = _reviewer_gate(env, monkeypatch, kind="self_review_subagent", started_for=7200.0)
    (line,) = watchdog.reviewer_stalls(con, run)
    assert did in line and "review wall budget" in line, line


def test_watchdog_tick_stays_quiet_for_a_running_self_review_subagent(env, monkeypatch):
    con, run, _, _ = _reviewer_gate(env, monkeypatch, kind="self_review_subagent")
    monkeypatch.setattr(root_wake, "_binding", lambda con, run_id: {"attached": True})
    monkeypatch.setattr(questions_mod, "scan", lambda con, run: (None, None, {}))
    assert root_wake.watchdog_tick(con, run) == "unchanged"


def test_other_stale_reviewer_is_still_a_stall(env, monkeypatch):
    """Negative control: the exemption is for self-review subagents only."""
    con, run, gate_id, did = _reviewer_gate(env, monkeypatch, kind="review")
    (line,) = watchdog.reviewer_stalls(con, run)
    assert did in line and "no output or worktree progress" in line, line


def test_queued_self_review_gate_without_an_owner_is_still_a_stall(env, monkeypatch):
    """Negative control: no running subagent and no job means no live owner."""
    con, run, gate_id, did = _reviewer_gate(env, monkeypatch, kind="self_review_subagent")
    con.execute("UPDATE dispatches SET ended_at=?, status='exited' WHERE id=?", (_ago(1), did))
    con.commit()
    lines = _gate_lines(con, run, gate_id, did)
    assert len(lines) == 1 and "self_review_subagent gate" in lines[0] and "no job is queued or running" in lines[0], lines


# --- Executor waiting on a live child --------------------------------------------------------------------------------


def test_idle_executor_with_a_command_in_flight_is_not_reported(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    _script_agent(monkeypatch, _table(in_flight=True))
    before = _executor_row(con, did)["idle_since"]
    assert guide._idle_executors(con, run, {did: _idle_act()}) == []
    assert _executor_row(con, did)["idle_since"] > before, "the idle clock restarts while a command runs"


def test_idle_executor_with_only_an_mcp_server_is_reported(env, monkeypatch):
    """Regression: a long-lived MCP child must not make every idle agent look busy."""
    con, run, did = _idle_executor(env, monkeypatch)
    _script_agent(monkeypatch, _table())
    (line,) = guide._idle_executors(con, run, {did: _idle_act()})
    assert did in line and "idle" in line and "without submitting" in line, line


def test_a_shell_under_a_harness_wrapper_is_work_in_flight(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    wrapper = [_row(5300, AGENT, "node /opt/harness/wrapper.js"), _row(5301, 5300, f"/bin/zsh -c eval '{WAIT}'"),
               _row(5302, 5301, WAIT)]
    _script_agent(monkeypatch, _table(extra=wrapper))
    assert guide._idle_executors(con, run, {did: _idle_act()}) == []


def test_an_mcp_server_with_its_own_child_is_still_reported(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    _script_agent(monkeypatch, _table(extra=[_row(5101, 5100, "node helper.js")]))
    (line,) = guide._idle_executors(con, run, {did: _idle_act()})
    assert "without submitting" in line, line


def test_a_headless_executor_waiting_on_a_command_is_not_reported(env, monkeypatch):
    """The supervisor's pid is not the agent: the recorded agent identity decides."""
    con, run, did = _idle_executor(env, monkeypatch)
    con.execute("UPDATE dispatches SET launcher='process', pid=9999 WHERE id=?", (did,))
    con.commit()
    _script_agent(monkeypatch, _table(in_flight=True))
    assert guide._idle_executors(con, run, {did: _idle_act()}) == []


def test_an_oversized_agent_tree_is_unknown_not_a_stall(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    flood = [_row(6000 + i, AGENT, "sleep 1") for i in range(watchdog.MAX_TREE)]
    _script_agent(monkeypatch, _table(extra=flood))
    assert guide._idle_executors(con, run, {did: _idle_act()}) == []


def test_idle_bash_shell_with_no_command_is_reported(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    _script_agent(monkeypatch, _table(extra=[_row(5001, 4242, "/bin/zsh -c source snapshot.sh")]))
    (line,) = guide._idle_executors(con, run, {did: _idle_act()})
    assert "without submitting" in line, line


def test_no_verifiable_agent_is_reported_as_before(env, monkeypatch):
    """Without a recorded agent identity the pane's agent is unknown: nothing suppresses the idle report."""
    con, run, did = _idle_executor(env, monkeypatch)
    _script_agent(monkeypatch, _table(in_flight=True), identity=False)
    (line,) = guide._idle_executors(con, run, {did: _idle_act()})
    assert "without submitting" in line, line


def test_a_zombie_agent_is_not_waiting_on_anything(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    table = _table(in_flight=True)
    table[0] = _row(AGENT, 1, "claude", state="Z")
    _script_agent(monkeypatch, table)
    (line,) = guide._idle_executors(con, run, {did: _idle_act()})
    assert "without submitting" in line, line


def test_unreadable_process_table_is_unknown_not_a_stall(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    _script_agent(monkeypatch, None)
    assert guide._idle_executors(con, run, {did: _idle_act()}) == []


def test_a_reused_pid_is_not_the_recorded_agent(env, monkeypatch):
    """The pid now belongs to another process, so its children are not the agent's."""
    con, run, did = _idle_executor(env, monkeypatch)
    _script_agent(monkeypatch, _table(in_flight=True, agent_start="Mon Mar 9 10:00:00 2026"))
    (line,) = guide._idle_executors(con, run, {did: _idle_act()})
    assert "without submitting" in line, line


def test_a_zombie_command_does_not_keep_an_executor_in_flight(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    extra = [_row(5001, AGENT, f"/bin/zsh -c eval '{WAIT}'"), _row(5002, 5001, WAIT, state="Z")]
    _script_agent(monkeypatch, _table(extra=extra))
    (line,) = guide._idle_executors(con, run, {did: _idle_act()})
    assert "without submitting" in line, line


def test_busy_executor_with_a_command_in_flight_is_not_a_progress_stall(env, monkeypatch):
    con, run, did = _idle_executor(env, monkeypatch)
    busy = {"alive": True, "busy": True, "hash": "h-busy", "text": "✽ Waiting on monitor (9m)\n", "status": "working"}
    row = _executor_row(con, did)
    con.execute("UPDATE dispatches SET watchdog_progress_hash=? WHERE id=?", (watchdog.progress(row, busy), did))
    con.commit()
    row = _executor_row(con, did)
    _script_agent(monkeypatch, _table(in_flight=True))
    assert watchdog.check(con, run, row, role="executor", activity=busy) is None
    con.execute("UPDATE dispatches SET watchdog_progress_since=? WHERE id=?", (_ago(600), did))  # the clock restarted
    con.commit()
    _script_agent(monkeypatch, _table())
    reason = watchdog.check(con, run, _executor_row(con, did), role="executor", activity=busy)
    assert reason and "no pane, worktree or log progress" in reason, reason
