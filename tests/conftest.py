"""Shared pytest setup.

Tests outside tests/v31 exercise the retained Auto Office 3.0 helper surface
(scripts/). They run as that pinned runtime, which is what the 3.1 guard in
scripts/office_runtime.py expects of a 3.0 run's own helper process.
"""
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _legacy_helper_identity(request, monkeypatch):
    if "v31" not in Path(str(request.fspath)).parts:
        monkeypatch.setenv("OFFICE_PINNED_LEGACY", "1")
    yield
