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
          no_prs: bool = False, end_state: str | None = None, deploy: dict | None = None,
          benchmark_refresh: bool = False, from_run: str | None = None) -> Result:
    source = None
    if from_run:
        # #337: moving old work onto the current review contract is an explicit,
        # auditable new run, never an in-place rewrite of the old run.
        scon = db.connect()
        try:
            source = state.find_run(scon, from_run)
            if source is None:
                raise Usage("unknown-run", f"no run {from_run}", next_step="office list --all")
            source = {**source, "frozen": state.current_requirements(scon, source["id"])["frozen"],
                      "plan": state.current_plan(scon, source["id"])}
        finally:
            scon.close()
        goal = goal or source["goal"]
    if not goal or not goal.strip():
        raise Usage("missing-goal", "office start needs a goal", next_step='office start "<goal>"')
    ident = paths.repo_identity(cwd)
    if ident is None:
        raise Usage("no-repository", "office start must run inside a git repository",
                    next_step="cd into the repository, then office start")
    top, common = ident
    # One read of the config files feeds both the pinned policy and the raw
    # file blocks recorded as its drift baseline (no defaults, no --set), so an
    # edit during start cannot pin one value and record another; a later drift
    # check compares files to files, and neither a launch override nor a
    # changed shipped default reads as an edit.
    files = cfg.read_files(top)
    try:
        config, warnings = cfg.resolve(top, sets, files=files)
    except ValueError as exc:
        raise Usage("bad-config", f"config is invalid: {exc}",
                    next_step="fix ~/.config/auto-office/config.yaml or .auto-office/config.yaml, then retry")
    pinned = {**config, cfg.FILE_BLOCKS_KEY: cfg.file_blocks(top, files)}
    risk = cfg.resolve_risk(config, blast_radius, size_class, irreversible)
    gear = cfg.fit_gear(gear, risk, volume, interview, adversarial)
    if gear not in cfg.GEARS:
        raise Usage("bad-gear", f"unknown gear {gear!r}", next_step="use one of " + ", ".join(cfg.GEARS))
    try:
        gates = cfg.resolve_gates(gear, risk["high"], config)
    except ValueError as exc:
        raise Usage("bad-config", f"config is invalid: {exc}", next_step="fix review.contract, then retry")
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
    if source:
        # The user's frozen intent carries over; authorization, plan review and
        # every review record start fresh under this run's contract.
        frozen = {**{k: v for k, v in source["frozen"].items() if k != "goal"}, **{k: v for k, v in frozen.items()
                                                                                  if v not in (None, [], "")},
                  "goal": goal.strip()}
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
                 playbook, base_sha, str(sdir), 1, 0, 1, dumps(pinned), dumps(risk), dumps(gates), dumps([]),
                 dumps(plan_review), planner_mode, now))
            landing = {**({"issue": issue} if issue else {}),
                       **({"prs": {"enabled": False, "reason": "--no-prs"}} if no_prs else {})}
            if source:
                from office import contract
                landing["derived_from"] = {"run": source["id"], "review_contract": contract.of(source),
                                           "plan_version": (source["plan"] or {}).get("version"), "at": now}
            if landing:
                con.execute("UPDATE runs SET landing_json=? WHERE id=?", (dumps(landing), run_id))
            from office import benchmarks
            # The user's intake answer, recorded either way; off unless chosen.
            con.execute("UPDATE runs SET benchmark_refresh_json=? WHERE id=?",
                        (dumps({"enabled": bool(benchmark_refresh), "max": benchmarks.MAX_REFRESHES, "used": 0}), run_id))
            con.execute("INSERT INTO requirements(run_id, version, frozen_json, source, quote, created_at) "
                        "VALUES(?,?,?,?,?,?)", (run_id, 1, dumps(frozen), "start", None, now))
            run = state.get_run(con, run_id)
            state.emit(con, run, "run.started", f"run started: {goal.strip()[:80]}")
            if source:
                from office import contract
                state.emit(con, run, "run.derived", f"derived from run {short(source['id'])} "
                           f"({contract.of(source)}); review state starts fresh under {gates['review_contract']}",
                           payload={"source": source["id"]})
                # Appended to the source run's own history; nothing there is rewritten.
                state.emit(con, state.get_run(con, source["id"]), "run.successor",
                           f"successor run {short(run_id)} carries this work under the {gates['review_contract']} "
                           f"review contract; this run keeps its {contract.of(source)} records",
                           payload={"successor": run_id})
            bound = discovery.bind(con, run, discovery.session_keys(harness, session), "start")
            planner_note = "plan inline"
            if planner_mode == "dedicated":
                from office import dispatch
                dispatch.create_planner_task(con, run, decision=planner_decision)
                planner_note = "planner P1 queued"
        _ensure_local_exclude(common)
        state.write_projection(con, run_id)
        if source and source.get("plan"):
            draft = planpath.draft(top, run)
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text(source["plan"]["body"], encoding="utf-8")
        if planner_mode == "dedicated":
            jobs.kick(con, run_id)
        res = Result()
        res.add(f"{short(run_id)} planning | gear {gear} | {planner_note} | review contract {gates['review_contract']}")
        if source:
            res.add(f"derived from {short(source['id'])}: requirements carried over; "
                    + (f"its plan p{source['plan']['version']} is the draft at {planpath.rel(run)} (review it, then "
                       "office submit)" if source.get("plan") else "no plan to carry"))
        from office import candidates
        report_con = db.connect()
        try:
            trust_summary, _ = candidates.trust_report(report_con)
        finally:
            report_con.close()
        for line in trust_summary:
            res.add(line)
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


