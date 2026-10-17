"""#420 (unknown risk never silently disables independent review) and #424 (planner-declared lightweight path)."""
from __future__ import annotations

import json

import pytest
from hypothesis import given, strategies as st
from conftest import GOOD_ADD, PLAN_ONE, start_inline

from office import config as cfg
from office import planfile, risk

CONFIG = cfg.load_yaml(cfg.default_config_path())


def _plan(blast: str | None = "repo", extra: str = "") -> str:
    head = PLAN_ONE.split("blast_radius: repo\n")
    body = head[0] + (f"blast_radius: {blast}\n" if blast else "") + extra + head[1]
    return body


def _run_row(env) -> dict:
    con = env.con()
    try:
        from office import state
        return state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()["id"])
    finally:
        con.close()


# ---------------------------------------------------------------- #420: classification and floor

def test_explicit_low_is_distinct_from_unknown():
    low = cfg.resolve_risk(CONFIG, "repo", None, False)
    unknown = cfg.resolve_risk(CONFIG, None, None, False)
    assert low["classification"] == "low" and unknown["classification"] == "unknown"
    assert low["classified_by"] == "intake" and unknown["classified_by"] is None
    assert cfg.resolve_risk(CONFIG, "production", None, False)["classification"] == "elevated"
    assert cfg.resolve_risk(CONFIG, "repo", "L", False)["classification"] == "elevated"
    assert cfg.resolve_risk(CONFIG, "local", None, True)["classification"] == "elevated"


@given(gear=st.sampled_from(sorted(CONFIG["gear_presets"])),
       blast=st.sampled_from([None, "local", "repo", "production", "production-data"]),
       size=st.sampled_from([None, "S", "M", "L", "XL"]), irreversible=st.booleans())
def test_only_an_explicit_low_risk_can_fund_no_independent_review_under_any_preset(gear, blast, size, irreversible):
    """#420: an unknown or elevated risk keeps independent code review whatever the gear preset says."""
    risk = cfg.resolve_risk(CONFIG, blast, size, irreversible)
    elevated = irreversible or blast in ("production", "production-data") or size in ("L", "XL")
    assert risk["classification"] == ("elevated" if elevated else "unknown" if blast is None else "low")
    gates = cfg.resolve_gates(gear, risk, CONFIG)
    if risk["classification"] != "low":
        assert gates["code_review"] is True and gates["risk_classification"] == risk["classification"], gates
    if risk["classification"] == "unknown":
        assert "independent code review required" in gates["review_basis"]


def test_explicit_low_on_direct_funds_no_review_and_says_why():
    gates = cfg.resolve_gates("direct", cfg.resolve_risk(CONFIG, "repo", None, False), CONFIG)
    assert gates["code_review"] is False and not gates["review_floor"]
    assert "not required" in gates["review_basis"] and "low" in gates["review_basis"]


def test_floor_beats_a_gear_that_funds_no_review():
    config = json.loads(json.dumps(CONFIG))
    config["gear_presets"]["light"]["independent_code_review"] = False
    unknown = cfg.resolve_gates("light", cfg.resolve_risk(config, None, None, False), config)
    assert unknown["code_review"] is True and unknown["review_floor"] is True
    low = cfg.resolve_gates("light", cfg.resolve_risk(config, "repo", None, False), config)
    assert low["code_review"] is False


def test_record_without_classification_keeps_the_old_gates():
    # A pre-#420 run (or a bare bool) resolves as before: no floor.
    assert cfg.resolve_gates("direct", False, CONFIG)["code_review"] is False
    assert cfg.resolve_gates("direct", {"high": False, "blast_radius": None}, CONFIG)["code_review"] is False


def test_start_without_risk_requires_review_and_names_the_obligation(env):
    code, out = env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline")
    assert code == 0 and "risk is unclassified" in out, out
    run = _run_row(env)
    assert run["risk"]["classification"] == "unknown" and run["gates"]["code_review"] is True
    code, status = env.office("status")
    assert "risk unknown" in status, status


def test_plan_classification_lowers_unknown_to_low_and_is_recorded(env):
    start_inline(env, plan=_plan("repo"), gear="direct")
    run = _run_row(env)
    assert run["risk"]["classification"] == "low" and run["risk"]["classified_by"] == "planner"
    assert run["gates"]["code_review"] is False
    assert "explicit low risk" in run["gates"]["review_basis"]


def test_plan_without_classification_leaves_unknown_and_warns(env):
    env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline", check=0)
    env.write_plan(_plan(None))
    code, out = env.office("submit")
    assert code == 0 and "risk is unclassified" in out, out
    run = _run_row(env)
    assert run["risk"]["classification"] == "unknown" and run["gates"]["code_review"] is True


def test_plan_can_raise_risk_but_not_lower_intake_classification(env):
    env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline", "--blast-radius", "production",
               check=0)
    env.write_plan(_plan("repo"))
    env.office("submit", check=0)
    run = _run_row(env)
    assert run["risk"]["classification"] == "elevated" and run["gates"]["code_review"] is True


