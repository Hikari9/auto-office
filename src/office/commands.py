"""Command receipts: one row per client request, keyed by its idempotency id.

A receipt is `accepted` when recorded, `running` while an executor process
holds it, then `completed`, `failed` or `unknown`. `unknown` means the
executor vanished mid-flight: the effect may or may not have happened, so the
receipt is never marked failed and never retried automatically.
"""
from __future__ import annotations

import sqlite3

from office import db
from office.state import Refused
from office.util import dumps, now_iso, sha256_obj

TRANSITIONS = {
    "accepted": ("running",),
    "running": ("completed", "failed", "unknown"),
}


def get(con: sqlite3.Connection, command_id: str) -> dict | None:
    return db.row_dict(con.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())


def record(con: sqlite3.Connection, *, command_id: str, kind: str, target: str | None, payload: dict,
           origin: str | None) -> dict:
    """Insert an `accepted` receipt, or return the existing one for a repeat.

    The caller executes only when the returned receipt's `replayed` is False.
    """
    digest = sha256_obj({"kind": kind, "target": target, "payload": payload})
    with db.transaction(con):
        existing = get(con, command_id)
        if existing is not None:
            if existing["payload_hash"] != digest:
                raise Refused("idempotency-conflict",
                              f"command id {command_id} was already used for a different request",
                              next_step="retry with a new command id")
            return {**existing, "replayed": True}
        con.execute("INSERT INTO commands(id, kind, target, payload_json, payload_hash, origin, status, accepted_at) "
                    "VALUES(?,?,?,?,?,?,'accepted',?)",
                    (command_id, kind, target, dumps(payload), digest, origin, now_iso()))
        return {**get(con, command_id), "replayed": False}


def transition(con: sqlite3.Connection, command_id: str, status: str, *, pid: int | None = None,
               result: dict | None = None, error: str | None = None) -> dict:
    with db.transaction(con):
        row = get(con, command_id)
        if row is None:
            raise Refused("unknown-command", f"no command receipt {command_id}")
        if status not in TRANSITIONS.get(row["status"], ()):
            raise Refused("invalid-transition", f"command {command_id} is {row['status']}; it cannot become {status}")
        at = now_iso()
        if status == "running":
            con.execute("UPDATE commands SET status='running', pid=?, started_at=? WHERE id=?", (pid, at, command_id))
        else:
            con.execute("UPDATE commands SET status=?, result_json=?, error=?, finished_at=? WHERE id=?",
                        (status, dumps(result) if result is not None else None, error, at, command_id))
        return get(con, command_id)


def recover_interrupted(con: sqlite3.Connection, alive_pids) -> list[str]:
    """Mark `running` receipts whose executor is gone as `unknown`; return their ids."""
    alive = set(alive_pids)
    with db.transaction(con):
        lost = [r["id"] for r in con.execute("SELECT id, pid FROM commands WHERE status='running'")
                if r["pid"] not in alive]
        at = now_iso()
        for command_id in lost:
            con.execute("UPDATE commands SET status='unknown', error='executor process ended before completion', "
                        "finished_at=? WHERE id=?", (at, command_id))
    return lost
