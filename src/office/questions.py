"""An agent stopped on a question: detect it in the pane, surface it to `office wait`, answer it.

Run ff040876 T4 (2026-10-05): a Claude executor opened an AskUserQuestion widget and sat
blocked. The widget footer ("Enter to select · ... · Esc to cancel") matches the busy
markers, so the pane never read as idle and neither `office wait` nor `office status`
reported anything. The user had to tell the orchestrator.

A question is one of:
- `select`: a selection widget (Claude AskUserQuestion, a codex or agy approval list),
  answered with a keypress (the option number);
- `dialog`: herdr reports the agent `blocked` (it recognized an approval or question UI)
  but the pane text did not parse as a list;
- `text`: the agent ended its turn on a plain-text question (its last message ends in `?`).

Protocol: the orchestrator answers on its own judgement (planning, scope, ordering, test
detail) and takes a question to the user only when it hints at a user decision
(requirements, authority, an irreversible or external action).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from office import db, dispatch, paths, state
from office.result import Result
from office.state import Refused, Usage
from office.util import atomic_write_text, now_iso, parse_iso

EVENT = "agent.question"
CLEARED = "agent.question.cleared"
ANSWERED = "agent.answered"
EXIT = 5  # `office wait`: an agent asked something; answer it (see PROTOCOL)
TAIL_LINES = 60
PROTOCOL = ("answer it yourself (planning, scope, ordering, test detail); ask the user only if it is a "
            "user decision (requirements, authority, irreversible or external action)")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-_]")
# Footer lines a selection widget shows while it waits for a choice. Claude:
# "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"; codex: "Press enter
# to confirm or esc to cancel"; agy: "Waiting for user confirmation".
_SELECT_FOOTER = re.compile(r"enter to (?:select|confirm)|arrow keys to navigate|↑/↓ to navigate"
                            r"|waiting for user confirmation", re.I)
_OPTION = re.compile(r"^\s*(?:[❯›>●○◯▶▸]\s*)?(\d{1,2})[.)]\s+(\S.*?)\s*$")
_RULE = re.compile(r"^\s*[─━═—–-]{8,}\s*$")
_BOX = "│┃|"
# A message bullet: Claude ⏺/●, codex •. A tool call bullet ("⏺ Bash(...)") ends in its
# output, never in a question, so it does not need excluding.
_BULLET = re.compile(r"^\s*[⏺●•]\s+\S")
_COMPOSER = re.compile(r"^\s*[❯›>]\s?")
MAX_QUESTION = 300
MAX_OPTION = 80
MAX_OPTIONS = 8


def _clean(text: str) -> list[str]:
    return [ln.rstrip() for ln in _ANSI.sub("", text or "").splitlines()]


def _line(s: str, limit: int) -> str:
    s = " ".join("".join(c if c.isprintable() else " " for c in s).split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _unbox(ln: str) -> str:
    s = ln.strip()
    while s[:1] in _BOX and s:
        s = s[1:].strip()
    return s


def _select(lines: list[str]) -> dict | None:
    footer = max((i for i, ln in enumerate(lines) if _SELECT_FOOTER.search(ln)), default=None)
    if footer is None:
        return None
    # The widget is the latest state only when nothing but blank lines and rules follow it.
    if any(ln.strip() and not _RULE.match(ln) for ln in lines[footer + 1:]):
        return None
    above = lines[max(footer - 40, 0):footer]
    first = max((i for i, ln in enumerate(above) if (m := _OPTION.match(ln)) and m.group(1) == "1"), default=None)
    if first is None:
        return None
    options, want = [], 1
    for ln in above[first:]:
        m = _OPTION.match(ln)
        if m and int(m.group(1)) == want:
            options.append({"n": want, "label": _line(m.group(2), MAX_OPTION)})
            want += 1
    body, gaps = [], 0
    for ln in reversed(above[:first]):
        if _RULE.match(ln) or _BULLET.match(ln):
            break
        s = _unbox(ln)
        if not s:
            gaps += 1 if body else 0
            if gaps > 2:
                break
            continue
        if "☐" in s or "☒" in s or "✔" in s:  # the multi-question tab strip
            break
        body.insert(0, s)
        if len(body) >= 8:
            break
    return {"kind": "select", "question": _line(" ".join(body), MAX_QUESTION) or "(question text not shown)",
            "options": options}


def _plain(lines: list[str]) -> dict | None:
    """The agent's last message, when the turn ended on a question."""
    start = max((i for i, ln in enumerate(lines) if _BULLET.match(ln)), default=None)
    if start is None:
        return None
    block = []
    for ln in lines[start:]:
        if _RULE.match(ln) or (block and _COMPOSER.match(ln)) or ln.strip()[:1] in "╭╰":
            break
        block.append(ln)
    text = [s for s in (re.sub(r"^\s*[⏺●•]\s+", "", ln).strip() for ln in block) if s]
    if not text or not text[-1].endswith("?"):
        return None
    options = []
    for ln in text:
        m = _OPTION.match(ln)
        if m and int(m.group(1)) == len(options) + 1:
            options.append({"n": len(options) + 1, "label": _line(m.group(2), MAX_OPTION)})
    # A long message: the question is its last lines.
    q = " ".join(t for t in text if not _OPTION.match(t))
    return {"kind": "text", "question": _line(q[-MAX_QUESTION:], MAX_QUESTION), "options": options[:MAX_OPTIONS]}


