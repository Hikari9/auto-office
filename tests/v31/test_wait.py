"""office wait: an orchestrator's watcher keys on the exit code, not status text.

Run b90bbb5b: watch loops grepped `office status` for a few phrases and stayed
silent through an amendment deadlock and a stalled gate that printed none of them.
"""
from __future__ import annotations

from pathlib import Path

from conftest import GOOD_ADD, PLAN_ONE, start_inline

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _go(env):
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    start_inline(env, plan=PLAN_ONE, gear="direct+review")
    env.office("approve", "plan", "--quote", "approved", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("status", check=0)  # consume the dispatch events


def test_wait_times_out_with_124_when_nothing_changes(env):
    _go(env)
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert code == 124 and "nothing new" in out, out


def test_wait_reports_a_gate_nothing_can_advance_as_a_stall(env):
    _go(env)
    con = env.con()
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, created_at) "
                "SELECT 'Gstuck', run_id, 'task', 'T1', 'checks', 'stuck', 'running', '2026-01-01' FROM tasks WHERE id='T1'")
    con.execute("UPDATE outbox SET status='done' WHERE status IN ('queued','claimed')")
    con.commit()
    code, out = env.office("wait", "--timeout", "5", "--poll", "0.2", env=EXTERNAL)
    assert code == 3 and "stall:" in out and "Gstuck" in out, out


def test_wait_returns_0_at_once_when_a_task_already_needs_the_orchestrator(env):
    _go(env)
    # A blocker already present when wait starts ends it at once.
    env.office("revoke", "T1", check=0)
    code, out = env.office("wait", "--timeout", "5", "--poll", "0.2", env=EXTERNAL)
    assert code == 0 and "blocker:" in out, out


# ---- an executor whose agent stopped without submitting

FAKE_HERDR = r'''#!{python}
import json, os, sys
d = os.environ["FAKE_HERDR_DIR"]
def get(name, default=""):
    p = os.path.join(d, name)
    return open(p).read().strip() if os.path.exists(p) else default
args = sys.argv[1:]
if args[:2] == ["agent", "get"]:
    mode = get("mode", "ok")
    if mode == "gone":
        print(json.dumps({{"error": {{"code": "not_found"}}}}))
        sys.exit(1)
    if mode == "unreachable":
        sys.stderr.write("connection refused")
        sys.exit(1)
    print(json.dumps({{"result": {{"agent": {{"status": get("status", "idle")}}}}}}))
elif args[:2] == ["agent", "read"]:
    if get("mode", "ok") == "unreachable":
        sys.exit(1)
    if get("changing"):
        n = int(get("counter", "0")) + 1
        open(os.path.join(d, "counter"), "w").write(str(n))
        print(get("pane", "") + " tick %d" % n)
    else:
        print(get("pane", ""))
else:
    print("{{}}")
'''


def _herdr(env, **files) -> dict:
    """A fake herdr driven by files in a directory; `files` sets the agent's state."""
    import sys
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    state = env.tmp / "herdr-fake"
    state.mkdir(exist_ok=True)
    for name, text in files.items():
        (state / name).write_text(text)
    return {**EXTERNAL, "FAKE_HERDR_DIR": str(state), "OFFICE_EXECUTOR_IDLE_STALL_MIN": "0"}


def _as_herdr(env) -> dict:
    """Make T1's dispatch a running pane-hosted one, as if herdr had launched it."""
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='w1:p1', status='running' WHERE task_id='T1' AND role='executor'")
    con.commit()
    return dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1' AND role='executor'").fetchone())


def _wait(env, e, timeout="1"):
    return env.office("wait", "--timeout", timeout, "--poll", "0.2", env=e)


def test_a_working_agent_is_not_a_stall(env):
    _go(env)
    e = _herdr(env, status="working", pane="thinking")
    _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124 and "stall" not in out, out


def test_an_agent_idle_past_the_threshold_without_submitting_is_a_stall(env):
    _go(env)
    e = _herdr(env, status="idle", pane="> waiting for input")
    d = _as_herdr(env)
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "stall:" in out and d["id"] in out and "idle" in out, out
    tail = Path(paths_run_dir(env)) / "dispatches" / d["id"] / "pane-tail.txt"
    assert "waiting for input" in tail.read_text()


def paths_run_dir(env) -> str:
    from office import paths
    con = env.con()
    return str(paths.run_dir(con.execute("SELECT id FROM runs").fetchone()[0]))


def test_the_idle_threshold_holds_across_wait_invocations(env):
    _go(env)
    e = _herdr(env, status="idle", pane="> waiting")
    e["OFFICE_EXECUTOR_IDLE_STALL_MIN"] = "30"
    d = _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124, out
    con = env.con()
    assert con.execute("SELECT idle_since FROM dispatches WHERE id=?", (d["id"],)).fetchone()[0]
    con.execute("UPDATE dispatches SET idle_since='2020-01-01T00:00:00.000000+00:00' WHERE id=?", (d["id"],))
    con.commit()
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and d["id"] in out, out


def test_an_agent_idle_after_an_accepted_submit_is_not_a_stall(env):
    _go(env)
    e = _herdr(env, status="idle", pane="> done")
    d = _as_herdr(env)
    (Path(d["worktree"]) / "calc.py").write_text(GOOD_ADD)
    code, out = env.office("submit", cwd=d["worktree"], env={**e, "OFFICE_DISPATCH_ID": d["id"], "OFFICE_ROLE": "executor"})
    assert code == 0, out
    code, out = _wait(env, e)
    assert "stall" not in out, out


def test_agy_reporting_idle_while_its_pane_changes_is_not_a_stall(env):
    _go(env)
    e = _herdr(env, status="idle", pane="esc to cancel", changing="1")
    _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124 and "stall" not in out, out


def test_herdr_unreachable_is_never_a_stall(env):
    _go(env)
    e = _herdr(env, mode="unreachable")
    _as_herdr(env)
    code, out = _wait(env, e)
    assert code == 124 and "stall" not in out, out


def test_a_dead_headless_process_is_a_stall(env):
    import subprocess
    import sys
    _go(env)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='process', pid=?, status='running' WHERE task_id='T1' AND role='executor'",
                (gone.pid,))
    con.commit()
    from office import guide, state
    did = con.execute("SELECT id FROM dispatches WHERE task_id='T1' AND role='executor'").fetchone()[0]
    lines = guide.stalls(con, state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0]))
    assert len(lines) == 1 and did in lines[0] and "gone" in lines[0], lines


def test_the_stall_line_cites_a_refused_submit(env):
    _go(env)
    e = _herdr(env, status="idle", pane="> stuck")
    d = _as_herdr(env)
    # Submitting from outside the task worktree is refused.
    code, out = env.office("submit", cwd=env.repo, env={**e, "OFFICE_DISPATCH_ID": d["id"], "OFFICE_ROLE": "executor"})
    assert code == 4 and "wrong-worktree" in out, out
    code, out = _wait(env, e, timeout="3")
    assert code == 3 and "last submit was refused" in out and "wrong-worktree" in out, out
