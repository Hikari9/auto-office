"""An amendment to a live worker: the notify prompt that lands marks the delivery delivered; a
worker that cannot be reached keeps it queued with the reason, shown by `office inspect task`
and `office status` instead of a bare "delivered 0x"."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, approved_run

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
MANUAL = {"OFFICE_JOBS": "manual"}
WIDER = PLAN_ONE.replace("scope: calc.py", "scope: calc.py, README.md")


def _amended_live_worker(env):
    """T1's worker is live (an external session) and an amendment to its contract is queued for it."""
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.write_plan(WIDER)
    env.office("amend", "T1", "--contract", "--", "add README.md", env=MANUAL, check=0)


def _notify(env, monkeypatch, *, launcher="herdr", landed="landed"):
    """Run the amendment's queued notify_worker job against a worker hosted by `launcher`
    whose prompt delivery reports `landed`."""
    from office import dispatch, gates, state
    con = env.con()
    con.execute("UPDATE dispatches SET launcher=?, pane_id='p1', status='running' WHERE role='executor'", (launcher,))
    con.commit()
    monkeypatch.setattr(gates, "_agent_alive", lambda name: True)
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: landed)
    row = con.execute("SELECT id FROM outbox WHERE kind='notify_worker' ORDER BY created_at DESC").fetchone()
    job = state.get_job(con, row["id"])
    return dispatch.job_notify_worker(con, state.get_run(con, job["run_id"]), job)


def _delivery(env) -> dict:
    return dict(env.con().execute("SELECT * FROM deliveries").fetchone())


def _task_lines(env) -> str:
    code, out = env.office("inspect", "task", "T1")
    assert code == 0, out
    return "\n".join(l for l in out.splitlines() if l.startswith("amendment "))


@pytest.mark.approved
def test_landed_notify_prompt_marks_the_delivery_delivered(env, monkeypatch):
    _amended_live_worker(env)
    assert _delivery(env)["status"] == "queued"
    assert _notify(env, monkeypatch, landed="landed") == {"sent": True, "landed": "landed"}
    row = _delivery(env)
    assert row["status"] == "delivered" and row["delivered_at"]
    line = _task_lines(env)
    assert "delivered" in line and "prompt landed in pane p1" in line, line
    assert "delivered 0x" not in line and "carried 0x on the worker's own commands" in line, line
    code, out = env.office("status", env=MANUAL)
    assert "A1 delivered (prompt landed in pane p1)" in out, out
    # The worker's own next office command still carries the text and counts it.
    from office import amend
    con = env.con()
    assert amend.pending_block(con, {"id": row["run_id"]}, row["dispatch_id"])
    assert _delivery(env)["delivered_count"] == 1


@pytest.mark.approved
@pytest.mark.parametrize("launcher", ["process", "process-fallback", "external"])
def test_headless_worker_stays_queued_with_a_recorded_reason(env, monkeypatch, launcher):
    _amended_live_worker(env)
    assert _notify(env, monkeypatch, launcher=launcher) == {"sent": False}
    assert _delivery(env)["status"] == "queued"
    reason = f"headless worker (launcher {launcher}): no pane to prompt"
    assert reason in _task_lines(env), _task_lines(env)
    assert "queued" in _task_lines(env)
    code, out = env.office("status", env=MANUAL)
    assert "A1 queued (" + reason in out, out


@pytest.mark.approved
@pytest.mark.parametrize("landed, reason", [("held", "typed but unsubmitted in pane p1 when last checked"),
                                            ("sent", "no landed signal")])
def test_prompt_that_did_not_land_stays_queued_with_why(env, monkeypatch, landed, reason):
    _amended_live_worker(env)
    assert _notify(env, monkeypatch, landed=landed)["landed"] == landed
    assert _delivery(env)["status"] == "queued"
    assert reason in _task_lines(env), _task_lines(env)


