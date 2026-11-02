"""#944: per-task and per-run usage (tokens and cost by role) in `office status` and `office inspect economics`.

A harness that reports no usage shows `unknown`, never 0. Codex rows count like Claude rows.
"""
from __future__ import annotations

import json

import pytest

from conftest import approved_run

pytestmark = pytest.mark.approved

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "22222222-2222-3333-4444-555555555555"
CODEX_SID = "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
UNKNOWN_OFF = "usage: unknown (cost collection is off"
NO_RECORD = "usage: unknown (no usage recorded)"
NO_TASK = "usage T1: unknown (no usage attributed to this task)"
OFF_NOW = "usage: collection is off; figures are from when it was on"


def _run_id(env) -> str:
    con = env.con()
    try:
        return con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    finally:
        con.close()


def _enable(env, on=True):
    env.office("config", "economics.collect", "true" if on else "false", check=0)


def _dispatch(env, did, *, role="executor", kind="executor", task="T1", harness="claude", sid=SID):
    con = env.con()
    try:
        con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, harness, session_id, resumed_from, status) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (did, _run_id(env), role, kind, task, harness, sid, None, "ended"))
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


def _codex_transcript(env, total=100):
    path = env.home / ".codex" / "sessions" / "2030" / "01" / "01" / f"rollout-2030-01-01T00-00-00-{CODEX_SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    tc = {"type": "event_msg", "timestamp": "2030-01-01T00:00:05Z",
          "payload": {"type": "token_count", "info": {
              "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50},
              "total_token_usage": {"total_tokens": total}}}}
    path.write_text(json.dumps(tc) + "\n")


