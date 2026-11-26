"""Every dispatch records the task's size class, the run gear and fastlane (#1363).

Per-size executor scoring reads dispatch records: packet.json and the dispatches row carry the size of the task
(or, for a lane review, the largest size of the lane's tasks), never a missing key; an unsized task says `unsized`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, approved_run, task_row
from office import state
from test_route_learning_discovery import con, seed, outcome as learning_outcome  # noqa: F401 - fixtures

SIZED_PLAN = PLAN_ONE.replace(
    "visual: none\n",
    "visual: none\ntask_size: M\ntask_size_rationale: one function and its check\n", 1)


def _packet(env, dispatch_id: str) -> dict:
    return json.loads(next((env.state / "runs").glob(f"*/dispatches/{dispatch_id}/packet.json")).read_text())


def _rows(env, role: str) -> list[dict]:
    con = env.con()
    try:
        return [dict(r) for r in con.execute("SELECT id, size_class FROM dispatches WHERE role=? ORDER BY started_at",
                                             (role,))]
    finally:
        con.close()


@pytest.mark.parametrize("task, want", [
    ({"descriptor": {"task_size": "S"}}, "S"),
    ({"descriptor": {"task_size": "XL"}}, "XL"),
    ({"descriptor": {}}, "unsized"),
    ({"descriptor": {"task_size": "huge"}}, "unsized"),
    ({}, "unsized"),
    (None, "unsized"),
])
def test_task_size_reads_the_descriptor_or_says_unsized(task, want):
    assert state.task_size(task) == want


def test_task_size_never_inherits_the_run_size():
    """F4 rebuttal: Rico's 2026-11-26 decision is to refuse unsized tasks, not to inherit the run size."""
    import inspect
    assert list(inspect.signature(state.task_size).parameters) == ["task"]


@pytest.mark.parametrize("sizes, want", [
    (["S", "XL", "M"], "XL"),
    (["unsized", "XS"], "XS"),
    (["unsized"], "unsized"),
    ([], "unsized"),
])
def test_largest_size_ignores_unsized(sizes, want):
    assert state.largest_size(sizes) == want


def test_recorded_size_reads_unsized_as_null():
    assert state.recorded_size("unsized") is None and state.recorded_size("L") == "L" and state.recorded_size(None) is None


def test_route_learning_reads_an_unsized_column_like_null(con, seed):
    """The column now says `unsized` where it was NULL; route learning's size fallback must not change."""
    seed.run("R1")
    seed.task("R1", "T1", accepted="V1")
    seed.dispatch("R1", "D1", "T1")
    seed.revision("R1", "V1", "D1", "T1")
    con.execute("UPDATE dispatches SET size_class=NULL WHERE id='D1'")
    before = learning_outcome(con, "D1")["size_class"]
    con.execute("UPDATE dispatches SET size_class='unsized' WHERE id='D1'")
    after = learning_outcome(con, "D1")["size_class"]
    assert after == before and after != "unsized"
    con.execute("UPDATE dispatches SET size_class='L' WHERE id='D1'")
    assert learning_outcome(con, "D1")["size_class"] == "L"


def test_context_reads_a_null_descriptor_as_unsized(env):
    approved_run(env, executor=[{}])
    con = env.con()
    con.execute("UPDATE tasks SET descriptor_json=NULL WHERE id='T1'")
    con.commit()
    code, out = env.office("context")
    assert "[unsized]: Implement add" in out, out


def test_size_record_never_substitutes_the_run_size_for_a_task_size():
    rec = state.size_record({"gear": "S", "risk": {"size_class": "S"}}, {"T2": "unsized", "T1": "unsized"})
    assert rec == {"task_size": "unsized", "task_sizes": {"T1": "unsized", "T2": "unsized"},
                   "run_gear": "S", "run_size_class": "S", "fastlane": False}


def test_lane_gate_sizes_cover_every_lane_task_and_integration_covers_the_run(monkeypatch):
    from office import convergence, gates
    sizes = {"T1": "S", "T2": "L", "T3": None}
    monkeypatch.setattr(convergence, "find_scope", lambda con, run, scope: {"tasks": ["T1", "T2", "T3"]})
    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: {"descriptor": {"task_size": sizes[tid]}})
    run = {"id": "R"}
    lane = gates.gate_task_sizes(None, run, {"scope": "L-T1"}, "code_reviewer")
    assert lane == {"T1": "S", "T2": "L", "T3": "unsized"}
    assert state.size_record(run, lane)["task_size"] == "L"
    assert gates.gate_task_sizes(None, run, {"task_id": "T1"}, "code_reviewer") == {"T1": "S"}

    class Con:
        def execute(self, sql, args):
            assert "status != 'cancelled'" in sql and args == ("R", "P1")
            return [("T1",), ("T2",)]
    assert gates.gate_task_sizes(Con(), run, {}, "integration_reviewer") == {"T1": "S", "T2": "L"}
    assert gates.gate_task_sizes(Con(), run, {}, "plan_reviewer") == {}


