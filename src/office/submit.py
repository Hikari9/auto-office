"""One role-aware `office submit`.

Planner: the run's plan draft (.office/plans/<run>/PLAN.md) becomes the next plan version.
Executor: the worktree as it is right now (commits plus uncommitted edits)
becomes an immutable revision, and every applicable gate is queued. The same
tree under the same lease and applied version is the same submission, so a
lost response never duplicates work.
"""
from __future__ import annotations

import os
import shlex
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from office import briefs, contract, db, discovery, dispatch as dispatch_mod, gates, jobs, paths, planpath, plans, state, version
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, now_iso, sha256_bytes, sha256_file, sha256_obj, short
def _draft(con, root: Path, run: dict) -> Path:
    planpath.relocate_legacy(con, root)
    return planpath.draft(root, run)


# A task blocked by its own worker's refused or scope-requesting submit. The
# worker may still resubmit (revert, or after the amendment), so these do not
# refuse a submit the way an orchestrator pause does.
SELF_BLOCK = ("submit refused", "scope requested")


def self_blocked(task: dict) -> bool:
    return task["status"] == "blocked" and (task.get("pause_reason") or "").startswith(SELF_BLOCK)


def _record_block(con, run: dict, task: dict, d: dict, reason: str, state_key: str, state_val: dict) -> None:
    """Durable: the task is a blocker the orchestrator sees at once, and the
    dispatch keeps what it was blocked on. Caller holds the transaction. Ownership is
    re-read here: a revoke or takeover since the caller looked refuses and changes nothing."""
    import json
    task = state.get_task(con, run["id"], task["id"])
    if task["current_dispatch_id"] != d["id"]:
        raise Refused("superseded-dispatch", f"{task['id']} now belongs to a newer dispatch; this session's submit is rejected",
                      scope=task["id"], preserved="your worktree", next_step="stop; the current holder continues the task")
    if dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is None:
        raise Refused("lease-lost", f"{task['id']} lease is no longer held by this session (revoked or taken over)",
                      scope=task["id"], preserved="your worktree", next_step="stop; the orchestrator decides what happens next")
    if task["status"] in ("paused", "blocked", "cancelled") and not self_blocked(task):
        raise Refused("task-paused", f"{task['id']} is {task['status']}: {task.get('pause_reason') or ''}",
                      scope=task["id"], preserved="your worktree", next_step="stop and wait; the orchestrator is resolving it")
    row = con.execute("SELECT override_json FROM dispatches WHERE id=?", (d["id"],)).fetchone()
    data = json.loads((row["override_json"] if row else None) or "{}")
    data[state_key] = state_val
    data["block_id"] = uuid.uuid4().hex[:12]  # names this block: a stale notice cannot lift a newer one
    # Never replace another blocker (revoke, orchestrator pause) with this one. A
    # `submitted` task is blocked too: its revision and gates stay as they are (acceptance
    # waits while the task is blocked) and the status it had is kept to restore on unblock.
    if task["status"] in ("running", "launching", "changes_required", "submitted"):
        data["blocked_from"] = task["status"]
        state.update_task(con, run["id"], task["id"], status="blocked", pause_reason=reason)
    elif self_blocked(task):
        state.update_task(con, run["id"], task["id"], pause_reason=reason)
    con.execute("UPDATE dispatches SET override_json=? WHERE id=?", (json.dumps(data), d["id"]))


def block_id(con, dispatch_id: str | None) -> str | None:
    import json
    row = con.execute("SELECT override_json FROM dispatches WHERE id=?", (dispatch_id,)).fetchone() if dispatch_id else None
    return json.loads((row["override_json"] if row else None) or "{}").get("block_id")


def unblock_self(con, run: dict, task: dict) -> str | None:
    """Lift a block this task's own worker caused, back to the status it was blocked from
    (`running` for a block recorded before that was kept). Caller holds the transaction.
    Returns the restored status, or None when the task is not self-blocked."""
    import json
    if not self_blocked(task):
        return None
    d = state.get_dispatch(con, task["current_dispatch_id"]) if task.get("current_dispatch_id") else None
    prior = json.loads((d or {}).get("override_json") or "{}").get("blocked_from")
    if prior not in ("running", "launching", "changes_required", "submitted"):
        prior = "running"
    if prior == "submitted" and not task.get("current_revision_id"):
        prior = "running"
    state.update_task(con, run["id"], task["id"], status=prior, pause_reason=None)
    return prior


