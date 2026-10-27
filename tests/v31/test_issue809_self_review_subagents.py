"""#809 R10: per-gear self-review subagent funding before the convergence review.

XS none (the executor's inline four-lens self-check); S and M one four-lens subagent; L and XL up to four
specialists. Each is a separate reviewer session on the accepted revision. A report is reused for the same
revision, commit and rubric. A missing, invalid or wrong-commit report blocks convergence and spends no round.
"""
import pytest

from conftest import GOOD_ADD
from test_convergence_contract import APPROVED, _gates, _q, _roles, _start

from office import contract, gates, paths, self_review_subagents as srs, state

@pytest.fixture(autouse=True)
def _in_process_routing(request):
    """These tests patch `gates.run_reviewer`, which only reaches work that runs in this process (#841)."""
    if "env" in request.fixturenames:
        request.getfixturevalue("env").hide_agy = True


EXECUTOR = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]
RUN = {"gates": {"gear_model_version": "run-gear-1"}, "risk": {"classification": "low"}}


def _run(gear, **risk):
    return {**RUN, "gear": gear, "risk": {**RUN["risk"], **risk}}


# ------------------------------------------------------------------ the funding table

@pytest.mark.parametrize("gear, expected", [
    ("XS", ()), ("S", ("four-lens",)), ("M", ("four-lens",)),
    ("L", ("edge-cases", "test-strength")), ("XL", ("edge-cases", "test-strength")),
])
def test_gear_funds_the_specified_subagents(gear, expected):
    assert srs.rubrics(_run(gear), {"scope": ["calc.py"]}) == expected


def test_l_and_xl_add_specialists_for_what_the_task_touches_up_to_four():
    task = {"scope": ["scripts/auth.sh"]}
    for gear in ("L", "XL"):
        picked = srs.rubrics(_run(gear), task)
        assert set(picked) == {"security", "platform", "edge-cases", "test-strength"} and len(picked) == 4
    assert srs.rubrics(_run("L", classification="elevated"), {"scope": ["calc.py"]}) == (
        "security", "edge-cases", "test-strength")
    assert srs.rubrics(_run("XL"), {"scope": ["pyproject.toml"]}) == ("edge-cases", "platform", "test-strength")


@pytest.mark.parametrize("run", [
    {"gear": "S", "gates": {"gear_model_version": "legacy-pinned"}},
    {"gear": "express", "gates": {}}, {"gear": "direct+review", "gates": {}}, {"gear": None, "gates": {}},
])
def test_legacy_pinned_runs_keep_their_contract(run):
    assert srs.rubrics(run, {"scope": ["calc.py"]}) == ()
    assert srs.audit_scope(None, run, [], None, "abc")["reports"] == []


# ------------------------------------------------------------------ through the convergence gate

def _s_run(env, gear="S", **script):
    script.setdefault("code_reviewer", [{"reply": APPROVED}])
    script.setdefault("convergence_reviewer", [{"reply": APPROVED}])
    _start(env, gear=gear, executor=EXECUTOR, **({"plan_reviewer": [{"reply": APPROVED}]} if gear in ("L", "XL") else {}),
           **script)
    env.office("dispatch", "T1", check=0)


def _subagent_gates(env):
    return _gates(env, "self_review_subagent")


def test_an_s_run_launches_one_subagent_before_the_one_convergence_review(env):
    _s_run(env)
    (sub,) = _subagent_gates(env)
    assert sub["review_status"] == contract.COMPLETED and sub["verdict"] == "APPROVED" and sub["status"] == "done"
    roles = _roles(env)
    assert roles.count("code_reviewer") == 1 and roles.count("convergence_reviewer") == 1
    assert roles.index("code_reviewer") < roles.index("convergence_reviewer")
    (conv,) = _gates(env, "convergence_review")
    assert conv["verdict"] == "APPROVED" and conv["round"] == 1
    brief = (paths.run_dir(_q(env, "SELECT id FROM runs")[0]["id"]) / "dispatches" / conv["reviewer_dispatch_id"] / "brief.md").read_text()
    assert "PRE-CONVERGENCE SUBAGENT AUDITS" in brief and "four-lens" in brief
    sub_brief = (paths.run_dir(sub["run_id"]) / "dispatches" / sub["reviewer_dispatch_id"] / "brief.md").read_text()
    assert sub_brief.startswith("ROLE independent code reviewer (runtime self-review subagent")
    assert "is the commit under review" in sub_brief and sub["reviewer_dispatch_id"] != conv["reviewer_dispatch_id"]


def test_an_l_run_launches_its_specialists_and_keeps_the_initial_plan_review(env):
    _s_run(env, gear="L")
    assert len(_subagent_gates(env)) == 2
    assert {g["input_key"].split(":")[2] for g in _subagent_gates(env)} == {"edge-cases", "test-strength"}
    roles = _roles(env)
    assert roles.count("plan_reviewer") == 1 and roles.count("code_reviewer") == 2
    assert roles.count("convergence_reviewer") == 1


def test_a_run_with_the_legacy_gear_launches_no_subagent(env):
    _s_run(env, gear="direct+review")
    assert _subagent_gates(env) == [] and _roles(env).count("code_reviewer") == 0


