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
import time
import uuid
from pathlib import Path

from office import briefs, contract, db, gates, paths, review_parse, state, worktree_setup
from office.util import claim_alive, dumps, now_iso, sha256_obj


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


def stale_base(run: dict, base: str, head: str) -> str | None:
    """The run's new base (`office land --rebase`) when the worktree at `head` already holds it but the
    task's recorded `base` does not: the default branch's own files would read as the task's edits
    until the orchestrator records the move (`office rebase <task>`)."""
    onto = ((run.get("landing") or {}).get("rebase") or {}).get("onto")
    if onto and not gates._is_ancestor(run, onto, base) and gates._is_ancestor(run, onto, head):
        return onto
    return None


def dependency_heads(con, run: dict, task: dict, *, stack_after: str | None = None) -> list[dict]:
    """[{task, revision, commit}] for each dependency (and the task it is stacked after) that has a
    revision: its accepted one, else its current one. A dependency with none is the caller's to
    report, except the stacked-after task, which may simply not have submitted yet."""
    out = []
    for dep in dict.fromkeys(list(task["depends"]) + ([stack_after] if stack_after else [])):
        d = state.get_task(con, run["id"], dep) or {}
        rev_id = d.get("accepted_revision_id") or d.get("current_revision_id")
        row = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (rev_id,)).fetchone() if rev_id else None
        if row:
            out.append({"task": dep, "revision": rev_id, "commit": row["commit_sha"]})
    return out


def _merge_tree(repo: Path, a: str, b: str) -> tuple[str, list[str]]:
    """(merged tree, conflicted paths) of two commits, however many merge bases they have."""
    proc = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", "--name-only", "-z", a, b],
                          capture_output=True, text=True)
    if proc.returncode == 129:
        raise state.Refused("git-too-old", "this needs git >= 2.38 (git merge-tree --write-tree)",
                            next_step="upgrade git, then retry")
    if proc.returncode not in (0, 1):
        raise state.Refused("merge-tree-failed", f"git merge-tree could not merge {a[:12]} and {b[:12]}: "
                            f"{(proc.stderr or proc.stdout).strip()[:200]}", next_step="office doctor")
    tree, files = parse_merge_tree(proc.stdout)
    return tree, (files if proc.returncode == 1 else [])


def parse_merge_tree(out: str) -> tuple[str, list[str]]:
    """(tree, conflicted paths) from `git merge-tree --write-tree --name-only -z`. Paths are made printable:
    a committed name is shown in messages and briefs, so control characters never reach them."""
    tree, *rest = out.split("\0")
    files = []
    for name in rest:
        if not name:
            break
        files.append("".join(c if c.isprintable() else "?" for c in name))
    return tree, files


def combine(run: dict, parts: list[dict], what: str) -> str:
    """One commit that contains every part's commit: the part that already contains the others
    when one does, else an Office merge commit of them (never checked out, reachable from the
    branch that starts there). Parts that conflict refuse, naming the tasks and paths.
    `parts` are {task, commit, ...} in dependency order."""
    repo = Path(run["repo_root"])
    heads: list[dict] = []
    for p in parts:
        same = next((h for h in heads if h["commit"] == p["commit"]), None)
        if same:
            same["tasks"].append(p["task"])
        else:
            heads.append({"commit": p["commit"], "tasks": [p["task"]]})
    heads = [h for h in heads if not any(o is not h and gates._is_ancestor(run, h["commit"], o["commit"]) for o in heads)]
    if not heads:
        return run["base_sha"]
    cur = heads[0]
    for nxt in heads[1:]:
        tree, files = _merge_tree(repo, cur["commit"], nxt["commit"])
        if files:
            raise _conflict(repo, what, heads, cur, nxt, files)
        env = {**os.environ, **paths.commit_identity_env(repo)}
        names = sorted(set(cur["tasks"] + nxt["tasks"]))
        commit = subprocess.run(
            ["git", "-C", str(repo), "commit-tree", tree, "-p", cur["commit"], "-p", nxt["commit"], "-m",
             f"office: base of {what}, a merge of {', '.join(names)}\n\n{paths.office_trailer(run['id'])}"],
            capture_output=True, text=True, env=env)
        if commit.returncode != 0:
            raise state.Refused("merge-commit-failed", f"could not record the merged base of {what}: {commit.stderr.strip()[:200]}")
        cur = {"commit": commit.stdout.strip(), "tasks": names}
    return cur["commit"]


