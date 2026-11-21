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
    from tests import _win_shebang  # the `python3` that forwards to the interpreter running the tests
    return (dirs + ([os.path.dirname(git)] if git else []) + ([_win_shebang.PYTHON3_DIR] if _win_shebang.PYTHON3_DIR else [])
            + _win_shebang.unix_tool_dirs())
