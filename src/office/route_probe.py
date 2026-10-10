"""Exact route conformance probe and fingerprint cache (#494).

A discovery-eligible route is a catalog row Office has no invocation receipt
for. This module proves one exact fingerprint (harness, harness version, adapter
hash, launch profile, invocation model id, effort) by launching the adapter's own
worker profile once, with a tiny prompt, a short timeout and a disposable git
worktree, then reading back the model identity, the reply and the filesystem.

Identity comes only from the harness's own invocation metadata (a header the
harness prints before any model output), never from text the model wrote, so a
silently substituted model cannot echo its way to a pass. A harness without such a
readback is not probed. The harness runs inside an OS-enforced write boundary
limited to the disposable workspace; a platform or profile without one is not
probed either. Descendants are attributed (environment tag, lineage, files held
in the workspace) and terminated individually, not only by process group.

A pass qualifies only that fingerprint. It is not adapter trust, not learned
quality and not evidence about any sibling effort or model. Nothing here writes
`adapter_trust_acts` or `recorded_overrides`, installs anything, widens a
permission or probes a denied, archived or unsupported row.

Cache semantics. Keys are exact fingerprints. A pass expires after
`probe_ttl_days`. A transient or auth/quota failure expires after at most an
hour so it can be retried. A conformance or isolation failure keeps its own
reason class and expires after `probe_ttl_days`; it says the harness path did
not prove out, never that the model is weak. A confirmed `unsupported-model-effort`
negative does not expire by time for the same exact fingerprint: only a material
fingerprint change lifts it.

State lives in the T1 tables. `route_probes` is a mutable fingerprint cache,
`route_probe_reservations` is the atomic allocation, and every attempt appends
immutable `route_discovery_events` in the transaction that changes state.
"""
from __future__ import annotations

import errno
import fcntl
import functools
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from office import adapters, db, paths, route_policy
from office.util import (ALIVE, DEAD, UNKNOWN, atomic_write_json, now_iso, parse_iso, process_is, process_start,
                         sha256_file)

PROFILE = "worker"
REASON_CLASSES = ("unsupported-model-effort", "auth-quota-blocked", "transient", "isolation-missing",
                  "conformance-failed")
BUILDER_ROLES = ("executor", "worker")
ROLLING_WINDOW = 20
GRACE_S = 30  # past probe_timeout_s, a reservation that is still open is expired
TEMPORARY_TTL = timedelta(hours=1)  # transient and auth/quota failures are retried sooner than probe_ttl_days
DETAIL_LIMIT = 600
# Identity that would let a probe child act as an Office run.
IDENTITY_ENV = ("OFFICE_RUN_ID", "OFFICE_TASK_ID", "OFFICE_DISPATCH_ID", "OFFICE_ROLE", "OFFICE_STATE_DIR",
                "OFFICE_SESSION", "OFFICE_HARNESS", "OFFICE_VERSION", "OFFICE_FRONT_DOOR_HOPS")
MANUAL_REASON = "manual: office doctor --probe-route"
NONCE_ENV = "OFFICE_PROBE_NONCE"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Extra paths a probe may write. Empty in production: only tests (whose fake harness logs launches) widen it.
_EXTRA_WRITABLE: tuple[str, ...] = ()


@dataclass(frozen=True)
class Refused:
    """A probe that was not allocated. Equality is on `reason` only, so callers
    and tests can compare against `Refused("probe-cap")`."""
    reason: str
    detail: str = field(default="", compare=False)
    allocation: dict | None = field(default=None, compare=False)
    attempt_id: str | None = field(default=None, compare=False)

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


# ------------------------------------------------------------------ fingerprint

def fingerprint(cand: dict, adapter: dict | None, profile: str = PROFILE) -> dict:
    """The exact invocation the probe proves. A part that cannot be read is
    recorded as `unknown`, which `reserve` refuses to probe."""
    return {
        "harness": cand.get("harness") or "unknown",
        "harness_version": cand.get("harness_version") or "unknown",
        "adapter_hash": (adapters.adapter_hash(adapter) if adapter else cand.get("adapter_hash")) or "unknown",
        "profile": profile,
        "invocation_model_id": cand.get("invocation_model_id") or cand.get("model_id") or "unknown",
        "effort": cand.get("effort") or "none",
    }


def key(cand: dict, adapter: dict | None, profile: str = PROFILE) -> str:
    """Cache key. Same shape as `conformance.key`: changing any part is a new route."""
    fp = fingerprint(cand, adapter, profile)
    return "|".join(fp[k] for k in ("harness", "harness_version", "invocation_model_id", "effort", "adapter_hash",
                                    "profile"))


def _adapter(cand: dict, adapter: dict | None = None) -> dict | None:
    if adapter is not None:
        return adapter
    return adapters.load_all().get(cand.get("adapter_id") or cand.get("harness"))


def _route_id(cand: dict) -> str:
    from office import routing
    return routing.candidate_id(cand)


# ------------------------------------------------------------------ cache

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parsed(value: str | None) -> datetime | None:
    try:
        dt = parse_iso(value) if value else None
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _cache_row(con, probe_key: str) -> dict | None:
    row = con.execute("SELECT * FROM route_probes WHERE key=?", (probe_key,)).fetchone()
    return dict(row) if row else None


def _is_fresh(rec: dict, ttl_days: float, timeout_s: float, now: datetime) -> bool:
    """Whether a cache row still speaks for its exact fingerprint.

    A pass, a conformance or isolation failure and (more briefly) a transient or
    auth/quota failure expire. A confirmed `unsupported-model-effort` negative
    never expires by time: only a material fingerprint change (harness version,
    adapter hash, profile, model or effort) is a new key and so lifts it."""
    if rec.get("result") == "fail" and rec.get("reason_class") == "unsupported-model-effort":
        return True
    at = _parsed(rec.get("probed_at"))
    if at is None:
        return False
    age = now - at
    if rec.get("result") == "pending":
        return age <= timedelta(seconds=timeout_s + GRACE_S)
    if rec.get("result") == "fail" and rec.get("reason_class") in ("transient", "auth-quota-blocked"):
        return age <= min(timedelta(days=ttl_days), TEMPORARY_TTL)
    return age <= timedelta(days=ttl_days)


def status(con, cand: dict, *, adapter: dict | None = None, ttl_days: float | None = None,
           timeout_s: float | None = None, now: datetime | None = None) -> dict | None:
    """The fresh probe record for this exact fingerprint, or None. A record of a
    sibling effort, model, harness version, adapter or profile is never returned."""
    ad = _adapter(cand, adapter)
    fp = fingerprint(cand, ad)
    if "unknown" in (fp["harness_version"], fp["adapter_hash"]):
        return None
    probe_key = key(cand, ad)
    rec = _cache_row(con, probe_key)
    if rec is None:
        return None
    defaults = route_policy.DISCOVERY_DEFAULTS
    if not _is_fresh(rec, ttl_days if ttl_days is not None else defaults["probe_ttl_days"],
                     timeout_s if timeout_s is not None else defaults["probe_timeout_s"], now or _utcnow()):
        return None
    return {**rec, "fresh": True}


