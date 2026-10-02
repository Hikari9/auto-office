"""Closing a run whose work landed through a PR Office did not open (#239)."""
from __future__ import annotations

import json

from conftest import GOOD_ADD, approved_run, task_row


def _gh(env, state: str, oid: str):
    (env.bin / "gh").write_text(f"#!/bin/sh\necho '{json.dumps({'state': state, 'url': 'https://github.com/o/r/pull/7', 'number': 7, 'mergeCommit': {'oid': oid}})}'\n")
    (env.bin / "gh").chmod(0o755)


def _blocked_run(env):
    """T1 accepted; a review Office still waits on blocks a normal close."""
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    con = env.con()
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, round, escalated, created_at) "
                "SELECT 'Gstuck', id, 'T1', 'T1', 'code_review', 'k', 'running', 1, 0, '2026-01-01' FROM runs")
    con.commit()
    code, out = env.office("close", "--handoff", "https://github.com/o/r/pull/7")
    assert code == 4 and "close-blocked" in out, out


def _accepted_commit(env):
    return env.con().execute("SELECT r.commit_sha FROM tasks t JOIN revisions r ON r.id=t.accepted_revision_id").fetchone()[0]


def test_merged_pr_containing_the_work_closes_as_landed(env):
    _blocked_run(env)
    env.git("merge", "--no-ff", "-qm", "external PR", _accepted_commit(env))
    _gh(env, "MERGED", env.git("rev-parse", "HEAD").strip())
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/7")
    assert code == 0 and "landed externally via https://github.com/o/r/pull/7" in out, out
    run = env.con().execute("SELECT phase, terminal_reason, landing_json FROM runs").fetchone()
    assert run["phase"] == "closed" and run["terminal_reason"].startswith("landed externally")
    assert json.loads(run["landing_json"])["contains"] == ["T1"]
    assert env.con().execute("SELECT status FROM gates WHERE id='Gstuck'").fetchone()[0] == "cancelled"
    assert "abandoned" not in [r[0] for r in env.con().execute("SELECT label FROM outcome_labels")]


def test_merged_pr_without_the_work_needs_the_users_words(env):
    _blocked_run(env)
    (env.repo / "calc.py").write_text(GOOD_ADD + "# squashed elsewhere\n")
    env.git("commit", "-qam", "squash of the run's work")
    _gh(env, "MERGED", env.git("rev-parse", "HEAD").strip())
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/7")
    assert code == 4 and "landing-unproven" in out and "does not contain T1" in out, out
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/7",
                           "--quote", "PR 7 squashed this run's work")
    assert code == 0 and "closed on the user's word: T1" in out, out
    con = env.con()
    assert con.execute("SELECT quote FROM authorizations WHERE kind='landing'").fetchone()[0] == "PR 7 squashed this run's work"


def test_open_pr_is_refused(env):
    _blocked_run(env)
    _gh(env, "OPEN", "")
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/7", "--quote", "x")
    assert code == 4 and "pr-not-merged" in out, out
