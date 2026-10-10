"""Recoverable interactive startup prompts in a Herdr pane (#510).

A harness can open on an interactive screen before Herdr sees it ready: an
update notice, a trust or login dialog, a what's-new splash. `herdr agent
start` then reports `agent_not_ready` while the agent sits `blocked` in its
pane. Rather than closing that pane and falling back to headless at once, the
launch job that owns the pane records a run-scoped startup blocker here, keeps
the pane, and waits a bounded time for `office answer <dispatch> --choice N`
or `--keys esc`. Once the harness is ready the launch continues and delivers
the brief exactly once; an unanswered or vanished prompt falls back headless
with the reason recorded.

Office never answers a startup prompt on its own. Trust, login, credential,
permission and update choices are the user's decision; the folder-trust
exception for Office-owned directories (#399) runs before this and is unchanged.

States: waiting -> answering -> answered -> resolved, or back to waiting when
the answer revealed another screen or was not taken. Terminal: resolved,
expired, pane_closed, cancelled, abandoned (the launcher died).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from office import db, state
from office.result import Result
from office.state import Refused, Usage
from office.util import DEAD, atomic_write_text, claim_identity, claim_liveness, dumps, now_iso, parse_iso

OPEN = ("waiting", "answering", "answered")
UNRECOGNIZED = "unrecognized startup screen"
MAX_ROUNDS = 5
MAX_KEYS = 6
# Keys an answer may press: menu navigation, confirm and dismiss, and option digits.
_KEYS = {"esc": "esc", "escape": "esc", "enter": "Enter", "up": "up", "down": "down", "left": "left",
         "right": "right", "tab": "tab", "space": "space", **{str(n): str(n) for n in range(1, 10)}}
_OPTION = re.compile(r"^\s*(?:[›❯>▸*]\s*)?(\d{1,2})[.)]\s+(\S.*)$")
_CURSOR = re.compile(r"^[›❯>▸*]\s*")
USER_DECISION = "trust, login, credential, permission and update choices are the user's decision"


def _wait_s() -> float:
    """How long a launch holds a pane on an unanswered startup prompt."""
    try:
        return max(0.0, float(os.environ.get("OFFICE_STARTUP_PROMPT_WAIT", "900")))
    except ValueError:
        return 900.0


def _poll_s() -> float:
    try:
        return max(0.01, float(os.environ.get("OFFICE_STARTUP_PROMPT_POLL", "2")))
    except ValueError:
        return 2.0


def _settle_s() -> float:
    try:
        return max(0.0, float(os.environ.get("OFFICE_STARTUP_PROMPT_SETTLE", "30")))
    except ValueError:
        return 30.0


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ------------------------------------------------------------------ screen

def screen_text(pane: str) -> str:
    """The pane's visible screen, ANSI stripped, last 40 non-empty lines."""
    from office import dispatch
    lines = [ln.rstrip() for ln in dispatch._strip_ansi(dispatch._pane_visible(pane)).splitlines()]
    return "\n".join([ln for ln in lines if ln.strip()][-40:])


def fingerprint(text: str) -> str:
    """Identity of a prompt screen. Moving the selection cursor is the same prompt."""
    norm = "\n".join(_CURSOR.sub("", ln.strip()) for ln in text.splitlines() if ln.strip())
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def options(text: str) -> list[dict]:
    out, seen = [], set()
    for ln in text.splitlines():
        m = _OPTION.match(ln)
        if m and int(m.group(1)) not in seen:
            seen.add(int(m.group(1)))
            out.append({"n": int(m.group(1)), "label": m.group(2).strip()[:120]})
    return out


def label(text: str) -> str:
    from office import dispatch
    return dispatch._startup_screen(text) or UNRECOGNIZED


def _agent_status(name: str) -> str | None:
    from office import dispatch
    res = dispatch._herdr_json(["agent", "get", name])
    agent = res.get("agent") or res
    return (agent.get("agent_status") or agent.get("status")) if isinstance(agent, dict) else None


def _ready(name: str, text: str, fp: str) -> bool:
    """Past the prompt: Herdr reports the agent usable and the prompt screen is gone."""
    return (_agent_status(name) in ("idle", "working", "done") and fingerprint(text) != fp
            and label(text) == UNRECOGNIZED)


