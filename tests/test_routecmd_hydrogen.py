"""Hydrogen policy output must describe the canary, not ordinary allows."""
import pytest

from office import routecmd


ROW = {"model_id": "gemini-4-hydrogen", "invocation_harness": "agy", "effort": "medium",
       "dispatchable": False}
CANDIDATE = {"model_id": "gemini-4-hydrogen", "harness": "agy", "effort": "medium"}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("state", ["neutral", "allow"])
def test_unsized_hydrogen_requires_proof(enabled, state):
    dispatches, reason = routecmd._dispatch_view(
        ROW, {"state": state, "source": "user"}, candidate=CANDIDATE,
        config={"routing": {"hydrogen_trials": {"enabled": enabled}}})
    assert not dispatches
    for text in ("depends on task size", "executor-only", "S/medium", "3 per rolling 20", "one per run",
                 "manual --as", "fresh exact conformance probe pass", "no-identity-readback",
                 "cannot dispatch until a fresh exact probe passes"):
        assert text in reason
    assert "your allow overrides this" not in reason
    assert "`office route allow` makes it dispatch" not in reason


def test_reviewer_and_sized_canary_reasons():
    state = {"state": "neutral", "source": "default"}
    _, reviewer = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, role="code_reviewer")
    assert "Hydrogen is executor-only" in reviewer
    config = {"routing": {"hydrogen_trials": {"enabled": True}}}
    dispatches, sized = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, size="S", config=config,
                                                probe={"result": "pass"})
    assert not dispatches
    assert "automatic Hydrogen requires a bounded, reversible, low-risk S/M task" in sized
    assert "cannot dispatch until" not in sized
    _, disabled = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, size="S", config={})
    assert "automatic Hydrogen canary is disabled" in disabled
    _, wrong_effort = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, size="M", config=config)
    assert "exact approved agy model/size/effort" in wrong_effort


def test_non_hydrogen_allow_behavior_unchanged():
    row = {"model_id": "other", "invocation_harness": "agy", "dispatchable": False}
    assert routecmd._dispatch_view(row, {"state": "allow", "source": "user"}) == (
        True, "your allow overrides this (its catalog row is not dispatchable)")
    assert routecmd._dispatch_view(row, {"state": "neutral", "source": "default"}) == (
        False, "its catalog row is not dispatchable; `office route allow` makes it dispatch")


def test_policy_uses_exact_probe_and_enabled_config(monkeypatch, tmp_path):
    from office import adapters, candidates, config, db, route_probe

    monkeypatch.setattr(config, "resolve", lambda _: ({"routing": {"hydrogen_trials": {"enabled": True}}}, None))
    monkeypatch.setattr(candidates, "catalog_rows", lambda: [ROW])
    monkeypatch.setattr(adapters, "load_sources", lambda: {"agy": ({}, None)})
    monkeypatch.setattr(adapters, "route_version", lambda *args: "1.0")
    monkeypatch.setattr(adapters, "installed", lambda _: True)
    monkeypatch.setattr(adapters, "profile", lambda *_: True)
    connect = db.connect
    monkeypatch.setattr(db, "connect", lambda: connect(tmp_path / "runs.db"))
    seen = []
    monkeypatch.setattr(route_probe, "status", lambda con, candidate: seen.append(candidate.copy()) or None)
    result = routecmd.policy(role="executor", size="S")
    row = result.data["effective"][0]
    assert seen[0]["effort"] == "medium" and seen[0]["harness"] == "agy"
    assert not row["dispatches"]
    assert "bounded, reversible, low-risk" in row["dispatch_reason"]
    assert "cannot dispatch until a fresh exact probe passes" in row["dispatch_reason"]
