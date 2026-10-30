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
