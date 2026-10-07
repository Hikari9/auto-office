"""The review contract a run is pinned to (#337).

Every run keeps the review semantics it started with. A run records its
contract in its pinned gate policy (`gates_json.review_contract`) at
`office start`. A run with no recorded contract predates #337 and stays on the
v3.1 contract: PASS | CHANGES_REQUIRED | PLAN_DEFECT | BRIEF_DEFECT |
UNAVAILABLE, rolling plan review, and per-task independent review.

New runs default to the convergence contract:

  APPROVED    no blocking finding; advance now. Findings are still repaired or
              dispositioned, without another independent review.
  RECHECK     a blocking finding; the producer repairs and the same reviewer
              (when available) reviews again.
  INTAKE_GAP  the right answer depends on a missing or conflicting user-owned
              decision; the reviewer names it.

Severity (high | medium | low) is independent of the verdict, and runtime or
evidence status (COMPLETED | UNAVAILABLE | EVIDENCE_BLOCKED | INVALID_RESULT)
is independent of both. Review is per ownership/composition lane, not per
task, and every RECHECK sequence stops at three substantive rounds.

The config key `review.contract` picks the default for future runs only;
changing it (a rollback) never reinterprets a run that already started.
Stored verdicts are never rewritten: `display_verdict` labels them.
"""
from __future__ import annotations

LEGACY = "v3.1"
CONVERGENCE = "convergence-v1"
CONTRACTS = (LEGACY, CONVERGENCE)
DEFAULT = CONVERGENCE

VERDICTS = ("APPROVED", "RECHECK", "INTAKE_GAP")
LEGACY_VERDICTS = ("PASS", "CHANGES_REQUIRED", "PLAN_DEFECT", "BRIEF_DEFECT", "UNAVAILABLE")
SEVERITIES = ("high", "medium", "low")

# Runtime/evidence status of one review attempt. Only COMPLETED carries a verdict.
COMPLETED = "COMPLETED"
UNAVAILABLE = "UNAVAILABLE"
EVIDENCE_BLOCKED = "EVIDENCE_BLOCKED"
INVALID_RESULT = "INVALID_RESULT"
STATUSES = (COMPLETED, UNAVAILABLE, EVIDENCE_BLOCKED, INVALID_RESULT)

# The substantive-round ceiling for every RECHECK sequence (plan, convergence,
# visual) and for executor self-review. Not configurable: #337 locks it.
MAX_ROUNDS = 3

# A finding whose repair would cross one of these seams is never APPROVED cleanup.
HARD_SEAMS = ("requirements", "authority", "ownership", "dependency", "interface", "acceptance")

# Reviewer independence recorded on a review gate.
INDEPENDENT = "independent"
DEGRADED = "degraded-orchestrator"

# Operator choices once a RECHECK sequence spends MAX_ROUNDS.
ESCALATION_CHOICES = ("escalate", "continue", "waive", "stop")

# Finding dispositions for non-blocking (APPROVED) findings.
DISPOSITIONS = ("fixed", "dismissed", "follow-up", "fix")

# The findings a task's fix round works on: open (blocking) ones, plus
# non-blocking ones the orchestrator routed as APPROVED cleanup (`fix`).
TASK_WORK_FINDINGS = "(state='open' OR (state='nonblocking' AND disposition='fix'))"


def of(run: dict | None) -> str:
    """The contract `run` is pinned to. No record means v3.1 (started before #337)."""
    return ((run or {}).get("gates") or {}).get("review_contract") or LEGACY


def is_convergence(run: dict | None) -> bool:
    return of(run) == CONVERGENCE


def default_for(config: dict) -> str:
    """The contract a run started now pins, from `review.contract`."""
    value = ((config or {}).get("review") or {}).get("contract") or DEFAULT
    if value not in CONTRACTS:
        raise ValueError(f"review.contract must be one of {', '.join(CONTRACTS)} (got {value!r})")
    return value


def display_verdict(verdict: str | None, contract: str | None) -> str:
    """A stored verdict for reading, labelled with its contract when that is v3.1.
    The stored value is returned unchanged inside the label."""
    if not verdict:
        return "-"
    if (contract or LEGACY) == LEGACY and verdict in LEGACY_VERDICTS:
        return f"{verdict} (v3.1)"
    return verdict


def round_cap_choices(scope: str) -> list[str]:
    """The four operator choices at the round cap, as commands."""
    q = '--quote "<user\'s words>"'
    return [f"office decide {scope} escalate {q}   (another reviewer/producer, a new bounded cycle)",
            f"office decide {scope} continue {q}   (same lane, another bounded cycle)",
            f"office decide {scope} waive {q} --reason \"<why>\"   (land despite the unmet gate; needs landing authority)",
            f"office decide {scope} stop {q}   (pause the lane; nothing lands from it)"]
