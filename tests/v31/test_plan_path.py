"""Plan drafts are per run: .office/plans/<run>/PLAN.md."""
from __future__ import annotations

from conftest import PLAN_ONE, PLAN_TWO, start_inline


def _runs(env):
    con = env.con()
    try:
        return [r[0] for r in con.execute("SELECT id FROM runs ORDER BY created_at")]
    finally:
        con.close()


def _draft(env, run_id):
    return env.repo / ".office" / "plans" / run_id[:8] / "PLAN.md"


def test_start_names_the_run_draft(env):
    code, data = env.ojson("start", "add a feature", "--planner", "inline")
    assert code == 0, data
    assert f".office/plans/{data['data']['run_id'][:8]}/PLAN.md" in data["next"], data["next"]


def test_a_new_run_does_not_see_another_runs_draft(env):
    env.trust()
    start_inline(env, plan=PLAN_ONE)
    first = _runs(env)[0]
    env.office("close", "--abandon", "superseded", check=0)
    assert not _draft(env, first).parent.exists()
    env.office("start", "second goal", "--planner", "inline", check=0)
    second = _runs(env)[-1]
    code, out = env.office("submit")
    assert code != 0 and f".office/plans/{second[:8]}/PLAN.md" in out, out


def test_legacy_plan_moves_to_the_run_it_matches(env):
    env.trust()
    start_inline(env, plan=PLAN_ONE)
    run_id = _runs(env)[0]
    body = _draft(env, run_id).read_text()
    _draft(env, run_id).unlink()
    (env.repo / ".office" / "PLAN.md").write_text(body)
    env.office("start", "another goal", "--planner", "inline", check=0)
    assert not (env.repo / ".office" / "PLAN.md").exists()
    assert _draft(env, run_id).read_text() == body
    assert not _draft(env, _runs(env)[-1]).exists()


def test_unmatched_legacy_plan_moves_aside(env):
    (env.repo / ".office").mkdir(exist_ok=True)
    (env.repo / ".office" / "PLAN.md").write_text(PLAN_TWO)
    code, out = env.office("start", "fresh goal", "--planner", "inline")
    assert code == 0 and "moved legacy .office/PLAN.md" in out, out
    assert not (env.repo / ".office" / "PLAN.md").exists()
    assert list((env.repo / ".office" / "plans").glob("legacy-*.md"))
