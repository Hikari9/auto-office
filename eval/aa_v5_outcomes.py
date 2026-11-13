#!/usr/bin/env python3
"""Read-only extraction of actual Office accepted-work episodes for #862.

Requires a consistent local snapshot of runs.db; outputs private attributed IDs.
No synthetic outcomes, no migrations, and no issue/PR upload of raw records.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from office import route_learning

def canonical_routes():
    with (ROOT / "catalog" / "routes.tsv").open(newline="") as fh:
        cat = list(csv.DictReader(fh, delimiter="\t"))
    aliases = {}
    ambiguous = set()
    for row in cat:
        exact = row["route_key"]
        alias = f'{row["invocation_harness"]}/{row["invocation_model_id"]}@{row["effort"]}'
        for key in (exact, alias):
            if key in aliases and aliases[key] != exact:
                ambiguous.add(key)
            aliases[key] = exact
    return {k: v for k, v in aliases.items() if k not in ambiguous}

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def domain_of(ep):
    d = ep.get("descriptor") or {}
    for key in ("evidence_domain", "domain", "task_domain"):
        v = d.get(key)
        if isinstance(v, str) and v in ("frontend", "backend", "infra", "docs", "tests", "mixed"):
            return v
    return "unknown"

def extract(db_path: Path):
    db_path = db_path.expanduser().resolve(strict=True)
    initial = digest(db_path)
    # read-only SQLite URI; do not call office.db.connect (it migrates schemas).
    uri = "file:" + quote(str(db_path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        need = {"runs", "tasks", "dispatches", "revisions", "gates", "findings", "outcome_labels"}
        if missing := (need - tables):
            raise ValueError(f"required outcome tables unavailable: {sorted(missing)}")
        integrity = con.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"database check failed: {integrity}")
        outcomes = route_learning.derive_outcomes(con)
        episodes = route_learning.episodes(outcomes)
        aliases = canonical_routes()
        grouped = {}
        for d in outcomes:
            grouped.setdefault((d["run_id"], d["task_id"], d["route"]), []).append(d)
        dispatch_cols = {x[1] for x in con.execute("PRAGMA table_info(dispatches)")}
        quota = {}
        if "quota_delta" in dispatch_cols:
            # Preserve only numeric quota deltas; token/plan units are unknown otherwise.
            quota = {r[0]: r[1] if type(r[1]) in (int, float) else None
                     for r in con.execute("SELECT id, quota_delta FROM dispatches")}
        final = []
        for e in episodes:
            original = grouped[(e["run_id"], e["task_id"], e["route"])]
            ids = e["dispatch_ids"]
            money = [d.get("money_actual") for d in original]
            times = [d.get("wall_seconds") for d in original]
            quota_values = [quota.get(i) for i in ids]
            raw_size = e.get("size_class")
            row = {
                "run_id": e["run_id"], "task_id": e["task_id"], "dispatch_ids": ids,
                "ended_at": e["ended_at"], "route": aliases.get(e["route"]),
                "recorded_route": e["route"], "catalog_matched": e["route"] in aliases,
                "size_class": raw_size if raw_size in ("XS", "S", "M", "L", "XL") else "unknown",
                "domain": domain_of(e), "role": e["role"], "effort": e["route"].rsplit("@", 1)[-1],
                "first_pass_accepted": bool(e["success"] and e["attempts"] == 1 and e["review_rounds"] == 0),
                "eventual_accepted": bool(e["success"]),
                "recheck_count": e["review_rounds"], "attempts": e["attempts"],
                "attribution": e["attribution"], "attribution_confidence": e["attribution_confidence"],
                "learn_weight": e["learn_weight"],
                "operational_failure": bool(not e["success"] and e["attribution"] in ("environment", "plan", "reviewer")),
                "model_quality_failure": bool(not e["success"] and e["attribution"] == "route"),
                "time_seconds": sum(times) if times and all(x is not None for x in times) else None,
                "money_actual": sum(money) if money and all(x is not None for x in money) else None,
                "quota_burn": sum(quota_values) if quota_values and all(type(x) in (int, float) for x in quota_values) else None,
                "regression_observed": None, "independent_review_passed": None,
                "missing_reasons": {
                    "regression_observed": "no independently attributed regression label in learner episodes",
                    "independent_review_passed": "review rounds alone do not prove independent reviewer identity",
                },
            }
            for key in ("time_seconds", "money_actual", "quota_burn"):
                if row[key] is None:
                    row["missing_reasons"][key] = "not fully recorded for this episode"
            final.append(row)
    finally:
        con.close()
    end = digest(db_path)
    if initial != end:
        raise ValueError("runs.db changed during extraction; use a consistent SQLite backup/snapshot")
    return final, {"source": str(db_path), "sha256_before": initial, "sha256_after": end,
                   "episodes": len(final), "dispatches_derived": len(outcomes),
                   "matched": sum(bool(x["catalog_matched"]) for x in final),
                   "cost_complete": sum(x["money_actual"] is not None for x in final),
                   "regression_verified": 0, "independent_review_verified": 0}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True, help="consistent copy of local Office runs.db")
    p.add_argument("--out", type=Path, required=True, help="PRIVATE outcome JSONL, do not commit")
    args = p.parse_args()
    rows, receipt = extract(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        os.chmod(args.out, 0o600)
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    print("Private run/task/dispatch IDs written to", args.out, "(do not commit/upload)")

if __name__ == "__main__":
    main()
