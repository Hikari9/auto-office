"""Partial landing recovery: a PR GitHub reports unmergeable is recovered only when
its tree provably composes to the reviewed integration tree (T4)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from office import prs
from test_land import _integration_tree, _main_tree, _plan, _run
from test_landing_gaps import _advance_main
from test_task_prs import PLAN_STACKED, _accepted_commit, gh, remote_head

pytestmark = pytest.mark.skipif(
    tuple(int(n) for n in subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.split()[2].split(".")[:2])
    < (2, 38), reason="git merge-tree --write-tree needs git >= 2.38")

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
    merges = [c for c in gh(env)["calls"] if c[:3] == ["pr", "merge", "1"]]
    assert merges[-1][-2:] == ["--match-head-commit", rec["commit"]], merges  # pinned to the proven head


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
    assert not [c for c in gh(env)["calls"] if c[:2] == ["pr", "merge"]]


# ------------------------------------------------------------------ mid-land state

def _stall_t2_on_a_real_conflict(env, monkeypatch):
    """T1 merges; T2 is reported unmergeable and really conflicts with main."""
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    t2_head = _head_of(env, bare, "T2")
    _advance_main(env, bare, "mul.py", "def mul(a, b):\n    return b * a  # main\n")
    _conflict(env, 2, t2_head)
    code, out = env.office("land")
    assert code == 4 and "merge-conflict" in out and "mul.py" in out, out
    assert "merged so far: T1" in out, out  # the refusal names what the land already merged
    return bare, t2_head


def test_rebase_after_a_partial_merge_still_refuses_with_the_real_recovery(env, monkeypatch):
    bare, _ = _stall_t2_on_a_real_conflict(env, monkeypatch)
    assert gh(env)["prs"][0]["state"] == "merged"
    t2_head, main = _head_of(env, bare, "T2"), remote_head(bare, "main")
    calls = len(gh(env)["calls"])
    code, out = env.office("land", "--rebase")
    assert code == 4 and "already-merging" in out, out
    assert _head_of(env, bare, "T2") == t2_head and remote_head(bare, "main") == main and len(gh(env)["calls"]) == calls
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


def test_recovery_waits_for_github_to_show_the_pushed_head(monkeypatch):
    from office import land
    from office.state import Refused
    heads = iter(["old", "old", "new"])
    monkeypatch.setattr(land, "_gh", lambda run, *a, **k: subprocess.CompletedProcess(
        a, 0, json.dumps({"headRefOid": next(heads)}), ""))
    monkeypatch.setattr("time.sleep", lambda s: None)
    land._await_head({}, 1, "new")  # returns once the PR shows the pushed head
    monkeypatch.setattr(land, "_gh", lambda run, *a, **k: subprocess.CompletedProcess(
        a, 0, json.dumps({"headRefOid": "old"}), ""))
    with pytest.raises(Refused) as exc:
        land._await_head({}, 1, "new", tries=3)
    assert exc.value.category == "head-not-synced"


def test_a_branch_policy_refusal_is_not_recovered_or_pushed(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    t1_head = _head_of(env, bare, "T1")
    s = gh(env)
    s["merge_error"] = {"1": "X Pull request #1 is not mergeable: the base branch policy prohibits the merge."}
    (env.tmp / "gh.json").write_text(json.dumps(s))
    code, out = env.office("land")
    assert code == 4 and "merge-failed" in out and "policy" in out, out
    assert _head_of(env, bare, "T1") == t1_head and not _events(env, "pr.recovered")


def test_recovery_pushes_from_the_task_worktree_and_a_push_timeout_is_a_refusal(monkeypatch):
    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, 120)

    monkeypatch.setattr(prs.subprocess, "run", slow)
    ok, err = prs.push({}, {"worktree": "/wt", "branch": "b"}, commit="abc")
    assert not ok and "timed out" in err


def test_a_restacked_child_is_recognised_and_recovered_on_squash(env, monkeypatch):
    """The head land itself pushed when it restacked the child is a head Office knows (not branch-moved)."""
    real, restacks = prs.push, []

    def push_then_report_conflict(run, dispatch, **kw):
        ok, err = real(run, dispatch, **kw)
        if kw.get("force") and ok and not restacks:  # the restack push; the recovery's own push comes later
            restacks.append(dispatch["branch"])
            _conflict(env, 2, remote_head(Path(env.tmp / "origin.git"), dispatch["branch"]))
        return ok, err

    monkeypatch.setattr(prs, "push", push_then_report_conflict)
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=SQUASH_ONLY)
    code, out = env.office("land")
    assert code == 0 and "T2 #2 was not mergeable" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)
    pr = _task_pr(env, "T2")
    assert pr["restacked"]["base"] == "main" and pr["recovered"]["from"] == pr["restacked"]["commit"], pr
    assert pr["recovered"]["from"] != _accepted_commit(env, "T2") and "recovering" not in pr


def test_a_restacked_child_is_not_restacked_again_on_retry(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=SQUASH_ONLY)
    s = gh(env)
    s["merge_error"] = {"2": "X Pull request #2 is not mergeable: the base branch policy prohibits the merge."}
    (env.tmp / "gh.json").write_text(json.dumps(s))
    code, out = env.office("land")
    assert code == 4 and "merge-failed" in out, out
    restacked = _task_pr(env, "T2")["restacked"]["commit"]
    pushes = lambda: [c for c in gh(env)["calls"] if c[:3] == ["pr", "edit", "2"] and "--base" in c]
    s = gh(env)
    s["merge_error"] = {}
    (env.tmp / "gh.json").write_text(json.dumps(s))
    code, out = env.office("land")
    assert code == 0 and "T2 #2 merged" in out, out
    assert len(pushes()) == 1 and _main_tree(env, bare) == _integration_tree(env)
    assert restacked == _task_pr(env, "T2")["restacked"]["commit"]


def test_rebase_method_is_never_recovered_by_pushing_a_merge_commit(env, monkeypatch):
    repo = {**SQUASH_ONLY, "squashMergeAllowed": False, "rebaseMergeAllowed": True}
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=repo)
    t1_head = _head_of(env, bare, "T1")
    _conflict(env, 1, t1_head)
    code, out = env.office("land")
    assert code == 4 and "merge-failed" in out and not _events(env, "pr.recovered"), out
    assert _head_of(env, bare, "T1") == t1_head


def test_a_second_recovery_of_an_already_recovered_head_refuses(env, monkeypatch):
    real = prs.push

    def push_then_stay_conflicting(run, dispatch, **kw):
        ok, err = real(run, dispatch, **kw)
        if kw.get("force") and ok:  # GitHub still calls the recovered head unmergeable
            _conflict(env, 1, remote_head(env.tmp / "origin.git", dispatch["branch"]))
        return ok, err

    monkeypatch.setattr(prs, "push", push_then_stay_conflicting)
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    _conflict(env, 1, _head_of(env, bare, "T1"))
    code, out = env.office("land")
    assert code == 4 and "merge-failed" in out, out
    rec = _task_pr(env, "T1")["recovered"]
    code, out = env.office("land")
    assert code == 4 and "not-mergeable" in out and "earlier recovery" in out, out
    assert _head_of(env, bare, "T1") == rec["commit"]  # no second merge commit was stacked on it


def test_a_land_killed_after_the_recovery_push_still_knows_the_head_is_its_own(env, monkeypatch):
    real = prs.push

    def push_then_die(run, dispatch, **kw):
        real(run, dispatch, **kw)
        raise KeyboardInterrupt("killed after the push")

    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    monkeypatch.setattr(prs, "push", push_then_die)
    _conflict(env, 1, _head_of(env, bare, "T1"))
    assert env.office("land")[0] == 130
    pushed = _head_of(env, bare, "T1")
    pr = _task_pr(env, "T1")
    assert pr["recovering"]["commit"] == pushed and "recovered" not in pr, pr
    monkeypatch.setattr(prs, "push", real)
    _conflict(env, 1, pushed)  # GitHub still calls it unmergeable
    code, out = env.office("land")
    assert code == 4 and "branch-moved" not in out and "earlier recovery" in out, out


def test_main_already_holding_the_same_change_recovers_with_main_folded_in(env, monkeypatch):
    """An ancestry-only conflict: main has T1's change under another commit, so main is not an ancestor of the PR head."""
    from conftest import GOOD_ADD
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    t1_head = _head_of(env, bare, "T1")
    _advance_main(env, bare, "calc.py", GOOD_ADD)
    main = remote_head(bare, "main")
    assert subprocess.run(["git", "--git-dir", str(bare), "merge-base", "--is-ancestor", main, t1_head]).returncode == 1
    _conflict(env, 1, t1_head)
    code, out = env.office("land")
    assert code == 0 and "T1 #1 was not mergeable" in out, out
    rec = _task_pr(env, "T1")["recovered"]
    parents = env.git("--git-dir", str(bare), "rev-list", "--parents", "-n1", rec["commit"]).split()[1:]
    assert parents == [t1_head, main], parents  # the recovery commit folds in the default branch's tip
    assert _main_tree(env, bare) == _integration_tree(env)


