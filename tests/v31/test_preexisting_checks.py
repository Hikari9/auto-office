"""A task check that already fails the same way on the revision's base commit is
pre-existing: it does not cancel or replace independent review (v3.1) and is a
nonblocking note, not a repair loop (convergence-v1). A failure the task
introduced behaves as before on both contracts."""
from __future__ import annotations

import json

import pytest

from conftest import BAD_ADD, GOOD_ADD, PLAN_ONE, approved_run, start_inline, task_row
from office import gates

OLD = "python3 -c \"print('FAILED legacy::test_old'); raise SystemExit(1)\""
OLD_PLAN = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', f"checks: {OLD}")
MIXED_PLAN = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"',
                              f'checks:\n- {OLD}\n- python3 -c "import calc; assert calc.add(2, 3) == 5"')
APPROVED = "VERDICT: APPROVED\nNEXT proceed"
V31 = pytest.mark.review_contract("v3.1")


@pytest.fixture(autouse=True)
def _quiet_host(monkeypatch):
    monkeypatch.setenv("OFFICE_CHECK_LOAD_FACTOR", "100000")


def _q(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _roles(env):
    return [c["role"] for c in env.calls()]


def _start(env, plan, **script):
    env.trust()
    env.script(**script)
    start_inline(env, plan=plan)
    env.office("approve", "plan", "--quote", "approved", check=0)


def _checks_gate(env):
    return _q(env, "SELECT * FROM gates WHERE kind='checks' ORDER BY created_at")[-1]


def _preexisting_events(env):
    return _q(env, "SELECT * FROM events WHERE kind='gate.preexisting'")


def _base_evidence(env):
    return _q(env, "SELECT * FROM evidence WHERE kind='preexisting_check_output'")


WORK = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]


# ------------------------------------------------------------------ signature

def test_failure_signature_ignores_checkout_paths_and_timings():
    a = "FAILED /tmp/a/t.py::test_x - boom (0.31s)\nok 12 ms\n"
    b = "FAILED /var/b/t.py::test_x - boom (1.95s)\nok 3 ms\n"
    assert gates.failure_signature(a, ("/tmp/a",)) == gates.failure_signature(b, ("/var/b",))


def test_failure_signature_differs_when_the_failures_differ():
    base = "FAILED t.py::test_x\n"
    assert gates.failure_signature(base, ()) != gates.failure_signature(base + "FAILED t.py::test_y\n", ())
    assert gates.failure_signature("boom\n", ()) != gates.failure_signature("other\n", ())


# ------------------------------------------------------------------ v3.1

@V31
def test_v31_preexisting_only_failure_keeps_independent_review(env):
    _start(env, OLD_PLAN, executor=WORK, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    g = _checks_gate(env)
    assert g["verdict"] == "PASS" and "pre-existing" in g["summary"] and "C1" in g["summary"], g
    assert _roles(env).count("code_reviewer") == 1, "review still runs"
    assert _q(env, "SELECT status FROM gates WHERE kind='code_review'")[0]["status"] == "done"
    assert task_row(env)["status"] == "accepted"
    assert not _q(env, "SELECT 1 FROM deliveries")
    ev = _preexisting_events(env)
    assert len(ev) == 1 and ev[0]["task_id"] == "T1"
    assert json.loads(ev[0]["payload_json"])["preexisting"][0]["code"] == "C1"
    base = _base_evidence(env)
    assert len(base) == 1 and base[0]["gate_id"] == g["id"] and json.loads(base[0]["meta_json"])["preexisting"] is True


@V31
def test_v31_failure_introduced_by_the_task_still_cancels_review(env):
    _start(env, PLAN_ONE, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}],
           code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "CHANGES_REQUIRED"
    assert _q(env, "SELECT status, stale_reason FROM gates WHERE kind='code_review'")[0] == \
        {"status": "cancelled", "stale_reason": "checks failed"}
    assert "code_reviewer" not in _roles(env) and task_row(env)["status"] == "changes_required"
    assert not _preexisting_events(env)


