"""#942: doctor fails on an unlaunchable Chromium when visual review is enabled; closeout discloses
visual reviews that were required and did not complete with comparable evidence."""
import json
import sys
from types import ModuleType

import pytest

from office import closeout, config as cfg, convergence, db, doctor, lifecycle, state
from office import visual
from office.result import Result

FAILING = ("visual capture: Chromium headless shell cannot launch (boom); fix: python -m playwright install "
           "chromium-headless-shell", True)
PASSING = ("visual capture: playwright and Chromium headless shell available", False)
ENABLED = {"gear_presets": {"L": {"funded_browser_verification": "acceptance_forced"}}}
DISABLED = {"gear_presets": {"L": {"funded_browser_verification": False}}}


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
    assert FAILING[0] in res.lines


def test_unlaunchable_chromium_is_not_a_failure_when_visual_review_is_disabled(isolated_state, monkeypatch):
    baseline, _ = _doctor_problems(monkeypatch, PASSING, DISABLED)
    failed, res = _doctor_problems(monkeypatch, FAILING, DISABLED)
    assert failed == baseline
    assert f"{FAILING[0]} (visual review is not enabled; not a failure)" in res.lines


def test_default_config_enables_visual_review(isolated_state, monkeypatch):
    assert doctor._visual_review_enabled(doctor._effective_config()) is True


def test_unreadable_user_config_yields_no_config_instead_of_a_crash(isolated_state, monkeypatch):
    user = isolated_state / "user.yaml"
    user.write_text("gear_presets: [\n")
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    assert doctor._effective_config() == {}


def test_config_resolve_error_yields_no_config(isolated_state, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise ValueError("review.contract must be one of v3.1, convergence-v1")

    monkeypatch.setattr(cfg, "resolve", refuse)
    assert doctor._effective_config() == {}


@pytest.mark.parametrize("config", [
    {},
    {"gear_presets": None},
    {"gear_presets": {"L": None}},
    {"gear_presets": {"L": "yes"}},
    {"gear_presets": {"L": {"funded_browser_verification": False}}},
])
def test_visual_review_enabled_needs_a_preset_that_funds_browser_verification(config):
    assert doctor._visual_review_enabled(config) is False


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


def test_missing_headless_shell_is_a_problem_and_names_the_install_fix(monkeypatch):
    _fake_playwright(monkeypatch, "BrowserType.launch: Executable doesn't exist at /cache/chrome-headless-shell")
    message, problem = doctor._visual_capture_status()
    assert problem and "headless shell missing" in message
    assert "-m playwright install chromium-headless-shell" in message
    assert "--with-deps" in message


def test_other_launch_failure_is_a_problem_and_names_the_fix(monkeypatch):
    _fake_playwright(monkeypatch, "Host is missing required shared libraries")
    message, problem = doctor._visual_capture_status()
    assert problem and "cannot launch" in message and "shared libraries" in message
    assert "-m playwright install chromium-headless-shell" in message


def test_missing_optional_playwright_package_is_not_a_problem(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    message, problem = doctor._visual_capture_status()
    assert problem is False and "playwright missing" in message


def test_failed_custom_chrome_names_that_binary_not_the_install_command(monkeypatch):
    _fake_playwright(monkeypatch, "Executable doesn't exist at /opt/chrome")
    monkeypatch.setattr(visual, "launch_args", lambda: {"headless": True, "executable_path": "/opt/chrome"})
    message, problem = doctor._visual_capture_status()
    assert problem and "/opt/chrome" in message and "OFFICE_CHROME" in message
    assert "playwright install" not in message


# ----------------------------------------------------------------- closeout


def _task(tid: str, *, visual_spec=None, title="", status="done") -> dict:
    return {"id": tid, "status": status, "title": title, "accept": [], "visual": visual_spec}


def _gate(con, gid: str, *, task_id=None, members=None, scope=None, status="done", evidence=None,
          review_status=None, summary=None, created_at="2026-10-31T00:00:00", kind="visual"):
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, evidence_status, "
                "review_status, summary, created_at, scope, members_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, "R1", "task" if task_id else "lane", task_id, kind, gid, status, evidence, review_status,
                 summary, created_at, scope, json.dumps(members) if members is not None else None))


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
        _gate(con, "G1", task_id="T1", evidence="CAPTURE_BLOCKED", summary="Chromium cannot\nlaunch")
        _gate(con, "G2", members=["T2", "T5"], scope="L1", evidence="COMPARABLE")
        _gate(con, "G3", task_id="T7", status="queued")
        _gate(con, "G4", task_id="T8", evidence="COMPARABLE", created_at="2026-10-31T00:00:01")
        _gate(con, "G5", task_id="T8", status="cancelled", created_at="2026-10-31T00:00:02")

    assert _disclosures(monkeypatch, tmp_path, tasks, setup) == [
        "visual review skipped/degraded for T1: CAPTURE_BLOCKED (Chromium cannot launch)",  # newlines collapsed
        "visual review required for T3 but never ran",
        "visual review skipped/degraded for T7: queued",
    ]


