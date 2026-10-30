#!/usr/bin/env python3
"""Completion-event delivery, replay and monitor health for Auto Office v3 (T3).

Discovery, not assumption (issue 93 + live `herdr --help` introspection on this
machine, `adapters/seed/*.yaml`, `scripts/office_spawn.sh`):

- All four required orchestrator harnesses (agy, claude, codex, hermes) are
  `dispatch_forms: [cli]` and are spawned the same way, by
  `scripts/office_spawn.sh`, as a plain background OS process: it always
  writes `dispatches/<id>/pid`, `meta.json` and, on exit, `exit_code`,
  regardless of adapter. That PID/exit-code pair is a native, reliable,
  harness-independent completion signal for every dispatch spawned this way
  and needs no corroboration window: it is the OS's own report of the exact
  process the dispatch identity names, not a reported status about it. This
  module treats it as `source="process_exit"` and trusts it directly.
- A dispatch may *additionally* be hosted in a Herdr pane (`office-spawn.sh
  --pane-id`), so a human or another agent can watch it live. For that
  subset, Herdr's CLI (`herdr --help`, `herdr agent`, `herdr notification`)
  exposes no event/watch/subscribe primitive — only polling
  (`agent get`/`agent list`/`agent read`) and a blocking wait
  (`agent wait`/`agent prompt --wait`) that settles on `idle`, `done`, or
  `blocked`. `adapters/seed/agy.yaml`'s own `status-disagrees-with-pane`
  failure signature, and the live observation recorded at
  `/tmp/aoffice-0b3e/evidence/agy-false-done-observation.md` (agy's `--wait`
  returned exit 0 — a settled state — while the pane still showed an active
  spinner and no deliverables existed), confirm a single Herdr status sample
  is not trustworthy completion evidence. `herdr notification show` is a
  local desktop toast, unrelated to programmatic completion detection. So for
  Herdr-hosted panes there is no native completion-delivery mechanism to
  reuse; `HerdrBridge` below is the added background bridge, and it emits a
  terminal event only after corroborating multiple consecutive samples
  (`source="monitor_bridge"`).
- A raw, single, uncorroborated Herdr status read is still recorded (for
  replay visibility) but always as `terminal_classification="non_terminal"`
  and `source="herdr"` — `TRUSTED_SOURCES` excludes it deliberately, so nothing
  downstream (see `scripts/hooks/close_finished_panes.mjs`) can mistake a
  reported status for verified completion.

Functions `record_completion_event`, `get_event_cursor` and
`acknowledge_events` implement the exact signatures pinned in
`docs/v3-runtime-contracts.md` §4.3. Everything else in this module (the
Herdr bridge, monitor health, the CLI) is T3's own design within that
contract; no pinned schema or CLI signature is altered here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from jsonschema import Draft202012Validator

    HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover - jsonschema is a declared dependency
    HAS_JSONSCHEMA = False

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"

OBSERVED_STATUSES = {"finish", "idle", "blocked", "unknown", "disappeared", "running"}
TERMINAL_CLASSIFICATIONS = {"success", "failure", "cancelled", "timeout", "non_terminal"}
MONITOR_HEALTH_STATUSES = {"healthy", "degraded", "failing", "stopped"}

# Trust tiers for completion-event `source`. `completion-event.schema.json`
# leaves `source` a free-form string (no schema enum); the trust distinction
# below is this module's own policy layer on top of that free-form field.
UNCORROBORATED_SOURCE = "herdr"
CORROBORATED_SOURCE = "monitor_bridge"
PROCESS_EXIT_SOURCE = "process_exit"
TRUSTED_SOURCES = {CORROBORATED_SOURCE, PROCESS_EXIT_SOURCE}

HERDR_BIN = os.environ.get("OFFICE_HERDR_BIN", "herdr")


class MonitorArgumentError(ValueError):
    """Caller-supplied identity/value does not match its required derivation."""


class MonitorSchemaError(ValueError):
    """Constructed record fails its pinned JSON Schema."""


class MonitorSequenceError(ValueError):
    """Out-of-order acknowledgement (a sequence gap)."""


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_obj(obj: Any) -> str:
    return "sha256:" + _sha256_hex(_canonical_bytes(obj))


def derive_event_id(dispatch_id: str, sequence: int) -> str:
    """`"evt-" + sha256(f"{dispatch_id}:{sequence}")[:16]` — pinned in §4.3."""
    return "evt-" + _sha256_hex(f"{dispatch_id}:{sequence}".encode("utf-8"))[:16]


def derive_cursor_id(session_id: str, family_id: str, dispatch_id: str) -> str:
    """`"cur-" + sha256(f"{session_id}:{family_id}:{dispatch_id}")[:12]` — pinned in §5.5."""
    return "cur-" + _sha256_hex(f"{session_id}:{family_id}:{dispatch_id}".encode("utf-8"))[:12]


def derive_ack_hash(dispatch_id: str, sequence: int, event_id: str) -> str:
    """`"sha256:" + sha256(f"{dispatch_id}:{sequence}:{event_id}")` — pinned in §5.5."""
    return "sha256:" + _sha256_hex(f"{dispatch_id}:{sequence}:{event_id}".encode("utf-8"))


def _events_path(state_dir: Path) -> Path:
    return Path(state_dir) / "events" / "completions.jsonl"


def _cursor_path(state_dir: Path) -> Path:
    return Path(state_dir) / "events" / "cursor.json"


def _health_path(state_dir: Path) -> Path:
    return Path(state_dir) / "events" / "monitor_health.jsonl"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _validate(data: dict, schema_name: str) -> list[str]:
    if not HAS_JSONSCHEMA:  # pragma: no cover
        return []
    schema = json.loads((SCHEMAS / schema_name).read_text())
    validator = Draft202012Validator(schema)
    errors = []
    for e in sorted(validator.iter_errors(data), key=lambda x: list(x.path)):
        loc = ".".join(str(x) for x in e.path) or "$"
        errors.append(f"{loc}: {e.message}")
    return errors


# --------------------------------------------------------------------------
# §4.3 pinned functions
# --------------------------------------------------------------------------

def record_completion_event(
    state_dir: Path,
    event_id: str,
    session_id: str,
    family_id: str,
    dispatch_id: str,
    sequence: int,
    observed_status: str,
    terminal_classification: str,
    source: str,
    evidence_timestamp: str,
    evidence_hash: str,
    evidence_payload: dict | None = None,
) -> dict:
    """Appends a sequence-numbered event to events/completions.jsonl with deduplication."""
    state_dir = Path(state_dir)
    expected_id = derive_event_id(dispatch_id, sequence)
    if event_id != expected_id:
        raise MonitorArgumentError(
            f"event_id {event_id!r} does not match required derivation {expected_id!r} "
            f"for dispatch_id={dispatch_id!r} sequence={sequence}"
        )

    events_path = _events_path(state_dir)
    for row in _read_jsonl(events_path):
        if row.get("dispatch_id") == dispatch_id and row.get("sequence") == sequence:
            return row  # same (dispatch_id, sequence): no-op, return the existing row

    event = {
        "event_id": event_id,
        "session_id": session_id,
        "family_id": family_id,
        "dispatch_id": dispatch_id,
        "sequence": sequence,
        "observed_status": observed_status,
        "terminal_classification": terminal_classification,
        "source": source,
        "evidence_timestamp": evidence_timestamp,
        "evidence_hash": evidence_hash,
    }
    if evidence_payload is not None:
        event["evidence_payload"] = evidence_payload

    errors = _validate(event, "completion-event.schema.json")
    if errors:
        raise MonitorSchemaError("; ".join(errors))

    _append_jsonl(events_path, event)
    return event


def get_event_cursor(state_dir: Path, session_id: str, family_id: str, dispatch_id: str) -> int:
    """Returns last acknowledged sequence number for (session_id, family_id, dispatch_id)."""
    cursor_path = _cursor_path(Path(state_dir))
    if not cursor_path.exists():
        return 0
    try:
        data = json.loads(cursor_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    row = data.get(derive_cursor_id(session_id, family_id, dispatch_id))
    if not row:
        return 0
    return int(row.get("last_acknowledged_sequence", 0))


def acknowledge_events(
    state_dir: Path,
    session_id: str,
    family_id: str,
    dispatch_id: str,
    sequence: int,
    event_id: str,
) -> dict:
    """Advances the replay cursor for (session_id, family_id, dispatch_id)."""
    state_dir = Path(state_dir)
    stored = next(
        (
            e
            for e in _read_jsonl(_events_path(state_dir))
            if e.get("dispatch_id") == dispatch_id and e.get("sequence") == sequence
        ),
        None,
    )
    if stored is None or stored.get("event_id") != event_id:
        raise MonitorArgumentError(
            f"event_id {event_id!r} does not match the stored event at "
            f"dispatch_id={dispatch_id!r} sequence={sequence}"
        )

    cursor_path = _cursor_path(state_dir)
    all_cursors: dict = {}
    if cursor_path.exists():
        try:
            all_cursors = json.loads(cursor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            all_cursors = {}

    cursor_id = derive_cursor_id(session_id, family_id, dispatch_id)
    current = all_cursors.get(cursor_id)
    current_seq = int(current.get("last_acknowledged_sequence", 0)) if current else 0

    if sequence <= current_seq:
        # Idempotent duplicate ack: current is guaranteed non-None here
        # because current_seq defaults to 0 and sequence >= 1 always.
        return {
            "status": "already_acknowledged",
            "cursor_id": current["cursor_id"],
            "last_acknowledged_sequence": current["last_acknowledged_sequence"],
            "last_acknowledged_event_id": current["last_acknowledged_event_id"],
            "acknowledgement_hash": current["acknowledgement_hash"],
        }

    expected_next = current_seq + 1
    if sequence > expected_next:
        raise MonitorSequenceError(
            f"sequence_gap: expected {expected_next}, got {sequence}"
        )

    record = {
        "cursor_id": cursor_id,
        "session_id": session_id,
        "family_id": family_id,
        "dispatch_id": dispatch_id,
        "last_acknowledged_sequence": sequence,
        "last_acknowledged_event_id": event_id,
        "acknowledgement_hash": derive_ack_hash(dispatch_id, sequence, event_id),
        "acknowledged_at": _now_iso(),
    }
    errors = _validate(record, "replay-cursor.schema.json")
    if errors:
        raise MonitorSchemaError("; ".join(errors))

    all_cursors[cursor_id] = record
    _atomic_write_text(cursor_path, json.dumps(all_cursors, indent=2, sort_keys=True))
    return {
        "status": "acknowledged",
        "cursor_id": record["cursor_id"],
        "last_acknowledged_sequence": record["last_acknowledged_sequence"],
        "last_acknowledged_event_id": record["last_acknowledged_event_id"],
        "acknowledgement_hash": record["acknowledgement_hash"],
    }


# --------------------------------------------------------------------------
# Event/replay conveniences built on top of the pinned functions
# --------------------------------------------------------------------------

def list_events(state_dir: Path, dispatch_id: str | None = None, since_seq: int | None = None) -> list[dict]:
    events = _read_jsonl(_events_path(Path(state_dir)))
    if dispatch_id is not None:
        events = [e for e in events if e.get("dispatch_id") == dispatch_id]
    if since_seq is not None:
        events = [e for e in events if e.get("sequence", 0) > since_seq]
    events.sort(key=lambda e: (e.get("dispatch_id", ""), e.get("sequence", 0)))
    return events


def next_sequence(state_dir: Path, dispatch_id: str) -> int:
    events = list_events(state_dir, dispatch_id=dispatch_id)
    if not events:
        return 1
    return max(e.get("sequence", 0) for e in events) + 1


def latest_trusted_terminal_event(state_dir: Path, dispatch_id: str) -> dict | None:
    """Latest event for dispatch_id that is both terminal and independently corroborated.

    A terminal-looking event whose `source` is the raw, single-sample `herdr`
    status read is not trustworthy by itself (the agy false-done case) — only
    `monitor_bridge` (corroborated over several samples) or `process_exit`
    (the OS's own report) count as verified completion here.
    """
    candidates = [
        e
        for e in list_events(state_dir, dispatch_id=dispatch_id)
        if e.get("terminal_classification") != "non_terminal" and e.get("source") in TRUSTED_SOURCES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.get("sequence", 0))


def completion_status(state_dir: Path, dispatch_id: str) -> dict:
    events = list_events(state_dir, dispatch_id=dispatch_id)
    if not events:
        return {"dispatch_id": dispatch_id, "found": False}
    latest = max(events, key=lambda e: e.get("sequence", 0))
    terminal = latest.get("terminal_classification") != "non_terminal" and latest.get("source") in TRUSTED_SOURCES
    return {
        "dispatch_id": dispatch_id,
        "found": True,
        "observed_status": latest.get("observed_status"),
        "terminal": terminal,
        "classification": latest.get("terminal_classification"),
        "source": latest.get("source"),
    }


# --------------------------------------------------------------------------
# Monitor health (§2.9) — heartbeat, so monitor loss is visible, not silent
# --------------------------------------------------------------------------

def next_health_sequence(state_dir: Path, monitor_id: str) -> int:
    rows = [r for r in _read_jsonl(_health_path(Path(state_dir))) if r.get("monitor_id") == monitor_id]
    if not rows:
        return 1
    return max(r.get("sequence", 0) for r in rows) + 1


def record_monitor_health(
    state_dir: Path,
    monitor_id: str,
    session_id: str,
    sequence: int,
    status: str,
    active_panes: list[str],
    event_lag_ms: int,
    health_evidence: dict,
    timestamp: str | None = None,
) -> dict:
    record = {
        "monitor_id": monitor_id,
        "session_id": session_id,
        "sequence": sequence,
        "status": status,
        "active_panes": list(active_panes),
        "event_lag_ms": event_lag_ms,
        "health_evidence": health_evidence,
        "timestamp": timestamp or _now_iso(),
    }
    errors = _validate(record, "monitor-health.schema.json")
    if errors:
        raise MonitorSchemaError("; ".join(errors))
    _append_jsonl(_health_path(Path(state_dir)), record)
    return record


def latest_monitor_health(state_dir: Path, monitor_id: str | None = None) -> dict | None:
    rows = _read_jsonl(_health_path(Path(state_dir)))
    if monitor_id is not None:
        rows = [r for r in rows if r.get("monitor_id") == monitor_id]
    if not rows:
        return None
    return max(rows, key=lambda r: r.get("sequence", 0))


def monitor_is_stale(state_dir: Path, monitor_id: str, max_age_seconds: float, now: float | None = None) -> bool:
    """True when the monitor's heartbeat is missing, stopped, or older than max_age_seconds.

    Monitor loss must be visible, not silent: a consumer calls this instead of
    assuming a monitor is running just because nothing said otherwise.
    """
    row = latest_monitor_health(state_dir, monitor_id=monitor_id)
    if row is None or row.get("status") == "stopped":
        return True
    try:
        observed = datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError, KeyError):
        return True
    now = time.time() if now is None else now
    return (now - observed) > max_age_seconds


# --------------------------------------------------------------------------
# process_exit source: native signal for any office-spawn.sh dispatch
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else: still alive
    return True


def process_exit_observation(state_dir: Path, dispatch_id: str) -> dict | None:
    """Reads dispatches/<id>/{pid,exit_code} written by office-spawn.sh.

    Returns None if the dispatch has no recorded pid at all. Otherwise
    returns observed_status/terminal_classification: "running"/non_terminal
    while alive, "unknown"/non_terminal if the process is gone but no
    exit_code was recorded yet (never assume success from silence), and
    "finish" with classification derived from the real exit code once both
    agree the process has ended.
    """
    dispatch_dir = Path(state_dir) / "dispatches" / dispatch_id
    pidfile = dispatch_dir / "pid"
    exit_file = dispatch_dir / "exit_code"
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        return None

    if _pid_alive(pid):
        return {"observed_status": "running", "terminal_classification": "non_terminal", "exit_code": None}

    if not exit_file.exists():
        return {"observed_status": "unknown", "terminal_classification": "non_terminal", "exit_code": None}
    try:
        code = int(exit_file.read_text().strip())
    except ValueError:
        return {"observed_status": "unknown", "terminal_classification": "non_terminal", "exit_code": None}

    return {
        "observed_status": "finish",
        "terminal_classification": "success" if code == 0 else "failure",
        "exit_code": code,
    }


def emit_process_exit_event(state_dir: Path, session_id: str, family_id: str, dispatch_id: str) -> dict | None:
    """Records a process_exit completion event once, idempotently, on real process exit."""
    state_dir = Path(state_dir)
    existing = latest_trusted_terminal_event(state_dir, dispatch_id)
    if existing is not None:
        return existing  # restart/reconnect: do not re-fire a stale terminal fact

    obs = process_exit_observation(state_dir, dispatch_id)
    if obs is None or obs["terminal_classification"] == "non_terminal":
        return None

    seq = next_sequence(state_dir, dispatch_id)
    payload = {"exit_code": obs["exit_code"]}
    return record_completion_event(
        state_dir=state_dir,
        event_id=derive_event_id(dispatch_id, seq),
        session_id=session_id,
        family_id=family_id,
        dispatch_id=dispatch_id,
        sequence=seq,
        observed_status=obs["observed_status"],
        terminal_classification=obs["terminal_classification"],
        source=PROCESS_EXIT_SOURCE,
        evidence_timestamp=_now_iso(),
        evidence_hash=_sha256_obj(payload),
        evidence_payload=payload,
    )


# --------------------------------------------------------------------------
# monitor_bridge source: corroborated Herdr-pane observation
# --------------------------------------------------------------------------

def _run_herdr_json(args: list[str], timeout: float = 8.0) -> dict | None:
    """For herdr subcommands that answer JSON (`agent get/list`, `pane list/close`, ...).

    Live introspection (`herdr agent get <target>`, run against this session's
    own panes) confirmed the shape: `{"result": {...}}` on success,
    `{"error": {"code": ..., "message": ...}}` on stdout with exit 1 on
    failure — never only on stderr for these subcommands, but both streams
    are checked defensively.
    """
    try:
        proc = subprocess.run([HERDR_BIN, *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    for stream in (proc.stdout, proc.stderr):
        if not stream:
            continue
        try:
            return json.loads(stream)
        except json.JSONDecodeError:
            continue
    return None


def _run_herdr_text(args: list[str], timeout: float = 8.0) -> str | None:
    """For `herdr agent read`/`herdr pane read`, which answer plain terminal text, not JSON.

    Live introspection (`herdr agent read <target> --source recent-unwrapped
    --lines N`) confirmed this: there is no `--format json`, only
    `text`/`ansi`. Treating this output as JSON (as an earlier draft of this
    module did) silently discarded every sample, since `json.loads` on
    terminal transcript text always fails.
    """
    try:
        proc = subprocess.run([HERDR_BIN, *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def herdr_agent_snapshot(target: str) -> dict | None:
    """One raw sample: `herdr agent get <target>` plus a content fingerprint.

    Never sufficient completion evidence on its own — only ever fed into
    `HerdrBridge.observe()`.
    """
    resp = _run_herdr_json(["agent", "get", target])
    if resp is None or resp.get("error"):
        return None
    # `agent get`'s payload nests the agent object one level under
    # `result.agent` (confirmed live), not directly under `result`.
    result = resp.get("result", {})
    agent = result.get("agent", result)
    status = agent.get("agent_status") or agent.get("status")

    content = _run_herdr_text(["agent", "read", target, "--source", "recent-unwrapped", "--lines", "200"]) or ""

    return {"status": status, "content": content, "content_hash": _sha256_hex(content.encode("utf-8"))}


def _classify_raw_status(raw_status: str | None) -> str:
    if raw_status is None:
        return "unknown"
    s = str(raw_status).lower()
    if s in ("done", "finish", "finished", "completed", "exited"):
        return "finish"
    if s == "blocked":
        return "blocked"
    if s == "idle":
        return "idle"
    if s in ("working", "running", "busy"):
        return "running"
    if s in ("disappeared", "gone"):
        return "disappeared"
    return "unknown"


class HerdrBridge:
    """Corroborates raw Herdr status samples into a trustworthy completion event.

    A single `done`/`idle` sample is never trusted: agy has reported `done`
    (or returned a settled `--wait` exit code) while its pane was still
    visibly working (issue 93; `agy-false-done-observation.md`). Corroboration
    requires `stable_samples` consecutive samples that agree on both status
    AND unchanged pane content (no advancing output — the spinner in the
    known-bad case keeps the content hash moving) before a terminal event may
    be emitted. `idle` and `blocked` are never corroborated into terminal:
    an idle agent may have dropped its prompt, and a blocked one is waiting on
    input, not finished — both stay open until independently confirmed.
    """

    def __init__(self, dispatch_id: str, stable_samples: int = 3, completion_marker: str | None = None):
        if stable_samples < 1:
            raise ValueError("stable_samples must be >= 1")
        self.dispatch_id = dispatch_id
        self.stable_samples = stable_samples
        self.completion_marker = completion_marker
        self._history: list[dict] = []

    def observe(self, sample: dict | None) -> dict:
        """Feed one `herdr_agent_snapshot`-shaped sample, or None for "agent not found"."""
        if sample is None:
            self._history.append({"status": "disappeared", "content_hash": None, "content": ""})
        else:
            self._history.append(
                {"status": sample.get("status"), "content_hash": sample.get("content_hash"), "content": sample.get("content") or ""}
            )

        latest = self._history[-1]
        observed_status = "disappeared" if sample is None else _classify_raw_status(latest["status"])
        window = self._history[-self.stable_samples:]

        stable = (
            len(window) == self.stable_samples
            and len({w["status"] for w in window}) == 1
            and len({w["content_hash"] for w in window}) == 1
        )
        marker_ok = self.completion_marker is None or (self.completion_marker in latest["content"])
        corroborated = stable and marker_ok and observed_status in ("finish", "disappeared")

        terminal_classification = "non_terminal"
        if corroborated:
            # A spontaneous disappearance (not an orchestrator-initiated
            # replacement — see §3.1, which is the orchestrator's own call,
            # not this bridge's) is classified failure, not success: we
            # cannot assume the work completed just because the pane is gone.
            terminal_classification = "success" if observed_status == "finish" else "failure"

        return {
            "observed_status": observed_status,
            "terminal_classification": terminal_classification,
            "corroborated": corroborated,
            "samples": len(window),
        }


def emit_herdr_bridge_event(
    state_dir: Path,
    session_id: str,
    family_id: str,
    dispatch_id: str,
    bridge: HerdrBridge,
    sample: dict | None,
) -> dict | None:
    state_dir = Path(state_dir)
    existing = latest_trusted_terminal_event(state_dir, dispatch_id)
    if existing is not None:
        return existing  # restart/reconnect: do not re-fire a stale terminal fact

    result = bridge.observe(sample)
    if not result["corroborated"]:
        return None

    seq = next_sequence(state_dir, dispatch_id)
    payload = {
        "samples": result["samples"],
        "stable_samples_required": bridge.stable_samples,
        "raw_status": (sample or {}).get("status"),
    }
    return record_completion_event(
        state_dir=state_dir,
        event_id=derive_event_id(dispatch_id, seq),
        session_id=session_id,
        family_id=family_id,
        dispatch_id=dispatch_id,
        sequence=seq,
        observed_status=result["observed_status"],
        terminal_classification=result["terminal_classification"],
        source=CORROBORATED_SOURCE,
        evidence_timestamp=_now_iso(),
        evidence_hash=_sha256_obj(payload),
        evidence_payload=payload,
    )


def emit_raw_herdr_observation(
    state_dir: Path, session_id: str, family_id: str, dispatch_id: str, sample: dict | None
) -> dict:
    """Records one raw, always-non-terminal, source="herdr" observation for replay visibility.

    This is the "reported" half of "distinguish reported done from verified
    completion": it is written on every sample so replay shows exactly what
    the raw signal said and when, but it can never itself satisfy
    `latest_trusted_terminal_event` — see `TRUSTED_SOURCES`.
    """
    state_dir = Path(state_dir)
    status = "disappeared" if sample is None else _classify_raw_status(sample.get("status"))
    seq = next_sequence(state_dir, dispatch_id)
    payload = {"raw_status": (sample or {}).get("status") if sample else None}
    return record_completion_event(
        state_dir=state_dir,
        event_id=derive_event_id(dispatch_id, seq),
        session_id=session_id,
        family_id=family_id,
        dispatch_id=dispatch_id,
        sequence=seq,
        observed_status=status,
        terminal_classification="non_terminal",
        source=UNCORROBORATED_SOURCE,
        evidence_timestamp=_now_iso(),
        evidence_hash=_sha256_obj(payload),
        evidence_payload=payload,
    )


# --------------------------------------------------------------------------
# Start-receipt read (written by whichever task wires office-spawn.sh; T3 only reads it)
# --------------------------------------------------------------------------

def read_start_receipt(state_dir: Path, dispatch_id: str) -> dict | None:
    path = Path(state_dir) / "dispatches" / dispatch_id / "start_receipt.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print(obj: dict) -> None:
    print(json.dumps(obj, sort_keys=True))


def cmd_record_event(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    if args.file:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        data = {
            "event_id": args.event_id,
            "session_id": args.session_id,
            "family_id": args.family_id,
            "dispatch_id": args.dispatch_id,
            "sequence": args.sequence,
            "observed_status": args.observed_status,
            "terminal_classification": args.terminal_classification,
            "source": args.source,
            "evidence_timestamp": args.evidence_timestamp,
            "evidence_hash": args.evidence_hash,
        }
        if args.evidence_payload:
            data["evidence_payload"] = json.loads(args.evidence_payload)

    required = (
        "event_id", "session_id", "family_id", "dispatch_id", "sequence",
        "observed_status", "terminal_classification", "source",
        "evidence_timestamp", "evidence_hash",
    )
    missing = [k for k in required if data.get(k) in (None, "")]
    if missing:
        _print({"error": "missing required fields", "fields": missing})
        return 1

    try:
        result = record_completion_event(
            state_dir=state_dir,
            evidence_payload=data.get("evidence_payload"),
            **{k: data[k] for k in required},
        )
    except MonitorArgumentError as e:
        _print({"error": str(e)})
        return 1
    except MonitorSchemaError as e:
        _print({"error": str(e)})
        return 2

    _print({"status": "recorded", "event_id": result["event_id"], "sequence": result["sequence"]})
    return 0


def cmd_list_events(args: argparse.Namespace) -> int:
    events = list_events(Path(args.state_dir), dispatch_id=args.dispatch_id, since_seq=args.since_seq)
    _print({"events": events})
    return 0


def cmd_ack_event(args: argparse.Namespace) -> int:
    try:
        result = acknowledge_events(
            state_dir=Path(args.state_dir),
            session_id=args.session_id,
            family_id=args.family_id,
            dispatch_id=args.dispatch_id,
            sequence=args.sequence,
            event_id=args.event_id,
        )
    except MonitorArgumentError as e:
        _print({"error": str(e)})
        return 1
    except MonitorSchemaError as e:
        _print({"error": str(e)})
        return 2
    except MonitorSequenceError as e:
        _print({"status": "sequence_gap", "error": str(e)})
        return 3
    _print(result)
    return 0


def cmd_completion_status(args: argparse.Namespace) -> int:
    result = completion_status(Path(args.state_dir), args.dispatch_id)
    _print(result)
    return 0 if result.get("found") else 1


def cmd_process_exit_event(args: argparse.Namespace) -> int:
    try:
        event = emit_process_exit_event(Path(args.state_dir), args.session_id, args.family_id, args.dispatch_id)
    except (MonitorArgumentError, MonitorSchemaError) as e:
        _print({"error": str(e)})
        return 2
    if event is None:
        _print({"status": "non_terminal"})
        return 0
    _print({
        "status": "recorded",
        "event_id": event["event_id"],
        "sequence": event["sequence"],
        "observed_status": event["observed_status"],
        "terminal_classification": event["terminal_classification"],
    })
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    seq = args.sequence or next_health_sequence(state_dir, args.monitor_id)
    checks_passed = args.status == "healthy"
    record = record_monitor_health(
        state_dir=state_dir,
        monitor_id=args.monitor_id,
        session_id=args.session_id,
        sequence=seq,
        status=args.status,
        active_panes=args.active_panes or [],
        event_lag_ms=args.event_lag_ms,
        health_evidence={
            "checks_passed": checks_passed,
            "checks": ["cli_heartbeat"],
            "last_ping": _now_iso(),
            "evidence_hash": _sha256_obj({"monitor_id": args.monitor_id, "sequence": seq}),
        },
    )
    _print({"status": "recorded", "sequence": record["sequence"]})
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Poll-and-corroborate bridge for a single Herdr-hosted dispatch.

    `--once` makes one observation and returns (used by tests and by callers
    that drive their own poll loop); without it, this runs until a trusted
    terminal event is recorded. Live use against a real herdr server is T4's
    wiring / T6's verification — this command's own correctness is proven
    here only against the deterministic fake in tests/test_monitor.py.
    """
    state_dir = Path(args.state_dir)
    bridge = HerdrBridge(args.dispatch_id, stable_samples=args.stable_samples, completion_marker=args.completion_marker)
    monitor_id = args.monitor_id or f"mon-{args.dispatch_id}"
    iteration = 0

    while True:
        iteration += 1
        sample = herdr_agent_snapshot(args.target) if args.target else None
        if args.record_raw:
            emit_raw_herdr_observation(state_dir, args.session_id, args.family_id, args.dispatch_id, sample)
        event = emit_herdr_bridge_event(state_dir, args.session_id, args.family_id, args.dispatch_id, bridge, sample)

        sampled_ok = sample is not None or args.target is None
        record_monitor_health(
            state_dir=state_dir,
            monitor_id=monitor_id,
            session_id=args.session_id,
            sequence=next_health_sequence(state_dir, monitor_id),
            status="stopped" if event is not None else ("healthy" if sampled_ok else "degraded"),
            active_panes=[] if event is not None or not args.target else [args.target],
            event_lag_ms=0,
            health_evidence={
                "checks_passed": sampled_ok,
                "checks": ["herdr_sample"],
                "last_ping": _now_iso(),
                "evidence_hash": _sha256_obj({"iteration": iteration}),
            },
        )

        if event is not None:
            _print({"status": "terminal", "event_id": event["event_id"], "sequence": event["sequence"]})
            return 0
        if args.once:
            _print({"status": "pending", "samples": iteration})
            return 0
        time.sleep(args.poll_interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="office_monitor.py")
    sub = p.add_subparsers(dest="command", required=True)

    re_p = sub.add_parser("record-event")
    re_p.add_argument("--file")
    re_p.add_argument("--event-id")
    re_p.add_argument("--session-id")
    re_p.add_argument("--family-id")
    re_p.add_argument("--dispatch-id")
    re_p.add_argument("--sequence", type=int)
    re_p.add_argument("--observed-status")
    re_p.add_argument("--terminal-classification")
    re_p.add_argument("--source")
    re_p.add_argument("--evidence-timestamp")
    re_p.add_argument("--evidence-hash")
    re_p.add_argument("--evidence-payload")
    re_p.add_argument("--state-dir", default=".office")
    re_p.set_defaults(func=cmd_record_event)

    le_p = sub.add_parser("list-events")
    le_p.add_argument("--dispatch-id")
    le_p.add_argument("--since-seq", type=int)
    le_p.add_argument("--state-dir", default=".office")
    le_p.set_defaults(func=cmd_list_events)

    ack_p = sub.add_parser("ack-event")
    ack_p.add_argument("--session-id", required=True)
    ack_p.add_argument("--family-id", required=True)
    ack_p.add_argument("--dispatch-id", required=True)
    ack_p.add_argument("--sequence", type=int, required=True)
    ack_p.add_argument("--event-id", required=True)
    ack_p.add_argument("--state-dir", default=".office")
    ack_p.set_defaults(func=cmd_ack_event)

    cs_p = sub.add_parser("completion-status")
    cs_p.add_argument("--dispatch-id", required=True)
    cs_p.add_argument("--state-dir", default=".office")
    cs_p.set_defaults(func=cmd_completion_status)

    pe_p = sub.add_parser("process-exit-event")
    pe_p.add_argument("--session-id", required=True)
    pe_p.add_argument("--family-id", required=True)
    pe_p.add_argument("--dispatch-id", required=True)
    pe_p.add_argument("--state-dir", default=".office")
    pe_p.set_defaults(func=cmd_process_exit_event)

    h_p = sub.add_parser("health")
    h_p.add_argument("--monitor-id", required=True)
    h_p.add_argument("--session-id", required=True)
    h_p.add_argument("--status", choices=sorted(MONITOR_HEALTH_STATUSES), required=True)
    h_p.add_argument("--active-panes", nargs="*", default=[])
    h_p.add_argument("--event-lag-ms", type=int, default=0)
    h_p.add_argument("--sequence", type=int)
    h_p.add_argument("--state-dir", default=".office")
    h_p.set_defaults(func=cmd_health)

    w_p = sub.add_parser("watch")
    w_p.add_argument("--session-id", required=True)
    w_p.add_argument("--family-id", required=True)
    w_p.add_argument("--dispatch-id", required=True)
    w_p.add_argument("--target", help="Herdr pane id or agent name to observe")
    w_p.add_argument("--monitor-id")
    w_p.add_argument("--stable-samples", type=int, default=3)
    w_p.add_argument("--completion-marker")
    w_p.add_argument("--poll-interval", type=float, default=5.0)
    w_p.add_argument("--once", action="store_true")
    w_p.add_argument("--record-raw", action="store_true")
    w_p.add_argument("--state-dir", default=".office")
    w_p.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
