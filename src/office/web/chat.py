"""Chat delivery to a run's orchestrator, and only to it.

The target is `{host, run_id, session}` where `session` is the orchestrator's
session identity (`session:<run>/<harness>/<id>`). Delivery requires the
binding to still be active and a live Herdr agent on that exact pane. Outcomes:
landed -> completed, held or unconfirmed -> unknown, refused -> failed. An
`unknown` receipt is never retried automatically: a resend is a new command
whose `payload.resend_of` names the earlier one.
"""
from __future__ import annotations

from typing import Mapping

from office.web import identity

MAX_TEXT = 8000
OUTCOME = {"landed": "completed", "held": "unknown", "": "unknown"}


class ChatRefused(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def resolve(run: dict | None, target: Mapping, host_id: str | None, panes: Mapping[str, str]) -> dict:
    """Validate a chat target against a fresh run projection; return {session, pane}.

    `panes` maps a session identity to the Herdr pane the web launcher started it
    in; a `herdr` binding names its pane as its own session id.
    """
    if target.get("host") not in (None, identity.host(host_id), host_id):
        raise ChatRefused("foreign-host", "the target names another host")
    if run is None:
        raise ChatRefused("run-missing", "the run no longer exists")
    if run.get("liveness") == "terminal":
        raise ChatRefused("run-terminal", f"run is {run.get('phase')}")
    session = target.get("session")
    if not session or not str(session).startswith(f"session:{run['run_id']}/"):
        if str(session or "").startswith("dispatch:"):
            raise ChatRefused("not-orchestrator", "chat reaches orchestrators only, never workers or reviewers")
        raise ChatRefused("bad-target", "target.session must be this run's orchestrator session")
    orchestrators = {n["id"]: n for n in run["agents"]["columns"]["orchestrators"]}
    if session not in orchestrators:
        raise ChatRefused("binding-ended", "that orchestrator session is no longer bound to the run")
    harness, _, sid = str(session).removeprefix(f"session:{run['run_id']}/").partition("/")
    pane = sid if harness == "herdr" else panes.get(session)
    if not pane:
        raise ChatRefused("no-pane", "no Herdr pane is known for that orchestrator session")
    return {"session": session, "pane": pane}


def deliver(launcher, pane: str, text: str) -> tuple[str, dict, str | None]:
    """Send once; (status, result, error). Never retried here."""
    if not launcher.agent_live(pane):
        return "failed", {"pane": pane, "delivery": "refused"}, "no live Herdr agent on that pane"
    got = launcher.send(pane, text)
    status = OUTCOME.get(got, "unknown")
    return status, {"pane": pane, "delivery": got or "unconfirmed"}, \
        None if status == "completed" else f"delivery {got or 'unconfirmed'}; not retried automatically"