def submit(con, run: dict, *, cwd: Path, plan_path: str | None = None, redirect: dict | None = None,
           request_scope: list[str] | None = None, reason: str = "", exempt: str | None = None) -> Result:
    """A refusal inside a dispatch is recorded on it, so a worker that stopped
    after one can be told from one that is still working."""
    try:
        if request_scope:
            return request_scope_change(con, run, cwd=cwd, files=request_scope, reason=reason)
        return _submit(con, run, cwd=cwd, plan_path=plan_path, redirect=redirect, exempt=exempt, reason=reason)
    except Refused as exc:
        dispatch_id = os.environ.get("OFFICE_DISPATCH_ID")
        # outside-scope records its own events, which also block the task on exit.
        if dispatch_id and exc.category != "outside-scope":
            d = state.get_dispatch(con, dispatch_id)
            if d is not None and d["run_id"] == run["id"]:
                with db.transaction(con):
                    state.emit(con, run, "submit.rejected", f"{exc.category}: {exc.message}", audience="runtime",
                               task_id=d.get("task_id"), dispatch_id=dispatch_id, payload={"code": exc.category})
                    if exc.category not in ("self-review-ledger-signaled", "self-review-stale", "self-review-exemption"):
                        signal_refused(con, run, d, f"{exc.category}: {exc.message}")
        raise


def signal_refused(con, run: dict, d: dict, reason: str) -> None:
    """A refused submit is a worker stopped on something only the orchestrator resolves: `office
    wait` returns for it at once. Caller holds the transaction."""
    tid = d.get("task_id") or "run"
    if reason.startswith("superseded-dispatch"):  # a stale session ended itself; the current holder continues
        nxt = f"none: {tid} has a newer session; office status shows it"
    else:
        nxt = (f"office status; then office rerun {tid} --resume|--fresh, office revoke {tid}, "
               f"or office amend {tid} -- \"<change>\"")
    state.signal_orchestrator(con, run, source="submit refused", task_id=d.get("task_id"), dispatch_id=d["id"],
                              reason=reason, next_step=nxt)


def _bounded(argv: list[str], cwd: Path, limit: int) -> tuple[str, bool]:
    """Run `argv`, reading at most `limit` bytes of stdout: past that the process is
    killed, so a huge diff is never buffered. Returns (text, truncated)."""
    import subprocess
    proc = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        data = proc.stdout.read(limit + 1)
        cut = len(data) > limit
        if cut:
            proc.kill()
    finally:
        proc.stdout.close()
        proc.wait()
    return data[:limit].decode("utf-8", errors="replace"), cut


def clean_request_paths(wt: Path, files: list[str]) -> list[str]:
    """Normalized, repo-relative request paths; anything empty, absolute, pathspec magic,
    or outside the worktree is refused before the worktree is read."""
    out = []
    for raw in files:
        f = (raw or "").strip()
        bad = (not f or os.path.isabs(f) or f.startswith(":") or "\0" in f
               or os.path.normpath(f) in (".", "..") or os.path.normpath(f).startswith("../")
               or not (wt / f).resolve().is_relative_to(wt))
        if bad:
            raise Usage("scope-path", f"cannot request scope for {raw!r}: give a path inside the task worktree",
                        next_step='office submit --request-scope <repo-relative path> -- "<reason>"')
        out.append(os.path.normpath(f))
    if not out:
        raise Usage("scope-path", "name at least one path to add to the scope",
                    next_step='office submit --request-scope <repo-relative path> -- "<reason>"')
    out = list(dict.fromkeys(out))
    if len(out) > 50:
        raise Usage("scope-path", f"{len(out)} paths is too many for one request; ask for a directory or split it",
                    next_step='office submit --request-scope <dir>/ -- "<reason>"')
    return out


