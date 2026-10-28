"""Integration: verify the actual composed result, not each green worktree.

When every task is accepted the runtime composes the accepted revisions onto
the run base in dependency order and runs the run-level checks on the composed
tree.

v3.1 contract: it also runs an integration review at a real boundary (Q7): a
task built on another's unmerged output, or two tasks that changed the same
file or share a declared interface.

Convergence contract (#337): those boundaries are lanes and shared scopes,
reviewed by office.convergence before integration starts; integration waits
until every scope converged (APPROVED, or waived by landing authority) and
adds no review of its own.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from office import briefs, contract, db, gates, jobs, paths, planfile, review_parse, state, verification_coverage, worktree_setup
from office.util import dumps, now_iso, sha256_obj


DETAIL_LIMIT = 400


def blocked_detail(outcome):
    """The integration blocked line: the check summary, shortened in the middle so the trailing next step survives."""
    summary = outcome.get("summary") or ""
    if len(summary) > DETAIL_LIMIT:
        # The next step is the last "; " clause; elide the middle (usually the quoted command) instead.
        cut = summary.rfind("; ")
        tail = summary[cut:] if 0 < cut and len(summary) - cut <= DETAIL_LIMIT // 2 else summary[-(DETAIL_LIMIT // 2):]
        summary = summary[:DETAIL_LIMIT - len(tail) - 3] + "..." + tail
    return f"run checks {outcome['verdict']}: {summary}"


def _topo(tasks: list[dict]) -> list[dict]:
    by_id = {t["id"]: t for t in tasks}
    out, seen = [], set()

    def visit(t):
        if t["id"] in seen:
            return
        seen.add(t["id"])
        for dep in t["depends"]:
            if dep in by_id:
                visit(by_id[dep])
        out.append(t)

    for t in tasks:
        visit(t)
    return out


def accepted_set(con, run: dict) -> list[dict] | None:
    tasks = [t for t in state.tasks(con, run["id"]) if t["status"] != "cancelled"]
    if not tasks or any(t["status"] != "accepted" for t in tasks):
        return None
    return _topo(tasks)


def status(con, run: dict) -> dict:
    run = state.get_run(con, run["id"])
    integ = (run.get("landing") or {}).get("integration")
    tasks = accepted_set(con, run)
    if tasks is None:
        return {"required": True, "status": "waiting", "detail": "tasks not all accepted"}
    if contract.is_convergence(run):
        from office import convergence
        open_scopes = [s for s in convergence.summary(con, run) if s["status"] not in convergence.CONVERGED]
        if open_scopes:
            return {"required": True, "status": "converging",
                    "detail": "; ".join(f"{s['id']} {s['status']}" for s in open_scopes[:4])}
    key = _set_key(con, tasks)
    if not integ or integ.get("key") != key:
        return {"required": True, "status": "pending", "detail": "composition queued"}
    return {"required": True, "status": integ.get("status", "pending"), "detail": integ.get("detail", ""),
            "branch": integ.get("branch"), "commit": integ.get("commit")}


def _set_key(con, tasks: list[dict]) -> str:
    return sha256_obj([(t["id"], t["accepted_revision_id"]) for t in tasks])


def final_commit(con, run: dict) -> str | None:
    s = status(con, run)
    return s.get("commit") if s.get("status") == "accepted" else None


def maybe_queue(con, run: dict) -> None:
    """Queue composition once per accepted set. Caller holds tx."""
    tasks = accepted_set(con, run)
    if tasks is None:
        return
    key = _set_key(con, tasks)
    state.enqueue(con, run, "integrate", {"key": key}, dedup_key=f"integrate:{run['id']}:{key}", max_attempts=2)


LOCK_WAIT_SECONDS = 7200.0  # a compose runs the run checks, which can take their whole timeout


@contextlib.contextmanager
def worktree_lock(run: dict, *, wait: float = 0.0):
    """Exclusive ownership of the `_integration` worktree. Held by a compose for
    its whole run and by `office land --rebase` while it queues the next one, so
    no second compose can remove the worktree under a running check. A flock
    dies with its process, so a crashed job never leaves it held."""
    import fcntl
    path = paths.worktrees_dir() / run["id"][:8] / "_integration.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                fh.close()
                raise state.Refused("integration-running", "another integration is composing or running checks in "
                                    "_integration", preserved="the running checks",
                                    next_step="office wait, then retry")
            time.sleep(0.2)
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def retrigger(con, run: dict) -> bool:
    """Force a fresh integration pass on the current accepted set, bypassing the
    per-set dedup key (used when run-level checks changed but the accepted
    revisions did not). Caller holds the tx."""
    tasks = accepted_set(con, run)
    if tasks is None:
        return False
    key = _set_key(con, tasks)
    state.enqueue(con, run, "integrate", {"key": key},
                  dedup_key=f"integrate:{run['id']}:{key}:recheck:{uuid.uuid4().hex[:8]}", max_attempts=2)
    return True


def branch_holder(repo: Path, branch: str, *, exclude: Path | None = None) -> str | None:
    """The worktree (other than `exclude`) that has `branch` checked out."""
    listing = paths.git(repo, "worktree", "list", "--porcelain", check=False)
    path = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path:
            try:
                same = exclude is not None and Path(path).resolve() == Path(exclude).resolve()
            except OSError:
                same = False
            if not same:
                return path
    return None


def retry_failed(con, run: dict) -> bool:
    """`office resume`: re-run a blocked or unavailable integration (or one
    whose job failed outright) on the current accepted set once its cause has
    been fixed outside Office. A conflict needs a plan change, not a retry.
    Caller holds the tx."""
    if accepted_set(con, run) is None:
        return False
    if con.execute("SELECT 1 FROM outbox WHERE run_id=? AND kind='integrate' AND status IN ('queued','claimed')",
                   (run["id"],)).fetchone():
        return False
    s = status(con, run)
    failed_job = con.execute("SELECT status FROM outbox WHERE run_id=? AND kind='integrate' ORDER BY rowid DESC LIMIT 1",
                             (run["id"],)).fetchone()
    if s["status"] not in ("blocked", "unavailable") and not (failed_job and failed_job["status"] == "failed"
                                                              and s["status"] == "pending"):
        return False
    state.emit(con, run, "integration.retry", f"integration {s['status']}; retry queued")
    return retrigger(con, run)


def compose_base(run: dict) -> str:
    """Where integration composes: the run base, or the newer default-branch
    commit `office land --rebase` moved the run onto."""
    return ((run.get("landing") or {}).get("rebase") or {}).get("onto") or run["base_sha"]


def moved_base(con, run: dict) -> str | None:
    """origin/<default branch> when it moved ahead of the compose base by a
    fast-forward, else None. A failed fetch or a diverged branch is None:
    `office land --rebase` names those to the orchestrator."""
    from office import prs
    branch = prs.settings(con, run).get("base_branch") or "main"
    repo = Path(run["repo_root"])
    try:  # runs inside the integrate job, under the worktree lock: never hang it on the network
        fetched = subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin", branch], capture_output=True,
                                 timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if fetched.returncode != 0:
        return None
    new = paths.git(repo, "rev-parse", f"origin/{branch}", check=False)
    old = compose_base(run)
    if not new or new == old:
        return None
    ahead = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", old, new], capture_output=True)
    return new if ahead.returncode == 0 else None


def _failed_check_logs(outcome: dict) -> str:
    """Surface the precise failed check and its recorded evidence path (#577)."""
    failures = [(i, row) for i, row in enumerate(outcome.get("results") or [], start=1)
                if row.get("exit") != 0]
    return "; ".join(f"check {i} exit {row['exit']} log {row.get('log') or 'unavailable'}"
                     for i, row in failures)


def _set_integration(con, run: dict, **fields) -> None:
    run = state.get_run(con, run["id"])
    landing = dict(run.get("landing") or {})
    integ = dict(landing.get("integration") or {})
    integ.update(fields)
    landing["integration"] = integ
    state.update_run(con, run["id"], landing=landing)


def _fenced_dir(run: dict) -> Path:
    return paths.worktrees_dir() / run["id"][:8] / "_integration.fenced"


def live_integrate_pids(con, run: dict, *, unfenced_only: bool = False) -> list[int]:
    """Processes running a claimed integrate job of this run. An integrate
    process from an older patch holds no flock, so the flock alone cannot see
    it; the job table can. `unfenced_only` drops processes that registered as
    flock holders (this code), which wait on the flock instead."""
    fenced = {p.name for p in _fenced_dir(run).glob("*")} if unfenced_only else set()
    rows = con.execute("SELECT * FROM outbox WHERE run_id=? AND kind='integrate' AND status='claimed'",
                       (run["id"],)).fetchall()
    return [r["claimed_pid"] for r in rows if r["claimed_pid"] and r["claimed_pid"] != os.getpid()
            and str(r["claimed_pid"]) not in fenced and jobs.claim_live(r)]


def refuse_if_integrating(con, run: dict) -> None:
    """Refuse while any integrate job is live, flock or not."""
    pids = live_integrate_pids(con, run)
    if pids:
        raise state.Refused("integration-running", f"an integrate job (pid {', '.join(map(str, pids))}) is composing or "
                            "running checks in _integration", preserved="the running checks",
                            next_step="office wait, then retry")


def job_integrate(con, run: dict, job: dict) -> dict:
    marker = _fenced_dir(run) / str(os.getpid())  # tells peers this process honors the flock
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    try:
        with worktree_lock(run, wait=LOCK_WAIT_SECONDS):
            # An integrate process from an older patch takes no flock: wait it out too.
            deadline = time.time() + LOCK_WAIT_SECONDS
            while live_integrate_pids(con, run, unfenced_only=True):
                if time.time() >= deadline:
                    raise state.Refused("integration-running", "an integrate job from an older Office patch is still "
                                        "running in _integration", next_step="office wait, then office resume")
                time.sleep(1.0)
            return _integrate(con, run, job)
    finally:
        marker.unlink(missing_ok=True)


def _integrate(con, run: dict, job: dict) -> dict:
    run = state.get_run(con, run["id"])  # a rebase may have moved the compose base since this job was queued
    base = compose_base(run)
    tasks = accepted_set(con, run)
    key = job["payload"]["key"]
    if tasks is None or _set_key(con, tasks) != key:
        return {"skipped": "accepted set changed"}
    repo = Path(run["repo_root"])
    branch = f"office/{run['id'][:8]}/integration"
    wt = paths.worktrees_dir() / run["id"][:8] / "_integration"
    revs = {t["id"]: dict(con.execute("SELECT * FROM revisions WHERE id=?", (t["accepted_revision_id"],)).fetchone())
            for t in tasks}
    stale = stale_composition(run, tasks, revs)
    if stale:
        # A merge would only report this as a conflict (or compose a tree no one reviewed): name the task to restack.
        with db.transaction(con):
            _set_integration(con, run, key=key, status="stale", detail=stale)
            state.emit(con, run, "integration.stale", f"INTEGRATION stale: {stale}")
        return {"status": "stale"}
    if (wt / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    holder = branch_holder(repo, branch, exclude=wt)
    if holder:
        # `worktree add -B` refuses a branch checked out elsewhere; say where.
        detail = (f"{branch} is checked out in another worktree: {holder}; remove it "
                  f"(git worktree remove {holder}), then office resume retries integration")
        with db.transaction(con):
            _set_integration(con, run, key=key, status="blocked", detail=detail, branch=branch)
            state.emit(con, run, "integration.failed", f"INTEGRATION blocked: {detail}")
        return {"status": "blocked"}
    try:
        paths.git(repo, "worktree", "add", "-B", branch, str(wt), compose_base(run))
    except paths.GitError as exc:
        detail = f"could not create the integration worktree: {exc.stderr[:200]}; fix it, then office resume"
        with db.transaction(con):
            _set_integration(con, run, key=key, status="blocked", detail=detail, branch=branch)
            state.emit(con, run, "integration.failed", f"INTEGRATION blocked: {detail}")
        return {"status": "blocked"}
    import os
    genv = dict(os.environ, **paths.commit_identity_env(repo))
    previous = []
    for t in tasks:
        commit = revs[t["id"]]["commit_sha"]
        if subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", commit, "HEAD"], capture_output=True).returncode == 0:
            previous.append(t)
            continue
        proc = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", "--no-edit", "-m", f"office: land {t['id']}\n\n{paths.office_trailer(run['id'])}", commit],
                              capture_output=True, text=True, env=genv)
        if proc.returncode != 0 and not finish_shared_append_merge(wt, t, previous, env=genv):
            conflicted = paths.git(wt, "diff", "--name-only", "--diff-filter=U", check=False)
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True)
            detail = f"{t['id']} conflicts with earlier tasks on {conflicted.replace(chr(10), ', ') or 'files'}"
            with db.transaction(con):
                _set_integration(con, run, key=key, status="conflict", detail=detail, branch=branch)
                state.emit(con, run, "integration.conflict", f"INTEGRATION CONFLICT: {detail}")
            return {"status": "conflict"}
        try:
            preserve_shared_append_multiplicity(
                wt, {**t, "base_commit": revs[t["id"]].get("base_commit") or run["base_sha"]},
                previous, env=genv)
        except SharedAppendUnsafe as exc:
            detail = f"{t['id']}: shared append integrity unverified: {exc}; resolve shared edits or serialize tasks, then office resume"
            with db.transaction(con):
                _set_integration(con, run, key=key, status="conflict", detail=detail, branch=branch)
                state.emit(con, run, "integration.conflict", f"INTEGRATION CONFLICT: {detail}")
            return {"status": "conflict", "detail": detail}
        previous.append(t)
    commit = paths.git(wt, "rev-parse", "HEAD")
    tree = paths.git(wt, "rev-parse", "HEAD^{tree}")
    needs_review, why = review_boundary(con, run, tasks, revs)
    if contract.is_convergence(run):
        # Lanes and shared scopes (a rebase included) were reviewed before this.
        needs_review, why = False, "every lane and shared scope converged"
    elif compose_base(run) != run["base_sha"]:
        # The accepted revisions were reviewed against the old base; the
        # composition onto the newer one gets its own independent review.
        needs_review, why = True, f"rebased onto {compose_base(run)[:12]}" + (f"; {why}" if why else "")
    run_checks = list((run.get("landing") or {}).get("run_checks") or [])
    integ_rev = {"id": "I" + commit[:7], "commit_sha": commit, "tree_sha": tree, "base_commit": compose_base(run),
                 "dispatch_id": None}
    with db.transaction(con):
        _set_integration(con, run, key=key, status="verifying", detail=why, branch=branch, commit=commit,
                         review=needs_review)
    results = {}
    check_outcome = None
    checks_gate_id = None
    if run_checks:
        # The merged lockfile is in place; install before the checks that need it.
        worktree_setup.prepare(run, wt, "integration", paths.run_dir(run["id"]) / "setup" / "integration.log", created=True)
        gid = _gate(con, run, integ_rev, "checks", commit)
        checks_gate_id = gid
        extra_checks = verification_coverage.supplemental_checks(repo, commit, run_checks)
        outcome = gates.run_commands(con, run, [*run_checks, *extra_checks], wt, integ_rev,
                                     {"id": gid, "task_id": None}, check_tree=False)
        if outcome["verdict"] == "CHANGES_REQUIRED":
            try:
                base_failures = gates.preexisting_failures(
                    con, run, integ_rev, {"id": gid, "task_id": None}, outcome, allow_unchanged=True)
            except Exception:
                base_failures = []  # no proof -> block, never guess that it is pre-existing
            proven = {entry["code"] for entry in base_failures}
            failures = {f"C{i}" for i, result in enumerate(outcome.get("results") or [], 1)
                        if result["exit"] != 0}
            if base_failures:
                summary = (f"{len(base_failures)} integration check(s) fail identically on pinned base "
                           f"{base[:12]}: pre-existing, NONBLOCKING (not green)")
                with db.transaction(con):
                    state.emit(con, run, "integration.preexisting", summary,
                               payload={"commit": commit, "base_commit": base, "checks": base_failures})
                if failures and failures <= proven:
                    outcome = {**outcome, "verdict": "PASS", "summary": summary}
                else:
                    outcome = {**outcome, "summary": f"{outcome['summary']}; {summary}; introduced failures BLOCK"}
        check_outcome = outcome
        with db.transaction(con):
            con.execute("UPDATE gates SET status='done', verdict=?, summary=?, finished_at=? WHERE id=?",
                        (outcome["verdict"], outcome.get("summary"), now_iso(), gid))
        results["checks"] = outcome["verdict"]
        if outcome["verdict"] != "PASS":
            detail = blocked_detail(outcome)
            failed_logs = _failed_check_logs(outcome)
            if failed_logs:
                detail += f" | failing evidence: {failed_logs}"
            hint = missing_deps_hint(outcome)
            if hint:
                detail += f" | {hint}"
            # A failure on a base the default branch has since moved past may be
            # one the branch already fixed: rebase once onto the new head and
            # re-check there before calling the work blocked. The accepted
            # revisions are kept either way.
            onto = moved_base(con, run) if outcome["verdict"] == "CHANGES_REQUIRED" else None
            with db.transaction(con):
                if onto and state.enqueue(con, run, "auto_rebase", {"onto": onto},
                                          dedup_key=f"auto_rebase:{run['id']}:{onto}", max_attempts=1):
                    _set_integration(con, run, status="pending",
                                     detail=f"{detail} | the default branch moved to {onto[:12]}: rebasing onto it "
                                            "and re-checking")
                    state.emit(con, run, "integration.auto_rebase", f"INTEGRATION checks {outcome['verdict']} on base "
                               f"{base[:12]}; the default branch moved to {onto[:12]}, so the run rebases onto it and "
                               "re-checks" + (f"; {failed_logs}" if failed_logs else ""))
                    return results
                _set_integration(con, run, status="blocked", detail=detail)
                state.emit(con, run, "integration.failed", f"INTEGRATION checks {outcome['verdict']} on the composed result"
                           + (f"; {hint}" if hint else ""))
            return results
    if needs_review:
        gid = _gate(con, run, integ_rev, "integration_review", commit)
        gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (gid,)).fetchone())
        diff = gates.cap_diff(paths.git(repo, "diff", compose_base(run), commit))
        checkout = gates.detached_checkout(run, commit, f"integration-{gid}", purpose="review")
        try:
            brief = briefs.code_review_brief(run, None, integ_rev, diff, ", ".join(f"{k} {v}" for k, v in results.items()) or "none",
                                             [], str(checkout), integration=True)
            outcome = gates.run_reviewer(con, run, gate, "integration_reviewer", brief, cwd=checkout, include_dirs=[checkout])
        finally:
            gates.remove_checkout(run, checkout)
        with db.transaction(con):
            con.execute("UPDATE gates SET status='done', verdict=?, summary=?, finished_at=? WHERE id=?",
                        (outcome["verdict"], outcome.get("summary"), now_iso(), gid))
        results["integration_review"] = outcome["verdict"]
        if outcome["verdict"] != "PASS":
            findings = (outcome.get("parsed").findings if outcome.get("parsed") else [])
            detail = "; ".join(f"{f['code']} {f['location']} {f['summary'][:80]}" for f in findings[:3]) or outcome.get("summary", "")
            with db.transaction(con):
                _set_integration(con, run, status="blocked" if outcome["verdict"] != "UNAVAILABLE" else "unavailable",
                                 detail=f"integration review {outcome['verdict']}: {detail[:200]}")
                state.emit(con, run, "integration.failed", f"INTEGRATION review {outcome['verdict']}: {detail[:140]}")
            return results
    # An independent integration reviewer has its own outcome/gate. The proof
    # must come from the original passing check gate, not the last reviewer.
    executed = ([r["command"] for r in check_outcome.get("results") or [] if r.get("exit") == 0]
                if check_outcome is not None else [])
    coverage = verification_coverage.audit(repo, commit, run_checks, verified_commands=executed)
    incomplete = coverage and coverage[0] == verification_coverage.PARTIAL
    detail = (f"composed result verified by declared checks; FULL SUITE NOT VERIFIED: {coverage[1]}"
              if incomplete else
              "composed result verified with SCOPED PR TESTS (not full-suite)"
              if coverage and coverage[0] == verification_coverage.SCOPED
              else "composed result verified")
    with db.transaction(con):
        if compose_base(state.get_run(con, run["id"])) != base:
            return {"skipped": "rebased while composing"}
        direct = next((c for c in executed
                       if verification_coverage._complete_pytest_command(c)
                       or verification_coverage._scoped_validate(repo, commit, c)), None)
        proof = ({"commit": commit, "gate": checks_gate_id, "command": direct,
                  "scope": coverage[0] if coverage else None} if direct else None)
        _set_integration(con, run, status="accepted", detail=detail, full_suite=proof)
        if incomplete:
            state.emit(con, run, "integration.coverage_partial",
                       f"integration {commit[:12]}: FULL SUITE NOT VERIFIED: {coverage[1]}")
        state.emit(con, run, "integration.accepted", f"READY: integration verified on {commit[:10]} ({branch})"
                   if contract.is_convergence(run) else f"READY: integration PASS on {commit[:10]} ({branch})")
    return results


