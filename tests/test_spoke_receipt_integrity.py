"""A spoke receipt must not be producible for a spoke that was never located.

Incident 2026-09-15 (run e6167374): `mark-spoke` recorded a timestamp from a
spoke NAME alone, so the receipt was self-attesting -- the same agent the gate
constrains could satisfy the gate by typing the spoke's name. An orchestrator
batch-marked `auto-routing` and `auto-execution` in one shell call as a
convenience, having loaded neither, and `check-spoke` returned 0 for both. The
drift was caught by the user reading the transcript.

`--digest` cannot prove comprehension -- nothing a CLI can check does. It
proves the caller located that specific file at its current version, which
converts the failure from an accident into a deliberate act.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

RUNTIME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "office_runtime.py")
SPOKE = "auto-execution"


def run(*args):
    r = subprocess.run([sys.executable, RUNTIME, *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


@pytest.fixture()
def state_dir():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as fh:
        json.dump({"run_id": "r", "family_id": "f", "phase": "intake"}, fh)
    return d


def digest():
    rc, out = run("spoke-digest", "--spoke", SPOKE)
    assert rc == 0, out
    return json.loads(out)["digest"]


def test_a_bare_mark_is_refused(state_dir):
    """The exact call that produced the incident."""
    rc, out = run("mark-spoke", "--state-dir", state_dir, "--spoke", SPOKE)
    assert rc == 2
    assert json.loads(out)["error"] == "digest_required"


def test_a_refused_mark_leaves_the_gate_closed(state_dir):
    """Refusing the mark is only worth anything if check-spoke still fails --
    a half-written receipt would be worse than none."""
    run("mark-spoke", "--state-dir", state_dir, "--spoke", SPOKE)
    assert run("check-spoke", "--state-dir", state_dir, "--spoke", SPOKE)[0] == 2


def test_a_wrong_digest_is_refused(state_dir):
    rc, out = run("mark-spoke", "--state-dir", state_dir, "--spoke", SPOKE,
                  "--digest", "deadbeefdeadbeef")
    assert rc == 2
    assert json.loads(out)["error"] == "digest_mismatch"


def test_a_digest_from_a_different_spoke_is_refused(state_dir):
    """The likeliest real forgery is not a random string, it is a digest
    copied from the spoke you DID read."""
    other = json.loads(run("spoke-digest", "--spoke", "auto-review")[1])["digest"]
    rc, out = run("mark-spoke", "--state-dir", state_dir, "--spoke", SPOKE, "--digest", other)
    assert rc == 2
    assert json.loads(out)["error"] == "digest_mismatch"


def test_the_correct_digest_opens_the_gate(state_dir):
    assert run("mark-spoke", "--state-dir", state_dir, "--spoke", SPOKE,
               "--digest", digest())[0] == 0
    rc, out = run("check-spoke", "--state-dir", state_dir, "--spoke", SPOKE)
    assert rc == 0
    assert json.loads(out)["verified"] is True


def test_digest_is_case_insensitive_and_whitespace_tolerant(state_dir):
    """Transcribed by hand off a terminal; do not fail a correct answer on
    shell noise."""
    assert run("mark-spoke", "--state-dir", state_dir, "--spoke", SPOKE,
               "--digest", "  " + digest().upper() + "  ")[0] == 0


def test_unverified_is_permitted_but_recorded(state_dir):
    """Fail-soft and visible beats fail-closed and routed around: an unusual
    layout must not hard-block a run, but closeout has to be able to see it."""
    assert run("mark-spoke", "--state-dir", state_dir, "--spoke", SPOKE, "--unverified")[0] == 0
    rc, out = run("check-spoke", "--state-dir", state_dir, "--spoke", SPOKE)
    assert rc == 0
    assert json.loads(out)["verified"] is False


def test_a_digest_for_a_nonexistent_spoke_is_refused(state_dir):
    rc, out = run("mark-spoke", "--state-dir", state_dir, "--spoke", "auto-not-a-spoke",
                  "--digest", "0123456789abcdef")
    assert rc == 2
    assert json.loads(out)["error"] == "spoke_not_found"


def test_legacy_string_rows_still_read_as_loaded(state_dir):
    """A run resumed from state written before this change must not be
    re-gated -- that would strand in-flight runs."""
    path = os.path.join(state_dir, "state.json")
    obj = json.load(open(path, encoding="utf-8"))
    obj["spokes_loaded"] = {SPOKE: "2026-01-01T00:00:00+00:00"}
    json.dump(obj, open(path, "w", encoding="utf-8"))
    rc, out = run("check-spoke", "--state-dir", state_dir, "--spoke", SPOKE)
    assert rc == 0
    assert json.loads(out)["verified"] is False
