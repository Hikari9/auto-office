"""#945: closeout labels each acceptance criterion with its verification kind, lists the faked and
unverified ones together, and fails closed on an unlabeled report."""
import json
import re

import pytest

from office import closeout, db, lifecycle, state
from office.result import Result

LIVE = "verified-live"
FAKES = "verified-against-fakes"
NOT = "not-verified"
NO_CRITERIA = re.escape("acceptance cannot be reported as met without labeled criteria")
NO_LABEL = re.escape("verified-live, verified-against-fakes, not-verified); got ")


def test_each_criterion_is_labeled_with_its_verification_kind():
    lines = closeout.criteria_report([("CLI exits 0", LIVE), ("provider returns 200", FAKES), ("deploy hook fires", NOT)])
    assert lines == ["verified-live: CLI exits 0", "verified-against-fakes: provider returns 200",
                     "not-verified: deploy hook fires",
                     "not verified live: provider returns 200 (verified-against-fakes), deploy hook fires (not-verified)"]


def test_faked_and_unverified_criteria_are_listed_together_in_the_summary():
    lines = closeout.criteria_report([("a", LIVE), ("b", FAKES), ("c", NOT), ("d", FAKES)])
    assert lines[-1] == "not verified live: b (verified-against-fakes), c (not-verified), d (verified-against-fakes)"


def test_all_live_report_says_so_in_the_summary():
    assert closeout.criteria_report([("a", LIVE), ("b", LIVE)]) == [
        "verified-live: a", "verified-live: b", "all 2 acceptance criteria verified live"]


def test_criterion_text_is_cleaned_in_the_summary_too():
    lines = closeout.criteria_report([("\x1b[31mship\x1b[0m\tit؜", FAKES)])
    assert lines == ["verified-against-fakes: ship it", "not verified live: ship it (verified-against-fakes)"]


@pytest.mark.parametrize("kind", [None, "", "verified", "Verified-Live", "met", ["verified-live"],
                                  " verified-live", "verified-live ", "verified-live\n"])
def test_criterion_without_a_known_kind_is_rejected(kind):
    with pytest.raises(ValueError, match=NO_LABEL + re.escape(f"('deploy hook', {kind!r})")):
        closeout.criteria_report([("a", LIVE), ("deploy hook", kind)])


@pytest.mark.parametrize("item", [None, "a", ("a",), ("a", LIVE, "extra"), ["a", LIVE]])
def test_malformed_criterion_entry_is_rejected_as_a_value_error(item):
    with pytest.raises(ValueError, match=NO_LABEL + re.escape(repr(item))):
        closeout.criteria_report([item])


@pytest.mark.parametrize("cp", [0x00, 0x1F, 0x7F, 0x80, 0x9F, 0xAD, 0x61C, 0x180E, 0x200B, 0x200F, 0x202A, 0x202E,
                                0x2060, 0x2064, 0x2066, 0x2069, 0xFEFF, 0xFFF9, 0xFFFB, 0xE0001, 0xE007F])
def test_every_unprintable_code_point_is_stripped(cp):
    assert closeout.clean_text(f"a{chr(cp)}b") == "a b"


def test_visible_neighbours_of_the_unprintable_ranges_are_kept():
    assert closeout.clean_text("a¬b⁥c") == "a¬b⁥c"


def test_empty_report_is_rejected():
    with pytest.raises(ValueError, match=NO_CRITERIA):
        closeout.criteria_report([])


def test_empty_generator_is_rejected_like_an_empty_list():
    with pytest.raises(ValueError, match=NO_CRITERIA):
        closeout.criteria_report((item for item in []))


def test_generator_of_criteria_is_fully_reported():
    lines = closeout.criteria_report((text, LIVE) for text in ["a", "b"])
    assert lines == ["verified-live: a", "verified-live: b", "all 2 acceptance criteria verified live"]


@pytest.mark.parametrize("text", ["", "   ", None, "\x1b[0m", "\x07‮", 123, b"ship", ["ship"]])
def test_criterion_without_printable_text_is_rejected(text):
    with pytest.raises(ValueError, match=NO_LABEL):
        closeout.criteria_report([(text, LIVE)])


