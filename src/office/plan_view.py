"""The plan diagram a user approves: lanes, stacking, route preview, checkpoints.

Parallel vs stacked is derived from `depends` (no new plan field): a task with
no dependency is a parallel root off the run base; a task with dependencies
stacks on the last one, which is the branch `dispatch` bases it on. Routes are
a preview; dispatch re-resolves and reports any drift against the preview.

The runtime writes the diagram into PLAN.md between generated markers. The
parser strips that block, so rewriting it never changes the plan's hash.
"""
from __future__ import annotations

import re
from pathlib import Path

from office import candidates, planfile, state
from office.util import dumps, loads

# ------------------------------------------------------------------ layout

def layout(tasks: list[dict]) -> dict[str, dict]:
    """Per task: wave (1-based), base (None = run base), and extra dependencies."""
    graph = {t["id"]: list(t.get("depends") or []) for t in tasks}
    waves: dict[str, int] = {}

    def wave(tid: str) -> int:
        if tid not in waves:
            waves[tid] = 1 + max((wave(d) for d in graph.get(tid, []) if d in graph), default=0)
        return waves[tid]

    out = {}
    for t in tasks:
        deps = [d for d in graph[t["id"]] if d in graph]
        out[t["id"]] = {"wave": wave(t["id"]), "base": deps[-1] if deps else None, "needs": deps[:-1]}
    return out


# ------------------------------------------------------------------ route preview

def short_why(disclosure: dict | None) -> str:
    """The deciding clauses of a selection disclosure, without the gate boilerplate."""
    if not disclosure:
        return ""
    parts = []
    for clause in (disclosure.get("reason") or "").split("; "):
        if clause.startswith("cleared the applicable"):
            continue
        m = re.match(r"matched preferred seed #(\d+)", clause)
        if m:
            parts.append(f"preferred seed #{m.group(1)}")
        elif clause.startswith("won the "):
            parts.append(clause.replace("won the ", "best ").replace(" and local-evidence comparison", "/evidence")
                         .replace(" comparison", ""))
        elif clause == "fit within the protected quota reserve":
            parts.append("quota ok")
        elif "quota headroom explicitly unknown" in clause:
            parts.append("quota unknown")
        elif clause:
            parts.append(clause)
    return ", ".join(parts)


def route_label(disclosure: dict | None) -> str:
    if not disclosure:
        return "no route"
    if not disclosure.get("harness"):
        return disclosure.get("triple") or "no route"
    return f"{disclosure.get('harness')}/{disclosure.get('model_id')}@{disclosure.get('effort')}"


def _preview_one(con, config: dict, run: dict, role: str, tid: str, exclude: set[str] | None = None) -> dict:
    try:
        decision = candidates.route_role(con, config, run, role, task_id=tid, exclude=exclude)
    except Exception as exc:  # a preview never blocks submit
        return {"route": None, "why": f"preview failed: {exc}"[:160]}
    if decision.get("status") != "selected":
        top = (decision.get("rejected") or [{}])[0]
        why = f"{decision.get('status')}" + (f": {top.get('reason')}" if top.get("reason") else "")
        return {"route": None, "why": why[:160]}
    d = decision["selection_disclosure"]
    return {"route": route_label(d), "triple": decision.get("selected"), "why": short_why(d),
            "family": candidates.model_family(d.get("model_id"))}


def preview(con, run: dict, tasks: list[dict]) -> dict:
    """Route every executor task and its code reviewer as a non-binding preview."""
    config = state.pinned_config(run)
    code_review = bool((run.get("gates") or {}).get("code_review"))
    lay = layout(tasks)
    out = {}
    for t in tasks:
        ex = _preview_one(con, config, run, "executor", t["id"])
        rv = {}
        if code_review:
            exclude = {ex["triple"], f"family:{ex['family']}"} if ex.get("triple") and ex.get("family") else None
            rv = _preview_one(con, config, run, "code_reviewer", t["id"], exclude=exclude)
        visual = t.get("visual") or {}
        task_gates = (["checks"] if t.get("checks") else []) + (["code review"] if code_review else []) \
            + (["ui review"] if visual and not visual.get("none") else [])
        out[t["id"]] = {"title": t["title"], "depends": list(t.get("depends") or []), **lay[t["id"]],
                        "route": ex.get("route"), "why": ex.get("why"), "gates": task_gates,
                        "review": rv.get("route"), "review_why": rv.get("why")}
    return {"tasks": out}


def store(con, run: dict, version: int, pv: dict) -> None:
    con.execute("UPDATE plans SET preview_json=? WHERE run_id=? AND version=?", (dumps(pv), run["id"], version))


