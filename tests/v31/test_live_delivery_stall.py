"""An amendment left undelivered to a live executor ages into a stall (#738)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import task_row
from test_wait import EXTERNAL, _go


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _live_delivery(env, *, age=600.0, status="queued", task_status="running", delivered_at=None, launcher=None):
    """T1's live executor holds amendment A38, bound to it `age` seconds ago."""
    from office import state
    _go(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    did = task_row(env)["current_dispatch_id"]
    con.execute("UPDATE dispatches SET status='running', ended_at=NULL, started_at=? WHERE id=?", (_ago(age + 60), did))
    if launcher:
        con.execute("UPDATE dispatches SET launcher=? WHERE id=?", (launcher, did))
    con.execute("UPDATE tasks SET status=? WHERE id='T1'", (task_status,))
    con.execute("INSERT INTO deliveries(id, run_id, amendment_id, task_id, dispatch_id, target_version, status, "
                "content, delivered_at, created_at) VALUES('DLlive', ?, 'A38', 'T1', ?, 2, ?, 'apply it', ?, ?)",
                (run["id"], did, status, delivered_at, _ago(age)))
    con.commit()
    return con, run, did


def _lines(con, run):
    from office import guide
    return [line for line in guide.stalls(con, run) if "A38" in line]


@pytest.mark.approved
@pytest.mark.parametrize("status", ["queued", "delivered"])
def test_old_unacked_delivery_to_a_live_executor_is_a_stall(env, status):
    con, run, did = _live_delivery(env, status=status)
    (line,) = _lines(con, run)
    assert "T1" in line and did in line and "live executor" in line, line
    assert "next:" in line and "office rerun T1" in line, line
    code, out = env.office("wait", "--timeout", "0", env=EXTERNAL)
    assert code == 3 and any(l.startswith("stall:") and "A38" in l for l in out.splitlines()), out
    status_out = env.office("status")[1]
    assert "blocker:" in status_out and "A38" in status_out, status_out


@pytest.mark.approved
def test_herdr_executor_recovery_offers_a_prompt(env):
    con, run, did = _live_delivery(env, launcher="herdr")
    (line,) = _lines(con, run)
    assert f'office prompt {did} -- "apply amendment A38, then office ack A38"' in line, line


@pytest.mark.approved
def test_headless_executor_recovery_is_not_a_prompt(env):
    con, run, did = _live_delivery(env, launcher="process")
    (line,) = _lines(con, run)
    assert "office prompt" not in line and "office rerun T1" in line, line


@pytest.mark.approved
def test_young_delivery_is_not_a_stall(env, monkeypatch):
    monkeypatch.setenv("OFFICE_EXECUTOR_IDLE_STALL_S", "60")
    con, run, _ = _live_delivery(env, age=5)
    assert _lines(con, run) == []


@pytest.mark.approved
def test_age_counts_from_the_recipients_start_and_the_delivery(env):
    """A relaunch rebinds an old delivery to a fresh dispatch; a first delivery is just now."""
    con, run, did = _live_delivery(env, age=600)
    con.execute("UPDATE dispatches SET started_at=? WHERE id=?", (_ago(2), did))
    con.commit()
    assert _lines(con, run) == []
    con.execute("UPDATE dispatches SET started_at=? WHERE id=?", (_ago(900), did))
    con.execute("UPDATE deliveries SET status='delivered', delivered_at=? WHERE id='DLlive'", (_ago(2),))
    con.commit()
    assert _lines(con, run) == []


@pytest.mark.approved
@pytest.mark.parametrize("task_status", ["accepted", "cancelled"])
def test_finished_task_is_not_a_stall(env, task_status):
    con, run, _ = _live_delivery(env, task_status=task_status)
    assert _lines(con, run) == []


@pytest.mark.approved
def test_applied_delivery_is_not_a_stall(env):
    con, run, _ = _live_delivery(env)
    con.execute("UPDATE deliveries SET status='applied', applied_at=?", (_ago(1),))
    con.commit()
    assert _lines(con, run) == []


@pytest.mark.approved
def test_ended_executor_stall_is_unchanged(env):
    con, run, did = _live_delivery(env, task_status="changes_required")
    con.execute("UPDATE dispatches SET status='exited', ended_at=started_at WHERE id=?", (did,))
    con.commit()
    (line,) = _lines(con, run)
    assert "awaits ack from ended executor" in line and "live executor" not in line, line
    assert "office rerun T1 --fresh" in line, line
