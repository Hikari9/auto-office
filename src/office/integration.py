"""Integration: verify the actual composed result, not each green worktree.

When every task is accepted the runtime composes the accepted revisions onto
the run base in dependency order, runs the run-level checks on the composed
tree, and runs an integration review only at a real boundary (Q7): a task
built on another's unmerged output, or two tasks that changed the same file
or share a declared interface.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from office import briefs, db, gates, paths, review_parse, state
from office.util import dumps, now_iso, sha256_obj


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


def _set_integration(con, run: dict, **fields) -> None:
    run = state.get_run(con, run["id"])
    landing = dict(run.get("landing") or {})
    integ = dict(landing.get("integration") or {})
    integ.update(fields)
    landing["integration"] = integ
    state.update_run(con, run["id"], landing=landing)


def job_integrate(con, run: dict, job: dict) -> dict:
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
        paths.git(repo, "worktree", "add", "-B", branch, str(wt), run["base_sha"])
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
    run_checks = list((run.get("landing") or {}).get("run_checks") or [])
    integ_rev = {"id": "I" + commit[:7], "commit_sha": commit, "tree_sha": tree, "base_commit": run["base_sha"],
                 "dispatch_id": None}
    with db.transaction(con):
        _set_integration(con, run, key=key, status="verifying", detail=why, branch=branch, commit=commit,
                         review=needs_review)
    results = {}
    if run_checks:
        gid = _gate(con, run, integ_rev, "checks", commit)
        outcome = gates.run_commands(con, run, run_checks, wt, integ_rev, {"id": gid, "task_id": None}, check_tree=False)
        with db.transaction(con):
            con.execute("UPDATE gates SET status='done', verdict=?, summary=?, finished_at=? WHERE id=?",
                        (outcome["verdict"], outcome.get("summary"), now_iso(), gid))
        results["checks"] = outcome["verdict"]
        if outcome["verdict"] != "PASS":
            detail = f"run checks {outcome['verdict']}: {outcome.get('summary', '')[:160]}"
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
        diff = paths.git(repo, "diff", run["base_sha"], commit)[: gates.MAX_DIFF_CHARS]
        checkout = gates.detached_checkout(run, commit, f"integration-{gid}")
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
        _set_integration(con, run, status="accepted", detail="composed result verified")
        state.emit(con, run, "integration.accepted", f"READY: integration PASS on {commit[:10]} ({branch})")
    return results


MISSING_DEPS_HINT = ("the composed worktree is a fresh checkout with no installed dependencies (anything not in git, "
                     "such as node_modules or a virtualenv, is absent and is recreated on every compose); make the "
                     "plan's run-level `checks:` install them first, e.g. `pnpm install --frozen-lockfile && pnpm lint`, "
                     "then office amend plan")


def missing_deps_hint(outcome: dict) -> str | None:
    """When a run-level check could not find its command, say why that happens
    on the composed tree and what the plan must do. Office never runs a package
    manager on its own."""
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
