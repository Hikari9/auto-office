"""Non-blocking `bug?:` hints for outcomes that look like an Office defect.

A hint is a nudge to the orchestrator, who decides whether it is a defect and files it by hand
(`office self-improve`). This module only reads: it starts no process, makes no network or model call and
writes nothing to runs.db. A hint never changes an outcome or an exit code.

Signals are decided by explicit kinds and categories (a refusal category, a gate verdict, an outbox status, a
dispatch classification, an exception class). Free text never decides one, and only reaches a hint through
`sanitize` (the protocol/privacy-self-improvement.md rules: no paths, emails, URLs, secrets or long identifiers).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

SUFFIX = "(if this is an Office defect: office self-improve)"
MAX_SIGNAL = 160
ERROR_HEAD = 60

# Refusals that are the ordinary shape of a gate or a user mistake, never a defect.
ROUTINE_REFUSALS = frozenset({
    "not-ready", "user-quote-required", "quote-required", "authorization-required", "usage", "quota",
    "orchestrator-quota-exhausted", "invalid-override", "no-task", "no-run", "no-active-run", "no-such-run",
    "ambiguous-run", "close-blocked", "unknown-task",
})
# Flags that only choose the output format, so they never make a refused command differ from the one `next:` names.
OUTPUT_FLAGS = frozenset({"--json"})
# A dispatch that ended in one of these classifications was lost to the runtime, not finished or stopped.
LOST_DISPATCH = ("lost", "crashed")
# A later event of one of these kinds on the same task (or run, for a task-less signal) means the signal was recovered.
RECOVERY_EVENTS = ("task.unblocked", "task.rerun", "task.relaunch", "task.accepted", "gate.rerun",
                   "integration.retry", "integration.recheck", "integration.accepted")

_REDACTIONS = (
    (re.compile(r"(?i)(?:gh[pousr]_|github_pat_|sk-[a-z0-9_-]{10,}|Bearer\s+)[A-Za-z0-9_./+=-]+"), "[redacted-secret]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    (re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I), "[redacted-url]"),
    (re.compile(r"(?i)\b(?:token|password|secret|apikey|api_key|authorization)\s*[:=]\s*\S+"), "[redacted-field]"),
    (re.compile(r"(?<![\w.-])(?:~|\.{1,2}|[A-Za-z]:)?[/\\][\w.@+-]+(?:[/\\][\w.@+-]+)*"), "[redacted-path]"),
    (re.compile(r"\b[\w.-]+/[\w.-]+\b"), "[redacted-path]"),
    (re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I), "[redacted-identifier]"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "[redacted-identifier]"),
)


def sanitize(value) -> str:
    """One short line with paths, emails, URLs, secrets and long hex or UUID identifiers replaced."""
    text = str(value).replace("\x00", " ")
    for pattern, substitute in _REDACTIONS:
        text = pattern.sub(substitute, text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_SIGNAL else text[:MAX_SIGNAL - 1].rstrip() + "…"


def line(signal: str) -> str:
    """The exact hint line for one signal."""
    return f"bug?: {sanitize(signal)} {SUFFIX}"


@dataclass(frozen=True)
class Signal:
    kind: str  # refusal | gate | job | dispatch | internal
    text: str
    at: str = ""  # ISO time the outcome was recorded; "" for one that is happening now
    task_id: str | None = None


# ------------------------------------------------------------------ one-shot outcomes

_NAMED = re.compile(r"\boffice\s+([a-z][a-z-]*)((?:\s+(?:[A-Z][A-Za-z0-9]*\d[A-Za-z0-9]*|<[a-z-]+>|--[a-z][a-z-]*))*)")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*")


def named_commands(next_line: str | None) -> list[tuple[str, set[str], set[str]]]:
    """Each `office <command> [<id> ...] [--flag ...]` the `next:` line names, as (command, ids, flags). A lowercase
    word after the command is prose or a subcommand, not an id, so the command alone matches."""
    found = []
    for m in _NAMED.finditer(next_line or ""):
        words = m.group(2).split()
        found.append((m.group(1), {w.upper() for w in words if w[0] not in "<-"}, {w for w in words if w[:2] == "--"}))
    return found


def refusal_signal(category: str, command: str | None, argv: list[str], next_line: str | None) -> Signal | None:
    """A refusal is a signal only when it refuses the command the run's `next:` line names, as that line spells it
    (exactly the same ids and `--` flags, no more), and the category is not a routine one. `argv` is the refused
    command line."""
    if not command or not category or category in ROUTINE_REFUSALS:
        return None
    ids = {w.upper() for w in argv if _ID.fullmatch(w)}
    flags = {w.split("=", 1)[0] for w in argv if w.startswith("--")} - OUTPUT_FLAGS
    for name, want_ids, want_flags in named_commands(next_line):
        if name == command and want_ids == ids and want_flags == flags:
            return Signal("refusal", f"office {command} {' '.join(sorted(want_ids))}".strip()
                          + f" was refused (blocked: {category}) though next: names it")
    return None


def internal_error_signal(exc: BaseException) -> Signal:
    return Signal("internal", f"internal error: uncaught {type(exc).__name__}")


# ------------------------------------------------------------------ recorded state (read-only)

def _job_task(payload_json: str | None) -> str | None:
    try:
        task = json.loads(payload_json or "{}").get("task_id")
    except (TypeError, ValueError, AttributeError):
        return None
    return task if isinstance(task, str) else None


def _unavailable_check_gates(con, run_id: str) -> list:
    return con.execute("SELECT id, subject, task_id, kind, created_at, finished_at FROM gates WHERE run_id=? "
                       "AND kind='checks' AND (verdict='UNAVAILABLE' OR review_status='UNAVAILABLE')",
                       (run_id,)).fetchall()


def _gate_job(gate, jobs: list) -> dict | None:
    """The failed job that owned this gate, by the ownership rule gates.owning_jobs uses: a task gate's job
    names it in its payload; an integration gate is created inside the integrate job that was running."""
    for job in jobs:
        if gate["subject"] == "integration":
            if job["kind"] == "integrate" and (job["claimed_at"] or "") <= gate["created_at"] <= (job["finished_at"] or "~"):
                return job
        elif f'"{gate["id"]}"' in (job["payload_json"] or ""):
            return job
    return None


def collect(con, run_id: str) -> list[Signal]:
    """The run's current signals, oldest first. Reads runs.db with SELECT only."""
    failed = [dict(r) for r in con.execute(
        "SELECT id, kind, attempts, error, payload_json, claimed_at, finished_at, created_at FROM outbox "
        "WHERE run_id=? AND status='failed' AND COALESCE(error,'')<>''", (run_id,))]
    found: list[Signal] = []
    gate_jobs = set()
    for gate in _unavailable_check_gates(con, run_id):
        job = _gate_job(gate, failed)
        if job is None:
            continue  # UNAVAILABLE for a missing command or host load: nothing failed inside Office
        gate_jobs.add(job["id"])
        scope = gate["task_id"] or "integration"
        found.append(Signal("gate", f"{scope} {gate['kind']} gate is UNAVAILABLE because its {job['kind']} job failed",
                            job["finished_at"] or job["created_at"], gate["task_id"]))
    for job in failed:
        if job["id"] not in gate_jobs:
            # Only the error's head (its exception class or fixed reason) is quoted: the message after it is free text.
            head = str(job["error"]).split(":", 1)[0][:ERROR_HEAD]
            found.append(Signal("job", f"{job['kind']} job failed after {job['attempts']} attempts: {head}",
                                job["finished_at"] or job["created_at"], _job_task(job["payload_json"])))
    for d in con.execute(
            "SELECT role, task_id, terminal_classification AS how, ended_at FROM dispatches WHERE run_id=? "
            f"AND ended_at IS NOT NULL AND COALESCE(status,'')<>'cancelled' "
            f"AND terminal_classification IN ({','.join('?' * len(LOST_DISPATCH))}) ORDER BY ended_at",
            (run_id, *LOST_DISPATCH)):
        found.append(Signal("dispatch", f"{d['task_id'] or d['role']} {d['role']} dispatch ended {d['how']}",
                            d["ended_at"], d["task_id"]))
    recovered = con.execute(f"SELECT task_id, created_at FROM events WHERE run_id=? AND kind IN "
                            f"({','.join('?' * len(RECOVERY_EVENTS))})", (run_id, *RECOVERY_EVENTS)).fetchall()
    found = [s for s in found if not any(r["task_id"] == s.task_id and r["created_at"] > s.at for r in recovered)]
    return sorted(found, key=lambda s: s.at)


def lines(signals: list[Signal], limit: int | None = None) -> list[str]:
    """Hint lines for `signals`, newest `limit` of them, deduplicated."""
    out = list(dict.fromkeys(line(s.text) for s in signals))
    return out if limit is None else out[-limit:]
