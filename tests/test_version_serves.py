"""#843: which runtime lines may serve a pinned run. Compatible MINOR/PATCH bumps do; a MAJOR does not."""
import pytest

from office import version


@pytest.mark.parametrize("pinned, runtime, ok", [
    ("3.6", "3.6.6", True), ("3.6.1", "3.6.6", True), ("3.6", "3.7.0", True), ("3.1", "3.9.2", True),
    ("3.7", "3.6.6", False), ("3.6.6", "3.6.1", True), ("3.9", "4.0.0", False), ("4.0", "3.9.9", False),
    ("4.0", "4.1.0", True), ("4.2", "4.1.0", False), ("", "3.7.0", False), (None, "3.7.0", False),
])
def test_serves_matrix(pinned, runtime, ok):
    assert version.serves(pinned, runtime) is ok


def test_a_breaking_minor_needs_an_explicit_upgrade(monkeypatch):
    monkeypatch.setattr(version, "BREAKING_LINES", frozenset({"3.8"}))
    assert version.serves("3.6", "3.7.0") is True
    assert version.serves("3.6", "3.8.0") is False and version.serves("3.7", "3.9.0") is False
    assert version.serves("3.8", "3.9.0") is True


def test_compatible_pins_share_a_major():
    assert version.compatible("3.6", "3.7.0") and version.compatible("3.7.0", "3.6")
    assert not version.compatible("3.9", "4.0.0")
