"""One reviewer result shape for both review contracts, and a REVOKED outcome that is not a reviewer failure."""
from __future__ import annotations

import pytest

from office import contract, convergence, gates, review_parse

KINDS = ("completed", "evidence_blocked", "invalid", "unavailable", "revoked")


def _parsed(verdict):
    return review_parse.Parsed(verdict=verdict, evidence_status=None if verdict else "CAPTURE_BLOCKED")


def _build(convergence_run: bool, kind: str) -> dict:
    parsed = _parsed("APPROVED" if convergence_run else "PASS") if kind == "completed" else \
        _parsed(None) if kind == "evidence_blocked" else None
    return gates.reviewer_outcome(convergence_run, kind, summary=f"{kind} summary", parsed=parsed, route="r/m/h",
                                  dispatch_id="D1", producer_route="p/m/h", env_failures=1)


@pytest.mark.parametrize("kind,status", [("completed", "COMPLETED"), ("evidence_blocked", "EVIDENCE_BLOCKED"),
                                         ("invalid", "INVALID_RESULT"), ("unavailable", "UNAVAILABLE"),
                                         ("revoked", "REVOKED")])
def test_convergence_status_for_each_outcome_kind(kind, status):
    out = _build(True, kind)
    assert out["status"] == status
    assert out["verdict"] == ("APPROVED" if kind == "completed" else None)
    assert (out["parsed"] is not None) == (kind in ("completed", "evidence_blocked"))
    assert out["summary"] == f"{kind} summary"
    assert out.get("exhausted") is (True if kind == "unavailable" else None)
    assert out.get("revoked") is (True if kind == "revoked" else None)


@pytest.mark.parametrize("kind,verdict", [("completed", "PASS"), ("invalid", "ATTENTION"),
                                          ("unavailable", "UNAVAILABLE"), ("revoked", "REVOKED")])
def test_legacy_verdict_for_each_outcome_kind(kind, verdict):
    out = _build(False, kind)
    assert out["verdict"] == verdict
    assert "status" not in out and "exhausted" not in out and "env_failures" not in out


def test_legacy_evidence_blocked_has_no_verdict_and_no_status():
    out = _build(False, "evidence_blocked")
    assert out["verdict"] is None and "status" not in out


def test_revoked_is_distinct_from_unavailable_and_both_settle_as_no_answer():
    assert contract.REVOKED != contract.UNAVAILABLE
    assert contract.REVOKED in contract.STATUSES and "REVOKED" in contract.LEGACY_VERDICTS
    assert set(contract.NO_ANSWER) == {contract.UNAVAILABLE, contract.REVOKED}
    assert contract.REVOKED in convergence.FALLBACK_STATUSES


@pytest.mark.parametrize("gate", [{"verdict": "REVOKED"}, {"verdict": None, "review_status": "REVOKED"},
                                  {"verdict": "UNAVAILABLE"}])
def test_revoked_review_counts_as_a_review_with_no_verdict(gate):
    assert gates.review_unavailable(gate)


def test_a_completed_review_is_not_unavailable():
    assert not gates.review_unavailable({"verdict": "PASS", "review_status": None})
    assert not gates.review_unavailable({"verdict": None, "review_status": "COMPLETED"})
