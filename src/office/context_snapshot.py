"""`office context`: a bounded root snapshot assembled on demand (#502 PR 2).

Read-only. Everything comes from runs.db plus the versioned artifacts it
references, read in one SQLite read transaction; nothing is cached or written,
so the snapshot is never a second source of truth. It links evidence by path
and hash instead of replaying transcripts, diffs or logs.

Determinism rules:
- an artifact whose file is missing is `missing`; one whose bytes no longer hash
  to the recorded digest is `stale`; one too large to hash cheaply is `unverified`.
- the PLAN.md draft is compared with the current plan version's content hash:
  `matches`, `differs` (an unsubmitted edit or an older draft, reported stale) or `absent`.
- a run pinned to another runtime version is reported, not reinterpreted.
- if the run changed while the snapshot was assembled (a concurrent amendment,
  a new event), the snapshot says so and asks for a rerun rather than mixing states.
Every stale or missing reference is listed under `stale:`; none is silently trusted.
"""
from __future__ import annotations

from pathlib import Path

from office import state, version
from office.result import Result
from office.util import loads, now_iso, sha256_bytes, sha256_file, short

MAX_ITEMS = 20
MAX_TASKS = 40
MAX_TEXT = 200
MAX_CHARS = 16000
HASH_LIMIT_BYTES = 8 << 20
HASH_BUDGET_BYTES = 32 << 20  # total bytes hashed per call; artifacts past it are `unhashed`
MAX_JSON_CHARS = 24000


