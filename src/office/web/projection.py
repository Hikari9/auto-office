"""The Office projection: runs.db snapshot -> the web view model.

Every value here is read from a recorded row. Nothing is inferred from text or
branch names, no routing score is recomputed, and absent evidence is reported
as `unknown`/`unavailable`, never as a zero. All reads happen inside the
caller's snapshot (`observer.read_snapshot`).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from office.version import release_key, release_line
from office.web import identity

TERMINAL_PHASES = ("closed", "abandoned")
COLUMNS = ("orchestrators", "plan_reviewers", "executors", "code_reviewers", "visual_verifiers", "other")
ROLE_COLUMNS = {"executor": "executors", "plan_reviewer": "plan_reviewers", "code_reviewer": "code_reviewers",
                "visual_reviewer": "visual_verifiers", "visual_verifier": "visual_verifiers",
                "browser_verifier": "visual_verifiers"}
REVIEWER_ROLES = ("plan_reviewer", "code_reviewer", "visual_reviewer", "visual_verifier", "browser_verifier")
RUNNING_DISPATCH = ("launching", "running", "claimed")
STALE_DISPATCH = ("stale", "superseded")
UNAVAILABLE_DISPATCH = ("failed", "cancelled")
PROGRESS_BASIS = "task_count: each non-cancelled task weighs 1; accepted tasks count as done"
TELEMETRY_UNITS = {"cpu": "percent", "ram": "bytes", "context": "tokens", "quota": "state"}
LEGACY_LINE = (3, 1)  # runs below this release line are read-only (3.0 recorder runs)


def _unknown_probe(*_args) -> str:
    return "unknown"


def _no_telemetry(*_args) -> dict:
    return {}


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


@dataclass
class Context:
    """Everything a projection needs from outside runs.db, injectable.

    `process(kind, row)` and `activity(kind, row)` are liveness probes (herdr,
    ps); they default to `unknown` so a read never shells out. `telemetry(kind,
    row)` returns measured telemetry objects. `fs(path)` reads a file or returns
    None. `repo_slugs` maps git_common_dir to a GitHub `owner/name`.
    """
    host_id: str | None = None
    fs: Callable[[Path], str | None] = _read_file
    process: Callable[..., str] = _unknown_probe
    activity: Callable[..., str] = _unknown_probe
    telemetry: Callable[..., dict] = _no_telemetry
    repo_slugs: Mapping[str, str] | Callable[[str], str | None] = field(default_factory=dict)
    runs_dir: Path | None = None
    home: str | Path | None = None

    def slug(self, git_common_dir: str | None) -> str | None:
        if not git_common_dir:
            return None
        if callable(self.repo_slugs):
            return self.repo_slugs(git_common_dir)
        return self.repo_slugs.get(git_common_dir)


def _loads(raw, default=None):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ snapshot loading

class _Rows:
    """Every table a projection reads, loaded once per snapshot and grouped by run."""

    def __init__(self, snap, run_ids: list[str] | None):
        self.snap = snap
        where, params = "", ()
        if run_ids is not None:
            where = f"run_id IN ({','.join('?' * len(run_ids))})" if run_ids else "0"
            params = tuple(run_ids)
        self.by_run = {}
        for table in ("tasks", "dispatches", "gates", "requirements", "authorizations", "route_audit"):
            grouped = defaultdict(list)
            for row in snap.table(table, where, params):
                grouped[row.get("run_id")].append(row)
            self.by_run[table] = grouped
        bindings = defaultdict(list)
        if snap.has("session_bindings"):
            active = "ended_at IS NULL" if snap.has("session_bindings", "ended_at") else ""
            clause = " AND ".join(c for c in (where, active) if c)
            for row in snap.table("session_bindings", clause, params):
                bindings[row["run_id"]].append(row)
        self.by_run["session_bindings"] = bindings

    def of(self, table: str, run_id: str) -> list[dict]:
        return self.by_run[table].get(run_id, [])


def workspace(snap, ctx: Context) -> dict:
    """Every run on this host, each fully projected, plus the repositories they belong to."""
    runs = snap.table("runs", order="created_at, rowid")
    rows = _Rows(snap, None)
    projected = [_run(snap, ctx, r, rows) for r in runs]
    repos: dict[str, dict] = {}
    for p in projected:
        repo = p["repo"]
        if repo["key"]:
            entry = repos.setdefault(repo["key"], {**repo, "runs": []})
            entry["runs"].append(p["id"])
    return {"host": identity.host(ctx.host_id), "schema": _schema(snap), "repos": list(repos.values()),
            "runs": projected}


def run(snap, ctx: Context, run_id: str) -> dict | None:
    found = snap.table("runs", "id=?", (run_id,))
    if not found:
        return None
    return _run(snap, ctx, found[0], _Rows(snap, [run_id]))


def _schema(snap) -> dict:
    return {"office_schema": _schema_version(snap), "tables": sorted(snap.tables)}


def _schema_version(snap) -> int | None:
    if not snap.has("schema_meta"):
        return None
    row = snap.rows("SELECT value FROM schema_meta WHERE key='office_schema'")
    try:
        return int(row[0]["value"]) if row else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ run

def capabilities(snap, run_row: dict) -> dict:
    """What this run can show, from schema presence and its runtime line."""
    ver = run_row.get("office_version")
    legacy = not ver or release_key(ver)[:2] < LEGACY_LINE
    return {
        "tasks": snap.has("tasks"),
        "issue_link": snap.has("runs", "landing_json"),
        "pr_links": snap.has("tasks", "pr_json"),
        "gates": snap.has("gates"),
        "route_audit": snap.has("route_audit"),
        "session_bindings": snap.has("session_bindings"),
        "quota_wait": snap.has("dispatches", "stall_kind"),
        "idle_tracking": snap.has("dispatches", "idle_since"),
        "activity": snap.has("events"),
        "legacy_runtime": legacy,
        "read_only": legacy,
    }


def _run(snap, ctx: Context, r: dict, rows: _Rows) -> dict:
    run_id = r["id"]
    slug = ctx.slug(r.get("git_common_dir"))
    repo = {"key": identity.repo_key(slug, r.get("git_common_dir")),
            "local_key": identity.local_repo_key(r.get("git_common_dir")), "slug": slug}
    landing = _loads(r.get("landing_json"), {}) or {}
    tasks = sorted(rows.of("tasks", run_id), key=lambda t: (str(t.get("id")), t["_rowid"]))
    dispatches = sorted(rows.of("dispatches", run_id), key=lambda d: (d.get("started_at") or "", d["_rowid"]))
    bindings = rows.of("session_bindings", run_id)
    audits = rows.of("route_audit", run_id)
    ver = r.get("office_version")
    prs = _prs(run_id, tasks, repo["key"])
    agents, history = _agents(ctx, r, tasks, dispatches, bindings)
    latest_by_task = {}
    for d in dispatches:
        if d.get("task_id") and d.get("role") == "executor":
            latest_by_task[d["task_id"]] = d
    return {
        "id": identity.run(run_id),
        "run_id": run_id,
        "repo": repo,
        "goal": r.get("goal"),
        "phase": r.get("phase") or r.get("status"),
        "gear": r.get("gear"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "office_version": ver,
        "release_line": release_line(ver) if ver else None,
        "runtime": identity.runtime(ver),
        "end_state": _end_state(rows.of("requirements", run_id), landing),
        "authorizations": [{"kind": a.get("kind"), "target": a.get("target"),
                            "requirements_version": a.get("requirements_version"), "created_at": a.get("created_at")}
                           for a in rows.of("authorizations", run_id) if not a.get("revoked_at")],
        "issue": _issue(landing, repo["key"]),
        "tasks": [_task(run_id, t, audits, latest_by_task.get(t["id"])) for t in tasks],
        "prs": prs,
        "gates": {"office": _gate_summary(rows.of("gates", run_id)),
                  "github_checks": {"status": "unavailable", "source": None,
                                    "reason": "GitHub check data is not part of runs.db"}},
        "owner": _owner(run_id, bindings),
        "liveness": _liveness(r, dispatches, bindings),
        "progress": _progress(tasks),
        "agents": agents,
        "history": history,
        "capabilities": capabilities(snap, r),
    }


def _end_state(requirements: list[dict], landing: dict) -> dict:
    latest = max(requirements, key=lambda q: q.get("version") or 0, default=None)
    frozen = _loads((latest or {}).get("frozen_json"), {}) or {}
    value = frozen.get("end_state") if isinstance(frozen, dict) else None
    source = "requirements" if value else None
    if not value and landing.get("end_state"):
        value, source = landing["end_state"], "landing_json"
    return {"value": value, "source": source,
            "requirements_version": (latest or {}).get("version")}


def _issue(landing: dict, repo_key: str | None) -> dict | None:
    parsed = identity.parse_issue(landing.get("issue"), repo_key)
    if parsed is None:
        return None
    return {**parsed, "provenance": "office-record", "source": "landing_json.issue",
            "closed": bool(landing.get("issue_closed"))}


def _prs(run_id: str, tasks: list[dict], repo_key: str | None) -> list[dict]:
    out = []
    for t in tasks:
        pr = _loads(t.get("pr_json"), None)
        if not isinstance(pr, dict) or not (pr.get("number") or pr.get("url")):
            continue
        out.append({"ref": identity.pr_ref(repo_key, pr["number"]) if pr.get("number") else None,
                    "task": identity.task(run_id, t["id"]), "number": pr.get("number"), "url": pr.get("url"),
                    "base": pr.get("base"), "branch": pr.get("branch"), "merged": pr.get("merged"),
                    "stacked_on": t.get("stack_after"), "provenance": "office-record", "source": "tasks.pr_json"})
    return out


def _gate_summary(gates: list[dict]) -> dict:
    by_kind: dict[str, dict] = {}
    for g in gates:
        k = by_kind.setdefault(g.get("kind") or "unknown", {"total": 0, "status": {}, "verdict": {}})
        k["total"] += 1
        k["status"][g.get("status")] = k["status"].get(g.get("status"), 0) + 1
        if g.get("verdict"):
            k["verdict"][g["verdict"]] = k["verdict"].get(g["verdict"], 0) + 1
    return {"source": "runs.db gates", "total": len(gates), "by_kind": by_kind}


def _owner(run_id: str, bindings: list[dict]) -> dict:
    if not bindings:
        return {"kind": "none"}
    b = max(bindings, key=lambda x: (x.get("bound_at") or "", x["_rowid"]))
    return {"kind": "session", "id": identity.session(run_id, b["harness"], b["session_id"]),
            "harness": b["harness"], "bound_at": b.get("bound_at"), "bound_by": b.get("bound_by")}


def _liveness(r: dict, dispatches: list[dict], bindings: list[dict]) -> str:
    if (r.get("phase") or r.get("status")) in TERMINAL_PHASES or r.get("terminal_at") or r.get("pruned_at"):
        return "terminal"
    if bindings or any(not d.get("ended_at") and d.get("status") in RUNNING_DISPATCH for d in dispatches):
        return "live"
    return "resumable"


def _progress(tasks: list[dict]) -> dict | None:
    counted = [t for t in tasks if t.get("status") != "cancelled"]
    if not tasks:
        return None
    done = sum(1 for t in counted if t.get("status") == "accepted")
    total = len(counted)
    return {"value": (done / total) if total else None, "accepted_weight": done, "total_weight": total,
            "basis": PROGRESS_BASIS}


# ------------------------------------------------------------------ tasks and routes

def _task(run_id: str, t: dict, audits: list[dict], executor: dict | None) -> dict:
    pr = _loads(t.get("pr_json"), None)
    return {"id": identity.task(run_id, t["id"]), "task_id": t["id"], "title": t.get("title"), "role": t.get("role"),
            "status": t.get("status"), "pause_reason": t.get("pause_reason"), "stack_after": t.get("stack_after"),
            "depends": _loads(t.get("depends_json"), []),
            "current_dispatch": identity.dispatch(t["current_dispatch_id"]) if t.get("current_dispatch_id") else None,
            "pr": pr.get("number") if isinstance(pr, dict) else None,
            "route": route(t["id"], audits, executor)}


def route(task_id: str, audits: list[dict], dispatch: dict | None) -> dict:
    """The recorded routing decision for one task: no scoring is recomputed here."""
    rows = sorted((a for a in audits if a.get("task_id") == task_id),
                  key=lambda a: (a.get("created_at") or "", a["_rowid"]))
    dispatch_route = _loads((dispatch or {}).get("route_json"), {}) or {}
    taken = dispatch_route.get("fallbacks_taken") or []
    if not rows:
        return {"available": False, "reason": "no route_audit rows recorded for this task (legacy or pre-#300 route)",
                "dispatched": _candidate(dispatch_route), "fallbacks_taken": taken, "audits": []}
    latest = _loads(rows[-1].get("disclosure_json"), {}) or {}
    slate = latest.get("slate") or []
    planner = latest.get("planner") or {}
    primary = planner.get("primary") or rows[-1].get("primary_route") or (slate[0].get("route") if slate else None)
    head = next((s for s in slate if s.get("route") == primary), slate[0] if slate else {})
    fallbacks = planner.get("fallbacks") or [s.get("route") for s in slate if s.get("route") != primary]
    disp = latest.get("dispatch") or {}
    taken = taken or disp.get("fallbacks_taken") or []
    return {
        "available": True,
        "primary": primary,
        "fallbacks": fallbacks,
        "reason": head.get("reason"),
        "strength": head.get("strength"),
        "weakness": head.get("weakness"),
        "dispatched": disp.get("dispatched") or rows[-1].get("dispatched_route") or _candidate(dispatch_route),
        "fallbacks_taken": [{"route": f.get("route"), "reason": f.get("reason")} for f in taken if isinstance(f, dict)],
        "planner_override": ({"why": planner.get("why"), "departs_from_ranking": planner.get("departs_from_ranking")}
                             if planner.get("override") else None),
        "audits": [{"id": a["id"], "phase": a.get("phase"), "plan_version": a.get("plan_version"),
                    "primary_route": a.get("primary_route"), "dispatched_route": a.get("dispatched_route"),
                    "explored": bool(a.get("explored")), "decision_hash": a.get("decision_hash"),
                    "created_at": a.get("created_at")} for a in rows],
    }


def _candidate(route_json: dict) -> str | None:
    cand = route_json.get("candidate") or {}
    if not isinstance(cand, dict) or not cand:
        return None
    return cand.get("id") or "/".join(str(cand.get(k)) for k in ("harness", "invocation_model_id", "effort")
                                      if cand.get(k))


# ------------------------------------------------------------------ agents

def _metric(name: str, raw) -> dict:
    base = {"status": "unknown", "value": None, "unit": TELEMETRY_UNITS[name], "source": None, "observed_at": None}
    if isinstance(raw, dict):
        base.update({k: raw[k] for k in base if k in raw})
    return base


def _telemetry(ctx: Context, kind: str, row: dict) -> dict:
    measured = ctx.telemetry(kind, row) or {}
    out = {name: _metric(name, measured.get(name)) for name in TELEMETRY_UNITS}
    if kind == "dispatch" and row.get("stall_kind") and out["quota"]["status"] == "unknown":
        out["quota"] = {"status": "measured", "value": "exhausted", "unit": "state",
                        "source": "dispatches.stall_kind", "observed_at": row.get("last_seen_at")}
    return out


def _reply_path(ctx: Context, run_row: dict, dispatch_id: str) -> Path:
    base = run_row.get("state_dir")
    if not base:
        if ctx.runs_dir is not None:
            base = Path(ctx.runs_dir) / run_row["id"]
        else:
            from office import paths
            base = paths.run_dir(run_row["id"])
    return Path(base) / "dispatches" / dispatch_id / "reply.txt"


def _dispatch_state(ctx: Context, run_row: dict, d: dict, task: dict | None) -> dict:
    ended = bool(d.get("ended_at"))
    status = d.get("status")
    process = "exited" if ended else ctx.process("dispatch", d)
    activity = ctx.activity("dispatch", d)
    if activity == "unknown" and not ended and d.get("idle_since"):
        activity = "idle"
    awaiting = False
    if not ended and (d.get("role") in REVIEWER_ROLES or d.get("kind") == "reviewer"):
        text = ctx.fs(_reply_path(ctx, run_row, d["id"]))
        awaiting = bool(text and text.strip())
    task_status = (task or {}).get("status")
    return {
        "process": process,
        "activity": activity,
        "paused": task_status == "paused",
        "blocked": task_status == "blocked",
        "quota_wait": ({"active": True, "kind": d.get("stall_kind"), "resets_at": d.get("resets_at"),
                        "label": d.get("limit_label")} if d.get("stall_kind") and not ended
                       else {"active": False, "kind": None, "resets_at": None, "label": None}),
        "reply_written_awaiting_ingestion": awaiting,
        "complete": ended and status not in UNAVAILABLE_DISPATCH,
        "stale": status in STALE_DISPATCH,
        "unavailable": status in UNAVAILABLE_DISPATCH or d.get("terminal_classification") == "lost",
        "status": status,
        "terminal_classification": d.get("terminal_classification"),
    }


def _agents(ctx: Context, run_row: dict, tasks: list[dict], dispatches: list[dict],
            bindings: list[dict]) -> tuple[dict, list[dict]]:
    run_id = run_row["id"]
    columns: dict[str, list] = {c: [] for c in COLUMNS}
    for b in sorted(bindings, key=lambda x: (x.get("bound_at") or "", x["_rowid"])):
        columns["orchestrators"].append({
            "id": identity.session(run_id, b["harness"], b["session_id"]), "kind": "session", "role": "orchestrator",
            "harness": b["harness"], "model": None, "effort": None,
            "current_work": {"run": identity.run(run_id), "phase": run_row.get("phase")},
            "state": {"process": ctx.process("session", b), "activity": ctx.activity("session", b), "paused": False,
                      "blocked": False, "quota_wait": {"active": False, "kind": None, "resets_at": None, "label": None},
                      "reply_written_awaiting_ingestion": False, "complete": False, "stale": False,
                      "unavailable": False},
            "telemetry": _telemetry(ctx, "session", b)})
    task_by_id = {t["id"]: t for t in tasks}
    latest: dict[tuple, dict] = {}
    for d in dispatches:  # sorted oldest first: the last one per key wins
        latest[(d.get("task_id"), d.get("role"))] = d
    current = {d["id"] for d in latest.values()}
    history = []
    for d in dispatches:
        task = task_by_id.get(d.get("task_id"))
        node = {"id": identity.dispatch(d["id"]), "kind": "dispatch", "role": d.get("role"),
                "task": identity.task(run_id, d["task_id"]) if d.get("task_id") else None,
                "harness": d.get("harness"), "model": d.get("model") or d.get("invocation_model_id"),
                "effort": d.get("effort"), "started_at": d.get("started_at"), "ended_at": d.get("ended_at")}
        if d["id"] not in current:
            history.append({**node, "status": d.get("status"),
                            "terminal_classification": d.get("terminal_classification")})
            continue
        node["current_work"] = {"task": node["task"], "title": (task or {}).get("title"),
                                "dispatch_kind": d.get("kind"), "gate_id": d.get("gate_id")}
        node["state"] = _dispatch_state(ctx, run_row, d, task)
        node["telemetry"] = _telemetry(ctx, "dispatch", d)
        columns[ROLE_COLUMNS.get(d.get("role") or "", "other")].append(node)
    return {"columns": columns}, history
