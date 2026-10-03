"""Issue #257: a reboot-orphaned review dispatch, superseded integration reviews,
`office revoke <dispatch|integration>`, a re-run `office land`, and the
`office wait` false stall while integration checks run."""
from __future__ import annotations

import json
import os
import sys

import pytest

from conftest import GOOD_ADD, approved_run
from office import dispatch as dispatch_mod
from office import gates, integration, state
from office.util import now_iso
from test_land import _plan, _run
from test_task_prs import gh

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}

FAKE_HERDR = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_AGENT", "gone")
if args[:2] == ["agent", "get"]:
    if mode == "alive":
        print(json.dumps({{"result": {{"agent": {{"name": args[2], "agent_status": "working"}}}}}})); sys.exit(0)
    if mode == "gone":
        print(json.dumps({{"error": {{"code": "not_found"}}}})); sys.exit(1)
    sys.stderr.write("server unavailable"); sys.exit(1)
print(json.dumps({{"result": {{}}}}))
'''
DEAD_PID = 2 ** 22 + 12345


def _run_row(con):
    return state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])


def _orphan(env, monkeypatch, *, pid=DEAD_PID, age="2026-01-01T00:00:00+00:00", subject="integration", gate="Gorph"):
    """A running pane-hosted reviewer on a still-running gate, as a host reboot leaves it."""
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    con = env.con()
    run = _run_row(con)
    con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at) VALUES(?,?,?,?,?,?,?)",
                (gate, run["id"], subject, "integration_review", "integration:Iold", "running", age))
    con.execute("INSERT INTO dispatches(id, run_id, role, started_at, kind, status, launcher, pane_id, pid, gate_id) "
                "VALUES('Dorph', ?, 'integration_reviewer', ?, 'reviewer', 'running', 'herdr', 'w1:p1', ?, ?)",
                (run["id"], age, pid, gate))
    con.commit()
    return con, run


def _dispatch(con):
    return dict(con.execute("SELECT * FROM dispatches WHERE id='Dorph'").fetchone())


@pytest.mark.approved
def test_a_reviewer_whose_process_and_agent_are_gone_is_reaped(env, monkeypatch):
    con, run = _orphan(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_AGENT", "gone")
    notes = dispatch_mod.reap_orphans(con, run)
    d = _dispatch(con)
    assert notes and d["ended_at"] and d["status"] == "failed" and d["terminal_classification"] == "lost", d
    kinds = [r[0] for r in con.execute("SELECT kind FROM events WHERE dispatch_id='Dorph'")]
    assert "dispatch.ended" in kinds and "dispatch.reaped" in kinds, kinds
    gate = con.execute("SELECT status, verdict FROM gates WHERE id='Gorph'").fetchone()
    assert (gate[0], gate[1]) == ("done", "UNAVAILABLE")


@pytest.mark.approved
@pytest.mark.parametrize("mode", ["alive", "unreachable"])
def test_a_reviewer_whose_agent_may_be_alive_is_never_reaped(env, monkeypatch, mode):
    con, run = _orphan(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_AGENT", mode)
    assert dispatch_mod.reap_orphans(con, run) == []
    assert _dispatch(con)["ended_at"] is None


@pytest.mark.approved
def test_a_live_supervisor_a_reply_or_a_fresh_launch_is_never_reaped(env, monkeypatch):
    con, run = _orphan(env, monkeypatch, pid=os.getpid())
    monkeypatch.setenv("FAKE_HERDR_AGENT", "gone")
    assert dispatch_mod.reap_orphans(con, run) == []  # supervisor alive
    con.execute("UPDATE dispatches SET pid=?", (DEAD_PID,))
    ddir = dispatch_mod.paths.run_dir(run["id"]) / "dispatches" / "Dorph"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "reply.txt").write_text("VERDICT: PASS\n")
    assert dispatch_mod.reap_orphans(con, run) == []  # the review answered
    (ddir / "reply.txt").unlink()
    con.execute("UPDATE dispatches SET started_at=?", (now_iso(),))
    assert dispatch_mod.reap_orphans(con, run) == []  # inside the launch grace
    assert _dispatch(con)["ended_at"] is None


@pytest.mark.approved
def test_status_reaps_and_a_superseded_integration_review_does_not_block_close(env, monkeypatch):
    con, run = _orphan(env, monkeypatch)
    monkeypatch.setenv("FAKE_HERDR_AGENT", "gone")
    landing = {"integration": {"commit": "Inew", "status": "accepted"}}
    con.execute("UPDATE runs SET landing_json=?", (json.dumps(landing),))
    con.commit()
    # Reviews on a superseded revision never block, even before the reaper runs.
    assert "reviews are still running" not in gates.close_blockers(con, _run_row(con))
    # The current revision's running review still blocks.
    con.execute("UPDATE gates SET input_key='integration:Inew'")
    con.commit()
    assert "reviews are still running" in gates.close_blockers(con, _run_row(con))
    env.office("status", env={"FAKE_HERDR_AGENT": "gone"}, check=0)
    assert con.execute("SELECT ended_at FROM dispatches WHERE id='Dorph'").fetchone()[0]


@pytest.mark.approved
def test_revoke_ends_a_dispatch_by_id(env, monkeypatch):
    con, run = _orphan(env, monkeypatch)
    code, out = env.office("revoke", "Dorph", env={"FAKE_HERDR_AGENT": "alive"})
    assert code == 0 and "Dorph ended" in out, out
    d = _dispatch(env.con())
    assert d["ended_at"] and d["terminal_classification"] in ("signal", "revoked")
    code, out = env.office("revoke", "Dnope", env=EXTERNAL)
    assert code == 2 and "unknown-task" in out, out


@pytest.mark.approved
def test_revoke_integration_ends_its_reviews(env, monkeypatch):
    con, run = _orphan(env, monkeypatch)
    code, out = env.office("revoke", "integration", env={"FAKE_HERDR_AGENT": "gone"})
    assert code == 0 and "Dorph" in out, out
    con = env.con()
    assert _dispatch(con)["ended_at"]
    assert con.execute("SELECT status FROM gates WHERE id='Gorph'").fetchone()[0] == "done"
    assert "reviews are still running" not in gates.close_blockers(con, _run_row(con))


@pytest.mark.approved
def test_a_running_integrate_job_is_not_a_checks_stall(env, monkeypatch):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("status", check=0)
    con = env.con()
    run = _run_row(con)
    con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at) "
                "VALUES('Gint', ?, 'integration', 'checks', 'integration:Icur', 'running', '2026-01-01')", (run["id"],))
    con.execute("UPDATE outbox SET status='done' WHERE status IN ('queued','claimed')")
    con.execute("UPDATE runs SET landing_json=?", (json.dumps({"integration": {"commit": "Icur"}}),))
    con.commit()
    from office import guide
    assert any("Gint" in s for s in guide.stalls(con, run))  # nothing runs it
    con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, claimed_pid, created_at) "
                "VALUES('Jint', ?, 'integrate', 'k', '{\"key\": \"x\"}', ?, 'claimed', ?, '2026-01-01')",
                (run["id"], run["office_version"], os.getpid()))
    con.commit()
    assert not any("Gint" in s for s in guide.stalls(con, run))


@pytest.mark.approved
def test_rebase_and_a_second_compose_wait_for_the_integration_lock(env, monkeypatch):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = _run_row(con)
    with integration.worktree_lock(run):
        code, out = env.office("land", "--rebase")
        assert code == 4 and "integration-running" in out, out
        monkeypatch.setattr(integration, "LOCK_WAIT_SECONDS", 0.3)
        with pytest.raises(state.Refused) as err:
            integration.job_integrate(con, run, {"payload": {"key": "x"}})
        assert err.value.category == "integration-running"
    with integration.worktree_lock(run):  # released by the holder
        pass


@pytest.mark.approved
def test_revoke_integration_cancels_and_stops_the_owning_job(env, monkeypatch):
    import subprocess
    con, run = _orphan(env, monkeypatch)
    job = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, claimed_pid, "
                    "attempts, created_at) VALUES('Jint', ?, 'integrate', 'k3', '{\"key\": \"x\"}', ?, 'claimed', ?, 1, "
                    "'2026-01-01')", (run["id"], run["office_version"], job.pid))
        con.commit()
        code, out = env.office("revoke", "integration", env={"FAKE_HERDR_AGENT": "alive"})
        assert code == 0 and "cancelled job Jint" in out, out
        assert job.wait(timeout=10) is not None  # stopped
        con = env.con()
        row = con.execute("SELECT status, max_attempts, attempts FROM outbox WHERE id='Jint'").fetchone()
        assert row[0] == "failed" and row[1] == row[2]
        assert json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])["integration"]["status"] == "blocked"
        assert _dispatch(con)["ended_at"]
    finally:
        job.kill()


@pytest.mark.approved
def test_revoking_a_stale_or_ended_executor_dispatch_changes_nothing(env, monkeypatch):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    run = _run_row(con)
    cur = con.execute("SELECT current_dispatch_id FROM tasks WHERE id='T1'").fetchone()[0]
    con.execute("INSERT INTO dispatches(id, run_id, role, started_at, kind, status, launcher, task_id) "
                "VALUES('Dold', ?, 'executor', '2026-01-01', 'executor', 'running', 'external', 'T1')", (run["id"],))
    con.commit()
    before = dict(con.execute("SELECT status, pause_reason FROM tasks WHERE id='T1'").fetchone())
    code, out = env.office("revoke", "Dold", env=EXTERNAL)
    assert code == 0 and "nothing revoked" in out, out
    con.execute("UPDATE dispatches SET ended_at='2026-01-02', status='exited' WHERE id=?", (cur,))
    con.commit()
    code, out = env.office("revoke", cur, env=EXTERNAL)
    assert code == 0 and "ended; nothing revoked" in out, out
    assert dict(con.execute("SELECT status, pause_reason FROM tasks WHERE id='T1'").fetchone()) == before
    assert not con.execute("SELECT 1 FROM leases WHERE revoked_at IS NOT NULL").fetchone()


def test_a_second_land_skips_the_prod_deploy_of_the_same_tree(env, monkeypatch):
    marker = env.tmp / "deploys"
    plan = _plan("e2e", prod=f"python3 -c \"open('{marker}','a').write('x')\"", verify=f"test -f {marker}")
    _run(env, monkeypatch, plan)
    code, out = env.office("land")
    assert code == 0 and "prod deploy ok" in out, out
    assert marker.read_text() == "x"
    code, out = env.office("land")
    assert code == 0 and "already landed" in out and "prod deploy ok" not in out, out
    assert marker.read_text() == "x"
    assert gh(env)["closed_issues"] == ["7"]
    code, out = env.office("close")
    assert code == 0 and "closed" in out, out


def _strip_deployed(env):
    con = env.con()
    landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    landing.pop("deployed")
    con.execute("UPDATE runs SET landing_json=?", (json.dumps(landing),))
    con.commit()


def test_a_land_recorded_by_the_old_runtime_does_not_deploy_prod_again(env, monkeypatch):
    marker = env.tmp / "deploys"
    plan = _plan("e2e", prod=f"python3 -c \"open('{marker}','a').write('x')\"", verify=f"test -f {marker}")
    _run(env, monkeypatch, plan)
    assert env.office("land")[0] == 0
    _strip_deployed(env)  # the old runtime recorded `merged` only
    code, out = env.office("land")
    assert code == 0 and "already landed" in out, out
    assert marker.read_text() == "x"


def test_a_merge_only_land_does_not_count_as_a_prod_deploy(env, monkeypatch):
    marker = env.tmp / "deploys"
    plan = _plan("merge", prod=f"python3 -c \"open('{marker}','a').write('x')\"", verify=f"test -f {marker}")
    _run(env, monkeypatch, plan)
    assert env.office("land")[0] == 0 and not marker.exists()
    code, out = env.office("land", "--e2e", "--quote", "now deploy it")
    assert code == 0 and "prod deploy ok" in out, out
    assert marker.read_text() == "x"


def _ambiguous(env, monkeypatch):
    """An old-runtime land whose prod deploy event predates the merge record's invocation."""
    marker = env.tmp / "deploys"
    plan = _plan("e2e", prod=f"python3 -c \"open('{marker}','a').write('x')\"", verify=f"test -f {marker}")
    _run(env, monkeypatch, plan)
    assert env.office("land")[0] == 0
    _strip_deployed(env)
    con = env.con()
    con.execute("UPDATE events SET created_at='2000-01-01T00:00:00+00:00' WHERE kind='land.deploy'")
    con.commit()
    return marker


def test_an_untied_legacy_deploy_record_refuses_with_both_operator_commands(env, monkeypatch):
    marker = _ambiguous(env, monkeypatch)
    code, out = env.office("land")
    assert code == 4 and "blocked: deploy-unproven: run " in out and "but no deploy record names that tree" in out, out
    assert "office land --e2e --redeploy" in out and "office land --e2e --mark-deployed" in out, out
    assert marker.read_text() == "x"


def test_mark_deployed_records_the_operator_claim_without_deploying(env, monkeypatch):
    marker = _ambiguous(env, monkeypatch)
    code, out = env.office("land", "--e2e", "--mark-deployed", "--quote", "it is live")
    assert code == 0 and "already landed" in out, out
    assert marker.read_text() == "x"
    landing = json.loads(env.con().execute("SELECT landing_json FROM runs").fetchone()[0])
    assert landing["deployed"]["prod"]["by"] == "operator-confirmed"
    assert env.office("land")[0] == 0 and marker.read_text() == "x"


def test_redeploy_deploys_the_tree_again_and_records_it(env, monkeypatch):
    marker = _ambiguous(env, monkeypatch)
    code, out = env.office("land", "--e2e", "--redeploy", "--quote", "deploy again")
    assert code == 0 and "prod deploy ok" in out, out
    assert marker.read_text() == "xx"
    landing = json.loads(env.con().execute("SELECT landing_json FROM runs").fetchone()[0])
    assert landing["deployed"]["prod"]["by"] == "redeploy"


def test_the_deploy_flags_need_an_e2e_land(env, monkeypatch):
    _run(env, monkeypatch, _plan("merge"))
    code, out = env.office("land", "--merge", "--redeploy", "--quote", "go")
    assert code == 2 and "deploy-flag-needs-e2e" in out, out


def test_preview_deploys_from_an_isolated_checkout_not_the_integration_worktree(env, monkeypatch):
    seen = env.tmp / "cwd"
    plan = _plan("preview", preview=f"python3 -c \"import os; open('{seen}','w').write(os.getcwd())\"")
    _run(env, monkeypatch, plan)
    code, out = env.office("land")
    assert code == 0 and "preview deploy ok" in out, out
    where = seen.read_text()
    assert "_integration" not in where and "checkouts" in where, where
    assert not os.path.exists(where)  # removed after the deploy
