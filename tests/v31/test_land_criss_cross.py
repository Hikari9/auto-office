"""`office land` with a task branch and the default branch that have several merge bases (#398 item 4).

Restacks merge main-side commits into a task branch while main merges the branch's earlier commits: a
criss-cross. GitHub then reports the PR CONFLICTING although `git merge-tree` merges it cleanly. The fake
`gh` below says the same, so a land that does not settle it itself cannot merge the PR.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conftest import GOOD_ADD
from test_land import _plan, _run
from test_task_prs import PLAN_STACKED, gh, remote_head

TESTS = Path(__file__).resolve().parent
CONFLICTING_GH = f"""#!{sys.executable}
import subprocess, sys
sys.path.insert(0, {str(TESTS)!r})
import fake_gh

real = fake_gh.git_merge


def guarded(pr, method):
    origin = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    bases = subprocess.run(["git", "--git-dir", origin, "merge-base", "--all", "refs/heads/" + pr["head"],
                            "refs/heads/" + pr["base"]], capture_output=True, text=True).stdout.split()
    if len(bases) > 1:
        print("Pull request is not mergeable: the merge commit cannot be cleanly created (CONFLICTING)", file=sys.stderr)
        raise SystemExit(1)
    real(pr, method)


fake_gh.git_merge = guarded
raise SystemExit(fake_gh.main(sys.argv[1:]))
"""


def _g(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _criss_cross(env, bare, *, clash: bool) -> tuple[str, str]:
    """Give T1's PR branch and main two incomparable common ancestors; returns (branch, branch head)."""
    branch = gh(env)["prs"][0]["head"]
    wt = Path(env.con().execute("SELECT d.worktree FROM dispatches d JOIN tasks t ON t.current_dispatch_id=d.id "
                                "WHERE t.id='T1'").fetchone()[0])
    accepted = remote_head(bare, branch)
    clone = env.tmp / "main-side"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    (clone / ("README.md" if clash else "NOTES.md")).write_text("main side\n")
    _g(clone, "add", "-A")
    _g(clone, "commit", "-qm", "m1")
    m1 = _g(clone, "rev-parse", "HEAD")
    _g(clone, "push", "-q", "origin", "main")
    # The branch takes m1 (the restack), then moves on.
    _g(wt, "fetch", "-q", "origin", "main")
    _g(wt, "merge", "--no-edit", "-q", m1)
    (wt / ("README.md" if clash else "calc.py")).write_text("branch side\n" if clash else GOOD_ADD + "# more\n")
    _g(wt, "add", "-A")
    _g(wt, "commit", "-qm", "x2")
    head = _g(wt, "rev-parse", "HEAD")
    _g(wt, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    # Main takes the accepted commit, so it and m1 are both common ancestors and neither contains the other.
    _g(clone, "merge", "--no-ff", "--no-edit", "-q", accepted)
    if clash:  # main then edits the line the branch edited
        (clone / "README.md").write_text("main side, again\n")
        _g(clone, "commit", "-qam", "m2")
    _g(clone, "push", "-q", "origin", "main")
    bases = _g(bare, "merge-base", "--all", f"refs/heads/{branch}", "refs/heads/main").split()
    assert len(bases) == 2, bases
    return branch, head


def test_land_merges_a_criss_cross_branch_that_merge_tree_merges_cleanly(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    branch, head = _criss_cross(env, bare, clash=False)
    (env.bin / "gh").write_text(CONFLICTING_GH)
    code, out = env.office("land")
    assert code == 0, out
    assert "T1 had several merge bases with main: merged main into" in out and "T1 #1 merged" in out, out
    assert "T2 #2 merged" in out, out
    state = gh(env)
    assert [p["state"] for p in state["prs"]] == ["merged", "merged"]
    # The task's own work reached main; the merge Office made is on the pushed branch.
    assert subprocess.run(["git", "--git-dir", str(bare), "merge-base", "--is-ancestor", head, "main"]).returncode == 0
    assert len(_g(bare, "merge-base", "--all", f"refs/heads/{branch}", "refs/heads/main").split()) == 1
    assert _g(bare, "show", "main:NOTES.md") == "main side"


def test_a_real_conflict_in_a_criss_cross_branch_still_refuses_and_merges_nothing(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    branch, head = _criss_cross(env, bare, clash=True)
    (env.bin / "gh").write_text(CONFLICTING_GH)
    main_before = remote_head(bare, "main")
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 4 and "merge-conflict" in out and "README.md" in out, out
    assert [p["state"] for p in gh(env)["prs"]] == ["open", "open"]
    assert remote_head(bare, "main") == main_before and remote_head(bare, branch) == head


def test_a_branch_with_one_merge_base_is_left_alone(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    (env.bin / "gh").write_text(CONFLICTING_GH)
    branch = gh(env)["prs"][0]["head"]
    before = remote_head(bare, branch)
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 0 and "several merge bases" not in out, out
    assert remote_head(bare, branch) == before


def test_a_single_branch_clone_is_settled_too(env, monkeypatch):
    """The compare refs are fetched explicitly: a clone whose fetch refspec names only main still gets them."""
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    branch, head = _criss_cross(env, bare, clash=False)
    _g(env.repo, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    _g(env.repo, "update-ref", "-d", f"refs/remotes/origin/{branch}")  # a push left one behind; a fresh clone has none
    (env.bin / "gh").write_text(CONFLICTING_GH)
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 0 and "several merge bases" in out, out
