"""Opt-in, run-scoped root continuation transport (issue #723).

This is not a second lifecycle engine. The Office event log is authority; rows here
are durable notification receipts. No agent is started or user authority minted.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

from office import db, discovery, paths, state
from office.util import now_iso, parse_iso, pid_alive

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
    "pr.error", "pr.merged", "worker.signal", "amendment.applied", "amendment.contract_changed",
    "requirements.changed", "finding.disposition", "authority.plan", "authority.land",
})
MIN_POLL = 0.5
MAX_ATTEMPTS = 4


def _binding(con, run_id: str) -> dict | None:
    row = con.execute("SELECT * FROM root_wake_bindings WHERE run_id=? AND enabled=1", (run_id,)).fetchone()
    return dict(row) if row else None


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


def attach(con, run: dict, *, off: bool = False) -> str:
    """Explicit orchestrator opt-in only; fail closed if the pane is ambiguous."""
    from office.state import Refused
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("wake-worker", "an executor cannot attach the root wake supervisor")
    pane = os.environ.get("HERDR_PANE_ID")
    if not off and not pane:
        raise Refused("wake-no-pane", "auto-wake pilot requires the active HERDR_PANE_ID; no detached root launch",
                      next_step="office resume without --auto-wake")
    if off:
        with db.transaction(con):
            con.execute("UPDATE root_wake_bindings SET enabled=0, generation=generation+1 WHERE run_id=?", (run["id"],))
        return "root auto-wake disabled (pending receipts preserved)"
    binding = con.execute("SELECT 1 FROM session_bindings WHERE run_id=? AND harness='herdr' AND session_id=? "
                          "AND ended_at IS NULL", (run["id"], pane)).fetchone()
    if not binding:
        raise Refused("wake-unbound", "this Herdr pane is not bound to the run as its orchestrator")
    verified = _agent_in_pane(pane)
    if verified is None:
        raise Refused("wake-unsupported", "Herdr did not verify exactly one named Claude agent in the bound root pane; "
                      "no autonomous prompt will be sent", next_step="office status; use office wait manually")
    name, _ = verified
    with db.transaction(con):
        con.execute("INSERT INTO root_wake_bindings(run_id, harness, session_id, pane_id, agent_name, enabled, "
                    "generation, registered_at, last_event_seq) VALUES(?,?,?,?,?,1,1,?, "
                    "(SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?)) "
                    "ON CONFLICT(run_id) DO UPDATE SET harness=excluded.harness, session_id=excluded.session_id, "
                    "pane_id=excluded.pane_id, agent_name=excluded.agent_name, enabled=1, "
                    "generation=root_wake_bindings.generation+1, registered_at=excluded.registered_at, "
                    "last_event_seq=root_wake_bindings.last_event_seq, supervisor_pid=NULL", (run["id"], "herdr", pane, pane, name, now_iso(), run["id"]))
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
    binding = _binding(con, run["id"])
    if not binding:
        return 0
    if state.is_terminal(state.get_run(con, run["id"])):
        with db.transaction(con):
            con.execute("UPDATE root_wake_intents SET state='retired' WHERE run_id=? "
                        "AND state IN ('pending','retry','claimed')", (run["id"],))
        return 0
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


def _runnable(con, run: dict) -> bool:
    from office import guide
    action = guide.next_action(con, run)
    return not action.startswith(("no action", "exceptions only", "none;"))


def _claim(con, run: dict) -> tuple[dict, list[int], str] | None:
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
        # One claimed batch per run. One other supervisor cannot deliver a competing prompt.
        held = con.execute("SELECT 1 FROM root_wake_intents WHERE run_id=? AND state='claimed' "
                           "AND lease_until>? LIMIT 1", (run["id"], now)).fetchone()
        if held:
            return None
        token = uuid.uuid4().hex
        seqs = [r[0] for r in pending]
        con.executemany("UPDATE root_wake_intents SET state='claimed', token=?, "
                        "lease_until=?, attempts=attempts+1 WHERE run_id=? AND event_seq=?",
                        [(token, (parse_iso(now) + timedelta(seconds=30)).isoformat(), run["id"], seq) for seq in seqs])
    return binding, seqs, token


def _finish(con, run_id: str, seqs: list[int], token: str, state_name: str, error: str = "") -> None:
    with db.transaction(con):
        if state_name == "delivered":
            con.executemany("UPDATE root_wake_intents SET state='delivered', delivered_at=?, token=NULL, "
                            "lease_until=NULL, last_error=NULL WHERE run_id=? AND event_seq=? AND token=?",
                            [(now_iso(), run_id, seq, token) for seq in seqs])
        else:
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


def poll(con, run: dict) -> str:
    """One non-model supervisor tick; no authority or work dispatch happens here."""
    from office import dispatch
    reconcile(con, run)
    batch = _claim(con, run)
    if batch is None:
        return "no-action"
    binding, seqs, token = batch
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
        if verified[1] != "idle":
            if verified[1] == "working":
                _finish(con, run["id"], seqs, token, "defer", "root busy; queued for idle")
                return "busy"
            _finish(con, run["id"], seqs, token, "awaiting_attachment", "root state not safely idle")
            return "unsupported"
        msg = (f"Auto Office run {run['id']} has new lifecycle transitions (through event {max(seqs)}). "
               f"Read `office --run {run['id']} context`, then follow only its authorized next action; "
               "continue through checks, independent reviews and closeout until a genuine wait or human decision. "
               "Do not infer success from a submit. This notification may be a duplicate; use fresh Office state.")
        # Herdr prompt landed != root observation. The next `office context`
        # advances the distinct observed cursor; no repeated model keepalives.
        landed = dispatch.submit_prompt(binding["agent_name"], msg, pane=binding["pane_id"])
        if landed == "landed":
            _finish(con, run["id"], seqs, token, "delivered")
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
    if not binding or ("herdr", binding["session_id"]) not in discovery.session_keys():
        return
    with db.transaction(con):
        con.execute("UPDATE root_wake_intents SET state='observed', observed_at=? WHERE run_id=? "
                    "AND state='delivered'", (now_iso(), run["id"]))


def acted(con, run: dict, *, before: int, command: str) -> None:
    """Record that a *root-invoked* lifecycle command changed authoritative state."""
    binding = _binding(con, run["id"])
    if not binding or os.environ.get("OFFICE_DISPATCH_ID") or ("herdr", binding["session_id"]) not in discovery.session_keys():
        return
    latest = con.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?", (run["id"],)).fetchone()[0]
    if latest <= before:
        return
    with db.transaction(con):
        con.execute("UPDATE root_wake_intents SET state='acted', acted_at=? WHERE run_id=? "
                    "AND state='observed'", (now_iso(), run["id"]))


def metrics(con, run: dict) -> dict:
    """Inspectable durable delivery/observation/continuation receipts in runs.db."""
    binding = _binding(con, run["id"])
    if not binding:
        return {"enabled": False}
    row = con.execute("SELECT i.event_seq, i.kind, e.created_at AS event_at, i.delivered_at, "
                      "i.observed_at, i.acted_at, i.last_error "
                      "FROM root_wake_intents i JOIN events e ON e.run_id=i.run_id AND e.seq=i.event_seq "
                      "WHERE i.run_id=? AND i.delivered_at IS NOT NULL ORDER BY i.delivered_at DESC LIMIT 1",
                      (run["id"],)).fetchone()
    last = dict(row) if row else None
    delay = int((parse_iso(last["delivered_at"]) - parse_iso(last["event_at"])).total_seconds() * 1000) if last else None
    return {"enabled": True, "harness": binding["harness"], "pane": binding["pane_id"],
            "generation": binding["generation"], "last_event_seq": binding["last_event_seq"],
            "last_delivery": last, "last_delivery_ms": delay}


def lines(con, run: dict) -> list[str]:
    binding = _binding(con, run["id"])
    if not binding:
        return []
    counts = dict(con.execute("SELECT state, COUNT(*) FROM root_wake_intents WHERE run_id=? "
                              "GROUP BY state", (run["id"],)).fetchall())
    waiting = counts.get("pending", 0) + counts.get("retry", 0) + counts.get("claimed", 0)
    failed = counts.get("awaiting_attachment", 0)
    delivered = counts.get("delivered", 0)
    text = f"auto-wake: Claude/Herdr opted in; queued {waiting}, delivered/unobserved {delivered}, awaiting attachment {failed}"
    if failed:
        text += "; next: inspect pane before office resume --auto-wake"
    last_ms = metrics(con, run).get("last_delivery_ms")
    if last_ms is not None:
        text += f"; last completion wake {last_ms}ms"
    return [text]


def ensure(run: dict) -> None:
    """Restore a non-model supervisor after a crash from a normal Office command."""
    con = db.connect()
    try:
        binding = _binding(con, run["id"])
        if not binding or state.is_terminal(state.get_run(con, run["id"])):
            return
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
        while True:
            con = db.connect()
            try:
                run = state.get_run(con, run_id)
                if not run or state.is_terminal(run) or not _binding(con, run_id):
                    return 0
                # A lost process might have delivered the prompt right before its
                # crash; NEVER automatically replay the ambiguous model turn.
                with db.transaction(con):
                    con.execute("UPDATE root_wake_intents SET state='awaiting_attachment', token=NULL, "
                                "lease_until=NULL, last_error='claim lease expired; inspect pane before re-attach' "
                                "WHERE run_id=? AND state='claimed' AND lease_until<=?",
                                (run_id, now_iso()))
                poll(con, run)
            finally:
                con.close()
            if once:
                return 0
            time.sleep(MIN_POLL)
    finally:
        lock.close()
