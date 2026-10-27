"""#809 R9: XL-only Elephant-Goldfish plan review, end to end and at the seams.

An XL run's INITIAL plan review is three mandatory Goldfish (requirements and comprehension; architecture,
reuse, simplicity and scalability; implementation readiness and codebase reality) plus up to two hazard
specialists, each a separate reviewer session, then ONE lead (Elephant) verdict. Later RECHECK rounds, XS/S/M/L
runs, legacy-pinned runs and runs whose XL is only a task size never get it. A missing or failed Goldfish
blocks the lead (fail closed) and spends no round.
"""
import pytest

from conftest import PLAN_ONE, start_inline
from office import contract, gates, plans, review_parse, state, xl_review

APPROVED = "VERDICT: APPROVED\nNEXT proceed"
RECHECK = "VERDICT: RECHECK\nFINDING P1 | high | blocking | T1 | add has no contract | add one\nNEXT fix it"
INTAKE_GAP = ("VERDICT: INTAKE_GAP\nNEXT ask\nDECISION which rounding mode\nAFFECTS T1\n"
              "WHY the requirements do not say")


def _parsed(verdict="APPROVED"):
    return review_parse.Parsed(verdict=verdict, contract=contract.CONVERGENCE)


def _launches(env, role="plan_reviewer"):
    return [c for c in env.calls() if c["role"] == role]


def _rows(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _goldfish(env):
    return _rows(env, "SELECT * FROM gates WHERE kind='goldfish_audit' ORDER BY created_at")


def _plan_gates(env):
    return _rows(env, "SELECT * FROM gates WHERE kind='plan_review' ORDER BY created_at")


# ------------------------------------------------------------------ end to end through plans.job_plan_review

def test_xl_initial_plan_review_runs_three_goldfish_then_one_distinct_lead(env):
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    start_inline(env, plan=PLAN_ONE, gear="XL")
    fish, lead = _goldfish(env), _plan_gates(env)
    assert [g["subject"] for g in fish] == ["goldfish:requirements", "goldfish:architecture", "goldfish:readiness"]
    assert all(g["status"] == "done" and g["review_status"] == contract.COMPLETED for g in fish)
    assert len(lead) == 1 and lead[0]["verdict"] == "APPROVED" and lead[0]["review_status"] == contract.COMPLETED
    assert len(_launches(env)) == 4  # three Goldfish and the Elephant, each a real harness launch
    ids = [g["reviewer_dispatch_id"] for g in fish] + [lead[0]["reviewer_dispatch_id"]]
    assert len(set(ids)) == 4 and all(ids)
    rows = _rows(env, "SELECT id, role, session_id, resumed_from, gate_id FROM dispatches WHERE role='plan_reviewer'")
    assert {r["id"] for r in rows} == set(ids)
    assert all(r["resumed_from"] is None for r in rows), "no reviewer continues another's session"
    assert len({r["gate_id"] for r in rows}) == 4
    sessions = [r["session_id"] for r in rows if r["session_id"]]
    assert len(sessions) == len(set(sessions))


def test_goldfish_get_their_rubric_and_the_lead_gets_their_reports(env):
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    start_inline(env, plan=PLAN_ONE, gear="XL")
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    from office import paths
    briefs = {r["subject"]: (paths.run_dir(run["id"]) / "dispatches" / r["reviewer_dispatch_id"] / "brief.md").read_text()
              for r in _goldfish(env)}
    assert "Requirements and comprehension" in briefs["goldfish:requirements"]
    assert "Architecture, reuse, simplicity and scalability" in briefs["goldfish:architecture"]
    assert "Implementation readiness and codebase reality" in briefs["goldfish:readiness"]
    for text in briefs.values():
        assert text.startswith("ROLE independent plan reviewer")  # the role line still routes the reviewer
        assert "INDEPENDENT XL GOLDFISH AUDIT" in text and "XL LEAD PLAN REVIEW" not in text
    lead = _plan_gates(env)[0]
    lead_brief = (paths.run_dir(run["id"]) / "dispatches" / lead["reviewer_dispatch_id"] / "brief.md").read_text()
    assert "XL LEAD PLAN REVIEW" in lead_brief and "INDEPENDENT XL GOLDFISH AUDIT" not in lead_brief
    assert all(f"AUDIT {name}:" in lead_brief for name in ("requirements", "architecture", "readiness"))


@pytest.mark.parametrize("verdict_reply, verdict", [(APPROVED, "APPROVED"), (RECHECK, "RECHECK"), (INTAKE_GAP, "INTAKE_GAP")])
def test_the_lead_gives_one_of_three_verdicts_after_the_goldfish(env, verdict_reply, verdict):
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}] * 3 + [{"reply": verdict_reply}])
    start_inline(env, plan=PLAN_ONE, gear="XL")
    (lead,) = _plan_gates(env)
    assert lead["verdict"] == verdict and len(_goldfish(env)) == 3
    assert state.get_run(env.con(), lead["run_id"])["plan_review"].get("status") != "unavailable"