@pytest.mark.approved
def test_unreachable_agent_behind_a_blocked_worker_records_why(env, monkeypatch):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE role='executor'").fetchone())
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1",
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}
    wt = Path(d["worktree"])
    (wt / "calc.py").write_text(GOOD_ADD)
    (wt / "README.md").write_text("changed\n")
    env.git("add", "-N", "README.md", cwd=wt)
    env.office("submit", cwd=wt, env=wenv, check=4)  # refused: blocked on its own out-of-scope file
    env.write_plan(WIDER)
    env.office("amend", "T1", "--contract", "--", "add README.md", env=MANUAL, check=0)
    from office import dispatch, gates, state
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='p1', status='running' WHERE role='executor'")
    con.commit()
    monkeypatch.setattr(gates, "_agent_alive", lambda name: False)
    row = con.execute("SELECT id FROM outbox WHERE kind='notify_worker' ORDER BY created_at DESC").fetchone()
    job = state.get_job(con, row["id"])
    assert dispatch.job_notify_worker(con, state.get_run(con, job["run_id"]), job) == {"sent": False}
    assert _delivery(env)["status"] == "queued"
    assert "agent not alive in pane p1" in _task_lines(env), _task_lines(env)
    notes = [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE kind='amendment.notify'")]
    assert notes and notes[-1]["delivered"] is False and notes[-1]["amendment_id"] == "A1"


# ------------------------------------------------------------------ unit tier: no Office env


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A bare runs.db holding one live T1 worker dispatch and its queued amendment delivery."""
    for k in ("OFFICE_DATA_HOME", "OFFICE_STATE_HOME"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    from office import db
    con = db.connect()
    con.execute("INSERT INTO dispatches(id, run_id, role, task_id, status, launcher, pane_id, started_at) "
                "VALUES('D1','r1','executor','T1','running','herdr','p1','2026-01-01')")
    con.execute("INSERT INTO deliveries(id, run_id, amendment_id, task_id, dispatch_id, target_version, status, content, created_at) "
                "VALUES('r1:A1:T1','r1','A1','T1','D1',2,'queued','fix it','2026-01-01')")
    return con


def _job(dispatch_id="D1"):
    return {"payload": {"dispatch_id": dispatch_id, "task_id": "T1", "amendment_id": "A1",
                        "text": "AMENDMENT A1: run office status"}}


def test_unit_landed_prompt_marks_delivered_and_records_it(bare, monkeypatch):
    from office import amend, dispatch
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "landed")
    assert dispatch.job_notify_worker(bare, {"id": "r1"}, _job()) == {"sent": True, "landed": "landed"}
    row = bare.execute("SELECT status, delivered_at FROM deliveries").fetchone()
    assert row["status"] == "delivered" and row["delivered_at"]
    assert amend.delivery_note(bare, "r1", "T1", "A1", "D1") == "prompt landed in pane p1"


@pytest.mark.parametrize("launcher", ["process", "process-fallback", "external", None])
def test_unit_headless_worker_stays_queued_with_the_reason(bare, monkeypatch, launcher):
    from office import amend, dispatch
    bare.execute("UPDATE dispatches SET launcher=? WHERE id='D1'", (launcher,))
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: pytest.fail("a headless worker has no pane to prompt"))
    assert dispatch.job_notify_worker(bare, {"id": "r1"}, _job()) == {"sent": False}
    assert bare.execute("SELECT status FROM deliveries").fetchone()["status"] == "queued"
    assert f"headless worker (launcher {launcher or 'none'}): no pane to prompt" in amend.delivery_note(bare, "r1", "T1", "A1", "D1")


def test_unit_a_later_landed_prompt_replaces_the_recorded_reason(bare, monkeypatch):
    from office import amend, dispatch
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "held")
    dispatch.job_notify_worker(bare, {"id": "r1"}, _job())
    assert "typed but unsubmitted" in amend.delivery_note(bare, "r1", "T1", "A1", "D1")
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "landed")
    dispatch.job_notify_worker(bare, {"id": "r1"}, _job())
    assert amend.delivery_note(bare, "r1", "T1", "A1", "D1") == "prompt landed in pane p1"
    assert bare.execute("SELECT status FROM deliveries").fetchone()["status"] == "delivered"


def test_unit_a_notice_that_names_no_amendment_records_nothing(bare, monkeypatch):
    from office import dispatch
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "landed")
    job = {"payload": {"dispatch_id": "D1", "task_id": "T1", "text": "Findings: run office status"}}
    assert dispatch.job_notify_worker(bare, {"id": "r1"}, job)["landed"] == "landed"
    assert bare.execute("SELECT status FROM deliveries").fetchone()["status"] == "queued"
    assert bare.execute("SELECT count(*) FROM events WHERE kind='amendment.notify'").fetchone()[0] == 0


def test_unit_a_pane_that_is_not_the_workers_gets_no_amendment_text(bare, monkeypatch):
    from office import amend, dispatch
    bare.execute("UPDATE dispatches SET worktree='/wt/T1' WHERE id='D1'")
    monkeypatch.setattr(dispatch, "_pane_mismatch", lambda *a, **k: "pane p1 is reserved for dispatch D2 (T2), not for D1 (T1)")
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: pytest.fail("typed into another dispatch's pane"))
    assert dispatch.job_notify_worker(bare, {"id": "r1"}, _job()) == {"sent": False}
    assert bare.execute("SELECT status FROM deliveries").fetchone()["status"] == "queued"
    assert "not sent: pane p1 is reserved for dispatch D2" in amend.delivery_note(bare, "r1", "T1", "A1", "D1")


def test_unit_a_note_from_an_earlier_session_never_describes_the_new_holder(bare, monkeypatch):
    from office import amend, dispatch
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "landed")
    dispatch.job_notify_worker(bare, {"id": "r1"}, _job())
    # D1 was revoked; the relaunch D2 holds the re-queued delivery.
    bare.execute("UPDATE deliveries SET dispatch_id='D2', status='queued'")
    assert amend.delivery_note(bare, "r1", "T1", "A1", "D2") == ""
    # A late job for D1 finds the delivery is no longer its own: nothing is recorded or marked delivered.
    amend.record_notify(bare, {"id": "r1"}, _job()["payload"], delivered=True, reason="prompt landed")
    assert bare.execute("SELECT status FROM deliveries").fetchone()["status"] == "queued"
    assert amend.delivery_note(bare, "r1", "T1", "A1", "D2") == ""


def test_unit_a_failing_record_never_skips_the_unblock_or_fails_the_job(bare, monkeypatch):
    from office import amend, dispatch
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "landed")
    monkeypatch.setattr(amend, "record_notify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    assert dispatch.job_notify_worker(bare, {"id": "r1"}, _job()) == {"sent": True, "landed": "landed"}


@pytest.mark.parametrize("status", ["superseded", "applied"])
def test_unit_a_landed_prompt_never_revives_a_delivery_that_is_no_longer_waiting(bare, monkeypatch, status):
    from office import dispatch
    bare.execute("UPDATE deliveries SET status=?", (status,))
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "landed")
    dispatch.job_notify_worker(bare, {"id": "r1"}, _job())
    assert bare.execute("SELECT status FROM deliveries").fetchone()["status"] == status
    assert bare.execute("SELECT count(*) FROM events WHERE kind='amendment.notify'").fetchone()[0] == 0


def test_unit_a_failed_nudge_after_the_delivery_was_delivered_adds_no_failure_note(bare, monkeypatch):
    from office import amend, dispatch
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "landed")
    dispatch.job_notify_worker(bare, {"id": "r1"}, _job())
    bare.execute("UPDATE dispatches SET launcher='process' WHERE id='D1'")
    dispatch.job_notify_worker(bare, {"id": "r1"}, _job())
    assert amend.delivery_note(bare, "r1", "T1", "A1", "D1") == "prompt landed in pane p1"


def test_unit_a_stale_failure_note_is_not_shown_once_the_worker_took_the_delivery_itself(bare, monkeypatch):
    from office import amend, dispatch
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: "held")
    dispatch.job_notify_worker(bare, {"id": "r1"}, _job())
    assert "typed but unsubmitted" in amend.delivery_note(bare, "r1", "T1", "A1", "D1")
    bare.execute("UPDATE deliveries SET status='delivered'")  # the worker's own `office status` carried it
    assert amend.delivery_note(bare, "r1", "T1", "A1", "D1", "delivered") == ""


def test_unit_unreachable_reasons(bare):
    from office import amend
    assert amend.unreachable_reason(None) == "dispatch not found"
    assert amend.unreachable_reason({"launcher": "herdr", "pane_id": "p1", "status": "exited"}) == "dispatch is exited, not running"
    assert amend.unreachable_reason({"launcher": "herdr", "pane_id": "p1", "status": "running"}) == "agent not alive in pane p1"


@pytest.mark.parametrize("unblock", [False, True])
def test_unit_a_pane_herdr_places_in_another_tasks_worktree_gets_no_amendment_text(bare, tmp_path, monkeypatch, unblock):
    import test_dispatch_identity as ident
    from office import amend, dispatch, gates
    (tmp_path / "bin").mkdir()
    state = ident.install_fake(tmp_path / "bin", tmp_path, monkeypatch, delay="0")
    t1, t2 = tmp_path / "wt" / "T1", tmp_path / "wt" / "T2"
    bare.execute("UPDATE dispatches SET worktree=? WHERE id='D1'", (str(t1),))
    bare.execute("INSERT INTO dispatches(id, run_id, role, task_id, status, worktree, started_at) "
                 "VALUES('D2','r1','executor','T2','running',?,'2026-01-02')", (str(t2),))
    ident.pane_state(state, "p1", cwd=str(t2))  # herdr says D1's recorded pane sits in T2's worktree
    undelivered = []
    monkeypatch.setattr(gates, "_agent_alive", lambda name: True)
    monkeypatch.setattr(dispatch, "_amendment_undelivered", lambda *a: undelivered.append(a))
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **k: pytest.fail("typed into another task's pane"))
    job = _job()
    job["payload"]["unblock"] = unblock
    assert dispatch.job_notify_worker(bare, {"id": "r1"}, job) == {"sent": False}
    note = amend.delivery_note(bare, "r1", "T1", "A1", "D1")
    assert note.startswith("not sent: pane p1 is in ") and "dispatch D2 (T2)" in note, note
    assert bool(undelivered) is unblock  # a blocked worker's blocker stays, with its notice

