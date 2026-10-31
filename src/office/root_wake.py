"""Opt-in, run-scoped root continuation transport (issue #723).

This is not a second lifecycle engine. The Office event log is authority; rows here
are durable notification receipts. No agent is started or user authority minted.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
import re
import threading
from datetime import timedelta
from pathlib import Path

from office import db, discovery, paths, state
from office.util import now_iso, parse_iso, pid_alive, sha256_obj

# Event families with an actual lifecycle outcome or a possible next step.
# In particular launch.*, setup.*, logs, and wait.* must never wake a root.
ACTIONABLE = frozenset({
    "submit", "submit.refused", "submit.rejected", "task.blocked", "task.paused",
    "task.accepted", "task.findings_queued", "task.restack_needed", "task.scope_requested",
    "task.raised", "task.amend_undelivered", "dispatch.ended", "dispatch.reaped",
    "gate.pass", "gate.approved", "gate.attention", "gate.unavailable",
    "gate.recheck", "gate.changes_required", "gate.brief_defect", "gate.escalated",
    "plan.pass", "plan.ready", "plan.approved", "plan.unavailable", "plan.recheck",
    "plan.changes_required", "plan.questions", "plan.intake_gap", "plan.budget_exhausted",
    "convergence.verdict", "convergence.approved", "convergence.recheck",
    "convergence.unavailable", "convergence.intake_gap", "convergence.escalation",
    "integration.accepted", "integration.failed", "integration.conflict", "integration.stale",
    "pr.error", "pr.merged", "worker.signal", "watchdog.stall", "amendment.applied", "amendment.contract_changed",
    "requirements.changed", "finding.disposition", "authority.plan", "authority.land",
    "ctx.high",
    # review gates past a bound (#919)
    "review.queued_too_long", "review.stalled", "review.closed_unread",
    # the install was rebuilt under a running root wake and is healthy again (#920)
    "office.reinstalled",
    # a route the run fell back from is available again (#921)
    "route.recovered",
})
MIN_POLL = 0.5
MAX_ATTEMPTS = 4
WATCHDOG_POLL_SECONDS = 15.0  # separate from completion wake
# Herdr reports a Claude agent whose turn finished as `done` until its pane is
# focused, then `idle`. Both mean the turn ended and the composer is free; an
# unattended root that just finished its turn is normally `done`.
IDLE_STATES = frozenset({"idle", "done"})



_LOCAL = threading.local()  # per-thread reentrancy; other threads must block


def _held() -> dict[str, int]:
    if not hasattr(_LOCAL, "held"):
        _LOCAL.held = {}
    return _LOCAL.held


@contextlib.contextmanager
def _flock(key: str, path: Path):
    """Exclusive file lock, reentrant within one thread; other threads block."""
    held = _held()
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield
        finally:
            del held[key]
            fcntl.flock(fh, fcntl.LOCK_UN)


def _reinstall_mark(run_id: str) -> Path:
    return paths.run_dir(run_id) / "root-wake-reinstall.mark"


def _mark_reinstall(run_id: str) -> bool:
    """Open a recovery episode for this run. True only for the first mark.

    The token goes into a temp file that is hard-linked into place, so the mark never exists without its
    token. Any OSError means the episode cannot be recorded, or one is already open; the caller keeps running."""
    mark = _reinstall_mark(run_id)
    tmp = mark.with_name(f"{mark.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(uuid.uuid4().hex)
        os.link(tmp, mark)
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return True


def _tick_failed(run_id: str, exc: Exception) -> None:
    """A tick raised ImportError or OSError: the signature of an install replaced under this process (#833, #920).

    Only the first failure of an episode prints. A later healthy tick records the recovery."""
    if _mark_reinstall(run_id):
        with contextlib.suppress(OSError):  # a closed stderr must not end the supervisor
            print(f"root wake: tick failed ({type(exc).__name__}: {exc}); retrying", file=sys.stderr)


def _recorded(con, run_id: str, token: str) -> bool:
    """True when an `office.reinstalled` event already carries this mark token."""
    return con.execute("SELECT 1 FROM events WHERE run_id=? AND kind='office.reinstalled' "
                       "AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.mark') END = ?",
                       (run_id, token)).fetchone() is not None


def _recover_reinstall(con, run: dict) -> bool:
    """Record one `office.reinstalled` event after a healthy tick ends an episode opened by `_tick_failed` (#920).

    The event carries the mark's token, and the mark is removed only after the event commits. A crash or a
    failed unlink after commit leaves the token recorded, so the retry removes the mark without recording
    again. A run that has ended gets no wake. The run lock serializes the supervisor and the watchdog thread."""
    mark = _reinstall_mark(run["id"])
    if not mark.exists():
        return False
    with _flock(f"reinstall:{run['id']}", paths.run_dir(run["id"]) / "root-wake.reinstall.lock"):
        if not mark.exists():
            return False
        token = mark.read_text().strip()
        with db.transaction(con):  # the run check and the emit share one write transaction
            current = state.get_run(con, run["id"])
            if current and not state.is_terminal(current) and not _recorded(con, run["id"], token):
                state.emit(con, current, "office.reinstalled",
                           "Office install was unavailable and is healthy again; resume this run from its current state",
                           payload={"mark": token})
        mark.unlink()
    return True


def _pane_lock(pane: str):
    """Fences everything that can change which run a root pane belongs to
    (office start/resume rebinding, attach) against a wake being sent to it.
    Lock order everywhere: pane lock, then run lock, then SQLite."""
    name = sha256_obj(("herdr", pane))[:32]
    return _flock(f"pane:{pane}", paths.state_home() / "root-wake-locks" / f"{name}.lock")


def _native_proc_lock(key: str):
    """Fence a native Claude root's process binding across a wake and rebind."""
    name = sha256_obj(("proc", key))[:32]
    return _flock(f"proc:{key}", paths.state_home() / "root-wake-locks" / f"{name}.lock")


def _delivery_lock(run_id: str):
    """Serializes the final pre-send check and prompt with --wake-off and attach."""
    return _flock(f"run:{run_id}", paths.run_dir(run_id) / "root-wake.deliver.lock")


@contextlib.contextmanager
def rebind_fence(con, keys):
    """Held by office start/resume while they move the session binding: locks
    every Herdr pane among the caller's session keys, with no lookup first, so
    no wake to that pane can be between its final check and its send."""
    targets = sorted({(h, s) for h, s in keys if h in ("herdr", "proc") and s})
    with contextlib.ExitStack() as stack:
        for harness, ident in targets:
            stack.enter_context(_pane_lock(ident) if harness == "herdr" else _native_proc_lock(ident))
        yield


def _binding(con, run_id: str) -> dict | None:
    row = con.execute("SELECT * FROM root_wake_bindings WHERE run_id=? AND enabled=1", (run_id,)).fetchone()
    return dict(row) if row else None


def _authoritative_action_signature(con, run: dict) -> str | None:
    """Canonical next action plus durable review/finding/amendment changes.

    `guide._action_signature` intentionally tracks only task/revision/plan
    guidance for `office wait`. A second review verdict or amendment can be
    relevant even if that short guidance remains identical. Keep those *actual*
    SQLite lifecycle records in the wake signature; never hash mere log events.
    """
    from office import guide
    action = guide._action_signature(con, run)
    if action is None:
        return None
    rid = run["id"]
    gates = tuple(tuple(r) for r in con.execute(
        "SELECT id, task_id, revision_id, kind, status, verdict, round, attempt, summary "
        "FROM gates WHERE run_id=? ORDER BY id", (rid,)).fetchall())
    findings = tuple(tuple(r) for r in con.execute(
        "SELECT id, task_id, revision_id, state, severity, blocking, disposition, summary "
        "FROM findings WHERE run_id=? ORDER BY id", (rid,)).fetchall())
    deliveries = tuple(tuple(r) for r in con.execute(
        "SELECT id, task_id, amendment_id, status, target_version FROM deliveries "
        "WHERE run_id=? ORDER BY id", (rid,)).fetchall())
    return sha256_obj((action, gates, findings, deliveries))


def _agent_in_pane(pane: str) -> tuple[str, str] | None:
    """Only Herdr's named Claude agent is supported by the pilot, never a shell."""
    from office import dispatch
    registered = (dispatch._herdr_json(["agent", "list"]).get("agents") or [])
    candidates = [a for a in registered if a.get("pane_id") == pane and a.get("agent") == "claude"
                  and isinstance(a.get("name"), str) and a["name"]]
    if len(candidates) != 1:
        return None
    name = candidates[0]["name"]
    current = dispatch._herdr_json(["agent", "get", name])
    agent = current.get("agent") or current
    if not isinstance(agent, dict) or agent.get("pane_id") != pane or agent.get("agent") != "claude":
        return None
    return name, agent.get("status") or agent.get("agent_status") or "unknown"


def armed(con, run: dict) -> bool:
    return _binding(con, run["id"]) is not None


def _off_marker(run_id: str) -> Path:
    """`--wake-off` is sticky: a plain resume must not re-arm wake until `office resume --auto-wake`."""
    return paths.run_dir(run_id) / "root-wake.off"


def _native_proc_key() -> str | None:
    """Only a real Claude parent process is eligible for native Stop-hook wake."""
    key = discovery.process_key()
    return key if key and key.split(":", 1)[0].lower() == "claude" else None


def _native_hook_installed() -> bool:
    """Do not promise a wake the Claude Code host has not been configured to run."""
    from office import install
    status = install.integration_status("claude")
    return any(i["id"] == "turn.stop" and i["state"] == "current"
               for i in status.get("items", []))


def _attach_native(con, run: dict) -> str:
    """Register this already-bound Claude root, never an arbitrary shell or worker."""
    from office.state import Refused
    key = _native_proc_key()
    if not key:
        raise Refused("wake-no-pane", "no verified Claude process for the native Stop wake path",
                      next_step="office wait")
    if not _native_hook_installed():
        raise Refused("wake-hook-missing", "Claude Stop/asyncRewake hook is not installed",
                      next_step="office install --only claude --item turn.stop, then office resume")
    with _native_proc_lock(key), _delivery_lock(run["id"]):
        with db.transaction(con):
            if not con.execute("SELECT 1 FROM session_bindings WHERE run_id=? AND harness='proc' "
                               "AND session_id=? AND ended_at IS NULL", (run["id"], key)).fetchone():
                raise Refused("wake-unbound", "this Claude process is not bound to the run as its orchestrator")
            if discovery.executor_session_dispatch(con, [("proc", key)]):
                raise Refused("wake-worker", "an executor cannot attach root wake")
            baseline = _authoritative_action_signature(con, run)
            con.execute("INSERT INTO root_wake_bindings(run_id, harness, session_id, pane_id, agent_name, enabled, "
                        "generation, registered_at, last_event_seq, last_action_signature) VALUES(?, 'proc', ?, '', '', 1, 1, ?, "
                        "(SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?), ?) "
                        "ON CONFLICT(run_id) DO UPDATE SET harness='proc', session_id=excluded.session_id, "
                        "pane_id='', agent_name='', enabled=1, generation=root_wake_bindings.generation+1, "
                        "registered_at=excluded.registered_at, supervisor_pid=NULL, "
                        "last_action_signature=excluded.last_action_signature",
                        (run["id"], key, now_iso(), run["id"], baseline))
            # Re-arm receipts that were not confirmed by a previous transport;
            # a claimed prompt remains ambiguous and is not replayed.
            con.execute("UPDATE root_wake_intents SET state='pending', token=NULL, lease_until=NULL, "
                        "not_before=NULL WHERE run_id=? AND state='awaiting_attachment'",
                        (run["id"],))
            con.execute("UPDATE root_wake_intents SET state='awaiting_attachment', token=NULL, "
                        "lease_until=NULL, last_error='root binding replaced; inspect original session' "
                        "WHERE run_id=? AND state='claimed'", (run["id"],))
    return "auto-wake enabled for Claude native Stop/asyncRewake (starts when this root turn ends)"


def arm_on_resume(con, run: dict, *, explicit: bool = False) -> str:
    """Default wake for `office resume`. Never refuses: a root that cannot be verified resumes unarmed.
    `explicit` is `--auto-wake`, the only way to re-arm after `--wake-off`."""
    from office.state import Refused
    if _off_marker(run["id"]).exists() and not explicit:
        return "root auto-wake is off (--wake-off); office resume --auto-wake re-arms it"
    try:
        text = attach(con, run)
    except Refused as exc:
        return f"root auto-wake not armed: {exc.message}"
    _off_marker(run["id"]).unlink(missing_ok=True)
    ensure(run)
    return text


def attach(con, run: dict, *, off: bool = False) -> str:
    """Explicit orchestrator opt-in only; fail closed if the pane is ambiguous."""
    from office.state import Refused
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("wake-worker", "an executor cannot attach the root wake supervisor")
    pane = os.environ.get("HERDR_PANE_ID")
    if not off and not pane:
        return _attach_native(con, run)
    if off:
        _off_marker(run["id"]).touch()
        with _delivery_lock(run["id"]), db.transaction(con):
            con.execute("UPDATE root_wake_bindings SET enabled=0, generation=generation+1 WHERE run_id=?", (run["id"],))
        return "root auto-wake disabled (pending receipts preserved)"
    # Pane lock, then run lock, then reads: nothing that moves this pane or
    # delivers to it can interleave with verification and registration.
    with _pane_lock(pane), _delivery_lock(run["id"]):
        binding = con.execute("SELECT 1 FROM session_bindings WHERE run_id=? AND harness='herdr' AND session_id=? "
                              "AND ended_at IS NULL", (run["id"], pane)).fetchone()
        if not binding:
            raise Refused("wake-unbound", "this Herdr pane is not bound to the run as its orchestrator")
        verified = _agent_in_pane(pane)
        if verified is None:
            raise Refused("wake-unsupported", "Herdr did not verify exactly one named Claude agent in the bound root pane; "
                          "no autonomous prompt will be sent", next_step="office status; use office wait manually")
        name, _ = verified
        # Capture existing guidance as an initial baseline. A later event that leaves
        # the same human decision or unchanged next action is not a wake trigger.
        initial_action = _authoritative_action_signature(con, run)
        with db.transaction(con):
            # The pane may have moved to another run since the check above.
            if not con.execute("SELECT 1 FROM session_bindings WHERE run_id=? AND harness='herdr' AND session_id=? "
                               "AND ended_at IS NULL", (run["id"], pane)).fetchone():
                raise Refused("wake-unbound", "this Herdr pane was rebound to another run before auto-wake registered")
            con.execute("INSERT INTO root_wake_bindings(run_id, harness, session_id, pane_id, agent_name, enabled, "
                        "generation, registered_at, last_event_seq, last_action_signature) VALUES(?,?,?,?,?,1,1,?, "
                        "(SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?), ?) "
                        "ON CONFLICT(run_id) DO UPDATE SET harness=excluded.harness, session_id=excluded.session_id, "
                        "pane_id=excluded.pane_id, agent_name=excluded.agent_name, enabled=1, "
                        "generation=root_wake_bindings.generation+1, registered_at=excluded.registered_at, "
                        "last_event_seq=root_wake_bindings.last_event_seq, last_action_signature=NULL, "
                        "supervisor_pid=NULL", (run["id"], "herdr", pane, pane, name, now_iso(), run["id"], initial_action))
            # Explicit opt-in after inspecting the pane safely retries undelivered
            # receipts. An in-flight claim's outcome is *ambiguous* on rebind and
            # must remain stopped for a further operator inspection.
            con.execute("UPDATE root_wake_intents SET state='pending', token=NULL, lease_until=NULL, "
                        "not_before=NULL WHERE run_id=? AND state='awaiting_attachment'", (run["id"],))
            con.execute("UPDATE root_wake_intents SET state='awaiting_attachment', token=NULL, "
                        "lease_until=NULL, last_error='binding replaced during delivery; inspect pane' "
                        "WHERE run_id=? AND state='claimed'", (run["id"],))
        return f"auto-wake enabled for the verified Claude agent {name} in pane {pane}"


def reconcile(con, run: dict) -> int:
    """Turn committed authoritative transitions into idempotent notification receipts."""
    from office import ctx_alert
    ctx_alert.notice(con, run)
    binding = _binding(con, run["id"])
    if not binding:
        return 0
    if state.is_terminal(state.get_run(con, run["id"])):
        with db.transaction(con):
            con.execute("UPDATE root_wake_intents SET state='retired' WHERE run_id=? "
                        "AND state IN ('pending','retry','claimed')", (run["id"],))
        return 0
    from office import routechange
    routechange.sweep_recovered(con, run)  # before the events read below, so a recovery is a receipt this pass
    rows = con.execute("SELECT seq, kind FROM events WHERE run_id=? AND seq>? ORDER BY seq",
                       (run["id"], binding["last_event_seq"])).fetchall()
    if not rows:
        return 0
    with db.transaction(con):
        # Re-read the generation while holding the writer lock: an old supervisor
        # cannot resurrect receipts for a binding that has since changed.
        live = _binding(con, run["id"])
        if not live or live["generation"] != binding["generation"]:
            return 0
        for row in rows:
            if row["kind"] in ACTIONABLE or row["kind"].endswith(".failed"):
                con.execute("INSERT OR IGNORE INTO root_wake_intents(run_id, event_seq, kind, state, "
                            "created_at) VALUES(?,?,?,'pending',?)",
                            (run["id"], row["seq"], row["kind"], now_iso()))
        con.execute("UPDATE root_wake_bindings SET last_event_seq=? WHERE run_id=? AND generation=?",
                    (rows[-1]["seq"], run["id"], binding["generation"]))
    return len(rows)


def _human_only(action: str) -> bool:
    """Guidance that involves the user, decided conservatively.

    Every Office command that records user authority takes `--quote`, and
    guidance that needs a person names "the user" (ask the user, still needs
    the user, only the user may, stays with the user, ...). A false positive
    costs one ask-the-user round trip; a false negative could let the root
    mint authority, so any user mention except user-visible behaviour counts."""
    low = action.lower().replace("user-visible", "")
    return ("--quote" in low or "authorization" in low or "intake_gap" in low
            or re.search(r"\buser", low) is not None)


def _actionable_now(con, run: dict, seqs: list[int]) -> tuple[str, str, bool] | None:
    """Derive a fresh reason to notify from the *current* Office authority.

    Event names are a cheap candidate filter, not a permission to prompt. The
    actual next action comes from Office's canonical guide, except for a live,
    durable worker-stop signal or confirmed watchdog episode that Office may
    show as a blocker while other work is running. No pane/network reads here.
    """
    from office import guide
    current = state.get_run(con, run["id"])
    if not current or state.is_terminal(current):
        return None
    keys = ",".join("?" for _ in seqs)
    candidates = con.execute(
        f"SELECT seq, kind, task_id, dispatch_id, summary, payload_json FROM events "
        f"WHERE run_id=? AND seq IN ({keys}) ORDER BY seq",
        (run["id"], *seqs)).fetchall()
    if not candidates:
        return None
    # Human-only guidance is derived first so every wake reason below carries
    # the stop-and-ask guard; a signal or stall never bypasses it.
    action = guide.next_action(con, current)
    human = _human_only(action)

    for e in reversed(candidates):
        if e["kind"] == "ctx.high":
            return (sha256_obj(("ctx.high", e["seq"], e["summary"])), e["summary"], human)
    for e in reversed(candidates):
        if e["kind"] == "office.reinstalled":
            return (sha256_obj(("office.reinstalled", e["seq"])),
                    f"office.reinstalled: {e['summary']}. Run `office --run {run['id']} context` first.", human)

    # A confirmed stall is only live while its fingerprint remains the latest
    # active watchdog state. Clearing the stall makes old event rows inert.
    stall = con.execute("SELECT fingerprint FROM root_wake_stall_state WHERE run_id=?",
                        (run["id"],)).fetchone()
    for e in reversed(candidates):
        if e["kind"] != "watchdog.stall" or not stall or not stall["fingerprint"]:
            continue
        payload = json.loads(e["payload_json"] or "{}")
        if payload.get("fingerprint") == stall["fingerprint"]:
            return (sha256_obj(("watchdog.stall", e["seq"], stall["fingerprint"])),
                    "A confirmed silent stall requires orchestrator attention", human)

    # Terminal preflight stops can carry their own durable signal even when
    # guide.next_action still says "exceptions only" for the worker.
    for e in reversed(candidates):
        if e["kind"] != "worker.signal" or not e["task_id"] or not e["dispatch_id"]:
            continue
        task = state.get_task(con, run["id"], e["task_id"])
        if (task and task["status"] not in state.TASK_TERMINAL
                and task.get("current_dispatch_id") == e["dispatch_id"]):
            payload = e["payload_json"] or ""
            return (sha256_obj(("worker.signal", e["task_id"], e["dispatch_id"], payload,
                                task["status"])),
                    f"Worker {e['task_id']} reported an unresolved stop or decision", human)

    # The same action-signature mechanism that protects office wait is derived
    # from phase, task/revision status, frozen requirements/plan and next action.
    sig = _authoritative_action_signature(con, current)
    if sig is None:
        return None
    return sig, action, human


def _claim(con, run: dict) -> tuple[dict, list[int], str, str, str, bool] | None:
    binding = _binding(con, run["id"])
    if not binding:
        return None
    now = now_iso()
    with db.transaction(con):
        live = _binding(con, run["id"])
        if not live or live["generation"] != binding["generation"] or state.is_terminal(state.get_run(con, run["id"])):
            return None
        pending = con.execute("SELECT event_seq FROM root_wake_intents WHERE run_id=? "
                              "AND state IN ('pending','retry') AND (not_before IS NULL OR not_before<=?) "
                              "ORDER BY event_seq LIMIT 100", (run["id"], now)).fetchall()
        if not pending:
            return None
        seqs = [r[0] for r in pending]
        actionable = _actionable_now(con, run, seqs)
        if not actionable or actionable[0] == live["last_action_signature"]:
            # Events may settle before their delayed notification, or be
            # duplicates of guidance the root has already received. Preserve
            # receipts for audit, but do not spend a redundant model turn.
            con.executemany(
                "UPDATE root_wake_intents SET state='settled', last_error=? "
                "WHERE run_id=? AND event_seq=? AND state IN ('pending','retry')",
                [("no new authoritative next action", run["id"], seq) for seq in seqs])
            return None
        # One claimed batch per run. One other supervisor cannot deliver a competing prompt.
        held = con.execute("SELECT 1 FROM root_wake_intents WHERE run_id=? AND state='claimed' "
                           "AND lease_until>? LIMIT 1", (run["id"], now)).fetchone()
        if held:
            return None
        token = uuid.uuid4().hex
        con.executemany("UPDATE root_wake_intents SET state='claimed', token=?, "
                        "lease_until=?, attempts=attempts+1 WHERE run_id=? AND event_seq=?",
                        [(token, (parse_iso(now) + timedelta(seconds=30)).isoformat(), run["id"], seq) for seq in seqs])
    return binding, seqs, token, *actionable


def _finish(con, run_id: str, seqs: list[int], token: str, state_name: str, error: str = "",
            *, signature: str | None = None, generation: int | None = None) -> None:
    with db.transaction(con):
        if state_name == "delivered":
            # The root may already have read `office context` while Herdr was
            # still confirming the landing; keep that stronger `observed` receipt.
            con.executemany("UPDATE root_wake_intents SET state=CASE WHEN state IN "
                            "('observed','root_activity_observed') THEN state ELSE 'delivered' END, delivered_at=?, token=NULL, "
                            "lease_until=NULL, last_error=NULL WHERE run_id=? AND event_seq=? AND token=?",
                            [(now_iso(), run_id, seq, token) for seq in seqs])
            if signature is not None and generation is not None:
                con.execute("UPDATE root_wake_bindings SET last_action_signature=? "
                            "WHERE run_id=? AND enabled=1 AND generation=?",
                            (signature, run_id, generation))
        else:
            # A root that already observed fresh state outranks any transport
            # outcome; only release the claim on those receipts.
            con.executemany("UPDATE root_wake_intents SET token=NULL, lease_until=NULL "
                            "WHERE run_id=? AND event_seq=? AND token=? "
                            "AND state IN ('observed','root_activity_observed')",
                            [(run_id, seq, token) for seq in seqs])
            for seq in seqs:
                row = con.execute("SELECT attempts FROM root_wake_intents WHERE run_id=? AND event_seq=? "
                                  "AND token=?", (run_id, seq, token)).fetchone()
                if not row:
                    continue
                attempts = row[0]
                retry = state_name == "retry" and attempts < MAX_ATTEMPTS
                defer = state_name == "defer"
                until = (parse_iso(now_iso()) + timedelta(seconds=1 if defer else min(2 ** attempts, 16))).isoformat() if (retry or defer) else None
                con.execute("UPDATE root_wake_intents SET state=?, token=NULL, lease_until=NULL, "
                            "last_error=?, not_before=?, attempts=? WHERE run_id=? AND event_seq=? AND token=?",
                            ("retry" if retry or defer else state_name if state_name != "retry" else "awaiting_attachment",
                             error[:300], until, attempts - 1 if defer else attempts, run_id, seq, token))


def _wake_message(run: dict, seqs: list[int], action: str, human: bool) -> str:
    """The same authority-guarded continuation for both verified transports."""
    guard = (" HUMAN DECISION REQUIRED: ask the user and stop. No authorization, "
             "waiver, approval, or merge may be inferred." if human else "")
    msg = (f"Auto Office run {run['id']} has new lifecycle transitions (through event {max(seqs)}). "
           f"Read `office --run {run['id']} context`, then follow only its authorized next action; "
           "continue through checks, independent reviews and closeout until a genuine wait or human decision. "
           "Do not infer success from a submit. This notification may be a duplicate; use fresh Office state."
           + guard)
    if action.startswith(("ctx-high:", "office.reinstalled:")):
        msg += "\n\n" + action
    return msg


def poll(con, run: dict) -> str:
    """One non-model supervisor tick; no authority or work dispatch happens here."""
    from office import dispatch
    reconcile(con, run)
    batch = _claim(con, run)
    if batch is None:
        return "no-action"
    binding, seqs, token, _, _, _ = batch
    if binding["harness"] != "herdr":
        _finish(con, run["id"], seqs, token, "defer", "not a Herdr root binding")
        return "unsupported"
    try:
        # Revalidate current run, root ownership and registered pane immediately
        # before touching Herdr, never trust a stale session id or an output pane.
        live = _binding(con, run["id"])
        if (not live or live["generation"] != binding["generation"] or
                live["pane_id"] != binding["pane_id"] or
                not con.execute("SELECT 1 FROM session_bindings WHERE run_id=? AND harness='herdr' "
                                "AND session_id=? AND ended_at IS NULL", (run["id"], binding["pane_id"])).fetchone() or
                state.is_terminal(state.get_run(con, run["id"]))):
            _finish(con, run["id"], seqs, token, "awaiting_attachment", "root binding changed or ended")
            return "detached"
        verified = _agent_in_pane(binding["pane_id"])
        if verified is None or verified[0] != binding["agent_name"]:
            _finish(con, run["id"], seqs, token, "awaiting_attachment", "root agent unavailable or mismatched")
            return "detached"
        if verified[1] not in IDLE_STATES:
            if verified[1] == "working":
                _finish(con, run["id"], seqs, token, "defer", "root busy; queued for idle")
                return "busy"
            _finish(con, run["id"], seqs, token, "awaiting_attachment", "root state not safely idle")
            return "unsupported"
        # The worker/reviewer may have finished, been accepted or been
        # superseded while Herdr was reporting idle. Re-evaluate Office's
        # authoritative action *immediately* before the external prompt.
        # Fence the final check and the prompt against --wake-off and rebinds.
        with _pane_lock(binding["pane_id"]), _delivery_lock(run["id"]):
            # The root may have started a turn since the idle check above.
            again = _agent_in_pane(binding["pane_id"])
            if again is None or again[0] != binding["agent_name"]:
                _finish(con, run["id"], seqs, token, "awaiting_attachment", "root agent unavailable or mismatched")
                return "detached"
            if again[1] not in IDLE_STATES:
                _finish(con, run["id"], seqs, token, "defer", "root busy; queued for idle")
                return "busy"
            current = _actionable_now(con, run, seqs)
            live = _binding(con, run["id"])
            if (not live or live["generation"] != binding["generation"] or
                    live["pane_id"] != binding["pane_id"] or
                    not con.execute("SELECT 1 FROM session_bindings WHERE run_id=? AND harness='herdr' "
                                    "AND session_id=? AND ended_at IS NULL",
                                    (run["id"], binding["pane_id"])).fetchone()):
                _finish(con, run["id"], seqs, token, "awaiting_attachment",
                        "root detached or rebound during actionability check")
                return "detached"
            if current is None or current[0] == live["last_action_signature"]:
                _finish(con, run["id"], seqs, token, "settled", "transition was settled before delivery")
                return "no-action"
            signature, action, human = current
            msg = _wake_message(run, seqs, action, human)
            # Herdr prompt landed != root observation. The next `office context`
            # advances the distinct observed cursor; no repeated model keepalives.
            landed = dispatch.submit_prompt(binding["agent_name"], msg, pane=binding["pane_id"])
            if landed == "landed":
                _finish(con, run["id"], seqs, token, "delivered",
                        signature=signature, generation=binding["generation"])
                return "delivered"
            _finish(con, run["id"], seqs, token, "awaiting_attachment",
                    f"prompt receipt {landed or 'unknown'}; cannot prove delivery; inspect pane before manual recovery")
            return "unknown"
    except Exception as exc:
        # A transport exception can happen *after* Herdr took the prompt. Never
        # issue a second unverified model turn; require explicit inspection.
        _finish(con, run["id"], seqs, token, "awaiting_attachment",
                f"ambiguous prompt transport {type(exc).__name__}: {exc}; inspect pane")
        return "unknown"


def observe(con, run: dict) -> None:
    """A bound root actually called office context; transport alone is not this proof."""
    if os.environ.get("OFFICE_DISPATCH_ID"):
        return
    binding = _binding(con, run["id"])
    if not binding or (binding["harness"], binding["session_id"]) not in discovery.session_keys():
        return
    with db.transaction(con):
        # `claimed` too: Herdr's landing confirmation takes seconds, and a root
        # that is already reading context must not lose its observation receipt.
        con.execute("UPDATE root_wake_intents SET state='observed', observed_at=? WHERE run_id=? "
                    "AND state IN ('delivered','claimed')", (now_iso(), run["id"]))


def record_root_activity(con, run: dict, *, before: int, command: str) -> None:
    """Record root activity, NOT completion of any particular wake intent.

    A state-changing authorized root command is evidence that the root resumed,
    but unrelated commands cannot prove the event's requested work is done.
    The authoritative task/gate state remains the only completion evidence.
    """
    binding = _binding(con, run["id"])
    if not binding or os.environ.get("OFFICE_DISPATCH_ID") or (binding["harness"], binding["session_id"]) not in discovery.session_keys():
        return
    latest = con.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?", (run["id"],)).fetchone()[0]
    if latest <= before:
        return
    with db.transaction(con):
        con.execute("UPDATE root_wake_intents SET state='root_activity_observed', "
                    "root_activity_at=?, root_activity_command=?, root_activity_event_seq=? "
                    "WHERE run_id=? AND state='observed'",
                    (now_iso(), command, latest, run["id"]))


def metrics(con, run: dict) -> dict:
    """Inspectable durable delivery/observation/continuation receipts in runs.db."""
    binding = _binding(con, run["id"])
    if not binding:
        return {"enabled": False}
    row = con.execute("SELECT i.event_seq, i.kind, e.created_at AS event_at, i.delivered_at, "
                      "i.observed_at, i.acted_at, i.root_activity_at, i.root_activity_command, "
                      "i.root_activity_event_seq, i.last_error "
                      "FROM root_wake_intents i JOIN events e ON e.run_id=i.run_id AND e.seq=i.event_seq "
                      "WHERE i.run_id=? AND i.delivered_at IS NOT NULL ORDER BY i.delivered_at DESC LIMIT 1",
                      (run["id"],)).fetchone()
    last = dict(row) if row else None
    delay = int((parse_iso(last["delivered_at"]) - parse_iso(last["event_at"])).total_seconds() * 1000) if last else None
    return {"enabled": True, "harness": binding["harness"],
            "transport": "claude-asyncRewake" if binding["harness"] == "proc" else "herdr",
            "listener_active": (bool(binding["supervisor_pid"]) and pid_alive(binding["supervisor_pid"]))
            if binding["harness"] == "proc" else True,
            "pane": binding["pane_id"],
            "generation": binding["generation"], "last_event_seq": binding["last_event_seq"],
            "last_delivery": last, "last_delivery_ms": delay}


def _unarmed_line(con, run: dict) -> str:
    if _off_marker(run["id"]).exists():
        return "wake: off (--wake-off); office resume --auto-wake re-arms it"
    if not os.environ.get("HERDR_PANE_ID"):
        if _native_proc_key() and not _native_hook_installed():
            return "wake: unavailable (Claude Stop/asyncRewake hook missing); office install --only claude --item turn.stop"
        return "wake: unavailable (not inside a Herdr pane); use office wait"
    return "wake: unavailable (this pane is not a verified bound Claude root); office resume from it arms wake"


def lines(con, run: dict) -> list[str]:
    """The wake state line `office status` prints for every non-terminal run."""
    if state.is_terminal(run):
        return []
    binding = _binding(con, run["id"])
    if not binding:
        return [_unarmed_line(con, run)]
    counts = dict(con.execute("SELECT state, COUNT(*) FROM root_wake_intents WHERE run_id=? "
                              "GROUP BY state", (run["id"],)).fetchall())
    waiting = counts.get("pending", 0) + counts.get("retry", 0) + counts.get("claimed", 0)
    failed = counts.get("awaiting_attachment", 0)
    delivered = counts.get("delivered", 0)
    if binding["harness"] == "proc":
        pid = binding["supervisor_pid"]
        phase = "listener active" if pid and pid_alive(pid) else "arms on next Claude Stop"
        text = (f"wake: armed (Claude native Stop/asyncRewake; {phase}); "
                f"queued {waiting}, delivered/unobserved {delivered}, awaiting attachment {failed}")
    else:
        text = (f"wake: armed (Claude agent {binding['agent_name']} in Herdr pane {binding['pane_id']}); "
                f"queued {waiting}, delivered/unobserved {delivered}, awaiting attachment {failed}")
    if failed:
        text += ("; next: inspect native Claude session before office resume --auto-wake"
                 if binding["harness"] == "proc" else "; next: inspect pane before office resume")
    last_ms = metrics(con, run).get("last_delivery_ms")
    if last_ms is not None:
        text += f"; last completion wake {last_ms}ms"
    return [text]


def watchdog_tick(con, run: dict) -> str:
    """Slow, separate no-model stall pass, persistently deduped by episode.

    The root's fast completion path never reads the agent pane or worktree for
    stalls. A new *confirmed* stalled state emits one ordinary Office event.
    This event only wakes the attached root; it never changes a gate or dispatch.
    """
    if not _binding(con, run["id"]) or state.is_terminal(state.get_run(con, run["id"])):
        return "inactive"
    from office import guide, questions, watchdog
    episodes = watchdog.review_episodes(con, run, probe=True)
    with db.transaction(con):
        watchdog.record_review_episodes(con, run, episodes)
    _, _, acts = questions.scan(con, run)
    stalls = guide.stalls(con, run, acts=acts, episodes=episodes)
    stable = tuple(sorted(re.sub(r"\b\d+s\b", "Xs", s) for s in stalls))
    signature = sha256_obj(stable) if stalls else None
    with db.transaction(con):
        row = con.execute("SELECT fingerprint FROM root_wake_stall_state WHERE run_id=?", (run["id"],)).fetchone()
        prior = row[0] if row else None
        if signature == prior:
            return "unchanged"
        con.execute("INSERT INTO root_wake_stall_state(run_id, fingerprint, last_alert_at) VALUES(?,?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET fingerprint=excluded.fingerprint, "
                    "last_alert_at=excluded.last_alert_at", (run["id"], signature, now_iso() if signature else None))
        if signature:
            state.emit(con, run, "watchdog.stall", f"silent-stall watchdog detected {len(stalls)} blocker(s): "
                       + "; ".join(stalls)[:260], payload={"fingerprint": signature, "count": len(stalls)},
                       audience="orchestrator")
            return "alerted"
    return "cleared"


def _watchdog_loop(run_id: str) -> None:
    """Dedicated slower thread with its own DB connection; cannot hold the wake poll."""
    while True:
        con = None
        try:
            con = db.connect()
            run = state.get_run(con, run_id)
            if not run or state.is_terminal(run) or not _binding(con, run_id):
                return
            watchdog_tick(con, run)
            _recover_reinstall(con, run)
        except Exception as exc:
            if isinstance(exc, (ImportError, OSError)):
                _tick_failed(run_id, exc)
            # Monitoring failures never mean a worker is stalled. Keep normal
            # completion notification healthy and retry on the next slow tick.
            print(f"root wake watchdog probe unavailable: {type(exc).__name__}", file=sys.stderr)
        finally:
            if con is not None:
                con.close()
        time.sleep(WATCHDOG_POLL_SECONDS)


def ensure(run: dict) -> None:
    """Restore a non-model supervisor after a crash from a normal Office command."""
    con = db.connect()
    try:
        binding = _binding(con, run["id"])
        if not binding or state.is_terminal(state.get_run(con, run["id"])):
            return
        if binding["harness"] == "proc":
            return  # the native Claude Stop hook is itself the async watcher
        if binding["supervisor_pid"] and pid_alive(binding["supervisor_pid"]):
            return
        log_path = paths.run_dir(run["id"]) / "root-wake.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        for key in ("OFFICE_DISPATCH_ID", "OFFICE_TASK_ID", "OFFICE_ROLE", "OFFICE_WORKER_TAG"):
            env.pop(key, None)
        with log_path.open("a") as log:
            proc = subprocess.Popen([sys.executable, "-m", "office", "_wake_supervise", run["id"]],
                                    cwd=run["repo_root"], env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, start_new_session=True, close_fds=True)
        with db.transaction(con):
            con.execute("UPDATE root_wake_bindings SET supervisor_pid=? WHERE run_id=? AND generation=?",
                        (proc.pid, run["id"], binding["generation"]))
    finally:
        con.close()


def _supervise_tick(run_id: str) -> bool:
    """One supervisor pass on its own connection. False when the run no longer needs a supervisor."""
    con = db.connect()
    try:
        run = state.get_run(con, run_id)
        if not run or state.is_terminal(run) or not _binding(con, run_id):
            return False
        # A lost process might have delivered the prompt right before its
        # crash; NEVER automatically replay the ambiguous model turn.
        with db.transaction(con):
            con.execute("UPDATE root_wake_intents SET state='awaiting_attachment', token=NULL, "
                        "lease_until=NULL, last_error='claim lease expired; inspect pane before re-attach' "
                        "WHERE run_id=? AND state='claimed' AND lease_until<=?",
                        (run_id, now_iso()))
        poll(con, run)
        _recover_reinstall(con, run)
        return True
    finally:
        con.close()


def supervise(run_id: str, *, once: bool = False) -> int:
    """Short, single-owner DB watcher. No model polling or fabricated prompts."""
    run_dir = paths.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock = (run_dir / "root-wake.lock").open("a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # Pane/worktree stall probes can be slow or unavailable. Run them off
        # the 0.5s completion-wake critical path, with an independent cadence.
        threading.Thread(target=_watchdog_loop, args=(run_id,), daemon=True,
                         name=f"office-stall-{run_id[:8]}").start()
        while True:
            try:
                if not _supervise_tick(run_id):
                    return 0
            except (ImportError, OSError) as exc:
                # The install is being replaced under this process (#833, #920): keep supervising.
                _tick_failed(run_id, exc)
            if once:
                return 0
            time.sleep(MIN_POLL)
    finally:
        lock.close()


# #940: non-Herdr Claude Code root continuation. Native asyncRewake is a
# *host-managed* idle callback, never a second model client or a shell injection.
# Its Stop hook is a single background process with a bounded lifetime; the
# same event intents, action signatures and receipts as Herdr remain authority.
NATIVE_WATCH_SECONDS = 86380  # below the configured 24h Claude hook timeout


def _native_valid(con, run: dict, key: str, session: str, generation: int) -> bool:
    binding = _binding(con, run["id"])
    return bool(binding and binding["harness"] == "proc"
                and binding["session_id"] == key and binding["agent_name"] == session
                and binding["generation"] == generation and not _off_marker(run["id"]).exists()
                and not state.is_terminal(state.get_run(con, run["id"]))
                and con.execute("SELECT 1 FROM session_bindings WHERE run_id=? AND harness='proc' "
                                "AND session_id=? AND ended_at IS NULL", (run["id"], key)).fetchone())


def _native_poll(con, run: dict, key: str, session: str, generation: int) -> str:
    """One safe native receipt. Only the *caller*'s exit 2 signals Claude Code."""
    reconcile(con, run)
    batch = _claim(con, run)
    if batch is None:
        return "no-action"
    _, seqs, token, _, _, _ = batch
    # The lock fences prompt.started, --wake-off and rebinding against the
    # final check, stderr write and durable receipt (exactly one wake intent).
    with _native_proc_lock(key), _delivery_lock(run["id"]):
        if not _native_valid(con, run, key, session, generation):
            _finish(con, run["id"], seqs, token, "defer", "root resumed or rebound before wake")
            return "detached"
        current = _actionable_now(con, run, seqs)
        live = _binding(con, run["id"])
        if current is None or current[0] == live["last_action_signature"]:
            _finish(con, run["id"], seqs, token, "settled", "transition settled before native wake")
            return "no-action"
        signature, action, human = current
        try:
            # Documented Claude Code asyncRewake contract: exit 2 and stderr
            # become a system reminder in the *existing idle* session.
            sys.stderr.write(_wake_message(run, seqs, action, human) + "\n")
            sys.stderr.flush()
        except OSError as exc:
            _finish(con, run["id"], seqs, token, "awaiting_attachment",
                    f"native hook output ambiguous: {type(exc).__name__}; inspect session")
            return "unknown"
        _finish(con, run["id"], seqs, token, "delivered", signature=signature,
                generation=generation)
        return "delivered"


def native_prompt_started(payload: dict) -> None:
    """An actual new user prompt invalidates the former idle Stop watcher.

    Prompt-submit already has a managed Claude hook; no turn is synthesized.
    Its immediate action cannot be mistaken for an idle agent to wake later.
    """
    if os.environ.get("OFFICE_DISPATCH_ID") or not payload.get("session_id"):
        return
    key = _native_proc_key()
    if not key:
        return
    con = db.connect()
    try:
        row = con.execute("SELECT run_id FROM session_bindings WHERE harness='proc' AND session_id=? "
                          "AND ended_at IS NULL", (key,)).fetchone()
        if not row:
            return
        run_id = row[0]
        with _native_proc_lock(key), _delivery_lock(run_id), db.transaction(con):
            binding = _binding(con, run_id)
            if binding and binding["harness"] == "proc" and binding["session_id"] == key \
                    and binding["agent_name"] == payload["session_id"]:
                con.execute("UPDATE root_wake_bindings SET generation=generation+1, "
                            "supervisor_pid=NULL WHERE run_id=? AND generation=?",
                            (run_id, binding["generation"]))
                con.execute("UPDATE root_wake_intents SET state='pending', token=NULL, "
                            "lease_until=NULL, not_before=NULL WHERE run_id=? AND state='claimed'",
                            (run_id,))
    finally:
        con.close()


def native_stop_hook(payload: dict, *, timeout: float = NATIVE_WATCH_SECONDS,
                     poll_seconds: float = MIN_POLL) -> int:
    """Wait inside Claude's async Stop hook; exit 2 only for a verified event.

    An executor, a stray shell, a rebound root, and a different Claude session
    cannot select a run from this hook. A duplicate Stop hook exits quietly.
    """
    if os.environ.get("OFFICE_DISPATCH_ID"):
        return 0
    session = payload.get("session_id")
    key = _native_proc_key()
    if not isinstance(session, str) or not session or not key:
        return 0
    con = db.connect()
    try:
        row = con.execute("SELECT run_id FROM session_bindings WHERE harness='proc' AND session_id=? "
                          "AND ended_at IS NULL", (key,)).fetchone()
        if not row:
            return 0
        run = state.get_run(con, row[0])
        if not run or state.is_terminal(run):
            return 0
        if discovery.executor_session_dispatch(con, [("proc", key)]) or discovery.task_worktree(
                con, Path(payload.get("cwd") or os.getcwd())):
            return 0
        binding = _binding(con, run["id"])
        if not binding or binding["harness"] != "proc" or binding["session_id"] != key or \
                (binding["agent_name"] and binding["agent_name"] != session):
            return 0
        from office import version
        if not version.serves(run["office_version"], version.current()):
            return 0
        # No second background watcher when Claude re-enters and triggers an
        # overlapping Stop. The wait is bounded so a previous hook can retire.
        lock_path = paths.run_dir(run["id"]) / "root-wake.native.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            for _ in range(20):
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.1)
            else:
                return 0
            try:
                with _native_proc_lock(key), _delivery_lock(run["id"]), db.transaction(con):
                    live = _binding(con, run["id"])
                    if not live or live["harness"] != "proc" or live["session_id"] != key or \
                            (live["agent_name"] and live["agent_name"] != session):
                        return 0
                    con.execute("UPDATE root_wake_bindings SET agent_name=?, supervisor_pid=? "
                                "WHERE run_id=? AND generation=?", (session, os.getpid(), run["id"], live["generation"]))
                    generation = live["generation"]
                # The slow 3.15.0 stall/review watchdog is *reused*, not reimplemented.
                threading.Thread(target=_watchdog_loop, args=(run["id"],), daemon=True,
                                 name=f"office-native-stall-{run['id'][:8]}").start()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if not _native_valid(con, run, key, session, generation):
                        return 0
                    outcome = _native_poll(con, run, key, session, generation)
                    if outcome == "delivered":
                        return 2
                    if outcome in ("detached", "unknown"):
                        return 0
                    time.sleep(poll_seconds)
                return 0
            finally:
                with db.transaction(con):
                    con.execute("UPDATE root_wake_bindings SET supervisor_pid=NULL WHERE run_id=? "
                                "AND harness='proc' AND session_id=? AND agent_name=? AND generation=? "
                                "AND supervisor_pid=?",
                                (run["id"], key, session, generation, os.getpid()))
                fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        con.close()