def test_unknown_run_executes_with_independent_review(env):
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("start", "fixture goal", "--gear", "direct", "--planner", "inline", check=0)
    env.write_plan(_plan(None))
    env.office("submit", check=0)
    env.office("approve", "plan", "--quote", "approved", check=0)
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    code, data = env.ojson("inspect", "run")
    assert data["data"]["risk"]["independent_code_review"] is True
    assert [c for c in env.calls() if c.get("role") == "convergence_reviewer"], "never reached an independent reviewer"


# ---------------------------------------------------------------- #424: lightweight path

def test_lightweight_requires_a_rationale():
    plan = planfile.parse(_plan("repo", "lightweight:\n"))
    assert any("lightweight" in e and "rationale" in e for e in plan.errors), plan.errors
    ok = planfile.parse(_plan("repo", "lightweight: fix a typo in one doc line\n"))
    assert not ok.errors and ok.requirements["lightweight"] == {"rationale": "fix a typo in one doc line"}


def test_lightweight_on_explicit_low_drops_tunable_review_and_discloses(env):
    start_inline(env, plan=_plan("repo", "lightweight: typo fix in a doc line\n"), gear="light")
    run = _run_row(env)
    lw = run["gates"]["lightweight"]
    assert lw["declared"] and lw["rationale"] == "typo fix in a doc line" and "independent code review" in lw["dropped"]
    assert run["gates"]["code_review"] is False
    assert "lightweight path" in run["gates"]["review_basis"]
    code, out = env.office("inspect", "run")
    assert "lightweight path: typo fix in a doc line" in out, out
    code, out = env.office("status")
    assert "lightweight path" in out, out


def test_lightweight_keeps_a_review_the_gear_firmly_funds(env):
    start_inline(env, plan=_plan("repo", "lightweight: typo fix\n"), gear="direct+review")
    run = _run_row(env)
    assert run["gates"]["code_review"] is True and run["gates"]["lightweight"]["dropped"] == []


def test_lightweight_refused_for_unknown_risk(env):
    env.office("start", "fixture goal", "--gear", "light", "--planner", "inline", check=0)
    env.write_plan(_plan(None, "lightweight: typo fix\n"))
    code, out = env.office("submit")
    assert code != 0 and "lightweight path refused" in out and "unknown" in out, out
    assert _run_row(env)["gates"]["code_review"] is True and not _run_row(env)["plan_version"]


def test_lightweight_refused_for_misclassified_high_risk(env):
    env.office("start", "fixture goal", "--gear", "light", "--planner", "inline", "--blast-radius", "production",
               check=0)
    env.write_plan(_plan("repo", "lightweight: just a small config tweak\n"))
    code, out = env.office("submit")
    assert code != 0 and "lightweight path refused" in out and "elevated" in out, out
    env.write_plan(_plan("production-data", "lightweight: trivial\n"))
    code, out = env.office("submit")
    assert code != 0 and "elevated" in out, out


def test_lightweight_refused_when_plan_declares_irreversible(env):
    env.office("start", "fixture goal", "--gear", "light", "--planner", "inline", check=0)
    env.write_plan(_plan("repo", "irreversible: yes\nlightweight: trivial\n"))
    code, out = env.office("submit")
    assert code != 0 and "elevated" in out, out


def test_lightweight_refused_on_full_gear(env):
    env.office("start", "fixture goal", "--gear", "full", "--planner", "inline", "--blast-radius", "repo", check=0)
    env.write_plan(_plan("repo", "lightweight: trivial\n"))
    code, out = env.office("submit")
    assert code != 0 and "gear full" in out, out


def test_lightweight_work_completes_without_independent_review_and_self_review_stays(env):
    from office import briefs
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}])
    start_inline(env, plan=_plan("repo", "lightweight: trivial one-line add\n"), gear="direct")
    env.office("approve", "plan", "--quote", "approved", check=0)
    code, out = env.office("dispatch", "T1")
    assert code == 0, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted"}, data
    run = _run_row(env)
    assert not [c for c in env.calls() if c.get("role") in ("code_reviewer", "convergence_reviewer")]
    # #309 composes: the planner-classified record still drives the executor's self-review tier.
    assert briefs.self_review_tier(run["gear"], run["risk_json"]) == "inline"
    assert run["gates"]["lightweight"]["kept"]


def test_withdrawing_the_declaration_restores_the_gear_review(env):
    start_inline(env, plan=_plan("repo", "lightweight: typo\n"), gear="light")
    assert _run_row(env)["gates"]["code_review"] is False
    env.write_plan(_plan("repo"))
    env.office("submit", check=0)
    run = _run_row(env)
    assert run["gates"]["code_review"] is True and run["gates"]["code_review_depth"] == "shallow"
    assert "lightweight" not in run["gates"]


# ---------------------------------------------------------------- resume / recovery / receipts / legacy

def test_resume_restores_the_stored_classification(env):
    start_inline(env, plan=_plan("repo", "lightweight: typo\n"), gear="light")
    before = _run_row(env)
    code, out = env.office("resume")
    assert code == 0, out
    after = _run_row(env)
    assert after["risk"] == before["risk"] and after["gates"] == before["gates"]
    assert after["gates"]["lightweight"]["rationale"] == "typo"


