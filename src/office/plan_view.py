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

from office import adaptive, candidates, contract, convergence, planfile, state, visual
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


def _preview_one(con, config: dict, run: dict, role: str, tid: str, *,
                 task: dict | None = None, wave_load: dict | None = None, pending_explorations: int = 0,
                 quota_snapshot: dict[str, dict] | None = None,
                 quota_event_seen: set[str] | None = None) -> dict:
    try:
        decision = candidates.route_role(con, config, run, role, task_id=tid, wave_load=wave_load,
                                         pending_explorations=pending_explorations,
                                         quota_snapshot=quota_snapshot, quota_event_seen=quota_event_seen)
    except Exception as exc:  # a preview never blocks submit
        return {"route": None, "why": f"preview failed: {exc}"[:160]}
    if decision.get("status") != "selected":
        top = (decision.get("rejected") or [{}])[0]
        why = f"{decision.get('status')}" + (f": {top.get('reason')}" if top.get("reason") else "")
        return {"route": None, "why": why[:160]}
    d = decision["selection_disclosure"]
    out = {"route": route_label(d), "triple": decision.get("selected"), "why": short_why(d)}
    audit = decision.get("routing")
    if audit:
        # #300: the planner's primary + fallbacks over the router's slate.
        choice = {"routes": (task or {}).get("route"), "why": (task or {}).get("route_why")}
        plan = adaptive.apply_planner_choice(audit, choice)
        slate = adaptive.slate_for(audit, plan)
        primary = decision["qualifying_candidates"][plan["primary"]]
        out.update({"route": adaptive.label(primary), "triple": plan["primary"],
                    "why": slate[0]["reason"] if slate else out["why"],
                    "slate": slate, "route_plan": plan, "decision_hash": decision.get("decision_hash"),
                    "_audit": {**audit, "planner": plan, "phase": "plan", "task_id": tid, "role": role}})
    return out


def preview(con, run: dict, tasks: list[dict]) -> dict:
    """Route every executor task and its code reviewer as a non-binding preview."""
    config = state.pinned_config(run)
    code_review = bool((run.get("gates") or {}).get("code_review"))
    lay = layout(tasks)
    roles = ["executor"] + (["code_reviewer"] if code_review else [])
    quota_snapshot = candidates.probe_quota_snapshot(
        con, roles, family_floors=config.get("model_family_floors")) if tasks else {}
    quota_event_seen: set[str] = set()
    recorded_tasks = {t["id"]: t for t in state.tasks(con, run["id"])}
    out = {}
    audits = []
    loads: dict[int, dict[str, int]] = {}
    for t in tasks:
        # Soft spread: routes already planned in this wave weigh a little less.
        wave_load = loads.setdefault(lay[t["id"]]["wave"], {})
        explored = sum(1 for a in audits if (a.get("exploration") or {}).get("picked") == (a.get("planner") or {}).get("primary"))
        ex = _preview_one(con, config, run, "executor", t["id"], task=t, wave_load=wave_load,
                          pending_explorations=explored, quota_snapshot=quota_snapshot,
                          quota_event_seen=quota_event_seen)
        if ex.get("triple"):
            wave_load[ex["triple"]] = wave_load.get(ex["triple"], 0) + 1
        if ex.get("_audit"):
            audits.append(ex.pop("_audit"))
        rv = {}
        if code_review:
            rv = _preview_one(con, config, run, "code_reviewer", t["id"], quota_snapshot=quota_snapshot,
                              quota_event_seen=quota_event_seen)
        recorded = recorded_tasks.get(t["id"])
        dispatched_route = None
        if recorded and recorded.get("current_dispatch_id"):
            dispatch = state.get_dispatch(con, recorded["current_dispatch_id"])
            if dispatch:
                dispatched_route = dispatch.get("triple")
        visual_gate = visual.applicability(con, run, t, [])["status"] in ("required", "probe")
        if contract.is_convergence(run):
            task_gates = ["checks"] if t.get("checks") else []
        else:
            task_gates = (["checks"] if t.get("checks") else []) + (["code review"] if code_review else []) \
                + (["ui review"] if visual_gate else [])
        out[t["id"]] = {"title": t["title"], "depends": list(t.get("depends") or []), **lay[t["id"]],
                        "route": dispatched_route or ex.get("route"), "dispatched_route": dispatched_route,
                        "why": ex.get("why"), "gates": task_gates,
                        "lane": t.get("lane"), "converge": list(t.get("converge") or []),
                        "visual_gate": visual_gate,
                        "review": rv.get("route"), "review_why": rv.get("why"),
                        **{k: ex[k] for k in ("slate", "route_plan", "decision_hash") if k in ex}}
    from office import prs
    frozen = state.current_requirements(con, run["id"])["frozen"]
    s = prs.settings(con, run)
    return {"tasks": out, "end_state": frozen.get("end_state") or "ask", "deploy": frozen.get("deploy") or {},
            "prs": {k: s.get(k) for k in ("enabled", "reason", "base_branch", "merge_method")}, "_audits": audits}