def test_criterion_text_is_cleaned_before_it_is_printed():
    lines = closeout.criteria_report([("\x1b[31mred\x1b[0m\tcheck‮", LIVE)])
    assert lines == ["verified-live: red check", "all 1 acceptance criteria verified live"]


# ------------------------------------------------ evidence-derived kinds (A15) and closeout wiring

RUN = {"id": "R1", "repo_root": "/nonexistent", "base_sha": None}
REV = "rev-accepted"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AUTO_OFFICE_RUNS_DB", str(tmp_path / "runs.db"))
    return tmp_path


def _task(tid, accept=("the button saves",), rev=REV, status="accepted") -> dict:
    return {"id": tid, "status": status, "accept": list(accept), "accepted_revision_id": rev, "visual": None}


def _gate(con, gid, *, task_id=None, revision=None, kind="visual", status="done", evidence=None, verdict=None,
          members=None, created_at="2026-10-31T00:00:00"):
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, kind, input_key, status, verdict, "
                "evidence_status, created_at, scope, members_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, "R1", "task" if task_id else "lane", task_id, revision, kind, gid, status, verdict, evidence,
                 created_at, None, json.dumps(members) if members is not None else None))


def _deploy_verify(con, exit_code: int) -> None:
    con.execute("INSERT INTO events(run_id, kind, audience, task_id, dispatch_id, summary, payload_json, "
                "office_version, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("R1", "land.verify", "runtime", None, None, "prod verify", json.dumps({"exit": exit_code}),
                 "3.15.1", "2026-10-31T00:00:00"))


def _report(isolated, monkeypatch, tasks, setup=lambda con: None) -> list[str]:
    con = db.connect(isolated / "runs.db")
    setup(con)
    monkeypatch.setattr(state, "tasks", lambda _con, _run_id, **_kw: tasks)
    return closeout.acceptance_report(con, RUN)


FAKES_ONLY = ["verified-against-fakes: the button saves",
              "not verified live: the button saves (verified-against-fakes)"]
LIVE_ONLY = ["verified-live: the button saves", "all 1 acceptance criteria verified live"]
NOT_RUN = ["not-verified: the button saves", "not verified live: the button saves (not-verified)"]


@pytest.mark.parametrize("name, setup, expected", [
    ("passing checks on the accepted revision", lambda con: _gate(
        con, "K1", task_id="T1", revision=REV, kind="checks", verdict="PASS"), FAKES_ONLY),
    ("failing checks on the accepted revision", lambda con: _gate(
        con, "K1", task_id="T1", revision=REV, kind="checks", verdict="CHANGES_REQUIRED"), NOT_RUN),
    ("passing checks on an older revision", lambda con: _gate(
        con, "K1", task_id="T1", revision="rev-old", kind="checks", verdict="PASS"), NOT_RUN),
    ("latest checks gate failed after an earlier pass", lambda con: (
        _gate(con, "K1", task_id="T1", revision=REV, kind="checks", verdict="PASS", created_at="2026-10-31T00:00:00"),
        _gate(con, "K2", task_id="T1", revision=REV, kind="checks", verdict="CHANGES_REQUIRED",
              created_at="2026-10-31T00:00:01")), NOT_RUN),
    ("checks still queued", lambda con: _gate(
        con, "K1", task_id="T1", revision=REV, kind="checks", status="queued"), NOT_RUN),
    ("a cancelled checks gate that had passed", lambda con: _gate(
        con, "K1", task_id="T1", revision=REV, kind="checks", status="cancelled", verdict="PASS"), NOT_RUN),
    ("a COMPARABLE visual capture of the accepted revision", lambda con: _gate(
        con, "V1", task_id="T1", revision=REV, evidence="COMPARABLE"), LIVE_ONLY),
    ("a COMPARABLE visual capture of another revision", lambda con: _gate(
        con, "V1", task_id="T1", revision="rev-old", evidence="COMPARABLE"), NOT_RUN),
    ("a visual capture that is not comparable", lambda con: _gate(
        con, "V1", task_id="T1", revision=REV, evidence="CAPTURE_BLOCKED"), NOT_RUN),
    ("a queued visual capture", lambda con: _gate(
        con, "V1", task_id="T1", revision=REV, status="queued", evidence="COMPARABLE"), NOT_RUN),
    ("a COMPARABLE lane capture that lists the task", lambda con: _gate(
        con, "L1", kind="visual", revision=None, members=["T1", "T2"], evidence="COMPARABLE"), LIVE_ONLY),
    ("a lane capture that does not list the task", lambda con: _gate(
        con, "L1", kind="visual", revision=None, members=["T2"], evidence="COMPARABLE"), NOT_RUN),
    ("a newer unattributed lane capture blocks the live label", lambda con: (
        _gate(con, "V1", task_id="T1", revision=REV, evidence="COMPARABLE", created_at="2026-10-31T00:00:00"),
        _gate(con, "L1", kind="visual", revision=None, members=None, evidence="COMPARABLE",
              created_at="2026-10-31T00:00:01")), NOT_RUN),
    ("a live capture beats passing checks", lambda con: (
        _gate(con, "K1", task_id="T1", revision=REV, kind="checks", verdict="PASS"),
        _gate(con, "V1", task_id="T1", revision=REV, evidence="COMPARABLE")), LIVE_ONLY),
    ("a passing deploy verify", lambda con: _deploy_verify(con, 0), LIVE_ONLY),
    ("a failing deploy verify", lambda con: _deploy_verify(con, 1), NOT_RUN),
    ("a deploy verify that failed after an earlier pass", lambda con: (
        _deploy_verify(con, 0), _deploy_verify(con, 1)), NOT_RUN),
    ("nothing recorded", lambda con: None, NOT_RUN),
])
def test_kind_is_derived_from_recorded_evidence(isolated, monkeypatch, name, setup, expected):
    assert _report(isolated, monkeypatch, [_task("T1")], setup) == expected, name


