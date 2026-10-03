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
from office.util import claim_alive, claim_identity, claim_signalable, now_iso
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
                    "claimed_by, attempts, created_at) VALUES('Jint', ?, 'integrate', 'k3', '{\"key\": \"x\"}', ?, 'claimed', "
                    "?, ?, 1, '2026-01-01')", (run["id"], run["office_version"], job.pid, claim_identity(job.pid)))
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


def test_a_merge_only_land_does_not_count_as_a_prod_deploy(env, monkeypatch):
    marker = env.tmp / "deploys"
    plan = _plan("merge", prod=f"python3 -c \"open('{marker}','a').write('x')\"", verify=f"test -f {marker}")
    _run(env, monkeypatch, plan)
    assert env.office("land")[0] == 0 and not marker.exists()
    code, out = env.office("land", "--e2e", "--quote", "now deploy it")
    assert code == 0 and "prod deploy ok" in out, out
    assert marker.read_text() == "x"


def _legacy(env):
    """Rewrite a land's records into the old runtime's shape: a merge record
    with no integration commit, no `deployed` marker, deploy events that name
    no tree or commit, and no start events."""
    _strip_deployed(env)
    con = env.con()
    landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    landing["merged"].pop("integration_commit", None)
    con.execute("UPDATE runs SET landing_json=?", (json.dumps(landing),))
    con.execute("DELETE FROM events WHERE kind='land.deploy.start'")
    for seq, payload in con.execute("SELECT seq, payload_json FROM events WHERE kind IN ('land.deploy','land.verify')").fetchall():
        p = json.loads(payload)
        con.execute("UPDATE events SET payload_json=? WHERE seq=?",
                    (json.dumps({"target": p["target"], "exit": p["exit"]}), seq))
    con.commit()


def _ambiguous(env, monkeypatch):
    """An old-runtime e2e land: merged, deployed, verified, recorded in the old shape."""
    marker = env.tmp / "deploys"
    plan = _plan("e2e", prod=f"python3 -c \"open('{marker}','a').write('x')\"", verify=f"test -f {marker}")
    _run(env, monkeypatch, plan)
    assert env.office("land")[0] == 0
    _legacy(env)
    return marker


def test_a_legacy_deploy_inside_the_merge_window_is_never_proof(env, monkeypatch):
    """Finding r3-1: the deploy event falls between the last PR merge and
    `merged.at`, which the old backfill credited. It names no tree, so Office
    refuses rather than skip or repeat the prod deploy."""
    marker = _ambiguous(env, monkeypatch)
    code, out = env.office("land")
    assert code == 4 and "deploy-unproven" in out and "recorded before Office named deployed trees" in out, out
    assert "office land --e2e --redeploy" in out and "office land --e2e --mark-deployed" in out, out
    assert marker.read_text() == "x"
    landing = json.loads(env.con().execute("SELECT landing_json FROM runs").fetchone()[0])
    assert "deployed" not in landing  # nothing was credited


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


def _move_integration(env) -> str:
    """The accepted integration moves to a commit with another tree after the land."""
    con = env.con()
    landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    base = con.execute("SELECT base_sha FROM runs").fetchone()[0]
    old = landing["integration"]["commit"]
    moved = env.git("commit-tree", f"{base}^{{tree}}", "-p", old, "-m", "moved").strip()
    landing["integration"]["commit"] = moved
    con.execute("UPDATE runs SET landing_json=?", (json.dumps(landing),))
    con.commit()
    return moved


def test_a_land_of_an_older_integration_is_not_reported_as_landing_the_current_one(env, monkeypatch):
    """Finding r3-2: the integration moved after tree A landed. With every task
    PR merged there is nothing to land it with: land and close both refuse."""
    _run(env, monkeypatch, _plan("merge"))
    assert env.office("land")[0] == 0
    moved = _move_integration(env)
    code, out = env.office("land")
    assert code == 4 and "landed-mismatch" in out and moved[:12] in out and "already landed" not in out, out
    code, out = env.office("close")
    assert code == 4 and "is for integration" in out and moved[:12] in out, out


