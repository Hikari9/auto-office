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
    paths.git(repo, "worktree", "add", "-B", branch, str(wt), run["base_sha"])
    env = {"GIT_AUTHOR_NAME": "Auto Office", "GIT_AUTHOR_EMAIL": "office@localhost",
           "GIT_COMMITTER_NAME": "Auto Office", "GIT_COMMITTER_EMAIL": "office@localhost"}
    import os
    genv = dict(os.environ, **env)
    for t in tasks:
        commit = revs[t["id"]]["commit_sha"]
        if subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", commit, "HEAD"], capture_output=True).returncode == 0:
            continue
        proc = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", "--no-edit", "-m", f"office: land {t['id']}", commit],
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
            with db.transaction(con):
                _set_integration(con, run, status="blocked", detail=f"run checks {outcome['verdict']}: {outcome.get('summary', '')[:160]}")
                state.emit(con, run, "integration.failed", f"INTEGRATION checks {outcome['verdict']} on the composed result")
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
