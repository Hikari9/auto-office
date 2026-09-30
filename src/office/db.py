"""runs.db: the single transactional lifecycle authority (ADR 0001).

One semantic transition is one `BEGIN IMMEDIATE` transaction. Work that must
happen outside the database after a transition (launching an agent, running a
reviewer, capturing a screenshot) is written to `outbox` in the same
transaction, so a crash can delay that work but never lose it.

The legacy v3 recorder tables are created with their original definitions and
extended only with nullable columns, so a retained v3 runtime keeps writing to
the same file without schema errors.
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from office import paths

SCHEMA_VERSION = 3

LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, family_id TEXT, created_at TEXT, plugin_commit TEXT, policy_hash TEXT, catalog_hash TEXT, adapter_hash TEXT, config_hash TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS dispatches(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, holder_id TEXT, triple TEXT, invocation_model_id TEXT, selection_reason TEXT, task_shape TEXT, size_class TEXT, started_at TEXT, ended_at TEXT, money_estimate REAL, money_actual REAL, quota_estimate REAL, quota_delta REAL, wall_clock_seconds REAL, attribution TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY, dispatch_id TEXT, reviewer_dispatch_id TEXT, status TEXT, severity TEXT, summary TEXT, evidence_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS validations(id TEXT PRIMARY KEY, dispatch_id TEXT, kind TEXT, command TEXT, passed INTEGER, known_bad_proven INTEGER, evidence_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS routing_decisions(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, request_hash TEXT, selected_triple TEXT, decision_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS artifact_versions(id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, version INTEGER, content_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS ownership_events(id TEXT PRIMARY KEY, run_id TEXT, role TEXT, scope TEXT, prior_holder TEXT, new_holder TEXT, event TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS outcome_labels(id TEXT PRIMARY KEY, dispatch_id TEXT, label TEXT, primary_attribution TEXT, contributing_attributions TEXT, labeled_at TEXT, evidence_hash TEXT);
CREATE TABLE IF NOT EXISTS lineage(id TEXT PRIMARY KEY, component_kind TEXT, component_id TEXT, parent_id TEXT, event TEXT, multiplier REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL, scope TEXT NOT NULL, holder_id TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, released_at TEXT, revoked_at TEXT, revoke_reason TEXT);
CREATE TABLE IF NOT EXISTS adapter_trust_acts(id TEXT PRIMARY KEY, triple TEXT NOT NULL, target_state TEXT NOT NULL, actor_id TEXT NOT NULL, reason TEXT NOT NULL, evidence_reference TEXT, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recorded_overrides(override_id TEXT PRIMARY KEY, run_id TEXT, family_id TEXT, task_id TEXT, role TEXT, candidate_id TEXT, bypass_stage INTEGER, rationale TEXT, authorized_by TEXT, authorized_at TEXT, expires_at TEXT);
"""

# Nullable 3.1 columns on tables shared with the legacy recorder.
SHARED_COLUMNS = {
    "runs": [
        "office_version TEXT", "repo_root TEXT", "git_common_dir TEXT", "goal TEXT", "phase TEXT",
        "gear TEXT", "playbook TEXT", "base_sha TEXT", "state_dir TEXT",
        "requirements_version INTEGER", "plan_version INTEGER", "routing_version INTEGER",
        "policy_json TEXT", "risk_json TEXT", "gates_json TEXT", "envelope_json TEXT",
        "plan_review_json TEXT", "planner_mode TEXT", "terminal_at TEXT", "terminal_reason TEXT",
        "archive_digest TEXT", "pruned_at TEXT", "prune_status TEXT", "updated_at TEXT",
        "landing_json TEXT", "escalations_used INTEGER",
    ],
    "dispatches": [
        "invocation_model_id TEXT", "selection_reason TEXT",
        "task_id TEXT", "kind TEXT", "office_version TEXT", "status TEXT", "pid INTEGER",
        "exit_code INTEGER", "signal INTEGER", "terminal_classification TEXT", "worktree TEXT",
        "branch TEXT", "base_commit TEXT", "lease_id TEXT", "packet_hash TEXT", "packet_path TEXT",
        "harness TEXT", "model TEXT", "effort TEXT", "adapter_id TEXT", "applied_plan_version INTEGER",
        "log_path TEXT", "launcher TEXT", "pane_id TEXT", "launched_at TEXT", "last_seen_at TEXT",
        "route_json TEXT", "gate_id TEXT", "override_json TEXT",
        # #200: pane lifecycle (session capture for resume, reclaim on accept).
        "session_id TEXT", "resumed_from TEXT", "keep_pane INTEGER", "pane_closed_at TEXT",
    ],
    # v2 (#185): user-declared model overrides.
    "tasks": ["review_override_json TEXT"],
    # v3 (3.2): the route preview shown in the plan diagram.
    "plans": ["preview_json TEXT"],
    "findings": [
        "run_id TEXT", "task_id TEXT", "gate_id TEXT", "revision_id TEXT", "gate_kind TEXT",
        "code TEXT", "fingerprint TEXT", "location TEXT", "category TEXT", "action TEXT",
        "measurement_json TEXT", "state TEXT", "origin_gate_id TEXT", "updated_at TEXT",
        "evidence TEXT",
    ],
    "leases": [
        "task_id TEXT", "fencing INTEGER", "pid INTEGER", "dispatch_id TEXT", "renewed_at TEXT",
    ],
}

