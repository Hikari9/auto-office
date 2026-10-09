"""#324: a task's pr_sync jobs run in order, and a GitHub error is a retry, never a recorded success."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta

from conftest import GOOD_ADD, start_inline
from test_land_criss_cross import CONFLICTING_GH, _criss_cross
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


def test_another_tasks_pr_jobs_are_not_held_back_by_a_backing_off_job(env, monkeypatch):
    from test_task_prs import PLAN_STACKED
    from test_land import SCRIPT
    from office import jobs as jobmod
    github(env, monkeypatch)
    env.trust()
    env.script(**SCRIPT)
    start_inline(env, plan=PLAN_STACKED, extra=("--issue", "7"))
    env.office("approve", "plan", "--quote", "go", check=0)
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    env.office("dispatch", "T1", "T2", check=0)
    con = env.con()
    _drain(con)
    by_task = {}
    for r in con.execute("SELECT * FROM outbox WHERE kind='pr_sync' ORDER BY created_at, rowid").fetchall():
        by_task.setdefault(json.loads(r["payload_json"])["task_id"], []).append(dict(r))
    assert set(by_task) == {"T1", "T2"}, list(by_task)
    # T1's first job is backing off after a failure; its later jobs wait for it, T2's do not.
    con.execute("UPDATE outbox SET attempts=1, not_before='2999-01-01T00:00:00+00:00' WHERE id=?", (by_task["T1"][0]["id"],))
    assert jobmod.execute(con, by_task["T1"][1]["id"]) == 0
    assert con.execute("SELECT status FROM outbox WHERE id=?", (by_task["T1"][1]["id"],)).fetchone()[0] == "queued"
    assert jobmod.execute(con, by_task["T2"][0]["id"]) == 0
    assert con.execute("SELECT status FROM outbox WHERE id=?", (by_task["T2"][0]["id"],)).fetchone()[0] == "done"
    jobmod.run_pending(con)
    statuses = {t: [con.execute("SELECT status FROM outbox WHERE id=?", (j["id"],)).fetchone()[0] for j in js]
                for t, js in by_task.items()}
    assert set(statuses["T2"]) == {"done"} and set(statuses["T1"]) == {"queued"}, statuses


def test_a_sync_stops_waiting_for_a_stuck_revision_sync(env, monkeypatch):
    """A revision job held `claimed` by a hung process does not defer the accepted job forever."""
    from office import jobs as jobmod, prs
    from office.util import claim_identity
    con, queued = _accepted_task_with_queued_pr_jobs(env, monkeypatch)
    revision, accepted = queued[0], _job(queued, "accepted")
    con.execute("UPDATE outbox SET status='claimed', claimed_pid=?, claimed_by=?, attempts=1 WHERE id=?",
                (os.getpid(), claim_identity(os.getpid()), revision["id"]))
    con.execute("UPDATE outbox SET created_at='2999-01-01T00:00:00+00:00' WHERE id=?", (revision["id"],))  # no ordering hold
    con.execute("UPDATE outbox SET created_at='1999-01-01T00:00:00+00:00' WHERE id=?", (accepted["id"],))
    con.execute("UPDATE outbox SET status='done' WHERE kind='pr_sync' AND status='queued' AND id<>?", (accepted["id"],))
    for n in range(1, prs.MAX_DEFERRALS + 1):
        con.execute("UPDATE outbox SET not_before=NULL WHERE id=?", (accepted["id"],))
        assert jobmod.execute(con, accepted["id"]) == 0
        row = con.execute("SELECT status, payload_json, error FROM outbox WHERE id=?", (accepted["id"],)).fetchone()
        assert row["status"] == "queued" and json.loads(row["payload_json"])["deferrals"] == n, dict(row)
        assert row["error"].startswith("deferred:"), dict(row)
    con.execute("UPDATE outbox SET not_before=NULL WHERE id=?", (accepted["id"],))
    jobmod.execute(con, accepted["id"])
    assert con.execute("SELECT status FROM outbox WHERE id=?", (accepted["id"],)).fetchone()[0] == "done"
    (pr,) = gh(env)["prs"]
    assert pr["draft"] is False, pr


def test_a_verdict_sync_fails_visibly_once_its_waiting_is_capped(env, monkeypatch):
    from office import jobs as jobmod, prs
    from office.util import claim_identity
    con, queued = _accepted_task_with_queued_pr_jobs(env, monkeypatch)
    revision, verdict = queued[0], _job(queued, "verdict")
    con.execute("UPDATE outbox SET status='claimed', claimed_pid=?, claimed_by=?, attempts=1, created_at='2999-01-01T00:00:00+00:00' "
                "WHERE id=?", (os.getpid(), claim_identity(os.getpid()), revision["id"]))
    con.execute("UPDATE outbox SET created_at='1999-01-01T00:00:00+00:00', payload_json=json_set(payload_json,'$.deferrals',?) "
                "WHERE id=?", (prs.MAX_DEFERRALS, verdict["id"]))
    assert jobmod.execute(con, verdict["id"]) == 1
    row = con.execute("SELECT status, attempts, error FROM outbox WHERE id=?", (verdict["id"],)).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 1 and "still pending" in row["error"], dict(row)
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='pr.error'").fetchone()[0] == 1


def test_pr_sync_jobs_get_enough_attempts_for_a_github_outage(env, monkeypatch):
    con, queued = _accepted_task_with_queued_pr_jobs(env, monkeypatch)
    assert {j["max_attempts"] for j in queued} == {5}, [j["max_attempts"] for j in queued]


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
    done = {j["event"]: con.execute("SELECT finished_at FROM outbox WHERE id=?", (j["id"],)).fetchone()[0] for j in queued}
    assert done["revision"] <= done["accepted"], done  # the accepted job finished after the revision, not as a no-op
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
    for _ in range(5):  # the verdict and accepted comments fail for good; the jobs end failed, not done
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


READY_AFTER_SETTLE_GH = CONFLICTING_GH.replace(
    "fake_gh.git_merge = guarded\n",
    """fake_gh.git_merge = guarded
_main = fake_gh.main


def logged(argv):
    if argv[:2] == ["pr", "ready"]:  # record how many merge bases the branch had when it was readied
        origin = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
        head = "refs/heads/" + json.load(open(os.environ["FAKE_GH_STATE"]))["prs"][int(argv[2]) - 1]["head"]
        n = len(subprocess.run(["git", "--git-dir", origin, "merge-base", "--all", head, "refs/heads/main"],
                               capture_output=True, text=True).stdout.split())
        open(os.environ["FAKE_GH_STATE"] + ".bases", "a").write(f"{argv[2]}:{n}\\n")
    return _main(argv)


fake_gh.main = logged
""").replace("import subprocess, sys", "import json, os, subprocess, sys")


def test_land_settles_a_criss_cross_before_it_readies_the_draft(env, monkeypatch):
    _draft_stack(env, monkeypatch)
    _criss_cross(env, env.tmp / "origin.git", clash=False)
    (env.bin / "gh").write_text(READY_AFTER_SETTLE_GH)
    code, out = env.office("land", "--merge", "--quote", "merge them")
    assert code == 0 and "several merge bases" in out, out
    assert [p["state"] for p in gh(env)["prs"]] == ["merged", "merged"]
    assert (env.tmp / "gh.json.bases").read_text().split()[0] == "1:1"  # settled (one base) by the time #1 was readied