def test_a_moved_integration_with_an_open_task_pr_is_landed(env, monkeypatch):
    """Finding r3-2: an open task PR carries the change, so land merges it and
    records the new integration."""
    _run(env, monkeypatch, _plan("merge"))
    assert env.office("land")[0] == 0
    moved = _move_integration(env)
    con = env.con()
    pr = json.loads(con.execute("SELECT pr_json FROM tasks WHERE id='T2'").fetchone()[0])
    con.execute("UPDATE tasks SET pr_json=? WHERE id='T2'", (json.dumps({**pr, "merged": False}),))
    con.commit()
    code, out = env.office("land")
    assert code == 0 and "landing it" in out and "already landed" not in out, out
    landing = json.loads(env.con().execute("SELECT landing_json FROM runs").fetchone()[0])
    assert landing["merged"]["integration_commit"] == moved


def test_an_unrelated_live_job_does_not_keep_an_integration_gate_open(env, monkeypatch):
    """Finding r3-3: only the gate's owning job counts."""
    con, run = _orphan(env, monkeypatch)
    con.execute("UPDATE outbox SET status='done' WHERE status IN ('queued','claimed')")
    con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, claimed_pid, "
                "claimed_at, created_at) VALUES('Jrev', ?, 'review', 'k4', '{\"gate_id\": \"Gother\"}', ?, 'claimed', ?, "
                "'2025-01-01', '2025-01-01')", (run["id"], run["office_version"], os.getpid()))
    con.commit()
    code, out = env.office("revoke", "integration", env={"FAKE_HERDR_AGENT": "gone"})
    assert code == 0, out
    con = env.con()
    assert con.execute("SELECT status FROM gates WHERE id='Gorph'").fetchone()[0] == "done"
    assert con.execute("SELECT status FROM outbox WHERE id='Jrev'").fetchone()[0] == "claimed"  # untouched


@pytest.mark.approved
def test_the_owning_integrate_job_keeps_its_gate_and_a_later_one_does_not(env, monkeypatch):
    con, run = _orphan(env, monkeypatch)
    con.execute("UPDATE dispatches SET ended_at='2026-01-02', status='failed' WHERE id='Dorph'")
    con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, claimed_pid, "
                "claimed_at, created_at) VALUES('Jlater', ?, 'integrate', 'k5', '{\"key\": \"x\"}', ?, 'claimed', ?, "
                "'2026-06-01', '2026-06-01')", (run["id"], run["office_version"], os.getpid()))
    con.commit()
    gate = con.execute("SELECT * FROM gates WHERE id='Gorph'").fetchone()
    assert gates.owning_jobs(con, run, gate) == []  # claimed after the gate existed: not its owner
    con.execute("UPDATE outbox SET claimed_at='2025-12-31' WHERE id='Jlater'")
    con.commit()
    assert [j["id"] for j in gates.owning_jobs(con, run, gate)] == ["Jlater"]
    with con:
        dispatch_mod._close_orphaned_gate(con, run, "Gorph", "test")
    assert con.execute("SELECT status FROM gates WHERE id='Gorph'").fetchone()[0] == "running"


@pytest.mark.approved
def test_rebase_refuses_while_an_integrate_job_without_the_flock_is_live(env, monkeypatch):
    """Finding r3-4: an integrate process from an older patch holds no flock;
    the job table plus its pid still refuses the rebase, and a new compose waits."""
    import subprocess
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = _run_row(con)
    old = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, claimed_pid, "
                    "claimed_at, created_at) VALUES('Jold', ?, 'integrate', 'k6', '{\"key\": \"x\"}', ?, 'claimed', ?, "
                    "'2026-01-01', '2026-01-01')", (run["id"], run["office_version"], old.pid))
        con.commit()
        code, out = env.office("land", "--rebase")
        assert code == 4 and "integration-running" in out and str(old.pid) in out, out
        monkeypatch.setattr(integration, "LOCK_WAIT_SECONDS", 0.3)
        with pytest.raises(state.Refused) as err:
            integration.job_integrate(con, run, {"payload": {"key": "x"}})
        assert err.value.category == "integration-running" and "older Office patch" in str(err.value)
        # A peer running this code registers as a flock holder and is left to the flock.
        marker = integration._fenced_dir(run) / str(old.pid)
        marker.touch()
        assert integration.live_integrate_pids(con, run, unfenced_only=True) == []
        assert integration.live_integrate_pids(con, run) == [old.pid]
        marker.unlink()
    finally:
        old.kill()