OFFICE_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS requirements(run_id TEXT NOT NULL, version INTEGER NOT NULL, frozen_json TEXT NOT NULL, source TEXT NOT NULL, quote TEXT, created_at TEXT NOT NULL, PRIMARY KEY(run_id, version));
CREATE TABLE IF NOT EXISTS authorizations(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT, requirements_version INTEGER, envelope_json TEXT, authorized_by TEXT NOT NULL, quote TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT, revoke_reason TEXT);
CREATE TABLE IF NOT EXISTS plans(run_id TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, tasks_json TEXT NOT NULL, requirements_json TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL, content_hash TEXT NOT NULL, parent_version INTEGER, amendment_id TEXT, PRIMARY KEY(run_id, version));
CREATE TABLE IF NOT EXISTS tasks(run_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, role TEXT NOT NULL, scope_json TEXT NOT NULL, depends_json TEXT NOT NULL, interfaces_json TEXT, accept_json TEXT NOT NULL, checks_json TEXT NOT NULL, visual_json TEXT, status TEXT NOT NULL, pause_reason TEXT, introduced_plan_version INTEGER NOT NULL, contract_version INTEGER NOT NULL, acceptance_version INTEGER NOT NULL, current_dispatch_id TEXT, current_revision_id TEXT, accepted_revision_id TEXT, escalations_used INTEGER NOT NULL DEFAULT 0, stack_after TEXT, route_json TEXT, review_override_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(run_id, id));
CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL, seq INTEGER NOT NULL, dispatch_id TEXT, lease_id TEXT, fencing INTEGER, commit_sha TEXT NOT NULL, tree_sha TEXT NOT NULL, base_commit TEXT, requirements_version INTEGER NOT NULL, plan_version INTEGER NOT NULL, applied_version INTEGER NOT NULL, env_fingerprint TEXT NOT NULL, refs_json TEXT, operation_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, supersedes TEXT, changed_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gates(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, subject TEXT NOT NULL, task_id TEXT, revision_id TEXT, plan_version INTEGER, kind TEXT NOT NULL, input_key TEXT NOT NULL, status TEXT NOT NULL, verdict TEXT, evidence_status TEXT, route TEXT, route_json TEXT, job_id TEXT, round INTEGER NOT NULL DEFAULT 1, attempt INTEGER NOT NULL DEFAULT 1, env_failures INTEGER NOT NULL DEFAULT 0, recaptures INTEGER NOT NULL DEFAULT 0, escalated INTEGER NOT NULL DEFAULT 0, summary TEXT, stale_reason TEXT, fallback TEXT, reused_from TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
CREATE INDEX IF NOT EXISTS gates_subject ON gates(run_id, subject, kind);
CREATE TABLE IF NOT EXISTS amendments(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL, class TEXT NOT NULL, scope_json TEXT NOT NULL, delta TEXT NOT NULL, structured_json TEXT, from_plan_version INTEGER, to_plan_version INTEGER, requested_by TEXT NOT NULL, status TEXT NOT NULL, reject_reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, amendment_id TEXT NOT NULL, task_id TEXT NOT NULL, dispatch_id TEXT, target_version INTEGER NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL, delivered_at TEXT, delivered_count INTEGER NOT NULL DEFAULT 0, applied_at TEXT, superseded_by TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL, audience TEXT NOT NULL, task_id TEXT, dispatch_id TEXT, summary TEXT NOT NULL, payload_json TEXT, office_version TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, audience, seq);
CREATE TABLE IF NOT EXISTS cursors(run_id TEXT NOT NULL, consumer TEXT NOT NULL, last_seq INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(run_id, consumer));
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, office_version TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, not_before TEXT, claimed_by TEXT, claimed_pid INTEGER, claimed_at TEXT, kicked_at TEXT, finished_at TEXT, result_json TEXT, error TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_run ON outbox(run_id, status);
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT, revision_id TEXT, gate_id TEXT, kind TEXT NOT NULL, path TEXT, sha256 TEXT, bytes INTEGER, meta_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_bindings(harness TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL, bound_at TEXT NOT NULL, bound_by TEXT NOT NULL, ended_at TEXT, PRIMARY KEY(harness, session_id));
CREATE TABLE IF NOT EXISTS capability_proofs(key TEXT PRIMARY KEY, harness TEXT NOT NULL, harness_version TEXT, model TEXT NOT NULL, effort TEXT, adapter_hash TEXT, capability TEXT NOT NULL, result TEXT NOT NULL, evidence_hash TEXT, details TEXT, proved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS compat_calls(id TEXT PRIMARY KEY, at TEXT NOT NULL, office_version TEXT NOT NULL, run_id TEXT, command TEXT NOT NULL, argv_json TEXT NOT NULL, caller TEXT, outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visual_refs(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, approved_by TEXT NOT NULL, provenance TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deviations(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT, reference_id TEXT, description TEXT NOT NULL, reason TEXT NOT NULL, authorized_by TEXT NOT NULL, scope_json TEXT, created_at TEXT NOT NULL);
"""

# #131 F6: a 3.1 finding must name a recorded dispatch. Scoped to 3.1 rows
# (run_id set) so retained v3 writers keep their original behaviour.
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS findings_dispatch_fk BEFORE INSERT ON findings
WHEN NEW.run_id IS NOT NULL AND NEW.dispatch_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM dispatches WHERE id = NEW.dispatch_id)
BEGIN SELECT RAISE(ABORT, 'finding references an unrecorded dispatch'); END;
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else paths.runs_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":  # pragma: no cover - platform specific
        raise RuntimeError(f"runs.db could not enter WAL mode ({mode})")
    con.execute("PRAGMA foreign_keys=ON")
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> None:
    # The version alone is not enough: #200 added columns without a bump, so
    # every runs.db already at v2 skipped them (#211). A missing table or
    # column re-runs the idempotent migration whatever the stored version says.
    if _schema_version(con) >= SCHEMA_VERSION and not _drifted(con):
        return
    with transaction(con):
        if _schema_version(con) >= SCHEMA_VERSION and not _drifted(con):
            return
        for stmt in _statements(LEGACY_DDL + OFFICE_DDL):
            con.execute(stmt)
        for table, columns in SHARED_COLUMNS.items():
            have = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            for col in columns:
                name = col.split()[0]
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        con.execute(TRIGGERS)
        con.execute("CREATE INDEX IF NOT EXISTS runs_repo ON runs(git_common_dir, phase)")
        con.execute("CREATE INDEX IF NOT EXISTS findings_task ON findings(run_id, task_id, state)")
        con.execute("CREATE INDEX IF NOT EXISTS dispatches_run ON dispatches(run_id, task_id)")
        # Never lower a stamp a newer runtime wrote; drift repair runs below it.
        con.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('office_schema', ?)",
                    (str(max(SCHEMA_VERSION, _schema_version(con))),))


def _schema_version(con: sqlite3.Connection) -> int:
    try:
        row = con.execute("SELECT value FROM schema_meta WHERE key='office_schema'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


_TABLES = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", LEGACY_DDL + OFFICE_DDL)


def _drifted(con: sqlite3.Connection) -> bool:
    """True when a table or shared column this version expects is absent."""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not have.issuperset(_TABLES):
        return True
    for table, columns in SHARED_COLUMNS.items():
        cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if any(col.split()[0] not in cols for col in columns):
            return True
    return False


def _statements(ddl: str):
    for stmt in ddl.split(";"):
        if stmt.strip():
            yield stmt


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One semantic transition. BEGIN IMMEDIATE serializes writers up front,
    so preconditions read inside the block cannot change before commit."""
    if con.in_transaction:
        # Nested call inside an outer transition: the outer block commits.
        yield con
        return
    delay = 0.05
    for attempt in range(40):
        try:
            con.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)
    else:  # pragma: no cover - only under pathological contention
        raise sqlite3.OperationalError("runs.db stayed locked")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
