#!/usr/bin/env python3
"""Derives adapter trust, capability floors and local reward from recorded evidence.

Implements Task T2B (`docs/plans/v3-final-merge.md`) against the contract pinned in
`docs/v3-runtime-contracts.md` section 7 (Deliverable F). The governing invariant for
adapter trust (amendments v5/v6): a query may LOWER adapter trust and may NEVER RAISE
it. Promotion is never computed from a count, a label, or a validation row -- it is an
explicit recorded act (`adapter_trust_acts`). `tests/test_trust_conformance.py` is the
normative artifact for that invariant; this module is what it imports and runs against.

Deviation from the pinned module boundary, reported in the T2B landing: section 7.1.4.3
pins `record_trust_act`/`get_current_trust_state` in a new `scripts/office_trust.py`
module. The T2B dispatch brief's allowed-path list names only this file, its sibling
`office_routing.py`, and their two test files, so those two functions live here instead,
under a `db_path` parameter rather than the pinned `state_dir` parameter. Behavior matches
the contract; only the module/parameter name differs.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

EFFORT_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}

_EVIDENCE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_FAILURE_LABELS = ("recurrence_failure", "material_post_merge_defect")

# §7.3.1's mapping table: (stored label, narrative:* tag or None for "no tag / default").
# revert_failure and pending are deliberately absent -- both are unscored (§7.3.1, §7.4.1).
_NARRATIVE_BASE = {
    ("verified_no_observed_failure", None): 0.8,
    ("verified_no_observed_failure", "narrative:success"): 0.8,
    ("verified_no_observed_failure", "narrative:partial_success"): 0.4,
    ("environment_failure", None): 0.0,
    ("abandoned", None): -0.4,
    ("abandoned", "narrative:abandoned"): -0.4,
    ("abandoned", "narrative:defect_detected"): -0.5,
    ("abandoned", "narrative:failed_verification"): -0.6,
    ("abandoned", "narrative:operator_rejected"): -0.7,
    ("recurrence_failure", None): -0.9,
    ("material_post_merge_defect", None): -1.0,
}


def _valid_evidence_hash(value: Any) -> bool:
    """Evidence-validity checklist properties 1-4 (§7.1.2 / Finding F21): present,
    correctly prefixed, correct length, hex-only lowercase body."""
    return isinstance(value, str) and bool(_EVIDENCE_RE.match(value))


def _narrative_tag(contributing_attributions: Any) -> str | None:
    """Extracts the single `narrative:*` tag, or None. `schemas/outcome-label.schema.json`
    already enforces at most one such tag (Finding F22); this reads, not re-validates."""
    if not contributing_attributions:
        return None
    if isinstance(contributing_attributions, str):
        try:
            contributing_attributions = json.loads(contributing_attributions)
        except (TypeError, ValueError):
            return None
    for item in contributing_attributions or []:
        if isinstance(item, str) and item.startswith("narrative:"):
            return item
    return None


# ---------------------------------------------------------------------------
# §7.1 -- derived adapter trust (amendment v6: no normative SQL is pinned; this
# function is the sole implementation tests/test_trust_conformance.py imports).
# ---------------------------------------------------------------------------

def evaluate_trust_state(con: sqlite3.Connection, target_triple: str) -> tuple[int, str]:
    """Evaluates the §7.1.2 trust-state properties for `target_triple` against the
    dispatches/findings/outcome_labels/adapter_trust_acts tables reachable on `con`.
    Returns (critical_failures, qualified_trust_state): `critical_failures` is the count
    of distinct dispatches on this triple carrying latched, unresolved adapter-attributed
    failure evidence, and `qualified_trust_state` is one of `quarantined`, `proven`, or
    `valid-unverified` (never `invalid`, which is upstream schema/mandatory-semantics
    validation and unrelated to runs.db history).

    The invariant: a query may lower adapter trust and it may never raise it. Downward
    transitions (evidence -> quarantined) are fully automatic; upward transitions
    (anything -> valid-unverified/proven) happen only via the most recent
    `adapter_trust_acts` row for this exact triple, and only when no qualifying failure
    currently stands -- a standing quarantine is never cleared by any act.
    """
    cur = con.cursor()
    dispatch_rows = cur.execute(
        "SELECT id, attribution FROM dispatches WHERE triple = ?", (target_triple,)
    ).fetchall()
    attribution_by_dispatch = {row[0]: row[1] for row in dispatch_rows}

    qualifying: set[str] = set()
    if attribution_by_dispatch:
        placeholders = ",".join("?" * len(attribution_by_dispatch))
        label_rows = cur.execute(
            f"SELECT dispatch_id, label, primary_attribution, evidence_hash "
            f"FROM outcome_labels WHERE dispatch_id IN ({placeholders})",
            tuple(attribution_by_dispatch),
        ).fetchall()

        abandoned_dispatch_ids: set[str] = set()
        for dispatch_id, label, primary_attribution, evidence_hash in label_rows:
            is_adapter_attributed = (
                attribution_by_dispatch.get(dispatch_id) == "adapter"
                or primary_attribution == "adapter"
            )
            if not is_adapter_attributed:
                continue
            # Every qualifying label ever recorded counts -- not just the latest one
            # (§7.1.2's latch property). Evaluating only `rn = 1` is the exact
            # laundering defect amendment v6 exists to close.
            if label in _FAILURE_LABELS and _valid_evidence_hash(evidence_hash):
                qualifying.add(dispatch_id)
            elif label == "abandoned":
                abandoned_dispatch_ids.add(dispatch_id)

        if abandoned_dispatch_ids:
            # Finding F12 correction: an adapter-attributed `abandoned` dispatch that
            # never landed but carries an accepted-material critical/high finding also
            # quarantines, qualified by the finding's own evidence (not the label's).
            placeholders2 = ",".join("?" * len(abandoned_dispatch_ids))
            finding_rows = cur.execute(
                f"SELECT dispatch_id, status, severity, evidence_hash FROM findings "
                f"WHERE dispatch_id IN ({placeholders2})",
                tuple(abandoned_dispatch_ids),
            ).fetchall()
            for dispatch_id, status, severity, evidence_hash in finding_rows:
                if (
                    status == "accepted-material"
                    and severity in ("critical", "high")
                    and _valid_evidence_hash(evidence_hash)
                ):
                    qualifying.add(dispatch_id)

    critical_failures = len(qualifying)
    if critical_failures > 0:
        # Quarantine is not query-clearable, and no act clears it either (§7.1.2): a
        # standing failure always wins over any trust act, however recent.
        return (critical_failures, "quarantined")

    act_row = cur.execute(
        "SELECT target_state FROM adapter_trust_acts WHERE triple = ? "
        "ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
        (target_triple,),
    ).fetchone()
    if act_row:
        return (0, act_row[0])
    # The floor, absent any explicit trust act and any qualifying failure, is
    # valid-unverified -- never proven, no matter how many successes accumulated.
    return (0, "valid-unverified")


def ensure_trust_schema(con: sqlite3.Connection) -> None:
    """Creates `adapter_trust_acts` if it is not already present on `con`. `runs.db`'s
    `init_db` (owned by T2, `scripts/office_runtime.py`) does not create this table, so
    the write/read paths below ensure it defensively rather than editing that file."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS adapter_trust_acts("
        "id TEXT PRIMARY KEY, triple TEXT NOT NULL, target_state TEXT NOT NULL, "
        "actor_id TEXT NOT NULL, reason TEXT NOT NULL, evidence_reference TEXT, "
        "recorded_at TEXT NOT NULL)"
    )
    con.commit()