def store(con, run: dict, version: int, pv: dict) -> None:
    """Store the diagram and, for each adaptive route plan, its full audit record. Caller holds the tx."""
    for audit in pv.pop("_audits", None) or []:
        audit_id = record_audit(con, run, audit, plan_version=version)
        task = (pv.get("tasks") or {}).get(audit["task_id"])
        if task is not None:
            task["audit_id"] = audit_id
    con.execute("UPDATE plans SET preview_json=? WHERE run_id=? AND version=?", (dumps(pv), run["id"], version))


def record_audit(con, run: dict, audit: dict, *, plan_version: int | None, dispatched: str | None = None,
                 explored: bool | None = None) -> str:
    """Append one routing audit row (`route_audit`): the complete decision, for inspect and replay."""
    import uuid

    from office import route_learning
    from office.util import now_iso
    route_learning.ensure_schema(con)
    audit_id = "RA" + uuid.uuid4().hex[:12]
    plan = audit.get("planner") or {}
    primary = plan.get("primary") or ((audit.get("slate") or [{}])[0]).get("route")
    con.execute("INSERT INTO route_audit(id, run_id, task_id, role, phase, plan_version, decision_hash, policy_version, "
                "learner_version, seed, primary_route, dispatched_route, explored, disclosure_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (audit_id, run["id"], audit.get("task_id"), audit.get("role"), audit.get("phase", "plan"), plan_version,
                 audit.get("decision_hash"), audit.get("policy_version"), audit.get("learner_version"),
                 (audit.get("clincher") or {}).get("seed"), primary, dispatched,
                 int(explored if explored is not None
                     else bool((audit.get("exploration") or {}).get("picked")) and primary == audit["exploration"]["picked"]),
                 dumps(audit), now_iso()))
    return audit_id


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
    tasks = pv["tasks"]
    waves: dict[int, list[str]] = {}
    for tid, task in tasks.items():
        waves.setdefault(task["wave"], []).append(tid)
    task_steps = []
    for wave in sorted(waves):
        ids = sorted(waves[wave], key=convergence._tid_key)
        parallel = " | ".join(_accepted(tid, tasks[tid]) for tid in ids)
        task_steps.append(f"wave {wave} {{{parallel}}}")

    if contract.is_convergence(run):
        review_steps = _convergence_checkpoints(tasks, code_review=bool((run.get("gates") or {}).get("code_review")))
    else:
        review_steps = ["integration review"]
    return " -> ".join([*task_steps, *review_steps, *landing_chain(pv)])


def _accepted(tid: str, task: dict) -> str:
    gates = task.get("gates") or []
    suffix = f" [{', '.join(gates)}]" if gates else ""
    return f"{tid} accepted{suffix}"


