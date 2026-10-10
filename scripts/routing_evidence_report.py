#!/usr/bin/env python3
"""routing_evidence_report.py -- offline, read-only, aggregate-only routing evidence report (#528).

Reads one runs.db (a copy, or the live file) and prints counts that say how executor routes behaved:
dispatch counts per role x harness x model x effort, raw strict dispatch successes beside accepted task-route
episodes, first executor vs accepted producer, same-route retries and cross-route handoffs, revisions to accept,
self-review receipts, lane exposure vs unique and unassigned findings, latency and cost where recorded, task size
vs run size, and planner tag coverage.

  * Opened only through a SQLite URI with mode=ro, then PRAGMA query_only=ON. `office.db.connect` is never used:
    it migrates and enters WAL mode. The file is hashed (SHA-256) before the first query and again after the last;
    both are printed. Opening a WAL database read-only can create empty -wal/-shm files beside it; the main file's
    bytes are not changed.
  * Aggregate only: no goal, title, prompt, path, summary, commit, token, run id, task id or dispatch id is read
    into the report. Free-text columns are never selected; categorical values are clamped to their known sets.
  * An absent column reads as NULL (unknown), an absent table makes its section "unavailable". Nothing historical is
    backfilled: a NULL stays unknown.
  * Every metric is one SQL statement over the normalized views in `prelude()`; docs/routing-evidence-report.md
    lists each statement and its denominator, and a test keeps the two identical.

  scripts/routing_evidence_report.py --db /tmp/runs-copy.db [--format text|json]

Exit codes: 0 report printed, 1 report printed but integrity_check failed, 2 the file could not be read as a database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

try:
    from office import route_learning
except ImportError:  # the episodes section then reports itself unavailable
    route_learning = None

REPORT_VERSION = 1

CAVEATS = (
    "sha256 and size_bytes cover the main database file only. Committed frames still in a -wal file are read by the "
    "report but are not covered by the hash: copy the .db, -wal and -shm files together, or checkpoint first, for a "
    "point-in-time snapshot.",
    "The report cannot tell whether this file is the live runs.db or a copy, when it was taken, or what was pruned "
    "before it: population.runs.pruned counts runs Office itself marked pruned, nothing else.",
    "A NULL column is unknown, never a default: runs recorded before an evidence column existed stay unknown.",
)

# Evidence-only planner tags (#415) and the values each may take. Anything else is reported as `other`.
TAGS = {
    "evidence_domain": ("frontend", "backend", "infra", "docs", "tests"),
    "intent": ("feature", "fix", "refactor", "test-pruning", "migration"),
    "difficulty_estimate": ("low", "medium", "high", "very-high", "unknown"),
    "brief_shape": ("deliverables-enumerated", "checks-only", "unknown"),
}
SIZES = ("S", "M", "L", "XL")
FINDING_BASES = ("reviewer-declared", "unique-path", "unassigned")
SELF_REVIEW_KINDS = ("ledger", "exempt", "missing", "unreadable")
EXEMPT_TYPES = ("read-only", "empty", "trivial", "mechanical")
ATTRIBUTIONS = ("route", "mixed", "unknown", "plan", "environment", "reviewer")

# normalized view -> (source table, canonical columns). A column the source lacks reads as NULL; a table it
# lacks reads as no rows, and any section that needs it says so instead of showing zeros.
VIEWS = {
    "dispatches": ("id", "run_id", "role", "task_id", "started_at", "ended_at", "wall_clock_seconds", "money_actual",
                   "size_class", "predecessor_dispatch_id", "descriptor_json"),
    "tasks": ("run_id", "id", "status", "accepted_revision_id", "descriptor_json", "first_executor_dispatch_id"),
    "revisions": ("id", "run_id", "task_id", "seq", "dispatch_id", "self_review_json"),
    "gates": ("id", "run_id", "subject", "scope", "members_json"),
    "findings": ("id", "run_id", "gate_id", "code", "contract", "attribution_basis", "attributed_task", "clone_of"),
    "runs": ("id", "created_at", "phase", "risk_json", "pruned_at"),
}
# Columns read only through an expression (the dispatch route parts come from `triple` when the columns are empty).
EXTRA_COLUMNS = {"dispatches": ("triple", "harness", "model", "effort")}


def _has(schema: dict, table: str, column: str) -> bool:
    return column in schema.get(table, ())


def _col(schema: dict, table: str, alias: str, column: str) -> str:
    return f"{alias}.{column}" if _has(schema, table, column) else "NULL"


def _proj(schema: dict, table: str, alias: str, column: str, rename: str | None = None) -> str:
    name = rename or column
    if _has(schema, table, column):
        return f"{alias}.{column}" if name == column else f"{alias}.{column} AS {name}"
    return f"NULL AS {name}"


def _route_part(schema: dict, part: str) -> str:
    """harness / model / effort read from a `harness@version/model@effort` triple, as route_learning splits it."""
    t = "d.triple" if _has(schema, "dispatches", "triple") else "NULL"
    last_at = f"length(rtrim({t}, replace({t}, '@', '')))"
    slash = f"instr({t}, '/')"
    if part == "harness":
        return (f"CASE WHEN {slash} > 0 THEN CASE WHEN instr(substr({t}, 1, {slash} - 1), '@') > 0 "
                f"THEN substr({t}, 1, instr({t}, '@') - 1) ELSE substr({t}, 1, {slash} - 1) END END")
    if part == "model":
        return f"CASE WHEN {slash} > 0 AND {last_at} > {slash} THEN substr({t}, {slash} + 1, {last_at} - {slash} - 1) END"
    return f"CASE WHEN {slash} > 0 AND {last_at} > {slash} THEN NULLIF(substr({t}, {last_at} + 1), '') END"


def prelude(schema: dict) -> str:
    """The normalized views every metric reads (WITH ... , no trailing comma). With a full schema this is the text
    printed in docs/routing-evidence-report.md; an absent column becomes NULL and an absent table an empty view."""
    def empty(columns: tuple[str, ...]) -> str:
        return "SELECT " + ", ".join(f"NULL AS {c}" for c in columns) + " WHERE 0"

    if "dispatches" in schema:
        route_cols = ", ".join((
            f"COALESCE(NULLIF({_col(schema, 'dispatches', 'd', 'harness')}, ''), {_route_part(schema, 'harness')}) AS harness",
            f"COALESCE(NULLIF({_col(schema, 'dispatches', 'd', 'model')}, ''), {_route_part(schema, 'model')}) AS model",
            f"COALESCE(NULLIF({_col(schema, 'dispatches', 'd', 'effort')}, ''), {_route_part(schema, 'effort')}) AS effort"))
        inner = ("SELECT " + ", ".join(_proj(schema, "dispatches", "d", c) for c in VIEWS["dispatches"])
                 + f", {route_cols} FROM dispatches d")
    else:
        inner = empty((*VIEWS["dispatches"], "harness", "model", "effort"))
    dr = ("dr AS (SELECT p.*, CASE WHEN p.harness IS NOT NULL AND p.model IS NOT NULL THEN "
          f"p.harness || '/' || p.model || '@' || COALESCE(p.effort, '-') END AS route FROM ({inner}) p)")

    def plain(table: str, alias: str, name: str, renames: dict | None = None, extra: str = "") -> str:
        renames = renames or {}
        if table not in schema:
            cols = tuple(renames.get(c, c) for c in VIEWS[table])
            return f"{name} AS ({empty(cols + (('is_accepted',) if extra else ()))})"
        sel = ", ".join(_proj(schema, table, alias, c, renames.get(c)) for c in VIEWS[table])
        return f"{name} AS (SELECT {sel}{extra} FROM {table} {alias})"

    tk = plain("tasks", "t", "tk", {"id": "task_id"},
               ", CASE WHEN t.status = 'accepted' AND t.accepted_revision_id IS NOT NULL THEN 1 ELSE 0 END AS is_accepted")
    tr = ("tr AS (SELECT t.run_id, t.task_id, t.is_accepted, fe.id AS first_id, fe.route AS first_route, "
          "pd.id AS producer_id, pd.route AS producer_route FROM tk t "
          "LEFT JOIN dr fe ON fe.id = t.first_executor_dispatch_id AND fe.run_id = t.run_id "
          "AND fe.task_id = t.task_id AND fe.role = 'executor' "
          "LEFT JOIN rv ar ON ar.id = t.accepted_revision_id AND ar.run_id = t.run_id "
          "LEFT JOIN dr pd ON pd.id = ar.dispatch_id AND pd.run_id = t.run_id AND pd.task_id = t.task_id)")
    ed = ("ed AS (SELECT d.run_id, d.task_id, d.id AS to_id, p.route AS from_route, d.route AS to_route FROM dr d "
          "JOIN dr p ON p.id = d.predecessor_dispatch_id AND p.run_id = d.run_id AND p.task_id = d.task_id "
          "AND p.role = 'executor' WHERE d.role = 'executor')")
    return "WITH " + ",\n".join((
        dr, tk, plain("revisions", "r", "rv"), plain("gates", "g", "gt"), plain("findings", "f", "fd"),
        plain("runs", "r", "rn"), tr, ed))


# A metric is one SQL statement appended to the prelude. A body that starts with "," adds CTEs of its own.
# `needs` lists the source tables a section cannot be read without.
METRICS = {
    "runs.span": {
        "needs": ("runs",),
        "denominator": "every row of runs",
        "sql": "SELECT COUNT(*) AS runs, MIN(substr(created_at, 1, 10)) AS first_day, "
               "MAX(substr(created_at, 1, 10)) AS last_day, COALESCE(SUM(pruned_at IS NOT NULL), 0) AS pruned FROM rn",
    },
    "runs.by_phase": {
        "needs": ("runs",),
        "denominator": "every row of runs",
        "sql": "SELECT COALESCE(phase, 'unknown') AS phase, COUNT(*) AS runs FROM rn GROUP BY 1 ORDER BY 1",
    },
    "size.run": {
        "needs": ("runs",),
        "denominator": "every row of runs; the size is the run's risk size_class",
        "sql": "SELECT COALESCE(CASE WHEN json_valid(risk_json) THEN json_extract(risk_json, '$.size_class') END, "
               "'unknown') AS size, COUNT(*) AS runs FROM rn GROUP BY 1 ORDER BY 1",
    },
    "tasks.by_status": {
        "needs": ("tasks",),
        "denominator": "every row of tasks",
        "sql": "SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS tasks FROM tk GROUP BY 1 ORDER BY 1",
    },
    "size.task": {
        "needs": ("tasks",),
        "denominator": "every row of tasks; the size is the planner's task_size in the task descriptor",
        "sql": "SELECT COALESCE(CASE WHEN json_valid(descriptor_json) THEN json_extract(descriptor_json, '$.task_size') "
               "END, 'unknown') AS size, COUNT(*) AS tasks FROM tk GROUP BY 1 ORDER BY 1",
    },
    "size.dispatch_snapshot": {
        "needs": ("dispatches",),
        "denominator": "every executor dispatch; the size is dispatches.size_class, the task size snapshotted at launch",
        "sql": "SELECT COALESCE(harness, 'unknown') AS harness, COALESCE(model, 'unknown') AS model, "
               "COALESCE(effort, 'unknown') AS effort, COALESCE(size_class, 'unknown') AS size, COUNT(*) AS dispatches "
               "FROM dr WHERE role = 'executor' GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4",
    },
    "dispatch.routes": {
        "needs": ("dispatches",),
        "denominator": "every row of dispatches; terminal = ended_at is set, pending = it is not",
        "sql": "SELECT COALESCE(role, 'unknown') AS role, COALESCE(harness, 'unknown') AS harness, "
               "COALESCE(model, 'unknown') AS model, COALESCE(effort, 'unknown') AS effort, COUNT(*) AS dispatches, "
               "COALESCE(SUM(ended_at IS NOT NULL), 0) AS terminal, COALESCE(SUM(ended_at IS NULL), 0) AS pending "
               "FROM dr GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4",
    },
    "dispatch.strict_successes": {
        "needs": ("dispatches", "tasks", "revisions"),
        "denominator": "executor and worker dispatches; a success is a dispatch whose revision is its task's "
                       "accepted_revision_id (route_learning's success definition, ended or not)",
        "sql": "SELECT d.role AS role, COALESCE(d.harness, 'unknown') AS harness, COALESCE(d.model, 'unknown') AS model, "
               "COALESCE(d.effort, 'unknown') AS effort, COUNT(DISTINCT d.id) AS strict_successes "
               "FROM tk t JOIN rv r ON r.id = t.accepted_revision_id AND r.run_id = t.run_id "
               "JOIN dr d ON d.id = r.dispatch_id AND d.run_id = t.run_id "
               "WHERE d.role IN ('executor', 'worker') GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4",
    },
    "dispatch.latency": {
        "needs": ("dispatches",),
        "denominator": "terminal dispatches (ended_at set); recorded = wall_clock_seconds, else ended_at - started_at, "
                       "when it is not negative; the rest are unknown",
        "sql": ", lat AS (SELECT role, harness, model, effort, "
               "CASE WHEN COALESCE(wall_clock_seconds, (julianday(ended_at) - julianday(started_at)) * 86400.0) >= 0 "
               "THEN COALESCE(wall_clock_seconds, (julianday(ended_at) - julianday(started_at)) * 86400.0) END AS secs "
               "FROM dr WHERE ended_at IS NOT NULL), "
               "rk AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY role, harness, model, effort "
               "ORDER BY secs IS NULL, secs) AS pos, COUNT(secs) OVER (PARTITION BY role, harness, model, effort) AS n "
               "FROM lat) "
               "SELECT COALESCE(role, 'unknown') AS role, COALESCE(harness, 'unknown') AS harness, "
               "COALESCE(model, 'unknown') AS model, COALESCE(effort, 'unknown') AS effort, COUNT(*) AS terminal, "
               "COUNT(secs) AS recorded, COUNT(*) - COUNT(secs) AS unknown, AVG(secs) AS mean_seconds, "
               "AVG(CASE WHEN secs IS NOT NULL AND pos IN ((n + 1) / 2, (n + 2) / 2) THEN secs END) AS median_seconds, "
               "MAX(secs) AS max_seconds FROM rk GROUP BY role, harness, model, effort ORDER BY 1, 2, 3, 4",
    },
    "dispatch.cost": {
        "needs": ("dispatches",),
        "denominator": "terminal dispatches (ended_at set); recorded = money_actual is not NULL, the rest are unknown",
        "sql": "SELECT COALESCE(role, 'unknown') AS role, COALESCE(harness, 'unknown') AS harness, "
               "COALESCE(model, 'unknown') AS model, COALESCE(effort, 'unknown') AS effort, COUNT(*) AS terminal, "
               "COUNT(money_actual) AS recorded, COUNT(*) - COUNT(money_actual) AS unknown, "
               "SUM(money_actual) AS total FROM dr WHERE ended_at IS NOT NULL GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4",
    },
    "task.accepted_paths": {
        "needs": ("tasks", "dispatches", "revisions"),
        "denominator": "accepted tasks (status accepted with an accepted_revision_id); first executor is "
                       "tasks.first_executor_dispatch_id, accepted producer is the dispatch of the accepted revision",
        "sql": "SELECT COUNT(*) AS accepted_tasks, COALESCE(SUM(first_id IS NULL), 0) AS first_executor_unknown, "
               "COALESCE(SUM(producer_id IS NULL), 0) AS producer_unknown, "
               "COALESCE(SUM(first_id IS NOT NULL AND producer_id IS NOT NULL), 0) AS comparable, "
               "COALESCE(SUM(first_id = producer_id), 0) AS first_executor_is_producer, "
               "COALESCE(SUM(first_id <> producer_id AND first_route IS NOT NULL AND first_route = producer_route), 0) "
               "AS later_dispatch_same_route, "
               "COALESCE(SUM(first_id <> producer_id AND first_route IS NOT NULL AND producer_route IS NOT NULL "
               "AND first_route <> producer_route), 0) AS first_final_mismatch, "
               "COALESCE(SUM(first_id <> producer_id AND (first_route IS NULL OR producer_route IS NULL)), 0) "
               "AS route_unknown FROM tr WHERE is_accepted = 1",
    },
    "task.handoffs": {
        "needs": ("tasks", "dispatches", "revisions"),
        "denominator": "tasks, split into accepted and unresolved; an edge is an executor dispatch whose recorded "
                       "predecessor is an executor dispatch of the same run and task",
        "sql": ", tc AS (SELECT tr.run_id, tr.task_id, tr.is_accepted, tr.first_route, tr.producer_route, "
               "COALESCE(SUM(ed.from_route IS NOT NULL AND ed.to_route IS NOT NULL AND ed.from_route = ed.to_route), 0) "
               "AS same_route_retries, "
               "COALESCE(SUM(ed.from_route IS NOT NULL AND ed.to_route IS NOT NULL AND ed.from_route <> ed.to_route), 0) "
               "AS cross_route_handoffs, "
               "COALESCE(SUM(ed.from_route IS NOT NULL AND ed.to_route IS NOT NULL AND ed.from_route <> ed.to_route "
               "AND ed.to_id IS NOT tr.producer_id), 0) AS intermediate_cross_route_handoffs, "
               "COALESCE(SUM(ed.to_id IS NOT NULL AND (ed.from_route IS NULL OR ed.to_route IS NULL)), 0) "
               "AS route_unknown_edges FROM tr LEFT JOIN ed ON ed.run_id = tr.run_id AND ed.task_id = tr.task_id "
               "GROUP BY tr.run_id, tr.task_id) "
               "SELECT CASE WHEN is_accepted = 1 THEN 'accepted' ELSE 'unresolved' END AS task_state, COUNT(*) AS tasks, "
               "SUM(same_route_retries) AS same_route_retries, SUM(same_route_retries > 0) AS tasks_with_same_route_retry, "
               "SUM(cross_route_handoffs) AS cross_route_handoffs, "
               "SUM(cross_route_handoffs > 0) AS tasks_with_cross_route_handoff, "
               "SUM(intermediate_cross_route_handoffs) AS intermediate_cross_route_handoffs, "
               "SUM(cross_route_handoffs > 0 AND first_route IS NOT NULL AND first_route = producer_route) "
               "AS swap_away_and_back_tasks, SUM(route_unknown_edges) AS route_unknown_handoffs "
               "FROM tc GROUP BY 1 ORDER BY 1",
    },
    "task.links": {
        "needs": ("dispatches",),
        "denominator": "every executor dispatch; first = it is its task's first_executor_dispatch_id, linked = it has "
                       "a valid recorded predecessor, unknown = neither",
        "sql": "SELECT COUNT(*) AS executor_dispatches, COALESCE(SUM(t.first_executor_dispatch_id = d.id), 0) AS first, "
               "COALESCE(SUM(d.id IN (SELECT to_id FROM ed)), 0) AS linked, "
               "COALESCE(SUM(COALESCE(t.first_executor_dispatch_id <> d.id, 1) AND d.id NOT IN (SELECT to_id FROM ed)), 0) "
               "AS unknown FROM dr d LEFT JOIN tk t ON t.run_id = d.run_id AND t.task_id = d.task_id "
               "WHERE d.role = 'executor'",
    },
    "task.revisions_to_accept": {
        "needs": ("tasks", "revisions"),
        "denominator": "accepted tasks: revisions of the task up to and including the accepted one (NULL = the accepted "
                       "revision row is missing); unresolved tasks: revisions submitted so far, shown apart",
        "sql": "SELECT 'accepted' AS task_state, n AS revisions, COUNT(*) AS tasks FROM ("
               "SELECT t.run_id, t.task_id, CASE WHEN ar.id IS NULL THEN NULL ELSE COUNT(r.id) END AS n FROM tk t "
               "LEFT JOIN rv ar ON ar.id = t.accepted_revision_id AND ar.run_id = t.run_id "
               "LEFT JOIN rv r ON r.run_id = t.run_id AND r.task_id = t.task_id AND r.seq <= ar.seq "
               "WHERE t.is_accepted = 1 GROUP BY t.run_id, t.task_id) GROUP BY n "
               "UNION ALL SELECT 'unresolved', n, COUNT(*) FROM ("
               "SELECT t.run_id, t.task_id, COUNT(r.id) AS n FROM tk t "
               "LEFT JOIN rv r ON r.run_id = t.run_id AND r.task_id = t.task_id "
               "WHERE t.is_accepted = 0 GROUP BY t.run_id, t.task_id) GROUP BY n ORDER BY 1, 2",
    },
    "self_review": {
        "needs": ("revisions", "tasks"),
        "denominator": "every row of revisions; missing = self_review_json is NULL",
        "sql": "SELECT CASE WHEN r.self_review_json IS NULL THEN 'missing' WHEN NOT json_valid(r.self_review_json) "
               "THEN 'unreadable' ELSE COALESCE(json_extract(r.self_review_json, '$.kind'), 'unreadable') END AS kind, "
               "CASE WHEN json_valid(r.self_review_json) THEN json_extract(r.self_review_json, '$.type') END AS exempt_type, "
               "COUNT(*) AS revisions, "
               "COALESCE(SUM(r.id IN (SELECT accepted_revision_id FROM tk WHERE is_accepted = 1)), 0) "
               "AS accepted_revisions FROM rv r GROUP BY 1, 2 ORDER BY 1, 2",
    },
    "lanes.exposure": {
        "needs": ("gates",),
        "denominator": "gates with subject lane; a pair is one distinct (run, lane scope, member task) from the "
                       "frozen members_json; NULL members_json is unknown membership",
        "sql": ", lm AS (SELECT DISTINCT g.run_id, g.scope, j.value AS task_id FROM gt g, "
               "json_each(CASE WHEN json_valid(g.members_json) THEN g.members_json ELSE '[]' END) j "
               "WHERE g.subject = 'lane') "
               "SELECT (SELECT COUNT(*) FROM gt WHERE subject = 'lane') AS lane_gates, "
               "(SELECT COUNT(*) FROM gt WHERE subject = 'lane' AND members_json IS NULL) AS membership_unknown_gates, "
               "(SELECT COUNT(DISTINCT run_id || char(31) || scope) FROM gt WHERE subject = 'lane') AS lanes, "
               "(SELECT COUNT(*) FROM lm) AS lane_task_pairs, "
               "(SELECT COUNT(DISTINCT run_id || char(31) || task_id) FROM lm) AS exposed_tasks, "
               "(SELECT COUNT(*) FROM (SELECT 1 FROM lm GROUP BY run_id, task_id HAVING COUNT(*) > 1)) "
               "AS tasks_in_several_lanes",
    },
    "findings.attribution": {
        "needs": ("findings", "gates"),
        "denominator": "convergence-v1 findings, one per (run, gate, code): the rows recorded once per repair owner "
                       "count once; basis NULL = recorded before attribution existed",
        "sql": ", uf AS (SELECT f.run_id, f.gate_id, f.code, COUNT(*) AS recorded_rows, "
               "COALESCE(SUM(f.clone_of IS NOT NULL), 0) AS clone_rows, MIN(f.attribution_basis) AS basis, "
               "MIN(g.members_json) AS members_json FROM fd f LEFT JOIN gt g ON g.id = f.gate_id "
               "WHERE f.contract = 'convergence-v1' AND f.gate_id IS NOT NULL GROUP BY f.run_id, f.gate_id, f.code) "
               "SELECT COALESCE(basis, 'unknown') AS basis, COUNT(*) AS unique_findings, "
               "SUM(recorded_rows) AS recorded_rows, SUM(recorded_rows) - COUNT(*) AS duplicate_rows, "
               "SUM(clone_rows) AS clone_marked_rows, "
               "COALESCE(SUM(CASE WHEN json_valid(members_json) THEN json_array_length(members_json) END), 0) "
               "AS lane_exposure, SUM(NOT json_valid(members_json) OR members_json IS NULL) AS membership_unknown "
               "FROM uf GROUP BY 1 ORDER BY 1",
    },
    "tags.tasks": {
        "needs": ("tasks",),
        "denominator": "every row of tasks, once per tag; not-recorded = the descriptor has no such key",
        "sql": "SELECT k.key AS tag, CASE WHEN t.descriptor_json IS NULL THEN 'descriptor-null' "
               "WHEN NOT json_valid(t.descriptor_json) THEN 'descriptor-unreadable' "
               "WHEN json_extract(t.descriptor_json, '$.' || k.key) IS NULL THEN 'not-recorded' "
               "ELSE CAST(json_extract(t.descriptor_json, '$.' || k.key) AS TEXT) END AS value, COUNT(*) AS tasks "
               "FROM tk t, (SELECT 'evidence_domain' AS key UNION ALL SELECT 'intent' UNION ALL "
               "SELECT 'difficulty_estimate' UNION ALL SELECT 'brief_shape') k GROUP BY 1, 2 ORDER BY 1, 2",
    },
    "tags.dispatches": {
        "needs": ("dispatches",),
        "denominator": "every executor dispatch, once per tag; the descriptor is the one snapshotted at launch",
        "sql": "SELECT k.key AS tag, CASE WHEN d.descriptor_json IS NULL THEN 'descriptor-null' "
               "WHEN NOT json_valid(d.descriptor_json) THEN 'descriptor-unreadable' "
               "WHEN json_extract(d.descriptor_json, '$.' || k.key) IS NULL THEN 'not-recorded' "
               "ELSE CAST(json_extract(d.descriptor_json, '$.' || k.key) AS TEXT) END AS value, COUNT(*) AS dispatches "
               "FROM dr d, (SELECT 'evidence_domain' AS key UNION ALL SELECT 'intent' UNION ALL "
               "SELECT 'difficulty_estimate' UNION ALL SELECT 'brief_shape') k WHERE d.role = 'executor' "
               "GROUP BY 1, 2 ORDER BY 1, 2",
    },
}


# ------------------------------------------------------------------ source

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_readonly(path: Path) -> sqlite3.Connection:
    """The only way this report opens a database: URI mode=ro, then query_only. It never creates the file."""
    con = sqlite3.connect(f"file:{quote(os.path.abspath(path))}?mode=ro", uri=True, timeout=30)
    con.execute("PRAGMA query_only=ON")
    return con


def read_schema(con: sqlite3.Connection) -> dict[str, set[str]]:
    """table -> its columns, for the tables this report reads."""
    wanted = {*VIEWS, "schema_meta", "outcome_labels"}
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'") if r[0] in wanted]
    return {t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")} for t in tables}


def schema_version(con: sqlite3.Connection, schema: dict) -> int | str:
    if "schema_meta" not in schema:
        return "absent"
    row = con.execute("SELECT value FROM schema_meta WHERE key = 'office_schema'").fetchone()
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return "absent"


def missing_schema(schema: dict) -> list[str]:
    """`table.column` (or `table`) this report reads that the database lacks; each reads as unknown."""
    out = []
    for table, columns in VIEWS.items():
        if table not in schema:
            out.append(table)
            continue
        out.extend(f"{table}.{c}" for c in (*columns, *EXTRA_COLUMNS.get(table, ())) if c not in schema[table])
    return sorted(out)


def _rows(con: sqlite3.Connection, schema: dict, metric: str) -> list[dict]:
    cursor = con.execute(prelude(schema) + " " + METRICS[metric]["sql"])
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _section(con, schema, metrics: tuple[str, ...], build, optional: tuple[str, ...] = ()) -> dict:
    """Run a section's metrics and hand their rows to `build`. A metric in `optional` whose tables are absent gives
    None; any other absent table makes the whole section unavailable."""
    def lacking(metric: str) -> list[str]:
        return [t for t in METRICS[metric]["needs"] if t not in schema]
    need = sorted({t for m in metrics if m not in optional for t in lacking(m)})
    if need:
        return {"available": False, "missing_tables": need}
    return {"available": True, **build(*(None if lacking(m) else _rows(con, schema, m) for m in metrics))}


def _clamp(value, allowed: tuple[str, ...], other: str = "other") -> str:
    return value if value in allowed else other


def _sum(rows: list[dict], key: str) -> int:
    return sum(r.get(key) or 0 for r in rows)


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    mid = len(v) // 2
    return float(v[mid]) if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _histogram(rows: list[dict], state: str) -> dict:
    hist = {r["revisions"]: r["tasks"] for r in rows if r["task_state"] == state}
    known = {n: t for n, t in hist.items() if n is not None}
    count = sum(known.values())
    values = [n for n, t in known.items() for _ in range(t)]
    return {"tasks": sum(hist.values()), "revision_row_missing": hist.get(None, 0),
            "mean": round(sum(values) / count, 3) if count else None, "median": _median(values),
            "histogram": {str(n): known[n] for n in sorted(known)}}


# ------------------------------------------------------------------ sections

def _dispatch_section(con, schema) -> dict:
    def build(routes, strict):
        by_key = None if strict is None else {(r["role"], r["harness"], r["model"], r["effort"]): r["strict_successes"]
                                              for r in strict}

        def successes(r):
            if by_key is None or r["role"] not in ("executor", "worker"):
                return None  # unknown: tasks or revisions are absent, or the role never produces a revision
            return by_key.get((r["role"], r["harness"], r["model"], r["effort"]), 0)
        rows = [{**r, "strict_successes": successes(r)} for r in routes]
        return {"routes": rows, "totals": {"dispatches": _sum(routes, "dispatches"), "terminal": _sum(routes, "terminal"),
                                           "pending": _sum(routes, "pending"),
                                           "strict_successes": None if by_key is None else sum(by_key.values())}}
    return _section(con, schema, ("dispatch.routes", "dispatch.strict_successes"), build, optional=("dispatch.strict_successes",))


def _episode_section(con, schema) -> dict:
    """Accepted task-route episodes, from route_learning's read-only derivation (it only SELECTs; the report's
    connection is query_only, so a write would raise instead of landing)."""
    if route_learning is None:
        return {"available": False, "reason": "the office package is not importable"}
    lacking = sorted(t for t in ("dispatches", "tasks", "revisions", "outcome_labels") if t not in schema)
    if lacking:
        return {"available": False, "missing_tables": lacking}
    try:
        outcomes = route_learning.derive_outcomes(con)
        episodes = route_learning.episodes(outcomes)
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"{type(exc).__name__} while deriving outcomes"}
    rows: dict[tuple, dict] = {}

    def row(role: str, route: str) -> dict:
        harness, _, rest = route.partition("/")
        model, _, effort = rest.rpartition("@")
        key = (role, harness, model, "unknown" if effort in ("None", "") else effort)
        return rows.setdefault(key, {"role": key[0], "harness": key[1], "model": key[2], "effort": key[3],
                                     "settled_dispatches": 0, "settled_strict_successes": 0, "episodes": 0,
                                     "accepted_episodes": 0, "failed_episodes": 0, "multi_attempt_episodes": 0,
                                     "failed_by_attribution": {a: 0 for a in ATTRIBUTIONS}})
    for o in outcomes:
        r = row(o["role"], o["route"])
        r["settled_dispatches"] += 1
        r["settled_strict_successes"] += int(o["success"])
    for e in episodes:
        r = row(e["role"], e["route"])
        r["episodes"] += 1
        r["multi_attempt_episodes"] += int(e["attempts"] > 1)
        if e["success"]:
            r["accepted_episodes"] += 1
        else:
            r["failed_episodes"] += 1
            r["failed_by_attribution"][_clamp(e["attribution"], ATTRIBUTIONS, "unknown")] += 1
    out = [rows[k] for k in sorted(rows)]
    return {"available": True, "routes": out,
            "totals": {"settled_dispatches": _sum(out, "settled_dispatches"),
                       "settled_strict_successes": _sum(out, "settled_strict_successes"),
                       "episodes": _sum(out, "episodes"), "accepted_episodes": _sum(out, "accepted_episodes"),
                       "failed_episodes": _sum(out, "failed_episodes")}}


def _task_path_section(con, schema) -> dict:
    def build(paths, handoffs, links, revisions):
        return {"accepted": paths[0] if paths else {}, "handoffs_by_task_state": handoffs, "executor_dispatch_links": links[0],
                "revisions_to_accept": _histogram(revisions, "accepted"),
                "unresolved_revisions_so_far": _histogram(revisions, "unresolved")}
    return _section(con, schema, ("task.accepted_paths", "task.handoffs", "task.links", "task.revisions_to_accept"), build)


def _self_review_section(con, schema) -> dict:
    def build(rows):
        out: dict[tuple, dict] = {}
        for r in rows:
            kind = _clamp(r["kind"], SELF_REVIEW_KINDS, "unreadable")
            etype = _clamp(r["exempt_type"], EXEMPT_TYPES) if kind == "exempt" else None
            cell = out.setdefault((kind, etype), {"kind": kind, "exempt_type": etype, "revisions": 0, "accepted_revisions": 0})
            cell["revisions"] += r["revisions"]
            cell["accepted_revisions"] += r["accepted_revisions"]
        table = [out[k] for k in sorted(out, key=lambda k: (k[0], k[1] or ""))]
        return {"by_receipt": table, "totals": {"revisions": _sum(table, "revisions"),
                                                "accepted_revisions": _sum(table, "accepted_revisions")}}
    return _section(con, schema, ("self_review",), build)


def _finding_section(con, schema) -> dict:
    def build(lanes, findings):
        out: dict[str, dict] = {}
        for r in findings:
            basis = r["basis"] if r["basis"] == "unknown" else _clamp(r["basis"], FINDING_BASES)
            cell = out.setdefault(basis, {k: 0 for k in ("unique_findings", "recorded_rows", "duplicate_rows",
                                                         "clone_marked_rows", "lane_exposure", "membership_unknown")})
            for k in cell:
                cell[k] += r[k] or 0
        table = [{"basis": b, **out[b]} for b in sorted(out)]
        return {"lanes": lanes[0], "by_attribution": table,
                "totals": {k: _sum(table, k) for k in ("unique_findings", "recorded_rows", "duplicate_rows", "lane_exposure")},
                "attributed_findings": sum(c["unique_findings"] for b, c in out.items() if b in FINDING_BASES[:2]),
                "unassigned_findings": out.get("unassigned", {}).get("unique_findings", 0),
                "attribution_unknown_findings": out.get("unknown", {}).get("unique_findings", 0)}
    return _section(con, schema, ("lanes.exposure", "findings.attribution"), build)


def _latency_cost_section(con, schema) -> dict:
    def build(latency, cost):
        for r in cost:
            r["total"] = "unknown" if not r["recorded"] else round(r["total"], 6)
        for r in latency:
            for k in ("mean_seconds", "median_seconds", "max_seconds"):
                r[k] = "unknown" if r[k] is None else round(r[k], 3)
        return {"latency": latency, "cost": cost,
                "cost_note": "money_actual as recorded by the harness adapter; unit not recorded; 'unknown' = none recorded"}
    return _section(con, schema, ("dispatch.latency", "dispatch.cost"), build)


def _size_section(con, schema) -> dict:
    def build(task, snapshot, run):
        def dist(rows, key):
            out: dict[str, int] = {}
            for r in rows:
                size = r["size"] if r["size"] == "unknown" else _clamp(r["size"], SIZES)
                out[size] = out.get(size, 0) + r[key]
            return dict(sorted(out.items()))
        by_route: dict[tuple, dict] = {}
        for r in snapshot:
            size = r["size"] if r["size"] == "unknown" else _clamp(r["size"], SIZES)
            cell = by_route.setdefault((r["harness"], r["model"], r["effort"]),
                                       {"harness": r["harness"], "model": r["model"], "effort": r["effort"], "sizes": {}})
            cell["sizes"][size] = cell["sizes"].get(size, 0) + r["dispatches"]
        return {"task_size": {"label": "planner task_size per task (tasks.descriptor_json)", "tasks": dist(task, "tasks")},
                "dispatch_task_size_snapshot": {"label": "task size snapshotted on executor dispatches (dispatches.size_class)",
                                                "dispatches": dist(snapshot, "dispatches"),
                                                "by_route": [by_route[k] for k in sorted(by_route)]},
                "run_size": {"label": "run size_class from the run's risk record (runs.risk_json)", "runs": dist(run, "runs")}}
    return _section(con, schema, ("size.task", "size.dispatch_snapshot", "size.run"), build)


def _tag_section(con, schema) -> dict:
    def coverage(rows, key):
        out = {}
        for tag, allowed in TAGS.items():
            values: dict[str, int] = {}
            for r in rows:
                if r["tag"] != tag:
                    continue
                v = r["value"]
                if v not in ("descriptor-null", "descriptor-unreadable", "not-recorded"):
                    v = _clamp(v, allowed)
                values[v] = values.get(v, 0) + r[key]
            total = sum(values.values())
            unknown = sum(values.get(k, 0) for k in ("descriptor-null", "descriptor-unreadable", "not-recorded"))
            out[tag] = {"declared": total - unknown, "unknown_not_recorded": unknown, "values": dict(sorted(values.items()))}
        return out
    return _section(con, schema, ("tags.tasks", "tags.dispatches"),
                    lambda tasks, dispatches: {"tasks": coverage(tasks, "tasks"), "executor_dispatches": coverage(dispatches, "dispatches")})


def _population_section(con, schema) -> dict:
    def build(span, phases, statuses):
        return {"runs": span[0], "runs_by_phase": {r["phase"]: r["runs"] for r in phases},
                "tasks_by_status": {r["status"]: r["tasks"] for r in statuses}}
    return _section(con, schema, ("runs.span", "runs.by_phase", "tasks.by_status"), build)


def build_report(path: Path) -> dict:
    """The whole report for one database file. Raises sqlite3.DatabaseError for a file that is not a database."""
    path = Path(path)
    before = sha256_file(path)
    size = path.stat().st_size
    con = open_readonly(path)
    try:
        schema = read_schema(con)
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check").fetchmany(5)]
        report = {
            "report": "routing-evidence", "report_version": REPORT_VERSION,
            "source": {
                "sha256": before, "size_bytes": size, "integrity_check": integrity[0] if integrity == ["ok"] else integrity,
                "schema_version": schema_version(con, schema), "journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0],
                "wal_file_present": Path(f"{path}-wal").exists(), "shm_file_present": Path(f"{path}-shm").exists(),
                "caveats": list(CAVEATS),
            },
            "schema_gaps_read_as_unknown": missing_schema(schema),
        }
        population = _population_section(con, schema)
        report["source"]["run_date_range"] = ({"runs": population["runs"]["runs"], "first": population["runs"]["first_day"],
                                               "last": population["runs"]["last_day"]} if population["available"] else "unavailable")
        report["sections"] = {
            "population": population,
            "dispatches": _dispatch_section(con, schema),
            "episodes": _episode_section(con, schema),
            "task_paths": _task_path_section(con, schema),
            "self_review": _self_review_section(con, schema),
            "findings": _finding_section(con, schema),
            "latency_and_cost": _latency_cost_section(con, schema),
            "size": _size_section(con, schema),
            "tags": _tag_section(con, schema),
        }
    finally:
        con.close()
    after = sha256_file(path)
    report["source"]["sha256_after"] = after
    report["source"]["hash_unchanged"] = before == after
    return report


# ------------------------------------------------------------------ output

def render_text(value, indent: int = 0) -> str:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(render_text(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                lines.append(pad + "- " + " ".join(f"{k}={_scalar(v)}" for k, v in item.items()))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(value)}")
    return "\n".join(lines)


def _scalar(v) -> str:
    if v is None:
        return "unknown"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline, read-only, aggregate-only routing evidence report.")
    ap.add_argument("--db", required=True, type=Path, help="runs.db (or a copy) to read; it is never written")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    if not args.db.is_file():
        print("routing_evidence_report: --db is not a file", file=sys.stderr)
        return 2
    try:
        report = build_report(args.db)
    except sqlite3.Error as exc:
        print(f"routing_evidence_report: cannot read the file as a database ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.format == "json" else render_text(report))
    return 0 if report["source"]["integrity_check"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
