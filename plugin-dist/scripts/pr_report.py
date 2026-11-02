#!/usr/bin/env python3
"""pr_report.py -- deterministic before/after PR report (amendment v4).

Emits one compact row per PR version (after the first), each carrying its raw
metrics plus a delta against the immediately previous version and a delta
against a fixed baseline version. Every field is a number computed from the
input; there is no agent-generated narrative anywhere in the output.

Determinism is the receipt (docs/plans/v3-final-merge.md amendment v4): given
the same `--input` file, two runs of this script must produce byte-identical
stdout. That rules out timestamps, random identifiers, locale-dependent
formatting and unordered dict iteration in the output -- this script has none
of those; output keys are sorted and floats are rounded to a fixed precision.

Input shape (JSON):
    {
      "baseline_version": "<version id, optional -- defaults to the first
                            entry's version>",
      "versions": [
        {
          "version": "<id>",
          "pr": <number or string, optional>,
          "shipped_size_bytes": <number, optional>,
          "estimated_loaded_tokens": <number, optional>,
          "actual_loaded_tokens": <number, optional -- "where available">,
          "eval_score": <number, optional>,
          "reward": <number, optional>
        },
        ...
      ]
    }

`versions` order is the "immediately previous version" ordering; the first
entry has no previous version and is therefore not emitted as a row (there is
nothing to compare it against). A metric missing on either side of a
comparison yields a `null` delta rather than a fabricated number.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

METRICS = (
    "shipped_size_bytes",
    "estimated_loaded_tokens",
    "actual_loaded_tokens",
    "eval_score",
    "reward",
)

ROUND_DIGITS = 6


def _round(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return None if value is None else value
    if isinstance(value, (int, float)):
        rounded = round(float(value), ROUND_DIGITS)
        # Represent whole numbers without a trailing .0 so integer inputs
        # (e.g. shipped_size_bytes) stay integers across runs.
        if rounded == int(rounded):
            return int(rounded)
        return rounded
    return value


def _delta(current: Any, other: Any) -> Any:
    if current is None or other is None:
        return None
    if not isinstance(current, (int, float)) or not isinstance(other, (int, float)):
        return None
    return _round(current - other)


def build_report(data: dict) -> list[dict]:
    versions = data.get("versions") or []
    if not versions:
        return []
    baseline_id = data.get("baseline_version") or versions[0].get("version")
    by_version = {v.get("version"): v for v in versions}
    baseline = by_version.get(baseline_id, versions[0])

    rows = []
    for index in range(1, len(versions)):
        current = versions[index]
        previous = versions[index - 1]
        row: dict[str, Any] = {
            "version": current.get("version"),
            "pr": current.get("pr"),
            "previous_version": previous.get("version"),
            "baseline_version": baseline.get("version"),
        }
        for metric in METRICS:
            current_value = _round(current.get(metric))
            row[metric] = {
                "value": current_value,
                "delta_vs_previous": _delta(current_value, _round(previous.get(metric))),
                "delta_vs_baseline": _delta(current_value, _round(baseline.get(metric))),
            }
        rows.append(row)
    return rows


def _canonical_dump(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="path to the PR-version JSON input")
    parser.add_argument("--baseline-version", help="override the input's baseline_version")
    parser.add_argument("--out", help="write the report to this path instead of stdout")
    args = parser.parse_args(argv)

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if args.baseline_version:
        data = dict(data)
        data["baseline_version"] = args.baseline_version

    rows = build_report(data)
    output = _canonical_dump(rows)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
