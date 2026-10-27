"""#790 (T2): a reviewer that never worked ends honestly, a pointer that never landed is retried once on the
same route, and a review that leaves its route says so where the user reads it.

Lane reviews run the default convergence-v1 contract with scripted fake harnesses; no real agent runs.
"""
from __future__ import annotations

import json

from test_convergence_contract import APPROVED, _start
from test_review_reroute import EXECUTOR, GARBAGE, _q

EMPTY = {"reply": ""}


def _events(env, kind):
    return _q(env, "SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,))


def _lane_route_changes(env):
    return [json.loads(e["payload_json"]) for e in _events(env, "route.changed")
            if json.loads(e["payload_json"]).get("notice") == "review"]


def test_a_reviewer_that_wrote_no_reply_file_ends_with_an_event_naming_the_empty_reply(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    events = _events(env, "review.empty_reply")
    assert len(events) == 1, [e["summary"] for e in _q(env, "SELECT summary FROM events")]
    assert "empty reply" in events[0]["summary"] and "no reply file was written" in events[0]["summary"]
    assert "ended success" in events[0]["summary"] and events[0]["audience"] == "orchestrator"
    payload = json.loads(events[0]["payload_json"])
    assert payload["classification"] == "success"
    first = _q(env, "SELECT triple FROM dispatches WHERE role='code_reviewer' ORDER BY started_at")[0]["triple"]
    assert payload["route"] == first and first in events[0]["summary"]


def test_a_reviewer_that_replied_unreadably_is_not_called_empty(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[GARBAGE])
    env.office("dispatch", "T1", check=0)
    assert not _events(env, "review.empty_reply")


def test_a_fallback_review_route_is_announced_in_status_and_inspect_route(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    first, second = [r["triple"] for r in _q(env, "SELECT triple FROM dispatches WHERE role='code_reviewer' "
                                                  "ORDER BY started_at")][:2]
    assert first != second
    (change,) = _lane_route_changes(env)
    assert change["before"] == first and change["after"] == second and change["kind"] == "reviewer-fallback"
    assert change["stage"] == "code_reviewer" and "no reply after re-prompts" in change["reason"]
    code, status = env.office("status")
    assert f"{first} -> {second}" in status and "review route change" in status, status
    code, route = env.office("inspect", "route")
    assert f"{first} -> {second}" in route and "[reviewer-fallback]" in route, route
    code, data = env.ojson("inspect", "route")
    assert data["data"]["review_route_changes"] and first in data["data"]["review_route_changes"][0]


def test_a_review_that_stays_on_its_route_emits_no_route_notice(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert not _lane_route_changes(env)
    code, status = env.office("status")
    assert "review route change" not in status


def _mark_unlanded(monkeypatch, *, only_first=True):
    """launch.json says the brief pointer never landed, for the first reviewer dispatch (or every one)."""
    from office import gates
    seen = []
    real = gates._launch_spec

    def spec(ddir):
        if only_first and seen and ddir.name not in seen[:1]:
            return real(ddir)
        seen.append(ddir.name)
        return {**real(ddir), "retry_same_route": "held-pointer"}
    monkeypatch.setattr(gates, "_launch_spec", spec)


def test_a_reviewer_whose_pointer_never_landed_runs_once_more_on_the_same_route_before_any_fallback(env, monkeypatch):
    _mark_unlanded(monkeypatch)
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    triples = [r["triple"] for r in _q(env, "SELECT triple FROM dispatches WHERE role='code_reviewer' ORDER BY started_at")]
    assert len(triples) == 2 and triples[0] == triples[1], triples
    (retry,) = _events(env, "review.retry_same_route")
    assert "before any fallback" in retry["summary"] and triples[0] in retry["summary"]
    assert not _lane_route_changes(env), "the same route is not a route change"
    assert _q(env, "SELECT review_status FROM gates WHERE kind='convergence_review'")[-1]["review_status"] == "COMPLETED"


def test_the_same_route_is_retried_only_once_then_the_next_fallback_is_taken(env, monkeypatch):
    _mark_unlanded(monkeypatch, only_first=False)
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY, EMPTY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    triples = [r["triple"] for r in _q(env, "SELECT triple FROM dispatches WHERE role='code_reviewer' ORDER BY started_at")]
    assert triples[0] == triples[1] and triples[2] != triples[1], triples
    assert len(_events(env, "review.retry_same_route")) == 1 and len(_events(env, "review.empty_reply")) == 2
    (change,) = _lane_route_changes(env)
    assert change["before"] == triples[1] and change["after"] == triples[2]


def test_no_unlanded_marker_means_no_same_route_retry(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert not _events(env, "review.retry_same_route")
    triples = [r["triple"] for r in _q(env, "SELECT triple FROM dispatches WHERE role='code_reviewer' ORDER BY started_at")]
    assert triples[0] != triples[1]


# ------------------------------------------------------------------ units (no run)

def test_launch_spec_reads_the_recorded_marker_and_tolerates_a_missing_or_broken_file(tmp_path):
    from office import gates
    assert gates._launch_spec(tmp_path) == {}
    (tmp_path / "launch.json").write_text("{not json")
    assert gates._launch_spec(tmp_path) == {}
    (tmp_path / "launch.json").write_text("[1]")
    assert gates._launch_spec(tmp_path) == {}
    (tmp_path / "launch.json").write_text(json.dumps({"retry_same_route": "held-pointer"}))
    assert gates._launch_spec(tmp_path)["retry_same_route"] == "held-pointer"


def test_review_route_lines_lists_review_notices_only_and_skips_a_task_already_listed():
    import sqlite3
    from office import gates, state
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL, "
                      "audience TEXT NOT NULL, task_id TEXT, dispatch_id TEXT, summary TEXT NOT NULL, payload_json TEXT, "
                      "office_version TEXT NOT NULL, created_at TEXT NOT NULL)")
    run = {"id": "r1"}
    gate = {"id": "G1", "task_id": None, "scope": "L-T1", "subject": "lane"}
    gates.review_route_notice(con, run, gate, "code_reviewer", "a/x@high", "b/y@high", "no reply after re-prompts")
    gates.review_route_notice(con, run, {**gate, "task_id": "T1"}, "code_reviewer", "b/y@high", "c/z@high", "quota")
    state.record_route_change(con, run, "T1", "p/q@high", "r/s@high", kind="reroute", reason="user", actor="user")
    lines = gates.review_route_lines(con, "r1")
    assert len(lines) == 2 and "L-T1" in lines[0] and "a/x@high -> b/y@high" in lines[0] and "[reviewer-fallback]" in lines[0]
    assert gates.review_route_lines(con, "r1", skip_task="T1") == lines[:1]
    assert gates.review_route_lines(con, "r1", limit=1) == lines[1:]