def _scope_hunk(wt: Path, files: list[str], base: str = "HEAD", limit: int = 6000) -> str:
    """Everything changed since `base` (committed, staged, unstaged) plus the contents of
    new (untracked) files, read up to `limit` bytes."""
    text, cut = _bounded(["git", "--literal-pathspecs", "diff", base, "--", *files], wt, limit)
    if not cut:
        new = paths.git(wt, "--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "-z", "--", *files)
        names = [x for x in new.split("\0") if x and (wt / x).is_file()]
        over = len(names) > 200  # bound enumeration; the byte limit bounds the contents
        for f in names[:200]:
            more, cut = _bounded(["git", "diff", "--no-index", "--", "/dev/null", f], wt, limit - len(text))
            text += ("\n" if text else "") + more
            if cut or len(text) >= limit:
                cut = True
                break
        cut = cut or over
    if not text.strip():
        return "(no diff for these paths yet; they are unedited or do not exist)"
    return text + "\n... (truncated)" if cut else text


def request_scope_change(con, run: dict, *, cwd: Path, files: list[str], reason: str) -> Result:
    """Executor: ask the orchestrator to widen this task's scope. Blocks the
    task and wakes `office wait`/`status` with the files, reason and diff hunk."""
    dispatch_id = os.environ.get("OFFICE_DISPATCH_ID")
    d = state.get_dispatch(con, dispatch_id) if dispatch_id else None
    if d is None:
        d = _worktree_dispatch(con, run, cwd)
    if d is None or d["role"] != "executor" or not d.get("task_id"):
        raise Refused("not-an-executor", "--request-scope is for an executor inside its task worktree",
                      next_step="office submit")
    if not reason.strip():
        raise Usage("scope-reason", "say why the scope must grow",
                    next_step='office submit --request-scope <path> -- "<reason>"')
    reason = reason.strip()[:2000]
    task = state.get_task(con, run["id"], d["task_id"])
    wt = Path(d["worktree"]).resolve()
    ident = paths.repo_identity(cwd)
    if ident is None or ident[0] != wt:
        raise Refused("wrong-worktree", f"run this from the task worktree ({task['id']}), not {cwd}",
                      scope=task["id"], next_step=f'cd {shlex.quote(str(wt))} && office submit --request-scope '
                                                  f'{shlex.quote(files[0] if files else "<path>")} -- "<reason>"')
    files = clean_request_paths(wt, files)
    if task["current_dispatch_id"] != d["id"]:
        raise Refused("superseded-dispatch", f"{task['id']} now belongs to a newer dispatch; this session's request is rejected",
                      scope=task["id"], preserved="your worktree", next_step="stop; the current holder continues the task")
    if dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is None:
        raise Refused("lease-lost", f"{task['id']} lease is no longer held by this session (revoked or taken over)",
                      scope=task["id"], preserved="your worktree", next_step="stop; the orchestrator decides what happens next")
    if task["status"] in ("paused", "blocked", "cancelled") and not self_blocked(task):
        raise Refused("task-paused", f"{task['id']} is {task['status']}: {task.get('pause_reason') or ''}",
                      scope=task["id"], preserved="your worktree", next_step="stop and wait; the orchestrator is resolving it")
    hunk = _scope_hunk(wt, files, d.get("base_commit") or "HEAD")
    # Executor-controlled text reaches a command the orchestrator copies: one quoted argument.
    amend_cmd = ("office amend " + shlex.quote(task["id"]) + " --contract -- "
                 + shlex.quote(f'add {", ".join(files)} to {task["id"]} scope: {reason.strip()[:80]}'))
    summary = f"{task['id']} requests scope {', '.join(files)}: {reason.strip()}"
    with db.transaction(con):
        _record_block(con, run, task, d, f"scope requested: {', '.join(files)}: {reason.strip()}",
                      "scope_request", {"files": files, "reason": reason.strip()})
        state.emit(con, run, "task.scope_requested", f"{summary}\n{hunk}\nnext: {amend_cmd}", task_id=task["id"],
                   dispatch_id=d["id"], payload={"files": files, "reason": reason.strip(), "diff": hunk, "next": amend_cmd})
    return Result(lines=[f"{task['id']} scope request recorded for {', '.join(files)}; the orchestrator was woken"],
                  next="stop and wait; the orchestrator amends your contract and you are told to resubmit "
                       f"(it runs: {amend_cmd})")


def _submit(con, run: dict, *, cwd: Path, plan_path: str | None = None, redirect: dict | None = None,
            exempt: str | None = None, reason: str = "") -> Result:
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
        return submit_revision(con, run, d, cwd, exempt=exempt, reason=reason)
    d = None if plan_path else _worktree_dispatch(con, run, cwd)
    if d is not None:
        # A restarted executor session lost OFFICE_DISPATCH_ID; its task
        # worktree still names the dispatch, and the lease check still fences it.
        return submit_revision(con, run, d, cwd, exempt=exempt, reason=reason)
    if not plan_path:
        _refuse_lost_executor(con, run, cwd)
    if run.get("planner_mode") == "dedicated" and not plan_path:
        raise Refused("orchestrator-no-submit", "in this run the dedicated planner owns the plan",
                      next_step='use office amend for ordinary changes, or office amend plan --contract -- "<request>"')
    ident = paths.repo_identity(cwd)
    if ident is None and not plan_path:
        raise Usage("no-repository", "run office submit from the repository", next_step="cd into the repository")
    path = Path(plan_path) if plan_path else _draft(con, ident[0], run)
    return plans.submit_plan(con, run, path, submitter="orchestrator", redirect=redirect)


def _refuse_lost_executor(con, run: dict, cwd: Path) -> None:
    """The orchestrator path with no plan to submit while executors are open:
    the caller is most likely an executor that lost its dispatch env."""
    ident = paths.repo_identity(cwd)
    if run.get("planner_mode") != "dedicated" and ident is not None and _draft(con, ident[0], run).exists():
        return
    open_ = discovery.open_executor_dispatches(con, run)
    if open_:
        raise Refused("executor-lost-binding",
                      f"no plan draft to submit, and run {short(run['id'])} has open executor dispatches; if you are "
                      "one of those executors your dispatch env was lost; each executor's recovery command:",
                      # One labelled line per dispatch and nothing chaining
                      # them, so pasting the block cannot submit every task.
                      data={"candidates": [f"{d['task_id']} ({d['id']}): {discovery.recovery_command(run, d)}"
                                           for d in open_]},
                      next_step="run only your own task's line above")


def _worktree_dispatch(con, run: dict, cwd: Path) -> dict | None:
    """The current executor dispatch whose task worktree is `cwd`, if any."""
    ident = paths.repo_identity(cwd)
    if ident is None:
        return None
    for t in state.tasks(con, run["id"]):
        d = state.get_dispatch(con, t["current_dispatch_id"]) if t.get("current_dispatch_id") else None
        if d and d["role"] == "executor" and d.get("worktree") and Path(d["worktree"]).resolve() == ident[0]:
            return d
    return None


def _dependency_bases(con, run: dict, task: dict, commit: str) -> list[str]:
    """Commits of the task's dependencies' newest revisions (accepted, else
    current) that `commit` already contains: a stacked task that merged its
    parent's later revision builds on it, not on the revision it started from."""
    out = []
    for dep in task["depends"]:
        dt = state.get_task(con, run["id"], dep) or {}
        rev_id = dt.get("accepted_revision_id") or dt.get("current_revision_id")
        row = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (rev_id,)).fetchone() if rev_id else None
        if row and gates._is_ancestor(run, row["commit_sha"], commit):
            out.append(row["commit_sha"])
    return out


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


