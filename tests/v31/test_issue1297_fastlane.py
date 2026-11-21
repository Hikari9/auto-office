"""#1297: fastlane, an orchestrator-led way of working on any XS-XL gear, with independent review always required."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import PLAN_ONE

from office import config as cfg
from office import economics
from office import fastlane
from office import risk as risk_mod
from office import task_descriptors
from office import task_sizing

CONFIG = cfg.load_yaml(cfg.default_config_path())
LOW = {"classification": "low"}
EST = "inline=14 dedicated=22 minutes: reuses the loaded session and skips an eight minute brief and spin-up"
QUALITY = "claude/claude-opus-5-5@high: the orchestrator runs Opus at high effort, above the M executor floor"
AUTO = {"version": fastlane.VERSION, "policy": "auto", "by": "default"}
ON = {**AUTO, "policy": "on", "by": "flag"}
OFF = {**AUTO, "policy": "off", "by": "flag"}


def _desc(size="M", **extra):
    return {"task_size": size, "task_size_rationale": "one module, two functions, no interfaces", **extra}


def _run_row(env) -> dict:
    con = env.con()
    try:
        from office import state
        return state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()["id"])
    finally:
        con.close()


# ------------------------------------------------------------------ the pinned policy

def test_pin_defaults_to_auto_and_the_flag_sets_on_or_off():
    assert fastlane.pin("XS", None) == AUTO
    assert fastlane.pin("L", True)["policy"] == "on" and fastlane.pin("L", True)["by"] == "flag"
    assert fastlane.pin("M", False)["policy"] == "off"


def test_a_gear_with_its_own_execution_contract_pins_no_fastlane_and_refuses_the_flag():
    assert fastlane.pin("greenfield", None) is None
    with pytest.raises(ValueError, match="XS-XL"):
        fastlane.pin("greenfield", True)
    with pytest.raises(ValueError, match="no-fastlane"):
        fastlane.pin("greenfield", False)


def test_a_malformed_or_missing_gate_reads_as_no_fastlane():
    assert fastlane.gate({"gates": {}}) is None
    assert fastlane.gate({"gates": {"fastlane": {"policy": "on"}}}) is None
    assert fastlane.gate({"gates": {"fastlane": {**AUTO, "policy": "sometimes"}}}) is None
    assert fastlane.gate({"gates": {"fastlane": AUTO}}) == AUTO


# ------------------------------------------------------------------ who leads a task

@pytest.mark.parametrize("gear,size,risk", [("XS", "XS", LOW), ("S", "M", LOW), ("M", "M", {"classification": "unknown"}),
                                            ("XL", "XL", {"classification": "elevated"})])
def test_a_run_without_a_gate_decides_exactly_as_before(gear, size, risk):
    desc = _desc(size, inline_advantage=EST)
    assert task_sizing.execution_mode(gear, desc, risk, task_count=1) == \
        task_sizing.execution_mode(gear, desc, risk, task_count=1, fastlane=None, parallel=True)
    assert task_sizing.execution_mode(gear, desc, risk, task_count=1)["decision_version"] == "execution-mode-1"


@pytest.mark.parametrize("gear", ["XS", "S", "M", "L", "XL"])
@pytest.mark.parametrize("risk", [LOW, {"classification": "unknown"}, {"classification": "elevated"}])
def test_the_invokers_flag_always_wins(gear, risk):
    mode = task_sizing.execution_mode(gear, _desc("XL"), risk, fastlane=ON, parallel=True)
    assert (mode["mode"], mode["owner"], mode["fastlane"]) == ("inline", "orchestrator", "on")
    assert "own subagents" in mode["why"] and "review still required" in mode["why"]
    assert mode["decision_version"] == task_sizing.FASTLANE_EXECUTION_VERSION
    assert task_sizing.execution_mode(gear, _desc("XS"), LOW, fastlane=OFF)["mode"] == "delegated"


def test_fastlane_never_applies_to_a_gear_outside_xs_xl():
    assert task_sizing.execution_mode("greenfield", _desc("XL"), LOW, fastlane=ON)["mode"] == "delegated"


def test_xs_defaults_to_fastlane_on_low_risk_bounded_work():
    for size in ("XS", "S"):
        assert task_sizing.execution_mode("XS", _desc(size), LOW, fastlane=AUTO)["mode"] == "inline"


@pytest.mark.parametrize("risk", [{"classification": "unknown"}, {"classification": "elevated"}, {}])
def test_auto_is_never_suggested_on_unknown_or_high_risk(risk):
    mode = task_sizing.execution_mode("XS", _desc("XS"), risk, fastlane=AUTO)
    assert mode["mode"] == "delegated" and "not sure" in mode["why"]


def test_auto_beyond_the_xs_default_needs_quality_time_and_cost_all_in_favour():
    good = _desc("L", inline_advantage=EST, inline_quality=QUALITY)
    mode = task_sizing.execution_mode("L", good, LOW, fastlane=AUTO, parallel=False)
    assert mode["mode"] == "inline" and mode["fastlane_checks"] == {"quality": True, "time": True, "cost": True}
    assert mode["estimated_savings"]["savings"] == 8
    no_quality = task_sizing.execution_mode("L", _desc("L", inline_advantage=EST), LOW, fastlane=AUTO)
    assert no_quality["mode"] == "delegated" and "quality" in no_quality["why"]
    parallel = task_sizing.execution_mode("L", good, LOW, fastlane=AUTO, parallel=True)
    assert parallel["mode"] == "delegated" and "time" in parallel["why"] and parallel["fastlane_checks"]["time"] is False
    no_cost = task_sizing.execution_mode("L", _desc("L", inline_quality=QUALITY), LOW, fastlane=AUTO)
    assert no_cost["mode"] == "delegated" and "cost" in no_cost["why"]
    dearer = "inline=30 dedicated=22 minutes: retries and a second review round dominate the total"
    worse = task_sizing.execution_mode("M", _desc("M", inline_advantage=dearer, inline_quality=QUALITY), LOW, fastlane=AUTO)
    assert worse["mode"] == "delegated" and "does not beat" in worse["why"]


def test_an_xs_run_with_an_m_task_takes_the_three_checks_instead_of_always_delegating():
    good = _desc("M", inline_advantage=EST, inline_quality=QUALITY)
    assert task_sizing.execution_mode("XS", good, LOW, fastlane=AUTO)["mode"] == "inline"
    assert task_sizing.execution_mode("XS", _desc("M"), LOW, fastlane=AUTO)["mode"] == "delegated"


def test_an_unknown_task_size_is_not_sure():
    desc = {"task_size": "M", "task_size_rationale": "insufficient scope, dependency and uncertainty evidence: conservative M",
            "inline_advantage": EST, "inline_quality": QUALITY}
    mode = task_sizing.execution_mode("M", desc, LOW, fastlane=AUTO)
    assert mode["mode"] == "delegated" and "size is unknown" in mode["why"]
    assert task_sizing.execution_mode("M", {}, LOW, fastlane=AUTO)["mode"] == "delegated"


@pytest.mark.parametrize("policy", [AUTO, ON])
def test_an_overflow_latches_delegation_for_every_later_decision_even_under_the_flag(policy):
    gate = {**policy, "overflow": {"task": "T1", "reason": "context"}}
    mode = task_sizing.execution_mode("XS", _desc("XS"), LOW, fastlane=gate)
    assert mode["mode"] == "delegated" and "overflowed" in mode["why"]
    kept = {"mode": "inline", "owner": "orchestrator", "why": "x", "decision_version": "execution-mode-2"}
    handed = fastlane.delegate_after_overflow(kept)
    assert (handed["mode"], handed["owner"]) == ("delegated", "executor") and "overflowed" in handed["why"]


def test_parallel_means_two_tasks_with_no_dependency_path():
    assert fastlane.parallel([{"id": "T1"}]) is False
    chain = [{"id": "T1"}, {"id": "T2", "depends": ["T1"]}, {"id": "T3", "depends": ["T2"]}]
    assert fastlane.parallel(chain) is False
    assert fastlane.parallel([{"id": "T1"}, {"id": "T2"}]) is True
    assert fastlane.parallel([{"id": "T1"}, {"id": "T2", "depends": ["T1"]}, {"id": "T3", "depends": ["T1"]}]) is True
    assert fastlane.parallel([{"id": "T1", "depends": ["T0"]}]) is False  # an unknown dependency is ignored


def test_inline_quality_is_a_parsed_descriptor_field_that_needs_real_content():
    assert "inline_quality" in task_descriptors.PLAN_KEYS
    assert task_descriptors.parse_field("inline_quality", QUALITY) == QUALITY
    with pytest.raises(ValueError, match="executor floor"):
        task_descriptors.parse_field("inline_quality", "fine")


# ------------------------------------------------------------------ independent review is mandatory

def test_xs_fastlane_always_funds_independent_review_and_xs_without_it_keeps_the_risk_rule():
    low = cfg.resolve_risk(CONFIG, "repo", None, False)
    assert cfg.resolve_gates("XS", low, CONFIG)["code_review"] is False
    assert cfg.resolve_gates("XS", low, CONFIG, fastlane=AUTO)["code_review"] is True
    for gear in ("S", "M", "L", "XL"):
        assert cfg.resolve_gates(gear, low, CONFIG, fastlane=ON)["code_review"] is True


def test_the_lightweight_path_cannot_drop_a_fastlane_review():
    low = cfg.resolve_risk(CONFIG, "repo", None, False)
    gates = {**cfg.resolve_gates("XS", low, CONFIG, fastlane=AUTO), "fastlane": AUTO}
    out = risk_mod.apply_lightweight(gates, "XS", CONFIG, {"rationale": "typo fix in one doc line"}, 1)
    assert out["code_review"] is True and "independent code review" not in out["lightweight"]["dropped"]
    plain = risk_mod.apply_lightweight(cfg.resolve_gates("XS", {**low, "high": True}, CONFIG), "XS", CONFIG,
                                       {"rationale": "typo fix in one doc line"}, 1)
    assert plain["code_review"] is False  # a run without fastlane keeps the old lightweight behavior


def test_start_pins_the_policy_and_xs_reviews_by_default(env):
    code, out = env.office("start", "fixture goal", "--gear", "XS", "--blast-radius", "repo", "--planner", "inline")
    assert code == 0, out
    assert "fastlane: auto" in out and "independent review always required" in out
    run = _run_row(env)
    assert run["gates"]["fastlane"] == AUTO and run["gates"]["code_review"] is True


def test_start_records_the_flag_and_refuses_it_on_greenfield(env):
    code, out = env.office("start", "fixture goal", "--gear", "L", "--fastlane", "--planner", "inline")
    assert code == 0, out
    assert _run_row(env)["gates"]["fastlane"]["policy"] == "on"
    code, out = env.office("start", "another goal", "--gear", "greenfield", "--blast-radius", "repo", "--fastlane")
    assert code != 0 and "fastlane" in out


def test_the_plan_keeps_the_fastlane_review_when_it_reclassifies_risk(env):
    code, out = env.office("start", "fixture goal", "--gear", "XS", "--planner", "inline")
    assert code == 0, out
    env.write_plan(PLAN_ONE)  # blast_radius: repo classifies the unknown run low
    code, out = env.office("submit")
    assert code == 0, out
    run = _run_row(env)
    assert run["risk"]["classification"] == "low"
    assert run["gates"]["code_review"] is True and run["gates"]["fastlane"]["policy"] == "auto"


# ------------------------------------------------------------------ overflow and compaction

def _overflow_stubs(monkeypatch, herdr: bool):
    monkeypatch.setattr(fastlane, "live_led_task", lambda con, run: ({"id": "T1"}, None))
    monkeypatch.setattr(fastlane, "_record", lambda con, run, tid, reason, detail: {"task": tid, "reason": reason, **detail})
    monkeypatch.setattr(fastlane, "_herdr", lambda con, run: herdr)


@pytest.mark.parametrize("herdr", [True, False])
def test_context_overflow_appends_the_handoff_and_compacts_only_where_it_can(monkeypatch, herdr):
    _overflow_stubs(monkeypatch, herdr)
    run = {"id": "r1", "gates": {"fastlane": AUTO}}
    line, overflow = fastlane.on_context_high(None, run, "ctx-high: orchestrator 260k > 250k", 260_000, 250_000)
    assert line.startswith("ctx-high: orchestrator 260k > 250k; fastlane T1 is too big")
    assert "office rerun T1 now delegates" in line and overflow["trigger"] == "context"
    if herdr:
        assert "/compactable" in line and "suggestion" not in line
    else:
        assert "suggestion: compact this session" in line and "carries on" in line


def test_no_led_task_or_no_gate_leaves_the_ctx_line_alone(monkeypatch):
    monkeypatch.setattr(fastlane, "live_led_task", lambda con, run: None)
    assert fastlane.on_context_high(None, {"id": "r", "gates": {"fastlane": AUTO}}, "ctx-high: x", 1, 1) == ("ctx-high: x", None)
    assert fastlane.on_context_high(None, {"id": "r", "gates": {}}, "ctx-high: x", 1, 1) == ("ctx-high: x", None)
    done = {"fastlane": {**AUTO, "overflow": {"task": "T1"}}}
    assert fastlane.on_context_high(None, {"id": "r", "gates": done}, "ctx-high: x", 1, 1) == ("ctx-high: x", None)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_the_file_trigger_counts_files_outside_scope_and_past_twice_the_plan(tmp_path):
    repo = tmp_path / "wt"
    (repo / "src").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "src" / "a.py").write_text("a = 1\n")
    (repo / "src" / "b.py").write_text("b = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    task = {"id": "T1", "scope": ["src/a.py", "src/new*.py"]}
    dispatch = {"worktree": str(repo), "base_commit": base}
    assert fastlane.files_reason(task, dispatch) is None
    (repo / "src" / "a.py").write_text("a = 2\n")
    (repo / "src" / "new1.py").write_text("x = 1\n")
    assert fastlane.files_reason(task, dispatch) is None  # 2 touched, 1 planned: not past 2x
    (repo / "src" / "new2.py").write_text("x = 2\n")
    reason, detail = fastlane.files_reason(task, dispatch)
    assert "more than 2x" in reason and detail == {"trigger": "files", "touched": 3, "planned": 1}
    (repo / "src" / "b.py").write_text("b = 2\n")
    reason, detail = fastlane.files_reason(task, dispatch)
    assert "outside T1 scope" in reason and detail["outside"] == ["src/b.py"]
    assert fastlane.files_reason(task, {"worktree": str(tmp_path / "gone"), "base_commit": base}) is None


def test_the_summary_line_names_the_policy_and_an_overflow():
    assert fastlane.summary({"gates": {}}) is None
    assert fastlane.summary({"gates": {"fastlane": OFF}}).startswith("fastlane: off")
    over = fastlane.summary({"gates": {"fastlane": {**ON, "overflow": {"task": "T2", "reason": "context"}}}})
    assert "overflowed on T2" in over and "independent review always required" in over


def test_the_orchestrator_context_default_is_250k():
    from office import ctx_alert
    assert ctx_alert.DEFAULT_THRESHOLD == 250_000
    assert CONFIG["orchestrator_context"]["threshold_tokens"] == 250_000


# ------------------------------------------------------------------ honest cost

def test_the_ledger_reads_claude_subagent_transcripts(tmp_path, monkeypatch):
    from office import transcripts
    proj = tmp_path / "projects" / "-repo"
    (proj / "sess-1" / "subagents").mkdir(parents=True)
    (proj / "sess-1.jsonl").write_text("")
    (proj / "sess-1" / "subagents" / "agent-a.jsonl").write_text("")
    monkeypatch.setattr(transcripts, "claude_home", lambda: tmp_path)
    found = economics._transcripts("claude", "sess-1")
    assert [p.name for p in found] == ["sess-1.jsonl", "agent-a.jsonl"]
    assert found[1].parent.name == "subagents" and economics.SUBAGENT_ROLE == "root-subagent"


def test_a_callable_task_id_is_resolved_per_row(tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "u.db")
    con.row_factory = sqlite3.Row
    ev = {"harness": "claude", "session_id": "s", "turn_id": "t", "event_id": "e1", "model": "m",
          "occurred_at": "2026-11-21T00:00:05Z", "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
          "cache_write_tokens": 0, "cost_usd": None, "cost_kind": "unknown", "source": "claude-transcript", "source_path": "x"}
    economics.record(con, [ev], run_id="r", role="root-subagent", led=lambda at: ("T1", "D1") if at else None)
    row = dict(con.execute("SELECT task_id, dispatch_id, role FROM usage_events").fetchone())
    assert (row["task_id"], row["dispatch_id"], row["role"]) == ("T1", "D1", "root-subagent")
    # Task reports count the row: its task comes through the led dispatch Office recorded.
    assert economics._task_of(row) == "T1"


def test_a_route_below_the_task_size_floor_fails_the_quality_check():
    # Luna at high scores below the L floor; Opus at high clears it.
    low = _desc("L", inline_advantage=EST, inline_quality="codex/gpt-6-luna@high: luna is plenty for this work")
    out = task_sizing.execution_mode("L", low, LOW, fastlane=AUTO)
    assert out["mode"] == "delegated" and out["fastlane_checks"]["quality"] is False
    assert "requires" in out["why"] and "gpt-6-luna" in out["why"]
    assert task_sizing.orchestrator_floor(QUALITY, "L") is None


def test_an_unknown_or_missing_orchestrator_route_fails_the_quality_check():
    assert "not in the catalog" in task_sizing.orchestrator_floor("nope/model-x@high: a made up route here", "M")
    assert "must start with" in task_sizing.orchestrator_floor("the orchestrator is strong enough for this", "M")
    out = task_sizing.execution_mode("M", _desc("M", inline_advantage=EST,
                                                inline_quality="the orchestrator is strong enough for this"), LOW, fastlane=AUTO)
    assert out["mode"] == "delegated" and out["fastlane_checks"]["quality"] is False