def _usage_file(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def _usage_lines(env) -> list[str]:
    """Every `usage` line of the default status, in order."""
    text = env.office("status", check=0)[1]
    return [line.strip() for line in text.splitlines() if line.strip().startswith("usage")]


def _status_verbose(env) -> str:
    return env.office("status", "--verbose", check=0)[1]


def test_collection_off_status_is_silent_by_default_and_unknown_when_verbose(env):
    approved_run(env)
    assert _usage_lines(env) == [], "default status stays quiet while collection is off"
    assert UNKNOWN_OFF in _status_verbose(env)


def test_enabled_task_without_reported_usage_shows_unknown_not_zero(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-none")
    assert _usage_lines(env) == [NO_RECORD, NO_TASK], _usage_lines(env)
    text = "\n".join(_usage_lines(env))
    assert "input 0" not in text and "$0" not in text, text


def test_claude_usage_shows_per_run_and_per_task_by_role(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-claude")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    cells = "1 turn(s), input 10, output 5, cache read unknown, cache write unknown, cost unknown x1"
    assert _usage_lines(env) == [f"usage: {cells}", f"usage run/executor: {cells}", f"usage T1/executor: {cells}"]


def test_codex_usage_is_counted_with_unknown_cache_writes(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-codex", harness="codex", sid=CODEX_SID)
    _codex_transcript(env)
    env.office("economics", "ingest", check=0)
    # Codex reports cached input: 1000 raw minus 800 cached is 200 fresh; no cache writes are reported.
    cells = "1 turn(s), input 200, output 50, cache read 800, cache write unknown, cost unknown x1"
    assert f"usage T1/executor: {cells}" in _usage_lines(env), _usage_lines(env)


def test_partial_totals_say_how_many_turns_did_not_report(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-partial")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5),
                             _claude_entry("m2", output_tokens=7)])
    env.office("economics", "ingest", check=0)
    assert "usage T1/executor: 2 turn(s), input 10 (+1 unknown), output 12" in " ".join(_usage_lines(env))
    code, data = env.ojson("inspect", "economics")
    (group,) = data["data"]["groups"]
    assert group["missing"]["input_tokens"] == 1 and group["missing"]["output_tokens"] == 0, group
    (task,) = data["data"]["tasks"]
    assert task["input_tokens"] == 10 and task["output_tokens"] == 12, task
    assert task["missing"]["input_tokens"] == 1 and task["missing"]["output_tokens"] == 0, task
    assert task["cost"] == {"unknown": {"count": 2, "usd": None}}, task


def test_reviewer_kinds_roll_up_to_one_role_in_inspect(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-code", role="code_reviewer", kind="review")
    _dispatch(env, "D-944-plan", role="plan_reviewer", kind="plan_review", sid=SID2)
    _claude_transcript(env, [_claude_entry("m1", input_tokens=3, output_tokens=1)])
    _claude_transcript(env, [_claude_entry("m2", sid=SID2, input_tokens=4, output_tokens=2)], sid=SID2)
    env.office("economics", "ingest", check=0)
    code, data = env.ojson("inspect", "economics")
    assert code == 0, data
    roles = {(g["role"], g["phase"]): g["turns"] for g in data["data"]["groups"]}
    assert roles == {("reviewer", "review"): 1, ("reviewer", "plan_review"): 1}, roles


def test_inspect_measured_and_unmeasured_tasks(env):
    approved_run(env)
    _enable(env)
    _add_task(env, "T2")
    _dispatch(env, "D-944-inspect")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    code, data = env.ojson("inspect", "economics")
    text = "\n".join(data["lines"])
    assert "task/role: turns | input | output | cache read | cache write | cost" in text, text
    assert "  task T1/executor: 1 | 10 | 5 | unknown | unknown | unknown x1" in text, text
    assert "  task T2: unknown (no usage attributed to this task)" in text, text
    assert [t["task"] for t in data["data"]["tasks"]] == ["T1"], data
    assert data["data"]["unmeasured_tasks"] == ["T2"], data


def test_inspect_without_ingest_lists_unknown_tasks(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-noingest")
    code, data = env.ojson("inspect", "economics")
    assert data["lines"] == ["no usage recorded for this run yet", "task T1: unknown (no usage attributed to this task)"], data
    assert data["data"] == {"enabled": True, "rows": 0}, data


def test_inspect_with_only_outside_usage_shows_unknown_tasks(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-outside")
    con = env.con()
    try:
        from office import economics
        economics.ensure_schema(con)
        con.execute("INSERT INTO usage_events(harness,session_id,turn_id,event_id,run_id,role,attribution,source,"
                    "cost_kind,ledger_version,recorded_at) VALUES('claude',?,'t','e',?,'root','outside','fixture',"
                    "'unknown',1,'2030-01-01T00:00:00Z')", (SID, _run_id(env)))
        con.commit()
    finally:
        con.close()
    code, data = env.ojson("inspect", "economics")
    text = "\n".join(data["lines"])
    assert "no Office-attributed usage recorded for this run" in text, text
    assert "task T1: unknown (no usage attributed to this task)" in text, text
    assert data["data"]["rows"] == 0 and data["data"]["outside_rows"] == 1, data
    assert "$0" not in text and "input 0" not in text, text


def test_numeric_cost_is_summed_and_partial_marked(env, tmp_path):
    approved_run(env)
    _enable(env)
    rows = [{"harness": "claude", "session_id": SID, "event_id": f"c{i}", "role": "executor",
             "cost_kind": "actual", **extra} for i, extra in enumerate([{"cost_usd": 0.12}, {"cost_usd": 0.03}, {}])]
    rows.append({"harness": "claude", "session_id": SID, "event_id": "e0", "role": "executor",
                 "cost_kind": "estimated-nominal", "cost_usd": 1.0})
    env.office("economics", "ingest", "--file", _usage_file(tmp_path / "usage.jsonl", rows), check=0)
    assert "cost actual $0.1500 (+1 unknown), estimated-nominal $1.0000" in " ".join(_usage_lines(env)), _usage_lines(env)
    code, data = env.ojson("inspect", "economics")
    (group,) = data["data"]["groups"]
    assert group["cost"]["actual"] == {"count": 3, "usd": pytest.approx(0.15), "unknown_count": 1}, group
    assert group["cost"]["estimated-nominal"] == {"count": 1, "usd": pytest.approx(1.0)}, group
    assert "actual $0.1500 (+1 unknown), estimated-nominal $1.0000" in "\n".join(data["lines"])


def test_normalized_quota_cost_without_amount_is_unknown_not_zero(env, tmp_path):
    approved_run(env)
    _enable(env)
    row = {"harness": "claude", "session_id": SID, "event_id": "q1", "turn_id": "t1",
           "role": "executor", "input_tokens": 4, "output_tokens": 2, "cost_kind": "quota"}
    env.office("economics", "ingest", "--file", _usage_file(tmp_path / "usage.jsonl", [row]), check=0)
    assert ("usage run/executor: 1 turn(s), input 4, output 2, cache read unknown, cache write unknown, "
            "cost quota unknown x1") in _usage_lines(env), _usage_lines(env)
    code, data = env.ojson("inspect", "economics")
    (group,) = data["data"]["groups"]
    assert group["cost"] == {"quota": {"count": 1, "usd": None}}, group


def test_boolean_cost_is_not_a_price(env, tmp_path):
    approved_run(env)
    _enable(env)
    row = {"harness": "claude", "session_id": SID, "event_id": "b1", "role": "executor",
           "cost_kind": "actual", "cost_usd": True}
    env.office("economics", "ingest", "--file", _usage_file(tmp_path / "usage.jsonl", [row]), check=0)
    code, data = env.ojson("inspect", "economics")
    (group,) = data["data"]["groups"]
    assert group["cost"] == {"actual": {"count": 1, "usd": None}}, group


def test_normalized_file_cannot_reattribute_usage_to_a_task(env, tmp_path):
    approved_run(env)
    _enable(env)
    row = {"harness": "claude", "session_id": SID, "event_id": "a1", "task_id": "T9",
           "input_tokens": 4, "cost_kind": "unknown"}
    env.office("economics", "ingest", "--file", _usage_file(tmp_path / "usage.jsonl", [row]), check=0)
    con = env.con()
    try:
        task = con.execute("SELECT task_id FROM usage_events WHERE event_id='a1'").fetchone()[0]
    finally:
        con.close()
    assert task is None, "a usage file cannot pick the task; only the run's own dispatch association does"
    assert not any(line.startswith("usage T9") for line in _usage_lines(env)), _usage_lines(env)


def test_legacy_row_naming_a_task_without_a_dispatch_is_not_attributed(env):
    approved_run(env)
    _enable(env)
    con = env.con()
    try:
        from office import economics
        economics.ensure_schema(con)
        con.execute("INSERT INTO usage_events(harness,session_id,turn_id,event_id,run_id,task_id,role,attribution,"
                    "source,input_tokens,cost_kind,ledger_version,recorded_at) "
                    "VALUES('claude',?,'t','legacy',?,'T9','executor','office','normalized-file',4,'unknown',1,"
                    "'2030-01-01T00:00:00Z')", (SID, _run_id(env)))
        con.commit()
    finally:
        con.close()
    assert not any(line.startswith("usage T9") for line in _usage_lines(env)), _usage_lines(env)


def test_file_usage_ingested_before_the_transcript_still_gets_the_task(env, tmp_path):
    approved_run(env)
    _enable(env)
    first = {"harness": "claude", "session_id": SID, "turn_id": "req-m1", "event_id": "m1",
             "role": "executor", "input_tokens": 10, "output_tokens": 5, "cost_kind": "unknown"}
    env.office("economics", "ingest", "--file", _usage_file(tmp_path / "usage.jsonl", [first]), check=0)
    _dispatch(env, "D-944-order")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    cells = "1 turn(s), input 10, output 5, cache read unknown, cache write unknown, cost unknown x1"
    assert f"usage T1/executor: {cells}" in _usage_lines(env), _usage_lines(env)


def test_cancelled_task_keeps_its_spend_visible(env):
    approved_run(env)
    _enable(env)
    _add_task(env, "T3")
    _set_task_status(env, "T3", "cancelled")
    _dispatch(env, "D-944-cancel")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    _set_task_status(env, "T1", "cancelled")
    cells = "1 turn(s), input 10, output 5, cache read unknown, cache write unknown, cost unknown x1"
    assert f"usage T1/executor: {cells}" in _usage_lines(env), _usage_lines(env)
    code, data = env.ojson("inspect", "economics")
    assert "  task T1/executor: 1 | 10 | 5 | unknown | unknown | unknown x1" in "\n".join(data["lines"]), data
    assert [t["task"] for t in data["data"]["tasks"]] == ["T1"], data
    assert data["data"]["unmeasured_tasks"] == [], "a cancelled task with no usage is not reported as unmeasured"


def test_disabling_collection_keeps_recorded_usage_in_default_status(env):
    approved_run(env)
    _enable(env)
    _dispatch(env, "D-944-disabled")
    _claude_transcript(env, [_claude_entry("m1", input_tokens=10, output_tokens=5)])
    env.office("economics", "ingest", check=0)
    _enable(env, on=False)
    cells = "1 turn(s), input 10, output 5, cache read unknown, cache write unknown, cost unknown x1"
    assert _usage_lines(env) == [f"usage: {cells}", f"usage run/executor: {cells}", f"usage T1/executor: {cells}",
                                 OFF_NOW], _usage_lines(env)
    assert _status_verbose(env).count(OFF_NOW) == 1, "the off notice prints once, from the default lines"