MISSING_DEPS_HINT = ("the composed worktree is a fresh checkout with no installed dependencies (anything not in git, "
                     "such as node_modules or a virtualenv, is absent and is recreated on every compose); declare the "
                     "repo's install once as `worktree.setup` in .auto-office/config.yaml (Office runs it in every new "
                     "worktree), or make the plan's run-level `checks:` install them first, e.g. "
                     "`pnpm install --frozen-lockfile && pnpm lint`, then office amend plan")


def missing_deps_hint(outcome: dict) -> str | None:
    """When a run-level check could not find its command, say why that happens
    on the composed tree and what the plan must do. Office runs only the command the
    repo declares as `worktree.setup`; it never chooses a package manager."""
    if outcome.get("verdict") == "UNAVAILABLE" and "command not found" in (outcome.get("summary") or ""):
        return MISSING_DEPS_HINT
    return None


def _gate(con, run, rev, kind, commit) -> str:
    gid = "G" + uuid.uuid4().hex[:8]
    with db.transaction(con):
        con.execute("INSERT INTO gates(id, run_id, subject, revision_id, plan_version, kind, input_key, status, created_at, started_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", (gid, run["id"], "integration", rev["id"], run["plan_version"], kind,
                                                   f"integration:{commit}", "running", now_iso(), now_iso()))
    return gid


