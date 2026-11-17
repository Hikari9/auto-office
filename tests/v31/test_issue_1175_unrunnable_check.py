"""A check that could not run is never pre-existing (#1175): a runner usage or
environment exit code, or a test path missing on base, says nothing about base."""
from __future__ import annotations

import json

import pytest

from conftest import GOOD_ADD, PLAN_ONE, start_inline, task_row
from office import gates

APPROVED = "VERDICT: APPROVED\nNEXT proceed"
CHECK_LINE = 'checks: python3 -c "import calc; assert calc.add(2, 3) == 5"'


@pytest.fixture(autouse=True)
def _quiet_host(monkeypatch):
    monkeypatch.setenv("OFFICE_CHECK_LOAD_FACTOR", "100000")


def _q(env, sql):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def _run(env, check, write=None):
    plan = PLAN_ONE.replace(CHECK_LINE, f"checks: {check}")
    env.trust()
    env.script(executor=[{"write": write or {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": APPROVED}])
    start_inline(env, plan=plan)
    env.office("approve", "plan", "--quote", "approved", check=0)
    env.office("dispatch", "T1", check=0)
    return _q(env, "SELECT * FROM gates WHERE kind='checks' ORDER BY created_at")[-1]


def _exit(code, extra=""):
    return f"python3 -c \"print('FAILED legacy::test_old'); raise SystemExit({code})\"{extra}"


def _assert_blocking(env, g, reason):
    assert g["verdict"] == "RECHECK", g
    assert task_row(env)["status"] == "changes_required"
    assert not _q(env, "SELECT 1 FROM events WHERE kind='gate.preexisting'")
    assert not _q(env, "SELECT 1 FROM evidence WHERE kind='preexisting_check_output'")
    rows = _q(env, "SELECT state, summary FROM findings WHERE gate_kind='checks'")
    assert len(rows) == 1 and rows[0]["state"] == "open"
    assert "blocking, cannot be pre-existing" in rows[0]["summary"] and reason in rows[0]["summary"], rows


@pytest.mark.parametrize("code", [126])
def test_shell_runner_exit_is_never_preexisting(env, code):
    g = _run(env, _exit(code))
    _assert_blocking(env, g, f"exited {code}")


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_pytest_runner_exit_is_never_preexisting(env, code):
    g = _run(env, _exit(code, " # pytest"))
    _assert_blocking(env, g, f"exited {code}")


def test_exit_two_of_a_non_pytest_check_stays_preexisting(env):
    g = _run(env, _exit(2))
    assert g["verdict"] == "APPROVED" and "pre-existing" in g["summary"], g
    assert len(_q(env, "SELECT 1 FROM events WHERE kind='gate.preexisting'")) == 1


def test_a_test_path_missing_on_base_is_never_preexisting(env):
    g = _run(env, _exit(1, " newtest.py"), write={"calc.py": GOOD_ADD, "newtest.py": "x = 1\n"})
    assert g["verdict"] == "RECHECK", g
    rows = _q(env, "SELECT summary FROM findings WHERE gate_kind='checks'")
    assert "newtest.py does not exist on the base commit" in rows[0]["summary"], rows
    assert not _q(env, "SELECT 1 FROM events WHERE kind='gate.preexisting'")


def test_a_test_path_present_on_base_stays_preexisting(env):
    g = _run(env, _exit(1, " calc.py"))
    assert g["verdict"] == "APPROVED" and "pre-existing" in g["summary"], g


def _r(command, exit_code, cwd):
    return {"command": command, "exit": exit_code, "cwd": str(cwd)}


def test_unrunnable_reason_unit(tmp_path):
    head, base = tmp_path / "head", tmp_path / "base"
    for d in (head, base):
        d.mkdir()
    (head / "tests").mkdir()
    (head / "tests" / "test_new.py").write_text("")
    assert gates.unrunnable_reason(_r("pytest tests/test_new.py::t", 1, head), base).endswith("base commit")
    assert gates.unrunnable_reason(_r("pytest tests/test_new.py", 5, head), head).startswith("exited 5")
    assert gates.unrunnable_reason(_r("make test", 127, head), head).startswith("exited 127")
    assert gates.unrunnable_reason(_r("make test", 5, head), head) is None
    assert gates.unrunnable_reason(_r("pytest tests/test_new.py", 1, head), head) is None
    assert gates.unrunnable_reason(_r("echo 'unbalanced", 1, head), head) is None
    json.dumps(gates.unrunnable_reason(_r("pytest -x", 1, head), head))
