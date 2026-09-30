"""3.2 plan diagram: lanes derived from depends, route preview, amend deltas."""
from __future__ import annotations

from conftest import PLAN_TWO, start_inline

from office import plan_view, planfile

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
    assert "why: " in out and "review: " in out, out
    assert ("checkpoints: T1 accepted [checks, code review] -> T2 accepted [checks, code review] "
            "-> integration review -> closeout") in out, out
    body = (env.repo / ".office" / "PLAN.md").read_text()
    assert planfile.DIAGRAM_BEGIN in body and planfile.DIAGRAM_END in body
    # The generated block is output, not plan content: resubmitting is a no-op.
    code, out = env.office("submit")
    assert code == 0 and "already submitted" in out, out


def test_depends_become_stacked_waves(env):
    env.trust()
    out = start_inline(env, plan=PLAN_STACKED, gear="direct")
    assert "review: " not in out and "T2 accepted [checks] ->" in out, out
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
    assert "<!-- office:diagram p2" in (env.repo / ".office" / "PLAN.md").read_text()


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