def resume(con, target: discovery.Target, *, harness: str | None = None, session: str | None = None,
           cwd: Path | None = None) -> Result:
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
    if not worker:
        # An executor that lost its env must not become the orchestrator.
        here = discovery.task_worktree(con, cwd)
        if here:
            raise discovery.refuse_executor_binding(here[0], here[1], "this task worktree")
        by_session = discovery.executor_session_dispatch(con, keys)
        if by_session:
            raise discovery.refuse_executor_binding(by_session[0], by_session[1], "this session")
    with db.transaction(con):
        if not worker:
            discovery.bind(con, run, keys, "resume")
        reconcile(con, run)
        if not worker:
            # An explicit resume is the retry for an integration that failed on
            # something fixed outside Office (a stray worktree, missing deps).
            from office import integration
            integration.retry_failed(con, state.get_run(con, run["id"]))
            from office import gates
            for t in state.tasks(con, run["id"]):
                gates.rerun_unavailable_checks(con, state.get_run(con, run["id"]), t)
            from office import contract
            if contract.is_convergence(run):
                # Runtime/evidence failures retry on the same revision; no round is spent.
                from office import convergence, plans
                convergence.retry_blocked(con, state.get_run(con, run["id"]))
                current = state.get_run(con, run["id"])
                if (current.get("plan_review") or {}).get("status") in ("unavailable", "attention") and current["plan_version"]:
                    plans.queue_plan_review(con, current, current["plan_version"])
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
    from office import closeout, dispatch, gates, guide
    dispatch.reap_orphans(con, run)
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
            return Result(lines=[f"{short(run['id'])} already {current['phase']}"], next=None,
                          final=closeout.done(f"already {current['phase']}, nothing to do"))
        receipt = _archive_receipt(con, current, landing, handoff)
        scoring.label_run_outcomes(con, run["id"], "closed")
        state.update_run(con, run["id"], phase="closed", terminal_at=now_iso(), terminal_reason="closed",
                         archive_digest=receipt["digest"], landing={**landing, "handoff": handoff})
        _learn(con, current)
        state.emit(con, current, "run.closed", f"run closed, archive {receipt['digest'][7:19]}")
        _release_all(con, run["id"], "closed")
        _end_bindings(con, run["id"])
    state.write_projection(con, run["id"])
    from office import dispatch, rerun
    rerun.reclaim_all(run)  # snapshot, then close each dispatch pane
    dispatch.close_herdr_tab(run)
    planpath.remove(Path(run["repo_root"]), run)
    res = Result(lines=[f"{short(run['id'])} closed | archive receipt {receipt['digest'][7:15]}"],
                 data={"archive_digest": receipt["digest"]})
    _closeout(con, run, landing, res, handoff=handoff)
    return res


def _closeout(con, run: dict, landing: dict, res: Result, *, handoff: str | None = None, pr: str | None = None) -> None:
    """The /cleanup tail of a close: docs warning, then either the handoff (PR
    ready, everything kept) or, on a real merge, base sync and worktree removal."""
    from office import closeout
    warn = closeout.docs_warning(Path(run["repo_root"]), run.get("base_sha"), landing.get("commit") or landing.get("merge_commit"))
    if warn:
        res.notices.append(warn)
    if handoff:
        note = closeout.mark_ready(run["repo_root"], handoff)
        if note:
            res.add(note)
        res.add("handed off: base sync and worktree removal left for after the user merges")
        res.final = closeout.done(f"handed off {handoff}, worktrees kept")
        return
    merged = pr or landing.get("target") or str(landing.get("detail", "")).startswith("task PRs merged")
    if not merged:
        res.add(f"not merged ({landing.get('detail')}): base sync and worktree removal skipped")
        res.final = closeout.done(f"closed without a merge ({landing.get('detail')}), worktrees kept")
        return
    lines, ask, summary = closeout.finish(con, run, landing, pr=pr)
    res.lines.extend(lines)
    if ask:
        res.next = ask
    res.data["closeout"] = {"summary": summary, "ask": ask}
    res.final = closeout.done(summary)


