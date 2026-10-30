"""#910: visual routing must select newly proved reviewers and report actual rejections."""
from __future__ import annotations

from office import adapters, candidates, conformance, gates, routing


def _candidate(i: int) -> dict:
    return {"harness": "agy", "harness_version": "1", "adapter_id": "agy",
            "model_id": f"model-{i}", "invocation_model_id": f"model-{i}",
            "effort": "low", "quota": {"status": "unknown"}}


def _probe_scenario(monkeypatch, count: int, passing: int):
    slate = [_candidate(i) for i in range(count)]
    passed = set()
    probed = []
    selections = []

    def route(_con, _config, _run, _role, **kwargs):
        assert kwargs.get("observe") is True
        selections.append(len(passed))
        winner = next((c for c in slate if routing.candidate_id(c) in passed), None)
        if winner:
            return {"status": "selected", "candidate": winner}
        return {"status": "no_qualifying_candidate",
                "request": {"candidates": slate, "policy": {"quota_reserve_percent": 5}},
                "rejected": []}

    def probe(_con, _run, cand, _adapter):
        probed.append(cand["model_id"])
        if cand is slate[passing]:
            passed.add(routing.candidate_id(cand))
            return {"result": "pass"}
        return {"result": "fail"}

    monkeypatch.setattr(candidates, "route_role", route)
    monkeypatch.setattr(candidates, "role_policy", lambda *_: {"preferred_seed": []})
    monkeypatch.setattr(adapters, "load_all", lambda: {"agy": {}})
    monkeypatch.setattr(conformance, "proof_status", lambda *_: None)
    monkeypatch.setattr(conformance, "probe_vision", probe)
    return slate, passed, probed, selections


def test_a_newly_passing_reviewer_is_selectable_in_the_same_job(monkeypatch):
    slate, passed, probed, selections = _probe_scenario(monkeypatch, 1, 0)
    conformance.ensure_vision_route(object(), {"id": "run"}, {})
    assert probed == ["model-0"]
    assert routing.candidate_id(slate[0]) in passed
    assert selections == [0, 1]  # the success is immediately re-routed, not deferred


def test_vision_probe_search_is_not_limited_to_four_candidates(monkeypatch):
    slate, passed, probed, selections = _probe_scenario(monkeypatch, 6, 5)
    conformance.ensure_vision_route(object(), {"id": "run"}, {})
    assert probed == [f"model-{i}" for i in range(6)]
    assert routing.candidate_id(slate[5]) in passed
    assert selections[-1] == 1


def test_actual_routing_rejection_reasons_are_displayed_not_a_generic_failure():
    primary, fallback = _candidate(0), _candidate(1)
    primary_id, fallback_id = routing.candidate_id(primary), routing.candidate_id(fallback)
    decision = {
        "request": {"preferred_seed": [{"model_id": primary["model_id"], "harness": "agy", "effort": "low"}],
                    "candidates": [primary, fallback]},
        "rejected": [{"candidate": primary_id, "stage": 6, "reason": "protected quota reserve"},
                     {"candidate": fallback_id, "stage": 3, "reason": "missing capabilities ['vision']"}],
        "skipped": [{"candidate": "opencode/deepseek-v4.1-flash-free@max",
                     "reason": "no vision launch profile"}],
    }
    reason = gates.reviewer_route_rejections(decision)
    assert "protected quota reserve" in reason
    assert "missing capabilities" in reason
    assert "no vision launch profile" in reason
    assert reason.index(primary_id) < reason.index(fallback_id)

def test_allowed_primary_still_gets_real_image_proof_even_when_route_is_selectable(monkeypatch):
    primary = _candidate(0)
    probed = []
    monkeypatch.setattr(candidates, "route_role", lambda *_args, **_kwargs: {
        "status": "selected", "candidate": primary,
        "request": {"candidates": [primary], "policy": {"quota_reserve_percent": 5}}})
    monkeypatch.setattr(candidates, "role_policy", lambda *_: {"preferred_seed": []})
    monkeypatch.setattr(adapters, "load_all", lambda: {"agy": {}})
    monkeypatch.setattr(conformance, "proof_status", lambda *_: None)
    monkeypatch.setattr(conformance, "probe_vision",
                        lambda *_args: probed.append("image checked") or {"result": "pass"})
    conformance.ensure_vision_route(object(), {"id": "run"}, {})
    assert probed == ["image checked"]
