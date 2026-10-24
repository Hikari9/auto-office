"""#783 local acceptance: the real macOS herdr session against disposable fixtures.

Opt-in only (OFFICE_HERDR_SMOKE=1): these tests split a real pane in the
operator's live herdr session and close it again. All Office state is the env
fixture's isolated scratch home; nothing here may touch the real
~/.local/state/auto-office. The standard gate never selects this module.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from office import state, gates
from test_closeout import _merged
from test_issue_783_cleanup import _prove_integration_online, _run_id, _task

pytestmark = pytest.mark.skipif(os.environ.get("OFFICE_HERDR_SMOKE") != "1",
                                reason="real-herdr smoke; opt in with OFFICE_HERDR_SMOKE=1")


def _herdr(*argv: str, timeout: int = 20) -> tuple[dict, str]:
    proc = subprocess.run(["herdr", *argv], capture_output=True, text=True, timeout=timeout)
    text = proc.stdout
    start = text.find("{")
    payload = json.loads(text[start:]) if start >= 0 else {}
    return payload, proc.stdout + proc.stderr


def _split_pane(cwd: str) -> str:
    listing, _ = _herdr("pane", "list")
    origin = listing["result"]["panes"][0]["pane_id"]
    out, raw = _herdr("pane", "split", origin, "--direction", "right", "--cwd", cwd, "--no-focus")
    return out["result"]["pane"]["pane_id"]


def _closed_run(env, monkeypatch) -> str:
    """A landed run whose terminal close committed but whose cleanup has not run."""
    _merged(env, monkeypatch)
    rid = _run_id(env)
    _prove_integration_online(env, rid)
    con = env.con()
    run = state.get_run(con, rid)
    landing = gates.landing_state(con, run, None)
    con.execute("UPDATE runs SET phase='closed', terminal_at='2026-10-24', "
                "archive_digest='sha256:smoke', landing_json=? WHERE id=?", (json.dumps(landing), rid))
    con.commit()
    # A real crash after the terminal write still leaves close's base sync done.
    env.git("fetch", "-q", "origin")
    if env.git("symbolic-ref", "--short", "HEAD").strip() == "main":
        env.git("merge", "--ff-only", "origin/main")
    else:
        env.git("branch", "-f", "main", "origin/main")
    return rid


def _receipt(env, rid: str):
    row = env.con().execute("SELECT status, receipt_json FROM cleanup_runs WHERE run_id=?", (rid,)).fetchone()
    return row[0], json.loads(row[1])


def _office_worktrees(env) -> list[str]:
    return [l.removeprefix("worktree ") for l in env.git("worktree", "list", "--porcelain").splitlines()
            if l.startswith("worktree ") and ("/worktrees/" in l or "/checkouts/" in l)]


def test_real_pane_attached_between_preview_and_apply_blocks_deletion(env, monkeypatch):
    rid = _closed_run(env, monkeypatch)
    wt = _task(env, rid)
    code, out = env.office("cleanup", "--run", rid)
    assert code == 0 and "candidate" in out and wt.exists(), out
    pane = None
    try:
        pane = _split_pane(str(wt))
        env.con().execute("UPDATE dispatches SET pane_id=?, launcher='herdr' WHERE run_id=? AND worktree=?",
                          (pane, rid, str(wt)))
        env.con().commit()
        code, out = env.office("cleanup", "--run", rid, "--apply")
        assert code == 0 and f"Herdr pane {pane} still present or unverified" in out and wt.exists(), out
        status, report = _receipt(env, rid)
        assert status == "incomplete" and any(pane in k["reason"] for k in report["kept"]), report
    finally:
        if pane:
            _herdr("pane", "close", pane)
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and not wt.exists(), out
    assert _receipt(env, rid)[0] == "complete"


def test_partial_sweep_receipt_resumes_with_legacy_dispatch_location(env, monkeypatch):
    rid = _closed_run(env, monkeypatch)
    # A dispatch-recorded checkout under a legacy root inside the state home,
    # outside the modern worktrees/<rid8>/ layout older runs did not use.
    legacy = env.state / "dispatch-legacy" / rid / "T1"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    env.git("worktree", "add", "-q", "-b", f"office/{rid[:8]}/legacy-dispatch", str(legacy), "origin/main")
    env.con().execute("INSERT INTO dispatches(id, run_id, task_id, role, status, worktree, started_at) "
                      "VALUES('d-smoke-legacy', ?, 'T1', 'executor', 'accepted', ?, '2026-10-24')",
                      (rid, str(legacy)))
    env.con().commit()
    wt = _task(env, rid)
    env.git("worktree", "lock", "--reason", "mid-sweep crash", str(wt))
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and "Git worktree is locked" in out and wt.exists() and not legacy.exists(), out
    status, report = _receipt(env, rid)
    assert status == "incomplete" and str(legacy) in report["removed"], report
    env.git("worktree", "unlock", str(wt))
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and not wt.exists(), out
    status, report = _receipt(env, rid)
    assert status == "complete" and report["removed"], report
    assert not _office_worktrees(env), env.git("worktree", "list", "--porcelain")
    # Task and recorded-legacy branches merge into the landed base; converge and
    # integration branches may stay when git branch -d (never -D) refuses them.
    assert not env.git("branch", "--list", f"office/{rid[:8]}/T*").strip()
    assert not env.git("branch", "--list", f"office/{rid[:8]}/legacy-dispatch").strip()
    assert report["bytes"] > 0


def test_missing_worktree_directory_is_reported_then_converges_after_repair(env, monkeypatch):
    rid = _closed_run(env, monkeypatch)
    wt = _task(env, rid)
    subprocess.run(["rm", "-rf", str(wt)], check=True)
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and "prunable" in out, out
    status, report = _receipt(env, rid)
    assert status == "incomplete" and any("prunable" in k["reason"] for k in report["kept"]), report
    env.git("worktree", "prune")
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0, out
    status, report = _receipt(env, rid)
    assert status == "complete", report
    assert not _office_worktrees(env), env.git("worktree", "list", "--porcelain")


def test_deploy_untracked_evidence_is_archived_and_provably_restorable(env, monkeypatch):
    rid = _closed_run(env, monkeypatch)
    deploy = env.state / "runs" / rid / "checkouts" / "deploy-smoke"
    env.git("worktree", "add", "-q", "--detach", str(deploy), "origin/main")
    (env.repo / ".git" / "info" / "exclude").write_text(".env\n")
    (deploy / "rollback").mkdir()
    (deploy / "rollback" / "run-785.txt").write_text("rollback evidence that must survive\n")
    (deploy / ".env").write_text("smoke-secret-not-real\n")
    code, out = env.office("cleanup", "--run", rid, "--apply")
    assert code == 0 and not deploy.exists(), out
    status, report = _receipt(env, rid)
    assert status == "complete" and str(deploy) in report["removed"], report
    archived = list((env.state / "deploy-artifacts" / rid).glob("deploy-smoke-*/files"))
    assert len(archived) == 1, archived
    assert (archived[0] / "rollback" / "run-785.txt").read_text() == "rollback evidence that must survive\n"
    assert (archived[0] / ".env").read_text() == "smoke-secret-not-real\n"
    manifest = json.loads((archived[0].parent / "manifest.json").read_text())
    assert set(manifest["files"]) == {".env", "rollback/run-785.txt"}
    # Restore proof: a fresh scratch checkout of the same commit plus the
    # archive reproduces the deleted checkout's local content exactly.
    fresh = env.tmp / "restore-proof"
    env.git("worktree", "add", "-q", "--detach", str(fresh), "origin/main")
    try:
        for name in manifest["files"]:
            dest = fresh / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((archived[0] / name).read_bytes())
        assert (fresh / "rollback" / "run-785.txt").read_text() == "rollback evidence that must survive\n"
        assert (fresh / ".env").read_text() == "smoke-secret-not-real\n"
    finally:
        env.git("worktree", "remove", "--force", str(fresh))
    assert not _office_worktrees(env), env.git("worktree", "list", "--porcelain")