# ------------------------------------------------------------------ allocation

def rolling(con, settings: dict) -> dict:
    """Trials among the last 20 pooled executor/worker dispatch decisions.

    The decision being made counts in the denominator (`window` is the recorded
    decisions plus this one, at most 20). A trial needs `used + 1` of `window` to
    stay within the percentage, so a cold history blocks trials until enough real
    decisions exist: at 15 percent, six recorded decisions. Nothing is invented
    to open the window; `warmup` says when this is what blocks.
    """
    percent = float(settings["max_trial_percent_rolling_20"])
    try:
        rows = con.execute("SELECT created_at FROM route_audit WHERE role IN ('executor','worker') "
                           "AND phase IN ('dispatch','reroute') ORDER BY created_at DESC, rowid DESC LIMIT ?",
                           (ROLLING_WINDOW,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    since = rows[-1][0] if rows else None
    query = "SELECT COUNT(*) FROM route_trials WHERE role IN ('executor','worker')"
    args: tuple = ()
    if since is not None:
        query += " AND created_at >= ?"
        args = (since,)
    used = con.execute(query, args).fetchone()[0]
    window = min(ROLLING_WINDOW, len(rows) + 1)
    cap = int(math.floor(window * percent / 100.0 + 1e-9))
    return {"used": used, "max": cap, "window": window, "percent": percent,
            "warmup": cap == 0 and percent > 0}


def allocation(con, run_id: str | None, settings: dict) -> dict:
    """Probe, trial and rolling cap snapshot. Pure reads, so a router can use it as a request input."""
    probes = con.execute("SELECT COUNT(*) FROM route_probe_reservations WHERE run_id=?", (run_id,)).fetchone()[0] \
        if run_id else 0
    trials = con.execute("SELECT COUNT(*) FROM route_trials WHERE run_id=?", (run_id,)).fetchone()[0] \
        if run_id else 0
    return {"probes": {"used": probes, "max": int(settings["max_probes_per_run"])},
            "trials": {"used": trials, "max": int(settings["max_trials_per_run"])},
            "rolling": rolling(con, settings)}


# ------------------------------------------------------------------ context

def _config(run: dict | None, ctx: dict) -> dict:
    if ctx.get("config") is not None:
        return ctx["config"]
    if run is not None:
        return run.get("policy") or {}
    from office import config as cfg
    return cfg.resolve(None)[0]


def _settings(config: dict, budget) -> dict:
    settings = route_policy.discovery_settings(config)
    if isinstance(budget, dict):
        settings.update({k: v for k, v in budget.items() if k in settings})
    elif isinstance(budget, int) and not isinstance(budget, bool):
        settings["max_probes_per_run"] = budget
    return settings


def _prepare(run: dict | None, context: dict | None, budget=None) -> tuple[dict, dict, dict]:
    ctx = dict(context or {})
    ctx.setdefault("origin", "preflight" if run else "manual")
    if ctx["origin"] not in route_policy.EVENT_ORIGINS:
        raise ValueError(f"unknown probe origin {ctx['origin']!r}")
    config = _config(run, ctx)
    ctx.setdefault("policy_digest", config.get(route_policy.DIGEST_KEY) or route_policy.policy_digest(config))
    if not ctx.get("reason"):
        ctx["reason"] = MANUAL_REASON if ctx["origin"] == "manual" else f"{ctx['origin']}: exact route conformance probe"
    if run is not None:
        ctx.setdefault("plan_version", run.get("plan_version"))
    return ctx, config, _settings(config, budget)


def _event(con, kind: str, *, attempt_id: str, ctx: dict, run: dict | None, cand: dict, fp: dict, probe_key: str,
           alloc: dict, freshness: str, dispatch_id: str | None = None, **extra) -> str:
    return route_policy.record_event(
        con, kind=kind, attempt_id=attempt_id, origin=ctx["origin"], policy_digest=ctx["policy_digest"],
        probe_key=probe_key, reason=ctx["reason"], run_id=run["id"] if run else None,
        plan_version=ctx.get("plan_version") if run else None, task_id=ctx.get("task_id"),
        dispatch_id=dispatch_id or ctx.get("dispatch_id"), role=ctx.get("role"), fingerprint_json=fp,
        candidate_route=_route_id(cand), primary_route=ctx.get("primary_route"),
        fallback_route=ctx.get("fallback_route"), probe_freshness=freshness, allocation_json=alloc, **extra)


# ------------------------------------------------------------------ catalog gates

def _archived(harness: str, model: str, effort: str | None) -> bool:
    for path in sorted((paths.resources_root() / "catalog" / "archive").glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        for row in data.get("models") or []:
            if (row.get("invocation_harness") == harness and effort in (None, row.get("effort"))
                    and model in (row.get("model_id"), row.get("invocation_model_id"))):
                return True
    return False


def catalog_row(cand: dict) -> dict | None:
    """The active catalog row for this exact harness, model and effort. The
    catalog, not the caller's candidate dict, says what a route is allowed to be."""
    from office import candidates
    harness, effort = cand.get("harness"), cand.get("effort")
    rows = [r for r in candidates.catalog_rows() if r.get("invocation_harness") == harness
            and r.get("effort") == effort
            and cand.get("model_id") in (r.get("model_id"), r.get("invocation_model_id"))]
    rows.sort(key=lambda r: r.get("model_id") != cand.get("model_id"))
    return rows[0] if rows else None


def row_state(row: dict) -> dict:
    """`route_policy.row_status` for a catalog row. An alias row inherits its exact
    target's status, so `haiku` is as probe-able as the concrete model it resolves to."""
    target_name = row.get("alias_resolved_to")
    if target_name:
        from office import candidates
        for target in candidates.catalog_rows():
            if (target.get("model_id") == target_name and target.get("invocation_harness") == row.get("invocation_harness")
                    and target.get("effort") == row.get("effort") and not target.get("alias_family")):
                return route_policy.alias_status(row, target)
    return route_policy.row_status(row)


def _denial(cand: dict, run: dict | None, config: dict) -> tuple[str, str] | None:
    """A denial in the pinned config or in the live user and repo policy (the path
    `route_role` and `declared_candidate` read), so a denial written after `office start`
    stops a probe too. A live policy that cannot be read refuses: it might deny this route."""
    from office import candidates
    try:
        live = candidates.live_user_policies((run or {}).get("repo_root"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return "policy-unreadable", f"the live user or repo policy cannot be read ({exc}), so a denial cannot be ruled out"
    for policy in (route_policy.user_policy(config), *live):
        denied = route_policy.is_denied(cand, policy)
        if denied:
            return "route-denied", denied
    return None


def _static_refusal(cand: dict, adapter: dict | None, fp: dict, ctx: dict, config: dict,
                    run: dict | None = None) -> tuple[str, str] | None:
    """Refusals that need no database state: denied, archived, unsupported, not installed."""
    denial = _denial(cand, run, config)
    if denial:
        return denial
    row = catalog_row(cand)
    if row is None:
        if _archived(cand.get("harness") or "", cand.get("model_id") or "", cand.get("effort")):
            return "archived", "archived catalog rows never return as routes"
        return "unknown-route", "no active catalog row names this harness, model and effort"
    state = row_state(row)
    if state["status"] != "available" and not state["discovery_eligible"]:
        # Fail closed, but say only what is known: no discovery eligibility (or no invocation id) was
        # declared. That is not evidence the model or effort is unsupported; only a probe can show that.
        return "not-eligible", f"not explicitly discovery-eligible in the catalog ({state['reason']})"
    if state["status"] == "available" and ctx["origin"] != "manual":
        return "already-available", "this route is dispatchable and needs no discovery"
    if adapter is None or not adapters.profile(adapter, fp["profile"]):
        return "no-profile", f"adapter has no {fp['profile']} launch profile"
    if not adapters.installed(adapter):
        return "not-installed", f"{fp['harness']} is not installed"
    if "unknown" in (fp["harness_version"], fp["adapter_hash"]):
        return "no-fingerprint", "the harness version cannot be read, so no exact fingerprint exists"
    if adapter.get("id") not in _IDENTITY_READBACK:
        return "no-identity-readback", (f"{adapter.get('id')} exposes no harness-reported model and effort in its "
                                        f"{fp['profile']} output, so an exact probe cannot be authoritative")
    unavailable = write_boundary_reason()
    if unavailable:
        return "no-write-boundary", unavailable
    return None


def _quota_refusal(cand: dict, config: dict) -> str | None:
    quota = cand.get("quota") or {}
    remaining = quota.get("tightest_remaining_percent")
    if quota.get("status") != "ok" or remaining is None:
        return None
    reserve = float((config.get("quota") or {}).get("reserve_percent", 5.0))
    burn = float(quota.get("projected_burn_percent") or 0)
    if float(remaining) - burn < reserve:
        return f"quota {float(remaining):g}% left would cross the {reserve:g}% protected reserve"
    return None


# ------------------------------------------------------------------ ownership

_HELD: dict[str, int] = {}


def _lock_dir() -> Path:
    return paths.state_home() / "route-probes"


def _lock_path(attempt_id: str) -> Path:
    return _lock_dir() / f"{attempt_id}.lock"


def _sidecar(attempt_id: str) -> Path:
    return _lock_dir() / f"{attempt_id}.json"


def _acquire(attempt_id: str) -> None:
    """The owner's proof of life: an flock held until the attempt ends, released by the OS if it dies."""
    _lock_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(_lock_path(attempt_id), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    _HELD[attempt_id] = fd


def _release(attempt_id: str, *, forget: bool = True) -> None:
    fd = _HELD.pop(attempt_id, None)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    if forget:
        for path in (_lock_path(attempt_id), _sidecar(attempt_id)):
            try:
                path.unlink()
            except OSError:
                pass


def _owner_state(attempt_id: str, reserved_at: datetime | None, timeout_s: float, now: datetime) -> tuple[str, str]:
    try:
        fd = os.open(_lock_path(attempt_id), os.O_RDONLY)
    except FileNotFoundError:
        # No lock file: the owner never got as far as creating it or the state was cleared.
        if reserved_at is not None and now - reserved_at > timedelta(seconds=timeout_s + GRACE_S):
            return DEAD, "its probe lock file is missing and the reservation is past its deadline"
        return UNKNOWN, "its probe lock file is missing"
    except OSError as exc:
        return UNKNOWN, f"its probe lock cannot be opened ({exc.strerror})"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return ALIVE, "a live process holds its probe lock"
        return UNKNOWN, f"its probe lock cannot be probed ({exc.strerror})"
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return DEAD, "no process holds its probe lock"
    finally:
        os.close(fd)


def _group_alive(pgid: int) -> bool:
    if pgid <= 1:  # 0 would address this process's own group
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# macOS ps shows the environment with -E (its -e is a no-op); procps takes the BSD-style `e` letter.
_PS_ENV_FLAGS = "-axEww" if sys.platform == "darwin" else "axeww"
_PS_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\w{3}\s+\w+\s+\w+\s+[\d:]{8}\s+\d{4})\s+(.*)$")


def _tool(name: str, *system: str) -> str:
    """A system tool by absolute path, so a restricted PATH cannot hide it."""
    return next((p for p in system if os.access(p, os.X_OK)), shutil.which(name) or name)


def _process_table() -> list[tuple[int, int, str, str]] | None:
    """(pid, ppid, start time, command and, where the OS shows it, environment) for
    every process, or None when it cannot be read."""
    try:
        listing = subprocess.run([_tool("ps", "/bin/ps", "/usr/bin/ps"), _PS_ENV_FLAGS, "-o", "pid=,ppid=,lstart=,command="], capture_output=True,
                                 text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [(int(m.group(1)), int(m.group(2)), m.group(3), m.group(4))
            for m in map(_PS_LINE.match, listing.splitlines()) if m]


def _workspace_pids(base: Path) -> set[int] | None:
    """Processes holding any file or directory (including their cwd) under the
    probe's disposable base. Nothing else knows that random path, so a holder is
    the probe's. None when the holders cannot be listed."""
    try:
        proc = subprocess.run([_tool("lsof", "/usr/sbin/lsof", "/usr/bin/lsof"), "-nP", "-F", "p", "+D", str(base)], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):  # 1 is lsof's "nothing found"
        return None
    return {int(line[1:]) for line in proc.stdout.splitlines() if line[:1] == "p" and line[1:].isdigit()}


class _Tracker:
    """Which processes belong to one probe attempt.

    A process group alone is not enough: a descendant that calls setsid leaves it.
    So a process is the probe's when any of these holds, checked afresh each scan:
    it carries the attempt's nonce in its environment (where the OS shows
    environments), it descends from the leader or from a process seen to descend
    from it (identified by pid and start time, so a reused pid is not one), or it
    holds a file or its cwd under the probe's disposable workspace."""

    def __init__(self, nonce: str, base: Path | None) -> None:
        self.nonce, self.base = nonce, base
        self.seen: dict[int, str] = {}

    def observe(self, root: int | None) -> set[int] | None:
        table = _process_table()
        if table is None:
            return None
        tag = re.compile(rf"(?:^|\s){NONCE_ENV}={re.escape(self.nonce)}(?:\s|$)")
        children: dict[int, list[int]] = {}
        started = {}
        found: set[int] = set()
        for pid, ppid, start, command in table:
            children.setdefault(ppid, []).append(pid)
            started[pid] = start
            if tag.search(command):
                found.add(pid)
        queue = [pid for pid, start in self.seen.items() if started.get(pid) == start]
        if root:
            queue.append(root)
        while queue:
            pid = queue.pop()
            if pid not in found:
                found.add(pid)
                queue.extend(children.get(pid, []))
        found = {pid for pid in found if pid in started}
        self.seen.update({pid: started[pid] for pid in found})
        return found

    def scan(self, root: int | None) -> set[int] | None:
        found = self.observe(root)
        if found is None:
            return None
        if self.base is not None:
            held = _workspace_pids(self.base)
            if held is None:
                return None
            found |= held
        return {pid for pid in found if pid > 1 and pid != os.getpid()}


def _terminate_tree(pgid: int, tracker: _Tracker, *, proc: subprocess.Popen | None = None, seed: bool = True,
                    term_wait: float = 3.0, kill_wait: float = 3.0) -> bool:
    """Stop the probe's process group and every process the tracker attributes to
    it, including ones that left the group, and confirm none is left."""

    def left() -> set[int] | None:
        if proc is not None:
            proc.poll()  # reap the leader so a zombie does not count as alive
        # A reaped leader's pid may be reused, so it only seeds the tree while it is still ours.
        return tracker.scan(pgid if seed and (proc is None or proc.returncode is None) else None)

    for sig, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, kill_wait)):
        pids = left()
        if pids is None:
            return False
        if not pids and not _group_alive(pgid):
            return True
        if pgid > 1:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(0.1)
            pids = left()
            if pids is None:
                return False
            if not pids and not _group_alive(pgid):
                return True
    pids = left()
    return pids is not None and not pids and not _group_alive(pgid)


def _probe_base(value) -> Path | None:
    """A recorded workspace base, only if it is a probe workspace directly under the
    temp directory. The sidecar is read back by another process and its base is
    handed to a process-killing scan, so a path that is not one is ignored."""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    try:
        ok = path.name.startswith("office-probe-") and path.parent.resolve() == Path(tempfile.gettempdir()).resolve()
    except OSError:
        return None
    return path if ok else None


def _kill_recorded_children(attempt_id: str) -> str | None:
    """Stop the probe processes another (dead or overdue) owner recorded: the
    process group when its start time proves the pid is still that process, and
    every process the recorded nonce or workspace attributes to the attempt."""
    try:
        rec = json.loads(_sidecar(attempt_id).read_text())
    except (OSError, ValueError):
        return None
    pid, nonce = rec.get("pid"), rec.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return None
    base = _probe_base(rec.get("base"))
    ours = isinstance(pid, int) and pid > 1 and pid != os.getpid() and process_is(pid, rec.get("start"))
    gone = _terminate_tree(pid if ours else 0, _Tracker(nonce, base), seed=ours)
    if not ours and pid:
        return (f"recorded probe pid {pid} is no longer that process; "
                + ("attributable descendants stopped" if gone else "attributable descendants did not exit"))
    return f"terminated probe process group {pid}" if gone else f"probe process group {pid} did not exit"


def candidate_from_spec(spec: str) -> dict:
    """The candidate `harness/model@effort` names, built from its active catalog
    row. Unlike `candidates.build_candidates` this keeps rows that are not
    dispatchable, because probing them is what discovery is for. The catalog gates
    in `reserve` still decide whether the route may be probed."""
    from office import candidates
    from office.state import Usage
    want = candidates.parse_route_override(spec)
    if not (want["harness"] and want["model_id"] and want["effort"]):
        raise Usage("invalid-route", f"--probe-route {spec!r}: expected <harness>/<model>@<effort>",
                    next_step="office doctor --probe-route codex/gpt-6.1-sol@high")
    adapter = adapters.load_all().get(want["harness"])
    if adapter is None:
        raise Usage("unknown-harness", f"--probe-route names harness {want['harness']!r}, which has no adapter",
                    next_step="use one of: " + ", ".join(sorted(adapters.load_all())))
    cand = {"harness": want["harness"], "model_id": want["model_id"], "effort": want["effort"]}
    row = catalog_row(cand)
    if row is None:
        if _archived(cand["harness"], cand["model_id"], cand["effort"]):
            raise Usage("route-archived", f"{spec} is an archived catalog row; archived rows never return as routes")
        raise Usage("unknown-route", f"no active catalog row names {spec}",
                    next_step="office inspect route <task|role> lists the routes Office knows")
    state = row_state(row)
    return {
        "harness": want["harness"], "harness_version": adapters.harness_version(adapter) or "unknown",
        "model_id": row.get("model_id"), "invocation_model_id": row.get("invocation_model_id") or row.get("model_id"),
        "invocation_source": row.get("invocation_source"), "effort": row.get("effort"),
        "alias_resolved_to": row.get("alias_resolved_to"),
        "benchmark_indexes": row.get("benchmark_indexes") or {}, "task_benchmarks": row.get("task_benchmarks") or [],
        "capabilities": sorted(set(adapter.get("capabilities") or [])), "adapter_id": adapter.get("id"),
        "adapter_hash": adapters.adapter_hash(adapter), "cost": {"money_estimate": None},
        "price_fields": row.get("price_fields") or {}, "speed_fields": row.get("speed_fields") or {},
        "quota": {"status": "unknown", "tightest_remaining_percent": None},
        "route_status": state["status"], "status_reason": state["reason"], "discovery": state["discovery_eligible"],
    }


# ------------------------------------------------------------------ reserve

def _inflight(con, probe_key: str) -> dict | None:
    row = con.execute("SELECT * FROM route_probe_reservations WHERE probe_key=? AND status='reserved'",
                      (probe_key,)).fetchone()
    return dict(row) if row else None


def reserve(con, run: dict | None, cand: dict, *, attempt_id: str, context: dict | None = None,
            dispatch_id: str | None = None, budget=None, adapter: dict | None = None) -> dict | Refused:
    """`_reserve` that answers lock contention with a refusal instead of an exception.
    `db.transaction` already waits out a busy writer; a lock that outlasts that wait
    means nothing was reserved, so nothing may launch."""
    try:
        return _reserve(con, run, cand, attempt_id=attempt_id, context=context, dispatch_id=dispatch_id,
                        budget=budget, adapter=adapter)
    except sqlite3.OperationalError as exc:
        if not any(w in str(exc) for w in ("locked", "busy")):
            raise
        return Refused("db-busy", f"runs.db stayed locked, so no probe was reserved: {exc}", None, attempt_id)


def _reserve(con, run: dict | None, cand: dict, *, attempt_id: str, context: dict | None = None,
             dispatch_id: str | None = None, budget=None, adapter: dict | None = None) -> dict | Refused:
    """Atomically allocate one probe, or explain why not.

    One `db.transaction`: static gates, a fresh cached record, the fingerprint's
    in-flight reservation and the per-run cap are checked and, on allocation, the
    `reserved` row and the pending cache row are written. Every branch appends
    one `probe-reserved`, `probe-cache-hit` or `probe-refused` event in that same
    transaction. `run` is None only for an unbound manual probe. Returns a
    reservation (`reserved: True`), the fresh cached record (`cached: True`), or
    `Refused`.
    """
    ctx, config, settings = _prepare(run, context, budget)
    ad = _adapter(cand, adapter)
    fp = fingerprint(cand, ad)
    probe_key = key(cand, ad)
    run_id = run["id"] if run else None
    now = _utcnow()
    held = False
    try:
        with db.transaction(con):
            alloc = allocation(con, run_id, settings)
            cached = status(con, cand, adapter=ad, ttl_days=settings["probe_ttl_days"],
                            timeout_s=settings["probe_timeout_s"], now=now)
            stale = cached is None and _cache_row(con, probe_key) is not None

            def refuse(reason: str, detail: str = "") -> Refused:
                alloc_out = {**alloc, "refused": reason if not detail else f"{reason}: {detail}"}
                _event(con, "probe-refused", attempt_id=attempt_id, ctx=ctx, run=run, cand=cand, fp=fp,
                       probe_key=probe_key, alloc=alloc_out,
                       freshness="cached-fresh" if cached else ("stale" if stale else "none"),
                       dispatch_id=dispatch_id, outcome="refused", detail=f"{reason}: {detail}" if detail else reason)
                return Refused(reason, detail, alloc_out, attempt_id)

            problem = _static_refusal(cand, ad, fp, ctx, config, run)
            if problem:
                return refuse(*problem)
            if ctx["origin"] != "manual" and not settings["enabled"]:
                return refuse("discovery-disabled", "routing.discovery.enabled is false in the pinned config")
            if ctx["origin"] != "manual" and ctx.get("role") not in (None, *settings["roles"]):
                return refuse("role-not-eligible", f"discovery is limited to {', '.join(settings['roles'])}")
            quota = _quota_refusal(cand, config)
            if quota:
                return refuse("quota-reserve", quota)
            if cached and cached["result"] != "pending":
                _event(con, "probe-cache-hit", attempt_id=attempt_id, ctx=ctx, run=run, cand=cand, fp=fp,
                       probe_key=probe_key, alloc=alloc, freshness="cached-fresh", dispatch_id=dispatch_id,
                       source_attempt_id=cached.get("attempt_id") or attempt_id, outcome=f"cache-hit:{cached['result']}",
                       reason_class=cached.get("reason_class"), detail=cached.get("detail"))
                return {**cached, "cached": True, "source": "cache", "freshness": "cached-fresh",
                        "attempt_id": attempt_id, "source_attempt_id": cached.get("attempt_id"),
                        "fingerprint": fp, "allocation": alloc}
            open_row = _inflight(con, probe_key)
            if open_row:
                return refuse("probe-in-flight", open_row["id"])
            if run_id and alloc["probes"]["used"] >= alloc["probes"]["max"]:
                return refuse("probe-cap", f"{alloc['probes']['used']}/{alloc['probes']['max']} probes used in this run")
            token = uuid.uuid4().hex
            _acquire(attempt_id)
            held = True
            stamp = now_iso()
            con.execute("INSERT INTO route_probe_reservations(id, run_id, probe_key, dispatch_id, task_id, status, "
                        "claim_token, reserved_at) VALUES(?,?,?,?,?,?,?,?)",
                        (attempt_id, run_id, probe_key, dispatch_id, ctx.get("task_id"), "reserved", token, stamp))
            con.execute("INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
                        "invocation_model_id, effort, result, reason_class, detail, probed_at, run_id, dispatch_id, "
                        "attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (probe_key, fp["harness"], fp["harness_version"], fp["adapter_hash"], fp["profile"],
                         fp["invocation_model_id"], fp["effort"], "pending", None, "probe reserved", stamp, run_id,
                         dispatch_id, attempt_id))
            alloc = allocation(con, run_id, settings)
            _event(con, "probe-reserved", attempt_id=attempt_id, ctx=ctx, run=run, cand=cand, fp=fp,
                   probe_key=probe_key, alloc=alloc, freshness="stale" if stale else "none", dispatch_id=dispatch_id,
                   outcome="reserved")
            atomic_write_json(_sidecar(attempt_id), {"attempt_id": attempt_id, "timeout_s": settings["probe_timeout_s"],
                                                     "pid": None, "start": None})
            return {"reserved": True, "attempt_id": attempt_id, "probe_key": probe_key, "claim_token": token,
                    "fingerprint": fp, "allocation": alloc, "reserved_at": stamp, "settings": settings}
    except BaseException:
        if held:
            _release(attempt_id)
        raise


# ------------------------------------------------------------------ expiry

def _reserved_event(con, attempt_id: str) -> dict | None:
    row = con.execute("SELECT * FROM route_discovery_events WHERE attempt_id=? AND kind='probe-reserved' "
                      "ORDER BY seq LIMIT 1", (attempt_id,)).fetchone()
    return dict(row) if row else None


def _close(con, attempt_id: str, status_: str, detail: str, *, kind: str, outcome: str) -> bool:
    """Close a still-reserved attempt (`expired` or `abandoned`) and append its event.
    False when the attempt was not open, so nothing changed."""
    with db.transaction(con):
        cur = con.execute("UPDATE route_probe_reservations SET status=?, finished_at=? WHERE id=? AND status='reserved'",
                          (status_, now_iso(), attempt_id))
        if cur.rowcount != 1:
            return False
        reserved = _reserved_event(con, attempt_id)
        row = con.execute("SELECT probe_key, run_id FROM route_probe_reservations WHERE id=?", (attempt_id,)).fetchone()
        # The cache holds verdicts. An attempt that reached none leaves nothing behind.
        con.execute("DELETE FROM route_probes WHERE key=? AND attempt_id=? AND result='pending'",
                    (row["probe_key"], attempt_id))
        if reserved is None:
            return True
        snapshot = json.loads(reserved["allocation_json"])
        settings = {**route_policy.DISCOVERY_DEFAULTS, "max_probes_per_run": snapshot["probes"]["max"],
                    "max_trials_per_run": snapshot["trials"]["max"],
                    "max_trial_percent_rolling_20": snapshot["rolling"].get("percent", 15)}
        route_policy.record_event(
            con, kind=kind, attempt_id=attempt_id, origin=reserved["origin"], policy_digest=reserved["policy_digest"],
            probe_key=reserved["probe_key"], reason=reserved["reason"], run_id=reserved["run_id"],
            plan_version=reserved["plan_version"], task_id=reserved["task_id"], dispatch_id=reserved["dispatch_id"],
            role=reserved["role"], fingerprint_json=reserved["fingerprint_json"],
            candidate_route=reserved["candidate_route"], primary_route=reserved["primary_route"],
            fallback_route=reserved["fallback_route"], probe_freshness="none",
            allocation_json=allocation(con, row["run_id"], settings), outcome=outcome, detail=detail[:DETAIL_LIMIT])
        return True


def expire_stale(con, *, now: datetime | None = None) -> list[str]:
    """Expire open reservations whose owner died or whose deadline passed.

    The outbox dead-claim pattern: any `office` command may run this. It
    terminates only a probe process whose recorded start time proves it is
    still the attempt's own, never relaunches a probe, and leaves the attempt
    counted against its run's cap.
    """
    now = now or _utcnow()
    expired = []
    try:
        rows = con.execute("SELECT * FROM route_probe_reservations WHERE status='reserved'").fetchall()
    except sqlite3.OperationalError:
        return expired
    for row in rows:
        attempt_id = row["id"]
        try:
            timeout_s = float(json.loads(_sidecar(attempt_id).read_text()).get("timeout_s"))
        except (OSError, ValueError, TypeError):
            timeout_s = float(route_policy.DISCOVERY_DEFAULTS["probe_timeout_s"])
        reserved_at = _parsed(row["reserved_at"])
        owner, why = _owner_state(attempt_id, reserved_at, timeout_s, now)
        overdue = reserved_at is not None and now - reserved_at > timedelta(seconds=timeout_s + GRACE_S)
        if owner != DEAD and not overdue:
            continue
        reason = why if owner == DEAD else f"reservation is past its {timeout_s + GRACE_S:g}s deadline"
        cleanup = _kill_recorded_children(attempt_id)
        detail = f"expired: {reason}" + (f"; {cleanup}" if cleanup else "")
        if _close(con, attempt_id, "expired", detail, kind="probe-expired", outcome="expired"):
            expired.append(attempt_id)
            if owner == DEAD:
                _release(attempt_id)
    return expired


def abandon(con, attempt_id: str, reason: str = "caller abandoned the probe") -> bool:
    """End an open attempt that will not run (cancelled preflight). It stays counted."""
    changed = _close(con, attempt_id, "abandoned", reason, kind="probe-abandoned", outcome="abandoned")
    if changed:
        _kill_recorded_children(attempt_id)
        _release(attempt_id)
    return changed


def sweep() -> list[str]:
    """Best-effort expiry for every `office` command. Reads a read-only handle first,
    so a command never creates or migrates a database just to find nothing to do."""
    try:
        db_path = paths.runs_db()
        if not db_path.exists():
            return []
        ro = sqlite3.connect(str(db_path), timeout=5)
        try:
            ro.execute("PRAGMA query_only=ON")
            if not ro.execute("SELECT 1 FROM sqlite_master WHERE name='route_probe_reservations'").fetchone():
                return []
            if not ro.execute("SELECT 1 FROM route_probe_reservations WHERE status='reserved' LIMIT 1").fetchone():
                return []
        finally:
            ro.close()
        con = db.connect()
        try:
            return expire_stale(con)
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - housekeeping must never fail a command
        return []


# ------------------------------------------------------------------ the probe

_PATTERNS = (
    ("auth-quota-blocked", re.compile(
        r"\b(?:401|403|429)\b|unauthori[sz]ed|not logged in|log ?in required|please (?:log ?in|sign ?in)|"
        r"authentication (?:failed|required|error)|api[ _-]?key|credential|quota|rate[ -]?limit|usage limit|"
        r"insufficient (?:credit|quota|balance)|billing|too many requests", re.I)),
    ("unsupported-model-effort", re.compile(
        r"unsupported (?:reasoning[ _-]?)?(?:effort|model|value)|invalid (?:reasoning[ _-]?)?(?:effort|model)|"
        r"(?:effort|reasoning_effort)[^\n]{0,60}(?:not supported|invalid|unsupported|unknown)|"
        r"(?:not supported|does not support)[^\n]{0,60}(?:effort|reasoning)|"
        r"(?:unknown|unrecognized|no such) model|"
        r"model[^\n]{0,60}(?:not found|does not exist|not supported|is not available)", re.I)),
    ("transient", re.compile(
        r"timed? ?out|connection (?:reset|refused|closed|error)|network (?:error|unreachable)|temporar(?:y|ily)|"
        r"\b50[234]\b|overloaded|try again|econn|eai_again|socket hang up|service unavailable", re.I)),
    ("isolation-missing", re.compile(
        r"read-only file system|operation not permitted|permission denied|sandbox|approval (?:is )?required|"
        r"requires approval|not allowed to (?:write|run|execute)", re.I)),
)
_FENCE = re.compile(r"^-{4,}\s*$")
_FIELD = re.compile(r"^\s*([A-Za-z][A-Za-z _-]*?)\s*:\s*(.*?)\s*$")


def _codex_header(text: str) -> dict[str, str] | None:
    """The `codex exec` banner: the harness's own record of the invocation it
    resolved. It is the first thing on the stream, a banner line then a fenced
    block of `key: value` lines, so nothing a model writes can appear before it.
    Only that first block is read; a fence, `model:` or `reasoning effort:` line
    in the reply is ignored."""
    lines = text.lstrip().splitlines()
    if len(lines) < 3 or not re.match(r"OpenAI Codex\b", lines[0]) or not _FENCE.match(lines[1]):
        return None
    fields: dict[str, str] = {}
    for line in lines[2:]:
        if _FENCE.match(line):
            return fields
        m = _FIELD.match(line)
        if m:
            fields.setdefault(re.sub(r"[ _-]+", " ", m.group(1).lower()), m.group(2))
    return None  # the block never closed


# Harness id -> parser of authoritative model and effort metadata. A harness absent here is never probed.
_IDENTITY_READBACK = {"codex": _codex_header}


def classify_text(text: str) -> str | None:
    """The first reason class whose signature appears in harness output."""
    for reason_class, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return reason_class
    return None


def probe_prompt(token: str | None = None) -> str:
    """The tiny probe prompt. It names no expected reply token, so a harness that
    echoes its prompt cannot pass by echoing it, and it asks the model nothing about
    its own identity: that comes from the harness's metadata."""
    return ("Conformance probe. Work only inside the current directory.\n"
            "1. Read the file probe-input.txt. It holds one token.\n"
            "2. Create probe-output.txt containing that same token and a newline.\n"
            "3. Do not change any other file and do not leave the current directory.\n"
            "4. Reply with exactly one line: PROBE-OK followed by a space and the token.\n")


class _Workspace:
    """A disposable git repository and linked worktree plus an outside canary.

    The probe agent works in `wt`. `tmp` and `home` are its scratch space. The OS
    write boundary lets the harness write only `wt`, the repository's `.git` (the
    linked worktree's metadata), `tmp` and `home`. Anything else that changes
    under `base` is a write outside the worktree, which the boundary should make
    impossible and the snapshot proves."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="office-probe-")).resolve()
        self.repo, self.wt, self.outside = self.base / "repo", self.base / "wt", self.base / "outside"
        self.tmp, self.home = self.base / "tmp", self.base / "home"
        self.token = uuid.uuid4().hex[:12]
        self.setup_error: str | None = None
        try:
            self._setup()
        except (OSError, subprocess.SubprocessError) as exc:
            self.setup_error = f"could not create the disposable git worktree: {exc}"[:DETAIL_LIMIT]

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        proc = subprocess.run(["git", "-c", "user.name=office-probe", "-c", "user.email=probe@office.invalid",
                               "-c", "commit.gpgsign=false", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True, timeout=30, env=env, check=True)
        return proc.stdout

    def _setup(self) -> None:
        self.repo.mkdir()
        self.outside.mkdir()
        self.tmp.mkdir(mode=0o700)
        self.home.mkdir(mode=0o700)
        self._git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("disposable probe repository\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "probe base")
        self._git("worktree", "add", "-q", "-b", f"probe-{self.token}", str(self.wt))
        (self.outside / "canary.txt").write_text(f"canary {self.token}\n")
        (self.wt / "probe-input.txt").write_text(self.token + "\n")
        top = self._git("rev-parse", "--show-toplevel", cwd=self.wt).strip()
        if Path(top).resolve() != self.wt.resolve():
            raise OSError(f"worktree toplevel is {top}")
        self.before = self.snapshot()
        self.head = self._git("rev-parse", "main").strip()

    @property
    def writable(self) -> list[Path]:
        return [self.wt, self.repo / ".git", self.tmp, self.home]

    def snapshot(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted(self.base.rglob("*")):
            rel = path.relative_to(self.base)
            if rel.parts[0] in ("wt", "tmp", "home") or rel.parts[:2] == ("repo", ".git"):
                continue
            out[str(rel)] = "dir" if path.is_dir() else sha256_file(path)
        return out

    def outside_change(self) -> str | None:
        after = self.snapshot()
        changed = sorted(k for k in set(self.before) | set(after) if self.before.get(k) != after.get(k))
        try:
            head = self._git("rev-parse", "main").strip()
        except (OSError, subprocess.SubprocessError):
            head = None
        if head != self.head:
            changed.append("repo main branch head")
        return ", ".join(changed[:5]) if changed else None

    def output_file(self) -> str | None:
        path = self.wt / "probe-output.txt"
        return path.read_text(errors="replace") if path.is_file() else None

    def remove(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


def _sbpl(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


@functools.lru_cache(maxsize=1)
def write_boundary_reason() -> str | None:
    """None when this platform can enforce a filesystem write boundary on a probe,
    else why not. Real-harness probing stays refused wherever this is not None.

    Only macOS `sandbox-exec` is implemented. It is exercised here, once per
    process, so a nested or disabled sandbox is reported instead of assumed."""
    if sys.platform != "darwin":
        return f"no OS-enforced write boundary is implemented for {sys.platform}"
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return f"{SANDBOX_EXEC} is not available"
    base = Path(tempfile.mkdtemp(prefix="office-boundary-")).resolve()
    try:
        target = base / "denied"
        proc = subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)(deny file-write*)", "/bin/sh", "-c",
                               f"echo x > {target}"], capture_output=True, timeout=15)
        if proc.returncode == 0 or target.exists():
            return "sandbox-exec did not enforce a write boundary here"
        ok = subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                            capture_output=True, timeout=15)
        if ok.returncode != 0:
            return "sandbox-exec cannot apply a profile here (already inside a sandbox?)"
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        return f"sandbox-exec could not be exercised: {exc}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _boundary_argv(ws: _Workspace) -> list[str]:
    """`sandbox-exec` denying every file write except the probe's own workspace.
    The profile is inherited by every descendant, including one that leaves the
    process group."""
    allow = " ".join(f"(subpath {_sbpl(p)})" for p in [*ws.writable, *map(Path, _EXTRA_WRITABLE)])
    devices = " ".join(f'(literal "{d}")' for d in ("/dev/null", "/dev/tty", "/dev/dtracehelper"))
    return [SANDBOX_EXEC, "-p", f"(version 1)(allow default)(deny file-write*)(allow file-write* {allow} {devices})"]


def _spawn(argv: list[str], cwd: str, env: dict, stdin: int) -> subprocess.Popen:
    """The one place a probe child is created: its own session, output piped back."""
    return subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)


def _probe_env(attempt_id: str, probe_key: str, ws: _Workspace, harness: str, nonce: str) -> dict:
    """No Office identity, scratch space inside the boundary, and a nonce that tags
    every descendant. A harness's state directory is a private one in the
    workspace whose credentials are read-only links, so the probe cannot rewrite
    the user's configuration or rotate their login."""
    env = {k: v for k, v in os.environ.items() if k not in IDENTITY_ENV}
    env.update({"OFFICE_PROBE_ATTEMPT_ID": attempt_id, "OFFICE_PROBE_KEY": probe_key, NONCE_ENV: nonce,
                "TMPDIR": str(ws.tmp), "TMP": str(ws.tmp), "TEMP": str(ws.tmp)})
    if harness == "codex":
        real = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        private = ws.home / ".codex"
        private.mkdir(exist_ok=True)
        for name in ("auth.json", "config.toml"):
            if (real / name).is_file():
                try:
                    (private / name).symlink_to(real / name)
                except OSError:
                    pass
        env["CODEX_HOME"] = str(private)
    return env


def _launch(argv: list[str], prof: dict, prompt: str, cwd: Path, env: dict, timeout_s: float,
            attempt_id: str, base: Path | None = None) -> dict:
    """Run the harness once under a hard timeout. Always ends with the probe's
    process group and every process attributed to it terminated and
    confirmed gone, or says it could not confirm."""
    stdin = subprocess.PIPE if prof.get("prompt") == "stdin" else subprocess.DEVNULL
    if prof.get("prompt") == "argv":
        argv = argv + [prompt]
    elif prof.get("prompt") == "argv-bound":
        argv = argv + [prof.get("prompt_flag", "--prompt=") + prompt]
    nonce = env[NONCE_ENV]
    tracker = _Tracker(nonce, base)
    out = {"output": "", "returncode": None, "timed_out": False, "launch_error": None, "group_gone": True}
    # Recorded before the spawn, so an owner that dies mid-launch still leaves its tag for the sweeper.
    atomic_write_json(_sidecar(attempt_id), {"attempt_id": attempt_id, "timeout_s": timeout_s, "pid": None,
                                             "start": None, "nonce": nonce, "base": str(base or "")})
    try:
        proc = _spawn(argv, str(cwd), env, stdin)
    except PermissionError as exc:
        out["launch_error"] = ("isolation-missing", f"launch denied: {exc}")
        return out
    except OSError as exc:
        out["launch_error"] = ("transient", f"launch failed: {exc}")
        return out
    atomic_write_json(_sidecar(attempt_id), {"attempt_id": attempt_id, "timeout_s": timeout_s, "pid": proc.pid,
                                             "start": process_start(proc.pid), "nonce": nonce,
                                             "base": str(base or "")})
    deadline = time.monotonic() + timeout_s
    try:
        try:
            feed = prompt.encode() if stdin == subprocess.PIPE else None
            while True:
                try:
                    data, _ = proc.communicate(input=feed, timeout=max(0.01, min(0.5, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    feed = None
                    tracker.observe(proc.pid)  # remember descendants while the leader can still name them
                    if time.monotonic() >= deadline:
                        raise
        except subprocess.TimeoutExpired:
            out["timed_out"] = True
            out["group_gone"] = _terminate_tree(proc.pid, tracker, proc=proc)
            try:
                data, _ = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                data = b""
        out["output"] = (data or b"").decode(errors="replace")
        out["returncode"] = proc.returncode
    finally:
        # Normal exit or not, nothing the probe started may outlive it.
        out["group_gone"] = _terminate_tree(proc.pid, tracker, proc=proc) and out["group_gone"]
    return out


def _normal(value: str | None) -> str:
    return (value or "").strip().strip("`'\"").lower()


def _evaluate(launch: dict, ws: _Workspace, prompt: str, harness: str, expected_model: str,
              expected_effort: str | None) -> tuple:
    """(result, reason_class, detail) for one finished launch.

    Model and effort pass only when the harness's own metadata names both and both
    match the exact request. The model's reply is never identity evidence."""
    text = launch["output"].replace(prompt, "")
    tail = " ".join(text.split())[-DETAIL_LIMIT // 2:]
    if launch["launch_error"]:
        return "fail", launch["launch_error"][0], launch["launch_error"][1]
    if not launch["group_gone"]:
        return "fail", "isolation-missing", "the probe process group could not be confirmed gone"
    moved = ws.outside_change()
    if moved:
        return "fail", "isolation-missing", f"write outside the disposable worktree: {moved}"
    if launch["timed_out"]:
        return "fail", "transient", f"probe timed out; its process group was terminated. {tail}"
    if launch["returncode"] != 0:
        return "fail", classify_text(text) or "conformance-failed", f"harness exited {launch['returncode']}: {tail}"
    reader = _IDENTITY_READBACK.get(harness)
    meta = reader(text) if reader else None
    if meta is None:
        return "fail", "conformance-failed", "no harness-reported invocation metadata, so model and effort are unproven"
    seen_model, seen_effort = meta.get("model"), meta.get("reasoning effort")
    if not seen_model or not seen_effort:
        return "fail", "conformance-failed", "harness metadata does not report both model and effort"
    if _normal(seen_model) != _normal(expected_model):
        return "fail", "conformance-failed", f"harness reported model {seen_model}, asked {expected_model}"
    if expected_effort is None or _normal(seen_effort) != _normal(expected_effort):
        return ("fail", "unsupported-model-effort", f"harness reported effort {seen_effort}, asked {expected_effort}")
    if not re.search(rf"^[ \t]*PROBE-OK[ \t]+{re.escape(ws.token)}[ \t]*$", text, re.M):
        return "fail", classify_text(text) or "conformance-failed", f"no well-formed probe reply: {tail}"
    written = ws.output_file()
    if written is None or written.strip() != ws.token:
        return ("fail", "isolation-missing",
                "the permitted file write inside the disposable worktree was not observed")
    return "pass", None, (f"model {expected_model} at effort {expected_effort} read back from the harness metadata; "
                          "reply, write, write boundary and process cleanup verified")


def _run_probe(reservation: dict, cand: dict, adapter: dict) -> tuple[str, str | None, str]:
    settings = reservation["settings"]
    fp = reservation["fingerprint"]
    unavailable = write_boundary_reason()
    if unavailable:  # re-checked at launch: nothing ever runs outside the boundary
        return "fail", "isolation-missing", unavailable
    ws = _Workspace()
    try:
        if ws.setup_error:
            return "fail", "isolation-missing", ws.setup_error
        prompt = probe_prompt()
        try:
            argv, prof = adapters.build_argv(adapter, fp["profile"], model=fp["invocation_model_id"],
                                             effort=fp["effort"], cwd=ws.wt)
        except adapters.AdapterError as exc:
            return "fail", "isolation-missing", str(exc)
        env = _probe_env(reservation["attempt_id"], reservation["probe_key"], ws, fp["harness"], uuid.uuid4().hex)
        launch = _launch([*_boundary_argv(ws), *argv], prof, prompt, ws.wt, env, float(settings["probe_timeout_s"]),
                         reservation["attempt_id"], ws.base)
        return _evaluate(launch, ws, prompt, fp["harness"], fp["invocation_model_id"],
                         adapters.effort_value(adapter, fp["effort"]))
    finally:
        ws.remove()


def _record(con, run: dict | None, cand: dict, reservation: dict, ctx: dict, result: str, reason_class: str | None,
            detail: str) -> dict:
    """Second transaction: cache row, reservation, and the immutable `probe-result` event."""
    attempt_id, probe_key, fp = reservation["attempt_id"], reservation["probe_key"], reservation["fingerprint"]
    run_id = run["id"] if run else None
    detail = detail[:DETAIL_LIMIT]
    with db.transaction(con):
        cur = con.execute("UPDATE route_probe_reservations SET status=?, finished_at=? WHERE id=? AND status='reserved'",
                          ("completed" if result == "pass" else "failed", now_iso(), attempt_id))
        live = cur.rowcount == 1
        stamp = now_iso()
        if live:
            con.execute("INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
                        "invocation_model_id, effort, result, reason_class, detail, probed_at, run_id, dispatch_id, "
                        "attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (probe_key, fp["harness"], fp["harness_version"], fp["adapter_hash"], fp["profile"],
                         fp["invocation_model_id"], fp["effort"], result, reason_class, detail, stamp, run_id,
                         ctx.get("dispatch_id"), attempt_id))
        else:
            detail = ("late result after the reservation was closed; cache unchanged: " + detail)[:DETAIL_LIMIT]
        settings = reservation["settings"]
        _event(con, "probe-result", attempt_id=attempt_id, ctx=ctx, run=run, cand=cand, fp=fp, probe_key=probe_key,
               alloc=allocation(con, run_id, settings), freshness="fresh-run", dispatch_id=ctx.get("dispatch_id"),
               outcome=result, reason_class=reason_class, detail=detail)
    return {"key": probe_key, "result": result, "reason_class": reason_class, "detail": detail, "probed_at": stamp,
            "run_id": run_id, "attempt_id": attempt_id, "fingerprint": fp, "fresh": True, "freshness": "fresh-run",
            "source": "probe", "cached": False}


def _wait_inflight(con, probe_key: str, timeout_s: float) -> None:
    """Wait for another attempt's probe of this fingerprint, expiring it when its owner died."""
    deadline = time.time() + timeout_s + GRACE_S
    while time.time() < deadline:
        if _inflight(con, probe_key) is None:
            return
        expire_stale(con)
        time.sleep(0.05)


def ensure(con, run: dict | None, cand: dict, *, budget=None, attempt_id: str, context: dict | None = None,
           adapter: dict | None = None, dispatch_id: str | None = None) -> dict | Refused:
    """Prove this exact route: a fresh cached record, a refusal, or one bounded probe.

    Reserves first (`reserve`, atomic), launches at most one probe outside any
    transaction, then records the result in a second transaction. Never adds
    permissions, extensions or installs, and never probes a refused route. If
    this call is interrupted after reserving, the attempt is closed as abandoned
    and its probe process group is terminated.
    """
    ad = _adapter(cand, adapter)
    probe_key = key(cand, ad)
    ctx, _, settings = _prepare(run, context, budget)
    for _ in range(3):
        if _inflight(con, probe_key):
            _wait_inflight(con, probe_key, float(settings["probe_timeout_s"]))
        reservation = reserve(con, run, cand, attempt_id=attempt_id, context=context, dispatch_id=dispatch_id,
                              budget=budget, adapter=ad)
        if isinstance(reservation, Refused) and reservation.reason == "probe-in-flight":
            continue
        break
    if isinstance(reservation, Refused) or reservation.get("cached"):
        return reservation
    ctx = {**ctx, "dispatch_id": dispatch_id or ctx.get("dispatch_id")}
    try:
        result, reason_class, detail = _run_probe(reservation, cand, ad)
        record = _record(con, run, cand, reservation, ctx, result, reason_class, detail)
    except BaseException as exc:
        abandon(con, attempt_id, f"probe interrupted: {type(exc).__name__}")
        raise
    finally:
        _release(attempt_id)
    return record
