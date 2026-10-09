"""Issue-only bug observer: persistence, non-blocking audits, privacy and publishing."""
from __future__ import annotations

import json
import sqlite3
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
    # Redact before truncating, including credentials that cross the old input cutoff.
    public = bugwatch.sanitize("x " * 880 + "token=private-value")
    assert "private" not in public


def test_nonblocking_attempt_and_persistent_failure(env, monkeypatch):
    con=env.con()
    try:
        _event(con,"run-2",1,"job.failed","runtime internal scheduler failed")
        monkeypatch.setattr(bugwatch,"start_reporter",lambda:None)
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
        assert con.execute("SELECT status FROM self_improve_incidents").fetchone()[0]=="pending"
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