LANDED_FORM = 'office close --landed-externally <merged PR URL> [--quote "<user\'s words>"]'


def _merged_pr(run: dict, ref: str) -> dict:
    """The merged PR `ref` as GitHub reports it: {url, number, merge_commit}."""
    import json
    import subprocess
    try:
        proc = subprocess.run(["gh", "pr", "view", ref, "--json", "state,mergeCommit,url,number,baseRefName"], cwd=run["repo_root"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused("pr-unverified", f"cannot ask GitHub about {ref}: {exc}", next_step=LANDED_FORM)
    try:
        info = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except ValueError:
        info = {}
    if not info:
        raise Refused("pr-unverified", f"gh pr view {ref} failed: {(proc.stderr or proc.stdout).strip()[:200]}",
                      next_step=LANDED_FORM)
    if info.get("state") != "MERGED" or not (info.get("mergeCommit") or {}).get("oid"):
        raise Refused("pr-not-merged", f"{info.get('url') or ref} is {info.get('state') or 'unknown'}, not merged",
                      next_step="merge it first, or office close --handoff <pr-url> to hand it to the user")
    return {"url": info.get("url") or ref, "number": info.get("number"), "merge_commit": info["mergeCommit"]["oid"],
            "base": info.get("baseRefName")}


def _contains(run: dict, commit: str, merge: str) -> bool:
    repo = run["repo_root"]
    if not paths.git(repo, "cat-file", "-t", merge, check=False):
        paths.git(repo, "fetch", "-q", "origin", check=False)  # the merge happened on GitHub
    import subprocess
    return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", commit, merge],
                          capture_output=True).returncode == 0


def close_landed_externally(con, run: dict, ref: str, quote: str | None = None) -> Result:
    """Close a run whose work reached the target through a PR Office did not
    open (#239). The PR must be merged. It is landing evidence on its own when
    its merge commit contains every accepted revision and no task is left;
    otherwise the user's words (--quote) record the override. Open gates,
    blockers, and an unaccepted integration no longer apply: the work has landed."""
    from office import closeout
    if state.is_terminal(run):
        return Result(lines=[f"{short(run['id'])} already {run['phase']}"], next=None,
                      final=closeout.done(f"already {run['phase']}, nothing to do"))
    pr = _merged_pr(run, ref)
    tasks = state.tasks(con, run["id"])
    accepted = {t["id"]: con.execute("SELECT commit_sha FROM revisions WHERE id=?", (t["accepted_revision_id"],)
                                     ).fetchone()["commit_sha"] for t in tasks if t["status"] == "accepted"}
    contained = sorted(tid for tid, sha in accepted.items() if _contains(run, sha, pr["merge_commit"]))
    unproven = sorted(set(accepted) - set(contained)) + [f"{t['id']} ({t['status']})" for t in tasks
                                                         if t["status"] not in ("accepted", "cancelled")]
    if (unproven or not accepted) and not (quote and quote.strip()):
        raise Refused("landing-unproven", f"{pr['url']} is merged, but its merge commit {pr['merge_commit'][:12]} does not "
                      f"contain {', '.join(unproven) or 'any accepted revision'}", scope=f"run {short(run['id'])}",
                      preserved="the run", next_step=f"ask the user (native question tool) whether {pr['url']} landed "
                      f"this run's work, then {LANDED_FORM.replace('<merged PR URL>', pr['url'])}")
    landing = {"status": "landed-externally", "pr": pr["url"], "merge_commit": pr["merge_commit"],
               **({"base": pr["base"]} if pr.get("base") else {}),
               "contains": contained, "unproven": unproven, "quote": (quote or "").strip() or None}
    with db.transaction(con):
        current = state.get_run(con, run["id"])
        if state.is_terminal(current):
            return Result(lines=[f"{short(run['id'])} already {current['phase']}"], next=None,
                          final=closeout.done(f"already {current['phase']}, nothing to do"))
        live = _stop_work(con, run)
        con.execute("UPDATE gates SET status='cancelled', stale_reason='landed externally' WHERE run_id=? "
                    "AND status IN ('queued','running','waiting')", (run["id"],))
        if landing["quote"]:
            import uuid
            con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, authorized_by, quote, "
                        "created_at) VALUES(?,?,?,?,?,?,?,?)", ("Z" + uuid.uuid4().hex[:8], run["id"], "landing", pr["url"],
                                                               current["requirements_version"], "user", landing["quote"], now_iso()))
        receipt = _archive_receipt(con, current, landing, pr["url"])
        scoring.label_run_outcomes(con, run["id"], "closed")
        state.update_run(con, run["id"], phase="closed", terminal_at=now_iso(),
                         terminal_reason=f"landed externally via {pr['url']}", archive_digest=receipt["digest"],
                         landing={**landing, "handoff": pr["url"]})
        _learn(con, current)
        state.emit(con, current, "run.closed", f"run closed: landed externally via {pr['url']}, archive "
                   f"{receipt['digest'][7:19]}")
        _release_all(con, run["id"], "closed")
        _end_bindings(con, run["id"])
    _finish_terminal(con, run, live)
    lines = [f"{short(run['id'])} closed | landed externally via {pr['url']} ({pr['merge_commit'][:12]})"]
    if unproven:
        lines.append(f"not in the merge commit, closed on the user's word: {', '.join(unproven)}")
    res = Result(lines=lines + [f"archive receipt {receipt['digest'][7:15]}"], data={"archive_digest": receipt["digest"],
                                                                                   "landing": landing})
    _closeout(con, run, landing, res, pr=f"PR #{pr['number']}" if pr.get("number") else pr["url"])
    return res


