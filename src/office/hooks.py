"""Harness hook shim: `office hook <event> --harness <h> --office-managed`.

Hooks only nudge and inject; explicit office commands are the contract. A hook
never starts or binds a run, never mints a pass, and fails open (except the
tool.pre hard stop for a bound worker on harnesses with a verified denial).

The unbound path reads environment variables and stats files only, so it
stays within the architecture's 50 ms budget before the runtime is imported.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

INJECT_MAX_LINES = 12
INJECT_MAX_BYTES = 1024
EVENT_ALIASES = {
    "SessionStart": "session.start", "UserPromptSubmit": "prompt.submit", "PreToolUse": "tool.pre",
    "PostToolUse": "tool.post", "Stop": "turn.stop", "PreCompact": "compact.pre", "SessionEnd": "session.end",
    "BeforeAgent": "prompt.submit", "BeforeTool": "tool.pre", "AfterTool": "tool.post", "AfterAgent": "turn.stop",
    "PreCompress": "compact.pre",
}
DENY_CAPABLE = {"claude", "gemini"}


def _primary_checkout(start: Path) -> Path | None:
    cur = start.resolve()
    for _ in range(40):
        dotgit = cur / ".git"
        if dotgit.is_dir():
            return cur
        if dotgit.is_file():
            try:
                text = dotgit.read_text().strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                gitdir = (cur / text.split(":", 1)[1].strip()).resolve()
                common = gitdir
                cd = gitdir / "commondir"
                if cd.is_file():
                    common = (gitdir / cd.read_text().strip()).resolve()
                return common.parent if common.name == ".git" else common
            return None
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _session_files(primary: Path, harness: str, session: str | None) -> list[Path]:
    base = primary / ".office" / "sessions"
    out = []
    if session:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session)
        out.append(base / f"{harness}-{safe}.json")
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        out.append(base / ("herdr-" + "".join(c if c.isalnum() or c in "-_." else "_" for c in pane) + ".json"))
    return out


def _active_runs(primary: Path) -> list[tuple[str, str]]:
    """(run id, phase) from the fast-path projection and legacy pointers."""
    out = []
    active = primary / ".office" / "active"
    if active.is_dir():
        for p in active.iterdir():
            try:
                phase = p.read_text().split("\t", 1)[0].strip()
            except OSError:
                phase = "?"
            out.append((p.name, phase))
    refs = primary / ".office" / "runs"
    if refs.is_dir():
        for ref in refs.glob("*.ref"):
            try:
                state_json = Path(ref.read_text().strip()) / "state.json"
                phase = json.loads(state_json.read_text()).get("phase", "?")
            except (OSError, ValueError):
                continue
            if phase not in ("closed", "abandoned"):
                out.append((ref.stem, phase))
    return out


def _disabled(primary: Path) -> bool:
    if (primary / ".office" / "hooks.on").exists():
        return False
    if (primary / ".office" / "hooks.off").exists():
        return True
    return (Path.home() / ".office" / "hooks.off").exists()


def _read_stdin() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except (OSError, ValueError):
        return {}


def main(argv: list[str]) -> int:
    if not argv:
        return 0
    event = EVENT_ALIASES.get(argv[0], argv[0])
    harness = "claude"
    if "--harness" in argv:
        i = argv.index("--harness")
        if i + 1 < len(argv):
            harness = argv[i + 1]
    if os.environ.get("OFFICE_HOOKS", "").lower() in ("off", "0", "false"):
        return 0
    payload = _read_stdin()
    session = payload.get("session_id") or payload.get("conversationId") or payload.get("sessionId")
    cwd = Path(payload.get("cwd") or os.getcwd())
    primary = _primary_checkout(cwd)
    if primary is None:
        return 0
    runs = _active_runs(primary)
    if not runs:
        return 0
    bound_files = [p for p in _session_files(primary, harness, session) if p.exists()]
    worker = bool(os.environ.get("OFFICE_DISPATCH_ID"))
    if not bound_files and not worker:
        if event == "session.start" and not _disabled(primary):
            if len(runs) == 1:
                _say(harness, event, f"Active Office run {runs[0][0][:8]} (phase {runs[0][1]}). office resume to bind.")
            else:
                _say(harness, event, f"{len(runs)} active Office runs here. office list, then office resume <id> to bind.")
        return 0
    if _disabled(primary):
        return 0
    try:
        return _bound(event, harness, payload, bound_files, worker)
    except Exception as exc:  # fail open, but leave a trace for office doctor
        _log_error(primary, event, harness, exc)
        return 0


def _bound(event: str, harness: str, payload: dict, bound_files: list[Path], worker: bool) -> int:
    from office import db, guide, state
    run_id = os.environ.get("OFFICE_RUN_ID")
    if not run_id and bound_files:
        run_id = json.loads(bound_files[0].read_text()).get("run_id")
    if not run_id:
        return 0
    con = db.connect()
    try:
        run = state.get_run(con, run_id)
        if run is None or state.is_terminal(run):
            return 0
        from office import version
        if not version.same_line(run["office_version"], version.current()):
            return 0  # a run is served only by a runtime on its release line
        if event == "session.start":
            source = payload.get("source", "startup")
            if source in ("resume", "compact", "startup"):
                res = guide.status(con, run)
                lines = res.lines[:10] + ([f"next: {res.next}"] if res.next else []) + ["office status --verbose"]
                _say(harness, event, _budget(lines))
            return 0
        if event == "prompt.submit":
            lines = []
            if worker and os.environ.get("OFFICE_DISPATCH_ID"):
                from office import amend
                lines = amend.pending_block(con, run, os.environ["OFFICE_DISPATCH_ID"])
            else:
                events = state.unread_events(con, run["id"], "orchestrator", ("orchestrator",), limit=8)
                lines = [f"office: {e['summary']}" for e in events]
                if events:
                    text = _budget(lines)
                    shown = text.count("\n") + 1
                    with db.transaction(con):
                        state.advance_cursor(con, run["id"], "orchestrator", events[min(shown, len(events)) - 1]["seq"])
            if lines:
                _say(harness, event, _budget(lines + ["office status --verbose"]))
            return 0
        if event == "tool.pre" and worker:
            return _guard_write(harness, payload, run, con)
        return 0
    finally:
        con.close()


def _guard_write(harness: str, payload: dict, run: dict, con) -> int:
    """Hard stop: a bound worker writing outside its own worktree."""
    from office import state
    d = state.get_dispatch(con, os.environ.get("OFFICE_DISPATCH_ID", ""))
    if not d or not d.get("worktree"):
        return 0
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if not target:
        return 0
    wt = Path(d["worktree"]).resolve()
    try:
        Path(target).resolve().relative_to(wt)
        return 0
    except ValueError:
        pass
    reason = f"Office: this worker may only write inside its worktree ({wt}); {target} is outside it."
    if harness in DENY_CAPABLE:
        sys.stderr.write(reason + "\n")
        return 2
    sys.stderr.write("warning: " + reason + "\n")
    return 0


def _budget(lines: list[str]) -> str:
    out, size = [], 0
    for line in lines[:INJECT_MAX_LINES]:
        line = line.replace("\x1b", "")
        if size + len(line) + 1 > INJECT_MAX_BYTES:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def _say(harness: str, event: str, text: str) -> None:
    if not text:
        return
    if harness == "gemini":
        print(json.dumps({"hookSpecificOutput": {"additionalContext": text}}))
    elif harness in ("claude",):
        print(text)
    elif harness == "codex":
        # No verified injection channel on Codex: say nothing into context.
        sys.stderr.write(text + "\n")
    else:
        sys.stderr.write(text + "\n")


def _log_error(primary: Path, event: str, harness: str, exc: Exception) -> None:
    try:
        from office import paths
        log = paths.state_home() / "hook-errors.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            fh.write(json.dumps({"event": event, "harness": harness, "error": f"{type(exc).__name__}: {exc}"[:300]}) + "\n")
    except Exception:
        pass
