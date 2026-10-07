"""office prompt: send a live Herdr agent a message and confirm it was submitted.

Use this, never `herdr pane run` or `pane send-text`, to reach a worker or
reviewer by hand. `pane run` writes the text and its Enter in one chunk, and
Claude's composer takes that Enter as part of the paste: the text stays typed,
unsubmitted. `herdr agent prompt` (which Office sends) pauses before the Enter,
and Office checks the prompt landed and presses Enter for one left in the
composer, never sending the text twice.
"""
from __future__ import annotations

from office import db, dispatch, gates, state
from office.result import Result
from office.state import Refused, Usage


def _resolve(con, run: dict, target: str) -> dict:
    if target[:1].upper() == "D":
        d = state.get_dispatch(con, target[0].upper() + target[1:])
        if d is None or d.get("run_id") != run["id"]:
            raise Refused("no-dispatch", f"{target} is not a dispatch of this run", scope=target)
        return d
    task = state.get_task(con, run["id"], target.upper())
    if task is None:
        raise Refused("no-task", f"{target} is not a task of this run", scope=target)
    if not task.get("current_dispatch_id"):
        raise Refused("no-dispatch", f"{task['id']} has no dispatch", scope=task["id"],
                      next_step=f"office dispatch {task['id']}")
    return state.get_dispatch(con, task["current_dispatch_id"])


def _pane_identity(con, run: dict, d: dict, who: str, pane: str) -> str:
    """What herdr says the pane is in, and whose task that is. A pane in another task's
    worktree is refused: the text would brief the wrong agent."""
    cwd = (dispatch._herdr_json(["pane", "get", pane]).get("pane") or {}).get("cwd")
    if not cwd:
        return "cwd not reported by herdr"
    owner = dispatch.cwd_owner(con, run["id"], cwd)
    owner_task = owner and owner.get("task_id")
    if owner_task and owner_task != d.get("task_id"):
        raise Refused("pane-mismatch", f"pane {pane} of {d['id']} ({who}) is in {cwd}, which belongs to {owner_task} "
                      f"(dispatch {owner['id']}); not prompting it", scope=who,
                      next_step=f"herdr pane read {pane}; office status")
    return f"cwd {cwd}" + (f", task {owner_task}" if owner_task else ", no task of this run")


def prompt(con, run: dict, target: str | None, text: str) -> Result:
    if not target or not text.strip():
        raise Usage("prompt-usage", "name the task or dispatch and the message",
                    next_step='office prompt <task|dispatch> -- "<message>"')
    d = _resolve(con, run, target)
    who = d.get("task_id") or d["id"]
    if d.get("launcher") != "herdr" or not d.get("pane_id"):
        raise Refused("no-pane", f"{d['id']} ({who}) has no Herdr pane to prompt", scope=who)
    # Ask herdr, not the dispatch row: a reviewer that settled without a reply
    # is recorded as exited while its agent still waits in the pane for this.
    if not gates._agent_alive(dispatch.herdr_agent_name(d["id"])):
        nxt = "office status"
        if d.get("task_id") and d.get("role") == "executor":
            # The dispatch row still says live until it is revoked, and rerun refuses a live one.
            nxt = (f"office revoke {who}, then office rerun {who} --resume | --fresh" if gates.worker_live(con, d["id"])
                   else f"office rerun {who} --resume | --fresh")
        raise Refused("dispatch-ended", f"{d['id']} ({who}) has no live agent; nothing is listening in its pane",
                      scope=who, next_step=nxt)
    pane = d["pane_id"]
    where = _pane_identity(con, run, d, who, pane)
    got = dispatch.submit_prompt(pane, text, pane=pane)
    outcome = {"landed": "landed", "held": "typed but unsubmitted"}.get(got, "sent, not confirmed")
    with db.transaction(con):
        state.emit(con, run, "prompt", f"{who} {d['id']}: orchestrator prompt {outcome}", audience="runtime",
                   task_id=d.get("task_id"), dispatch_id=d["id"], payload={"text": text[:500], "outcome": got})
    if got == "held":
        raise Refused("prompt-held", f"the prompt is typed but unsubmitted in pane {pane} after Office's Enters",
                      scope=who, next_step=f"herdr pane send-keys {pane} Enter (never send the text again)")
    if got == "landed":
        return Result(lines=[f"{who} {d['id']}: prompt landed in pane {pane} ({where})"], next="office status")
    return Result(lines=[f"{who} {d['id']}: prompt sent to pane {pane} ({where}); no landed signal and nothing left "
                         "in the composer (a busy agent may have queued it)"],
                  next=f"herdr pane read {pane}, then office status")
