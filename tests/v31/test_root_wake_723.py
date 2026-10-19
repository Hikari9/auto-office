"""#723: durable root wake receipts and a fake-harness continuation seam.

All transport and continuation tests are local: no paid models or real sessions.
"""
from __future__ import annotations

import os

import pytest

from conftest import PLAN_TWO, GOOD_ADD, approved_run, self_reviewed, task_row
from office import db, discovery, dispatch, root_wake, state

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
PANE = "fake-workspace:p-root"


def setup_root(env, monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    monkeypatch.delenv("OFFICE_DISPATCH_ID", raising=False)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    with db.transaction(con):
        discovery.bind(con, run, [("herdr", PANE)], "resume")
    monkeypatch.setattr(root_wake, "_agent_in_pane", lambda pane: ("root-claude", "idle") if pane == PANE else None)
    assert "enabled" in root_wake.attach(con, run)
    return con, run


def events(con, run):
    return [dict(row) for row in con.execute("SELECT * FROM root_wake_intents WHERE run_id=? ORDER BY event_seq", (run["id"],))]


def test_wake_is_opt_in_and_rejects_unverified_sessions(env, monkeypatch):
    approved_run(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0])
    assert root_wake.lines(con, run) == []
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    with pytest.raises(state.Refused, match="not bound"):
        root_wake.attach(con, run)
    with db.transaction(con):
        discovery.bind(con, run, [("herdr", PANE)], "resume")
    monkeypatch.setattr(root_wake, "_agent_in_pane", lambda _pane: None)
    with pytest.raises(state.Refused, match="did not verify"):
        root_wake.attach(con, run)
    assert root_wake.lines(con, run) == []


def test_worker_submit_when_root_idle_wakes_and_fake_root_continues_without_a_human(env, monkeypatch):
    """Actual Office submission -> DB intent -> Herdr prompt -> root observes -> Office dispatch."""
    approved_run(env, plan=PLAN_TWO, executor=[{"submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con, run = setup_root(env, monkeypatch)
    delivered = []

    def send(name, text, *, pane=None):
        delivered.append((name, text, pane))
        return "landed"

    monkeypatch.setattr(dispatch, "submit_prompt", send)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env)["current_dispatch_id"],)).fetchone())
    worker_env = {"OFFICE_RUN_ID": run["id"], "OFFICE_DISPATCH_ID": d["id"],
                  "OFFICE_TASK_ID": "T1", "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}
    from pathlib import Path
    wt = Path(d["worktree"])
    (wt / "calc.py").write_text(GOOD_ADD)
    self_reviewed(wt, "calc.py")
    code, out = env.office("submit", cwd=wt, env=worker_env)
    assert code == 0, out

    # The root completed its earlier turn, and no user has typed continue.
    assert root_wake.poll(con, run) == "delivered"
    assert len(delivered) == 1 and delivered[0][0] == "root-claude" and delivered[0][2] == PANE
    assert "office --run" in delivered[0][1] and "context" in delivered[0][1]
    assert {x["state"] for x in events(con, run)} == {"delivered"}

    # Scripted fake root acts *only* because its harness received that prompt.
    if delivered:
        code, out = env.office("context", env={"HERDR_PANE_ID": PANE})
        assert code == 0, out
        assert {x["state"] for x in events(con, run)} == {"observed"}
        code, out = env.office("dispatch", "T2", env={**EXTERNAL, "HERDR_PANE_ID": PANE})
        assert code == 0, out
    assert state.get_task(con, run["id"], "T2")["status"] == "running"
    assert {x["state"] for x in events(con, run)} == {"acted"}
    assert all(x["state"] == "acted" for x in events(con, run))  # old receipt never re-delivered


def test_restarts_coalesce_bursts_and_never_awaken_for_informational_events(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: sent.append(a) or "landed")
    with db.transaction(con):
        state.emit(con, run, "launch.session", "agent started", audience="orchestrator")
        state.emit(con, run, "setup.done", "setup done", audience="orchestrator")
    assert root_wake.poll(con, run) == "no-action"
    assert not sent and not events(con, run)
    with db.transaction(con):
        for i in range(3):
            state.emit(con, run, "gate.recheck", f"review result {i}")
    assert root_wake.poll(con, run) == "delivered"
    assert len(sent) == 1 and len(events(con, run)) == 3
    assert root_wake.poll(con, run) == "no-action"
    con.close()
    con = env.con()  # process restart: durable acknowledgements survive
    assert root_wake.poll(con, run) == "no-action"
    assert len(sent) == 1


def test_busy_root_defers_without_burning_retry_budget(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    with db.transaction(con):
        state.emit(con, run, "task.blocked", "T1 blocked")
    monkeypatch.setattr(root_wake, "_agent_in_pane", lambda _pane: ("root-claude", "working"))
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *_a, **_kw: pytest.fail("busy root must not be prompted"))
    for _ in range(6):
        assert root_wake.poll(con, run) in ("busy", "no-action")
        con.execute("UPDATE root_wake_intents SET not_before=NULL WHERE run_id=?", (run["id"],))
    rows = events(con, run)
    assert len(rows) == 1 and rows[0]["attempts"] == 0 and rows[0]["state"] == "retry"


def test_detached_and_rebound_root_never_receives_a_wrong_pane_prompt(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    with db.transaction(con):
        state.emit(con, run, "gate.recheck", "review finished")
        con.execute("UPDATE session_bindings SET ended_at=? WHERE harness='herdr' AND session_id=?", ("2026-01-01", PANE))
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: pytest.fail("stale binding was prompted"))
    assert root_wake.poll(con, run) == "detached"
    assert {x["state"] for x in events(con, run)} == {"awaiting_attachment"}
    assert "awaiting attachment 1" in root_wake.lines(con, run)[0]
    with db.transaction(con):
        discovery.bind(con, run, [("herdr", PANE)], "resume")
    assert "enabled" in root_wake.attach(con, run)
    got = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: got.append(a) or "landed")
    assert root_wake.poll(con, run) == "delivered"
    assert len(got) == 1


