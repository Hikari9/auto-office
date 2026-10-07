"""Landing gaps from run 10944d06: PR body notes survive pushes, `office land
--rebase` onto a moved main, and a check timeout under host load is the
environment, not a code failure."""
from __future__ import annotations

import json
import subprocess

from conftest import GOOD_ADD, PLAN_ONE, approved_run
from office import gates, prs, state
from test_land import _integration_tree, _main_tree, _run
from test_task_prs import PLAN_STACKED, gh

MARK = "<!-- office:pr run=r task=T1 -->"


# ------------------------------------------------------------------ PR body

def test_merge_body_keeps_text_outside_the_office_block():
    managed = f"{prs.BEGIN}\nnew office text\n\n{MARK}\n"
    current = f"intro by user\n{prs.BEGIN}\nold office text\n\n{MARK}\n\n## Timings\nT1 223s\n"
    assert prs.merge_body(current, managed) == f"intro by user\n{managed}\n## Timings\nT1 223s\n"


def test_merge_body_replaces_a_legacy_body_and_an_unmarked_one():
    managed = f"{prs.BEGIN}\nnew\n{MARK}\n"
    assert prs.merge_body(f"old office text\n{MARK}\nnotes\n", managed) == managed + "notes\n"
    assert prs.merge_body("someone replaced it all", managed) == managed


def test_executor_notes_in_the_pr_body_survive_the_next_sync(env, monkeypatch):
    _run(env, monkeypatch, PLAN_STACKED)
    s = gh(env)
    s["prs"][0]["body"] += "\n## Coverage\nall paths hit\n"
    (env.tmp / "gh.json").write_text(json.dumps(s))
    con = env.con()
    try:
        run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
        task = state.get_task(con, run["id"], "T1")
        prs.ensure_pr(con, run, task, state.get_dispatch(con, task["current_dispatch_id"]))
    finally:
        con.close()
    body = gh(env)["prs"][0]["body"]
    assert body.count(prs.BEGIN) == 1 and body.endswith("## Coverage\nall paths hit\n"), body


# ------------------------------------------------------------------ rebase

def _advance_main(env, bare, path: str, text: str) -> None:
    clone = env.tmp / "advance"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    (clone / path).write_text(text)
    for args in (("add", path), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved"),
                 ("push", "-q", "origin", "main")):
        subprocess.run(["git", "-C", str(clone), *args], check=True)


def test_rebase_recomposes_onto_moved_main_then_merge_matches(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, PLAN_STACKED)
    _advance_main(env, bare, "NOTES.md", "unrelated\n")
    # #337: the rebase is a shared composition boundary (S-rebase) reviewed once by the lane reviewer.
    env.script(convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    code, out = env.office("land", "--rebase")
    assert code == 0 and "merges cleanly onto origin/main" in out, out
    code, data = env.ojson("status")
    con = env.con()
    try:
        landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    finally:
        con.close()
    assert landing["integration"]["status"] == "accepted", landing
    assert landing["convergence"]["S-rebase"]["status"] == "approved", landing["convergence"]
    assert landing["integration"]["detail"] == "composed result verified"
    assert env.git("--git-dir", str(bare), "rev-parse", "main").strip() == landing["rebase"]["onto"]
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 0 and "matches the reviewed integration tree" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)


def test_rebase_conflict_refuses_with_the_by_hand_steps(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, PLAN_STACKED)
    _advance_main(env, bare, "calc.py", "def add(a, b):\n    return b + a  # main\n")
    code, out = env.office("land", "--rebase")
    assert code == 4 and "rebase-conflict" in out and "calc.py" in out and "compose by hand" in out, out
    code, out = env.office("land", "--rebase")
    assert "rebase-conflict" in out, out  # nothing was recorded


def test_rebase_when_main_has_not_moved_is_a_no_op(env, monkeypatch):
    _run(env, monkeypatch, PLAN_STACKED)
    code, out = env.office("land", "--rebase")
    assert code == 0 and "nothing to rebase" in out, out


# ------------------------------------------------------------------ check timeout under load

def test_host_overloaded_compares_load_with_cpus(monkeypatch):
    monkeypatch.setattr(gates.os, "getloadavg", lambda: (100.0, 0, 0))
    monkeypatch.setattr(gates.os, "cpu_count", lambda: 10)
    assert gates.host_overloaded() == "host load 100 exceeds 20 (10 CPUs)"
    monkeypatch.setattr(gates.os, "getloadavg", lambda: (5.0, 0, 0))
    assert gates.host_overloaded() is None


def test_run_check_timeout_under_load_is_unavailable_not_a_finding(env, monkeypatch):
    plan = PLAN_ONE.replace("blast_radius: repo\n", "blast_radius: repo\nchecks: sleep 5\n")
    (env.repo / ".auto-office").mkdir(exist_ok=True)
    (env.repo / ".auto-office" / "config.yaml").write_text("verification:\n  check_timeout_seconds: 2\n")
    monkeypatch.setenv("OFFICE_CHECK_LOAD_FACTOR", "0")  # any load counts as overloaded
    approved_run(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    try:
        landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    finally:
        con.close()
    detail = landing["integration"]["detail"]
    assert landing["integration"]["status"] == "blocked" and "UNAVAILABLE" in detail and "host load" in detail, landing


def test_task_check_timeout_under_load_blocks_then_resume_reruns_it(env, monkeypatch):
    marker = env.tmp / "slow-once"
    plan = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"',
                            f"checks: test -f {marker} || (touch {marker}; sleep 5)")
    assert plan != PLAN_ONE
    (env.repo / ".auto-office").mkdir(exist_ok=True)
    (env.repo / ".auto-office" / "config.yaml").write_text("verification:\n  check_timeout_seconds: 2\n")
    monkeypatch.setenv("OFFICE_CHECK_LOAD_FACTOR", "0")
    approved_run(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "blocked", data
    env.office("resume", check=0)
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data


# ------------------------------------------------------------------ automatic rebase

def test_run_checks_failing_on_a_moved_base_rebase_and_continue(env, monkeypatch):
    """Run checks that fail only because the base is stale do not discard the
    accepted work: integration rebases onto the moved main and re-checks there."""
    plan = PLAN_STACKED.replace("blast_radius: repo\n", "blast_radius: repo\nchecks: test -f MAIN_FIX\n", 1)
    bare, _, _ = _run(env, monkeypatch, plan)
    con = env.con()
    try:
        landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    finally:
        con.close()
    assert landing["integration"]["status"] == "blocked", landing  # main has not moved: the failure stands
    _advance_main(env, bare, "MAIN_FIX", "fixed on main\n")
    # #337: the rebase is a shared composition boundary (S-rebase) reviewed once by the lane reviewer.
    env.script(convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("resume", check=0)
    con = env.con()
    try:
        landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
        kinds = [r[0] for r in con.execute("SELECT kind FROM events ORDER BY seq")]
    finally:
        con.close()
    assert "integration.auto_rebase" in kinds, kinds
    assert landing["rebase"]["onto"] == env.git("--git-dir", str(bare), "rev-parse", "main").strip(), landing
    assert landing["integration"]["status"] == "accepted", landing
