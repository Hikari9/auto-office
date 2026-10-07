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
import re
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
POINTER_NAME = re.compile(r"[A-Za-z0-9_-]+")  # a run id; a file with a dot is something else


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


TERMINAL_PHASES = ("closed", "abandoned")


def active_pointers(primary: Path) -> list[Path]:
    """The fast-path pointers `.office/active/<run id>`, one per run office last projected as active.
    Only regular files named like a run id (no dots), in a real directory: nothing else there is a pointer."""
    active = primary / ".office" / "active"
    if active.is_symlink() or not active.is_dir():
        return []
    return sorted(p for p in active.iterdir() if POINTER_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink())


def _active_runs(primary: Path, *, verify: bool = False) -> list[tuple[str, str]]:
    """(run id, phase) from the fast-path projection and legacy pointers. A pointer that says its run
    ended is not active; with `verify`, neither is one whose run runs.db lacks or has ended."""
    pointers = []
    for p in active_pointers(primary):
        try:
            phase = p.read_text().split("\t", 1)[0].strip()
        except OSError:
            phase = "?"
        if phase not in TERMINAL_PHASES:
            pointers.append((p.name, phase))
    out = _verified(pointers) if verify else pointers
    refs = primary / ".office" / "runs"
    if refs.is_dir():
        for ref in refs.glob("*.ref"):
            try:
                state_json = Path(ref.read_text().strip()) / "state.json"
                phase = json.loads(state_json.read_text()).get("phase", "?")
            except (OSError, ValueError):
                continue
            if phase not in TERMINAL_PHASES:
                out.append((ref.stem, phase))
    return out


def _verified(pointers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The pointed-at runs runs.db still has as active (phase from the db). Stdlib only, read-only, a
    short timeout: this runs in a hook. An unreadable db keeps them all."""
    if not pointers:
        return pointers
    try:
        import sqlite3
        from office import paths
        db_path = paths.runs_db()
        if not db_path.exists():
            return []  # no runs in this data home: nothing is active here
        con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            marks = ",".join("?" * len(pointers))
            phases = dict(con.execute(f"SELECT id, phase FROM runs WHERE id IN ({marks})", [p[0] for p in pointers]))
        finally:
            con.close()
    except Exception:  # fail open: an unreadable db must not hide a real run
        return pointers
    return [(rid, phases[rid]) for rid, _ in pointers if rid in phases and phases[rid] not in TERMINAL_PHASES]


def stale_pointers(con, primary: Path) -> list[tuple[Path, str]]:
    """(pointer, why) for each active-run pointer whose run is missing from runs.db or already terminal."""
    from office import state
    out = []
    for p in active_pointers(primary):
        run = state.get_run(con, p.name)
        if run is None:
            out.append((p, "run missing"))
        elif state.is_terminal(run):
            out.append((p, f"run {run['phase']}"))
    return out


def _disabled(primary: Path) -> bool:
    if (primary / ".office" / "hooks.on").exists():
        return False
    if (primary / ".office" / "hooks.off").exists():
        return True
    return (Path.home() / ".office" / "hooks.off").exists()


def _payload_session(payload: dict) -> str | None:
    return payload.get("session_id") or payload.get("conversationId") or payload.get("sessionId")


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
    if event == "shell.pre":
        return _shell_guard(harness, payload)
    session = _payload_session(payload)
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
            live = _active_runs(primary, verify=True)
            if len(live) == 1:
                _say(harness, event, f"Active Office run {live[0][0][:8]} (phase {live[0][1]}). office resume to bind.")
            elif live:
                _say(harness, event, f"{len(live)} active Office runs here. office list, then office resume <id> to bind.")
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
        dispatch_id, session = os.environ.get("OFFICE_DISPATCH_ID"), _payload_session(payload)
        if dispatch_id and session:
            # Resume needs the harness session id: take it from the first event that carries one.
            # A failure here must not skip the write guard below.
            try:
                from office import dispatch
                dispatch.record_session(con, run, dispatch_id, str(session), harness=harness, source="hook")
            except Exception as exc:
                _log_error(None, event, harness, exc)
        if event == "session.start":
            source = payload.get("source", "startup")
            if source in ("resume", "compact", "startup"):
                res = guide.status(con, run, record=False)
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


_SED_I = re.compile(r"(?<![\w./-])sed((?:[ \t]+-[A-Za-z]+)*?)[ \t]+(-[A-Za-z]*i)(?=[ \t])(?![ \t]+(?:''|\"\"))")


def _quoted_at(command: str, pos: int) -> bool:
    """Whether `pos` falls inside a single- or double-quoted string, or at or
    after an unquoted heredoc operator, whose body is data, not commands."""
    quote = None
    i = 0
    while i < pos:
        c = command[i]
        if quote is None and c in "'\"":
            quote = c
        elif c == quote:
            quote = None
        elif c == "\\" and quote != "'":
            i += 1
        elif quote is None and command.startswith("<<", i):
            return True
        i += 1
    return quote is not None


def portable_sed(command: str) -> str | None:
    """`sed -i <script>` rewritten to BSD's `sed -i '' <script>`, or None when
    nothing changes. BSD sed takes the word after -i as a backup suffix, so the
    GNU form either errors or, with `-i -e`, edits and leaves a `<file>-e` copy.
    The script is left byte-identical: translating it to another regex dialect
    (perl -pi) would change what BRE escapes such as \\( and \\{ mean."""
    out, last, changed = [], 0, False
    for m in _SED_I.finditer(command):
        if _quoted_at(command, m.start()):
            continue
        out.append(command[last:m.end()] + " ''")
        last = m.end()
        changed = True
    return "".join(out) + command[last:] if changed else None


def _gnu_sed() -> bool:
    import subprocess
    try:
        return subprocess.run(["sed", "--version"], capture_output=True, timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _shell_guard(harness: str, payload: dict) -> int:
    """PreToolUse(Bash): make a GNU-style in-place sed portable on macOS. The
    rewritten command still goes through the harness's permission flow (no
    permissionDecision is returned), and anything unexpected fails open."""
    if harness != "claude" or sys.platform != "darwin":
        return 0
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or "sed" not in command:
        return 0
    fixed = portable_sed(command)
    if fixed is None or _gnu_sed():
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": {**tool_input, "command": fixed},
        "additionalContext": "Office rewrote `sed -i` to `sed -i ''` (BSD sed on macOS). Check git diff that the edit landed.",
    }}))
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


def _log_error(primary: Path | None, event: str, harness: str, exc: Exception) -> None:
    try:
        from office import paths
        log = paths.state_home() / "hook-errors.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            fh.write(json.dumps({"event": event, "harness": harness, "error": f"{type(exc).__name__}: {exc}"[:300]}) + "\n")
    except Exception:
        pass
