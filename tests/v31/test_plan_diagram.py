"""3.2 plan diagram: lanes derived from depends, route preview, amend deltas."""
from __future__ import annotations

from conftest import PLAN_TWO, start_inline

from office import plan_view, planfile, state


def _draft(env):
    return next((env.repo / ".office" / "plans").glob("*/PLAN.md"))

PLAN_STACKED = PLAN_TWO.replace("### T2: Implement mul\nscope: mul.py\ndepends: none",
                                "### T2: Implement mul\nscope: mul.py\ndepends: T1")
PLAN_THREE = PLAN_TWO + """
### T3: Docs
scope: README.md
depends: T1, T2
checks: none
accept:
- README mentions mul
visual: none
"""


def test_submit_shows_parallel_lanes_routes_and_checkpoints(env):
    env.trust()
    out = start_inline(env, plan=PLAN_TWO)
    assert "plan p1 diagram" in out, out
    assert "wave 1  (T1 | T2 in parallel)" in out, out
    assert out.count("off base") == 2, out
    # #300: each executor task shows the Inline Slate (primary + fallbacks, + and - each).
    assert "ROUTING" in out and "PRIMARY" in out and "FALLBACK 1" in out and "review: " in out, out
    assert ("checkpoints: wave 1 {T1 accepted [checks] | T2 accepted [checks]} "
            "-> lane T1 convergence review -> lane T2 convergence review -> handoff PR ") in out, out
    assert "end state: ask" in out, out
    body = _draft(env).read_text()
    assert planfile.DIAGRAM_BEGIN in body and planfile.DIAGRAM_END in body
    # The generated block is output, not plan content: resubmitting is a no-op.
    code, out = env.office("submit")
    assert code == 0 and "already submitted" in out, out


def test_depends_become_stacked_waves(env):
    env.trust()
    out = start_inline(env, plan=PLAN_STACKED, gear="direct")
    assert "review: " not in out and "wave 2 {T2 accepted [checks]}" in out, out
    assert "wave 2" in out and "stacked on T1" in out, out
    code, out = env.office("inspect", "plan")
    assert code == 0 and "stacked on T1" in out, out


def test_contract_amendment_shows_only_the_delta(env):
    env.trust()
    start_inline(env, plan=PLAN_TWO)
    env.office("approve", "plan", "--quote", "approved", check=0)
    env.write_plan(PLAN_THREE)
    code, out = env.office("amend", "plan", "--contract", "--", "add docs task")
    assert code == 0, out
    assert "changes vs p1:" in out and "T3 added: Docs" in out and "stacked on T2 (+ needs T1)" in out, out
    assert "wave 1" not in out and "full diagram: office inspect plan" in out, out
    assert "<!-- office:diagram p2" in _draft(env).read_text()


def test_layout_and_diff_units():
    lay = plan_view.layout([{"id": "T1", "depends": []}, {"id": "T2", "depends": []},
                            {"id": "T3", "depends": ["T1", "T2"]}, {"id": "T4", "depends": ["T3"]}])
    assert lay["T3"] == {"wave": 2, "base": "T2", "needs": ["T1"]}
    assert lay["T4"]["wave"] == 3 and lay["T4"]["base"] == "T3"
    task = {"title": "x", "depends": [], "wave": 1, "base": None, "needs": [], "route": "codex/gpt-5.5@high",
            "why": "preferred seed #1", "review": None, "review_why": None}
    prev = {"tasks": {"T4": task}}
    cur = {"tasks": {"T4": {**task, "route": "claude/claude-sonnet-5-5@medium", "why": "quota ok"}}}
    assert plan_view.diff(prev, cur, 1) == [
        "changes vs p1:", "  T4 route: codex/gpt-5.5@high -> claude/claude-sonnet-5-5@medium (quota ok)"]
    assert plan_view.diff(prev, prev, 1) == ["diagram unchanged from p1"]


