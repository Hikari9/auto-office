"""#573: deterministic visual evidence blockage must offer a legal, non-looping next step."""

from office import convergence


def test_evidence_blocked_hint_names_contract_amendment_instead_of_blind_retry(monkeypatch):
    monkeypatch.setattr(convergence, "summary", lambda con, run: [
        {"id": "L-T1", "status": "evidence_blocked", "detail": "fonts cannot load",
         "tasks": ["T1"], "commit": "reviewed-commit"}])
    monkeypatch.setattr(convergence, "_latest", lambda *a: {
        "visual": {"evidence_status": "CAPTURE_BLOCKED", "summary": "fonts cannot load"}})
    monkeypatch.setattr(convergence, "spec_defect_gate", lambda *a: None)
    monkeypatch.setattr(convergence, "fallback_gate", lambda *a: None)
    hint = convergence.next_action(None, {"id": "run"})
    assert "office amend <task> --contract" in hint
    assert "start:" in hint and "then office resume re-captures" in hint
    assert "fallback is not available" in hint
    assert "fonts cannot load" in hint and "playwright install" not in hint
