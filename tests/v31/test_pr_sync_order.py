"""#324: a task's pr_sync jobs run in order, and a GitHub error is a retry, never a recorded success."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from conftest import GOOD_ADD, start_inline
from test_task_prs import github, gh


def _drain(con, skip=("pr_sync",)):
    """Run every queued job except `skip` kinds, until none is left."""
    from office import jobs
    while True:
        row = next((r for r in con.execute("SELECT id, kind FROM outbox WHERE status='queued' "
                                           "AND (not_before IS NULL OR not_before<=? ) ORDER BY created_at",
                                           (jobs.now_iso(),)).fetchall() if r["kind"] not in skip), None)
        if row is None:
            return
        jobs.execute(con, row["id"])


def _pr_jobs(con) -> list[dict]:
    return [{**dict(r), "event": json.loads(r["payload_json"])["event"]}
            for r in con.execute("SELECT * FROM outbox WHERE kind='pr_sync' ORDER BY created_at, rowid").fetchall()]


def row_created(con, job_id):
    return con.execute("SELECT created_at FROM outbox WHERE id=?", (job_id,)).fetchone()[0]


def _job(jobs, event):
    return next(j for j in jobs if j["event"] == event)


def _accepted_task_with_queued_pr_jobs(env, monkeypatch, **gh_state):
    github(env, monkeypatch, **gh_state)
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env)
    env.office("approve", "plan", "--quote", "go", check=0)
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    env.office("dispatch", "T1", check=0)
    con = env.con()
    _drain(con)
    assert con.execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] == "accepted"
    jobs = _pr_jobs(con)
    assert jobs[0]["event"] == "revision" and "accepted" in {j["event"] for j in jobs}, [j["event"] for j in jobs]
    return con, jobs


def test_no_pr_job_is_claimed_while_an_earlier_one_for_the_task_is_queued_or_claimed(env, monkeypatch):
    """The #324 order: the revision job is claimed (mid-push), the accepted job gets its turn first."""
    from office import jobs as jobmod
    from office.util import claim_identity
    con, queued = _accepted_task_with_queued_pr_jobs(env, monkeypatch)
    revision, accepted = queued[0], _job(queued, "accepted")
    con.execute("UPDATE outbox SET status='claimed', claimed_pid=?, claimed_by=?, attempts=1 WHERE id=?",
                (os.getpid(), claim_identity(os.getpid()), revision["id"]))
    assert jobmod.execute(con, accepted["id"]) == 0
    row = con.execute("SELECT status, attempts FROM outbox WHERE id=?", (accepted["id"],)).fetchone()
    assert (row["status"], row["attempts"]) == ("queued", 0), dict(row)
    assert not (env.tmp / "gh.json").exists() or gh(env)["prs"] == []
    # An earlier job that is queued but backing off blocks its successors too, and neither kick nor drain picks one.
    con.execute("UPDATE outbox SET status='queued', claimed_pid=NULL, not_before='2999-01-01T00:00:00+00:00' WHERE id=?",
                (revision["id"],))
    assert jobmod.execute(con, accepted["id"]) == 0
    assert jobmod.run_pending(con) == 0
    assert con.execute("SELECT COUNT(*) FROM outbox WHERE kind='pr_sync' AND status='done'").fetchone()[0] == 0
    # Another task's jobs are not held back.
    assert con.execute("SELECT COUNT(*) FROM outbox WHERE kind='pr_sync' AND status='queued'").fetchone()[0] == len(queued)


def test_the_324_order_ends_with_the_pr_ready(env, monkeypatch):
    from office import jobs as jobmod
    from office.util import claim_identity
    con, queued = _accepted_task_with_queued_pr_jobs(env, monkeypatch)
    revision = queued[0]
    con.execute("UPDATE outbox SET status='claimed', claimed_pid=?, claimed_by=?, attempts=1 WHERE id=?",
                (os.getpid(), claim_identity(os.getpid()), revision["id"]))
    for job in (_job(queued, "accepted"), *[j for j in queued[1:] if j["event"] != "accepted"]):
        jobmod.execute(con, job["id"])  # accepted first, as the racing processes did
    con.execute("UPDATE outbox SET status='queued', claimed_pid=NULL, claimed_by=NULL, attempts=0 WHERE id=?", (revision["id"],))
    jobmod.run_pending(con)
    assert [r["status"] for r in con.execute("SELECT status FROM outbox WHERE kind='pr_sync' ORDER BY created_at")] \
        == ["done"] * len(queued)
    (pr,) = gh(env)["prs"]
    assert pr["draft"] is False, pr
    assert any("accepted" in c for c in pr["comments"]), pr["comments"]
    # Run in order: the revision created the one PR, the accepted job only readied it.
    assert [c[:2] for c in gh(env)["calls"] if c[:2] == ["pr", "create"]] == [["pr", "create"]]


