"""`office raw <legacy-subcommand>`: the bounded 3.1.x compatibility window.

Every call warns and is recorded in runs.db (compat_calls) so remaining
consumers are discoverable before removal in 3.2.0. A legacy v3 run is
forwarded to its exact retained runtime. A 3.1 run is never written by a
legacy helper: read-only verbs are answered from canonical state, pure helpers
run unchanged, and state-writing verbs are refused with their semantic
replacement.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from office import db, discovery, legacy, paths, state, version
from office.result import Result
from office.state import Refused
from office.util import dumps, now_iso

REMOVAL = "3.2.0"

PURE = {"validate-packet", "validate-adapter", "route", "hash", "maturity", "privacy-lint", "scaffold-adapter",
        "catalog-snapshot", "effective-config", "proposal-id", "replay", "spoke-digest", "validate-checkpoint",
        "validate-landing", "validate-review", "reuse-plan", "plan-review-round-authorized", "init-db", "tmp-dir"}

REPLACEMENT = {
    "start": 'office start "<goal>"', "new-run": 'office start "<goal>"', "state-save": "runtime-owned (no command)",
    "state-load": "office inspect run --json", "increment-plan": 'office amend plan -- "<delta>"',
    "invalidate-packets": 'office amend plan -- "<delta>"', "approve-plan": 'office approve plan --quote "<words>"',
    "freeze-intent": "office submit (the plan's ## Requirements)", "amend": 'office amend <scope> -- "<delta>"',
    "family-update": "runtime-owned", "family-show": "office inspect run", "family-list": "office list",
    "family-focus": "office resume <run>", "mark-spoke": "retired: role briefs are delivered just in time",
    "check-spoke": "retired: role briefs are delivered just in time", "record-dispatch": "runtime-owned (office dispatch)",
    "record-finding": "runtime-owned (office submit)", "record-validation": "runtime-owned (office submit)",
    "record-review": "runtime-owned (office submit)", "record-landing": "office close", "verify-landing": "office close",
    "record-event": "runtime-owned", "list-events": "office inspect events", "ack-event": "office ack <amendment-id>",
    "completion-status": "office status", "record-start-receipt": "runtime-owned (office dispatch)",
    "save-checkpoint": "runtime-owned (office submit captures revisions)", "load-checkpoint": "office inspect task <id>",
    "lease-acquire": "runtime-owned (office dispatch)", "lease-renew": "runtime-owned", "lease-release": "runtime-owned",
    "lease-check": "office inspect task <id>", "state-reconcile": "office resume", "route-defect": "office inspect route",
    "resolve-route-defect": "runtime-owned", "check-route-defects": "office close", "resolve-gates": "office inspect run",
    "cleanup-worktrees": "office prune", "tmp-dir": "runtime-owned",
}


def record(command: str, argv: list[str], run_id: str | None, outcome: str) -> None:
    try:
        con = db.connect()
        try:
            with db.transaction(con):
                con.execute("INSERT INTO compat_calls(id, at, office_version, run_id, command, argv_json, caller, outcome) "
                            "VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, now_iso(), version.current(), run_id, command,
                                                        dumps(argv), os.environ.get("AI_AGENT") or os.environ.get("OFFICE_ROLE"),
                                                        outcome))
        finally:
            con.close()
    except Exception:
        pass


def warn(command: str) -> None:
    sys.stderr.write(f"office raw {command}: deprecated compatibility path (removed in Auto Office {REMOVAL}); "
                     f"use {REPLACEMENT.get(command, 'the office semantic commands')}\n")


def _state_dir_arg(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--state-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--state-dir="):
            return a.split("=", 1)[1]
    return None


def raw(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: office raw <legacy-subcommand> [args...]\n")
        return 2
    command = argv[0]
    warn(command)
    sdir = _state_dir_arg(argv) or os.environ.get("OFFICE_STATE_DIR")
    target_run = None
    if sdir:
        leg = legacy.read_legacy_state(Path(sdir).expanduser())
        if leg is not None:
            record(command, argv, leg.run_id, "forwarded-legacy")
            return forward_legacy(leg, argv)
        con = db.connect()
        try:
            target_run = state.get_run(con, Path(sdir).expanduser().name)
        finally:
            con.close()
    if target_run is None and os.environ.get("OFFICE_RUN_ID"):
        con = db.connect()
        try:
            target_run = state.find_run(con, os.environ["OFFICE_RUN_ID"])
        finally:
            con.close()
    if command in PURE:
        record(command, argv, target_run["id"] if target_run else None, "pure-helper")
        return _run_legacy_script(argv)
    if target_run is not None:
        if command == "state-load":
            record(command, argv, target_run["id"], "answered-from-canonical")
            con = db.connect()
            try:
                view = {**{k: v for k, v in state.get_run(con, target_run["id"]).items() if not k.endswith("_json")},
                        "_authority": "runs.db", "_note": "read-only compatibility view"}
            finally:
                con.close()
            print(json.dumps(view, indent=2, sort_keys=True, default=str))
            return 0
        record(command, argv, target_run["id"], "refused-3.1-run")
        sys.stderr.write(f"refused: run {target_run['id'][:8]} is owned by Auto Office {target_run['office_version']} "
                         f"(runs.db authority); a legacy helper cannot write it. use: {REPLACEMENT.get(command, 'office --help')}\n")
        return 4
    record(command, argv, None, "refused-no-legacy-target")
    sys.stderr.write(f"refused: `{command}` only operates on a legacy 3.0 run (pass its --state-dir). "
                     f"For new work use: {REPLACEMENT.get(command, 'office --help')}\n")
    return 4


def _run_legacy_script(argv: list[str]) -> int:
    script = paths.resources_root() / "scripts" / "office_runtime.py"
    if not script.is_file():
        sys.stderr.write("legacy helpers are not packaged in this install; use the office commands\n")
        return 4
    env = dict(os.environ, OFFICE_RAW_PASSTHROUGH="1")
    return subprocess.call([sys.executable, str(script), *argv], env=env)


def forward_legacy(leg: legacy.LegacyRun, argv: list[str]) -> int:
    runtime = legacy.retained_runtime(leg.plugin_commit)
    if runtime is None:
        msg, nxt = legacy.guidance(leg)
        sys.stderr.write(f"blocked: {msg}\nnext: {nxt}\n")
        return 5
    env = dict(os.environ, OFFICE_PINNED_LEGACY="1", AUTO_OFFICE_PLUGIN_COMMIT=leg.plugin_commit)
    return subprocess.call([sys.executable, str(runtime / "scripts" / "office_runtime.py"), *argv], env=env)
