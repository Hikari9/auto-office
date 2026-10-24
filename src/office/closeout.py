"""Closeout after `office close`: the /cleanup steps the runtime owns.

Once a run is closed on a real merge (landed or landed-externally):
  1. sync the local base branch to origin (fast-forward only; a dirty checkout
     of it is left alone and the user is asked),
  2. remove the worktrees and local branches Office created for this run
     (never anything else), then `git worktree prune`.
A handoff close marks the PR ready and keeps everything: the user merges.
Nothing here commits leftovers; uncommitted work stays a reported defect.
Every `office close` path ends with one `office close done — <summary>` line.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from office import paths

DOCS_STEP = ("update the docs this repo already keeps (CHANGELOG, README) for this change and make the PR body "
             "describe it accurately, then ")
_DOC = re.compile(r"(^|/)(CHANGELOG|README)[^/]*$", re.IGNORECASE)
_PR_REF = re.compile(r"^(https://github\.com/[^/]+/[^/]+/pull/\d+|#?\d+)$")


def done(summary: str) -> str:
    return f"office close done — {summary}"


def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def base_branch(run: dict, landing: dict) -> str:
    """The branch the work merged into: GitHub's answer, the pinned PR base, else main."""
    named = landing.get("base") or ((run.get("landing") or {}).get("prs") or {}).get("base_branch")
    if named:
        return named
    target = landing.get("target") or ""
    return target.split("/", 1)[1] if target.startswith("origin/") else (target or "main")


def sync_base(repo: Path, base: str) -> dict:
    """Fast-forward local `base` to origin/`base`. Returns {line, synced, ask}."""
    if not paths.git(repo, "remote", "get-url", "origin", check=False):
        return {"line": f"base sync skipped: no origin remote for {base}", "synced": False, "ask": None}
    fetch = _git(repo, "fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    remote = paths.git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}", check=False)
    if fetch.returncode != 0 or not remote:
        return {"line": f"base sync skipped: git fetch origin {base} failed: {fetch.stderr.strip()[:160]}",
                "synced": False, "ask": None}
    local = paths.git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}", check=False)
    if not local:
        return {"line": f"base sync skipped: no local {base} branch", "synced": False, "ask": None}
    if local == remote:
        return {"line": f"base {base} already at origin/{base} ({remote[:12]})", "synced": True, "ask": None}
    if _git(repo, "merge-base", "--is-ancestor", local, remote).returncode != 0:
        return {"line": f"base sync skipped: local {base} has commits origin/{base} lacks; not fast-forwardable",
                "synced": False, "ask": None}
    from office import integration
    holder = integration.branch_holder(repo, base)
    if holder is None:
        moved = _git(repo, "update-ref", f"refs/heads/{base}", remote, local)
    else:
        if paths.git(holder, "status", "--porcelain", "--untracked-files=no", check=False):
            return {"line": f"base sync skipped: {base} is checked out in {holder} with uncommitted changes",
                    "synced": False,
                    "ask": f"ask the user (native question tool) whether to fast-forward {base} in {holder}, which has "
                           f"uncommitted changes (git -C {holder} merge --ff-only origin/{base}), or leave it"}
        moved = _git(holder, "merge", "--ff-only", "-q", f"origin/{base}")
    now = paths.git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}", check=False)
    if moved.returncode != 0 or now != remote:
        return {"line": f"base sync failed: {base} is {now[:12]}, origin/{base} is {remote[:12]}: "
                        f"{moved.stderr.strip()[:160]}", "synced": False, "ask": None}
    return {"line": f"base {base} fast-forwarded {local[:12]} -> {remote[:12]}; local {base} == origin/{base}",
            "synced": True, "ask": None}


def _office_worktrees(run: dict) -> list[Path]:
    root = paths.worktrees_dir() / run["id"][:8]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists()) if root.is_dir() else []


