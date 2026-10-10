"""Orchestrator economics ledger (#502 PR 1): opt-in, idempotent, honest about unknowns."""
from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT

pytestmark = pytest.mark.approved

SID = "11111111-2222-3333-4444-555555555555"
ROOT_SID = "99999999-2222-3333-4444-555555555555"
CODEX_SID = "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _run_id(env) -> str:
    con = env.con()
    try:
        return con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()


def _enable(env, on=True):
    env.office("config", "economics.collect", "true" if on else "false", check=0)


def _table(env) -> bool:
    con = env.con()
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_events'").fetchone() is not None
    finally:
        con.close()


def _claude_entry(mid, *, req="req1", ts="2030-01-01T00:00:10Z", sid=SID, **usage):
    return {"type": "assistant", "sessionId": sid, "requestId": req, "timestamp": ts,
            "message": {"id": mid, "model": "claude-x", "usage": usage}}


def _write_claude(env, sid, entries):
    path = env.home / ".claude" / "projects" / "-tmp-x" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def _dispatch(env, run_id, did="D-econ-1", harness="claude", sid=SID, resumed_from=None):
    con = env.con()
    try:
        con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, harness, session_id, resumed_from, status) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (did, run_id, "executor", "executor", "T1", harness, sid,
                                                   resumed_from, "ended"))
    finally:
        con.close()


def _rows(env):
    con = env.con()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM usage_events ORDER BY event_id")]
    finally:
        con.close()


def test_default_off_reads_and_writes_nothing(env):
    run_id = _run_id(env)
    _dispatch(env, run_id)
    _write_claude(env, SID, [_claude_entry("m1", input_tokens=10, output_tokens=5)])
    code, out = env.office("economics", "ingest")
    assert code == 0 and "disabled" in out, out
    assert not _table(env), "no table is created while collection is off"
    code, data = env.ojson("inspect", "economics")
    assert code == 0 and data["data"] == {"enabled": False, "rows": 0}, data
    assert "disabled" in data["lines"][0] and "$" not in " ".join(data["lines"])


def test_existing_db_migrates_and_duplicates_reorders_merge(env):
    run_id = _run_id(env)
    _dispatch(env, run_id)
    partial = _claude_entry("m1", input_tokens=10, output_tokens=1, cache_read_input_tokens=100,
                            cache_creation_input_tokens=20)
    full = _claude_entry("m1", input_tokens=10, output_tokens=7, cache_read_input_tokens=100,
                         cache_creation_input_tokens=20)
    other = _claude_entry("m2", req="req2", ts="2030-01-01T00:00:20Z", input_tokens=3, output_tokens=2,
                          cache_read_input_tokens=130, cache_creation_input_tokens=0)
    _enable(env)
    _write_claude(env, SID, [partial, full, partial, other])
    env.office("economics", "ingest", check=0)
    first = [(r["event_id"], r["input_tokens"], r["output_tokens"], r["cache_read_tokens"], r["cache_write_tokens"])
             for r in _rows(env)]
    assert first == [("m1", 10, 7, 100, 20), ("m2", 3, 2, 130, 0)]
    _write_claude(env, SID, [other, full, partial])  # reordered and repeated
    env.office("economics", "ingest", check=0)
    env.office("economics", "ingest", check=0)
    again = [(r["event_id"], r["input_tokens"], r["output_tokens"], r["cache_read_tokens"], r["cache_write_tokens"])
             for r in _rows(env)]
    assert again == first, "re-ingest, duplicates and reordering never double count"
    code, data = env.ojson("inspect", "economics")
    group = data["data"]["groups"][0]
    assert (group["role"], group["turns"], group["input_tokens"], group["cache_read_tokens"]) == ("executor", 2, 13, 230)
    assert group["cost"] == {"unknown": {"count": 2, "usd": None}}, "no price is invented"


def test_missing_usage_fields_stay_null_and_are_reported(env):
    run_id = _run_id(env)
    _dispatch(env, run_id)
    _enable(env)
    _write_claude(env, SID, [_claude_entry("m1", output_tokens=4)])
    env.office("economics", "ingest", check=0)
    (row,) = _rows(env)
    assert row["output_tokens"] == 4
    assert row["input_tokens"] is None and row["cache_read_tokens"] is None and row["cache_write_tokens"] is None
    code, data = env.ojson("inspect", "economics")
    text = "\n".join(data["lines"])
    assert "cache: unknown" in text and data["data"]["missing"]["input_tokens"] == 1, text
    assert "| unknown |" in text, text


