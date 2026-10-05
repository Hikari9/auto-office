"""office preflight: read-only verdicts (ready|fix|wait|stop) an executor checks before
office submit, the opt-in macOS sed shell guard, and the executor brief's tail."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _worker(env, tid="T1"):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}, Path(d["worktree"]), d


def _dispatched(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt, d = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    return wenv, wt, d


def _set_task(env, **cols):
    con = env.con()
    con.execute("UPDATE tasks SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id='T1'", tuple(cols.values()))
    con.commit()


@pytest.mark.integration
@pytest.mark.approved
def test_ready_prints_the_sourced_submit_line_and_submit_follows(env):
    wenv, wt, d = _dispatched(env)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and out.startswith("PREFLIGHT ready"), out
    assert f"agent.env && office submit" in out and d["id"] in out, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0, out


@pytest.mark.integration
@pytest.mark.approved
def test_wrong_role_is_a_fix_naming_the_agent_env(env):
    wenv, wt, d = _dispatched(env)
    code, out = env.office("preflight", cwd=wt, env={**wenv, "OFFICE_ROLE": "reviewer"})
    assert code == 1 and out.startswith("PREFLIGHT fix"), out
    assert "OFFICE_ROLE=reviewer" in out and f"{d['id']}/agent.env && office submit" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_lost_lease_is_terminal_and_never_reacquired(env):
    wenv, wt, d = _dispatched(env)
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and out.startswith("PREFLIGHT stop") and "stop: lease-lost" in out, out
    assert "do not retry" in out, out
    con = env.con()
    lease = con.execute("SELECT revoked_at FROM leases WHERE id=?", (d["lease_id"],)).fetchone()
    assert lease["revoked_at"], "preflight must not reacquire a revoked lease"


@pytest.mark.integration
@pytest.mark.approved
def test_expired_lease_alone_stops_even_while_the_task_looks_active(env):
    wenv, wt, d = _dispatched(env)
    con = env.con()
    con.execute("UPDATE leases SET revoked_at='2026-01-01T00:00:00+00:00' WHERE id=?", (d["lease_id"],))
    con.commit()
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "stop: lease-lost" in out and "fix:" not in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_plan_defect_pause_waits_then_turns_ready_when_it_clears(env):
    wenv, wt, _ = _dispatched(env)
    prior = task_row(env)["status"]
    _set_task(env, status="paused", pause_reason="plan defect")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 75 and out.startswith("PREFLIGHT wait") and "plan defect" in out, out
    _set_task(env, status=prior, pause_reason=None)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and out.startswith("PREFLIGHT ready"), out


@pytest.mark.integration
@pytest.mark.approved
def test_other_pauses_and_blocks_stop(env):
    wenv, wt, _ = _dispatched(env)
    _set_task(env, status="blocked", pause_reason="worker ended (crash) without submitting")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "stop: blocked" in out, out


def _fix_round(env, d, **fields):
    from office import paths
    pkt = paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "packet.json"
    data = json.loads(pkt.read_text())
    data.update(fix_of="R1", **fields)
    pkt.write_text(json.dumps(data))


@pytest.mark.integration
@pytest.mark.approved
def test_fix_round_without_findings_waits_keeps_its_work_and_tells_the_orchestrator(env, tmp_path):
    wenv, wt, d = _dispatched(env)
    con = env.con()
    run_id = d["run_id"]
    _fix_round(env, d)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 75 and out.startswith("PREFLIGHT wait") and "no open findings" in out, out
    assert "office prompt" in out and "do not retry" not in out, out
    env.office("preflight", cwd=wt, env=wenv)
    events = con.execute("SELECT summary FROM events WHERE kind='preflight.waiting' AND dispatch_id=?",
                         (d["id"],)).fetchall()
    assert len(events) == 1, "the orchestrator is told once per dispatch and reason"
    from office import guide, state
    stalls = guide.stalls(con, state.get_run(con, run_id))
    assert any("is waiting on you, its work kept" in s and f"office prompt {d['id']}" in s for s in stalls), stalls
    con.execute("INSERT INTO findings (id, run_id, task_id, code, severity, location, summary, state, created_at) "
                "VALUES ('f1', ?, 'T1', 'F1', 'medium', 'calc.py:1', 'add() drops negatives', 'open', '2026-01-01')",
                (run_id,))
    con.commit()
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "finding: F1 [medium] calc.py:1 add() drops negatives" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_out_of_scope_edit_and_bsd_sed_backup_are_fixes(env):
    wenv, wt, d = _dispatched(env)
    (wt / "README.md").write_text("stamped by a hook\n")
    env.git("add", "-N", "README.md", cwd=wt)
    env.git("add", "calc.py", cwd=wt)
    (wt / "calc.py-e").write_text(GOOD_ADD)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and out.startswith("PREFLIGHT fix"), out
    assert "scope: tracked edits outside SCOPE: README.md" in out and "--request-scope" in out, out
    assert "sed: BSD sed wrote backup files calc.py-e" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_dispatch_env_from_another_directory_is_a_worktree_fix(env):
    wenv, wt, _ = _dispatched(env)
    code, out = env.office("preflight", cwd=env.repo, env=wenv)
    assert code == 1 and f"fix: worktree: submit refuses outside the task worktree; cd {wt.resolve()}" in out, out


@pytest.mark.integration
def test_scope_none_stale_evidence_is_a_fix(env):
    from conftest import PLAN_ONE
    approved_run(env, plan=PLAN_ONE.replace("scope: calc.py", "scope: none"),
                 executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt, d = _worker(env)
    (wt / "OFFICE_EVIDENCE.md").write_text("posted comment https://example.test/c/1: hello\n")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0, out
    from office import submit
    con = env.con()
    run = dict(con.execute("SELECT * FROM runs").fetchone())
    digest = submit._read_evidence(wt)[2]
    con.execute("INSERT INTO evidence (id, run_id, task_id, kind, sha256, created_at) VALUES ('e1', ?, 'T1', ?, ?, '2026-01-01')",
                (run["id"], submit.EVIDENCE_KIND, digest))
    con.commit()
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and "fix: evidence: OFFICE_EVIDENCE.md repeats evidence already submitted" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_preflight_outside_a_task_worktree_stops(env):
    _dispatched(env)
    code, out = env.office("preflight", cwd=env.repo, env=EXTERNAL)
    assert code == 4 and "no executor dispatch owns this directory" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_executor_brief_carries_self_review_preflight_and_status_line(env):
    _, _, d = _dispatched(env)
    from office import paths
    brief = (paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "brief.md").read_text()
    for lens in ("(a) security", "(b) edge cases", "(c) platform and build", "(d) test strength"):
        assert lens in brief, lens
    assert f"git diff {d['base_commit']}" in brief
    assert "office preflight" in brief and "lease-lost, superseded-dispatch, or task-paused is terminal" in brief
    last = brief.rstrip().splitlines()[-1].strip()
    assert last.startswith("TASK=<id> COMMIT=<sha> PUSHED=") and "SUBMIT=<accepted Rn | refused: exact reason" in last


# ------------------------------------------------------------------ shell guard

@pytest.mark.parametrize("cmd, want", [
    ("sed -i 's/a/b/' f", "sed -i '' 's/a/b/' f"),
    ("sed -i -e 's/a/b/' f", "sed -i '' -e 's/a/b/' f"),
    ("sed -E -i 's/(a)/b/' f", "sed -E -i '' 's/(a)/b/' f"),
    ("sed -Ei 's/a/b/' f", "sed -Ei '' 's/a/b/' f"),
    ("find . -name x | xargs sed -i 's/x/y/'", "find . -name x | xargs sed -i '' 's/x/y/'"),
    ("echo ok && sed -i s/a/b/ f && sed -i s/c/d/ g", "echo ok && sed -i '' s/a/b/ f && sed -i '' s/c/d/ g"),
    ("sed -i '' 's/a/b/' f", None),
    ("sed -i \"\" 's/a/b/' f", None),
    ("sed -i.bak 's/a/b/' f", None),
    ("gsed -i 's/a/b/' f", None),
    ("sed -n 1p f", None),
    ('git commit -m "replace sed -i with perl"', None),
    ("cat > fix.sh <<'EOF'\nsed -i 's/a/b/' f\nEOF", None),
    ("cat > fix.sh <<EOF\nsed -i 's/a/b/' f\nEOF", None),
    ("sed -i 's/a/b/' f && cat <<EOF\nsed -i s/x/y/ g\nEOF", "sed -i '' 's/a/b/' f && cat <<EOF\nsed -i s/x/y/ g\nEOF"),
])
def test_portable_sed(cmd, want):
    from office.hooks import portable_sed
    assert portable_sed(cmd) == want


def _guard(monkeypatch, capsys, command, platform="darwin", gnu=False):
    import io
    from office import hooks
    monkeypatch.setattr(hooks.sys, "platform", platform)
    monkeypatch.setattr(hooks, "_gnu_sed", lambda: gnu)
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command, "description": "d"}})))
    assert hooks.main(["shell.pre", "--harness", "claude", "--office-managed"]) == 0
    return capsys.readouterr().out


def test_shell_guard_rewrites_without_a_permission_decision(monkeypatch, capsys):
    out = json.loads(_guard(monkeypatch, capsys, "sed -i 's/a/b/' f"))["hookSpecificOutput"]
    assert out["updatedInput"] == {"command": "sed -i '' 's/a/b/' f", "description": "d"}
    assert "permissionDecision" not in out, "the rewrite must stay inside the normal permission flow"


@pytest.mark.parametrize("kw", [{"platform": "linux"}, {"gnu": True}])
def test_shell_guard_is_silent_off_macos_and_with_gnu_sed(monkeypatch, capsys, kw):
    assert _guard(monkeypatch, capsys, "sed -i 's/a/b/' f", **kw) == ""


@pytest.mark.integration
def test_install_shell_guard_is_opt_in_kept_and_removable(env, tmp_path, monkeypatch):
    from office import install
    cfg = tmp_path / "settings.json"
    cfg.write_text("{}")
    monkeypatch.setitem(install.CONFIG, "claude", cfg)
    monkeypatch.setitem(install.CONFIG, "gemini", tmp_path / "absent" / "settings.json")
    monkeypatch.setattr(install.read_scope, "sync", lambda data, ledger: ([], [], []))
    monkeypatch.setattr(install.read_scope, "load_ledger", lambda: [])

    def guards():
        pre = json.loads(cfg.read_text())["hooks"]["PreToolUse"]
        return [e for e in pre if install._is_managed(e, "shell.pre")], [e for e in pre if install._is_managed(e, "tool.pre")]

    install.install(only=["claude"], dry_run=True)  # no file write in dry run
    res = install.install(only=["claude"], dry_run=False)  # noqa: F841
    assert guards()[0] == [] and len(guards()[1]) == 1
    install.install(only=["claude"], shell_guard=True)
    g, w = guards()
    assert len(g) == 1 and g[0]["matcher"] == "Bash" and len(w) == 1
    res = install.install(only=["claude"])  # plain reinstall keeps it
    assert len(guards()[0]) == 1 and any("hooks already current" in l for l in res.lines), res.lines
    install.install(only=["claude"], shell_guard=False)
    assert guards()[0] == [] and len(guards()[1]) == 1


@pytest.mark.integration
@pytest.mark.approved
def test_amendment_relaunch_is_its_own_fix_round_work(env):
    wenv, wt, d = _dispatched(env)
    _fix_round(env, d, amendment_id="A1")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and out.startswith("PREFLIGHT ready") and "amendment: A1" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_a_landed_orchestrator_prompt_names_the_fix_round_and_ends_the_wait(env):
    wenv, wt, d = _dispatched(env)
    con = env.con()
    _fix_round(env, d)
    env.office("preflight", cwd=wt, env=wenv)
    from office import db, guide, state
    run = state.get_run(con, d["run_id"])
    with db.transaction(con):
        state.emit(con, run, "prompt", "T1: orchestrator prompt landed", audience="runtime", task_id="T1",
                   dispatch_id=d["id"], payload={"text": "F1: show the invite only once saved", "outcome": "landed"})
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "named by the orchestrator: F1: show the invite only once saved" in out, out
    assert not any("is waiting on you" in s for s in guide.stalls(con, run))
