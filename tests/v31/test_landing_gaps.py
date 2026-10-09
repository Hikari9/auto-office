"""Landing gaps from run 10944d06: PR body notes survive pushes, `office land
--rebase` onto a moved main, and a check timeout under host load is the
environment, not a code failure."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import GOOD_ADD, GOOD_MUL, PLAN_ONE, approved_run
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


# ------------------------------------------------------------------ open task after land --rebase (#361)

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
APPROVED = {"reply": "VERDICT: APPROVED\nNEXT proceed"}
PLAN_SHARED_README = PLAN_STACKED.replace("scope: calc.py", "scope: calc.py, README.md")


def _reopened_after_rebase(env, monkeypatch, main_files: dict[str, str], plan: str = PLAN_STACKED, tid: str = "T1"):
    """A run landed-ready, `land --rebase`d onto a moved main, then T1 reopened by an amendment.
    Returns (bare, T1's dispatch row, T1's worktree, T1's worker env, the run's new base)."""
    bare, _, _ = _run(env, monkeypatch, plan)
    clone = env.tmp / "advance"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    for path, text in main_files.items():
        (clone / path).write_text(text)
    for args in (("add", "-A"), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved"),
                 ("push", "-q", "origin", "main")):
        subprocess.run(["git", "-C", str(clone), *args], check=True)
    env.script(convergence_reviewer=[APPROVED] * 4, integration_reviewer=[{"reply": "VERDICT: PASS"}] * 2)
    code, out = env.office("land", "--rebase")
    assert code == 0, out
    assert "office rebase T<n> --move" in out and "--merge" in out, out  # both supported paths are named
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited' WHERE task_id=?", (tid,))
    con.commit()
    code, out = env.office("amend", tid, "--", "also bump the version", env=EXTERNAL)
    assert code == 0, out
    row = dict(env.con().execute("SELECT d.* FROM dispatches d JOIN tasks t ON t.current_dispatch_id=d.id WHERE t.id=?", (tid,)).fetchone())
    onto = env.git("--git-dir", str(bare), "rev-parse", "main").strip()
    wenv = {"OFFICE_RUN_ID": row["run_id"], "OFFICE_DISPATCH_ID": row["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual", **EXTERNAL}
    return bare, row, Path(row["worktree"]), wenv, onto


def _commit(env, wt, files: dict[str, str], msg="T1 amended"):
    for name, text in files.items():
        (wt / name).write_text(text)
    env.git("add", "-A", cwd=wt)
    env.git("-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-qm", msg, cwd=wt)


def _base_of(env, did):
    return env.con().execute("SELECT base_commit FROM dispatches WHERE id=?", (did,)).fetchone()[0]


def _worker_exits(env, row):
    con = env.con()
    con.execute("UPDATE dispatches SET ended_at='2026-01-01T00:00:00+00:00', status='exited' WHERE id=?", (row["id"],))
    con.commit()


def _ack(env, wenv):
    env.office("ack", "A1", env=wenv, check=0)


def test_land_rebase_names_both_ways_to_put_an_open_task_on_the_new_base(env, monkeypatch):
    _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})


def test_rebase_task_needs_an_explicit_path_and_a_moved_base(env, monkeypatch):
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})
    code, out = env.office("rebase", "T1")
    assert code == 2 and "--move" in out and "--merge" in out and "rebase-path" in out, out
    assert _base_of(env, row["id"]) == row["base_commit"]  # Office picked nothing


def test_main_files_are_not_the_tasks_edits_once_the_new_base_is_recorded(env, monkeypatch):
    """#361: the executor merged main into its branch; preflight and submit judged it against the old base."""
    from test_self_review_ledger import write_ledger
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n", "docs.md": "x\n"})
    env.git("fetch", "-q", "origin", "main", cwd=wt)
    env.git("-c", "user.email=t@e.test", "-c", "user.name=t", "merge", "--no-edit", "-q", onto, cwd=wt)
    _commit(env, wt, {"calc.py": GOOD_ADD + "# amended\n"})
    write_ledger(wt)
    _ack(env, wenv)
    # Not recorded yet: preflight judges the scope against the new base, waits for the record, and says who records it.
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 75 and "office rebase T1 --record" in out and "NOTES.md" not in out, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "base-not-recorded" in out and "NOTES.md" not in out and "outside its scope" not in out, out
    # The orchestrator records it; nothing of main's is the task's edit any more.
    code, out = env.office("rebase", "T1", "--record")
    assert code == 0 and onto[:12] in out, out
    assert _base_of(env, row["id"]) == onto
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "PREFLIGHT ready" in out, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "outside" not in out, out
    changed = env.con().execute("SELECT changed_json FROM revisions WHERE task_id='T1' ORDER BY seq DESC LIMIT 1").fetchone()[0]
    assert "NOTES.md" not in (changed or "") and "docs.md" not in (changed or ""), changed


