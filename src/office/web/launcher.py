"""Launch an orchestrator agent for a web start/resume, in a new Herdr pane.

The agent runs the configured orchestrator route (`scheduler.orchestrator_route`)
in the repository checkout and gets an Auto Office prompt carrying the issue,
the authorization target as its end state and the command receipt id. When
Herdr or the harness is missing the launcher says why; the web UI then offers
the copyable `office start ...` command instead.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Protocol


def start_command(issue_ref: str, end_state: str | None) -> str:
    """The `office start` line a person can paste when no launch is possible."""
    cmd = f"office start --issue {shlex.quote(str(issue_ref))}"
    if end_state:
        cmd += f" --end-state {shlex.quote(end_state)}"
    return cmd + f' "Resolve issue {issue_ref}"'


def start_prompt(*, issue_ref: str, issue_title: str | None, end_state: str | None, receipt_id: str) -> str:
    end = end_state or "preview"
    title = f" ({issue_title})" if issue_title else ""
    return (f"/auto-office Resolve GitHub issue {issue_ref}{title}. Start the run with "
            f"`office start --issue {issue_ref} --end-state {end}`; the operator authorized end state `{end}` "
            f"from the Office web UI (command receipt {receipt_id}). Do not go beyond that end state.")


def resume_prompt(*, run_id: str, receipt_id: str) -> str:
    return (f"/auto-office Resume Auto Office run {run_id}: run `office resume {run_id}` and continue it. "
            f"Requested from the Office web UI (command receipt {receipt_id}).")


class Launcher(Protocol):
    def unavailable(self, harness: str | None = None) -> str | None: ...
    def launch(self, *, cwd: Path, prompt: str, label: str, harness: str | None = None) -> dict: ...
    def agent_live(self, pane: str) -> bool: ...
    def send(self, pane: str, text: str) -> str: ...
    def focus(self, pane: str) -> bool: ...


def _agent_name(label: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", f"office-web-{label}".lower())[:32]


class HerdrLauncher:
    """Real launches through the herdr CLI and Office's prompt delivery."""

    def __init__(self, harness: str = "claude"):
        self.harness = harness

    def unavailable(self, harness: str | None = None) -> str | None:
        from office import dispatch
        if not dispatch.herdr_usable():
            return "Herdr is not available to the web service (start it inside Herdr, HERDR_ENV=1)"
        if not shutil.which(harness or self.harness):
            return f"orchestrator harness {harness or self.harness!r} is not on PATH"
        return None

    def launch(self, *, cwd: Path, prompt: str, label: str, harness: str | None = None) -> dict:
        from office import dispatch
        harness = harness or self.harness
        why = self.unavailable(harness)
        if why:
            return {"ok": False, "reason": why}
        res = dispatch._herdr_json(["tab", "create", "--label", f"office-{label}", "--cwd", str(cwd), "--no-focus"])
        pane = (res.get("root_pane") or {}).get("pane_id")
        if not pane:
            return {"ok": False, "reason": "herdr did not create a pane"}
        name = _agent_name(label)
        try:
            proc = subprocess.run(["herdr", "agent", "start", name, "--kind", harness, "--pane", pane, "--",
                                   harness], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "pane": pane, "reason": f"herdr agent start failed: {type(exc).__name__}"}
        if proc.returncode != 0:
            return {"ok": False, "pane": pane, "reason": (proc.stdout or proc.stderr or "").strip()[:200]}
        delivered = dispatch._deliver_prompt(name, pane, prompt)
        return {"ok": delivered, "pane": pane, "agent": name, "harness": harness,
                "reason": None if delivered else "the prompt did not land in the agent"}

    def agent_live(self, pane: str) -> bool:
        from office import dispatch
        return bool(dispatch._agent_up(pane))

    def send(self, pane: str, text: str) -> str:
        from office import dispatch
        agent = (dispatch._herdr_json(["pane", "get", pane]).get("pane") or {}).get("agent")
        name = agent.get("name") if isinstance(agent, dict) else agent
        if not name:
            return ""
        return dispatch.submit_prompt(str(name), text, pane=pane)

    def focus(self, pane: str) -> bool:
        try:
            return subprocess.run(["herdr", "pane", "focus", pane], capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class FakeLauncher:
    """Fixture mode and tests: records launches, never touches Herdr."""

    def __init__(self, *, reason: str | None = None, send_result: str = "landed"):
        self.reason, self.send_result = reason, send_result
        self.launches: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.live: set[str] = set()

    def unavailable(self, harness: str | None = None) -> str | None:
        return self.reason

    def launch(self, *, cwd: Path, prompt: str, label: str, harness: str | None = None) -> dict:
        if self.reason:
            return {"ok": False, "reason": self.reason}
        pane = f"fake-pane-{len(self.launches) + 1}"
        self.launches.append({"cwd": str(cwd), "prompt": prompt, "label": label, "pane": pane, "harness": harness})
        self.live.add(pane)
        return {"ok": True, "pane": pane, "agent": _agent_name(label), "harness": harness}

    def agent_live(self, pane: str) -> bool:
        return pane in self.live

    def send(self, pane: str, text: str) -> str:
        self.sent.append((pane, text))
        return self.send_result

    def focus(self, pane: str) -> bool:
        return pane in self.live