def _convergence_checkpoints(tasks: dict[str, dict], *, code_review: bool) -> list[str]:
    """List the convergence and visual reviews for each connected ownership lane."""
    lanes = convergence.group_planned_tasks(
        [{"id": tid, "depends": task.get("depends"), "lane": task.get("lane")}
         for tid, task in tasks.items()])
    checkpoints = []
    lane_by_converge: dict[str, list[str]] = {}
    for lane in lanes:
        tids = lane["tasks"]
        names = lane["lane_names"]
        lane_id = lane["id"]
        label = f"lane {names[0]}" if names else f"lane {', '.join(tids)}"
        if code_review:
            checkpoints.append(f"{label} convergence review")
        if any(tasks[tid].get("visual_gate") for tid in tids):
            checkpoints.append(f"{label} visual review")
        for tid in tids:
            for name in tasks[tid].get("converge") or []:
                lane_by_converge.setdefault(name, []).append(lane_id)
    if code_review:
        for name, lane_ids in sorted(lane_by_converge.items()):
            if len(set(lane_ids)) > 1:
                checkpoints.append(f"shared-scope {name} review")
    return checkpoints


def landing_chain(pv: dict) -> list[str]:
    """What happens after the integration review, per the user's end state."""
    p, end, deploy = pv.get("prs") or {}, pv.get("end_state") or "ask", pv.get("deploy") or {}
    if not p.get("enabled"):
        tail = ["handoff PR (task PRs off: " + (p.get("reason") or "unknown") + ")"]
        return tail + (["merge/deploy need task PRs"] if end in ("merge", "e2e") else [])
    verify = [f"verify `{deploy['verify']}`"] if deploy.get("verify") else []
    merge = f"merge PRs bottom-up into {p.get('base_branch')} ({p.get('merge_method')})"
    return {"ask": ["PRs ready", "ask: merge / preview / e2e / stop"],
            "preview": ["PRs ready", f"deploy preview `{deploy.get('preview')}`", *verify, "PRs left open"],
            "merge": ["PRs ready", merge, "closeout"],
            "e2e": ["PRs ready", merge, f"deploy prod `{deploy.get('prod')}`", *verify, "closeout"]}[end]


def render(run: dict, version: int, pv: dict) -> list[str]:
    tasks = pv["tasks"]
    waves = sorted({e["wave"] for e in tasks.values()})
    width = max((len(f"{t}  {e['title']}") for t, e in tasks.items()), default=0)
    width = min(max(width, 20), 44)
    route_w = max((len(_display_route(e)) for e in tasks.values()), default=8)
    lines = [f"plan p{version} diagram (routes are previews until dispatched; actual route shown after dispatch)", ""]
    for w in waves:
        ids = [t for t, e in tasks.items() if e["wave"] == w]
        lines.append(f"wave {w}" + (f"  ({' | '.join(ids)} in parallel)" if len(ids) > 1 else ""))
        for tid in ids:
            e = tasks[tid]
            head = f"{tid}  {e['title']}"
            head = head if len(head) <= width else head[: width - 1] + "~"
            if e.get("dispatched_route"):
                lines.append(f"  {head:<{width}}  {_display_route(e):<{route_w}}  {_where(e)}")
            elif e.get("slate") is not None:
                lines.append(f"  {head:<{width}}  {_where(e)}")
                lines.extend(adaptive.render_slate(e["slate"], indent="      ", notes=_slate_notes(e)))
            else:
                lines.append(f"  {head:<{width}}  {_display_route(e):<{route_w}}  {_where(e)}")
                if e.get("why"):
                    lines.append(f"  {'':<{width}}  why: {e['why']}")
            if e.get("review"):
                lines.append(f"  {'':<{width}}  review: {e['review']}" + (f" ({e['review_why']})" if e.get("review_why") else ""))
        lines.append("")
    lines.append(f"checkpoints: {checkpoints(run, pv)}")
    lines.append(f"end state: {pv.get('end_state') or 'ask'}")
    return lines


def _slate_notes(entry: dict) -> list[str]:
    plan = entry.get("route_plan") or {}
    notes = []
    if plan.get("chooser") == "planner":
        notes.append("planner choice" + (", departs from ranking" if plan.get("departs_from_ranking") else ""))
    if plan.get("planner_error"):
        notes.append(f"planner route ignored: {plan['planner_error']}")
    return notes


def _display_route(entry: dict) -> str:
    route = entry.get("dispatched_route")
    if route:
        return f"dispatched: {route}"
    return entry.get("route") or "no route"


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
        if not y.get("dispatched_route") and x.get("route") != y["route"]:
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
