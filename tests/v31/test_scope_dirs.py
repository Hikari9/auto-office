"""#334: a scope entry ending in `/` names a directory and matches every file under it, at submit
and in overlap checks, exactly as `dir/**` does."""
from __future__ import annotations

import pytest

from conftest import PLAN_ONE, approved_run
from office import planfile

PLAN_DIR = PLAN_ONE.replace("scope: calc.py", "scope: pkg/")


def _scope(text: str) -> list[str]:
    plan = planfile.parse(text)
    assert not plan.errors, plan.errors
    return plan.tasks[0]["scope"]


def test_a_directory_entry_parses_to_the_dir_glob():
    assert _scope(PLAN_DIR) == ["pkg/**"]
    assert _scope(PLAN_ONE.replace("scope: calc.py", "scope: src/auth/, tests/auth/, README.md")) == [
        "src/auth/**", "tests/auth/**", "README.md"]


def test_a_shared_directory_entry_keeps_its_marker():
    text = PLAN_ONE.replace("scope: calc.py", "scope: calc.py\nshared: docs/")
    assert _scope(text) == ["calc.py", "+docs/**"]


def test_every_file_under_the_directory_is_in_scope_however_it_is_spelled():
    for entry in ("pkg/", "pkg/**", "+pkg/"):
        for path in ("pkg/a.py", "pkg/deep/er/b.py", "pkg"):
            assert planfile.path_in_scope(path, [entry]), (entry, path)
        for path in ("pkgx/a.py", "other/pkg/a.py", "pk"):
            assert not planfile.path_in_scope(path, [entry]), (entry, path)


def test_directory_entries_overlap_what_they_contain_and_nothing_else():
    assert planfile.scopes_overlap(["pkg/"], ["pkg/a.py"])
    assert planfile.scopes_overlap(["pkg/a.py"], ["pkg/"])
    assert planfile.scopes_overlap(["pkg/"], ["pkg/sub/**"])
    assert planfile.scopes_overlap(["pkg/"], ["pkg/**"])
    assert not planfile.scopes_overlap(["pkg/"], ["pkgx/a.py"])
    assert not planfile.scopes_overlap(["pkg/"], ["lib/"])


def test_parallel_tasks_naming_one_directory_warn_about_the_overlap():
    plan = planfile.parse(PLAN_DIR + """
### T2: Other
scope: pkg/sub/mod.py
depends: none
checks: none
accept:
- x
visual: none
""")
    assert any("T1 and T2" in w and "overlap" in w for w in plan.warnings), plan.warnings


@pytest.mark.integration
def test_a_task_scoped_to_a_directory_submits_its_nested_files(env):
    """Before the fix a file under `pkg/` counted as outside `pkg/`: it stayed out of the revision
    and the task was accepted with nothing in it."""
    plan = PLAN_DIR.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', "checks: none")
    approved_run(env, plan=plan, executor=[{"write": {"pkg/a.py": "x = 1\n", "pkg/sub/b.py": "y = 2\n"}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}], convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    code, out = env.office("dispatch", "T1", check=0)
    con = env.con()
    task = dict(con.execute("SELECT status, pause_reason FROM tasks WHERE id='T1'").fetchone())
    assert task["status"] == "accepted", (task, out)
    sha = con.execute("SELECT commit_sha FROM revisions WHERE task_id='T1'").fetchone()[0]
    changed = env.git("diff", "--name-only", f"{sha}^", sha).split()
    assert changed == ["pkg/a.py", "pkg/sub/b.py"], changed
