"""One role-aware `office submit`.

Planner: the run's plan draft (.office/plans/<run>/PLAN.md) becomes the next plan version.
Executor: the worktree as it is right now (commits plus uncommitted edits)
becomes an immutable revision, and every applicable gate is queued. The same
tree under the same lease and applied version is the same submission, so a
lost response never duplicates work.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from office import db, dispatch as dispatch_mod, gates, jobs, paths, planpath, plans, state, version
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, now_iso, sha256_obj, short
def _draft(con, root: Path, run: dict) -> Path:
    planpath.relocate_legacy(con, root)
    return planpath.draft(root, run)


def submit(con, run: dict, *, cwd: Path, plan_path: str | None = None, redirect: dict | None = None) -> Result:
    """`redirect` ({defect, quote, root_cause, requirement, reviewer}) submits a plan
    revision that follows the user's redirect of a plan defect (office.redirect)."""
    dispatch_id = os.environ.get("OFFICE_DISPATCH_ID")
    if os.environ.get("OFFICE_ROLE") == "reviewer":
        raise Refused("reviewer-cannot-submit", "reviewers return their verdict in their reply; they do not submit")
    if redirect is not None:
        from office import redirect as redirect_mod
        d = state.get_dispatch(con, dispatch_id) if dispatch_id else None
        if d is not None and d["role"] != "planner":
            raise Refused("redirect-not-planner", "only the planner submits a defect redirect",
                          next_step="office submit")
        redirect = redirect_mod.validate(con, run, redirect, form=redirect_mod.SUBMIT_FORM)
    if dispatch_id:
        d = state.get_dispatch(con, dispatch_id)
        if d is None or d["run_id"] != run["id"]:
            raise Refused("unknown-dispatch", f"dispatch {dispatch_id} is not part of run {short(run['id'])}")
        if d["role"] == "planner":
            top = paths.repo_identity(cwd)
            base = top[0] if top else Path(d["worktree"])
            return plans.submit_plan(con, run, Path(plan_path) if plan_path else _draft(con, base, run),
                                     submitter=dispatch_id, dispatch_id=dispatch_id, redirect=redirect)
        return submit_revision(con, run, d, cwd)
    if run.get("planner_mode") == "dedicated" and not plan_path:
        raise Refused("orchestrator-no-submit", "in this run the dedicated planner owns the plan",
                      next_step='use office amend for ordinary changes, or office amend plan --contract -- "<request>"')
    ident = paths.repo_identity(cwd)
    if ident is None and not plan_path:
        raise Usage("no-repository", "run office submit from the repository", next_step="cd into the repository")
    path = Path(plan_path) if plan_path else _draft(con, ident[0], run)
    return plans.submit_plan(con, run, path, submitter="orchestrator", redirect=redirect)


def capture_tree(worktree: Path, scratch: Path, leave_out: list[str] | None = None,
                 restore: list[str] | None = None) -> tuple[str, str]:
    """(tree_sha, head_sha) of the worktree including uncommitted edits,
    without touching the worker's own index. `leave_out` names untracked
    paths that are not part of the revision; `restore` names tracked paths
    whose uncommitted edits are not part of it (they keep their HEAD content)."""
    scratch.mkdir(parents=True, exist_ok=True)
    fd, index = tempfile.mkstemp(prefix="index.", dir=str(scratch))
    os.close(fd)
    os.unlink(index)
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        head = paths.git(worktree, "rev-parse", "HEAD")
        paths.git(worktree, "read-tree", "HEAD", env=env)
        paths.git(worktree, "add", "-A", env=env)
        if leave_out:
            paths.git(worktree, "rm", "-q", "--cached", "--", *leave_out, env=env)
        if restore:
            paths.git(worktree, "reset", "-q", "HEAD", "--", *restore, env=env)
        tree = paths.git(worktree, "write-tree", env=env)
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass
    return tree, head


def matches_revision(worktree: Path, commit: str, scratch: Path) -> bool:
    """True when every file in `commit` is unchanged in the worktree. Files the
    check itself creates (caches, build output) do not count as a change."""
    scratch.mkdir(parents=True, exist_ok=True)
    fd, index = tempfile.mkstemp(prefix="verify.", dir=str(scratch))
    os.close(fd)
    os.unlink(index)
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        paths.git(worktree, "read-tree", commit, env=env)
        paths.git(worktree, "update-index", "-q", "--refresh", env=env, check=False)
        import subprocess
        proc = subprocess.run(["git", "-C", str(worktree), "diff-files", "--name-only", "-z", "--ignore-submodules"],
                              env=env, capture_output=True, text=True)
        if proc.returncode:
            return False
        # Harness config edits were left out of the revision at submit (it carries
        # HEAD's content there), so they are not a change to what was submitted.
        return all(_harness_path(f) and paths.git(worktree, "rev-parse", "--verify", "-q", f"{commit}:{f}", check=False)
                   == paths.git(worktree, "rev-parse", "--verify", "-q", f"HEAD:{f}", check=False)
                   for f in proc.stdout.split("\0") if f)
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass


