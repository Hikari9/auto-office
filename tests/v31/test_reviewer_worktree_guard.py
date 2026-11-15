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
    return env.office("dispatch", "T1", "--as", "codex/gpt-6-luna@high", "--review-as", LUNA, env=PI_LUNA_ENV, check=0)


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
