"""The local Office web service: lifecycle, snapshot/delta stream and commands.

Startup (`Service.start`) is the one explicit writer step: it runs the runs.db
migration through `db.connect` (logged), creates the host id and turns
`running` command receipts whose executor is gone into `unknown`. Every later
read goes through T1's read-only `Observer`; the writer connection is kept for
command receipts only.

The poller rebuilds the snapshot when `PRAGMA data_version` or the max event
seq changes (or GitHub/host data moved) and publishes a delta
`{epoch, rev, base_rev, upserts, removes, scalars}`. `epoch` is per process,
so a client that reconnects to a restarted service resyncs.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from office import commands as receipts
from office import db, hostmetrics, scheduler
from office.state import OfficeError
from office.util import dumps
from office.web import capabilities as caps
from office.web import chat, identity, launcher as launch_mod, settings as settings_mod
from office.web.executor import Executor, Outcome
from office.web.observer import Observer

log = logging.getLogger("office.web")

KINDS = ("start_issue", "queue_issue", "resume_run", "attach_run", "pause", "resume", "set_priority", "demote",
         "set_auto_mode", "change_route", "chat_send", "settings_set", "settings_unset")
COLLECTIONS = ("repos", "issues", "prs", "runs", "tasks", "agents", "queue", "commands")
COMMAND_ID = re.compile(r"[A-Za-z0-9_.:-]{8,128}")
STALE_AFTER = 15.0
RING = 512
COMMANDS_SHOWN = 200
LIVE, STALE, DISCONNECTED = "live", "stale", "disconnected"


class CommandRefused(Exception):
    """A command refused before or at validation; `reason` is a stable name."""

    def __init__(self, reason: str, message: str, *, http: int = 409, receipt: dict | None = None,
                 data: dict | None = None):
        super().__init__(message)
        self.reason, self.http, self.receipt, self.data = reason, http, receipt, data or {}

    def body(self) -> dict:
        return {"ok": False, "reason": self.reason, "message": str(self), "receipt": self.receipt, **self.data}


def apply_delta(state: dict, delta: dict) -> str:
    """Client-side reference: apply `delta` to a snapshot in place.

    Returns 'applied', 'duplicate' (already have this rev; ignored) or 'resync'
    (another epoch, or a gap: base_rev is not our rev).
    """
    if delta.get("epoch") != state.get("epoch"):
        return "resync"
    if delta["rev"] <= state["rev"]:
        return "duplicate"
    if delta["base_rev"] != state["rev"]:
        return "resync"
    for coll, items in (delta.get("upserts") or {}).items():
        state["entities"].setdefault(coll, {}).update(items)
    for coll, ids in (delta.get("removes") or {}).items():
        for i in ids:
            state["entities"].get(coll, {}).pop(i, None)
    for key, value in (delta.get("scalars") or {}).items():
        if key == "freshness":
            state["freshness"] = value
        else:
            state["scalars"][key] = value
    state["rev"] = delta["rev"]
    return "applied"


def _now_iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Command:
    id: str
    kind: str
    target: dict
    expect: dict
    payload: dict

    @classmethod
    def parse(cls, body: Any) -> "Command":
        if not isinstance(body, dict):
            raise CommandRefused("bad-request", "the body must be a JSON object", http=400)
        cid, kind = body.get("id"), body.get("kind")
        if not isinstance(cid, str) or not COMMAND_ID.fullmatch(cid):
            raise CommandRefused("bad-request", "id must be 8-128 characters of [A-Za-z0-9_.:-]", http=400)
        if kind not in KINDS:
            raise CommandRefused("unknown-kind", f"{kind!r} is not a web command kind", http=400)
        parts = {k: body.get(k) if body.get(k) is not None else {} for k in ("target", "expect", "payload")}
        for k, v in parts.items():
            if not isinstance(v, dict):
                raise CommandRefused("bad-request", f"{k} must be an object", http=400)
        return cls(cid, kind, parts["target"], parts["expect"], parts["payload"])


class _Writer:
    """The writer connection, used for command receipts only."""

    def __init__(self, path: Path):
        self.con = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=30000")
        self.lock = threading.Lock()

    def __call__(self, fn: Callable, *args, **kwargs):
        with self.lock:
            return fn(self.con, *args, **kwargs)

    def close(self) -> None:
        self.con.close()


class _LockedObserver(Observer):
    """One read-only connection shared by the poller and request threads, one reader at a time."""

    def __init__(self, path, **context):
        super().__init__(path, **context)
        self._lock = threading.RLock()

    def read(self, fn):
        with self._lock:
            return super().read(fn)

    def close(self) -> None:
        with self._lock:
            super().close()


class FakeExecutor:
    """Fixture mode and tests: records `office` invocations, runs nothing."""

    def __init__(self, status: str = "completed"):
        self.status, self.calls = status, []

    def run(self, args, cwd) -> Outcome:
        self.calls.append({"args": list(args), "cwd": str(cwd) if cwd else None})
        return Outcome(self.status, {"args": list(args), "fake": True},
                       None if self.status == "completed" else "fake executor")


class Service:
    def __init__(self, db_path: Path, state_home: Path, *, launcher=None, executor=None, resolver=None,
                 github=None, checkouts: Callable[[str], Path | None] | None = None,
                 readiness: Callable[[dict, Path | None], dict] | None = None,
                 host_probe: Callable[[], dict] = hostmetrics.host, observer_ctx: dict | None = None,
                 config: Callable[[], dict] | None = None, fixture: str | None = None,
                 clock: Callable[[], float] = time.time, stale_after: float = STALE_AFTER,
                 queue_loop: bool = True):
        self.db_path, self.state_home = Path(db_path), Path(state_home)
        self.launcher = launcher or launch_mod.FakeLauncher(reason="no launcher configured")
        self.executor = executor or Executor(env={"AUTO_OFFICE_RUNS_DB": str(self.db_path)})
        self.resolver = resolver or caps.Resolver()
        self.github = github
        self.checkouts = checkouts or (lambda full_name: None)
        self.readiness = readiness
        self.host_probe, self.observer_ctx = host_probe, dict(observer_ctx or {})
        self.config = config or _effective_config
        self.fixture, self.clock, self.stale_after, self.queue_loop = fixture, clock, stale_after, queue_loop
        self.epoch = secrets.token_hex(6)
        self.token = secrets.token_urlsafe(32)
        self.rev = 0
        self.snapshot_state: dict | None = None
        self.ring: collections.deque = collections.deque(maxlen=RING)
        self.cond = threading.Condition()
        self.lock = threading.RLock()
        self.marker = None
        self._github_rev: str | None = None
        self._db_inode: tuple[int, int] | None = None
        self.last_ok: float | None = None
        self.last_error: tuple[float, str] | None = None
        self.launches: dict[str, dict] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.stopping = threading.Event()
        self.host_id: str | None = None
        self.writer: _Writer | None = None
        self.observer: Observer | None = None
        self.recovered: list[str] = []

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> "Service":
        con = db.connect(self.db_path)  # the one migration, explicitly at startup
        try:
            schema = con.execute("SELECT value FROM schema_meta WHERE key='office_schema'").fetchone()
            log.info("office web: runs.db %s migrated to schema %s", self.db_path, schema[0] if schema else "?")
            self.recovered = receipts.recover_interrupted(con, ())
            if self.recovered:
                log.info("office web: %d interrupted command receipt(s) marked unknown", len(self.recovered))
        finally:
            con.close()
        self.host_id = identity.ensure_host_id(self.state_home)
        self.writer = _Writer(self.db_path)
        self.observer = _LockedObserver(self.db_path, host_id=self.host_id, **self.observer_ctx)
        self._load_launches()
        self.poll(force=True)
        return self

    def close(self) -> None:
        self.stopping.set()
        with self.cond:
            self.cond.notify_all()
        deadline = time.monotonic() + getattr(self.executor, "timeout", 0) + 5  # one bound for all of them
        for t in list(self.threads.values()):
            t.join(max(0.0, deadline - time.monotonic()))  # let in-flight commands record their outcome; a hung one stays `running` -> unknown at restart
            if t.is_alive():
                log.warning("office web: %s still running at shutdown; its receipt becomes unknown on restart", t.name)
        if self.observer:
            self.observer.close()
        if self.writer:
            self.writer.close()

    def run_poller(self, interval: float = 1.0) -> threading.Thread:
        def loop():
            while not self.stopping.wait(interval):
                try:
                    self.poll()
                    if self.queue_loop:
                        self.admit_queue()
                except Exception:  # noqa: BLE001 - the poller must survive one bad pass
                    log.exception("office web: poll failed")
        t = threading.Thread(target=loop, name="office-web-poller", daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------ freshness

    def office_freshness(self) -> dict:
        now = self.clock()
        if self.last_error:
            state, reason = DISCONNECTED, self.last_error[1]
        elif self.last_ok is None or now - self.last_ok > self.stale_after:
            state, reason = STALE, f"no successful runs.db read in {self.stale_after:g}s"
        else:
            state, reason = LIVE, None
        return {"state": state, "reason": reason,
                "last_ok_at": _now_iso(self.last_ok) if self.last_ok else None}

    def github_freshness(self) -> dict:
        if self.github is None:
            return {"state": "disabled", "reason": "no GitHub client", "sources": {}}
        fresh = self.github.snapshot()["freshness"]
        states = [s["state"] for s in [fresh.get("discovery") or {}] + [v for r in fresh["repos"].values()
                                                                         for v in r.values()] if s]
        worst = next((s for s in ("unauthenticated", "revoked", "rate_limited", "stale") if s in states),
                     "fresh" if states else "unknown")
        return {"state": worst, "reason": None, "sources": fresh}

    def freshness(self) -> dict:
        return {"office": self.office_freshness(), "github": self.github_freshness()}

    # ------------------------------------------------------------------ polling

    def _marker(self):
        st = os.stat(self.db_path)  # a missing or replaced runs.db is not read through a stale handle
        if (st.st_dev, st.st_ino) != self._db_inode:
            self.observer.close()
            self._db_inode = (st.st_dev, st.st_ino)

        def read(snap):
            dv = snap.con.execute("PRAGMA data_version").fetchone()[0]
            seq = snap.con.execute("SELECT MAX(seq) FROM events").fetchone()[0] if snap.has("events") else None
            return dv, seq
        return self.observer.read(read)

    def poll(self, *, force: bool = False) -> dict | None:
        """One pass: publish a delta when anything moved. Returns it (or None)."""
        with self.lock:
            before = self.office_freshness()["state"]
            try:
                marker = self._marker()
                self.last_ok, self.last_error = self.clock(), None
            except (sqlite3.Error, OSError) as exc:
                self.last_error = (self.clock(), f"runs.db unreadable: {type(exc).__name__}")
                self.observer.close()
                marker = None
            github_rev = _github_signature(self.github.snapshot()) if self.github else None
            changed = force or marker != self.marker or github_rev != self._github_rev
            freshness_moved = self.office_freshness()["state"] != before
            if not changed and not freshness_moved:
                return None
            self.marker, self._github_rev = marker, github_rev
            return self._rebuild(reuse_entities=marker is None)

    def _rebuild(self, *, reuse_entities: bool = False) -> dict:
        prev = self.snapshot_state
        if reuse_entities and prev is not None:
            entities, scalars = prev["entities"], dict(prev["scalars"])
        else:
            entities, scalars = self._build()
        snap = {"epoch": self.epoch, "rev": self.rev + 1, "freshness": self.freshness(),
                "entities": entities, "scalars": scalars}
        delta = _diff(prev, snap)
        if prev is not None and not (delta["upserts"] or delta["removes"] or delta["scalars"]):
            return None
        with self.cond:
            self.rev += 1
            self.snapshot_state = snap
            self.ring.append(delta)
            self.cond.notify_all()
        return delta

    def snapshot(self) -> dict:
        with self.cond:
            return json.loads(json.dumps(self.snapshot_state, default=str))

    def events_since(self, last_event_id: str | None) -> tuple[str, list[dict]]:
        """('snapshot'|'resync', []) or ('deltas', [...]) for a reconnecting client."""
        if not last_event_id:
            return "snapshot", []
        epoch, _, rev = last_event_id.partition(":")
        if epoch != self.epoch or not re.fullmatch(r"[0-9]{1,18}", rev):
            return "resync", []
        rev_n = int(rev)
        with self.cond:
            if rev_n > self.rev:
                return "resync", []
            if rev_n == self.rev:
                return "deltas", []
            pending = [d for d in self.ring if d["rev"] > rev_n]
            if not pending or pending[0]["base_rev"] != rev_n:
                return "resync", []
            return "deltas", pending

    def wait_for(self, rev: int, timeout: float) -> list[dict]:
        with self.cond:
            self.cond.wait_for(lambda: self.rev > rev or self.stopping.is_set(), timeout=timeout)
            return [d for d in self.ring if d["rev"] > rev]

    # ------------------------------------------------------------------ building

    def _build(self) -> tuple[dict, dict]:
        ws = self.observer.workspace()
        conf = self.config()
        queue_rows, active, auto = self.observer.read(lambda s: _queue_rows(s, conf))
        host = self.host_probe()
        plan = scheduler.plan_admission(queue_rows, active, host, {}, conf, datetime.now(timezone.utc))
        command_rows = self.observer.read(
            lambda s: s.table("commands", order=f"accepted_at DESC LIMIT {COMMANDS_SHOWN}") if s.has("commands") else [])
        gh = self.github.snapshot() if self.github else {"repos": [], "issues": [], "pulls": [], "github_checks": []}
        self._link_launches(ws["runs"])
        launcher_reason = self.launcher.unavailable()
        ent: dict[str, dict] = {c: {} for c in COLLECTIONS}
        for r in ws["runs"]:
            run = {k: v for k, v in r.items() if k not in ("tasks", "agents")}
            run["controls"] = caps.for_run(r, self.resolver, launcher_reason=launcher_reason)
            run["launch"] = next(({"command": cid, "pane": l["pane"], "provenance": "web-launch"}
                                  for cid, l in self.launches.items() if l.get("run") == r["run_id"]), None)
            ent["runs"][r["id"]] = run
            for t in r["tasks"]:
                ent["tasks"][t["id"]] = {**t, "run": r["id"]}
            for column, nodes in r["agents"]["columns"].items():
                for n in nodes:
                    ent["agents"][n["id"]] = {**n, "column": column, "run": r["id"]}
            for p in r["prs"]:
                if p.get("ref"):
                    ent["prs"][p["ref"]] = {**p, "run": r["id"]}
        ready = {}
        for repo in gh["repos"]:
            key = identity.repo_key(repo["full_name"])
            checkout = self.checkouts(repo["full_name"])
            ready[key] = self._assess(repo, checkout)
            ent["repos"][key] = {"id": key, "full_name": repo["full_name"], "github": repo, "readiness": ready[key],
                                 "runs": []}
        for repo in ws["repos"]:
            entry = ent["repos"].setdefault(repo["key"], {"id": repo["key"], "full_name": repo.get("slug"),
                                                          "github": None, "readiness": None, "runs": []})
            entry["runs"] = repo["runs"]
            entry["local_key"] = repo.get("local_key")
        runs_by_issue: dict[str, list[dict]] = collections.defaultdict(list)
        for r in ws["runs"]:
            if r.get("issue") and r["issue"].get("ref"):
                runs_by_issue[r["issue"]["ref"]].append(r)
        for i in gh["issues"]:
            ref = identity.issue_ref(identity.repo_key(i["repo"]), i["number"])
            linked = runs_by_issue.get(ref, [])
            ent["issues"][ref] = {**i, "id": ref, "runs": [r["id"] for r in linked], "provenance": "github",
                                  "live_run": next((r["id"] for r in linked if r["liveness"] == "live"), None),
                                  "resumable_run": next((r["id"] for r in linked if r["liveness"] == "resumable"),
                                                        None)}
        checks = {(c["repo"], c["number"]): c["state"] for c in gh["github_checks"]}
        for p in gh["pulls"]:
            ref = identity.pr_ref(identity.repo_key(p["repo"]), p["number"])
            ent["prs"][ref] = {**ent["prs"].get(ref, {}), "github": p,
                               "github_checks": checks.get((p["repo"], p["number"]))}
        for e in plan["entries"]:
            ent["queue"][e["id"]] = e
        for c in command_rows:
            ent["commands"][f"command:{c['id']}"] = _receipt_view(c)
        scalars = {
            "host": {"id": identity.host(self.host_id), "telemetry": host},
            "scheduler": {"active": plan["active"], "host": plan["host"], "auto_mode": auto},
            "fixture": self.fixture,
            "launcher": {"available": launcher_reason is None, "reason": launcher_reason},
            "schema": ws["schema"],
        }
        return ent, scalars

    def _assess(self, repo: dict, checkout: Path | None) -> dict:
        if self.readiness is not None:
            return self.readiness(repo, checkout)
        from office.web import repos
        return repos.assess(repo, [checkout] if checkout else [])

    # ------------------------------------------------------------------ launches and the queue loop

    def _launch_file(self) -> Path:
        return self.state_home / "web" / ("launches-fixture.json" if self.fixture else "launches.json")

    def _load_launches(self) -> None:
        try:
            self.launches = json.loads(self._launch_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.launches = {}

    def _save_launches(self) -> None:
        path = self._launch_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.launches, indent=1, sort_keys=True), encoding="utf-8")

    def _link_launches(self, runs: list[dict]) -> None:
        """Link a launched pane to the run it created: issue + repository + the pane's own binding."""
        changed = False
        for cid, l in self.launches.items():
            if l.get("run") or not l.get("issue"):
                continue
            for r in runs:
                if (r.get("issue") or {}).get("ref") != l["issue"] or r["repo"]["key"] != l["repo"]:
                    continue
                sessions = {n["id"] for n in r["agents"]["columns"]["orchestrators"]}
                if identity.session(r["run_id"], "herdr", l["pane"]) in sessions or \
                        any(s == l.get("session") for s in sessions):
                    l["run"], changed = r["run_id"], True
                    break
        if changed:
            self._save_launches()

    def panes(self) -> dict[str, str]:
        out = {}
        for l in self.launches.values():
            if l.get("run") and l.get("pane"):
                for agent in (self.snapshot_state or {}).get("entities", {}).get("agents", {}).values():
                    if agent["run"] == identity.run(l["run"]) and agent["column"] == "orchestrators":
                        out.setdefault(agent["id"], l["pane"])
        return out

    def admit_queue(self) -> list[str]:
        """Launch admitted queued issues (one receipt per item, so never twice)."""
        if self.office_freshness()["state"] != LIVE or self.launcher.unavailable():
            return []
        launched = []
        for e in list((self.snapshot_state or {}).get("entities", {}).get("queue", {}).values()):
            if e.get("kind") != "issue" or e.get("decision") != "admit":
                continue
            cid = "queue-admit:" + e["id"]
            if self.writer(receipts.get, cid) is not None:
                continue
            repo, number = _split_ref(e.get("ref"))
            if not repo:
                continue
            cmd = Command(cid, "start_issue", {"repo": repo, "issue": number},
                          {}, {"end_state": (self.config().get("intake") or {}).get("authorization") or "preview",
                               "title": e.get("title"), "new_run_confirmed": False, "origin": "queue"})
            try:
                self.submit(cmd, origin="web-queue")  # never blocks the poller; the receipt stops repeats
                launched.append(e["id"])
            except CommandRefused as exc:
                log.info("office web: queue item %s not admitted: %s", e["id"], exc.reason)
        return launched

    # ------------------------------------------------------------------ commands

    def submit(self, cmd: Command, *, origin: str = "web", wait: bool = False) -> dict:
        if self.office_freshness()["state"] != LIVE:
            raise CommandRefused("office-stale", "Office data is not live; mutations are refused until it is",
                                 http=409, data={"freshness": self.office_freshness()})
        existing = self.observer.read(lambda s: _receipt(s, cmd.id))
        if existing is not None:
            return self._replay(cmd, existing)
        try:
            receipt = self.writer(receipts.record, command_id=cmd.id, kind=cmd.kind, target=dumps(cmd.target),
                                  payload={"target": cmd.target, "expect": cmd.expect, "payload": cmd.payload},
                                  origin=origin)
        except OfficeError as exc:
            raise CommandRefused(exc.category, exc.message, http=409) from None
        if receipt["replayed"]:
            return self._replay(cmd, receipt)
        try:
            plan = self._validate(cmd)
        except Exception as exc:  # noqa: BLE001 - a recorded receipt never stays `accepted`
            refusal = exc if isinstance(exc, CommandRefused) else CommandRefused(
                "validation-error", f"validation failed: {type(exc).__name__}", http=503)
            self.writer(receipts.transition, cmd.id, "running", pid=os.getpid())
            refusal.receipt = _receipt_view(self.writer(receipts.transition, cmd.id, "failed",
                                                        result={"refused": refusal.reason, **refusal.data},
                                                        error=str(refusal)))
            self.poll()
            raise refusal from None
        self.writer(receipts.transition, cmd.id, "running", pid=os.getpid())
        t = threading.Thread(target=self._execute, args=(cmd, plan), name=f"office-web-{cmd.id}", daemon=True)
        self.threads[cmd.id] = t
        t.start()
        if wait:
            t.join()
        return _receipt_view(self.writer(receipts.get, cmd.id))

    def wait(self, command_id: str, timeout: float = 30) -> dict:
        t = self.threads.get(command_id)
        if t:
            t.join(timeout)
        return _receipt_view(self.writer(receipts.get, command_id))

    def _replay(self, cmd: Command, existing: dict) -> dict:
        from office.util import sha256_obj
        digest = sha256_obj({"kind": cmd.kind, "target": dumps(cmd.target),
                             "payload": {"target": cmd.target, "expect": cmd.expect, "payload": cmd.payload}})
        if existing["payload_hash"] != digest:
            raise CommandRefused("idempotency-conflict", f"command id {cmd.id} was already used for another request")
        return {**_receipt_view(existing), "replayed": True}

    def _execute(self, cmd: Command, plan: Callable[[], Outcome]) -> None:
        try:
            out = plan()
        except Exception as exc:  # noqa: BLE001 - the effect may have happened
            log.exception("office web: command %s raised", cmd.id)
            out = Outcome("unknown", {}, f"executor raised {type(exc).__name__}")
        try:
            self.writer(receipts.transition, cmd.id, out.status, result=out.result, error=out.error)
            self.poll()
        except Exception:  # noqa: BLE001
            log.exception("office web: recording command %s failed", cmd.id)
        finally:
            self.threads.pop(cmd.id, None)

    # ------------------------------------------------------------------ validation

    def _run(self, target: dict) -> dict:
        run_id = target.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise CommandRefused("bad-target", "target.run_id is required", http=400)
        run = self.observer.run(run_id)
        if run is None:
            raise CommandRefused("run-missing", f"no run {run_id}")
        return run

    def _require_capability(self, run: dict, kind: str) -> dict:
        control = caps.for_run(run, self.resolver, launcher_reason=self.launcher.unavailable())
        if not control[kind]["allowed"]:
            raise CommandRefused("capability-missing", control[kind]["reason"] or "not allowed for this run")
        return control

    def _expect(self, cmd: Command, facts: dict) -> None:
        for key, want in cmd.expect.items():
            if key not in facts:
                raise CommandRefused("bad-expect", f"expect.{key} is not checked for {cmd.kind}", http=400)
            if facts[key] != want:
                raise CommandRefused("expectation-failed", f"{key} is {facts[key]!r}, expected {want!r}",
                                     data={"actual": {key: facts[key]}})

    def _checkout_of(self, run: dict) -> Path | None:
        root = self.observer.read(lambda s: s.rows("SELECT repo_root FROM runs WHERE id=?", (run["run_id"],)))
        path = Path(root[0]["repo_root"]) if root and root[0]["repo_root"] else None
        return path if path and path.is_dir() else (self.checkouts(run["repo"]["slug"]) if run["repo"]["slug"]
                                                    else None)

    def _validate(self, cmd: Command) -> Callable[[], Outcome]:
        return getattr(self, f"_v_{cmd.kind}")(cmd)

    def _issue_target(self, cmd: Command) -> tuple[str, int, dict]:
        repo, number = cmd.target.get("repo"), cmd.target.get("issue")
        if not isinstance(repo, str) or "/" not in repo or not isinstance(number, int):
            raise CommandRefused("bad-target", "target needs repo (owner/name) and issue (number)", http=400)
        gh = self.github.snapshot() if self.github else {"repos": []}
        record = next((r for r in gh["repos"] if r["full_name"].lower() == repo.lower()), None)
        if record is None:
            raise CommandRefused("repo-unknown", f"{repo} is not a discovered repository")
        readiness = self._assess(record, self.checkouts(record["full_name"]))
        if not readiness["ready"]:
            raise CommandRefused("repo-not-ready", f"{repo} is not execution-ready: {', '.join(readiness['failing'])}",
                                 data={"failing": readiness["failing"]})
        return record["full_name"], number, readiness

    def _v_start_issue(self, cmd: Command):
        repo, number, _ = self._issue_target(cmd)
        key = identity.repo_key(repo)
        ref = identity.issue_ref(key, number)
        runs = [r for r in self.observer.workspace()["runs"] if (r.get("issue") or {}).get("ref") == ref]
        live = [r for r in runs if r["liveness"] == "live"]
        if live:
            raise CommandRefused("issue-has-live-run", f"{ref} already has live run {live[0]['run_id']}; attach to it",
                                 data={"run": live[0]["id"], "next": "attach_run"})
        resumable = [r for r in runs if r["liveness"] == "resumable"]
        if resumable and cmd.payload.get("new_run_confirmed") is not True:
            raise CommandRefused("issue-has-resumable-run",
                                 f"{ref} has resumable run {resumable[0]['run_id']}; resume it, or confirm a new run "
                                 "(payload.new_run_confirmed)", data={"run": resumable[0]["id"], "next": "resume_run"})
        end_state = cmd.payload.get("end_state") or "preview"
        if end_state not in ("preview", "merge", "e2e", "ask"):
            raise CommandRefused("bad-payload", "payload.end_state must be preview, merge, e2e or ask", http=400)
        if resumable:
            from office import runtime_default
            try:
                runtime_default.require_new_run_runtime()
            except OfficeError as exc:
                raise CommandRefused("runtime-refuses-new-run", exc.message) from None
        url = f"https://github.com/{repo}/issues/{number}"
        copy = launch_mod.start_command(url, end_state)
        why = self.launcher.unavailable()
        if why:
            raise CommandRefused("launcher-unavailable", why, data={"command": copy})
        checkout = self.checkouts(repo)
        self._expect(cmd, {"live_run": None, "resumable_run": resumable[0]["id"] if resumable else None})

        def go() -> Outcome:
            prompt = launch_mod.start_prompt(issue_ref=url, issue_title=cmd.payload.get("title"),
                                             end_state=end_state, receipt_id=cmd.id)
            res = self.launcher.launch(cwd=checkout, prompt=prompt, label=cmd.id[-8:])
            return self._record_launch(cmd, res, issue=ref, repo=key, copy=copy)
        return go

    def _record_launch(self, cmd: Command, res: dict, *, issue: str | None, repo: str | None, copy: str | None,
                       run: str | None = None) -> Outcome:
        if res.get("pane"):
            with self.lock:
                self.launches[cmd.id] = {"pane": res["pane"], "issue": issue, "repo": repo, "run": run,
                                         "kind": cmd.kind}
                self._save_launches()
        result = {"pane": res.get("pane"), "command": copy}
        if res.get("ok"):
            return Outcome("completed", result)
        return Outcome("unknown" if res.get("pane") else "failed", result, res.get("reason"))

    def _v_queue_issue(self, cmd: Command):
        repo, number, _ = self._issue_target(cmd)
        priority = cmd.payload.get("priority") or "normal"
        if priority not in scheduler.PRIORITY_WEIGHTS:
            raise CommandRefused("bad-payload", "payload.priority must be urgent, high, normal or low", http=400)
        args = ["queue", "add", f"{repo}#{number}", "--priority", priority]
        if cmd.payload.get("title"):
            args += ["--title", str(cmd.payload["title"])]
        return lambda: self.executor.run(args, self.state_home)

    def _live_run(self, cmd: Command, kind: str) -> dict:
        run = self._run(cmd.target)
        if run["liveness"] == "terminal":
            raise CommandRefused("run-terminal", f"run is {run.get('phase')}")
        self._require_capability(run, kind)
        return run

    def _v_resume_run(self, cmd: Command):
        run = self._live_run(cmd, "resume_run")
        if run["liveness"] == "live":
            raise CommandRefused("run-live", "the run already has a live orchestrator; attach to it",
                                 data={"next": "attach_run"})
        self._expect(cmd, {"liveness": run["liveness"], "phase": run["phase"]})
        checkout = self._checkout_of(run)
        if checkout is None:
            raise CommandRefused("repo-not-ready", "the run's checkout is not on this machine")

        def go() -> Outcome:
            res = self.launcher.launch(cwd=checkout, prompt=launch_mod.resume_prompt(run_id=run["run_id"],
                                                                                     receipt_id=cmd.id),
                                       label=cmd.id[-8:])
            return self._record_launch(cmd, res, issue=None, repo=run["repo"]["key"],
                                       copy=f"office resume {run['run_id']}", run=run["run_id"])
        return go

    def _v_attach_run(self, cmd: Command):
        run = self._live_run(cmd, "attach_run")
        if run["liveness"] != "live":
            raise CommandRefused("run-not-live", "the run has no live orchestrator to attach to")
        panes = self.panes()
        pane = next((panes.get(n["id"]) or (n["id"].rsplit("/", 1)[-1] if n["harness"] == "herdr" else None)
                     for n in run["agents"]["columns"]["orchestrators"]), None)
        if not pane:
            raise CommandRefused("no-pane", "no Herdr pane is known for the run's orchestrator")

        def go() -> Outcome:
            ok = self.launcher.focus(pane)
            return Outcome("completed" if ok else "failed", {"pane": pane}, None if ok else "herdr could not focus the pane")
        return go

    def _queue_args(self, cmd: Command, action: str, kind: str) -> tuple[list[str], Path | None]:
        if cmd.target.get("item") and not cmd.target.get("run_id"):
            item = str(cmd.target["item"])
            exists = self.observer.read(lambda s: s.rows("SELECT id FROM sched_items WHERE id=?", (item,)))
            if not exists:
                raise CommandRefused("item-missing", f"no queue item {item}")
            return ["queue", action, item], self.state_home
        run = self._live_run(cmd, kind)
        args = ["queue", action, "--run", run["run_id"]]
        task = cmd.target.get("task_id")
        if task:
            if not any(t["task_id"] == task for t in run["tasks"]):
                raise CommandRefused("task-missing", f"{task} is not a task of run {run['run_id']}")
            args += ["--task", str(task)]
        self._expect(cmd, {"phase": run["phase"], "liveness": run["liveness"]})
        return args, self._checkout_of(run)

    def _v_pause(self, cmd):
        args, cwd = self._queue_args(cmd, "pause", "pause")
        if cmd.payload.get("reason"):
            args += ["--reason", str(cmd.payload["reason"])]
        return lambda: self.executor.run(args, cwd)

    def _v_resume(self, cmd):
        args, cwd = self._queue_args(cmd, "resume", "resume")
        return lambda: self.executor.run(args, cwd)

    def _v_set_priority(self, cmd):
        level = cmd.payload.get("level")
        if level not in scheduler.PRIORITY_WEIGHTS:
            raise CommandRefused("bad-payload", "payload.level must be urgent, high, normal or low", http=400)
        args, cwd = self._queue_args(cmd, "priority", "set_priority")
        if args[2] == "--run":
            args += ["--priority", level]
        else:
            args.append(level)
        return lambda: self.executor.run(args, cwd)

    def _v_demote(self, cmd):
        args, cwd = self._queue_args(cmd, "demote", "demote")
        return lambda: self.executor.run(args, cwd)

    def _v_set_auto_mode(self, cmd):
        mode = cmd.payload.get("mode")
        if mode not in ("on", "off"):
            raise CommandRefused("bad-payload", "payload.mode must be on or off", http=400)
        if cmd.target.get("run_id"):
            run = self._live_run(cmd, "set_auto_mode")
            args, cwd = ["queue", "auto", mode, "--run", run["run_id"]], self._checkout_of(run)
        else:
            args, cwd = ["queue", "auto", mode], self.state_home
        return lambda: self.executor.run(args, cwd)

    def _v_change_route(self, cmd):
        run = self._live_run(cmd, "change_route")
        did = cmd.target.get("dispatch_id")
        route, quote = cmd.payload.get("route"), (cmd.payload.get("quote") or "").strip()
        if not isinstance(route, str) or not re.fullmatch(r"[\w.-]+/[\w.:-]+(@[\w-]+)?", route):
            raise CommandRefused("bad-payload", "payload.route must be harness/model[@effort]", http=400)
        if not quote:
            raise CommandRefused("quote-required", "a route change records the user's words (payload.quote)", http=400)
        current = {n["id"]: n for col in run["agents"]["columns"].values() for n in col if n["kind"] == "dispatch"}
        node = current.get(identity.dispatch(str(did)))
        if node is None or node["state"]["process"] == "exited" or node["state"]["stale"] or node["state"]["complete"]:
            raise CommandRefused("dispatch-not-current", f"{did} is not a current live dispatch of the run")
        if route.split("/", 1)[0] != node["harness"]:
            raise CommandRefused("harness-mismatch", f"{did} runs on {node['harness']}; a route change keeps the harness")
        self._expect(cmd, {"dispatch_id": did, "route": f"{node['harness']}/{node['model']}@{node['effort']}"})
        args = ["--run", run["run_id"], "amend", "route", str(did), "--as", route, "--quote", quote, "--restart"]
        return lambda: self.executor.run(args, self._checkout_of(run))

    def _v_chat_send(self, cmd):
        text = cmd.payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > chat.MAX_TEXT:
            raise CommandRefused("bad-payload", f"payload.text must be 1-{chat.MAX_TEXT} characters", http=400)
        resend = cmd.payload.get("resend_of")
        if resend is not None:
            prior = self.observer.read(lambda s: _receipt(s, str(resend)))
            if prior is None or prior["kind"] != "chat_send" or prior["id"] == cmd.id:
                raise CommandRefused("bad-resend", "payload.resend_of must name an earlier chat_send command")
        run = self._live_run(cmd, "chat_send")
        try:
            where = chat.resolve(run, cmd.target, self.host_id, self.panes())
        except chat.ChatRefused as exc:
            raise CommandRefused(exc.reason, str(exc)) from None
        if not self.launcher.agent_live(where["pane"]):
            raise CommandRefused("agent-not-live", "no live Herdr agent on the orchestrator's pane")

        def go() -> Outcome:
            status, result, error = chat.deliver(self.launcher, where["pane"], text)
            return Outcome(status, {**result, "session": where["session"]}, error)
        return go

    def _settings_target(self, cmd: Command) -> tuple[str, Path | None]:
        tier, key = cmd.target.get("tier"), cmd.target.get("key")
        if tier not in settings_mod.EDITABLE:
            raise CommandRefused("tier-not-editable", "target.tier must be machine or repository", http=400)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_-]+)*", key):
            raise CommandRefused("bad-target", "target.key must be a dotted config key", http=400)
        if tier == "machine":
            return tier, self.state_home
        run = self._run(cmd.target) if cmd.target.get("run_id") else None
        checkout = self._checkout_of(run) if run else (self.checkouts(cmd.target["repo"])
                                                       if cmd.target.get("repo") else None)
        if checkout is None:
            raise CommandRefused("repo-not-ready", "a repository setting needs the repository's local checkout")
        return tier, checkout

    def _v_settings_set(self, cmd):
        tier, cwd = self._settings_target(cmd)
        if "value" not in cmd.payload:
            raise CommandRefused("bad-payload", "payload.value is required", http=400)
        args = settings_mod.config_args(cmd.kind, tier, cmd.target["key"], cmd.payload["value"])
        return lambda: self.executor.run(args, cwd)

    def _v_settings_unset(self, cmd):
        tier, cwd = self._settings_target(cmd)
        args = settings_mod.config_args(cmd.kind, tier, cmd.target["key"])
        return lambda: self.executor.run(args, cwd)

    # ------------------------------------------------------------------ reads for the API

    def settings_view(self, *, run_id: str | None = None, repo: str | None = None) -> dict:
        if run_id:
            row = self.observer.read(lambda s: s.rows("SELECT id, repo_root, policy_json FROM runs WHERE id=?",
                                                      (run_id,)))
            if not row:
                raise CommandRefused("run-missing", f"no run {run_id}", http=404)
            root = Path(row[0]["repo_root"]) if row[0]["repo_root"] else None
            pinned = _loads(row[0]["policy_json"])
            return settings_mod.view(scope="run", run=identity.run(run_id),
                                     repo_root=root if root and root.is_dir() else None, pinned=pinned)
        if repo:
            return settings_mod.view(scope="repository", repo=repo, repo_root=self.checkouts(repo))
        return settings_mod.view(scope="machine")

    def activity(self, run_id: str, *, limit: int | None, before_seq: int | None) -> dict:
        return self.observer.activity(run_id, limit=limit, before_seq=before_seq)

    def command(self, command_id: str) -> dict | None:
        row = self.observer.read(lambda s: _receipt(s, command_id))
        return _receipt_view(row) if row else None


