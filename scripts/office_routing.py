#!/usr/bin/env python3
"""Derived routing for Task T2B (`docs/plans/v3-final-merge.md`, amendment v3).

`route()` here is the sole production implementation that `scripts/office_runtime.py`'s
delegation shim (§8.2, applied by T2) delegates to. The old monolithic `route()` in
that file decided every dispatch from four values the caller supplied rather than
derived: `adapter_state`, `absolute_floor_pass`, `advisory_pass`, and `local_reward`. An
orchestrator -- or a candidate describing itself -- could assert any of them, so the
gates constrained only an agent that chose to be constrained. This module makes all four
derived from recorded evidence in `runs.db` and the pinned `roles.<role>.floor` config
block; the caller's own claims about them are read nowhere below.

This module never edits `scripts/office_runtime.py` or `config/config.default.yaml`
(§8's exclusive T2 boundary) -- it only imports read-only helpers from the former.
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

try:
    from scripts import office_runtime as rt
except ImportError:
    import office_runtime as rt

try:
    from scripts import office_scoring as scoring
except ImportError:
    import office_scoring as scoring

MUTABLE_TRUST_ROLES = rt.MUTABLE_TRUST_ROLES

_REQUIRED_OVERRIDE_FIELDS = (
    "override_id", "run_id", "family_id", "task_id", "role", "candidate_id",
    "bypass_stage", "rationale", "authorized_by", "authorized_at", "expires_at",
)


def _num(v, default=float("inf")):
    return default if v is None else float(v)


def ensure_override_schema(con: sqlite3.Connection) -> None:
    """Creates `recorded_overrides` if not already present on `con`. No table for this
    is pinned in T0's contract (unlike `outcome_labels`, which has an explicit DDL in
    §7.3.3) and `office_runtime.py`'s `init_db` -- T2-owned -- does not create one, so
    this module owns the table's lifecycle end to end, the same way it owns
    `adapter_trust_acts` via `office_scoring.ensure_trust_schema`."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS recorded_overrides("
        "override_id TEXT PRIMARY KEY, run_id TEXT, family_id TEXT, task_id TEXT, "
        "role TEXT, candidate_id TEXT, bypass_stage INTEGER, rationale TEXT, "
        "authorized_by TEXT, authorized_at TEXT, expires_at TEXT)"
    )
    con.commit()


def record_override(db_path, record: dict) -> dict:
    """Logs a RecordedOverride (§7.5.1) into runs.db. §7.5.2 rule 5 requires the record
    be logged before it can authorize a bypass; `validate_override_record` below checks
    for exactly this row rather than trusting the in-request copy of the record."""
    con = sqlite3.connect(str(db_path))
    try:
        ensure_override_schema(con)
        con.execute(
            "INSERT OR REPLACE INTO recorded_overrides VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                record["override_id"], record.get("run_id"), record.get("family_id"),
                record.get("task_id"), record.get("role"), record.get("candidate_id"),
                record.get("bypass_stage"), record.get("rationale"),
                record.get("authorized_by"), record.get("authorized_at"),
                record.get("expires_at"),
            ),
        )
        con.commit()
    finally:
        con.close()
    return record


def _parse_datetime(value: Any):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_override_record(record: dict | None, request: dict, db_path=None) -> bool:
    """§7.5.2's validation rules, all of which must hold:
    1. authorized_by is strictly "user".
    2. expires_at is strictly in the future.
    3. family_id/task_id/role/candidate_id match the active routing request context.
    4. rationale is a substantive justification (>=10 non-whitespace characters).
    5. the record is logged into runs.db (checked by override_id, not re-trusted from
       the in-request copy).
    Returns False on any defect -- including a malformed record -- never raises; the
    shim's hard stop in `route()` treats "not valid" and "absent" identically.
    """
    if not isinstance(record, dict):
        return False
    if any(record.get(f) in (None, "") for f in _REQUIRED_OVERRIDE_FIELDS):
        return False
    if record["authorized_by"] != "user":
        return False
    if record["bypass_stage"] not in (2, 4, 7):
        return False
    if len(re.sub(r"\s+", "", str(record["rationale"]))) < 10:
        return False

    expires_at = _parse_datetime(record["expires_at"])
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        return False
    if _parse_datetime(record["authorized_at"]) is None:
        return False

    if record.get("role") != request.get("role"):
        return False
    if record.get("family_id") != request.get("family_id"):
        return False
    if record.get("task_id") != request.get("task_id"):
        return False
    if record.get("run_id") != request.get("run_id"):
        return False
    candidate_ids = {rt.candidate_id(c) for c in request.get("candidates", [])}
    if record["candidate_id"] not in candidate_ids:
        return False

    if not db_path:
        return False
    con = sqlite3.connect(str(db_path))
    try:
        ensure_override_schema(con)
        row = con.execute(
            "SELECT 1 FROM recorded_overrides WHERE override_id = ?",
            (record["override_id"],),
        ).fetchone()
    finally:
        con.close()
    return row is not None


