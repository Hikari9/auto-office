"""office wait: an orchestrator's watcher keys on the exit code, not status text.

Run b90bbb5b: watch loops grepped `office status` for a few phrases and stayed
silent through an amendment deadlock and a stalled gate that printed none of them.
"""
from __future__ import annotations

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
