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

from office import db, paths, state
from office.result import Result
from office.state import Refused
from office.util import now_iso, pid_alive, short


def _git(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True,
                          timeout=45, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


@contextlib.contextmanager
def _lock(run: dict):
    import fcntl
    root = paths.state_home() / "cleanup-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / (run["id"] + ".lock")).open("a") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Refused("cleanup-running", f"cleanup for {short(run['id'])} is running",
                          preserved="all worktrees", next_step="retry office close --run " + short(run["id"])) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


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


def _base_ref(repo: Path) -> str | None:
    """The default branch's remote-tracking ref (origin/HEAD, else main, else master)."""
    head = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    names = [head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else []
    for name in names + ["refs/remotes/origin/main", "refs/remotes/origin/master"]:
        if _git(repo, "rev-parse", "--verify", "--quiet", name + "^{commit}").returncode == 0:
            return name
    return None


def _live_base(repo: Path, tips: list[str]) -> str | None:
    """The default branch ref, only when its local SHA is a live origin tip.

    A stale or unverifiable remote-tracking ref proves nothing about what the
    online default branch holds now, so content proofs need a live tip.
    """
    base = _base_ref(repo)
    if not base:
        return None
    sha = _git(repo, "rev-parse", base + "^{commit}")
    return base if sha.returncode == 0 and sha.stdout.strip() in tips else None


def _patch_ids(repo: Path, patch_text: str) -> list[str]:
    if not patch_text.strip():
        return []
    proc = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"], input=patch_text,
                          capture_output=True, text=True, timeout=120)
    return [line.split()[0] for line in proc.stdout.splitlines() if line.split()] if proc.returncode == 0 else []


BASE_SCAN = 2000  # default-branch commits since the merge-base searched for a content match


_BASE_IDS: dict[tuple[str, str, str], set[str]] = {}  # (repo, merge-base, base tip) -> patch ids, per process


def _merged_by_content(repo: Path, head: str, base: str) -> str | None:
    """Squash-aware: HEAD's work is on `base` by content or by patch."""
    mb = _git(repo, "merge-base", base, head)
    if mb.returncode or not mb.stdout.strip():
        return None
    mb = mb.stdout.strip()
    commits = _git(repo, "rev-list", "--no-merges", f"{mb}..{head}").stdout.split()
    if not commits:
        # Only merge commits beyond the base (a composed convergence or
        # integration branch): merged when merging it into base changes nothing.
        # merge-tree --write-tree writes tree objects: send them to a throwaway
        # object directory so a dry run (or any proof) never touches the repo.
        import tempfile
        objects = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects").stdout.strip()
        with tempfile.TemporaryDirectory(prefix="office-gc-") as scratch:
            tree = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", base, head],
                                  capture_output=True, text=True, timeout=45,
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OBJECT_DIRECTORY": scratch,
                                       "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects})
        base_tree = _git(repo, "rev-parse", base + "^{tree}").stdout.strip()
        if tree.returncode == 0 and tree.stdout.split()[:1] == [base_tree]:
            return "merging it into the default branch changes nothing"
        return None
    # Every path HEAD changed holds HEAD's exact content on the default branch
    # tip: the work is there however it was squashed. Nothing would be lost.
    touched = _git(repo, "diff", "--name-only", "--no-renames", "-z", mb, head).stdout.split("\0")
    touched = [p for p in touched if p]
    if touched and len(touched) <= 5000 and \
            _git(repo, "diff", "--quiet", "--no-renames", head, base, "--", *touched).returncode == 0:
        return "its changed files match the default branch"
    tip = _git(repo, "rev-parse", base + "^{commit}").stdout.strip()
    key = (str(repo), mb, tip)
    if key not in _BASE_IDS:
        base_log = _git(repo, "log", "-p", "--no-merges", f"--max-count={BASE_SCAN}", f"{mb}..{tip}")
        _BASE_IDS[key] = set(_patch_ids(repo, base_log.stdout)) if base_log.returncode == 0 else set()
    base_ids = _BASE_IDS[key]
    squash = _patch_ids(repo, _git(repo, "diff", mb, head).stdout)
    if squash and squash[0] in base_ids:
        return "squash-merged into the default branch (patch match)"
    # Merge commits can carry conflict-resolution content no single commit
    # patch shows; the per-commit proof is only for linear branches.
    merges = _git(repo, "rev-list", "--merges", f"{mb}..{head}").stdout.split()
    if not merges:
        each = _patch_ids(repo, _git(repo, "log", "-p", "--no-merges", f"{mb}..{head}").stdout)
        if base_ids and len(each) == len(commits) and all(pid in base_ids for pid in each):
            return "every commit's patch is on the default branch"
    return None


