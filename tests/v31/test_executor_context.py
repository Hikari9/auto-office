"""#939: bounded executor sessions rotate with a durable, fenced handoff."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from office import briefs, db, dispatch, executor_context as cap, jobs, state
from office.util import dumps


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.org"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "file.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "file.txt").write_text("in progress\n")
    con = db.connect()
    con.execute(
        "INSERT INTO runs(id, office_version, phase, created_at, plan_version, repo_root, policy_json) "
        "VALUES('R939', ?, 'executing', 'now', 2, ?, ?)",
        (state.version.current(), str(repo),
         dumps({"executor_context": {"enabled": True, "threshold_tokens": 200000}})))
    con.execute(
        "INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, "
        "status, introduced_plan_version, contract_version, acceptance_version, current_dispatch_id, created_at, updated_at) "
        "VALUES('R939', 'T1', 'Context rotation', 'executor', '[\"src/**\"]', '[]', "
        "'[\"implement cap\", \"test cap\"]', '[\"pytest\"]', 'running', 2, 2, 2, 'DOLD', 'now', 'now')")
    con.execute(
        "INSERT INTO dispatches(id, run_id, role, kind, task_id, status, harness, model, session_id, "
        "worktree, branch, base_commit, started_at) "
        "VALUES('DOLD', 'R939', 'executor', 'executor', 'T1', 'running', 'claude', 'model', "
        "'old-session', ?, 'branch', ?, 'now')", (str(repo), base))
    con.execute("INSERT INTO findings(id, run_id, task_id, state, code, severity, summary, action, created_at) "
                "VALUES('F1', 'R939', 'T1', 'open', 'F1', 'high', 'Fix test', 'Add test', 'now')")
    con.commit()
    monkeypatch.setattr(jobs, "kick", lambda *_args, **_kwargs: 0)
    yield con, repo
    con.close()


def test_default_disabled_and_invalid_thresholds():
    assert cap.threshold({"policy": {}}) == 200_000
    assert cap.threshold({"policy": {"executor_context": {"threshold_tokens": 0}}}) == 0
    assert cap.threshold({"policy": {"executor_context": {"enabled": False}}}) == 0
    assert cap.threshold({"policy": {"executor_context": {"threshold_tokens": True}}}) == 200_000
    assert cap.threshold({"policy": {"executor_context": {"threshold_tokens": -1}}}) == 200_000


def test_codex_last_turn_not_cumulative():
    records = [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 1_000_000},
            "last_token_usage": {"input_tokens": 180_000, "output_tokens": 2_000}}}},
        {"type": "event_msg", "payload": {"type": "turn_complete"}},
    ]
    assert cap._codex_tokens("\n".join(json.dumps(r) for r in records)) == 182_000
    assert cap._codex_tokens('{"type":"assistant"}') is None


def test_claude_metric_bound_to_exact_session(tmp_path, monkeypatch):
    from office import ctx_alert
    path = tmp_path / "old-session.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 500, "cache_read_input_tokens": 199_500}}}) + "\n")
    monkeypatch.setattr(ctx_alert, "_transcript_path", lambda h, s: path if (h, s) == ("claude", "old-session") else None)
    assert cap.session_tokens({"harness": "claude", "session_id": "old-session"}) == 200_000
    assert cap.session_tokens({"harness": "claude", "session_id": "other-session"}) is None
    assert cap.session_tokens({"harness": "claude", "session_id": "../bad"}) is None
    assert cap.session_tokens({"harness": "unknown", "session_id": "old-session"}) is None


def test_threshold_queues_once_and_handoff_captures_wip_findings(tmp_path, monkeypatch, ledger):
    con, repo = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda d: 199_999)
    assert not cap.check("DOLD")
    assert con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    monkeypatch.setattr(cap, "session_tokens", lambda d: 200_001)
    assert cap.check("DOLD")
    assert not cap.check("DOLD")
    queued = con.execute("SELECT kind FROM outbox").fetchall()
    assert [r[0] for r in queued] == ["rotate_executor"]
    record = state.get_dispatch(con, "DOLD")
    report = json.loads(record["context_handoff_json"])
    assert report["phase"] == "requested"
    assert report["tokens"] == 200_001
    assert "file.txt" in report["state"]["working_tree"]
    assert "in progress" not in report["done"][0]  # never invent completion
    assert report["remaining"][0] == "implement cap"
    assert report["open_findings"][0]["code"] == "F1"
    path = cap.handoff_path(state.get_run(con, "R939"), "DOLD")
    assert json.loads(path.read_text())["tokens"] == 200_001
    assert path.stat().st_mode & 0o777 == 0o600


def test_fresh_rotation_requires_old_writer_exit_and_reuses_same_worktree(monkeypatch, ledger):
    con, repo = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    assert cap.check("DOLD")
    calls = []

    def stop(run, old):
        calls.append(("stop", old["id"]))
        con.execute("UPDATE dispatches SET ended_at='now', status='exited' WHERE id='DOLD'")
        return True, "verified no processes remain"

    def launch(conn, run, tid, **kwargs):
        calls.append(("launch", tid, kwargs))
        assert conn.execute("SELECT ended_at FROM dispatches WHERE id='DOLD'").fetchone()[0]
        assert (repo / "file.txt").read_text() == "in progress\n"
        assert kwargs["extra"]["context_handoff"]["state"]["working_tree"]
        state.update_task(conn, run["id"], tid, status="launching", current_dispatch_id="DNEW")
        return "DNEW"

    monkeypatch.setattr(dispatch, "stop_worker_tree", stop)
    monkeypatch.setattr(dispatch, "_launch_or_block", launch)
    result = cap.job_rotate(con, state.get_run(con, "R939"), {"payload": {"dispatch_id": "DOLD"}})
    assert result["replacement"] == "DNEW"
    assert [c[0] for c in calls] == ["stop", "launch"]
    assert state.get_task(con, "R939", "T1")["current_dispatch_id"] == "DNEW"
    assert cap.recent(con, state.get_run(con, "R939"))[0]["phase"] == "restarted"
    assert cap.recent(con, state.get_run(con, "R939"))[0]["new_dispatch_id"] == "DNEW"
    assert not cap.check("DOLD")


def test_no_overlap_when_old_process_not_confirmed_stopped(monkeypatch, ledger):
    con, repo = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    assert cap.check("DOLD")
    monkeypatch.setattr(dispatch, "stop_worker_tree", lambda *_: (False, "unattributed writer still running"))
    monkeypatch.setattr(dispatch, "_launch_or_block", lambda *_a, **_k: pytest.fail("must not launch"))
    result = cap.job_rotate(con, state.get_run(con, "R939"), {"payload": {"dispatch_id": "DOLD"}})
    assert "unattributed writer" in result["blocked"]
    assert state.get_task(con, "R939", "T1")["status"] == "blocked"
    assert cap.recent(con, state.get_run(con, "R939"))[0]["phase"] == "blocked"


def test_normal_worker_exit_does_not_confuse_context_rotation_with_route_failure(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    assert cap.check("DOLD")
    monkeypatch.setattr(dispatch, "_launch_or_block", lambda *_a, **_k: pytest.fail("generic retry must not launch"))
    dispatch._after_worker_exit(con, state.get_run(con, "R939"), "DOLD")
    assert state.get_task(con, "R939", "T1")["status"] == "running"


def test_fresh_brief_includes_structured_handoff_without_resuming_predecessor(ledger):
    con, _ = ledger
    monkey = {"from_dispatch_id": "DOLD", "tokens": 220000, "threshold_tokens": 200000,
              "state": {"working_tree": " M file.txt"}, "done": ["commit abc"],
              "remaining": ["test cap"], "open_findings": [{"code": "F1"}]}
    packet = {
        "task_id": "T1", "title": "Context rotation", "scope": ["src/**"],
        "depends": [], "accept": ["test cap"], "checks": [], "visual": None,
        "requirements_version": 1, "plan_version": 2, "base_commit": "HEAD",
        "dispatch_id": None, "pr": None, "fix_of": None,
        "context_handoff": monkey,
    }
    text = briefs.executor_brief(con, state.get_run(con, "R939"), packet)
    assert "FRESH EXECUTOR CONTEXT HANDOFF" in text
    assert "DOLD" in text and "220000" in text and "F1" in text
    assert "SAME worktree" in text and "Do NOT resume" in text
