"""`worktree.setup` (#255): a repo-declared command run once in each new worktree.

Tests use a trivial shell command as the setup; no package manager runs."""
from __future__ import annotations

import json
from pathlib import Path

from conftest import GOOD_ADD, PLAN_ONE, approved_run
from office import briefs, worktree_setup

INSTALL = "mkdir -p node_modules && echo installed >> node_modules/marker"
PLAN_RC = PLAN_ONE.replace("blast_radius: repo\n", "blast_radius: repo\nchecks: test -f node_modules/marker\n")
EXECUTOR = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]


import pytest as _pytest  # noqa: E402

pytestmark = _pytest.mark.integration


def _config(env, setup, **extra):
    """Commit a .gitignore (so installed deps never reach a revision) and write the repo config."""
    (env.repo / ".gitignore").write_text("node_modules/\n.office/\n")
    env.git("add", ".gitignore")
    env.git("commit", "-qm", "ignore deps")
    worktree = {"setup": setup, **extra}
    lines = ["worktree:"] + [f"  {k}: {json.dumps(v)}" for k, v in worktree.items()]
    (env.repo / ".auto-office").mkdir(exist_ok=True)
    (env.repo / ".auto-office" / "config.yaml").write_text("\n".join(lines) + "\n")


def _events(env, kind):
    con = env.con()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,)).fetchall()]
    finally:
        con.close()


def _dispatch_dir(env):
    con = env.con()
    try:
        run_id = con.execute("SELECT id FROM runs").fetchone()[0]
        did = con.execute("SELECT id FROM dispatches WHERE kind='executor' LIMIT 1").fetchone()[0]
    finally:
        con.close()
    return next(env.state.glob(f"**/{run_id}/dispatches/{did}"))


def _run_dict(setup=None, **kw):
    return {"policy": {"worktree": ({"setup": setup, **kw} if setup is not None else kw)}}


# ------------------------------------------------------------------ settings and decisions


def test_settings_default_to_no_setup_all_three_kinds():
    cfg = worktree_setup.settings({"policy": {}})
    assert cfg == {"setup": "", "timeout": 600, "applies_to": ["task", "integration", "check"], "inputs": []}


def test_applies_to_limits_kinds_and_unset_runs_nothing(tmp_path):
    run = _run_dict("true", applies_to=["task"])
    assert worktree_setup.should_run(run, tmp_path, "task", created=True)
    assert not worktree_setup.should_run(run, tmp_path, "integration", created=True)
    assert not worktree_setup.should_run(_run_dict(), tmp_path, "task", created=True)


def test_reuse_reruns_only_when_the_lockfile_changed(tmp_path):
    run = _run_dict("true")
    (tmp_path / "pnpm-lock.yaml").write_text("a\n")
    res = worktree_setup.execute(run, tmp_path, tmp_path / "setup.log", marker=True)
    assert res["exit"] == 0
    assert not worktree_setup.should_run(run, tmp_path, "task", created=False)
    (tmp_path / "pnpm-lock.yaml").write_text("b\n")
    assert worktree_setup.should_run(run, tmp_path, "task", created=False)
    assert worktree_setup.should_run(run, tmp_path, "task", created=True)


def test_timeout_kills_the_command_and_reports_124(tmp_path):
    run = _run_dict("echo started; sleep 30", setup_timeout_s=1)
    res = worktree_setup.execute(run, tmp_path, tmp_path / "setup.log")
    assert res["exit"] == 124 and res["timed_out"] and res["seconds"] < 10
    assert "timed out" in worktree_setup.failure_text(res)


def test_executor_brief_forbids_sharing_node_modules_and_names_a_failed_log():
    packet = {"task_id": "T1", "title": "t", "scope": ["a.py"], "plan_version": 1, "requirements_version": 1}
    failed = {"command": "pnpm install", "exit": 1, "log": "/x/setup.log"}
    text = briefs.executor_brief(None, {"id": "r"}, packet, setup=failed)
    assert "never symlink or copy node_modules" in text
    assert "SETUP FAILED" in text and "/x/setup.log" in text


# ------------------------------------------------------------------ end to end


def test_new_task_worktree_is_installed_before_the_agent_and_recorded(env):
    _config(env, INSTALL)
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", check=0)
    con = env.con()
    wt = Path(con.execute("SELECT worktree FROM dispatches WHERE kind='executor'").fetchone()[0])
    assert (wt / "node_modules" / "marker").read_text() == "installed\n"  # ran once, not per resume
    ddir = _dispatch_dir(env)
    assert (ddir / "setup.log").exists()
    rec = json.loads((ddir / "setup.json").read_text())
    assert rec["exit"] == 0 and rec["seconds"] >= 0 and rec["command"] == INSTALL
    assert "SETUP Office ran" in (ddir / "brief.md").read_text()
    assert "never symlink or copy node_modules" in (ddir / "brief.md").read_text()
    done = _events(env, "setup.done")
    assert done and json.loads(done[0]["payload_json"])["exit"] == 0