def _pr_merged(repo: Path, branch: str | None, head: str, base_branch: str | None) -> bool:
    """GitHub says a PR from this exact commit MERGED into the default branch. Any failure is unproven."""
    if not branch or not base_branch or not shutil.which("gh"):
        return False
    try:
        proc = subprocess.run(["gh", "pr", "list", "--head", branch, "--base", base_branch, "--state", "merged",
                               "--json", "number,headRefOid,baseRefName", "--limit", "5"],
                              cwd=str(repo), capture_output=True, text=True, timeout=20)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return isinstance(data, list) and any(isinstance(pr, dict) and pr.get("headRefOid") == head
                                          and pr.get("baseRefName") == base_branch for pr in data)


def merge_proof(repo: Path, head: str, tips: list[str], base: str | None, branch: str | None = None,
                *, landed: bool = False) -> str | None:
    """Why HEAD's work is merged (or, for a landed run, superseded and online), else None.

    Merged means on the live default branch: by ancestry, by patch or title
    (squash merges), or a merged PR whose head is this commit. A landed run's
    other commits (integration, review and check checkouts) are superseded by
    the landing and removable once the commit itself is reachable online.
    """
    if base:
        tip = _git(repo, "rev-parse", base + "^{commit}").stdout.strip()
        if tip and _git(repo, "merge-base", "--is-ancestor", head, tip).returncode == 0:
            return "reachable from the default branch"
    if landed and _has_online_counterpart(repo, head, tips):
        return "superseded by the landing; commit reachable online"
    if base and (why := _merged_by_content(repo, head, base)):
        return why
    base_branch = base.removeprefix("refs/remotes/origin/") if base else None
    if tips and _pr_merged(repo, branch.removeprefix("refs/heads/") if branch else None, head, base_branch):
        return "its PR merged on GitHub"
    return None


_PANES: list = [0.0, None]  # [monotonic time, answer]: one herdr call per few seconds, not per worktree


def _live_pane_cwds() -> set[Path] | None:
    """Every live Herdr pane's cwd; None when Herdr is installed but cannot answer."""
    import time
    if _PANES[1] is not None and time.monotonic() - _PANES[0] < 3:
        return set(_PANES[1])
    answer = _query_pane_cwds()
    _PANES[:] = [time.monotonic(), answer]
    return answer


def _query_pane_cwds() -> set[Path] | None:
    if not shutil.which("herdr"):
        return set()
    try:
        proc = subprocess.run(["herdr", "pane", "list"], capture_output=True, text=True, timeout=10)
        start = proc.stdout.find("{")
        data = json.loads(proc.stdout[start:]) if start >= 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    panes = (data.get("result") or {}).get("panes") if isinstance(data, dict) else None
    if proc.returncode or not isinstance(panes, list):
        return None
    cwds = set()
    for pane in panes:
        values = [pane.get(key) for key in ("cwd", "foreground_cwd")] if isinstance(pane, dict) else []
        values = [v for v in values if isinstance(v, str) and v]
        if not values:
            return None  # a pane with no known cwd could be anywhere: fail closed
        cwds.update(Path(v).absolute() for v in values)
    return cwds


