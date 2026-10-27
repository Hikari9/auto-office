"""The release version is written once, in src/office/_version.py."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"src/office/_version.py", "uv.lock", "CHANGELOG.md"}


def _tracked():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if (ROOT / p).is_file()]


def test_version_string_is_not_hardcoded_elsewhere():
    from office._version import __version__
    token = re.compile(r"(?<![\w.\-])v?" + re.escape(__version__) + r"(?![\w.])")
    hits = []
    for rel in _tracked():
        if rel in ALLOWED or rel.startswith("tests/"):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if token.search(text):
            hits.append(rel)
    assert hits == [], f"hardcoded {__version__} outside src/office/_version.py: {hits}"


def test_runtime_reports_the_single_source():
    from office import version
    from office._version import __version__
    assert version.base_version() == __version__
    assert version.current().startswith(__version__)