def _conflict(repo: Path, what: str, heads: list[dict], cur: dict, nxt: dict, files: list[str]) -> state.Refused:
    """Name the dependency pairs whose revisions conflict, or the group the fold stopped on."""
    pairs = []
    for i, a in enumerate(heads):
        for b in heads[i + 1:]:
            _, conflicted = _merge_tree(repo, a["commit"], b["commit"])
            if conflicted:
                pairs.append((a["tasks"], b["tasks"], conflicted))
    if not pairs:
        pairs = [(cur["tasks"], nxt["tasks"], files)]
    said = "; ".join(f"{'+'.join(a)} and {'+'.join(b)} conflict on {', '.join(f[:8])}" for a, b, f in pairs)
    tasks = sorted({t for a, b, _ in pairs for t in a + b})
    return state.Refused("dependency-conflict", f"{what}: its dependency revisions conflict with each other: {said}",
                         preserved="every dependency's accepted revision",
                         next_step=f"make the later of {' and '.join(tasks)} depend on the other (office amend plan), "
                                   "or amend one of them so their changes agree; then dispatch or rerun again")


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
    rows = con.execute("SELECT claimed_pid, claimed_by FROM outbox WHERE run_id=? AND kind='integrate' AND status='claimed'",
                       (run["id"],)).fetchall()
    return [r["claimed_pid"] for r in rows if r["claimed_pid"] and r["claimed_pid"] != os.getpid()
            and str(r["claimed_pid"]) not in fenced and claim_alive(r["claimed_pid"], r["claimed_by"])]


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
    for t in tasks:
        commit = revs[t["id"]]["commit_sha"]
        if subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", commit, "HEAD"], capture_output=True).returncode == 0:
            continue
        proc = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", "--no-edit", "-m", f"office: land {t['id']}\n\n{paths.office_trailer(run['id'])}", commit],
                              capture_output=True, text=True, env=genv)
        if proc.returncode != 0:
            conflicted = paths.git(wt, "diff", "--name-only", "--diff-filter=U", check=False)
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True)
            detail = f"{t['id']} conflicts with earlier tasks on {conflicted.replace(chr(10), ', ') or 'files'}"
            with db.transaction(con):
                _set_integration(con, run, key=key, status="conflict", detail=detail, branch=branch)
                state.emit(con, run, "integration.conflict", f"INTEGRATION CONFLICT: {detail}")
            return {"status": "conflict"}
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
    if run_checks:
        # The merged lockfile is in place; install before the checks that need it.
        worktree_setup.prepare(run, wt, "integration", paths.run_dir(run["id"]) / "setup" / "integration.log", created=True)
        gid = _gate(con, run, integ_rev, "checks", commit)
        outcome = gates.run_commands(con, run, run_checks, wt, integ_rev, {"id": gid, "task_id": None}, check_tree=False)
        with db.transaction(con):
            con.execute("UPDATE gates SET status='done', verdict=?, summary=?, finished_at=? WHERE id=?",
                        (outcome["verdict"], outcome.get("summary"), now_iso(), gid))
        results["checks"] = outcome["verdict"]
        if outcome["verdict"] != "PASS":
            detail = blocked_detail(outcome)
            hint = missing_deps_hint(outcome)
            if hint:
                detail += f" | {hint}"
            with db.transaction(con):
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
    with db.transaction(con):
        if compose_base(state.get_run(con, run["id"])) != base:
            return {"skipped": "rebased while composing"}
        _set_integration(con, run, status="accepted", detail="composed result verified")
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


def review_boundary(con, run: dict, tasks: list[dict], revs: dict) -> tuple[bool, str]:
    if len(tasks) < 2:
        return False, "single task: composed result equals the accepted revision"
    changed = {t["id"]: set(json.loads(revs[t["id"]].get("changed_json") or "[]")) |
               set(paths.git(Path(run["repo_root"]), "diff", "--name-only", run["base_sha"], revs[t["id"]]["commit_sha"]).split())
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