def test_record_refuses_a_worktree_that_does_not_hold_the_new_base(env, monkeypatch):
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})
    code, out = env.office("rebase", "T1", "--record")
    assert code == 4 and "base-not-in-worktree" in out, out
    assert _base_of(env, row["id"]) == row["base_commit"]


def test_merge_path_merges_the_new_main_in_and_records_it(env, monkeypatch):
    from test_self_review_ledger import write_ledger
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})
    _commit(env, wt, {"calc.py": GOOD_ADD + "# amended\n"})
    _worker_exits(env, row)
    code, out = env.office("rebase", "T1", "--merge")
    assert code == 0, out
    assert (wt / "NOTES.md").exists()
    assert env.git("merge-base", "--is-ancestor", onto, "HEAD", cwd=wt) == ""
    assert _base_of(env, row["id"]) == onto
    assert "rewritten" not in out  # a merge keeps history: no force-push
    write_ledger(wt)
    _ack(env, wenv)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0, out


def test_move_path_reapplies_the_change_on_the_new_base_and_records_it(env, monkeypatch):
    from test_self_review_ledger import write_ledger
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})
    _commit(env, wt, {"calc.py": GOOD_ADD + "# amended\n"})
    _worker_exits(env, row)
    code, out = env.office("rebase", "T1", "--move")
    assert code == 0 and "rewritten" in out, out
    assert env.git("rev-parse", "HEAD^", cwd=wt).strip() == onto  # one commit on the new base
    assert (wt / "calc.py").read_text().endswith("# amended\n") and (wt / "NOTES.md").exists()
    assert _base_of(env, row["id"]) == onto
    write_ledger(wt)
    _ack(env, wenv)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "outside" not in out, out
    assert env.git("diff", "--name-only", onto, "HEAD", cwd=wt).split() == ["calc.py"]


@pytest.mark.parametrize("how", ["--move", "--merge"])
def test_a_version_collision_is_named_with_both_paths_and_nothing_is_resolved(env, monkeypatch, how):
    """Main and the task both bump the same version line: Office names the file and both ways out."""
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"README.md": "fixture 1.0.1\n"},
                                                       plan=PLAN_SHARED_README)
    _commit(env, wt, {"README.md": "fixture 1.0.1-task\n"})
    _worker_exits(env, row)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    code, out = env.office("rebase", "T1", how)
    assert code == 4 and "rebase-collision" in out and "README.md" in out, out
    assert "version field" in out and "git reset --hard" in out and "git merge" in out, out  # (a) and (b)
    assert env.git("rev-parse", "HEAD", cwd=wt).strip() == head  # the worktree is untouched
    assert env.git("status", "--porcelain", "--untracked-files=no", cwd=wt) == ""
    assert _base_of(env, row["id"]) == row["base_commit"]
    # Settling it by hand (path b) and recording it works.
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@e.test", "-c", "user.name=t", "merge", "--no-edit", onto],
                   capture_output=True)
    (wt / "README.md").write_text("fixture 1.0.2\n")
    _commit(env, wt, {}, "resolve the version")
    code, out = env.office("rebase", "T1", "--record")
    assert code == 0 and _base_of(env, row["id"]) == onto, out


