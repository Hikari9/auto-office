"""Best-effort, once-per-episode notice for a bound orchestrator's context."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from office import db, discovery, paths, state as office_state
from office.util import atomic_write_text

DEFAULT_THRESHOLD = 272_000
CHECK_INTERVAL_SECONDS = 60
TAIL_BYTES = 256 * 1024
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def context_tokens(tail: str) -> int | None:
    """Match rico-mode ctxTokens over a transcript tail, ignoring a cut first row."""
    for line in reversed(tail.split("\n")):
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        message = row.get("message") if isinstance(row, dict) else None
        usage = (message.get("usage") if isinstance(message, dict) and row.get("type") == "assistant"
                 and not row.get("isSidechain") else None)
        if isinstance(usage, dict):
            values = [usage.get(key) or 0 for key in
                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
            if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
                return sum(values)
            return None
    return None


def tick(high: bool, tokens: int | None, threshold: int) -> tuple[bool, str | None]:
    """Return the next episode state and notice without mutating caller state."""
    if tokens is None:
        return high, None
    if tokens <= threshold:
        return False, None
    if high:
        return True, None
    tokens_k = math.floor(tokens / 1000 + 0.5)
    threshold_k = math.floor(threshold / 1000 + 0.5)
    return True, (f"ctx-high: orchestrator {tokens_k}k > {threshold_k}k; run /compactable at the next boundary; "
                  "compact on COMPACT NOW")


def _transcript_path(harness: str, session_id: str) -> Path | None:
    """Resolve only a Claude transcript whose filename is the bound session key."""
    if harness != "claude" or not _SESSION_ID.fullmatch(session_id):
        return None
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    try:
        matches = list(projects.glob(f"*/{session_id}.jsonl"))
        if len(matches) != 1:
            return None
        root = projects.resolve()
        candidate = matches[0].resolve(strict=True)
        candidate.relative_to(root)
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def _read_tail(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - TAIL_BYTES))
        return stream.read(TAIL_BYTES).decode("utf-8", errors="replace")


def _state_path(run_id: str) -> Path:
    return paths.run_dir(run_id) / "ctx-alert.state"


@contextmanager
def _state_lock(run_id: str):
    lock_path = paths.run_dir(run_id) / "ctx-alert.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _load_state(path: Path, session_id: str) -> dict:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) == 3 and lines[0] == session_id:
            return {"session": session_id, "checked_at": float(lines[1]), "high": lines[2] == "high"}
    except (OSError, ValueError):
        pass
    return {"session": session_id, "checked_at": 0.0, "high": False}


def _save_state(path: Path, state: dict) -> None:
    atomic_write_text(path, f"{state['session']}\n{state['checked_at']:.3f}\n{'high' if state['high'] else 'low'}\n", mode=0o600)


def notice(con, run: dict, *, now: float | None = None) -> str | None:
    """Return a compact notice for the caller's bound root session, when due."""
    policy = run.get("policy") or {}
    settings = policy.get("orchestrator_context") or {}
    if settings.get("enabled", True) is False:
        return None
    try:
        threshold = settings.get("threshold_tokens", DEFAULT_THRESHOLD)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
            threshold = DEFAULT_THRESHOLD
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD

    root_session = None
    for harness, session_id in discovery.fast_session_keys():
        bound = con.execute("SELECT 1 FROM session_bindings WHERE harness=? AND session_id=? AND run_id=? "
                            "AND ended_at IS NULL", (harness, session_id, run["id"])).fetchone()
        if bound:
            if harness == "claude":
                root_session = (harness, session_id)
                break
    if root_session is None:
        sessions = con.execute("SELECT session_id FROM session_bindings WHERE harness='claude' AND run_id=? "
                               "AND ended_at IS NULL", (run["id"],)).fetchall()
        if len(sessions) == 1:
            root_session = ("claude", sessions[0][0])
    if root_session is None:
        return None
    harness, session_id = root_session
    checked_at = time.time() if now is None else now
    try:
        with _state_lock(run["id"]):
            path = _state_path(run["id"])
            state = _load_state(path, session_id)
            if checked_at - state["checked_at"] < CHECK_INTERVAL_SECONDS:
                return None
            transcript = _transcript_path(harness, session_id)
            if transcript is None:
                state["checked_at"] = checked_at
                _save_state(path, state)
                return None
            try:
                tokens = context_tokens(_read_tail(transcript))
            except OSError:
                state["checked_at"] = checked_at
                _save_state(path, state)
                return None
            state["high"], line = tick(state["high"], tokens, threshold)
            state["checked_at"] = checked_at
            if line:
                with db.transaction(con):
                    office_state.emit(con, run, "ctx.high", line, payload={"session_id": session_id,
                                                                            "tokens": tokens,
                                                                            "threshold": threshold})
            _save_state(path, state)
            return line
    except OSError:
        return None
