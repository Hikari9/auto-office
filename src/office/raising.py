"""`office raise`: an executor stops on something only the orchestrator resolves, without submitting (#472).

`office submit` takes finished work. A question, a blocker the executor cannot clear, or a file outside its
scope has no submit: before this, the executor left the question in its pane or final text, and whether the
orchestrator heard depended on the transport. A raise is a durable event tied to run, task, dispatch and
session that blocks the task (self-blocked, like a refused submit), wakes `office wait` (exit 5, once per
raise), stays in `office status` until answered, and is answered with `office answer`, for a live pane and a
headless worker alike.

Authority does not move: a raise is a request. An answer is a message, never a contract change; a
scope-request is satisfied only by `office amend --contract`, which also lifts the block.

State is events only (`task.raised`, `task.raise_answered`, `task.raise_closed`), so `office prune` removes
it with the run. A raise is open while its dispatch still holds the task with a live lease and the block it
recorded is still in force; a revoke, a newer dispatch, an amendment or a resubmit ends it.
"""
from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path

from office import db, dispatch, gates, prompting, state, submit
from office.result import Result
from office.state import Refused, Usage
from office.util import loads

KINDS = ("question", "blocker", "scope-request")
RAISED = "task.raised"
ANSWERED = "task.raise_answered"
CLOSED = "task.raise_closed"
REPORTED = "raise-reported"  # cursor consumer: the newest raise `office wait` has already reported
MAX_TEXT = 2000
SHOWN = 300