# ------------------------------------------------------------------ records

def _get(con, prompt_id: str) -> dict | None:
    row = con.execute("SELECT * FROM startup_prompts WHERE id=?", (prompt_id,)).fetchone()
    return dict(row) if row else None


def open_prompt(con, dispatch_id: str) -> dict | None:
    row = con.execute("SELECT * FROM startup_prompts WHERE dispatch_id=? AND state IN ('waiting','answering','answered') "
                      "ORDER BY created_at DESC LIMIT 1", (dispatch_id,)).fetchone()
    return dict(row) if row else None


def _snapshot(ddir: Path, text: str, rnd: int) -> str:
    path = ddir / f"startup-prompt-{rnd}.txt"
    atomic_write_text(path, text + "\n", mode=0o600)  # an unredacted screen: private (#500)
    return str(path)


def _update(prompt_id: str, **fields) -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            cols = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE startup_prompts SET {cols}, updated_at=? WHERE id=?",
                        (*fields.values(), now_iso(), prompt_id))
    finally:
        con.close()


def _close(run: dict, dispatch: dict, prompt_id: str, final: str, why: str) -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute("UPDATE startup_prompts SET state=?, resolution=?, updated_at=? WHERE id=? "
                        "AND state IN ('waiting','answering','answered')", (final, why, now_iso(), prompt_id))
            state.emit(con, run, f"startup.{final}", f"{dispatch.get('task_id') or dispatch['id']}: startup prompt "
                       f"{prompt_id} {final}: {why}", audience="runtime", task_id=dispatch.get("task_id"),
                       dispatch_id=dispatch["id"])
    finally:
        con.close()


def _ended(dispatch_id: str) -> bool:
    con = db.connect()
    try:
        d = state.get_dispatch(con, dispatch_id)
    finally:
        con.close()
    return d is None or bool(d.get("ended_at")) or d.get("status") not in ("launching", "running", None)


def answer_command(dispatch_id: str, opts: list[dict], fp: str) -> str:
    pick = f"--choice <1-{len(opts)}>" if opts else "--keys <esc|enter|up|down|1-9>"
    return f"office answer {dispatch_id} {pick} --expect {fp} (or --keys esc --expect {fp})"


# ------------------------------------------------------------------ the launcher side

