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
    assert {x["state"] for x in events(con, run)} == {"root_activity_observed"}
    assert all(x["root_activity_at"] and x["acted_at"] is None for x in events(con, run))
    assert all(x["root_activity_command"] == "dispatch" for x in events(con, run))


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
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason="decision required")
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
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason="worker blocked")
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
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason="review needs decision")
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
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason="worker refused")
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

    approved_run(env, plan=PLAN_TWO, executor=[{"submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0])
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?",
                         (task_row(env)["current_dispatch_id"],)).fetchone())
    wt = Path(d["worktree"])
    (wt / "calc.py").write_text(GOOD_ADD)
    self_reviewed(wt, "calc.py")
    worker_env = {"OFFICE_RUN_ID": run["id"], "OFFICE_DISPATCH_ID": d["id"],
                  "OFFICE_TASK_ID": "T1", "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}
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
        code, out = env.office("submit", cwd=wt, env=worker_env)
        assert code == 0, out
        start = time.monotonic()
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
        assert {x["state"] for x in events(con, run)} & {"root_activity_observed"}
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


def test_723_silent_stall_is_a_separate_deduplicated_event_and_wakes_idle_root(env, monkeypatch):
    """A stuck pane generates a slow-watch event once; fast notifier only sees DB events."""
    from office import guide, questions
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    symptoms = ["T1 reviewer D-test no output or progress for 900s"]
    monkeypatch.setattr(questions, "scan", lambda *a, **kw: ([], [], {}))
    monkeypatch.setattr(guide, "stalls", lambda *a, **kw: list(symptoms))
    calls = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: calls.append(a) or "landed")
    assert root_wake.watchdog_tick(con, run) == "alerted"
    assert root_wake.watchdog_tick(con, run) == "unchanged"
    assert con.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='watchdog.stall'", (run["id"],)).fetchone()[0] == 1
    assert root_wake.poll(con, run) == "delivered"
    assert len(calls) == 1
    symptoms.clear()
    assert root_wake.watchdog_tick(con, run) == "cleared"
    symptoms.append("T1 reviewer D-test no output or progress for 1200s")
    assert root_wake.watchdog_tick(con, run) == "alerted"  # new episode, not a ticking timer
    assert root_wake.poll(con, run) == "delivered"
    assert len(calls) == 2



def test_723_settled_event_and_duplicate_do_not_prompt_unmodified_guidance(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: pytest.fail("no authorized change"))
    with db.transaction(con):
        state.emit(con, run, "gate.recheck", "stale reviewer event")
    assert root_wake.poll(con, run) == "no-action"
    assert {x["state"] for x in events(con, run)} == {"settled"}
    # A genuine new task state is different even though it uses the same
    # broad event name; no hardcoded event->permission mapping.
    delivered = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: delivered.append(a) or "landed")
    with db.transaction(con):
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason="needs repair")
        state.emit(con, run, "gate.recheck", "new review finding")
    assert root_wake.poll(con, run) == "delivered"
    assert len(delivered) == 1
    with db.transaction(con):
        state.emit(con, run, "gate.recheck", "repeated result, no new work")
    assert root_wake.poll(con, run) == "no-action"
    assert len(delivered) == 1
    assert events(con, run)[-1]["state"] == "settled"


def test_723_settled_between_claim_and_send_is_not_delivered(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    with db.transaction(con):
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason="needs repair")
        state.emit(con, run, "task.blocked", "T1 needs repair")
    def changed_during_probe(pane):
        with db.transaction(con):
            state.update_task(con, run["id"], "T1", status="planned", pause_reason=None)
        return "root-claude", "idle"
    monkeypatch.setattr(root_wake, "_agent_in_pane", changed_during_probe)
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: pytest.fail("stale state"))
    assert root_wake.poll(con, run) == "no-action"
    assert events(con, run)[0]["state"] == "settled"


def test_723_human_only_guidance_is_raised_once_without_granting_authority(env, monkeypatch):
    from office.util import now_iso
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    got = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: got.append(a) or "landed")
    with db.transaction(con):
        con.execute("UPDATE authorizations SET revoked_at=? WHERE run_id=? AND kind='plan'",
                    (now_iso(), run["id"]))
        state.emit(con, run, "authority.plan", "new plan authorization needed")
    assert root_wake.poll(con, run) == "delivered"
    assert len(got) == 1 and "HUMAN DECISION REQUIRED" in got[0][1]
    assert state.active_authorization(con, run, "plan") is None
    with db.transaction(con):
        state.emit(con, run, "authority.plan", "duplicate authorization reminder")
    assert root_wake.poll(con, run) == "no-action"


