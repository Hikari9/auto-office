"""`office land`: take accepted work as far as the user's end state says (3.2).

End states (plan requirements `end_state:`, the user's intake answer):
  ask      open PRs, then ask the user: merge, preview deploy, end to end, or stop.
  preview  deploy the verified integration to preview and verify it; PRs stay open.
  merge    merge the task PRs bottom-up.
  e2e      merge, deploy prod from the merged default branch, verify.

The approved plan carries the end state and deploy commands, so the plan
authorization is the authority for them. In `ask` mode the user's later answer
is recorded with `office land --merge|--preview|--e2e --quote "<words>"`.

Merging is bottom-up in the plan's order. A child PR is retargeted to the
default branch once its parent merged; when the repository forces squash or
rebase merges, the child is rebased onto the new base and force-pushed with
lease first. Required checks must pass before each merge, and a branch that
must be up to date is updated once. After the last merge the default branch's
tree is compared with the reviewed integration tree; if main moved meanwhile
the run checks re-run on it before any prod deploy.

A PR GitHub reports not mergeable gets one recovery attempt (`_recover_conflicting`):
if the remote branch is still the recorded head, the PR merges with the default
branch without conflicts, and folding every open task onto it reproduces the
reviewed integration tree, one merge commit of the default branch is pushed to
the task branch (leased to the head just read) and the merge is retried once.
Anything else refuses with the reason and pushes nothing.

`office land --rebase` moves a run onto a default branch that moved after
`office start`: when every accepted revision still merges cleanly onto the
new head, integration re-composes there and re-runs the run checks plus an
independent integration review. A conflict refuses with the by-hand steps.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from office import closeout, db, gates, integration, paths, prs, state
from office.result import Result
from office.state import Refused, Usage
from office.util import now_iso, short

MODES = ("preview", "merge", "e2e")
CHECKS_TIMEOUT = int(os.environ.get("OFFICE_PR_CHECKS_TIMEOUT", "1800"))


def end_state(con, run: dict) -> dict:
    frozen = state.current_requirements(con, run["id"])["frozen"]
    return {"mode": frozen.get("end_state") or "ask", "deploy": frozen.get("deploy") or {}}


def land(con, run: dict, *, mode: str | None = None, quote: str | None = None, redeploy: bool = False,
         mark_deployed: bool = False) -> Result:
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-land", "a worker cannot land the run")
    snap = _snapshot(con, run)
    commit = snap["commit"]
    if not commit:
        blockers = gates.close_blockers(con, run)
        raise Refused("not-ready", "nothing verified to land: " + ("; ".join(blockers[:3]) or "integration not accepted"),
                      next_step="office status")
    es = end_state(con, run)
    if mode:
        if mode not in MODES:
            raise Usage("unknown-mode", f"land mode must be one of {', '.join(MODES)}")
        if not quote or len(re.sub(r"\s+", "", quote)) < 2:
            raise Usage("user-quote-required", "landing beyond PRs records the user's own words",
                        next_step=f'office land --{mode} --quote "<user\'s exact words>"')
        _authorize(con, run, mode, quote)
    else:
        mode = es["mode"]
    if mode == "ask":
        return _ask(con, run)
    if (redeploy or mark_deployed) and mode != "e2e":
        raise Usage("deploy-flag-needs-e2e", "--redeploy and --mark-deployed apply to a prod deploy (--e2e)")
    if redeploy and mark_deployed:
        raise Usage("deploy-flag-conflict", "--redeploy deploys again; --mark-deployed says it is already live; pick one")
    if mode in ("preview", "e2e") and not es["deploy"].get("preview" if mode == "preview" else "prod"):
        raise Refused("no-deploy-command", f"{mode} needs a confirmed deploy_{'preview' if mode == 'preview' else 'prod'} "
                      "command in the plan requirements", next_step='office amend requirements --quote "<words>" -- '
                      '"deploy_prod: <command>" (office land --detect proposes one)')
    with _land_lock(run):
        _require_unchanged(con, run, snap, "before landing")  # it may have moved while this land waited
        if mode == "preview":
            return _land_preview(con, run, snap, es["deploy"])
        return _land_merge(con, run, mode, snap, es["deploy"], redeploy=redeploy, mark_deployed=mark_deployed)


def _snapshot(con, run: dict) -> dict:
    """What a land merges and deploys: the accepted integration commit, the
    accepted revision of every task, and the integration gate verdicts on it."""
    commit = integration.final_commit(con, run)
    tasks = integration.accepted_set(con, run)
    gates_ = con.execute("SELECT id, kind, status, verdict FROM gates WHERE run_id=? AND subject='integration' "
                         "AND input_key=? ORDER BY id", (run["id"], f"integration:{commit}")).fetchall()
    from office import contract
    if contract.is_convergence(run):
        # Lane and shared-scope reviews and waivers are part of what a land stands on.
        from office import convergence
        conv = convergence.receipt(con, run)
        gates_ = list(gates_) + [(s["id"], s["status"], s["commit"]) for s in conv["scopes"]] \
            + [(w["target"], w["by"]) for w in conv["waivers"]]
    return {"commit": commit,
            "accepted": sorted((t["id"], t["accepted_revision_id"]) for t in tasks or []) if tasks is not None else None,
            "gates": [tuple(g) for g in gates_]}


def _require_unchanged(con, run: dict, snap: dict, when: str, preserved: str = "nothing was merged or deployed") -> None:
    now = _snapshot(con, run)
    if now == snap:
        return
    what = [k for k in ("commit", "accepted", "gates") if now[k] != snap[k]]
    raise Refused("integration-changed", f"the integration this land read ({(snap['commit'] or '')[:12]}) changed {when}: "
                  + ", ".join({"commit": f"integration is now {(now['commit'] or 'not accepted')[:12]}",
                               "accepted": "the accepted tasks changed", "gates": "its gate verdicts changed"}[k]
                              for k in what),
                  preserved=preserved, next_step="office status, then office land again once integration is accepted")


@contextlib.contextmanager
def _land_lock(run: dict):
    """One `office land` per run at a time: two concurrent lands would both
    see no deploy record and both deploy. A flock dies with its process."""
    import fcntl
    path = paths.run_dir(run["id"]) / "land.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("land-running", f"another office land is running for run {short(run['id'])}",
                          preserved="the running land", next_step="wait for it to finish, then office land again")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _tree(repo: Path, commit: str) -> str:
    return paths.git(repo, "rev-parse", f"{commit}^{{tree}}")


def _land_preview(con, run: dict, snap: dict, deploy: dict) -> Result:
    res = Result(lines=_waivers(con, run))
    commit = snap["commit"]
    tree = _tree(Path(run["repo_root"]), commit)
    if _deployed(con, run, "preview", tree):
        res.add(f"preview already deployed from this tree ({tree[:12]}); skipped")
    else:
        # A checkout of its own: a compose may remove `_integration`, and another land may run its own deploy.
        with _deploy_checkout(run, commit, "deploy-preview") as (checkout, head, head_tree):
            _require_unchanged(con, run, snap, "before the preview deploy")
            _require_tree(head_tree, tree, "the accepted integration")
            _deploy(con, run, "preview", deploy, checkout, res, commit=head, tree=head_tree)
        _record_deployed(con, run, "preview", head, head_tree)
    _record(con, run, delivered=f"preview deployed from {commit[:12]}; PRs left open for review")
    res.next = closeout.DOCS_STEP + "office close (PRs stay open for the user to merge)"
    return res


def _land_merge(con, run: dict, mode: str, snap: dict, deploy: dict, *, redeploy: bool, mark_deployed: bool) -> Result:
    res = Result(lines=_waivers(con, run))
    if not prs.enabled(run) and not any(prs.has_pr(t) for t in integration.accepted_set(con, run) or []):
        # PRs are off and no accepted task has file scope: nothing to merge or deploy.
        res.add("nothing to merge: no accepted task has a PR (all have no file scope)")
        _record(con, run, delivered="no task PRs to merge")
        res.next = closeout.DOCS_STEP + "office close"
        return res
    commit = snap["commit"]
    repo = Path(run["repo_root"])
    landing = state.get_run(con, run["id"]).get("landing") or {}
    merged = landing.get("merged") or {}
    done = merged.get("commit")
    current = done and _landed_tree(repo, merged) == _tree(repo, commit)
    if done and not current:
        _require_landable_change(con, run, merged, commit)
        res.add(f"the run landed integration {(merged.get('integration_commit') or done)[:12]} at {done[:12]}; "
                f"the accepted integration is now {commit[:12]}: landing it")
    if mark_deployed and not current and _open_prs(con, run):
        # The operator vouches for a tree already on the default branch, never for one this land would merge.
        raise Usage("mark-deployed-open-prs", "--mark-deployed confirms a merged tree is live, but task PRs are still "
                    "open", next_step='office land --e2e --quote "<words>" (merges, then deploys)')
    if current and mark_deployed:
        _record_deployed(con, run, "prod", done, _tree(repo, done), by="operator-confirmed")
    if current and not redeploy and (mode != "e2e" or _deployed(con, run, "prod", _tree(repo, done))):
        res.add(f"already landed at {done[:12]}; merge and deploy steps skipped")
        _close_issue(con, run, done, res)
        res.next = closeout.DOCS_STEP + "office close"
        return res
    if mode == "e2e" and not (redeploy or mark_deployed):
        # Before any merge: a prod state Office cannot read refuses here, with nothing changed.
        _require_deploy_proof(con, run, _tree(repo, done) if current else _tree(repo, commit))
    before = _merge_all(con, run, res)
    before = landing.get("rollback") or before  # a rerun's `before` already holds the merges
    _record(con, run, rollback=before)
    main = _verify_main(con, run, commit, res)
    checked = _tree(repo, main)
    if mode == "e2e":
        tree = _tree(repo, main)
        if not redeploy and _deployed(con, run, "prod", tree):
            res.add(f"prod already deployed from this tree ({tree[:12]}); skipped")
        elif mark_deployed:
            _record_deployed(con, run, "prod", main, tree, by="operator-confirmed")
            res.add(f"prod recorded as live at {main[:12]} on the operator's word; not deployed")
        else:
            if not redeploy:
                _require_deploy_proof(con, run, tree)
            with _deploy_checkout(run, main, "deploy-prod") as (checkout, head, head_tree):
                _require_unchanged(con, run, snap, "before the prod deploy",
                                   preserved=f"the merges; nothing was deployed; rollback target {before[:12]}")
                # The tree _verify_main passed: the reviewed integration tree, or main's re-checked tree.
                _require_tree(head_tree, checked, "the tree that passed the checks")
                _deploy(con, run, "prod", deploy, checkout, res, rollback=before, commit=head, tree=head_tree)
            _record_deployed(con, run, "prod", head, head_tree, by="redeploy" if redeploy else None)
    _close_issue(con, run, main, res)
    _record(con, run, merged={"commit": main, "integration_commit": commit, "rollback": before, "at": now_iso()})
    res.next = closeout.DOCS_STEP + "office close"
    return res


def _require_tree(deployed: str, expected: str, what: str) -> None:
    if deployed != expected:
        raise Refused("deploy-tree-mismatch", f"the deploy checkout's tree {deployed[:12]} is not {what} ({expected[:12]})",
                      preserved="nothing was deployed", next_step="office status, then office land again")


def _landed_tree(repo: Path, merged: dict) -> str:
    """The integration tree a merge record covers. A record from an older Office
    names only the merged default-branch commit, whose tree matches the
    integration unless the branch moved meanwhile; then the trees differ and
    the record reads as covering some other integration (the safe side)."""
    return _tree(repo, merged.get("integration_commit") or merged["commit"])


def _open_prs(con, run: dict) -> list[str]:
    return [t["id"] for t in integration.accepted_set(con, run) or []
            if not (state.get_task(con, run["id"], t["id"]).get("pr") or {}).get("merged")]


def _require_landable_change(con, run: dict, merged: dict, commit: str) -> None:
    """The integration moved after the run landed. `office land` can only land
    the difference through task PRs that are still open."""
    if _open_prs(con, run):
        return
    legacy = not merged.get("integration_commit")
    raise Refused("landed-mismatch",
                  f"run {short(run['id'])} landed at {merged['commit'][:12]}, but the accepted integration is now "
                  f"{commit[:12]} with a different tree, and every task PR is already merged, so office land has nothing "
                  "left to merge it with" + (" (the landing record predates integration tracking: the default branch may "
                                             "simply have moved after the merge)" if legacy else ""),
                  preserved="the merges, the deploy record, and the accepted integration",
                  next_step=("office close if the default branch already holds this work; otherwise " if legacy else "")
                  + "open a PR with the difference, merge it, then office close --landed-externally <PR>")


def _require_deploy_proof(con, run: dict, tree: str) -> None:
    """Refuse when Office cannot tell whether `tree` is already live in prod.

    Only a `deployed.prod` record naming the exact tree proves a deploy, and the
    caller has checked that one is absent. The latest successful prod deploy
    event decides the rest: none, or one of another tree, means this tree never
    went out and the normal path deploys it. An event from an older Office names
    no tree, so it is never credited to any tree. An event naming this tree with
    no verified record after it, or a start with no result, means a land died
    mid-deploy. A failed verify after it was reported; deploying again is its retry."""
    if _deployed(con, run, "prod", tree):
        return
    last, failed_after, started = None, False, None
    for row in con.execute("SELECT kind, payload_json FROM events WHERE run_id=? AND kind IN "
                           "('land.deploy.start','land.deploy','land.verify') ORDER BY seq", (run["id"],)).fetchall():
        payload = json.loads(row["payload_json"] or "{}")
        if payload.get("target") != "prod":
            continue
        if row["kind"] == "land.deploy.start":
            started = payload
        elif row["kind"] == "land.deploy":
            started = None
            if payload.get("exit") == 0:
                last, failed_after = payload, False
        elif payload.get("exit") not in (0, None) and last is not None:
            failed_after = True
    if started is not None and started.get("tree") == tree:
        why = "a prod deploy of this tree started and never reported its result (the land was killed mid-deploy)"
    elif last is None:
        return
    elif not last.get("tree"):
        why = "an earlier prod deploy (recorded before Office named deployed trees) may or may not be this tree"
    elif last["tree"] == tree and not failed_after:
        why = "a prod deploy of this tree ran but no verified finish was recorded (the land stopped mid-deploy)"
    else:
        return
    raise Refused("deploy-unproven", f"run {short(run['id'])}: no deploy record names tree {tree[:12]}; {why}",
                  preserved="the merges and the earlier deploy record",
                  next_step='office land --e2e --redeploy --quote "<words>" (deploy this tree) | '
                            'office land --e2e --mark-deployed --quote "<words>" (it is already live)')


@contextlib.contextmanager
def _deploy_checkout(run: dict, commit: str, name: str):
    """A detached checkout of `commit` at a path unique to this invocation,
    removed afterwards. Yields (path, the commit and tree actually checked out),
    which is what the deployed record names."""
    import uuid
    checkout = gates.detached_checkout(run, commit, f"{name}-{uuid.uuid4().hex[:8]}", purpose="deploy")
    try:
        head = paths.git(checkout, "rev-parse", "HEAD")
        head_tree = paths.git(checkout, "rev-parse", "HEAD^{tree}")
        if head != commit:
            raise Refused("checkout-mismatch", f"the deploy checkout holds {head[:12]}, not {commit[:12]}",
                          preserved="nothing was deployed", next_step="office land again")
        yield checkout, head, head_tree
    finally:
        gates.remove_checkout(run, checkout)


def _deployed(con, run: dict, target: str, tree: str) -> bool:
    """Whether this tree already went out (and verified) to `target`: a land
    run again after it must not deploy the same tree twice."""
    landing = state.get_run(con, run["id"]).get("landing") or {}
    return ((landing.get("deployed") or {}).get(target) or {}).get("tree") == tree


def _record_deployed(con, run: dict, target: str, commit: str, tree: str, by: str | None = None) -> None:
    with db.transaction(con):
        landing = dict(state.get_run(con, run["id"]).get("landing") or {})
        rec = {"commit": commit, "tree": tree, "at": now_iso(), **({"by": by} if by else {})}
        landing["deployed"] = {**(landing.get("deployed") or {}), target: rec}
        state.update_run(con, run["id"], landing=landing)


# ------------------------------------------------------------------ rebase

COMPOSE_BY_HAND = ("compose by hand: a scratch worktree from origin/{base}, cherry-pick each accepted task's commits, "
                   "resolve the conflicts, run the full suite, have an independent reviewer check the resolution, open "
                   "one PR, close the task PRs as superseded, then office close --abandon \"landed as #<N>\"")


def rebase(con, run: dict) -> Result:
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-land", "a worker cannot rebase the run")
    tasks = integration.accepted_set(con, run)
    if tasks is None:
        raise Refused("not-ready", "rebase needs every task accepted", next_step="office status")
    # The land lock keeps a rebase from moving the integration under a running land;
    # the worktree lock is held until the next compose is queued.
    with _land_lock(run), integration.worktree_lock(run):
        res = _rebase_locked(con, run, tasks)
    from office import jobs
    jobs.kick(con, run["id"])
    return res


def job_auto_rebase(con, run: dict, job: dict) -> dict:
    """Integration checks failed on a base the default branch has moved past:
    rebase onto the new head and re-check, as `office land --rebase` would. The
    integrate job that queued this may still hold its claim, so wait it out. A
    refusal (conflict, divergence) leaves the integration blocked with the
    reason and every accepted revision kept."""
    import time
    deadline = time.time() + integration.LOCK_WAIT_SECONDS
    while integration.live_integrate_pids(con, run) and time.time() < deadline:
        time.sleep(1.0)
    run = state.get_run(con, run["id"])

    def block(detail: str, summary: str) -> None:
        # A manual `office land --rebase` that raced this job has already queued
        # the next compose: that integration owns the status, so leave it.
        with db.transaction(con):
            if con.execute("SELECT 1 FROM outbox WHERE run_id=? AND kind='integrate' AND status IN ('queued','claimed')",
                           (run["id"],)).fetchone():
                return
            integration._set_integration(con, run, status="blocked", detail=detail)
            state.emit(con, run, "integration.failed", summary)

    try:
        tasks = integration.accepted_set(con, run)
        if tasks is None:
            raise Refused("not-ready", "a task was reopened after integration ran; nothing to rebase yet")
        # Not rebase(): a job spawned from a worker's command inherits its
        # OFFICE_DISPATCH_ID, and this is the runtime's own step, not the worker's.
        with _land_lock(run), integration.worktree_lock(run, wait=60.0):
            res = _rebase_locked(con, run, tasks)
    except Refused as exc:
        block(f"automatic rebase refused ({exc.category}): {exc.message[:160]}",
              f"INTEGRATION automatic rebase refused: {exc.message[:140]}"
              + (f"; next: {exc.next_step}" if exc.next_step else ""))
        return {"refused": exc.category}
    except Exception as exc:
        # The job fails after its one attempt; without this the integration
        # would read "pending" with nothing queued, and office resume skips it.
        block(f"automatic rebase failed: {str(exc)[:160]}; office land --rebase retries it",
              f"INTEGRATION automatic rebase failed: {str(exc)[:140]}")
        raise
    if integration.compose_base(state.get_run(con, run["id"])) == integration.compose_base(run):
        # Nothing moved after all (a reset or a racing land): the failure stands.
        block(f"run checks failed; {res.lines[0] if res.lines else 'nothing to rebase onto'}",
              "INTEGRATION checks failed and there was nothing to rebase onto")
        return {"rebased": False, "lines": res.lines}
    from office import jobs
    jobs.kick(con, run["id"])
    return {"rebased": True, "lines": res.lines}


def _rebase_locked(con, run: dict, tasks: list[dict]) -> Result:
    integration.refuse_if_integrating(con, run)  # an older patch's integrate job holds no flock
    base = prs.settings(con, run).get("base_branch") or "main"
    if any((t.get("pr") or {}).get("merged") for t in tasks):
        raise Refused("already-merging", "some task PRs are merged; office land restacks the rest itself",
                      next_step="office land (merges the rest; a PR GitHub reports conflicting is recovered when it "
                                "provably composes to the reviewed tree, else it refuses with by-hand steps)")
    repo = Path(run["repo_root"])
    if _git(repo, "fetch", "-q", "origin", base).returncode != 0:
        raise Refused("fetch-failed", f"could not fetch origin/{base}")
    new, old = paths.git(repo, "rev-parse", f"origin/{base}"), integration.compose_base(run)
    if _git(repo, "merge-base", "--is-ancestor", new, old).returncode == 0:
        return Result(lines=[f"already on origin/{base} {new[:12]}; nothing to rebase"], next="office land")
    if _git(repo, "merge-base", "--is-ancestor", old, new).returncode != 0:
        raise Refused("base-diverged", f"origin/{base} {new[:12]} does not contain the run base {old[:12]}",
                      next_step=COMPOSE_BY_HAND.format(base=base))
    checkout = gates.detached_checkout(run, new, "rebase-trial", purpose="rebase-trial")
    try:
        genv = dict(os.environ, **paths.commit_identity_env(repo))
        for t in tasks:
            commit = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (t["accepted_revision_id"],)).fetchone()[0]
            proc = subprocess.run(["git", "-C", str(checkout), "merge", "--no-ff", "--no-edit", commit],
                                  capture_output=True, text=True, env=genv)
            if proc.returncode != 0:
                files = paths.git(checkout, "diff", "--name-only", "--diff-filter=U", check=False).replace("\n", ", ")
                raise Refused("rebase-conflict", f"{t['id']} conflicts with origin/{base} {new[:12]} on {files or 'files'}",
                              preserved="the run, its PRs, and its accepted integration",
                              next_step=COMPOSE_BY_HAND.format(base=base))
    finally:
        gates.remove_checkout(run, checkout)
    with db.transaction(con):
        _record(con, run, rebase={"from": old, "onto": new, "at": now_iso()})
        run = state.get_run(con, run["id"])
        integration._set_integration(con, run, status="pending", detail=f"rebasing onto {new[:12]}")
        state.emit(con, run, "integration.rebase", f"rebased onto origin/{base} {new[:12]}; integration re-check queued")
        from office import contract
        if contract.is_convergence(run):
            # The rebase is a shared composition boundary (S-rebase): reviewed once, then integration.
            from office import convergence
            convergence.requeue_all(con, run)
        else:
            integration.retrigger(con, run)
    return Result(lines=[f"every accepted task merges cleanly onto origin/{base} {new[:12]}",
                         "integration re-composes there and re-runs the run checks and "
                         + ("one review of the rebase scope (S-rebase)" if contract.is_convergence(run)
                            else "an integration review")],
                  next="office status (then office land once integration is accepted)")


# ------------------------------------------------------------------ ask

def _waivers(con, run: dict) -> list[str]:
    from office import convergence
    return convergence.waiver_lines(con, run)


def _ask(con, run: dict) -> Result:
    lines = _waivers(con, run) + ["integration verified; the task PRs are ready:"]
    for t in integration._topo(integration.accepted_set(con, run) or []):
        if not prs.has_pr(t):
            continue
        pr = t.get("pr") or {}
        lines.append(f"  {t['id']} {pr.get('url') or '(no PR: ' + _pr_reason(run) + ')'}")
    return Result(lines=lines, next='ask the user (native question tool): merge | preview deploy | merge + prod | stop; '
                  'then office land --merge|--preview|--e2e --quote "<words>", or office close --handoff <pr-url>')


def _pr_reason(run: dict) -> str:
    return ((run.get("landing") or {}).get("prs") or {}).get("reason") or "PRs off"


def _authorize(con, run: dict, mode: str, quote: str) -> None:
    import uuid
    from office.util import dumps
    kinds = {"preview": ["deploy"], "merge": ["merge"], "e2e": ["merge", "deploy"]}[mode]
    with db.transaction(con):
        for kind in kinds:
            con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, envelope_json, "
                        "authorized_by, quote, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("Z" + uuid.uuid4().hex[:8], run["id"], kind, f"land:{mode}", run["requirements_version"],
                         dumps(run.get("envelope") or []), "user", quote.strip(), now_iso()))
        state.emit(con, run, "authority.land", f"user authorized land {mode}", audience="runtime")


# ------------------------------------------------------------------ merge

def _gh(run: dict, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=run["repo_root"], capture_output=True, text=True, timeout=timeout)


def _git(cwd, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)


def _accepted_commit(con, task: dict) -> str:
    return con.execute("SELECT commit_sha FROM revisions WHERE id=?", (task["accepted_revision_id"],)).fetchone()[0]


NOT_MERGEABLE = re.compile(r"not mergeable|cannot be cleanly created", re.I)


def _merge_all(con, run: dict, res: Result) -> str:
    """Merge every task PR bottom-up. Returns the default branch's commit
    before the first merge (the rollback target)."""
    run = state.get_run(con, run["id"])
    s = prs.settings(con, run)
    if not s.get("enabled"):
        raise Refused("prs-off", f"merging needs task PRs, which are off ({s.get('reason')})",
                      next_step="push the integration branch, open a PR, then office close --handoff <pr-url>")
    base, method = s["base_branch"], s["merge_method"]
    repo = Path(run["repo_root"])
    _git(repo, "fetch", "-q", "origin", base)
    before = paths.git(repo, "rev-parse", f"origin/{base}")
    merged: list[str] = []
    for t in integration._topo(integration.accepted_set(con, run) or []):
        t = state.get_task(con, run["id"], t["id"])
        if not prs.has_pr(t):
            res.add(f"{t['id']} has no file scope and no PR; skipped")
            continue
        pr = t.get("pr") or {}
        if not pr.get("number"):
            raise Refused("no-pr", f"{t['id']} has no PR", next_step="office status (a pr.error notice names why)")
        if pr.get("merged"):
            continue
        up = prs.parent(con, run, t)
        if up is not None and pr.get("base") != base:
            if method != "merge":
                _restack(con, run, t, up, base)
            if _gh(run, "pr", "edit", str(pr["number"]), "--base", base).returncode != 0:
                raise Refused("retarget-failed", f"could not retarget #{pr['number']} to {base}")
            # Recorded at once: a retry after a failed merge must not restack the restacked branch again.
            pr = {**pr, "base": base}
            with db.transaction(con):
                state.update_task(con, run["id"], t["id"], pr=pr)
        _wait_checks(run, pr["number"])
        proc = _gh(run, "pr", "merge", str(pr["number"]), prs.MERGE_FLAGS[method])
        if proc.returncode != 0 and re.search(r"not up to date|behind|out of date", proc.stderr + proc.stdout, re.I):
            _gh(run, "pr", "update-branch", str(pr["number"]))
            _wait_checks(run, pr["number"])
            proc = _gh(run, "pr", "merge", str(pr["number"]), prs.MERGE_FLAGS[method])
        if proc.returncode != 0 and NOT_MERGEABLE.search(proc.stderr + proc.stdout):
            pr = _recover_conflicting(con, run, t, pr, base, res)
            _wait_checks(run, pr["number"])
            proc = _gh(run, "pr", "merge", str(pr["number"]), prs.MERGE_FLAGS[method])
        if proc.returncode != 0:
            raise Refused("merge-failed", f"#{pr['number']} ({t['id']}) did not merge: "
                          f"{(proc.stderr or proc.stdout).strip()[:200]}", preserved=f"merged so far: "
                          f"{', '.join(merged) or 'none'}; {base} was {before[:12]}",
                          next_step="resolve it on GitHub, then office land again (merged PRs are skipped)")
        with db.transaction(con):
            state.update_task(con, run["id"], t["id"], pr={**pr, "merged": True, "base": base, "method": method})
            state.emit(con, run, "pr.merged", f"{t['id']} #{pr['number']} merged into {base} ({method})",
                       task_id=t["id"])
        res.add(f"{t['id']} #{pr['number']} merged ({method})")
        merged.append(t["id"])
    return before


def _restack(con, run: dict, task: dict, up: dict, base: str) -> None:
    """Squash/rebase merges rewrite the parent, so rebase the child's own
    commits onto the new base and force-push with lease."""
    d = state.get_dispatch(con, task["current_dispatch_id"])
    wt = Path(d["worktree"])
    old_parent = _accepted_commit(con, up)
    _git(wt, "fetch", "-q", "origin", base)
    proc = _git(wt, "rebase", "--onto", f"origin/{base}", old_parent)
    if proc.returncode != 0:
        _git(wt, "rebase", "--abort")
        raise Refused("restack-conflict", f"{task['id']} does not rebase cleanly onto {base} after {up['id']} merged",
                      preserved=f"{task['id']} branch unchanged", next_step="resolve the rebase by hand, push, then office land")
    ok, err = prs.push(run, d, force=True, expected=_accepted_commit(con, task))
    if not ok:
        raise Refused("restack-push-failed", f"force-push of {d['branch']} failed: {err[:160]}")


def _merge_tree(repo: Path, ours: str, theirs: str) -> tuple[str, list[str] | None]:
    """`git merge-tree --write-tree`: the merged tree and None, or the conflicted files."""
    proc = _git(repo, "merge-tree", "--write-tree", "--name-only", ours, theirs)
    lines = proc.stdout.splitlines()
    if proc.returncode == 0 and lines:
        return lines[0], None
    if proc.returncode == 1 and lines:
        return lines[0], lines[1:lines.index("")] if "" in lines else lines[1:]
    raise Refused("recovery-failed", f"git merge-tree {ours[:12]} {theirs[:12]} failed: "
                  f"{(proc.stderr or proc.stdout).strip()[:160]}", next_step="office land again once git is fixed")


def _recover_conflicting(con, run: dict, task: dict, pr: dict, base: str, res: Result) -> dict:
    """GitHub says the PR cannot be merged, though the reviewed work may still
    compose cleanly (a squash or rebase of the parent leaves the child's
    ancestry conflicting). Prove it before touching the branch: the remote head
    is the recorded one, the PR merges with `base` without conflicts, and
    folding every remaining open task onto `base` reproduces the reviewed
    integration tree. Then push one merge commit of `base` into the branch,
    leased to the head just read, and return the updated PR record."""
    repo, tid, number = Path(run["repo_root"]), task["id"], pr["number"]
    branch = pr.get("branch") or state.get_dispatch(con, task["current_dispatch_id"])["branch"]
    by_hand = COMPOSE_BY_HAND.format(base=base)
    untouched = f"{tid} #{number}, its branch and {base}; nothing was pushed"
    ns = f"refs/office/land/{short(run['id'])}"
    if _git(repo, "fetch", "-q", "origin", f"+refs/heads/{base}:{ns}/base",
            f"+refs/heads/{branch}:{ns}/{tid}").returncode != 0:
        raise Refused("fetch-failed", f"could not fetch origin/{base} and {branch}", preserved=untouched,
                      next_step="office land again")
    default, head = paths.git(repo, "rev-parse", f"{ns}/base"), paths.git(repo, "rev-parse", f"{ns}/{tid}")
    accepted = _accepted_commit(con, task)
    if head not in (accepted, (pr.get("recovered") or {}).get("commit")):
        raise Refused("branch-moved", f"{branch} on origin is {head[:12]}, not the reviewed {accepted[:12]}",
                      preserved=untouched, next_step=f"find out who pushed to {branch}; then office land again, or " + by_hand)
    open_ids = [u["id"] for u in integration._topo(integration.accepted_set(con, run) or [])
                if u["id"] != tid and prs.has_pr(u)
                and not (state.get_task(con, run["id"], u["id"]).get("pr") or {}).get("merged")]
    heads = [(tid, head)] + [(uid, _accepted_commit(con, state.get_task(con, run["id"], uid))) for uid in open_ids]
    genv = dict(os.environ, **paths.commit_identity_env(repo))

    def commit_tree(tree: str, parents: tuple[str, str], message: str) -> str:
        proc = _git(repo, "commit-tree", tree, "-p", parents[0], "-p", parents[1], "-m", message, env=genv)
        if proc.returncode != 0:
            raise Refused("recovery-failed", f"git commit-tree failed: {proc.stderr.strip()[:160]}", preserved=untouched)
        return proc.stdout.strip()

    cur, trees = default, []
    for uid, commit in heads:
        tree, files = _merge_tree(repo, cur, commit)
        if files is not None:
            raise Refused("merge-conflict", f"{uid} conflicts with {base} {default[:12]} on {', '.join(files) or 'files'}",
                          preserved=untouched, next_step=by_hand)
        cur = commit_tree(tree, (cur, commit), f"office land: fold {uid} onto {base}")  # scratch: never pushed
        trees.append(tree)
    reviewed = _tree(repo, integration.final_commit(con, run))
    if trees[-1] != reviewed:
        raise Refused("recovery-tree-mismatch", f"merging the open PRs onto {base} {default[:12]} gives tree {trees[-1][:12]}, "
                      f"not the reviewed integration tree {reviewed[:12]}", preserved=untouched, next_step=by_hand)
    new = commit_tree(trees[0], (head, default), f"Merge {base} into {branch}\n\nOffice land recovery: GitHub reported "
                      f"#{number} not mergeable.\n\n{paths.office_trailer(run['id'])}")
    ok, err = prs.push(run, {"worktree": str(repo), "branch": branch}, commit=new, force=True, expected=head)
    if not ok:
        raise Refused("recovery-push-failed", f"push of {branch} failed: {err[:160]}", preserved=untouched,
                      next_step="office land again, or " + by_hand)
    pr = {**pr, "recovered": {"from": head, "commit": new, "tree": trees[0], "at": now_iso()}}
    with db.transaction(con):
        state.update_task(con, run["id"], tid, pr=pr)
        state.emit(con, run, "pr.recovered", f"{tid} #{number} recovered: {base} {default[:12]} merged into {branch} "
                   f"({head[:12]} -> {new[:12]})", task_id=tid, payload={**pr["recovered"], "base": default})
    res.add(f"{tid} #{number} was not mergeable; merged {base} {default[:12]} into {branch} ({new[:12]}), tree matches the reviewed integration")
    return pr


def _wait_checks(run: dict, number: int) -> None:
    try:
        proc = _gh(run, "pr", "checks", str(number), "--required", "--watch", "--fail-fast", timeout=CHECKS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused("checks-timeout", f"required checks on #{number} did not finish in {CHECKS_TIMEOUT}s",
                      next_step="office land again once they finish")
    out = (proc.stdout + proc.stderr).lower()
    if proc.returncode != 0 and "no required checks" not in out and "no checks reported" not in out:
        raise Refused("checks-failed", f"required checks failed on #{number}: {(proc.stdout or proc.stderr).strip()[:200]}",
                      next_step="fix it on the task branch (office rerun), then office land")


def _verify_main(con, run: dict, integrated: str, res: Result) -> str:
    s = prs.settings(con, run)
    repo = Path(run["repo_root"])
    _git(repo, "fetch", "-q", "origin", s["base_branch"])
    main = paths.git(repo, "rev-parse", f"origin/{s['base_branch']}")
    if paths.git(repo, "rev-parse", f"{main}^{{tree}}") == paths.git(repo, "rev-parse", f"{integrated}^{{tree}}"):
        res.add(f"{s['base_branch']} {main[:12]} matches the reviewed integration tree")
        return main
    checks = list((run.get("landing") or {}).get("run_checks") or [])
    res.add(f"{s['base_branch']} moved beyond the reviewed integration; "
            + ("re-running the run checks on it" if checks else "no run checks to re-run"))
    if checks:
        checkout = gates.detached_checkout(run, main, "land-verify", purpose="check")
        try:
            for cmd in checks:
                proc = subprocess.run(cmd, shell=True, cwd=checkout, capture_output=True, text=True, timeout=1800)
                if proc.returncode != 0:
                    raise Refused("main-checks-failed", f"`{cmd}` fails on merged {s['base_branch']} {main[:12]}",
                                  preserved="the merges; nothing was deployed",
                                  next_step="fix forward on a new task, or revert the merges")
        finally:
            gates.remove_checkout(run, checkout)
    return main


# ------------------------------------------------------------------ deploy

def _deploy(con, run: dict, target: str, deploy: dict, cwd: Path, res: Result, rollback: str | None = None, *,
            commit: str, tree: str) -> None:
    """Every event names the commit, tree and cwd checked out. `land.deploy.start`
    goes first, so a land killed mid-deploy leaves a start with no result,
    which `_require_deploy_proof` treats as unknown rather than not deployed."""
    link = Path(run["repo_root"]) / ".vercel"
    if link.is_dir() and not (cwd / ".vercel").exists():
        shutil.copytree(link, cwd / ".vercel")  # the local project link is untracked
    copied = copy_env_files(Path(run["repo_root"]), cwd, env_files(state.pinned_config(run)))
    if copied:
        res.add(f"{target} deploy checkout {cwd}: copied deploy.env_files {', '.join(copied)} (never committed)")
    log_dir = paths.run_dir(run["id"]) / "deploys"
    log_dir.mkdir(parents=True, exist_ok=True)
    ident = {"target": target, "commit": commit, "tree": tree, "cwd": str(cwd)}
    for step, cmd in (("deploy", deploy.get(target)), ("verify", deploy.get("verify"))):
        if not cmd:
            continue
        if step == "deploy":
            with db.transaction(con):
                state.emit(con, run, "land.deploy.start", f"{target} deploy of {commit[:12]} started in {cwd}",
                           payload=ident, audience="runtime")
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=3600)
        header = f"# office land {target} {step}\n# cwd: {cwd}\n# commit: {commit}\n# command: {cmd}\n\n"
        (log_dir / f"{target}-{step}.log").write_text(header + proc.stdout + proc.stderr, encoding="utf-8")
        with db.transaction(con):
            state.emit(con, run, f"land.{step}", f"{target} {step} `{cmd}` exit {proc.returncode}",
                       payload={**ident, "exit": proc.returncode})
        if proc.returncode != 0:
            raise Refused(f"{step}-failed", f"{target} {step} failed (exit {proc.returncode}) in {cwd}: `{cmd}`",
                          preserved=(f"merges are done; rollback target {rollback[:12]}" if rollback else "the PRs"),
                          next_step=f"see {log_dir / f'{target}-{step}.log'}; fix or roll back, then office land again")
        res.add(f"{target} {step} ok: `{cmd}` (cwd {cwd})")


# ------------------------------------------------- deploy environment files

def env_files(config: dict) -> list[str]:
    """The `deploy.env_files` entries of a config: repo-relative paths of ignored files a deploy needs."""
    listed = (config.get("deploy") or {}).get("env_files") or []
    return [str(e) for e in (listed if isinstance(listed, list) else [listed])]


def copy_env_files(repo: Path, checkout: Path, entries: list[str]) -> list[str]:
    """Copy each listed file from the repo into the deploy checkout, mode preserved. The
    checkout is a throwaway worktree: nothing is staged or committed, and a tracked file
    the checkout already holds is left alone. Returns the entries copied."""
    root, dest_root = repo.resolve(), checkout.resolve()

    def refuse(entry: str, why: str) -> Refused:
        return Refused("deploy-env-file-invalid", f"deploy.env_files entry {entry!r} {why}", preserved="nothing was deployed",
                       next_step="fix deploy.env_files in the config, then office land again")

    copied = []
    for entry in entries:
        rel = Path(entry)
        if not entry.strip() or rel.is_absolute() or ".." in rel.parts:
            raise refuse(entry, "must be a path inside the repo")
        src, dest = root / rel, dest_root / rel
        if not src.is_file():
            continue
        if not src.resolve().is_relative_to(root):
            raise refuse(entry, "is a link to a file outside the repo")
        if dest.exists() or dest.is_symlink():
            continue  # tracked: the checkout already holds the committed file
        anchor = dest.parent
        while not anchor.exists():
            anchor = anchor.parent
        if not anchor.resolve().is_relative_to(dest_root):
            raise refuse(entry, "resolves outside the deploy checkout")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private until the mode is applied
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
        shutil.copymode(src, dest)
        copied.append(entry)
    return copied


def _command_paths(cmd: str) -> list[str]:
    """Path-like words of a shell command: arguments, `--flag=value` values and `VAR=value`
    values, with operators dropped. Variable expansions, globs and URLs are not paths."""
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        words = cmd.split()
    found = []
    for word in words:
        if re.search(r"\s", word):  # a quoted script, as in `sh -c '. ./.env && deploy'`
            found += _command_paths(word)
            continue
        value = word.partition("=")[2] if word.startswith("-") else word.partition("=")[2] or word
        if not value or value.startswith("-") or re.search(r"[$*?`;&|<>()~]|://", value):
            continue
        found.append(value)
    return found


def deploy_path_warnings(repo: Path, commands: dict[str, str], listed: list[str] | None = None) -> list[str]:
    """Warnings for deploy commands that name an existing repo path a fresh checkout lacks
    (gitignored or untracked, e.g. `.env`, or `myaccount/.env` passed to a sourced script).
    Paths listed in `deploy.env_files` are copied in, so they are not reported. `commands`
    maps a label (deploy_prod, deploy_verify, ...) to its shell command."""
    root = repo.resolve()
    covered = {Path(e).as_posix() for e in listed or []}
    warnings: list[str] = []
    for label, cmd in commands.items():
        seen: set[str] = set()
        for word in _command_paths(cmd or ""):
            try:
                rel = (root / word).resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            if rel in seen or rel in covered or rel == "." or rel.split("/")[0] in (".git", ".vercel") \
                    or not (root / rel).exists():
                continue
            seen.add(rel)
            if _git(root, "ls-files", "--", rel).stdout.strip():
                continue
            kind = "gitignored" if _git(root, "check-ignore", "-q", "--", rel).returncode == 0 else "untracked"
            warnings.append(f"{label} references {rel}, which is {kind} and absent from the fresh checkout the deploy "
                            f"runs in; list it under deploy.env_files in the config to copy it in")
    return warnings


def _close_issue(con, run: dict, main: str, res: Result) -> None:
    landing = state.get_run(con, run["id"]).get("landing") or {}
    issue = landing.get("issue")
    if not issue or landing.get("issue_closed"):
        return
    number = str(issue).rstrip("/").rsplit("/", 1)[-1]
    proc = _gh(run, "issue", "close", number, "--comment", f"Landed by Office run {short(run['id'])} at {main[:12]}.")
    res.add(f"issue #{number} closed" if proc.returncode == 0 else f"issue #{number} not closed: {proc.stderr.strip()[:120]}")
    if proc.returncode == 0:
        _record(con, run, issue_closed=main)


def _record(con, run: dict, **fields) -> None:
    with db.transaction(con):
        landing = dict(state.get_run(con, run["id"]).get("landing") or {})
        landing.update(fields)
        state.update_run(con, run["id"], landing=landing)


# ------------------------------------------------------------------ detection

def detect_deploy(repo: Path) -> Result:
    """Propose deploy commands for the user to confirm at intake."""
    lines, proposals = [], {}
    if (repo / "vercel.json").exists() or (repo / ".vercel").exists():
        proposals = {"deploy_preview": "vercel deploy", "deploy_prod": "vercel deploy --prod"}
        lines.append("vercel project: preview and prod deploys through the vercel CLI")
    pkg = repo / "package.json"
    scripts, deploy_scripts = {}, []
    if pkg.exists():
        scripts = (json.loads(pkg.read_text() or "{}").get("scripts") or {})
        deploy_scripts = [n for n in scripts if n.startswith("deploy")]
        for name in scripts:
            if name in ("deploy:preview", "deploy-preview"):
                proposals["deploy_preview"] = f"pnpm run {name}"
            elif name in ("deploy", "deploy:prod", "deploy-prod"):
                proposals.setdefault("deploy_prod", f"pnpm run {name}")
        if deploy_scripts:
            lines.append("package.json deploy scripts: " + ", ".join(deploy_scripts))
    wf = repo / ".github" / "workflows"
    ci = sorted(p.name for p in wf.glob("*.y*ml") if re.search(r"deploy|release|publish", p.read_text(errors="ignore"), re.I)) \
        if wf.is_dir() else []
    if ci:
        lines.append(f"CI workflows that deploy: {', '.join(ci)} (merging may deploy by itself)")
    skills = sorted(p.parent.name for p in (repo / ".claude" / "skills").glob("*deploy*/SKILL.md")) \
        if (repo / ".claude" / "skills").is_dir() else []
    if skills:
        lines.append(f"repo deploy skills: {', '.join(skills)}")
    if not lines:
        lines.append("no deploy route detected; ask the user for the commands, or offer ask/merge only")
    if proposals:
        lines += ["", "proposed PLAN.md requirements (confirm with the user first):",
                  *[f"{k}: {v}" for k, v in proposals.items()], "deploy_verify: <command that exits 0 when healthy>"]
    listed = env_files(config_for(repo))
    warnings = deploy_path_warnings(repo, {**proposals, **{f"package.json script {n}": scripts[n] for n in deploy_scripts}},
                                    listed)
    lines += [f"warning: {w}" for w in warnings]
    return Result(lines=lines, data={"proposals": proposals, "ci": ci, "skills": skills, "warnings": warnings})


def config_for(repo: Path) -> dict:
    """The effective config for `repo` (shipped defaults, user and repo files), {} when it is invalid."""
    from office import config as cfg
    try:
        return cfg.resolve(repo)[0]
    except ValueError:
        return {}
