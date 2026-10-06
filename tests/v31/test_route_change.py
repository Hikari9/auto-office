"""office amend route: re-record a live dispatch's producer route (#301).

Agents never run here: the dispatch is launched external, so its row stays live
and the in-process launcher mock (conftest) owns any relaunch.

The `env` fixture is taken through `request.getfixturevalue`: a test that names
it statically is tiered `integration` and deselected from this module's check
(`pytest -q tests/v31/test_route_change.py`, no -m).
"""
from __future__ import annotations

import json

import pytest

from conftest import GOOD_ADD, approved_run

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
NEW = "claude/sonnet@high"
QUOTE = "I switched the pane to sonnet"


@pytest.fixture
def live(request, monkeypatch):
    env = request.getfixturevalue("env")
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}] * 3)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    for k, v in EXTERNAL.items():
        monkeypatch.setenv(k, v)
    return env


def _run(con):
    from office import state
    return state.get_run(con, con.execute("SELECT run_id FROM tasks WHERE id='T1'").fetchone()[0])


def _snapshot(con):
    rows = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at")]
    events = con.execute("SELECT COUNT(*) FROM events WHERE kind='route.changed'").fetchone()[0]
    return rows, events


def _route_events(con):
    return [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE kind='route.changed'")]


pytestmark = pytest.mark.approved


def test_record_only_rewrites_the_route_and_keeps_lease_and_session(live):
    con = live.con()
    before = dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1'").fetchone())
    code, out = live.office("amend", "route", "T1", "--as", NEW, "--quote", QUOTE, env=EXTERNAL)
    assert code == 0 and f"route {before['triple']} -> claude@1/sonnet@high" in out, out
    rows, _ = _snapshot(con)
    assert len(rows) == 1, "a record-only change launches nothing"
    after = rows[0]
    route = json.loads(after["route_json"])
    assert after["triple"] == "claude@1/sonnet@high" and after["effort"] == "high"
    assert after["model"] == after["invocation_model_id"] == route["candidate"]["invocation_model_id"] != before["model"]
    assert route["candidate"]["model_id"] == "sonnet" and route["route_change"]["quote"] == QUOTE
    for col in ("lease_id", "session_id", "harness", "status", "worktree", "pane_id"):
        assert after[col] == before[col], col
    lease = con.execute("SELECT revoked_at, released_at FROM leases WHERE id=?", (before["lease_id"],)).fetchone()
    assert lease["revoked_at"] is None and lease["released_at"] is None
    assert _route_events(con) == [{"before": before["triple"], "after": after["triple"], "quote": QUOTE,
                                   "restart": False, "dispatch_id": before["id"]}]
    _, out = live.office("inspect", "task", "T1", env=EXTERNAL)
    assert f"dispatch {before['id']} executor claude@1/sonnet@high" in out and "user override" in out, out


@pytest.mark.parametrize("args,code,needle", [
    (("--as", "codex/gpt-6-luna@high", "--quote", QUOTE), 4, "harness-fixed"),
    (("--as", "claude/no-such-model", "--quote", QUOTE), 2, "known claude models:"),
    (("--as", "claude/sonnet@turbo", "--quote", QUOTE), 2, "known claude efforts:"),
    (("--as", NEW), 2, "--quote"),
    (("--as", "{same}", "--quote", QUOTE), 4, "already records"),
])
def test_refusals_leave_the_dispatch_and_events_untouched(live, args, code, needle):
    con = live.con()
    d = con.execute("SELECT model, effort FROM dispatches WHERE task_id='T1'").fetchone()
    args = tuple(a.format(same=f"claude/{d['model']}@{d['effort']}") for a in args)
    snap = _snapshot(con)
    got, out = live.office("amend", "route", "T1", *args, env=EXTERNAL)
    assert got == code and needle in out, out
    assert _snapshot(con) == snap


def test_unknown_model_lists_the_known_options(live):
    _, out = live.office("amend", "route", "T1", "--as", "claude/nope", "--quote", QUOTE, env=EXTERNAL)
    assert "sonnet" in out and "claude-opus-5-5" in out, out


def test_a_dispatch_that_is_not_live_is_refused_naming_the_command(live):
    con = live.con()
    con.execute("UPDATE dispatches SET status='exited', terminal_classification='success', ended_at=started_at")
    con.commit()
    snap = _snapshot(con)
    code, out = live.office("amend", "route", "T1", "--as", NEW, "--quote", QUOTE, env=EXTERNAL)
    assert code == 4 and "dispatch-not-live" in out and "office rerun T1 --fresh --reroute" in out, out
    assert _snapshot(con) == snap


def test_refused_after_submit(live):
    con = live.con()
    d = con.execute("SELECT id, run_id FROM dispatches WHERE task_id='T1'").fetchone()
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, requirements_version, "
                "plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                "VALUES('R1', ?, 'T1', 1, ?, 'c', 't', 1, 1, 1, 'e', 'op1', 'submitted', '2026-10-07')",
                (d["run_id"], d["id"]))
    con.commit()
    snap = _snapshot(con)
    code, out = live.office("amend", "route", d["id"], "--as", NEW, "--quote", QUOTE, env=EXTERNAL)
    assert code == 4 and "already submitted" in out, out
    assert _snapshot(con) == snap