def test_a_live_worker_blocks_the_path_commands(env, monkeypatch):
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})
    code, out = env.office("rebase", "T1", "--merge")  # the reopened session is still running
    assert code == 4 and "worker-live" in out and "office revoke T1" in out, out


@pytest.mark.parametrize("how", ["--move", "--merge"])
def test_the_path_commands_only_touch_the_task_branch_checkout(env, monkeypatch, how):
    """A worktree left on another branch is not reset or merged into."""
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})
    _commit(env, wt, {"calc.py": GOOD_ADD + "# amended\n"})
    env.git("switch", "-q", "-c", "scratch", cwd=wt)
    _worker_exits(env, row)
    scratch = env.git("rev-parse", "HEAD", cwd=wt).strip()
    code, out = env.office("rebase", "T1", how)
    assert code == 4 and "worktree-mismatch" in out, out
    assert env.git("rev-parse", "HEAD", cwd=wt).strip() == scratch
    assert _base_of(env, row["id"]) == row["base_commit"]


@pytest.mark.parametrize("how", ["--merge", "--move"])
def test_a_stacked_task_is_put_on_a_base_holding_its_dependency_and_the_new_main(env, monkeypatch, how):
    """T2 builds on T1's revision, which main does not contain: its new base is an Office merge of that revision and
    the new main, and the worktree must hold both."""
    from test_self_review_ledger import write_ledger
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"}, tid="T2")
    _commit(env, wt, {"mul.py": GOOD_MUL + "# amended\n"})
    _worker_exits(env, row)
    old = row["base_commit"]
    code, out = env.office("rebase", "T2", how)
    assert code == 0, out
    new = _base_of(env, row["id"])
    assert new not in (old, onto)
    for ancestor in (old, onto):
        assert subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", ancestor, new]).returncode == 0
        assert subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", ancestor, "HEAD"]).returncode == 0
    write_ledger(wt)
    _ack(env, wenv)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "outside" not in out, out
    assert env.git("diff", "--name-only", new, "HEAD", cwd=wt).split() == ["mul.py"]


def test_a_dependent_tasks_collision_with_the_new_base_refuses_a_move_but_not_a_hand_merge(env, monkeypatch):
    """The task's old base (a dependency revision) and the new main change one file differently: there is no merged
    base commit to move onto, but a merge or a hand merge records the new run base itself."""
    from office import land
    _run(env, monkeypatch, PLAN_STACKED)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    env.git("switch", "-q", "-c", "side", "HEAD")
    (env.repo / "README.md").write_text("old base side\n")
    env.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "dependency side")
    old = env.git("rev-parse", "HEAD").strip()
    env.git("switch", "-q", "main")
    (env.repo / "README.md").write_text("new main side\n")
    env.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "main side")
    onto = env.git("rev-parse", "HEAD").strip()
    with pytest.raises(state.Refused) as e:
        land._new_base(run, "T2", old, onto, strict=True)
    assert e.value.category == "rebase-collision" and "README.md" in e.value.message
    assert land._new_base(run, "T2", old, onto, strict=False) == onto


def test_a_restack_after_a_task_move_keeps_the_new_run_base(env, monkeypatch):
    """`office rebase T2` recorded a base holding main's move; the next restack must not drop it (the worktree
    still holds main, so submit would refuse base-not-recorded on every restack)."""
    from office import integration, rerun
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"}, tid="T2")
    _worker_exits(env, row)
    assert env.office("rebase", "T2", "--merge")[0] == 0
    # T1 is accepted again on a newer revision the T2 worktree lacks.
    t1_wt = Path(env.con().execute("SELECT worktree FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone()[0])
    (t1_wt / "calc.py").write_text(GOOD_ADD + "# t1 again\n")
    env.git("add", "-A", cwd=t1_wt)
    env.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "t1 again", cwd=t1_wt)
    newer = env.git("rev-parse", "HEAD", cwd=t1_wt).strip()
    con = env.con()
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, commit_sha, tree_sha, requirements_version, plan_version, "
                "applied_version, env_fingerprint, operation_id, status, created_at) SELECT 'Rnew', run_id, 'T1', 99, ?, tree_sha, "
                "requirements_version, plan_version, applied_version, env_fingerprint, 'op-new', 'current', created_at "
                "FROM revisions WHERE task_id='T1' LIMIT 1", (newer,))
    con.execute("UPDATE tasks SET accepted_revision_id='Rnew', current_revision_id='Rnew' WHERE id='T1'")
    con.commit()
    run = state.get_run(con, row["run_id"])
    restack = rerun._restack(con, run, state.get_task(con, run["id"], "T2"), str(wt))
    assert restack and restack["conflict"] is None, restack
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    assert integration.stale_base(run, restack["base"], head) is None
    assert subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", onto, restack["base"]]).returncode == 0