def normalize(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def dedup_key(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}\0{normalize(text)}".encode()).hexdigest()[:16]


def _shown(text: str, limit: int = SHOWN) -> str:
    s = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _amend_command(task_id: str, files: list[str], text: str) -> str:
    # Executor-controlled text reaches a command the orchestrator copies: one quoted argument.
    return ("office amend " + shlex.quote(task_id) + " --contract -- "
            + shlex.quote(f'add {", ".join(files)} to {task_id} scope: {_shown(text, 80)}'))


def history(con, run_id: str, *, dispatch_id: str | None = None, task_id: str | None = None) -> list[dict]:
    """Every raise of the run (of one dispatch or task), oldest first, with what became of it:
    `state` is open, answered or closed (`closed_why` says how); `open` may still be stale, see `status`."""
    sql, args = "SELECT * FROM events WHERE run_id=? AND kind IN (?,?,?)", [run_id, RAISED, ANSWERED, CLOSED]
    for col, val in (("dispatch_id", dispatch_id), ("task_id", task_id)):
        if val:
            sql, args = sql + f" AND {col}=?", [*args, val]
    out: dict[int, dict] = {}
    for e in con.execute(sql + " ORDER BY seq", args).fetchall():
        p = loads(e["payload_json"], {})
        if e["kind"] == RAISED:
            out[e["seq"]] = {"seq": e["seq"], "task_id": e["task_id"], "dispatch_id": e["dispatch_id"], "state": "open",
                             "kind": p.get("kind"), "text": p.get("text") or "", "files": p.get("files") or [],
                             "dedup": p.get("dedup"), "block_id": p.get("block_id"), "session": p.get("session"),
                             "next": p.get("next"), "created_at": e["created_at"]}
        elif (r := out.get(p.get("raise"))) is not None:
            r.update(state="answered" if e["kind"] == ANSWERED else "closed",
                     answer=p.get("answer"), delivery=p.get("delivery"), closed_why=p.get("reason"),
                     answered_at=e["created_at"])
    return list(out.values())


def status(con, r: dict) -> str | None:
    """None while the raise is open, else why it no longer is. The holder facts are read, not stored, so a
    revoke or takeover closes a raise whatever wrote it."""
    if r["state"] != "open":
        return r["state"]
    d = state.get_dispatch(con, r["dispatch_id"])
    task = state.get_task(con, d["run_id"], r["task_id"]) if d else None
    if task is None or task["current_dispatch_id"] != d["id"]:
        return "superseded"
    if dispatch.live_lease(con, d["run_id"], d["lease_id"]) is None:
        return "revoked"
    if not submit.self_blocked(task) or submit.block_id(con, d["id"]) != r["block_id"]:
        return "resolved"
    return None


def open_raises(con, run: dict, *, dispatch_id: str | None = None) -> list[dict]:
    return [r for r in history(con, run["id"], dispatch_id=dispatch_id) if status(con, r) is None]


def close_stale(con, run: dict) -> int:
    """Record the end of every raise that is no longer open and was never answered (revoked, superseded,
    resolved by an amendment or a resubmit), once each. Caller holds no transaction."""
    stale = [(r, why) for r in history(con, run["id"]) if r["state"] == "open" and (why := status(con, r))]
    if stale:
        with db.transaction(con):
            for r, why in stale:
                state.emit(con, run, CLOSED, f"{r['task_id']} raise closed: {why}", audience="runtime",
                           task_id=r["task_id"], dispatch_id=r["dispatch_id"], payload={"raise": r["seq"], "reason": why})
    return len(stale)


def answer_command(r: dict) -> str:
    return f'office answer {r["task_id"]} -- "<text>"'


def line(r: dict) -> str:
    """One printable line for the orchestrator: who, kind, text, paths and the commands that resolve it."""
    from office import questions
    paths = f"; paths: {', '.join(r['files'])}" if r["files"] else ""
    contract = f"; scope changes only with: {r['next']}" if r["kind"] == "scope-request" and r.get("next") else ""
    return (f"{r['task_id']} {r['dispatch_id']} raised {r['kind']}: \"{_shown(r['text'])}\"{paths}; "
            f"answer: {answer_command(r)}{contract}; {questions.PROTOCOL}")


def report(con, run: dict) -> tuple[list[str], list[str]]:
    """(lines for raises `office wait` has not reported, lines for every open raise). A raise is reported
    once: the cursor moves past it, so a second wait does not exit 5 for it again. Closes stale ones first."""
    close_stale(con, run)
    every = open_raises(con, run)
    row = con.execute("SELECT last_seq FROM cursors WHERE run_id=? AND consumer=?", (run["id"], REPORTED)).fetchone()
    seen = row[0] if row else 0
    fresh = [r for r in every if r["seq"] > seen]
    if fresh:
        with db.transaction(con):
            state.advance_cursor(con, run["id"], REPORTED, max(r["seq"] for r in fresh))
    return [line(r) for r in fresh], [line(r) for r in every]


# ------------------------------------------------------------------ executor: office raise

def raise_issue(con, run: dict, *, cwd: Path, kind: str, text: str, files: list[str]) -> Result:
    """Record a raise for this executor's dispatch and self-block its task. The same holder checks as
    `submit --request-scope`: a revoked, superseded or paused dispatch cannot raise."""
    if kind not in KINDS:
        raise Usage("raise-kind", f"--kind is one of {', '.join(KINDS)}", next_step='office raise --kind question -- "<text>"')
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        raise Usage("raise-text", "say what you are raising and the decision you need",
                    next_step=f'office raise --kind {kind} -- "<context and the decision you need>"')
    d = submit.executor_dispatch(con, run, cwd, "office raise")
    task = state.get_task(con, run["id"], d["task_id"])
    wt = submit.task_worktree(task, d, cwd, f'office raise --kind {kind} -- "<text>"')
    if kind == "scope-request" and not files:
        raise Usage("raise-paths", "a scope-request names the paths it needs: --path <repo-relative path>",
                    next_step='office raise --kind scope-request --path <path> -- "<why>"')
    files = submit.clean_request_paths(wt, files) if files else []
    submit.check_holder(con, run, task, d, "raise")
    key = dedup_key(kind, text)
    extra = {}
    if kind == "scope-request":
        extra = {"diff": submit._scope_hunk(wt, files, d.get("base_commit") or "HEAD"),
                 "next": _amend_command(task["id"], files, text)}
    reason = (f"scope requested: {', '.join(files)}: {_shown(text)}" if kind == "scope-request"
              else f"raised {kind}: {_shown(text)}")
    with db.transaction(con):
        prior = next((r for r in history(con, run["id"], dispatch_id=d["id"]) if r["dedup"] == key and r["kind"] == kind), None)
        if prior is not None:
            return _again(con, prior)
        submit._record_block(con, run, task, d, reason, "raised", {"kind": kind, "files": files}, keep_id=True)
        payload = {"kind": kind, "text": text, "files": files, "dedup": key, "block_id": submit.block_id(con, d["id"]),
                   "session": d.get("session_id"), "harness": d.get("harness"), **extra}
        seq = state.emit(con, run, RAISED, f"{task['id']} raised {kind}: {_shown(text)}", audience="orchestrator",
                         task_id=task["id"], dispatch_id=d["id"], payload=payload)
    return Result(lines=[f"{task['id']} {kind} raised (#{seq}); the orchestrator was woken"],
                  next="stop and wait for the answer; it arrives as a message in this session (or a rerun brief if it "
                       "ended). Do not submit unfinished work to get one. Resubmit when the answer says to")


def _again(con, r: dict) -> Result:
    """A repeat of a raise this dispatch already made: nothing is recorded."""
    why = status(con, r)
    if r["state"] == "answered":
        return Result(lines=[f"{r['task_id']} {r['kind']} already raised (#{r['seq']}) and answered: {_shown(r.get('answer') or '')}"],
                      next="continue the task; office submit when ready")
    if why is None:
        return Result(lines=[f"{r['task_id']} {r['kind']} already raised (#{r['seq']}); still waiting for the orchestrator"],
                      next="stop and wait for the answer; do not raise it again")
    return Result(lines=[f"{r['task_id']} {r['kind']} already raised (#{r['seq']}); it is closed ({why})"],
                  next="continue the task; office submit when ready" if why == "resolved" else "stop; the orchestrator decides")


# ------------------------------------------------------------------ orchestrator: office answer

def _message(r: dict, answer: str) -> str:
    text = f"ANSWER to your raised {r['kind']} ({_shown(r['text'], 120)}): {answer}\n"
    if r["kind"] == "scope-request":
        text += ("Your scope and contract are unchanged by this answer; a file outside your scope is still refused at "
                 "submit. Only a contract amendment widens it.\n")
    return text + "You are unblocked: continue the task and office submit when it is done."


def refuse_worker() -> None:
    """Answering is the orchestrator's authority: a dispatched worker (OFFICE_DISPATCH_ID or OFFICE_ROLE set)
    cannot answer its own raise or any other. Checked before anything is read or written."""
    if os.environ.get("OFFICE_DISPATCH_ID") or os.environ.get("OFFICE_ROLE"):
        raise Refused("worker-cannot-answer", "a worker cannot answer a raise or a question; the orchestrator does",
                      next_step="stop and wait for the orchestrator's answer")


def answer(con, run: dict, d: dict, text: str) -> Result | None:
    """Answer the open raises of dispatch `d`: deliver the text to its worker, mark the raises answered and
    lift the block. None when `d` has no open raise (the caller answers a pane question instead).

    A live pane takes the answer as a prompt; a headless or external worker has it queued for its next
    office command, and the result says so. A worker that already ended gets no delivery: the answer is
    recorded, the task stays blocked for `office rerun`, and the next session's brief carries the answer."""
    refuse_worker()
    opened = open_raises(con, run, dispatch_id=d["id"])
    if not opened:
        return None
    tid = d["task_id"]
    message = _message(opened[-1], text)
    how, note = "pending-rerun", None
    row_live = gates.worker_live(con, d["id"])
    if row_live:
        try:
            if d.get("launcher") == "herdr" and d.get("pane_id"):
                res = prompting.prompt(con, run, d["id"], message)
                how, note = "prompt", " ".join(res.lines)
            else:
                prompting._queue(con, run, d, tid, message)
                how = "queued"
                note = f"queued: {d['id']} runs headless; it sees the answer on its next office command"
        except Refused as exc:
            if exc.category not in ("dispatch-ended", "no-pane"):
                raise
    with db.transaction(con):
        task = state.get_task(con, run["id"], tid)
        still = open_raises(con, run, dispatch_id=d["id"])
        if not still:  # answered or closed by someone else while the answer was being delivered
            return Result(lines=[f"{tid} {d['id']}: the raise is no longer open"], next="office status")
        for r in still:
            state.emit(con, run, ANSWERED, f"{tid} {r['kind']} answered ({how}): {_shown(text, 160)}", audience="runtime",
                       task_id=tid, dispatch_id=d["id"], payload={"raise": r["seq"], "answer": text[:MAX_TEXT], "delivery": how})
        if how == "pending-rerun":
            if submit.self_blocked(task):
                state.update_task(con, run["id"], tid, status="blocked",
                                  pause_reason=f"raise answered but {d['id']} ended: "
                                  + ("office revoke it, then " if row_live else "") + f"office rerun {tid} --resume|--fresh")
        elif submit.unblock_self(con, run, task):
            state.emit(con, run, "task.unblocked", f"{tid} unblocked: raise answered; the worker resubmits",
                       audience="runtime", task_id=tid, dispatch_id=d["id"])
    contract = (f'; this did not change its contract: office amend {tid} --contract -- "<delta>" does'
                if opened[-1]["kind"] == "scope-request" else "")
    if how == "pending-rerun":
        # A dispatch row that still reads live (its agent is gone) must be revoked before a rerun is allowed.
        rerun = (f"office revoke {tid}, then " if row_live else "") + f"office rerun {tid} --resume|--fresh"
        return Result(lines=[f"{tid} {d['id']}: answer recorded; the worker has ended, so nothing was delivered{contract}"],
                      next=f"{rerun} (the new session's brief carries the answer)")
    return Result(lines=[f"{tid} {d['id']}: raise answered; {note}{contract}"], next="office wait")


def answered_for_brief(con, run_id: str, task_id: str, dispatch_id: str) -> list[dict]:
    """Answers recorded for earlier sessions of this task that never reached a worker because it had ended
    (an answered raise, or an answered ended-on-question): the next session's brief carries them, once. An
    answer older than another session's start was already in that session's brief."""
    from office import questions
    from office.util import parse_iso
    out = [{"seq": r["seq"], "at": r["answered_at"], "kind": r["kind"], "text": r["text"], "answer": r.get("answer") or ""}
           for r in history(con, run_id, task_id=task_id)
           if r["state"] == "answered" and r.get("delivery") == "pending-rerun" and r["dispatch_id"] != dispatch_id]
    for e in con.execute("SELECT seq, dispatch_id, payload_json, created_at FROM events WHERE run_id=? AND task_id=? AND kind=? "
                         "ORDER BY seq", (run_id, task_id, questions.ANSWERED)).fetchall():
        p = loads(e["payload_json"], {})
        if p.get("delivery") == "pending-rerun" and e["dispatch_id"] != dispatch_id:
            out.append({"seq": e["seq"], "at": e["created_at"], "kind": "question", "text": p.get("question") or "",
                        "answer": p.get("answer") or ""})
    starts = [parse_iso(r[0]) for r in con.execute("SELECT started_at FROM dispatches WHERE run_id=? AND task_id=? AND id!=? "
                                                   "AND started_at IS NOT NULL", (run_id, task_id, dispatch_id))]
    return sorted((a for a in out if not any(t > parse_iso(a["at"]) for t in starts)), key=lambda a: a["seq"])[-3:]
