"""office doctor: verify the installation and name every known defect."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

from office import adapters, config_repairs, db, frontdoor, install, legacy, paths, read_scope, runtime_default, state, version
from office.result import Result


def _visual_capture_status() -> tuple[str, bool]:
    """Verify the browser real visual captures launch, not just Playwright import (#651).

    The default headless browser is a separate chromium_headless_shell
    executable; chromium.executable_path can point to a different binary.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("visual capture: playwright missing (uv tool install 'auto-office[visual]'); "
                "visual gates report CAPTURE_BLOCKED", False)
    try:
        from office import visual
        with sync_playwright() as playwright:
            # Match capture's optional OFFICE_CHROME override, without hanging doctor.
            browser = playwright.chromium.launch(**{**visual.launch_args(), "timeout": 10000})
            browser.close()
    except Exception as error:
        reason = (str(error).splitlines() or [type(error).__name__])[0][:240]
        if "Executable doesn't exist" in str(error):
            install = f"{shlex.quote(sys.executable)} -m playwright install chromium-headless-shell"
            return (f"visual capture: Chromium headless shell missing ({reason}); "
                    f"install the matching browser: {install}", True)
        return (f"visual capture: Chromium headless shell cannot launch ({reason}); "
                "check Playwright browser installation and system dependencies", True)
    return "visual capture: playwright and Chromium headless shell available", False


def doctor(fix: bool = False, probe_vision: bool = False, probe_route: str | None = None,
          ...[truncated]