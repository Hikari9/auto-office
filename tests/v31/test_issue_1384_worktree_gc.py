"""#1384: squash-aware worktree cleanup at closeout and office gc, in real git worktrees."""
from __future__ import annotations

from pathlib import Path

from office import cleanup
from test_closeout import _merged


def _run_id(env):
    return env.con().execute("SELECT id FROM runs").fetchone()[0]


def _closed(env, monkeypatch) -> str:
    _merged(env, monkeypatch)
    rid = _run_id(env)
    code, out = env.office("close")
    assert code == 0, out
    return rid


def _leftover(env, rid: str, name: str, *, branch: bool = True) -> Path:
    """A run-owned worktree at origin/main that a crash or old version left behind."""
    wt = env.state / "worktrees" / rid[:8] / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    if branch:
        env.git("worktree", "add", "-q", "-b", f"office/{rid[:8]}/{name}", str(wt), "origin/main")
    else:
        env.git("worktree", "add", "-q", "--detach", str(wt), "origin/main")
    return wt


def _commit(env, wt: Path, name: str, text: str, msg: str) -> str:
    (wt / name).write_text(text)
    env.git("-C", str(wt), "add", name)
    env.git("-C", str(wt), "commit", "-qm", msg)
    return env.git("-C", str(wt), "rev-parse", "HEAD").strip()


def _squash_onto_main(env, wt: Path, title: str):
    """Land wt's work on origin/main as one squash commit with a different title."""
    env.git("fetch", "-q", "origin")
    diff = env.git("-C", str(wt), "diff", "--binary", "origin/main", "HEAD")
    tmp = env.tmp / "squash"
    env.git("worktree", "add", "-q", "--detach", str(tmp), "origin/main")
    (env.tmp / "squash.diff").write_text(diff)
    env.git("-C", str(tmp), "apply", "--index", str(env.tmp / "squash.diff"))
    env.git("-C", str(tmp), "commit", "-qm", title)
    env.git("-C", str(tmp), "push", "-q", "origin", "HEAD:refs/heads/main")
    env.git("worktree", "remove", "--force", str(tmp))
    env.git("fetch", "-q", "origin")


def _no_panes(monkeypatch, cwds=frozenset()):
    monkeypatch.setattr(cleanup, "_live_pane_cwds", lambda: set(cwds))


