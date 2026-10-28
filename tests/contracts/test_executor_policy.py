"""#871 PR A: changing policy data must not require changing an asserted model list."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
import yaml

from office import candidates, executor_slates as slates


def _load_mutated(tmp_path: Path, mutation):
    source = slates.POLICY
    data = {key: copy.deepcopy(value) for key, value in source.items() if not key.startswith("_")}
    mutation(data)
    target = tmp_path / "catalog" / "executor-policy.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False))
    return slates.load_policy(tmp_path)


def test_shipped_policy_is_consumed_by_runtime():
    source = Path(slates.POLICY["_provenance"]["source"])
    assert source.is_file()
    assert slates.POLICY["_provenance"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert slates.POLICY["floors"] is slates.FLOORS
    for size, rows in slates.POLICY["executor_preferred"].items():
        assert slates.ideal_seed(size) == [
            {key: value for key, value in row.items() if value is not None}
            for row in rows
        ]


def test_reversing_only_size_preference_data_reverses_runtime_seed(tmp_path, monkeypatch):
    size = next(size for size, rows in slates.POLICY["executor_preferred"].items() if len(rows) > 1)
    original = slates.ideal_seed(size)
    updated = _load_mutated(tmp_path, lambda cfg: cfg["executor_preferred"][size].reverse())
    monkeypatch.setattr(slates, "IDEAL", {
        size: tuple((r["model_id"], r["effort"], r.get("harness")) for r in entries)
        for size, entries in updated["executor_preferred"].items()
    })
    # Use the production preferred-seed choice (not a duplicate oracle).
    runtime_seed = candidates._preferred_seed({}, {"gear": size}, size)
    assert runtime_seed == list(reversed(original))
    assert runtime_seed != original


def test_floor_data_changes_runtime_gate_without_changing_test(tmp_path, monkeypatch):
    size = next(iter(slates.FLOORS))
    previous = slates.FLOORS[size]
    raised = previous + 5
    updated = _load_mutated(tmp_path, lambda cfg: cfg["floors"].__setitem__(size, raised))
    monkeypatch.setattr(slates, "FLOORS", updated["floors"])
    candidate = {"benchmark_indexes": {slates.INDEX: raised - 1}}
    assert slates.benchmark_gate(candidate, size)
    candidate["benchmark_indexes"][slates.INDEX] = raised
    assert slates.benchmark_gate(candidate, size) is None


@pytest.mark.parametrize("mutation", [
    lambda d: d["floors"].pop(next(iter(d["floors"]))),
    lambda d: d["executor_preferred"][next(iter(d["executor_preferred"]))].append(
        copy.deepcopy(d["executor_preferred"][next(iter(d["executor_preferred"]))][0])),
    lambda d: d["executor_preferred"][next(iter(d["executor_preferred"]))][0].__setitem__("effort", "invalid"),
    lambda d: d["planner_preferred"].__setitem__("XS", []),
    lambda d: d["floor_exceptions"][0].__setitem__("minimum_score", 9999),
    lambda d: d.__setitem__("schema_version", 999),
])
def test_policy_errors_fail_closed(tmp_path, mutation):
    with pytest.raises(slates.ExecutorPolicyError):
        _load_mutated(tmp_path, mutation)


def test_exception_requires_its_actual_configured_scope_and_evidence():
    for rule in slates.POLICY["floor_exceptions"]:
        c = {"model_id": rule["model_id"], "effort": rule["effort"],
             "harness": rule.get("harness"), "benchmark_indexes": {}}
        if rule["score_evidence"] == "numeric-below-floor":
            c["benchmark_indexes"][slates.INDEX] = rule["minimum_score"]
        receipt = slates.floor_exception(c, rule["size"], rule["role"])
        assert receipt and receipt["exception"] == rule["name"]
        assert slates.floor_exception(c, rule["size"], "code_reviewer") is None
        assert slates.floor_exception({**c, "effort": "invalid"}, rule["size"], rule["role"]) is None


def test_model_restrictions_follow_configured_scopes():
    for restriction in slates.POLICY["model_restrictions"]:
        valid = {"model_id": restriction["model_id"], "harness": "missing"}
        if "allowed_sizes" in restriction:
            assert slates.model_restriction(valid, restriction["allowed_sizes"][0]) is None
            wrong = next(s for s in slates.SIZES if s not in restriction["allowed_sizes"])
            assert slates.model_restriction(valid, wrong)
        else:
            assert slates.model_restriction({**valid, "harness": restriction["allowed_harnesses"][0]}, "XL") is None
            assert slates.model_restriction(valid, "XL")
