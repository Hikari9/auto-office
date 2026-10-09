"""`depends:` is an acceptance barrier, and status, resume and wait agree (#405).

Plan p3 stacked T1 -> T2 -> T3. While T1's checks ran, and later while T1 had
findings waiting, `office status` and `office resume` printed `next: ...
office dispatch T2`, a dispatch whose dependency was not accepted. A dependent
task is ready only once every task it depends on is accepted, and the three
commands print one `next:` from the same rule.
"""
from __future__ import annotations

import json
import re

from unittest import mock

import pytest
from hypothesis import given, strategies as st

from conftest import GOOD_ADD, approved_run

PLAN_STACK = """# Plan

## Requirements
done:
- add, mul and sub exist
blast_radius: repo

## Tasks
### T1: Implement add
scope: calc.py
depends: none
checks: python3 -c "import calc; assert calc.add(2, 3) == 5"
accept:
- calc.add(2, 3) == 5
visual: none

### T2: Implement mul
scope: mul.py
depends: T1
checks: python3 -c "import mul; assert mul.mul(2, 3) == 6"
accept:
- mul.mul(2, 3) == 6
visual: none

### T3: Implement sub
scope: sub.py
depends: T2
checks: python3 -c "import sub; assert sub.sub(3, 2) == 1"
accept:
- sub.sub(3, 2) == 1
visual: none
"""


def _ready(con, run):
    from office import guide, state
    return guide.ready_tasks(con, state.get_run(con, run["id"]))


def _next(con, run):
    from office import guide, state
    return guide.next_action(con, state.get_run(con, run["id"]))


def _run(con):
    from office import state
    return state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])


@pytest.mark.parametrize("status", ["submitted", "accepted"])
def test_only_an_accepted_dependency_makes_its_dependent_ready(env, status):
    approved_run(env, plan=PLAN_STACK)
    con = env.con()
    run = _run(con)
    assert _ready(con, run) == ["T1"]
    # A submitted T1 has a current revision: that was enough for the old rule. It is not enough now.
    con.execute("UPDATE tasks SET status=?, current_revision_id='R1-x' WHERE id='T1'", (status,))
    ready = _ready(con, run)
    if status == "accepted":
        assert ready == ["T2"], ready  # T3 still waits for T2
    else:
        assert "T2" not in ready and "T3" not in ready, (status, ready)
        assert "office dispatch T2" not in _next(con, run), _next(con, run)


STATUSES = ["planned", "launching", "running", "submitted", "changes_required", "blocked", "paused", "accepted", "cancelled"]


@st.composite
def _stacks(draw):
    """Tasks T1..Tn whose `depends` name earlier tasks (so the graph is acyclic) or, now and then, a task the plan lacks."""
    n = draw(st.integers(min_value=1, max_value=7))
    tasks = []
    for i in range(1, n + 1):
        earlier = [f"T{j}" for j in range(1, i)] + ["T99"]
        deps = draw(st.lists(st.sampled_from(earlier), unique=True, max_size=3))
        tasks.append({"id": f"T{i}", "depends": deps, "status": draw(st.sampled_from(STATUSES)), "role": "executor"})
    return tasks


@given(tasks=_stacks())
def test_a_task_is_ready_exactly_when_it_is_planned_and_every_dependency_is_accepted(tasks):
    """Safety: nothing ready has an unaccepted or unknown dependency. Liveness: nothing is held back once its
    dependencies are accepted. And status and resume never disagree: each planned task is ready xor waiting."""
    from office import guide, state
    status = {t["id"]: t["status"] for t in tasks}
    with mock.patch.object(state, "tasks", lambda con, run_id: tasks):
        ready = set(guide.ready_tasks(None, {"id": "r"}))
        waiting = {w.split()[0] for w in guide.held_by_dependencies(None, {"id": "r"})}
    for t in tasks:
        deps_accepted = all(status.get(d) == "accepted" for d in t["depends"])
        if t["id"] in ready:
            assert t["status"] == "planned" and deps_accepted, t
        if t["status"] == "planned" and deps_accepted:
            assert t["id"] in ready, t
        assert (t["id"] in ready) != (t["id"] in waiting) if t["status"] == "planned" else t["id"] not in ready | waiting, t