def _pane_cwd_blocker(wt: Path) -> str | None:
    cwds = _live_pane_cwds()
    if cwds is None:
        return "Herdr pane list unavailable; live pane cwds unverified"
    targets = {wt.absolute(), wt.resolve()}
    for cwd in cwds:
        for c in {cwd, cwd.resolve()}:
            if any(c == t or c.is_relative_to(t) for t in targets):
                return f"live Herdr pane cwd {cwd}"
    return None


def patch_dir(run: dict) -> Path:
    """Outside the run directory, so office prune never deletes saved work."""
    return paths.state_home() / "worktree-patches" / run["id"][:8]


def save_patch(run: dict, wt: Path, head: str, base: str | None, *, label: str | None = None) -> Path:
    """Save HEAD's commits beyond the default branch (else beyond every remote ref) as a .patch."""
    repo = Path(run["repo_root"])
    root = patch_dir(run)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = (label or wt.name).replace("/", "_")
    target = root / f"{name}-{head[:12]}.patch"
    if target.is_file() and target.stat().st_size:
        return target
    base = base or _base_ref(repo)
    if base:
        proc = _git(repo, "format-patch", "--stdout", f"{base}..{head}")
    else:
        own = _git(repo, "rev-list", "--reverse", head, "--not", "--remotes").stdout.split()
        first = own[0] if own else None
        if first and _git(repo, "rev-parse", "--verify", "--quiet", first + "^").returncode == 0:
            proc = _git(repo, "format-patch", "--stdout", f"{first}^..{head}")
        elif first:
            proc = _git(repo, "format-patch", "--stdout", "--root", head)
        else:
            proc = subprocess.CompletedProcess([], 0, "", "")
    if proc.returncode:
        raise OSError("cannot save patch: " + proc.stderr.strip()[:160])
    body = proc.stdout or f"# {wt} HEAD {head}: no commits beyond {base or 'the remotes'}\n"
    # format-patch omits merge commits; a full diff from the merge-base keeps
    # conflict-resolution content too.
    mb = _git(repo, "merge-base", base, head) if base else None
    if mb is not None and mb.returncode == 0 and mb.stdout.strip():
        full = _git(repo, "diff", "--binary", mb.stdout.strip(), head)
        if full.returncode == 0 and full.stdout:
            diff_tmp = target.with_suffix(".diff.tmp")
            diff_tmp.write_text(full.stdout)
            diff_tmp.replace(target.with_suffix(".diff"))
    tmp = target.with_suffix(".patch.tmp")
    tmp.write_text(body)
    tmp.replace(target)
    return target


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


UNMERGED = "commit not proven recoverable from current online origin: unmerged commits"
NO_ORIGIN = "commit not proven recoverable from current online origin (origin unreachable)"


def _landed(run: dict) -> bool:
    return (run.get("landing") or {}).get("status") in ("landed", "landed-externally")