@V31
def test_v31_mixed_failures_only_count_the_new_one(env):
    _start(env, MIXED_PLAN, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}],
           code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "CHANGES_REQUIRED"
    assert [r["code"] for r in _q(env, "SELECT code FROM findings WHERE gate_kind='checks'")] == ["C2"]
    assert "code_reviewer" not in _roles(env) and task_row(env)["status"] == "changes_required"
    assert len(_preexisting_events(env)) == 1


@V31
def test_v31_output_naming_a_changed_file_is_never_preexisting(env):
    plan = OLD_PLAN.replace("legacy::test_old", "calc.py::test_old")
    _start(env, plan, executor=WORK, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "CHANGES_REQUIRED"
    assert not _preexisting_events(env) and not _base_evidence(env)


@V31
def test_v31_a_different_exit_status_on_base_is_not_preexisting(env):
    code = 'python3 -c "import sys; sys.exit(3 if \'return a + b\' in open(\'calc.py\').read() else 4)"'
    plan = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', f"checks: {code}")
    _start(env, plan, executor=WORK, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "CHANGES_REQUIRED"
    assert not _preexisting_events(env)
    assert json.loads(_base_evidence(env)[0]["meta_json"])["preexisting"] is False


@V31
def test_v31_a_test_runner_timeout_is_never_preexisting(env):
    plan = OLD_PLAN.replace("FAILED legacy::test_old", "Test timed out in 5000ms")
    _start(env, plan, executor=WORK, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "CHANGES_REQUIRED"
    assert not _preexisting_events(env) and not _base_evidence(env)


@V31
def test_v31_a_revision_that_changes_no_file_is_never_preexisting(env):
    # The failing check is the work this task owes; an empty revision did none of it.
    approved_run(env, gear="direct+review", executor=[{"write": {}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "CHANGES_REQUIRED"
    assert not _preexisting_events(env) and not _base_evidence(env)
    assert task_row(env)["status"] != "accepted"


# ------------------------------------------------------------------ convergence-v1

def test_convergence_preexisting_failure_is_a_nonblocking_note_and_lane_review_runs(env):
    _start(env, OLD_PLAN, executor=WORK, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    g = _checks_gate(env)
    assert g["verdict"] == "APPROVED" and "pre-existing" in g["summary"], g
    assert _roles(env).count("convergence_reviewer") == 1, "lane review still runs"
    assert task_row(env)["status"] == "accepted"
    assert not _q(env, "SELECT 1 FROM deliveries"), "no repair round is delivered"
    assert len(_q(env, "SELECT 1 FROM gates WHERE kind='checks'")) == 1, "no checks round loops"
    notes = _q(env, "SELECT state, summary FROM findings WHERE gate_kind='checks'")
    assert len(notes) == 1 and notes[0]["state"] == "minor" and notes[0]["summary"].startswith("pre-existing on base")
    assert not _q(env, "SELECT 1 FROM findings WHERE gate_kind='checks' AND state='open'")
    assert len(_preexisting_events(env)) == 1 and len(_base_evidence(env)) == 1


def test_convergence_failure_introduced_by_the_task_still_loops_repair(env):
    _start(env, PLAN_ONE, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "RECHECK"
    assert _q(env, "SELECT state FROM findings WHERE gate_kind='checks'") == [{"state": "open"}]
    assert "convergence_reviewer" not in _roles(env) and task_row(env)["status"] == "changes_required"
    assert not _preexisting_events(env)


def test_convergence_mixed_failures_block_only_on_the_new_one(env):
    _start(env, MIXED_PLAN, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "RECHECK"
    rows = {r["code"]: r["state"] for r in _q(env, "SELECT code, state FROM findings WHERE gate_kind='checks'")}
    assert rows == {"C1": "minor", "C2": "open"}
    assert task_row(env)["status"] == "changes_required"
