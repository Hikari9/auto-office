"""Partial landing recovery: a PR GitHub reports unmergeable is recovered only when
its tree provably composes to the reviewed integration tree (T4)."""
from __future__ import annotations

import json
import subprocess

import pytest

from office import prs
from test_land import _integration_tree, _main_tree, _plan, _run
from test_landing_gaps import _advance_main
from test_task_prs import PLAN_STACKED, _accepted_commit, gh, remote_head

SQUASH_ONLY = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "mergeCommitAllowed": False,
               "squashMergeAllowed": True, "rebaseMergeAllowed": False}


def _head_of(env, bare, task: str) -> str:
    return remote_head(bare, next(p["head"] for p in gh(env)["prs"] if p["title"].startswith(f"{task}:")))


def _conflict(env, number: int, head: str) -> None:
    s = gh(env)
    s["conflicting"] = {**s.get("conflicting", {}), str(number): head}
    (env.tmp / "gh.json").write_text(json.dumps(s))


def _task_pr(env, task: str) -> dict:
    con = env.con()
    try:
        return json.loads(con.execute("SELECT pr_json FROM tasks WHERE id=?", (task,)).fetchone()[0])
    finally:
        con.close()


def _events(env, kind: str) -> list[dict]:
    con = env.con()
    try:
        return [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE kind=?", (kind,))]
    finally:
        con.close()


def _push_to_branch(env, bare, branch: str, path: str, text: str) -> str:
    clone = env.tmp / "pusher"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    for args in (("checkout", "-q", branch), ("config", "user.name", "t"), ("config", "user.email", "t@t")):
        subprocess.run(["git", "-C", str(clone), *args], check=True)
    (clone / path).write_text(text)
    for args in (("add", path), ("commit", "-qm", "someone else"), ("push", "-q", "origin", branch)):
        subprocess.run(["git", "-C", str(clone), *args], check=True)
    return remote_head(bare, branch)


# ------------------------------------------------------------------ recovery

@pytest.mark.parametrize("method", ["merge", "squash"])
def test_clean_ancestry_only_conflict_recovers_and_main_matches_the_integration(env, monkeypatch, method):
    kwargs = {"repo": SQUASH_ONLY} if method == "squash" else {}
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), **kwargs)
    t1_head = _head_of(env, bare, "T1")
    assert t1_head == _accepted_commit(env, "T1")
    _conflict(env, 1, t1_head)
    code, out = env.office("land")
    assert code == 0, out
    assert "T1 #1 was not mergeable" in out and f"T1 #1 merged ({method})" in out and f"T2 #2 merged ({method})" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)
    rec = _task_pr(env, "T1")["recovered"]
    assert rec["from"] == t1_head and rec["commit"] == _head_of(env, bare, "T1") and rec["tree"], rec
    parents = env.git("--git-dir", str(bare), "rev-list", "--parents", "-n1", rec["commit"]).split()[1:]
    assert parents[0] == t1_head and len(parents) == 2, parents
    assert [e["commit"] for e in _events(env, "pr.recovered")] == [rec["commit"]]
    assert "recovered" not in _task_pr(env, "T2")


