"""#939: an executor at its context cap is signalled; rotation waits for the orchestrator."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import stat

import pytest

from office import briefs, db, dispatch, executor_context as cap, jobs, paths, root_wake, state
from office.state import Refused, Usage
from office.util import dumps


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def ledger(env_home, monkeypatch):
    repo = env_home / "repo"
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


def _run(con):
    return state.get_run(con, "R939")


def _set_policy(con, policy):
    con.execute("UPDATE runs SET policy_json=? WHERE id='R939'", (dumps({"executor_context": policy}),))
    con.commit()


def _outbox_kinds(con):
    return [r[0] for r in con.execute("SELECT kind FROM outbox").fetchall()]


def test_limit_defaults_disable_keys_and_task_decisions(ledger):
    con, _ = ledger
    run = _run(con)
    assert cap.limit(con, run, "T1") == 200_000
    assert cap.policy_warning(run) is None
    _set_policy(con, {"threshold_tokens": 0})
    assert cap.limit(con, _run(con), "T1") is None
    _set_policy(con, {"enabled": False})
    assert cap.limit(con, _run(con), "T1") is None
    _set_policy(con, {"threshold_tokens": True})
    assert cap.limit(con, _run(con), "T1") == 200_000
    assert "invalid" in cap.policy_warning(_run(con))
    _set_policy(con, {"threshold_tokens": -1})
    assert cap.limit(con, _run(con), "T1") == 200_000
    assert "invalid" in cap.policy_warning(_run(con))


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


def test_crossing_cap_signals_once_and_never_rotates(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda d: 199_999)
    assert not cap.check("DOLD")
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='executor.context_cap'").fetchone()[0] == 0
    monkeypatch.setattr(cap, "session_tokens", lambda d: 200_001)
    assert cap.check("DOLD")
    assert not cap.check("DOLD")
    assert _outbox_kinds(con) == []  # no decision, no rotation
    assert state.get_dispatch(con, "DOLD")["ended_at"] is None
    handoff = json.loads(state.get_dispatch(con, "DOLD")["context_handoff_json"])
    assert handoff["phase"] == cap.SIGNALLED and handoff["tokens"] == 200_001
    lines = cap.status_lines(_run(con), cap.recent(con, _run(con)))
    assert any("T1 at 200,001/200,000" in line and "executor-context T1 rotate" in line for line in lines)
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='executor.context_cap'").fetchone()[0] == 1




def test_disabled_or_zero_threshold_never_signals(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda d: 300_000)
    _set_policy(con, {"enabled": False, "threshold_tokens": 200000})
    assert not cap.check("DOLD")
    _set_policy(con, {"threshold_tokens": 0})
    assert not cap.check("DOLD")
    assert _outbox_kinds(con) == []


def test_continue_acknowledges_and_rearms_next_episode(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda d: 220_000)
    assert cap.check("DOLD")
    res = cap.decide(con, _run(con), "T1", "continue")
    assert "kept going" in res.lines[0]
    assert json.loads(state.get_dispatch(con, "DOLD")["context_handoff_json"])["phase"] == cap.ACKNOWLEDGED
    monkeypatch.setattr(cap, "session_tokens", lambda d: 230_000)
    assert not cap.check("DOLD")  # same episode: not repeated
    assert cap.status_lines(_run(con), cap.recent(con, _run(con))) == []
    monkeypatch.setattr(cap, "session_tokens", lambda d: 190_000)
    assert not cap.check("DOLD")  # back under the cap: the episode ends
    assert state.get_dispatch(con, "DOLD")["context_handoff_json"] is None
    monkeypatch.setattr(cap, "session_tokens", lambda d: 210_000)
    assert cap.check("DOLD")  # a new episode signals again


def test_continue_needs_a_pending_signal(ledger):
    con, _ = ledger
    with pytest.raises(Refused) as err:
        cap.decide(con, _run(con), "T1", "continue")
    assert err.value.category == "no-pending-signal"


def test_orchestrator_rotation_writes_durable_handoff_and_relaunches(monkeypatch, ledger):
    con, repo = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    assert cap.check("DOLD")
    res = cap.decide(con, _run(con), "T1", "rotate")
    assert "rotation requested" in res.lines[0]
    requested = json.loads(state.get_dispatch(con, "DOLD")["context_handoff_json"])
    assert requested["phase"] == cap.REQUESTED and requested["tokens"] == 220_000
    assert "file.txt" in requested["state"]["working_tree"]
    assert requested["remaining"][0] == "implement cap"
    assert requested["open_findings"][0]["code"] == "F1"
    assert _outbox_kinds(con) == ["rotate_executor"]
    path = cap.handoff_path(_run(con), "DOLD")
    assert json.loads(path.read_text())["phase"] == cap.REQUESTED
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    calls = []

    def stop(run, old):
        calls.append(("stop", old["id"]))
        # the handoff and its job are committed before the old writer is touched
        assert json.loads(con.execute("SELECT context_handoff_json FROM dispatches WHERE id='DOLD'")
                          .fetchone()[0])["phase"] == cap.REQUESTED
        assert _outbox_kinds(con) == ["rotate_executor"]
        con.execute("UPDATE dispatches SET ended_at='now', status='exited' WHERE id='DOLD'")
        return True, "verified no processes remain"

    def launch(conn, run, tid, **kwargs):
        calls.append(("launch", tid))
        assert conn.execute("SELECT ended_at FROM dispatches WHERE id='DOLD'").fetchone()[0]
        assert (repo / "file.txt").read_text() == "in progress\n"
        assert kwargs["extra"]["context_handoff"]["state"]["working_tree"]
        state.update_task(conn, run["id"], tid, status="launching", current_dispatch_id="DNEW")
        return "DNEW"

    monkeypatch.setattr(dispatch, "stop_worker_tree", stop)
    monkeypatch.setattr(dispatch, "_launch_or_block", launch)
    result = cap.job_rotate(con, _run(con), {"payload": {"dispatch_id": "DOLD"}})
    assert result["replacement"] == "DNEW"
    assert [c[0] for c in calls] == ["stop", "launch"]
    assert state.get_task(con, "R939", "T1")["current_dispatch_id"] == "DNEW"
    restarted = cap.recent(con, _run(con))[0]
    assert restarted["phase"] == "restarted" and restarted["new_dispatch_id"] == "DNEW"
    assert not cap.check("DOLD")


def test_rotation_is_refused_when_disabled_duplicated_or_without_live_executor(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    _set_policy(con, {"threshold_tokens": 0})
    with pytest.raises(Refused) as err:
        cap.decide(con, _run(con), "T1", "rotate")
    assert err.value.category == "context-disabled"
    _set_policy(con, {"threshold_tokens": 200000})
    assert cap.decide(con, _run(con), "T1", "rotate").lines
    with pytest.raises(Refused) as err:
        cap.decide(con, _run(con), "T1", "rotate")
    assert err.value.category == "rotation-in-flight"
    con.execute("UPDATE tasks SET current_dispatch_id=NULL WHERE id='T1'")
    con.commit()
    with pytest.raises(Refused) as err:
        cap.decide(con, _run(con), "T1", "rotate")
    assert err.value.category == "no-live-executor"


def test_no_overlap_when_old_process_not_confirmed_stopped(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    cap.decide(con, _run(con), "T1", "rotate")
    monkeypatch.setattr(dispatch, "stop_worker_tree", lambda *_: (False, "unattributed writer still running"))
    monkeypatch.setattr(dispatch, "_launch_or_block", lambda *_a, **_k: pytest.fail("must not launch"))
    result = cap.job_rotate(con, _run(con), {"payload": {"dispatch_id": "DOLD"}})
    assert "unattributed writer" in result["blocked"]
    assert state.get_task(con, "R939", "T1")["status"] == "blocked"
    assert cap.recent(con, _run(con))[0]["phase"] == "blocked"


def test_requested_rotation_exit_is_not_a_route_failure(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    cap.decide(con, _run(con), "T1", "rotate")
    monkeypatch.setattr(dispatch, "_launch_or_block", lambda *_a, **_k: pytest.fail("generic retry must not launch"))
    dispatch._after_worker_exit(con, _run(con), "DOLD")
    assert state.get_task(con, "R939", "T1")["status"] == "running"


def test_threshold_decision_is_per_task_and_refuses_bad_values(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    cap.decide(con, _run(con), "T1", "threshold", "250000")
    assert cap.limit(con, _run(con), "T1") == 250_000
    assert not cap.check("DOLD")  # 220k is under the raised cap
    for bad in ("-5", "lots", None):
        with pytest.raises(Usage):
            cap.decide(con, _run(con), "T1", "threshold", bad)
    cap.decide(con, _run(con), "T1", "threshold", "0")
    assert cap.limit(con, _run(con), "T1") is None
    _set_policy(con, {"enabled": False})
    with pytest.raises(Refused) as err:
        cap.decide(con, _run(con), "T1", "threshold", "300000")
    assert err.value.category == "context-disabled"


def test_unknown_task_is_refused(ledger):
    con, _ = ledger
    with pytest.raises(Refused) as err:
        cap.decide(con, _run(con), "T99", "continue")
    assert err.value.category == "no-task"


def test_fresh_brief_includes_structured_handoff_without_resuming_predecessor(ledger):
    con, _ = ledger
    handoff = {"from_dispatch_id": "DOLD", "tokens": 220000, "threshold_tokens": 200000,
               "state": {"working_tree": " M file.txt"}, "done": ["commit abc"],
               "remaining": ["test cap"], "open_findings": [{"code": "F1"}]}
    packet = {
        "task_id": "T1", "title": "Context rotation", "scope": ["src/**"],
        "depends": [], "accept": ["test cap"], "checks": [], "visual": None,
        "requirements_version": 1, "plan_version": 2, "base_commit": "HEAD",
        "dispatch_id": None, "pr": None, "fix_of": None,
        "context_handoff": handoff,
    }
    text = briefs.executor_brief(con, _run(con), packet)
    assert "FRESH EXECUTOR CONTEXT HANDOFF" in text
    assert "DOLD" in text and "220000" in text and "F1" in text
    assert "SAME worktree" in text and "Do NOT resume" in text


def test_v19_database_migrates_to_v20_with_rows_intact(env_home):
    path = paths.runs_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("PRAGMA journal_mode=WAL")
    for stmt in db._statements(db.LEGACY_DDL + db.OFFICE_DDL):
        raw.execute(stmt)
    for table, columns in db.SHARED_COLUMNS.items():
        have = db._columns(raw, table)
        for col in columns:
            name = col.split()[0]
            if name not in have and not (table == "dispatches" and name == "context_handoff_json"):
                raw.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    raw.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('office_schema', '19')")
    raw.execute("INSERT INTO dispatches(id, run_id, role, status) VALUES('D1', 'R1', 'executor', 'exited')")
    raw.commit()
    raw.close()

    con = db.connect()
    try:
        assert "context_handoff_json" in db._columns(con, "dispatches")
        assert db._schema_version(con) == db.SCHEMA_VERSION == 20
        assert con.execute("SELECT status, context_handoff_json FROM dispatches WHERE id='D1'").fetchone()[0] == "exited"
        notnull_default = [(r[3], r[4]) for r in con.execute("PRAGMA table_info(dispatches)")
                           if r[1] == "context_handoff_json"]
        assert notnull_default == [(0, None)]  # nullable, no default
    finally:
        con.close()


def test_worker_session_cannot_decide_its_own_context(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    assert cap.check("DOLD")
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "DOLD")
    for action, value in (("continue", None), ("rotate", None), ("threshold", "0")):
        with pytest.raises(Refused) as err:
            cap.decide(con, _run(con), "T1", action, value)
        assert err.value.category == "worker-cannot-decide"
    monkeypatch.delenv("OFFICE_DISPATCH_ID")
    assert json.loads(state.get_dispatch(con, "DOLD")["context_handoff_json"])["phase"] == cap.SIGNALLED
    assert cap.limit(con, _run(con), "T1") == 200_000
    assert _outbox_kinds(con) == []


def test_writer_changed_during_snapshot_refuses_the_decision(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, status, harness, model, worktree, branch, "
                "base_commit, started_at) VALUES('DNEW', 'R939', 'executor', 'executor', 'T1', 'launching', 'claude', "
                "'model', (SELECT worktree FROM dispatches WHERE id='DOLD'), 'branch', "
                "(SELECT base_commit FROM dispatches WHERE id='DOLD'), 'now')")
    con.commit()
    real_handoff = cap._handoff

    def replaced_mid_snapshot(*args, **kwargs):
        snapshot = real_handoff(*args, **kwargs)
        con.execute("UPDATE tasks SET current_dispatch_id='DNEW' WHERE id='T1'")
        con.commit()
        return snapshot

    monkeypatch.setattr(cap, "_handoff", replaced_mid_snapshot)
    with pytest.raises(Refused) as err:
        cap.decide(con, _run(con), "T1", "rotate")
    assert err.value.category == "writer-changed"
    assert state.get_dispatch(con, "DOLD")["context_handoff_json"] is None
    assert _outbox_kinds(con) == []


def test_skipped_rotation_is_not_left_pending_and_can_be_decided_again(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    cap.decide(con, _run(con), "T1", "rotate")
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    monkeypatch.setattr(dispatch, "stop_worker_tree", lambda *_: pytest.fail("a moved task is not stopped"))
    result = cap.job_rotate(con, _run(con), {"payload": {"dispatch_id": "DOLD"}})
    assert result == {"skipped": "task has moved"}
    skipped = json.loads(state.get_dispatch(con, "DOLD")["context_handoff_json"])
    assert skipped["phase"] == cap.SKIPPED and skipped["error"] == "task has moved"
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='executor.rotate_skipped'").fetchone()[0] == 1
    assert cap.recent(con, _run(con))[0]["phase"] == cap.SKIPPED
    con.execute("UPDATE tasks SET status='running' WHERE id='T1'")
    con.commit()
    assert cap.decide(con, _run(con), "T1", "rotate").lines  # the orchestrator may decide again


def test_submitted_executor_during_stop_is_not_replaced(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    cap.decide(con, _run(con), "T1", "rotate")

    def stop(run, old):
        con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, "
                    "requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, "
                    "created_at) VALUES('RV1', 'R939', 'T1', 1, 'DOLD', 'abc', 'def', 1, 2, 2, 'env', 'op1', "
                    "'accepted', 'now')")
        con.execute("UPDATE dispatches SET ended_at='now', status='exited' WHERE id='DOLD'")
        return True, "verified no processes remain"

    monkeypatch.setattr(dispatch, "stop_worker_tree", stop)
    monkeypatch.setattr(dispatch, "_launch_or_block", lambda *_a, **_k: pytest.fail("must not replace a submitted executor"))
    result = cap.job_rotate(con, _run(con), {"payload": {"dispatch_id": "DOLD"}})
    assert result == {"skipped": "executor already submitted"}
    assert cap.recent(con, _run(con))[0]["phase"] == cap.SKIPPED


def test_stopped_old_writer_without_recorded_end_is_finished_as_context_cap(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    cap.decide(con, _run(con), "T1", "rotate")
    finished = []

    def stop(run, old):
        return True, "verified no processes remain"  # the supervisor has not recorded the end

    def finish(did, code, sig, classification, wall, **kwargs):
        finished.append((did, classification, kwargs))
        con.execute("UPDATE dispatches SET ended_at='now', status='exited', terminal_classification=? WHERE id=?",
                    (classification, did))

    monkeypatch.setattr(dispatch, "stop_worker_tree", stop)
    monkeypatch.setattr(dispatch, "_finish", finish)
    monkeypatch.setattr(dispatch, "_launch_or_block", lambda *_a, **_k: None)
    cap.job_rotate(con, _run(con), {"payload": {"dispatch_id": "DOLD"}})
    assert finished and finished[0][0] == "DOLD" and finished[0][1] == "context_cap"
    assert finished[0][2] == {"only_if_open": True}


def test_permanent_rotation_failure_blocks_the_task_visibly(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    cap.decide(con, _run(con), "T1", "rotate")
    from office import jobs as jobs_mod
    jobs_mod.on_permanent_failure(con, _run(con), {"kind": "rotate_executor", "payload": {"dispatch_id": "DOLD"}},
                                  "boom")
    assert state.get_task(con, "R939", "T1")["status"] == "blocked"
    assert json.loads(state.get_dispatch(con, "DOLD")["context_handoff_json"])["phase"] == cap.BLOCKED
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='executor.handoff_blocked'").fetchone()[0] == 1


def test_unmeasured_executor_rotation_is_labelled_not_none(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: None)
    cap.decide(con, _run(con), "T1", "rotate")

    def stop(run, old):
        con.execute("UPDATE dispatches SET ended_at='now', status='exited' WHERE id='DOLD'")
        return True, "verified no processes remain"

    def launch(conn, run, tid, **kwargs):
        state.update_task(conn, run["id"], tid, status="launching", current_dispatch_id="DNEW")
        return "DNEW"

    monkeypatch.setattr(dispatch, "stop_worker_tree", stop)
    monkeypatch.setattr(dispatch, "_launch_or_block", launch)
    assert cap.job_rotate(con, _run(con), {"payload": {"dispatch_id": "DOLD"}})["replacement"] == "DNEW"
    summary = con.execute("SELECT summary FROM events WHERE kind='executor.handoff'").fetchone()[0]
    assert "unmeasured" in summary and "None" not in summary


def test_status_hides_stale_signal_and_clears_it_when_disabled(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    assert cap.check("DOLD")
    con.execute("UPDATE dispatches SET ended_at='now', status='exited' WHERE id='DOLD'")
    con.commit()
    assert cap.recent(con, _run(con)) == []  # the writer that signalled has ended
    con.execute("UPDATE dispatches SET ended_at=NULL, status='running' WHERE id='DOLD'")
    con.commit()
    assert [r["phase"] for r in cap.recent(con, _run(con))] == [cap.SIGNALLED]
    con.execute("UPDATE runs SET policy_json=? WHERE id='R939'",
                (dumps({"executor_context": {"threshold_tokens": 0}}),))
    con.commit()
    assert not cap.check("DOLD")  # disabling the cap clears the pending signal
    assert cap.recent(con, _run(con)) == []


def test_non_mapping_policy_warns_instead_of_crashing_status(ledger):
    con, _ = ledger
    con.execute("UPDATE runs SET policy_json=? WHERE id='R939'", (dumps({"executor_context": "off"}),))
    con.commit()
    run = _run(con)
    assert cap.limit(con, run, "T1") == 200_000
    assert "not a mapping" in cap.policy_warning(run)
    assert cap.status_lines(run, cap.recent(con, run)) == [cap.policy_warning(run)]


def test_recent_reports_only_the_latest_dispatch_per_task(ledger):
    con, _ = ledger
    older = dumps({"task_id": "T1", "phase": cap.RESTARTED, "from_dispatch_id": "DOLD", "tokens": 1,
                   "threshold_tokens": 2})
    con.execute("UPDATE dispatches SET context_handoff_json=? WHERE id='DOLD'", (older,))
    con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, status, harness, started_at, ended_at, "
                "context_handoff_json) VALUES('DPREV', 'R939', 'executor', 'executor', 'T1', 'exited', 'claude', "
                "'earlier', 'earlier', ?)", (dumps({"phase": cap.BLOCKED, "error": "old"}),))
    con.commit()
    rows = cap.recent(con, _run(con))
    assert [(r["task_id"], r["phase"]) for r in rows] == [("T1", cap.RESTARTED)]


def test_codex_session_tokens_use_the_exact_rollout_file(tmp_path, monkeypatch):
    codex = tmp_path / "codex"
    day = codex / "sessions" / "2026" / "10" / "31"
    day.mkdir(parents=True)
    rollout = day / "rollout-2026-10-31T00-00-00-abc123.jsonl"
    rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": 150_000, "output_tokens": 1_000}}}}) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(codex))
    assert cap.session_tokens({"harness": "codex", "session_id": "abc123"}) == 151_000
    assert cap.session_tokens({"harness": "codex", "session_id": "missing"}) is None
    twin = day / "rollout-2026-10-31T00-00-01-abc123.jsonl"
    twin.write_text(rollout.read_text())
    assert cap.session_tokens({"harness": "codex", "session_id": "abc123"}) is None  # ambiguous: never guessed




def test_status_lines_show_the_pending_signal_through_guide_status(monkeypatch, ledger):
    from office import guide
    con, _ = ledger
    monkeypatch.setattr(guide, "next_action", lambda *_a: "no action; waiting on executors")  # no plan in this ledger
    monkeypatch.setattr(cap, "session_tokens", lambda _: 220_000)
    assert cap.check("DOLD")
    res = guide.status(con, _run(con))
    assert any(line.startswith("executor context: T1 at 220,000/200,000 tokens") for line in res.lines), res.lines
    assert res.data["executor_handoffs"][0]["phase"] == cap.SIGNALLED




def test_replacement_packet_carries_the_handoff(monkeypatch, ledger):
    con, _ = ledger
    monkeypatch.setattr(state, "current_requirements", lambda *_a: {"frozen": {}, "version": 1})
    handoff = {"from_dispatch_id": "DOLD", "tokens": 220_000, "state": {"working_tree": "2 changed path(s)"}}
    packet = dispatch.build_packet(con, _run(con), state.get_dispatch(con, "DOLD"), "executor",
                                   {"context_handoff": handoff})
    assert packet["context_handoff"] == handoff
    plain = dispatch.build_packet(con, _run(con), state.get_dispatch(con, "DOLD"), "executor", {})
    assert plain["context_handoff"] is None


def test_handoff_replacement_never_restacks_the_preserved_worktree(monkeypatch, ledger):
    from office import rerun
    con, repo = ledger
    base = con.execute("SELECT base_commit FROM dispatches WHERE id='DOLD'").fetchone()[0]
    # the replacement is a launching dispatch whose predecessor is the old writer
    con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, status, harness, model, worktree, branch, "
                "base_commit, predecessor_dispatch_id, started_at) VALUES('DNEW', 'R939', 'executor', 'executor', 'T1', "
                "'launching', 'claude', 'model', ?, 'branch', ?, 'DOLD', 'now')", (str(repo), base))
    con.commit()
    restacks = []
    monkeypatch.setattr(rerun, "_restack", lambda *a, **k: restacks.append(a) or {})
    handoff = {"from_dispatch_id": "DOLD", "tokens": 220_000}
    job = {"payload": {"dispatch_id": "DNEW", "task_id": "T1", "role": "executor", "context_handoff": handoff}}
    try:
        dispatch.job_launch_agent(con, _run(con), job)
    except Exception:
        pass  # the launch after the restack decision is out of scope; only the restack call is asserted
    assert restacks == []