def restore_tracked_paths(worktree: Path, commit: str, scratch: Path) -> list[str]:
    """Restore tracked worktree changes from `commit`, preserving untracked output."""
    import subprocess
    scratch.mkdir(parents=True, exist_ok=True)
    fd, index = tempfile.mkstemp(prefix="restore.", dir=str(scratch))
    os.close(fd)
    os.unlink(index)
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        paths.git(worktree, "read-tree", commit, env=env)
        paths.git(worktree, "update-index", "-q", "--refresh", env=env, check=False)
        changed = subprocess.run(
            ["git", "-C", str(worktree), "diff-files", "--name-only", "-z", "--ignore-submodules"],
            env=env, capture_output=True, check=True).stdout.split(b"\0")
        tracked = sorted(os.fsdecode(path) for path in changed if path)
        if tracked:
            # The temporary index contains only paths from the submitted revision.
            # checkout-index therefore restores tracked content and leaves all
            # untracked and ignored serve output in place.
            paths.git(worktree, "checkout-index", "--force", "--all", env=env)
        return tracked
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


def submit_revision(con, run: dict, d: dict, cwd: Path, *, exempt: str | None = None, reason: str = "") -> Result:
    task = state.get_task(con, run["id"], d["task_id"])
    if task["current_dispatch_id"] != d["id"]:
        raise Refused("superseded-dispatch", f"{task['id']} now belongs to a newer dispatch; this session's submit is rejected",
                      scope=task["id"], preserved="your worktree", next_step="stop; the current holder continues the task")
    ident = paths.repo_identity(cwd)
    wt = Path(d["worktree"]).resolve()
    if ident is None or ident[0] != wt:
        raise Refused("wrong-worktree", f"submit from the task worktree ({task['id']}), not {cwd}",
                      scope=task["id"], next_step=f"cd {shlex.quote(str(wt))} && office submit")
    seen_block = block_id(con, d["id"]) if self_blocked(task) else None  # the block this submit may resolve
    left_out = untracked_outside(wt, task["scope"])
    ledger = _executor_ledger(wt)
    if ledger and briefs.LEDGER_FILE not in left_out and paths.git(
            wt, "ls-files", "--others", "--cached", "--exclude-standard", "--", briefs.LEDGER_FILE).strip():
        left_out.append(briefs.LEDGER_FILE)  # never part of a revision, whatever the scope covers or staged
    restored = harness_edits_outside(wt, task["scope"])
    tree, head = capture_tree(wt, paths.run_dir(run["id"]) / "tmp", left_out, restored)
    applied = d["applied_plan_version"] or 0
    op_id = sha256_obj({"task": task["id"], "lease": d["lease_id"], "tree": tree, "applied": applied})
    existing = con.execute("SELECT * FROM revisions WHERE operation_id=?", (op_id,)).fetchone()
    if existing:
        # A replay of a submission whose response was lost: same receipt,
        # regardless of what has happened to the lease or task since.
        with db.transaction(con):
            cur = state.get_task(con, run["id"], task["id"])
            # A worker that reverted the refused file resubmits the tree already captured:
            # that clears its own block (and lets a finished review accept the task).
            if cur["current_dispatch_id"] == d["id"] and self_blocked(cur) \
                    and dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is not None:
                if unblock_self(con, run, cur) == "submitted":
                    gates.evaluate_acceptance(con, run, task["id"])
        return _duplicate(con, run, dict(existing))
    if dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is None:
        raise Refused("lease-lost", f"{task['id']} lease is no longer held by this session (revoked or taken over)",
                      scope=task["id"], preserved="your worktree", next_step="stop; the orchestrator decides what happens next")
    if task["status"] in ("paused", "blocked", "cancelled") and not self_blocked(task):
        raise Refused("task-paused", f"{task['id']} is {task['status']}: {task.get('pause_reason') or ''}",
                      scope=task["id"], preserved="your worktree", next_step="stop and wait; the orchestrator is resolving it")
    # A duplicate submission is returned above before this check. For a new revision,
    # use the same ledger gate as preflight against committed and pending work.
    from office import preflight
    base = d["base_commit"]
    dep_bases = [b for b in _dependency_bases(con, run, task, head) if b != base]
    tier = briefs.self_review_tier(run.get("gear"), run.get("risk_json"))
    enforced = contract.of(run) != contract.LEGACY  # runs pinned to v3.1 keep ledger-less submissions (#421)
    has_ledger = _read_untracked_text(wt, briefs.LEDGER_FILE, briefs.LEDGER_MAX_CHARS) is not None
    if exempt and not has_ledger:
        _check_exemption(task, exempt, reason, tier)
    ledger_stop, ledger_fix, signaled = preflight.ledger_gate(
        con, run, task, d, wt, base, head, dep_bases, stop=[], fix=[], submission=True,
        require=enforced and not (exempt and not has_ledger))
    if ledger_stop or ledger_fix:
        details = "; ".join(ledger_stop + ledger_fix)
        category = "self-review-ledger-signaled" if signaled else "self-review-ledger"
        next_step = "run office preflight, apply its repairs, then office submit" if not ledger_stop else \
            "run office preflight, report the stop for the orchestrator, and stop"
        if enforced and not ledger_stop and not has_ledger:
            next_step = (f"write {briefs.LEDGER_FILE} (format in your brief's LEDGER lines) after your last commit with "
                         f"COMMIT {head}, then office submit"
                         + ('; a trivial or mechanical change may instead run: office submit --self-review-exempt '
                            'trivial|mechanical -- "<reason>"' if tier == "inline" else ""))
        raise Refused(category, f"{task['id']} self-review ledger refused submission: {details}",
                      scope=task["id"], preserved="your worktree (nothing was submitted)", next_step=next_step)
    receipt = _self_review_receipt(wt, task, run, head, tree, tier, enforced, exempt, reason,
                                   preflight.committed_changes(wt, base, head, dep_bases), preflight)
    commit = make_commit(wt, tree, head, f"office: {task['id']} submission\n\nrun {run['id'][:8]} task {task['id']}\n\n"
                         f"{paths.office_trailer(run['id'])}")
    from office import planfile, prs
    base = d["base_commit"]
    touched = paths.git(wt, "diff", "--no-renames", "--name-only", base, commit).split()
    dep_bases = [b for b in _dependency_bases(con, run, task, commit) if b != base]
    for b in dep_bases:
        # A file counts only if it differs from every base the task builds on,
        # so a dependency's own later files are not this task's changes.
        also = set(paths.git(wt, "diff", "--no-renames", "--name-only", b, commit).split())
        touched = [f for f in touched if f in also]
    if len(dep_bases) == 1 and gates._is_ancestor(run, base, dep_bases[0]):
        base = dep_bases[0]  # reviewers diff against the dependency revision it now contains
    outside = [f for f in touched if not planfile.path_in_scope(f, task["scope"])]
    if outside:
        with db.transaction(con):
            # A deterministic refusal: relaunching the same brief would repeat it.
            msg = f"{task['id']} changed files outside its scope: {', '.join(outside[:6])}"
            # Recorded now, not at worker exit or idle stall: a live worker sits in its pane after a refusal.
            # First, so a revoke or takeover since the checks above refuses before anything is written.
            _record_block(con, run, task, d, f"submit refused: {msg}", "submit_refused", {"files": outside[:20]})
            state.emit(con, run, "submit.refused", msg,
                       audience="runtime", task_id=task["id"], dispatch_id=d["id"], payload={"code": "outside-scope"})
            state.emit(con, run, "task.blocked", f"{task['id']} submit refused ({msg}); amend the scope "
                       f"(office amend {task['id']} --contract -- ...) or tell the worker to revert; work is preserved "
                       "in its worktree", task_id=task["id"], dispatch_id=d["id"])
            state.signal_orchestrator(con, run, source="submit refused", task_id=task["id"], dispatch_id=d["id"],
                                      reason=f"outside-scope: {msg}", next_step=f'office amend {task["id"]} --contract -- '
                                      f'"<add the file to SCOPE>", or office prompt {d["id"]} -- "revert <file>"')
        raise Refused("outside-scope", f"{task['id']} changed files outside its scope: {', '.join(outside[:6])}",
                      scope=task["id"], preserved="your worktree (nothing was submitted)",
                      next_step='revert those files, or run office submit --request-scope <path> -- "<reason>" '
                                "and wait (the orchestrator amends the scope)")
    pending = con.execute("SELECT id, target_version FROM deliveries WHERE run_id=? AND task_id=? AND status IN "
                          "('queued','delivered') ORDER BY target_version DESC LIMIT 1", (run["id"], task["id"])).fetchone()
    evidence = None if task["scope"] else _read_evidence(wt)
    if evidence and evidence[2] in _ingested_digests(con, run, task["id"]):
        with db.transaction(con):
            state.emit(con, run, "submit.refused", f"{task['id']} {briefs.EVIDENCE_FILE} repeats evidence already submitted",
                       audience="runtime", task_id=task["id"], dispatch_id=d["id"], payload={"code": "stale-evidence"})
        raise _stale_evidence(task)
    res = Result()
    with _evidence_commit(ledger) as staged, db.transaction(con):
        again = con.execute("SELECT * FROM revisions WHERE operation_id=?", (op_id,)).fetchone()
        if again:
            return _duplicate(con, run, dict(again))
        if dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is None:
            raise Refused("lease-lost", f"{task['id']} lease was revoked during submit", scope=task["id"])
        now_task = state.get_task(con, run["id"], task["id"])
        if now_task["current_dispatch_id"] != d["id"]:
            raise Refused("superseded-dispatch", f"{task['id']} now belongs to a newer dispatch", scope=task["id"])
        if now_task["status"] in ("paused", "blocked", "cancelled") and \
                not (self_blocked(now_task) and block_id(con, d["id"]) == seen_block):
            # Recorded after this submit looked: keep it; only the block this submit resolves may clear.
            raise Refused("task-paused", f"{task['id']} is {now_task['status']}: {now_task.get('pause_reason') or ''}",
                          scope=task["id"], preserved="your worktree", next_step="stop and wait; the orchestrator is resolving it")
        if evidence and evidence[2] in _ingested_digests(con, run, task["id"]):
            raise _stale_evidence(task)  # a concurrent submit ingested the same file first
        seq = con.execute("SELECT COUNT(*) FROM revisions WHERE run_id=?", (run["id"],)).fetchone()[0] + 1
        # revisions.id is a GLOBAL primary key shared by every run in runs.db; a bare
        # per-run "R{seq}" collides with the first revision of any earlier run.
        rev_id = f"R{seq}-{run['id'][:8]}"
        prev = task.get("current_revision_id")
        prev_row = con.execute("SELECT * FROM revisions WHERE id=?", (prev,)).fetchone() if prev else None
        changed = paths.git(wt, "diff", "--name-only", prev_row["commit_sha"] if prev_row else base, commit).split()
        status = "amendment_pending" if pending else "current"
        env_fp = sha256_obj({"office": run["office_version"], "policy": run["config_hash"]})
        con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, lease_id, fencing, commit_sha, tree_sha, "
                    "base_commit, requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, "
                    "supersedes, changed_json, created_at, self_review_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rev_id, run["id"], task["id"], seq, d["id"], d["lease_id"],
                     (dispatch_mod.live_lease(con, run["id"], d["lease_id"]) or {}).get("fencing"), commit, tree,
                     base, run["requirements_version"], run["plan_version"], applied, env_fp, op_id, status,
                     prev, dumps(changed), now_iso(), dumps(receipt) if receipt else None))
        paths.git(wt, "update-ref", f"refs/office/{run['id'][:8]}/{task['id']}/{rev_id}", commit)
        if evidence:
            dest = _stage_evidence(run, d, rev_id, evidence, staged)
            # The digest is the duplicate guard for every later submit of this task.
            state.record_evidence(con, run["id"], EVIDENCE_KIND, dest, task_id=task["id"], revision_id=rev_id,
                                  digest=evidence[2])
        if prs.enabled(run):
            prs.advance_branch(run, d, commit)
            prs.queue(con, run, task["id"], "revision", rev_id)
        dispatch_mod.renew_lease(con, d["lease_id"])
        if status == "amendment_pending":
            state.emit(con, run, "submit.amendment_pending", f"{task['id']} {rev_id} submitted under p{applied}; "
                       "amendment pending", audience=f"dispatch:{d['id']}", task_id=task["id"])
            res.add(f"rev {rev_id} captured | amendment pending: apply it, then office ack; not a failure")
            res.next = ("apply the delivered amendment, office ack <id>, rewrite the self-review ledger after your last "
                        "commit, then office submit")
            return res
        if prev:
            con.execute("UPDATE revisions SET status='superseded' WHERE id=? AND status='current'", (prev,))
            gates.stale_open_gates(con, run, task["id"], rev_id)
        state.update_task(con, run["id"], task["id"], current_revision_id=rev_id, status="submitted",
                           pause_reason=None)
        planned = gates.plan_for_revision(con, run, state.get_task(con, run["id"], task["id"]), rev_id, changed, d)
        state.emit(con, run, "submit", f"{task['id']} submitted {rev_id}", audience="runtime", task_id=task["id"],
                   payload={"self_review": receipt} if receipt else None)
    jobs.kick(con, run["id"])
    parts = [f"rev {rev_id} captured"] + ([f"supersedes {prev}"] if prev else []) + planned["summary"]
    res.add(" | ".join(parts))
    if receipt:
        res.add(describe_receipt(receipt))
    left_out = [f for f in left_out if f not in (briefs.EVIDENCE_FILE, briefs.LEDGER_FILE)]
    for label, names in (("untracked files outside", left_out), ("harness config edits outside", restored)):
        if names:
            shown = ", ".join(names[:4]) + (f" (+{len(names) - 4} more)" if len(names) > 4 else "")
            res.add(f"warning: left out of {rev_id} ({label} {task['id']} scope): {shown}")
    res.next = "you may stop; results will be delivered"
    res.data = {"revision": rev_id, "commit": commit, "gates": planned["gates"]}
    return res


