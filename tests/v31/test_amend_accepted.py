"""#313 S3: `office amend` on an accepted task reopens it as a fix round and must hand the
amendment delta to the relaunched executor.

Run 570e33a1: the amendment showed "superseded (delivered 0x)", the new executor's brief held no
findings and no delta, and its preflight stopped on "no open findings are recorded" with a
suggestion (`office prompt`) that could not satisfy it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, task_row
from test_self_review_ledger import write_ledger

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
DELTA = "also reject non-numeric arguments with a TypeError"

pytestmark = pytest.mark.approved


def _accepted_then_amended(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    first = task_row(env)["current_dispatch_id"]
    code, out = env.office("amend", "T1", "--", DELTA, env=EXTERNAL)
    assert code == 0, out
    row = task_row(env)
    assert row["status"] == "running" and row["current_dispatch_id"] != first, row
    return first, row["current_dispatch_id"]


def _worker(env, did):
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1",
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}, Path(d["worktree"]), d


def _deliveries(env):
    return [dict(r) for r in env.con().execute("SELECT * FROM deliveries ORDER BY created_at")]


def _commit_work(env, wt):
    env.git("add", "-A", cwd=wt)
    env.git("-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-qm", "calc", cwd=wt)
    write_ledger(wt)


def test_the_relaunched_executor_brief_carries_the_amendment_delta(env):
    first, second = _accepted_then_amended(env)
    from office import paths
    run_id = env.con().execute("SELECT id FROM runs").fetchone()[0]
    brief = (paths.run_dir(run_id) / "dispatches" / second / "brief.md").read_text()
    assert "FIX ROUND" in brief and "no review findings" in brief, brief
    assert "AMENDMENT A1" in brief and DELTA in brief and "office ack A1" in brief, brief
    rows = _deliveries(env)
    assert len(rows) == 1 and rows[0]["dispatch_id"] == second and rows[0]["status"] == "queued", rows


def test_the_amendment_is_delivered_once_the_launch_prompt_is_confirmed(env):
    first, second = _accepted_then_amended(env)
    from office import paths
    run_id = env.con().execute("SELECT id FROM runs").fetchone()[0]
    ddir = paths.run_dir(run_id) / "dispatches" / second
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='p1', status='running' WHERE id=?", (second,))
    con.commit()
    # The pointer prompt was typed but has not landed: still only queued.
    spec = json.loads((ddir / "launch.json").read_text())
    spec["prompt_landed"] = False
    (ddir / "launch.json").write_text(json.dumps(spec))
    env.office("status", check=0, env=EXTERNAL)
    assert [r["status"] for r in _deliveries(env)] == ["queued"]
    spec["prompt_landed"] = True
    (ddir / "launch.json").write_text(json.dumps(spec))
    env.office("status", check=0, env=EXTERNAL)
    rows = _deliveries(env)
    assert [r["status"] for r in rows] == ["delivered"] and rows[0]["delivered_at"], rows


def test_preflight_after_an_accepted_amend_is_not_the_no_findings_stop(env):
    first, second = _accepted_then_amended(env)
    wenv, wt, d = _worker(env, second)
    from office import paths
    pkt = json.loads((paths.run_dir(d["run_id"]) / "dispatches" / second / "packet.json").read_text())
    assert pkt["fix_of"], pkt  # a fix round with no findings: the 570e33a1 shape
    (wt / "calc.py").write_text(GOOD_ADD)
    _commit_work(env, wt)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert "no open findings" not in out and "no open findings or amendments" not in out, out
    # The amendment is in the brief but not yet applied: the repair is the ack, not a stop.
    assert code == 1 and "fix: amendment: A1" in out and "office ack A1" in out, out
    code, out = env.office("ack", "A1", cwd=wt, env=wenv)
    assert code == 0, out
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "PREFLIGHT ready" in out and "amendment: A1 applied" in out, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out and "amendment pending" not in out, out


def test_a_fix_round_with_neither_findings_nor_an_amendment_still_stops(env):
    first, second = _accepted_then_amended(env)
    wenv, wt, d = _worker(env, second)
    con = env.con()
    con.execute("UPDATE deliveries SET status='superseded', superseded_by='test'")
    con.commit()
    (wt / "calc.py").write_text(GOOD_ADD)
    _commit_work(env, wt)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "no open findings or amendments" in out, out
    assert "office amend T1 --" in out and "office prompt" not in out, out


def test_the_no_findings_stop_event_names_a_command_that_resolves_it(env):
    first, second = _accepted_then_amended(env)
    wenv, wt, d = _worker(env, second)
    con = env.con()
    con.execute("UPDATE deliveries SET status='superseded', superseded_by='test'")
    con.commit()
    (wt / "calc.py").write_text(GOOD_ADD)
    _commit_work(env, wt)
    env.office("preflight", cwd=wt, env=wenv, check=4)
    ev = env.con().execute("SELECT payload_json FROM events WHERE kind='worker.signal'").fetchone()
    nxt = json.loads(ev[0])["next"]
    assert "office amend T1 --" in nxt and "office rerun T1 --fresh" in nxt and "office prompt" not in nxt, nxt


def test_only_a_delivery_in_the_brief_is_confirmed_by_the_launch_prompt(env):
    first, second = _accepted_then_amended(env)
    from office import paths
    run_id = env.con().execute("SELECT id FROM runs").fetchone()[0]
    ddir = paths.run_dir(run_id) / "dispatches" / second
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='p1', status='running' WHERE id=?", (second,))
    con.execute("UPDATE deliveries SET created_at='2999-01-01T00:00:00+00:00'")  # recorded after the brief was written
    con.commit()
    (ddir / "brief-deliveries.json").write_text("[]")  # ... so the brief does not carry it
    spec = json.loads((ddir / "launch.json").read_text())
    spec["prompt_landed"] = True
    (ddir / "launch.json").write_text(json.dumps(spec))
    env.office("status", check=0, env=EXTERNAL)
    assert [r["status"] for r in _deliveries(env)] == ["queued"]
    (ddir / "launch.json").unlink()  # no record of the prompt: nothing is confirmed, and nothing crashes
    (ddir / "brief-deliveries.json").unlink()
    con.execute("UPDATE deliveries SET created_at='2000-01-01T00:00:00+00:00'")
    con.commit()
    env.office("status", check=0, env=EXTERNAL)
    assert [r["status"] for r in _deliveries(env)] == ["queued"]


def test_a_long_amendment_is_cut_in_the_brief_with_a_marker(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    env.office("amend", "T1", "--", "also handle edge cases " + "of integers " * 700, env=EXTERNAL, check=0)
    from office import paths
    run_id = env.con().execute("SELECT id FROM runs").fetchone()[0]
    brief = (paths.run_dir(run_id) / "dispatches" / task_row(env)["current_dispatch_id"] / "brief.md").read_text()
    assert "AMENDMENT A1" in brief and "[cut at 6000 characters" in brief, brief[-600:]


def test_an_amendment_relaunch_with_no_prior_revision_still_gates_preflight_on_the_ack(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    first = task_row(env)["current_dispatch_id"]
    con = env.con()
    con.execute("UPDATE dispatches SET status='cancelled', ended_at=started_at WHERE id=?", (first,))  # the worker died
    con.commit()
    code, out = env.office("amend", "T1", "--", DELTA, env=EXTERNAL)
    assert code == 0, out
    second = task_row(env)["current_dispatch_id"]
    assert second != first
    wenv, wt, d = _worker(env, second)
    from office import paths
    pkt = json.loads((paths.run_dir(d["run_id"]) / "dispatches" / second / "packet.json").read_text())
    assert not pkt["fix_of"], pkt  # no revision to fix: not a fix round
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and "fix: amendment: A1" in out and "office ack A1" in out, out
    env.office("ack", "A1", cwd=wt, env=wenv, check=0)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "PREFLIGHT ready" in out, out


@pytest.mark.parametrize("state_", ["queued", "delivered", "applied", "other-session"])
def test_preflight_gates_only_this_sessions_unacknowledged_amendments(env, state_):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    first = task_row(env)["current_dispatch_id"]
    con = env.con()
    con.execute("UPDATE dispatches SET status='cancelled', ended_at=started_at WHERE id=?", (first,))
    con.commit()
    env.office("amend", "T1", "--", DELTA, env=EXTERNAL, check=0)
    second = task_row(env)["current_dispatch_id"]
    wenv, wt, d = _worker(env, second)
    if state_ == "other-session":
        con.execute("UPDATE deliveries SET dispatch_id='Dstale'")
    else:
        con.execute("UPDATE deliveries SET status=?", (state_,))
    con.commit()
    code, out = env.office("preflight", cwd=wt, env=wenv)
    if state_ in ("queued", "delivered"):
        assert code == 1 and "office ack A1" in out, out
    else:
        assert code == 0 and ("amendment: A1 applied" in out) == (state_ == "applied"), out
