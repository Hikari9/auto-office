"""#1233 (Rico, 2026-11-17: "Separate panes on XL only").

A new run funds Office's own per-task self-review audit (a separately routed reviewer session) on XL only. S, M and
L tasks rely on the executor's in-session subagent fan-out. The gears are pinned on the run at start, so a run
started before #1233 (no key in its gates) keeps auditing S, M, L and XL.
"""
import pytest

from test_convergence_contract import APPROVED, _gates, _roles, _run_row
from test_issue809_self_review_subagents import _s_run

from office import briefs, config as cfg, contract
from office import self_review_subagents as srs

GATES = {"gear_model_version": "run-gear-1", "review_contract": contract.CONVERGENCE}


def _run(gear, **gates):
    return {"gear": gear, "gates": {**GATES, **gates}, "risk": {"classification": "low"}}


def test_a_new_run_pins_xl_as_its_only_audit_gear():
    shipped = cfg.load_yaml(cfg.default_config_path())
    assert shipped["review"][srs.AUDIT_GEARS_KEY] == ["XL"]
    for config in (shipped, {}):
        assert cfg.resolve_gates("S", {"high": False}, config)[srs.AUDIT_GEARS_KEY] == ["XL"]


def test_the_audit_gears_come_from_config_and_bad_values_are_refused():
    assert srs.audit_gears(None) == ["XL"]
    assert srs.audit_gears(["XL", "S"]) == ["S", "XL"]
    assert srs.audit_gears([]) == []
    for bad in ("XL", ["XS"], ["xl"], [None]):
        with pytest.raises(ValueError):
            srs.audit_gears(bad)
    gates = cfg.resolve_gates("M", {"high": False}, {"review": {srs.AUDIT_GEARS_KEY: ["M", "XL"]}})
    assert gates[srs.AUDIT_GEARS_KEY] == ["M", "XL"]


@pytest.mark.parametrize("gear, funded", [("XS", False), ("S", False), ("M", False), ("L", False), ("XL", True)])
def test_a_new_run_funds_the_per_task_audit_on_xl_only(gear, funded):
    run = _run(gear, **{srs.AUDIT_GEARS_KEY: ["XL"]})
    assert srs.funded(run) is funded
    assert bool(srs.rubrics(run, {"scope": ["calc.py"]})) is funded


@pytest.mark.parametrize("gear", ["S", "M", "L", "XL"])
def test_a_run_started_before_1233_keeps_its_audits(gear):
    assert srs.funded(_run(gear)), "no pinned gears: the pre-#1233 funding (S, M, L, XL) holds for the run's life"


@pytest.mark.parametrize("gear", ["S", "M", "L"])
def test_an_unaudited_gear_fans_the_executor_self_review_out_in_session(gear):
    run = _run(gear, **{srs.AUDIT_GEARS_KEY: ["XL"]})
    assert briefs.in_session_fan_out(run)
    text = "\n".join(briefs.self_review_lines("abc", briefs.run_self_review_tier(run), fan_out=True))
    assert "Start one subagent per lens below that applies, all in one message, in" in text
    assert "Start exactly one subagent" not in text and "ROUND 1" in text  # same ledger, one round
    (line,) = briefs.runtime_review_lines(run)
    assert "Office launches no per-task self-review audit" in line and "launches one independent" not in line


def test_xl_and_pre_1233_runs_keep_the_single_subagent_and_the_audit_line():
    for run in (_run("XL", **{srs.AUDIT_GEARS_KEY: ["XL"]}), _run("S")):
        assert not briefs.in_session_fan_out(run)
        (line,) = briefs.runtime_review_lines(run)
        assert "launches one independent self-review subagent" in line
    text = "\n".join(briefs.focused_review_lines("abc"))
    assert "Start exactly one subagent" in text


def test_without_subagents_the_fan_out_falls_back_to_a_pass_in_this_session():
    text = "\n".join(briefs.focused_review_lines("abc", subagents=False, fan_out=True))
    assert "no subagent on this" in text and "Start one subagent per lens" not in text


def test_an_s_run_launches_no_per_task_audit_only_the_convergence_review(env):
    _s_run(env)
    assert _run_row(env)["gates"][srs.AUDIT_GEARS_KEY] == ["XL"]
    assert _gates(env, "self_review_subagent") == []
    roles = _roles(env)
    assert "code_reviewer" not in roles and roles.count("convergence_reviewer") == 1
    (conv,) = _gates(env, "convergence_review")
    assert conv["verdict"] == "APPROVED"
