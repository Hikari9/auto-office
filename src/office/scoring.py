"""Derives adapter trust, capability floors and local reward from recorded evidence.

Ported unchanged from the v3 `scripts/office_scoring.py` so the packaged runtime
and the retained v3 runtime apply one trust contract.

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

_MAJOR_RE = re.compile(r"^v?(\d+)(?:[.\-+].*)?$")
_TRIPLE_RE = re.compile(r"^(?P<harness>[^@/]+)@(?P<version>[^/]*)/(?P<rest>.+)$")


def harness_major(version: Any) -> str:
    """The harness version that identity and trust key on: the major number only.
    `agy 1.2.11` and `agy 1.2.12` are one route; so are `codex 0.155` and `0.157`.
    Non-numeric labels (`local`, `cli`, `unknown`) pass through unchanged, so they never
    silently inherit trust recorded under a real major."""
    text = "" if version is None else str(version).strip()
    m = _MAJOR_RE.match(text)
    return m.group(1) if m else (text or "unknown")


def normalize_triple(triple: str) -> str:
    """Rewrites `harness@version/model@effort` so its version is the major number."""
    m = _TRIPLE_RE.match(triple or "")
    if not m:
        return triple
    return f"{m.group('harness')}@{harness_major(m.group('version'))}/{m.group('rest')}"

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
    # Rows recorded under any release of the same harness major count as this route,
    # including rows stored with a full version before identity keyed on the major.
    target_triple = normalize_triple(target_triple)
    cur = con.cursor()
    attribution_by_dispatch = {
        row[0]: row[1]
        for row in cur.execute("SELECT id, attribution, triple FROM dispatches").fetchall()
        if normalize_triple(row[2] or "") == target_triple
    }

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

    act_row = next(
        (
            row[0]
            for row in cur.execute(
                "SELECT target_state, triple FROM adapter_trust_acts "
                "ORDER BY recorded_at DESC, rowid DESC"
            ).fetchall()
            if normalize_triple(row[1] or "") == target_triple
        ),
        None,
    )
    if act_row:
        return (0, act_row)
    # A local act always wins; absent one, the shipped baseline is itself a recorded
    # act (a reviewed, committed grant), so it may supply `proven` for a fresh install.
    baseline = trust_baseline().get(target_triple)
    if baseline:
        return (0, baseline)
    # The floor, absent any explicit trust act and any qualifying failure, is
    # valid-unverified -- never proven, no matter how many successes accumulated.
    return (0, "valid-unverified")


def trust_baseline() -> dict[str, str]:
    """`catalog/trust-baseline.yaml`: triple -> target_state for routes the maintainers
    verified across real runs. Shipped with the plugin so a new install starts from
    verified agents instead of an empty `adapter_trust_acts` log."""
    import yaml

    from office import paths

    path = paths.resources_root() / "catalog" / "trust-baseline.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    out = {}
    for entry in data.get("routes") or []:
        state = entry.get("target_state")
        if state in ("valid-unverified", "proven") and entry.get("triple"):
            out[normalize_triple(entry["triple"])] = state
    return out


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

    triple = normalize_triple(triple.strip())
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
    target_triple = normalize_triple(target_triple)
    cur = con.cursor()
    dispatch_rows = [
        row[:3]
        for row in cur.execute(
            "SELECT id, money_estimate, money_actual, triple FROM dispatches"
        ).fetchall()
        if normalize_triple(row[3] or "") == target_triple
    ]
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


# ---------------------------------------------------------------------------
# Outcome labels at closeout (F4). Labels come only from evidence already recorded
# for a dispatch; nothing here attributes a failure to the adapter.
# ---------------------------------------------------------------------------

def _dispatch_evidence_hash(con: sqlite3.Connection, dispatch_id: str) -> str | None:
    """Returns the earliest valid sha256 evidence hash recorded for `dispatch_id` across
    validations, findings and gate evidence on its revisions, or None."""
    queries = (
        "SELECT evidence_hash, created_at FROM validations WHERE dispatch_id = ?",
        "SELECT evidence_hash, created_at FROM findings WHERE dispatch_id = ?",
        "SELECT e.sha256, e.created_at FROM evidence e JOIN revisions r ON r.id = e.revision_id "
        "WHERE r.dispatch_id = ?",
    )
    candidates = []
    for sql in queries:
        for value, created_at in con.execute(sql, (dispatch_id,)).fetchall():
            if _valid_evidence_hash(value):
                candidates.append((created_at or "", value))
    return min(candidates)[1] if candidates else None


def label_run_outcomes(con: sqlite3.Connection, run_id: str, terminal: str) -> list[dict]:
    """Writes one outcome label per evidenced dispatch of `run_id`. `terminal` is
    `closed` or `abandoned`. On close only dispatches that produced an accepted revision
    are labeled (verified_no_observed_failure); on abandon every evidenced dispatch is
    labeled `abandoned`. Dispatches without evidence, or already labeled, are skipped,
    so a repeated close writes nothing new. primary_attribution stays NULL."""
    if terminal == "closed":
        rows = con.execute(
            "SELECT DISTINCT r.dispatch_id FROM tasks t JOIN revisions r ON r.id = t.accepted_revision_id "
            "WHERE t.run_id = ? AND r.dispatch_id IS NOT NULL ORDER BY r.dispatch_id", (run_id,)
        ).fetchall()
        label = "verified_no_observed_failure"
    elif terminal == "abandoned":
        rows = con.execute(
            "SELECT id FROM dispatches WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        label = "abandoned"
    else:
        raise ValueError(f"unrecognised terminal state {terminal!r}")
    written = []
    for (dispatch_id,) in rows:
        if con.execute("SELECT 1 FROM outcome_labels WHERE dispatch_id = ?", (dispatch_id,)).fetchone():
            continue
        evidence_hash = _dispatch_evidence_hash(con, dispatch_id)
        if evidence_hash is None:
            continue
        row = {
            "id": "OL" + uuid.uuid5(uuid.NAMESPACE_URL, f"office-outcome:{dispatch_id}").hex[:16],
            "dispatch_id": dispatch_id, "label": label, "primary_attribution": None,
            "contributing_attributions": None,
            "labeled_at": datetime.now(timezone.utc).isoformat(), "evidence_hash": evidence_hash,
        }
        con.execute(
            "INSERT OR IGNORE INTO outcome_labels(id, dispatch_id, label, primary_attribution, "
            "contributing_attributions, labeled_at, evidence_hash) VALUES(?,?,?,?,?,?,?)",
            tuple(row.values()),
        )
        written.append(row)
    return written