def hold(run: dict, dispatch: dict, spec: dict, pane: str, name: str, harness: str, ddir: Path) -> str:
    """Hold a pane whose harness is blocked on a startup prompt until it is answered and
    the harness is ready. Returns 'ready' (deliver the brief now), 'fallback' (run headless;
    spec['startup_prompt'] says why), 'cancelled' (the dispatch ended meanwhile) or
    'not-a-prompt' (nothing recognisable holds the pane: the old failure path applies)."""
    text = screen_text(pane)
    status = _agent_status(name)
    if not text or (status != "blocked" and label(text) == UNRECOGNIZED):
        return "not-a-prompt"
    fp, opts, wait = fingerprint(text), options(text), _wait_s()
    now = now_iso()
    prompt_id = "SP-" + secrets.token_hex(4)
    expires = (parse_iso(now) + timedelta(seconds=wait)).isoformat(timespec="seconds")
    record = {"id": prompt_id, "screen": label(text), "fingerprint": fp, "pane": pane, "wait_seconds": wait}
    spec["startup_prompt"] = record
    con = db.connect()
    try:
        with db.transaction(con):
            con.execute("INSERT INTO startup_prompts(id, run_id, dispatch_id, pane_id, agent, harness, screen, fingerprint, "
                        "options_json, snapshot_path, state, round, launcher_pid, launcher_identity, created_at, updated_at, "
                        "expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)",
                        (prompt_id, run["id"], dispatch["id"], pane, name, harness, record["screen"], fp, dumps(opts),
                         _snapshot(ddir, text, 1), "waiting" if wait > 0 else "expired", os.getpid(),
                         claim_identity(os.getpid()), now, now, expires))
            if wait > 0:
                state.emit(con, run, "startup.blocked",
                           f"{dispatch.get('task_id') or dispatch['id']}: {record['screen']} holds pane {pane} at startup; "
                           f"answer: {answer_command(dispatch['id'], opts, fp)}; {USER_DECISION}",
                           task_id=dispatch.get("task_id"), dispatch_id=dispatch["id"],
                           payload={"prompt_id": prompt_id, "fingerprint": fp, "options": opts})
    finally:
        con.close()
    if wait <= 0:
        record["resolution"] = "no startup-prompt wait configured (OFFICE_STARTUP_PROMPT_WAIT=0)"
        return "fallback"
    deadline = time.time() + wait
    from office import dispatch as dispatch_mod
    while True:
        _sleep(_poll_s())
        con = db.connect()
        try:
            row = _get(con, prompt_id)
        finally:
            con.close()
        if row is None or row["state"] not in OPEN:
            record["resolution"] = (row or {}).get("resolution") or "startup prompt closed"
            return "cancelled" if row and row["state"] == "cancelled" else "fallback"
        if _ended(dispatch["id"]):
            _close(run, dispatch, prompt_id, "cancelled", "the dispatch ended while its startup prompt was open")
            record["resolution"] = "cancelled"
            return "cancelled"
        if not dispatch_mod._pane_exists(pane):
            _close(run, dispatch, prompt_id, "pane_closed", f"pane {pane} closed before the harness was ready")
            record["resolution"] = f"pane {pane} closed before the harness was ready"
            return "fallback"
        if row["state"] == "answered":
            outcome = _settle(run, dispatch, row, name, pane, ddir)
            if outcome == "ready":
                record["resolution"] = f"answered ({row['answer']}); harness ready"
                return "ready"
        elif row["state"] == "waiting":
            text = screen_text(pane)
            if text and _ready(name, text, row["fingerprint"]):
                # Someone answered it in the pane directly.
                _close(run, dispatch, prompt_id, "resolved", "answered in the pane; harness ready")
                record["resolution"] = "answered in the pane; harness ready"
                return "ready"
            if text and fingerprint(text) != row["fingerprint"]:
                _rerecord(run, dispatch, row, text, ddir, why="the startup screen changed")
        if time.time() >= deadline:
            why = f"unanswered within {wait:g}s"
            _close(run, dispatch, prompt_id, "expired", why)
            record["resolution"] = why
            return "fallback"


def _settle(run: dict, dispatch: dict, row: dict, name: str, pane: str, ddir: Path) -> str:
    """After an answer: 'ready', or the row is back to waiting with what the pane now shows."""
    deadline = time.time() + _settle_s()
    while True:
        text = screen_text(pane)
        if text and _ready(name, text, row["fingerprint"]):
            _close(run, dispatch, row["id"], "resolved", f"answered ({row['answer']}); harness ready")
            return "ready"
        if time.time() >= deadline:
            break
        _sleep(min(_poll_s(), 1.0))
    moved = bool(text) and fingerprint(text) != row["fingerprint"]
    if moved and (label(text) != UNRECOGNIZED or _agent_status(name) == "blocked"):
        _rerecord(run, dispatch, row, text, ddir, why=f"the answer ({row['answer']}) opened another startup screen")
    else:
        why = "the harness is not ready yet" if moved else "the answer was not taken"
        _update(row["id"], state="waiting", reported=0)
        _notify(run, dispatch, row["id"], f"{why} after {row['answer']}; the prompt stays open", audience="orchestrator")
    return "waiting"


def _rerecord(run: dict, dispatch: dict, row: dict, text: str, ddir: Path, *, why: str) -> None:
    rnd = row["round"] + 1
    if rnd > MAX_ROUNDS:
        _close(run, dispatch, row["id"], "expired", f"more than {MAX_ROUNDS} startup screens")
        return
    fp, opts = fingerprint(text), options(text)
    _update(row["id"], state="waiting", fingerprint=fp, screen=label(text), options_json=dumps(opts),
            snapshot_path=_snapshot(ddir, text, rnd), round=rnd, answer=None, reported=0)
    _notify(run, dispatch, row["id"], f"{why}: {label(text)}; answer: {answer_command(dispatch['id'], opts, fp)}; "
                                      f"{USER_DECISION}", audience="orchestrator")


