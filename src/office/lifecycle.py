"""Run lifecycle: start, resume, close, abandon, list.

`start` is the only operation that creates a run. Opening a session, a hook, or
a status call never does.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path

from office import config as cfg
from office import db, discovery, jobs, legacy, paths, planpath, scoring, state, version
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, new_run_id, now_iso, pid_alive, sha256_obj, short


def _ensure_local_exclude(common_dir: Path) -> None:
    """Keep `.office/` out of git without editing a tracked .gitignore."""
    exclude = common_dir / "info" / "exclude"
    try:
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if not any(line.strip() in (".office/", ".office") for line in text.splitlines()):
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as fh:
                if text and not text.endswith("\n"):
                    fh.write("\n")
                fh.write(".office/\n")
    except OSError:
        pass


def start(goal: str, *, cwd: Path | None = None, gear: str | None = None, playbook: str = "Change",
          blast_radius: str | None = None, size_class: str | None = None, irreversible: bool = False,
          volume: bool = False, interview: bool = False, adversarial: bool = False,
          sets: list[str] | None = None, harness: str | None = None, session: str | None = None,
          base: str | None = None, planner: str | None = None, issue: str | None = None,
          no_prs: bool = False, end_state: str | None = None, deploy: dict | None = None) -> Result:
    if not goal or not goal.strip():
        raise Usage("missing-goal", "office start needs a goal", next_step='office start "<goal>"')
    ident = paths.repo_identity(cwd)
    if ident is None:
        raise Usage("no-repository", "office start must run inside a git repository",
                    next_step="cd into the repository, then office start")
    top, common = ident
    config, warnings = cfg.resolve(top, sets)
    risk = cfg.resolve_risk(config, blast_radius, size_class, irreversible)
    gear = cfg.fit_gear(gear, risk, volume, interview, adversarial)
    if gear not in cfg.GEARS:
        raise Usage("bad-gear", f"unknown gear {gear!r}", next_step="use one of " + ", ".join(cfg.GEARS))
    gates = cfg.resolve_gates(gear, risk["high"], config)
    base_sha = paths.git(top, "rev-parse", base or "HEAD")
    run_id = new_run_id()
    sdir = paths.run_dir(run_id)
    sdir.mkdir(parents=True, exist_ok=True)
    os.chmod(sdir, 0o700)
    hashes = cfg.snapshot_hashes()
    exact = version.current()
    ver = version.release_line(exact)  # the run is pinned to MAJOR.MINOR; plugin_commit keeps the exact creator
    planner_mode = planner or ("dedicated" if gates["dedicated_planner"] else "inline")
    plan_review = {"required": bool(gates["plan_review"]), "ended": False}
    frozen = {"goal": goal.strip(), "done_criteria": [], "blast_radius": blast_radius,
              "non_goals": [], "named_actions": []}
    if end_state:
        frozen["end_state"] = end_state  # the user's intake answer; the plan may restate it
    if deploy:
        frozen["deploy"] = deploy
    con = db.connect()
    planner_decision, planner_problem = None, None
    if planner_mode == "dedicated":
        from office import candidates
        provisional = {"id": run_id, "family_id": run_id, "playbook": playbook, "gear": gear, "office_version": ver}
        planner_decision = candidates.route_role(con, config, provisional, "planner")
        if planner_decision.get("status") != "selected":
            planner_problem = f"no qualifying planner route ({planner_decision.get('status')}); planning inline instead"
            planner_mode, planner_decision = "inline", None
    try:
        with db.transaction(con):
            now = now_iso()
            con.execute(
                "INSERT INTO runs(id, family_id, created_at, plugin_commit, policy_hash, catalog_hash, adapter_hash, "
                "config_hash, status, office_version, repo_root, git_common_dir, goal, phase, gear, playbook, base_sha, "
                "state_dir, requirements_version, plan_version, routing_version, policy_json, risk_json, gates_json, "
                "envelope_json, plan_review_json, planner_mode, updated_at, escalations_used) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (run_id, run_id, now, exact, hashes["policy_hash"], hashes["catalog_hash"], hashes["adapter_hash"],
                 sha256_obj(config), "planning", ver, str(top), str(common), goal.strip(), "planning", gear,
                 playbook, base_sha, str(sdir), 1, 0, 1, dumps(config), dumps(risk), dumps(gates), dumps([]),
                 dumps(plan_review), planner_mode, now))
            landing = {**({"issue": issue} if issue else {}),
                       **({"prs": {"enabled": False, "reason": "--no-prs"}} if no_prs else {})}
            if landing:
                con.execute("UPDATE runs SET landing_json=? WHERE id=?", (dumps(landing), run_id))
            con.execute("INSERT INTO requirements(run_id, version, frozen_json, source, quote, created_at) "
                        "VALUES(?,?,?,?,?,?)", (run_id, 1, dumps(frozen), "start", None, now))
            run = state.get_run(con, run_id)
            state.emit(con, run, "run.started", f"run started: {goal.strip()[:80]}")
            bound = discovery.bind(con, run, discovery.session_keys(harness, session), "start")
            planner_note = "plan inline"
            if planner_mode == "dedicated":
                from office import dispatch
                dispatch.create_planner_task(con, run, decision=planner_decision)
                planner_note = "planner P1 queued"
        _ensure_local_exclude(common)
        state.write_projection(con, run_id)
        if planner_mode == "dedicated":
            jobs.kick(con, run_id)
        res = Result()
        res.add(f"{short(run_id)} planning | gear {gear} | {planner_note}")
        from office import candidates
        report_con = db.connect()
        try:
            for line in candidates.trust_report(report_con):
                res.add(line)
        finally:
            report_con.close()
        if planner_problem:
            res.notices.append(planner_problem)
        moved = planpath.relocate_legacy(con, top)
        if moved:
            res.notices.append(moved)
        if planner_mode == "dedicated":
            res.next = "no action; the plan will return here (office status)"
        else:
            res.next = f"write the plan to {planpath.rel(run)} (office submit --help shows the format), then office submit"
        res.data = {"run_id": run_id, "office_version": ver, "gear": gear, "risk": risk, "gates": gates,
                    "planner_mode": planner_mode, "bound": [f"{h}:{s}" for h, s in bound], "warnings": warnings}
        res.verbose = [f"office_version {ver} (created by {exact})", f"state {sdir}", f"base {base_sha[:12]}",
                       f"bindings {', '.join(f'{h}' for h, _ in bound) or 'none (use OFFICE_RUN_ID or --run)'}"]
        return res
    finally:
        con.close()


def resume(con, target: discovery.Target, *, harness: str | None = None, session: str | None = None) -> Result:
    if target.legacy is not None:
        msg, nxt = legacy.guidance(target.legacy)
        return Result(lines=[msg], next=nxt, data={"legacy": True, "run_id": target.legacy.run_id,
                                                   "office_version": target.legacy.office_version})
    run = target.run
    if state.is_terminal(run):
        raise Refused("run-terminal", f"{short(run['id'])} is {run['phase']}; a terminal run cannot resume",
                      next_step='office start "<goal>" for new work')
    keys = discovery.session_keys(harness, session)
    worker = os.environ.get("OFFICE_DISPATCH_ID")
    with db.transaction(con):
        if not worker:
            discovery.bind(con, run, keys, "resume")
        reconcile(con, run)
        if not worker:
            # An explicit resume is the retry for an integration that failed on
            # something fixed outside Office (a stray worktree, missing deps).
            from office import integration
            integration.retry_failed(con, state.get_run(con, run["id"]))
    jobs.kick(con, run["id"])
    from office import guide
    return guide.status(con, state.get_run(con, run["id"]), resumed=True)


def reconcile(con, run: dict) -> list[str]:
    """Repair runtime-owned state after an interruption. Caller holds the tx."""
    notes = []
    for d in con.execute("SELECT * FROM dispatches WHERE run_id=? AND status IN ('launching','running')",
                         (run["id"],)).fetchall():
        d = dict(d)
        if d.get("launcher") in ("herdr", "external"):
            continue
        if d.get("pid") and not pid_alive(d["pid"]):
            con.execute("UPDATE dispatches SET status='exited', terminal_classification=COALESCE(terminal_classification,'lost'), "
                        "ended_at=COALESCE(ended_at, ?) WHERE id=?", (now_iso(), d["id"]))
            notes.append(f"{d['task_id']} worker process is gone")
            from office import dispatch
            dispatch.after_worker_exit(con, run, d["id"])
    # A revision planned with no gates before acceptance was evaluated on
    # submit (or by an older runtime) has nothing left to trigger it.
    from office import gates
    for t in con.execute("SELECT t.id FROM tasks t WHERE t.run_id=? AND t.status='submitted' AND t.current_revision_id IS NOT NULL "
                         "AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.revision_id=t.current_revision_id)",
                         (run["id"],)).fetchall():
        if gates.evaluate_acceptance(con, run, t["id"]):
            notes.append(f"{t['id']} accepted (no gate required by policy)")
    jobs.reclaim(con, run["id"])
    return notes


def close(con, run: dict, *, handoff: str | None = None) -> Result:
    from office import gates, guide
    blockers = gates.close_blockers(con, run)
    landing = gates.landing_state(con, run, handoff)
    if landing["status"] != "landed" and not handoff:
        blockers.append(f"landing not recorded: {landing['detail']}")
    if blockers:
        raise Refused("close-blocked", "cannot close: " + "; ".join(blockers[:4]),
                      scope=f"run {short(run['id'])}", preserved="all work, evidence and reviews",
                      next_step=guide.next_action(con, run))
    with db.transaction(con):
        current = state.get_run(con, run["id"])
        if state.is_terminal(current):
            return Result(lines=[f"{short(run['id'])} already {current['phase']}"], next=None)
        receipt = _archive_receipt(con, current, landing, handoff)
        scoring.label_run_outcomes(con, run["id"], "closed")
        state.update_run(con, run["id"], phase="closed", terminal_at=now_iso(), terminal_reason="closed",
                         archive_digest=receipt["digest"], landing={**landing, "handoff": handoff})
        state.emit(con, current, "run.closed", f"run closed, archive {receipt['digest'][7:19]}")
        _release_all(con, run["id"], "closed")
        _end_bindings(con, run["id"])
    state.write_projection(con, run["id"])
    from office import dispatch, rerun
    rerun.reclaim_all(run)  # snapshot, then close each dispatch pane
    dispatch.close_herdr_tab(run)
    planpath.remove(Path(run["repo_root"]), run)
    return Result(lines=[f"{short(run['id'])} closed | archive receipt {receipt['digest'][7:15]}"],
                  data={"archive_digest": receipt["digest"]})


def abandon(con, run: dict, reason: str) -> Result:
    if not reason or not reason.strip():
        raise Usage("missing-reason", "abandoning needs a reason", next_step='office close --abandon "<reason>"')
    with db.transaction(con):
        current = state.get_run(con, run["id"])
        if state.is_terminal(current):
            return Result(lines=[f"{short(run['id'])} already {current['phase']}"])
        con.execute("UPDATE outbox SET status='cancelled', finished_at=? WHERE run_id=? AND status='queued'",
                    (now_iso(), run["id"]))
        live = con.execute("SELECT id, pid FROM dispatches WHERE run_id=? AND status IN ('launching','running')",
                           (run["id"],)).fetchall()
        for row in live:
            con.execute("UPDATE dispatches SET status='cancelled', ended_at=? WHERE id=?", (now_iso(), row["id"]))
        receipt = _archive_receipt(con, current, {"status": "abandoned"}, None)
        scoring.label_run_outcomes(con, run["id"], "abandoned")
        state.update_run(con, run["id"], phase="abandoned", terminal_at=now_iso(),
                         terminal_reason=reason.strip()[:500], archive_digest=receipt["digest"])
        state.emit(con, current, "run.abandoned", f"run abandoned: {reason.strip()[:80]}")
        _release_all(con, run["id"], "abandoned")
        _end_bindings(con, run["id"])
    for row in live:
        if row["pid"] and pid_alive(row["pid"]):
            try:
                os.killpg(os.getpgid(row["pid"]), signal.SIGTERM)
            except OSError:
                pass
    state.write_projection(con, run["id"])
    from office import dispatch, rerun
    rerun.reclaim_all(run)  # snapshot, then close each dispatch pane
    dispatch.close_herdr_tab(run)
    planpath.remove(Path(run["repo_root"]), run)
    return Result(lines=[f"{short(run['id'])} abandoned | work preserved in its worktrees until office prune -f"])


def _release_all(con, run_id: str, reason: str) -> None:
    con.execute("UPDATE leases SET released_at=? WHERE run_id=? AND released_at IS NULL AND revoked_at IS NULL",
                (now_iso(), run_id))


def _end_bindings(con, run_id: str) -> None:
    con.execute("UPDATE session_bindings SET ended_at=? WHERE run_id=? AND ended_at IS NULL", (now_iso(), run_id))


def _archive_receipt(con, run: dict, landing: dict, handoff: str | None) -> dict:
    tasks = [{"id": t["id"], "status": t["status"], "accepted_revision": t["accepted_revision_id"]}
             for t in state.tasks(con, run["id"])]
    revs = {r["id"]: r["commit_sha"] for r in con.execute(
        "SELECT id, commit_sha FROM revisions WHERE run_id=?", (run["id"],)).fetchall()}
    body = {"run_id": run["id"], "office_version": run["office_version"],
            "requirements_version": run["requirements_version"], "plan_version": run["plan_version"],
            "policy_hash": run["policy_hash"], "config_hash": run["config_hash"], "tasks": tasks,
            "accepted_commits": {t["id"]: revs.get(t["accepted_revision"]) for t in tasks},
            "landing": landing, "handoff": handoff}
    digest = sha256_obj(body)
    try:
        from office.util import atomic_write_json
        atomic_write_json(Path(run["state_dir"]) / "archive-receipt.json", {**body, "digest": digest})
    except OSError:
        pass
    return {"digest": digest, "body": body}


def list_runs(con, *, all_runs: bool = False, cwd: Path | None = None) -> Result:
    ident = paths.repo_identity(cwd)
    if all_runs or ident is None:
        rows = con.execute("SELECT id FROM runs WHERE office_version IS NOT NULL ORDER BY created_at").fetchall()
    else:
        rows = con.execute("SELECT id FROM runs WHERE office_version IS NOT NULL AND git_common_dir=? "
                           "AND phase NOT IN ('closed','abandoned') ORDER BY created_at", (str(ident[1]),)).fetchall()
    runs = [state.get_run(con, r[0]) for r in rows]
    if not all_runs:
        runs = [r for r in runs if not state.is_terminal(r)]
    lines = []
    for r in runs:
        tag = "" if version.same_line(r["office_version"], version.current()) else f"  [on {version.release_line(r['office_version'])}]"
        pr = " pruned" if r.get("pruned_at") else ""
        lines.append(f"{short(r['id'])}  {r['phase']:<10} {r['goal'][:60]}{tag}{pr}")
    legacy_rows = []
    if ident is not None:
        for leg in legacy.legacy_runs(paths.primary_checkout(ident[1])):
            if all_runs or leg.active:
                legacy_rows.append(leg)
                lines.append(f"{short(leg.run_id)}  {leg.phase:<10} {leg.goal[:60]}  [3.0 legacy]")
    if not lines:
        lines = ["no active Office runs here" if not all_runs else "no Office runs recorded"]
    return Result(lines=lines, next="office resume <id> to bind one" if len(runs) + len(legacy_rows) > 1 else None,
                  data={"runs": [{"id": r["id"], "phase": r["phase"], "goal": r["goal"],
                                  "office_version": r["office_version"], "pruned_at": r.get("pruned_at")} for r in runs],
                        "legacy": [{"id": l.run_id, "phase": l.phase, "office_version": l.office_version}
                                   for l in legacy_rows]})
