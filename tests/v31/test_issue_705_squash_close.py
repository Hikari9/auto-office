"""Recover after the task squash merges without pretending their commits are ancestors."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from office import db, gates, jobs, land, landing_recovery, state
from office.state import Refused
from test_land import _plan, _run
from test_land_recovery import SQUASH_ONLY
from test_landing_gaps import _advance_main
from test_task_prs import gh, remote_head


def _run_row(env):
    con = env.con()
    return state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])


def _interrupted(env, monkeypatch, plan=None):
    bare, _, _ = _run(env, monkeypatch, plan or _plan("merge"), repo=SQUASH_ONLY)
    with monkeypatch.context() as patch:
        def interrupted(*args):
            raise Refused("post-merge-interrupted", "stopped before default-branch verification")
        patch.setattr(land, "_verify_main", interrupted)
        code, out = env.office("land")
    assert code == 4 and "post-merge-interrupted" in out, out
    assert [p["state"] for p in gh(env)["prs"]] == ["merged", "merged"]
    assert not (_run_row(env).get("landing") or {}).get("merged")
    return bare


def test_land_reentry_verifies_already_squashed_prs_without_merging_again(env, monkeypatch):
    _interrupted(env, monkeypatch)
    before = sum(c[:2] == ["pr", "merge"] for c in gh(env)["calls"])
    code, out = env.office("land")
    assert code == 0 and "matches the reviewed integration tree" in out, out
    assert sum(c[:2] == ["pr", "merge"] for c in gh(env)["calls"]) == before == 2
    assert gh(env)["closed_issues"] == ["7"]
    env.office("close", check=0)
    assert _run_row(env)["phase"] == "closed"


def test_owned_squash_pr_close_proves_heads_and_checks_without_fake_ancestry_or_consent(env, monkeypatch):
    _interrupted(env, monkeypatch)
    con = env.con()
    before = con.execute("SELECT count(*) FROM authorizations").fetchone()[0]
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/2")
    assert code == 0, out
    run = _run_row(env)
    landing = run["landing"]
    assert run["phase"] == "closed" and landing["contains"] == [] and landing["unproven"] == []
    proof = landing["squash_proof"]
    assert proof["kind"] == "office-task-prs" and sorted(proof["tasks"]) == ["T1", "T2"]
    assert proof["checks"]["verdict"] == "PASS" and len(proof["checks"]["results"]) == 2
    assert proof["merge_commit"] == gh(env)["prs"][1]["merge_commit"]
    assert con.execute("SELECT count(*) FROM authorizations").fetchone()[0] == before
    assert not gh(env).get("closed_issues")
    assert sum(c[:2] == ["pr", "merge"] for c in gh(env)["calls"]) == 2
    gate = con.execute("SELECT status, verdict FROM gates WHERE id=?", (proof["checks"]["gate_id"],)).fetchone()
    assert tuple(gate) == ("done", "PASS")
    job = state.get_job(con, proof["checks"]["job_id"])
    assert job["kind"] == "landing_verify" and job["status"] == "done" and job["claim_token"]
    receipt = json.loads((env.state / "runs" / run["id"] / "archive-receipt.json").read_text())
    assert receipt["landing"]["squash_proof"] == proof


def test_an_external_pr_with_the_exact_reviewed_tree_has_a_separate_proof(env, monkeypatch):
    _interrupted(env, monkeypatch)
    s = gh(env)
    s["prs"].append({**s["prs"][1], "number": 3, "url": "https://github.com/o/r/pull/3", "head": "external",
                     "merged_head": "f" * 40})
    (env.tmp / "gh.json").write_text(json.dumps(s))
    env.office("close", "--landed-externally", "https://github.com/o/r/pull/3", check=0)
    proof = _run_row(env)["landing"]["squash_proof"]
    assert proof["kind"] == "reviewed-tree" and proof["tree_equal"]
    assert proof["checks"]["verdict"] == "PASS" and all("pr" not in t for t in proof["tasks"].values())


@pytest.mark.parametrize("field,value", [("head", "someone-elses-branch"), ("merged_head", "f" * 40)])
def test_unproven_task_pr_identity_is_not_claimed_even_when_tree_proof_is_valid(env, monkeypatch, field, value):
    _interrupted(env, monkeypatch)
    s = gh(env)
    s["prs"][0][field] = value
    (env.tmp / "gh.json").write_text(json.dumps(s))
    env.office("close", "--landed-externally", "2", check=0)
    assert _run_row(env)["landing"]["squash_proof"]["kind"] == "reviewed-tree"


def test_wrong_base_or_incomplete_squash_tree_cannot_close(env, monkeypatch):
    _interrupted(env, monkeypatch)
    s = gh(env)
    s["prs"][1]["base"] = "release"
    (env.tmp / "gh.json").write_text(json.dumps(s))
    for ref in ("1", "2"):
        code, out = env.office("close", "--landed-externally", ref)
        assert code == 4 and "landing-unproven" in out, out
        assert _run_row(env)["phase"] != "closed"
    assert not env.con().execute("SELECT 1 FROM gates WHERE subject='landing-recovery'").fetchone()


def test_changed_tree_and_unreviewed_head_cannot_use_a_stale_owned_pr_record(env, monkeypatch):
    bare = _interrupted(env, monkeypatch)
    _advance_main(env, bare, "NOTES.md", "unreviewed addition\n")
    s = gh(env)
    s["prs"][1]["merge_commit"] = remote_head(bare, "main")
    s["prs"][1]["merged_head"] = "f" * 40
    (env.tmp / "gh.json").write_text(json.dumps(s))
    code, out = env.office("close", "--landed-externally", "2")
    assert code == 4 and "landing-unproven" in out, out
    assert _run_row(env)["phase"] != "closed"


@pytest.mark.parametrize("ref,closes", [("1", False), ("2", True)])
def test_missing_task_pr_needs_the_complete_reviewed_tree(env, monkeypatch, ref, closes):
    _interrupted(env, monkeypatch)
    con = env.con()
    # An accepted task without file scope has no task PR. A proof about T1's
    # squash alone must not silently assert that every accepted task landed.
    con.execute("UPDATE tasks SET scope_json='[]', pr_json=NULL WHERE id='T2'")
    con.commit()
    code, out = env.office("close", "--landed-externally", ref)
    if closes:
        assert code == 0, out
        proof = _run_row(env)["landing"]["squash_proof"]
        assert proof["kind"] == "reviewed-tree" and sorted(proof["tasks"]) == ["T1", "T2"]
    else:
        assert code == 4 and "landing-unproven" in out, out
        assert _run_row(env)["phase"] != "closed"


def test_failed_fresh_checks_keep_run_open_and_a_retry_rechecks(env, monkeypatch):
    flag = env.tmp / "checks-available"
    flag.write_text("yes")
    plan = _plan("merge").replace("checks: python3 -c", f"checks: test -f {flag} && python3 -c", 1)
    _interrupted(env, monkeypatch, plan)
    flag.unlink()
    code, out = env.office("close", "--landed-externally", "2")
    assert code == 4 and "landing-checks-failed" in out, out
    assert _run_row(env)["phase"] != "closed"
    assert env.con().execute("SELECT verdict FROM gates WHERE subject='landing-recovery'").fetchone()[0] == "CHANGES_REQUIRED"
    flag.write_text("yes")
    env.office("close", "--landed-externally", "2", check=0)
    assert env.con().execute("SELECT count(*) FROM gates WHERE subject='landing-recovery'").fetchone()[0] == 2


def test_current_review_gates_cannot_be_bypassed_by_squash_proof(env, monkeypatch):
    _interrupted(env, monkeypatch)
    con = env.con()
    con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at) "
                "SELECT 'Gpending', id, 'task', 'code_review', 'pending', 'running', '2026-01-01' FROM runs")
    con.commit()
    code, out = env.office("close", "--landed-externally", "2")
    assert code == 4 and "landing-unproven" in out, out
    assert con.execute("SELECT status FROM gates WHERE id='Gpending'").fetchone()[0] == "running"
    assert _run_row(env)["phase"] != "closed"


def test_amendment_during_checks_invalidates_the_close_proof(env, monkeypatch):
    _interrupted(env, monkeypatch)
    original = gates.run_commands
    def changed(con, run, *args, **kwargs):
        result = original(con, run, *args, **kwargs)
        with land.db.transaction(con):
            state.update_run(con, run["id"], requirements_version=run["requirements_version"] + 1)
        return result
    monkeypatch.setattr(gates, "run_commands", changed)
    code, out = env.office("close", "--landed-externally", "2")
    assert code == 4 and "landing-proof-stale" in out, out
    assert _run_row(env)["phase"] != "closed" and not _run_row(env).get("archive_digest")


def test_failed_fetch_never_uses_a_stale_default_ref(env, monkeypatch):
    _interrupted(env, monkeypatch)
    original = landing_recovery._git
    def failed(run, *args):
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 1, "", "origin unavailable")
        return original(run, *args)
    monkeypatch.setattr(landing_recovery, "_git", failed)
    code, out = env.office("close", "--landed-externally", "2")
    assert code == 4 and "landing-fetch-failed" in out, out
    assert _run_row(env)["phase"] != "closed"


def _interrupted_check_claim(env):
    con = env.con()
    run = _run_row(env)
    with db.transaction(con):
        con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at) "
                    "VALUES('Ginterrupted', ?, 'landing-recovery', 'checks', 'old', 'running', '2026-01-01')", (run["id"],))
        jid = state.enqueue(con, run, "landing_verify", {"gate_id": "Ginterrupted"}, dedup_key="interrupted", max_attempts=1)
        con.execute("UPDATE outbox SET status='claimed', claim_token='old-claim', attempts=1 WHERE id=?", (jid,))
    return jid, jobs.try_lock(run["id"], jid)


def test_interrupted_recovery_check_gate_is_reclaimed_before_fresh_evidence(env, monkeypatch):
    _interrupted(env, monkeypatch)
    jid, fd = _interrupted_check_claim(env)
    os.close(fd)  # the supervisor and every check process have exited
    env.office("close", "--landed-externally", "2", check=0)
    con = env.con()
    old = con.execute("SELECT status, verdict FROM gates WHERE id='Ginterrupted'").fetchone()
    assert tuple(old) == ("done", "UNAVAILABLE")
    assert state.get_job(con, jid)["status"] == "failed"
    assert _run_row(env)["landing"]["squash_proof"]["checks"]["gate_id"] != "Ginterrupted"


def test_a_live_check_claim_is_preserved_without_launching_duplicate_checks(env, monkeypatch):
    _interrupted(env, monkeypatch)
    jid, fd = _interrupted_check_claim(env)
    try:
        code, out = env.office("close", "--landed-externally", "2")
        assert code == 4 and "landing-checks-running" in out, out
        assert state.get_job(env.con(), jid)["status"] == "claimed"
        assert env.con().execute("SELECT count(*) FROM outbox WHERE kind='landing_verify'").fetchone()[0] == 1
    finally:
        os.close(fd)
    assert _run_row(env)["phase"] != "closed"


@pytest.mark.review_contract("v3.1")
def test_squash_close_preserves_the_pinned_v31_review_contract(env, monkeypatch):
    import test_land
    monkeypatch.setattr(test_land, "SCRIPT", {**test_land.SCRIPT,
        "code_reviewer": [{"reply": "VERDICT: PASS"}, {"reply": "VERDICT: PASS"}]})
    test_owned_squash_pr_close_proves_heads_and_checks_without_fake_ancestry_or_consent(env, monkeypatch)


@pytest.mark.parametrize("failure", ["UNAVAILABLE", "STALE"])
def test_missing_commands_or_changed_checkout_never_complete_the_proof(env, monkeypatch, failure):
    _interrupted(env, monkeypatch)
    original = gates._run_check
    def failed(command, cwd, timeout, environment):
        assert jobs.attempt_lock_fds(), "the check must inherit its outbox attempt's execution lock"
        if failure == "UNAVAILABLE":
            return 127, "command not found"
        result = original(command, cwd, timeout, environment)
        (cwd / "calc.py").write_text("def add(a, b): return 0\n")
        return result
    monkeypatch.setattr(gates, "_run_check", failed)
    code, out = env.office("close", "--landed-externally", "2")
    assert code == 4 and "landing-checks-failed" in out, out
    gate = env.con().execute("SELECT verdict FROM gates WHERE subject='landing-recovery'").fetchone()[0]
    assert gate == failure and _run_row(env)["phase"] != "closed"


def test_e2e_reentry_after_deploy_does_not_merge_or_deploy_twice(env, monkeypatch):
    marker = env.tmp / "deploy-count"
    _run(env, monkeypatch, _plan("e2e", prod=f"printf deployed >> {marker}", verify=f"test -f {marker}"), repo=SQUASH_ONLY)
    original = land._record
    with monkeypatch.context() as patch:
        def interrupted(con, run, **values):
            if "merged" in values:
                raise Refused("post-deploy-interrupted", "stopped before recording final landing")
            return original(con, run, **values)
        patch.setattr(land, "_record", interrupted)
        code, out = env.office("land")
    assert code == 4 and "post-deploy-interrupted" in out, out
    before = env.con().execute("SELECT count(*) FROM authorizations").fetchone()[0]
    env.office("land", check=0)
    env.office("close", check=0)
    assert marker.read_text() == "deployed"
    assert sum(c[:2] == ["pr", "merge"] for c in gh(env)["calls"]) == 2
    assert env.con().execute("SELECT count(*) FROM authorizations").fetchone()[0] == before
