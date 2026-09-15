#!/usr/bin/env python3
"""Family registry, sticky focus, scoped amendments, landings/checkpoints/reviews (T2), §4.2.

Two durable layers live under a run's state_dir (`docs/v3-runtime-contracts.md` §1.3):

- `family_registry.json` -- a strict, schema-conformant summary (`family-registry.schema.json`,
  `additionalProperties: false`) consumed by `family-show`/`family-list`/`family-focus`.
- `families/<family_id>/family.json` -- the durable, schema-free full record (approval,
  paused scopes, advisory resource-demand, legacy-migration provenance) the registry
  summary is projected from. Restart reconstructs the registry from these files if the
  top-level registry is missing or corrupted, so no family is silently lost.

All registry/family mutation happens under a single per-state_dir advisory lock
(`.family_registry.lock`), read-modify-write, so concurrent callers against the same
state_dir cannot interleave a partial write or silently drop each other's records.
"""
from __future__ import annotations

import fcntl
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts import office_runtime as rt
except ImportError:
    import office_runtime as rt

FAMILY_PHASES = rt.PHASE_ORDER
AMENDMENT_KINDS = ("routing", "requirements", "plan_contract")
KIND_VERSION_FIELD = {
    "routing": "routing_version",
    "requirements": "requirements_version",
    "plan_contract": "plan_version",
}
VERSION_FIELDS = ("requirements_version", "plan_version", "routing_version")