@given(tasks=_stacks(), data=st.data())
def test_accepting_a_task_never_makes_another_task_wait(tasks, data):
    from office import guide, state
    before = {}
    with mock.patch.object(state, "tasks", lambda con, run_id: tasks):
        before = set(guide.ready_tasks(None, {"id": "r"}))
    target = data.draw(st.sampled_from(tasks))
    after_tasks = [{**t, "status": "accepted"} if t["id"] == target["id"] else t for t in tasks]
    with mock.patch.object(state, "tasks", lambda con, run_id: after_tasks):
        after = set(guide.ready_tasks(None, {"id": "r"}))
    assert before - {target["id"]} <= after


def test_a_stack_opens_one_acceptance_at_a_time(env):
    approved_run(env, plan=PLAN_STACK)
    con = env.con()
    run = _run(con)
    con.execute("UPDATE tasks SET status='accepted', current_revision_id='R1', accepted_revision_id='R1' WHERE id='T1'")
    con.execute("UPDATE tasks SET status='submitted', current_revision_id='R2' WHERE id='T2'")
    assert _ready(con, run) == []
    nxt = _next(con, run)
    assert nxt.startswith("exceptions only") and "office dispatch" not in nxt
    assert "T3 waits for T2 to be accepted (T2 submitted)" in nxt, nxt
    con.execute("UPDATE tasks SET status='accepted', accepted_revision_id='R2' WHERE id='T2'")
    assert _ready(con, run) == ["T3"]
    assert _next(con, run) == "choose execution strategy; office dispatch T3"


def _next_lines(env) -> dict:
    """The `next:` each command prints for the same canonical state."""
    out = {}
    _, data = env.ojson("status")
    out["status"] = data["next"]
    _, text = env.office("resume")
    out["resume"] = re.search(r"^next: (.*)$", text, re.M).group(1)
    _, text = env.office("wait", "--timeout", "0", "--poll", "0")
    out["wait"] = re.search(r"^next: (.*)$", text, re.M).group(1)
    return out


def test_status_resume_and_wait_agree_while_t1_checks_then_has_findings_then_is_accepted(env, monkeypatch):
    approved_run(env, plan=PLAN_STACK, executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD}}, "submit": True}])
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    env.office("dispatch", "T1", check=0)
    from office import db, gates, jobs, review_parse, state
    con = env.con()
    jobs.execute(con, con.execute("SELECT id FROM outbox WHERE kind='launch_agent' AND status='queued'").fetchone()[0])
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "submitted"

    # T1's checks are queued: every command waits on T1.
    lines = _next_lines(env)
    assert len(set(lines.values())) == 1, lines
    assert "office dispatch T2" not in lines["status"] and "T2 waits for T1" in lines["status"], lines

    # T1 has findings: every command names T1's repair path, never T2.
    job = con.execute("SELECT * FROM outbox WHERE kind='run_checks'").fetchone()
    gate_id = json.loads(job["payload_json"])["gate_id"]
    run = _run(con)
    with db.transaction(con):
        gates.ingest_task_gate(con, run, gate_id, {"verdict": "CHANGES_REQUIRED", "summary": "0/1", "parsed": review_parse.Parsed(
            verdict="CHANGES_REQUIRED", findings=[{"code": "C1", "severity": "material", "location": "c", "summary": "x"}])})
        con.execute("UPDATE outbox SET status='done' WHERE id=?", (job["id"],))
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "changes_required"
    lines = _next_lines(env)
    assert set(lines.values()) == {"findings on T1 wait for you: office rerun T1 --resume | --fresh"}, lines

    # Only an accepted T1 opens T2.
    con.execute("UPDATE tasks SET status='accepted', accepted_revision_id=current_revision_id WHERE id='T1'")
    lines = _next_lines(env)
    assert set(lines.values()) == {"choose execution strategy; office dispatch T2"}, lines
    assert state.get_task(con, run["id"], "T3")["status"] == "planned"
