"""Explicitly authorized amendments to the small allowlist of live run options (#937).

Requirements, routing policy, base SHA and review gates remain pinned. This is
not a second path for editing the approved task/requirements contract.
"""
from __future__ import annotations

import os
import re
import sys

from office import db, jobs, prs, state
from office.result import Result
from office.state import Refused, Usage
from office.util import now_iso

USAGE = ('office amend options --prs|--no-prs --quote "<user approval>" --confirm')


def _confirm(run: dict, enabled: bool, quote: str | None, confirmed: bool) -> str:
    if not quote or len(re.sub(r"\s+", "", quote)) < 2:
        raise Refused("user-authorization-required", "changing live run options requires the user's exact words "
                      "in --quote", next_step=USAGE)
    if not confirmed:
        if not sys.stdin.isatty():
            raise Refused("confirmation-required", "a non-interactive run option change requires --confirm "
                          "as well as the user's --quote", next_step=USAGE)
        choice = input(f"Change run {run['id'][:8]} task PRs to {'ON' if enabled else 'OFF'}? [y/N] ")
        if choice.strip().lower() not in ("y", "yes"):
            raise Refused("confirmation-declined", "run option amendment not confirmed; state unchanged",
                          next_step="office status")
    return quote.strip()


def _disable_blocker(con, run: dict) -> str | None:
    """Never revoke PR instructions already in flight or remote PR work of uncertain state."""
    for task in state.tasks(con, run["id"]):
        if task.get("pr"):
            return f"{task['id']} already has a recorded PR; changing --no-prs would abandon its lifecycle"
    # Any job, even a done one: a sync that errors after the push can leave a remote branch or PR unrecorded.
    job = con.execute("SELECT id, status FROM outbox WHERE run_id=? AND kind IN ('pr', 'pr_sync') "
                      "ORDER BY created_at LIMIT 1", (run["id"],)).fetchone()
    if job:
        return f"PR job {job['id']} ({job['status']}) may already have pushed or created a PR"
    dispatch = con.execute("SELECT id, task_id FROM dispatches WHERE run_id=? AND role='executor' "
                           "AND status IN ('launching', 'running') AND ended_at IS NULL LIMIT 1",
                           (run["id"],)).fetchone()
    if dispatch:
        return (f"executor {dispatch['task_id']} ({dispatch['id']}) is still live with PR instructions; "
                "finish or revoke the dispatch first")
    return None


def amend(con, run: dict, *, enable_prs: bool | None, quote: str | None,
          confirm: bool, extra: list[str] | None = None) -> Result:
    if os.environ.get("OFFICE_DISPATCH_ID") or os.environ.get("OFFICE_ROLE"):
        raise Refused("worker-not-authorized", "a worker cannot amend run options")
    if extra:
        raise Refused("immutable-run-option", f"run option {extra[0]!r} is not safe to amend: "
                      "only task PR publishing is mutable; base, gear, review gates and requirements remain pinned",
                      next_step=USAGE)
    if enable_prs is None:
        raise Usage("missing-run-option", "choose --prs to enable or --no-prs to disable task PRs",
                    next_step=USAGE)
    if state.is_terminal(run):
        raise Refused("run-terminal", f"run is {run['phase']}; closed run options cannot change")
    user_quote = _confirm(run, enable_prs, quote, confirm)
    # External probing is deliberately outside BEGIN IMMEDIATE. An initial
    # --no-prs pins off; a fresh probe here sees newly granted GitHub access.
    before = prs.settings(con, run)
    desired = bool(enable_prs)
    # A transient gh probe may report PRs off only provisionally. Explicit
    # --no-prs must replace that retriable state or it could silently turn on
    # at the next dispatch when GitHub recovers (#937, #479).
    if bool(before.get("enabled")) == desired and not (not desired and before.get("transient")):
        return Result(lines=[f"task PRs already {'on' if desired else 'off'}; no change recorded"],
                      next="office status", data={"prs": before, "changed": False})
    if desired:
        detected = prs._detect(con, run)
        if not detected.get("enabled"):
            raise Refused("prs-unavailable", f"cannot enable task PRs: {detected.get('reason') or 'GitHub unavailable'}",
                          next_step="fix GitHub/origin access and retry " + USAGE)
    else:
        # Carry over repo, base_branch and merge_method when the prior record has them.
        kept = {k: before[k] for k in ("repo", "base_branch", "merge_method") if k in before}
        detected = {**kept, "enabled": False, "reason": "disabled by confirmed run option amendment"}
    queued = 0
    with db.transaction(con):
        latest = state.get_run(con, run["id"])
        if state.is_terminal(latest):
            raise Refused("run-terminal", f"run is {latest['phase']}; closed run options cannot change")
        previous = ((latest.get("landing") or {}).get("prs") or {})
        if previous != before:
            raise Refused("run-options-stale", "run PR settings changed while confirming; review and retry",
                          next_step="office inspect run")
        if not desired:
            blocker = _disable_blocker(con, latest)
            if blocker:
                raise Refused("prs-in-use", f"cannot disable task PRs: {blocker}", next_step="office inspect run")
        timestamp = now_iso()
        updated = {**detected, "amended_at": timestamp, "amended_by": "user"}
        landing = dict(latest.get("landing") or {})
        landing["prs"] = updated
        state.update_run(con, run["id"], landing=landing)
        refreshed = state.get_run(con, run["id"])
        state.emit(con, refreshed, "run.options_amended", f"task PRs {'on' if previous.get('enabled') else 'off'} "
                   f"-> {'on' if desired else 'off'} (user confirmed)", payload={
                       "option": "prs.enabled", "before": previous, "after": updated,
                       "quote": user_quote, "confirmed": True, "at": timestamp})
        if desired:
            # A run may already have finished work while --no-prs was pinned.
            # Catch up its latest revisions/acceptance through normal outbox
            # jobs; never push/launch GitHub work inside this transaction.
            for task in state.tasks(con, run["id"]):
                if not prs.has_pr(task):
                    continue
                if task.get("current_revision_id"):
                    prs.queue(con, refreshed, task["id"], "revision", task["current_revision_id"])
                    queued += 1
                if task.get("status") == "accepted" and task.get("accepted_revision_id"):
                    prs.queue(con, refreshed, task["id"], "accepted", task["accepted_revision_id"])
                    queued += 1
    state.write_projection(con, run["id"])
    if queued:
        jobs.kick(con, run["id"])
    return Result(lines=[f"task PRs {'enabled' if desired else 'disabled'} for run {run['id'][:8]}; "
                         f"user confirmation and before/after recorded in run.options_amended"
                         + (f"; queued {queued} existing PR sync step(s)" if queued else "")],
                  next="office status", data={"prs": updated, "changed": True, "queued": queued})