def worktree_equals_commit(worktree: Path, commit: str) -> bool:
    """Read-only: every file in `commit` is unchanged and every untracked,
    non-ignored file in the worktree is identical in `commit`. Writes no git
    objects and nothing inside the repository or run state."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        if subprocess.run(["git", "-C", str(worktree), "read-tree", commit], env=env, capture_output=True).returncode:
            return False
        subprocess.run(["git", "-C", str(worktree), "update-index", "-q", "--refresh"], env=env, capture_output=True)
        if subprocess.run(["git", "-C", str(worktree), "diff-files", "--quiet", "--ignore-submodules"], env=env,
                          capture_output=True).returncode:
            return False
        others = subprocess.run(["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
                                env=env, capture_output=True, text=True).stdout.split("\0")
    for rel in [o for o in others if o]:
        in_commit = paths.git(worktree, "ls-tree", commit, "--", rel, check=False).split()
        if len(in_commit) < 3 or in_commit[2] != paths.git(worktree, "hash-object", "--", rel, check=False):
            return False
    return True


# Directories where harnesses keep their own config and hook output. Edits to
# tracked files here are the harness's, not the task's, unless the task owns them.
_HARNESS_DIRS = {".claude", ".codex", ".agents", ".office"}


def _harness_path(rel: str) -> bool:
    return bool(_HARNESS_DIRS.intersection(rel.split("/")[:-1]))


def untracked_outside(worktree: Path, scope) -> list[str]:
    """New, non-ignored files outside the task's scope. They are not the task's
    work and stay out of the revision. Tracked edits outside the scope are a
    different case and are still refused (see `harness_edits_outside`)."""
    from office import planfile
    others = paths.git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted(f for f in others if f and not planfile.path_in_scope(f, scope))


def harness_edits_outside(worktree: Path, scope) -> list[str]:
    """Tracked files under harness config paths that are edited (or deleted) in
    the worktree, outside the task's scope. They keep their HEAD content in the
    revision. Tracked edits to any other path outside the scope are not listed."""
    from office import planfile
    changed = paths.git(worktree, "diff", "--name-only", "-z", "HEAD").split("\0")
    return sorted(f for f in changed if f and _harness_path(f) and not planfile.path_in_scope(f, scope))


def make_commit(worktree: Path, tree: str, head: str, message: str) -> str:
    existing = paths.git(worktree, "rev-parse", "HEAD^{tree}")
    if existing == tree:
        return head
    env = dict(os.environ, **paths.commit_identity_env(worktree))
    return paths.git(worktree, "commit-tree", tree, "-p", head, "-m", message, env=env)


def submit_revision(con, run: dict, d: dict, cwd: Path) -> Result:
    task = state.get_task(con, run["id"], d["task_id"])
    if task["current_dispatch_id"] != d["id"]:
        raise Refused("superseded-dispatch", f"{task['id']} now belongs to a newer dispatch; this session's submit is rejected",
                      scope=task["id"], preserved="your worktree", next_step="stop; the current holder continues the task")
    ident = paths.repo_identity(cwd)
    wt = Path(d["worktree"]).resolve()
    if ident is None or ident[0] != wt:
        raise Refused("wrong-worktree", f"submit from the task worktree ({task['id']}), not {cwd}",
                      scope=task["id"], next_step=f"cd {wt} && office submit")
    left_out = untracked_outside(wt, task["scope"])
    restored = harness_edits_outside(wt, task["scope"])
    tree, head = capture_tree(wt, paths.run_dir(run["id"]) / "tmp", left_out, restored)
    applied = d["applied_plan_version"] or 0
    op_id = sha256_obj({"task": task["id"], "lease": d["lease_id"], "tree": tree, "applied": applied})
    existing = con.execute("SELECT * FROM revisions WHERE operation_id=?", (op_id,)).fetchone()
    if existing:
        # A replay of a submission whose response was lost: same receipt,
        # regardless of what has happened to the lease or task since.
        return _duplicate(con, run, dict(existing))
    if dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is None:
        raise Refused("lease-lost", f"{task['id']} lease is no longer held by this session (revoked or taken over)",
                      scope=task["id"], preserved="your worktree", next_step="stop; the orchestrator decides what happens next")
    if task["status"] in ("paused", "blocked", "cancelled"):
        raise Refused("task-paused", f"{task['id']} is {task['status']}: {task.get('pause_reason') or ''}",
                      scope=task["id"], preserved="your worktree", next_step="stop and wait; the orchestrator is resolving it")
    commit = make_commit(wt, tree, head, f"office: {task['id']} submission\n\nrun {run['id'][:8]} task {task['id']}\n\n"
                         f"{paths.office_trailer(run['id'])}")
    from office import planfile, prs
    touched = paths.git(wt, "diff", "--name-only", d["base_commit"], commit).split()
    outside = [f for f in touched if not planfile.path_in_scope(f, task["scope"])]
    if outside:
        with db.transaction(con):
            # A deterministic refusal: relaunching the same brief would repeat it.
            state.emit(con, run, "submit.refused", f"{task['id']} changed files outside its scope: {', '.join(outside[:6])}",
                       audience="runtime", task_id=task["id"], dispatch_id=d["id"], payload={"code": "outside-scope"})
        raise Refused("outside-scope", f"{task['id']} changed files outside its scope: {', '.join(outside[:6])}",
                      scope=task["id"], preserved="your worktree (nothing was submitted)",
                      next_step="revert those files, or stop and report that the scope must grow (the orchestrator amends it)")
    pending = con.execute("SELECT id, target_version FROM deliveries WHERE run_id=? AND task_id=? AND status IN "
                          "('queued','delivered') ORDER BY target_version DESC LIMIT 1", (run["id"], task["id"])).fetchone()
    res = Result()
    with db.transaction(con):
        again = con.execute("SELECT * FROM revisions WHERE operation_id=?", (op_id,)).fetchone()
        if again:
            return _duplicate(con, run, dict(again))
        if dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is None:
            raise Refused("lease-lost", f"{task['id']} lease was revoked during submit", scope=task["id"])
        seq = con.execute("SELECT COUNT(*) FROM revisions WHERE run_id=?", (run["id"],)).fetchone()[0] + 1
        # revisions.id is a GLOBAL primary key shared by every run in runs.db; a bare
        # per-run "R{seq}" collides with the first revision of any earlier run.
        rev_id = f"R{seq}-{run['id'][:8]}"
        prev = task.get("current_revision_id")
        prev_row = con.execute("SELECT * FROM revisions WHERE id=?", (prev,)).fetchone() if prev else None
        changed = paths.git(wt, "diff", "--name-only", prev_row["commit_sha"] if prev_row else d["base_commit"], commit).split()
        status = "amendment_pending" if pending else "current"
        env_fp = sha256_obj({"office": run["office_version"], "policy": run["config_hash"]})
        con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, lease_id, fencing, commit_sha, tree_sha, "
                    "base_commit, requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, "
                    "supersedes, changed_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rev_id, run["id"], task["id"], seq, d["id"], d["lease_id"],
                     (dispatch_mod.live_lease(con, run["id"], d["lease_id"]) or {}).get("fencing"), commit, tree,
                     d["base_commit"], run["requirements_version"], run["plan_version"], applied, env_fp, op_id, status,
                     prev, dumps(changed), now_iso()))
        paths.git(wt, "update-ref", f"refs/office/{run['id'][:8]}/{task['id']}/{rev_id}", commit)
        if prs.enabled(run):
            prs.advance_branch(run, d, commit)
            prs.queue(con, run, task["id"], "revision", rev_id)
        dispatch_mod.renew_lease(con, d["lease_id"])
        if status == "amendment_pending":
            state.emit(con, run, "submit.amendment_pending", f"{task['id']} {rev_id} submitted under p{applied}; "
                       "amendment pending", audience=f"dispatch:{d['id']}", task_id=task["id"])
            res.add(f"rev {rev_id} captured | amendment pending: apply it, then office ack; not a failure")
            res.next = "apply the delivered amendment, office ack <id>, then office submit"
            return res
        if prev:
            con.execute("UPDATE revisions SET status='superseded' WHERE id=? AND status='current'", (prev,))
            gates.stale_open_gates(con, run, task["id"], rev_id)
        state.update_task(con, run["id"], task["id"], current_revision_id=rev_id, status="submitted")
        planned = gates.plan_for_revision(con, run, state.get_task(con, run["id"], task["id"]), rev_id, changed, d)
        state.emit(con, run, "submit", f"{task['id']} submitted {rev_id}", audience="runtime", task_id=task["id"])
    jobs.kick(con, run["id"])
    parts = [f"rev {rev_id} captured"] + ([f"supersedes {prev}"] if prev else []) + planned["summary"]
    res.add(" | ".join(parts))
    for label, names in (("untracked files outside", left_out), ("harness config edits outside", restored)):
        if names:
            shown = ", ".join(names[:4]) + (f" (+{len(names) - 4} more)" if len(names) > 4 else "")
            res.add(f"warning: left out of {rev_id} ({label} {task['id']} scope): {shown}")
    res.next = "you may stop; results will be delivered"
    res.data = {"revision": rev_id, "commit": commit, "gates": planned["gates"]}
    return res


def _duplicate(con, run: dict, rev: dict) -> Result:
    pending = con.execute("SELECT kind, status FROM gates WHERE revision_id=? AND status IN ('queued','running')",
                          (rev["id"],)).fetchall()
    note = "verification still running" if pending else f"status {rev['status']}"
    return Result(lines=[f"{rev['id']} already submitted | {note}"], next="no action",
                  data={"revision": rev["id"], "duplicate": True})
