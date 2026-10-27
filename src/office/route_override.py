"""Authenticated one-off user override of a role deny (#837).

A `--quote` is text an orchestrator can write, so it never overrides a policy
deny. A receipt is issued only by a person at a terminal (`office route
override`): it is bound to one run, one role and one exact route, is consumed by the
first dispatch that records it, expires quickly, is HMAC-signed with a per-user key, and every use is appended to an
audit table. A receipt overrides policy only. It never widens capability,
sandbox, reviewer-independence or landing checks, which run after it.

Threat model: this stops an orchestrator or repo from impersonating the user
through the CLI, config or text. A process with the user's OS account can read
the key file and sign its own receipt; that boundary belongs to the OS, not to
Office.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from office import db, paths, route_policy
from office.state import Refused, Usage
from office.util import now_iso

DEFAULT_TTL_S = 1800
MAX_TTL_S = 24 * 3600
_DDL = (
    "CREATE TABLE IF NOT EXISTS route_override_receipts("
    "id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, role TEXT NOT NULL, "
    "route TEXT NOT NULL, reason TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
    "mac TEXT NOT NULL, revoked_at TEXT)",
    "CREATE TABLE IF NOT EXISTS route_override_claims("
    "receipt_id TEXT PRIMARY KEY, scope TEXT NOT NULL, claimed_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS route_override_uses("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL, used_at TEXT NOT NULL, context TEXT)",
)


def ensure_schema(con: sqlite3.Connection) -> None:
    for ddl in _DDL:
        con.execute(ddl)


def _key() -> bytes:
    path = paths.user_config_path().parent / "override.key"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
    return path.read_bytes()


def _mac(receipt: dict) -> str:
    body = "|".join(str(receipt[k]) for k in ("id", "nonce", "run_id", "role", "route", "issued_at", "expires_at"))
    return hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()


def parse_ttl(text: str | None) -> int:
    if not text:
        return DEFAULT_TTL_S
    unit = {"s": 1, "m": 60, "h": 3600}.get(text[-1:])
    try:
        seconds = int(text[:-1]) * unit if unit else int(text)
    except (TypeError, ValueError):
        raise Usage("invalid-ttl", f"--ttl {text!r}: expected e.g. 30m, 2h or seconds") from None
    if not 60 <= seconds <= MAX_TTL_S:
        raise Usage("invalid-ttl", "--ttl must be between 1m and 24h")
    return seconds


def require_user_terminal() -> None:
    """Only a person at a terminal issues a receipt: a worker, a pipe or a script cannot."""
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-override", "a worker cannot issue a user override")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise Refused("user-terminal-required",
                      "a policy override must be issued by the user at an interactive terminal; a quote or "
                      "an orchestrator command cannot create one",
                      next_step="the user runs: office route override <harness/model@effort> --role <role> --run <run> "
                                "--reason \"<why>\" in their own terminal")


def _confirm(summary: str) -> bool:
    print(summary)
    return input("type 'authorize' to issue this override: ").strip().lower() == "authorize"


def issue(con: sqlite3.Connection, *, run_id: str, role: str, route: str, reason: str, ttl_s: int,
          confirm=_confirm, terminal_check=require_user_terminal) -> dict:
    from office import candidates, state
    terminal_check()
    if role not in route_policy.POLICY_ROLES:
        raise Usage("unknown-role", f"unknown routing role {role!r}")
    if not reason or len(reason.strip()) < 3:
        raise Usage("reason-required", "state why this denied route should run (--reason)")
    want = candidates.parse_route_override(route)
    if not want["harness"] or not want["model_id"] or not want["effort"]:
        raise Usage("invalid-route", "an override names one exact route: <harness>/<model>@<effort>")
    run = state.find_run(con, run_id)
    if run is None:
        raise Usage("unknown-run", f"no run {run_id!r}")
    now = datetime.now(timezone.utc)
    receipt = {"id": "O" + secrets.token_hex(4), "nonce": secrets.token_hex(16), "run_id": run["id"], "role": role,
               "route": f"{want['harness']}/{want['model_id']}@{want['effort']}", "reason": reason.strip(),
               "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "expires_at": (now + timedelta(seconds=ttl_s)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    summary = (f"override a role deny: {receipt['route']} as {role} in run {run['id'][:8]} for "
               f"{ttl_s // 60} min. Capability, sandbox, reviewer independence and landing checks still apply.")
    if not confirm(summary):
        raise Refused("override-declined", "the override was not confirmed")
    receipt["mac"] = _mac(receipt)
    with db.transaction(con):
        ensure_schema(con)
        con.execute("INSERT INTO route_override_receipts(id, nonce, run_id, role, route, reason, issued_at, expires_at, mac) "
                    "VALUES(:id,:nonce,:run_id,:role,:route,:reason,:issued_at,:expires_at,:mac)", receipt)
    return receipt


def revoke(con: sqlite3.Connection, receipt_id: str) -> bool:
    with db.transaction(con):
        ensure_schema(con)
        cur = con.execute("UPDATE route_override_receipts SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                          (now_iso(), receipt_id))
    return cur.rowcount > 0


def list_receipts(con: sqlite3.Connection, run_id: str | None = None) -> list[dict]:
    ensure_schema(con)
    sql = "SELECT * FROM route_override_receipts" + (" WHERE run_id=?" if run_id else "") + " ORDER BY issued_at DESC"
    return [dict(r) for r in con.execute(sql, (run_id,) if run_id else ())]


def _route_matches(route: str, cand: dict) -> bool:
    spec, _, effort = route.rpartition("@")
    harness, _, model = spec.partition("/")
    models = {cand.get(k) for k in ("model_id", "invocation_model_id", "alias_resolved_to")}
    return harness == cand.get("harness") and model in models and effort == cand.get("effort")


def find_valid(run_id: str | None, role: str, cand: dict, *, con: sqlite3.Connection | None = None) -> dict | None:
    """The unexpired, unrevoked, correctly signed receipt for exactly this run, role and route."""
    if not run_id:
        return None
    own = con is None
    try:
        con = con or db.connect()
        ensure_schema(con)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = con.execute("SELECT * FROM route_override_receipts WHERE run_id=? AND role=? AND revoked_at IS NULL "
                           "AND expires_at>? ORDER BY issued_at DESC", (run_id, role, now)).fetchall()
        for row in rows:
            receipt = dict(row)
            if hmac.compare_digest(receipt["mac"], _mac(receipt)) and _route_matches(receipt["route"], cand):
                if _launched(con, receipt):
                    continue  # one-off: a dispatch that carries this receipt consumed it
                return receipt
        return None
    except sqlite3.Error:
        return None  # an unreadable store never grants an override
    finally:
        if own and con is not None:
            con.close()


def _launched(con: sqlite3.Connection, receipt: dict) -> bool:
    """True once any dispatch of the run recorded this receipt id in its route (launch consumes it)."""
    try:
        like = f"%{receipt['id']}%"
        return any(con.execute(f"SELECT 1 FROM {table} WHERE run_id=? AND route_json LIKE ? LIMIT 1",
                               (receipt["run_id"], like)).fetchone() for table in ("dispatches", "gates"))
    except sqlite3.Error:
        return True  # an unreadable dispatch table never grants an override


def available_for(receipt: dict, scope: str | None, *, con: sqlite3.Connection | None = None) -> bool:
    """True when no other scope has claimed the receipt (a read-only check for validation and inspection)."""
    own = con is None
    try:
        con = con or db.connect()
        ensure_schema(con)
        row = con.execute("SELECT scope FROM route_override_claims WHERE receipt_id=?", (receipt["id"],)).fetchone()
        return row is None or row[0] == (scope or "")
    except sqlite3.Error:
        return False
    finally:
        if own and con is not None:
            con.close()


def record_use(receipt: dict, context: str, *, scope: str | None = None, con: sqlite3.Connection | None = None) -> bool:
    """Atomically claim the receipt for one scope (a task), then append the use to the audit table.

    The first claim wins; another scope (a concurrent dispatch of a different task) is refused, while the
    same scope may re-validate. False (no override) when it cannot be claimed or recorded."""
    own = con is None
    try:
        con = con or db.connect()
        ensure_schema(con)
        # The claim re-checks revocation and expiry in the same statement, so a revoke that lands
        # between lookup and claim wins.
        con.execute("INSERT OR IGNORE INTO route_override_claims(receipt_id, scope, claimed_at) "
                    "SELECT id, ?, ? FROM route_override_receipts WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                    (scope or "", now_iso(), receipt["id"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        winner = con.execute("SELECT scope FROM route_override_claims WHERE receipt_id=?", (receipt["id"],)).fetchone()
        if winner is None or winner[0] != (scope or ""):
            return False
        # An earlier same-scope claim is still only valid while the receipt is unrevoked and unexpired.
        if con.execute("SELECT 1 FROM route_override_receipts WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                       (receipt["id"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone() is None:
            return False
        if not con.execute("SELECT 1 FROM route_override_uses WHERE receipt_id=? AND context=?",
                           (receipt["id"], context)).fetchone():
            con.execute("INSERT INTO route_override_uses(receipt_id, used_at, context) VALUES(?,?,?)",
                        (receipt["id"], now_iso(), context))
        return True
    except sqlite3.Error:
        return False
    finally:
        if own and con is not None:
            con.close()