def test_closeout_discloses_a_review_still_running(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", status="running", evidence="COMPARABLE"))
    assert lines == ["visual review skipped/degraded for T1: running"]


def test_closeout_reason_falls_back_to_review_status_when_evidence_is_absent(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence=None, review_status="UNAVAILABLE"))
    assert lines == ["visual review skipped/degraded for T1: UNAVAILABLE"]


def test_closeout_accepts_not_applicable_evidence(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence="NOT_APPLICABLE"))
    assert lines == []


def test_closeout_truncates_a_long_gate_summary(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence="CAPTURE_BLOCKED", summary="x" * 200))
    assert lines == [f"visual review skipped/degraded for T1: CAPTURE_BLOCKED ({'x' * 180})"]


def test_lane_gate_without_membership_covers_its_scopes_current_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(convergence, "find_scope",
                        lambda _con, _run, scope_id: {"id": scope_id, "tasks": ["T1"]} if scope_id == "L1" else None)
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", scope="L1", evidence="COMPARABLE"))
    assert lines == []


def test_lane_gate_without_membership_or_known_scope_covers_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(convergence, "find_scope", lambda *_args: None)
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", scope="L9", evidence="COMPARABLE"))
    assert lines == ["visual review required for T1 but never ran"]


@pytest.mark.parametrize("path", ["merged", "handoff", "unmerged"])
def test_every_closeout_path_puts_disclosures_in_the_final_line(tmp_path, monkeypatch, path):
    con = db.connect(tmp_path / "runs.db")
    _gate(con, "G1", task_id="T1", evidence="INVALID_COMPARISON", summary="DOM only")
    _gate(con, "G2", task_id="T2", status="queued")
    monkeypatch.setattr(state, "tasks", lambda _con, _run_id, **_kw: [
        _task("T1", visual_spec={"url": "/a"}), _task("T2", visual_spec={"url": "/b"})])
    monkeypatch.setattr(closeout, "docs_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(closeout, "mark_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(closeout, "finish", lambda *_a, **_k: (["cleanup"], None, "merged"))
    kwargs = {"merged": {"pr": "#1"}, "handoff": {"handoff": "#1"}, "unmerged": {}}[path]
    landing = {} if path == "unmerged" else {"target": "main"}
    res = Result()
    lifecycle._closeout(con, {"id": "R1", "repo_root": str(tmp_path)}, landing, res, **kwargs)
    lines = ["visual review skipped/degraded for T1: INVALID_COMPARISON (DOM only)",
             "visual review skipped/degraded for T2: queued"]
    assert res.data["visual_review_disclosures"] == lines
    assert all(line in res.lines for line in lines)
    assert res.final.endswith("; " + "; ".join(lines))


def test_closeout_without_disclosures_leaves_the_final_line_unchanged(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "runs.db")
    monkeypatch.setattr(state, "tasks", lambda _con, _run_id, **_kw: [_task("T1")])
    monkeypatch.setattr(closeout, "docs_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(closeout, "finish", lambda *_a, **_k: ([], None, "merged"))
    res = Result()
    lifecycle._closeout(con, {"id": "R1", "repo_root": str(tmp_path)}, {"target": "main"}, res, pr="#1")
    assert "visual_review_disclosures" not in res.data
    assert res.final == closeout.done("merged")
