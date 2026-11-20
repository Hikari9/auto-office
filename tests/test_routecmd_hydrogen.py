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
    assert dispatches is False
    for text in ("depends on task size", "reviews plans (@high) and visuals (@low) only as the exact user-allowed", "S/medium", "3 per rolling 20", "one per run",
                 "manual --as", "fresh exact conformance probe pass",
                 "cannot dispatch until a fresh exact probe passes"):
        assert text in reason
    assert ("automatic canary disabled" in reason) is (not enabled)
    assert "your allow overrides this" not in reason
    assert "`office route allow` makes it dispatch" not in reason
    assert "no-identity-readback" not in reason


def test_reviewer_and_sized_canary_reasons():
    state = {"state": "neutral", "source": "default"}
    dispatches, reviewer = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, role="plan_reviewer")
    assert dispatches is False
    assert "Hydrogen reviews only as the exact agy/gemini-4-hydrogen@high route" in reviewer
    dispatches, visual = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, role="visual_reviewer")
    assert dispatches is False
    assert "Hydrogen reviews only as the exact agy/gemini-4-hydrogen@low route" in visual
    # Code review left Hydrogen (AA v5): it is not a Hydrogen review role at any effort.
    dispatches, code = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, role="code_reviewer")
    assert dispatches is False
    assert "code review left Hydrogen" in code
    config = {"routing": {"hydrogen_trials": {"enabled": True}}}
    dispatches, sized = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, size="S", config=config,
                                                probe={"result": "pass"})
    assert dispatches is None
    assert "can dispatch as an automatic canary trial" in sized
    assert "cannot dispatch until" not in sized
    dispatches, disabled = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, size="S", config={})
    assert dispatches is False
    assert "automatic Hydrogen canary is disabled" in disabled
    dispatches, wrong_effort = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, size="M", config=config)
    assert dispatches is False
    assert "Hydrogen runs automatically at @high for M tasks, not @medium" in wrong_effort


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
    assert dispatches is False
    assert reason.startswith(expected)


def test_sized_user_allow_does_not_override_canary():
    dispatches, reason = routecmd._dispatch_view(
        ROW, {"state": "allow", "source": "user"}, candidate=CANDIDATE, size="S",
        config={"routing": {"hydrogen_trials": {"enabled": True}}})
    assert dispatches is False
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
    assert row["dispatches"] is False
    assert not db_path.exists()
    assert "cannot dispatch until a fresh exact probe passes" in row["dispatch_reason"]

    candidate = {**CANDIDATE, "harness_version": "1.0"}
    con = db.connect(db_path)
    con.execute("INSERT INTO route_probes (key, result, probed_at) VALUES (?, 'pass', ?)",
                (route_probe.key(candidate, adapter), now_iso()))
    con.close()
    result = routecmd.policy(role="executor", size="S")
    row = result.data["effective"][0]
    assert row["dispatches"] is None
    assert "can dispatch as an automatic canary trial" in row["dispatch_reason"]
    assert "3 per rolling 20 and 1 per run" in row["dispatch_reason"]
    assert "cold start of 19 recorded opportunities" in row["dispatch_reason"]
    assert any("conditionally dispatches" in line and "does not dispatch" not in line for line in result.lines)
    assert "cannot dispatch until" not in row["dispatch_reason"]


def test_policy_with_invalid_probe_database(monkeypatch, tmp_path):
    from office import adapters, candidates, config, paths

    monkeypatch.setattr(config, "resolve", lambda _: ({}, None))
    monkeypatch.setattr(candidates, "catalog_rows", lambda: [ROW])
    monkeypatch.setattr(adapters, "load_sources", lambda: {"agy": ({"source_notes": "adapter"}, None)})
    monkeypatch.setattr(adapters, "route_version", lambda *args: "1.0")
    monkeypatch.setattr(adapters, "installed", lambda _: True)
    monkeypatch.setattr(adapters, "profile", lambda *_: True)
    db_path = tmp_path / "runs.db"
    db_path.write_text("not a database")
    monkeypatch.setattr(paths, "runs_db", lambda: db_path)
    row = routecmd.policy(role="executor", size="S").data["effective"][0]
    assert row["dispatches"] is False
    assert "cannot dispatch until a fresh exact probe passes" in row["dispatch_reason"]
    assert db_path.read_text() == "not a database"


@pytest.mark.parametrize("role,state,expected", [
    ("executor", {"state": "deny", "source": "user"}, "denied by user"),
    ("plan_reviewer", {"state": "neutral", "source": "default"}, "Hydrogen reviews only as the exact"),
])
def test_disabled_canary_note_only_on_unsized_executor(role, state, expected):
    dispatches, reason = routecmd._dispatch_view(
        ROW, state, candidate=CANDIDATE, role=role,
        config={"routing": {"hydrogen_trials": {"enabled": False}}})
    assert dispatches is False
    assert reason.startswith(expected)
    assert "automatic canary disabled" not in reason


def test_exact_user_allow_dispatches_only_with_a_fresh_pass():
    state = {"state": "allow", "source": "user", "rule": "agy/gemini-4-hydrogen@medium"}
    dispatches, reason = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, size="S",
                                                 probe={"result": "pass"})
    assert dispatches is True
    assert reason.startswith("user allow; exact probe passed; a normal executor route")
    assert "cannot dispatch until" not in reason and "no-identity-readback" not in reason
    dispatches, reason = routecmd._dispatch_view(ROW, state, candidate=CANDIDATE, size="S")
    assert dispatches is False
    assert "cannot dispatch until a fresh exact probe passes" in reason
    assert not reason.startswith("dispatches")
    for bad in ({**state, "rule": "agy/gemini-4-hydrogen"}, {**state, "source": "shipped"}):
        dispatches, reason = routecmd._dispatch_view(ROW, bad, candidate=CANDIDATE, size="S",
                                                     probe={"result": "pass"})
        assert dispatches is not True
