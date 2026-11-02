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


def test_a_non_visual_evidence_block_names_resume_not_a_capture_contract(monkeypatch):
    """#870: a T2 four-lens subagent that never ran, on a visual: none lane, named a visual capture amend."""
    detail = "T2 four-lens self-review subagent: subagent was not launched and completed"
    monkeypatch.setattr(convergence, "summary", lambda con, run: [
        {"id": "S-rebase", "status": "evidence_blocked", "detail": detail, "tasks": ["T2"], "commit": "c"}])
    monkeypatch.setattr(convergence, "_latest", lambda *a: {
        "convergence_review": {"review_status": "EVIDENCE_BLOCKED", "summary": detail}})
    monkeypatch.setattr(convergence, "spec_defect_gate", lambda *a: None)
    monkeypatch.setattr(convergence, "fallback_gate", lambda *a: None)
    hint = convergence.next_action(None, {"id": "run"})
    assert "office resume re-runs the review" in hint and detail in hint
    assert "visual: capture contract" not in hint and "--contract" not in hint
