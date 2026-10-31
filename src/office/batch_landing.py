"""Native ordered batch landing across reviewed Office runs and individual PRs.

A batch uses an operator-owned JSON manifest, never infers authorization from
a PR's description. Its one designated version owner carries the only version
change.

Order: the pinned PR heads of every lane are composed, in lane order, onto the
default branch in a throwaway checkout. The version and changelog are checked
on the composed tree, and the full suite runs once on that composed SHA. Only
a green suite merges anything. Each merge pins the head the suite validated,
and when the last lane lands the default branch tree must equal the composed
tree.

An interrupted or merge-blocked batch resumes from its record. A red batch
accepts appended fix PR lanes before it recomposes.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
from pathlib import Path

from office import integration, land, paths, prs, state
from office.result import Result
from office.state import Refused, Usage

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
_DEFAULT_VERSION_FILE = "src/office/_version.py"


def _path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise Usage("batch-invalid", f"{field} must be a relative repository file")
    parts = Path(value).parts
    if any(p in ("..", ".git") for p in parts):
        raise Usage("batch-invalid", f"{field} cannot escape the repository or use .git")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise Usage("batch-invalid", f"duplicate manifest keys: {', '.join(repeated)}")
    return dict(pairs)


def _manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, ValueError) as exc:
        raise Usage("batch-invalid", f"cannot read batch manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise Usage("batch-invalid", "the batch manifest must be a JSON object")
    batch_id = manifest.get("id")
    if not isinstance(batch_id, str) or not _ID.fullmatch(batch_id):
        raise Usage("batch-invalid", "batch id must be 1-64 letters, digits, dots, hyphens or underscores")
    base = manifest.get("base", "main")
    if not isinstance(base, str) or not _ID.fullmatch(base):
        raise Usage("batch-invalid", "base must name a simple local branch (e.g. main)")
    version = manifest.get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise Usage("batch-invalid", "version must be an explicit semver-like release (e.g. 3.16.0)")
    version_file = _path(manifest.get("version_file", _DEFAULT_VERSION_FILE), "version_file")
    changelog_file = _path(manifest.get("changelog_file", "CHANGELOG.md"), "changelog_file")
    suite = manifest.get("full_suite")
    if isinstance(suite, str):
        try:
            suite = shlex.split(suite)
        except ValueError as exc:
            raise Usage("batch-invalid", f"full_suite is not shell-parseable: {exc}") from exc
    if not isinstance(suite, list) or not suite or not all(isinstance(x, str) and x for x in suite):
        raise Usage("batch-invalid", "full_suite must be a nonempty argv list (or a shell-quoted command)")
    if any(x in (";", "&&", "||", "|", ">", "<") for x in suite):
        raise Usage("batch-invalid", "full_suite must be a command, not a shell pipeline")
    timeout = manifest.get("timeout_seconds", 7200)
    if type(timeout) is not int or not 60 <= timeout <= 14400:
        raise Usage("batch-invalid", "timeout_seconds must be between 60 and 14400")
    method = manifest.get("merge_method", "merge")
    if method not in prs.MERGE_FLAGS:
        raise Usage("batch-invalid", f"merge_method must be one of {', '.join(prs.MERGE_FLAGS)}")
    lanes = manifest.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise Usage("batch-invalid", "lanes must be a nonempty list")
    ids = set()
    owners = []
    for lane in lanes:
        if not isinstance(lane, dict):
            raise Usage("batch-invalid", "every lane must be an object")
        ident = lane.get("id")
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in ids:
            raise Usage("batch-invalid", f"duplicate or invalid lane id: {ident!r}")
        ids.add(ident)
        if ("pr" in lane) == ("run" in lane):
            raise Usage("batch-invalid", f"{ident}: supply exactly one of pr or run")
        if "pr" in lane and (type(lane["pr"]) is not int or lane["pr"] <= 0):
            raise Usage("batch-invalid", f"{ident}: pr must be a positive integer")
        if "run" in lane and (not isinstance(lane["run"], str) or not lane["run"].strip()):
            raise Usage("batch-invalid", f"{ident}: run must be a run ID")
        deps = lane.get("land_after", [])
        if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps) or len(set(deps)) != len(deps):
            raise Usage("batch-invalid", f"{ident}: land_after must be a list of unique lane IDs")
        if type(lane.get("version_bump", False)) is not bool:
            raise Usage("batch-invalid", f"{ident}: version_bump must be true or false")
        if lane.get("version_bump"):
            owners.append(ident)
    if len(owners) != 1:
        raise Usage("batch-version-owner", "exactly one lane must declare version_bump: true")
    by_id = {lane["id"]: lane for lane in lanes}
    order = []
    visiting = set()
    visited = set()

    def visit(ident: str):
        if ident in visiting:
            raise Usage("batch-cycle", f"land_after dependency cycle at {ident}")
        if ident in visited:
            return
        visiting.add(ident)
        for dependency in by_id[ident].get("land_after", []):
            if dependency not in by_id:
                raise Usage("batch-missing-dependency", f"{ident}: unknown land_after lane {dependency}")
            visit(dependency)
        visiting.remove(ident)
        visited.add(ident)
        order.append(by_id[ident])

    for lane in lanes:
        visit(lane["id"])
    return {"id": batch_id, "base": base, "version": version,
            "version_file": version_file, "changelog_file": changelog_file,
            "full_suite": suite, "timeout_seconds": timeout, "merge_method": method,
            "lanes": order}


def _git(repo: Path, *args: str) -> str:
    return paths.git(repo, *args)


def _github(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return land._gh({"repo_root": str(repo)}, *args, timeout=timeout)


def _pr_info(repo: Path, number: int, base: str, *, allow_merged: bool, stacked: bool = False) -> dict:
    proc = _github(repo, "pr", "view", str(number), "--json",
                   "number,state,baseRefName,headRefName,headRefOid,isDraft,files")
    if proc.returncode:
        raise Refused("batch-pr-unavailable", f"#{number}: {(proc.stderr or proc.stdout).strip()[:200]}")
    try:
        info = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise Refused("batch-pr-unavailable", f"#{number}: invalid GitHub response") from exc
    if not stacked and info.get("baseRefName") != base:
        raise Refused("batch-wrong-base", f"#{number} targets {info.get('baseRefName')}, expected {base}")
    status = info.get("state")
    if status != "OPEN" and not (allow_merged and status == "MERGED"):
        raise Refused("batch-pr-state", f"#{number} is {status}, expected an open PR")
    if status == "OPEN" and info.get("isDraft"):
        raise Refused("batch-pr-draft", f"#{number} is a draft")
    if status == "OPEN" and not re.fullmatch(r"[0-9a-f]{40}", info.get("headRefOid") or ""):
        raise Refused("batch-pr-head", f"#{number} has no readable head SHA")
    files = info.get("files")
    if not isinstance(files, list):
        raise Refused("batch-pr-files", f"#{number}: cannot inspect changed paths")
    info["paths"] = [f.get("path") for f in files if isinstance(f, dict)]
    return info


def _run_lane(con, lane: dict, repo: Path, base: str) -> tuple[dict, list[tuple[str, int]]]:
    """Return the run and its accepted (task ID, PR number) pairs in dependency order."""
    if con is None:
        raise Refused("batch-no-recorder", f"{lane['id']}: Office runs need a readable runs.db")
    run = state.find_run(con, lane["run"])
    if run is None:
        raise Refused("batch-run-not-found", f"{lane['id']}: run {lane['run']} not found")
    if Path(run["repo_root"]).resolve() != repo.resolve():
        raise Refused("batch-wrong-repo", f"{lane['id']}: run belongs to {run['repo_root']}")
    if land.end_state(con, run)["mode"] != "merge":
        raise Refused("batch-run-authority", f"{lane['id']}: run must have the approved merge end_state")
    snap = land._snapshot(con, run)
    if not snap["commit"] or snap["accepted"] is None:
        raise Refused("batch-run-unaccepted", f"{lane['id']}: integration is not accepted")
    land._require_run_coverage(run, snap["commit"], con=con)
    settings = prs.settings(con, run)
    if not settings.get("enabled") or settings.get("base_branch") != base:
        raise Refused("batch-run-settings", f"{lane['id']}: PRs are disabled or the run uses another base")
    pairs = []
    for task in integration._topo(integration.accepted_set(con, run) or []):
        if prs.has_pr(task):
            pr = (state.get_task(con, run["id"], task["id"]) or {}).get("pr") or {}
            if not isinstance(pr.get("number"), int):
                raise Refused("batch-run-pr", f"{lane['id']}/{task['id']}: accepted task has no PR")
            pairs.append((task["id"], pr["number"]))
    return run, pairs


def _state_path(repo: Path, ident: str) -> Path:
    root = Path(_git(repo, "rev-parse", "--git-common-dir"))
    if not root.is_absolute():
        root = (repo / root).resolve()
    return root / "office" / "batches" / f"{ident}.json"


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.with_suffix(".tmp")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(target, path)


@contextlib.contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # The lock is repository-scoped, not batch-scoped. Two release batches
    # cannot mutate the same default branch simultaneously.
    lock = path.parent.parent / "landing.lock"
    with open(lock, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("batch-running", "another batch landing is active in this repository") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _entries(manifest: dict, con, repo: Path) -> tuple[list[dict], dict[int, dict]]:
    entries = []
    infos: dict[int, dict] = {}
    seen = set()
    for lane in manifest["lanes"]:
        run, tasks = None, {}
        if "run" in lane:
            run, pairs = _run_lane(con, lane, repo, manifest["base"])
            numbers = [number for _, number in pairs]
            tasks = {number: task for task, number in pairs}
        else:
            numbers = [lane["pr"]]
        if not numbers:
            raise Refused("batch-empty-lane", f"{lane['id']}: no PRs to land")
        for number in numbers:
            if number in seen:
                raise Refused("batch-duplicate-pr", f"#{number} appears in more than one lane")
            seen.add(number)
            info = _pr_info(repo, number, manifest["base"], allow_merged=True, stacked=run is not None)
            if info["state"] == "OPEN" and info["baseRefName"] != manifest["base"] and manifest["merge_method"] != "merge":
                raise Refused("batch-stacked-method",
                              f"#{number} is stacked on {info['baseRefName']}; stacked run PRs need merge_method "
                              "merge, because squash or rebase would rewrite the pinned head")
            infos[number] = info
        # A stacked run PR may only rest on the batch base or on another PR of the same run.
        # Otherwise its pinned head would carry history the run never accepted.
        run_branches = {infos[n]["headRefName"] for n in numbers}
        for number in numbers:
            info = infos[number]
            if info["state"] == "OPEN" and info["baseRefName"] not in run_branches | {manifest["base"]}:
                raise Refused("batch-stacked-base",
                              f"#{number} is stacked on {info['baseRefName']}, which is not part of run lane {lane['id']}")
        entries.append({"lane": lane, "run": run, "prs": numbers, "tasks": tasks})
    version_owners = [(e["lane"]["id"], number) for e in entries for number in e["prs"]
                      if manifest["version_file"] in infos[number]["paths"]]
    declared = next(e["lane"]["id"] for e in entries if e["lane"].get("version_bump"))
    if len(version_owners) != 1 or version_owners[0][0] != declared:
        raise Refused("batch-version-owner",
                      f"expected exactly one PR in version owner {declared} changing {manifest['version_file']}; "
                      f"found {version_owners or 'none'}", preserved="no PRs merged by this attempt")
    return entries, infos


def _fetch_pr_head(repo: Path, number: int, sha: str) -> None:
    """Fetch the PR head for source inspection when it is not already local."""
    if land._git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0:
        return
    proc = land._git(repo, "fetch", "-q", "origin", f"refs/pull/{number}/head")
    if proc.returncode or land._git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
        raise Refused("batch-pr-head-unavailable",
                      f"#{number}: cannot inspect the pinned head {sha[:12]} before merging")


def _compose(repo: Path, base_sha: str, entries: list[dict], infos: dict[int, dict]) -> tuple[str, dict, dict]:
    """Merge each pinned PR head, in lane order, onto base_sha in a throwaway checkout.

    Returns the composed commit, the head merged for each PR number, and the
    tree after each lane (the proof a default branch must match after that lane).
    """
    env = {**os.environ, **paths.commit_identity_env(repo)}
    heads: dict[str, str] = {}
    trees: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="office-batch-compose-") as temporary:
        checkout = Path(temporary) / "compose"
        _git(repo, "worktree", "add", "--detach", str(checkout), base_sha)
        try:
            for entry in entries:
                for number in entry["prs"]:
                    head = infos[number]["headRefOid"]
                    _fetch_pr_head(repo, number, head)
                    heads[str(number)] = head
                    # Signing and hooks belong to the operator's commits, not this throwaway merge.
                    proc = land._git(checkout, "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                                     "merge", "--no-ff", "--no-edit", head, env=env)
                    if proc.returncode:
                        conflicted = land._git(checkout, "diff", "--name-only", "--diff-filter=U").stdout.strip()
                        land._git(checkout, "merge", "--abort", env=env)
                        if not conflicted:
                            raise Refused("batch-compose-failed",
                                          f"{entry['lane']['id']}: merging #{number} failed: "
                                          f"{(proc.stderr or proc.stdout).strip()[:200]}",
                                          preserved="no PRs merged by this attempt")
                        raise Refused("batch-compose-conflict",
                                      f"{entry['lane']['id']}: #{number} does not merge cleanly into {base_sha[:12]}",
                                      preserved="no PRs merged by this attempt",
                                      next_step="reconcile the conflicting lanes in a reviewed PR, then rerun office land --batch")
                trees[entry["lane"]["id"]] = _git(checkout, "rev-parse", "HEAD^{tree}")
            composed = _git(checkout, "rev-parse", "HEAD")
        finally:
            _git(repo, "worktree", "remove", "--force", str(checkout))
    return composed, heads, trees


def _version_text(repo: Path, commit: str, file: str) -> str:
    return _git(repo, "show", f"{commit}:{file}")


def _verify_version(repo: Path, start: str, current: str, manifest: dict) -> None:
    file = manifest["version_file"]
    old = _version_text(repo, start, file)
    new = _version_text(repo, current, file)
    if old == new or manifest["version"] in old or new.count(manifest["version"]) != 1:
        raise Refused("batch-version-mismatch", f"single version bump to {manifest['version']} not present in {file}")
    # The Auto Office version source is a Python assignment. Other repositories
    # can use a different version_file but must still contain the exact version.
    if file == _DEFAULT_VERSION_FILE:
        pattern = r'(?m)^__version__\s*=\s*["\x27]([^"\x27]+)["\x27]\s*$'
        previous = re.search(pattern, old)
        updated = re.search(pattern, new)
        if not previous or not updated or updated.group(1) != manifest["version"]:
            raise Refused("batch-version-mismatch", f"{file} does not define the requested release version")
        try:
            before_parts = tuple(int(x) for x in previous.group(1).split(".")[:3])
            after_parts = tuple(int(x) for x in updated.group(1).split(".")[:3])
            if after_parts <= before_parts:
                raise ValueError("not an increase")
        except ValueError as exc:
            raise Refused("batch-version-mismatch", "version must increase from the batch starting version") from exc
    changelog = manifest.get("changelog_file")
    if changelog:
        content = _version_text(repo, current, changelog)
        # Accept "## 3.15.0", "## 3.15.0 (2026-10-31)" and "## [3.15.0] - date".
        headers = re.findall(r"(?m)^##\s+\[?v?(\d[^\s\]]*)\]?(?=\s|$)", content)
        if headers.count(manifest["version"]) != 1:
            raise Refused("batch-changelog-mismatch", f"{changelog} needs exactly one {manifest['version']} header")


def _resume(record: dict | None, manifest: dict, current: str) -> dict:
    stable = ("base", "version", "version_file", "changelog_file", "full_suite", "merge_method")
    if record is None:
        return {"id": manifest["id"], **{key: manifest[key] for key in stable},
                "start": current, "lanes": [], "completed": [], "status": "pending"}
    if any(record.get(key) != manifest[key] for key in stable):
        raise Refused("batch-manifest-changed", "base, version, full suite or merge method changed since batch start")
    prior = record.get("lanes", [])
    supplied = manifest["lanes"]
    if prior != supplied[:len(prior)]:
        raise Refused("batch-manifest-changed", "previous lane definitions/order changed; only append fix lanes")
    if record.get("status") == "green":
        if len(prior) != len(supplied):
            raise Refused("batch-already-green", "a green batch cannot be extended; start a new batch")
        return record
    if len(prior) != len(supplied):
        if record.get("status") != "red":
            raise Refused("batch-manifest-changed",
                          "lanes were already validated or merged; fix lanes can be appended only after a red suite")
        # Nothing merged after a red suite, so the fix lanes compose onto the current default branch.
        record["status"] = "pending"
    if record.get("status") == "blocked" and not record.get("last_head"):
        # No PR merged yet, so nothing is stale: recompose, so a PR whose head changed is revalidated.
        record["status"] = "pending"
    return record


def _record_run_landings(con, entries: list[dict], record: dict, commit: str) -> None:
    """Make closeout possible for accepted Office runs after the batch suite passed."""
    if con is None:
        return
    for entry in entries:
        run = entry["run"]
        if run is None:
            continue
        fresh = state.get_run(con, run["id"])
        if not (fresh.get("landing") or {}).get("merged"):
            land._record(con, fresh, merged={"commit": commit,
                                              "integration_commit": land._snapshot(con, fresh)["commit"],
                                              "rollback": record["start"]})


def _mark_task_merged(con, run: dict, task_id: str, number: int, base: str, method: str) -> None:
    from office import db
    pr = (state.get_task(con, run["id"], task_id) or {}).get("pr") or {}
    if pr.get("merged"):
        return
    with db.transaction(con):
        state.update_task(con, run["id"], task_id, pr={**pr, "merged": True, "base": base, "method": method})
        state.emit(con, run, "pr.merged", f"{task_id} #{number} merged into {base} ({method})", task_id=task_id)


def _merge_pr(repo: Path, number: int, info: dict, method: str, base: str, expected: str) -> None:
    """Merge one PR on its pinned head. `expected` is the default-branch head this batch last recorded."""
    if info["state"] == "MERGED":
        return
    land._wait_checks({"repo_root": str(repo)}, number)
    latest = _pr_info(repo, number, base, allow_merged=True, stacked=True)
    if latest["state"] == "MERGED":
        return
    if latest["headRefOid"] != info["headRefOid"]:
        raise Refused("batch-head-moved", f"#{number} changed head since preflight")
    # GitHub does not pin the base branch. Re-check it after the checks wait, right before the merge.
    _git(repo, "fetch", "-q", "origin", base)
    if _git(repo, "rev-parse", f"origin/{base}") != expected:
        raise Refused("batch-main-moved", f"{base} moved while #{number} waited for its checks")
    if info["baseRefName"] != base:
        # A stacked run PR: retarget only once its checks and head have passed. The head does not move.
        proc = _github(repo, "pr", "edit", str(number), "--base", base)
        if proc.returncode:
            raise Refused("batch-retarget-failed",
                          f"#{number}: cannot retarget to {base}: {(proc.stderr or proc.stdout).strip()[:200]}")
    proc = _github(repo, "pr", "merge", str(number), prs.MERGE_FLAGS[method],
                   "--match-head-commit", info["headRefOid"])
    if proc.returncode:
        raise Refused("batch-merge-failed", f"#{number}: {(proc.stderr or proc.stdout).strip()[:200]}")


# Credentials must not reach the suite: its checkout holds unmerged PR code.
_SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|(?:^|_)KEY$|^GH_|^GITHUB_|^OFFICE_|^SSH_AUTH_SOCK$|^AWS_")


def _execute(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    """Run the suite in its own process group, without credentials in its environment.

    A timeout kills the whole group, so pytest workers cannot outlive the checkout.
    """
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", start_new_session=True)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out + err
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            return 124, f"full suite timed out after {timeout}s\n" + out + err
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


def _run_full(repo: Path, sha: str, manifest: dict, folder: Path) -> tuple[int, Path]:
    from office import shell_safety
    argv = manifest["full_suite"]
    if shell_safety.unsafe_manual_push(" ".join(shlex.quote(arg) for arg in argv)):
        raise Refused("batch-unsafe-suite", "full_suite must not push or mutate a remote")
    logs = folder / manifest["id"]
    logs.mkdir(parents=True, exist_ok=True)
    # Every attempt keeps its log, including a --retry-full on the same SHA.
    log = logs / f"full-{sha}-{1 + len(list(logs.glob(f'full-{sha}-*.log')))}.log"
    with tempfile.TemporaryDirectory(prefix="office-batch-") as temporary:
        checkout = Path(temporary) / "checkout"
        _git(repo, "worktree", "add", "--detach", str(checkout), sha)
        try:
            code, output = _execute(argv, checkout, manifest["timeout_seconds"])
            log.write_text(f"batch {manifest['id']}\ncommit {sha}\ncommand {argv!r}\n\n" + output, encoding="utf-8")
        finally:
            _git(repo, "worktree", "remove", "--force", str(checkout))
    return code, log


def _plain(value):
    """A snapshot as JSON would store it, so a stored and a fresh snapshot compare equal."""
    return json.loads(json.dumps(value))


def _run_snapshots(con, entries: list[dict]) -> dict[str, dict]:
    return {e["lane"]["id"]: _plain(land._snapshot(con, e["run"])) for e in entries if e["run"] is not None}


def _validate(repo: Path, record: dict, manifest: dict, record_path: Path, con, entries: list[dict]) -> None:
    """Run the full suite on the composed SHA. Nothing merges unless it passes."""
    base, sha = manifest["base"], record["composed"]
    before = _run_snapshots(con, entries)
    code, log = _run_full(repo, sha, manifest, record_path.parent)
    _git(repo, "fetch", "-q", "origin", base)
    moved = _git(repo, "rev-parse", f"origin/{base}") != record["start"]
    if moved or _run_snapshots(con, entries) != before:
        # The composition was built on older state. Attest nothing about the new state.
        record.update(status="pending", validated=None)
        _save(record_path, record)
        what = f"{base} moved" if moved else "a run's integration changed"
        raise Refused("batch-stale", f"{what} while the full suite ran on {sha[:12]}; the composition is stale "
                                     "and nothing merged",
                      preserved="no PRs merged by this attempt",
                      next_step="rerun office land --batch to recompose and revalidate")
    record.update(run_snapshots=before, validated={"sha": sha, "exit_code": code, "log": str(log)},
                  status="validated" if code == 0 else "red")
    _save(record_path, record)
    if code:
        raise Refused("batch-full-suite-red", f"full suite exited {code} on {sha[:12]}; log: {log}",
                      preserved="no PRs merged by this attempt",
                      next_step="append an ordered fix PR lane (land_after), then rerun office land --batch; "
                                "or --retry-full for a transient failure")


def _compose_and_validate(repo: Path, record: dict, record_path: Path, manifest: dict,
                          entries: list[dict], infos: dict[int, dict], current: str, con) -> None:
    record.update(start=current, lanes=manifest["lanes"], completed=[], last_head=None, validated=None)
    composed, heads, trees = _compose(repo, current, entries, infos)
    _verify_version(repo, current, composed, manifest)
    record.update(composed=composed, heads=heads, trees=trees)
    _validate(repo, record, manifest, record_path, con, entries)


def _merge_lanes(con, repo: Path, record: dict, record_path: Path, manifest: dict,
                 entries: list[dict], infos: dict[int, dict], result: Result) -> None:
    """Merge the validated lanes in order. After each lane, the default branch must carry that lane's composed tree."""
    base, method = manifest["base"], manifest["merge_method"]
    completed = set(record["completed"])

    def preserved() -> str:
        return f"merged lanes: {', '.join(record['completed']) or 'none'}"

    def main_head() -> str:
        _git(repo, "fetch", "-q", "origin", base)
        return _git(repo, "rev-parse", f"origin/{base}")

    for entry in entries:
        ident = entry["lane"]["id"]
        if ident in completed:
            result.add(f"{ident}: already merged; skipped")
            continue
        expected = record.get("last_head") or record["start"]
        observed = main_head()
        if observed != expected:
            raise Refused("batch-main-moved",
                          f"{base} moved outside this batch ({expected[:12]} -> {observed[:12]}); stop before another merge",
                          preserved=preserved())
        for number in entry["prs"]:
            if infos[number]["headRefOid"] != record["heads"][str(number)]:
                raise Refused("batch-head-moved",
                              f"#{number} changed head after the suite validated {record['composed'][:12]}",
                              preserved=preserved())

        def merge(number: int) -> None:
            _merge_pr(repo, number, infos[number], method, base, record.get("last_head") or record["start"])
            # Record main after every PR, so a restart inside a multi-PR lane resumes instead of refusing.
            record["last_head"] = main_head()
            _save(record_path, record)

        try:
            if entry["run"]:
                run = entry["run"]
                with land._land_lock(run):
                    if _plain(land._snapshot(con, run)) != record["run_snapshots"][ident]:
                        raise Refused("integration-changed",
                                      f"{ident}: the run's integration changed after the suite validated it",
                                      preserved=preserved())
                    for number in entry["prs"]:
                        merge(number)
                        _mark_task_merged(con, run, entry["tasks"][number], number, base, method)
                        result.add(f"{ident}: #{number} merged ({method})")
            else:
                for number in entry["prs"]:
                    merge(number)
                    result.add(f"{ident}: #{number} merged")
        except (Refused, subprocess.TimeoutExpired) as exc:
            reason = getattr(exc, "reason", str(exc))
            record["failed_lane"] = {"id": ident, "reason": str(exc)}
            record["status"] = "blocked"
            _save(record_path, record)
            raise Refused("batch-lane-failed", f"{ident}: {reason}: {exc}",
                          preserved=preserved(),
                          next_step=f"fix {ident}, then rerun office land --batch with the same manifest") from exc
        if _git(repo, "rev-parse", f"{record['last_head']}^{{tree}}") != record["trees"][ident]:
            record.update(status="blocked", failed_lane={"id": ident, "reason": "tree"})
            _save(record_path, record)
            raise Refused("batch-tree-mismatch",
                          f"{base} after lane {ident} does not carry the suite-validated tree",
                          preserved=preserved(),
                          next_step="reconcile the default branch by hand, then rerun office land --batch")
        record["completed"].append(ident)
        record["status"] = "merging"
        completed.add(ident)
        _save(record_path, record)
    final = record["last_head"]
    _record_run_landings(con, entries, record, final)
    record.update(status="green", landed_head=final)
    _save(record_path, record)
    result.add(f"full suite PASS on {record['composed'][:12]} before any merge; {base} is {final[:12]} "
               "with the validated tree")
    result.add(f"log: {record['validated']['log']}")
    result.next = "batch is green; review release and perform closeout"
    result.data = {"batch": record}