def test_unknown_prompt_delivery_requires_manual_inspection_not_duplicate_retries(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    with db.transaction(con):
        state.emit(con, run, "task.blocked", "worker refused")
    calls = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *args, **kw: calls.append(args) or "")
    assert root_wake.poll(con, run) == "unknown"
    assert root_wake.poll(con, run) == "no-action"
    assert len(calls) == 1
    assert events(con, run)[0]["state"] == "awaiting_attachment"


def test_723_background_supervisor_wakes_idle_root_and_root_dispatches_within_five_seconds(env, monkeypatch):
    """Actual detached Office supervisor + local fake Herdr CLI, not just a mocked poll.

    The fake root agent is a process invoked *only* by Herdr's agent prompt. It
    reads context then dispatches T2. A worker-completion event is the sole trigger.
    """
    import json
    import subprocess
    import sys
    import time
    from pathlib import Path

    approved_run(env, plan=PLAN_TWO)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0])
    marker = env.tmp / "root-was-prompted.json"
    responded = env.tmp / "root-auto-continued.json"
    responder = env.tmp / "fake-root-responder.py"
    responder.write_text('''import json, os, subprocess, sys, time\n'''
                         '''from pathlib import Path\n'''
                         '''time.sleep(0.3)  # wait until prompt has been marked delivered\n'''
                         '''cmd = [sys.executable, "-m", "office", "--run", sys.argv[1]]\n'''
                         '''a = subprocess.run(cmd + ["context"], capture_output=True, text=True)\n'''
                         '''b = subprocess.run(cmd + ["dispatch", "T2"], capture_output=True, text=True,\n'''
                         '''                   env={**os.environ, "OFFICE_WORKER_LAUNCHER": "external"})\n'''
                         '''Path(os.environ["FAKE_RESPONDED"]).write_text(json.dumps({"context": a.returncode, "dispatch": b.returncode,\n'''
                         '''                                                  "stdout": b.stdout + b.stderr}))\n''')
    herdr = env.bin / "herdr"
    herdr.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
pane = os.environ["FAKE_WAKE_PANE"]
name = "root-claude"
marker = Path(os.environ["FAKE_WAKE_MARKER"])
if args[:2] == ["agent", "list"]:
    result = {"agents": [{"name": name, "agent": "claude", "pane_id": pane}]}
elif args[:2] == ["agent", "get"]:
    result = {"agent": {"name": name, "agent": "claude", "pane_id": pane, "status": "idle"}}
elif args[:2] == ["agent", "read"]:
    print("esc to interrupt\\nctx: 22k" if marker.exists() else "idle")
    sys.exit(0)
elif args[:2] == ["agent", "prompt"]:
    if not marker.exists():
        marker.write_text(json.dumps({"agent": args[2], "message": args[3]}))
        subprocess.Popen([sys.executable, os.environ["FAKE_RESPONDER"], os.environ["FAKE_WAKE_RUN_ID"]],
                         cwd=os.environ["FAKE_WAKE_REPO"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = {"ok": True}
else:
    result = {}
print(json.dumps({"result": result}))
''')
    herdr.chmod(0o755)
    for key, value in {
        "HERDR_PANE_ID": PANE, "FAKE_WAKE_PANE": PANE, "FAKE_WAKE_MARKER": str(marker),
        "FAKE_WAKE_RUN_ID": run["id"], "FAKE_WAKE_REPO": str(env.repo),
        "FAKE_RESPONDER": str(responder), "FAKE_RESPONDED": str(responded),
    }.items():
        monkeypatch.setenv(key, value)
    code, output = env.office("resume", "--auto-wake", env={"HERDR_PANE_ID": PANE})
    assert code == 0 and "auto-wake enabled" in output, output
    try:
        start = time.monotonic()
        with db.transaction(con):
            state.emit(con, run, "gate.recheck", "reviewer returned actionable RECHECK", audience="orchestrator")
        while not marker.exists() and time.monotonic() - start < 5:
            time.sleep(0.05)
        elapsed = time.monotonic() - start
        print(f"ISSUE_723_FAKE_HERDR_WAKE_LATENCY_MS={int(elapsed*1000)}")
        assert marker.exists() and elapsed < 5, f"no wake within five seconds: {elapsed:.2f}s"
        assert json.loads(marker.read_text())["agent"] == "root-claude"
        while not responded.exists() and time.monotonic() - start < 10:
            time.sleep(0.05)
        assert responded.exists(), (env.tmp / "root-wake.log")
        result = json.loads(responded.read_text())
        assert result["context"] == result["dispatch"] == 0, result
        assert state.get_task(con, run["id"], "T2")["status"] == "running"
        print(f"ISSUE_723_FAKE_HERDR_CONTINUATION_MS={int((time.monotonic()-start)*1000)}")
        assert {x["state"] for x in events(con, run)} & {"acted"}
        assert root_wake.metrics(con, run)["last_delivery_ms"] < 5000
    finally:
        # Process exits when the opt-in is revoked; no background test pollution.
        current = root_wake._binding(con, run["id"])
        pid = current["supervisor_pid"] if current else None
        root_wake.attach(con, run, off=True)
        from office.util import pid_alive
        deadline = time.monotonic() + 3
        while pid and pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