def test_failing_setup_notifies_emits_setup_failed_and_still_launches(env):
    _config(env, "echo boom-line; exit 3")
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, shown = env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    ddir = _dispatch_dir(env)
    assert "boom-line" in (ddir / "setup.log").read_text()
    assert json.loads((ddir / "setup.json").read_text())["exit"] == 3
    failed = _events(env, "setup.failed")
    assert failed and failed[0]["audience"] == "orchestrator" and "exit 3" in failed[0]["summary"]
    notices = [e for e in _events(env, "launch") if "worktree setup failed" in e["summary"]]
    assert notices and "boom-line" in notices[0]["summary"] and "setup.log" in notices[0]["summary"]
    brief = (ddir / "brief.md").read_text()
    assert "SETUP FAILED" in brief and str(ddir / "setup.log") in brief
    # The orchestrator-audience event is what office dispatch / wait / status print as news.
    assert "worktree setup failed" in shown, shown


def test_no_setup_configured_changes_nothing(env):
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    ddir = _dispatch_dir(env)
    assert not (ddir / "setup.log").exists()
    assert not _events(env, "setup.done") and not _events(env, "setup.failed")


def test_integration_runs_setup_so_run_level_checks_need_no_install_prefix(env):
    _config(env, INSTALL)
    approved_run(env, plan=PLAN_RC, executor=EXECUTOR, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    landing = json.loads(con.execute("SELECT landing_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])
    assert landing["run_checks"] == ["test -f node_modules/marker"]
    assert landing["integration"]["status"] == "accepted", landing
    kinds = [json.loads(e["payload_json"])["kind"] for e in _events(env, "setup.done")]
    assert "integration" in kinds and "task" in kinds


def test_doctor_suggests_setup_when_pnpm_lock_exists_and_none_is_set(env):
    (env.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    code, out = env.office("doctor")
    assert "worktree.setup is not set" in out and "pnpm install --offline --frozen-lockfile" in out, out
    _config(env, "true")
    code, out = env.office("doctor")
    assert "worktree.setup is not set" not in out, out


def test_missing_or_failed_marker_runs_setup_on_reuse_and_only_success_marks_done(tmp_path):
    run = _run_dict("test -f ok-flag")
    # Office stopped after creating the worktree and before setup finished: no marker.
    assert worktree_setup.should_run(run, tmp_path, "task", created=False)
    failed = worktree_setup.execute(run, tmp_path, tmp_path / "setup.log", marker=True)
    assert failed["exit"] != 0 and not (tmp_path / worktree_setup.MARKER).exists()
    assert worktree_setup.should_run(run, tmp_path, "task", created=False)  # a failure retries next launch
    (tmp_path / "ok-flag").write_text("")
    assert worktree_setup.execute(run, tmp_path, tmp_path / "setup.log", marker=True)["exit"] == 0
    assert not worktree_setup.should_run(run, tmp_path, "task", created=False)  # completed: no rerun


def test_only_check_checkouts_run_setup(env):
    from office import gates, state
    _config(env, "touch setup-ran")
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": "VERDICT: PASS"}])
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    head = env.git("rev-parse", "HEAD").strip()
    seen = {}
    for purpose in ("review", "deploy", "rebase-trial", "check"):
        path = gates.detached_checkout(run, head, f"t-{purpose}", purpose=purpose)
        seen[purpose] = (path / "setup-ran").exists()
        gates.remove_checkout(run, path)
    assert seen == {"review": False, "deploy": False, "rebase-trial": False, "check": True}


def _done(run, tmp_path):
    assert worktree_setup.execute(run, tmp_path, tmp_path / "setup.log", marker=True)["exit"] == 0
    return worktree_setup.should_run(run, tmp_path, "task", created=False)


def test_custom_setup_inputs_decide_staleness(tmp_path):
    run = _run_dict("true", setup_inputs=["config/deps.txt"])
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "deps.txt").write_text("a\n")
    (tmp_path / "pnpm-lock.yaml").write_text("x\n")
    assert not _done(run, tmp_path)
    (tmp_path / "pnpm-lock.yaml").write_text("y\n")  # not an input when setup_inputs is set
    assert not worktree_setup.should_run(run, tmp_path, "task", created=False)
    (tmp_path / "config" / "deps.txt").write_text("b\n")
    assert worktree_setup.should_run(run, tmp_path, "task", created=False)


def test_nested_and_extra_lockfiles_count_but_node_modules_does_not(tmp_path):
    run = _run_dict("true")
    (tmp_path / "web").mkdir()
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "web" / "pnpm-lock.yaml").write_text("a\n")
    (tmp_path / "composer.lock").write_text("a\n")
    (tmp_path / "node_modules" / "pkg" / "yarn.lock").write_text("a\n")
    assert not _done(run, tmp_path)
    (tmp_path / "node_modules" / "pkg" / "yarn.lock").write_text("b\n")
    assert not worktree_setup.should_run(run, tmp_path, "task", created=False)
    (tmp_path / "web" / "pnpm-lock.yaml").write_text("b\n")
    assert worktree_setup.should_run(run, tmp_path, "task", created=False)
    assert not _done(run, tmp_path)
    (tmp_path / "composer.lock").write_text("b\n")
    assert worktree_setup.should_run(run, tmp_path, "task", created=False)


def test_editing_the_setup_command_reruns_it(tmp_path):
    assert not _done(_run_dict("true"), tmp_path)
    assert worktree_setup.should_run(_run_dict("true && true"), tmp_path, "task", created=False)
