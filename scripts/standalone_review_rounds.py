"""Pure standalone /auto-review bookkeeping; no Office run or reviewer launch.

The caller must verify read-only isolation and the independent reviewer's
signed/evidenced reply before passing these fields. This helper is a counter,
not a source of truth for review quality or authorization.
"""
from __future__ import annotations

from copy import deepcopy
import re

MAX_SUBSTANTIVE_ROUNDS = 3
VERDICTS = {"APPROVED", "RECHECK", "INTAKE_GAP"}


class ReviewContractError(ValueError):
    """The invoker must repair or escalate an invalid review record."""


def _session(value) -> str:
    """Session ids compare case- and whitespace-insensitively; blank means missing."""
    return value.strip().casefold() if isinstance(value, str) else ""


def record(ledger: dict, *, verdict: str, revision: str, reviewer_session: str,
           invoker_session: str, producer_session: str,
           fix_owner_session: str | None = None, evidence: str = "",
           read_only: bool = False, decision_resolved: bool = False) -> dict:
    """Return a new ledger. UNAVAILABLE doesn't consume a review round.

    A caller can be orchestrator or executor. A chosen fix owner need not be the
    caller; the reviewer must be independent of caller, producer and fixer.
    """
    if not isinstance(ledger, dict) or not isinstance(ledger.get("reviews", []), list) \
            or not all(isinstance(entry, dict) for entry in ledger.get("reviews", [])):
        raise ReviewContractError("invalid previous review ledger")
    if not isinstance(revision, str) or not isinstance(evidence, str):
        raise ReviewContractError("revision and evidence must be strings")
    reviewer_session, invoker_session, producer_session = (
        _session(reviewer_session), _session(invoker_session), _session(producer_session))
    fix_owner_session = _session(fix_owner_session) if fix_owner_session is not None else None
    reviews = ledger.get("reviews", [])
    if len(reviews) >= MAX_SUBSTANTIVE_ROUNDS:
        raise ReviewContractError("three substantive rounds exhausted; unresolved review remains blocked")
    if reviews and reviews[-1].get("verdict") == "APPROVED":
        raise ReviewContractError("already approved; new revision requires its own review contract")
    if reviews and reviews[-1].get("verdict") == "INTAKE_GAP" and not decision_resolved:
        raise ReviewContractError("INTAKE_GAP requires a real owner decision before recheck")
    if verdict not in VERDICTS | {"UNAVAILABLE"}:
        raise ReviewContractError("invalid reviewer verdict")
    if not reviewer_session or not invoker_session or not producer_session:
        raise ReviewContractError("missing independently identifiable sessions")
    if reviewer_session in {invoker_session, producer_session, fix_owner_session}:
        raise ReviewContractError("reviewer must be a distinct session from invoker/producer/fixer")
    copy = deepcopy(ledger)
    if verdict == "UNAVAILABLE":
        copy.setdefault("unavailable", []).append({"revision": revision,
                                                    "reviewer_session": reviewer_session,
                                                    "reason": evidence.strip() or "unknown"})
        return copy
    if not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise ReviewContractError("substantive review must bind to an immutable commit SHA")
    if not read_only or not evidence.strip():
        raise ReviewContractError("independent, read-only evidence is required for substantive review")
    copy.setdefault("reviews", []).append({
        "round": len(reviews) + 1,
        "revision": revision.lower(),
        "reviewer_session": reviewer_session,
        "invoker_session": invoker_session,
        "producer_session": producer_session,
        "fix_owner_session": fix_owner_session,
        "verdict": verdict,
        "evidence": evidence.strip(),
    })
    copy["remaining"] = MAX_SUBSTANTIVE_ROUNDS - len(copy["reviews"])
    copy["status"] = ("approved" if verdict == "APPROVED" else
                      "owner-decision-needed" if verdict == "INTAKE_GAP" else
                      "blocked-exhausted" if not copy["remaining"] else "needs-recheck")
    return copy