def test_sized_executor_and_lane_reviewer_record_the_size(env):
    approved_run(env, plan=SIZED_PLAN, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    (ex,) = _rows(env, "executor")
    assert ex["size_class"] == "M"
    pkt = _packet(env, ex["id"])
    assert (pkt["task_size"], pkt["task_sizes"], pkt["run_gear"]) == ("M", {"T1": "M"}, "S")
    assert isinstance(pkt["fastlane"], bool) and "run_size_class" in pkt, pkt  # the fixture sets no intake size
    reviewers = _rows(env, "code_reviewer")  # the lane convergence reviewer
    assert reviewers, "the lane convergence review dispatched no reviewer"
    for r in reviewers:
        assert r["size_class"] == "M"
        rp = _packet(env, r["id"])
        assert rp["packet_kind"] == "reviewer-dispatch" and rp["gate_kind"] == "convergence_review", rp
        assert (rp["task_size"], rp["task_sizes"], rp["run_gear"]) == ("M", {"T1": "M"}, "S"), rp
    code, out = env.office("status")
    assert "sizes: T1 M" in out, out


def test_unsized_task_records_unsized_and_context_shows_it(env):
    approved_run(env, executor=[{}])
    con = env.con()  # a task whose descriptor names no size (a legacy plan)
    con.execute("UPDATE tasks SET descriptor_json='{}' WHERE id='T1'")
    con.commit()
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    (ex,) = _rows(env, "executor")
    assert ex["size_class"] == "unsized"
    pkt = _packet(env, ex["id"])
    assert pkt["task_size"] == "unsized" and pkt["task_sizes"] == {"T1": "unsized"}, pkt
    code, out = env.office("status")
    assert "sizes: T1 unsized" in out, out
    code, out = env.office("context")
    assert "  T1 running [unsized]: Implement add" in out, out


@pytest.mark.review_contract("v3.1")
@pytest.mark.approved
def test_task_code_reviewer_records_the_reviewed_task_size(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    size = state.task_size(task_row(env) | {"descriptor": json.loads(task_row(env)["descriptor_json"] or "{}")})
    reviewers = _rows(env, "code_reviewer")
    assert reviewers
    for r in reviewers:
        assert r["size_class"] == size
        rp = _packet(env, r["id"])
        assert rp["task_size"] == size and rp["task_sizes"] == {"T1": size} and rp["run_gear"] == "S", rp


def test_conformance_probe_dispatch_records_unsized(tmp_path, monkeypatch, request):
    """#1365: the vision probe writes its own dispatches row (no task), so it records unsized, never NULL or the run size."""
    import uuid

    from office import conformance, db, dispatch, version
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    con = db.connect(tmp_path / "runs.db")
    run_id = "R" + uuid.uuid4().hex[:8]
    # A sized run: a probe that copied the run size would record L here instead of unsized.
    con.execute("INSERT INTO runs(id, status, requirements_version, plan_version, office_version, gear, risk_json) "
                "VALUES(?, 'executing', 1, 1, ?, 'M', ?)", (run_id, version.current(), json.dumps({"size_class": "L"})))
    con.commit()
    request.addfinalizer(con.close)
    run = state.get_run(con, run_id)
    seen = {}

    def launch(run_, d, kind, ddir, *, output, **kw):
        seen["size_class"] = d.get("size_class")
        output.write_text("PROBE NO_IMAGE\n")

    monkeypatch.setattr(dispatch, "launch", launch)
    monkeypatch.setattr(dispatch, "reclaim_pane", lambda run_, did, **kw: "closed")
    cand = {"harness": "agy", "invocation_model_id": "gemini-3.8-flash", "effort": "low", "adapter_id": "agy",
            "model_id": "gemini-3.8-flash", "harness_version": "1"}
    conformance.probe_vision(con, run, cand, {"id": "agy"})
    rows = [dict(r) for r in con.execute(
        "SELECT size_class FROM dispatches WHERE run_id=? AND kind='conformance'", (run_id,))]
    assert rows == [{"size_class": state.UNSIZED}]
    assert seen["size_class"] == state.UNSIZED