def _clip(text, n: int = MAX_TEXT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _artifact(path: str | None, digest: str | None, budget: list[int]) -> str:
    if not path:
        return "no-path"
    p = Path(path)
    if not p.is_file():
        return "missing"
    if not digest:
        return "unhashed"
    try:
        size = p.stat().st_size
        if size > HASH_LIMIT_BYTES:
            return "unverified"
        if size > budget[0]:
            return "unhashed"
        budget[0] -= size
        return "ok" if sha256_file(p) == digest else "stale"
    except OSError:
        return "missing"


def _plan_draft(run: dict, plan: dict | None) -> dict:
    from office import planfile, planpath
    if not run.get("repo_root"):
        return {"path": None, "status": "no-repo"}
    path = planpath.draft(Path(run["repo_root"]), run)
    if not path.is_file():
        return {"path": str(path), "status": "absent"}
    try:
        digest = sha256_bytes(planfile.strip_generated(path.read_text(encoding="utf-8")).encode())
    except OSError:
        return {"path": str(path), "status": "missing"}
    status = "unsubmitted" if plan is None else ("matches" if digest == plan["content_hash"] else "differs")
    return {"path": str(path), "sha256": digest, "status": status}


def _cursor(con, run_id: str) -> tuple[int, str | None]:
    row = con.execute("SELECT MAX(seq) FROM events WHERE run_id=?", (run_id,)).fetchone()
    run = con.execute("SELECT updated_at FROM runs WHERE id=?", (run_id,)).fetchone()
    return int(row[0] or 0), (run[0] if run else None)


def _read(con, run_id: str, since: int | None) -> dict:
    run = state.get_run(con, run_id)
    rid = run["id"]
    req = con.execute("SELECT version, frozen_json, created_at FROM requirements WHERE run_id=? ORDER BY version DESC "
                      "LIMIT 1", (rid,)).fetchone()
    plan = state.current_plan(con, rid)
    terminal = ",".join("?" * len(state.TASK_TERMINAL))
    open_tasks = [dict(r) for r in con.execute(
        "SELECT id, title, role, status, pause_reason, current_dispatch_id, accepted_revision_id FROM tasks "
        f"WHERE run_id=? AND status NOT IN ({terminal}) ORDER BY id LIMIT ?",
        (rid, *state.TASK_TERMINAL, MAX_TASKS + 1))]
    done_tasks = [r[0] for r in con.execute(
        f"SELECT id FROM tasks WHERE run_id=? AND status IN ({terminal}) ORDER BY id LIMIT ?",
        (rid, *state.TASK_TERMINAL, MAX_TASKS + 1))]
    total = con.execute("SELECT COUNT(*) FROM tasks WHERE run_id=?", (rid,)).fetchone()[0]
    done_count = con.execute(f"SELECT COUNT(*) FROM tasks WHERE run_id=? AND status IN ({terminal})",
                             (rid, *state.TASK_TERMINAL)).fetchone()[0]
    tasks = {"open": open_tasks, "done": done_tasks, "done_count": done_count, "total": total}
    gates = [dict(r) for r in con.execute(
        "SELECT id, kind, subject, task_id, status, verdict, round FROM gates WHERE run_id=? "
        "AND status NOT IN ('done','stale') ORDER BY created_at DESC LIMIT ?", (rid, MAX_ITEMS + 1))]
    findings = [dict(r) for r in con.execute(
        "SELECT id, task_id, code, severity, summary FROM findings WHERE run_id=? AND state='open' "
        "AND COALESCE(blocking, 1)=1 ORDER BY created_at DESC LIMIT ?", (rid, MAX_ITEMS + 1))]
    amendments = [dict(r) for r in con.execute(
        "SELECT id, seq, class, status, from_plan_version, to_plan_version FROM amendments WHERE run_id=? "
        "ORDER BY seq DESC LIMIT 5", (rid,))]
    auths = [dict(r) for r in con.execute(
        "SELECT kind, target, requirements_version, created_at FROM authorizations WHERE run_id=? "
        "AND revoked_at IS NULL ORDER BY created_at DESC LIMIT ?", (rid, MAX_ITEMS))]
    evidence = [dict(r) for r in con.execute(
        "SELECT id, kind, task_id, gate_id, path, sha256 FROM evidence WHERE run_id=? ORDER BY created_at DESC "
        "LIMIT ?", (rid, MAX_ITEMS + 1))]
    visuals = [dict(r) for r in con.execute(
        "SELECT id, name, version, path, sha256 FROM visual_refs WHERE run_id=? ORDER BY created_at DESC LIMIT ?",
        (rid, MAX_ITEMS + 1))]
    events = []
    if since is not None:
        events = [dict(r) for r in con.execute(
            "SELECT seq, kind, task_id, summary, created_at FROM events WHERE run_id=? AND seq>? ORDER BY seq "
            "LIMIT ?", (rid, since, MAX_ITEMS + 1))]
    seq, updated = _cursor(con, rid)
    return {"run": run, "req": dict(req) if req else None, "plan": plan, "tasks": tasks, "gates": gates,
            "findings": findings, "amendments": amendments, "auths": auths, "evidence": evidence,
            "visuals": visuals, "events": events, "seq": seq, "updated": updated}


def snapshot(con, run: dict, *, since: int | None = None) -> dict:
    if con.in_transaction:
        raw = _read(con, run["id"], since)
    else:
        con.execute("BEGIN")  # one read snapshot (WAL): concurrent writers cannot mix states into it
        try:
            raw = _read(con, run["id"], since)
        finally:
            con.execute("COMMIT")
    r, plan = raw["run"], raw["plan"]
    stale: list[str] = []
    try:
        from office import guide
        nxt = guide.next_action(con, r)
    except Exception as exc:  # the snapshot still reports state when the guide cannot
        nxt = None
        stale.append(f"next action unavailable ({type(exc).__name__})")
    if _cursor(con, r["id"]) != (raw["seq"], raw["updated"]):
        stale.append("run changed while this snapshot was assembled; rerun office context")
    pinned, current = r.get("office_version"), version.current()
    if pinned and not version.same_line(pinned, current):
        stale.append(f"run pinned to office {pinned}, this runtime is {current} (another release line)")
    draft = _plan_draft(r, plan)
    if draft["status"] == "differs":
        stale.append(f"plan draft differs from stored p{plan['version']}: {draft['path']}")
    if plan and r.get("plan_version") and plan["version"] != r["plan_version"]:
        stale.append(f"run names plan p{r['plan_version']} but the latest stored plan is p{plan['version']}")
    if raw["req"] and r.get("requirements_version") and raw["req"]["version"] != r["requirements_version"]:
        stale.append(f"run names requirements r{r['requirements_version']}, latest stored r{raw['req']['version']}")
    artifacts, budget = [], [HASH_BUDGET_BYTES]
    seen: set[str] = set()  # rows are newest first: only a path's latest row is checked
    for kind, rows in (("evidence", raw["evidence"]), ("visual", raw["visuals"])):
        for row in rows[:MAX_ITEMS]:
            # Office rewrites some evidence in place (check-<i>.log on a gate retry); an older row of a path
            # with a newer row is superseded, never stale.
            key = f"{kind}:{row['path']}"
            status = "superseded" if row["path"] and key in seen else _artifact(row["path"], row["sha256"], budget)
            if row["path"]:
                seen.add(key)
            artifacts.append({"kind": kind, "id": row["id"], "path": row["path"], "sha256": row["sha256"],
                              "status": status})
            if status in ("missing", "stale"):
                stale.append(f"{kind} {row['id']} {status}: {row['path']}")
    open_tasks, done_tasks = raw["tasks"]["open"], raw["tasks"]["done"]
    req = raw["req"]
    return _bound({
        "run": {"id": r["id"], "goal": _clip(r.get("goal"), 400), "phase": r.get("phase"), "gear": r.get("gear"),
                "office_version": pinned, "runtime_version": current},
        "requirements": ({"version": req["version"], "sha256": sha256_bytes(req["frozen_json"].encode()),
                          "done": [_clip(x) for x in ((loads(req["frozen_json"], {}) or {}).get("done_criteria") or [])[:10]]}
                         if req else None),
        "plan": ({"version": plan["version"], "kind": plan["kind"], "sha256": plan["content_hash"]} if plan else None),
        "plan_draft": draft,
        "tasks": {"open": [{k: (_clip(v) if k == "title" else v) for k, v in t.items() if v is not None}
                           for t in open_tasks[:MAX_TASKS]],
                  "accepted_or_cancelled": done_tasks[:MAX_TASKS], "accepted_or_cancelled_count": raw["tasks"]["done_count"],
                  "total": raw["tasks"]["total"]},
        "pending_gates": raw["gates"][:MAX_ITEMS],
        "blocking_findings": [{**f, "summary": _clip(f["summary"])} for f in raw["findings"][:MAX_ITEMS]],
        "amendments": raw["amendments"],
        "authorizations": raw["auths"],
        "artifacts": artifacts,
        "events": [{**e, "summary": _clip(e["summary"])} for e in raw["events"][:MAX_ITEMS]],
        "events_truncated": len(raw["events"]) > MAX_ITEMS,
        "truncated": {k: True for k, rows in (("gates", raw["gates"]), ("findings", raw["findings"]),
                                              ("evidence", raw["evidence"]), ("visuals", raw["visuals"]))
                      if len(rows) > MAX_ITEMS} | ({"tasks": True} if len(open_tasks) > MAX_TASKS or len(done_tasks) > MAX_TASKS else {}),
        "next": nxt,
        "cursor": {"event_seq": raw["seq"], "since": since, "run_updated_at": raw["updated"]},
        "generated_at": now_iso(),
        "stale": stale[:MAX_ITEMS * 2] + ([f"{len(stale) - MAX_ITEMS * 2} more stale reference(s)"]
                                          if len(stale) > MAX_ITEMS * 2 else []),
    })


def _clip_all(obj):
    if isinstance(obj, str):
        return _clip(obj, 400)
    if isinstance(obj, list):
        return [_clip_all(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _clip_all(v) for k, v in obj.items()}
    return obj


def _bound(snap: dict) -> dict:
    """The --json payload obeys the same bounds as the text: every string clipped,
    and the longest lists halved (and marked truncated) until the whole fits."""
    import json
    snap = _clip_all(snap)
    lists = (("events",), ("artifacts",), ("tasks", "open"), ("tasks", "accepted_or_cancelled"),
             ("pending_gates",), ("blocking_findings",), ("authorizations",), ("stale",))
    while len(json.dumps(snap, default=str)) > MAX_JSON_CHARS:
        keys = max(lists, key=lambda ks: len(json.dumps(_get(snap, ks), default=str)))
        items = _get(snap, keys)
        if len(items) <= 1:
            break
        parent = snap if len(keys) == 1 else snap[keys[0]]
        parent[keys[-1]] = items[: len(items) // 2]
        snap["truncated"]["/".join(keys)] = True
    return snap


def _get(snap: dict, keys: tuple):
    node = snap
    for k in keys:
        node = node[k]
    return node


def render(snap: dict) -> list[str]:
    r = snap["run"]
    lines = [f"run {short(r['id'])} ({r['phase']}) gear {r['gear']} office {r['office_version']}",
             f"goal: {r['goal']}"]
    req, plan, draft = snap["requirements"], snap["plan"], snap["plan_draft"]
    lines.append("requirements " + (f"r{req['version']} {req['sha256'][7:19]}" if req else "none")
                 + " | plan " + (f"p{plan['version']} {plan['kind']} {plan['sha256'][7:19]}" if plan else "none")
                 + f" | draft {draft['status']}")
    for d in (req or {}).get("done", []):
        lines.append(f"  done: {d}")
    t = snap["tasks"]
    lines.append(f"tasks: {t['accepted_or_cancelled_count']}/{t['total']} accepted or cancelled")
    for task in t["open"]:
        lines.append(f"  {task['id']} {task['status']}: {task.get('title', '')}"
                     + (f" (paused: {_clip(task['pause_reason'], 80)})" if task.get("pause_reason") else ""))
    for g in snap["pending_gates"]:
        lines.append(f"gate {g['id']} {g['kind']} {g['task_id'] or g['subject']} {g['status']} round {g['round']}")
    for f in snap["blocking_findings"]:
        lines.append(f"blocking {f['task_id'] or ''} {f['code'] or f['id']}: {f['summary']}")
    for a in snap["amendments"][:3]:
        lines.append(f"amendment {a['id']} #{a['seq']} {a['class']} {a['status']}")
    for a in snap["authorizations"][:5]:
        lines.append(f"authorized {a['kind']} {a['target'] or ''} r{a['requirements_version']}")
    bad = [a for a in snap["artifacts"] if a["status"] != "ok"]
    lines.append(f"artifacts: {len(snap['artifacts'])} referenced, {len(bad)} not verified ok (office inspect evidence)")
    for e in snap["events"]:
        lines.append(f"  #{e['seq']} {e['kind']} {e['task_id'] or ''}: {e['summary']}")
    if snap["events_truncated"]:
        lines.append(f"  more events after #{snap['events'][-1]['seq']}: office context --since {snap['events'][-1]['seq']}")
    c = snap["cursor"]
    lines.append(f"cursor: event #{c['event_seq']} | run updated {c['run_updated_at']} | snapshot {snap['generated_at']}")
    if snap["truncated"]:
        lines.append("truncated: " + ", ".join(sorted(snap["truncated"])) + " (office inspect run for the rest)")
    lines.extend(f"stale: {s}" for s in snap["stale"]) if snap["stale"] else lines.append("stale: none")
    out, used = [], 0
    for line in lines:
        used += len(line) + 1
        if used > MAX_CHARS:
            out.append("… output bounded; office inspect run|task|evidence for detail")
            break
        out.append(line)
    return out


def context(con, run: dict, *, since: int | None = None) -> Result:
    snap = snapshot(con, run, since=since)
    return Result(lines=render(snap), next=snap["next"] or "office status", data=snap)