EXEMPT_TYPES = ("trivial", "mechanical")  # declared by the executor; `empty` and `read-only` are derived


def describe_receipt(r: dict) -> str:
    """One line for a revision's self-review receipt, for submit output and review briefs."""
    if r.get("kind") == "exempt":
        why = f": {r['reason']}" if r.get("reason") else ""
        return f"self-review exempt ({r['type']}{why}); independent review still applies"
    return (f"self-review receipt: ledger {r['sha256'][:12]} bound to tree {r['tree'][:12]} "
            f"(tier {r['tier']}, round {r['round']}, {r['findings']} findings)")


def _check_exemption(task: dict, exempt: str, reason: str, tier: str) -> None:
    """A declared exemption is for trivial or mechanical work on a low-risk task only (the inline tier, #309)."""
    why = None
    if exempt not in EXEMPT_TYPES:
        why = f"--self-review-exempt takes {' or '.join(EXEMPT_TYPES)} (empty and read-only work is exempt on its own)"
    elif not reason.strip():
        why = 'an exemption needs a reason: office submit --self-review-exempt ' + exempt + ' -- "<reason>"'
    elif tier != "inline":
        why = (f"the {tier} self-review tier (set from this run's gear and blast radius) allows no exemption: "
               f"write {briefs.LEDGER_FILE}")
    if why:
        raise Refused("self-review-exemption", f"{task['id']} self-review exemption refused: {why}",
                      scope=task["id"], preserved="your worktree (nothing was submitted)",
                      next_step=f"write {briefs.LEDGER_FILE} after your last commit, then office submit")


