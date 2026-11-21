"""#1297: fastlane, an orchestrator-led way of working on any XS-XL run gear.

Fastlane means the orchestrator leads a task with its own strategy: it implements inline or uses its own
(cheaper) in-session subagents, instead of Office routing and dispatching an executor. One independent code
review is always mandatory. The gear still decides planning and review ceremony; fastlane removes only Office
executor routing.

The policy is pinned per run at `office start` (`gates["fastlane"]`): `on` (the invoker's --fastlane, which
always wins), `off` (--no-fastlane) or `auto` (XS defaults to it; other sizes only when quality, time and cost
all favour it, see `task_sizing.execution_mode`). A run without the gate (every run started before #1297, a
greenfield run without --fastlane, a legacy preset) keeps its original execution semantics. A greenfield run with
--fastlane (#1300) pins `on`: the orchestrator leads the one cohesive build at the XL floor and the convergence
review stays required.

When an orchestrator-led task grows too big (the orchestrator's context crosses the `orchestrator_context`
threshold, or the task touches far more files than its scope planned), the run records an overflow: the rest of
the work goes to Office executors, and the orchestrator compacts itself only where its environment can (Herdr);
elsewhere it gets a one-line suggestion. The overflow never blocks or fails anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from itertools import combinations

VERSION = "fastlane-1"
POLICIES = ("on", "off", "auto")
CHECK_INTERVAL_SECONDS = 60
FILE_FACTOR = 2  # touched files past this multiple of the files the scope planned is "far more files"


def pin(gear: str, flag: bool | None) -> dict | None:
    """The fastlane gate a new run pins at start, or None for a gear with its own execution contract.
    `flag` is the invoker's choice: True (--fastlane), False (--no-fastlane) or None (no flag).
    Greenfield (#1300 G3) pins `on` only for --fastlane; otherwise it keeps its executor contract (None)."""
    from office import config as cfg
    if gear == "greenfield":
        return {"version": VERSION, "policy": "on", "by": "flag"} if flag else None
    if gear not in cfg.RUN_GEARS:
        if flag is not None:
            raise ValueError(f"--{'fastlane' if flag else 'no-fastlane'} applies to the XS-XL run gears; "
                             f"{gear} keeps its own execution contract")
        return None
    return {"version": VERSION, "policy": "auto" if flag is None else ("on" if flag else "off"),
            "by": "default" if flag is None else "flag"}


def gate(run: dict | None) -> dict | None:
    """The run's pinned fastlane gate, or None for a run that predates #1297 or never pinned one."""
    g = ((run or {}).get("gates") or {}).get("fastlane")
    return g if isinstance(g, dict) and g.get("version") == VERSION and g.get("policy") in POLICIES else None


def overflowed(run: dict | None) -> bool:
    g = gate(run)
    return bool(g and g.get("overflow"))


def parallel(tasks: list[dict]) -> bool:
    """True when two live tasks have no dependency path between them, so Office executors could run them side
    by side (the time check fails). One task, or a chain, is not parallel."""
    from office import planfile
    graph = {t["id"]: [d for d in (t.get("depends") or [])] for t in tasks}
    for d in graph.values():
        d[:] = [x for x in d if x in graph]
    above = {tid: planfile.ancestors(graph, tid) for tid in graph}
    return any(a not in above[b] and b not in above[a] for a, b in combinations(graph, 2))


def summary(run: dict | None) -> str | None:
    """One status line for the pinned policy, or None for a run without one."""
    g = gate(run)
    if g is None:
        return None
    text = {"on": "on (--fastlane): the orchestrator leads every task with its own strategy",
            "off": "off (--no-fastlane): Office routes executors",
            "auto": "auto: XS tasks are orchestrator-led; larger ones only when quality, time and cost favour it"}[g["policy"]]
    if g.get("overflow"):
        text += f"; overflowed on {g['overflow'].get('task')} ({g['overflow'].get('reason')}): remaining work goes to executors"
    return "fastlane: " + text + "; independent review always required"


# ------------------------------------------------------------------ overflow (reuses the ctx-high notice)

def _route(task: dict) -> dict:
    raw = task.get("route_json")
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def live_led_task(con, run: dict) -> tuple[dict, dict | None] | None:
    """The live orchestrator-led task and its current dispatch row, if any."""
    from office import state
    for task in state.tasks(con, run["id"]):
        if task["status"] in (*state.TASK_TERMINAL, "superseded"):
            continue
        if ((_route(task).get("execution_mode") or {}).get("mode")) != "inline":
            continue
        row = (con.execute("SELECT * FROM dispatches WHERE id=?", (task["current_dispatch_id"],)).fetchone()
               if task.get("current_dispatch_id") else None)
        return task, (dict(row) if row else None)
    return None


def _herdr(con, run: dict) -> bool:
    if os.environ.get("HERDR_PANE_ID"):
        return True
    try:
        from office import root_wake
        return root_wake._binding(con, run["id"]) is not None
    except Exception:  # an unreadable binding is not evidence of Herdr
        return False


def handoff_text(con, run: dict, task_id: str, reason: str) -> str:
    """What the orchestrator does on overflow: hand the rest to executors, then compact where it can."""
    # Herdr (which is also where rico-mode runs its agents, so its SO81 watcher prompts the same deduped
    # /compactable check) can compact the pane; anywhere else Office can only suggest it.
    compact = ("then run /compactable and compact this pane on COMPACT NOW" if _herdr(con, run) else
               "suggestion: compact this session at the next boundary (Office cannot compact it here; "
               "the run carries on without it)")
    return (f"fastlane {task_id} is too big ({reason}): commit a checkpoint, hand the rest to Office executors "
            f"(office rerun {task_id} now delegates, or split the rest with office amend), {compact}")


def _record(con, run: dict, task_id: str, reason: str, detail: dict) -> dict | None:
    """Pin the overflow on the run's fastlane gate. Caller holds the transaction. None when already recorded."""
    from office import state
    fresh = state.get_run(con, run["id"]) or run
    g = gate(fresh)
    if g is None or g.get("overflow"):
        return None
    overflow = {"task": task_id, "reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **detail}
    gates = {**(fresh.get("gates") or {}), "fastlane": {**g, "overflow": overflow}}
    state.update_run(con, run["id"], gates=gates)
    run.setdefault("gates", {})["fastlane"] = gates["fastlane"]
    return overflow


def on_context_high(con, run: dict, line: str, tokens: int | None, threshold: int) -> tuple[str, dict | None]:
    """Called by ctx_alert inside its transaction when the orchestrator's ctx-high line fires. With a live
    orchestrator-led task the same line carries the fastlane handoff (no second notice)."""
    if gate(run) is None or overflowed(run):
        return line, None
    live = live_led_task(con, run)
    if live is None:
        return line, None
    task = live[0]
    reason = f"orchestrator context {round((tokens or 0) / 1000)}k past the {round(threshold / 1000)}k threshold"
    overflow = _record(con, run, task["id"], reason, {"trigger": "context", "tokens": tokens, "threshold": threshold})
    if overflow is None:
        return line, None
    return f"{line}; {handoff_text(con, run, task['id'], reason)}", overflow


def _git(wt: str, *args: str) -> list[str] | None:
    try:
        out = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split("\0") if p]


def files_reason(task: dict, dispatch: dict | None) -> tuple[str, dict] | None:
    """Why the led task touches far more files than planned: any path outside its scope, or more than
    FILE_FACTOR x the base-tree files its scope matches. None when within plan or unmeasurable."""
    from office import planfile, submit
    wt, base = (dispatch or {}).get("worktree"), (dispatch or {}).get("base_commit")
    scope = task.get("scope") or []
    if not wt or not base or not scope or not os.path.isdir(wt):
        return None
    changed = _git(wt, "diff", "--no-renames", "--name-only", "-z", base)
    untracked = _git(wt, "ls-files", "--others", "--exclude-standard", "-z")
    planned_tree = _git(wt, "ls-tree", "-r", "--name-only", "-z", base)
    if changed is None or untracked is None or planned_tree is None:
        return None
    touched = sorted({f for f in (*changed, *untracked) if not submit._harness_path(f)})
    outside = [f for f in touched if not planfile.path_in_scope(f, scope)]
    if outside:
        return (f"{len(outside)} file(s) outside {task['id']} scope: {', '.join(outside[:3])}",
                {"trigger": "files", "touched": len(touched), "outside": outside[:10]})
    planned = sum(1 for f in planned_tree if planfile.path_in_scope(f, scope))
    if planned and len(touched) > FILE_FACTOR * planned:
        return (f"{len(touched)} files touched, more than {FILE_FACTOR}x the {planned} its scope planned",
                {"trigger": "files", "touched": len(touched), "planned": planned})
    return None


def _throttle_path(run_id: str):
    from office import paths
    return paths.run_dir(run_id) / "fastlane-watch.state"


def watch_files(con, run: dict, *, now: float | None = None) -> str | None:
    """The file trigger, throttled like the ctx-high check. Emits one ctx.high event (the existing notice and
    wake path) carrying the handoff when it fires. Never raises."""
    try:
        if gate(run) is None or overflowed(run):
            return None
        live = live_led_task(con, run)
        if live is None:
            return None
        checked = time.time() if now is None else now
        path = _throttle_path(run["id"])
        try:
            last = float(path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        if checked - last < CHECK_INTERVAL_SECONDS:
            return None
        from office.util import atomic_write_text
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, f"{checked:.3f}\n", mode=0o600)
        task, dispatch = live
        found = files_reason(task, dispatch)
        if found is None:
            return None
        reason, detail = found
        from office import db, state
        with db.transaction(con):
            overflow = _record(con, run, task["id"], reason, detail)
            if overflow is None:
                return None
            line = "ctx-high: " + handoff_text(con, run, task["id"], reason)
            state.emit(con, run, "ctx.high", line, payload={"fastlane_overflow": overflow})
        return line
    except Exception:  # advisory only: a watch failure never breaks status, wait or wake
        return None


def delegate_after_overflow(mode: dict) -> dict:
    """A kept orchestrator-led execution mode, turned into a delegation once the run overflowed."""
    return {**mode, "mode": "delegated", "owner": "executor",
            "why": "dedicated executor: fastlane overflowed, so the remaining work goes to Office executors"}