def stale_composition(run: dict, tasks: list[dict], revs: dict) -> str | None:
    """Why the accepted set cannot compose: a task's accepted revision does not contain the accepted
    revision of a dependency (it was built on one that was re-accepted since), else None."""
    for t in tasks:
        for dep in t["depends"]:
            if dep in revs and not gates._is_ancestor(run, revs[dep]["commit_sha"], revs[t["id"]]["commit_sha"]):
                return (f"{t['id']} accepted on {revs[t['id']]['id']} does not contain {dep} accepted on "
                        f"{revs[dep]['id']}; restack {t['id']} onto {dep}")
    return None


def review_boundary(con, run: dict, tasks: list[dict], revs: dict) -> tuple[bool, str]:
    if len(tasks) < 2:
        return False, "single task: composed result equals the accepted revision"
    changed = {t["id"]: set(json.loads(revs[t["id"]].get("changed_json") or "[]")) |
               set(paths.git(Path(run["repo_root"]), "diff", "--name-only",
                             revs[t["id"]].get("base_commit") or run["base_sha"], revs[t["id"]]["commit_sha"]).split())
               for t in tasks}
    ids = [t["id"] for t in tasks]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            shared = changed[a] & changed[b]
            if shared:
                return True, f"{a} and {b} both changed {sorted(shared)[0]}"
    provides = {}
    for t in tasks:
        for item in t.get("interfaces") or []:
            words = item.lower().split()
            if words and words[0] in ("provides", "provide"):
                provides[" ".join(words[1:])] = t["id"]
    for t in tasks:
        for item in t.get("interfaces") or []:
            words = item.lower().split()
            if words and words[0] in ("consumes", "consume") and " ".join(words[1:]).split(":")[-1] in {k.split(":")[-1] for k in provides}:
                return True, f"{t['id']} consumes an interface another task provides"
    for t in tasks:
        if t["depends"]:
            return True, f"{t['id']} built on {', '.join(t['depends'])}"
    return False, "independent scopes with no shared files or interfaces"