def test_receipts_expose_classification_and_reason(env):
    start_inline(env, plan=_plan("repo", "lightweight: typo\n"), gear="light")
    run = _run_row(env)
    s = risk.summary(run)
    assert s["classification"] == "low" and s["lightweight"]["rationale"] == "typo" and s["why"]
    from office import convergence, lifecycle
    con = env.con()
    try:
        assert convergence.receipt(con, run)["risk"]["classification"] == "low"
        assert lifecycle._archive_receipt(con, run, {}, None)["body"]["risk"]["lightweight"]["declared"]
    finally:
        con.close()


def test_run_without_classification_is_untouched_and_cannot_go_lightweight(env):
    start_inline(env, plan=_plan("repo"), gear="direct")
    con = env.con()
    try:
        con.execute("UPDATE runs SET risk_json=?", (json.dumps({"blast_radius": None, "irreversible": False,
                                                                "high": False, "size_class": None}),))
        con.commit()
    finally:
        con.close()
    legacy = _run_row(env)
    assert risk.classification(legacy["risk"]) is None and risk.summary(legacy) is None
    assert "predates" in risk.lightweight_problem(legacy, legacy["risk"], {"rationale": "x"})


# ---------------------------------------------------------------- review follow-ups (F1-F5)

def _resubmit(env, plan):
    env.write_plan(plan)
    return env.office("submit")


def test_a_raise_after_authorization_applies_and_gets_high_risk_review(env):
    start_inline(env, plan=_plan("repo"), gear="direct")
    env.office("approve", "plan", "--quote", "approved", check=0)
    assert _run_row(env)["gates"]["code_review"] is False
    code, out = _resubmit(env, _plan("repo", "irreversible: yes\n"))
    assert code == 0, out
    run = _run_row(env)
    assert run["risk"]["classification"] == "elevated" and run["gates"]["code_review"] is True
    assert run["gear"] == "full" and run["gates"]["visual"] is True  # same as intake-declared irreversible work


def test_planner_raise_gets_the_same_gates_as_an_intake_raise(env):
    start_inline(env, plan=_plan("production"), gear="direct")
    run = _run_row(env)
    intake = cfg.resolve_gates("express", cfg.resolve_risk(CONFIG, "production", None, False), CONFIG)
    assert run["gear"] == "express"
    for k in ("code_review", "code_review_max_rounds", "plan_review", "visual"):
        assert run["gates"][k] == intake[k], k


def test_a_lowering_after_authorization_is_ignored(env):
    start_inline(env, plan=_plan("production"), gear="light")
    env.office("approve", "plan", "--quote", "approved", check=0)
    # production is frozen at authorization; a resubmitted plan naming a lower radius is refused or ignored.
    _resubmit(env, _plan("production", "size_class: S\n"))
    assert _run_row(env)["risk"]["classification"] == "elevated"


def test_authorization_landing_mid_submit_blocks_a_new_lightweight_declaration(env, monkeypatch):
    start_inline(env, plan=_plan("repo"), gear="light")
    from office import visual
    real = visual.preflight

    def approve_then_preflight(tasks):
        env.office("approve", "plan", "--quote", "approved", check=0)  # lands between parse and the write
        return real(tasks)

    monkeypatch.setattr(visual, "preflight", approve_then_preflight)
    code, out = _resubmit(env, _plan("repo", "lightweight: typo\n"))
    assert code != 0 and "authorized" in out, out
    assert "lightweight" not in _run_row(env)["gates"] and _run_row(env)["gates"]["code_review"]


def test_lightweight_on_a_later_plan_does_not_skip_a_queued_plan_review(env):
    start_inline(env, plan=_plan("repo"), gear="express")
    run = _run_row(env)
    assert run["plan_review"]["required"] is True
    code, out = _resubmit(env, _plan("repo", "lightweight: typo\n"))
    assert code == 0, out
    assert _run_row(env)["plan_review"]["required"] is True


def test_reclassification_history_is_kept(env):
    env.office("start", "fixture goal", "--gear", "light", "--planner", "inline", check=0)
    _resubmit(env, _plan("repo"))
    _resubmit(env, _plan("repo", "irreversible: yes\n"))
    hist = _run_row(env)["risk"]["history"]
    assert [h["classification"] for h in hist] == ["low", "elevated"], hist
    assert risk.summary(_run_row(env))["history"] == hist


def test_planner_raise_marks_integration_risk():
    # #420 x #422: a plan raising risk to irreversible or production is integration risk; size is not.
    from office import config as config_mod, risk as risk_mod
    base = config_mod.resolve_risk({}, None, None, False)
    assert base["integration"] is False
    for req in ({"irreversible": True}, {"blast_radius": "production"}):
        out = risk_mod.planner_reclassify(base, req)
        assert out and out["integration"] is True, out
    out = risk_mod.planner_reclassify(base, {"size_class": "XL"})
    assert out and out["high"] and not out["integration"], out
