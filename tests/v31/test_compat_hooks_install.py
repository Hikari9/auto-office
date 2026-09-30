"""Compatibility window, hooks, install/doctor, and the dedicated planner flow."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from conftest import GOOD_ADD, PLAN_ONE, ROOT, start_inline


def _db_fingerprint(env):
    con = env.con()
    return [tuple(tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall())
            for t in ("runs", "tasks", "plans", "requirements", "outbox")]


def test_raw_on_a_31_run_warns_records_and_writes_nothing(env):
    start_inline(env)
    con = env.con()
    run = dict(con.execute("SELECT id, state_dir FROM runs").fetchone())
    before = _db_fingerprint(env)
    code, out = env.office("raw", "increment-plan", "--state-dir", run["state_dir"])
    assert code == 4 and "deprecated" in out and "office amend plan" in out, out
    code, out = env.office("raw", "state-load", "--state-dir", run["state_dir"])
    assert code == 0 and '"_authority": "runs.db"' in out
    assert _db_fingerprint(env) == before
    outcomes = [r[0] for r in con.execute("SELECT outcome FROM compat_calls ORDER BY at")]
    assert outcomes == ["refused-3.1-run", "answered-from-canonical"], outcomes


def test_legacy_helper_cannot_write_a_31_run(env):
    start_inline(env)
    con = env.con()
    run = dict(con.execute("SELECT id, state_dir FROM runs").fetchone())
    before = _db_fingerprint(env)
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "office_runtime.py"), "increment-plan", "--state-dir",
                           run["state_dir"]], capture_output=True, text=True, env={**os.environ})
    # A 3.1 run dir holds only a read-only view; the helper refuses either way.
    assert proc.returncode != 0, proc.stdout
    assert _db_fingerprint(env) == before
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "office_runtime.py"), "start", "--goal", "x", "--playbook",
                           "Change"], capture_output=True, text=True, cwd=env.repo, env={**os.environ})
    assert proc.returncode == 5 and "new_runs_use_office" in proc.stdout
    assert con.execute("SELECT outcome FROM compat_calls ORDER BY at DESC").fetchone()[0] == "refused-new-run"


def test_hook_fast_path_is_quiet_and_fast_when_no_runs(env):
    payload = json.dumps({"session_id": "s", "cwd": str(env.repo), "source": "startup"})
    t = time.time()
    proc = subprocess.run([sys.executable, "-m", "office", "hook", "UserPromptSubmit", "--harness", "claude", "--office-managed"],
                          input=payload, capture_output=True, text=True, cwd=env.repo)
    elapsed = time.time() - t
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert elapsed < 1.5, elapsed  # includes interpreter start; the shim itself only stats files


def test_bound_session_gets_capsule_and_events(env):
    start_inline(env, extra=("--harness", "claude", "--session", "abc"))
    payload = json.dumps({"session_id": "abc", "cwd": str(env.repo), "source": "resume"})
    proc = subprocess.run([sys.executable, "-m", "office", "hook", "SessionStart", "--harness", "claude", "--office-managed"],
                          input=payload, capture_output=True, text=True, cwd=env.repo)
    assert proc.returncode == 0 and "next:" in proc.stdout and "office status --verbose" in proc.stdout, proc.stdout
    assert len(proc.stdout.encode()) <= 1100 and len(proc.stdout.splitlines()) <= 12


def test_worker_write_outside_worktree_is_denied_on_claude(env):
    env.trust()
    start_inline(env)
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    con = env.con()
    d = dict(con.execute("SELECT id, run_id, worktree FROM dispatches WHERE role='executor'").fetchone())
    wenv = {**os.environ, "OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"]}
    outside = json.dumps({"session_id": "w", "cwd": d["worktree"], "tool_name": "Write",
                          "tool_input": {"file_path": str(env.repo / "calc.py")}})
    proc = subprocess.run([sys.executable, "-m", "office", "hook", "PreToolUse", "--harness", "claude", "--office-managed"],
                          input=outside, capture_output=True, text=True, cwd=d["worktree"], env=wenv)
    assert proc.returncode == 2 and "only write inside its worktree" in proc.stderr
    inside = json.dumps({"session_id": "w", "cwd": d["worktree"], "tool_name": "Write",
                         "tool_input": {"file_path": str(Path(d["worktree"]) / "calc.py")}})
    proc = subprocess.run([sys.executable, "-m", "office", "hook", "PreToolUse", "--harness", "claude", "--office-managed"],
                          input=inside, capture_output=True, text=True, cwd=d["worktree"], env=wenv)
    assert proc.returncode == 0


def test_install_is_idempotent_backs_up_and_uninstall_removes_only_managed(env, monkeypatch):
    home = env.home
    import site
    if site.ENABLE_USER_SITE and hasattr(site, "USER_BASE") and Path(site.USER_BASE).exists():
        monkeypatch.setenv("PYTHONUSERBASE", site.USER_BASE)
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    user_hook = {"hooks": [{"type": "command", "command": "echo mine"}]}
    settings.write_text(json.dumps({"hooks": {"SessionStart": [user_hook]}, "theme": "dark"}))
    code, out = env.office("install", "--only", "claude")
    assert code == 0 and "hooks installed" in out, out
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark" and user_hook in data["hooks"]["SessionStart"]
    managed = [e for e in data["hooks"]["SessionStart"] if "--office-managed" in json.dumps(e)]
    assert len(managed) == 1
    assert list((home / ".claude").glob("settings.json.bak.*"))
    code, out = env.office("install", "--only", "claude")
    assert "already current" in out
    assert json.loads(settings.read_text()) == data
    code, out = env.office("doctor")
    assert "claude: 3 office hook(s)" in out, out
    code, out = env.office("uninstall")
    data = json.loads(settings.read_text())
    assert data["hooks"]["SessionStart"] == [user_hook]


def test_dedicated_planner_flow_needs_no_orchestrator_plumbing(env):
    env.trust()
    env.script(planner=[{"plan": PLAN_ONE, "submit": True}], plan_reviewer=[{"reply": "VERDICT: PASS"}],
               executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("start", "add numbers", "--gear", "full")
    assert code == 0 and "planner P1 queued" in out, out
    code, data = env.ojson("status")
    assert data["data"]["plan_version"] == 1 and data["data"]["plan_review"]["ended"] is True, data
    assert "authorization" in data["next"]
    env.office("approve", "plan", "--quote", "looks right", check=0)
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    # The orchestrator issued: start, status, approve, dispatch, status. No JSON, no receipts.
    roles = [c["role"] for c in env.calls()]
    assert roles == ["planner", "plan_reviewer", "executor", "code_reviewer"], roles