def parse(text: str | None, *, status: str | None = None, busy: bool | None = None) -> dict | None:
    """The question an agent's pane is waiting on, or None. `status` is herdr's agent status
    (`blocked` means herdr itself recognized an approval or question UI); `busy` is whether
    the turn is still running. A plain-text question counts only once the turn has ended."""
    lines = _clean(text or "")[-TAIL_LINES:]
    q = _select(lines)
    if q is None and status == "blocked":
        q = {"kind": "dialog", "question": "an approval or question dialog Office could not parse", "options": []}
    if q is None and busy is False and status in (None, "idle", "done"):
        q = _plain(lines)
    if q is not None:
        q["options"] = q["options"][:MAX_OPTIONS]
        q["fingerprint"] = hashlib.sha256(json.dumps([q["kind"], q["question"], q["options"]]).encode()).hexdigest()
    return q


# A worker's closing status line names its next step: `... SUBMIT=not attempted NEXT=Answer Q1 ...`.
_NEXT = re.compile(r"\bNEXT=(.+)$")
_ASKS = re.compile(r"^\W*(?:answer|approve|authori[sz]e|confirm|decide|choose|tell me)\b|\bQ\d+\b", re.I)
_QUESTION_LINE = re.compile(r"\bQ\d+\b|\bquestion\b", re.I)
ENDED_PREFIX = "worker ended on a question: "


def final_question(text: str | None) -> dict | None:
    """The question a headless worker ended its run on, from its final message:
    a closing `NEXT=` that asks the orchestrator to answer, approve or decide,
    or a last line that is a question. None for any other ending."""
    lines = [ln.strip() for ln in _clean(text or "") if ln.strip()]
    if not lines:
        return None
    nxt = next((m.group(1).strip() for ln in reversed(lines[-6:]) if (m := _NEXT.search(ln))), None)
    if not ((nxt and _ASKS.search(nxt)) or lines[-1].endswith("?")):
        return None
    asked = next((ln for ln in lines if "?" in ln and _QUESTION_LINE.search(ln)), None) \
        or next((ln for ln in reversed(lines) if ln.endswith("?")), None) or nxt or lines[-1]
    q = {"kind": "text", "question": _line(re.sub(r"[*_`]+", "", asked), MAX_QUESTION), "options": [], "ended": True}
    q["fingerprint"] = hashlib.sha256(json.dumps([q["kind"], q["question"], []]).encode()).hexdigest()
    return q


def _ended_question_dispatches(con, run: dict) -> list[tuple[dict, dict]]:
    """(dispatch, question) for tasks blocked because their worker ended on a question."""
    out = []
    for t in con.execute("SELECT id, current_dispatch_id FROM tasks WHERE run_id=? AND status='blocked' "
                         "AND pause_reason LIKE ?", (run["id"], ENDED_PREFIX + "%")).fetchall():
        d = state.get_dispatch(con, t["current_dispatch_id"]) if t["current_dispatch_id"] else None
        f = paths.run_dir(run["id"]) / "dispatches" / (d or {}).get("id", "-") / "question.json"
        if d and f.is_file():
            try:
                out.append((d, json.loads(f.read_text())))
            except ValueError:
                continue
    return out


def _repeat_s() -> float:
    try:
        return float(os.environ.get("OFFICE_QUESTION_REPEAT_S", state.SIGNAL_REPEAT_S))
    except ValueError:
        return float(state.SIGNAL_REPEAT_S)


