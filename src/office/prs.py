"""Per-task GitHub PRs (3.2): every task branch is pushed and has a draft PR.

A root task's PR targets the repository's default branch; a task that depends
on another targets that task's branch, so GitHub shows the stack the plan
diagram shows. Executors push work in progress themselves; at submit the
runtime moves the branch to the reviewed revision and pushes it, so the PR
head is always what the gates judged. Verdicts are posted as one line (never
evidence), and a PR leaves draft when its task is accepted.

Everything here runs as outbox jobs (`pr_sync`): GitHub is never called inside
a transaction, and a GitHub failure is a notice, never a lifecycle failure.
PRs are off when the run is local, the repository has no `origin`, `gh` is
missing, or `office start --no-prs` was given; the 3.1 flow then applies.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from office import db, paths, plan_view, state
from office.util import dumps

MERGE_FLAGS = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}


# ------------------------------------------------------------------ settings

def _gh(args: list[str], cwd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _blast_radius(con, run: dict) -> str | None:
    frozen = state.current_requirements(con, run["id"])["frozen"]
    return frozen.get("blast_radius") or (run.get("risk") or {}).get("blast_radius")


def _detect(con, run: dict) -> dict:
    repo = run["repo_root"]
    if _blast_radius(con, run) == "local":
        return {"enabled": False, "reason": "blast radius is local"}
    if paths.git(Path(repo), "remote", "get-url", "origin", check=False) == "":
        return {"enabled": False, "reason": "no origin remote"}
    if shutil.which("gh") is None:
        return {"enabled": False, "reason": "gh is not installed"}
    try:
        proc = _gh(["repo", "view", "--json", "nameWithOwner,defaultBranchRef,mergeCommitAllowed,"
                    "squashMergeAllowed,rebaseMergeAllowed"], repo)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"enabled": False, "reason": f"gh repo view failed: {exc}"[:200]}
    if proc.returncode != 0:
        return {"enabled": False, "reason": f"gh repo view failed: {(proc.stderr or proc.stdout).strip()}"[:200]}
    info = json.loads(proc.stdout or "{}")
    method = next((m for m, key in (("merge", "mergeCommitAllowed"), ("squash", "squashMergeAllowed"),
                                    ("rebase", "rebaseMergeAllowed")) if info.get(key, m == "merge")), "merge")
    return {"enabled": True, "repo": info.get("nameWithOwner"),
            "base_branch": (info.get("defaultBranchRef") or {}).get("name") or "main", "merge_method": method}


def settings(con, run: dict) -> dict:
    """The run's PR settings, detected once (outside any transaction) and pinned."""
    run = state.get_run(con, run["id"])
    landing = dict(run.get("landing") or {})
    current = landing.get("prs") or {}
    if "enabled" in current:
        return current
    detected = {**_detect(con, run), **{k: v for k, v in current.items() if k != "enabled"}}
    with db.transaction(con):
        landing = dict(state.get_run(con, run["id"]).get("landing") or {})
        landing["prs"] = detected
        state.update_run(con, run["id"], landing=landing)
    return detected


def enabled(run: dict) -> bool:
    return bool(((run.get("landing") or {}).get("prs") or {}).get("enabled"))


# ------------------------------------------------------------------ stack shape

def parent(con, run: dict, task: dict) -> dict | None:
    """The task this one stacks on: its last dependency, as in the diagram."""
    deps = [d for d in task["depends"] if state.get_task(con, run["id"], d)]
    return state.get_task(con, run["id"], deps[-1]) if deps else None


def pr_base(con, run: dict, task: dict) -> str:
    s = (run.get("landing") or {}).get("prs") or {}
    up = parent(con, run, task)
    if up and (up.get("pr") or {}).get("branch") and not (up.get("pr") or {}).get("merged"):
        return up["pr"]["branch"]
    return s.get("base_branch") or "main"


def body(con, run: dict, task: dict, dispatch: dict | None) -> str:
    landing = run.get("landing") or {}
    up = parent(con, run, task)
    base = pr_base(con, run, task)
    stack = f"stacked on {up['id']}" + (f" (#{up['pr']['number']})" if (up.get("pr") or {}).get("number") else "") \
        if up and base != (landing.get("prs") or {}).get("base_branch") else f"base `{base}`"
    lines = [f"**{task['id']}: {task['title']}** (Office run `{run['id'][:8]}`)", "", f"Stack: {stack}"]
    disclosure = ((dispatch or {}).get("route") or {}).get("selection_disclosure")
    if disclosure:
        why = plan_view.short_why(disclosure)
        lines.append(f"Route: `{plan_view.route_label(disclosure)}`" + (f", why: {why}" if why else ""))
    if task["accept"]:
        lines += ["", "Accept:", *[f"- {a}" for a in task["accept"]]]
    if landing.get("issue"):
        lines += ["", f"Part of #{str(landing['issue']).rstrip('/').rsplit('/', 1)[-1]}"]
    lines += ["", f"<!-- office:pr run={run['id']} task={task['id']} -->"]
    return "\n".join(lines) + "\n"


def create_command(con, run: dict, task: dict, dispatch: dict, body_path: Path) -> str:
    title = f"{task['id']}: {task['title']}".replace('"', "'")
    return (f'gh pr create --draft --base {pr_base(con, run, task)} --head {dispatch["branch"]} '
            f'--title "{title}" --body-file {body_path}')


# ------------------------------------------------------------------ queueing (caller holds the tx)