def test_a_task_with_no_accepted_revision_is_not_verified_whatever_was_recorded(isolated, monkeypatch):
    def setup(con):
        _gate(con, "K1", task_id="T1", revision=REV, kind="checks", verdict="PASS")
        _gate(con, "V1", task_id="T1", revision=REV, evidence="COMPARABLE")
        _deploy_verify(con, 0)
    assert _report(isolated, monkeypatch, [_task("T1", rev=None)], setup) == NOT_RUN


def test_each_task_takes_its_own_kind_and_cancelled_tasks_are_left_out(isolated, monkeypatch):
    tasks = [_task("T1", ["a works"]), _task("T2", ["b works"]), _task("T3", ["c works"], status="cancelled")]

    def setup(con):
        _gate(con, "V1", task_id="T1", revision=REV, evidence="COMPARABLE")
        _gate(con, "K2", task_id="T2", revision=REV, kind="checks", verdict="PASS")
        _gate(con, "K3", task_id="T3", revision=REV, kind="checks", verdict="PASS")
    assert _report(isolated, monkeypatch, tasks, setup) == [
        "verified-live: a works", "verified-against-fakes: b works",
        "not verified live: b works (verified-against-fakes)"]


def test_run_without_criteria_is_reported_as_not_reported(isolated, monkeypatch):
    assert _report(isolated, monkeypatch, [_task("T1", accept=())]) == [
        "acceptance not reported: acceptance cannot be reported as met without labeled criteria"]


def test_closeout_wiring_puts_each_derived_criterion_in_the_final_report(isolated, monkeypatch):
    con = db.connect(isolated / "runs.db")
    _gate(con, "K1", task_id="T1", revision=REV, kind="checks", verdict="PASS")
    monkeypatch.setattr(state, "tasks", lambda _con, _run_id, **_kw: [_task("T1")])
    monkeypatch.setattr(closeout, "mark_ready", lambda *_a, **_k: None)
    res = Result()
    lifecycle._closeout(con, dict(RUN), {}, res, handoff="https://github.com/o/r/pull/1")
    assert res.data["acceptance"] == FAKES_ONLY
    start = res.lines.index(FAKES_ONLY[0])
    assert res.lines[start:start + 2] == FAKES_ONLY
    assert res.lines[start + 2].startswith("handed off")  # the report precedes the handoff line
