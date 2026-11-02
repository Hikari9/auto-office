"""#917: a nonblocking `oversize:` line (and next: options) when a dispatch outgrows its size class.

Office only signals; the orchestrator decides (split, resize, fresh rerun).
"""
from __future__ import annotations

import json

import pytest

from conftest import approved_run

pytestmark = pytest.mark.approved

SID = "11111111-2222-3333-4444-555555555555"


def _run_id(env) -> str:
    con = env.con()
    try:
        return con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()


def _enable(env, on=True):
    env.office("config", "economics.collect", "true" if on else "false", check=0)


def _dispatch(env, did, *, role="executor", kind="executor", task="T1", harness="claude", sid=SID, started_at=None):
    con = env.con()
    try:
        con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, harness, session_id, resumed_from, status, "
                    "started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (did, _run_id(env), role, kind, task, harness, sid, None, "ended", started_at))
        con.commit()
    finally:
        con.close()


def _set_task_status(env, tid, status):
    con = env.con()
    try:
        con.execute("UPDATE tasks SET status=? WHERE run_id=? AND id=?", (status, _run_id(env), tid))
        con.commit()
    finally:
        con.close()


def _add_task(env, tid):
    """A second task on the run, copied from T1 so it has every column the plan wrote."""
    con = env.con()
    try:
        row = dict(con.execute("SELECT * FROM tasks WHERE run_id=? AND id='T1'", (_run_id(env),)).fetchone())
        row["id"] = tid
        cols = list(row)
        con.execute(f"INSERT INTO tasks({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    [row[c] for c in cols])
        con.commit()
    finally:
        con.close()


def _claude_transcript(env, entries, sid=SID):
    path = env.home / ".claude" / "projects" / "-tmp-x" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _claude_entry(mid, sid=SID, **usage):
    return {"type": "assistant", "sessionId": sid, "requestId": "req-" + mid, "timestamp": "2030-01-01T00:00:10Z",
            "message": {"id": mid, "model": "claude-x", "usage": usage}}




def _set_size(env, size):
    con = env.con()
    try:
        row = con.execute("SELECT descriptor_json FROM tasks WHERE id='T1'").fetchone()
        d = json.loads(row[0] or "{}")
        d["task_size"] = size
        con.execute("UPDATE tasks SET descriptor_json=?, status='running', current_dispatch_id=? WHERE id='T1'",
                    (json.dumps(d), "D-917"))
        con.commit()
    finally:
        con.close()


def _oversize(env):
    out = env.office("status", check=0)[1]
    return [ln.strip() for ln in out.splitlines() if ln.strip().startswith("oversize:")], out


def _setup(env, size, started_at="2029-12-31T00:00:00+00:00"):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-917", started_at=started_at)
    _set_size(env, size)


def test_context_over_budget_prints_oversize_with_options_and_no_action(env):
    _setup(env, "S")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, cache_read_input_tokens=300_000, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    lines, out = _oversize(env)
    assert lines == ["oversize: T1 (S) context 300k > 2.5x 100k of its size class"], out
    nxt = [ln for ln in out.splitlines() if ln.startswith("next:")][0]
    assert "office amend T1 --contract" in nxt and "resize" in nxt and "office rerun T1 --fresh --as <route>" in nxt
    assert "no automatic action" in nxt


def test_within_budget_prints_nothing(env):
    _setup(env, "S")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, cache_read_input_tokens=240_000, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    lines, out = _oversize(env)
    assert lines == [] and "oversize" not in out, out


def test_larger_size_class_tolerates_the_same_context(env):
    _setup(env, "XL")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, cache_read_input_tokens=300_000, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    assert _oversize(env)[0] == []


def test_wall_time_over_budget_prints_oversize(env):
    _setup(env, "XS", started_at="2000-01-01T00:00:00+00:00")
    lines, out = _oversize(env)
    assert len(lines) == 1 and lines[0].startswith("oversize: T1 (XS) wall ") and "> 2.5x 10m" in lines[0], out


def test_no_measurement_is_not_oversize(env):
    _setup(env, "S")
    assert _oversize(env)[0] == []


def test_finished_task_is_not_flagged(env):
    _setup(env, "XS", started_at="2000-01-01T00:00:00+00:00")
    _set_task_status(env, "T1", "accepted")
    assert _oversize(env)[0] == []


def test_wait_prints_the_same_line(env):
    _setup(env, "XS", started_at="2000-01-01T00:00:00+00:00")
    out = env.office("wait", "--timeout", "1", check=124)[1]
    assert "oversize: T1 (XS) wall" in out, out


def test_cost_over_budget_counts_known_cost_only():
    from office import executor_context
    task, disp = {"descriptor": {"task_size": "XS"}}, {"started_at": None, "ended_at": None}
    row = lambda usd, kind: {"input_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None,
                             "cost_usd": usd, "cost_kind": kind}
    assert executor_context.exceeded(task, disp, [row(1.0, "actual"), row(0.3, "estimated-nominal")]) == [
        "cost $1.30 > 2.5x $0.50"]
    assert executor_context.exceeded(task, disp, [row(1.0, "actual"), row(None, "unknown")]) == ["cost $1.00 > 2.5x $0.50"]
    assert executor_context.exceeded(task, disp, [row(0.5, "actual"), row(9.0, "quota")]) == []
