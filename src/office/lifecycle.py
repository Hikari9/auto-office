"""Run lifecycle: start, resume, close, abandon, list.

`start` is the only operation that creates a run. Opening a session, a hook, or
a status call never does.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from office import config as cfg
from office import db, discovery, jobs, legacy, paths, planpath, root_wake, scoring, state, version
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


def _safe_start_base(repo: Path, explicit: str | None) -> str:
    # A task PR targets the default branch. A feature-only run base silently
    # includes unrelated commits in every PR (#678); refuse before run creation.
    candidate = paths.git(repo, "rev-parse", explicit or "HEAD")
    default = paths.git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD",
                        check=False).strip()
    if not default:
        # Some clones have a tracking branch but no origin/HEAD symref.
        # Only infer main/master when exactly one exists; never guess between
        # two different remote branches or make a network call during start.
        refs = [ref for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master")
                if subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "-q", ref],
                                  capture_output=True).returncode == 0]
        if len(refs) == 1:
            default = refs[0]
        elif len(refs) > 1:
            raise Usage("ambiguous-default-branch",
                        "origin/HEAD is not set and both origin/main and origin/master exist, "
                        "so the default branch cannot be determined to check the run base",
                        next_step="run git remote set-head origin -a to configure origin/HEAD, then retry")
    if not default:
        return candidate  # Local-only/offline worktrees retain existing behavior.
    remote_base = paths.git(repo, "rev-parse", f"{default}^{{commit}}")
    check = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", candidate, remote_base],
                           capture_output=True)
    if check.returncode == 1:
        ref = default.removeprefix("refs/remotes/")
        raise Usage("unsafe-run-base",
                    f"run base {candidate[:12]} contains commits not in tracked default {ref}; "
                    "starting here would put unrelated feature commits into task PRs",
                    next_step=f"start this goal from the default branch, or use office start --base {ref} "
                              "with the same goal")
    if check.returncode != 0:
        raise Usage("base-check-failed", "cannot prove run base is on the tracked default branch",
                    next_step="inspect the repository's git refs before starting this run")
    if explicit and candidate != remote_base:
        ref = default.removeprefix("refs/remotes/")
        raise Usage("stale-run-base", f"explicit run base {candidate[:12]} is behind fetched default "
                    f"{ref} at {remote_base[:12]}; upstream changes must not enter the task's scope diff",
                    next_step=f"retry office start --base {ref} with the same goal")
    # The caller may be detached or its local main may lag the already-fetched
    # remote. Pin the fetched commit without moving either local branch (#734).
    return remote_base


def start(goal: str, *, cwd: Path | None = None, gear: str | None = None, playbook: str = "Change",
          blast_radius: str | None = None, size_class: str | None = None, irreversible: bool = False,
          volume: bool = False, interview: bool = False, adversarial: bool = False,
          sets: list[str] | None = None, harness: str | None = None, session: str | None = None,
          base: str | None = None, planner: str | None = None, issue: str | None = None,
          no_prs: bool = False, end_state: str | None = None, deploy: dict | None = None,
          benchmark_refresh: bool = False, from_run: str | None = None,
          plan_review_rounds: int | None = None, review_rounds: int | None = None) -> Result:
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
    caller_top, common = ident
    # One read of the config files feeds both the pinned policy and the raw
    # file blocks recorded as its drift baseline (no defaults, no --set), so an
    # edit during start cannot pin one value and record another; a later drift
    # check compares files to files, and neither a launch override nor a
    # changed shipped default reads as an edit.
    files = cfg.read_files(caller_top)
    try:
        config, warnings = cfg.resolve(caller_top, sets, files=files)
    except ValueError as exc:
        raise Usage("bad-config", f"config is invalid: {exc}",
                    next_step="fix ~/.config/auto-office/config.yaml or .auto-office/config.yaml, then retry")
    pinned = {**config, cfg.FILE_BLOCKS_KEY: cfg.file_blocks(caller_top, files)}
    # For new XS-XL runs complexity is not a risk signal. Existing explicit
    # legacy presets retain their exact historical size/risk interpretation.
    legacy = gear in cfg.LEGACY_GEARS
    risk = cfg.resolve_risk(config, blast_radius, size_class if legacy else None, irreversible)
    if not legacy:
        risk["size_class"] = size_class
        risk["complexity_only"] = True
        if size_class and not risk.get("classified_by"):
            risk["classified_by"] = "intake"
    gear = cfg.fit_gear(gear, risk, volume, interview, adversarial)
    if gear not in cfg.GEARS:
        raise Usage("bad-gear", f"unknown gear {gear!r}", next_step="use one of " + ", ".join(cfg.GEARS))
    try:
        gates = cfg.resolve_gates(gear, risk, config)
    except ValueError as exc:
        raise Usage("bad-config", f"config is invalid: {exc}", next_step="fix review.contract, then retry")
    gates["run_gear_rationale"] = cfg.gear_rationale(gear, risk, volume=volume,
                                                      interview=interview, adversarial=adversarial)
    if plan_review_rounds is not None:
        # The user's intake choice (#418): the initial plan review's round cap,
        # pinned for the run's life. Config never stands in for it.
        if not gates["plan_review"]:
            raise Usage("no-plan-review", f"gear {gear} funds no plan review, so it has no rounds to set",
                        next_step="drop --plan-review-rounds, or pick a gear that funds plan review")
        from office import plans
        gates["plan_review_max_rounds"] = plans.check_round_cap(plan_review_rounds)
        gates["plan_review_rounds_by"] = "user"
    if review_rounds is not None:
        # The convergence-review cap for this run (#423), pinned like the config value.
        from office import contract
        if gates.get("review_contract") != contract.CONVERGENCE:
            raise Usage("no-convergence-contract", "--review-rounds sets the convergence-v1 round cap; this run "
                        "pins the v3.1 contract", next_step="drop --review-rounds")
        gates["convergence_max_rounds"] = contract.check_round_cap(review_rounds)
        gates["visual_review_max_rounds"] = gates["convergence_max_rounds"]
    # HEAD and relative refs belong to the caller, even when its linked
    # checkout is disposable. All later Git work uses the durable anchor.
    base_sha = _safe_start_base(caller_top, base)
    top = paths.repository_anchor(common, checkout=caller_top)
    if top is None:
        raise Usage("repository-unavailable", f"cannot locate a durable repository root for {common}",
                    next_step="restore the main checkout or shared Git directory, then retry office start")
    run_id = new_run_id()
    sdir = paths.run_dir(run_id)
    sdir.mkdir(parents=True, exist_ok=True)
    os.chmod(sdir, 0o700)
    hashes = cfg.snapshot_hashes()
    exact = version.current()
    ver = version.release_line(exact)  # the run is pinned to MAJOR.MINOR; plugin_commit keeps the exact creator
    planner_mode = planner or ("dedicated" if gates["dedicated_planner"] else "inline")
    plan_review = {"required": bool(gates["plan_review"]), "ended": False, "lifecycle": "initial"}
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
        with root_wake.rebind_fence(con, discovery.session_keys(harness, session)), db.transaction(con):
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
            if gates.get("plan_review_rounds_by") == "user":
                state.emit(con, run, "plan.review_rounds", f"the user set the initial plan review to at most "
                           f"{gates['plan_review_max_rounds']} substantive round(s)",
                           payload={"max_rounds": gates["plan_review_max_rounds"]})
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
        # Inline planners must be able to write the plan path advertised by next.
        # Keep PLAN.md absent until the orchestrator actually authors it.
        if planner_mode == "inline":
            planpath.draft(top, run).parent.mkdir(parents=True, exist_ok=True)
        if source and source.get("plan"):
            draft = planpath.draft(top, run)
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text(source["plan"]["body"], encoding="utf-8")
        if planner_mode == "dedicated":
            jobs.kick(con, run_id)
        res = Result()
        res.add(f"{short(run_id)} planning | gear {gear} | {planner_note} | review contract {gates['review_contract']}")
        res.add(f"run gear why: {gates['run_gear_rationale']}")
        if gear in cfg.LEGACY_GEARS:
            res.add(f"compatibility notice: {gear} is a historical preset, not an XS-XL run gear; "
                    "use --gear XS|S|M|L|XL for new work; original review/risk gates remain pinned")
        res.add(f"funded gates: plan review={bool(gates['plan_review'])}, independent convergence="
                f"{bool(gates['code_review'])}, browser={bool(gates['visual'])}; "
                "scope, checks, authorization and landing remain mandatory")
        if source:
            res.add(f"derived from {short(source['id'])}: requirements carried over; "
                    + (f"its plan p{source['plan']['version']} is the draft at {planpath.display(run)} (review it, then "
                       "office submit)" if source.get("plan") else "no plan to carry"))
        res.add("routing policy: allow / neutral / deny per role; a new route is neutral and eligible | "
                "office route policy --effective --role <role>")
        if planner_problem:
            res.notices.append(planner_problem)
        if top != caller_top:
            res.notices.append(f"run repository: {top}; the caller's selected base and configuration are pinned")
        moved = planpath.relocate_legacy(con, top)
        if moved:
            res.notices.append(moved)
        if deploy:
            from office import land
            res.notices += [f"warning: {w}" for w in land.deploy_path_warnings(
                top, {f"deploy_{k}": v for k, v in deploy.items()}, land.env_files(config))]
        if risk["classification"] == "unknown":
            res.notices.append("risk is unclassified: independent code review is required until the plan declares "
                               "`blast_radius` (or `office start --blast-radius ...`); unknown risk is never low")
        if planner_mode == "dedicated":
            res.next = "no action; the plan will return here (office status)"
        else:
            res.next = f"write the plan to {planpath.display(run)} (office submit --help shows the format), then office submit"
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
    with root_wake.rebind_fence(con, [] if worker else keys), db.transaction(con):
        # Binding writes repository-local indexes. Verify/recover the Git
        # root before those writes can recreate a deleted directory (#704).
        reconcile(con, run)
        run = state.get_run(con, run["id"])
        if not worker:
            # Only `office resume <run>` is an explicit choice that wins across repositories;
            # a bare resume binds by cwd and yields like an `office start` binding.
            # A bare resume that resolved through an explicit binding keeps it explicit (R3-9).
            bound, how = discovery._binding(con, keys)
            kept = target.source == "session" and bound is not None and bound["id"] == run["id"] and how == "resume"
            discovery.bind(con, run, keys, "resume" if target.source == "flag" or kept else "resume-cwd")
        if not worker:
            # An explicit resume is the retry for an integration that failed on
            # something fixed outside Office (a stray worktree, missing deps).
            from office import integration
            integration.retry_failed(con, state.get_run(con, run["id"]))
            from office import gates
            for t in state.tasks(con, run["id"]):
                gates.rerun_unavailable_checks(con, state.get_run(con, run["id"]), t)
                # An UNAVAILABLE or ATTENTION task review retries on the same revision: no round, no executor, pin kept.
                gates.rerun_unavailable_review(con, state.get_run(con, run["id"]), state.get_task(con, run["id"], t["id"]))
            from office import contract
            if contract.is_convergence(run):
                # Runtime/evidence failures retry on the same revision; no round is spent.
                from office import convergence, plans
                convergence.retry_blocked(con, state.get_run(con, run["id"]))
                current = state.get_run(con, run["id"])
                # Retries only a round of an open cycle; a closed one stays closed (plans.can_auto_queue).
                if (current.get("plan_review") or {}).get("status") in ("unavailable", "attention") and current["plan_version"]:
                    plans.queue_plan_review(con, current, current["plan_version"])
    jobs.kick(con, run["id"])
    from office import guide
    return guide.status(con, state.get_run(con, run["id"]), resumed=True)


def reconcile(con, run: dict) -> list[str]:
    """Repair runtime-owned state after an interruption. Caller holds the tx."""
    notes = []
    repository_note = reconcile_repository(con, run)
    if repository_note:
        notes.append(repository_note)
        run = state.get_run(con, run["id"])
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
    # A submitted revision with no gate still pending has nothing left to
    # trigger its acceptance: it was planned with no gates, or its gates ended
    # while something else held it (an open plan review or blocking finding).
    from office import gates
    notes += [f"{tid} accepted" for tid in gates.reevaluate_submitted(con, run)]
    jobs.reclaim(con, run["id"])
    from office import convergence
    notes += convergence.reconcile_dismissals(con, state.get_run(con, run["id"]))
    # A gate left open after its job failed has no owner and never finishes:
    # settle it the way a failed job does now (#404).
    for g, job in gates.orphaned_gates(con, run):
        jobs.on_permanent_failure(con, run, job, job.get("error") or "worker process died")
        notes.append(f"{g['task_id']} {g['kind']} gate {g['id']}: its job had failed; gate settled")
    return notes


def reconcile_repository(con, run: dict) -> str | None:
    """Recover a removed caller without repinning. Caller holds the transaction."""
    root = Path(run["repo_root"])
    common = Path(run["git_common_dir"])
    # An index writer can recreate .office/ after the checkout disappeared;
    # that directory alone does not make the repository usable again.
    if state.is_terminal(run) or (root.is_dir() and ((root / ".git").exists()
                                                    or root == common and (common / "HEAD").is_file())):
        return None
    anchor = paths.repository_anchor(common)
    if anchor is None:
        raise Refused("repository-unavailable", f"recorded run repository {root} is missing; "
                      f"no available root matches its shared Git directory {common}",
                      scope=f"run {short(run['id'])}", preserved="run state, pinned base, evidence and reviews",
                      next_step=f"restore the repository at {root} or its shared Git directory at {common}, "
                                f"then office resume {short(run['id'])}")
    state.update_run(con, run["id"], repo_root=str(anchor))
    note = f"run repository recovered: {root} was removed; using {anchor} with the same shared Git directory"
    state.emit(con, run, "run.repository_reanchored", note,
               payload={"before": str(root), "after": str(anchor), "git_common_dir": str(common)})
    return note


def close(con, run: dict, *, handoff: str | None = None) -> Result:
    from office import cleanup, closeout, dispatch, gates, guide, jobs
    if state.is_terminal(run):
        # A crash may have committed terminal status before the cleanup tail.
        # Replay does not reopen the run, label learning twice, or repeat pane teardown.
        if run["phase"] != "closed":
            return Result(lines=[f"{short(run['id'])} already {run['phase']}"],
                          final=closeout.done(f"already {run['phase']}, worktrees retained"))
        landing = run.get("landing") or {}
        res = Result(lines=[f"{short(run['id'])} already closed | retrying closeout"])
        retried_jobs = jobs.cancel_run_jobs(con, run["id"])
        if retried_jobs["stopped"]:
            res.add(f"stopped {len(retried_jobs['stopped'])} live job process(es) from the previous close")
        # A landed-externally close stores the PR URL in landing.handoff and
        # neither target nor detail; that landing is already verified, so a
        # replay sweeps instead of re-offering the handoff. The PR must be
        # passed on: _closeout's merged-detection reads it, not the status.
        external = landing.get("status") == "landed-externally"
        _closeout(con, run, landing, res,
                  handoff=None if external else landing.get("handoff"),
                  pr=landing.get("pr") if external else None)
        res.lines.extend(cleanup.offer(con, run))
        return res
    dispatch.reap_orphans(con, run)
    blockers = gates.close_blockers(con, run)
    landing = gates.landing_state(con, run, handoff)
    if landing["status"] != "landed" and not handoff:
        blockers.append(f"landing not recorded: {landing['detail']}")
    if blockers:
        raise Refused("close-blocked", "cannot close: " + "; ".join(blockers[:4]),
                      scope=f"run {short(run['id'])}", preserved="all work, evidence and reviews",
                      next_step=guide.next_action(con, run))
    from office import deploy_artifacts
    deploy_artifacts.preserve_interrupted(run)
    with db.transaction(con):
        current = state.get_run(con, run["id"])
        if state.is_terminal(current):
            # A concurrent closer finished the transition. Never fake cleanup.
            return Result(lines=[f"{short(run['id'])} already {current['phase']}; retry close"],
                          next=f"office close --run {short(run['id'])}",
                          final=closeout.done("terminal transition committed; retry cleanup"))
        receipt = _archive_receipt(con, current, landing, handoff)
        scoring.label_run_outcomes(con, run["id"], "closed")
        state.update_run(con, run["id"], phase="closed", terminal_at=now_iso(), terminal_reason="closed",
                         archive_digest=receipt["digest"], landing={**landing, "handoff": handoff})
        cancelled_jobs = jobs.cancel_run_jobs_in_transaction(con, run["id"])
        _learn(con, current)
        state.emit(con, current, "run.closed", f"run closed, archive {receipt['digest'][7:19]}")
        _release_all(con, run["id"], "closed")
        _end_bindings(con, run["id"])
    stopped_jobs = jobs.stop_cancelled_jobs(con, cancelled_jobs)
    state.write_projection(con, run["id"])
    from office import dispatch, rerun
    rerun.reclaim_all(run)  # snapshot, then close each dispatch pane
    dispatch.close_herdr_tab(run)
    planpath.remove(Path(run["repo_root"]), run)
    res = Result(lines=[f"{short(run['id'])} closed | archive receipt {receipt['digest'][7:15]}"],
                 data={"archive_digest": receipt["digest"]})
    _append_cancelled_jobs(res, cancelled_jobs, stopped_jobs)
    _append_verification_disclosure(res, receipt)
    _closeout(con, run, landing, res, handoff=handoff)
    res.lines.extend(cleanup.offer(con, run))
    return res


def _closeout(con, run: dict, landing: dict, res: Result, *, handoff: str | None = None, pr: str | None = None) -> None:
    """The /cleanup tail of a close: docs warning, then either the handoff (PR
    ready, everything kept) or, on a real merge, base sync and worktree removal."""
    from office import closeout
    warn = closeout.docs_warning(Path(run["repo_root"]), run.get("base_sha"), landing.get("commit") or landing.get("merge_commit"))
    if warn:
        res.notices.append(warn)
    if handoff:
        owned = {str(t["pr"]["number"]) for t in state.tasks(con, run["id"])
                 if (t.get("pr") or {}).get("number")}
        note = closeout.mark_ready(run["repo_root"], handoff, owned)
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
        proc = subprocess.run(["gh", "pr", "view", ref, "--json", "state,mergeCommit,url,number,baseRefName,headRefOid,headRefName"], cwd=run["repo_root"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused("pr-unverified", f"cannot ask GitHub about {ref}: {exc}", next_step=LANDED_FORM)
    try:
        info = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except ValueError:
        info = {}
    if not isinstance(info, dict) or not info:
        raise Refused("pr-unverified", f"gh pr view {ref} failed: {(proc.stderr or proc.stdout).strip()[:200]}",
                      next_step=LANDED_FORM)
    merge = info.get("mergeCommit") if isinstance(info.get("mergeCommit"), dict) else {}
    if info.get("state") != "MERGED" or not merge.get("oid"):
        raise Refused("pr-not-merged", f"{info.get('url') or ref} is {info.get('state') or 'unknown'}, not merged",
                      next_step="merge it first, or office close --handoff <pr-url> to hand it to the user")
    import re
    if not isinstance(merge["oid"], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge["oid"]):
        raise Refused("pr-unverified", f"GitHub did not return a complete merge commit for {ref}", next_step=LANDED_FORM)
    return {"url": info.get("url") or ref, "number": info.get("number"), "merge_commit": merge["oid"],
            "base": info.get("baseRefName"), "head_commit": info.get("headRefOid"), "branch": info.get("headRefName")}


def _contains(run: dict, commit: str, merge: str) -> bool:
    repo = run["repo_root"]
    if not paths.git(repo, "cat-file", "-t", merge, check=False):
        paths.git(repo, "fetch", "-q", "origin", check=False)  # the merge happened on GitHub
    import subprocess
    return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", commit, merge],
                          capture_output=True).returncode == 0


def close_landed_externally(con, run: dict, ref: str, quote: str | None = None) -> Result:
    from office import land
    if state.is_terminal(run):
        return _close_landed_externally(con, run, ref, quote)
    with land._land_lock(run):
        return _close_landed_externally(con, run, ref, quote)


def _close_landed_externally(con, run: dict, ref: str, quote: str | None = None) -> Result:
    """Close work landed through a merged PR. Actual accepted-commit ancestry
    or the user's explicit quote retain the external-close behavior (#239).
    Squash recovery instead requires the current accepted integration, proven
    PR heads or exact tree equality, and fresh post-merge checks (#705)."""
    from office import cleanup, closeout, landing_recovery
    if state.is_terminal(run) and run["phase"] != "closed":
        raise Refused("run-terminal", f"{short(run['id'])} is abandoned; cannot certify it as landed",
                      preserved="all run evidence")
    pr = _merged_pr(run, ref)
    tasks = state.tasks(con, run["id"])
    accepted = {t["id"]: con.execute("SELECT commit_sha FROM revisions WHERE id=?", (t["accepted_revision_id"],)
                                     ).fetchone()["commit_sha"] for t in tasks if t["status"] == "accepted"}
    contained = sorted(tid for tid, sha in accepted.items() if _contains(run, sha, pr["merge_commit"]))
    unproven = sorted(set(accepted) - set(contained)) + [f"{t['id']} ({t['status']})" for t in tasks
                                                         if t["status"] not in ("accepted", "cancelled")]
    recovered = None
    if unproven and not (quote and quote.strip()):
        recovered = landing_recovery.prove(con, run, pr)
        if recovered:
            accepted = {tid: proof["accepted_commit"] for tid, proof in recovered[0]["tasks"].items()}
            contained = sorted(tid for tid, sha in accepted.items() if _contains(run, sha, pr["merge_commit"]))
            unproven = []
    if (unproven or not accepted) and not (quote and quote.strip()):
        raise Refused("landing-unproven", f"{pr['url']} is merged, but its merge commit {pr['merge_commit'][:12]} does not "
                      f"contain {', '.join(unproven) or 'any accepted revision'}", scope=f"run {short(run['id'])}",
                      preserved="the run", next_step=f"ask the user (native question tool) whether {pr['url']} landed "
                      f"this run's work, then {LANDED_FORM.replace('<merged PR URL>', pr['url'])}")
    landing = {"status": "landed-externally", "pr": pr["url"], "merge_commit": pr["merge_commit"],
               **({"base": pr["base"]} if pr.get("base") else {}),
               "contains": contained, "unproven": unproven, "quote": (quote or "").strip() or None}
    if recovered:
        landing["squash_proof"] = recovered[0]
    if state.is_terminal(run):
        # Previously closed handoff now has a proven merged PR. Keep the
        # original terminal receipt and record the new verified landing proof.
        with db.transaction(con):
            current = state.get_run(con, run["id"])
            if current["phase"] != "closed":
                raise Refused("run-terminal", "terminal run is not closed")
            state.update_run(con, run["id"], landing={**landing, "handoff": None})
        res = Result(lines=[f"{short(run['id'])} external landing verified: {pr['url']}"])
        _closeout(con, state.get_run(con, run["id"]), landing, res,
                  pr=f"PR #{pr['number']}" if pr.get("number") else pr["url"])
        res.lines.extend(cleanup.offer(con, run))
        return res
    from office import deploy_artifacts
    deploy_artifacts.preserve_interrupted(run)
    with db.transaction(con):
        current = state.get_run(con, run["id"])
        if state.is_terminal(current):
            return Result(lines=[f"{short(run['id'])} already {current['phase']}"], next=None,
                          final=closeout.done("terminal transition committed; retry office close"))
        if recovered:
            landing_recovery.require_current(con, current, recovered[1])
        live, cancelled_jobs = _stop_work(con, run)
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
    stopped_jobs = _finish_terminal(con, run, live, cancelled_jobs)
    lines = [f"{short(run['id'])} closed | landed externally via {pr['url']} ({pr['merge_commit'][:12]})"]
    if unproven:
        lines.append(f"not in the merge commit, closed on the user's word: {', '.join(unproven)}")
    if recovered:
        proof = recovered[0]
        lines.append(f"verified {proof['kind']} at {pr['merge_commit'][:12]}; {proof['checks']['summary']}; "
                     "squash proof recorded separately from commit ancestry")
    res = Result(lines=lines + [f"archive receipt {receipt['digest'][7:15]}"], data={"archive_digest": receipt["digest"],
                                                                                   "landing": landing})
    _append_cancelled_jobs(res, cancelled_jobs, stopped_jobs)
    _append_verification_disclosure(res, receipt)
    _closeout(con, run, landing, res, pr=f"PR #{pr['number']}" if pr.get("number") else pr["url"])
    res.lines.extend(cleanup.offer(con, run))
    return res


def _stop_work(con, run: dict) -> tuple[list, list[dict]]:
    """Cancel run jobs and mark live dispatches cancelled in the terminal transaction."""
    from office import jobs
    cancelled = jobs.cancel_run_jobs_in_transaction(con, run["id"])
    live = con.execute("SELECT id, pid FROM dispatches WHERE run_id=? AND status IN ('launching','running')",
                       (run["id"],)).fetchall()
    for row in live:
        con.execute("UPDATE dispatches SET status='cancelled', ended_at=? WHERE id=?", (now_iso(), row["id"]))
    return live, cancelled


def _finish_terminal(con, run: dict, live: list, cancelled_jobs: list[dict]) -> list[str]:
    """After a run ends: stop live agents, close panes and tab, drop the plan draft."""
    from office import jobs
    stopped_jobs = jobs.stop_cancelled_jobs(con, cancelled_jobs)
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
    return stopped_jobs


def abandon(con, run: dict, reason: str) -> Result:
    if not reason or not reason.strip():
        raise Usage("missing-reason", "abandoning needs a reason", next_step='office close --abandon "<reason>"')
    with db.transaction(con):
        current = state.get_run(con, run["id"])
        if state.is_terminal(current):
            return Result(lines=[f"{short(run['id'])} already {current['phase']}"])
        live, cancelled_jobs = _stop_work(con, run)
        receipt = _archive_receipt(con, current, {"status": "abandoned"}, None)
        scoring.label_run_outcomes(con, run["id"], "abandoned")
        state.update_run(con, run["id"], phase="abandoned", terminal_at=now_iso(),
                         terminal_reason=reason.strip()[:500], archive_digest=receipt["digest"])
        _learn(con, current)
        state.emit(con, current, "run.abandoned", f"run abandoned: {reason.strip()[:80]}")
        _release_all(con, run["id"], "abandoned")
        _end_bindings(con, run["id"])
    stopped_jobs = _finish_terminal(con, run, live, cancelled_jobs)
    res = Result(lines=[f"{short(run['id'])} abandoned | work preserved in its worktrees until office prune -f"])
    _append_cancelled_jobs(res, cancelled_jobs, stopped_jobs)
    return res


def _append_cancelled_jobs(res: Result, rows: list[dict], stopped: list[str]) -> None:
    if rows:
        kinds = sorted({row["kind"] for row in rows})
        line = f"cancelled {len(rows)} outstanding outbox job(s) ({', '.join(kinds)})"
        if stopped:
            line += f"; stopped {len(stopped)} live job process(es)"
        res.lines.append(line)


def _release_all(con, run_id: str, reason: str) -> None:
    con.execute("UPDATE leases SET released_at=? WHERE run_id=? AND released_at IS NULL AND revoked_at IS NULL",
                (now_iso(), run_id))


def _end_bindings(con, run_id: str) -> None:
    con.execute("UPDATE session_bindings SET ended_at=? WHERE run_id=? AND ended_at IS NULL", (now_iso(), run_id))


def _verified_test_tiers(con, run: dict) -> dict | None:
    """Record the integration-check scope in closeout, not only in status (#563)."""
    from office import integration, verification_coverage
    commit = integration.final_commit(con, run)
    if not commit:
        return None
    commands = list((run.get("landing") or {}).get("run_checks") or [])
    result = verification_coverage.audit(
        Path(run["repo_root"]), commit, commands,
        verified_commands=verification_coverage.attested_checks(con, run, commit))
    return {"commit": commit, "status": result[0], "detail": result[1]} if result else None


def _append_verification_disclosure(res: Result, receipt: dict) -> None:
    verified = receipt["body"].get("verification") or {}
    if verified.get("status") == "partial":
        res.notices.append("FULL SUITE NOT VERIFIED: " + verified["detail"])
        res.data["verification"] = verified
    elif verified.get("status") == "scoped":
        res.notices.append("SCOPED PR TESTS VERIFIED; full suite not run: " + verified["detail"])
        res.data["verification"] = verified


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
    from office import deploy_artifacts
    body["deploy_artifacts"] = deploy_artifacts.receipt(run)
    verification = _verified_test_tiers(con, run)
    if verification:
        body["verification"] = verification
    from office import contract
    body["review_contract"] = contract.of(run)
    from office import risk as risk_mod
    if risk_mod.summary(run):
        body["risk"] = risk_mod.summary(run)
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
        tag = "" if version.serves(r["office_version"], version.current()) else f"  [on {version.release_line(r['office_version'])}]"
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
