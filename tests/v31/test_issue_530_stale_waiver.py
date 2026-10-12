"""#530: do not record waivers on superseded lane compositions."""
import pytest
from office import convergence
from office.state import Refused


def test_waiver_refuses_when_accepted_revision_key_moved(monkeypatch):
    scope = {"id": "L-T1", "tasks": ["T1"]}
    state = {"key": "older-key", "commit": "a" * 40, "status": "escalated"}
    monkeypatch.setattr(convergence, "_key", lambda con, run, scope: "new-accepted-key")
    with pytest.raises(Refused) as exc:
        convergence._require_current_waiver_target(None, {"id": "R"}, scope, state)
    assert exc.value.category == "stale-composition"
    assert "office decide L-T1 review" in exc.value.next_step


def test_waiver_guard_requires_composed_commit_and_acceptance_key(monkeypatch):
    scope = {"id": "L-T1", "tasks": ["T1"]}
    monkeypatch.setattr(convergence, "_key", lambda con, run, scope: "accepted-key")
    for st in [{"key": "accepted-key"}, {"key": "older", "commit": "x"}, {"key": None, "commit": "x"}]:
        with pytest.raises(Refused, match="waiver for the older commit"):
            convergence._require_current_waiver_target(None, {}, scope, st)
    assert convergence._require_current_waiver_target(None, {}, scope,
                                                       {"key": "accepted-key", "commit": "b" * 40}) is None
