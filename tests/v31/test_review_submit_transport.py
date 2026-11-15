"""T6: `office review submit` is the one reviewer completion transport; every fallback is recorded."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from office import adapters, gates, review_submit, state
from test_issue_1030_review_submit import reviewer  # noqa: F401  (fixture)

OUT = Path("/tmp/office-t6/D1/reply.txt")
GOOD = "VERDICT: PASS"


def test_pointer_leads_with_submit_and_names_the_file_fallback():
    p = review_submit.pointer(OUT, prompt_file="/x/brief.md")
    assert p.index(review_submit.submit_command(OUT)) < p.index(f"write the complete review to {OUT} instead")
    assert "cannot write files" in p


def test_every_reviewer_profile_declares_its_transport_and_no_shell_list_matches():
    no_shell = set()
    for name, a in adapters.load_all().items():
        for kind in ("reviewer", "vision"):
            prof = adapters.profile(a, kind)
            if not prof:
                continue
            assert prof.get("submit") in ("shell", "file"), (name, kind)
            if prof["submit"] == "shell":  # a shell transport needs the grant that lets the one command run
                assert prof.get("submit_grant") or "{submit_allow}" in " ".join(map(str, prof["argv"])), (name, kind)
            elif kind == "reviewer":
                no_shell.add(name)
    assert no_shell == set(review_submit.NO_SHELL_HARNESSES)


def test_record_fallback_writes_one_event_per_dispatch(reviewer):
    con, run, sha, out, _ = reviewer
    d = state.get_dispatch(con, "Dsub1")
    review_submit.record_fallback(d, "reply_file")
    review_submit.record_fallback(d, "stdout_log")
    rows = _events(con, review_submit.FALLBACK_EVENT)
    assert len(rows) == 1 and json.loads(rows[0][0])["source"] == "reply_file"


def _events(con, kind):
    return con.execute("SELECT payload_json FROM events WHERE kind=?", (kind,)).fetchall()


def test_submitted_reply_is_not_a_fallback(tmp_path, monkeypatch):
    out = tmp_path / "reply.txt"
    out.write_text(GOOD)
    (tmp_path / review_submit.SUBMITTED_FILE).write_text("{}")
    calls = []
    monkeypatch.setattr(review_submit, "record_fallback", lambda d, s: calls.append(s))
    assert gates._reply_text({"launcher": "process", "id": "D1"}, tmp_path, out) == GOOD
    assert calls == []


@pytest.mark.parametrize("setup,source", [("file", "reply_file"), ("last", "last_message"), ("log", "stdout_log")])
def test_each_fallback_is_recorded_with_its_source(tmp_path, monkeypatch, setup, source):
    out = tmp_path / "reply.txt"
    if setup == "file":
        out.write_text(GOOD)
    elif setup == "last":
        (tmp_path / "last-message.txt").write_text(GOOD)
    else:
        (tmp_path / "output.log").write_text(GOOD)
    calls = []
    monkeypatch.setattr(review_submit, "record_fallback", lambda d, s: calls.append(s))
    assert gates._reply_text({"launcher": "process", "id": "D1"}, tmp_path, out) == GOOD
    assert calls == [source]


def test_no_reply_records_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(review_submit, "record_fallback", lambda d, s: calls.append(s))
    assert gates._reply_text({"launcher": "herdr", "id": "D1"}, tmp_path, tmp_path / "reply.txt") == ""
    assert calls == []