def test_an_accepted_job_that_finds_no_pr_beside_a_pending_revision_is_deferred_not_skipped(env, monkeypatch):
    """Even queued out of order (accepted first), it waits for the revision sync instead of finishing as skipped."""
    from office import db, jobs as jobmod
    con, queued = _accepted_task_with_queued_pr_jobs(env, monkeypatch)
    revision, accepted = queued[0], _job(queued, "accepted")
    # Make the revision job the later one, so ordering alone does not hold the accepted job back.
    just_after = (datetime.fromisoformat(row_created(con, accepted["id"])) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds")
    with db.transaction(con):
        con.execute("UPDATE outbox SET created_at=? WHERE id=?", (just_after, revision["id"]))
    assert jobmod.execute(con, accepted["id"]) == 0
    row = con.execute("SELECT status, attempts, not_before, result_json, error FROM outbox WHERE id=?",
                      (accepted["id"],)).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 0 and row["not_before"], dict(row)
    assert row["result_json"] is None and "revision" in row["error"], dict(row)
    assert not (env.tmp / "gh.json").exists() or gh(env)["prs"] == []
    # It went to the back of the task's queue, so the revision sync is no longer held up by it.
    assert row_created(con, accepted["id"]) > row_created(con, revision["id"])
    with db.transaction(con):
        con.execute("UPDATE outbox SET not_before=NULL WHERE id=?", (accepted["id"],))
    jobmod.run_pending(con)
    assert gh(env)["prs"][0]["draft"] is False
    assert [c[:2] for c in gh(env)["calls"] if c[:2] == ["pr", "create"]] == [["pr", "create"]]


def test_a_failing_ready_raises_retries_and_emits_pr_error(env, monkeypatch):
    from office import jobs as jobmod
    con, queued = _accepted_task_with_queued_pr_jobs(env, monkeypatch, fail={"pr ready": 1})
    jobmod.run_pending(con)
    accepted = con.execute("SELECT * FROM outbox WHERE id=?", (_job(queued, "accepted")["id"],)).fetchone()
    assert accepted["status"] == "queued" and accepted["attempts"] == 1 and "gh pr ready failed" in accepted["error"], dict(accepted)
    assert gh(env)["prs"][0]["draft"] is True  # nothing failed is recorded as success
    errors = con.execute("SELECT summary FROM events WHERE kind='pr.error'").fetchall()
    assert len(errors) == 1 and "gh pr ready failed" in errors[0]["summary"], [dict(e) for e in errors]
    con.execute("UPDATE outbox SET not_before=NULL WHERE id=?", (accepted["id"],))
    jobmod.run_pending(con)
    assert con.execute("SELECT status FROM outbox WHERE id=?", (accepted["id"],)).fetchone()[0] == "done"
    assert gh(env)["prs"][0]["draft"] is False


def test_a_failing_comment_or_lookup_is_not_a_success_either(env, monkeypatch):
    from office import jobs as jobmod
    con, queued = _accepted_task_with_queued_pr_jobs(env, monkeypatch, fail={"pr comment": -1, "pr list": 1})
    jobmod.run_pending(con)
    first = con.execute("SELECT status, error FROM outbox WHERE id=?", (queued[0]["id"],)).fetchone()
    assert first["status"] == "queued" and "gh pr list failed" in first["error"], dict(first)
    assert gh(env)["prs"] == []  # a failed lookup did not read as "no PR" and open one
    for _ in range(3):  # the verdict and accepted comments fail for good; the jobs end failed, not done
        con.execute("UPDATE outbox SET not_before=NULL WHERE status='queued'")
        jobmod.run_pending(con)
    states = {j["event"]: con.execute("SELECT status FROM outbox WHERE id=?", (j["id"],)).fetchone()[0]
              for j in _pr_jobs(con)}
    assert states["accepted"] == "failed" and "done" not in {s for e, s in states.items() if e != "revision"}, states
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='pr.error'").fetchone()[0] >= 2


def _draft_stack(env, monkeypatch, **gh_state):
    """An accepted two-task stack whose PRs are still drafts (what #324 left behind)."""
    from test_land import SCRIPT
    from test_task_prs import PLAN_STACKED
    github(env, monkeypatch, **gh_state)
    env.trust()
    env.script(**SCRIPT)
    start_inline(env, plan=PLAN_STACKED, extra=("--issue", "7"))
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", "T2", check=0)
    state = gh(env)
    assert [p["draft"] for p in state["prs"]] == [False, False]
    for p in state["prs"]:
        p["draft"] = True
    (env.tmp / "gh.json").write_text(json.dumps(state))


def test_land_marks_each_accepted_draft_ready_before_merging_it(env, monkeypatch):
    _draft_stack(env, monkeypatch)
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 0, out
    calls = [" ".join(c[:3]) for c in gh(env)["calls"] if c[:2] in (["pr", "ready"], ["pr", "merge"])][-4:]
    assert calls == ["pr ready 1", "pr merge 1", "pr ready 2", "pr merge 2"], calls
    assert [p["state"] for p in gh(env)["prs"]] == ["merged", "merged"]


def test_land_refuses_when_a_pr_cannot_be_readied_and_merges_nothing(env, monkeypatch):
    _draft_stack(env, monkeypatch)
    state = gh(env)
    state["fail"] = {"pr ready": -1}
    (env.tmp / "gh.json").write_text(json.dumps(state))
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 4 and "ready-failed" in out and "could not mark #1 (T1) ready" in out, out
    assert [p["state"] for p in gh(env)["prs"]] == ["open", "open"]