def _safe_candidate(con, run: dict, wt: Path, registered: dict[Path, dict],
                    tips: list[str], base: str | None = None, facts: dict | None = None) -> str | None:
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
    if (blocker := _pane_cwd_blocker(wt)):
        return blocker
    staged = _git(wt, "diff", "--quiet", "--cached", "HEAD", "--")
    unstaged = _git(wt, "diff", "--quiet", "HEAD", "--")
    if staged.returncode or unstaged.returncode:
        return "local tracked edits not archived (preserved in worktree)"
    head = _git(wt, "rev-parse", "HEAD")
    if head.returncode:
        return "commit not proven recoverable from current online origin (HEAD unreadable)"
    sha = head.stdout.strip()
    if not tips:
        if facts is not None:
            facts.update(head=sha, proof=None, base=None)
        return NO_ORIGIN
    branch = info.get("branch") if isinstance(info.get("branch"), str) else None
    proof = merge_proof(repo, sha, tips, base, branch, landed=_landed(run))
    if facts is not None:
        facts.update(head=sha, proof=proof, base=base)
    if not proof:
        return UNMERGED
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
    # A handoff or unverified close no longer keeps every worktree: each one
    # is removed only on its own merge proof (_safe_candidate), else kept.
    if con.execute("SELECT 1 FROM runs WHERE substr(id,1,8)=? AND id<>? "
                   "AND phase NOT IN ('closed','abandoned') LIMIT 1", (run["id"][:8], run["id"])).fetchone():
        return "a live run shares this run's worktree directory prefix"
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
        try:
            tips = _remote_tips(Path(run["repo_root"]))
            base = _live_base(Path(run["repo_root"]), tips) if tips else None
        except (OSError, subprocess.SubprocessError):
            tips, base = [], None
        for wt in worktrees:
            facts: dict = {}
            try:
                blocker = _safe_candidate(con, run, wt, registered, tips, base, facts)
                if blocker in (UNMERGED, NO_ORIGIN):
                    blocker += " (would save a .patch)"
                modified = (datetime.fromtimestamp(wt.stat().st_mtime, tz=timezone.utc).isoformat()
                            if wt.exists() else None)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                blocker, modified = str(exc), None
            details.append({"path": str(wt), "modified_at": modified,
                            "bytes": _size(wt), "blocker": blocker, "proof": facts.get("proof")})
    return {
        "run_id": run["id"], "repo": run.get("repo_root"), "office_version": run.get("office_version"),
        "phase": run["phase"], "issue": landing.get("issue"),
        "pr": landing.get("pr") or landing.get("handoff"),
        "landing": landing.get("detail") or landing.get("status"),
        "terminal_at": run.get("terminal_at"),
        "worktrees": [str(p) for p in worktrees], "bytes": sum(_size(p) for p in worktrees),
        "details": details, "blocker": why,
    }


def sweep(con, run: dict, *, override_decline: bool = False, refresh: bool = True,
          deadline: float | None = None) -> dict:
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
            tips = _remote_tips(repo, refresh=refresh)
            base = _live_base(repo, tips) if tips else None
            candidates = _candidates(con, current, registered)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            candidates = []
            kept.append({"path": str(repo), "reason": f"discovery failed: {exc}"})
            registered, tips, base = {}, [], None
        patches = []
        import time
        for wt in candidates:
            if deadline is not None and time.monotonic() > deadline:
                kept.append({"path": str(wt), "reason": "time budget reached; next close or office gc"})
                continue
            try:
                facts: dict = {}
                reason = _safe_candidate(con, current, wt, registered, tips, base, facts)
                if reason == "already absent":
                    continue
                if reason in (UNMERGED, NO_ORIGIN):
                    # Unproven work stays in its worktree, and a .patch is saved too.
                    saved = save_patch(current, wt, facts["head"], base)
                    patches.append(str(saved))
                    kept.append({"path": str(wt), "reason": f"{reason} (patch saved: {saved})", "patch": str(saved)})
                    continue
                if reason:
                    kept.append({"path": str(wt), "reason": reason})
                    continue
                if facts.get("proof") != "reachable from the default branch":
                    # Proven by content, PR or landing rather than by ancestry:
                    # keep a .patch of the commits before removing the checkout.
                    patches.append(str(save_patch(current, wt, facts["head"], base)))
                from office import deploy_artifacts
                # Capture and verify all nontracked and ignored files, including .env,
                # before risking any destructive Git worktree operation.
                backup = deploy_artifacts.preserve_or_refuse(current, wt)
                # Re-check immediately: writers, panes or commits could change
                # while the backup was being prepared.
                recent = _registered(repo)
                _PANES[:] = [0.0, None]  # the destructive recheck never uses a cached pane list
                reason = _safe_candidate(con, current, wt, recent, _remote_tips(repo), base)
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
        branch_deleted, branch_kept = _delete_merged_branches(current, repo, tips, base)
        report = {"removed": removed, "kept": kept, "branches": branch_deleted,
                  "branches_kept": branch_kept, "bytes": bytes_reclaimed, "patches": patches}
        _record(con, current, "incomplete" if kept else "complete", report)
        return report


