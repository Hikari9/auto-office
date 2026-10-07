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
REPO_ROOT = Path(__file__).resolve().parents[1]


def office_dir_snapshot(root: Path):
    """What the repository's `.office/` holds: None when absent, else every entry's size and mtime.
    A test that creates or writes it compares unequal."""
    # os-level calls only: a test may patch pathlib while this runs around it.
    base = os.path.join(root, ".office")
    try:
        os.lstat(base)
    except FileNotFoundError:
        return None
    entries = []
    for top, dirs, files in os.walk(base):
        for name in sorted([*dirs, *files]):
            try:
                st = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            entries.append((os.path.relpath(os.path.join(top, name), root), st.st_size, st.st_mtime_ns))
    return (os.lstat(base).st_mtime_ns, *sorted(entries))


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


@pytest.fixture(autouse=True)
def _real_repo_office_dir_untouched(request):
    """A test never creates or writes the real repository's `.office/` (plans, sessions, hook state).
    Office state belongs in the test's own repository and state home; a leak there is the test's bug."""
    before = office_dir_snapshot(REPO_ROOT)
    yield
    after = office_dir_snapshot(REPO_ROOT)
    if after != before:
        pytest.fail(f"{request.node.nodeid} {'created' if before is None else 'wrote'} the real repository's "
                    f"{REPO_ROOT / '.office'}; point it at a temporary repository (the `env` fixture) and a temporary "
                    "OFFICE_STATE_HOME", pytrace=False)


# Tiers: the default run is unit-only. integration, legacy and slow are
# deselected unless --all is given or the user passes their own -m expression.
_NON_UNIT = ("integration", "legacy", "slow")


def pytest_addoption(parser):
    parser.addoption("--all", action="store_true", default=False,
                     help="run every test: disable the default deselection of integration, legacy and slow")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "env" in item.fixturenames:
            item.add_marker(pytest.mark.integration)
    if config.getoption("--all") or config.getoption("markexpr"):
        return
    kept, deselected = [], []
    for item in items:
        (deselected if any(item.get_closest_marker(m) for m in _NON_UNIT) else kept).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
