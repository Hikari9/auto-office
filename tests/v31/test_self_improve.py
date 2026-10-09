"""Issue-only bug observer: persistence, non-blocking audits, privacy and publishing."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from office import bugwatch, db, version


def _event(con, run_id, seq, kind, summary):
    con.execute("INSERT INTO events(run_id,kind,audience,summary,payload_json,office_version,created_at) "
                "VALUES(?,?,?,?,?,?,datetime('now'))",
                (run_id,kind,"orchestrator",summary,"{}",version.current()))


def test_schema_and_runwide_backfill_across_subagents(env):
    con=env.con()
    try:
        _event(con,"run-1",1,"task.blocked","executor could not launch")
        _event(con,"run-1",2,"job.failed","agent relay failed unexpectedly")
        _event(con,"run-1",3,"gate.unavailable","review channel crashed")
        _event(con,"run-1",4,"quota-probe-unknown","external quota unknown")
        assert bugwatch.capture(con,"run-1") == 0  # not yet armed
        assert bugwatch.capture(con,"run-1",force=True) == 4  # catch-up on land/close
        first=con.execute("SELECT cursor,armed FROM self_improve_runs WHERE run_id='run-1'").fetchone()
        assert (first["cursor"],first["armed"])==(4,0)
        assert con.execute("SELECT COUNT(*) FROM self_improve_incidents").fetchone()[0] == 3
        bugwatch.arm(con,"run-1")
        _event(con,"run-1",5,"dispatch.failed","second subagent model adapter mismatch")
        assert bugwatch.capture(con,"run-1") == 1
        assert bugwatch.capture(con,"run-1") == 0  # idempotent cursor
        assert con.execute("SELECT COUNT(*) FROM self_improve_incidents").fetchone()[0] == 4
    finally:
        con.close()


def test_redaction_removes_private_identifiers():
    raw="worker crashed at /Users/rico/company/foo key=secret token=topsecret mail=me@private.org https://secret.example/path"
    public=bugwatch.sanitize(raw)
    for private in ("/Users", "rico", "topsecret", "me@", "https://secret"):
        assert private not in public
    assert "private value" not in bugwatch.sanitize('{"token": "private value"}')
    assert bugwatch.sanitize("/Users/rico/company/file") == "[redacted-path]"
    assert bugwatch.sanitize(r"C:\Users\rico\company\file") == "[redacted-path]"
    # Redact before truncating, including credentials that cross the old input cutoff.
    public = bugwatch.sanitize("x " * 880 + "token=private-value")
    assert "private" not in public


def test_nonblocking_attempt_and_persistent_failure(env, monkeypatch):
    con=env.con()
    try:
        _event(con,"run-2",1,"job.failed","runtime internal scheduler failed")
        monkeypatch.setattr(bugwatch,"start_reporter",lambda *a,**k:None)
        # A refused gate is not a bug, but the attempt and pre-existing event are recorded.
        note=bugwatch.lifecycle_attempt("run-2","close","refused","close-blocked")
        assert "pending" in note
        assert con.execute("SELECT COUNT(*) FROM self_improve_attempts").fetchone()[0]==1
        # A model failure is retried and does not affect lifecycle success.
        incident=dict(con.execute("SELECT * FROM self_improve_incidents").fetchone())
        monkeypatch.setattr(bugwatch,"investigate",lambda *a:(_ for _ in ()).throw(RuntimeError("agent unavailable")))
        bugwatch._process(con,incident)
        row=con.execute("SELECT status,attempts,next_retry_at FROM self_improve_incidents").fetchone()
        assert row["status"]=="retry" and row["attempts"]==1 and row["next_retry_at"]
        assert con.execute("SELECT fingerprint FROM self_improve_incidents").fetchone()
    finally:
        # db.connect monkeypatch is removed automatically when the test ends.
        con.close()


def test_weak_suspicion_retained_without_publishing(env,monkeypatch):
    con=env.con()
    try:
        bugwatch._record(con,"run-3","job.failed","weird unrelated external failure","event:1")
        row=dict(con.execute("SELECT * FROM self_improve_incidents").fetchone())
        monkeypatch.setattr(bugwatch,"investigate",lambda *a:{"confidence":"weak","title":"unclear",
                            "actual":"unconfirmed","evidence":"limited","expected":"none","reproduction":""})
        monkeypatch.setattr(bugwatch,"publish",lambda *a:(_ for _ in ()).throw(AssertionError("must not publish")))
        bugwatch._process(con,row)
        assert con.execute("SELECT status FROM self_improve_incidents").fetchone()[0]=="suspected"
        bugwatch._record(con,"run-3","job.failed","weird unrelated external failure","event:2")
        again=con.execute("SELECT status,report_json FROM self_improve_incidents").fetchone()
        assert again["status"]=="pending" and again["report_json"] is None
    finally:
        con.close()


def test_issue_only_publisher_deduplicates_and_redacts(env,monkeypatch):
    calls=[]
    incident={"fingerprint":"a"*64,"summary":"adapter crashed","kind":"dispatch.failed","occurrences":2}
    report={"confidence":"strong","title":"model adapter refused valid route","expected":"launch succeeds",
            "actual":"launch fails","evidence":"dispatch error captured","reproduction":"intermittent"}
    def fake(*args):
        calls.append(args)
        if args[0]=="api":
            assert "--paginate" in args and "--slurp" in args
            return "[[]]"
        assert args[0:2]==("issue","create")
        assert "--repo" in args and "Hikari9/auto-office" in args
        assert "--body-file" in args and "--title" in args
        from pathlib import Path
        body=Path(args[args.index("--body-file")+1]).read_text()
        assert "<!-- auto-self-improve:" in body and "dispatch error captured" in body
        return "https://github.com/Hikari9/auto-office/issues/999\n"
    monkeypatch.setattr(bugwatch,"_gh",fake)
    assert bugwatch.publish(incident,report).endswith("/999")
    assert len(calls)==2 and not any("pr" in c or "push" in c for call in calls for c in call)
    calls.clear()
    monkeypatch.setattr(bugwatch,"_gh",lambda *a:json.dumps([[{"title":"different",
                    "body":"<!-- auto-self-improve:"+"a"*64+" -->", "html_url":"https://github.com/Hikari9/auto-office/issues/999"}]]))
    assert bugwatch.publish(incident,report).endswith("/999")


def test_dedup_searches_later_pages_and_ignores_pull_requests(monkeypatch):
    fingerprint = "b" * 64
    marker = f"<!-- auto-self-improve:{fingerprint} -->"
    def gh(*args):
        assert args[0] == "api"  # a match on a later page must prevent creation
        return json.dumps([
            [{"pull_request": {}, "body": marker, "html_url": "pr-url"}],
            [{"body": marker, "html_url": "closed-issue-url"}],
        ])
    monkeypatch.setattr(bugwatch, "_gh", gh)
    assert bugwatch.publish({"fingerprint": fingerprint}, {"title": "defect"}) == "closed-issue-url"


def test_pruneable_details_not_needed_for_retry(env):
    con=env.con()
    try:
        bugwatch._record(con,"run-4","job.failed","crashed subprocess","event:2")
        assert con.execute("SELECT COUNT(*) FROM self_improve_incidents").fetchone()[0]==1
        # The run's prunable recorder rows and state directory can disappear.
        con.execute("DELETE FROM events WHERE run_id='run-4'")
        con.execute("DELETE FROM outbox WHERE run_id='run-4'")
        assert bugwatch._due(con) and bugwatch._due(con)[0]["summary"]=="crashed subprocess"
    finally:
        con.close()


def test_repeat_weak_incident_is_reinvestigated(env, monkeypatch):
    con = env.con()
    try:
        bugwatch._record(con, "r", "job.failed", "relay crashed", "event:1")
        reports = iter([
            {"confidence": "weak", "title": "unclear"},
            {"confidence": "strong", "title": "relay defect"},
        ])
        monkeypatch.setattr(bugwatch, "investigate", lambda *a: next(reports))
        monkeypatch.setattr(bugwatch, "publish", lambda *a: "issue-url")
        bugwatch._process(con, dict(con.execute("SELECT * FROM self_improve_incidents").fetchone()))
        bugwatch._record(con, "r", "job.failed", "relay crashed", "event:2")
        row = dict(con.execute("SELECT * FROM self_improve_incidents").fetchone())
        assert row["report_json"] is None
        bugwatch._process(con, row)
        assert con.execute("SELECT status FROM self_improve_incidents").fetchone()[0] == "filed"
    finally:
        con.close()


def test_dead_jobs_and_subagents_without_events_are_captured_once(env):
    con=env.con()
    try:
        con.execute("INSERT INTO outbox(id,run_id,kind,dedup_key,payload_json,office_version,status,error,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,datetime('now'))",
                    ("job1","run-5","launch_agent","test-job1","{}",version.current(),"failed","child crashed"))
        con.execute("INSERT INTO dispatches(id,run_id,status,terminal_classification,exit_code) "
                    "VALUES(?,?,?,?,?)",("disp1","run-5","failed","crashed",1))
        assert bugwatch.capture(con,"run-5",force=True)==0
        assert con.execute("SELECT COUNT(*) FROM self_improve_incidents").fetchone()[0]==2
        assert bugwatch.capture(con,"run-5",force=True)==0
        assert con.execute("SELECT SUM(occurrences) FROM self_improve_incidents").fetchone()[0]==2
    finally:
        con.close()


def test_worker_drains_more_than_one_batch(env, monkeypatch):
    con = env.con()
    for i in range(23):
        bugwatch._record(con, "r", "job.failed", f"relay {i} crashed", "event:1")
    monkeypatch.setattr(bugwatch, "investigate", lambda *a: {"confidence": "none"})
    assert bugwatch.worker() == 0
    assert con.execute("SELECT COUNT(*) FROM self_improve_incidents WHERE status='dismissed'").fetchone()[0] == 23
    con.close()


def test_reporter_launch_failures_are_nonblocking(monkeypatch):
    from office import frontdoor
    monkeypatch.setattr(frontdoor, "current_argv", lambda: (_ for _ in ()).throw(ValueError("bad runtime")))
    bugwatch.start_reporter()


def test_investigator_has_no_tools_or_inherited_authority(env, monkeypatch):
    from office import adapters
    adapter = adapters.load_all()["claude"]
    monkeypatch.setattr(bugwatch, "_route", lambda con: (
        {"invocation_model_id": "test-model", "effort": "low"}, adapter))
    monkeypatch.setenv("GH_TOKEN", "private-token")
    monkeypatch.setenv("OFFICE_STATE_HOME", "/private/state")
    monkeypatch.setenv("DATABASE_URL", "private-database")
    def run(argv, **kwargs):
        assert argv[argv.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in argv
        assert "--bare" in argv
        assert "--add-dir" not in argv
        assert "--disable-slash-commands" in argv
        assert "disableAllHooks" in argv[argv.index("--settings") + 1]
        assert not {"GH_TOKEN", "OFFICE_STATE_HOME", "DATABASE_URL"} & kwargs["env"].keys()
        return type("Proc", (), {"returncode": 0, "stdout": json.dumps({
            "confidence": "strong", "title": "relay defect", "actual": "crashed", "evidence": "observed error"})})()
    monkeypatch.setattr(bugwatch.subprocess, "run", run)
    assert bugwatch.investigate(None, {"kind": "job.failed", "summary": "relay crashed",
                                      "origin": "event:1", "occurrences": 1})["confidence"] == "strong"


def test_worker_drains_more_than_ten_ready_incidents(env, monkeypatch):
    con=env.con()
    try:
        for i in range(12):
            bugwatch._record(con,'run-batch','job.failed',f'failure case {i}',f'event:{i}')
        monkeypatch.setattr(bugwatch,'investigate',lambda *args:{'confidence':'strong','title':'test defect',
                            'expected':'success','actual':'failed','evidence':'recorded failure','reproduction':''})
        monkeypatch.setattr(bugwatch,'publish',lambda incident, report:
                            'https://github.com/Hikari9/auto-office/issues/999')
        assert bugwatch.worker()==0
        assert con.execute("SELECT COUNT(*) FROM self_improve_incidents WHERE status='filed'").fetchone()[0]==12
    finally:
        con.close()


# ------------------------------------------- bounded reporter lifetime (#501)

def _queue_retry(con, summary: str) -> None:
    """A queued incident whose next retry is an hour out: a real reporter
    sleeps on it without reaching any model or GitHub."""
    bugwatch._record(con, "run-501", "job.failed", summary, "event:1")
    con.execute("UPDATE self_improve_incidents SET status='retry',attempts=1,next_retry_at=?",
                ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),))


def test_reporter_spawn_requires_free_ownership_and_pending_work(env, monkeypatch):
    """A wake-up spawns nothing when a reporter owns the home's lock or when
    no incident awaits work; an idle lock with work spawns exactly one."""
    from office import frontdoor
    con = env.con()
    try:
        launched = []
        monkeypatch.setattr(frontdoor, "current_argv", lambda: (["office"], {}))
        monkeypatch.setattr(bugwatch.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
        bugwatch.start_reporter(con)  # no incident is waiting
        assert launched == []
        _queue_retry(con, "relay route unavailable")
        held = open(env.state / "self-improve.lock", "a+")
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            bugwatch.start_reporter(con)  # work exists, but the home is owned
            assert launched == []
        finally:
            held.close()  # releases the ownership lock
        bugwatch.start_reporter(con)
        assert len(launched) == 1 and launched[0][-1] == "_self_improve"
        bugwatch.start_reporter()  # a wake-up without a connection cannot gate on work
        assert len(launched) == 2
        assert os.environ.get(bugwatch.SELF_ENV) != "1"  # the guard stays for the spawned process only
    finally:
        con.close()


def test_detached_reporters_stay_bounded_and_leave_with_their_home(env):
    """#501: racing wakeups leave at most one live reporter per home, and a
    disposable state home that its owner deletes takes its reporter with it.
    The pending retry receipt survives for a later Office invocation."""
    con = env.con()
    _queue_retry(con, "relay route unavailable")
    con.close()
    env_probe = {**os.environ, "OFFICE_SELF_IMPROVE_MAX_SECONDS": "120"}
    workers = [subprocess.Popen([sys.executable, "-m", "office", "_self_improve"], cwd=str(env.repo),
                                env=env_probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for _ in range(4)]
    try:
        deadline = time.monotonic() + 30
        alive = workers
        while time.monotonic() < deadline:
            alive = [w for w in workers if w.poll() is None]
            if len(alive) <= 1:
                break
            time.sleep(0.2)
        assert len(alive) == 1, "the per-home lock bounds racing wakeups to one live reporter"
        # The owner releases the disposable home (the test's teardown, in real life).
        shutil.rmtree(env.state)
        assert alive[0].wait(timeout=20) == 0, "the reporter leaves when its home is released"
    finally:
        for w in workers:
            if w.poll() is None:
                w.kill()
        for w in workers:
            w.wait()
    check = env.con()
    try:
        row = check.execute("SELECT status,next_retry_at FROM self_improve_incidents").fetchone()
        assert row["status"] == "retry" and row["next_retry_at"]  # receipts durable across reclamation
    finally:
        check.close()


def test_reporter_lifetime_is_bounded_even_while_its_home_lives(env):
    """A reporter with endless queued retries still exits on its lifetime
    bound; nothing is lost and no model or GitHub was reached."""
    con = env.con()
    _queue_retry(con, "reporter route unavailable")
    con.close()
    worker = subprocess.Popen([sys.executable, "-m", "office", "_self_improve"], cwd=str(env.repo),
                              env={**os.environ, "OFFICE_SELF_IMPROVE_MAX_SECONDS": "2"},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert worker.wait(timeout=25) == 0
        assert env.state.exists()  # it exited on the bound, not on home deletion
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
    check = env.con()
    try:
        row = check.execute("SELECT status,next_retry_at FROM self_improve_incidents").fetchone()
        assert row["status"] == "retry" and row["next_retry_at"]
    finally:
        check.close()
