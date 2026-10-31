"""Native ordered batch landing across reviewed Office runs and individual PRs.

A batch uses an operator-owned JSON manifest, never infers authorization from
a PR's description. Its one designated version owner carries the only version
change. The full-suite command executes only after every ordered lane merges,
on an isolated checkout of the final default-branch commit.

An interrupted or red batch can be resumed. Existing lanes are immutable;
extra fix PR lanes may be appended after a failure before revalidating.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
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


def _manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
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
                   "number,state,baseRefName,headRefOid,isDraft,files")
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
    if status == "OPEN" and not info.get("headRefOid"):
        raise Refused("batch-pr-head", f"#{number} has no readable head SHA")
    files = info.get("files")
    if not isinstance(files, list):
        raise Refused("batch-pr-files", f"#{number}: cannot inspect changed paths")
    info["paths"] = [f.get("path") for f in files if isinstance(f, dict)]
    return info


def _run_lane(con, lane: dict, repo: Path, base: str) -> tuple[dict, list[int]]:
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
    numbers = []
    for task in integration._topo(integration.accepted_set(con, run) or []):
        if prs.has_pr(task):
            pr = (state.get_task(con, run["id"], task["id"]) or {}).get("pr") or {}
            if not isinstance(pr.get("number"), int):
                raise Refused("batch-run-pr", f"{lane['id']}/{task['id']}: accepted task has no PR")
            numbers.append(pr["number"])
    return run, numbers


def _state_path(repo: Path, ident: str) -> Path:
    root = Path(_git(repo, "rev-parse", "--git-common-dir"))
    if not root.is_absolute():
        root = (repo / root).resolve()
    return root / "office" / "batches" / f"{ident}.json"


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.with_suffix(".tmp")
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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


def _entries(manifest: dict, con, repo: Path) -> tuple[list[dict], dict[str, dict]]:
    entries = []
    infos = {}
    seen = set()
    for lane in manifest["lanes"]:
        run = None
        if "run" in lane:
            run, numbers = _run_lane(con, lane, repo, manifest["base"])
        else:
            numbers = [lane["pr"]]
        if not numbers:
            raise Refused("batch-empty-lane", f"{lane['id']}: no PRs to land")
        for number in numbers:
            if number in seen:
                raise Refused("batch-duplicate-pr", f"#{number} appears in more than one lane")
            seen.add(number)
            infos[number] = _pr_info(repo, number, manifest["base"], allow_merged=True, stacked=run is not None)
        entries.append({"lane": lane, "run": run, "prs": numbers})
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


def _preflight_version(repo: Path, record: dict, manifest: dict, entries: list[dict], infos: dict) -> None:
    """Prove the single owner already carries the requested bump BEFORE a merge."""
    owner = next(e for e in entries if e["lane"].get("version_bump"))
    number = next(n for n in owner["prs"] if manifest["version_file"] in infos[n]["paths"])
    head = infos[number].get("headRefOid")
    if not head:
        raise Refused("batch-pr-head", f"version-owner PR #{number} has no head")
    _fetch_pr_head(repo, number, head)
    _verify_version(repo, record["start"], head, manifest)


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
        headers = re.findall(r"(?m)^##\s+(\S+)\s*$", content)
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
    if record.get("status") == "green" and len(prior) != len(supplied):
        raise Refused("batch-already-green", "a green batch cannot be extended; start a new batch")
    if record.get("status") == "green":
        return record
    if prior and len(prior) != len(supplied):
        record["status"] = "pending"
        record["validated"] = None
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


def _merge_pr(repo: Path, number: int, info: dict, method: str, base: str) -> None:
    if info["state"] == "MERGED":
        return
    land._wait_checks({"repo_root": str(repo)}, number)
    latest = _pr_info(repo, number, base, allow_merged=True)
    if latest["state"] == "MERGED":
        return
    if latest["headRefOid"] != info["headRefOid"]:
        raise Refused("batch-head-moved", f"#{number} changed head since preflight")
    proc = _github(repo, "pr", "merge", str(number), prs.MERGE_FLAGS[method],
                   "--match-head-commit", info["headRefOid"])
    if proc.returncode:
        raise Refused("batch-merge-failed", f"#{number}: {(proc.stderr or proc.stdout).strip()[:200]}")


def _run_full(repo: Path, sha: str, manifest: dict, folder: Path) -> tuple[int, Path]:
    from office import shell_safety
    argv = manifest["full_suite"]
    if shell_safety.unsafe_manual_push(" ".join(shlex.quote(arg) for arg in argv)):
        raise Refused("batch-unsafe-suite", "full_suite must not push or mutate a remote")
    log = folder / f"full-{sha[:12]}.log"
    with tempfile.TemporaryDirectory(prefix="office-batch-") as temporary:
        checkout = Path(temporary) / "checkout"
        _git(repo, "worktree", "add", "--detach", str(checkout), sha)
        try:
            try:
                proc = subprocess.run(argv, cwd=checkout, capture_output=True, text=True,
                                      timeout=manifest["timeout_seconds"])
                code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired as exc:
                code = 124
                output = f"full suite timed out after {manifest['timeout_seconds']}s\n"
                output += str(exc.stdout or "") + str(exc.stderr or "")
            log.write_text(f"batch {manifest['id']}\ncommit {sha}\ncommand {argv!r}\n\n" + output, encoding="utf-8")
        finally:
            _git(repo, "worktree", "remove", "--force", str(checkout))
    return code, log


def land_batch(manifest_file: str, *, cwd: Path, dry_run: bool = False, retry_full: bool = False) -> Result:
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-land", "a worker cannot land a batch")
    manifest = _manifest(Path(manifest_file))
    repo = Path(_git(cwd, "rev-parse", "--show-toplevel")).resolve()
    record_path = _state_path(repo, manifest["id"])
    with _lock(record_path):
        _git(repo, "fetch", "-q", "origin", manifest["base"])
        current = _git(repo, "rev-parse", f"origin/{manifest['base']}")
        saved = json.loads(record_path.read_text()) if record_path.exists() else None
        record = _resume(saved, manifest, current)
        if record["status"] == "green":
            if current != record["validated"]["sha"]:
                raise Refused("batch-main-moved",
                              f"{manifest['base']} moved after the green batch at "
                              f"{record['validated']['sha'][:12]}; this batch attests only its recorded SHA")
            return Result(lines=[f"batch {manifest['id']} already green at {record['validated']['sha'][:12]}; skipped"],
                          data={"batch": record})
        con = None
        if any("run" in lane for lane in manifest["lanes"]):
            from office import db
            if not paths.runs_db().is_file():
                raise Refused("batch-no-recorder", "the Office runs database does not exist")
            con = db.connect()
        try:
            entries, infos = _entries(manifest, con, repo)
            completed = set(record["completed"])
            if not saved and any(info["state"] == "MERGED" for info in infos.values()):
                raise Refused("batch-premerged", "cannot start a new batch containing previously merged PRs")
            # Fail closed before a partial batch if the release bump or command
            # itself is invalid. Do not discover those problems after merging.
            from office import shell_safety
            if shell_safety.unsafe_manual_push(" ".join(shlex.quote(arg) for arg in manifest["full_suite"])):
                raise Refused("batch-unsafe-suite", "full_suite must not push or mutate a remote")
            _preflight_version(repo, record, manifest, entries, infos)
            # Already-completed lanes must still be merged on the requested base.
            for entry in entries:
                if entry["lane"]["id"] in completed and any(infos[n]["state"] != "MERGED" for n in entry["prs"]):
                    raise Refused("batch-state-mismatch", f"{entry['lane']['id']}: recorded merge is not merged on GitHub")
            ordered = [e["lane"]["id"] for e in entries]
            if dry_run:
                return Result(lines=[f"batch {manifest['id']}: {' -> '.join(ordered)}",
                                     f"version {manifest['version']} owned by "
                                     f"{next(e['lane']['id'] for e in entries if e['lane'].get('version_bump'))}",
                                     "full suite deferred until every lane merges; no changes made"],
                              data={"ordered_lanes": ordered, "status": record["status"]})
            if record.get("status") == "red" and len(record.get("lanes", [])) == len(entries) and not retry_full:
                raise Refused("batch-red-unchanged",
                              "last full suite failed; append an ordered fix PR lane or use --retry-full for a transient failure",
                              preserved=f"merged lanes: {', '.join(record['completed'])}")
            if saved and current != (record.get("last_head") or record["start"]):
                # An operator cannot silently absorb unrelated default-branch writes.
                expected = record.get("last_head") or record["start"]
                raise Refused("batch-main-moved",
                              f"{manifest['base']} moved outside this batch ({expected[:12]} -> {current[:12]})",
                              preserved=f"merged lanes: {', '.join(record['completed'])}")
            record["lanes"] = manifest["lanes"]
            _save(record_path, record)
            result = Result(lines=[f"batch {manifest['id']}: {' -> '.join(ordered)}"])
            for entry in entries:
                _git(repo, "fetch", "-q", "origin", manifest["base"])
                observed = _git(repo, "rev-parse", f"origin/{manifest['base']}")
                expected = record.get("last_head") or record["start"]
                if observed != expected:
                    raise Refused("batch-main-moved",
                                  f"{manifest['base']} moved between lanes "
                                  f"({expected[:12]} -> {observed[:12]}); stop before another merge",
                                  preserved=f"merged lanes: {', '.join(record['completed']) or 'none'}")
                lane = entry["lane"]
                ident = lane["id"]
                if ident in completed:
                    result.add(f"{ident}: already merged; skipped")
                    continue
                try:
                    if entry["run"]:
                        run = entry["run"]
                        snap = land._snapshot(con, run)
                        with land._land_lock(run):
                            land._require_unchanged(con, run, snap, "before batch merge")
                            land._merge_all(con, run, result)
                    else:
                        for number in entry["prs"]:
                            _merge_pr(repo, number, infos[number], manifest["merge_method"], manifest["base"])
                            result.add(f"{ident}: #{number} merged")
                    _git(repo, "fetch", "-q", "origin", manifest["base"])
                    current = _git(repo, "rev-parse", f"origin/{manifest['base']}")
                    record["completed"].append(ident)
                    record["last_head"] = current
                    record["status"] = "merging"
                    completed.add(ident)
                    _save(record_path, record)
                except (Refused, subprocess.TimeoutExpired) as exc:
                    reason = getattr(exc, "reason", str(exc))
                    record["failed_lane"] = {"id": ident, "reason": str(exc)}
                    record["status"] = "blocked"
                    _save(record_path, record)
                    raise Refused("batch-lane-failed", f"{ident}: {reason}: {exc}",
                                  preserved=f"merged lanes: {', '.join(record['completed']) or 'none'}",
                                  next_step=f"fix {ident}, then office land --batch {manifest_file}") from exc
            # The version owner must have produced the sole release bump.
            _verify_version(repo, record["start"], current, manifest)
            if record.get("validated") and record["validated"].get("sha") == current and record["validated"].get("exit_code") == 0:
                _record_run_landings(con, entries, record, current)
                record["status"] = "green"
                _save(record_path, record)
                return Result(lines=[f"batch {manifest['id']} already validated on {current[:12]}"],
                              data={"batch": record})
            code, log = _run_full(repo, current, manifest, record_path.parent)
            # A full-suite PASS attests one SHA, not an unrelated main push
            # that landed during the (potentially hours-long) validation.
            if code == 0:
                _git(repo, "fetch", "-q", "origin", manifest["base"])
                observed = _git(repo, "rev-parse", f"origin/{manifest['base']}")
                if observed != current:
                    record["status"] = "blocked"
                    record["failed_lane"] = {"id": "post-suite", "reason": "main moved while full suite ran"}
                    _save(record_path, record)
                    raise Refused("batch-main-moved",
                                  f"full suite passed {current[:12]} but {manifest['base']} is now {observed[:12]}; "
                                  "do not treat the new head as validated")
            record["validated"] = {"sha": current, "exit_code": code, "log": str(log)}
            record["status"] = "validated" if code == 0 else "red"
            _save(record_path, record)
            if code:
                raise Refused("batch-full-suite-red",
                              f"full suite exited {code} on {current[:12]}; log: {log}",
                              preserved=f"all {len(record['completed'])} lanes merged; no further merge or deploy",
                              next_step="add a fix PR lane with land_after ordering, then rerun office land --batch")
            # A crash while recording a run's closeout leaves a recoverable
            # validated state, not a false green without the run receipts.
            _record_run_landings(con, entries, record, current)
            record["status"] = "green"
            _save(record_path, record)
            result.add(f"full suite PASS on {current[:12]} (once after final merge); log: {log}")
            result.next = "batch is green; review release and perform closeout"
            result.data = {"batch": record}
            return result
        finally:
            if con is not None:
                con.close()
