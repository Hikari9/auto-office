"""#1068 integration READY needs a passing run-level check; use only isolated fake Office fixtures."""
from __future__ import annotations

import json

from conftest import GOOD_ADD, PLAN_ONE, approved_run

CHECK = 'python3 -c "import calc; assert calc.add(2, 3) == 5"'
OLD = 'python3 -c "print(chr(70)+chr(65)+chr(73)+chr(76)+chr(69)+chr(68), 1); raise SystemExit(1)"'
OLDER = "python3 -c \"print('SECOND FAILURE', 2); raise SystemExit(1)\""


def _rows(env, sql, args=()):
    c = env.con()
    try:
        return [dict(x) for x in c.execute(sql, args)]
    finally:
        c.close()


def _run_level(*commands: str) -> str:
    return PLAN_ONE.replace("blast_radius: repo\n", "blast_radius: repo\nchecks:\n" + "".join(f"- {c}\n" for c in commands))


def test_every_run_level_check_pre_existing_on_base_does_not_report_ready(env):
    approved_run(env, plan=_run_level(OLD, OLDER),
                 executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    landing = json.loads(_rows(env, "SELECT landing_json FROM runs")[0]["landing_json"])
    assert landing["integration"]["status"] == "blocked", landing
    assert "no run-level check passed" in landing["integration"]["detail"], landing
    assert "failing evidence: check 1 exit 1" in landing["integration"]["detail"], landing
    checks = _rows(env, "SELECT * FROM gates WHERE subject='integration' AND kind='checks'")
    assert checks and checks[-1]["verdict"] == "CHANGES_REQUIRED", checks
    assert not _rows(env, "SELECT * FROM events WHERE kind='integration.accepted'"), "READY reported with zero passing checks"


def test_pre_existing_failure_beside_a_passing_run_level_check_stays_ready(env):
    approved_run(env, plan=_run_level(CHECK, OLD),
                 executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    landing = json.loads(_rows(env, "SELECT landing_json FROM runs")[0]["landing_json"])
    assert landing["integration"]["status"] == "accepted", landing
    checks = _rows(env, "SELECT * FROM gates WHERE subject='integration' AND kind='checks'")
    assert len(checks) == 1 and checks[0]["verdict"] == "PASS", checks
    assert "pre-existing" in checks[0]["summary"].lower(), checks
    assert _rows(env, "SELECT * FROM events WHERE kind='integration.accepted'"), "no READY event"


def test_pre_existing_failure_before_a_passing_run_level_check_stays_ready(env):
    # Any passing run-level check counts, not only the first one.
    approved_run(env, plan=_run_level(OLD, CHECK),
                 executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    landing = json.loads(_rows(env, "SELECT landing_json FROM runs")[0]["landing_json"])
    assert landing["integration"]["status"] == "accepted", landing
    checks = _rows(env, "SELECT * FROM gates WHERE subject='integration' AND kind='checks'")
    assert len(checks) == 1 and checks[0]["verdict"] == "PASS", checks
    assert _rows(env, "SELECT * FROM events WHERE kind='integration.accepted'"), "no READY event"


def test_zero_passing_checks_with_one_introduced_failure_stays_blocked_as_introduced(env):
    changed = ('python3 -c "from pathlib import Path; p=Path(\'calc.py\').read_text(); '
               'raise SystemExit(1 if \'return a + b\' in p else 0)"')
    approved_run(env, plan=_run_level(OLD, changed),
                 executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    landing = json.loads(_rows(env, "SELECT landing_json FROM runs")[0]["landing_json"])
    assert landing["integration"]["status"] == "blocked", landing
    assert "introduced failures" in landing["integration"]["detail"], landing
    assert not _rows(env, "SELECT * FROM events WHERE kind='integration.accepted'"), "READY reported"
