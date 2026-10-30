"""#922 and #919: root wake is armed by default, status reports wake, and review gates past a bound are classified.

Herdr agent identity, pane activity and gate ownership are scripted with monkeypatch; no real pane or model is called.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_issue_895_false_stalls import _ago, _reviewer_gate
from test_root_wake_723 import PANE, setup_root
from test_wait import _go

from office import context_snapshot, db, guide, rerun, root_wake, state, watchdog
from office import gates as gates_mod

REPO = Path(__file__).resolve().parents[2]


def _run(con) -> dict:
    return state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])


def _review_gate(con, run, gate_id, *, kind="lane", status="queued", created_for=0.0, finished_for=None):
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, plan_version, kind, input_key, status, "
                "verdict, created_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (gate_id, run["id"], "task", "T1", None, run.get("plan_version"), kind, f"k-{gate_id}", status,
                 "PASS" if status == "done" else None, _ago(created_for),
                 _ago(finished_for) if finished_for is not None else None))
    con.commit()


def _verified_root(monkeypatch):
    monkeypatch.setattr(root_wake, "ensure", lambda run: None)
    monkeypatch.setattr(root_wake, "_agent_in_pane", lambda pane: ("root-claude", "idle") if pane == PANE else None)


# --- #922: resume arms wake by default ------------------------------------------------------------------------------


def test_resume_in_a_verified_root_pane_arms_wake_without_a_flag(env, monkeypatch):
    _go(env)
    _verified_root(monkeypatch)
    code, out = env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    con = env.con()
    assert root_wake.armed(con, _run(con)), out
    assert "auto-wake enabled for the verified Claude agent root-claude" in out, out
    assert "Refused" not in out


def test_auto_wake_flag_is_still_accepted_and_arms_the_same_way(env, monkeypatch):
    _go(env)
    _verified_root(monkeypatch)
    code, out = env.office("resume", "--auto-wake", check=0, env={"HERDR_PANE_ID": PANE})
    assert "auto-wake enabled" in out, out


def test_wake_off_disables_the_default_and_status_says_so(env, monkeypatch):
    _go(env)
    _verified_root(monkeypatch)
    env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    code, out = env.office("resume", "--wake-off", check=0, env={"HERDR_PANE_ID": PANE})
    con = env.con()
    assert not root_wake.armed(con, _run(con))
    assert "root auto-wake disabled" in out, out
    assert any(line.startswith("wake: off (--wake-off)") for line in root_wake.lines(con, _run(con)))


def test_resume_outside_herdr_succeeds_with_a_notice_and_no_wake(env, monkeypatch):
    _go(env)
    armed = []
    monkeypatch.setattr(root_wake, "ensure", lambda run: armed.append(run["id"]))
    code, out = env.office("resume", check=0, env={"HERDR_PANE_ID": ""})
    con = env.con()
    assert not root_wake.armed(con, _run(con)) and armed == [], out
    assert "root auto-wake not armed" in out and "Refused" not in out, out
    assert root_wake.lines(con, _run(con)) == ["wake: unavailable (not inside a Herdr pane); use office wait"]


def test_resume_with_an_unverifiable_pane_succeeds_with_a_notice_and_no_wake(env, monkeypatch):
    _go(env)
    monkeypatch.setattr(root_wake, "ensure", lambda run: None)
    monkeypatch.setattr(root_wake, "_agent_in_pane", lambda pane: None)
    code, out = env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    con = env.con()
    assert not root_wake.armed(con, _run(con)), out
    assert "did not verify" in out and "Refused" not in out, out


def test_status_prints_the_wake_state_on_every_non_terminal_run(env, monkeypatch):
    _go(env)
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": ""})
    assert "wake: unavailable (not inside a Herdr pane)" in out, out
    _verified_root(monkeypatch)
    env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": PANE})
    assert "wake: armed (Claude agent root-claude in Herdr pane " + PANE in out, out


def test_status_names_office_wait_when_work_runs_with_no_wake_and_no_wait(env, monkeypatch):
    _go(env)  # T1's executor is running: live work
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": ""})
    assert "next: office wait" in out, out


def test_status_suppresses_office_wait_only_while_wake_is_armed_or_a_root_wait_is_in_flight(env, monkeypatch):
    _go(env)
    con = env.con()
    run = _run(con)
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": ""})
    assert "next: office wait" in out, out  # positive control: unarmed, no wait
    with guide.waiting(run["id"]):
        code, out = env.office("status", check=0, env={"HERDR_PANE_ID": ""})
        assert "next: office wait" not in out, out
    setup_root(env, monkeypatch)
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": PANE})
    assert "next: office wait" not in out, out


def test_an_executor_wait_does_not_stand_in_for_the_root_wait(env, monkeypatch):
    _go(env)
    run = _run(env.con())
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "D-executor")
    with guide.waiting(run["id"]):
        assert not guide.wait_in_flight(run["id"])


def test_a_wait_marker_left_by_a_dead_process_is_not_in_flight(env):
    _go(env)
    run = _run(env.con())
    marker = guide._wait_marker(run["id"])
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("stale")
    assert not guide.wait_in_flight(run["id"])


def test_office_context_carries_the_turn_rule(env):
    _go(env)
    code, out = env.office("context", check=0)
    assert context_snapshot.TURN_RULE in out, out


@pytest.mark.parametrize("doc", ["SKILL.md", "docs/orchestrator-reference.md"])
def test_docs_describe_default_on_wake_and_the_turn_rule(doc):
    text = (REPO / doc).read_text()
    assert "is on by default" in text and "`--wake-off`" in text, doc
    assert "arm `office wait` instead of ending the turn" in text, doc


# --- #919: review gates classified and emitted once per episode -----------------------------------------------------


def test_review_event_kinds_are_actionable_and_their_bounds_come_from_env(monkeypatch):
    assert set(watchdog.REVIEW_EVENT_KINDS) <= root_wake.ACTIONABLE
    assert set(watchdog.REVIEW_EVENT_KINDS) <= guide._URGENT_KINDS
    monkeypatch.setenv("OFFICE_REVIEW_QUEUED_S", "42")
    assert watchdog.threshold("queued_review") == 42
    monkeypatch.setenv("OFFICE_REVIEW_UNREAD_S", "not-a-number")
    assert watchdog.threshold("closed_unread") == 900


def test_a_queued_review_past_its_bound_is_one_episode_with_a_concrete_next(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_QUEUED_S", "60")
    monkeypatch.setattr(gates_mod, "owner_state", lambda con, run, g: ("queued", "job J1 is queued"))
    con = env.con()
    run = _run(con)
    _review_gate(con, run, "GQ1", created_for=600)
    episodes = watchdog.review_episodes(con, run, probe=False)
    assert [(e["kind"], e["episode"]) for e in episodes] == [("review.queued_too_long", "GQ1")]
    for _ in range(2):
        with db.transaction(con):
            watchdog.record_review_episodes(con, run, episodes)
    events = con.execute("SELECT kind FROM events WHERE run_id=? AND kind='review.queued_too_long'", (run["id"],)).fetchall()
    assert len(events) == 1
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": ""})
    assert "next: office revoke T1, then office review T1" in out, out


def test_a_queued_review_within_its_bound_emits_nothing(env, monkeypatch):
    _go(env)
    monkeypatch.setattr(gates_mod, "owner_state", lambda con, run, g: ("queued", "job J1 is queued"))
    con = env.con()
    run = _run(con)
    _review_gate(con, run, "GQ2", created_for=0.0)
    assert watchdog.review_episodes(con, run, probe=False) == []


def test_a_stalled_review_is_classified_stalled_once_with_the_reviewer_next_step(env, monkeypatch):
    con, run, gate_id, did = _reviewer_gate(env, monkeypatch, kind="lane", stale_for=7200.0)
    episodes = [e for e in watchdog.review_episodes(con, run, probe=True) if e["kind"] == "review.stalled"]
    assert [e["episode"].split(":")[0] for e in episodes] == [did]
    assert episodes[0]["next"].startswith("inspect/stop the attempt"), episodes[0]
    with db.transaction(con):
        watchdog.record_review_episodes(con, run, episodes)
        watchdog.record_review_episodes(con, run, episodes)
    rows = con.execute("SELECT summary FROM events WHERE run_id=? AND kind='review.stalled'", (run["id"],)).fetchall()
    assert len(rows) == 1


def test_status_prints_a_stalled_reviewer_once_and_reads_its_pane_once(env, monkeypatch):
    con, run, _, did = _reviewer_gate(env, monkeypatch, kind="lane", stale_for=7200.0)
    probes = []
    monkeypatch.setattr(rerun, "agent_activity", lambda d: probes.append(d["id"]) or {
        "alive": True, "busy": False, "hash": "h-review", "text": "Reviewing the diff.\n", "status": "idle", "question": None})
    code, out = env.office("status", check=0)
    blockers = [line for line in out.splitlines() if line.startswith("blocker:") and did in line]
    assert len(blockers) == 1, out
    assert probes.count(did) == 1, probes


def test_a_healthy_running_review_emits_nothing(env, monkeypatch):
    con, run, _, _ = _reviewer_gate(env, monkeypatch, kind="lane", stale_for=0.0, started_for=0.0)
    assert watchdog.review_episodes(con, run, probe=True) == []


def test_a_closed_review_with_an_unread_verdict_is_closed_unread_until_read(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_UNREAD_S", "60")
    con = env.con()
    run = _run(con)
    _review_gate(con, run, "GU1", status="done", created_for=900, finished_for=900)
    con.execute("INSERT INTO events(run_id, kind, audience, task_id, summary, payload_json, office_version, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (run["id"], "gate.pass", "runtime", "T1", "lane PASS", "{}", "test", _ago(899)))
    con.commit()
    episodes = watchdog.review_episodes(con, run, probe=False)
    assert [e["kind"] for e in episodes] == ["review.closed_unread"], episodes
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": ""})
    assert "next: office context to read the T1 verdict" in out, out
    assert watchdog.review_episodes(con, _run(con), probe=False) == []


def test_a_review_wall_episode_has_a_real_next_step(env, monkeypatch):
    monkeypatch.setenv("OFFICE_REVIEW_WALL_S", "3600")
    con, run, _, did = _reviewer_gate(env, monkeypatch, kind="lane", stale_for=7200.0, started_for=7200.0)
    stalled = [e for e in watchdog.review_episodes(con, run, probe=True) if e["kind"] == "review.stalled"]
    assert [e["episode"].split(":")[0] for e in stalled] == [did], stalled
    assert "review wall budget" in stalled[0]["text"]
    assert stalled[0]["next"].startswith("inspect its output, then office review T1"), stalled[0]


def test_a_read_verdict_is_not_reflagged_by_another_tasks_later_gate_event(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_UNREAD_S", "60")
    con = env.con()
    run = _run(con)
    _review_gate(con, run, "GU2", status="done", created_for=900, finished_for=900)
    con.execute("INSERT INTO events(run_id, kind, audience, task_id, summary, payload_json, office_version, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (run["id"], "gate.pass", "runtime", "T1", "lane PASS", "{}", "test", _ago(899)))
    con.commit()
    env.office("status", check=0, env={"HERDR_PANE_ID": ""})  # the root reads the verdict
    con.execute("INSERT INTO events(run_id, kind, audience, task_id, summary, payload_json, office_version, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (run["id"], "gate.approved", "runtime", "T2", "other PASS", "{}", "test", _ago(100)))
    con.commit()
    assert watchdog.review_episodes(con, _run(con), probe=False) == []
