"""Recoverable, ownership-fenced cleanup of Office worktrees.

No directory name alone grants deletion authority. Worktree ownership must
come from a run record, its managed root, or a recorded checkout, and Git must
confirm registration and repository identity. Remote recovery is checked at
execution time, not inferred from a successful land command.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from office import db, paths, portable, state
from office.result import Result
from office.state import Refused
from office.util import now_iso, pid_alive, short


def _git(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True,
                          timeout=45, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


@contextlib.contextmanager
def _lock(run: dict):
    root = paths.state_home() / "cleanup-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / (run["id"] + ".lock")).open("a") as fd:
        try:
            portable.lock_exclusive(fd, blocking=False)
        except OSError as exc:
            raise Refused("cleanup-running", f"cleanup for {short(run['id'])} is running",
                          preserved="all worktrees", next_step="retry office close --run " + short(run["id"])) from exc
        try:
            yield
        finally:
            portable.unlock(fd)


def _registered(repo: Path) -> dict[Path, dict]:
    proc = _git(repo, "worktree", "list", "--porcelain")
    if proc.returncode:
        raise OSError("cannot list Git worktrees: " + proc.stderr.strip()[:160])
    result = {}
    for stanza in proc.stdout.strip().split("\n\n"):
        info = {}
        for line in stanza.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                info["path"] = Path(value).absolute()
            elif key in ("locked", "prunable", "detached", "branch", "HEAD"):
                info[key] = value or True
        if "path" in info:
            result[info.pop("path")] = info
    return result


def _candidates(con, run: dict, registered: dict[Path, dict]) -> list[Path]:
    """Discover current, recorded legacy, convergence and deployment locations."""
    root = paths.worktrees_dir() / run["id"][:8]
    found = set()
    if root.is_dir():
        found.update(p.absolute() for p in root.iterdir()
                     if p.is_dir() and ((p / ".git").exists() or p.absolute() in registered))
    # Older Office versions wrote dispatch worktrees outside the modern root.
    for row in con.execute("SELECT worktree FROM dispatches WHERE run_id=? AND worktree IS NOT NULL", (run["id"],)):
        if row["worktree"]:
            candidate = Path(row["worktree"]).expanduser().absolute()
            if candidate.is_dir() or candidate in registered:
                found.add(candidate)
    from office import deploy_artifacts
    found.update(p.absolute() for p in deploy_artifacts.checkouts(run))
    # Detached review/check/convergence checkouts live beside deploy ones
    # under the run directory; a crash before their finally-removal leaves
    # them registered there. Plain non-worktree debris is not Office's to move.
    checkouts = paths.run_dir(run["id"]) / "checkouts"
    if checkouts.is_dir() and checkouts.resolve().is_relative_to(paths.state_home()):
        found.update(p.absolute() for p in checkouts.iterdir()
                     if p.is_dir() and not p.name.startswith(".")
                     and ((p / ".git").exists() or p.absolute() in registered))
    # Git still registers paths whose .git file or original directory is gone.
    found.update(p for p in registered if p.is_relative_to(root.absolute()))
    return sorted(found)


def _size(directory: Path) -> int:
    total = 0
    if directory.exists():
        for root, _dirs, files in os.walk(directory, followlinks=False):
            for name in files:
                try:
                    total += (Path(root) / name).lstat().st_size
                except OSError:
                    pass
    return total


def _remote_tips(repo: Path, *, refresh: bool = False) -> list[str]:
    """Prove live origin tips by SHA without mutating a dry-run checkout.

    Apply may fetch object data; previews only inspect refs and local objects.
    A remote tip we have not fetched does not count as recovery evidence.
    """
    if refresh and _git(repo, "fetch", "-q", "origin").returncode:
        return []
    live = _git(repo, "ls-remote", "--heads", "origin")
    if live.returncode:
        return []
    tips = []
    for line in live.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/heads/"):
            continue
        sha = parts[0]
        if _git(repo, "cat-file", "-e", sha + "^{commit}").returncode == 0:
            tips.append(sha)
    return tips


def _has_online_counterpart(repo: Path, head: str, tips: list[str]) -> bool:
    # An identical tree (such as a squash merge) does NOT preserve locally
    # unique commit identity or history. Require the actual commit to exist
    # in reachable online history. False negatives preserve data.
    return any(_git(repo, "merge-base", "--is-ancestor", head, tip).returncode == 0
               for tip in tips)


def _herdr_blocker(con, run: dict, wt: Path) -> str | None:
    rows = list(con.execute(
        "SELECT id, pane_id, pid, status FROM dispatches WHERE run_id=? AND worktree=?",
        (run["id"], str(wt))))
    # A completed dispatch's supervisor PID may be reused (or be a shared
    # fixture launcher). Only an unfinished/cancelled dispatch can still own a
    # writer; terminal-accepted dispatches have passed the lifecycle gate.
    if any(r["pid"] and r["status"] in ("launching", "running", "cancelled")
           and pid_alive(r["pid"]) for r in rows):
        return "live Office agent process"
    panes = [r["pane_id"] for r in rows if r["pane_id"]]
    if panes and not shutil.which("herdr"):
        # No API to prove that a retained pane is no longer attached.
        return "Herdr cannot be queried for recorded pane"
    for pane in panes:
        try:
            p = subprocess.run(["herdr", "pane", "get", pane], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return f"Herdr pane {pane} liveness unknown"

        def answer(text: str) -> dict | None:
            start = text.find("{")
            try:
                payload = json.loads(text[start:]) if start >= 0 else None
            except ValueError:
                return None
            return payload if isinstance(payload, dict) else None

        # A present pane answers on stdout, a missing one on stderr. Only
        # herdr's structured not-found answer proves detachment; a busy,
        # unreachable or unexpectedly formatted pane may still hold open file
        # handles, regardless of its current shell cwd.
        payload = answer(p.stdout) or answer(p.stderr)
        error = payload.get("error") if isinstance(payload, dict) else None
        if not (isinstance(error, dict) and error.get("code") == "pane_not_found"):
            return f"Herdr pane {pane} still present or unverified"
    return None


def _safe_candidate(con, run: dict, wt: Path, registered: dict[Path, dict],
                    tips: list[str]) -> str | None:
    repo = Path(run["repo_root"]).absolute()
    root = (paths.worktrees_dir() / run["id"][:8]).absolute()
    state_dir = Path(run.get("state_dir") or paths.run_dir(run["id"])).absolute()
    runtime_root = paths.runtimes_dir().absolute()
    # A recorded dispatch path outside the current run root is accepted only
    # when the Git registration proves it shares the recorded repository.
    if wt.is_symlink() or wt == repo or wt == paths.primary_checkout(Path(run["git_common_dir"])).absolute():
        return "primary checkout or symlink"
    if wt == runtime_root or wt.is_relative_to(runtime_root):
        return "installed/pinned runtime"
    if not (wt.is_relative_to(root) or wt.is_relative_to(state_dir) or con.execute(
        "SELECT 1 FROM dispatches WHERE run_id=? AND worktree=?", (run["id"], str(wt))).fetchone()):
        return "no recorded Office ownership"
    if wt not in registered:
        return "not registered as a Git worktree" if wt.exists() else "already absent"
    info = registered[wt]
    if "locked" in info:
        return "Git worktree is locked"
    if "prunable" in info:
        return "Git worktree registration is prunable; requires repair"
    identity = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if identity.returncode or Path(identity.stdout.strip()).resolve() != Path(run["git_common_dir"]).resolve():
        return "Git repository identity mismatch"
    if con.execute("SELECT 1 FROM dispatches d JOIN runs r ON r.id=d.run_id "
                   "WHERE d.worktree=? AND r.id<>? AND r.phase NOT IN ('closed','abandoned') LIMIT 1",
                   (str(wt), run["id"])).fetchone():
        return "checkout is in use by a different active run"
    if con.execute("SELECT 1 FROM runs WHERE repo_root=? AND id<>? "
                   "AND phase NOT IN ('closed','abandoned') LIMIT 1",
                   (str(wt), run["id"])).fetchone():
        return "checkout is a different active run's repository"
    if (blocker := _herdr_blocker(con, run, wt)):
        return blocker
    staged = _git(wt, "diff", "--quiet", "--cached", "HEAD", "--")
    unstaged = _git(wt, "diff", "--quiet", "HEAD", "--")
    if staged.returncode or unstaged.returncode:
        return "local tracked edits not archived (preserved in worktree)"
    head = _git(wt, "rev-parse", "HEAD")
    if head.returncode or not _has_online_counterpart(repo, head.stdout.strip(), tips):
        return "commit not proven recoverable from current online origin"
    return None


def _record(con, run: dict, status: str, report: dict) -> None:
    with db.transaction(con):
        con.execute("INSERT INTO cleanup_runs(run_id, status, receipt_json, updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                    "receipt_json=excluded.receipt_json, updated_at=excluded.updated_at",
                    (run["id"], status, json.dumps(report, sort_keys=True), now_iso()))


def _eligible(con, run: dict) -> str | None:
    if not state.is_terminal(run):
        return f"run is {run['phase']} and resumable"
    if run.get("pruned_at") and run.get("prune_status") == "complete":
        return "run was already pruned"
    if not run.get("terminal_at"):
        return "terminal transition has no recorded timestamp"
    if run["phase"] == "closed" and not run.get("archive_digest"):
        return "close archive receipt is missing; retain until recovery"
    landing = run.get("landing") or {}
    if run["phase"] == "closed" and (landing.get("status") not in ("landed", "landed-externally")
                                         or landing.get("handoff") and landing.get("status") != "landed-externally"):
        return "no verified merge; handoff worktrees must be retained"
    return None


def preview(con, run: dict, *, inspect: bool = False) -> dict:
    why = _eligible(con, run)
    worktrees = []
    try:
        registered = _registered(Path(run["repo_root"])) if not why else {}
        worktrees = _candidates(con, run, registered) if not why else []
    except (OSError, ValueError) as exc:
        why = str(exc)
    landing = run.get("landing") or {}
    details = []
    if inspect and not why and worktrees:
        tips = _remote_tips(Path(run["repo_root"]))
        for wt in worktrees:
            try:
                blocker = _safe_candidate(con, run, wt, registered, tips)
                modified = (datetime.fromtimestamp(wt.stat().st_mtime, tz=timezone.utc).isoformat()
                            if wt.exists() else None)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                blocker, modified = str(exc), None
            details.append({"path": str(wt), "modified_at": modified,
                            "bytes": _size(wt), "blocker": blocker})
    return {
        "run_id": run["id"], "repo": run.get("repo_root"), "office_version": run.get("office_version"),
        "phase": run["phase"], "issue": landing.get("issue"),
        "pr": landing.get("pr") or landing.get("handoff"),
        "landing": landing.get("detail") or landing.get("status"),
        "terminal_at": run.get("terminal_at"),
        "worktrees": [str(p) for p in worktrees], "bytes": sum(_size(p) for p in worktrees),
        "details": details, "blocker": why,
    }


def sweep(con, run: dict, *, override_decline: bool = False) -> dict:
    """One run only; persist independently of terminal lifecycle state."""
    with _lock(run):
        current = state.get_run(con, run["id"])
        receipt = con.execute("SELECT status FROM cleanup_runs WHERE run_id=?", (run["id"],)).fetchone()
        if receipt and receipt["status"] == "declined" and not override_decline:
            return {"removed": [], "kept": [{"path": "", "reason": "previously declined; use "
                    "office cleanup --run <id> --apply to opt in"}],
                    "branches": [], "branches_kept": [], "bytes": 0}
        reason = _eligible(con, current)
        if reason:
            report = {"removed": [], "kept": [{"path": "", "reason": reason}],
                      "branches": [], "branches_kept": [], "bytes": 0}
            _record(con, current, "deferred", report)
            return report
        _record(con, current, "in_progress", {})
        removed, kept, branch_deleted, branch_kept, bytes_reclaimed = [], [], [], [], 0
        repo = Path(current["repo_root"])
        try:
            registered = _registered(repo)
            tips = _remote_tips(repo, refresh=True)
            candidates = _candidates(con, current, registered)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            candidates = []
            kept.append({"path": str(repo), "reason": f"discovery failed: {exc}"})
            registered, tips = {}, []
        for wt in candidates:
            try:
                reason = _safe_candidate(con, current, wt, registered, tips)
                if reason == "already absent":
                    continue
                if reason:
                    kept.append({"path": str(wt), "reason": reason})
                    continue
                from office import deploy_artifacts
                # Capture and verify all nontracked and ignored files, including .env,
                # before risking any destructive Git worktree operation.
                backup = deploy_artifacts.preserve_or_refuse(current, wt)
                # Re-check immediately: writers, panes or commits could change
                # while the backup was being prepared.
                recent = _registered(repo)
                reason = _safe_candidate(con, current, wt, recent, _remote_tips(repo))
                if reason:
                    kept.append({"path": str(wt), "reason": "changed since archive: " + reason})
                    continue
                # A post-archive untracked/ignored change must not go unnoticed,
                # including one that appeared after an initially empty inventory.
                after = deploy_artifacts.preserve_or_refuse(current, wt)
                if after != backup:
                    kept.append({"path": str(wt), "reason": "local evidence changed since archive"})
                    continue
                size = _size(wt)
                proc = _git(repo, "worktree", "remove", "--force", str(wt))
                if proc.returncode or wt.exists():
                    kept.append({"path": str(wt), "reason": "Git refused removal: " + proc.stderr.strip()[:160]})
                else:
                    removed.append(str(wt))
                    bytes_reclaimed += size
            except (OSError, ValueError, Refused, subprocess.SubprocessError) as exc:
                kept.append({"path": str(wt), "reason": str(exc)[:240]})
            finally:
                # A crash after deleting some paths does not erase which ones
                # succeeded: the next invocation resumes from Git registration.
                _record(con, current, "in_progress", {"removed": removed,
                        "kept": kept, "bytes": bytes_reclaimed})
        if removed:
            _git(repo, "worktree", "prune")
        if not kept:
            prefix = f"refs/heads/office/{current['id'][:8]}/"
            refs = _git(repo, "for-each-ref", "--format=%(refname)", prefix)
            if refs.returncode == 0:
                for ref in refs.stdout.splitlines():
                    name = ref.removeprefix("refs/heads/")
                    proc = _git(repo, "branch", "-d", name)
                    (branch_deleted if proc.returncode == 0 else branch_kept).append(name)
        report = {"removed": removed, "kept": kept, "branches": branch_deleted,
                  "branches_kept": branch_kept, "bytes": bytes_reclaimed}
        _record(con, current, "incomplete" if kept else "complete", report)
        return report


def older(con, *, skip_run: str | None = None, repo_common: str | None = None,
          inspect: bool = False) -> list[dict]:
    out = []
    for row in con.execute("SELECT id FROM runs WHERE office_version IS NOT NULL AND "
                           "phase IN ('closed','abandoned') AND (pruned_at IS NULL OR prune_status!='complete') "
                           "ORDER BY terminal_at DESC"):
        if row["id"] == skip_run:
            continue
        run = state.get_run(con, row["id"])
        # Explicitly declined runs remain discoverable by --run, but are not
        # nagged about on every later successful close.
        receipt = con.execute("SELECT status FROM cleanup_runs WHERE run_id=?", (run["id"],)).fetchone()
        if receipt and receipt["status"] == "declined":
            continue
        if repo_common and run.get("git_common_dir") != repo_common:
            continue
        item = preview(con, run, inspect=inspect)
        if item["worktrees"]:
            out.append(item)
    return out


def offer(con, run: dict) -> list[str]:
    candidates = older(con, skip_run=run["id"], repo_common=run.get("git_common_dir"))
    if not candidates:
        return []
    amount = sum(item["bytes"] for item in candidates)
    return [f"optional: {len(candidates)} older finished run(s) have {sum(len(c['worktrees']) for c in candidates)} "
            f"recorded worktree(s) (~{amount/1e9:.2f} GB); preview with office cleanup --older "
            "(opt in per run; no older worktrees were deleted)"]


def command(con, *, run_id: str | None = None, apply: bool = False, decline: bool = False) -> Result:
    if not run_id:
        items = older(con, inspect=True)
        res = Result(lines=[f"older cleanup candidates: {len(items)} (preview only)"])
        for item in items:
            res.add(f"{short(item['run_id'])} {item['repo']} created {item['office_version']} "
                    f"{item['phase']} at {item['terminal_at']}; {len(item['worktrees'])} worktree(s), "
                    f"~{item['bytes']/1e9:.3f} GB" +
                    (f"; blocked: {item['blocker']}" if item["blocker"] else ""))
            for detail in item["details"]:
                res.add(f"  {detail['path']} ({detail['modified_at']}): " +
                        (f"kept — {detail['blocker']}" if detail["blocker"] else "candidate; evidence verified"))
        res.next = "office cleanup --run <id> --apply to opt in to one run" if items else None
        res.data = {"candidates": items, "dry_run": True}
        return res
    run = state.find_run(con, run_id)
    if run is None:
        raise Refused("unknown-run", f"run {run_id} not found")
    if decline:
        _record(con, run, "declined", {"action": "operator declined older worktree cleanup"})
        return Result(lines=[f"{short(run['id'])} cleanup declined; worktrees preserved, future offers suppressed"],
                      next=f"office cleanup --run {short(run['id'])} to preview again")
    if not apply:
        info = preview(con, run, inspect=True)
        res = Result(lines=[f"{short(run['id'])} cleanup preview: {len(info['worktrees'])} worktrees, "
                            f"~{info['bytes']/1e9:.3f} GB" +
                            (f"; blocked: {info['blocker']}" if info["blocker"] else "")])
        for detail in info["details"]:
            res.add(f"  {detail['path']} ({detail['modified_at']}): " +
                    (f"kept — {detail['blocker']}" if detail["blocker"] else "candidate; evidence verified"))
        res.data = {"dry_run": True, "candidate": info}
        res.next = f"office cleanup --run {short(run['id'])} --apply" if not info["blocker"] and info["worktrees"] else None
        return res
    report = sweep(con, run, override_decline=True)
    res = Result(lines=[f"{short(run['id'])} cleanup: removed {len(report['removed'])}, "
                        f"retained {len(report['kept'])}; reclaimed ~{report['bytes']/1e9:.3f} GB"])
    for item in report["kept"]:
        res.add(f"kept {item['path']}: {item['reason']}")
    res.data = {"dry_run": False, "cleanup": report}
    return res