def test_codex_cached_input_is_not_double_counted(env):
    run_id = _run_id(env)
    _dispatch(env, run_id, harness="codex", sid=CODEX_SID)
    _enable(env)
    path = env.home / ".codex" / "sessions" / "2030" / "01" / "01" / f"rollout-2030-01-01T00-00-00-{CODEX_SID}.jsonl"
    path.parent.mkdir(parents=True)
    tc = {"type": "event_msg", "timestamp": "2030-01-01T00:00:05Z",
          "payload": {"type": "token_count", "info": {
              "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50},
              "total_token_usage": {"total_tokens": 1050}}}}
    lines = [{"type": "session_meta", "payload": {"id": CODEX_SID}},
             {"type": "turn_context", "payload": {"model": "gpt-x"}}, tc, tc]
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))
    env.office("economics", "ingest", check=0)
    (row,) = _rows(env)
    assert (row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"], row["model"]) == \
        (200, 800, None, "gpt-x")


def test_root_session_outside_the_run_window_is_kept_apart(env):
    run_id = _run_id(env)
    con = env.con()
    try:
        con.execute("INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by) VALUES(?,?,?,?,?)",
                    ("claude", ROOT_SID, run_id, "2030-01-01T00:00:00+00:00", "test"))
    finally:
        con.close()
    _enable(env)
    _write_claude(env, ROOT_SID, [
        _claude_entry("before", sid=ROOT_SID, ts="2029-12-31T23:00:00Z", input_tokens=5, output_tokens=5,
                      cache_read_input_tokens=0, cache_creation_input_tokens=0),
        _claude_entry("during", sid=ROOT_SID, ts="2030-01-01T01:00:00Z", input_tokens=7, output_tokens=1,
                      cache_read_input_tokens=0, cache_creation_input_tokens=0)])
    env.office("economics", "ingest", check=0)
    by_event = {r["event_id"]: r["attribution"] for r in _rows(env)}
    assert by_event == {"before": "outside", "during": "office"}
    code, data = env.ojson("inspect", "economics")
    assert data["data"]["rows"] == 1 and data["data"]["outside_rows"] == 1
    assert data["data"]["groups"][0]["role"] == "root"


def test_cold_resume_needs_cache_evidence_not_a_gap(env):
    run_id = _run_id(env)
    _dispatch(env, run_id, did="D-cold", sid=SID, resumed_from="D-prev")
    _enable(env)
    _write_claude(env, SID, [_claude_entry("m1", input_tokens=5, output_tokens=1, cache_read_input_tokens=0,
                                           cache_creation_input_tokens=900)])
    env.office("economics", "ingest", check=0)
    code, data = env.ojson("inspect", "economics")
    assert data["data"]["cold_resumes"] == ["D-cold"]


def test_opting_out_stops_further_collection(env):
    run_id = _run_id(env)
    _dispatch(env, run_id)
    _enable(env)
    _write_claude(env, SID, [_claude_entry("m1", input_tokens=1, output_tokens=1)])
    env.office("economics", "ingest", check=0)
    _enable(env, on=False)
    _write_claude(env, SID, [_claude_entry("m1", input_tokens=1, output_tokens=1),
                             _claude_entry("m2", input_tokens=1, output_tokens=1)])
    code, out = env.office("economics", "ingest")
    assert "disabled" in out
    assert [r["event_id"] for r in _rows(env)] == ["m1"]


def test_normalized_file_requires_a_known_cost_kind(env, tmp_path):
    _enable(env)
    good = tmp_path / "u.jsonl"
    good.write_text(json.dumps({"harness": "api", "session_id": "s", "event_id": "e1", "input_tokens": 3,
                                "cost_usd": 0.01, "cost_kind": "actual", "role": "root"}) + "\n")
    env.office("economics", "ingest", "--file", str(good), check=0)
    (row,) = _rows(env)
    assert (row["cost_kind"], row["cost_usd"], row["output_tokens"]) == ("actual", 0.01, None)
    bad = tmp_path / "b.jsonl"
    bad.write_text(json.dumps({"harness": "api", "session_id": "s", "event_id": "e2", "cost_kind": "free"}) + "\n")
    code, out = env.office("economics", "ingest", "--file", str(bad))
    assert code != 0 and "cost_kind" in out


def test_legacy_db_without_ledger_still_opens(tmp_path):
    """An existing runs.db from before the ledger opens unchanged; the ledger table is added on first ingest only."""
    sys.path.insert(0, str(ROOT / "src"))
    from office import db, economics
    path = tmp_path / "runs.db"
    con = db.connect(path)
    con.execute("DELETE FROM schema_meta")  # pretend an older schema stamp
    con.close()
    con = db.connect(path)
    assert not economics.has_table(con)
    economics.ensure_schema(con)
    economics.ensure_schema(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(usage_events)")}
    assert {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_kind"} <= cols
    con.close()


def test_hook_hot_path_never_touches_the_ledger(env):
    """Latency guard: hooks.py does not import the ledger, and a hook with collection on stays fast and writes nothing."""
    tree = ast.parse((ROOT / "src" / "office" / "hooks.py").read_text())
    names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not ({"economics", "context_snapshot"} & names) and not any("economics" in m for m in mods)
    _enable(env)
    payload = json.dumps({"session_id": SID, "tool_name": "Read", "tool_input": {}})
    best = None
    for _ in range(3):
        t = time.monotonic()
        proc = subprocess.run([sys.executable, "-m", "office", "hook", "PreToolUse", "--harness", "claude",
                               "--office-managed"], input=payload, capture_output=True, text=True, cwd=env.repo)
        took = time.monotonic() - t
        best = took if best is None else min(best, took)
        assert proc.returncode == 0, proc.stderr
    assert best < 2.0, f"hook took {best:.2f}s"
    assert not _table(env)


def test_resume_sharing_a_session_owns_only_its_window(env):
    run_id = _run_id(env)
    con = env.con()
    try:
        for did, start, end, prev in (("D-a", "2030-01-01T00:00:00+00:00", "2030-01-01T00:10:00+00:00", None),
                                      ("D-b", "2030-01-01T00:20:00+00:00", None, "D-a")):
            con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, harness, session_id, resumed_from, "
                        "status, started_at, ended_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (did, run_id, "executor", "executor", "T1", "claude", SID, prev, "ended", start, end))
    finally:
        con.close()
    _enable(env)
    _write_claude(env, SID, [
        _claude_entry("first", ts="2030-01-01T00:05:00Z", input_tokens=5, output_tokens=1,
                      cache_read_input_tokens=0, cache_creation_input_tokens=50),
        _claude_entry("resumed", req="r2", ts="2030-01-01T00:25:00Z", input_tokens=5, output_tokens=1,
                      cache_read_input_tokens=0, cache_creation_input_tokens=900)])
    env.office("economics", "ingest", check=0)
    owners = {r["event_id"]: (r["dispatch_id"], r["attribution"]) for r in _rows(env)}
    assert owners == {"first": ("D-a", "office"), "resumed": ("D-b", "office")}, owners
    code, data = env.ojson("inspect", "economics")
    assert data["data"]["cold_resumes"] == ["D-b"]


def test_codex_without_cached_input_keeps_raw_input(env):
    run_id = _run_id(env)
    _dispatch(env, run_id, harness="codex", sid=CODEX_SID)
    _enable(env)
    path = env.home / ".codex" / "sessions" / "2030" / "01" / "01" / f"rollout-2030-01-01T00-00-00-{CODEX_SID}.jsonl"
    path.parent.mkdir(parents=True)
    tc = {"type": "event_msg", "timestamp": "2030-01-01T00:00:05Z",
          "payload": {"type": "token_count", "info": {
              "last_token_usage": {"input_tokens": 1000, "output_tokens": 50},
              "total_token_usage": {"total_tokens": 1050}}}}
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": CODEX_SID}}) + "\n" + json.dumps(tc) + "\n")
    env.office("economics", "ingest", check=0)
    (row,) = _rows(env)
    assert (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]) == (1000, None, 50)


def test_cost_merge_is_order_independent(env, tmp_path):
    _enable(env)
    rows = [{"cost_kind": "estimated-nominal", "cost_usd": 0.5}, {"cost_kind": "actual", "cost_usd": 0.2},
            {"cost_kind": "quota", "cost_usd": 0.9}, {"cost_kind": "actual", "cost_usd": 0.3},
            {"cost_kind": "unknown"}]
    results = set()
    for n, order in enumerate((rows, rows[::-1], rows[2:] + rows[:2])):
        for i, extra in enumerate(order):
            f = tmp_path / f"c{n}-{i}.jsonl"
            f.write_text(json.dumps({"harness": "api", "session_id": f"s{n}", "event_id": "e", **extra}) + "\n")
            env.office("economics", "ingest", "--file", str(f), check=0)
    for row in _rows(env):
        results.add((row["cost_kind"], row["cost_usd"]))
    assert results == {("actual", 0.3)}