def _delete_merged_branches(run: dict, repo: Path, tips: list[str], base: str | None) -> tuple[list[str], list[str]]:
    """Delete local office/<run8>/* branches whose tip is merged. Never a remote branch."""
    deleted, kept = [], []
    if not tips:
        return deleted, kept
    refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", f"refs/heads/office/{run['id'][:8]}/")
    if refs.returncode:
        return deleted, kept
    try:
        checked_out = {info.get("branch") for info in _registered(repo).values()}
    except OSError:
        return deleted, [line.split()[0].removeprefix("refs/heads/") for line in refs.stdout.splitlines() if line]
    for line in refs.stdout.splitlines():
        ref, _, sha = line.partition(" ")
        if not ref.startswith("refs/heads/office/"):
            continue
        name = ref.removeprefix("refs/heads/")
        try:
            proof = None if ref in checked_out else merge_proof(repo, sha, tips, base, name, landed=_landed(run))
            if proof and proof != "reachable from the default branch":
                save_patch(run, Path(name), sha, base, label=name)  # deleting the ref drops its reflog
        except (OSError, subprocess.SubprocessError):
            proof = None
        # -D only after a merge proof (squash merges fail -d); the ref and SHA
        # are pinned so a branch that moved since the proof is not deleted.
        if proof and _git(repo, "update-ref", "-d", ref, sha).returncode == 0:
            deleted.append(name)
        else:
            kept.append(name)
    return deleted, kept


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


GC_BUDGET_SECONDS = 90.0  # close-time sweep of older runs; the rest waits for the next close or office gc