def _connect(db_path):
    if not db_path:
        return None
    con = sqlite3.connect(str(db_path))
    scoring.ensure_trust_schema(con)
    ensure_override_schema(con)
    return con


def route(request: dict) -> dict:
    role = request["role"]
    playbook = request.get("playbook")
    policy = request.get("policy", {})
    required = set(request.get("required_capabilities", policy.get("required_capabilities", [])))
    reserve = float(policy.get("quota_reserve_percent", 20))
    cost_policy = request.get("cost_policy", policy.get("cost_policy", "balanced"))
    allow_advisory_undercut = bool(request.get("allow_advisory_undercut", True))
    preferred_seed = request.get("preferred_seed") or policy.get("preferred_seed")
    floor = policy.get("floor")
    rejected = []
    stage = []

    db_path = request.get("runs_db")

    # Hard stop (§7.5.3): an override request without a valid, recorded authorization
    # halts before any candidate is evaluated. Pinned here (not only in the T2 shim)
    # because §7.5.3's title is literally "Hard Stop in route()".
    override_requested = bool(request.get("allow_unverified_override") or request.get("allow_override"))
    override_record = None
    if override_requested:
        candidate_override = request.get("recorded_override")
        if not validate_override_record(candidate_override, request, db_path=db_path):
            return {
                "selected": None,
                "status": "override_not_authorized",
                "reason": (
                    "Execution requested unverified override without a valid recorded "
                    "override authorization."
                ),
                "rejected": [],
            }
        override_record = candidate_override

    def override_applies(cid: str, stage_no: int) -> bool:
        return bool(
            override_record
            and override_record["candidate_id"] == cid
            and override_record["bypass_stage"] == stage_no
        )

    con = _connect(db_path)
    try:
        # 1 hard exclusions
        for c in request.get("candidates", []):
            cid = rt.candidate_id(c)
            if c.get("hard_excluded") or c.get("local_hard_excluded"):
                rejected.append({"candidate": cid, "stage": 1, "reason": "hard exclusion"})
            else:
                stage.append(c)

        # 2 adapter validity/trust -- derived (§7.1). A caller-supplied `adapter_state`
        # is read nowhere below; only `evaluate_trust_state` against `runs_db` decides.
        nxt = []
        for c in stage:
            cid = rt.candidate_id(c)
            if role in MUTABLE_TRUST_ROLES:
                if con is not None:
                    _, trust_state = scoring.evaluate_trust_state(con, cid)
                else:
                    trust_state = "valid-unverified"
                if trust_state != "proven" and not override_applies(cid, 2):
                    rejected.append({
                        "candidate": cid,
                        "stage": 2,
                        "reason": (
                            f"adapter trust is derived as '{trust_state}', not proven "
                            "for normal mutable/gate authority"
                        ),
                    })
                    continue
            nxt.append(c)
        stage = nxt

        # 3 required capabilities
        nxt = []
        for c in stage:
            cid = rt.candidate_id(c)
            caps = set(c.get("capabilities", []))
            if not required.issubset(caps):
                rejected.append({"candidate": cid, "stage": 3, "reason": f"missing capabilities {sorted(required - caps)}"})
                continue
            nxt.append(c)
        stage = nxt

        # 4 absolute role floor -- derived from `roles.<role>.floor` config against the
        # candidate's pinned catalog row (§7.2). A caller-supplied `absolute_floor_pass`
        # is read nowhere below.
        nxt = []
        for c in stage:
            cid = rt.candidate_id(c)
            passed, reason = scoring.evaluate_capability_floor(c, floor)
            if not passed and not override_applies(cid, 4):
                rejected.append({"candidate": cid, "stage": 4, "reason": reason})
                continue
            nxt.append(c)
        stage = nxt

        # 5 task shape
        nxt = []
        for c in stage:
            cid = rt.candidate_id(c)
            supported = c.get("supported_playbooks")
            if supported and playbook and playbook not in supported:
                rejected.append({"candidate": cid, "stage": 5, "reason": "task shape unsupported"})
                continue
            nxt.append(c)
        stage = nxt

        if not stage:
            return {"selected": None, "status": "no_qualifying_candidate", "rejected": rejected}

        # 6 quota safety (unchanged: not one of the four values in this task's scope).
        safe = []; unknown = []; unsafe = []
        for c in stage:
            q = c.get("quota", {}); status = q.get("status", "unknown")
            if status != "ok" or q.get("tightest_remaining_percent") is None:
                unknown.append(c); continue
            remaining = float(q["tightest_remaining_percent"]); burn = float(q.get("projected_burn_percent") or 0)
            (safe if remaining - burn >= reserve else unsafe).append(c)
        if safe:
            for c in unsafe:
                rejected.append({"candidate": rt.candidate_id(c), "stage": 6, "reason": "projected quota crosses reserve while safe alternative exists"})
            if not request.get("allow_unknown_quota_with_safe_alternative", False):
                for c in unknown:
                    rejected.append({"candidate": rt.candidate_id(c), "stage": 6, "reason": "quota unknown while known-safe alternative exists"})
                stage = safe
            else:
                stage = safe + unknown
        elif unknown:
            for c in unsafe:
                rejected.append({"candidate": rt.candidate_id(c), "stage": 6, "reason": "known quota crosses reserve; only unknown candidates remain"})
            stage = unknown
        else:
            return {
                "selected": None, "status": "protected_quota_would_be_consumed", "rejected": rejected,
                "action": "choose a smaller/cheaper valid strategy, propose another route, or obtain explicit user authority",
            }

        # 7 advisory quality anchor. A caller-supplied per-candidate `advisory_pass` is
        # read nowhere below: retention is derived purely from `preferred_seed` match,
        # which is itself config/request data, never a candidate's self-assertion.
        if preferred_seed:
            matched = [c for c in stage if rt.preferred_rank(c, preferred_seed) is not None]
            advisory = matched if matched else stage
        else:
            advisory = stage
        if advisory and not allow_advisory_undercut:
            excluded = [c for c in stage if c not in advisory]
            kept = []
            for c in excluded:
                cid = rt.candidate_id(c)
                if override_applies(cid, 7):
                    kept.append(c)
                else:
                    rejected.append({"candidate": cid, "stage": 7, "reason": "advisory anchor retained by gear/policy"})
            stage = advisory + kept

        # 8 cost, 9 local tie-break
        def quota_burn(c): return _num(c.get("cost", {}).get("quota_burn"))
        def money(c): return _num(c.get("cost", {}).get("money_estimate"))
        def wall(c): return _num(c.get("cost", {}).get("wall_clock_seconds"))

        reward_cache: dict[str, float | None] = {}

        def reward_key(c):
            cid = rt.candidate_id(c)
            if cid not in reward_cache:
                reward_cache[cid] = scoring.compute_local_reward(con, cid) if con is not None else None
            return scoring.reward_sort_key(reward_cache[cid])

        if preferred_seed:
            default_rank = len(preferred_seed)
            stage.sort(key=lambda c: (
                rt.preferred_rank(c, preferred_seed) if rt.preferred_rank(c, preferred_seed) is not None else default_rank,
                quota_burn(c), money(c), wall(c), reward_key(c), rt.candidate_id(c),
            ))
        elif cost_policy == "quota_saver":
            stage.sort(key=lambda c: (quota_burn(c), money(c), wall(c), reward_key(c), rt.candidate_id(c)))
        elif cost_policy == "money_saver":
            stage.sort(key=lambda c: (money(c), quota_burn(c), wall(c), reward_key(c), rt.candidate_id(c)))
        else:
            known_money = [money(c) for c in stage if math.isfinite(money(c))]
            if known_money:
                cheapest = min(known_money); band = cheapest * 1.20
                in_band = [c for c in stage if money(c) <= band]
                if in_band:
                    out_band = [c for c in stage if c not in in_band]
                    for c in out_band:
                        rejected.append({"candidate": rt.candidate_id(c), "stage": 8, "reason": "outside balanced 20% cheapest-money band"})
                    stage = in_band
                    stage.sort(key=lambda c: (quota_burn(c), wall(c), reward_key(c), money(c), rt.candidate_id(c)))
                else:
                    stage.sort(key=lambda c: (quota_burn(c), wall(c), reward_key(c), money(c), rt.candidate_id(c)))
            else:
                stage.sort(key=lambda c: (quota_burn(c), wall(c), reward_key(c), rt.candidate_id(c)))

        chosen = stage[0]
        cid = rt.candidate_id(chosen)
        disclosure = rt.selection_disclosure(role, chosen, preferred_seed, cost_policy)
        for stage_no in (2, 4, 7):
            if override_applies(cid, stage_no):
                disclosure["override"] = {
                    "override_id": override_record["override_id"],
                    "bypass_stage": stage_no,
                    "authorized_by": override_record["authorized_by"],
                    "rationale": override_record["rationale"],
                }
                break
        return {
            "selected": cid, "status": "selected", "candidate": chosen, "rejected": rejected,
            "selection_disclosure": disclosure,
            "decision_hash": rt.sha256_obj({"role": role, "playbook": playbook, "selected": cid, "policy": policy, "candidate": chosen}),
        }
    finally:
        if con is not None:
            con.close()
