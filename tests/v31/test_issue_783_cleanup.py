"""#783: explicit preservation/retry/legacy cleanup, verified in real git worktrees."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from office import state, gates
from test_closeout import _merged


def _run_id(env):
    return env.con().execute("SELECT id FROM runs").fetchone()[0]


def _task(env, run_id: str, task="T1") -> Path:
    return env.state / "worktrees" / run_id[:8] / task


def _prove_integration_online(env, run_id: str):
    """A detached integration commit must be backed by a LIVE remote branch."""
    env.git("-C", str(_task(env, run_id, "_integration")), "push", "origin",
            f"HEAD:refs/heads/office-integration-proof-{run_id[:8]}")


def test_crash_after_terminal_transition_is_recovered_by_repeating_close(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    con = env.con()
    run = state.get_run(con, rid)
    landing = gates.landing_state(con, run, None)
    # A merged integration tree alone does not prove a detached commit's
    # identity is recoverable. Prove that commit exists on the live remote.
    _prove_integration_online(env, rid)
    # Mimic a crash immediately after the terminal transaction committed.
    con.execute("UPDATE runs SET phase='closed', terminal_at='2026-10-24', "
                "archive_digest='sha256:recovery', landing_json=? WHERE id=?",
                (json.dumps(landing), rid))
    con.commit()
    assert _task(env, rid).exists()
    code, out = env.office("close", "--run", rid)
    assert code == 0 and "retrying closeout" in out and "worktree(s) removed" in out, out
    assert not _task(env, rid).exists(), out
    assert con.execute("SELECT status FROM cleanup_runs WHERE run_id=?", (rid,)).fetchone()[0] == "complete"
    code, out = env.office("close", "--run", rid)
    assert code == 0 and "0 worktree(s) removed" in out, out
    assert not _task(env, rid).exists()


def test_local_tracked_changes_are_retained_then_clean_close_retries(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    wt = _task(env, rid)
    (wt / "calc.py").write_text("local-only change\n")
    code, out = env.office("close")
    assert code == 0 and "local tracked edits not archived" in out, out
    assert wt.is_dir() and (wt / "calc.py").read_text() == "local-only change\n"
    assert env.con().execute("SELECT status FROM cleanup_runs WHERE run_id=?", (rid,)).fetchone()[0] == "incomplete"
    env.git("-C", str(wt), "restore", "calc.py")
    code, out = env.office("close", "--run", rid)
    assert code == 0 and "worktree(s) removed" in out and not wt.exists(), out


def test_untracked_environment_file_gets_verifiable_durable_archive(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    wt = _task(env, rid)
    # Ignore file in the shared worktree registry to model private config.
    (env.repo / ".git" / "info" / "exclude").write_text(".env\n")
    (wt / ".env").write_text("env-data-that-must-survive\n")
    code, out = env.office("close")
    assert code == 0 and not wt.exists(), out
    paths = list((env.state / "deploy-artifacts" / rid).glob("T1-*/files/.env"))
    assert len(paths) == 1, paths
    assert paths[0].read_text() == "env-data-that-must-survive\n"
    record = json.loads((paths[0].parent.parent / "manifest.json").read_text())
    assert record["files"][".env"]["sha256"].startswith("sha256:")
    assert record["checkout"] == str(wt)


def test_locked_worktree_retained_and_retry_removes_after_unlock(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    wt = _task(env, rid)
    env.git("worktree", "lock", "--reason", "live recovery", str(wt))
    code, out = env.office("close")
    assert code == 0 and "Git worktree is locked" in out and wt.exists(), out
    env.git("worktree", "unlock", str(wt))
    code, out = env.office("close", "--run", rid)
    assert code == 0 and "worktree(s) removed" in out and not wt.exists(), out


def test_older_landed_run_offers_preview_opt_in_and_repeat_is_idempotent(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    _prove_integration_online(env, rid)
    code, out = env.office("close")
    assert code == 0, out
    root = env.state / "worktrees" / rid[:8]
    root.mkdir(parents=True, exist_ok=True)
    leftover = root / "legacy-check"
    env.git("worktree", "add", "-q", "-b", f"office/{rid[:8]}/legacy-check", str(leftover), "origin/main")
    con = env.con()
    con.execute("UPDATE runs SET office_version='3.4.0' WHERE id=?", (rid,))
    con.commit()
    code, out = env.office("cleanup", "--older")
    assert code == 0 and rid[:8] in out and "candidate" in out and leftover.exists(), out
    code, out = env.office("cleanup", "--run", rid)
    assert code == 0 and "preview" in out and leftover.exists(), out
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and "removed 1" in out and not leftover.exists(), out
    code, out = env.office("cleanup", "--older")
    assert code == 0 and "older cleanup candidates: 0" in out, out
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and "removed 0" in out, out


def test_older_abandoned_run_requires_online_commit_and_opt_in(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    code, out = env.office("close", "--abandon", "operator moved to another approach")
    assert code == 0 and _task(env, rid).exists(), out
    code, out = env.office("cleanup", "--older")
    assert code == 0 and rid[:8] in out and _task(env, rid).exists(), out
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and "removed" in out and not _task(env, rid).exists(), out


def test_active_run_never_removed_even_with_apply(env):
    code, out = env.office("start", "still working", "--planner", "inline")
    assert code == 0, out
    rid = _run_id(env)
    con = env.con()
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and "resumable" in out, out
    assert state.get_run(con, rid)["phase"] == "planning"
    assert con.execute("SELECT status FROM cleanup_runs WHERE run_id=?", (rid,)).fetchone()[0] == "deferred"


def test_refuses_unpushed_local_commit_even_when_run_is_landed(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    wt = _task(env, rid)
    (wt / "private-change.txt").write_text("commit not on origin\n")
    env.git("-C", str(wt), "add", "private-change.txt")
    env.git("-C", str(wt), "commit", "-qm", "local-only work")
    code, out = env.office("close")
    assert code == 0 and "not proven recoverable from current online origin" in out, out
    assert wt.is_dir() and (wt / "private-change.txt").exists(), out


def test_live_herdr_pane_attached_after_preview_blocks_apply(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    wt = _task(env, rid)
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE run_id=? AND worktree=?",
                       (rid, str(wt))).fetchone()[0]
    run = state.get_run(con, rid)
    landing = gates.landing_state(con, run, None)
    con.execute("UPDATE runs SET phase='closed', terminal_at='2026-10-24', "
                "archive_digest='sha256:recovery', landing_json=? WHERE id=?",
                (json.dumps(landing), rid))
    con.commit()
    code, out = env.office("cleanup", "--run", rid)
    assert code == 0 and "candidate" in out and wt.exists(), out
    # The recorded pane attaches after the preview. This host may have a real
    # herdr that answers pane_not_found for the synthetic pane id, so force the
    # cannot-prove-detachment path; a genuinely attached pane is exercised by
    # the macOS/Herdr smoke fixture.
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "herdr" else real_which(name))
    con.execute("UPDATE dispatches SET pane_id='newly-attached', launcher='herdr' "
                "WHERE run_id=? AND worktree=?", (rid, str(wt)))
    con.commit()
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and "Herdr cannot be queried" in out and wt.exists(), out


def test_operator_decline_suppresses_future_offers_without_deleting(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    code, out = env.office("close", "--abandon", "stopped intentionally")
    assert code == 0, out
    wt = _task(env, rid)
    code, out = env.office("cleanup", "--older")
    assert code == 0 and rid[:8] in out
    code, out = env.office("cleanup", "--run", rid, "--decline")
    assert code == 0 and "declined" in out and wt.exists(), out
    code, out = env.office("cleanup", "--older")
    assert code == 0 and "older cleanup candidates: 0" in out and wt.exists(), out
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and not wt.exists(), out


def test_unavailable_online_origin_does_not_authorize_deletion(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    wt = _task(env, rid)
    env.git("remote", "set-url", "origin", "file:///nonexistent/office-git")
    code, out = env.office("close")
    assert code == 0 and "not proven recoverable from current online origin" in out, out
    assert wt.exists(), out


def test_older_preview_does_not_mutate_local_git_refs_or_cleanup_receipt(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    code, out = env.office("close", "--abandon", "stop and keep evidence")
    assert code == 0, out
    before_refs = env.git("show-ref")
    before_worktrees = env.git("worktree", "list", "--porcelain")
    before_db = list(env.con().execute("SELECT * FROM cleanup_runs"))
    code, out = env.office("cleanup", "--older")
    assert code == 0 and rid[:8] in out, out
    assert env.git("show-ref") == before_refs
    assert env.git("worktree", "list", "--porcelain") == before_worktrees
    assert list(env.con().execute("SELECT * FROM cleanup_runs")) == before_db


def test_previously_closed_handoff_can_later_verify_merge_and_cleanup(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    from test_close_external import _gh
    # Simulate the caller choosing a handoff even though the PR has already landed.
    code, out = env.office("close", "--handoff", "https://github.com/o/r/pull/7")
    assert code == 0 and "handed off" in out and _task(env, rid).exists(), out
    # A plain repeated close keeps the handoff. It cannot infer permission to delete.
    code, out = env.office("close", "--run", rid)
    assert code == 0 and _task(env, rid).exists(), out
    commit = env.git("rev-parse", "origin/main").strip()
    _gh(env, "MERGED", commit)
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/7", "--run", rid)
    assert code == 0 and "external landing verified" in out and not _task(env, rid).exists(), out


def test_crash_after_landed_externally_terminal_replays_as_sweep(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    _prove_integration_online(env, rid)
    con = env.con()
    # The exact landing shape _close_landed_externally stores: handoff keeps
    # the PR URL, and neither target nor detail exists, so the replay must
    # prove the merge through the stored pr, not an inherited detail key.
    con.execute("UPDATE runs SET phase='closed', terminal_at='2026-10-24', "
                "terminal_reason='landed externally', archive_digest='sha256:recovery', landing_json=? WHERE id=?",
                (json.dumps({"status": "landed-externally", "pr": "https://github.com/o/r/pull/7",
                             "merge_commit": env.git("rev-parse", "origin/main").strip(),
                             "contains": [], "unproven": [], "quote": None,
                             "handoff": "https://github.com/o/r/pull/7"}), rid))
    con.commit()
    code, out = env.office("close", "--run", rid)
    assert code == 0 and "worktree(s) removed" in out and not _task(env, rid).exists(), out
    assert "handed off" not in out, out
    assert "removal skipped" not in out, out


def test_identical_remote_tree_does_not_erase_unique_local_commit_history(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    wt = _task(env, rid)
    # The new commit has exactly the same tree as origin, but its commit hash
    # and message are local evidence not present in the remote history.
    env.git("-C", str(wt), "commit", "-qm", "local-only empty commit", "--allow-empty")
    new_commit = env.git("-C", str(wt), "rev-parse", "HEAD").strip()
    code, out = env.office("close")
    assert code == 0 and "not proven recoverable from current online origin" in out, out
    assert wt.exists(), out
    assert env.git("cat-file", "-t", new_commit).strip() == "commit"


def test_crash_leaked_convergence_checkout_is_discovered_and_removed(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    checkout = env.state / "runs" / rid / "checkouts" / "converge-Gfixture01"
    env.git("worktree", "add", "-q", "--detach", str(checkout), "origin/main")
    code, out = env.office("close")
    assert code == 0 and not checkout.exists(), out
    assert checkout.name not in env.git("worktree", "list", "--porcelain"), out


def test_leaked_checkout_at_unproven_commit_is_retained_with_reason(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    checkout = env.state / "runs" / rid / "checkouts" / "review-Rfixture02"
    base = env.git("rev-parse", "origin/main").strip()
    env.git("worktree", "add", "-q", "--detach", str(checkout), base)
    (checkout / "evidence.txt").write_text("review evidence\n")
    env.git("-C", str(checkout), "add", "evidence.txt")
    env.git("-C", str(checkout), "commit", "-qm", "unpushed review notes")
    code, out = env.office("close")
    assert code == 0 and "not proven recoverable from current online origin" in out, out
    assert checkout.is_dir(), out


def test_symlink_primary_checkout_runtime_and_unowned_paths_are_never_deleted(env, monkeypatch):
    _merged(env, monkeypatch)
    rid = _run_id(env)
    _prove_integration_online(env, rid)
    con = env.con()
    run = state.get_run(con, rid)
    landing = gates.landing_state(con, run, None)
    con.execute("UPDATE runs SET phase='closed', terminal_at='2026-10-24', "
                "archive_digest='sha256:recovery', landing_json=? WHERE id=?", (json.dumps(landing), rid))
    con.commit()
    root = env.state / "worktrees" / rid[:8]
    # A linked name that resolves into a real worktree must not be followed.
    real = env.tmp / "linked-target"
    env.git("worktree", "add", "-q", "--detach", str(real), "origin/main")
    (root / "T9-link").symlink_to(real, target_is_directory=True)
    runtime = env.data / "runtimes" / "3.4.1"
    runtime.mkdir(parents=True)
    unowned = env.tmp / "never-ours"
    unowned.mkdir()
    con.execute("INSERT INTO dispatches(id, run_id, task_id, role, status, worktree, started_at) VALUES "
                "('d-primary', ?, 'T1', 'executor', 'accepted', ?, '2026-10-24'),"
                "('d-runtime', ?, 'T1', 'executor', 'accepted', ?, '2026-10-24'),"
                "('d-unowned', ?, 'T1', 'executor', 'accepted', ?, '2026-10-24')",
                (rid, str(env.repo), rid, str(runtime), rid, str(unowned)))
    con.commit()
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0, out
    assert "primary checkout or symlink" in out and env.repo.is_dir(), out
    assert (root / "T9-link").is_symlink() and real.is_dir(), out
    assert "installed/pinned runtime" in out and runtime.is_dir(), out
    assert "not registered as a Git worktree" in out and unowned.is_dir(), out