def _stop_work(con, run: dict) -> list:
    """Cancel queued jobs and mark live dispatches cancelled. Caller holds the tx."""
    con.execute("UPDATE outbox SET status='cancelled', finished_at=? WHERE run_id=? AND status='queued'",
                (now_iso(), run["id"]))
    live = con.execute("SELECT id, pid FROM dispatches WHERE run_id=? AND status IN ('launching','running')",
                       (run["id"],)).fetchall()
    for row in live:
        con.execute("UPDATE dispatches SET status='cancelled', ended_at=? WHERE id=?", (now_iso(), row["id"]))
    return live


def _finish_terminal(con, run: dict, live: list) -> None:
    """After a run ends: stop live agents, close panes and tab, drop the plan draft."""
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


def abandon(con, run: dict, reason: str) -> Result:
    if not reason or not reason.strip():
        raise Usage("missing-reason", "abandoning needs a reason", next_step='office close --abandon "<reason>"')
    with db.transaction(con):
        current = state.get_run(con, run["id"])
        if state.is_terminal(current):
            return Result(lines=[f"{short(run['id'])} already {current['phase']}"])
        live = _stop_work(con, run)
        receipt = _archive_receipt(con, current, {"status": "abandoned"}, None)
        scoring.label_run_outcomes(con, run["id"], "abandoned")
        state.update_run(con, run["id"], phase="abandoned", terminal_at=now_iso(),
                         terminal_reason=reason.strip()[:500], archive_digest=receipt["digest"])
        _learn(con, current)
        state.emit(con, current, "run.abandoned", f"run abandoned: {reason.strip()[:80]}")
        _release_all(con, run["id"], "abandoned")
        _end_bindings(con, run["id"])
    _finish_terminal(con, run, live)
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
    from office import contract
    body["review_contract"] = contract.of(run)
    if contract.is_convergence(run):
        from office import convergence
        body["convergence"] = convergence.receipt(con, run)
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


def _learn(con, run: dict) -> None:
    """Route learning at run end (#300): persist outcome attributions and apply
    replay-validated learned-eligibility changes. Caller holds the tx; a learner
    failure rolls back its own writes and never blocks closing the run."""
    from office import candidates, route_learning
    con.execute("SAVEPOINT route_learning")
    try:
        written = route_learning.refresh(con, candidates.learner_priors(con, state.pinned_config(run)))
    except Exception as exc:  # noqa: BLE001 - see docstring
        con.execute("ROLLBACK TO route_learning")
        con.execute("RELEASE route_learning")
        state.emit(con, run, "learner.refresh_failed", f"route learner refresh failed: {exc}"[:200])
        return
    con.execute("RELEASE route_learning")
    for w in written:
        state.emit(con, run, "learner.eligibility", f"{w['role']} {w['route']}: {w['previous_state']} -> {w['state']}",
                   payload={k: w[k] for k in ("id", "role", "route", "state", "previous_state", "evidence", "replay")})
