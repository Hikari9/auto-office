"""#917: a nonblocking `oversize:` signal when a task's current dispatch outgrows its size class.

Observed context (largest single turn), wall time and known cost are compared with `task_sizing.BUDGETS`. The
signal names the options (split, resize, fresh rerun) and takes none: the orchestrator decides (as T4).
Read-only. A measurement that is not recorded is skipped, never read as zero.
"""
from __future__ import annotations

from office import economics, task_sizing
from office.util import now_iso, parse_iso

OPEN = ("launching", "running", "submitted", "changes_required", "idle")


def _turn_context(row: dict) -> int | None:
    parts = [row[f] for f in ("input_tokens", "cache_read_tokens", "cache_write_tokens")]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def _wall(dispatch: dict) -> float | None:
    if not dispatch["started_at"]:
        return None
    try:
        return max(0.0, (parse_iso(dispatch["ended_at"] or now_iso()) - parse_iso(dispatch["started_at"])).total_seconds())
    except (ValueError, TypeError):
        return None  # an unreadable or mixed naive/aware timestamp is not a measurement


def _k(tokens: float) -> str:
    return f"{round(tokens / 1000)}k"


def size_of(task: dict) -> str:
    size = (task.get("descriptor") or {}).get("task_size")
    return size if size in task_sizing.SIZES else task_sizing.classify(task)["task_size"]


def exceeded(task: dict, dispatch: dict, rows: list[dict]) -> list[str]:
    """The measures of this dispatch past the factor times its size class budget, as short phrases."""
    size = size_of(task)
    budget, factor = task_sizing.BUDGETS[size], task_sizing.OVERSIZE_FACTOR
    out = []
    contexts = [c for c in map(_turn_context, rows) if c is not None]
    if contexts and max(contexts) > factor * budget["context"]:
        out.append(f"context {_k(max(contexts))} > {factor:g}x {_k(budget['context'])}")
    wall = _wall(dispatch)
    if wall is not None and wall > factor * budget["wall"]:
        out.append(f"wall {wall / 60:.0f}m > {factor:g}x {budget['wall'] / 60:.0f}m")
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None and r["cost_kind"] in ("actual", "estimated-nominal")]
    if costs and sum(costs) > factor * budget["cost"]:
        out.append(f"cost ${sum(costs):.2f} > {factor:g}x ${budget['cost']:.2f}")
    return out


def lines(con, run: dict, tasks: list[dict]) -> tuple[list[str], str | None]:
    """(`oversize:` status lines, next-step text) for the open tasks whose current dispatch is oversized."""
    out, ids = [], []
    for t in tasks:
        did = t.get("current_dispatch_id")
        if t["status"] not in OPEN or not did:
            continue
        d = con.execute("SELECT started_at, ended_at, role FROM dispatches WHERE id=? AND run_id=?",
                        (did, run["id"])).fetchone()
        if d is None or d["role"] != "executor":
            continue
        rows = [r for r in economics.office_rows(con, run["id"]) if r["dispatch_id"] == did and r["task_id"] == t["id"]]
        over = exceeded(t, dict(d), rows)
        if over:
            out.append(f"oversize: {t['id']} ({size_of(t)}) {', '.join(over)} of its size class")
            ids.append(t["id"])
    if not ids:
        return [], None
    first = ids[0]
    return out, (f"oversize {', '.join(ids)}: split (office amend {first} --contract), resize, or "
                 f"office rerun {first} --fresh --as <route>; no automatic action")