def _age_days(run: dict) -> float | None:
    try:
        when = datetime.fromisoformat(str(run.get("terminal_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


def gc(con, *, dry_run: bool = False, older_than: float | None = None, skip_run: str | None = None,
       budget: float | None = None) -> dict:
    """Sweep worktrees of every terminal run, across all repos Office knows.

    Only closed or abandoned runs are visited; _safe_candidate still keeps any
    path another live run records or a live Herdr pane sits in. A dry run
    fetches nothing and writes nothing.
    """
    import time
    start = time.monotonic()
    out = {"dry_run": dry_run, "runs": 0, "removed": [], "kept": [], "would_remove": [], "branches": [],
           "patches": [], "bytes": 0, "deferred": []}
    rows = list(con.execute(
        "SELECT r.id FROM runs r LEFT JOIN cleanup_runs c ON c.run_id=r.id "
        "WHERE r.office_version IS NOT NULL AND r.phase IN ('closed','abandoned') "
        "AND (r.pruned_at IS NULL OR r.prune_status!='complete') "
        "AND (c.status IS NULL OR c.status<>'declined') ORDER BY " +
        # The budgeted pass starts with the runs swept longest ago, so runs
        # that keep unmerged work do not starve the rest.
        ("(c.updated_at IS NOT NULL), c.updated_at, r.terminal_at DESC" if budget is not None
         else "r.terminal_at DESC")))
    receipts = {r["run_id"]: r["status"] for r in con.execute("SELECT run_id, status FROM cleanup_runs")}
    fetched: set[str] = set()
    for row in rows:
        if row["id"] == skip_run:
            continue
        # The budgeted close-time pass skips runs already swept clean; an
        # explicit office gc rechecks them for later leaks.
        if budget is not None and receipts.get(row["id"]) == "complete":
            continue
        run = state.get_run(con, row["id"])
        if older_than is not None and ((age := _age_days(run)) is None or age < older_than):
            continue
        if budget is not None and time.monotonic() - start > budget:
            out["deferred"].append(run["id"])
            continue
        out["runs"] += 1
        try:
            _gc_one(con, run, out, dry_run=dry_run, fetched=fetched,
                    deadline=start + budget if budget is not None else None)
        except Exception as exc:  # one bad run never stops the sweep or a close
            out["kept"].append({"run_id": run["id"], "path": "", "reason": f"gc error: {str(exc)[:200]}"})
    return out


def _gc_one(con, run: dict, out: dict, *, dry_run: bool, fetched: set[str], deadline: float | None) -> None:
    if dry_run:
        info = preview(con, run, inspect=True)
        if info["blocker"]:
            out["kept"].append({"run_id": run["id"], "path": "", "reason": info["blocker"]})
        for d in info["details"]:
            if d["blocker"] is None:
                out["would_remove"].append({"run_id": run["id"], "path": d["path"], "proof": d.get("proof")})
                out["bytes"] += d["bytes"]
            elif d["blocker"] != "already absent":
                out["kept"].append({"run_id": run["id"], "path": d["path"], "reason": d["blocker"]})
        return
    common = str(run.get("git_common_dir"))
    try:
        report = sweep(con, run, refresh=common not in fetched, deadline=deadline)
    except Refused as exc:
        out["kept"].append({"run_id": run["id"], "path": "", "reason": str(exc)})
        return
    fetched.add(common)
    out["removed"].extend({"run_id": run["id"], "path": p} for p in report["removed"])
    out["kept"].extend({"run_id": run["id"], **k} for k in report["kept"]
                       if k.get("path") or k.get("reason", "").startswith(("discovery failed", "close archive")))
    out["branches"].extend(report.get("branches", []))
    out["patches"].extend(report.get("patches", []))
    out["bytes"] += report["bytes"]


def _gc_summary(report: dict) -> str:
    verb = "would remove" if report["dry_run"] else "removed"
    count = len(report["would_remove"] if report["dry_run"] else report["removed"])
    line = (f"gc: {report['runs']} finished run(s) checked; {verb} {count} worktree(s), kept {len(report['kept'])}, "
            f"~{report['bytes']/1e9:.2f} GB {'reclaimable' if report['dry_run'] else 'freed'}")
    if report["branches"]:
        line += f", deleted {len(report['branches'])} merged local branch(es)"
    if report["deferred"]:
        line += f"; {len(report['deferred'])} run(s) left for the next close or office gc (time budget)"
    return line


def auto_gc(con, run: dict) -> list[str]:
    """End of every close: sweep this run if abandoned, then older finished runs under a time budget."""
    if os.environ.get("OFFICE_NO_AUTO_GC") == "1":
        return ["gc: skipped (OFFICE_NO_AUTO_GC=1); run office gc"]
    lines = []
    try:
        current = state.get_run(con, run["id"])
        if current and current["phase"] == "abandoned":
            own = sweep(con, current)
            lines.append(f"abandoned run cleanup: removed {len(own['removed'])} worktree(s), "
                         f"kept {len([k for k in own['kept'] if k.get('path')])}, ~{own['bytes']/1e9:.3f} GB reclaimed")
            lines.extend(f"kept worktree {k['path']} ({k['reason']})" for k in own["kept"] if k.get("path"))
        lines.append(_gc_summary(gc(con, skip_run=run["id"], budget=GC_BUDGET_SECONDS)))
    except Exception as exc:  # a finished close never fails on housekeeping
        lines.append(f"gc: skipped ({str(exc)[:160]}); run office gc")
    return lines


def gc_command(con, *, dry_run: bool = False, older_than: float | None = None) -> Result:
    report = gc(con, dry_run=dry_run, older_than=older_than)
    res = Result(lines=[_gc_summary(report)])
    for item in report["would_remove"]:
        res.add(f"would remove {item['path']} ({item['proof']})")
    for item in report["removed"]:
        res.add(f"removed {item['path']}")
    for item in report["kept"]:
        res.add(f"kept {item['path'] or short(item['run_id'])}: {item['reason']}")
    res.data = report
    res.next = "office gc" if dry_run and report["would_remove"] else None
    return res


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
