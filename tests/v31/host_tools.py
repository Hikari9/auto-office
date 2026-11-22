"""Directories a test PATH must keep so git and the OS shell still resolve (Windows has no /usr/bin)."""
from __future__ import annotations

import os
import shutil
import sys


def host_tool_dirs() -> list[str]:
    if sys.platform != "win32":
        return []
    root = os.environ.get("SystemRoot", r"C:\Windows")
    dirs = [os.path.join(root, "System32"), root]
    git = shutil.which("git")
    return dirs + ([os.path.dirname(git)] if git else [])


def write_launcher(path, text: str, encoding: str | None = None):
    """Write a fake harness whose `text` starts with a `#!` line. POSIX runs the shebang. Windows
    cannot, so the body goes to `<name>.py` and a `<name>.cmd` beside it runs it with the
    interpreter on the shebang line (`which` finds `.cmd` through PATHEXT). Returns the file to execute: `path`, or the `.cmd` on Windows."""
    from pathlib import Path
    path = Path(path)
    if sys.platform != "win32":
        path.write_text(text, encoding=encoding)
        path.chmod(0o755)
        return path
    exe = text.splitlines()[0][2:].strip()
    if Path(exe).name in ("sh", "bash") or exe.endswith(("/env sh", "/env bash")):
        # a POSIX shell script: run it with the sh that Git for Windows provides, or skip when none is installed
        import shutil
        import pytest
        found = shutil.which("sh") or shutil.which("bash")
        if not found:
            pytest.skip("needs a POSIX sh, which this Windows host does not have")
        exe = found
    body = path.with_name(path.name + (".sh" if Path(exe).name.startswith(("sh", "bash")) else ".py"))
    body.write_text(text, encoding="utf-8")
    launcher = path.with_name(path.name + ".cmd")
    launcher.write_text(f'@"{exe}" "{body}" %*\r\n', encoding="utf-8")
    return launcher


def read_launcher(path) -> str:
    """The script text `write_launcher` wrote for `path`: the file itself, or its `.py`/`.sh` body on Windows."""
    from pathlib import Path
    path = Path(path)
    if sys.platform != "win32":
        return path.read_text()
    for suffix in (".py", ".sh"):
        body = path.with_name(path.name + suffix)
        if body.exists():
            return body.read_text(encoding="utf-8")
    return path.read_text()


def remove_launcher(path) -> None:
    """Delete a fake harness `write_launcher` wrote, with whatever Windows files go with it."""
    from pathlib import Path
    path = Path(path)
    for p in (path, *(path.with_name(path.name + x) for x in (".cmd", ".py", ".sh"))):
        p.unlink(missing_ok=True)
