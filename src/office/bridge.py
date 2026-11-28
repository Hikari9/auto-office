"""A narrow, externally admitted stdio interface for a trusted Office worker.

This module is not a network server, credential broker or engineering runtime.
The caller's admission service owns distributed leases, resource exclusion and
policy. Office still owns its lifecycle, routes, review and authorization gates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from office import commands, db, paths, state
from office.util import DEAD, claim_liveness, loads, now_iso, pid_gone, process_start

PROTOCOL = "rico-office-v1"
MAX_INPUT = 64 * 1024
MAX_OUTPUT = 1024 * 1024
OPERATIONS = {"start", "dispatch", "status", "cancel", "resume", "evidence"}
SAFE_INTEGER = 2**53 - 1
TASK = re.compile(r"T[1-9][0-9]{0,5}\Z")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
ISSUE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*\Z")


class BridgeError(Exception):
    def __init__(self, category: str, message: str):
        self.category, self.message = category, message
        super().__init__(message)


def canonical(request: dict) -> str:
    """Cross-language digest contract: sorted keys, compact JSON, literal UTF-8."""
    return json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def request_digest(request: dict) -> str:
    return hashlib.sha256(canonical(request).encode("utf-8")).hexdigest()


def _text(value, name: str, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise BridgeError("invalid-request", f"{name} must be a nonempty string of at most {maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise BridgeError("invalid-request", f"{name} must be valid Unicode") from None
    return value


def _integer(value, name: str, minimum: int = 0, maximum: int = SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BridgeError("invalid-request", f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _keys(value, allowed: set[str], required: set[str], name: str) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise BridgeError("invalid-request", f"{name} must be an object")
    if set(value) - allowed or required - set(value):
        raise BridgeError("invalid-request", f"{name} has missing or unsupported fields")
    return value


def validate(request: dict) -> dict:
    fields = {"protocol", "request_id", "mission_id", "coordinator_id", "fence", "operation", "arguments"}
    _keys(request, fields, fields, "request")
    if request["protocol"] != PROTOCOL or not isinstance(request["operation"], str) or request["operation"] not in OPERATIONS:
        raise BridgeError("invalid-request", "unsupported protocol or operation")
    for field in ("request_id", "mission_id"):
        value = _text(request[field], field, 36)
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError()
        except ValueError:
            raise BridgeError("invalid-request", f"{field} must be a canonical lowercase UUID") from None
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", _text(request["coordinator_id"], "coordinator_id", 128)):
        raise BridgeError("invalid-request", "invalid coordinator_id")
    _integer(request["fence"], "fence", 1)
    op, a = request["operation"], request["arguments"]
    allowed = {
        "start": ({"repository", "goal", "gear", "playbook", "issue"}, {"repository", "goal", "gear"}),
        "dispatch": ({"tasks", "parallel"}, {"tasks"}),
        "status": (set(), set()), "cancel": ({"reason"}, {"reason"}),
        "resume": (set(), set()), "evidence": ({"after", "limit"}, set()),
    }
    _keys(a, *allowed[op], "arguments")
    if op == "start":
        if not ALIAS.fullmatch(_text(a["repository"], "repository", 100)):
            raise BridgeError("invalid-request", "repository must be a configured workspace alias")
        _text(a["goal"], "goal", 16000)
        if a["gear"] not in ("XS", "S", "M", "L", "XL"):
            raise BridgeError("invalid-request", "gear must be XS, S, M, L or XL")
        if a.get("playbook", "Change") not in ("Change", "Restructure", "Investigate", "Prototype", "Visual"):
            raise BridgeError("invalid-request", "unsupported playbook")
        if "issue" in a and not ISSUE.fullmatch(_text(a["issue"], "issue", 500)):
            raise BridgeError("invalid-request", "issue must be a GitHub issue URL")
    elif op == "dispatch":
        tasks = a["tasks"]
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 50 or not all(
            isinstance(t, str) and TASK.fullmatch(t) for t in tasks
        ) or len(set(tasks)) != len(tasks):
            raise BridgeError("invalid-request", "tasks must name 1 to 50 distinct Office tasks")
        if "parallel" in a and type(a["parallel"]) is not bool:
            raise BridgeError("invalid-request", "parallel must be a boolean")
    elif op == "cancel":
        _text(a["reason"], "reason", 500)
    elif op == "evidence":
        _integer(a.get("after", 0), "after")
        _integer(a.get("limit", 40), "limit", 1, 100)
    if len(canonical(request).encode("utf-8")) > MAX_INPUT:
        raise BridgeError("invalid-request", "request is too large")
    return request


def _admit(request: dict, authorize: Callable[[dict], dict]) -> None:
    try:
        answer = authorize(request)
    except Exception:
        raise BridgeError("admission-unavailable", "current mission authority could not be verified") from None
    expected = {k: request[k] for k in ("mission_id", "request_id", "fence")}
    expected["request_digest"] = request_digest(request)
    if not isinstance(answer, dict) or answer.get("allowed") is not True or any(
        answer.get(k) != v or type(answer.get(k)) is not type(v) for k, v in expected.items()
    ):
        raise BridgeError("admission-denied", "admission did not attest this exact request and current fence")


def _binding(con, mission_id: str) -> dict | None:
    row = con.execute("SELECT * FROM bridge_missions WHERE mission_id=?", (mission_id,)).fetchone()
    return dict(row) if row else None


def _workspace(registry: dict[str, str], alias: str) -> Path:
    raw = registry.get(alias)
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise BridgeError("workspace-unavailable", "workspace is not in the operator's registry")
    target = Path(raw).resolve()
    if paths.repo_identity(target) is None:
        raise BridgeError("workspace-unavailable", "configured workspace is not an available Git checkout")
    return target


def _run(con, binding: dict, registry: dict[str, str]) -> tuple[dict, Path]:
    if not binding.get("run_id"):
        raise BridgeError("start-indeterminate", "mission start has no confirmed run; inspect its receipt before recovery")
    run = state.find_run(con, binding["run_id"])
    if run is None or run["id"] != binding["run_id"]:
        raise BridgeError("run-unavailable", "the mission's exact Office run is unavailable")
    cwd = _workspace(registry, binding["repository"])
    ident = paths.repo_identity(cwd)
    if not ident or ident[1] != Path(run["git_common_dir"]).resolve():
        raise BridgeError("workspace-conflict", "workspace registry no longer matches the mission's run")
    return run, cwd


def _command_id(request: dict) -> str:
    return f"{PROTOCOL}:{request['request_id']}"


def _receipt(row: dict) -> dict:
    raw = loads(row["result_json"]) if row.get("result_json") else None
    return {"request_id": row["id"].removeprefix(PROTOCOL + ":"), "status": row["status"],
            "result": _safe_result(raw) if raw else None,
            "error": "outcome-indeterminate" if row.get("error") else None,
            "started_at": row.get("started_at"), "finished_at": row.get("finished_at")}


def _safe_result(raw: dict, run_id: str | None = None) -> dict:
    """Only typed identifiers and outcome metadata cross into cloud context.

    Office's normal result includes free-form lines, handoff errors, next-step
    text and local paths. These remain available through the local Office CLI.
    Apply this projection on reads too, including historical receipt rows.
    """
    source = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    data = source.get("data") if isinstance(source.get("data"), dict) else {}
    rid = run_id or data.get("run_id")
    safe_data = {"run_id": rid} if isinstance(rid, str) and re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{24})", rid) else {}
    result = {"ok": source.get("ok") is True, "data": safe_data}
    error = source.get("error")
    if isinstance(error, dict):
        category = error.get("category")
        result["error"] = {"category": category if isinstance(category, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,79}", category)
                           else "office-refused"}
    code = raw.get("exit_code")
    out = {"exit_code": code if type(code) is int and -255 <= code <= 255 else None, "result": result}
    cancellation = raw.get("cancellation")
    if isinstance(cancellation, dict):
        out["cancellation"] = {
            "known_workers_stopped": cancellation.get("known_workers_stopped") is True,
            "pending_workers": [{"id": p["id"], "state": p["state"]} for p in cancellation.get("pending_workers", [])[:100]
                                if isinstance(p, dict) and isinstance(p.get("id"), str)
                                and re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", p["id"])
                                and p.get("state") in ("unverified", "alive-or-unknown")],
            "external_effects_verified": False,
        }
        count = cancellation.get("pending_worker_count")
        out["cancellation"]["pending_worker_count"] = count if type(count) is int and count >= 0 else len(out["cancellation"]["pending_workers"])
    return out


def _monitor(con, request: dict, binding: dict | None, registry: dict) -> dict:
    # Deliberately not office status/context: those paths can reconcile and kick jobs.
    receipts = [_receipt(dict(r)) for r in con.execute(
        "SELECT * FROM commands WHERE origin=? ORDER BY accepted_at DESC LIMIT 40",
        (f"{PROTOCOL}:{request['mission_id']}",))]
    if not binding or not binding["run_id"]:
        candidate = _start_candidate(con, binding, registry) if binding else None
        return {"binding": binding, "receipts": receipts, "run": None,
                "recovery_candidate": candidate["id"] if candidate else None,
                "known_workers_stopped": False, "pending_worker_count": 0, "pending_workers": [],
                "external_effects_verified": False}
    run, _ = _run(con, binding, registry)
    with _read_transaction(con):
        run = state.get_run(con, run["id"])
        tasks = [dict(r) for r in con.execute(
            "SELECT id,status,current_dispatch_id,accepted_revision_id FROM tasks WHERE run_id=? ORDER BY id LIMIT 100",
            (run["id"],))]
        gates = [dict(r) for r in con.execute(
            "SELECT id,task_id,kind,status,verdict,independence,evidence_status FROM gates WHERE run_id=? ORDER BY created_at DESC LIMIT 100",
            (run["id"],))]
        paused = con.execute("SELECT paused FROM sched_items WHERE id=?", (f"run:{run['id']}",)).fetchone()
        observations = _preserve_observations(con, request["mission_id"], _live_processes(con, run["id"], after=True))
        drained = _safe_result({"cancellation": _cancellation(observations)})["cancellation"]
        # A still-active run may start new queued work after this snapshot.
        drained["known_workers_stopped"] = drained["known_workers_stopped"] and state.is_terminal(run)
        return {"binding": binding, "receipts": receipts,
                "run": {k: run.get(k) for k in ("id", "phase", "gear", "office_version", "base_sha")},
                "tasks": tasks, "gates": gates, "paused": bool(paused and paused[0]),
                "terminal": state.is_terminal(run), **drained}


@contextmanager
def _read_transaction(con):
    con.execute("BEGIN")
    try:
        yield
    finally:
        con.execute("ROLLBACK")


def _evidence(con, run_id: str, arguments: dict) -> dict:
    after, limit = arguments.get("after", 0), arguments.get("limit", 40)
    with _read_transaction(con):
        rows = [dict(r) for r in con.execute(
            "SELECT rowid AS cursor,id,kind,task_id,gate_id,revision_id,sha256,bytes,created_at FROM evidence "
            "WHERE run_id=? AND rowid>? ORDER BY rowid LIMIT ?", (run_id, after, limit + 1))]
        shown = rows[:limit]
        return {"run_id": run_id, "evidence": shown, "has_more": len(rows) > limit,
                "next_cursor": shown[-1]["cursor"] if shown else after,
                "content_included": False, "note": "Evidence references and recorded hashes; raw files remain on the worker."}


def run_cli(argv: list[str], cwd: Path, timeout: int) -> dict:
    # No shell and no request-supplied environment, executable, cwd or switches.
    # Keep admission credentials out of Office's descendants.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RICO_", "CLOUD_RICO_", "OFFICE_BRIDGE_"))}
    for key in ("OFFICE_RUN_ID", "OFFICE_TASK_ID", "OFFICE_DISPATCH_ID", "OFFICE_ROLE", "OFFICE_SESSION", "HERDR_PANE_ID"):
        env.pop(key, None)
    proc = subprocess.run([sys.executable, "-m", "office", *argv], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=timeout)
    if len(proc.stdout.encode("utf-8")) > MAX_OUTPUT:
        raise BridgeError("outcome-unknown", "Office output exceeded the response limit; inspect durable run state")
    try:
        result = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise BridgeError("outcome-unknown", "Office did not return a JSON receipt; inspect durable run state") from None
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise BridgeError("outcome-unknown", "Office returned an invalid receipt")
    return {"exit_code": proc.returncode, "result": result}


def _argv(request: dict, run_id: str | None = None) -> list[str]:
    op, a = request["operation"], request["arguments"]
    if op == "start":
        args = ["start", "--json", "--gear", a["gear"], "--playbook", a.get("playbook", "Change"),
                "--end-state", "ask", "--no-card", "--harness", PROTOCOL, "--session", request["request_id"]]
        if "issue" in a:
            args += ["--issue", a["issue"]]
        return args + ["--", a["goal"]]
    args = ["--json", "--run", run_id]
    if op == "dispatch":
        return args + ["dispatch", *( ["--parallel"] if a.get("parallel") else []), *a["tasks"]]
    if op == "cancel":
        return args + ["close", "--abandon=" + a["reason"]]
    if op == "resume":
        return args + ["resume", "--wake-off"]
    raise BridgeError("invalid-operation", "operation does not launch an Office command")


def _live_processes(con, run_id: str, *, after: bool = False) -> list[dict]:
    dispatch_status = "('launching','running','cancelled')" if after else "('launching','running')"
    rows = [dict(r) for r in con.execute(
        f"SELECT id,pid,launcher FROM dispatches WHERE run_id=? AND status IN {dispatch_status}", (run_id,))]
    for row in rows:
        row["source"] = "dispatch"
        # A post-cancel PID alone cannot establish which process belonged to a dispatch.
        row["start"] = None if after else process_start(row["pid"])
    job_status = "(status='claimed' OR (status='cancelled' AND attempts>0))" if after else "status='claimed'"
    rows += [{"id": r["id"], "pid": r["claimed_pid"], "launcher": "job", "source": "job", "claimed_by": r["claimed_by"], "attempt": r["attempts"]}
             for r in con.execute(f"SELECT id,claimed_pid,claimed_by,attempts FROM outbox WHERE run_id=? AND {job_status}", (run_id,))]
    return rows


def _merge_observations(current: list[dict], previous: list[dict]) -> list[dict]:
    """Fill cleared identities but retain distinct attempts, even for one job ID."""
    observations = [dict(p) for p in current]
    for saved in previous:
        found = False
        for i, fresh in enumerate(observations):
            if (fresh["source"], fresh["id"], fresh.get("attempt")) != (saved["source"], saved["id"], saved.get("attempt")):
                continue
            if fresh.get("pid") is None:
                observations[i] = dict(saved)
                found = True
                break
            if saved.get("pid") is None:
                found = True
                break
            if fresh["pid"] == saved["pid"]:
                identity = "claimed_by" if fresh["source"] == "job" else "start"
                if not fresh.get(identity):
                    observations[i][identity] = saved.get(identity)
                    found = True
                    break
                if not saved.get(identity) or fresh[identity] == saved[identity]:
                    found = True
                    break
        if not found:
            observations.append(dict(saved))
    return observations


def _preserve_observations(con, mission_id: str, current: list[dict]) -> list[dict]:
    previous = con.execute("SELECT result_json FROM commands WHERE origin=? AND kind='bridge.cancel' AND status='completed' "
                           "ORDER BY finished_at DESC LIMIT 1", (f"{PROTOCOL}:{mission_id}",)).fetchone()
    if previous and previous[0]:
        return _merge_observations(current, loads(previous[0]).get("private_worker_observations", []))
    return current


def _cancellation(processes: list[dict]) -> dict:
    pending = []
    for item in processes:
        pid = item["pid"]
        if not pid:
            pending.append({"id": item["id"], "state": "unverified", "reason": "worker exposes no process ID"})
        elif item["source"] == "job":
            if claim_liveness(pid, item.get("claimed_by"))[0] != DEAD and not pid_gone(pid):
                pending.append({"id": item["id"], "state": "alive-or-unknown"})
        elif not pid_gone(pid):
            current = process_start(pid)
            if not (item["start"] and current and item["start"] != current):
                pending.append({"id": item["id"], "state": "alive-or-unknown"})
    return {"known_workers_stopped": not pending, "pending_workers": pending[:100], "pending_worker_count": len(pending),
            "external_effects_verified": False,
            "note": "Cancellation is terminal for this Office run. Reconcile external effects before releasing shared resources."}


def _start_candidate(con, binding: dict, registry: dict) -> dict | None:
    """Office commits its session binding in the same transaction as run creation.

    That existing correlation survives a lost stdout response. Never guess from
    matching timestamps, goals or a process ID.
    """
    row = con.execute("SELECT run_id FROM session_bindings WHERE harness=? AND session_id=? AND bound_by='start'",
                      (PROTOCOL, binding["start_request_id"])).fetchone()
    if not row:
        return None
    candidate = state.find_run(con, row["run_id"])
    original = commands.get(con, f"{PROTOCOL}:{binding['start_request_id']}")
    if candidate is None or not original or original["origin"] != f"{PROTOCOL}:{binding['mission_id']}":
        raise BridgeError("recovery-conflict", "start correlation cannot be verified")
    payload = loads(original["payload_json"])
    cwd = _workspace(registry, binding["repository"])
    if (candidate["goal"] != payload["arguments"]["goal"].strip()
            or Path(candidate["git_common_dir"]).resolve() != paths.repo_identity(cwd)[1]):
        raise BridgeError("recovery-conflict", "correlated run differs from the recorded mission intent or workspace")
    return candidate


def execute(request: dict, *, authorize: Callable[[dict], dict], workspaces: dict[str, str],
            runner: Callable = run_cli, timeout: int = 120) -> dict:
    request = validate(request)
    # The callback must validate current lease and policy, not merely sign input.
    _admit(request, authorize)
    con = db.connect()
    try:
        mission_id, op = request["mission_id"], request["operation"]
        binding = _binding(con, mission_id)
        if op == "status":
            return {"protocol": PROTOCOL, "ok": True, "data": _monitor(con, request, binding, workspaces)}
        if op == "evidence":
            if not binding:
                raise BridgeError("mission-unbound", "this mission has no Office run")
            run, _ = _run(con, binding, workspaces)
            return {"protocol": PROTOCOL, "ok": True, "data": _evidence(con, run["id"], request["arguments"])}
        command_id = _command_id(request)
        # Receipt, mission reservation and the running claim are one transaction.
        with db.transaction(con):
            receipt = commands.record(con, command_id=command_id, kind=f"bridge.{op}", target=mission_id,
                                      payload=request, origin=f"{PROTOCOL}:{mission_id}")
            if receipt["replayed"]:
                return {"protocol": PROTOCOL, "ok": receipt["status"] == "completed", "replayed": True,
                        "receipt": _receipt(receipt)}
            binding = _binding(con, mission_id)
            if op == "start":
                cwd = _workspace(workspaces, request["arguments"]["repository"])
                if binding:
                    raise BridgeError("mission-already-bound", "mission already has a start receipt; inspect status instead")
                con.execute("INSERT INTO bridge_missions(mission_id,repository,start_request_id,created_at) VALUES(?,?,?,?)",
                            (mission_id, request["arguments"]["repository"], request["request_id"], now_iso()))
                run_id = None
            else:
                if not binding:
                    raise BridgeError("mission-unbound", "this mission has no Office run")
                if not binding["run_id"] and op == "resume":
                    candidate = _start_candidate(con, binding, workspaces)
                    if candidate:
                        con.execute("UPDATE bridge_missions SET run_id=? WHERE mission_id=?", (candidate["id"], mission_id))
                        binding = _binding(con, mission_id)
                run, cwd = _run(con, binding, workspaces)
                run_id = run["id"]
            commands.transition(con, command_id, "running", pid=os.getpid())
        try:
            processes = _live_processes(con, run_id) if op == "cancel" else []
            if op == "resume":
                paused = con.execute("SELECT paused FROM sched_items WHERE id=?", (f"run:{run_id}",)).fetchone()
                if paused and paused[0]:
                    unpause = runner(["--json", "--run", run_id, "queue", "resume"], cwd, timeout)
                    if unpause["exit_code"] != 0 or not unpause["result"]["ok"]:
                        result = unpause
                    else:
                        result = runner(_argv(request, run_id), cwd, timeout)
                else:
                    result = runner(_argv(request, run_id), cwd, timeout)
            else:
                result = runner(_argv(request, run_id), cwd, timeout)
            completed = result["exit_code"] == 0 and result["result"].get("ok") is True
            if op == "start" and completed:
                new_run = result["result"].get("data", {}).get("run_id")
                if not isinstance(new_run, str) or state.find_run(con, new_run) is None:
                    raise BridgeError("outcome-unknown", "start returned no verifiable run ID")
                with db.transaction(con):
                    con.execute("UPDATE bridge_missions SET run_id=? WHERE mission_id=?", (new_run, mission_id))
            if op == "cancel" and completed:
                # Include workers that started between the initial snapshot and
                # the terminal transaction, retaining durable job identities.
                observed = _merge_observations(_live_processes(con, run_id, after=True), processes)
                observations = _preserve_observations(con, mission_id, observed)
                result["cancellation"] = _cancellation(observations)
            stored = _safe_result(result, new_run if op == "start" and completed else run_id)
            if op == "cancel" and completed:
                stored["private_worker_observations"] = observations
            row = commands.transition(con, command_id, "completed" if completed else "failed",
                                      result=stored)
        except Exception:
            # A timeout/transport crash can follow an actual mutation. Never replay it automatically.
            row = commands.transition(con, command_id, "unknown", error="Office outcome is indeterminate; inspect run and external effects before recovery")
        return {"protocol": PROTOCOL, "ok": row["status"] == "completed", "replayed": False, "receipt": _receipt(row)}
    finally:
        con.close()


def _load_config() -> dict:
    path = Path(os.environ.get("OFFICE_BRIDGE_CONFIG", ""))
    if not os.environ.get("OFFICE_BRIDGE_CONFIG") or not path.is_absolute():
        raise BridgeError("configuration-required", "set OFFICE_BRIDGE_CONFIG to an operator-owned absolute JSON path")
    try:
        info = path.stat()
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or info.st_uid not in (0, os.getuid()):
            raise BridgeError("unsafe-configuration", "bridge configuration must be operator-owned and not writable by group or others")
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BridgeError("configuration-unavailable", "cannot read bridge configuration") from None
    _keys(config, {"workspaces", "admission_argv", "timeout_seconds"}, {"workspaces", "admission_argv"}, "configuration")
    argv = config["admission_argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v and "\x00" not in v for v in argv) or not Path(argv[0]).is_absolute():
        raise BridgeError("invalid-configuration", "admission_argv must name an absolute operator executable")
    if not isinstance(config["workspaces"], dict):
        raise BridgeError("invalid-configuration", "workspaces must be an alias-to-absolute-path map")
    _integer(config.get("timeout_seconds", 120), "timeout_seconds", 1, 600)
    return config


def _admission(config: dict, request: dict) -> dict:
    proc = subprocess.run(config["admission_argv"], input=canonical(request), capture_output=True,
                          text=True, timeout=15)
    if proc.returncode or len(proc.stdout) > MAX_INPUT:
        raise BridgeError("admission-denied", "admission executable did not return an authorization")
    return json.loads(proc.stdout)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--help"]:
        print("office bridge: one rico-office-v1 JSON request on stdin; requires OFFICE_BRIDGE_CONFIG. See docs/cloud-bridge.md.")
        return 0
    try:
        if argv:
            raise BridgeError("invalid-request", "office bridge accepts JSON on stdin, no command arguments")
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise BridgeError("invalid-request", "request is too large")
        request = json.loads(raw)
        config = _load_config()
        result = execute(request, authorize=lambda value: _admission(config, value),
                         workspaces=config["workspaces"], timeout=config.get("timeout_seconds", 120))
    except (ValueError, UnicodeError):
        result = {"protocol": PROTOCOL, "ok": False, "error": {"category": "invalid-json", "message": "request must be UTF-8 JSON"}}
    except (BridgeError, state.OfficeError) as exc:
        result = {"protocol": PROTOCOL, "ok": False, "error": {"category": exc.category, "message": exc.message}}
    except Exception:
        # Never print hook stderr, environments, arbitrary paths or exception payloads.
        result = {"protocol": PROTOCOL, "ok": False, "error": {"category": "bridge-unavailable", "message": "worker failure; inspect durable receipts before retrying"}}
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