def record_trust_act(
    db_path,
    triple: str,
    target_state: str,
    actor_id: str,
    reason: str,
    evidence_reference: str | None = None,
) -> dict:
    """Appends an explicit, attributed trust act for `triple` to the append-only
    `adapter_trust_acts` log (§7.1.4). This is the only function permitted to raise a
    triple's trust state; `recorded_at` is stamped here at call time and is never
    accepted as a caller-supplied argument, so an act cannot be backdated to win the
    `ORDER BY recorded_at DESC` tiebreak in `evaluate_trust_state` against a later,
    more authoritative act.
    """
    if target_state not in ("valid-unverified", "proven"):
        raise ValueError(f"illegal trust act target_state: {target_state!r}")
    if not isinstance(triple, str) or not triple.strip():
        raise ValueError(
            "trust act triple must be a non-empty string identifying the exact "
            "routable triple"
        )
    if not actor_id or not str(actor_id).strip():
        raise ValueError("trust act actor_id must identify a real, non-empty holder")
    if str(actor_id).strip().lower() in ("system", "automated"):
        raise ValueError(
            "trust act actor_id must identify a real holder, not the reserved "
            "'system' or 'automated' value"
        )
    if len(re.sub(r"\s+", "", reason or "")) < 10:
        raise ValueError(
            "trust act reason must contain a substantive justification "
            "(>=10 non-whitespace characters)"
        )

    con = sqlite3.connect(str(db_path))
    try:
        ensure_trust_schema(con)
        act_id = str(uuid.uuid4())
        recorded_at = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO adapter_trust_acts VALUES (?,?,?,?,?,?,?)",
            (act_id, triple, target_state, actor_id, reason, evidence_reference, recorded_at),
        )
        con.commit()
    finally:
        con.close()
    return {
        "trust_act_id": act_id,
        "triple": triple,
        "target_state": target_state,
        "actor_id": actor_id,
        "reason": reason,
        "evidence_reference": evidence_reference,
        "recorded_at": recorded_at,
    }


def get_current_trust_state(db_path, triple: str) -> str:
    """Evaluates the §7.1.2 trust-state expression for `triple`: derives `quarantined`
    from runs.db evidence if it qualifies, otherwise returns the most recently recorded
    trust act's target_state for `triple`, otherwise `valid-unverified`. Never derives
    `proven` from evidence alone."""
    con = sqlite3.connect(str(db_path))
    try:
        ensure_trust_schema(con)
        return evaluate_trust_state(con, triple)[1]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# §7.2 -- per-role capability floor.