def _t1_accepted_again_on(env, files: dict[str, str]):
    """T1 gets a newer accepted revision (a commit on its branch) that T2's worktree lacks."""
    t1_wt = Path(env.con().execute("SELECT worktree FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone()[0])
    for name, text in files.items():
        (t1_wt / name).write_text(text)
    env.git("add", "-A", cwd=t1_wt)
    env.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "t1 again", cwd=t1_wt)
    con = env.con()
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, commit_sha, tree_sha, requirements_version, plan_version, "
                "applied_version, env_fingerprint, operation_id, status, created_at) SELECT 'Rnew', run_id, 'T1', 99, ?, tree_sha, "
                "requirements_version, plan_version, applied_version, env_fingerprint, 'op-new', 'current', created_at "
                "FROM revisions WHERE task_id='T1' LIMIT 1", (env.git("rev-parse", "HEAD", cwd=t1_wt).strip(),))
    con.execute("UPDATE tasks SET accepted_revision_id='Rnew', current_revision_id='Rnew' WHERE id='T1'")
    con.commit()
    return con


def test_a_restack_whose_dependency_collides_with_the_held_new_base_is_left_to_the_executor(env, monkeypatch):
    from office import rerun
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"}, tid="T2")
    _worker_exits(env, row)
    assert env.office("rebase", "T2", "--merge")[0] == 0
    con = _t1_accepted_again_on(env, {"calc.py": GOOD_ADD + "# t1 again\n", "NOTES.md": "t1's own notes\n"})
    run = state.get_run(con, row["run_id"])
    restack = rerun._restack(con, run, state.get_task(con, run["id"], "T2"), str(wt))  # does not refuse
    assert restack and restack["conflict"] and restack["conflict"]["task"] == "T1", restack
    assert env.git("status", "--porcelain", "--untracked-files=no", cwd=wt) == ""  # the failed merge was aborted


def test_move_refuses_rather_than_overwrite_an_untracked_file_the_moved_commit_tracks(env, monkeypatch):
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})
    _commit(env, wt, {"calc.py": GOOD_ADD + "# amended\n"})
    (wt / "NOTES.md").write_text("my scratch notes\n")  # untracked here, tracked on the new main
    exclude = Path(env.git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude", cwd=wt).strip())
    exclude.write_text(exclude.read_text() + "NOTES.md\n")  # even an ignored one is not overwritten
    _worker_exits(env, row)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    code, out = env.office("rebase", "T1", "--move")
    assert code == 4 and "worktree-dirty" in out and "NOTES.md" in out, out
    assert (wt / "NOTES.md").read_text() == "my scratch notes\n" and env.git("rev-parse", "HEAD", cwd=wt).strip() == head


def test_the_guard_refuses_a_worktree_that_is_the_primary_checkout(env, monkeypatch):
    bare, row, wt, wenv, onto = _reopened_after_rebase(env, monkeypatch, {"NOTES.md": "unrelated\n"})
    _worker_exits(env, row)
    con = env.con()
    con.execute("UPDATE dispatches SET worktree=?, branch='main' WHERE id=?", (str(env.repo), row["id"]))
    con.commit()
    head = env.git("rev-parse", "HEAD").strip()
    code, out = env.office("rebase", "T1", "--merge")
    assert code == 4 and "worktree-mismatch" in out, out
    assert env.git("rev-parse", "HEAD").strip() == head