def test_723_preexisting_human_blocker_not_reawakened_by_stale_event(env, monkeypatch):
    from conftest import start_inline
    start_inline(env)
    con, run = setup_root(env, monkeypatch)  # before plan is authorized
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: pytest.fail("no new decision"))
    with db.transaction(con):
        state.emit(con, run, "gate.recheck", "old event; still awaiting user approval")
    assert root_wake.poll(con, run) == "no-action"


def test_723_durable_worker_stop_is_actionable_while_guide_says_wait(env, monkeypatch):
    approved_run(env)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con, run = setup_root(env, monkeypatch)
    d = state.get_task(con, run["id"], "T1")["current_dispatch_id"]
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: sent.append(a) or "landed")
    with db.transaction(con):
        state.emit(con, run, "worker.signal", "executor terminal preflight stop", task_id="T1",
                   dispatch_id=d, payload={"reason": "self-review cap", "source": "preflight"})
    assert root_wake.poll(con, run) == "delivered"
    assert len(sent) == 1
    assert "Auto Office run" in sent[0][1]


def test_723_unrelated_root_event_records_activity_not_completion(env, monkeypatch):
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: "landed")
    with db.transaction(con):
        state.update_task(con, run["id"], "T1", status="blocked", pause_reason="unfinished finding")
        state.emit(con, run, "task.blocked", "pending finding", task_id="T1")
    assert root_wake.poll(con, run) == "delivered"
    monkeypatch.setattr(discovery, "session_keys", lambda: [("herdr", PANE)])
    root_wake.observe(con, run)
    before = con.execute("SELECT MAX(seq) FROM events WHERE run_id=?", (run["id"],)).fetchone()[0]
    with db.transaction(con):
        state.emit(con, run, "setup.done", "unrelated housekeeping", audience="runtime")
    root_wake.record_root_activity(con, run, before=before, command="setup")
    rows = events(con, run)
    assert rows[0]["state"] == "root_activity_observed"
    assert rows[0]["root_activity_at"] and rows[0]["root_activity_command"] == "setup"
    assert rows[0]["root_activity_event_seq"] > before
    assert rows[0]["acted_at"] is None
    assert state.get_task(con, run["id"], "T1")["status"] == "blocked"


def test_723_new_review_record_wakes_even_when_short_next_action_is_unchanged(env, monkeypatch):
    from office import guide
    approved_run(env)
    con, run = setup_root(env, monkeypatch)
    before = guide.next_action(con, run)
    seen = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: seen.append(a) or "landed")
    with db.transaction(con):
        con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, "
                    "verdict, summary, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("G-new-review", run["id"], "task", "T1", "code", "review-result", "done",
                     "RECHECK", "reviewer finished with a new finding", "2026-10-19T12:00:00+00:00"))
        state.emit(con, run, "gate.recheck", "fresh independent review verdict", task_id="T1")
    assert guide.next_action(con, run) == before
    assert root_wake.poll(con, run) == "delivered"
    assert len(seen) == 1
    with db.transaction(con):
        state.emit(con, run, "gate.recheck", "duplicate review report", task_id="T1")
    assert root_wake.poll(con, run) == "no-action"


@pytest.mark.parametrize("status,expected", [("done", "delivered"), ("idle", "delivered"),
                                             ("working", "busy"), ("blocked", "unsupported")])
