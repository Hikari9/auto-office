"""Issue #267 (run a9afacbf): a test runner's own per-test timeout under host
load is UNAVAILABLE, not a code finding (A2); a worker that ends on a harness
quota wall is blocked at once with the wall named, never relaunched into the
same quota (A3)."""
from __future__ import annotations

import pytest

from conftest import GOOD_ADD, PLAN_ONE, approved_run, task_row
from office import gates

ORIGINAL_CHECK = 'checks: python3 -c "import calc; assert calc.add(2, 3) == 5"'
VITEST_TIMEOUT = "Error: Test timed out in 5000ms."
CLAUDE_WALL = "You've hit your session limit · resets 12am (Asia/Manila)"


# ------------------------------------------------------------------ A2

@pytest.mark.parametrize("line", [
    VITEST_TIMEOUT,
    "thrown: Exceeded timeout of 5000 ms for a test.",
    "Error: Timeout of 2000ms exceeded. For async tests and hooks, ensure done() is called",
    "Test timeout of 30000ms exceeded.",
    "+++++++++++++++++++ Timeout +++++++++++++++++++",
    "Failed: Timeout >10.0s",
])
def test_runner_timeout_recognises_common_runners(line):
    assert gates.runner_timeout(f"lots of output\n{line}\nmore\n")


@pytest.mark.parametrize("text", ["AssertionError: expected 4 to be 5", "setTimeout callback ran", "timeout: 30"])
def test_runner_timeout_ignores_ordinary_failures(text):
    assert not gates.runner_timeout(text)


def _failing_check_plan(env) -> str:
    script = env.tmp / "runner.sh"
    script.write_text(f"echo 'FAIL  a.test.ts > slow'\necho '{VITEST_TIMEOUT}'\nexit 1\n")
    plan = PLAN_ONE.replace(ORIGINAL_CHECK, f"checks: sh {script}")
    assert plan != PLAN_ONE
    return plan


def _checks_gate(env) -> dict:
    con = env.con()
    try:
        return dict(con.execute("SELECT verdict, summary FROM gates WHERE kind='checks' ORDER BY rowid LIMIT 1").fetchone())
    finally:
        con.close()


def _open_check_findings(env) -> int:
    con = env.con()
    try:
        return con.execute("SELECT COUNT(*) FROM findings WHERE gate_kind='checks' AND state='open'").fetchone()[0]
    finally:
        con.close()


def test_runner_timeout_under_load_is_unavailable_not_a_finding(env, monkeypatch):
    monkeypatch.setenv("OFFICE_CHECK_LOAD_FACTOR", "0")  # any load counts as overloaded
    approved_run(env, plan=_failing_check_plan(env), executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    gate = _checks_gate(env)
    assert gate["verdict"] == "UNAVAILABLE" and "test-runner timeout" in gate["summary"] and "host load" in gate["summary"], gate
    assert _open_check_findings(env) == 0
    assert task_row(env)["status"] == "blocked"


def test_runner_timeout_on_a_quiet_host_is_still_a_finding(env, monkeypatch):
    monkeypatch.setenv("OFFICE_CHECK_LOAD_FACTOR", "1000000")  # never overloaded
    approved_run(env, plan=_failing_check_plan(env), executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert _checks_gate(env)["verdict"] == "CHANGES_REQUIRED"


# ------------------------------------------------------------------ A3

def test_quota_signature_matches_claude_session_limit():
    assert gates._quota_signature(CLAUDE_WALL)
    assert gates._quota_signature("You've hit your weekly limit · resets Sat 4am")


def _executor_dispatches(env) -> list[dict]:
    con = env.con()
    try:
        return [dict(r) for r in con.execute("SELECT id, terminal_classification FROM dispatches "
                                             "WHERE role='executor' ORDER BY started_at").fetchall()]
    finally:
        con.close()


@pytest.mark.approved
def test_quota_wall_blocks_without_relaunching(env):
    approved_run(env, executor=[{"stderr": CLAUDE_WALL + "\n", "exit": 1}] * 4,
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert len(_executor_dispatches(env)) == 1, _executor_dispatches(env)
    t = task_row(env)
    assert t["status"] == "blocked" and "quota exhausted" in t["pause_reason"] and "session limit" in t["pause_reason"], t


@pytest.mark.approved
def test_quota_word_in_early_narration_still_relaunches(env):
    narration = "Checking the quota module first.\n" + "\n".join(f"step {i}" for i in range(10)) + "\ncrashed\n"
    approved_run(env, executor=[{"stderr": narration, "exit": 3}] * 4, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert len(_executor_dispatches(env)) == 3
    t = task_row(env)
    assert t["status"] == "blocked" and "without submitting" in t["pause_reason"], t
