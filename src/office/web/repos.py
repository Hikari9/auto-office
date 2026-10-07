"""Local attachment and execution readiness for discovered GitHub repositories.

Candidate checkouts are the repo roots recorded in runs.db (passed in by the
caller) plus configured checkout paths. Readiness lists every prerequisite
with its result so the UI can show exactly what fails. Absolute paths never
leave this module: callers get a shortened label only.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Mapping

from office.web.github import is_writable

Git = Callable[[Path, list[str]], "str | None"]

_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)


def run_git(path: Path, args: list[str]) -> str | None:
    """stdout of `git -C path ...`, or None on any failure."""
    try:
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def remote_full_name(url: str | None) -> str | None:
    m = _REMOTE.search((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def path_label(path: Path) -> str:
    parts = Path(path).parts
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else Path(path).name or "?"


def _candidates(run_roots: Iterable[str | Path], checkout_paths: Iterable[str | Path]) -> list[Path]:
    seen: dict[str, Path] = {}
    for p in [*run_roots, *checkout_paths]:
        if p:
            path = Path(p).expanduser()
            seen.setdefault(str(path), path)
    return list(seen.values())


def assess(repo: Mapping, run_roots: Iterable[str | Path] = (),
           checkout_paths: Iterable[str | Path] = (), git: Git = run_git) -> dict:
    """Attachment and readiness for one repo record from `GitHubClient.snapshot()`."""
    want = (repo.get("full_name") or "").lower()
    probes = []
    for path in _candidates(run_roots, checkout_paths):
        exists = path.is_dir()
        is_git = exists and git(path, ["rev-parse", "--git-dir"]) is not None
        origin = remote_full_name(git(path, ["remote", "get-url", "origin"])) if is_git else None
        probes.append((path, exists, is_git, origin))
    attached = next((p for p in probes if p[3] == want), None)
    best = attached or next((p for p in probes if p[2]), None) or next((p for p in probes if p[1]), None) \
        or (probes[0] if probes else None)
    _, exists, is_git, origin = best or (None, False, False, None)
    revoked = repo.get("access") == "revoked"
    prereqs = [
        ("checkout_exists", exists, None if exists else "no local checkout found"),
        ("git_repository", is_git, None if is_git else "checkout is not a git repository"),
        ("origin_matches", origin == want and bool(want),
         None if origin == want else f"origin is {origin or 'missing'}"),
        ("push_permission", not revoked and is_writable(repo.get("permission")),
         f"permission is {repo.get('permission') or 'none'}"),
        ("issues_enabled", not revoked and bool(repo.get("has_issues")), "issues are disabled"),
        ("not_archived", not revoked and not repo.get("archived"), "repository is archived"),
    ]
    checks = [{"name": n, "ok": bool(ok), "detail": None if ok else why} for n, ok, why in prereqs]
    return {
        "full_name": repo.get("full_name"),
        "attached": attached is not None,
        "checkout": path_label(best[0]) if best else None,
        "ready": all(c["ok"] for c in checks),
        "failing": [c["name"] for c in checks if not c["ok"]],
        "prerequisites": checks,
    }


def assess_all(repos: Iterable[Mapping], run_roots: Iterable[str | Path] = (),
               checkout_paths: Mapping[str, Iterable[str | Path]] | None = None,
               git: Git = run_git) -> list[dict]:
    """`checkout_paths` maps full_name to configured paths; run roots apply to every repo."""
    roots = list(run_roots)
    config = {k.lower(): v for k, v in (checkout_paths or {}).items()}
    return [assess(r, roots, config.get((r.get("full_name") or "").lower(), ()), git) for r in repos]