def remove_worktrees(con, run: dict) -> dict:
    """One runtime cleanup engine for normal, interrupted and older run close."""
    from office import cleanup
    return cleanup.sweep(con, run)


def docs_warning(repo: Path, base_sha: str | None, commit: str | None) -> str | None:
    """Non-blocking: the diff touches code but no CHANGELOG/README the repo keeps."""
    if not base_sha or not commit:
        return None
    changed = paths.git(repo, "diff", "--name-only", base_sha, commit, check=False).split("\n")
    changed = [c for c in changed if c]
    kept = [f for f in paths.git(repo, "ls-tree", "-r", "--name-only", base_sha, check=False).split("\n") if _DOC.search(f)]
    if not kept or not changed or any(_DOC.search(c) for c in changed):
        return None
    if all(c.endswith((".md", ".rst", ".txt")) or c.startswith("docs/") for c in changed):
        return None
    return (f"warning: the diff changes code but none of {', '.join(kept[:3])} — update the docs this repo keeps "
            "if the change warrants it (non-blocking)")


def _pr_number(ref: str) -> str:
    return ref.strip().rstrip("/").rsplit("/", 1)[-1].lstrip("#")


def mark_ready(repo: str, ref: str, owned: set[str] | frozenset[str] = frozenset()) -> str | None:
    """Handoff: take an Office-opened PR out of draft. Best effort; returns a note.

    A PR Office did not open (not among `owned` PR numbers) keeps its draft
    state: that decision belongs to the user."""
    if not _PR_REF.match(ref.strip()):
        return None
    if _pr_number(ref) not in owned:
        return f"{ref} not opened by Office: draft state left as is"
    try:
        proc = subprocess.run(["gh", "pr", "ready", ref.strip().lstrip("#")], cwd=repo, capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not mark {ref} ready: {exc}"[:200]
    if proc.returncode != 0 and "already" not in (proc.stderr + proc.stdout).lower():
        return f"could not mark {ref} ready: {(proc.stderr or proc.stdout).strip()[:160]}"
    return f"{ref} marked ready for review"


def finish(con, run: dict, landing: dict, *, pr: str | None = None) -> tuple[list[str], str | None, str]:
    """After a merged close: sync base, remove run worktrees. Returns (lines, ask, summary)."""
    repo = Path(run["repo_root"])
    base = base_branch(run, landing)
    sync = sync_base(repo, base)
    cleaned = remove_worktrees(con, run)
    lines = [sync["line"]]
    from office import deploy_artifacts
    for archive in deploy_artifacts.receipt(run):
        lines.append(f"rollback output preserved: {archive['archive']} ({len(archive['files'])} files; manifest.json)")
    if cleaned["removed"] or cleaned["branches"]:
        lines.append(f"removed {len(cleaned['removed'])} run worktree(s), deleted {len(cleaned['branches'])} branch(es); "
                     "git worktree prune")
    for item in cleaned["kept"]:
        lines.append(f"kept worktree {item['path']} ({item['reason']})")
    if cleaned["branches_kept"]:
        lines.append(f"kept branch(es) git branch -d refused (not merged locally): {', '.join(cleaned['branches_kept'][:4])}")
    what = f"{pr} merged" if pr else "merged"
    base_part = (f"{base} synced" if sync["synced"] else f"{base} sync skipped")
    summary = f"{what}, {base_part}, {len(cleaned['removed'])} worktree(s) removed"
    if cleaned["kept"]:
        summary += f", {len(cleaned['kept'])} kept"
    if cleaned["branches_kept"]:
        summary += f", {len(cleaned['branches_kept'])} branch(es) kept"
    summary += f", ~{cleaned['bytes']/1e9:.3f} GB reclaimed"
    lines.append(f"cleanup receipt: removed {len(cleaned['removed'])}, retained {len(cleaned['kept'])}, "
                 f"~{cleaned['bytes']/1e9:.3f} GB reclaimed; retry office close --run {run['id'][:8]} if incomplete")
    return lines, sync["ask"], summary
