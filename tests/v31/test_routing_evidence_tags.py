"""#415 follow-up: evidence-only planner tags on a task descriptor (evidence_domain, intent,
difficulty_estimate, brief_shape). They are validated, persisted and snapshotted, and never move
routing dimensions, benchmark fit or the `domain` behavior."""
from __future__ import annotations

import json

import pytest

from conftest import PLAN_ONE, approved_run
from office import planfile, task_descriptors

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
TAGS = {"evidence_domain": "backend", "intent": "fix", "difficulty_estimate": "very-high",
        "brief_shape": "deliverables-enumerated"}
VALID = {"evidence_domain": ("frontend", "backend", "infra", "docs", "tests"),
         "intent": ("feature", "fix", "refactor", "test-pruning", "migration"),
         "difficulty_estimate": ("low", "medium", "high", "very-high", "unknown"),
         "brief_shape": ("deliverables-enumerated", "checks-only", "unknown")}
BASE = ("## Requirements\ndone:\n- updated\n## Tasks\n### T1: t\nscope: src/a.py\n"
        "depends: none\nchecks: none\nvisual: none\naccept:\n- works\n")


def _task(*lines):
    parsed = planfile.parse(BASE + "".join(f"{x}\n" for x in lines))
    return parsed.tasks[0], parsed.errors


@pytest.mark.parametrize("key,values", VALID.items())
def test_every_documented_value_is_accepted(key, values):
    for value in values:
        task, errors = _task(f"{key}: {value}")
        assert not errors and task["descriptor"] == {key: value}


@pytest.mark.parametrize("key,values", VALID.items())
def test_an_invalid_value_is_refused_with_the_allowed_list(key, values):
    task, errors = _task(f"{key}: bogus")
    assert task["descriptor"] == {}
    assert errors == [f"T1 (line 12): {key} must be one of {', '.join(values)}"]


def test_values_are_case_insensitive_and_omitted_tags_stay_absent():
    task, errors = _task("Intent:  FIX ", "domain: ui")
    assert not errors and task["descriptor"] == {"intent": "fix", "domain": "ui"}
    assert _task()[0]["descriptor"] == {}


def test_all_tags_parse_together_and_alongside_routing_fields():
    task, errors = _task(*(f"{k}: {v}" for k, v in TAGS.items()), "domain: ui", "task_size: M")
    assert not errors and task["descriptor"] == {**TAGS, "domain": "ui", "task_size": "M"}


def test_evidence_domain_is_a_separate_field_from_domain():
    task, errors = _task("evidence_domain: frontend")
    assert not errors and "domain" not in task["descriptor"]
    _, errors = _task("domain: frontend")
    assert errors and "domain must be one of ui, backend, data, infrastructure, mixed" in errors[0]


@pytest.mark.parametrize("base", [
    {}, {"domain": "ui", "quality": "taste", "modality": "visual", "work": "implementation"},
    {"domain": "backend", "work": "architecture"}, {"domain": "mixed", "modality": "browser", "quality": "mixed"},
    {"estimated_input_tokens": 150000, "task_size": "L"}])
@pytest.mark.parametrize("tags", [TAGS, {"evidence_domain": "frontend"}, {"brief_shape": "unknown"},
                                  {k: VALID[k][-1] for k in VALID}])
def test_tags_are_a_noop_on_routing_dimensions_and_fit(base, tags):
    candidate = {"model_id": "m", "invocation_model_id": "m", "effort": "medium", "task_benchmarks": [
        {"dimension": d, "benchmark_name": "b", "benchmark_version": "v1", "source_url": "https://e.org",
         "snapshot_date": "2026-10-08", "normalization": "n", "calibration_version": "c", "model_id": "m",
         "effort": "medium", "calibrated_log_odds_delta": 0.5, "confidence": 0.8}
        for d in ("ui_appearance", "repo_engineering", "architecture", "browser_interaction")]}
    tagged = {**base, **tags}
    assert task_descriptors.dimensions(tagged) == task_descriptors.dimensions(base)
    assert task_descriptors.benchmark_fit(candidate, tagged) == task_descriptors.benchmark_fit(candidate, base)
    price = {"input_per_mtok": 1, "output_per_mtok": 2, "prompt_token_threshold": 100000,
             "above_threshold_input_per_mtok": 3, "above_threshold_output_per_mtok": 4}
    assert task_descriptors.price_tier(price, tagged) == task_descriptors.price_tier(price, base)


def test_tags_alone_produce_no_routing_dimensions():
    assert task_descriptors.dimensions(TAGS) == {}


# --- persistence: submit, plan amendment, dispatch snapshot (needs a run; slow tier like its siblings)

PLAN_TAGGED = PLAN_ONE + "\n".join(f"{k}: {v}" for k, v in TAGS.items()) + "\n"


def _db(env, sql, *args):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def _desc(env, table="tasks", **where):
    sql = f"SELECT descriptor_json FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in where)
    return json.loads(_db(env, sql, *where.values())[0]["descriptor_json"])


def test_tags_persist_through_submit_amendment_and_dispatch_snapshot(env):
    approved_run(env, plan=PLAN_TAGGED, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    assert _desc(env, id="T1") == TAGS
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    snapshot = _desc(env, "dispatches", task_id="T1")
    assert snapshot == TAGS
    amended = {**TAGS, "intent": "refactor", "difficulty_estimate": "low"}
    env.write_plan(PLAN_ONE + "\n".join(f"{k}: {v}" for k, v in amended.items()) + "\n")
    code, out = env.office("amend", "plan", "--", "re-tag the task", env=EXTERNAL)
    assert code == 0, out
    assert _desc(env, id="T1") == amended
    assert _desc(env, "dispatches", task_id="T1") == TAGS  # the running episode keeps its snapshot


def test_an_invalid_tag_is_refused_at_submit(env):
    env.trust()
    env.script()
    code, out = env.office("start", "fixture goal", "--gear", "direct+review", "--planner", "inline")
    assert code == 0, out
    env.write_plan(PLAN_ONE + "intent: wishful\n")
    code, out = env.office("submit")
    assert code != 0 and "intent must be one of feature, fix, refactor, test-pruning, migration" in out, out