def test_each_deploy_uses_its_own_checkout_and_records_what_it_checked_out(env, monkeypatch):
    """Finding r3-5: concurrent deploys never share or remove each other's
    checkout, and the marker names the commit and tree actually checked out."""
    from office import land
    seen = env.tmp / "cwd"
    plan = _plan("preview", preview=f"python3 -c \"import os; open('{seen}','w').write(os.getcwd())\"")
    _run(env, monkeypatch, plan)
    con = env.con()
    run = _run_row(con)
    commit = integration.final_commit(con, run)
    with land._deploy_checkout(run, commit, "deploy-preview") as (a, head_a, tree_a):
        with land._deploy_checkout(run, commit, "deploy-preview") as (b, _head_b, _tree_b):
            assert a != b and a.is_dir() and b.is_dir()
        assert a.is_dir() and not b.exists()
    assert not a.exists()
    assert head_a == commit and tree_a == env.git("rev-parse", f"{commit}^{{tree}}").strip()
    code, out = env.office("land")
    assert code == 0 and "preview deploy ok" in out, out
    con = env.con()
    landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    assert (landing["deployed"]["preview"]["commit"], landing["deployed"]["preview"]["tree"]) == (head_a, tree_a)
    started = [json.loads(p) for (p,) in con.execute("SELECT payload_json FROM events WHERE kind='land.deploy.start'")]
    assert started and (started[0]["commit"], started[0]["tree"]) == (head_a, tree_a)


def test_a_second_concurrent_land_refuses(env, monkeypatch):
    from office import land
    _run(env, monkeypatch, _plan("merge"))
    with land._land_lock(_run_row(env.con())):
        code, out = env.office("land")
        assert code == 4 and "land-running" in out, out
    assert env.office("land")[0] == 0


def test_a_land_killed_mid_deploy_refuses_instead_of_deploying_again(env, monkeypatch):
    """Crash window: a start with no result for this tree is unknown, not undeployed."""
    marker = env.tmp / "deploys"
    plan = _plan("e2e", prod=f"python3 -c \"open('{marker}','a').write('x')\"", verify=f"test -f {marker}")
    _run(env, monkeypatch, plan)
    assert env.office("land")[0] == 0
    _strip_deployed(env)
    con = env.con()
    landing = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])
    landing.pop("merged")
    con.execute("UPDATE runs SET landing_json=?", (json.dumps(landing),))
    con.execute("DELETE FROM events WHERE kind IN ('land.deploy','land.verify')")  # killed before the result
    con.commit()
    code, out = env.office("land")
    assert code == 4 and "deploy-unproven" in out and "never reported its result" in out, out
    assert marker.read_text() == "x"
    code, out = env.office("land", "--e2e", "--mark-deployed", "--quote", "it is live")
    assert code == 0 and "on the operator's word" in out, out
    assert marker.read_text() == "x"


def test_a_task_accepted_while_land_waits_for_the_lock_refuses_before_any_merge(env, monkeypatch):
    """Round-4 L2: land read the integration, then a task revision was accepted
    before it took the lock. It refuses; nothing is merged or deployed."""
    import contextlib
    from office import land
    marker = env.tmp / "deploys"
    plan = _plan("e2e", prod=f"python3 -c \"open('{marker}','a').write('x')\"", verify=f"test -f {marker}")
    _run(env, monkeypatch, plan)
    con = env.con()
    real_lock = land._land_lock

    @contextlib.contextmanager
    def lock_after_an_acceptance(run):
        con.execute("UPDATE tasks SET accepted_revision_id='Rlater' WHERE id='T2'")  # accepted meanwhile
        con.commit()
        with real_lock(run):
            yield

    monkeypatch.setattr(land, "_land_lock", lock_after_an_acceptance)
    with pytest.raises(state.Refused) as err:
        land.land(con, _run_row(con))
    assert err.value.category == "integration-changed", err.value
    assert not any(c[:2] == ["pr", "merge"] for c in gh(env)["calls"])
    assert not marker.exists()
    landing = json.loads(env.con().execute("SELECT landing_json FROM runs").fetchone()[0])
    assert "merged" not in landing and "deployed" not in landing


