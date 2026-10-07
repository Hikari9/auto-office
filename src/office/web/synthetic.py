"""Deterministic synthetic workspaces for web tests and load checks.

`build_workspace(dir, scale)` writes `runs.db` through the normal writer
(`db.connect`, so the real schema and migrations apply) and `github.json`
describing the GitHub repositories, issues and PRs those runs refer to. The
same seed always yields byte-identical content.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from office import db
from office.route_learning import ensure_schema as ensure_route_schema

SCALES = {
    "small": {"repos": 3, "issues_per_repo": 10, "runs": 8, "tasks_per_run": 4},
    "large": {"repos": 40, "issues_per_repo": 50, "runs": 320, "tasks_per_run": 10},
}
EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc)
CURRENT_VERSION = "3.3.3"
LEGACY_VERSION = "3.0.4"
ROUTES = ("codex/gpt-6-luna/high", "claude/claude-opus-5-5/high", "agy/gemini-3.8-flash/medium")


def ts(minutes: float) -> str:
    return (EPOCH + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True)


# ------------------------------------------------------------------ row writers (shared with tests)

def insert_run(con, run_id: str, *, git_common_dir: str, goal: str = "goal", phase: str = "executing",
               gear: str = "M", office_version: str = CURRENT_VERSION, landing: dict | None = None,
               state_dir: str | None = None, at: float = 0, end_state: str | None = None) -> None:
    con.execute("INSERT INTO runs(id, family_id, created_at, status, office_version, repo_root, git_common_dir, goal, "
                "phase, gear, state_dir, requirements_version, plan_version, updated_at, landing_json, terminal_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, run_id, ts(at), phase, office_version, git_common_dir.removesuffix("/.git"), git_common_dir,
                 goal, phase, gear, state_dir, 1, 1, ts(at), dumps(landing) if landing else None,
                 ts(at + 1) if phase in ("closed", "abandoned") else None))
    frozen = {"goal": goal, **({"end_state": end_state} if end_state else {})}
    con.execute("INSERT INTO requirements(run_id, version, frozen_json, source, quote, created_at) VALUES(?,?,?,?,?,?)",
                (run_id, 1, dumps(frozen), "start", None, ts(at)))


def insert_task(con, run_id: str, task_id: str, *, status: str = "queued", title: str | None = None,
                pr: dict | None = None, stack_after: str | None = None, pause_reason: str | None = None,
                at: float = 0) -> None:
    con.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, status, "
                "pause_reason, introduced_plan_version, contract_version, acceptance_version, stack_after, pr_json, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, title or f"Task {task_id}", "executor", "[]",
                 dumps([stack_after] if stack_after else []), "[]", "[]", status, pause_reason, 1, 1, 1, stack_after,
                 dumps(pr) if pr else None, ts(at), ts(at)))


def insert_dispatch(con, dispatch_id: str, run_id: str, *, role: str = "executor", task_id: str | None = None,
                    status: str = "running", ended: bool = False, at: float = 0, route: str = ROUTES[0],
                    fallbacks_taken: list | None = None, **extra) -> None:
    harness, model, effort = route.split("/")
    kind = "reviewer" if role.endswith("reviewer") else role
    route_json = {"candidate": {"harness": harness, "invocation_model_id": model, "effort": effort},
                  **({"fallbacks_taken": fallbacks_taken} if fallbacks_taken else {})}
    con.execute("INSERT INTO dispatches(id, run_id, role, holder_id, triple, invocation_model_id, started_at, ended_at, "
                "task_id, kind, office_version, status, harness, model, effort, route_json, last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dispatch_id, run_id, role, dispatch_id, route, model, ts(at), ts(at + 5) if ended else None, task_id,
                 kind, CURRENT_VERSION, status, harness, model, effort, dumps(route_json), ts(at + 1)))
    for key, value in extra.items():
        if key not in {c.split()[0] for c in db.SHARED_COLUMNS["dispatches"]}:
            raise ValueError(f"unknown dispatch column {key}")
        con.execute(f"UPDATE dispatches SET {key}=? WHERE id=?", (value, dispatch_id))


def insert_event(con, run_id: str, kind: str, summary: str, *, payload: dict | None = None, task_id: str | None = None,
                 dispatch_id: str | None = None, at: float = 0) -> None:
    con.execute("INSERT INTO events(run_id, kind, audience, task_id, dispatch_id, summary, payload_json, office_version, "
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, kind, "orchestrator", task_id, dispatch_id, summary, dumps(payload) if payload else None,
                 CURRENT_VERSION, ts(at)))


def insert_binding(con, run_id: str, harness: str, session_id: str, *, bound_by: str = "office start",
                   ended: bool = False, at: float = 0) -> None:
    con.execute("INSERT INTO session_bindings(harness, session_id, run_id, bound_at, bound_by, ended_at) "
                "VALUES(?,?,?,?,?,?)", (harness, session_id, run_id, ts(at), bound_by, ts(at + 2) if ended else None))


def insert_route_audit(con, audit_id: str, run_id: str, task_id: str, *, primary: str, fallbacks: list[str],
                       planner_why: str | None = None, at: float = 0) -> None:
    ensure_route_schema(con)
    slate = [{"rank": rank, "route": r, "label": r, "reason": f"{r} ranks {i + 1} on utility",
              "strength": f"{r} strength", "weakness": f"{r} weakness"}
             for i, (rank, r) in enumerate(zip(("PRIMARY", "FALLBACK 1", "FALLBACK 2"), [primary, *fallbacks]))]
    disclosure = {"policy_version": "p1", "learner_version": "l1", "phase": "plan", "task_id": task_id,
                  "role": "executor", "slate": slate,
                  "planner": {"chooser": "planner" if planner_why else "router", "primary": primary,
                              "fallbacks": fallbacks, "override": bool(planner_why), "why": planner_why}}
    con.execute("INSERT INTO route_audit(id, run_id, task_id, role, phase, plan_version, decision_hash, policy_version, "
                "learner_version, seed, primary_route, dispatched_route, explored, disclosure_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (audit_id, run_id, task_id, "executor", "plan", 1, f"h{audit_id}", "p1", "l1", "s", primary, None, 0,
                 dumps(disclosure), ts(at)))


def insert_gate(con, gate_id: str, run_id: str, task_id: str, kind: str, status: str, verdict: str | None,
                at: float = 0) -> None:
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, verdict, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (gate_id, run_id, "task", task_id, kind, gate_id, status, verdict, ts(at)))


# ------------------------------------------------------------------ workspace

def build_workspace(directory: str | Path, scale: str = "small", *, seed: int = 0) -> dict:
    """Write `runs.db` and `github.json` under `directory`; return a summary of what was written."""
    if scale not in SCALES:
        raise ValueError(f"scale must be one of {sorted(SCALES)}")
    spec = SCALES[scale]
    rng = random.Random(f"{scale}:{seed}")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    runs_dir = root / "state" / "runs"
    repos = [{"slug": f"synth-org-{i % 3}/repo-{i:02d}", "git_common_dir": f"/synthetic/src/repo-{i:02d}/.git"}
             for i in range(spec["repos"])]
    issues = [{"repo": r["slug"], "number": n, "title": f"Issue {n} of {r['slug']}",
               "state": "open" if rng.random() < 0.7 else "closed"}
              for r in repos for n in range(1, spec["issues_per_repo"] + 1)]
    prs: list[dict] = []
    counts = {"runs": 0, "tasks": 0, "dispatches": 0, "events": 0}
    specials: dict[str, str] = {}
    con = db.connect(root / "runs.db")
    try:
        with db.transaction(con):
            ensure_route_schema(con)
            for i in range(spec["runs"]):
                _write_run(con, rng, i, spec, repos, runs_dir, prs, counts, specials)
    finally:
        con.close()
    github = {"seed": seed, "scale": scale, "repos": repos, "issues": issues, "prs": prs}
    (root / "github.json").write_text(json.dumps(github, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {"db": root / "runs.db", "github": root / "github.json", "runs_dir": runs_dir,
            "repos": len(repos), "issues": len(issues), "prs": len(prs), "specials": specials, **counts}


def _write_run(con, rng, i: int, spec: dict, repos: list, runs_dir: Path, prs: list, counts: dict,
               specials: dict) -> None:
    repo = repos[i % len(repos)]
    run_id = f"{rng.getrandbits(64):016x}-synthetic{i:04d}"
    legacy = i % 7 == 3
    terminal = i % 5 == 4
    issue = (i // (2 * len(repos))) % spec["issues_per_repo"] + 1  # repos share numbers; issues get two runs
    at = i * 30
    state_dir = runs_dir / run_id
    insert_run(con, run_id, git_common_dir=repo["git_common_dir"], goal=f"Synthetic goal {i}",
               phase="closed" if terminal else "executing", gear=rng.choice("SML"),
               office_version=LEGACY_VERSION if legacy else CURRENT_VERSION,
               landing={"issue": issue if i % 2 else f"https://github.com/{repo['slug']}/issues/{issue}"},
               state_dir=str(state_dir), at=at, end_state="merged" if i % 3 else "pr")
    counts["runs"] += 1
    specials.setdefault("legacy_route" if legacy else "routed", run_id)
    if terminal:
        specials.setdefault("terminal", run_id)
    if i == 1:
        # Started from a terminal by hand and finished: no live session left.
        insert_binding(con, run_id, "terminal", f"tty-{i}", bound_by="terminal", ended=True, at=at)
        specials["terminal_started"] = run_id
    elif not terminal:
        insert_binding(con, run_id, rng.choice(("claude", "codex")), f"sess-{i:04d}", at=at)
    n_tasks = spec["tasks_per_run"]
    for k in range(n_tasks):
        tid = f"T{k + 1}"
        status = "accepted" if terminal else rng.choice(("queued", "running", "submitted", "accepted", "accepted"))
        stack = f"T{k}" if k and k % 3 == 0 else None
        pr = None
        if status in ("submitted", "accepted"):
            number = 1000 + counts["tasks"]
            base = f"office/{run_id[:8]}/{stack}" if stack else "main"
            pr = {"number": number, "url": f"https://github.com/{repo['slug']}/pull/{number}", "base": base,
                  "branch": f"office/{run_id[:8]}/{tid}", "merged": status == "accepted"}
            prs.append({"repo": repo["slug"], "number": number, "head": pr["branch"], "base": base,
                        "run": run_id, "task": tid, "stacked": bool(stack)})
            if stack:
                specials.setdefault("stacked_pr", f"{run_id}/{tid}")
        pause = None
        if i == 2 and k == 0:
            status, pause = "paused", "user paused"
            specials["paused"] = f"{run_id}/{tid}"
        if i == 2 and k == 1:
            status = "blocked"
            specials["blocked"] = f"{run_id}/{tid}"
        insert_task(con, run_id, tid, status=status, pr=pr, stack_after=stack, pause_reason=pause, at=at + k)
        counts["tasks"] += 1
        if not legacy:
            insert_route_audit(con, f"RA{counts['tasks']:06d}", run_id, tid, primary=ROUTES[k % 3],
                               fallbacks=[ROUTES[(k + 1) % 3]], planner_why="cheaper on this shape" if k == 2 else None,
                               at=at + k)
        attempts = 1 + (1 if (i + k) % 3 else 0)
        for a in range(attempts):
            last = a == attempts - 1
            quota = i == 3 and k == 0 and last
            open_ = last and (quota or status in ("running", "queued", "paused", "blocked")) and not terminal
            did = f"D{counts['dispatches']:07d}"
            extra = {}
            if quota:
                extra = {"stall_kind": "usage_limit", "resets_at": ts(at + 300), "limit_label": "resets 5pm"}
                specials["quota_wait"] = did
            insert_dispatch(con, did, run_id, task_id=tid, status="running" if open_ else "done", ended=not open_,
                            at=at + k + a, route=ROUTES[(k + a) % 3],
                            fallbacks_taken=[{"route": ROUTES[k % 3], "reason": "usage limit"}] if a else None,
                            **extra)
            counts["dispatches"] += 1
        reviewing = i == 5 and k == 0
        did = f"D{counts['dispatches']:07d}"
        insert_dispatch(con, did, run_id, role="code_reviewer", task_id=tid, status="running" if reviewing else "done",
                        ended=not reviewing, at=at + k + 3, route=ROUTES[(k + 2) % 3])
        counts["dispatches"] += 1
        insert_gate(con, f"G{did}", run_id, tid, "code_review", "done" if not reviewing else "running",
                    "PASS" if not reviewing else None, at=at + k + 3)
        if reviewing:
            reply = state_dir / "dispatches" / did / "reply.txt"
            reply.parent.mkdir(parents=True, exist_ok=True)
            reply.write_text("VERDICT: PASS\n", encoding="utf-8")
            specials["awaiting_ingestion"] = did
        if k == 0:
            # Visual verification of the first task: running, reply written awaiting ingestion, or done.
            visual = "done" if terminal else ("running", "awaiting", "done")[i % 3]
            did = f"D{counts['dispatches']:07d}"
            insert_dispatch(con, did, run_id, role="browser_verifier" if i % 2 else "visual_reviewer", task_id=tid,
                            status="done" if visual == "done" else "running", ended=visual == "done", at=at + 4,
                            route=ROUTES[0])
            counts["dispatches"] += 1
            if visual == "awaiting":
                reply = state_dir / "dispatches" / did / "reply.txt"
                reply.parent.mkdir(parents=True, exist_ok=True)
                reply.write_text("VERDICT: PASS\n", encoding="utf-8")
                specials.setdefault("visual_awaiting_ingestion", did)
            did = f"D{counts['dispatches']:07d}"
            insert_dispatch(con, did, run_id, role="plan_reviewer", status="done", ended=True, at=at,
                            route=ROUTES[1])
            counts["dispatches"] += 1
    for e in range(3):
        insert_event(con, run_id, "note", f"event {e} for run {i}", payload={"n": e}, at=at + e)
        counts["events"] += 1


def repo_slugs(github_json: str | Path) -> dict[str, str]:
    """git_common_dir -> GitHub slug, from a workspace's github.json."""
    data = json.loads(Path(github_json).read_text(encoding="utf-8"))
    return {r["git_common_dir"]: r["slug"] for r in data["repos"]}
