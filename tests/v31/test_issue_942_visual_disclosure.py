"""#942: doctor fails on an unlaunchable Chromium when visual review is enabled; closeout discloses
visual reviews that were required and did not complete with comparable evidence."""
import json
import sys
from types import ModuleType

import pytest

from office import closeout, db, doctor, lifecycle, state, visual
from office.result import Result

FAILING = ("visual capture: Chromium headless shell cannot launch (boom); fix: python -m playwright install "
           "chromium-headless-shell", True)
PASSING = ("visual capture: playwright and Chromium headless shell available", False)
ENABLED = {"gear_presets": {"L": {"funded_browser_verification": "acceptance_forced"}}}
DISABLED = {"gear_presets": {"L": {"funded_browser_verification": False}}}


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AUTO_OFFICE_RUNS_DB", str(tmp_path / "runs.db"))
    return tmp_path


def _doctor_problems(monkeypatch, capture, config) -> tuple[int, Result]:
    monkeypatch.setattr(doctor, "_visual_capture_status", lambda: capture)
    monkeypatch.setattr(doctor, "_effective_config", lambda: config)
    res = doctor.doctor()
    return res.data["problems"], res


def test_unlaunchable_chromium_is_a_doctor_failure_when_visual_review_is_enabled(isolated_state, monkeypatch):
    baseline, _ = _doctor_problems(monkeypatch, PASSING, ENABLED)
    failed, res = _doctor_problems(monkeypatch, FAILING, ENABLED)
    assert failed == baseline + 1
    assert res.exit_code == 1
    assert any("playwright install chromium-headless-shell" in line for line in res.lines)


def test_unlaunchable_chromium_is_not_a_failure_when_visual_review_is_disabled(isolated_state, monkeypatch):
    baseline, _ = _doctor_problems(monkeypatch, PASSING, DISABLED)
    failed, res = _doctor_problems(monkeypatch, FAILING, DISABLED)
    assert failed == baseline
    assert any("not a failure" in line for line in res.lines)


def _fake_playwright(monkeypatch, failure: str) -> None:
    module = ModuleType("playwright.sync_api")

    class Chromium:
        def launch(self, **_kwargs):
            raise RuntimeError(failure)

    class Context:
        def __enter__(self):
            return type("API", (), {"chromium": Chromium()})()

        def __exit__(self, *_):
            return False

    module.sync_playwright = Context
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(visual, "launch_args", lambda: {"headless": True})


def test_cannot_launch_message_names_the_fix_command(monkeypatch):
    _fake_playwright(monkeypatch, "Host is missing required shared libraries")
    message, problem = doctor._visual_capture_status()
    assert problem and "cannot launch" in message
    assert "-m playwright install chromium-headless-shell" in message
    assert "--with-deps" in message


# ----------------------------------------------------------------- closeout


def _task(tid: str, *, visual_spec=None, title="", status="done") -> dict:
    return {"id": tid, "status": status, "title": title, "accept": [], "visual": visual_spec}


def _gate(con, gid: str, *, task_id=None, members=None, status="done", evidence=None, review_status=None,
          summary=None, created_at="2026-10-31T00:00:00", kind="visual"):
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, evidence_status, "
                "review_status, summary, created_at, scope, members_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, "R1", "lane" if members else "task", task_id, kind, gid, status, evidence, review_status,
                 summary, created_at, "L1" if members else None, json.dumps(members) if members else None))


def _disclosures(monkeypatch, tmp_path, tasks, setup) -> list[str]:
    con = db.connect(tmp_path / "runs.db")
    setup(con)
    monkeypatch.setattr(state, "tasks", lambda _con, _run_id, **_kw: tasks)
    return closeout.visual_review_disclosures(con, {"id": "R1"})


def test_closeout_discloses_each_required_visual_review_that_did_not_complete(tmp_path, monkeypatch):
    tasks = [
        _task("T1", visual_spec={"url": "/a"}),                       # blocked legacy gate
        _task("T2", visual_spec={"url": "/b"}),                       # covered by a COMPARABLE lane gate
        _task("T3", title="the button saves the draft"),              # probe, no gate ever ran
        _task("T4", visual_spec={"none": True}),                      # not applicable
        _task("T5", visual_spec={"url": "/c"}),                       # covered by the same lane gate as T2
        _task("T6", visual_spec={"url": "/d"}, status="cancelled"),   # cancelled tasks are not reviewed
        _task("T7", visual_spec={"url": "/e"}),                       # legacy gate still queued
        _task("T8", visual_spec={"url": "/f"}),                       # cancelled newer gate is ignored
    ]

    def setup(con):
        _gate(con, "G1", task_id="T1", evidence="CAPTURE_BLOCKED", summary="Chromium cannot launch")
        _gate(con, "G2", members=["T2", "T5"], evidence="COMPARABLE")
        _gate(con, "G3", task_id="T7", status="queued")
        _gate(con, "G4", task_id="T8", evidence="COMPARABLE", created_at="2026-10-31T00:00:01")
        _gate(con, "G5", task_id="T8", status="cancelled", created_at="2026-10-31T00:00:02")

    assert _disclosures(monkeypatch, tmp_path, tasks, setup) == [
        "visual review skipped/degraded for T1: CAPTURE_BLOCKED (Chromium cannot launch)",
        "visual review required for T3 but never ran",
        "visual review skipped/degraded for T7: queued",
    ]


def test_closeout_discloses_nothing_when_every_required_review_is_comparable(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"})]
    lines = _disclosures(monkeypatch, tmp_path, tasks,
                         lambda con: _gate(con, "G1", task_id="T1", evidence="COMPARABLE"))
    assert lines == []


def test_closeout_result_carries_disclosures_into_final_line(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "runs.db")
    _gate(con, "G1", task_id="T1", evidence="INVALID_COMPARISON", summary="DOM only")
    monkeypatch.setattr(state, "tasks", lambda _con, _run_id, **_kw: [_task("T1", visual_spec={"url": "/a"})])
    monkeypatch.setattr(closeout, "docs_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(closeout, "finish", lambda *_a, **_k: (["cleanup"], None, "merged"))
    res = Result()
    run = {"id": "R1", "repo_root": str(tmp_path)}
    lifecycle._closeout(con, run, {"target": "main"}, res, pr="#1")
    line = "visual review skipped/degraded for T1: INVALID_COMPARISON (DOM only)"
    assert res.data["visual_review_disclosures"] == [line]
    assert line in res.lines
    assert res.final.endswith("; " + line)