def load(con, run_id: str, version: int) -> dict | None:
    row = con.execute("SELECT preview_json FROM plans WHERE run_id=? AND version=?", (run_id, version)).fetchone()
    return loads(row["preview_json"], None) if row else None


# ------------------------------------------------------------------ render

def _where(entry: dict) -> str:
    if entry["base"] is None:
        return "off base"
    extra = f" (+ needs {', '.join(entry['needs'])})" if entry["needs"] else ""
    return f"stacked on {entry['base']}{extra}"


def checkpoints(run: dict, pv: dict) -> str:
    order = sorted(pv["tasks"], key=lambda t: (pv["tasks"][t]["wave"], int(t[1:]) if t[1:].isdigit() else 0))
    chain = [f"{t} accepted" + (f" [{', '.join(pv['tasks'][t]['gates'])}]" if pv["tasks"][t].get("gates") else "")
             for t in order] + ["integration review", "closeout"]
    return " -> ".join(chain)


def render(run: dict, version: int, pv: dict) -> list[str]:
    tasks = pv["tasks"]
    waves = sorted({e["wave"] for e in tasks.values()})
    width = max((len(f"{t}  {e['title']}") for t, e in tasks.items()), default=0)
    width = min(max(width, 20), 44)
    route_w = max((len(e["route"] or "no route") for e in tasks.values()), default=8)
    lines = [f"plan p{version} diagram (routes are a preview; dispatch re-resolves)", ""]
    for w in waves:
        ids = [t for t, e in tasks.items() if e["wave"] == w]
        lines.append(f"wave {w}" + (f"  ({' | '.join(ids)} in parallel)" if len(ids) > 1 else ""))
        for tid in ids:
            e = tasks[tid]
            head = f"{tid}  {e['title']}"
            head = head if len(head) <= width else head[: width - 1] + "~"
            lines.append(f"  {head:<{width}}  {(e['route'] or 'no route'):<{route_w}}  {_where(e)}")
            if e.get("why"):
                lines.append(f"  {'':<{width}}  why: {e['why']}")
            if e.get("review"):
                lines.append(f"  {'':<{width}}  review: {e['review']}" + (f" ({e['review_why']})" if e.get("review_why") else ""))
        lines.append("")
    lines.append(f"checkpoints: {checkpoints(run, pv)}")
    return lines


def diff(prev: dict | None, cur: dict, prev_version: int) -> list[str]:
    """What changed against the previous plan version's diagram."""
    if not prev:
        return []
    a, b = prev.get("tasks") or {}, cur["tasks"]
    out = []
    for tid in b:
        if tid not in a:
            out.append(f"{tid} added: {b[tid]['title']} ({b[tid]['route'] or 'no route'}, {_where(b[tid])})")
            continue
        x, y = a[tid], b[tid]
        if x.get("title") != y["title"]:
            out.append(f"{tid} title: {x.get('title')} -> {y['title']}")
        if (x.get("base"), x.get("needs")) != (y["base"], y["needs"]):
            out.append(f"{tid} {_where(x)} -> {_where(y)}")
        if x.get("route") != y["route"]:
            out.append(f"{tid} route: {x.get('route') or 'no route'} -> {y['route'] or 'no route'}"
                       + (f" ({y['why']})" if y.get("why") else ""))
        if x.get("review") != y.get("review"):
            out.append(f"{tid} review: {x.get('review') or 'none'} -> {y.get('review') or 'none'}")
    out += [f"{tid} removed" for tid in a if tid not in b]
    if not out:
        return [f"diagram unchanged from p{prev_version}"]
    return [f"changes vs p{prev_version}:"] + [f"  {line}" for line in out]


def write_into(plan_path: Path, version: int, lines: list[str]) -> None:
    """Replace the generated diagram block at the end of PLAN.md."""
    try:
        text = planfile.strip_generated(plan_path.read_text(encoding="utf-8"))
        block = [f"{planfile.DIAGRAM_BEGIN} p{version} (generated by office; edits here are ignored) -->", "```text", *lines, "```", planfile.DIAGRAM_END]
        plan_path.write_text(text.rstrip("\n") + "\n\n" + "\n".join(block) + "\n", encoding="utf-8")
    except OSError:
        pass  # the diagram in PLAN.md is a convenience; submit output carries it too


def drift(con, run: dict, tid: str, decision: dict) -> str | None:
    """One line when dispatch picked a different route than the approved preview."""
    pv = load(con, run["id"], run["plan_version"]) if run.get("plan_version") else None
    planned = ((pv or {}).get("tasks") or {}).get(tid, {}).get("route")
    now = route_label(decision.get("selection_disclosure"))
    if not planned or planned == now:
        return None
    why = short_why(decision.get("selection_disclosure"))
    return f"{tid} route differs from the plan preview: {planned} -> {now}" + (f" ({why})" if why else "")
