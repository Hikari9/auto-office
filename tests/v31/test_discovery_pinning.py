"""Run discovery, session binding, version pinning, legacy runs, rollback."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from conftest import PLAN_ONE, ROOT, SRC, start_inline


def _run_count(env):
    con = env.con()
    return con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def test_unbound_session_never_starts_or_binds(env):
    code, out = env.office("status")
    assert code == 3 and "no-active-run" in out
    hook_in = json.dumps({"session_id": "s1", "cwd": str(env.repo), "source": "startup"})
    proc = subprocess.run([sys.executable, "-m", "office", "hook", "SessionStart", "--harness", "claude", "--office-managed"],
                          input=hook_in, capture_output=True, text=True, cwd=env.repo)
    assert proc.returncode == 0 and proc.stdout == ""
    assert _run_count(env) == 0
    start_inline(env)
    con = env.con()
    con.execute("DELETE FROM session_bindings")
    for p in (env.repo / ".office" / "sessions").glob("*.json"):
        p.unlink()
    proc = subprocess.run([sys.executable, "-m", "office", "hook", "SessionStart", "--harness", "claude", "--office-managed"],
                          input=hook_in, capture_output=True, text=True, cwd=env.repo)
    assert "office resume to bind" in proc.stdout
    assert con.execute("SELECT COUNT(*) FROM session_bindings").fetchone()[0] == 0
    assert _run_count(env) == 1


def test_ambiguous_runs_never_select_latest_mtime(env):
    env.office("start", "first", "--planner", "inline", check=0)
    env.office("start", "second", "--planner", "inline", check=0)
    con = env.con()
    con.execute("DELETE FROM session_bindings")
    ids = [r[0] for r in con.execute("SELECT id FROM runs ORDER BY created_at")]
    # Make the older run look newest on disk; resolution must not care.
    os.utime(Path(con.execute("SELECT state_dir FROM runs WHERE id=?", (ids[0],)).fetchone()[0]), None)
    code, out = env.office("status", env={"HERDR_PANE_ID": ""})
    assert code == 3 and "ambiguous" in out and ids[0][:8] in out and ids[1][:8] in out
    code, out = env.office("resume", ids[0][:8], env={"OFFICE_SESSION": "sess-a", "OFFICE_HARNESS": "claude"})
    assert code == 0, out
    code, data = env.ojson("status", env={"OFFICE_SESSION": "sess-a", "OFFICE_HARNESS": "claude"})
    assert data["data"]["run_id"] == ids[0]
    code, data = env.ojson("status", "--run", ids[1][:8])
    assert data["data"]["run_id"] == ids[1]


def _register(env, ver: str):
    """Register a runtime that reports `ver` (stands in for another install)."""
    runtimes = env.data / "runtimes"
    runtimes.mkdir(parents=True, exist_ok=True)
    (runtimes / f"{ver}.json").write_text(json.dumps({
        "office_version": ver, "argv": [sys.executable, "-m", "office"],
        "env": {"PYTHONPATH": str(SRC), "OFFICE_VERSION_OVERRIDE": ver}}))


def test_old_version_run_stays_usable_after_new_default(env):
    old = "3.1.0"
    code, out = env.office("start", "old run", "--planner", "inline", env={"OFFICE_VERSION_OVERRIDE": old})
    assert code == 0, out
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    assert con.execute("SELECT office_version FROM runs").fetchone()[0] == old
    new_env = {"OFFICE_VERSION_OVERRIDE": "3.1.1"}
    code, out = env.office("status", "--run", run_id, env=new_env)
    assert code == 5 and "pinned-runtime-unavailable" in out and "3.1.0" in out, out
    _register(env, old)
    code, data = env.ojson("status", "--run", run_id, env=new_env)
    assert code == 0 and data["data"]["office_version"] == old, data
    assert con.execute("SELECT office_version FROM runs WHERE id=?", (run_id,)).fetchone()[0] == old


def test_rollback_changes_future_runs_only(env):
    start_inline(env)
    env.tmp.joinpath("user-config.yaml").write_text('runtime:\n  new_runs: "3.0"\n')
    code, out = env.office("start", "another")
    assert code == 4 and "new-runs-on-3.0" in out
    code, data = env.ojson("status")
    assert code == 0 and data["data"]["phase"] == "planning"


def _legacy_run(env, commit: str) -> tuple[str, Path]:
    run_id = "0legacy0-aaaa-bbbb-cccc-000000000001"
    sdir = env.tmp / "legacy-state" / run_id
    sdir.mkdir(parents=True)
    (sdir / "state.json").write_text(json.dumps({"run_id": run_id, "family_id": "f", "phase": "executing",
                                                  "goal": "a 3.0 run", "plugin_commit": commit, "plan_version": 1,
                                                  "packet_version": 1, "repo_root": str(env.repo)}))
    ref = env.repo / ".office" / "runs"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / f"{run_id}.ref").write_text(str(sdir) + "\n")
    return run_id, sdir


def test_legacy_run_is_listed_and_served_by_its_pinned_runtime(env):
    from office import runtime_default
    run_id, sdir = _legacy_run(env, runtime_default.LEGACY_V3_FINAL)
    code, out = env.office("list")
    assert "3.0 legacy" in out and run_id[:8] in out
    code, out = env.office("resume", run_id[:8])
    assert code == 0 and "stays on its pinned runtime" in out, out
    retained = env.data / "runtimes" / f"legacy-{runtime_default.LEGACY_V3_FINAL[:12]}"
    assert (retained / "scripts" / "office_runtime.py").is_file()
    code, out = env.office("raw", "check-spoke", "--state-dir", str(sdir), "--spoke", "auto-routing")
    assert "deprecated" in out
    con = env.con()
    assert con.execute("SELECT outcome FROM compat_calls ORDER BY at DESC").fetchone()[0] == "forwarded-legacy"


def test_missing_pinned_legacy_runtime_is_an_actionable_blocker(env):
    run_id, sdir = _legacy_run(env, "0123456789abcdef0123456789abcdef01234567")
    code, out = env.office("resume", run_id[:8])
    assert "not available on this machine" in out and "next:" in out
    code, out = env.office("raw", "state-load", "--state-dir", str(sdir))
    assert code == 5 and "blocked" in out
