"""Exercise the pre-push guard with Git's actual ref-update input."""
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("case, expected", [
    ("head", 0), ("different", 2), ("multiple", 2),
    ("delete", 0), ("main", 1), ("skip", 0),
])
def test_pre_push_ref_guard(tmp_path, case, expected):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "--allow-empty", "-qm", "base"], check=True)
    sha = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    zero = "0" * 40
    (tmp_path / "scripts").mkdir()
    marker = tmp_path / "validated"
    script = tmp_path / "scripts/validate.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    script.chmod(0o755)
    updates = {
        "head": f"refs/heads/topic {sha} refs/heads/topic {zero}\n",
        "different": f"refs/heads/topic {'1' * 40} refs/heads/topic {zero}\n",
        "multiple": f"refs/heads/a {sha} refs/heads/a {zero}\nrefs/heads/b {sha} refs/heads/b {zero}\n",
        "delete": f"(delete) {zero} refs/heads/topic {sha}\n",
        "main": f"refs/heads/main {sha} refs/heads/main {zero}\n",
        "skip": f"refs/heads/topic {sha} refs/heads/topic {zero}\n",
    }
    environ = {k: v for k, v in os.environ.items() if k not in ("SKIP_VALIDATE", "BYPASS_PR")}
    if case == "skip":
        environ["SKIP_VALIDATE"] = "1"
    hook = Path(__file__).resolve().parents[1] / ".githooks/pre-push"
    proc = subprocess.run(["bash", str(hook)], cwd=tmp_path, input=updates[case],
                          text=True, capture_output=True, env=environ)
    assert proc.returncode == expected, proc.stdout + proc.stderr
    assert marker.exists() == (case == "head")