def test_a_real_conflict_refuses_naming_files_and_pushes_nothing(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    t1_head = _head_of(env, bare, "T1")
    _advance_main(env, bare, "calc.py", "def add(a, b):\n    return b + a  # main\n")
    main = remote_head(bare, "main")
    _conflict(env, 1, t1_head)
    code, out = env.office("land")
    assert code == 4 and "merge-conflict" in out and "calc.py" in out and "compose by hand" in out, out
    assert _head_of(env, bare, "T1") == t1_head and remote_head(bare, "main") == main
    assert "recovered" not in _task_pr(env, "T1") and not _events(env, "pr.recovered")


def test_a_moved_remote_branch_refuses_branch_moved(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    branch = next(p["head"] for p in gh(env)["prs"] if p["title"].startswith("T1:"))
    moved = _push_to_branch(env, bare, branch, "extra.txt", "not reviewed\n")
    assert moved != _accepted_commit(env, "T1")
    _conflict(env, 1, moved)
    main = remote_head(bare, "main")
    code, out = env.office("land")
    assert code == 4 and "branch-moved" in out and moved[:12] in out, out
    assert _head_of(env, bare, "T1") == moved and remote_head(bare, "main") == main
    assert not _events(env, "pr.recovered")


def test_a_tree_that_differs_from_the_integration_refuses_before_pushing(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    t1_head = _head_of(env, bare, "T1")
    _advance_main(env, bare, "NOTES.md", "unrelated\n")  # composes cleanly, but not into the reviewed tree
    main = remote_head(bare, "main")
    _conflict(env, 1, t1_head)
    code, out = env.office("land")
    assert code == 4 and "recovery-tree-mismatch" in out and "compose by hand" in out, out
    assert _head_of(env, bare, "T1") == t1_head and remote_head(bare, "main") == main
    assert "recovered" not in _task_pr(env, "T1")


def test_ask_mode_never_recovers_or_pushes(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, PLAN_STACKED)
    t1_head = _head_of(env, bare, "T1")
    _conflict(env, 1, t1_head)
    code, out = env.office("land")
    assert code == 0 and "office land --merge" in out, out
    assert _head_of(env, bare, "T1") == t1_head and not _events(env, "pr.recovered")


# ------------------------------------------------------------------ mid-land state

def _stall_t2_on_a_real_conflict(env, monkeypatch):
    """T1 merges; T2 is reported unmergeable and really conflicts with main."""
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    t2_head = _head_of(env, bare, "T2")
    _advance_main(env, bare, "mul.py", "def mul(a, b):\n    return b * a  # main\n")
    _conflict(env, 2, t2_head)
    code, out = env.office("land")
    assert code == 4 and "merge-conflict" in out and "mul.py" in out, out
    return bare, t2_head


def test_rebase_after_a_partial_merge_still_refuses_with_the_real_recovery(env, monkeypatch):
    _stall_t2_on_a_real_conflict(env, monkeypatch)
    assert gh(env)["prs"][0]["state"] == "merged"
    code, out = env.office("land", "--rebase")
    assert code == 4 and "already-merging" in out, out
    assert "recovered when it provably composes to the reviewed tree" in out and "by-hand" in out, out


def test_the_retargeted_base_is_recorded_so_a_retry_does_not_retarget_again(env, monkeypatch):
    bare, t2_head = _stall_t2_on_a_real_conflict(env, monkeypatch)
    assert _task_pr(env, "T2")["base"] == "main"
    edits = lambda: [c for c in gh(env)["calls"] if c[:2] == ["pr", "edit"] and "--base" in c and c[2] == "2"]
    assert len(edits()) == 1
    code, out = env.office("land")
    assert code == 4 and "merge-conflict" in out, out
    assert len(edits()) == 1 and _head_of(env, bare, "T2") == t2_head


# ------------------------------------------------------------------ leases

def test_restack_pushes_with_an_explicit_lease(env, monkeypatch):
    pushes = []
    real = prs.push

    def spy(run, dispatch, **kw):
        pushes.append((dispatch["branch"], kw))
        return real(run, dispatch, **kw)

    monkeypatch.setattr(prs, "push", spy)
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=SQUASH_ONLY)
    pushes.clear()
    code, out = env.office("land")
    assert code == 0, out
    forced = [(b, kw) for b, kw in pushes if kw.get("force")]
    assert len(forced) == 1 and forced[0][1]["expected"] == _accepted_commit(env, "T2"), pushes


def test_push_leases_force_to_the_expected_sha_and_refuses_a_bare_force(monkeypatch):
    seen = []

    def fake_run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(prs.subprocess, "run", fake_run)
    d = {"worktree": "/wt", "branch": "office/x/T1"}
    assert prs.push({}, d, commit="abc", force=True, expected="def")[0]
    assert "--force-with-lease=refs/heads/office/x/T1:def" in seen[-1] and "--force-with-lease" not in seen[-1]
    assert prs.push({}, d, commit="abc")[0] and not any(a.startswith("--force") for a in seen[-1])
    with pytest.raises(ValueError):
        prs.push({}, d, commit="abc", force=True)
