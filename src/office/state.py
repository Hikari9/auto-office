"""Row-level access to the runs.db authority.

Every mutating function here expects to run inside `db.transaction` and never
opens its own. Semantic operations (office.lifecycle and friends) compose
these inside one transaction.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from office import paths, version
from office.util import atomic_write_json, dumps, loads, now_iso, sha256_obj

TERMINAL_PHASES = ("closed", "abandoned")
TASK_TERMINAL = ("accepted", "cancelled")


class OfficeError(Exception):
    """A refused operation with a category, scope, preserved work and next step."""

    exit_code = 1

    def __init__(self, category: str, message: str, *, next_step: str | None = None,
                 scope: str | None = None, preserved: str | None = None, exit_code: int | None = None,
                 data: dict | None = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.next_step = next_step
        self.scope = scope
        self.preserved = preserved
        self.data = data or {}
        if exit_code is not None:
            self.exit_code = exit_code


class Refused(OfficeError):
    exit_code = 4


class NoRun(OfficeError):
    exit_code = 3


class Usage(OfficeError):
    exit_code = 2


# ---------------------------------------------------------------- runs

def get_run(con: sqlite3.Connection, run_id: str) -> dict | None:
    row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run(row)


def find_run(con: sqlite3.Connection, prefix: str) -> dict | None:
    rows = con.execute("SELECT * FROM runs WHERE id=? OR id LIKE ?", (prefix, prefix + "%")).fetchall()
    if len(rows) > 1:
        exact = [r for r in rows if r["id"] == prefix]
        if exact:
            return _run(exact[0])
        raise Usage("ambiguous-run-id", f"run id prefix {prefix!r} matches {len(rows)} runs",
                    next_step="office list, then use a longer id")
    return _run(rows[0]) if rows else None


def _run(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("policy_json", "risk_json", "gates_json", "envelope_json", "plan_review_json", "landing_json"):
        d[key[:-5]] = loads(d.get(key), {} if key != "envelope_json" else [])
    return d


def update_run(con: sqlite3.Connection, run_id: str, **fields) -> None:
    fields["updated_at"] = now_iso()
    for key in ("policy", "risk", "gates", "envelope", "plan_review", "landing"):
        if key in fields:
            fields[key + "_json"] = dumps(fields.pop(key))
    if "phase" in fields:
        fields["status"] = fields["phase"]
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def is_terminal(run: dict) -> bool:
    return run.get("phase") in TERMINAL_PHASES


def pinned_config(run: dict) -> dict:
    return run.get("policy") or {}


# ---------------------------------------------------------------- tasks

TASK_JSON = ("scope_json", "depends_json", "interfaces_json", "accept_json", "checks_json", "visual_json",
             "review_override_json", "pr_json")


def _task(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in TASK_JSON:
        d[key[:-5]] = loads(d.get(key), None if key in ("visual_json", "review_override_json", "pr_json") else [])
    return d


def get_task(con: sqlite3.Connection, run_id: str, task_id: str) -> dict | None:
    return _task(con.execute("SELECT * FROM tasks WHERE run_id=? AND id=?", (run_id, task_id)).fetchone())


def tasks(con: sqlite3.Connection, run_id: str, *, include_planner: bool = False) -> list[dict]:
    rows = con.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY created_at, id", (run_id,)).fetchall()
    out = [_task(r) for r in rows]
    if not include_planner:
        out = [t for t in out if t["role"] != "planner"]
    return out


def update_task(con: sqlite3.Connection, run_id: str, task_id: str, **fields) -> None:
    fields["updated_at"] = now_iso()
    for key in ("scope", "depends", "interfaces", "accept", "checks", "visual", "review_override", "pr"):
        if key in fields:
            fields[key + "_json"] = dumps(fields.pop(key))
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE tasks SET {cols} WHERE run_id=? AND id=?", (*fields.values(), run_id, task_id))


# ---------------------------------------------------------------- dispatches

def get_dispatch(con: sqlite3.Connection, dispatch_id: str) -> dict | None:
    row = con.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["route"] = loads(d.get("route_json"), {})
    return d


def task_dispatches(con: sqlite3.Connection, run_id: str, task_id: str) -> list[dict]:
    rows = con.execute("SELECT * FROM dispatches WHERE run_id=? AND task_id=? ORDER BY started_at",
                       (run_id, task_id)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- events

def emit(con: sqlite3.Connection, run: dict, kind: str, summary: str, *, audience: str = "orchestrator",
         task_id: str | None = None, dispatch_id: str | None = None, payload: dict | None = None) -> int:
    """Append one event. The sequence is allocated inside the caller's
    transaction, so concurrent writers can neither collide nor skip."""
    cur = con.execute(
        "INSERT INTO events(run_id, kind, audience, task_id, dispatch_id, summary, payload_json, office_version, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (run["id"], kind, audience, task_id, dispatch_id, summary, dumps(payload or {}),
         version.current(), now_iso()))
    return cur.lastrowid


def unread_events(con: sqlite3.Connection, run_id: str, consumer: str, audiences: tuple[str, ...],
                  limit: int = 20) -> list[dict]:
    row = con.execute("SELECT last_seq FROM cursors WHERE run_id=? AND consumer=?", (run_id, consumer)).fetchone()
    last = row[0] if row else 0
    marks = ",".join("?" * len(audiences))
    rows = con.execute(
        f"SELECT * FROM events WHERE run_id=? AND seq>? AND audience IN ({marks}) ORDER BY seq LIMIT ?",
        (run_id, last, *audiences, limit)).fetchall()
    return [dict(r) for r in rows]


def advance_cursor(con: sqlite3.Connection, run_id: str, consumer: str, seq: int) -> None:
    con.execute(
        "INSERT INTO cursors(run_id, consumer, last_seq, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(run_id, consumer) DO UPDATE SET last_seq=MAX(last_seq, excluded.last_seq), updated_at=excluded.updated_at",
        (run_id, consumer, seq, now_iso()))


# ---------------------------------------------------------------- outbox

def enqueue(con: sqlite3.Connection, run: dict, kind: str, payload: dict, *, dedup_key: str,
            max_attempts: int = 3, not_before: str | None = None) -> str | None:
    """Queue external work in the caller's transaction. Returns the job id, or
    None when an identical job (same dedup key) already exists."""
    job_id = "J" + uuid.uuid4().hex[:10]
    payload = dict(payload)
    payload.setdefault("office_version", version.current())
    if not version.same_line(payload["office_version"], run["office_version"]):
        raise ValueError("job payload office_version must be on the run's release line")
    cur = con.execute(
        "INSERT OR IGNORE INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, attempts, "
        "max_attempts, not_before, created_at) VALUES(?,?,?,?,?,?,'queued',0,?,?,?)",
        (job_id, run["id"], kind, dedup_key, dumps(payload), payload["office_version"], max_attempts, not_before, now_iso()))
    return job_id if cur.rowcount else None


def get_job(con: sqlite3.Connection, job_id: str) -> dict | None:
    row = con.execute("SELECT * FROM outbox WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = loads(d["payload_json"], {})
    d["result"] = loads(d.get("result_json"), None)
    return d


def pending_jobs(con: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = con.execute("SELECT * FROM outbox WHERE run_id=? AND status IN ('queued','claimed') ORDER BY created_at",
                       (run_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- evidence

def record_evidence(con: sqlite3.Connection, run_id: str, kind: str, path: Path | None, *,
                    task_id: str | None = None, revision_id: str | None = None, gate_id: str | None = None,
                    meta: dict | None = None, digest: str | None = None) -> str:
    from office.util import sha256_file
    ev_id = "E" + uuid.uuid4().hex[:10]
    size = None
    if path is not None and Path(path).is_file():
        digest = digest or sha256_file(Path(path))
        size = Path(path).stat().st_size
    con.execute(
        "INSERT INTO evidence(id, run_id, task_id, revision_id, gate_id, kind, path, sha256, bytes, meta_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ev_id, run_id, task_id, revision_id, gate_id, kind, str(path) if path else None, digest, size,
         dumps(meta or {}), now_iso()))
    return ev_id


# ---------------------------------------------------------------- versions / plans

def current_plan(con: sqlite3.Connection, run_id: str) -> dict | None:
    row = con.execute("SELECT * FROM plans WHERE run_id=? ORDER BY version DESC LIMIT 1", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["tasks"] = loads(d["tasks_json"], [])
    d["requirements"] = loads(d.get("requirements_json"), {})
    return d


def get_plan(con: sqlite3.Connection, run_id: str, plan_version: int) -> dict | None:
    row = con.execute("SELECT * FROM plans WHERE run_id=? AND version=?", (run_id, plan_version)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["tasks"] = loads(d["tasks_json"], [])
    d["requirements"] = loads(d.get("requirements_json"), {})
    return d


def current_requirements(con: sqlite3.Connection, run_id: str) -> dict:
    row = con.execute("SELECT * FROM requirements WHERE run_id=? ORDER BY version DESC LIMIT 1", (run_id,)).fetchone()
    d = dict(row)
    d["frozen"] = loads(d["frozen_json"], {})
    return d


def active_authorization(con: sqlite3.Connection, run: dict, kind: str = "plan", target: str | None = None) -> dict | None:
    q = ("SELECT * FROM authorizations WHERE run_id=? AND kind=? AND revoked_at IS NULL "
         + ("AND target=? " if target is not None else "")
         + "ORDER BY created_at DESC LIMIT 1")
    args = (run["id"], kind) + ((target,) if target is not None else ())
    row = con.execute(q, args).fetchone()
    if row is None:
        return None
    d = dict(row)
    if kind == "plan" and d.get("requirements_version") != run.get("requirements_version"):
        return None
    return d


# ---------------------------------------------------------------- projections

def write_projection(con: sqlite3.Connection, run_id: str) -> None:
    """Regenerate the read-only JSON view. It is never read back as truth."""
    run = get_run(con, run_id)
    if run is None or not run.get("state_dir"):
        return
    view = {
        "_authority": "runs.db",
        "_note": "Generated read-only view. Editing this file has no effect; use office commands.",
        "run_id": run["id"],
        "office_version": run["office_version"],
        "phase": run["phase"],
        "goal": run["goal"],
        "gear": run["gear"],
        "requirements_version": run["requirements_version"],
        "plan_version": run["plan_version"],
        "routing_version": run["routing_version"],
        "tasks": [
            {"id": t["id"], "status": t["status"], "revision": t["current_revision_id"],
             "accepted_revision": t["accepted_revision_id"]}
            for t in tasks(con, run_id)
        ],
        "updated_at": run["updated_at"],
    }
    try:
        atomic_write_json(Path(run["state_dir"]) / "view.json", view)
    except OSError:
        pass
    # Stat-only index the hook fast path reads instead of opening runs.db.
    if run.get("git_common_dir"):
        active = paths.primary_checkout(Path(run["git_common_dir"])) / ".office" / "active" / run["id"]
        try:
            if is_terminal(run):
                active.unlink(missing_ok=True)
            else:
                from office.util import atomic_write_text
                atomic_write_text(active, f"{run['phase']}\t{(run['goal'] or '')[:80]}\n")
        except OSError:
            pass


def packet_envelope(run: dict, kind: str, body: dict) -> dict:
    """Every runtime-owned packet carries the exact runtime that wrote it; its
    release line must be the run's."""
    packet = {
        "packet_kind": kind,
        "packet_schema": 1,
        "office_version": version.current(),
        "run_id": run["id"],
        "created_at": now_iso(),
        **body,
    }
    packet["packet_hash"] = sha256_obj({k: v for k, v in packet.items() if k not in ("created_at",)})
    return packet


def check_packet(run: dict, packet: dict) -> None:
    """Reject a packet whose Office version is not the run's pinned version,
    and refuse to process any packet under a different runtime."""
    pv = packet.get("office_version")
    if not version.same_line(pv, run["office_version"]):
        raise Refused("packet-version-mismatch",
                      f"packet office_version {pv!r} does not match run {run['id'][:8]} pinned {run['office_version']!r}",
                      next_step="this packet cannot be used; the runtime regenerates packets for the pinned version")
    if not version.same_line(run["office_version"], version.current()):
        raise Refused("runtime-version-mismatch",
                      f"runtime {version.current()} may not process packets for a run pinned to {run['office_version']}",
                      next_step="invoke the pinned runtime through the office front door", exit_code=5)


def state_dir_for(run_id: str) -> Path:
    return paths.run_dir(run_id)


def json_or(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default
