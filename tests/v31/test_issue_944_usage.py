"""#944: per-task and per-run usage (tokens and cost by role) in `office status` and `office inspect economics`.

A harness that reports no usage shows `unknown`, never 0. Codex rows count like Claude rows.
"""
from __future__ import annotations

import json

import pytest

from conftest import approved_run

pytestmark = pytest.mark.approved

SID = "11111111-2222-3333-4444-555555555555"
CODEX_SID = "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
UNKNOWN_OFF = "usage: unknown (cost collection is off"


def _run_id(env) -> str:
    con = env.con()
    try:
        return con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()


def _enable(env):
    env.office("config", "economics.collect", "true", check=0)


def _dispatch(env, did, *, role="executor", kind="executor", task="T1", harness="claude", sid=SID):
    con = env.con()
    try:
        run_id = _run_id(env)
        con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, harness, session_id, resumed_from, status) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (did, run_id, role, kind, task, harness, sid, None, "ended"))
    finally:
        con.close()


def _claude_transcript(env, entries, sid=SID):
    path = env.home / ".claude" / "projects" / "-tmp-x" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _claude_entry(mid, **usage):
    return {"type": "assistant", "sessionId": SID, "requestId": "req-" + mid, "timestamp": "2030-01-01T00:00:10Z",
            "message": {"id": mid, "model": "claude-x", "usage": usage}}


def _codex_transcript(env, total=100):
    path = env.home / ".codex" / "sessions" / "2030" / "01" / "01" / f"rollout-2030-01-01T00-00-00-{CODEX_SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    tc = {"type": "event_msg", "timestamp": "2030-01-01T00:00:05Z",
          "payload": {"type": "token_count", "info": {
              "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50},
              "total_token_usage": {"total_tokens": total}}}}
    path.write_text(json.dumps(tc) + "\n")


def _status_verbose(env) -> str:
    return env.office("status", "--verbose", check=0)[1]


def _lines_with(text: str, prefix: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith(prefix)]


def test_collection_off_status_is_silent_by_default_and_unknown_when_verbose(env):
    approved_run(env)
    default = env.office("status", check=0)[1]
    assert "usage" not in default, "default status stays quiet while collection is off"
    assert UNKNOWN_OFF in _status_verbose(env)


def test_enabled_task_without_reported_usage_shows_unknown_not_zero(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-none")
    text = _status_verbose(env)
    assert _lines_with(text, "usage: ") == ["usage: unknown (no usage reported)"], text
    assert "usage T1: unknown (no usage reported)" in text, text
    assert "input 0" not in text and "$0" not in text, text


def test_claude_usage_shows_per_run_and_per_task_by_role(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-claude")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    default = env.office("status", check=0)[1]
    assert ("usage: 1 turn(s), input 10, output 5, cache read unknown, cache write unknown, cost unknown"
            in default), default
    text = _status_verbose(env)
    assert ("usage run/executor: 1 turn(s), input 10, output 5, cache read unknown, cache write unknown, cost unknown"
            in text), text
    assert ("usage T1/executor: 1 turn(s), input 10, output 5, cache read unknown, cache write unknown, cost unknown"
            in text), text


def test_codex_usage_is_counted_with_unknown_cache_writes(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-codex", harness="codex", sid=CODEX_SID)
    _codex_transcript(env)
    env.office("economics", "ingest", check=0)
    text = _status_verbose(env)
    # Codex reports cached input: 1000 raw minus 800 cached is 200 fresh; no cache writes are reported.
    assert ("usage T1/executor: 1 turn(s), input 200, output 50, cache read 800, cache write unknown, cost unknown"
            in text), text


def test_partial_totals_say_how_many_turns_did_not_report(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-partial")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5),
                             _claude_entry("m2", output_tokens=7)])
    env.office("economics", "ingest", check=0)
    text = _status_verbose(env)
    assert "usage T1/executor: 2 turn(s), input 10 (+1 unknown), output 12" in text, text


def test_reviewer_kinds_roll_up_to_one_role_in_inspect(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-code", role="code_reviewer", kind="review")
    _dispatch(env, "D-944-plan", role="plan_reviewer", kind="plan_review", sid="22222222-2222-3333-4444-555555555555")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=3, output_tokens=1)])
    _claude_transcript(env, [_claude_entry("m2", input_tokens=4, output_tokens=2)],
                       sid="22222222-2222-3333-4444-555555555555")
    env.office("economics", "ingest", check=0)
    code, data = env.ojson("inspect", "economics")
    assert code == 0, data
    roles = {(g["role"], g["phase"]): g["turns"] for g in data["data"]["groups"]}
    assert roles == {("reviewer", "review"): 1, ("reviewer", "plan_review"): 1}, roles


def test_inspect_reports_unmeasured_tasks_as_unknown(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-inspect")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    code, data = env.ojson("inspect", "economics")
    text = "\n".join(data["lines"])
    assert "task/role: turns | input | output | cache read | cache write | cost" in text, text
    assert "  task T1/executor: 1 | 10 | 5 | unknown | unknown | unknown" in text, text
    assert data["data"]["tasks"][0]["task"] == "T1" and data["data"]["unmeasured_tasks"] == [], data


def test_normalized_quota_cost_without_amount_is_unknown_not_zero(env, tmp_path):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-quota")
    usage = tmp_path / "usage.jsonl"
    usage.write_text(json.dumps({"harness": "claude", "session_id": SID, "event_id": "q1", "turn_id": "t1",
                                 "task_id": "T1", "role": "executor", "input_tokens": 4, "output_tokens": 2,
                                 "cost_kind": "quota"}) + "\n")
    env.office("economics", "ingest", "--file", str(usage), check=0)
    text = _status_verbose(env)
    assert "usage T1/executor: 1 turn(s), input 4, output 2, cache read unknown, cache write unknown, cost quota unknown x1" in text, text
    assert "$0" not in text, text