def _self_review_receipt(wt: Path, task: dict, run: dict, head: str, tree: str, tier: str, enforced: bool,
                         exempt: str | None, reason: str, changed: list[str], preflight) -> dict | None:
    """What a revision records about its producer self-review (#421): the ledger's digest bound to the
    submitted tree, or a typed exemption. None only for an unenforced run that submitted no ledger.
    Never an approval: it does not touch the independent review that applies."""
    committed, pending = preflight._in_scope_changes(wt, task, changed)
    substantive = bool(committed or pending)
    text = _read_untracked_text(wt, briefs.LEDGER_FILE, briefs.LEDGER_MAX_CHARS)
    if text is not None:
        if enforced and pending:  # in-scope work newer than the HEAD the ledger names
            raise Refused("self-review-stale", f"{task['id']} has uncommitted work the self-review ledger does not "
                          f"cover: it names HEAD {head[:12]}, but the submitted tree differs from HEAD's",
                          scope=task["id"], preserved="your worktree (nothing was submitted)",
                          next_step=f"commit the work, review what changed, rewrite {briefs.LEDGER_FILE} with the new "
                                    "HEAD as COMMIT, then office submit")
        led = preflight.parse_ledger(text)[0]
        return {"kind": "ledger", "commit": head, "tree": tree, "tier": tier, "round": led["round"],
                "lenses": sorted(led["lenses"]), "findings": len(led["findings"]), "substantive": substantive,
                "sha256": sha256_bytes(text.encode("utf-8"))}
    if not enforced:
        return None
    if not task["scope"]:
        return {"kind": "exempt", "type": "read-only", "tree": tree, "tier": tier}
    if not substantive:
        return {"kind": "exempt", "type": "empty", "tree": tree, "tier": tier}
    return {"kind": "exempt", "type": exempt, "reason": reason.strip(), "tree": tree, "tier": tier}


