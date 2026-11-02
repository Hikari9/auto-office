#!/usr/bin/env python3
"""Execution packet construction, validation and invalidation for Auto Office v3 (T2).

Implements the §4.1 pinned signatures (`docs/v3-runtime-contracts.md`) against
`schemas/execution-packet.schema.json` (§2.1). Packets built here are the sole way a
dispatch carries full session/family/version provenance forward; nothing else in this
module edits `scripts/office_runtime.py` or `config/config.default.yaml` (those remain
T2's exclusive verbatim-block files per §8) -- it only imports read-only helpers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts import office_runtime as rt
except ImportError:
    import office_runtime as rt


def create_execution_packet(
    run_id: str,
    session_id: str,
    family_id: str,
    task_id: str,
    versions: tuple[int, int, int],  # (req, plan, route)
    packet_version: int,
    task_scope: str | list[str],
    observable_outcome: str,
    blast_radius: dict | str,
    allowed_mutations: list[str],
    protected_paths: list[str],
    validation_commands: list[str],
    selection_disclosure: dict,
    effective_config_hash: str,
    base_sha: str,
    replaced_dispatch_id: str | None = None,
    is_replacement: bool = False,
    **kwargs,
) -> dict:
    """Constructs and validates a v3 execution packet against execution-packet.schema.json.

    Finding R4 correction: `session_id` and `packet_version` are named parameters, not
    reachable only via `**kwargs`, matching their presence in
    `execution-packet.schema.json`'s `required` list.

    Finding R4 addition (§2.1): a caller that asserts this packet is issued as a
    replacement (`is_replacement=True`, e.g. because a `routing_version` bump came from
    §3.1's replacement path) without also supplying `replaced_dispatch_id` is rejected
    outright -- the schema cannot express "replacement" as a conditional requirement
    because `replaced_dispatch_id`'s own non-null presence *is* the signal, so this
    function is the enforcement point instead.
    """
    if is_replacement and not replaced_dispatch_id:
        raise ValueError(
            "create_execution_packet: is_replacement=True requires a non-null "
            "replaced_dispatch_id (§2.1 Finding R4)"
        )

    requirements_version, plan_version, routing_version = versions
    packet_id = kwargs.pop("packet_id", None) or ("pkt-" + uuid.uuid4().hex[:16])

    packet: dict[str, Any] = {
        "packet_id": packet_id,
        "run_id": run_id,
        "session_id": session_id,
        "family_id": family_id,
        "task_id": task_id,
        "requirements_version": requirements_version,
        "plan_version": plan_version,
        "routing_version": routing_version,
        "packet_version": packet_version,
        "effective_config_hash": effective_config_hash,
        "base_sha": base_sha,
        "selection_disclosure": selection_disclosure,
        "task_scope": task_scope,
        "observable_outcome": observable_outcome,
        "blast_radius": blast_radius,
        "allowed_mutations": list(allowed_mutations),
        "protected_paths": list(protected_paths),
        "validation_commands": list(validation_commands),
        "known_bad_behavior_to_exclude": kwargs.pop("known_bad_behavior_to_exclude", []),
        "self_review": kwargs.pop("self_review", ""),
        "rollback_or_restore_notes": kwargs.pop("rollback_or_restore_notes", ""),
    }
    if replaced_dispatch_id is not None:
        packet["replaced_dispatch_id"] = replaced_dispatch_id
    for optional_key in ("plan_path", "plan_sha", "escalation"):
        if optional_key in kwargs and kwargs[optional_key] is not None:
            packet[optional_key] = kwargs[optional_key]

    errors = validate_packet(packet)
    if errors:
        raise ValueError("execution packet failed schema validation: " + "; ".join(errors))
    return packet


def validate_packet(packet_data: dict, schema_name: str = "execution-packet.schema.json") -> list[str]:
    """Validates packet against Draft 2020-12 schema, returning list of error strings."""
    return rt.validate_with_schema(packet_data, schema_name)


def _packets_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "packets"


def write_execution_packet(state_dir: Path, packet: dict) -> Path:
    """Persists a validated packet to `.office/packets/<packet_id>.json`.

    Not part of the §4.1 pinned surface, but required so `invalidate_packets` (also
    pinned) has something durable to scan; the dispatch spawn path is out of T2's scope.

    Stored as an envelope (`{"packet": ..., "meta": {...}}`), not the bare packet: the
    packet schema is `additionalProperties: false`, so invalidation bookkeeping
    (`invalidated`, `invalidated_at`) cannot live on the packet object itself without
    breaking re-validation against `execution-packet.schema.json`.
    """
    errors = validate_packet(packet)
    if errors:
        raise ValueError("refusing to persist an invalid execution packet: " + "; ".join(errors))
    out_dir = _packets_dir(state_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{packet['packet_id']}.json"
    envelope = {"packet": packet, "meta": {"invalidated": False}}
    rt._atomic_write_json(path, envelope)
    return path


def read_execution_packet(state_dir: Path, packet_id: str) -> dict | None:
    path = _packets_dir(state_dir) / f"{packet_id}.json"
    if not path.exists():
        return None
    return rt.load_data(path)


def invalidate_packets(state_dir: Path, plan_version: int, affected_scopes: list[str] | None = None) -> int:
    """Marks stale packets invalidated for given plan version and scopes.

    A packet is stale when its recorded `plan_version` is older than `plan_version` and,
    if `affected_scopes` is given, its `task_id` or any entry of its (possibly list-typed)
    `task_scope` intersects `affected_scopes`. Invalidation is recorded on the storage
    envelope's `meta` (`invalidated: true`, `invalidated_at`, `invalidated_for_plan_version`)
    rather than by deleting or mutating the packet object, preserving both the packet as
    schema-valid evidence and the invalidation event.
    """
    out_dir = _packets_dir(state_dir)
    if not out_dir.is_dir():
        return 0
    scopes = set(affected_scopes) if affected_scopes else None
    count = 0
    now = datetime.now(timezone.utc).isoformat()
    for path in sorted(out_dir.glob("*.json")):
        try:
            envelope = rt.load_data(path)
        except Exception:
            continue
        if not isinstance(envelope, dict) or "packet" not in envelope:
            continue
        packet = envelope["packet"]
        meta = envelope.setdefault("meta", {})
        if meta.get("invalidated"):
            continue
        if packet.get("plan_version", 0) >= plan_version:
            continue
        if scopes is not None:
            task_scope = packet.get("task_scope")
            scope_values = set(task_scope) if isinstance(task_scope, list) else {task_scope}
            scope_values.add(packet.get("task_id"))
            if not (scopes & scope_values):
                continue
        meta["invalidated"] = True
        meta["invalidated_at"] = now
        meta["invalidated_for_plan_version"] = plan_version
        rt._atomic_write_json(path, envelope)
        count += 1
    return count