# ---------------------------------------------------------------------------

def evaluate_capability_floor(candidate: dict, floor: dict | None) -> tuple[bool, str | None]:
    """Evaluates `candidate` against a `roles.<role>.floor` block (§7.2). Returns
    (passed, reason). A required catalog field the floor needs that is missing, null, or
    undefined on `candidate` FAILS CLOSED and names the missing field -- it is never
    treated as passing (§7.2.2 rule 4)."""
    if not floor:
        return True, None

    min_effort = floor.get("min_effort")
    if min_effort and min_effort != "none":
        if min_effort not in EFFORT_RANK:
            raise ValueError(f"unrecognised floor.min_effort {min_effort!r}")
        effort = candidate.get("effort")
        if effort not in EFFORT_RANK:
            return False, "missing required catalog field 'effort'"
        if EFFORT_RANK[effort] < EFFORT_RANK[min_effort]:
            return False, f"effort below role floor (got {effort}, required {min_effort})"

    allowed_sources = floor.get("allowed_sources")
    if allowed_sources:
        source = candidate.get("invocation_source")
        if not source or not any(str(source).startswith(p) for p in allowed_sources):
            return False, "invocation_source not permitted by role floor"

    min_benchmark = floor.get("min_benchmark_index")
    if min_benchmark:
        index_name = min_benchmark.get("index_name")
        min_score = min_benchmark.get("min_score")
        benchmarks = candidate.get("benchmark_indexes")
        score = benchmarks.get(index_name) if isinstance(benchmarks, dict) else None
        if score is None:
            return False, f"missing required catalog field 'benchmark_indexes.{index_name}'"
        if float(score) < float(min_score):
            return False, (
                f"benchmark index {index_name} below role floor "
                f"(got {score}, required {min_score})"
            )

    return True, None


# ---------------------------------------------------------------------------
# §7.4 -- derived local reward.
# ---------------------------------------------------------------------------

def _base_reward(label: str, narrative_tag: str | None) -> float | None:
    key = (label, narrative_tag if (label, narrative_tag) in _NARRATIVE_BASE else None)
    return _NARRATIVE_BASE.get(key)


def compute_local_reward(con: sqlite3.Connection, target_triple: str) -> float | None:
    """Derives §7.4's R for `target_triple` from labeled outcome rows on `con`. Absent
    any scored label, returns None (unknown) -- never 0.0, which would assert an
    empirical neutral result that was never observed (§7.4.2). When multiple labeled
    dispatches exist for the triple, each contributes one clamped R sample and the
    result is their mean; this aggregation rule is not pinned by the contract text
    (which speaks of a single R per triple) and is called out in the T2B landing.
    """
    cur = con.cursor()
    dispatch_rows = cur.execute(
        "SELECT id, money_estimate, money_actual FROM dispatches WHERE triple = ?",
        (target_triple,),
    ).fetchall()
    if not dispatch_rows:
        return None
    budget_by_dispatch = {row[0]: (row[1], row[2]) for row in dispatch_rows}
    placeholders = ",".join("?" * len(budget_by_dispatch))
    label_rows = cur.execute(
        f"SELECT dispatch_id, label, contributing_attributions FROM outcome_labels "
        f"WHERE dispatch_id IN ({placeholders})",
        tuple(budget_by_dispatch),
    ).fetchall()

    samples: list[float] = []
    for dispatch_id, label, contributing_attributions in label_rows:
        base = _base_reward(label, _narrative_tag(contributing_attributions))
        if base is None:
            # pending / revert_failure / unrecognised label: contributes no R_base
            # sample (§7.3.1, §7.4.1) -- same treatment as "no label at all".
            continue

        finding_counts = {"critical": 0, "high": 0, "medium": 0}
        for (severity,) in cur.execute(
            "SELECT severity FROM findings WHERE dispatch_id = ? AND status = 'accepted-material'",
            (dispatch_id,),
        ):
            if severity in finding_counts:
                finding_counts[severity] += 1
        findings_penalty = -(
            0.20 * finding_counts["critical"]
            + 0.10 * finding_counts["high"]
            + 0.02 * finding_counts["medium"]
        )

        budget_money, actual_money = budget_by_dispatch.get(dispatch_id, (None, None))
        efficiency = 0.0
        if budget_money not in (None, 0) and actual_money is not None:
            efficiency = 0.10 * (1.0 - (float(actual_money) / float(budget_money)))
            efficiency = max(-0.10, min(0.10, efficiency))

        samples.append(max(-1.0, min(1.0, base + findings_penalty + efficiency)))

    if not samples:
        return None
    return sum(samples) / len(samples)


def reward_sort_key(reward: float | None) -> tuple[int, float]:
    """§7.4.3's four-tier tie-break precedence: positive (best first), then unmeasured
    (None), then measured neutral, then negative (least negative first). Unmeasured must
    never rank equal to a measured zero."""
    if reward is None:
        return (1, 0.0)
    r = float(reward)
    if r > 0:
        return (0, -r)
    if r == 0:
        return (2, 0.0)
    return (3, -r)
