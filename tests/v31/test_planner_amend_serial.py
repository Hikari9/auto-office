"""#506: contract amendments coalesce; a planner never gets two live dispatches."""
from pathlib import Path
import json

from conftest import PLAN_TWO
from office import db, dispatch, plans, state


MANUAL = {"OFFICE_JOBS": "manual"}


def _dedicated_existing_plan(env):
    env.trust()
    env.script(planner=[{"plan": PLAN_TWO, "submit": True}],
               plan_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("start", "fixture goal", "--gear", "full", check=0)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    return con, state.get_run(con, run_id)


def test_two_queued_contract_amendments_keep_one_planner_and_one_revision(env, tmp_path, monkeypatch):
    con, run = _dedicated_existing_plan(env)
    env.office("amend", "T1", "--contract", "--", "first request", env=MANUAL, check=0)
    first = con.execute("SELECT id FROM dispatches WHERE role='planner' ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    env.office("amend", "T1", "--contract", "--", "second request", env=MANUAL, check=0)
    planners = con.execute("SELECT id FROM dispatches WHERE role='planner' ORDER BY started_at").fetchall()
    assert [r[0] for r in planners][-1] == first and len(planners) == 2, (
        "one initial planner, one amendment planner; never replace a live planner"
    )
    d = state.get_dispatch(con, first)
    packet = dispatch.build_packet(con, state.get_run(con, run["id"]), d, "planner", {"contract_request": "stale"})
    # Packets are constructed at launch: the second queued request cannot be missed.
    assert "first request" in str(packet) and "second request" in str(packet)
    packet_file = tmp_path / "packet.json"
    packet_file.write_text(json.dumps(packet))
    con.execute("UPDATE dispatches SET packet_path=? WHERE id=?", (str(packet_file), first))
    con.commit()
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    draft = tmp_path / "PLAN.md"
    draft.write_text(PLAN_TWO.replace("scope: calc.py", "scope: calc.py, calc_helpers.py"))
    prior_plan_count = con.execute("SELECT COUNT(*) FROM plans WHERE run_id=?", (run["id"],)).fetchone()[0]
    plans.submit_plan(con, state.get_run(con, run["id"]), draft, submitter=first, dispatch_id=first)
    versions = con.execute("SELECT to_plan_version FROM amendments WHERE class='contract' ORDER BY seq").fetchall()
    assert [r[0] for r in versions] == [2, 2], "both amendments must map to one revision"
    assert con.execute("SELECT COUNT(*) FROM plans WHERE run_id=?", (run["id"],)).fetchone()[0] == prior_plan_count + 1


def test_planner_launch_guard_rejects_second_writer_even_outside_amend(env):
    con, run = _dedicated_existing_plan(env)
    env.office("amend", "T1", "--contract", "--", "scope change", env=MANUAL, check=0)
    from office.state import Refused
    import pytest
    with pytest.raises(Refused, match="live"):
        with db.transaction(con):
            dispatch.request_launch(con, run, "P1", role="planner")
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE role='planner'").fetchone()[0] == 2


def test_late_amendment_is_not_silently_marked_included_in_earlier_packet(env, tmp_path, monkeypatch):
    con, run = _dedicated_existing_plan(env)
    env.office("amend", "T1", "--contract", "--", "early request", env=MANUAL, check=0)
    planner = state.get_task(con, run["id"], "P1")
    did = planner["current_dispatch_id"]
    d = state.get_dispatch(con, did)
    packet = dispatch.build_packet(con, run, d, "planner", {})
    assert packet["contract_request_max_seq"] == 1
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet))
    con.execute("UPDATE dispatches SET packet_path=? WHERE id=?", (str(packet_path), did))
    con.commit()

    # This happens while the first planner is already drafting from a frozen packet.
    env.office("amend", "T2", "--contract", "--", "late request", env=MANUAL, check=0)
    planners_before = con.execute("SELECT id FROM dispatches WHERE role='planner' ORDER BY started_at").fetchall()
    assert len(planners_before) == 2

    monkeypatch.setenv("OFFICE_JOBS", "manual")
    draft = tmp_path / "PLAN.md"
    draft.write_text(PLAN_TWO.replace("scope: calc.py", "scope: calc.py, calc_helpers.py"))
    plans.submit_plan(con, state.get_run(con, run["id"]), draft, submitter=did, dispatch_id=did)
    versions = con.execute("SELECT seq, to_plan_version FROM amendments WHERE class='contract' ORDER BY seq").fetchall()
    assert [(r[0], r[1]) for r in versions] == [(1, 2), (2, None)]

    # Only after first planner exited may the pending late request take the same worktree.
    dispatch._finish(did, 0, None, "success", 0)
    con2 = env.con()
    ids = [r[0] for r in con2.execute("SELECT id FROM dispatches WHERE role='planner' ORDER BY started_at")]
    assert len(ids) == 3 and ids[-1] != did
    # The follow-up packet carries the late request, so its revision may acknowledge it.
    follow = dispatch.build_packet(con2, state.get_run(con2, run["id"]), state.get_dispatch(con2, ids[-1]), "planner", {})
    assert follow["contract_request_max_seq"] == 2 and "late request" in follow["contract_request"]
    assert "early request" not in follow["contract_request"]


def test_running_planner_nudge_does_not_claim_late_request_is_in_this_revision(env, monkeypatch):
    con, run = _dedicated_existing_plan(env)
    env.office("amend", "T1", "--contract", "--", "early request", env=MANUAL, check=0)
    did = state.get_task(con, run["id"], "P1")["current_dispatch_id"]
    con.execute("UPDATE dispatches SET status='running' WHERE id=?", (did,))
    con.commit()
    from office import gates
    monkeypatch.setattr(gates, "live_task_session", lambda *a, **k: did)
    with db.transaction(con):
        assert dispatch.create_planner_task(con, run, contract_request="A2: late request") == did
    row = con.execute("SELECT payload_json FROM outbox WHERE kind='notify_worker' ORDER BY rowid DESC LIMIT 1").fetchone()
    text = json.loads(row[0])["text"]
    assert "follow-up revision" in text and "same PLAN.md revision" not in text


def test_no_follow_up_planner_on_a_terminal_run(env, tmp_path, monkeypatch):
    con, run = _dedicated_existing_plan(env)
    env.office("amend", "T1", "--contract", "--", "early request", env=MANUAL, check=0)
    did = state.get_task(con, run["id"], "P1")["current_dispatch_id"]
    packet = dispatch.build_packet(con, run, state.get_dispatch(con, did), "planner", {})
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet))
    con.execute("UPDATE dispatches SET packet_path=? WHERE id=?", (str(packet_path), did))
    con.commit()
    env.office("amend", "T2", "--contract", "--", "late request", env=MANUAL, check=0)
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    draft = tmp_path / "PLAN.md"
    draft.write_text(PLAN_TWO.replace("scope: calc.py", "scope: calc.py, calc_helpers.py"))
    plans.submit_plan(con, state.get_run(con, run["id"]), draft, submitter=did, dispatch_id=did)
    con.execute("UPDATE runs SET phase='abandoned' WHERE id=?", (run["id"],))
    con.commit()
    dispatch._finish(did, 0, None, "success", 0)
    con2 = env.con()
    assert con2.execute("SELECT COUNT(*) FROM dispatches WHERE role='planner'").fetchone()[0] == 2
    assert con2.execute("SELECT ended_at FROM dispatches WHERE id=?", (did,)).fetchone()[0]