def _executor_ledger(wt: Path) -> Path | None:
    """The executor's self-review ledger when it is a file (or link) that HEAD does not track."""
    path = wt / briefs.LEDGER_FILE
    if (path.is_file() or path.is_symlink()) and not paths.git(wt, "ls-tree", "HEAD", "--", briefs.LEDGER_FILE).strip():
        return path
    return None


@contextmanager
def _evidence_commit(ledger: Path | None = None):
    """Yields a list that `_stage_evidence` fills. The worktree file (and the executor's
    self-review `ledger`) is consumed only after the surrounding transaction commits;
    on any failure the staged copy is discarded and the worktree files are kept."""
    staged: list[tuple[Path, Path]] = []
    try:
        yield staged
    except BaseException:
        for _, dest in staged:
            dest.unlink(missing_ok=True)
        raise
    for src, _ in staged:
        src.unlink(missing_ok=True)  # consumed: the next submission must write its own
    if ledger:
        try:
            ledger.unlink(missing_ok=True)
        except OSError:
            pass  # the revision is recorded; a ledger that cannot be removed is overwritten by the next one


EVIDENCE_KIND = "executor_evidence"


def _read_untracked_text(wt: Path, name: str, limit: int) -> str | None:
    """Text of the file `name` an executor wrote at its worktree root, or None unless it is a regular,
    untracked, singly linked file. A symlink could point at a credential file, a hard link shares its
    inode, and a tracked file is repo content. At most `limit` + 1 characters come back."""
    src = wt / name
    if src.is_symlink() or not src.is_file():
        return None
    if paths.git(wt, "ls-files", "--", name).strip():
        return None
    try:
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            # A hard link to another file (a credential) shares its inode: ordinary files have one link.
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                return None
            raw = fh.read(limit * 4 + 1)
    except (OSError, ValueError):
        return None
    return raw.decode("utf-8", errors="replace")[:limit + 1]


