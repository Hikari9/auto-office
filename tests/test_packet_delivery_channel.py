"""A dispatch must be able to deliver its result through the channel it is told
to use.

Incident 2026-09-15 (run e6167374): a code reviewer was dispatched
`--sandbox read-only` with a brief ending "write your findings to
/tmp/office/review-findings.md and reply with only that path". It reviewed
correctly for ~16 minutes at xhigh effort and then could not deliver -- the
write was refused, and it burned further turns trying TextEdit, Terminal, an
IDE and a browser as write fallbacks. Four real defects were recovered only
because the orchestrator went and read the pane.

Nothing in the packet was malformed. The brief asked for an output channel the
sandbox forbade, and the validator had no opinion about that.
"""
import json
import os
import subprocess
import sys
import tempfile

RUNTIME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "office_runtime.py")


def validate(packet):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(packet, fh)
        path = fh.name
    r = subprocess.run([sys.executable, RUNTIME, "validate-packet", "--kind", "execution", path],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def _errors(out):
    try:
        return json.loads(out).get("errors", [])
    except Exception:
        return []


def test_file_delivery_with_no_allowed_mutations_is_rejected():
    rc, out = validate({"output": {"delivery": "file", "path": "/tmp/x.md"},
                        "allowed_mutations": []})
    assert rc == 2
    assert any("cannot deliver its result as a file" in e for e in _errors(out))


def test_file_delivery_outside_allowed_mutations_is_rejected():
    """The subtler shape: the dispatch may write, just not there."""
    rc, out = validate({"output": {"delivery": "file", "path": "/tmp/office/r.md"},
                        "allowed_mutations": ["/srv/repo/"]})
    assert rc == 2
    assert any("outside allowed_mutations" in e for e in _errors(out))


def test_reply_delivery_is_fine_under_a_read_only_dispatch():
    """The correct form of the incident's dispatch: review read-only, reply
    inline. Must not be flagged."""
    rc, out = validate({"output": {"delivery": "reply"}, "allowed_mutations": []})
    assert not any("deliver" in e for e in _errors(out))


def test_file_delivery_inside_allowed_mutations_is_fine():
    rc, out = validate({"output": {"delivery": "file", "path": "/srv/repo/out.md"},
                        "allowed_mutations": ["/srv/repo/"]})
    assert not any("deliver" in e or "outside" in e for e in _errors(out))


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "execution-packet", "accept_complete.json")


def _complete(**overrides):
    with open(FIXTURE) as fh:
        packet = json.load(fh)
    packet.update(overrides)
    return packet


def test_schema_accepts_a_complete_packet_declaring_its_delivery_channel():
    """The schema once rejected `output` outright (additionalProperties: false),
    so no real packet could declare a channel and the check above never ran."""
    rc, out = validate(_complete(output={"delivery": "file", "path": "/srv/repo/out.md"},
                                 allowed_mutations=["/srv/repo/"]))
    assert rc == 0, out


def test_complete_read_only_packet_asking_for_a_file_is_rejected_end_to_end():
    rc, out = validate(_complete(output={"delivery": "file", "path": "/tmp/x.md"},
                                 allowed_mutations=[]))
    assert rc == 2
    errors = _errors(out)
    assert any("cannot deliver its result as a file" in e for e in errors)
    assert not any("Additional properties" in e for e in errors), errors


def test_file_delivery_without_a_path_is_rejected_by_the_schema():
    rc, out = validate(_complete(output={"delivery": "file"},
                                 allowed_mutations=["/srv/repo/"]))
    assert rc == 2
    assert any(e.startswith("output") for e in _errors(out))