def test_a_reused_pid_is_neither_alive_nor_signalled(env, monkeypatch):
    """Round-4 L4: a claim names its process by pid and start time."""
    import subprocess
    con, run = _orphan(env, monkeypatch)
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        reused = f"host:{other.pid}@Thu Jan  1 00:00:00 1970"  # the claimant died; its pid now runs `sleep`
        assert not claim_alive(other.pid, reused) and not claim_signalable(other.pid, reused)
        assert claim_alive(other.pid, claim_identity(other.pid)) and claim_signalable(other.pid, claim_identity(other.pid))
        assert claim_alive(other.pid, f"host:{other.pid}") and not claim_signalable(other.pid, f"host:{other.pid}")
        con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, claimed_pid, "
                    "claimed_by, attempts, created_at) VALUES('Jreused', ?, 'integrate', 'k7', '{\"key\": \"x\"}', ?, "
                    "'claimed', ?, ?, 1, '2026-01-01')", (run["id"], run["office_version"], other.pid, reused))
        con.commit()
        code, out = env.office("revoke", "integration", env={"FAKE_HERDR_AGENT": "gone"})
        assert code == 0 and "cancelled job Jreused" in out, out
        assert other.poll() is None  # never signalled
        # An older claim (no start time) is alive for reaping but never signalled.
        con = env.con()
        con.execute("UPDATE outbox SET status='claimed', claimed_pid=?, claimed_by=? WHERE id='Jreused'",
                    (other.pid, f"host:{other.pid}"))
        con.commit()
        code, out = env.office("revoke", "integration", env={"FAKE_HERDR_AGENT": "gone"})
        assert code == 0 and "not signalled" in out and "Jreused" in out, out
        assert other.poll() is None
    finally:
        other.kill()


@pytest.mark.approved
def test_a_crash_between_the_dispatch_end_and_the_gate_close_is_repaired(env, monkeypatch):
    """Round-4 L5: the end and the gate close commit together, and a gate left
    running behind an ended reviewer (a crash, or an end recorded elsewhere)
    is closed by the reaper that status, wait and close run."""
    con, run = _orphan(env, monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(gates, "mark_unavailable", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError):
            dispatch_mod._end_dispatch(con, run, _dispatch(con), "revoked", "test", stop=False)
    assert _dispatch(con)["ended_at"] is None  # rolled back with the gate close: no half state
    con.execute("UPDATE dispatches SET ended_at='2026-01-02', status='failed' WHERE id='Dorph'")  # ended elsewhere
    con.commit()
    assert con.execute("SELECT status FROM gates WHERE id='Gorph'").fetchone()[0] == "running"
    notes = dispatch_mod.reap_orphans(con, run)
    assert any("Gorph" in n for n in notes), notes
    assert tuple(con.execute("SELECT status, verdict FROM gates WHERE id='Gorph'").fetchone()) == ("done", "UNAVAILABLE")


def _stoppable(env, monkeypatch, identity):
    """Dorph supervised by a live `sleep` with a live agent group, as an
    Office process launch leaves it. `identity` is how its pids were recorded."""
    import subprocess
    con, run = _orphan(env, monkeypatch)
    sup = subprocess.Popen(["sleep", "60"], start_new_session=True)
    agent = subprocess.Popen(["sleep", "60"], start_new_session=True)
    con.execute("UPDATE dispatches SET launcher='process', pane_id=NULL, pid=? WHERE id='Dorph'", (sup.pid,))
    con.commit()
    ddir = dispatch_mod.paths.run_dir(run["id"]) / "dispatches" / "Dorph"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "agent.pgid").write_text(str(agent.pid))
    for which, p in (("supervisor", sup), ("agent", agent)):
        if identity == "match":
            dispatch_mod._record_identity(run["id"], "Dorph", which, p.pid)
        elif identity == "mismatch":  # the pid was reused by a process that started at another time
            (ddir / f"{which}.identity").write_text(json.dumps({"pid": p.pid, "start": "Thu Jan  1 00:00:00 1970"}))
    return sup, agent


def test_a_dispatch_with_a_matching_identity_is_signalled(env, monkeypatch):
    sup, agent = _stoppable(env, monkeypatch, "match")
    try:
        code, out = env.office("revoke", "Dorph", env=EXTERNAL)
        assert code == 0 and "Dorph ended" in out and "manual recovery" not in out, out
        assert sup.wait(timeout=10) is not None and agent.wait(timeout=10) is not None
    finally:
        sup.kill()
        agent.kill()


@pytest.mark.parametrize("identity", ["mismatch", "missing"])
def test_a_dispatch_pid_that_cannot_be_proven_is_never_signalled(env, monkeypatch, identity):
    """Focused review finding 1: a reused or unrecorded pid gets no signal and
    the revoke names it for manual recovery."""
    sup, agent = _stoppable(env, monkeypatch, identity)
    try:
        code, out = env.office("revoke", "Dorph", env=EXTERNAL)
        assert code == 0 and "manual recovery needed" in out, out
        assert f"supervisor pid {sup.pid}" in out and f"agent process group {agent.pid}" in out, out
        assert sup.poll() is None and agent.poll() is None  # no signal sent
    finally:
        sup.kill()
        agent.kill()