# ------------------------------------------------------------------ helpers

def _loads(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _effective_config() -> dict:
    from office import config as cfg
    try:
        return cfg.resolve(None)[0]
    except (OSError, ValueError):
        return {}


def _split_ref(ref: str | None) -> tuple[str | None, int | None]:
    m = re.search(r"([\w.-]+/[\w.-]+?)(?:#|/issues/)(\d+)$", str(ref or "").strip())
    return (m.group(1), int(m.group(2))) if m else (None, None)


def _github_signature(gh: dict) -> str:
    """GitHub data and per-source states, without the ages that move every second."""
    fresh = gh["freshness"]
    states = {"discovery": (fresh.get("discovery") or {}).get("state"),
              "repos": {r: {k: v["state"] for k, v in src.items()} for r, src in fresh["repos"].items()}}
    return json.dumps({k: gh[k] for k in ("repos", "issues", "pulls", "github_checks")} | {"states": states},
                      sort_keys=True, default=str)


def _receipt(snap, command_id: str) -> dict | None:
    if not snap.has("commands"):
        return None
    rows = snap.rows("SELECT * FROM commands WHERE id=?", (command_id,))
    return rows[0] if rows else None


def _receipt_view(row: dict | None) -> dict | None:
    if row is None:
        return None
    payload = _loads(row.get("payload_json"))
    return {"id": row["id"], "kind": row["kind"], "status": row["status"], "origin": row.get("origin"),
            "target": payload.get("target"), "error": row.get("error"), "result": _loads(row.get("result_json")),
            "accepted_at": row.get("accepted_at"), "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"), "resend_of": (payload.get("payload") or {}).get("resend_of")}