def queue(con, run: dict, task_id: str, event: str, ref: str) -> None:
    if enabled(run):
        state.enqueue(con, run, "pr_sync", {"task_id": task_id, "event": event, "ref": ref},
                      dedup_key=f"pr_sync:{run['id'][:8]}:{task_id}:{event}:{ref}", max_attempts=2)


# ------------------------------------------------------------------ git side

def advance_branch(run: dict, dispatch: dict, commit: str) -> None:
    """Make the task branch head the submitted revision (its parent is the
    worker's HEAD, so this is a fast-forward) and sync the worker's index."""
    wt = Path(dispatch["worktree"])
    if paths.git(wt, "rev-parse", "HEAD") != commit:
        paths.git(wt, "reset", "-q", commit)


def push(run: dict, dispatch: dict, *, force: bool = False) -> tuple[bool, str]:
    args = ["git", "-C", dispatch["worktree"], "push", "-u", "origin", f"HEAD:refs/heads/{dispatch['branch']}"]
    if force:
        args.insert(4, "--force-with-lease")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


# ------------------------------------------------------------------ job

def _find(repo: str, branch: str) -> dict | None:
    proc = _gh(["pr", "list", "--head", branch, "--state", "open", "--json", "number,url,baseRefName,isDraft"], repo)
    if proc.returncode != 0:
        return None
    found = json.loads(proc.stdout or "[]")
    return found[0] if found else None


def _number(url: str) -> int | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return int(m.group(1)) if m else None


def ensure_pr(con, run: dict, task: dict, dispatch: dict) -> dict:
    """Find or open the task's draft PR and keep its base and body current."""
    repo = run["repo_root"]
    ddir = paths.run_dir(run["id"]) / "dispatches" / dispatch["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    body_path = ddir / "pr-body.md"
    body_path.write_text(body(con, run, task, dispatch), encoding="utf-8")
    base = pr_base(con, run, task)
    found = _find(repo, dispatch["branch"])
    if found is None:
        proc = _gh(["pr", "create", "--draft", "--base", base, "--head", dispatch["branch"],
                    "--title", f"{task['id']}: {task['title']}", "--body-file", str(body_path)], repo)
        if proc.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {(proc.stderr or proc.stdout).strip()}"[:300])
        url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        found = {"number": _number(url), "url": url, "baseRefName": base, "isDraft": True}
    else:
        args = ["pr", "edit", str(found["number"]), "--body-file", str(body_path)]
        if found.get("baseRefName") != base:
            args += ["--base", base]
        _gh(args, repo)
    pr = {**(task.get("pr") or {}), "number": found["number"], "url": found["url"], "base": base,
          "branch": dispatch["branch"]}
    with db.transaction(con):
        state.update_task(con, run["id"], task["id"], pr=pr)
    return pr


def _comment(run: dict, pr: dict, text: str) -> None:
    _gh(["pr", "comment", str(pr["number"]), "--body", text], run["repo_root"])


def _gate_line(con, run: dict, task: dict, rev_id: str) -> str:
    rows = con.execute("SELECT kind, status, verdict FROM gates WHERE run_id=? AND task_id=? AND revision_id=? "
                       "ORDER BY created_at", (run["id"], task["id"], rev_id)).fetchall()
    label = {"checks": "checks", "code_review": "code review", "visual": "ui review"}
    return ", ".join(f"{label.get(r['kind'], r['kind'])} {r['verdict'] or r['status']}" for r in rows) or "no gates"


def job_pr_sync(con, run: dict, job: dict) -> dict:
    p = job["payload"]
    task = state.get_task(con, run["id"], p["task_id"])
    if not enabled(run) or task is None:
        return {"skipped": "prs disabled"}
    try:
        return _sync(con, run, task, p["event"], p["ref"])
    except Exception as exc:  # GitHub trouble is a notice, never a lifecycle failure
        with db.transaction(con):
            state.emit(con, run, "pr.error", f"{task['id']} PR {p['event']}: {exc}"[:300], task_id=task["id"])
        return {"error": str(exc)[:300]}


def _sync(con, run: dict, task: dict, event: str, ref: str) -> dict:
    if event == "revision":
        rev = con.execute("SELECT * FROM revisions WHERE id=?", (ref,)).fetchone()
        dispatch = state.get_dispatch(con, rev["dispatch_id"])
        ok, err = push(run, dispatch)
        if not ok:
            raise RuntimeError(f"push of {dispatch['branch']} failed: {err}"[:300])
        pr = ensure_pr(con, run, task, dispatch)
        attempt = con.execute("SELECT COUNT(*) FROM revisions WHERE run_id=? AND task_id=?",
                              (run["id"], task["id"])).fetchone()[0]
        if attempt > 1:
            _comment(run, pr, f"office: revision {ref} pushed (attempt {attempt}, replaces attempt {attempt - 1})")
        return {"pr": pr["number"], "pushed": rev["commit_sha"]}
    pr = task.get("pr") or {}
    if not pr.get("number"):
        return {"skipped": "no PR yet"}
    if event == "verdict":
        gate = con.execute("SELECT * FROM gates WHERE id=?", (ref,)).fetchone()
        kind = {"checks": "checks", "code_review": "code review", "visual": "ui review"}.get(gate["kind"], gate["kind"])
        _comment(run, pr, f"office: {kind} {gate['verdict']} on {gate['revision_id']} | gates: "
                          f"{_gate_line(con, run, task, gate['revision_id'])}")
        return {"commented": gate["verdict"]}
    if event == "accepted":
        _gh(["pr", "ready", str(pr["number"])], run["repo_root"])
        _comment(run, pr, f"office: {task['id']} accepted on {ref}; ready for review")
        return {"ready": pr["number"]}
    return {"skipped": event}
