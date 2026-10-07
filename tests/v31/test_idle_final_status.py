"""An executor that printed its final status line without submitting is a stall
at once, not after the idle threshold (G4). Agent activity is mocked in-process."""
from __future__ import annotations

from conftest import GOOD_ADD, approved_run

from office import guide, state

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
FINAL = ("● Done.\n\n"
         "TASK=T1 COMMIT=abc123 PUSHED=yes CHECKS=pass 4/4 SUBMIT=not attempted\n"
         "NEXT=answer the scope question about calc.py\n\n> ")


def _setup(env, monkeypatch):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    monkeypatch.setenv("OFFICE_EXECUTOR_IDLE_STALL_S", "1800")
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='w1:p1', status='running' WHERE task_id='T1' AND role='executor'")
    con.commit()
    did = con.execute("SELECT id FROM dispatches WHERE task_id='T1' AND role='executor'").fetchone()[0]
    return con, state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0]), did


def _stalls(con, run, did, text, busy=False):
    act = {"alive": True, "busy": busy, "hash": str(hash(text)), "text": text, "status": "idle"}
    return guide._idle_executors(con, run, {did: act})


def test_a_final_status_line_stalls_at_once_with_its_next_text(env, monkeypatch):
    con, run, did = _setup(env, monkeypatch)
    lines = _stalls(con, run, did, FINAL)
    assert len(lines) == 1, lines
    assert did in lines[0] and "SUBMIT=not attempted" in lines[0], lines
    assert "NEXT=answer the scope question about calc.py" in lines[0] and "pane-tail.txt" in lines[0], lines


def test_a_refused_submit_status_line_stalls_at_once(env, monkeypatch):
    con, run, did = _setup(env, monkeypatch)
    text = "TASK=T1 COMMIT=abc PUSHED=yes CHECKS=pass SUBMIT=refused: lease-lost NEXT=redispatch T1\n"
    lines = _stalls(con, run, did, text)
    assert len(lines) == 1 and "SUBMIT=refused: lease-lost" in lines[0] and "NEXT=redispatch T1" in lines[0], lines


def test_an_accepted_status_line_does_not_stall_early(env, monkeypatch):
    con, run, did = _setup(env, monkeypatch)
    assert _stalls(con, run, did, FINAL.replace("not attempted", "accepted R1")) == []


def test_a_busy_agent_with_a_final_line_does_not_stall(env, monkeypatch):
    con, run, did = _setup(env, monkeypatch)
    assert _stalls(con, run, did, FINAL, busy=True) == []


def test_a_final_line_scrolled_above_a_newer_turn_does_not_stall(env, monkeypatch):
    con, run, did = _setup(env, monkeypatch)
    assert _stalls(con, run, did, FINAL + "\n> continue please\n● Working on it\n") == []
