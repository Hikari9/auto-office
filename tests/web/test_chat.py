"""Chat goes to an orchestrator only, once, and reports delivery honestly."""
from __future__ import annotations

import pytest

from office import db
from office.web import server, synthetic
from office.web.service import Command, CommandRefused

RUN = "CHAT-RUN"
PANE = "pane-7"
SESSION = f"session:{RUN}/herdr/{PANE}"


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    s = server.build_fixture("small", home=tmp_path / "fx").start()
    con = db.connect(s.db_path)
    with db.transaction(con):
        synthetic.insert_run(con, RUN, git_common_dir="/synthetic/src/repo-00/.git")
        synthetic.insert_binding(con, RUN, "herdr", PANE)
        synthetic.insert_dispatch(con, "DCHAT1", RUN, task_id="T1", status="running")
    con.close()
    s.poll()
    s.launcher.live.add(PANE)
    yield s
    s.close()


def send(svc, cid, text="hello", target=None, **payload):
    target = target or {"host": svc.host_id, "run_id": RUN, "session": SESSION}
    return svc.submit(Command.parse({"id": cid, "kind": "chat_send", "target": target,
                                     "payload": {"text": text, **payload}}), wait=True)


def refused(svc, cid, **kw):
    with pytest.raises(CommandRefused) as info:
        send(svc, cid, **kw)
    return info.value


def test_landed_is_completed(svc):
    out = send(svc, "chat-0000001")
    assert out["status"] == "completed" and out["result"]["delivery"] == "landed"
    assert svc.launcher.sent == [(PANE, "hello")]


@pytest.mark.parametrize("got", ["held", ""])
def test_held_or_unconfirmed_is_unknown_and_never_retried(svc, got):
    svc.launcher.send_result = got
    out = send(svc, "chat-0000002")
    assert out["status"] == "unknown" and "not retried" in out["error"]
    assert len(svc.launcher.sent) == 1
    svc.poll()
    svc.admit_queue()
    assert send(svc, "chat-0000002")["replayed"] is True
    assert len(svc.launcher.sent) == 1


def test_agent_gone_at_delivery_is_failed(svc):
    class Vanishing(type(svc.launcher)):
        checks = 0

        def agent_live(self, pane):
            Vanishing.checks += 1
            return Vanishing.checks == 1
    launcher = Vanishing()
    launcher.live.add(PANE)
    svc.launcher = launcher
    out = send(svc, "chat-0000003")
    assert out["status"] == "failed" and launcher.sent == []


def test_resend_needs_a_new_id_naming_an_earlier_chat(svc):
    svc.launcher.send_result = "held"
    send(svc, "chat-0000004")
    assert refused(svc, "chat-0000005", resend_of="nope-0000000").reason == "bad-resend"
    assert refused(svc, "chat-0000012", resend_of="chat-0000012").reason == "bad-resend"
    assert len(svc.launcher.sent) == 1
    svc.launcher.send_result = "landed"
    out = send(svc, "chat-0000006", resend_of="chat-0000004")
    assert out["status"] == "completed" and out["resend_of"] == "chat-0000004"
    assert len(svc.launcher.sent) == 2


def test_worker_and_reviewer_targets_are_refused(svc):
    target = {"host": svc.host_id, "run_id": RUN, "session": "dispatch:DCHAT1"}
    assert refused(svc, "chat-0000007", target=target).reason == "not-orchestrator"
    assert svc.launcher.sent == []


def test_ended_binding_is_refused(svc):
    con = db.connect(svc.db_path)
    with db.transaction(con):
        con.execute("UPDATE session_bindings SET ended_at='t' WHERE session_id=?", (PANE,))
    con.close()
    svc.poll()
    assert refused(svc, "chat-0000008").reason == "binding-ended"


def test_no_live_agent_on_the_pane_is_refused(svc):
    svc.launcher.live.clear()
    assert refused(svc, "chat-0000009").reason == "agent-not-live"


def test_foreign_host_is_refused(svc):
    target = {"host": "host:someone-else", "run_id": RUN, "session": SESSION}
    assert refused(svc, "chat-0000010", target=target).reason == "foreign-host"


def test_web_launched_orchestrator_is_reached_through_its_recorded_pane(svc):
    con = db.connect(svc.db_path)
    with db.transaction(con):
        synthetic.insert_run(con, "CHAT-2", git_common_dir="/synthetic/src/repo-00/.git")
        synthetic.insert_binding(con, "CHAT-2", "claude", "S-claude")
    con.close()
    svc.launches["cmd-x"] = {"pane": "pane-9", "issue": None, "repo": None, "run": "CHAT-2", "kind": "resume_run"}
    svc.poll(force=True)
    svc.launcher.live.add("pane-9")
    out = send(svc, "chat-0000011", target={"run_id": "CHAT-2", "session": "session:CHAT-2/claude/S-claude"})
    assert out["status"] == "completed" and svc.launcher.sent[-1][0] == "pane-9"
