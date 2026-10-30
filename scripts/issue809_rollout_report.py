#!/usr/bin/env python3
"""Read-only accepted-outcome observations for XS-XL rollout (#809).

Usage: python scripts/issue809_rollout_report.py --runs-db /path/to/copy/runs.db

Reports cost per accepted task, end-to-end latency, first-pass acceptance, rework,
undersizing and review/permission/landing gate-violation signals from recorded runs.db
data. It executes no routes, changes no data, and does not certify release; routes that
did not run are not measured.
Source is an operator-provided COPY of the existing runs.db.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from office import gear_rollout, route_learning  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-db", required=True, type=Path,
                        help="Read-only COPY of Office's runs.db, never production live DB")
    args = parser.parse_args()
    path = args.runs_db.expanduser().resolve()
    if not path.is_file():
        parser.error("runs.db COPY does not exist; no empty success report will be invented")
    uri = path.as_uri() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        try:
            columns = {r[1] for r in con.execute("PRAGMA table_info(runs)").fetchall()}
            if "gear" not in columns:
                parser.error("historical database does not record run gear; cannot identify XS-XL cohort")
            eligible = {r[0] for r in con.execute(
                "SELECT id FROM runs WHERE gear IN ('XS','S','M','L','XL')")}
            outcomes = [o for o in route_learning.derive_outcomes(con) if o["run_id"] in eligible]
            evidence = gear_rollout.collect_run_evidence(con, eligible)
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        parser.error(f"cannot read the database COPY: {exc}")
    result = gear_rollout.summarize(outcomes, evidence)
    result["new_gear_runs_in_copy"] = len(eligible)
    result["run_cohort"] = "new XS-XL only"
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