def test_723_root_whose_turn_finished_unfocused_is_idle_for_delivery(env, monkeypatch, status, expected):
    """Herdr reports a finished, unfocused Claude turn as `done`, not `idle`.
    That is the normal state of an unattended root, so it must still be woken."""
    approved_run(env, plan=PLAN_TWO, executor=[{"submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con, run = setup_root(env, monkeypatch)
    monkeypatch.setattr(root_wake, "_agent_in_pane",
                        lambda pane: ("root-claude", status) if pane == PANE else None)
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt",
                        lambda name, text, *, pane=None: sent.append(name) or "landed")
    with db.transaction(con):
        state.emit(con, run, "task.paused", "fake actionable pause", task_id="T1")
    with db.transaction(con):
        con.execute("UPDATE root_wake_bindings SET last_action_signature='stale' WHERE run_id=?", (run["id"],))
    assert root_wake.poll(con, run) == expected
    assert bool(sent) == (expected == "delivered")


@pytest.mark.parametrize("landing", ["landed", ""])
def test_723_root_reading_context_during_landing_confirmation_is_observed(env, monkeypatch, landing):
    """Live pilot: Herdr confirms a landing seconds after the prompt arrives, and the
    root runs `office context` before that. Its observation must not be dropped,
    and a late or unknown transport result must not downgrade it."""
    approved_run(env, plan=PLAN_TWO, executor=[{"submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con, run = setup_root(env, monkeypatch)

    def send(name, text, *, pane=None):
        root_wake.observe(con, run)  # root reacts before submit_prompt returns
        return landing

    monkeypatch.setattr(dispatch, "submit_prompt", send)
    with db.transaction(con):
        state.emit(con, run, "task.paused", "fake actionable pause", task_id="T1")
    with db.transaction(con):
        con.execute("UPDATE root_wake_bindings SET last_action_signature='stale' WHERE run_id=?", (run["id"],))
    root_wake.poll(con, run)
    rows = events(con, run)
    assert rows and {r["state"] for r in rows} == {"observed"}
    assert all(r["observed_at"] and r["token"] is None for r in rows)


def test_723_wake_off_cannot_complete_between_final_check_and_prompt(env, monkeypatch):
    """Review P1: --wake-off or a rebind racing an in-flight poll must not slip in after
    the final binding check; it waits for the send, and a later poll sends nothing."""
    import threading
    import time as _time
    approved_run(env, plan=PLAN_TWO, executor=[{"submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con, run = setup_root(env, monkeypatch)
    finished = []

    def wake_off():
        other = db.connect()
        try:
            root_wake.attach(other, run, off=True)
            finished.append(_time.monotonic())
        finally:
            other.close()

    sent = []

    def send(name, text, *, pane=None):
        t = threading.Thread(target=wake_off)
        t.start()
        _time.sleep(0.5)
        assert t.is_alive() and not finished, "wake-off completed while a prompt was in flight"
        sent.append(_time.monotonic())
        send.thread = t
        return "landed"

    monkeypatch.setattr(dispatch, "submit_prompt", send)
    with db.transaction(con):
        state.emit(con, run, "task.paused", "fake actionable pause", task_id="T1")
    with db.transaction(con):
        con.execute("UPDATE root_wake_bindings SET last_action_signature='stale' WHERE run_id=?", (run["id"],))
    assert root_wake.poll(con, run) == "delivered"
    send.thread.join(10)
    assert finished and finished[0] >= sent[0]
    assert root_wake._binding(con, run["id"]) is None
    with db.transaction(con):
        state.emit(con, run, "task.paused", "after wake-off", task_id="T1")
    assert root_wake.poll(con, run) == "no-action" and len(sent) == 1


def _pending_wake(env, monkeypatch):
    approved_run(env, plan=PLAN_TWO, executor=[{"submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con, run = setup_root(env, monkeypatch)
    with db.transaction(con):
        state.emit(con, run, "task.paused", "fake actionable pause", task_id="T1")
    with db.transaction(con):
        con.execute("UPDATE root_wake_bindings SET last_action_signature='stale' WHERE run_id=?", (run["id"],))
    return con, run


def test_723_late_landing_receipt_keeps_root_activity(env, monkeypatch):
    """Review P2: context, then a state-changing root command, both before Herdr
    confirms the landing. The late receipt must not demote root activity."""
    con, run = _pending_wake(env, monkeypatch)

    def send(name, text, *, pane=None):
        root_wake.observe(con, run)
        before = con.execute("SELECT MAX(seq) FROM events WHERE run_id=?", (run["id"],)).fetchone()[0]
        with db.transaction(con):
            state.emit(con, run, "note", "root command", audience="runtime")
        root_wake.record_root_activity(con, run, before=before, command="dispatch")
        return "landed"

    monkeypatch.setattr(dispatch, "submit_prompt", send)
    assert root_wake.poll(con, run) == "delivered"
    rows = events(con, run)
    assert {r["state"] for r in rows} == {"root_activity_observed"}
    assert all(r["delivered_at"] and r["token"] is None for r in rows)


def test_723_root_that_turns_busy_before_the_send_is_deferred(env, monkeypatch):
    """Review P2: the idle check is repeated at the delivery boundary."""
    con, run = _pending_wake(env, monkeypatch)
    statuses = iter(["done", "working"])
    monkeypatch.setattr(root_wake, "_agent_in_pane",
                        lambda pane: ("root-claude", next(statuses)) if pane == PANE else None)
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda name, text, *, pane=None: sent.append(1) or "landed")
    assert root_wake.poll(con, run) == "busy" and not sent
    assert {r["state"] for r in events(con, run)} == {"retry"}