def test_a_later_recheck_round_is_the_lead_alone(env):
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}] * 3 + [{"reply": RECHECK}, {"reply": APPROVED}])
    start_inline(env, plan=PLAN_ONE, gear="XL")
    assert len(_launches(env)) == 4 and len(_goldfish(env)) == 3
    env.write_plan(PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0"))
    env.office("amend", "T1", "--contract", "--", "revision", check=0)
    gates_ = _plan_gates(env)
    assert [g["verdict"] for g in gates_ if g["review_status"] == contract.COMPLETED] == ["RECHECK", "APPROVED"]
    assert len(_launches(env)) == 5, "round two is one reviewer: no new Goldfish"
    assert len(_goldfish(env)) == 3


def test_a_failed_goldfish_blocks_the_lead_and_spends_no_round(env, monkeypatch):
    real, seen = gates.run_reviewer, []

    def flaky(con, run, gate, role, brief, **kw):
        seen.append(gate["id"])
        if len(seen) == 2:  # the second Goldfish gets no report
            return {"status": contract.UNAVAILABLE, "verdict": None, "parsed": None, "summary": "no route answered"}
        return real(con, run, gate, role, brief, **kw)

    monkeypatch.setattr(gates, "run_reviewer", flaky)
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    start_inline(env, plan=PLAN_ONE, gear="XL")
    fish = _goldfish(env)
    assert [g["review_status"] for g in fish] == [contract.COMPLETED, contract.EVIDENCE_BLOCKED]
    assert len(seen) == 2 and len(_launches(env)) == 1, "the lead and the third Goldfish never launched"
    (lead,) = _plan_gates(env)
    assert lead["verdict"] is None and lead["review_status"] == contract.EVIDENCE_BLOCKED
    run = state.get_run(env.con(), lead["run_id"])
    assert run["plan_review"]["status"] == "unavailable"
    assert xl_review.initial_review(env.con(), run, {"id": "retry"}), "a blocked attempt is not a verdict"
    env.office("approve", "plan", "--quote", "go")
    code, out = env.office("dispatch", "T1")
    assert code != 0 and "plan" in out.lower(), "an XL plan with no lead verdict cannot be dispatched"


def test_a_retry_after_a_blocked_attempt_is_still_the_initial_review(env):
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    start_inline(env, plan=PLAN_ONE, gear="XL")
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    (lead,) = _plan_gates(env)
    con.execute("UPDATE gates SET review_status=?, verdict=NULL WHERE id=?", (contract.UNAVAILABLE, lead["id"]))
    con.commit()
    retry = {**lead, "id": "Gretry"}
    assert xl_review.initial_review(con, run, retry)
    con.execute("UPDATE gates SET review_status=?, verdict='RECHECK' WHERE id=?", (contract.COMPLETED, lead["id"]))
    con.commit()
    assert not xl_review.initial_review(con, run, retry), "the first verdict ends the initial review"


# ------------------------------------------------------------------ never for any other gear or sizing

@pytest.mark.parametrize("gear, plan_reviews", [("XS", 0), ("S", 0), ("M", 0), ("L", 1), ("express", 1)])
def test_no_other_gear_gets_goldfish(env, gear, plan_reviews):
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    start_inline(env, plan=PLAN_ONE, gear=gear)
    assert _goldfish(env) == []
    assert len(_launches(env)) == plan_reviews


def test_an_xl_task_size_in_an_l_run_is_not_an_xl_run(env):
    body = PLAN_ONE.replace("visual: none", "visual: none\ntask_size: XL\n"
                            "task_size_rationale: architecture across eight modules with three interfaces\n", 1)
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    start_inline(env, plan=body, gear="L")
    assert _goldfish(env) == [] and len(_launches(env)) == 1


# ------------------------------------------------------------------ the seams

def _xl_fixture(env):
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    start_inline(env, gear="XL")
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    gate = dict(con.execute("SELECT * FROM gates WHERE run_id=? AND kind='plan_review' ORDER BY created_at DESC LIMIT 1",
                            (run["id"],)).fetchone())
    # The runtime already reviewed this plan; isolate the injected reviewer from that attempt.
    con.execute("DELETE FROM gates WHERE run_id=? AND kind='goldfish_audit'", (run["id"],))
    con.execute("UPDATE gates SET status='queued', review_status=NULL, verdict=NULL WHERE id=?", (gate["id"],))
    con.commit()
    return con, run, {**gate, "status": "queued"}, state.current_plan(con, run["id"])


def test_goldfish_and_lead_are_distinct_calls_with_unique_gates(env):
    con, run, gate, plan = _xl_fixture(env)
    calls = []

    def reviewer(con, run, subgate, role, brief, **kw):
        calls.append((subgate["id"], brief, kw))
        return {"status": contract.COMPLETED, "verdict": "APPROVED", "parsed": _parsed(),
                "route": "agy/gemini-3.8-flash@medium", "dispatch_id": "D" + str(len(calls))}

    out = xl_review.audit_plan(con, run, gate, plan, "ROLE independent plan reviewer\nFROZEN", env.repo, reviewer=reviewer)
    assert out["verdict"] == "APPROVED" and out["goldfish"]["mandatory"] == 3 and out["goldfish"]["hazards"] == []
    assert len(calls) == 4 and len({c[0] for c in calls}) == 4
    assert calls[-1][0] == gate["id"] and "XL LEAD PLAN REVIEW" in calls[-1][1]
    assert all("resume_from" not in c[2] for c in calls), "no reviewer resumes another"
    assert all(c[1].startswith("ROLE independent plan reviewer\nINDEPENDENT XL GOLDFISH") for c in calls[:3])


def test_a_reviewer_that_reuses_a_goldfish_dispatch_is_not_independent(env):
    con, run, gate, plan = _xl_fixture(env)

    def reviewer(con, run, subgate, role, brief, **kw):
        return {"status": contract.COMPLETED, "verdict": "APPROVED", "parsed": _parsed(), "dispatch_id": "Dsame"}

    out = xl_review.audit_plan(con, run, gate, plan, "plan", env.repo, reviewer=reviewer)
    assert out["status"] == contract.EVIDENCE_BLOCKED and out["verdict"] is None


def test_a_lead_that_reuses_a_goldfish_session_is_refused(env):
    con, run, gate, plan = _xl_fixture(env)
    calls = []

    def reviewer(con, run, subgate, role, brief, **kw):
        calls.append(subgate["id"])
        return {"status": contract.COMPLETED, "verdict": "APPROVED", "parsed": _parsed(),
                "dispatch_id": "Dfish" if len(calls) == 1 else f"D{len(calls)}" if len(calls) < 4 else "Dfish"}

    # call 1 is Goldfish 1; the lead (call 4) answers with that dispatch again
    out = xl_review.audit_plan(con, run, gate, plan, "plan", env.repo, reviewer=reviewer)
    assert out["status"] == contract.EVIDENCE_BLOCKED and "distinct" in out["summary"]


def test_failed_goldfish_blocks_without_launching_the_lead(env):
    con, run, gate, plan = _xl_fixture(env)
    calls = []

    def reviewer(con, run, subgate, role, brief, **kw):
        calls.append(subgate["id"])
        return ({"status": contract.UNAVAILABLE, "verdict": None, "parsed": None} if len(calls) == 2 else
                {"status": contract.COMPLETED, "verdict": "APPROVED", "parsed": _parsed(), "dispatch_id": f"D{len(calls)}"})

    out = xl_review.audit_plan(con, run, gate, plan, "plan", env.repo, reviewer=reviewer)
    assert out["status"] == contract.EVIDENCE_BLOCKED and out["verdict"] is None and len(calls) == 2


def test_an_invalid_goldfish_report_is_a_failed_goldfish(env):
    con, run, gate, plan = _xl_fixture(env)
    bad = review_parse.Parsed(verdict=None, contract=contract.CONVERGENCE)

    def reviewer(con, run, subgate, role, brief, **kw):
        return {"status": contract.COMPLETED, "verdict": None, "parsed": bad, "dispatch_id": "D1"}

    out = xl_review.audit_plan(con, run, gate, plan, "plan", env.repo, reviewer=reviewer)
    assert out["status"] == contract.EVIDENCE_BLOCKED
    assert [g["review_status"] for g in _goldfish(env)] == [contract.EVIDENCE_BLOCKED]


def test_a_goldfish_that_raises_blocks_instead_of_crashing_or_approving(env):
    con, run, gate, plan = _xl_fixture(env)

    def reviewer(con, run, subgate, role, brief, **kw):
        raise RuntimeError("harness exploded")

    out = xl_review.audit_plan(con, run, gate, plan, "plan", env.repo, reviewer=reviewer)
    assert out["status"] == contract.EVIDENCE_BLOCKED and out["verdict"] is None


def test_audit_plan_refuses_a_run_that_is_not_an_initial_xl_review(env):
    con, run, gate, plan = _xl_fixture(env)
    with pytest.raises(ValueError):
        xl_review.audit_plan(con, {**run, "gear": "L"}, gate, plan, "plan", env.repo, reviewer=lambda *a, **k: {})
    con.execute("UPDATE gates SET review_status=?, verdict='RECHECK' WHERE id=?", (contract.COMPLETED, gate["id"]))
    con.commit()
    later = {**gate, "id": "Glater"}
    with pytest.raises(ValueError):
        xl_review.audit_plan(con, run, later, plan, "plan", env.repo, reviewer=lambda *a, **k: {})


def test_hazard_specialists_are_bounded_at_two_and_carry_their_trigger():
    run = {"gear": "XL", "gates": {"gear_model_version": "run-gear-1", "review_contract": contract.CONVERGENCE},
           "risk": {"blast_radius": "production-data", "irreversible": True}}
    plan = {"tasks": [{"scope": ["src/security/auth.py"], "visual": {"url": "http://localhost:1234"}}]}
    specs = xl_review.hazard_audits(run, plan)
    assert [s[0] for s in specs] == ["safety", "interaction"] and all(s[2] for s in specs)
    quiet = xl_review.hazard_audits({"risk": {"blast_radius": "repo"}}, {"tasks": [{"scope": ["README.md"]}]})
    assert quiet == []
    assert [s[0] for s in xl_review.hazard_audits({"risk": {}}, {"tasks": [{"scope": ["src/auth.py"]}]})] == ["security"]


def test_pinned_xl_needs_the_run_gear_not_a_task_size():
    base = {"gates": {"gear_model_version": "run-gear-1", "review_contract": contract.CONVERGENCE}}
    assert xl_review.pinned_xl({**base, "gear": "XL"})
    for gear in ("XS", "S", "M", "L", "express", None):
        assert not xl_review.pinned_xl({**base, "gear": gear})
    assert not xl_review.pinned_xl({"gear": "XL", "gates": {"gear_model_version": "legacy-pinned"}})
    assert not xl_review.pinned_xl({**base, "gear": "L", "task_size": "XL"})


def test_the_xl_gear_preset_names_the_goldfish():
    from office import config
    cfg = config.load_yaml(config.default_config_path())
    assert cfg["gear_presets"]["XL"]["xl_goldfish"] is True
    assert not any(cfg["gear_presets"][g].get("xl_goldfish") for g in ("XS", "S", "M", "L"))


def test_job_plan_review_sends_a_later_review_to_one_reviewer(env, monkeypatch):
    env.trust()
    env.script(plan_reviewer=[{"reply": APPROVED}])
    start_inline(env, plan=PLAN_ONE, gear="XL")
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    (first,) = _plan_gates(env)
    con.execute("INSERT INTO gates(id, run_id, subject, plan_version, kind, input_key, status, round, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", ("Gnext", run["id"], "plan", first["plan_version"], "plan_review",
                                              "k2", "queued", 2, "2999-01-01T00:00:00"))
    con.commit()
    calls = []
    monkeypatch.setattr(xl_review, "audit_plan", lambda *a, **k: calls.append("xl"))
    monkeypatch.setattr(gates, "run_reviewer", lambda *a, **k: calls.append("one") or
                        {"status": contract.UNAVAILABLE, "verdict": None, "parsed": None})
    plans.job_plan_review(con, run, {"payload": {"gate_id": "Gnext"}})
    assert calls == ["one"], "the first verdict exists, so this is no longer the initial review"