@pytest.mark.parametrize("pin,dropped", [("claude/claude-haiku-4-5@low", True), ("codex/gpt-6-luna@xhigh", False)])
def test_a_pinned_review_that_now_shares_the_family_routes_again(live, pin, dropped):
    con = live.con()
    # The producer starts on gpt so a claude pin is independent until the change.
    con.execute("UPDATE tasks SET review_override_json=? WHERE id='T1'", (json.dumps({"as": pin, "by": "user"}),))
    con.commit()
    code, out = live.office("amend", "route", "T1", "--as", NEW, "--quote", QUOTE, env=EXTERNAL)
    assert code == 0, out
    kept = con.execute("SELECT review_override_json FROM tasks WHERE id='T1'").fetchone()[0]
    rerouted = con.execute("SELECT COUNT(*) FROM events WHERE kind='review.rerouted'").fetchone()[0]
    if dropped:
        assert json.loads(kept) is None and rerouted == 1 and "routes again" in out, out
    else:
        assert json.loads(kept)["as"] == pin and rerouted == 0, out


def test_restart_without_resume_starts_fresh_on_the_same_worktree(live, monkeypatch):
    from office import dispatch, jobs, routechange
    monkeypatch.setattr(dispatch, "herdr_usable", lambda: False)
    monkeypatch.setattr(jobs, "kick", lambda con, run_id=None: 0)
    con = live.con()
    old = dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1'").fetchone())
    res = routechange.change_route(con, _run(con), "T1", NEW, QUOTE, restart=True)
    assert any("fresh session (worktree preserved)" in line for line in res.lines), res.lines
    rows, _ = _snapshot(con)
    assert len(rows) == 2
    prev, new = rows
    assert prev["triple"] == new["triple"] == "claude@1/sonnet@high", "the route is recorded before the interrupt"
    assert prev["ended_at"] and prev["terminal_classification"] == "route-restart"
    assert new["worktree"] == old["worktree"] and new["status"] == "launching" and new["harness"] == "claude"
    assert con.execute("SELECT current_dispatch_id FROM tasks WHERE id='T1'").fetchone()[0] == new["id"]
    assert con.execute("SELECT revoked_at FROM leases WHERE id=?", (old["lease_id"],)).fetchone()[0]
    job = json.loads(con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' "
                                 "ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    assert job["dispatch_id"] == new["id"] and "resume" not in job
    (event,) = _route_events(con)
    assert event["restart"] and event["relaunch"] == new["id"] and event["how"].startswith("fresh")


def test_restart_resumes_the_session_with_the_new_model(live, monkeypatch):
    from office import dispatch, jobs, routechange
    monkeypatch.setattr(dispatch, "herdr_usable", lambda: True)
    monkeypatch.setattr(jobs, "kick", lambda con, run_id=None: 0)
    con = live.con()
    old = dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1'").fetchone())
    res = routechange.change_route(con, _run(con), old["id"], NEW, QUOTE, restart=True)
    assert any(f"resuming session {old['session_id']}" in line for line in res.lines), res.lines
    new = con.execute("SELECT * FROM dispatches WHERE task_id='T1' AND id<>?", (old["id"],)).fetchone()
    assert new["resumed_from"] == old["id"]
    resume = json.loads(con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' "
                                    "ORDER BY created_at DESC LIMIT 1").fetchone()[0])["resume"]
    argv = resume["argv"]
    assert argv[argv.index("--model") + 1] == new["model"] and new["model"].startswith("claude-sonnet") and argv[-2:] == ["--resume", old["session_id"]]