def _load_record(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused("batch-record-corrupt", f"batch record {path} is unreadable: {exc}",
                      next_step="inspect it by hand; do not delete it while batch PRs may be merged") from exc


def land_batch(manifest_file: str, *, cwd: Path, dry_run: bool = False, retry_full: bool = False) -> Result:
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-land", "a worker cannot land a batch")
    manifest = _manifest(Path(manifest_file))
    repo = Path(_git(cwd, "rev-parse", "--show-toplevel")).resolve()
    record_path = _state_path(repo, manifest["id"])
    base = manifest["base"]
    with _lock(record_path):
        _git(repo, "fetch", "-q", "origin", base)
        current = _git(repo, "rev-parse", f"origin/{base}")
        saved = _load_record(record_path)
        record = _resume(saved, manifest, current)
        if record["status"] == "green":
            landed = record["landed_head"]
            if current != landed:
                raise Refused("batch-main-moved",
                              f"{base} moved after the green batch at {landed[:12]}; this batch attests only its recorded SHA")
            return Result(lines=[f"batch {manifest['id']} already green at {landed[:12]}; skipped"],
                          data={"batch": record})
        con = None
        if any("run" in lane for lane in manifest["lanes"]):
            from office import db
            if not paths.runs_db().is_file():
                raise Refused("batch-no-recorder", "the Office runs database does not exist")
            con = db.connect()
        try:
            entries, infos = _entries(manifest, con, repo)
            if not saved and any(info["state"] == "MERGED" for info in infos.values()):
                raise Refused("batch-premerged", "cannot start a new batch containing previously merged PRs")
            completed = set(record["completed"])
            for entry in entries:
                if entry["lane"]["id"] in completed and any(infos[n]["state"] != "MERGED" for n in entry["prs"]):
                    raise Refused("batch-state-mismatch", f"{entry['lane']['id']}: recorded merge is not merged on GitHub")
            ordered = [e["lane"]["id"] for e in entries]
            owner = next(e["lane"]["id"] for e in entries if e["lane"].get("version_bump"))
            if dry_run:
                return Result(lines=[f"batch {manifest['id']}: {' -> '.join(ordered)}",
                                     f"version {manifest['version']} owned by {owner}",
                                     "dry run: would compose the lanes, run the full suite once on the composed SHA, "
                                     "then merge; no PR, branch or default-branch changes made"],
                              data={"ordered_lanes": ordered, "status": record["status"]})
            if record["status"] == "red":
                if not retry_full:
                    raise Refused("batch-red-unchanged",
                                  "last full suite failed; append an ordered fix PR lane or use --retry-full for a transient failure",
                                  preserved=f"no PRs merged; composed {record['composed'][:12]}")
                if current != record["start"]:
                    raise Refused("batch-main-moved",
                                  f"{base} moved outside this batch ({record['start'][:12]} -> {current[:12]})",
                                  preserved="no PRs merged by this batch")
                _validate(repo, record, manifest, record_path, con, entries)
            elif record["status"] == "pending":
                _compose_and_validate(repo, record, record_path, manifest, entries, infos, current, con)
            result = Result(lines=[f"batch {manifest['id']}: {' -> '.join(ordered)}"])
            _merge_lanes(con, repo, record, record_path, manifest, entries, infos, result)
            return result
        finally:
            if con is not None:
                con.close()
