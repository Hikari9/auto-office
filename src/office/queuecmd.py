"""`office queue`: the machine-level scheduler queue.

Items are queued issues (`issue`), whole runs (`run`) or one task of a run
(`task`). A run started from a terminal has no item until an operator acts on
it; `list` projects it as active work and `pause --run` creates its item.

Pausing demotes an item to the end of the ready order. When it was the last
runnable work of its run (or, for queued issues, of the whole queue), that
run's (or the global) auto mode becomes `paused`; only an explicit `resume`
or `queue auto on` turns it back on.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from office import commands, config as cfg, db, scheduler, state
from office.result import Result
from office.state import Refused, Usage
from office.util import now_iso

GLOBAL = "global"
PRIORITIES = tuple(scheduler.PRIORITY_WEIGHTS)


def _scope(run_id: str | None) -> str:
    return f"run:{run_id}" if run_id else GLOBAL


def _settings() -> dict:
    effective, _ = cfg.resolve(None)
    return effective


def auto_mode(con: sqlite3.Connection, run_id: str | None = None, *, default_on: bool = True) -> str:
    """The effective auto mode: the global switch first, then the run's own."""
    modes = {r["scope"]: r["auto_mode"] for r in con.execute(
        "SELECT scope, auto_mode FROM sched_state WHERE scope IN (?, ?)", (GLOBAL, _scope(run_id)))}
    glob = modes.get(GLOBAL, "on" if default_on else "off")
    if glob != "on" or not run_id:
        return glob
    return modes.get(_scope(run_id), "on")


def _set_auto(con: sqlite3.Connection, run_id: str | None, mode: str, reason: str) -> None:
    con.execute("INSERT OR REPLACE INTO sched_state(scope, auto_mode, reason, updated_at) VALUES(?,?,?,?)",
                (_scope(run_id), mode, reason, now_iso()))


def _emit(con, run_id: str | None, kind: str, summary: str, payload: dict) -> None:
    if run_id:
        state.emit(con, {"id": run_id}, kind, summary, payload=payload)


def _item_id(run_id: str, task_id: str | None) -> str:
    return f"task:{run_id}:{task_id}" if task_id else f"run:{run_id}"


def _get(con, item_id: str) -> dict | None:
    return db.row_dict(con.execute("SELECT * FROM sched_items WHERE id=?", (item_id,)).fetchone())