def _live_pane_dispatches(con, run: dict) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM dispatches WHERE run_id=? AND launcher='herdr' AND pane_id IS NOT NULL "
        "AND status IN ('launching','running') AND ended_at IS NULL ORDER BY started_at", (run["id"],)).fetchall()]


def _who(d: dict) -> str:
    return " ".join(x for x in (d.get("task_id"), d.get("role"), d["id"]) if x)


def answer_command(d: dict, q: dict) -> str:
    target = d["id"]
    if q.get("ended"):
        # The session has ended: the answer travels as an amendment into the next session.
        t = d.get("task_id") or target
        return (f'office amend {t} --contract -- "<delta>" if the answer changes scope or acceptance, else '
                f'office amend {t} --no-review --reason "answer" -- "<answer>"; then office rerun {t} --resume|--fresh')
    if q["kind"] == "select" and q["options"]:
        return f'office answer {target} <1-{len(q["options"])}> (or office answer {target} -- "<text>")'
    return f'office answer {target} -- "<text>"'


def line(d: dict, q: dict) -> str:
    """One printable `question:` line: who, pane, kind, the question, its options, the answer command."""
    opts = " | ".join(f"{o['n']}) {o['label']}" for o in q["options"])
    return (f"{_who(d)} pane {d.get('pane_id') or '-'} [{q['kind']}] \"{q['question']}\""
            + (f"; options: {opts}" if opts else "")
            + f"; answer: {answer_command(d, q)}; {PROTOCOL}")


def _last(con, run_id: str, dispatch_id: str) -> dict | None:
    row = con.execute("SELECT * FROM events WHERE run_id=? AND dispatch_id=? AND kind IN (?,?,?) "
                      "ORDER BY seq DESC LIMIT 1", (run_id, dispatch_id, EVENT, CLEARED, ANSWERED)).fetchone()
    return dict(row) if row else None


def _record(con, run: dict, d: dict, q: dict, act: dict) -> bool:
    """Record a question sighting; True when it is news (new, or unanswered past the repeat window).
    The full pane tail is kept in the dispatch's question.txt."""
    last = _last(con, run["id"], d["id"])
    if last and last["kind"] == EVENT:
        p = json.loads(last["payload_json"] or "{}")
        age = (parse_iso(now_iso()) - parse_iso(last["created_at"])).total_seconds()
        if p.get("fingerprint") == q["fingerprint"] and age < _repeat_s():
            return False
    if act.get("text") is not None:
        tail = paths.run_dir(run["id"]) / "dispatches" / d["id"] / "question.txt"
        atomic_write_text(tail, "\n".join(_clean(act.get("text") or "")[-TAIL_LINES:]) + "\n")
    with db.transaction(con):
        state.emit(con, run, EVENT, f"{_who(d)} asked: {q['question'][:160]}", audience="runtime",
                   task_id=d.get("task_id"), dispatch_id=d["id"], payload=q)
    return True


def scan(con, run: dict) -> tuple[list[str], list[str], dict]:
    """Read every live pane agent once. Returns (new question lines, all question lines,
    {dispatch id: activity}) so stall detection reuses the reads. A question that went away
    without `office answer` (answered in the pane by hand) is recorded as cleared."""
    from office import rerun
    new, every, acts = [], [], {}
    for d in _live_pane_dispatches(con, run):
        act = rerun.agent_activity(d)
        if act is None:
            continue
        acts[d["id"]] = act
        q = parse(act.get("text"), status=act.get("status"), busy=act.get("busy")) if act.get("alive") else None
        if q is None:
            last = _last(con, run["id"], d["id"])
            if last and last["kind"] == EVENT:
                with db.transaction(con):
                    state.emit(con, run, CLEARED, f"{_who(d)} question no longer showing", audience="runtime",
                               task_id=d.get("task_id"), dispatch_id=d["id"])
            continue
        act["question"] = q
        text = line(d, q)
        every.append(text)
        if _record(con, run, d, q, act):
            new.append(text)
    for d, q in _ended_question_dispatches(con, run):
        text = line(d, q)
        every.append(text)
        if _record(con, run, d, q, {"text": None}):
            new.append(text)
    return new, every, acts