def test_a_failed_subagent_blocks_convergence_and_spends_no_round(env, monkeypatch):
    real = gates.run_reviewer

    def failing(con, run, gate, role, brief, **kw):
        if "runtime self-review subagent" in brief:
            return {"status": contract.UNAVAILABLE, "verdict": None, "parsed": None, "summary": "no route"}
        return real(con, run, gate, role, brief, **kw)

    monkeypatch.setattr(gates, "run_reviewer", failing)
    _s_run(env)
    (sub,) = _subagent_gates(env)
    assert sub["review_status"] == contract.EVIDENCE_BLOCKED
    (conv,) = _gates(env, "convergence_review")
    assert conv["review_status"] == contract.EVIDENCE_BLOCKED and conv["verdict"] is None
    assert "convergence_reviewer" not in _roles(env), "no convergence verdict without the funded subagent"
    assert _q(env, "SELECT COUNT(*) n FROM findings")[0]["n"] == 0


def test_scripted_routing_offers_no_agy_row_so_in_process_patches_reach_the_convergence_jobs(env):
    """An agy executor has a wall cap, so its fake worker runs as a real process and its `office submit` drains the
    convergence jobs there, past a monkeypatch of `gates.run_reviewer`. #809 made agy Gemini the preferred S
    route, so the test above failed whenever the routing-seed draw picked it (#841)."""
    from office import candidates
    offered = [r for r in candidates.catalog_rows() if r.get("invocation_harness") == "agy" and r.get("dispatchable")]
    assert offered == [], [(r.get("model_id"), r.get("effort")) for r in offered]


def test_a_subagent_report_about_another_commit_blocks_convergence(env):
    _s_run(env, code_reviewer=[{"reply": "REVIEWED_COMMIT deadbeefdeadbeef\nVERDICT: APPROVED\nNEXT proceed"}])
    (sub,) = _subagent_gates(env)
    assert sub["review_status"] == contract.EVIDENCE_BLOCKED and sub["verdict"] is None
    assert "convergence_reviewer" not in _roles(env)
    events = _q(env, "SELECT summary FROM events WHERE kind='self_review.subagent.blocked'")
    assert events and "is not the composed commit" in events[0]["summary"]


def test_a_subagent_report_with_no_commit_line_blocks_convergence(env):
    _s_run(env, code_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed", "stamp": False}])
    (sub,) = _subagent_gates(env)
    assert sub["review_status"] == contract.EVIDENCE_BLOCKED
    assert "convergence_reviewer" not in _roles(env)


# ------------------------------------------------------------------ reuse, driven directly

def _after_run(env):
    _s_run(env)
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    tasks = [state.get_task(con, run["id"], "T1")]
    (sub,) = _subagent_gates(env)
    commit = sub["input_key"].split(":")[1]
    return con, run, tasks, commit


def _spy(env, run):
    calls = []

    def reviewer(con, run_, gate, role, brief, **kw):
        calls.append(brief)
        d = "Dspy" + str(len(calls))
        reply = paths.run_dir(run["id"]) / "dispatches" / d / "reply.txt"
        reply.parent.mkdir(parents=True, exist_ok=True)
        reply.write_text(f"REVIEWED_COMMIT {stated[0]}\nVERDICT: APPROVED\n")
        from office import review_parse
        return {"status": contract.COMPLETED, "dispatch_id": d,
                "parsed": review_parse.Parsed(verdict="APPROVED", contract=contract.CONVERGENCE)}

    stated = [""]
    return calls, reviewer, stated


def test_a_report_for_the_same_revision_commit_and_rubric_is_reused(env):
    con, run, tasks, commit = _after_run(env)
    calls, reviewer, stated = _spy(env, run)
    out = srs.audit_scope(con, run, tasks, env.repo, commit, reviewer=reviewer)
    assert out["status"] == contract.COMPLETED and calls == []
    assert [r["reused"] for r in out["reports"]] == [True]


def test_a_new_commit_is_reviewed_again(env):
    con, run, tasks, commit = _after_run(env)
    calls, reviewer, stated = _spy(env, run)
    other = "f" * 40
    stated[0] = other
    out = srs.audit_scope(con, run, tasks, env.repo, other, reviewer=reviewer)
    assert out["status"] == contract.COMPLETED and len(calls) == 1 and out["reports"][0]["reused"] is False
    assert len(_subagent_gates(env)) == 2


def test_a_direct_report_naming_another_commit_is_blocked(env):
    con, run, tasks, commit = _after_run(env)
    calls, reviewer, stated = _spy(env, run)
    stated[0] = "a" * 40
    out = srs.audit_scope(con, run, tasks, env.repo, "b" * 40, reviewer=reviewer)
    assert out["status"] == contract.EVIDENCE_BLOCKED and "not the composed commit" in out["reason"]


def test_a_task_with_no_accepted_revision_blocks(env):
    con, run, tasks, commit = _after_run(env)
    out = srs.audit_scope(con, run, [{**tasks[0], "accepted_revision_id": None}], env.repo, commit)
    assert out["status"] == contract.EVIDENCE_BLOCKED and "no accepted revision" in out["reason"]
