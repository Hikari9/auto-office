"""#502: matched baseline-vs-treatment evidence, without synthetic wins.

Input JSON is an array of records with pair_id, arm (baseline/treatment),
task_shape, harness, billing_mode, verified_completion, integrated_defects,
metrics (measured scalars), and complete_metrics (names verified complete).
A metric comparison is withheld if even one side lacks complete evidence.
This tool does not execute runs or enable cost collection.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

METRICS = ("total_tokens", "wall_seconds", "first_dispatch_seconds", "admin_turns", "quota_units", "actual_usd")
MIN_MATCHED_FOR_CLAIM = 3


def compare(rows: list[dict]) -> dict:
    pairs = defaultdict(dict)
    duplicates = []
    for row in rows:
        key, arm = row.get("pair_id"), row.get("arm")
        if not isinstance(key, str) or not key or arm not in ("baseline", "treatment"):
            continue
        if arm in pairs[key]:
            duplicates.append(key)
        pairs[key][arm] = row
    accepted, excluded = [], []
    if duplicates:
        return {"error": "duplicate pair/arm entries", "pairs": sorted(set(duplicates))}
    for key, p in sorted(pairs.items()):
        a, b = p.get("baseline"), p.get("treatment")
        if not a or not b:
            excluded.append({"pair_id": key, "reason": "missing arm"})
            continue
        if any(not a.get(k) or a.get(k) != b.get(k) for k in ("task_shape", "harness", "billing_mode")):
            excluded.append({"pair_id": key, "reason": "task, harness or billing mode not matched"})
            continue
        if a.get("verified_completion") is not True or b.get("verified_completion") is not True:
            excluded.append({"pair_id": key, "reason": "completion not independently verified on both arms"})
            continue
        defects = [a.get("integrated_defects"), b.get("integrated_defects")]
        if not all(type(x) is int and x >= 0 for x in defects):
            excluded.append({"pair_id": key, "reason": "integrated defect evidence missing"})
            continue
        accepted.append((key, a, b))
    metrics = {}
    for k in METRICS:
        values = []
        for key, a, b in accepted:
            if k not in (a.get("complete_metrics") or []) or k not in (b.get("complete_metrics") or []):
                continue
            if k == "actual_usd" and (a.get("billing_mode") != "api" or
                                      a.get("cost_kind") != "actual" or b.get("cost_kind") != "actual"):
                continue
            before, after = (a.get("metrics") or {}).get(k), (b.get("metrics") or {}).get(k)
            if not all(type(x) in (int, float) and math.isfinite(x) and x >= 0 for x in (before, after)):
                continue
            values.append((key, before, after))
        before_sum, after_sum = sum(t[1] for t in values), sum(t[2] for t in values)
        supported = len(values) >= MIN_MATCHED_FOR_CLAIM and before_sum > 0
        metrics[k] = {"matched": len(values), "before": before_sum if values else None,
                      "after": after_sum if values else None,
                      "change_pct": ((after_sum - before_sum) / before_sum * 100 if supported else None),
                      "claim_ready": supported and after_sum < before_sum and all(b["integrated_defects"] <= a["integrated_defects"]
                                                       for _, a, b in accepted),
                      "pairs": [t[0] for t in values]}
    return {"paired": len(accepted), "excluded": excluded,
            "quality": {"baseline_defects": sum(a["integrated_defects"] for _, a, _ in accepted),
                        "treatment_defects": sum(b["integrated_defects"] for _, _, b in accepted)},
            "metrics": metrics, "rule": "no economic win without verified quality and >=3 matched, complete pairs"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path, help="JSON array of baseline/treatment records")
    args = p.parse_args(argv)
    result = compare(json.loads(args.input.read_text(encoding="utf-8")))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