_CANONICAL_FAMILY_KEYS = {
    "family_id", "repo", "issue", "phase", "requirements_version", "plan_version",
    "routing_version", "ownership", "dependencies", "active_dispatches", "latest_landing",
    "pending_decisions", "approval", "paused_scopes", "resource_demand", "session_id",
    "created_at", "updated_at",
}
_SUMMARY_KEYS = (
    "family_id", "repo", "issue", "phase", "requirements_version", "plan_version",
    "routing_version", "ownership", "dependencies", "active_dispatches", "latest_landing",
    "pending_decisions",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def _registry_path(state_dir) -> Path:
    return Path(state_dir) / "family_registry.json"


def _families_root(state_dir) -> Path:
    return Path(state_dir) / "families"


def _family_dir(state_dir, family_id) -> Path:
    return _families_root(state_dir) / family_id


def _family_json_path(state_dir, family_id) -> Path:
    return _family_dir(state_dir, family_id) / "family.json"


def _amendments_dir(state_dir, family_id) -> Path:
    return _family_dir(state_dir, family_id) / "amendments"


def _checkpoints_dir(state_dir, family_id) -> Path:
    return _family_dir(state_dir, family_id) / "checkpoints"


def _landings_dir(state_dir, family_id) -> Path:
    return _family_dir(state_dir, family_id) / "landings"


def _reviews_dir(state_dir, family_id) -> Path:
    return _family_dir(state_dir, family_id) / "reviews"


def _lock_path(state_dir) -> Path:
    return Path(state_dir) / ".family_registry.lock"


@contextmanager
def _locked(state_dir):
    lock_path = _lock_path(state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rt._atomic_write_json(path, obj)


def _summary_from_full(full: dict) -> dict:
    return {k: full.get(k) for k in _SUMMARY_KEYS}


# --------------------------------------------------------------------------
# registry load / reconstruct (§4.2 pinned: load_family_registry)
# --------------------------------------------------------------------------

def _reconstruct_locked(state_dir) -> dict:
    """Rebuilds the registry summary from `families/*/family.json` on disk.

    Used both when `family_registry.json` is missing/corrupted and directly by
    `reconstruct_family_registry` for the "restart reconstructs active families" receipt.
    """
    fam_root = _families_root(state_dir)
    families: dict = {}
    session_id = None
    focus_family_id = None
    if fam_root.is_dir():
        for d in sorted(p for p in fam_root.iterdir() if p.is_dir()):
            full = _read_json(d / "family.json")
            if not isinstance(full, dict) or "family_id" not in full:
                continue
            families[full["family_id"]] = _summary_from_full(full)
            session_id = session_id or full.get("session_id")
            if (d / "focus.ref").exists():
                focus_family_id = full["family_id"]
    if not families:
        return {"session_id": session_id or "", "current_focus_family_id": "", "families": {}}
    if focus_family_id is None:
        focus_family_id = sorted(families)[0]
    registry = {
        "session_id": session_id or "",
        "current_focus_family_id": focus_family_id,
        "updated_at": _now(),
        "families": families,
    }
    errors = rt.validate_with_schema(registry, "family-registry.schema.json")
    if not errors:
        _write_json_atomic(_registry_path(state_dir), registry)
    return registry


def _load_registry_locked(state_dir) -> dict:
    path = _registry_path(state_dir)
    data = _read_json(path)
    if data is None:
        return _reconstruct_locked(state_dir)
    errors = rt.validate_with_schema(data, "family-registry.schema.json")
    if errors:
        return _reconstruct_locked(state_dir)
    return data


def load_family_registry(state_dir: Path) -> dict:
    """Loads and validates family_registry.json with file locking."""
    with _locked(state_dir):
        return _load_registry_locked(state_dir)


def reconstruct_family_registry(state_dir: Path) -> dict:
    """Explicitly rebuilds and persists the registry from durable per-family records."""
    with _locked(state_dir):
        return _reconstruct_locked(state_dir)


def _write_registry_locked(state_dir, session_id: str, current_focus_family_id: str, families: dict) -> dict:
    registry = {
        "session_id": session_id or "",
        "current_focus_family_id": current_focus_family_id or "",
        "updated_at": _now(),
        "families": families,
    }
    errors = rt.validate_with_schema(registry, "family-registry.schema.json")
    if errors:
        raise ValueError("family registry failed schema validation: " + "; ".join(errors))
    _write_json_atomic(_registry_path(state_dir), registry)
    return registry


def _mark_focus_pointer(state_dir, family_id: str) -> None:
    fam_root = _families_root(state_dir)
    if fam_root.is_dir():
        for d in fam_root.iterdir():
            if not d.is_dir():
                continue
            ref = d / "focus.ref"
            if d.name != family_id and ref.exists():
                try:
                    ref.unlink()
                except OSError:
                    pass
    ref_path = _family_dir(state_dir, family_id) / "focus.ref"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(_now(), encoding="utf-8")


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def _default_full_record(family_id, session_id, repo, issue, phase, requirements_version,
                          plan_version, routing_version, ownership, dependencies, now) -> dict:
    return {
        "family_id": family_id,
        "repo": repo,
        "issue": issue,
        "phase": phase,
        "requirements_version": requirements_version,
        "plan_version": plan_version,
        "routing_version": routing_version,
        "ownership": ownership or {
            "holder_id": "unknown", "role": "orchestrator", "triple": "unknown@local/unknown@none",
        },
        "dependencies": list(dependencies or []),
        "active_dispatches": [],
        "latest_landing": None,
        "pending_decisions": [],
        "approval": None,
        "paused_scopes": [],
        "resource_demand": {},
        "session_id": session_id,
        "created_at": now,
        "updated_at": now,
    }


def register_family(state_dir, session_id: str, family_id: str, repo: str, issue,
                     *, phase: str = "intake", requirements_version: int = 1,
                     plan_version: int = 1, routing_version: int = 1,
                     ownership: dict | None = None, dependencies: list | None = None,
                     set_focus: bool | None = None) -> dict:
    """Registers (or idempotently re-registers) a family and, by default, focuses it
    only if it is the first family in the registry -- registering a second family never
    silently steals focus from the first (issue-77 sticky focus)."""
    with _locked(state_dir):
        registry = _load_registry_locked(state_dir)
        now = _now()
        existing_full = _read_json(_family_json_path(state_dir, family_id))
        if existing_full is not None:
            full = existing_full
        else:
            full = _default_full_record(family_id, session_id, repo, issue, phase,
                                         requirements_version, plan_version, routing_version,
                                         ownership, dependencies, now)
            _write_json_atomic(_family_json_path(state_dir, family_id), full)
        families = dict(registry.get("families", {}))
        families[family_id] = _summary_from_full(full)
        focus = registry.get("current_focus_family_id") or ""
        make_focus = set_focus if set_focus is not None else not focus
        if make_focus:
            focus = family_id
            _mark_focus_pointer(state_dir, family_id)
        session = registry.get("session_id") or session_id
        _write_registry_locked(state_dir, session, focus, families)
        return {"status": "registered", "family_id": family_id, "current_focus_family_id": focus}


def get_family(state_dir, family_id: str) -> dict | None:
    return _read_json(_family_json_path(state_dir, family_id))


# --------------------------------------------------------------------------
# sticky focus (§4.2 pinned: update_family_focus)
# --------------------------------------------------------------------------

def update_family_focus(state_dir: Path, family_id: str) -> dict:
    """Atomically shifts conversational focus to family_id."""
    with _locked(state_dir):
        registry = _load_registry_locked(state_dir)
        families = registry.get("families", {})
        if family_id not in families:
            return {"status": "error", "reason": "family_not_found", "family_id": family_id}
        previous = registry.get("current_focus_family_id")
        if previous == family_id:
            return {"status": "ok", "previous_focus": previous, "current_focus": family_id}
        _mark_focus_pointer(state_dir, family_id)
        session = registry.get("session_id") or ""
        _write_registry_locked(state_dir, session, family_id, families)
        return {"status": "ok", "previous_focus": previous, "current_focus": family_id}


def resolve_focus_target(state_dir, requested_family_id: str | None = None,
                          explicit_global: bool = False) -> dict:
    """Implements the F7 sticky-focus matrix (`protocol/families-and-amendments.md`):
    unqualified -> current focus only; named -> that family, and focus moves to it;
    genuinely ambiguous -> no mutation, with a reason; explicitly global -> every family,
    no focus change. Callers apply their own domain mutation (e.g. `apply_amendment`)
    only to the family/families this function resolves to.
    """
    with _locked(state_dir):
        registry = _load_registry_locked(state_dir)
        families = registry.get("families", {})
        if explicit_global:
            return {"status": "ok", "scope": "global", "family_ids": sorted(families), "mutated": False}
        if requested_family_id is not None:
            if requested_family_id not in families:
                return {"status": "ambiguous", "scope": "named",
                        "reason": f"family_id {requested_family_id!r} is not registered",
                        "mutated": False}
            previous = registry.get("current_focus_family_id")
            mutated = previous != requested_family_id
            if mutated:
                _mark_focus_pointer(state_dir, requested_family_id)
                session = registry.get("session_id") or ""
                _write_registry_locked(state_dir, session, requested_family_id, families)
            return {"status": "ok", "scope": "named", "family_id": requested_family_id,
                    "previous_focus": previous, "mutated": mutated}
        focus = registry.get("current_focus_family_id")
        if not focus or focus not in families:
            return {"status": "ambiguous", "scope": "unqualified",
                    "reason": "no unqualified focus family is registered; name a family explicitly",
                    "mutated": False}
        return {"status": "ok", "scope": "unqualified", "family_id": focus, "mutated": False}


def update_family(state_dir, family_id: str, *, phase: str | None = None,
                   latest_landing: dict | None = None) -> dict:
    """Backing implementation for the `family-update` CLI command (§5.1)."""
    with _locked(state_dir):
        full = _read_json(_family_json_path(state_dir, family_id))
        if full is None:
            return {"status": "error", "reason": "family_not_found", "family_id": family_id}
        if phase is not None:
            if phase not in FAMILY_PHASES:
                return {"status": "error", "reason": "invalid_phase", "phase": phase,
                        "expected": list(FAMILY_PHASES)}
            cur_idx = FAMILY_PHASES.index(full.get("phase", "intake"))
            new_idx = FAMILY_PHASES.index(phase)
            if new_idx not in (cur_idx, cur_idx + 1):
                expected = full.get("phase") if cur_idx == len(FAMILY_PHASES) - 1 else FAMILY_PHASES[cur_idx + 1]
                return {"status": "error", "reason": "invalid_phase_transition",
                        "from": full.get("phase"), "to": phase, "expected": expected}
            full["phase"] = phase
        if latest_landing is not None:
            required = ("landing_id", "task_id", "head_sha", "validation_evidence", "evidence_hash")
            missing = [k for k in required if k not in latest_landing]
            if missing:
                return {"status": "error", "reason": "invalid_latest_landing", "missing": missing}
            full["latest_landing"] = latest_landing
        full["updated_at"] = _now()
        _write_json_atomic(_family_json_path(state_dir, family_id), full)
        registry = _load_registry_locked(state_dir)
        families = dict(registry.get("families", {}))
        families[family_id] = _summary_from_full(full)
        _write_registry_locked(state_dir, registry.get("session_id") or "",
                                registry.get("current_focus_family_id") or family_id, families)
    return {"status": "updated", "family_id": family_id}


# --------------------------------------------------------------------------
# amendments (§4.2 pinned: apply_amendment)
# --------------------------------------------------------------------------

def apply_amendment(
    state_dir: Path,
    family_id: str,
    kind: str,
    affected_scopes: list[str],
    reason: str,
    evidence: str,
    evidence_hash: str,
    version_bumps: dict[str, int],
    *,
    expected_prior_versions: dict | None = None,
    session_id: str | None = None,
    amendment_id: str | None = None,
) -> dict:
    """Atomically applies amendment, updating versions and notifying affected scopes.

    Per issue-35 decision 5 and `protocol/families-and-amendments.md`: `kind` owns
    exactly one version field (`routing`->routing_version, `requirements`->
    requirements_version, `plan_contract`->plan_version); a `version_bumps` entry for any
    other field is rejected rather than silently applied -- naively bumping every version
    on every delta is exactly the defect this guards against. All validation happens
    before the first write, so a rejected/conflicting call leaves both `family.json` and
    `family_registry.json` byte-identical to their pre-call state.
    """
    if kind not in AMENDMENT_KINDS:
        return {"status": "error", "reason": "invalid_kind", "kind": kind, "expected": list(AMENDMENT_KINDS)}
    owned_field = KIND_VERSION_FIELD[kind]

    with _locked(state_dir):
        full = _read_json(_family_json_path(state_dir, family_id))
        if full is None:
            return {"status": "error", "reason": "family_not_found", "family_id": family_id}
        current_versions = {f: full[f] for f in VERSION_FIELDS}

        if expected_prior_versions is not None:
            mismatches = {
                f: {"expected": expected_prior_versions.get(f), "actual": current_versions[f]}
                for f in VERSION_FIELDS
                if f in expected_prior_versions and expected_prior_versions[f] != current_versions[f]
            }
            if mismatches:
                return {"status": "conflict", "reason": "stale expected_prior_versions",
                        "mismatches": mismatches, "current_versions": current_versions}

        out_of_scope = {
            f: v for f, v in version_bumps.items()
            if f in VERSION_FIELDS and f != owned_field and v != current_versions[f]
        }
        if out_of_scope:
            return {"status": "error", "reason": "amendment kind may only change its own version field",
                    "kind": kind, "owned_field": owned_field, "out_of_scope": out_of_scope}

        new_value = version_bumps.get(owned_field, current_versions[owned_field] + 1)
        if new_value <= current_versions[owned_field]:
            return {"status": "error", "reason": "resulting version must strictly increase",
                    "field": owned_field, "current": current_versions[owned_field], "requested": new_value}

        resulting_versions = dict(current_versions)
        resulting_versions[owned_field] = new_value

        amendment_id = amendment_id or ("amend-" + uuid.uuid4().hex[:16])
        now = _now()
        record = {
            "amendment_id": amendment_id, "family_id": family_id, "kind": kind,
            "affected_scopes": list(affected_scopes), "expected_prior_versions": current_versions,
            "resulting_versions": resulting_versions, "reason": reason, "evidence": evidence,
            "evidence_hash": evidence_hash, "applied_at": now,
        }
        if session_id:
            record["session_id"] = session_id
        errors = rt.validate_with_schema(record, "amendment.schema.json")
        if errors:
            return {"status": "error", "reason": "amendment failed schema validation", "errors": errors}

        amend_dir = _amendments_dir(state_dir, family_id)
        amend_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(amend_dir / f"{amendment_id}.json", record)

        wakes_planner = kind == "plan_contract"
        paused_scopes = full.get("paused_scopes", [])
        if wakes_planner:
            paused_scopes = sorted(set(paused_scopes) | set(affected_scopes))

        full.update(resulting_versions)
        full["paused_scopes"] = paused_scopes
        full["updated_at"] = now
        # A routing-only or requirements-fits-plan delta never invalidates run-level
        # plan/requirements approval (Core Invariant, §1.1); only a plan_contract delta
        # that actually changed plan_version can, and only the stale approval itself.
        approval = full.get("approval")
        if isinstance(approval, dict) and kind == "plan_contract" and approval.get("plan_version") != resulting_versions["plan_version"]:
            full.setdefault("invalidated_approvals", []).append({**approval, "invalidated_at": now})
            full["approval"] = None
        _write_json_atomic(_family_json_path(state_dir, family_id), full)

        registry = _load_registry_locked(state_dir)
        families = dict(registry.get("families", {}))
        families[family_id] = _summary_from_full(full)
        _write_registry_locked(state_dir, registry.get("session_id") or session_id or "",
                                registry.get("current_focus_family_id") or family_id, families)

        return {
            "status": "amended", "amendment_id": amendment_id, "family_id": family_id, "kind": kind,
            "resulting_versions": resulting_versions, "wakes_planner": wakes_planner,
            "paused_scopes": paused_scopes if wakes_planner else [],
            "active_dispatches": list(full.get("active_dispatches", [])),
            "approval_intact": full.get("approval") is not None,
        }


def approve_family_plan(state_dir, family_id: str, *, approved_by: str, quote: str) -> dict:
    """Family-scoped analogue of `office_runtime.cmd_approve_plan`, recorded on
    `family.json`'s free-form `approval` field (not schema-bound, unlike the registry)."""
    with _locked(state_dir):
        full = _read_json(_family_json_path(state_dir, family_id))
        if full is None:
            return {"status": "error", "reason": "family_not_found", "family_id": family_id}
        if not quote.strip():
            return {"status": "error", "reason": "empty_quote"}
        now = _now()
        approval = {
            "by": approved_by, "quote": quote, "at": now,
            "requirements_version": full["requirements_version"],
            "plan_version": full["plan_version"],
            "routing_version": full["routing_version"],
        }
        full["approval"] = approval
        full["updated_at"] = now
        _write_json_atomic(_family_json_path(state_dir, family_id), full)
    return {"status": "approved", "approval": approval}


# --------------------------------------------------------------------------
# advisory resource-demand projection (never blocks unrelated families)
# --------------------------------------------------------------------------

def project_resource_demand(family_id: str, quota_snapshot: dict, reserve_percent: float = 20.0) -> dict:
    remaining = quota_snapshot.get("tightest_remaining_percent")
    if remaining is None:
        return {"family_id": family_id, "status": "unknown", "projected_remaining": None}
    burn = float(quota_snapshot.get("projected_burn_percent") or 0)
    projected = float(remaining) - burn
    status = "ok" if projected >= reserve_percent else "warning"
    return {"family_id": family_id, "status": status, "projected_remaining": projected,
            "reserve_percent": reserve_percent}


def project_family_collisions(quota_snapshots: dict[str, dict], reserve_percent: float = 20.0) -> dict:
    """Advisory only (issue-77): a projected collision on one family never delays or
    blocks another family's unrelated dispatch."""
    per_family = {
        fid: project_resource_demand(fid, snap, reserve_percent)
        for fid, snap in quota_snapshots.items()
    }
    collisions = sorted(f for f, r in per_family.items() if r["status"] == "warning")
    runnable = sorted(f for f in per_family if f not in collisions)
    return {"collisions": collisions, "runnable": runnable, "per_family": per_family}


# --------------------------------------------------------------------------
# landings / checkpoints / reviews (amendment v2 finding F6)
# --------------------------------------------------------------------------

def record_landing(state_dir, family_id: str, landing: dict) -> dict:
    errors = rt.validate_with_schema(landing, "landing.schema.json")
    if errors:
        return {"status": "error", "reason": "schema_invalid", "errors": errors}
    validation_evidence = landing.get("validation_evidence") or {}
    if not validation_evidence.get("passed"):
        return {"status": "error", "reason": "missing_validation_evidence"}
    with _locked(state_dir):
        full = _read_json(_family_json_path(state_dir, family_id))
        if full is None:
            return {"status": "error", "reason": "family_not_found", "family_id": family_id}
        landing_id = landing["landing_id"]
        land_dir = _landings_dir(state_dir, family_id)
        land_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(land_dir / f"{landing_id}.json", landing)
        completed_tasks = landing.get("completed_tasks") or []
        full["latest_landing"] = {
            "landing_id": landing_id,
            "task_id": completed_tasks[0] if completed_tasks else str(landing.get("scope")),
            "head_sha": landing["head_sha"],
            "validation_evidence": validation_evidence.get("output_summary") or "PASS",
            "evidence_hash": validation_evidence["evidence_hash"],
            "timestamp": landing.get("created_at") or _now(),
        }
        full["updated_at"] = _now()
        _write_json_atomic(_family_json_path(state_dir, family_id), full)
        registry = _load_registry_locked(state_dir)
        families = dict(registry.get("families", {}))
        families[family_id] = _summary_from_full(full)
        _write_registry_locked(state_dir, registry.get("session_id") or "",
                                registry.get("current_focus_family_id") or family_id, families)
    return {"status": "recorded", "landing_id": landing_id, "head_sha": landing["head_sha"]}


def validate_checkpoint(data: dict) -> list[str]:
    return rt.validate_with_schema(data, "checkpoint.schema.json")


def save_checkpoint(state_dir, family_id: str, checkpoint: dict) -> dict:
    errors = validate_checkpoint(checkpoint)
    if errors:
        return {"status": "error", "reason": "schema_invalid", "errors": errors}
    with _locked(state_dir):
        if _read_json(_family_json_path(state_dir, family_id)) is None:
            return {"status": "error", "reason": "family_not_found", "family_id": family_id}
        checkpoint_id = checkpoint["checkpoint_id"]
        chk_dir = _checkpoints_dir(state_dir, family_id)
        chk_dir.mkdir(parents=True, exist_ok=True)
        path = chk_dir / f"{checkpoint_id}.json"
        _write_json_atomic(path, checkpoint)
    return {"status": "saved", "checkpoint_id": checkpoint_id, "path": str(path)}


def load_checkpoint(state_dir, *, family_id: str | None = None, checkpoint_id: str | None = None) -> dict | None:
    if family_id is not None and checkpoint_id is not None:
        return _read_json(_checkpoints_dir(state_dir, family_id) / f"{checkpoint_id}.json")
    fam_root = _families_root(state_dir)
    if not fam_root.is_dir() or checkpoint_id is None:
        return None
    for d in sorted(fam_root.iterdir()):
        if not d.is_dir():
            continue
        found = _read_json(d / "checkpoints" / f"{checkpoint_id}.json")
        if found is not None:
            return found
    return None


def record_review(state_dir, review: dict) -> dict:
    errors = rt.validate_with_schema(review, "review-result.schema.json")
    if errors:
        return {"status": "error", "reason": "schema_invalid", "errors": errors}
    if review.get("producer_id") == review.get("reviewer_id"):
        return {"status": "error", "reason": "self_review_not_independent"}
    if not review.get("evidence"):
        return {"status": "error", "reason": "empty_evidence"}
    family_id = review.get("family_id")
    full = _read_json(_family_json_path(state_dir, family_id)) if family_id else None
    if full is not None:
        for f in VERSION_FIELDS:
            if review.get(f) != full.get(f):
                return {"status": "error", "reason": "version_mismatch", "field": f,
                        "review": review.get(f), "family": full.get(f)}
        rev_dir = _reviews_dir(state_dir, family_id)
    else:
        rev_dir = Path(state_dir) / "reviews"
    rev_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(rev_dir / f"{review['review_id']}.json", review)
    return {"status": "recorded", "review_id": review["review_id"], "overall_status": review["overall_status"]}


# --------------------------------------------------------------------------
# legacy state migration -- never silently certify a stale packet or grant ownership
# --------------------------------------------------------------------------

def migrate_legacy_family(state_dir, legacy: dict, *, session_id: str, family_id: str | None = None,
                           persist: bool = True) -> dict:
    """Migrates a pre-v3 family-ish record into a v3 family: backs up the raw legacy
    bytes first, defaults missing version fields to 1, preserves every field the v3
    shape doesn't recognise under `_legacy_unknown_fields` on the durable (schema-free)
    `family.json`, and always resets `active_dispatches`/`approval`/`latest_landing`
    rather than inheriting a claimed ownership or a stale completed-looking packet from
    the legacy record. `persist=False` loads the migration read-only (returns the
    migrated shape without writing state)."""
    family_id = family_id or legacy.get("family_id") or ("fam-" + uuid.uuid4().hex[:12])
    now = _now()

    backup_dir = Path(state_dir) / "legacy_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{family_id}-{now.replace(':', '').replace('+', '_')}.json"
    backup_path.write_text(json.dumps(legacy, indent=2, sort_keys=True), encoding="utf-8")

    phase = legacy.get("phase")
    if phase not in FAMILY_PHASES:
        phase = "intake"
    unknown_fields = {k: v for k, v in legacy.items() if k not in _CANONICAL_FAMILY_KEYS}

    full = {
        "family_id": family_id,
        "repo": legacy.get("repo") or "unknown",
        "issue": legacy.get("issue") if legacy.get("issue") is not None else 0,
        "phase": phase,
        "requirements_version": legacy.get("requirements_version") or 1,
        "plan_version": legacy.get("plan_version") or 1,
        "routing_version": legacy.get("routing_version") or 1,
        "ownership": {"holder_id": "unknown", "role": "orchestrator", "triple": "unknown@local/unknown@none"},
        "dependencies": list(legacy.get("dependencies") or []),
        "active_dispatches": [],
        "latest_landing": None,
        "pending_decisions": list(legacy.get("pending_decisions") or []),
        "approval": None,
        "paused_scopes": [],
        "resource_demand": {},
        "session_id": session_id,
        "created_at": now,
        "updated_at": now,
        "migrated_from_legacy": True,
        "legacy_backup_path": str(backup_path),
        "_legacy_unknown_fields": unknown_fields,
    }
    if not persist:
        return {"status": "migrated_read_only", "family": full, "backup_path": str(backup_path)}

    with _locked(state_dir):
        _write_json_atomic(_family_json_path(state_dir, family_id), full)
        registry = _load_registry_locked(state_dir)
        families = dict(registry.get("families", {}))
        make_focus = not registry.get("families")
        families[family_id] = _summary_from_full(full)
        focus = registry.get("current_focus_family_id") or ""
        if make_focus or not focus:
            focus = family_id
            _mark_focus_pointer(state_dir, family_id)
        _write_registry_locked(state_dir, registry.get("session_id") or session_id, focus, families)
    return {"status": "migrated", "family_id": family_id, "backup_path": str(backup_path),
            "unknown_fields": sorted(unknown_fields)}


# --------------------------------------------------------------------------
# family/session-tier config resolution: dispatch > family/project > repo > session >
# user > default (protocol/families-and-amendments.md)
# --------------------------------------------------------------------------

def resolve_family_config_tiers(repo_root: Path | None, state_dir: Path | None, family_id: str | None,
                                 session_overrides: dict | None = None,
                                 dispatch_overrides: dict | None = None,
                                 user_path: str | None = None) -> tuple[dict, list, list]:
    """Extends `office_runtime.resolve_config_tiers`'s plugin_default/user/repo layering
    with the session and family/project tiers that resolver has no concept of, in the
    precedence `dispatch > family/project > repo > session > user > default`. Reuses
    `deep_merge`/`NON_CONFIGURABLE_KEYS` so type-mismatch and unknown-key handling match
    the existing resolver exactly."""
    default_cfg = rt.load_data(rt.config_default_path()) or {}
    allowed_top = set(default_cfg) - rt.NON_CONFIGURABLE_KEYS
    warnings: list = []
    report: list = []

    def _kept(tier, data):
        kept = {}
        for k, v in (data or {}).items():
            if k == "schema_version":
                continue
            if k in rt.NON_CONFIGURABLE_KEYS:
                warnings.append({"tier": tier, "key": k, "reason": "not-configurable-ignored"})
            elif k not in allowed_top:
                warnings.append({"tier": tier, "key": k, "reason": "unknown-key-ignored"})
            else:
                kept[k] = v
        return kept

    paths_cfg = default_cfg.get("paths", {}) or {}
    up = Path(user_path).expanduser() if user_path else Path(
        paths_cfg.get("user", "~/.config/auto-office/config.yaml")
    ).expanduser()
    user_data = rt.load_data(up) if up.is_file() else None

    session_path = (Path(state_dir) / "session.config.yaml") if state_dir else None
    session_data = session_overrides
    if session_data is None and session_path is not None and session_path.is_file():
        session_data = rt.load_data(session_path)

    repo_path = (Path(repo_root) / paths_cfg.get("repo", ".auto-office/config.yaml")) if repo_root else None
    repo_data = rt.load_data(repo_path) if repo_path is not None and repo_path.is_file() else None

    family_path = (_family_dir(state_dir, family_id) / "config.yaml") if (state_dir and family_id) else None
    family_data = rt.load_data(family_path) if family_path is not None and family_path.is_file() else None

    layers = [
        ("plugin_default", None, default_cfg),
        ("user", up, user_data),
        ("session", session_path, session_data),
        ("repo", repo_path, repo_data),
        ("family_project", family_path, family_data),
        ("dispatch", None, dispatch_overrides),
    ]

    effective = default_cfg
    for tier, path, data in layers:
        entry = {"tier": tier, "path": str(path) if path else None, "present": data is not None}
        if data is None:
            report.append(entry)
            continue
        if tier != "plugin_default":
            data = _kept(tier, data)
            effective = rt.deep_merge(effective, data, tier, warnings)
        entry["applied_keys"] = sorted(data)
        report.append(entry)

    return effective, report, warnings