def test_mid_land_recovery_of_the_second_pr_matches_the_integration_tree(env, monkeypatch):
    from conftest import GOOD_MUL
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    t2_head = _head_of(env, bare, "T2")
    _advance_main(env, bare, "mul.py", GOOD_MUL)  # main already holds T2's change
    _conflict(env, 2, t2_head)
    code, out = env.office("land")
    assert code == 0 and "T1 #1 merged" in out and "T2 #2 was not mergeable" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)
    assert _task_pr(env, "T2")["recovered"]["from"] == t2_head and "recovered" not in _task_pr(env, "T1")


def test_a_competing_push_between_the_proof_and_the_push_is_refused_by_the_lease(env, monkeypatch):
    real, raced = prs.push, []

    def race_then_push(run, dispatch, **kw):
        if kw.get("force") and not raced:
            raced.append(_push_to_branch(env, bare, dispatch["branch"], "race.txt", "competing\n"))
        return real(run, dispatch, **kw)

    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    _conflict(env, 1, _head_of(env, bare, "T1"))
    monkeypatch.setattr(prs, "push", race_then_push)
    code, out = env.office("land")
    assert code == 4 and "recovery-push-failed" in out, out
    assert _head_of(env, bare, "T1") == raced[0] and "recovered" not in _task_pr(env, "T1")


