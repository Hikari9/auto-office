#!/usr/bin/env python3
"""Read-only extraction of actual Office executor attempts for #862.

Reads a consistent local copy of runs.db (never the live file, never
office.db.connect, which migrates schemas) and writes PRIVATE JSONL with run,
task and dispatch IDs. Never commit or upload that output.

Unit: an attempt is one consecutive route segment of a task's executor
dispatches, the same unit as rico-mode's executor scoreboard. Labels:

- eventual_accepted: the task's accepted revision was produced by this segment.
- recheck_count: completed convergence_review RECHECK gates naming this task
  (directly or as a shared-gate member), charged to the segment live when the
  gate was created. The v3.1 code_review/CHANGES_REQUIRED verdict is counted
  too for older runs. route_learning.derive_outcomes counts only the latter,
  so it reports zero RECHECKs on every convergence-v1 run.
- first_pass_accepted: eventual_accepted with zero RECHECKs.
- operational_failure: replaced or abandoned before any RECHECK (no submission,
  stall, usage limit, worker exit). Zero weight for model-quality learning.
- size_class: the task-declared size only (dispatch.size_class or the task
  descriptor's task_size). The run-level risk size is not a task size and is
  never substituted; route_learning does substitute it.

Cost, quota, regression and reviewer facts stay null unless a source records
them. runs.db records no money or quota (all dispatches.money_*/quota_delta are
NULL in every snapshot examined), so estimated list-price cost is joined only
from an explicitly supplied private attempt-cost file.
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
from office import route_learning  # noqa: E402

SIZES = ("XS", "S", "M", "L", "XL")
DOMAINS = ("frontend", "backend", "infra", "docs", "tests", "mixed")
RECHECK = {("convergence_review", "RECHECK"), ("code_review", "CHANGES_REQUIRED")}
APPROVE = {("convergence_review", "APPROVED"), ("code_review", "APPROVED")}
NEED_TABLES = {"runs", "tasks", "dispatches", "revisions", "gates"}


def canonical_routes(path: Path | None = None) -> dict[str, str]:
    """Recorded route -> exact catalog route key for the same invocation.

    An exact catalog route_key always resolves to itself. A legacy alias row
    (claude/opus@medium) resolves to the exact key of its pinned invocation
    model (claude/claude-opus-5-5@medium) only when that exact row exists.
    A dispatch recorded with the harness's own invocation spelling
    (agy/gemini-3.8-flash-medium@medium, agy/Claude Sonnet 5.5 (High)@high)
    resolves to the one route whose invocation it is; an ambiguous spelling
    does not resolve. A floating alias whose invocation model differs from
    every exact row (claude/sonnet@high -> claude-sonnet-5) never lands on
    another model's row, so a sibling model or effort is never substituted.
    """
    with (path or ROOT / "catalog" / "routes.tsv").open(newline="") as fh:
        cat = list(csv.DictReader(fh, delimiter="\t"))
    keys = {row["route_key"] for row in cat}
    out = {key: key for key in keys}
    spelled: dict[str, set[str]] = {}
    for row in cat:
        inv = row.get("invocation_model_id")
        if not inv or inv == "\\N" or inv == row.get("model_id"):
            continue  # not an alias spelling
        pinned = f'{row["invocation_harness"]}/{inv}@{row["effort"]}'
        if pinned in keys:
            out[row["route_key"]] = pinned
        else:
            spelled.setdefault(pinned, set()).add(row["route_key"])
    for spelling, targets in spelled.items():
        if len(targets) == 1:
            out[spelling] = out[next(iter(targets))]
    return out


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _json(raw, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def domain_of(*descriptors: dict) -> str:
    for d in descriptors:
        for key in ("evidence_domain", "domain", "task_domain"):
            v = (d or {}).get(key)
            if isinstance(v, str) and v in DOMAINS:
                return v
    return "unknown"


def _family(model: str | None) -> str:
    m = (model or "").split("/")[-1].lower()
    for fam in ("haiku", "sonnet", "opus", "fable", "luna", "terra", "sol", "astra",
                "flash", "hydrogen", "argon", "glm", "deepseek", "mimo", "minimax", "muse"):
        if fam in m:
            return fam
    return m


def _route(harness, model, effort) -> str:
    return route_learning.route_key(harness, model, effort)


def load_attempt_costs(path: Path | None) -> dict[tuple, dict]:
    """Private per-attempt list-price estimates, keyed (run, task, position).

    Accepts the token-economy report's tasks-cost.json shape: a list of
    {run, task, atts: [{pos, covered, usd, revUsd, tokTot, usageLimit}]}.
    """
    if path is None:
        return {}
    data = json.loads(path.read_text())
    out = {}
    for task in data:
        for att in task.get("atts", []):
            out[(task["run"], task["task"], att["pos"])] = att
    return out


def _cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def extract(db_path: Path, *, catalog: Path | None = None, attempt_costs: Path | None = None):
    db_path = db_path.expanduser().resolve(strict=True)
    initial = digest(db_path)
    uri = "file:" + quote(str(db_path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if missing := (NEED_TABLES - tables):
            raise ValueError(f"required outcome tables unavailable: {sorted(missing)}")
        integrity = con.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"database check failed: {integrity}")
        rows, stats = _segments(con, canonical_routes(catalog), load_attempt_costs(attempt_costs))
    finally:
        con.close()
    end = digest(db_path)
    if initial != end:
        raise ValueError("runs.db changed during extraction; use a consistent SQLite backup/snapshot")
    receipt = {"source_sha256_before": initial, "source_sha256_after": end, **stats,
               "attempts": len(rows), "tasks": len({(r["run_id"], r["task_id"]) for r in rows}),
               "catalog_matched": sum(r["route"] is not None for r in rows),
               "estimated_cost_joined": sum(r["estimated_cost_usd"] is not None for r in rows),
               "money_actual_recorded": sum(r["money_actual"] is not None for r in rows),
               "quota_delta_recorded": sum(r["quota_burn"] is not None for r in rows),
               "regression_verified": 0,
               "independent_review_known": sum(r["independent_review_passed"] is not None for r in rows)}
    return rows, receipt


def _segments(con, aliases, costs):
    dcols = _cols(con, "dispatches")
    opt = lambda c: c if c in dcols else f"NULL AS {c}"  # noqa: E731 - older schemas
    disp = con.execute(
        "SELECT id, run_id, task_id, role, harness, model, effort, started_at, ended_at, "
        f"{opt('size_class')}, {opt('descriptor_json')}, {opt('money_actual')}, {opt('quota_delta')}, "
        f"{opt('wall_clock_seconds')} FROM dispatches "
        "WHERE role IN ('executor','worker') AND task_id IS NOT NULL AND started_at IS NOT NULL "
        "ORDER BY started_at, id").fetchall()
    by_id = {r[0]: r for r in con.execute(
        f"SELECT id, harness, model, effort FROM dispatches")}
    runs = {r[0]: r[1] for r in con.execute("SELECT id, phase FROM runs")}
    tcols = _cols(con, "tasks")
    tdesc = "descriptor_json" if "descriptor_json" in tcols else "NULL"
    tasks = {(r[0], r[1]): {"status": r[2], "accepted": r[3], "descriptor": _json(r[4], {})}
             for r in con.execute(f"SELECT run_id, id, status, accepted_revision_id, {tdesc} FROM tasks")}
    rev_dispatch = {r[0]: r[1] for r in con.execute("SELECT id, dispatch_id FROM revisions")}
    gcols = _cols(con, "gates")
    g_opt = lambda c: c if c in gcols else f"NULL AS {c}"  # noqa: E731
    verdict = "COALESCE(verdict, review_status)" if "review_status" in gcols else "verdict"
    gates = con.execute(
        f"SELECT run_id, task_id, revision_id, kind, {verdict}, {g_opt('status')}, {g_opt('members_json')}, "
        f"{g_opt('created_at')}, {g_opt('finished_at')}, {g_opt('independence')}, {g_opt('reviewer_dispatch_id')} "
        "FROM gates WHERE kind IN ('convergence_review','code_review')").fetchall()
    labels = {}
    if "outcome_labels" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        labels = {r[0]: r[1] for r in con.execute("SELECT dispatch_id, label FROM outcome_labels")}

    # Route segments per task, in start order.
    segs: dict[tuple, list[dict]] = {}
    for (did, run, task, role, harness, model, effort, started, ended, dsize, ddesc,
         money, quota, wall) in disp:
        route = _route(harness, model, effort)
        lst = segs.setdefault((run, task), [])
        if not lst or lst[-1]["recorded_route"] != route:
            lst.append({"recorded_route": route, "role": role, "model": model, "dispatches": [],
                        "started_at": started, "ended_at": None, "sizes": set(), "descriptors": [],
                        "money": [], "quota": [], "wall": [], "rechecks": 0, "gate_times": [],
                        "approvals": []})
        s = lst[-1]
        s["dispatches"].append(did)
        s["ended_at"] = max(filter(None, [s["ended_at"], ended])) if (s["ended_at"] or ended) else None
        if dsize:
            s["sizes"].add(dsize)
        s["descriptors"].append(_json(ddesc, {}))
        s["money"].append(money if isinstance(money, (int, float)) else None)
        s["quota"].append(quota if isinstance(quota, (int, float)) else None)
        s["wall"].append(wall if isinstance(wall, (int, float)) else None)

    # Charge each completed review gate to the segment live when it was created.
    unassigned = 0
    for run, gtask, rev, kind, v, status, members, created, finished, indep, reviewer in gates:
        if status not in (None, "done"):
            continue
        names = {gtask} | set(_json(members, []))
        for task in names:
            lst = segs.get((run, task))
            if not lst:
                continue
            producer = rev_dispatch.get(rev) if rev and task == gtask else None
            target = next((s for s in lst if producer in s["dispatches"]), None) if producer else None
            if target is None:
                live = [s for s in lst if (s["started_at"] or "") <= (created or "")]
                target = live[-1] if live else None
            if target is None:
                unassigned += 1
                continue
            if (kind, v) in RECHECK:
                target["rechecks"] += 1
            if (kind, v) in APPROVE:
                target["approvals"].append((indep, reviewer))
            target["gate_times"].append(finished or created)

    out = []
    in_flight = 0
    for (run, task), lst in segs.items():
        t = tasks.get((run, task)) or {"status": None, "accepted": None, "descriptor": {}}
        accepted_by = rev_dispatch.get(t.get("accepted")) if t.get("accepted") else None
        task_accepted = t.get("status") == "accepted"
        settled_task = task_accepted or t.get("status") == "cancelled" or runs.get(run) in ("closed", "abandoned")
        for pos, s in enumerate(lst):
            last = pos == len(lst) - 1
            landed = bool(accepted_by and accepted_by in s["dispatches"])
            if last and not landed and not settled_task:
                in_flight += 1
                continue
            outcome = "accepted" if landed else ("rerouted" if not last else "abandoned")
            cancelled = t.get("status") == "cancelled"
            operational = not landed and s["rechecks"] == 0 and not cancelled
            quality = not landed and s["rechecks"] > 0 and not cancelled
            attribution = "route" if landed or quality else ("plan" if cancelled else "operational")
            sizes = s["sizes"] | ({t["descriptor"].get("task_size")} if t["descriptor"].get("task_size") else set())
            size = next(iter(sizes)) if len(sizes) == 1 and next(iter(sizes)) in SIZES else "unknown"
            size_source = ("task-declared" if size != "unknown"
                           else "conflicting-task-sizes" if len(sizes) > 1 else "none-recorded")
            label_at = max(filter(None, [s["ended_at"], *s["gate_times"]]), default=None)
            route = aliases.get(s["recorded_route"])
            cost = costs.get((run, task, pos))
            covered = bool(cost and cost.get("covered"))
            approvals = s["approvals"]
            independent = None
            if landed and approvals:
                fams = {_family(by_id.get(rv, (None, None, None, None))[2]) for _, rv in approvals if rv}
                independent = any(ind == "independent" for ind, _ in approvals) and \
                    bool(fams) and _family(s["model"]) not in fams
            post = {labels.get(d) for d in s["dispatches"]} - {None}
            row = {
                "run_id": run, "task_id": task, "dispatch_ids": s["dispatches"], "segment_index": pos,
                "first_route": pos == 0, "started_at": s["started_at"], "ended_at": s["ended_at"],
                "label_at": label_at, "route": route, "recorded_route": s["recorded_route"],
                "catalog_matched": route is not None, "size_class": size, "size_source": size_source,
                "domain": domain_of(t["descriptor"], *s["descriptors"]),
                "role": s["role"], "effort": s["recorded_route"].rsplit("@", 1)[-1],
                "outcome": outcome, "first_pass_accepted": landed and s["rechecks"] == 0,
                "eventual_accepted": landed, "task_accepted": task_accepted,
                "recheck_count": s["rechecks"], "attempts": len(s["dispatches"]),
                "operational_failure": operational, "model_quality_failure": quality,
                "attribution": attribution, "learn_weight": 1.0 if attribution == "route" else 0.0,
                "time_seconds": sum(s["wall"]) if all(w is not None for w in s["wall"]) else None,
                "money_actual": sum(s["money"]) if all(m is not None for m in s["money"]) else None,
                "quota_burn": sum(s["quota"]) if all(q is not None for q in s["quota"]) else None,
                "estimated_cost_usd": round(cost["usd"] + cost.get("revUsd", 0), 6) if covered else None,
                "estimated_executor_cost_usd": round(cost["usd"], 6) if covered else None,
                "estimated_review_cost_usd": round(cost.get("revUsd", 0), 6) if covered else None,
                "cost_basis": "list-price token estimate (private attempt-cost file)" if covered else None,
                "usage_limit_hit": bool(cost.get("usageLimit")) if cost else None,
                "regression_observed": None, "post_land_labels": sorted(post),
                "independent_review_passed": independent,
                "missing_reasons": {},
            }
            mr = row["missing_reasons"]
            mr["regression_observed"] = ("no regression-positive label exists in outcome_labels; "
                                         "'verified_no_observed_failure' cannot observe a regression")
            if row["money_actual"] is None:
                mr["money_actual"] = "dispatches.money_actual not recorded"
            if row["quota_burn"] is None:
                mr["quota_burn"] = "dispatches.quota_delta not recorded"
            if row["estimated_cost_usd"] is None:
                mr["estimated_cost_usd"] = "no fully covered token record joined for this attempt"
            if row["time_seconds"] is None:
                mr["time_seconds"] = "a dispatch lacks wall_clock_seconds"
            if independent is None:
                mr["independent_review_passed"] = ("not accepted" if not landed else
                                                   "no approving review gate with a recorded reviewer")
            out.append(row)
    out.sort(key=lambda r: (r["label_at"] or "", r["run_id"], r["task_id"], r["segment_index"]))
    return out, {"in_flight_excluded": in_flight, "review_gates_unassigned": unassigned}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, required=True, help="consistent copy of local Office runs.db")
    p.add_argument("--out", type=Path, required=True, help="PRIVATE attempt JSONL, do not commit")
    p.add_argument("--attempt-costs", type=Path, help="PRIVATE per-attempt list-price estimates (tasks-cost.json)")
    args = p.parse_args()
    rows, receipt = extract(args.db, attempt_costs=args.attempt_costs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    print("Private run/task/dispatch IDs written to", args.out, "(do not commit/upload)")


if __name__ == "__main__":
    main()
