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


def write_launcher(path, text: str) -> None:
    """Write a fake harness whose `text` starts with a `#!` line. POSIX runs the shebang. Windows
    cannot, so the body goes to `<name>.py` and a `<name>.cmd` beside it runs it with the
    interpreter on the shebang line (`which` finds `.cmd` through PATHEXT)."""
    from pathlib import Path
    path = Path(path)
    if sys.platform != "win32":
        path.write_text(text)
        path.chmod(0o755)
        return
    exe = text.splitlines()[0][2:].strip()
    body = path.with_name(path.name + ".py")
    body.write_text(text, encoding="utf-8")
    path.with_name(path.name + ".cmd").write_text(f'@"{exe}" "{body}" %*\r\n', encoding="utf-8")
