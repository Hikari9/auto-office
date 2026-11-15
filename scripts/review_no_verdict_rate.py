#!/usr/bin/env python3
"""review_no_verdict_rate.py -- read-only share of done review gates that ended with no verdict.

Reads one runs.db and, for a time window on `gates.finished_at`, counts the done review gates (kinds
`code_review`, `visual` and `convergence_review`) and how many of them carry no verdict, split by why:

  revoked      the orchestrator stopped the reviewer (REVOKED). Not a reviewer failure.
  launch       every route failed to launch or hit a quota or auth wall (UNAVAILABLE and one of the gate's reviewer
               dispatches recorded a `launch`, `wall` or `pointer` failure)
  invalid      the reviewer answered with an unreadable reply (INVALID_RESULT, or ATTENTION on a v3.1 run)
  other        UNAVAILABLE with no recorded launch failure

A gate counts once, under the first matching reason in that order. EVIDENCE_BLOCKED is an answer with no verdict
because evidence was missing; it is reported as `evidence_blocked` and is not in the no-verdict share.

The file is opened through a SQLite URI with mode=ro and PRAGMA query_only=ON; it is never created or written.
Nothing free-text, id or path is read into the output: only counts and the window.

  scripts/review_no_verdict_rate.py --db runs.db [--since 2026-11-01] [--until 2026-11-15] [--format text|json]

`--since` is inclusive and `--until` exclusive, both ISO dates or timestamps compared as text against
`finished_at`. Exit codes: 0 report printed, 2 the file could not be read as a runs.db.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

REVIEW_KINDS = ("code_review", "visual", "convergence_review")
LAUNCH_CLASSES = ("launch", "wall", "pointer")
REASONS = ("revoked", "launch", "invalid", "other")


def _has(value: str) -> str:
    """SQL: any of the gate's verdict columns equals `value`. Never NULL, so `NOT` over it is safe."""
    return f"('{value}' IN (COALESCE(g.verdict,''), COALESCE(g.review_status,''), COALESCE(g.evidence_status,'')))"


# Every literal below is a constant of this file, never caller input.
_REVOKED = _has("REVOKED")
_INVALID = f"({_has('INVALID_RESULT')} OR {_has('ATTENTION')})"
_UNAVAILABLE = _has("UNAVAILABLE")
_EVIDENCE_BLOCKED = _has("EVIDENCE_BLOCKED")
_LAUNCH_FAILED = (
    "EXISTS (SELECT 1 FROM dispatches d WHERE d.gate_id=g.id AND d.kind='reviewer' "
    "AND json_valid(d.route_json) AND json_extract(d.route_json,'$.review_attempt.failure.class') IN ("
    + ",".join(f"'{c}'" for c in LAUNCH_CLASSES) + "))"
)


def connect_read_only(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file://{quote(os.path.abspath(path))}?mode=ro", uri=True, timeout=30)
    con.execute("PRAGMA query_only=ON")
    return con


def measure(con: sqlite3.Connection, since: str | None = None, until: str | None = None) -> dict:
    where = ["g.status='done'", "g.kind IN (" + ",".join(f"'{k}'" for k in REVIEW_KINDS) + ")"]
    args: list[str] = []
    if since:
        where.append("g.finished_at >= ?")
        args.append(since)
    if until:
        where.append("g.finished_at < ?")
        args.append(until)
    sql = (
        "SELECT COUNT(*), "
        f"COALESCE(SUM({_REVOKED}), 0), "
        f"COALESCE(SUM(NOT {_REVOKED} AND {_UNAVAILABLE} AND {_LAUNCH_FAILED}), 0), "
        f"COALESCE(SUM(NOT {_REVOKED} AND {_INVALID} AND NOT ({_UNAVAILABLE} AND {_LAUNCH_FAILED})), 0), "
        f"COALESCE(SUM({_UNAVAILABLE} AND NOT {_REVOKED} AND NOT {_INVALID} AND NOT {_LAUNCH_FAILED}), 0), "
        f"COALESCE(SUM({_EVIDENCE_BLOCKED} AND NOT {_REVOKED} AND NOT {_INVALID} AND NOT {_UNAVAILABLE}), 0) "
        "FROM gates g WHERE " + " AND ".join(where)
    )
    done, revoked, launch, invalid, other, blocked = con.execute(sql, args).fetchone()
    no_verdict = revoked + launch + invalid + other
    return {
        "since": since, "until": until, "done": done, "no_verdict": no_verdict,
        "share": round(no_verdict / done, 4) if done else None,
        "by_reason": {"revoked": revoked, "launch": launch, "invalid": invalid, "other": other},
        "evidence_blocked": blocked,
    }


def render_text(m: dict) -> str:
    share = "n/a (no done review gates)" if m["share"] is None else f"{m['share'] * 100:.1f}%"
    lines = [f"window: {m['since'] or 'start'} .. {m['until'] or 'now'}",
             f"done review gates: {m['done']}", f"no verdict: {m['no_verdict']} ({share})"]
    lines += [f"  {r}: {m['by_reason'][r]}" for r in REASONS]
    lines.append(f"evidence blocked (not in the share): {m['evidence_blocked']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, type=Path, help="runs.db (or a copy) to read; it is never written")
    ap.add_argument("--since", help="inclusive lower bound on gates.finished_at (ISO date or timestamp)")
    ap.add_argument("--until", help="exclusive upper bound on gates.finished_at")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ns = ap.parse_args(argv)
    try:
        report = measure(connect_read_only(ns.db), ns.since, ns.until)
    except sqlite3.Error as exc:
        print(f"cannot read {ns.db} as a runs.db: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2) if ns.format == "json" else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
