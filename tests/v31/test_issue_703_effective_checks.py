"""Task amendments never leak a superseded command into integration (#703).

The historical 3.3 command union is absent on current main. Run-level checks
remain a separate explicit contract; this locks the actual amendment flow.
"""
import json

from conftest import GOOD_ADD, PLAN_ONE, approved_run, task_row


def test_amended_task_check_and_explicit_run_check_reach_integration(env):
    original = 'python3 -c "import calc; assert calc.add(2, 3) == 5"'
    amended = 'python3 -c "import calc; assert calc.add(10, 20) == 30"'
    global_check = 'python3 -c "import calc; assert calc.add(3, 4) == 7"'
    plan = PLAN_ONE.replace("blast_radius: repo\n", f"blast_radius: repo\nchecks: {global_check}\n")
    approved_run(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    before = task_row(env, "T1")
    assert before["status"] == "accepted"
    env.office("amend", "T1", "--drop-check", original, "--add-check", amended,
               "--", "exercise the effective task check", check=0)
    after = task_row(env, "T1")
    assert after["status"] == "accepted"
    assert after["accepted_revision_id"] == before["accepted_revision_id"]
    assert json.loads(after["checks_json"]) == [amended]
    con = env.con()
    task_gate = con.execute("SELECT verdict FROM gates WHERE task_id='T1' AND kind='checks' "
                            "ORDER BY rowid DESC LIMIT 1").fetchone()
    assert task_gate["verdict"] == "APPROVED"
    landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    assert landing["run_checks"] == [global_check]
    assert landing["integration"]["status"] == "accepted"
    commands = [json.loads(row[0])["command"] for row in con.execute(
        "SELECT meta_json FROM evidence WHERE kind='check_output' AND task_id IS NULL")]
    assert commands and all(command == global_check for command in commands)
    assert con.execute("SELECT 1 FROM validations WHERE command=? AND passed=1", (amended,)).fetchone()


def test_amended_run_check_replaces_superseded_integration_command(env):
    # Fails only with the task's change present (the base calc.py raises NotImplementedError), so the
    # failure is attributable to the revision and not pre-existing on the base (#629).
    needle = GOOD_ADD.splitlines()[1].strip()  # the task's change: "return a + b"
    original = 'python3 -c "import sys; sys.exit(1 if \'' + needle + '\' in open(\'calc.py\').read() else 0)"'
    amended = 'python3 -c "import calc; assert calc.add(10, 20) == 30"'
    plan = PLAN_ONE.replace("blast_radius: repo\n", f"blast_radius: repo\nchecks: {original}\n")
    approved_run(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    assert json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])["integration"]["status"] == "blocked"
    env.write_plan(plan.replace(original, amended))
    env.office("amend", "plan", "--", "correct the integration check command", check=0)
    landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    assert landing["run_checks"] == [amended]
    assert landing["integration"]["status"] == "accepted"
    latest = con.execute("SELECT id FROM gates WHERE subject='integration' AND kind='checks' "
                         "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    evidence = [json.loads(row[0]) for row in con.execute("SELECT meta_json FROM evidence WHERE gate_id=?",
                                                       (latest,))]
    assert evidence and all(row["command"] == amended and row["exit"] == 0 for row in evidence)