def test_squash_merged_branch_worktree_is_removed_with_patch_and_branch_deleted(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    wt = _leftover(env, rid, "T7")
    _commit(env, wt, "feature-a.txt", "a\n", "T7: first step")
    _commit(env, wt, "feature-b.txt", "b\n", "T7: second step")
    _squash_onto_main(env, wt, "Squashed feature (#77)")
    code, out = env.office("gc", "--dry-run")
    assert code == 0 and "would remove" in out and str(wt) in out and wt.exists(), out
    code, out = env.office("gc")
    assert code == 0 and not wt.exists(), out
    assert "removed" in out and "GB freed" in out, out
    assert not env.git("branch", "--list", f"office/{rid[:8]}/T7").strip(), out
    assert env.git("ls-remote", "--heads", "origin", "main").strip(), "remote branches are never deleted"
    patches = list((env.state / "worktree-patches" / rid[:8]).glob("T7-*.patch"))
    assert len(patches) == 1 and "T7: first step" in patches[0].read_text(), patches
    branch_patches = list((env.state / "worktree-patches" / rid[:8]).glob(f"office_{rid[:8]}_T7-*.patch"))
    assert branch_patches, "a branch deleted on a content proof keeps a .patch"


def test_unmerged_commits_are_kept_and_a_patch_is_saved(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    wt = _leftover(env, rid, "T8")
    head = _commit(env, wt, "private.txt", "unmerged work\n", "T8: work nobody merged")
    code, out = env.office("gc")
    assert code == 0 and wt.is_dir() and (wt / "private.txt").exists(), out
    assert "unmerged commits (patch saved:" in out, out
    patch = env.state / "worktree-patches" / rid[:8] / f"T8-{head[:12]}.patch"
    assert patch.is_file() and "T8: work nobody merged" in patch.read_text(), out
    assert env.git("branch", "--list", f"office/{rid[:8]}/T8").strip(), "an unmerged branch is kept"


def test_live_run_worktree_is_never_touched(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    code, out = env.office("start", "still working", "--planner", "inline")
    assert code == 0, out
    live = env.con().execute("SELECT id FROM runs WHERE id<>?", (rid,)).fetchone()[0]
    wt = _leftover(env, live, "T1", branch=False)  # at origin/main: merged by any proof
    code, out = env.office("gc")
    assert code == 0 and wt.is_dir() and str(wt) not in out, out
    code, out = env.office("gc", "--dry-run")
    assert code == 0 and str(wt) not in out, out


def test_dirty_tree_with_real_changes_is_kept(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    wt = _leftover(env, rid, "T9", branch=False)
    tracked = next(p for p in wt.iterdir() if p.is_file() and p.name != ".git")
    tracked.write_text("uncommitted real change\n")
    code, out = env.office("gc")
    assert code == 0 and "local tracked edits not archived" in out, out
    assert tracked.read_text() == "uncommitted real change\n"


def test_live_herdr_pane_cwd_inside_a_worktree_keeps_it(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    wt = _leftover(env, rid, "T6", branch=False)
    (wt / "sub").mkdir()
    _no_panes(monkeypatch, {wt / "sub"})
    code, out = env.office("gc")
    assert code == 0 and "live Herdr pane cwd" in out and wt.is_dir(), out
    monkeypatch.setattr(cleanup, "_live_pane_cwds", lambda: None)
    code, out = env.office("gc")
    assert code == 0 and "Herdr pane list unavailable" in out and wt.is_dir(), out
    _no_panes(monkeypatch)
    code, out = env.office("gc")
    assert code == 0 and not wt.exists(), out


def test_older_than_skips_recent_runs(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    wt = _leftover(env, rid, "T5", branch=False)
    code, out = env.office("gc", "--older-than", "30")
    assert code == 0 and wt.is_dir() and "0 finished run(s) checked" in out, out


def test_close_runs_gc_over_older_finished_runs(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    wt = _leftover(env, rid, "T4", branch=False)
    # The close-time pass revisits runs whose own sweep kept something.
    con = env.con()
    con.execute("UPDATE cleanup_runs SET status='incomplete' WHERE run_id=?", (rid,))
    con.commit()
    code, out = env.office("start", "next task", "--planner", "inline")
    assert code == 0, out
    code, out = env.office("close", "--abandon", "not needed")
    assert code == 0 and "gc:" in out and not wt.exists(), out


def test_prune_saves_a_patch_for_unmerged_work_and_keeps_dirty_trees(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    dirty = _leftover(env, rid, "T3d", branch=False)
    tracked = next(p for p in dirty.iterdir() if p.is_file() and p.name != ".git")
    tracked.write_text("uncommitted\n")
    code, out = env.office("prune", "-f", "--run", rid)
    assert "pruned 0" in out and "T3d" in out and tracked.read_text() == "uncommitted\n", out
    env.git("-C", str(dirty), "checkout", "--", tracked.name)
    wt = _leftover(env, rid, "T3")
    _commit(env, wt, "keep.txt", "keep\n", "T3: unmerged")
    code, out = env.office("prune", "-f", "--run", rid)
    assert code == 0 and "pruned 1" in out and not wt.exists(), out
    patches = list((env.state / "worktree-patches" / rid[:8]).glob("T3-*.patch"))
    assert patches and "T3: unmerged" in patches[0].read_text(), out


def test_title_match_alone_never_proves_a_merge(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    wt = _leftover(env, rid, "T2")
    env.git("-C", str(wt), "reset", "-q", "--hard", "origin/main~1")  # the title is then after the merge-base
    title = env.git("log", "-1", "--format=%s", "origin/main").strip()
    _commit(env, wt, "different.txt", "content main never had\n", title)
    code, out = env.office("gc")
    assert code == 0 and wt.is_dir() and "unmerged commits (patch saved:" in out, out


def test_dry_run_mutates_nothing_and_remote_office_branches_survive(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    wt = _leftover(env, rid, "T1b")
    _commit(env, wt, "pushed.txt", "pushed\n", "T1b: pushed work")
    env.git("-C", str(wt), "push", "-q", "origin", f"HEAD:refs/heads/office/{rid[:8]}/T1b")
    _squash_onto_main(env, wt, "Squash of pushed work")
    snapshot = lambda: (env.git("show-ref"), env.git("worktree", "list", "--porcelain"),  # noqa: E731
                        list(env.con().execute("SELECT * FROM cleanup_runs")),
                        sorted((env.state / "worktree-patches").rglob("*")) if (env.state / "worktree-patches").exists() else [])
    before = snapshot()
    code, out = env.office("gc", "--dry-run")
    assert code == 0 and "would remove" in out and snapshot() == before, out
    code, out = env.office("gc")
    assert code == 0 and not wt.exists(), out
    assert env.git("ls-remote", "--heads", "origin", f"office/{rid[:8]}/T1b").strip(), "remote branch kept"


def test_closed_run_worktree_recorded_by_a_live_run_is_kept(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    _no_panes(monkeypatch)
    wt = _leftover(env, rid, "T0", branch=False)
    code, out = env.office("start", "still working", "--planner", "inline")
    assert code == 0, out
    con = env.con()
    live = con.execute("SELECT id FROM runs WHERE id<>?", (rid,)).fetchone()[0]
    con.execute("INSERT INTO dispatches(id, run_id, task_id, role, status, worktree, started_at) "
                "VALUES('d-live', ?, 'T1', 'executor', 'running', ?, '2026-11-28')", (live, str(wt)))
    con.commit()
    code, out = env.office("gc")
    assert code == 0 and "in use by a different active run" in out and wt.is_dir(), out



# --- unit checks of the merge proofs and guards (#1384 self-review findings) ---
import json as _json
import os as _os
import subprocess as _sp


def _g(repo, *args):
    return _sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    _g(repo, "config", "user.email", "t@example.test")
    _g(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    _g(repo, "add", "a.txt")
    _g(repo, "commit", "-qm", "base")
    return repo


def _fake_bin(tmp_path, monkeypatch, name, body):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / name
    exe.write_text("#!/bin/sh\n" + body + "\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{_os.pathsep}{_os.environ['PATH']}")
    return exe


def test_merge_resolution_content_blocks_the_per_commit_proof(tmp_path):
    repo = _repo(tmp_path)
    _g(repo, "checkout", "-qb", "side")
    (repo / "s.txt").write_text("side\n")
    _g(repo, "add", "s.txt")
    _g(repo, "commit", "-qm", "side work")
    _g(repo, "checkout", "-qb", "feature", "main")
    (repo / "f.txt").write_text("feature\n")
    _g(repo, "add", "f.txt")
    _g(repo, "commit", "-qm", "feature work")
    _g(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    (repo / "resolution.txt").write_text("only in the merge commit\n")
    _g(repo, "add", "resolution.txt")
    _g(repo, "commit", "-qm", "merge side")
    head = _g(repo, "rev-parse", "HEAD")
    _g(repo, "checkout", "-q", "main")
    _g(repo, "cherry-pick", "side")
    _g(repo, "cherry-pick", _g(repo, "rev-parse", "feature^1"))
    assert cleanup._merged_by_content(repo, head, "main") is None


def test_pr_proof_requires_the_default_branch_as_base(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    head = _g(repo, "rev-parse", "HEAD")
    reply = _json.dumps([{"number": 1, "headRefOid": head, "baseRefName": "stack-parent"}])
    _fake_bin(tmp_path, monkeypatch, "gh", f"echo '{reply}'")
    assert cleanup._pr_merged(repo, "office/x/T1", head, "main") is False
    reply = _json.dumps([{"number": 1, "headRefOid": head, "baseRefName": "main"}])
    _fake_bin(tmp_path, monkeypatch, "gh", f"echo '{reply}'")
    assert cleanup._pr_merged(repo, "office/x/T1", head, "main") is True


def test_pane_without_cwd_fails_closed_and_the_pane_list_is_cached(tmp_path, monkeypatch):
    counter = tmp_path / "calls"
    panes = _json.dumps({"result": {"panes": [{"pane_id": "p1"}]}})
    _fake_bin(tmp_path, monkeypatch, "herdr", f"echo x >> {counter}; echo '{panes}'")
    monkeypatch.setattr(cleanup, "_PANES", [0.0, None])
    assert cleanup._query_pane_cwds() is None
    panes = _json.dumps({"result": {"panes": [{"pane_id": "p1", "cwd": str(tmp_path)}]}})
    _fake_bin(tmp_path, monkeypatch, "herdr", f"echo x >> {counter}; echo '{panes}'")
    counter.unlink()
    assert cleanup._live_pane_cwds() == {tmp_path.absolute()}
    assert cleanup._live_pane_cwds() == {tmp_path.absolute()}
    assert counter.read_text().count("x") == 1


def test_one_failing_run_never_stops_gc(env, monkeypatch):
    rid = _closed(env, monkeypatch)
    def boom(*_a, **_k):
        raise RuntimeError("simulated git timeout")
    monkeypatch.setattr(cleanup, "sweep", boom)
    report = cleanup.gc(env.con())
    assert any("gc error: simulated git timeout" in k["reason"] and k["run_id"] == rid for k in report["kept"])
