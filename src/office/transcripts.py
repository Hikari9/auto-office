"""A pane-hosted agent's final reply, read from its harness's own session log.

A read-only reviewer in a Herdr pane cannot write `reply.txt`, and the pane
screen is a poor record of what it said: TUI chrome, bullets, wrapped lines,
or nothing at all once the view scrolls. The harness keeps the full reply in
its session transcript, so that is the preferred fallback.

Transcripts are located by content: the one-line prompt Office sends names the
dispatch's brief path, so the matching session is the most recent transcript
in which a user-role prompt (never a tool result) carries that path and, when
the transcript records one, whose cwd is the dispatch's cwd. That keeps an
orchestrator's own session, which may have printed the path, from being read
as the reviewer's. Only files modified since the dispatch started are read.

- Claude: $CLAUDE_CONFIG_DIR (default ~/.claude)/projects/<cwd slug>/*.jsonl,
  last `type: assistant` entry with text content.
- Codex: $CODEX_HOME (default ~/.codex)/sessions/YYYY/MM/DD/rollout-*.jsonl,
  last `payload.type: message, role: assistant` entry.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Transcripts are written continuously; allow for clock skew and for a session
# file created a little before the dispatch row recorded its start.
SLACK_SECONDS = 120


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_slug(cwd: str | Path) -> str:
    """Claude names a project directory after its cwd with every character
    other than a letter or digit replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _epoch(since) -> float:
    if since is None:
        return 0.0
    if isinstance(since, (int, float)):
        return float(since)
    try:
        dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _recent(files, since: float) -> list[Path]:
    out = []
    for f in files:
        try:
            if f.is_file() and f.stat().st_mtime >= since - SLACK_SECONDS:
                out.append(f)
        except OSError:
            continue
    return sorted(out, key=lambda f: f.stat().st_mtime, reverse=True)


def _mentions(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _same_dir(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def _prompted(path: Path, marker: str, cwd) -> bool:
    """True when a user-role prompt in this transcript carries `marker` and the
    session's recorded cwd (if any) is `cwd`."""
    prompted, seen_cwd = False, None
    for row in _jsonl(path):
        payload = row.get("payload") or {}
        row_cwd = row.get("cwd") or (payload.get("cwd") if row.get("type") in ("session_meta", "turn_context") else None)
        if row_cwd and seen_cwd is None:
            seen_cwd = row_cwd
        if row.get("type") == "user":  # claude
            content = (row.get("message") or {}).get("content")
            if marker in _text_blocks(content):
                prompted = True
        elif payload.get("type") == "message" and payload.get("role") == "user":  # codex
            if marker in _text_blocks(payload.get("content")):
                prompted = True
    if not prompted:
        return False
    return not (cwd and seen_cwd) or _same_dir(cwd, seen_cwd)


def _jsonl(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _text_blocks(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") in ("text", "output_text", "input_text") and block.get("text"):
            parts.append(block["text"])
    return "\n".join(parts)


def claude_final_text(path: Path) -> str | None:
    """The last assistant message's text. Claude logs one entry per content
    block, so text blocks sharing the final message id are joined."""
    last_id, chunks = None, []
    for row in _jsonl(path):
        if row.get("type") != "assistant":
            continue
        msg = row.get("message") or {}
        text = _text_blocks(msg.get("content"))
        if not text.strip():
            continue
        mid = msg.get("id")
        if mid is not None and mid == last_id:
            chunks.append(text)
        else:
            last_id, chunks = mid, [text]
    return "\n".join(chunks) if chunks else None


def codex_final_text(path: Path) -> str | None:
    last = None
    for row in _jsonl(path):
        payload = row.get("payload") or {}
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            text = _text_blocks(payload.get("content"))
            if text.strip():
                last = text
    return last


def _claude_candidates(cwd, since: float) -> list[Path]:
    projects = claude_home() / "projects"
    if not projects.is_dir():
        return []
    files = []
    if cwd:
        files = _recent((projects / claude_slug(cwd)).glob("*.jsonl"), since)
    # The slug rule is the harness's, not ours: widen to every project when the
    # expected directory holds nothing recent.
    return files or _recent(projects.glob("*/*.jsonl"), since)


def _codex_candidates(since: float) -> list[Path]:
    sessions = codex_home() / "sessions"
    if not sessions.is_dir():
        return []
    start = datetime.fromtimestamp(max(since - SLACK_SECONDS, 0), tz=timezone.utc).date() - timedelta(days=1)
    today = datetime.now(timezone.utc).date() + timedelta(days=1)
    # Reviews end within hours; never walk more than a month of day folders.
    day, files = max(start, today - timedelta(days=31)), []
    while day <= today:
        files.extend((sessions / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}").glob("rollout-*.jsonl"))
        day += timedelta(days=1)
    return _recent(files, since)


def final_reply(harness: str | None, *, marker: str, cwd: str | Path | None = None, since=None) -> str | None:
    """The final assistant reply of the session that was sent `marker` (the
    brief path), or None when no transcript for it is found."""
    if not marker:
        return None
    t = _epoch(since)
    h = (harness or "").lower()
    readers = []
    if h in ("claude", ""):
        readers.append((_claude_candidates(cwd, t), claude_final_text))
    if h in ("codex", ""):
        readers.append((_codex_candidates(t), codex_final_text))
    for files, read in readers:
        for f in files:
            if _mentions(f, marker) and _prompted(f, marker, cwd):
                text = read(f)
                if text and text.strip():
                    return text
    return None