def _queue_rows(snap, conf: dict) -> tuple[list[dict], list[dict], str]:
    from office import queuecmd
    if not snap.has("sched_items"):
        return [], [], "unknown"
    rows, active = queuecmd._projection(snap.con, conf)
    auto = queuecmd.auto_mode(snap.con, default_on=scheduler.settings(conf)["auto_mode"])
    return rows, active, auto


def _diff(prev: dict | None, snap: dict) -> dict:
    base = prev["rev"] if prev else 0
    upserts, removes, scalars = {}, {}, {}
    old_ent = prev["entities"] if prev else {}
    for coll, items in snap["entities"].items():
        before = old_ent.get(coll, {})
        changed = {k: v for k, v in items.items() if before.get(k) != v}
        gone = [k for k in before if k not in items]
        if changed:
            upserts[coll] = changed
        if gone:
            removes[coll] = gone
    old_sc = prev["scalars"] if prev else {}
    for k, v in snap["scalars"].items():
        if old_sc.get(k) != v:
            scalars[k] = v
    if not prev or prev["freshness"] != snap["freshness"]:
        scalars["freshness"] = snap["freshness"]
    return {"epoch": snap["epoch"], "rev": snap["rev"], "base_rev": base, "upserts": upserts, "removes": removes,
            "scalars": scalars}
