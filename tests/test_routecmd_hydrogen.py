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


@pytest.mark.parametrize("role,state,installed,profile,expected", [
    ("executor", {"state": "deny", "source": "user"}, True, True, "denied by user"),
    ("code_reviewer", {"state": "deny", "source": "seed"}, True, True, "denied by seed"),
    ("executor", {"state": "neutral", "source": "default"}, False, True, "agy is not installed"),
    ("executor", {"state": "neutral", "source": "default"}, True, False, "agy has no launch profile"),
])
def test_hydrogen_overrides(role, state, installed, profile, expected):
    dispatches, reason = routecmd._dispatch_view(ROW, state, installed, profile, candidate=CANDIDATE, role=role)
    assert not dispatches
    assert reason.startswith(expected)


def test_sized_user_allow_does_not_override_canary():
    dispatches, reason = routecmd._dispatch_view(
        ROW, {"state": "allow", "source": "user"}, candidate=CANDIDATE, size="S",
        config={"routing": {"hydrogen_trials": {"enabled": True}}})
    assert not dispatches
    assert "bounded, reversible, low-risk" in reason
    assert "allow overrides" not in reason and "office route allow" not in reason


def test_policy_uses_exact_probe_and_enabled_config(monkeypatch, tmp_path):
    from office import adapters, candidates, config, db, paths, route_probe
    from office.util import now_iso

    adapter = {"source_notes": "resolved adapter"}
    monkeypatch.setattr(config, "resolve", lambda _: ({"routing": {"hydrogen_trials": {"enabled": True}}}, None))
    monkeypatch.setattr(candidates, "catalog_rows", lambda: [ROW])
    monkeypatch.setattr(adapters, "load_sources", lambda: {"agy": (adapter, None)})
    monkeypatch.setattr(adapters, "load_all", lambda: {"agy": {"source_notes": "different adapter"}})
    monkeypatch.setattr(adapters, "route_version", lambda *args: "1.0")
    monkeypatch.setattr(adapters, "installed", lambda _: True)
    monkeypatch.setattr(adapters, "profile", lambda *_: True)
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(paths, "runs_db", lambda: db_path)
    row = routecmd.policy(role="executor", size="S").data["effective"][0]
    assert not db_path.exists()
    assert "cannot dispatch until a fresh exact probe passes" in row["dispatch_reason"]

    candidate = {**CANDIDATE, "harness_version": "1.0"}
    con = db.connect(db_path)
    con.execute("INSERT INTO route_probes (key, result, probed_at) VALUES (?, 'pass', ?)",
                (route_probe.key(candidate, adapter), now_iso()))
    con.close()
    row = routecmd.policy(role="executor", size="S").data["effective"][0]
    assert not row["dispatches"]
    assert "bounded, reversible, low-risk" in row["dispatch_reason"]
    assert "cannot dispatch until" not in row["dispatch_reason"]
