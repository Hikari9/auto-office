"""The reviewer worktree guard watches the reviewer's own snapshot, not the shared task worktree."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run
from office import gates
from test_pi_registration import LUNA, PI_LUNA_ENV, _events, _git_repo, _install_pi

PASS = {"reply": "VERDICT: APPROVED\nNEXT proceed"}


def test_snapshot_is_detached_at_head_and_removed(tmp_path):
    repo = _git_repo(tmp_path)
    snap = gates._review_snapshot(repo, tmp_path / "snap")
    assert snap and (snap / "a.txt").read_text() == "one\n"
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    assert subprocess.run(["git", "-C", str(snap), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head
    (repo / "a.txt").write_text("edited elsewhere\n")  # the shared worktree changes: the snapshot does not
    guard = gates._worktree_guard({"harness": "pi"}, "reviewer", snap)
    before = guard(None)
    assert guard(before) is None
    (snap / "a.txt").write_text("reviewer edit\n")
    assert "tracked diff" in guard(before)
    gates._drop_review_snapshot(repo, snap)
    assert not snap.exists()
    assert str(snap) not in subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout


def test_snapshot_failure_falls_back_to_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gates._review_snapshot(plain, tmp_path / "snap") is None


def _pi_review(env, reviewer):
    from office import harnesscmd
    _install_pi(env)
    harnesscmd.model_add("pi/openai/gpt-6-luna", ["high"], model_id="gpt-6-luna", roles=["reviewer"])
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD, "keep.txt": "x"}, "submit": True}],
                 convergence_reviewer=reviewer)
    return env.office("dispatch", "T1", "--as", "codex/gpt-6.1-terra@high", "--review-as", LUNA, env=PI_LUNA_ENV, check=0)


def _reviewer_cwds(env):
    return [Path(c["cwd"]) for c in env.calls() if c.get("role") == "convergence_reviewer" and c.get("harness") == "pi"]


@pytest.mark.approved
def test_reviewer_runs_in_a_snapshot_that_is_removed_and_the_shared_worktree_is_untouched(env):
    _pi_review(env, [PASS])
    cwds = _reviewer_cwds(env)
    assert cwds, env.calls()
    assert cwds[0].name == "snapshot" and cwds[0].parent.parent.name == "dispatches", cwds
    assert not cwds[0].exists(), cwds
    assert not _events(env, "review.worktree_changed")


@pytest.mark.approved
def test_reviewer_write_in_the_snapshot_still_degrades_it(env):
    _pi_review(env, [{"write": {"planted.txt": "reviewer"}, "reply": "VERDICT: APPROVED\nNEXT proceed"}])
    assert _events(env, "review.worktree_changed")
    for cwd in _reviewer_cwds(env):
        assert not cwd.exists(), "snapshots are removed after a failed review too"


def test_snapshot_paths_rewrite_the_checkout_and_keep_outside_dirs(tmp_path):
    checkout, snap, evidence = tmp_path / "wt", tmp_path / "snap", tmp_path / "evidence"
    brief = f"Review {checkout}/src and {checkout}. Not {checkout}2/x. Evidence {evidence}."
    text, dirs = gates._snapshot_paths(brief, [checkout, checkout / "src", evidence], checkout, snap)
    assert text == f"Review {snap}/src and {snap}. Not {checkout}2/x. Evidence {evidence}."
    assert dirs == [snap, snap / "src", evidence]
    assert gates._snapshot_paths("x", None, checkout, snap) == ("x", None)


def test_guarded_review_with_no_job_keeps_the_snapshot_until_settled(tmp_path, monkeypatch):
    """The no-job reviewer path checks the guard before the snapshot goes, and removes it after."""
    repo = _git_repo(tmp_path)
    snap = gates._review_snapshot(repo, tmp_path / "snap")
    guard = gates._worktree_guard({"harness": "pi"}, "reviewer", snap)
    before = guard(None)
    (repo / "a.txt").write_text("concurrent edit\n")  # the shared worktree moves during the review
    assert guard(before) is None  # checked while the snapshot still exists: not degraded
    gates._drop_review_snapshot(repo, snap)
    assert not snap.exists()


@pytest.mark.approved
def test_reviewer_brief_names_the_snapshot_not_the_shared_checkout(env):
    _pi_review(env, [PASS])
    cwd = _reviewer_cwds(env)[0]
    brief = (cwd.parent / "brief.md").read_text()
    assert f"a read-only checkout is at {cwd}" in brief, brief