def test_checkpoints_match_each_review_contract():
    tasks = {
        "T1": {"wave": 1, "gates": ["checks"], "depends": [], "lane": "alpha",
               "converge": ["release"], "visual_gate": False},
        "T2": {"wave": 1, "gates": [], "depends": [], "lane": "beta",
               "converge": ["release"], "visual_gate": True},
        "T3": {"wave": 2, "gates": ["checks"], "depends": ["T1"], "lane": "alpha",
               "converge": [], "visual_gate": False},
    }
    pv = {"tasks": tasks, "end_state": "ask"}
    convergence = plan_view.checkpoints({"gates": {"review_contract": "convergence-v1", "code_review": True}}, pv)
    assert convergence.startswith("wave 1 {T1 accepted [checks] | T2 accepted} -> wave 2 {T3 accepted [checks]}")
    assert "lane alpha convergence review" in convergence
    assert "lane alpha visual review" not in convergence
    assert "lane beta convergence review" in convergence and "lane beta visual review" in convergence
    assert "shared-scope release review" in convergence
    assert "integration review" not in convergence

    legacy_tasks = {tid: {**task, "gates": [*task["gates"], "code review"]}
                    for tid, task in tasks.items()}
    legacy_tasks["T2"]["gates"].append("ui review")
    legacy = plan_view.checkpoints({"gates": {"review_contract": "v3.1"}}, {**pv, "tasks": legacy_tasks})
    assert legacy.startswith("wave 1 {T1 accepted [checks, code review] | T2 accepted [code review, ui review]} "
                             "-> wave 2 {T3 accepted [checks, code review]}")
    assert legacy.count("integration review") == 1
    assert "convergence review" not in legacy and "visual review" not in legacy


def test_checkpoints_use_shared_convergence_lane_grouping(monkeypatch):
    calls = []
    group = plan_view.convergence.group_planned_tasks

    def observe(tasks):
        calls.append(tasks)
        return group(tasks)

    monkeypatch.setattr(plan_view.convergence, "group_planned_tasks", observe)
    plan_view.checkpoints(
        {"gates": {"review_contract": "convergence-v1", "code_review": True}},
        {"tasks": {"T1": {"wave": 1, "gates": [], "depends": [], "lane": "alpha",
                            "converge": [], "visual_gate": False}}})
    assert calls == [[{"id": "T1", "depends": [], "lane": "alpha"}]]


def test_dispatched_route_is_rendered_and_omitted_from_amendment_churn():
    previous = {"tasks": {"T1": {"title": "Build", "wave": 1, "base": None, "needs": [],
                                 "route": "codex/preview@high", "review": None}}}
    dispatched = {"tasks": {"T1": {**previous["tasks"]["T1"], "route": "claude/actual@medium",
                                    "dispatched_route": "claude/actual@medium"}}}
    lines = plan_view.render({}, 2, dispatched)
    assert any("dispatched: claude/actual@medium" in line for line in lines), lines
    assert not any("ROUTING" in line for line in lines), lines
    assert plan_view.diff(previous, dispatched, 1) == ["diagram unchanged from p1"]


def test_preview_pins_a_current_dispatch_route(env):
    env.trust()
    start_inline(env, plan=PLAN_TWO)
    con = env.con()
    try:
        run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        run = state.get_run(con, run_id)
        previous = plan_view.load(con, run_id, 1)
        state.update_task(con, run_id, "T1", current_dispatch_id="D-dispatched")
        con.execute("INSERT INTO dispatches(id, run_id, task_id, triple) VALUES(?,?,?,?)",
                    ("D-dispatched", run_id, "T1", "codex/dispatched@high"))
        tasks = state.current_plan(con, run_id)["tasks"]
        current = plan_view.preview(con, run, tasks)
        assert current["tasks"]["T1"]["route"] == "codex/dispatched@high"
        assert current["tasks"]["T1"]["dispatched_route"] == "codex/dispatched@high"
        assert not any(line.startswith("  T1 route:") for line in plan_view.diff(previous, current, 1))
    finally:
        con.close()


def test_short_why_drops_gate_boilerplate():
    reason = ("cleared the applicable trust, capability, role-floor, and task-shape gates; "
              "fit within the protected quota reserve; matched preferred seed #2, which decided the advisory ranking")
    assert plan_view.short_why({"reason": reason}) == "quota ok, preferred seed #2"


def test_dispatch_reports_drift_from_the_preview(env):
    env.trust()
    start_inline(env)
    env.office("approve", "plan", "--quote", "approved", check=0)
    code, out = env.office("dispatch", "T1", "--as", "claude/some-future-model@low",
                           env={"OFFICE_WORKER_LAUNCHER": "external"})
    assert code == 0, out
    assert "T1 route differs from the plan preview: " in out and "some-future-model@low" in out, out