def _read_evidence(wt: Path) -> tuple[Path, str, str] | None:
    """A scope-none task's evidence file as (path, reviewer text, sha256 of that text), or None.
    Freshness is not judged here: the launch moves any earlier file out of the
    worktree (dispatch._set_aside_evidence), and submit refuses content already ingested."""
    text = _read_untracked_text(wt, briefs.EVIDENCE_FILE, briefs.EVIDENCE_MAX_CHARS)
    if text is None:
        return None
    if len(text) > briefs.EVIDENCE_MAX_CHARS:
        text = text[:briefs.EVIDENCE_MAX_CHARS] + f"\n[evidence truncated at {briefs.EVIDENCE_MAX_CHARS} characters]\n"
    return wt / briefs.EVIDENCE_FILE, text, sha256_bytes(text.encode("utf-8"))


def _ingested_digests(con, run: dict, task_id: str) -> set[str]:
    """sha256 of every evidence text already ingested for this task, across all its dispatches."""
    digests = {r[0] for r in con.execute("SELECT sha256 FROM evidence WHERE run_id=? AND task_id=? AND kind=?",
                                         (run["id"], task_id, EVIDENCE_KIND)).fetchall() if r[0]}
    # Copies saved before digests were recorded have no evidence row: hash the saved file.
    for rev in con.execute("SELECT id, dispatch_id FROM revisions WHERE run_id=? AND task_id=? AND dispatch_id IS NOT NULL",
                           (run["id"], task_id)).fetchall():
        saved = briefs.evidence_path(run, rev["dispatch_id"], rev["id"])
        try:
            if saved.is_file() and not saved.is_symlink():
                digests.add(sha256_file(saved))
        except OSError:
            pass  # an unreadable old copy cannot be compared; its evidence row, if any, still is
    return digests


def _stale_evidence(task: dict) -> Refused:
    return Refused("stale-evidence", f"{briefs.EVIDENCE_FILE} has the same content as evidence already submitted for "
                   f"{task['id']}; it does not show work done for this submission",
                   scope=task["id"], preserved="your worktree (nothing was submitted)",
                   next_step=f"redo or reconfirm the external action (comment, issue edit), rewrite {briefs.EVIDENCE_FILE} "
                             "with what you found or did now (URLs, text, current state), then office submit")


def _stage_evidence(run: dict, d: dict, rev_id: str, evidence: tuple[Path, str, str], staged: list) -> Path:
    """Copy a scope-none task's evidence where the code reviewer's brief reads it."""
    src, text, _ = evidence
    dest = briefs.evidence_path(run, d["id"], rev_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged.append((src, dest))  # before the write, so a failed write still discards a partial copy
    dest.write_bytes(text.encode("utf-8"))
    return dest


def _duplicate(con, run: dict, rev: dict) -> Result:
    pending = con.execute("SELECT kind, status FROM gates WHERE revision_id=? AND status IN ('queued','running')",
                          (rev["id"],)).fetchall()
    note = "verification still running" if pending else f"status {rev['status']}"
    return Result(lines=[f"{rev['id']} already submitted | {note}"], next="no action",
                  data={"revision": rev["id"], "duplicate": True})