def _notify(run: dict, dispatch: dict, prompt_id: str, text: str, audience: str = "runtime") -> None:
    con = db.connect()
    try:
        with db.transaction(con):
            state.emit(con, run, "startup.blocked", f"{dispatch.get('task_id') or dispatch['id']}: startup prompt "
                       f"{prompt_id}: {text}", audience=audience, task_id=dispatch.get("task_id"),
                       dispatch_id=dispatch["id"])
    finally:
        con.close()


# ------------------------------------------------------------------ the orchestrator side

def _abandon_dead(con, run: dict) -> None:
    """An open prompt whose launcher died has nobody to deliver the brief: close it."""
    for r in con.execute("SELECT * FROM startup_prompts WHERE run_id=? AND state IN ('waiting','answering','answered')",
                         (run["id"],)).fetchall():
        if claim_liveness(r["launcher_pid"], r["launcher_identity"])[0] == DEAD:
            with db.transaction(con):
                con.execute("UPDATE startup_prompts SET state='abandoned', resolution=?, updated_at=? WHERE id=? "
                            "AND state IN ('waiting','answering','answered')",
                            (f"the launch process {r['launcher_pid']} ended; pane {r['pane_id']} is left for inspection",
                             now_iso(), r["id"]))


def line(r: dict) -> str:
    opts = json.loads(r["options_json"] or "[]")
    shown = " | ".join(f"{o['n']}) {o['label']}" for o in opts)
    return (f"{r['dispatch_id']} pane {r['pane_id']} [startup {r['harness'] or 'harness'}] \"{r['screen']}\""
            + (f"; options: {shown}" if shown else "") + f"; snapshot {r['snapshot_path']}"
            + f"; answer: {answer_command(r['dispatch_id'], opts, r['fingerprint'])}; {USER_DECISION}")


def report(con, run: dict) -> tuple[list[str], list[str]]:
    """(lines `office wait` has not reported, lines for every prompt waiting on an answer)."""
    _abandon_dead(con, run)
    rows = [dict(r) for r in con.execute("SELECT * FROM startup_prompts WHERE run_id=? AND state='waiting' "
                                         "ORDER BY created_at", (run["id"],)).fetchall()]
    fresh = [r for r in rows if not r["reported"]]
    if fresh:
        with db.transaction(con):
            con.executemany("UPDATE startup_prompts SET reported=1 WHERE id=?", [(r["id"],) for r in fresh])
    return [line(r) for r in fresh], [line(r) for r in rows]


def recorded(con, run: dict) -> list[str]:
    return [line(dict(r)) for r in con.execute("SELECT * FROM startup_prompts WHERE run_id=? AND state='waiting' "
                                                "ORDER BY created_at", (run["id"],)).fetchall()]


def _key_sequence(row: dict, keys: str | None, choice: int | None) -> list[str]:
    if (keys is None) == (choice is None):
        raise Usage("startup-answer-usage", "answer a startup prompt with exactly one of --choice or --keys",
                    next_step=answer_command(row["dispatch_id"], json.loads(row["options_json"] or "[]"), row["fingerprint"]))
    if choice is not None:
        opts = json.loads(row["options_json"] or "[]")
        valid = [o["n"] for o in opts] or list(range(1, 10))
        if choice not in valid:
            raise Refused("no-option", f"option {choice} is not on the startup screen ({', '.join(map(str, valid))})",
                          scope=row["dispatch_id"])
        return [str(choice)]
    seq = [k for k in re.split(r"[\s,]+", keys.strip().lower()) if k]
    bad = [k for k in seq if k not in _KEYS]
    if not seq or bad or len(seq) > MAX_KEYS:
        raise Usage("startup-keys", f"--keys takes up to {MAX_KEYS} of: esc, enter, up, down, left, right, tab, space, 1-9"
                    + (f" (not {', '.join(bad)})" if bad else ""))
    return [_KEYS[k] for k in seq]