# ------------------------------------------------------------------ combining revisions

class SharedAppendUnsafe(Exception):
    """An append-only shared file cannot be proved safe to compose."""


def preserve_shared_append_multiplicity(wt: Path, task: dict, previous: list[dict], *, env: dict) -> bool:
    """Verify *every* shared path after a clean merge, not just conflict cases.

    This is a proof-or-refuse operation: Git's clean result cannot authorize a
    non-append rewrite, missing file, mode change, ambiguous merge ancestry,
    binary data, or duplicate-line collapse.  Only the scratch composition
    commit may be amended; accepted task commits are untouched.
    """
    shared = {x[1:] for x in task.get("scope") or []
              if planfile.is_shared(x) and not any(c in x for c in "*?[]")}
    prior = {x[1:] for t in previous for x in t.get("scope") or [] if planfile.is_shared(x)}
    eligible = shared & prior
    if not eligible:
        return False
    parents = paths.git(wt, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) != 3:
        raise SharedAppendUnsafe(f"{', '.join(sorted(eligible))}: merge has no two parents; "
                                 "cannot verify shared append ancestry")
    task_base = task.get("base_commit")
    if task_base:
        # Declaring a shared file does not mean this revision wrote it (#700).
        # Its recorded dependency base is the scope-diff authority, including a
        # synthetic combine commit which the composition contains by its parents.
        ancestry = {"repo_root": str(wt)}
        if not contains(ancestry, parents[2], task_base):
            raise SharedAppendUnsafe(f"{', '.join(sorted(eligible))}: incoming revision does not contain "
                                     f"recorded task base {task_base}")
        try:
            changed = set(paths.git(wt, "diff", "--name-only", "-z", task_base, parents[2]).split("\0"))
        except paths.GitError as exc:
            raise SharedAppendUnsafe(f"{', '.join(sorted(eligible))}: cannot read task diff from "
                                     f"{task_base}: {exc}") from exc
        eligible &= changed
        if not eligible:
            return False
        if not contains(ancestry, parents[1], task_base):
            raise SharedAppendUnsafe(f"{', '.join(sorted(eligible))}: composition {parents[1]} does not "
                                     f"contain task base {task_base}; restack accepted dependencies")
        bases = [task_base]
    else:
        bases = paths.git(wt, "merge-base", "--all", parents[1], parents[2]).splitlines()
    if len(bases) != 1:
        raise SharedAppendUnsafe(f"{', '.join(sorted(eligible))}: multiple or missing merge bases "
                                 f"({', '.join(bases) or 'none'}) between {parents[1]} and {parents[2]}; "
                                 "shared append ancestry is ambiguous")

    def blob(commit: str, path: str, *, absent_ok: bool = False) -> tuple[str, bytes] | None:
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise SharedAppendUnsafe(f"invalid shared path {path!r}")
        entry = subprocess.run(["git", "-C", str(wt), "ls-tree", commit, "--", path],
                               capture_output=True)
        if entry.returncode or not entry.stdout:
            if absent_ok and not entry.returncode:
                return None
            raise SharedAppendUnsafe(f"{path}: missing shared file in merge ancestry")
        mode = entry.stdout.split(b" ", 1)[0].decode("ascii", "replace")
        if mode not in ("100644", "100755") or b" blob " not in entry.stdout:
            raise SharedAppendUnsafe(f"{path}: shared path is not a regular file")
        raw = subprocess.run(["git", "-C", str(wt), "show", f"{commit}:{path}"],
                             capture_output=True)
        if raw.returncode or b"\0" in raw.stdout:
            raise SharedAppendUnsafe(f"{path}: binary or unreadable shared file")
        try:
            raw.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise SharedAppendUnsafe(f"{path}: invalid UTF-8 shared file") from None
        return mode, raw.stdout

    changes = {}
    for path in sorted(eligible):
        base_blob = blob(bases[0], path, absent_ok=True)
        (left_mode, left), (right_mode, right), (merged_mode, merged) = (
            blob(commit, path) for commit in (parents[1], parents[2], parents[0]))
        if base_blob is None:
            # Added on both sides: byte-identical adds converge to one file (#655). Differing adds
            # are an add/add conflict that Git already refuses, so nothing here may append them.
            if len({left_mode, right_mode, merged_mode}) != 1 or not left == right == merged:
                raise SharedAppendUnsafe(f"{path}: missing shared file in merge ancestry")
            continue
        base_mode, base = base_blob
        if len({base_mode, left_mode, right_mode, merged_mode}) != 1:
            raise SharedAppendUnsafe(f"{path}: shared file mode changed")
        if not left.startswith(base) or not right.startswith(base):
            raise SharedAppendUnsafe(f"{path}: non-append edit or truncated shared file")
        if base and not base.endswith(b"\n") and (left != base or right != base):
            raise SharedAppendUnsafe(f"{path}: append to an incomplete base line")
        a, b = left[len(base):], right[len(base):]
        if any(suffix and not suffix.endswith(b"\n") for suffix in (a, b)):
            raise SharedAppendUnsafe(f"{path}: incomplete appended record")
        target = wt / path
        if target.is_symlink() or not target.is_file():
            raise SharedAppendUnsafe(f"{path}: worktree shared path is not a regular file")
        if target.read_bytes() != merged:
            raise SharedAppendUnsafe(f"{path}: worktree contents differ from composition HEAD")
        expected = base + a + b
        if merged != expected:
            changes[path] = expected
    if not changes:
        return False
    try:
        for path, content in changes.items():
            target = wt / path
            fd = os.open(target, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            paths.git(wt, "add", "--", path)
        paths.git(wt, "-c", "commit.gpgsign=false", "commit", "--amend", "--no-edit",
                  env={**os.environ, **env})
    except (OSError, paths.GitError) as exc:
        raise SharedAppendUnsafe(f"shared append correction failed; retry from clean composition: {exc}") from exc
    return True


def finish_shared_append_merge(wt: Path, task: dict, previous: list[dict], *, env: dict) -> bool:
    """Union only provable append-only changes to identically declared shared files."""
    shared = {x[1:] for x in task.get("scope") or [] if planfile.is_shared(x)
              and not any(c in x for c in "*?[]")}
    earlier = {x[1:] for t in previous for x in t.get("scope") or [] if planfile.is_shared(x)}
    eligible = shared & earlier
    merging = paths.git(wt, "rev-parse", "MERGE_HEAD", check=False).strip()
    if merging:
        bases = paths.git(wt, "merge-base", "--all", "HEAD", merging).splitlines()
        if len(bases) != 1:
            return False  # ambiguous ancestry must never be resolved automatically
    proc = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U", "-z"],
                          capture_output=True)
    try:
        conflicts = [x.decode("utf8") for x in proc.stdout.split(b"\0") if x]
    except UnicodeDecodeError:
        return False
    if proc.returncode or not conflicts or not set(conflicts) <= eligible:
        return False
    output = {}
    for path in conflicts:
        if Path(path).is_absolute() or ".." in Path(path).parts:
            return False
        ls = subprocess.run(["git", "-C", str(wt), "ls-files", "-u", "-z", "--", path],
                            capture_output=True)
        modes = {}
        for entry in ls.stdout.split(b"\0"):
            if not entry:
                continue
            try:
                metadata, name = entry.split(b"\t", 1)
                mode, _, stage = metadata.split(b" ")
                if name.decode("utf-8") == path:
                    modes[int(stage)] = mode
            except (UnicodeDecodeError, ValueError):
                return False
        if ls.returncode or set(modes) != {1, 2, 3} or len(set(modes.values())) != 1                 or modes[1] not in (b"100644", b"100755"):
            return False
        parts = []
        for i in (1, 2, 3):
            o = subprocess.run(["git", "-C", str(wt), "show", f":{i}:{path}"], capture_output=True)
            if o.returncode or b"\0" in o.stdout:
                return False
            try:
                o.stdout.decode("utf8")
            except UnicodeDecodeError:
                return False
            parts.append(o.stdout)
        base, ours, theirs = parts
        if (not ours.startswith(base) or not theirs.startswith(base)
                or (base and not base.endswith(b"\n"))):
            return False
        a, b = ours[len(base):], theirs[len(base):]
        if any(v and not v.endswith(b"\n") for v in (a,b)):
            return False
        # Both suffixes are independent append operations. Set-deduplication
        # silently discards repeated records (even when they are intentional).
        # Preserve each side's byte-exact lines and their multiplicity.
        output[path] = base + a + b
    for path, content in output.items():
        target = wt / path
        if target.is_symlink() or not target.is_file():
            return False
        fd = os.open(target, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        if subprocess.run(["git", "-C", str(wt), "add", "--", path], capture_output=True).returncode:
            return False
    return subprocess.run(["git", "-C", str(wt), "commit", "--no-edit"],
                          capture_output=True, env=env).returncode == 0


class CombineConflict(Exception):
    """Two heads cannot be merged. `left` and `right` label them, `paths` are the conflicting files."""

    def __init__(self, left: str, right: str, paths: list[str]):
        self.left, self.right, self.paths = left, right, paths
        super().__init__(f"{left} and {right} conflict on {', '.join(paths[:6]) or 'files'}")


def _merge_tree(repo: str, a: str, b: str) -> tuple[str, list[str]]:
    """(tree, conflicting paths) of merging commits `a` and `b` without touching a checkout."""
    proc = subprocess.run(["git", "-C", repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", a, b],
                          capture_output=True, text=True)
    if proc.returncode == 129:  # git before 2.38 has no --write-tree: merge in a throwaway worktree
        return _merge_in_scratch(repo, a, b)
    if proc.returncode not in (0, 1):
        raise paths.GitError(("merge-tree", a, b), proc.returncode, proc.stderr.strip())
    tree, *files = [f for f in proc.stdout.split("\0") if f]
    return tree, files if proc.returncode == 1 else []


def _merge_in_scratch(repo: str, a: str, b: str) -> tuple[str, list[str]]:
    with tempfile.TemporaryDirectory(prefix="office-combine-") as tmp:
        scratch = str(Path(tmp) / "wt")
        paths.git(repo, "worktree", "add", "-q", "--detach", scratch, a)
        try:
            proc = subprocess.run(["git", "-C", scratch, "merge", "--no-edit", "-q", "-m", "office: combine", b],
                                  capture_output=True, text=True, env={**os.environ, **paths.commit_identity_env(repo)})
            if proc.returncode == 0:
                return paths.git(scratch, "rev-parse", "HEAD^{tree}"), []
            return "", paths.git(scratch, "diff", "--name-only", "--diff-filter=U", check=False).splitlines() or ["(unknown)"]
        finally:
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", scratch], capture_output=True)


def dependency_tips(run: dict, parents: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep distinct maximal heads before merging any ancestors (#714).

    A later accepted task may already resolve a conflict between two earlier
    parents. Trying those ancestors first would reject a valid accepted tip.
    """
    unique = []
    seen = set()
    for label, commit in parents:
        if commit not in seen:
            unique.append((label, commit))
            seen.add(commit)
    return [(label, commit) for label, commit in unique
            if not any(commit != other and gates._is_ancestor(run, commit, other) for _, other in unique)]


def combine(run: dict, parents: list[tuple[str, str]]) -> str:
    """One commit containing every `(label, commit)` head. A head that is an ancestor of another adds
    nothing; the rest are merged without touching any worktree. Raises CombineConflict naming the
    conflicting pair and paths, or GitError when git cannot merge (merge-tree needs git 2.38). Each commit it
    creates is anchored by a `refs/office/<run>/combine/<sha>` ref."""
    repo = run["repo_root"]
    acc = None
    seen: list[tuple[str, str]] = []
    for label, commit in dependency_tips(run, parents):
        if acc is None:
            acc = commit
        elif commit == acc or gates._is_ancestor(run, commit, acc):
            pass
        elif gates._is_ancestor(run, acc, commit):
            acc = commit
        else:
            tree, conflicts = _merge_tree(repo, acc, commit)
            if conflicts:
                # Name the earlier head that collides, not the whole accumulated result.
                culprit = next((l for l, c in seen if _merge_tree(repo, c, commit)[1]), None)
                raise CombineConflict(culprit or " + ".join(l for l, _ in seen), label, conflicts)
            acc = paths.git(repo, "commit-tree", tree, "-p", acc, "-p", commit, "-m",
                            f"office: combine {', '.join(l for l, _ in [*seen, (label, commit)])}\n\n"
                            f"{paths.office_trailer(run['id'])}", env={**os.environ, **paths.commit_identity_env(repo)})
            # No branch holds this commit: a ref under the run's namespace keeps gc from pruning the base a
            # dispatch records, and `prune` deletes it with the run's other refs/office/<run>/ refs.
            paths.git(repo, "update-ref", f"refs/office/{run['id'][:8]}/combine/{acc}", acc)
        seen.append((label, commit))
    return acc


def contains(run: dict, head: str, commit: str) -> bool:
    """`head` has `commit` in its history. A merge commit Office combined (never on the branch itself)
    counts when every parent of it is."""
    if gates._is_ancestor(run, commit, head):
        return True
    parents = paths.git(run["repo_root"], "rev-list", "--parents", "-n", "1", commit, check=False).split()[1:]
    return len(parents) > 1 and all(contains(run, head, p) for p in parents)