def _ensure(con, run_id: str, task_id: str | None) -> dict:
    item_id = _item_id(run_id, task_id)
    if _get(con, item_id) is None:
        at = now_iso()
        con.execute("INSERT INTO sched_items(id, kind, run_id, task_id, enqueued_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (item_id, "task" if task_id else "run", run_id, task_id, at, at))
    return _get(con, item_id)


def _resolve(con, item: str | None, run_arg: str | None, task: str | None, *, create: bool) -> dict:
    """The item named by id (or unique prefix), or by --run [--task]."""
    if item:
        rows = con.execute("SELECT * FROM sched_items WHERE id=? OR id LIKE ?", (item, item + "%")).fetchall()
        exact = [r for r in rows if r["id"] == item]
        if exact or len(rows) == 1:
            return dict((exact or rows)[0])
        if rows:
            raise Usage("ambiguous-item", f"{item!r} matches {len(rows)} queue items", next_step="office queue list")
        raise Usage("unknown-item", f"no queue item {item!r}", next_step="office queue list")
    if not run_arg:
        raise Usage("usage", "name a queue item, or --run <id> [--task <T>]", next_step="office queue list")
    run = state.find_run(con, run_arg)
    if run is None:
        raise Usage("unknown-run", f"no run {run_arg!r}", next_step="office list --all")
    if task and state.get_task(con, run["id"], task) is None:
        raise Usage("unknown-task", f"{task} is not a task of run {run['id'][:8]}")
    if create:
        return _ensure(con, run["id"], task)
    found = _get(con, _item_id(run["id"], task))
    if found is None:
        raise Usage("unknown-item", f"run {run['id'][:8]}{' ' + task if task else ''} has no queue item",
                    next_step="office queue list")
    return found


def _next_demotion(con) -> int:
    return (con.execute("SELECT MAX(demoted_seq) FROM sched_items").fetchone()[0] or 0) + 1


def _runnable_left(con, item: dict) -> bool:
    """Whether the paused item's run (or the issue queue) still has runnable work."""
    if not item["run_id"]:
        return con.execute("SELECT 1 FROM sched_items WHERE kind='issue' AND paused=0").fetchone() is not None
    if item["task_id"] is None:
        return False  # the whole run is paused
    paused = {r["task_id"] for r in con.execute(
        "SELECT task_id FROM sched_items WHERE run_id=? AND kind='task' AND paused=1", (item["run_id"],))}
    return any(t["id"] not in paused and t["status"] not in state.TASK_TERMINAL
               for t in state.tasks(con, item["run_id"]))


def resume_command(item: dict) -> str:
    if item["kind"] == "issue":
        return f"office queue resume {item['id']}"
    return f"office queue resume --run {item['run_id'][:8]}" + (f" --task {item['task_id']}" if item["task_id"] else "")


def paused_block(con: sqlite3.Connection, run_id: str, task_id: str | None) -> dict | None:
    """The operator-paused item that holds this run or task, if any."""
    ids = [_item_id(run_id, None)] + ([_item_id(run_id, task_id)] if task_id else [])
    row = con.execute(f"SELECT * FROM sched_items WHERE paused=1 AND id IN ({','.join('?' * len(ids))}) "
                      "ORDER BY task_id IS NOT NULL", ids).fetchone()
    return db.row_dict(row)


def pause(con, *, item: str | None = None, run_arg: str | None = None, task: str | None = None,
          reason: str | None = None) -> Result:
    with db.transaction(con):
        it = _resolve(con, item, run_arg, task, create=True)
        at = now_iso()
        con.execute("UPDATE sched_items SET paused=1, pause_reason=?, paused_at=?, demoted_seq=?, updated_at=? "
                    "WHERE id=?", (reason or "operator", at, _next_demotion(con), at, it["id"]))
        it = _get(con, it["id"])
        auto = None
        if not _runnable_left(con, it):
            auto = "run" if it["run_id"] else "global"
            _set_auto(con, it["run_id"], "paused", f"last runnable work paused ({it['id']})")
        _emit(con, it["run_id"], "queue.paused", f"queue: paused {it['id']}",
              {"item": it["id"], "auto_paused": auto})
    res = Result(lines=[f"paused {it['id']}" + (f"; {auto} auto mode paused" if auto else "")],
                 next=resume_command(it), data={"item": it, "auto_paused": auto})
    return res


def resume(con, *, item: str | None = None, run_arg: str | None = None, task: str | None = None) -> Result:
    with db.transaction(con):
        it = _resolve(con, item, run_arg, task, create=False)
        con.execute("UPDATE sched_items SET paused=0, pause_reason=NULL, paused_at=NULL, updated_at=? WHERE id=?",
                    (now_iso(), it["id"]))
        _set_auto(con, it["run_id"], "on", f"resumed {it['id']}")
        _emit(con, it["run_id"], "queue.resumed", f"queue: resumed {it['id']}", {"item": it["id"]})
        it = _get(con, it["id"])
    return Result(lines=[f"resumed {it['id']}"], next="office queue list", data={"item": it})


def set_priority(con, item: str | None, level: str | None, *, run_arg=None, task=None) -> Result:
    if level not in PRIORITIES:
        raise Usage("usage", f"priority must be one of {', '.join(PRIORITIES)}")
    with db.transaction(con):
        it = _resolve(con, item, run_arg, task, create=True)
        con.execute("UPDATE sched_items SET priority=?, updated_at=? WHERE id=?", (level, now_iso(), it["id"]))
        _emit(con, it["run_id"], "queue.priority", f"queue: {it['id']} priority {level}",
              {"item": it["id"], "priority": level})
    return Result(lines=[f"{it['id']} priority {level}"], next="office queue list", data={"item": _get(con, it["id"])})


def demote(con, item: str | None, *, run_arg=None, task=None) -> Result:
    with db.transaction(con):
        it = _resolve(con, item, run_arg, task, create=True)
        con.execute("UPDATE sched_items SET demoted_seq=?, updated_at=? WHERE id=?",
                    (_next_demotion(con), now_iso(), it["id"]))
        _emit(con, it["run_id"], "queue.demoted", f"queue: demoted {it['id']}", {"item": it["id"]})
    return Result(lines=[f"demoted {it['id']}"], next="office queue list", data={"item": _get(con, it["id"])})


def auto(con, mode: str | None, *, run_arg: str | None = None) -> Result:
    run = None
    if run_arg:
        run = state.find_run(con, run_arg)
        if run is None:
            raise Usage("unknown-run", f"no run {run_arg!r}", next_step="office list --all")
    run_id = run["id"] if run else None
    if mode in (None, "status"):
        current = auto_mode(con, run_id, default_on=scheduler.settings(_settings())["auto_mode"])
        return Result(lines=[f"auto mode {current}" + (f" (run {run_id[:8]})" if run_id else "")],
                      data={"scope": _scope(run_id), "auto_mode": current})
    if mode not in ("on", "off"):
        raise Usage("usage", "office queue auto [on|off|status] [--run <id>]")
    with db.transaction(con):
        _set_auto(con, run_id, mode, "operator")
        _emit(con, run_id, "queue.auto", f"queue: auto mode {mode}", {"auto_mode": mode})
    return Result(lines=[f"auto mode {mode}" + (f" (run {run_id[:8]})" if run_id else "")],
                  next="office queue list", data={"scope": _scope(run_id), "auto_mode": mode})


def add(con, ref: str | None, *, title: str | None = None, priority: str = "normal",
        command_id: str | None = None) -> Result:
    if not ref:
        raise Usage("usage", "office queue add <issue-ref> [--title T] [--priority P]")
    if priority not in PRIORITIES:
        raise Usage("usage", f"priority must be one of {', '.join(PRIORITIES)}")
    command_id = command_id or uuid.uuid4().hex
    payload = {"ref": ref, "title": title, "priority": priority}
    receipt = commands.record(con, command_id=command_id, kind="queue.add", target=ref, payload=payload,
                              origin="cli")
    item_id = f"issue:{command_id[:12]}"
    if receipt["replayed"]:
        return Result(lines=[f"already queued {item_id} ({receipt['status']})"], next="office queue list",
                      data={"item": _get(con, item_id), "receipt": receipt})
    commands.transition(con, command_id, "running")
    try:
        with db.transaction(con):
            at = now_iso()
            con.execute("INSERT INTO sched_items(id, kind, ref, title, priority, enqueued_at, updated_at) "
                        "VALUES(?,'issue',?,?,?,?,?)", (item_id, ref, title, priority, at, at))
    except Exception as exc:
        commands.transition(con, command_id, "failed", error=str(exc))
        raise
    receipt = commands.transition(con, command_id, "completed", result={"item": item_id})
    return Result(lines=[f"queued {item_id} {ref}"], next="office queue list",
                  data={"item": _get(con, item_id), "receipt": receipt})


def _projection(con, config: dict) -> tuple[list[dict], list[dict]]:
    """Queue items plus every live run that has no run item (a terminal-started run)."""
    default_on = scheduler.settings(config)["auto_mode"]
    rows = [dict(r) for r in con.execute("SELECT * FROM sched_items")]
    have_run_item = {r["run_id"] for r in rows if r["kind"] == "run"}
    live = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, phase, goal FROM runs WHERE phase IS NOT NULL AND phase NOT IN (?, ?)", state.TERMINAL_PHASES)}
    for run_id, run in live.items():
        if run_id not in have_run_item:
            rows.append({"id": _item_id(run_id, None), "kind": "run", "run_id": run_id, "task_id": None,
                         "ref": None, "title": run.get("goal"), "priority": "normal", "paused": 0,
                         "demoted_seq": None, "enqueued_at": None, "projected": True})
    blocks: dict[tuple, int] = {}
    for run_id in {r["run_id"] for r in rows if r["kind"] == "task"}:
        for t in state.tasks(con, run_id):
            for dep in t.get("depends") or []:
                blocks[(run_id, dep)] = blocks.get((run_id, dep), 0) + 1
    active = []
    for r in rows:
        r["paused"] = bool(r["paused"])
        r["blocks"] = blocks.get((r["run_id"], r["task_id"]), 0)
        r["protected"] = r["kind"] == "run" and r["run_id"] in live and not r["paused"]
        r["auto_mode"] = auto_mode(con, r["run_id"], default_on=default_on)
        if r["kind"] == "run" and r["run_id"] in live:
            active.append({"id": r["id"], "state": "paused" if r["paused"] else "running"})
    return rows, active


def list_(con, *, host: dict | None = None, quota: dict | None = None, config: dict | None = None,
          now: datetime | None = None) -> Result:
    from office import hostmetrics
    config = _settings() if config is None else config
    rows, active = _projection(con, config)
    plan = scheduler.plan_admission(rows, active, hostmetrics.host() if host is None else host, quota or {},
                                    config, now or datetime.now(timezone.utc))
    lines = [f"{e['id']}  {e['priority']}  score {e['score']['total']:g}  {e['decision']}: {e['reason']}"
             + (" [projected]" if e.get("projected") else "") for e in plan["entries"]]
    lines.append(f"active {plan['active']}; cpu {plan['host']['cpu']}, ram {plan['host']['ram']}; "
                 f"auto mode {auto_mode(con, default_on=scheduler.settings(config)['auto_mode'])}")
    return Result(lines=lines if plan["entries"] else ["queue empty"] + lines[-1:], next="office queue add <issue>",
                  data=plan)


def run_command(con, args) -> Result:
    action, target, value = args.action, args.target, args.value
    run_arg, task = getattr(args, "run_arg", None), args.task
    if action == "list":
        return list_(con)
    if action == "add":
        return add(con, target, title=args.title, priority=args.priority or "normal", command_id=args.command_id)
    if action == "pause":
        return pause(con, item=target, run_arg=run_arg, task=task, reason=args.reason)
    if action == "resume":
        return resume(con, item=target, run_arg=run_arg, task=task)
    if action == "priority":
        if value is None and target in PRIORITIES and run_arg:
            target, value = None, target
        return set_priority(con, target, value or args.priority, run_arg=run_arg, task=task)
    if action == "demote":
        return demote(con, target, run_arg=run_arg, task=task)
    if action == "auto":
        return auto(con, target, run_arg=run_arg)
    raise Refused("usage", f"unknown queue action {action!r}")
