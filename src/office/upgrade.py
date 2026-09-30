"""Move a run across a MINOR or MAJOR release line (#223).

A run is pinned to MAJOR.MINOR, and PATCH releases on that line reach it with
no action. Crossing a line is explicit: `office upgrade [run] [--to X.Y]`
dry-runs by default and `--apply` commits. It refuses while a dispatch or job
is live, restamps queued jobs, runs any registered line migrations, and
records `run.upgraded` with the from/to lines. Requirements, plans, tasks,
gates, amendments and authorizations are not touched. The rollback path is the
same command with `--to <previous line>`, allowed only to a line the run was
on before.
"""
from __future__ import annotations

from office import db, frontdoor, state, version
from office.result import Result
from office.state import Refused
from office.util import dumps, loads, now_iso, short

# (from_line, to_line) -> fn(con, run). Runs inside the upgrade transaction,
# after the additive runs.db migration `db.connect` already applied. 3.1 -> 3.2
# needs none: every 3.2 column is nullable and additive.
MIGRATIONS: dict[tuple[str, str], object] = {}


def _prior_lines(con, run_id: str) -> set[str]:
    rows = con.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='run.upgraded'", (run_id,)).fetchall()
    return {(loads(r["payload_json"]) or {}).get("from") for r in rows} - {None}


def blockers(con, run_id: str) -> list[str]:
    """Live dispatches and claimed jobs, named, that an upgrade must not strand."""
    out = [f"dispatch {r['id']} ({r['kind'] or 'agent'} {r['task_id'] or ''}, {r['status']})".replace(" ,", ",")
           for r in con.execute("SELECT id, kind, task_id, status FROM dispatches WHERE run_id=? AND ended_at IS NULL "
                                "AND status IN ('launching','running') ORDER BY started_at", (run_id,)).fetchall()]
    out += [f"job {r['id']} ({r['kind']}, claimed)"
            for r in con.execute("SELECT id, kind FROM outbox WHERE run_id=? AND status='claimed'", (run_id,)).fetchall()]
    return out


def upgrade(con, run: dict, *, to: str | None = None, apply: bool = False) -> Result:
    run = state.get_run(con, run["id"])
    rid = short(run["id"])
    src = version.release_line(run["office_version"])
    installed = frontdoor.installed_lines()
    dest = version.release_line(to) if to else version.release_line(version.current())
    if state.is_terminal(run):
        raise Refused("run-terminal", f"run {rid} is {run['phase']}; only an active run is upgraded",
                      next_step="office list")
    if dest == src:
        return Result(lines=[f"run {rid} is already on Auto Office {src}; no change"],
                      data={"run_id": run["id"], "from": src, "to": dest, "changed": False})
    if dest not in installed:
        raise Refused("target-not-installed", f"Auto Office {dest} is not installed here (installed: {', '.join(installed)})",
                      next_step=f"install a {dest}.x runtime and run office install from it")
    if version.release_key(dest) < version.release_key(src) and dest not in _prior_lines(con, run["id"]):
        raise Refused("downgrade-refused", f"run {rid} was never on {dest}; a downgrade only rolls back an upgrade",
                      next_step="office inspect events to see the run's upgrade history")
    if not version.same_line(dest, version.current()):
        # Only the destination runtime may commit the run to its line.
        frontdoor.ensure_runtime({**run, "office_version": dest})
    live = blockers(con, run["id"])
    if live:
        raise Refused("live-dispatches", f"run {rid} has {len(live)} live dispatch(es) or job(s); upgrade would strand them",
                      scope=f"run {rid}", preserved="all run state; nothing was changed",
                      next_step=f"wait for them to finish (office status --run {rid}), then office upgrade {rid}",
                      data={"candidates": live})
    queued = con.execute("SELECT COUNT(*) FROM outbox WHERE run_id=? AND status='queued'", (run["id"],)).fetchone()[0]
    migration = MIGRATIONS.get((src, dest))
    plan = [f"run {rid}: Auto Office {src} -> {dest} (runtime {version.current()})",
            f"restamp {queued} queued job(s) to {dest}",
            f"migration: {'registered ' + src + '->' + dest if migration else 'none needed (runs.db schema is additive)'}",
            "carried over unchanged: requirements, plans, tasks, gates, amendments, authorizations",
            f"rollback: office upgrade {rid} --to {src} --apply"]
    data = {"run_id": run["id"], "from": src, "to": dest, "queued_jobs": queued, "changed": apply}
    if not apply:
        return Result(lines=["dry run; nothing changed", *plan], next=f"office upgrade {rid} --to {dest} --apply", data=data)
    ver = version.current()
    with db.transaction(con):
        if blockers(con, run["id"]):  # re-check under the write lock
            raise Refused("live-dispatches", f"run {rid} gained a live dispatch; nothing was changed",
                          next_step=f"office upgrade {rid} --apply once it finishes")
        for job in con.execute("SELECT id, payload_json FROM outbox WHERE run_id=? AND status='queued'",
                               (run["id"],)).fetchall():
            payload = loads(job["payload_json"]) or {}
            payload["office_version"] = ver
            con.execute("UPDATE outbox SET office_version=?, payload_json=? WHERE id=?", (ver, dumps(payload), job["id"]))
        if migration:
            migration(con, run)
        con.execute("UPDATE runs SET office_version=?, updated_at=? WHERE id=?", (dest, now_iso(), run["id"]))
        run = state.get_run(con, run["id"])
        state.emit(con, run, "run.upgraded", f"upgraded from Auto Office {src} to {dest}",
                   payload={"from": src, "to": dest, "runtime": ver, "queued_jobs": queued,
                            "rollback": f"office upgrade {rid} --to {src} --apply"})
    state.write_projection(con, run["id"])
    return Result(lines=[f"upgraded run {rid}: Auto Office {src} -> {dest}", *plan[1:]], data=data)


def notice(run: dict) -> str | None:
    """One line when a newer release line than the run's is installed."""
    line = version.release_line(run.get("office_version") or "")
    newest = frontdoor.installed_lines()[0]
    if line and version.release_key(newest) > version.release_key(line):
        return f"run {short(run['id'])} is on Auto Office {line}; {newest} is installed: office upgrade {short(run['id'])}"
    return None