def recorded(con, run: dict) -> list[str]:
    """`question:` lines for questions `office wait` saw and nothing has answered or cleared,
    on dispatches still live. Reads no pane, so `office status` stays cheap."""
    out = []
    for d in _live_pane_dispatches(con, run):
        last = _last(con, run["id"], d["id"])
        if last and last["kind"] == EVENT:
            out.append(line(d, json.loads(last["payload_json"] or "{}")))
    out += [line(d, q) for d, q in _ended_question_dispatches(con, run)]
    return out


def _current(d: dict) -> dict | None:
    from office import rerun
    act = rerun.agent_activity(d)
    if act is None or not act.get("alive"):
        return None
    return parse(act.get("text"), status=act.get("status"), busy=act.get("busy"))


def _settle(d: dict, fingerprint: str, timeout: float) -> dict | None:
    """Poll the pane until the question with `fingerprint` is gone; returns what shows then."""
    deadline = time.time() + timeout
    while True:
        q = _current(d)
        if q is None or q["fingerprint"] != fingerprint or time.time() >= deadline:
            return q
        time.sleep(0.5)


def _answer_timeout() -> float:
    try:
        return float(os.environ.get("OFFICE_ANSWER_TIMEOUT", "10"))
    except ValueError:
        return 10.0


def answer(con, run: dict, target: str | None, text: str) -> Result:
    """Answer the question a live pane agent is waiting on. A number answers a selection widget
    with that keypress (`office prompt` types text, which a widget ignores or misreads). Any other
    answer to a widget dismisses it with Esc, then is sent as a prompt. A plain-text question is
    answered with `office prompt`."""
    from office import prompting
    if not target or not text.strip():
        raise Usage("answer-usage", "name the task or dispatch and the answer",
                    next_step='office answer <task|dispatch> <option> | office answer <task|dispatch> -- "<text>"')
    d = prompting._resolve(con, run, target)
    who = d.get("task_id") or d["id"]
    if d.get("launcher") != "herdr" or not d.get("pane_id"):
        raise Refused("no-pane", f"{d['id']} ({who}) has no Herdr pane to answer in", scope=who)
    q = _current(d)
    if q is None:
        raise Refused("no-question", f"{d['id']} ({who}) is not showing a question now", scope=who,
                      next_step=f'herdr pane read {d["pane_id"]}; to message it: office prompt {d["id"]} -- "<message>"')
    pane, text = d["pane_id"], text.strip()
    choice = int(text) if text.isdigit() else None
    if q["kind"] in ("select", "dialog") and choice is not None:
        if q["options"] and not 1 <= choice <= len(q["options"]):
            raise Refused("no-option", f"option {choice} is not one of 1-{len(q['options'])}", scope=who,
                          next_step=answer_command(d, q))
        dispatch._herdr_quiet("pane", "send-keys", pane, str(choice))
        after = _settle(d, q["fingerprint"], _answer_timeout())
        took = after is None or after["fingerprint"] != q["fingerprint"]
        label = next((o["label"] for o in q["options"] if o["n"] == choice), "")
        how = f"pressed {choice}" + (f" ({label})" if label else "")
    else:
        if q["kind"] in ("select", "dialog"):
            dispatch._herdr_quiet("pane", "send-keys", pane, "esc")
            after = _settle(d, q["fingerprint"], _answer_timeout())
            if after is not None and after["fingerprint"] == q["fingerprint"]:
                raise Refused("answer-not-taken", f"the dialog in pane {pane} is still showing after Esc", scope=who,
                              next_step=f"herdr pane read {pane}; answer with an option number instead")
        res = prompting.prompt(con, run, d["id"], text)
        took, how = True, "sent as a prompt" + (" after Esc dismissed the dialog" if q["kind"] != "text" else "")
        res_lines = res.lines
    with db.transaction(con):
        state.emit(con, run, ANSWERED, f"{who} {d['id']}: question answered ({how})", audience="runtime",
                   task_id=d.get("task_id"), dispatch_id=d["id"],
                   payload={"fingerprint": q["fingerprint"], "answer": text[:500], "taken": took})
    if not took:
        return Result(lines=[f"{who} {d['id']}: {how}; the same question is still showing in pane {pane}"],
                      next=f"herdr pane read {pane}, then office status")
    if choice is None:
        return Result(lines=[f"{who} {d['id']}: {how}", *res_lines], next="office wait")
    return Result(lines=[f"{who} {d['id']}: {how}; the question is no longer showing"], next="office wait")
