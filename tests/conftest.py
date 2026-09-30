"""Shared pytest setup.

Tests outside tests/v31 exercise the retained Auto Office 3.0 helper surface
(scripts/). They run as that pinned runtime, which is what the 3.1 guard in
scripts/office_runtime.py expects of a 3.0 run's own helper process.
"""
import os
from pathlib import Path

import pytest
import yaml

_SCRUBBED_PREFIXES = ("OFFICE_", "HERDR_", "AUTO_OFFICE_")


@pytest.fixture(scope="session", autouse=True)
def _libyaml_safe_load():
    """Parse YAML with libyaml for the whole session.

    Office loads the catalog, config and adapter YAML many times per command, and
    pure-Python PyYAML made that most of every `office start`. The C loader
    returns identical data.
    """
    if not getattr(yaml, "__with_libyaml__", False):
        yield
        return
    original = yaml.safe_load
    yaml.safe_load = lambda stream: yaml.load(stream, Loader=yaml.CSafeLoader)
    try:
        yield
    finally:
        yaml.safe_load = original


@pytest.fixture(autouse=True)
def _clean_office_identity(request, monkeypatch):
    """Start every test with no Office/herdr identity from the launching shell.

    A test run started by an Office worker inherits OFFICE_RUN_ID and friends,
    which make hooks and helpers resolve that run instead of the test's own.
    """
    for key in [k for k in os.environ if k.startswith(_SCRUBBED_PREFIXES)]:
        monkeypatch.delenv(key)
    if "v31" not in Path(str(request.fspath)).parts:
        monkeypatch.setenv("OFFICE_PINNED_LEGACY", "1")
    yield
