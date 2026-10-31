"""#922 and #919: root wake is armed by default, status reports wake, and review gates past a bound are classified.

Herdr agent identity, pane activity and gate ownership are scripted with monkeypatch; no real pane or model is called.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_issue_895_false_stalls import _ago, _reviewer_gate
from test_root_wake_723 import PANE, setup_root
from test_wait import EXTERNAL, _go

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


def test_a_review_wall_episode_has_a_real_next_step(env, monkeypatch):
    monkeypatch.setenv("OFFICE_REVIEW_WALL_S", "3600")
    con, run, _, did = _reviewer_gate(env, monkeypatch, kind="lane", stale_for=7200.0, started_for=7200.0)
    stalled = [e for e in watchdog.review_episodes(con, run, probe=True) if e["kind"] == "review.stalled"]
    assert [e["episode"].split(":")[0] for e in stalled] == [did], stalled
    assert "review wall budget" in stalled[0]["text"]
    assert stalled[0]["next"].startswith("inspect its output, then office review T1"), stalled[0]


def _closed_review(con, run, gate_id="GU1", *, finished_for=900):
    _review_gate(con, run, gate_id, status="done", created_for=finished_for, finished_for=finished_for)


def _event(con, run, kind, task, ago, audience="runtime"):
    con.execute("INSERT INTO events(run_id, kind, audience, task_id, summary, payload_json, office_version, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (run["id"], kind, audience, task, kind, "{}", "test", _ago(ago)))
    con.commit()


def test_a_closed_review_with_an_unread_verdict_is_closed_unread_until_status_shows_it(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_UNREAD_S", "60")
    con = env.con()
    run = _run(con)
    _closed_review(con, run)
    _event(con, run, "gate.pass", "T1", 899)
    assert [e["kind"] for e in watchdog.review_episodes(con, run, probe=False)] == ["review.closed_unread"]
    env.office("context", check=0)  # context is read-only: it does not mark the verdict read
    assert [e["kind"] for e in watchdog.review_episodes(con, _run(con), probe=False)] == ["review.closed_unread"]
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": ""})  # shows the episode once
    assert "verdict is unread" in out, out
    assert watchdog.review_episodes(con, _run(con), probe=False) == []


def test_unrelated_consumption_does_not_clear_an_unread_verdict(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_UNREAD_S", "60")
    con = env.con()
    run = _run(con)
    _closed_review(con, run)
    _event(con, run, "gate.pass", "T1", 899)
    _event(con, run, "task.blocked", "T3", 100, audience="orchestrator")
    with db.transaction(con):
        state.consume_events(con, run["id"], state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=None))
    assert [e["kind"] for e in watchdog.review_episodes(con, _run(con), probe=False)] == ["review.closed_unread"]


def test_a_queued_review_with_a_live_job_is_not_routed_to_revoke(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_QUEUED_S", "60")
    monkeypatch.setattr(gates_mod, "owner_state", lambda con, run, g: ("alive", "job J2 is running"))
    con = env.con()
    run = _run(con)
    _review_gate(con, run, "GQ3", created_for=600)
    (ep,) = watchdog.review_episodes(con, run, probe=False)
    assert ep["kind"] == "review.queued_too_long" and "revoke" not in ep["next"].split(";")[0], ep
    assert ep["next"].startswith("office wait (review job is alive)"), ep


def test_queued_and_closed_unread_episodes_are_news_not_a_stall(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_QUEUED_S", "60")
    monkeypatch.setattr(gates_mod, "owner_state", lambda con, run, g: ("queued", "job J3 is queued"))
    con = env.con()
    run = _run(con)
    _review_gate(con, run, "GQ4", created_for=600)
    assert not any("queued" in line for line in guide.stalls(con, run))


def test_a_second_root_wait_does_not_block_on_the_marker_lock(env):
    _go(env)
    run = _run(env.con())
    with guide.waiting(run["id"]):
        with guide.waiting(run["id"]):  # would deadlock if the marker lock were blocking
            assert guide.wait_in_flight(run["id"])
        assert guide.wait_in_flight(run["id"])


def test_status_reports_a_stalled_review_once_across_two_status_calls(env, monkeypatch):
    con, run, _, did = _reviewer_gate(env, monkeypatch, kind="lane", stale_for=7200.0)
    env.office("status", check=0)
    code, out = env.office("status", check=0)
    assert len([line for line in out.splitlines() if line.startswith("blocker:") and did in line]) == 1, out
    rows = con.execute("SELECT summary FROM events WHERE run_id=? AND kind='review.stalled'", (run["id"],)).fetchall()
    assert len(rows) == 1, [r[0] for r in rows]


def test_wake_off_is_sticky_until_an_explicit_auto_wake(env, monkeypatch):
    _go(env)
    _verified_root(monkeypatch)
    env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    env.office("resume", "--wake-off", check=0, env={"HERDR_PANE_ID": PANE})
    code, out = env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    con = env.con()
    assert not root_wake.armed(con, _run(con)), out
    assert "root auto-wake is off (--wake-off); office resume --auto-wake re-arms it" in out, out
    code, out = env.office("resume", "--auto-wake", check=0, env={"HERDR_PANE_ID": PANE})
    assert root_wake.armed(con, _run(con)) and "auto-wake enabled" in out, out


def test_resume_status_reports_the_wake_state_after_arming(env, monkeypatch):
    _go(env)
    _verified_root(monkeypatch)
    code, out = env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    assert "wake: armed" in out and "wake: unavailable" not in out, out


def test_a_later_review_of_the_same_task_does_not_reflag_a_read_verdict(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_UNREAD_S", "60")
    con = env.con()
    run = _run(con)
    _closed_review(con, run, "GS1", finished_for=900)
    _event(con, run, "gate.pass", "T1", 899)
    env.office("status", check=0, env={"HERDR_PANE_ID": ""})  # shows and reads GS1's verdict
    _closed_review(con, run, "GS2", finished_for=100)
    _event(con, run, "gate.pass", "T1", 99)
    assert [e["episode"] for e in watchdog.review_episodes(con, _run(con), probe=False)] == ["GS2"]


def test_a_closed_unread_episode_is_the_first_status_line_after_the_header(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_UNREAD_S", "60")
    con = env.con()
    _closed_review(con, _run(con), "GS3", finished_for=900)
    _event(con, _run(con), "gate.pass", "T1", 899)
    code, out = env.office("status", check=0, env={"HERDR_PANE_ID": ""})
    assert out.splitlines()[1].startswith("blocker: T1 lane review GS3"), out


def test_a_reviewer_that_stalls_again_after_progress_is_a_new_episode(env, monkeypatch):
    con, run, _, did = _reviewer_gate(env, monkeypatch, kind="lane", stale_for=7200.0)
    with db.transaction(con):
        watchdog.record_review_episodes(con, run, [e for e in watchdog.review_episodes(con, run, probe=True)
                                                   if e["kind"] == "review.stalled"])
    con.execute("UPDATE dispatches SET watchdog_progress_since=? WHERE id=?", (_ago(7300.0), did))
    con.commit()
    with db.transaction(con):
        watchdog.record_review_episodes(con, run, [e for e in watchdog.review_episodes(con, run, probe=True)
                                                   if e["kind"] == "review.stalled"])
    rows = con.execute("SELECT summary FROM events WHERE run_id=? AND kind='review.stalled'", (run["id"],)).fetchall()
    assert len(rows) == 2, [r[0] for r in rows]


def test_a_stalled_reviewer_next_names_the_inspection_and_the_revoke(env, monkeypatch):
    con, run, _, did = _reviewer_gate(env, monkeypatch, kind="lane", stale_for=7200.0)
    (ep,) = [e for e in watchdog.review_episodes(con, run, probe=True) if e["kind"] == "review.stalled"]
    assert ep["next"].startswith("inspect/stop the attempt, then office review"), ep


def test_resume_kicks_jobs_before_the_wake_callback(env, monkeypatch):
    from office import jobs
    _go(env)
    order = []
    monkeypatch.setattr(jobs, "kick", lambda con, run_id: order.append("kick"))
    monkeypatch.setattr(root_wake, "arm_on_resume", lambda con, run, explicit=False: order.append("wake") or "armed")
    env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    assert order == ["kick", "wake"], order


def test_wake_off_writes_the_sticky_marker_before_disabling_the_binding(env, monkeypatch):
    _go(env)
    _verified_root(monkeypatch)
    env.office("resume", check=0, env={"HERDR_PANE_ID": PANE})
    con = env.con()
    run = _run(con)

    def no_marker(self, *args, **kwargs):
        raise OSError("read-only run dir")

    monkeypatch.setattr(Path, "touch", no_marker)
    with pytest.raises(OSError):
        root_wake.attach(con, run, off=True)
    assert root_wake.armed(con, run), "binding disabled although the sticky marker was never written"


def test_a_queued_live_job_next_is_office_wait_not_status(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_QUEUED_S", "60")
    monkeypatch.setattr(gates_mod, "owner_state", lambda con, run, g: ("alive", "job J9 is running"))
    con = env.con()
    _review_gate(con, _run(con), "GQ9", created_for=600)
    (ep,) = watchdog.review_episodes(con, _run(con), probe=False)
    assert ep["next"].startswith("office wait (review job is alive)"), ep


def test_a_queued_review_past_its_bound_wakes_the_wait_once_with_its_next(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_QUEUED_S", "60")
    monkeypatch.setattr(gates_mod, "owner_state", lambda con, run, g: ("queued", "job J1 is queued"))
    con = env.con()
    _review_gate(con, _run(con), "GW1", created_for=600)
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert code == 0, out
    assert "T1 lane review GW1 queued" in out and "next: office revoke T1, then office review T1" in out, out
    code, out = env.office("wait", "--timeout", "0.5", "--poll", "0.2", env=EXTERNAL)
    assert code == 124 and "wait: nothing new" in out, out


def test_an_unread_closed_review_wakes_the_wait_with_its_next(env, monkeypatch):
    _go(env)
    monkeypatch.setenv("OFFICE_REVIEW_UNREAD_S", "60")
    con = env.con()
    run = _run(con)
    _closed_review(con, run)
    _event(con, run, "gate.pass", "T1", 899)
    code, out = env.office("wait", "--timeout", "1", "--poll", "0.2", env=EXTERNAL)
    assert code == 0, out
    assert "verdict is unread" in out and "next: office context to read the T1 verdict" in out, out
