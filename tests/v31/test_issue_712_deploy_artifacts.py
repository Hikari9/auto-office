"""Rollback output is evidence, even when deployment or verification fails."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from office import land, paths, state
from test_land import _plan, _run


def _run_row(env):
    con = env.con()
    return state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])


def _backups(env, monkeypatch, *, verify="true"):
    (env.repo / ".gitignore").write_text("private-backups/\n")
    env.git("add", ".gitignore")
    env.git("commit", "-qm", "ignore rollback output")
    command = ("mkdir -p findings private-backups && "
               "printf rollback > findings/backup.json && printf ignored > private-backups/schema.sql")
    _run(env, monkeypatch, _plan("e2e", prod=command, verify=verify))


def _archives(run):
    root = paths.state_home() / "deploy-artifacts" / run["id"]
    return [json.loads(p.read_text()) for p in root.glob("*/manifest.json")]


def _assert_backups(run):
    archives = [r for r in _archives(run) if "findings/backup.json" in r["files"]]
    assert len(archives) == 1
    record = archives[0]
    archive = Path(record["archive"])
    assert (archive / "files/findings/backup.json").read_text() == "rollback"
    assert (archive / "files/private-backups/schema.sql").read_text() == "ignored"
    assert not (archive.stat().st_mode & 0o077)
    assert not Path(record["checkout"]).exists()
    return record


def test_deploy_outputs_survive_immediate_cleanup_close_and_prune(env, monkeypatch):
    _backups(env, monkeypatch)
    code, out = env.office("land")
    assert code == 0, out
    run = _run_row(env)
    record = _assert_backups(run)
    assert record["archive"] in out
    log = (paths.run_dir(run["id"]) / "deploys/prod-deploy.log").read_text()
    assert f"# cwd: {record['checkout']}" in log and record["archive"] in log
    code, out = env.office("close")
    assert code == 0 and record["archive"] in out, out
    receipt = json.loads((paths.run_dir(run["id"]) / "archive-receipt.json").read_text())
    assert any(r["archive"] == record["archive"] for r in receipt["deploy_artifacts"])
    env.office("prune", "-f", "--run", run["id"], check=0)
    _assert_backups(run)


def test_failed_verify_then_mark_deployed_and_close_keep_backups(env, monkeypatch):
    _backups(env, monkeypatch, verify="false")
    code, out = env.office("land")
    assert code == 4 and "verify-failed" in out, out
    run = _run_row(env)
    record = _assert_backups(run)
    assert record["archive"] in out
    env.office("land", "--mark-deployed", "--quote", "the deployed tree is live", check=0)
    env.office("close", check=0)
    _assert_backups(run)


def test_close_preserves_an_interrupted_deploy_checkout(env, monkeypatch):
    _backups(env, monkeypatch)
    env.office("land", check=0)
    run = _run_row(env)
    checkout = land.gates.detached_checkout(run, run["base_sha"], "deploy-prod-interrupted", purpose="deploy")
    (checkout / "orphan-backup.json").write_text("interrupted")
    code, out = env.office("close")
    assert code == 0, out
    saved = [r for r in _archives(run) if "orphan-backup.json" in r["files"]]
    assert len(saved) == 1 and not checkout.exists()
    assert (Path(saved[0]["archive"]) / "files/orphan-backup.json").read_text() == "interrupted"


@pytest.mark.parametrize("verify", ["true", "false"])
def test_archive_failure_keeps_checkout_and_the_primary_failure(env, monkeypatch, verify):
    from office import deploy_artifacts
    _backups(env, monkeypatch, verify=verify)
    def fail(*args, **kwargs):
        raise OSError("archive disk full")
    monkeypatch.setattr(deploy_artifacts, "preserve", fail)
    code, out = env.office("land")
    assert code == 4 and "archive disk full" in out, out
    assert ("deploy-artifacts-unpreserved" if verify == "true" else "verify-failed") in out, out
    run = _run_row(env)
    kept = list((paths.run_dir(run["id"]) / "checkouts").glob("deploy-prod-*"))
    assert len(kept) == 1 and (kept[0] / "findings/backup.json").read_text() == "rollback"
    env.office("land", "--mark-deployed", "--quote", "the deployed tree is live", check=0)
    code, out = env.office("close")
    assert code == 4 and "deploy-artifacts-unpreserved" in out, out
    assert _run_row(env)["phase"] != "closed" and kept[0].exists()


def test_archive_copies_symlinks_without_reading_their_targets(env, monkeypatch):
    _backups(env, monkeypatch)
    run = _run_row(env)
    secret = env.tmp / "outside-secret"
    secret.write_text("do not copy this")
    with land._deploy_checkout(run, run["base_sha"], "deploy-prod") as (checkout, _, _):
        (checkout / "link").symlink_to(secret)
        (checkout / "name\nwith-newline").write_text("exact name")
    records = [r for r in _archives(run) if "link" in r["files"]]
    assert len(records) == 1
    archive = Path(records[0]["archive"])
    assert (archive / "files/link").is_symlink()
    assert os.readlink(archive / "files/link") == str(secret)
    assert records[0]["files"]["link"]["kind"] == "symlink"
    assert (archive / "files/name\nwith-newline").read_text() == "exact name"


def test_git_inventory_warnings_refuse_cleanup_even_with_exit_zero(env, monkeypatch):
    import subprocess
    from office import deploy_artifacts
    run = {"id": "warning-run"}
    checkout = paths.run_dir(run["id"]) / "checkouts/deploy-prod-warn"
    checkout.mkdir(parents=True)
    original = deploy_artifacts.subprocess.run
    def partial(argv, **kwargs):
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 0, b"", b"warning: could not open directory: Permission denied\n")
        return original(argv, **kwargs)
    monkeypatch.setattr(deploy_artifacts.subprocess, "run", partial)
    with pytest.raises(OSError, match="cannot enumerate"):
        deploy_artifacts.preserve(run, checkout)
    assert checkout.exists()


def test_archive_dedup_does_not_reuse_a_copy_linked_back_to_the_checkout(env, monkeypatch):
    import shutil
    from office import deploy_artifacts
    _backups(env, monkeypatch)
    run = _run_row(env)
    with land._deploy_checkout(run, run["base_sha"], "deploy-prod") as (checkout, _, _):
        (checkout / "rollback.json").write_text("durable")
        first = deploy_artifacts.preserve(run, checkout)
        saved = Path(first["archive"]) / "files"
        shutil.rmtree(saved)
        saved.symlink_to(checkout, target_is_directory=True)
    records = [r for r in _archives(run) if "rollback.json" in r["files"]]
    independent = [r for r in records if r["archive"] != first["archive"]]
    assert len(independent) == 1 and not checkout.exists()
    restored = Path(independent[0]["archive"]) / "files/rollback.json"
    assert restored.read_text() == "durable" and not restored.parent.is_symlink()


@pytest.mark.parametrize("location", ["checkout", "archive", "run"])
def test_symlinked_storage_is_refused_without_touching_its_target(env, location):
    from office import deploy_artifacts
    run = {"id": "symlink-run"}
    outside = env.tmp / "unrelated"
    outside.mkdir(mode=0o755)
    before = outside.stat().st_mode
    checkout = paths.run_dir(run["id"]) / "checkouts/deploy-prod-link"
    if location == "checkout":
        checkout.parent.mkdir(parents=True)
        checkout.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError, match="symlinked"):
            deploy_artifacts.checkouts(run)
    else:
        base = paths.state_home() / "deploy-artifacts"
        base.mkdir()
        target = base if location == "archive" else base / run["id"]
        if location == "archive":
            base.rmdir()
        target.symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError, match="symlinked"):
            deploy_artifacts.receipt(run)
    assert outside.stat().st_mode == before and not list(outside.iterdir())


def test_log_annotation_failure_preserves_primary_verify_error(env, monkeypatch):
    _backups(env, monkeypatch, verify="false")
    original = Path.open
    def fail_annotation(self, mode="r", *args, **kwargs):
        if self.name == "prod-verify.log" and mode == "a":
            raise OSError("annotation denied")
        return original(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_annotation)
    code, out = env.office("land")
    assert code == 4 and "verify-failed" in out and "annotation denied" in out, out
    _assert_backups(_run_row(env))


def test_prune_keeps_leftover_deploy_output_if_archival_fails_then_retries(env, monkeypatch):
    from office import deploy_artifacts
    _backups(env, monkeypatch)
    env.office("land", check=0)
    env.office("close", check=0)
    run = _run_row(env)
    checkout = land.gates.detached_checkout(run, run["base_sha"], "deploy-prod-late", purpose="deploy")
    (checkout / "late-backup.json").write_text("late")
    original = deploy_artifacts.preserve
    def fail(*args, **kwargs):
        raise OSError("archive disk full")
    monkeypatch.setattr(deploy_artifacts, "preserve", fail)
    code, out = env.office("prune", "-f", "--run", run["id"])
    assert "rollback output kept" in out and "archive disk full" in out, out
    assert checkout.exists() and (checkout / "late-backup.json").read_text() == "late"
    assert _run_row(env).get("pruned_at") is None
    monkeypatch.setattr(deploy_artifacts, "preserve", original)
    env.office("prune", "-f", "--run", run["id"], check=0)
    records = [r for r in _archives(run) if "late-backup.json" in r["files"]]
    assert len(records) == 1 and not checkout.exists()
    assert (Path(records[0]["archive"]) / "files/late-backup.json").read_text() == "late"
