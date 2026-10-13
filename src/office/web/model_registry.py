"""Read-only, evidence-backed Model Registry view for the Office Workstation.

Catalog identity, model policy, technical qualification, and account state are
independent. This view never launches a harness, probes quota, writes config,
or turns a score into conformance or trust.
"""
from __future__ import annotations

from collections import defaultdict

import yaml

from office import adapters, candidates, config, model_lineages, paths, route_policy
from office.benchmarks import index_version


def view() -> dict:
    policy = route_policy.user_policy(config.resolve(None)[0])
    rows = candidates.catalog_rows()
    catalog = model_lineages.partition(rows)
    index = index_version()
    installed_adapters = adapters.load_all()
    harnesses = sorted({r.get("invocation_harness") for r in rows if r.get("invocation_harness")})
    installed = {h: bool(installed_adapters.get(h) and adapters.installed(installed_adapters[h])) for h in harnesses}
    by_identity: dict[tuple, list[dict]] = defaultdict(list)
    for row in catalog["active"]:
        by_identity[(row["lineage_key"], row["generation_version"], row["model_id"],
                     row.get("invocation_harness"))].append(row)
    generations = []
    for (lineage, version, model_id, harness), variants in sorted(by_identity.items()):
        variants = sorted(variants, key=lambda r: (str(r.get("effort")), str(r.get("source_identifier"))))
        permission = route_policy.is_denied({"model_id": model_id, "harness": harness}, policy)
        specs = []
        for r in variants:
            route_state = route_policy.row_status(r)
            specs.append({
                "effort": r.get("effort"),
                "invocation_model_id": r.get("invocation_model_id"),
                "source_identifier": r.get("source_identifier"),
                "source_name": r.get("source_name"),
                "source_effort": r.get("source_effort"),
                "source_metadata": r.get("source_snapshot_metadata"),
                "benchmark_score": (r.get("benchmark_indexes") or {}).get(index),
                "benchmark_index": index,
                "effort_confidence": r.get("effort_confidence"),
                "technical_state": ("not-installed" if not installed.get(harness) else route_state["status"]),
                "technical_reason": (f"{harness} is not installed on this machine" if not installed.get(harness)
                                     else route_state["reason"]),
                "dispatchable_seed": r.get("dispatchable") is not False,
            })
        generations.append({
            "key": f"{lineage}/{version}/{harness}",
            "lineage": lineage, "version": version, "model_id": model_id,
            "harness": harness, "installed": installed.get(harness, False),
            "efforts": specs,
            "user_denied": bool(permission), "user_denial_reason": permission,
            "user_policy_source": (policy.get("sources") or {}).get("denied_models"),
            "version_ceiling": (policy.get("ceilings") or {}).get(lineage),
            "ceiling_source": (policy.get("ceiling_sources") or {}).get(lineage),
            "status_source": "runtime facts (no live probe in Settings)",
            "quota": {"status": "unknown", "value": None},
        })
    archive = [{
        "lineage": r.get("lineage_key"), "version": r.get("generation_version"),
        "model_id": r.get("model_id"), "harness": r.get("invocation_harness"),
        "effort": r.get("effort"),
    } for r in catalog["archived"]]
    pending = [{
        "model_id": r.get("model_id"), "harness": r.get("invocation_harness"),
        "effort": r.get("effort"), "reason": r["catalog_status_reason"],
    } for r in catalog["pending"]]
    # Historical benchmark evidence lives in catalog/archive and must not
    # be reintroduced into candidates or conformance/trust calculations.
    archive_dir = paths.resources_root() / "catalog" / "archive"
    for file in sorted(archive_dir.glob("*.yaml")):
        evidence = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        for historic in evidence.get("models") or []:
            if not isinstance(historic, dict):
                continue
            archive.append({
                "lineage": None, "version": None,
                "model_id": historic.get("model_id"), "harness": historic.get("invocation_harness"),
                "effort": historic.get("effort"), "archived_source": file.name,
                "source_identifier": historic.get("source_identifier"),
                "benchmark_index": evidence.get("benchmark_index_version"),
            })
    return {
        "index": index,
        "generations": generations, "archive": archive, "pending": pending,
        "counts": {**{k: len(v) for k, v in catalog.items()}, "historical": len(archive)},
        "policy": {
            "denied_models": policy["denied"], "version_ceilings": policy["ceilings"],
            "overkill_rules": policy["overkill"], "sources": policy["sources"],
        },
        "authority": "Office model registry / global user config (read-only projection)",
        "quota_probed": False, "telemetry_opt_in": False,
    }