def test_a_failed_retarget_after_the_restack_push_does_not_restack_again(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"), repo=SQUASH_ONLY)
    real, forced = prs.push, []
    monkeypatch.setattr(prs, "push", lambda run, d, **kw: (forced.append(d["branch"]) if kw.get("force") else None,
                                                           real(run, d, **kw))[1])
    s = gh(env)
    s["edit_failures"] = {"2": 1}
    (env.tmp / "gh.json").write_text(json.dumps(s))
    code, out = env.office("land")
    assert code == 4 and "retarget-failed" in out, out
    restacked = _task_pr(env, "T2")["restacked"]["commit"]
    assert restacked == _head_of(env, bare, "T2") and _task_pr(env, "T2")["base"] != "main"
    code, out = env.office("land")
    assert code == 0 and "T2 #2 merged (squash)" in out, out
    assert _main_tree(env, bare) == _integration_tree(env)
    assert len(forced) == 1, forced  # the retry only retargets


def test_a_git_without_merge_tree_write_tree_refuses_with_the_by_hand_steps(monkeypatch, tmp_path):
    from office import land
    from office.state import Refused
    monkeypatch.setattr(land, "_git", lambda *a, **k: subprocess.CompletedProcess(a, 129, "", "usage: git merge-tree"))
    with pytest.raises(Refused) as exc:
        land._merge_tree(tmp_path, "a" * 40, "b" * 40)
    assert exc.value.category == "recovery-failed" and "git >= 2.38" in exc.value.message and "compose by hand" in exc.value.next_step