def answer(con, run: dict, d: dict, *, keys: str | None, choice: int | None, expect: str | None) -> Result | None:
    """Answer the open startup prompt on dispatch `d`, or None when it has none. The prompt
    must still be the one recorded (and `--expect`ed), its launcher alive and its pane Office's."""
    _abandon_dead(con, run)
    row = open_prompt(con, d["id"])
    if row is None:
        return None
    who = d.get("task_id") or d["id"]
    if row["state"] != "waiting":
        raise Refused("startup-answer-pending", f"an answer to startup prompt {row['id']} is already being applied",
                      scope=who, next_step="office wait")
    seq = _key_sequence(row, keys, choice)
    if parse_iso(row["expires_at"]) <= parse_iso(now_iso()):
        raise Refused("startup-prompt-expired", f"startup prompt {row['id']} expired; the launch is falling back",
                      scope=who, next_step="office status")
    if expect and expect != row["fingerprint"]:
        raise Refused("stale-answer", f"--expect {expect} does not match startup prompt {row['id']} "
                      f"(now {row['fingerprint']}: {row['screen']})", scope=who, next_step="office status")
    held = con.execute("SELECT 1 FROM dispatches WHERE pane_id=? AND id<>? AND ended_at IS NULL "
                       "AND status IN ('launching','running')", (row["pane_id"], d["id"])).fetchone()
    if held:
        raise Refused("pane-not-owned", f"pane {row['pane_id']} is held by another dispatch", scope=who)
    text = screen_text(row["pane_id"])
    if not text or fingerprint(text) != row["fingerprint"]:
        raise Refused("startup-prompt-changed", f"pane {row['pane_id']} no longer shows the recorded startup prompt "
                      f"{row['fingerprint']}; Office records the new screen", scope=who,
                      next_step=f"herdr pane read {row['pane_id']}; office status")
    with db.transaction(con):
        took = con.execute("UPDATE startup_prompts SET state='answering', answer=?, updated_at=? WHERE id=? "
                           "AND state='waiting' AND fingerprint=?",
                           (" ".join(seq), now_iso(), row["id"], row["fingerprint"])).rowcount
    if took != 1:
        raise Refused("startup-answer-race", f"startup prompt {row['id']} changed while answering", scope=who,
                      next_step="office status")
    sent = True
    for key in seq:
        try:
            proc = subprocess.run(["herdr", "pane", "send-keys", row["pane_id"], key], capture_output=True, text=True,
                                  timeout=30)
            sent = sent and proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            sent = False
        if not sent:
            break
    with db.transaction(con):
        con.execute("UPDATE startup_prompts SET state=?, updated_at=? WHERE id=? AND state='answering'",
                    ("answered" if sent else "waiting", now_iso(), row["id"]))
        state.emit(con, run, "startup.answered" if sent else "startup.answer_failed",
                   f"{who} {d['id']}: startup prompt {row['id']} ({row['screen']}) "
                   + (f"answered with {' '.join(seq)}" if sent else "answer could not be sent"),
                   audience="runtime", task_id=d.get("task_id"), dispatch_id=d["id"],
                   payload={"prompt_id": row["id"], "fingerprint": row["fingerprint"], "keys": seq})
    if not sent:
        raise Refused("startup-send-failed", f"herdr could not send keys to pane {row['pane_id']}", scope=who,
                      next_step=f"herdr pane read {row['pane_id']}")
    return Result(lines=[f"{who} {d['id']}: pressed {' '.join(seq)} on {row['screen']} in pane {row['pane_id']}; "
                         "Office delivers the brief once the harness is ready"], next="office wait")


def inspect(con, run: dict, ident: str | None) -> Result:
    q = "SELECT * FROM startup_prompts WHERE run_id=?" + (" AND (dispatch_id=? OR id=?)" if ident else "") + " ORDER BY created_at"
    rows = [dict(r) for r in con.execute(q, (run["id"], ident, ident) if ident else (run["id"],)).fetchall()]
    lines = [f"{r['id']} {r['dispatch_id']} pane {r['pane_id']} {r['harness'] or '-'} \"{r['screen']}\" {r['state']} "
             f"round {r['round']} fp {r['fingerprint']}" + (f" answer {r['answer']}" if r["answer"] else "")
             + (f" | {r['resolution']}" if r["resolution"] else "") + f" | snapshot {r['snapshot_path']}" for r in rows]
    return Result(lines=lines or ["no startup prompts"], data={"startup_prompts": rows})
