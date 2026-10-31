"""#942: doctor fails on an unlaunchable Chromium when visual review is enabled; closeout discloses
visual reviews that were required and did not complete with comparable evidence."""
import json
import sys
from types import ModuleType

import pytest

from office import closeout, config as cfg, db, doctor, lifecycle, state
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


def test_unreadable_config_fails_closed_so_the_launch_failure_counts(isolated_state, monkeypatch):
    baseline, _ = _doctor_problems(monkeypatch, PASSING, None)
    failed, res = _doctor_problems(monkeypatch, FAILING, None)
    assert failed == baseline + 1
    assert f"{FAILING[0]} (config unreadable; visual review assumed enabled)" in res.lines


def test_default_config_enables_visual_review(isolated_state, monkeypatch):
    empty = isolated_state / "empty.yaml"
    empty.write_text("")
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(empty))
    monkeypatch.chdir(isolated_state)  # outside any repository: only the packaged defaults and the empty user file
    assert doctor._visual_review_enabled(doctor._effective_config()) is True


def test_unreadable_user_config_yields_no_config(isolated_state, monkeypatch):
    user = isolated_state / "user.yaml"
    user.write_text("gear_presets: [\n")
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    assert doctor._effective_config() is None


def test_user_config_that_cannot_be_read_yields_no_config(isolated_state, monkeypatch):
    def unreadable(*_args, **_kwargs):
        raise PermissionError("EACCES: user config")

    monkeypatch.setattr(cfg, "read_files", unreadable)
    assert doctor._effective_config() is None


def test_config_resolve_error_yields_no_config(isolated_state, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise ValueError("review.contract must be one of v3.1, convergence-v1")

    monkeypatch.setattr(cfg, "resolve", refuse)
    assert doctor._effective_config() is None


@pytest.mark.parametrize("config", [
    {},
    {"gear_presets": None},
    {"gear_presets": {"L": None}},
    {"gear_presets": {"L": "yes"}},
    {"gear_presets": {"L": {"funded_browser_verification": False}}},
    {"gear_presets": {"L": {"funded_browser_verification": False}, "XL": {"funded_browser_verification": False}}},
])
def test_visual_review_disabled_when_no_preset_funds_browser_verification(config):
    assert doctor._visual_review_enabled(config) is False


@pytest.mark.parametrize("config", [{"gear_presets": ["L"]}, {"gear_presets": "L"}, ["not", "a", "mapping"]])
def test_malformed_config_shape_fails_closed(config):
    assert doctor._visual_review_enabled(config) is True


@pytest.mark.parametrize("presets", [
    {"L": {"funded_browser_verification": False}, "S": {"funded_browser_verification": "acceptance_forced"}},
    {"S": {"funded_browser_verification": "acceptance_forced"}, "L": {"funded_browser_verification": False}},
])
def test_visual_review_enabled_when_any_one_preset_funds_it(presets):
    assert doctor._visual_review_enabled({"gear_presets": presets}) is True


@pytest.mark.parametrize("value", [True, "risk_forced", "acceptance_forced"])
def test_every_funding_value_other_than_false_enables_visual_review(value):
    assert doctor._visual_review_enabled({"gear_presets": {"XL": {"funded_browser_verification": value}}}) is True


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
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    message, problem = doctor._visual_capture_status()
    assert problem and "headless shell missing" in message
    assert "-m playwright install chromium-headless-shell" in message
    assert "--with-deps" not in message


def test_linux_fix_includes_with_deps_as_a_runnable_flag(monkeypatch):
    _fake_playwright(monkeypatch, "Host is missing required shared libraries")
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    message, problem = doctor._visual_capture_status()
    assert problem and "cannot launch" in message
    assert "-m playwright install --with-deps chromium-headless-shell" in message


def test_first_non_blank_line_of_the_launch_error_is_the_reason(monkeypatch):
    _fake_playwright(monkeypatch, "\n  Host is missing required shared libraries\nmore detail")
    message, problem = doctor._visual_capture_status()
    assert problem and "(Host is missing required shared libraries)" in message


def test_launch_error_reason_is_stripped_of_terminal_sequences(monkeypatch):
    _fake_playwright(monkeypatch, "\x1b[31mHost is missing shared libraries\x1b[0m")
    message, problem = doctor._visual_capture_status()
    assert problem and "(Host is missing shared libraries)" in message
    assert "\x1b" not in message and "\x9b" not in message


def test_successful_launch_is_available_and_not_a_problem(monkeypatch):
    calls = []
    module = ModuleType("playwright.sync_api")

    class Browser:
        def close(self):
            calls.append("close")

    class Chromium:
        def launch(self, **kwargs):
            calls.append(("launch", kwargs))
            return Browser()

    class Context:
        def __enter__(self):
            return type("API", (), {"chromium": Chromium()})()

        def __exit__(self, *_):
            return False

    module.sync_playwright = Context
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(visual, "launch_args", lambda: {"headless": True})
    message, problem = doctor._visual_capture_status()
    assert problem is False and "available" in message
    assert calls == [("launch", {"headless": True, "timeout": 10000}), "close"]


def test_fix_command_quotes_an_interpreter_path_with_spaces(monkeypatch):
    _fake_playwright(monkeypatch, "Host is missing required shared libraries")
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor.sys, "executable", "/opt/my python/bin/python3")
    message, _ = doctor._visual_capture_status()
    assert "fix: '/opt/my python/bin/python3' -m playwright install chromium-headless-shell" in message


def test_long_launch_error_is_truncated_and_a_blank_one_falls_back_to_the_exception_name(monkeypatch):
    _fake_playwright(monkeypatch, "x" * 300)
    message, _ = doctor._visual_capture_status()
    assert f"({'x' * 240})" in message
    _fake_playwright(monkeypatch, "   ")
    message, _ = doctor._visual_capture_status()
    assert "(RuntimeError)" in message


def test_repo_tier_config_is_resolved_from_the_repository_root(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(doctor.paths, "repo_identity", lambda: (tmp_path, "main"))
    monkeypatch.setattr(cfg, "resolve", lambda root, *a, **k: (seen.append(root) or {}, []))
    doctor._effective_config()
    assert seen == [tmp_path]


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
          review_status=None, summary=None, created_at="2026-10-31T00:00:00", kind="visual", run_id="R1"):
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, evidence_status, "
                "review_status, summary, created_at, scope, members_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, run_id, "task" if task_id else "lane", task_id, kind, gid, status, evidence, review_status,
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


def test_latest_visual_gate_decides_when_insertion_order_differs_from_creation_order(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"})]

    def newer_first_inserted(con):
        _gate(con, "G-new", task_id="T1", evidence="COMPARABLE", created_at="2026-10-31T00:00:01")
        _gate(con, "G-old", task_id="T1", evidence="CAPTURE_BLOCKED", created_at="2026-10-31T00:00:00")

    assert _disclosures(monkeypatch, tmp_path, tasks, newer_first_inserted) == []


def test_latest_visual_gate_that_is_blocked_is_disclosed_even_after_an_older_comparable_one(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"})]

    def older_first_inserted(con):
        _gate(con, "G-old", task_id="T1", evidence="COMPARABLE", created_at="2026-10-31T00:00:00")
        _gate(con, "G-new", task_id="T1", evidence="CAPTURE_BLOCKED", created_at="2026-10-31T00:00:01")

    assert _disclosures(monkeypatch, tmp_path, tasks, older_first_inserted) == [
        "visual review skipped/degraded for T1: CAPTURE_BLOCKED"]


def test_closeout_discloses_a_review_still_running(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", status="running", evidence="COMPARABLE"))
    assert lines == ["visual review skipped/degraded for T1: running"]


def test_closeout_reason_falls_back_to_review_status_then_to_no_evidence(tmp_path, monkeypatch):
    with_review = _disclosures(monkeypatch, tmp_path / "a", [_task("T1", visual_spec={"url": "/a"})],
                               lambda con: _gate(con, "G1", task_id="T1", evidence=None, review_status="UNAVAILABLE"))
    assert with_review == ["visual review skipped/degraded for T1: UNAVAILABLE"]
    (tmp_path / "b").mkdir()
    bare = _disclosures(monkeypatch, tmp_path / "b", [_task("T1", visual_spec={"url": "/a"})],
                        lambda con: _gate(con, "G1", task_id="T1", evidence=None))
    assert bare == ["visual review skipped/degraded for T1: no comparable evidence"]


def test_closeout_accepts_not_applicable_evidence(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence="NOT_APPLICABLE"))
    assert lines == []


def test_closeout_truncates_the_summary_after_collapsing_whitespace(tmp_path, monkeypatch):
    summary = "a" * 170 + "  " + "b" * 50  # collapsed first: 170 a, one space, 50 b
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence="CAPTURE_BLOCKED", summary=summary))
    assert lines == [f"visual review skipped/degraded for T1: CAPTURE_BLOCKED ({'a' * 170} {'b' * 9})"]


def test_whitespace_only_summary_adds_no_empty_detail(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence="CAPTURE_BLOCKED", summary=" \n  "))
    assert lines == ["visual review skipped/degraded for T1: CAPTURE_BLOCKED"]


def test_control_characters_in_a_summary_do_not_reach_the_report(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence="CAPTURE_BLOCKED",
                                           summary="\x1b[2Kcleared\x07 now"))
    assert lines == ["visual review skipped/degraded for T1: CAPTURE_BLOCKED (cleared now)"]


def test_c1_and_bidi_characters_in_a_summary_do_not_reach_the_report(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence="CAPTURE_BLOCKED",
                                           summary="ok\x9b31m‮reversed​ end"))
    assert lines == ["visual review skipped/degraded for T1: CAPTURE_BLOCKED (ok 31m reversed end)"]


def test_lane_gate_with_no_recorded_membership_makes_its_required_reviews_unverifiable(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"}), _task("T2", visual_spec={"url": "/b"})]

    def legacy_lane(con):
        _gate(con, "G-legacy", scope="L1", evidence="COMPARABLE", created_at="2026-10-31T00:00:00")
        _gate(con, "G2", task_id="T2", evidence="COMPARABLE", created_at="2026-10-31T00:00:01")

    assert _disclosures(monkeypatch, tmp_path, tasks, legacy_lane) == [
        "visual review for T1 unverifiable: gate G-legacy has no recorded membership"]


def test_newer_unattributed_gate_makes_an_older_covering_gate_unverifiable(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"})]

    def done_then_legacy_failure(con):
        _gate(con, "G1", task_id="T1", evidence="COMPARABLE", created_at="2026-10-31T00:00:00")
        _gate(con, "G-legacy", scope="L1", evidence="CAPTURE_BLOCKED", created_at="2026-10-31T00:00:01")

    assert _disclosures(monkeypatch, tmp_path, tasks, done_then_legacy_failure) == [
        "visual review for T1 unverifiable: gate G-legacy has no recorded membership"]


def test_older_unattributed_gate_does_not_override_a_newer_covering_gate(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"})]

    def legacy_then_done(con):
        _gate(con, "G-legacy", scope="L1", evidence="CAPTURE_BLOCKED", created_at="2026-10-31T00:00:00")
        _gate(con, "G1", task_id="T1", evidence="COMPARABLE", created_at="2026-10-31T00:00:01")

    assert _disclosures(monkeypatch, tmp_path, tasks, legacy_then_done) == []


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


def test_gates_of_another_run_or_another_kind_are_not_visual_evidence(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"}), _task("T2", visual_spec={"url": "/b"})]

    def setup(con):
        _gate(con, "G-other-run", task_id="T1", evidence="COMPARABLE", run_id="R2")
        _gate(con, "G-review", task_id="T2", kind="code_review", evidence="COMPARABLE")

    assert _disclosures(monkeypatch, tmp_path, tasks, setup) == [
        "visual review required for T1 but never ran",
        "visual review required for T2 but never ran",
    ]


def test_lane_and_task_gates_compete_on_creation_order(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"})]

    def blocked_task_then_comparable_lane(con):
        _gate(con, "G-task", task_id="T1", evidence="CAPTURE_BLOCKED", created_at="2026-10-31T00:00:00")
        _gate(con, "G-lane", members=["T1"], scope="L1", evidence="COMPARABLE", created_at="2026-10-31T00:00:01")

    def comparable_task_then_blocked_lane(con):
        _gate(con, "G-task", task_id="T1", evidence="COMPARABLE", created_at="2026-10-31T00:00:00")
        _gate(con, "G-lane", members=["T1"], scope="L1", evidence="CAPTURE_BLOCKED", created_at="2026-10-31T00:00:01")

    assert _disclosures(monkeypatch, tmp_path / "a", tasks, blocked_task_then_comparable_lane) == []
    (tmp_path / "b").mkdir()
    assert _disclosures(monkeypatch, tmp_path / "b", tasks, comparable_task_then_blocked_lane) == [
        "visual review skipped/degraded for T1: CAPTURE_BLOCKED"]


def test_gates_with_the_same_creation_time_are_ordered_by_id(tmp_path, monkeypatch):
    tasks = [_task("T1", visual_spec={"url": "/a"})]

    def id_order_decides(con):
        # Inserted in reverse id order, so insertion order would pick the blocked gate.
        _gate(con, "G-b", task_id="T1", evidence="COMPARABLE", created_at="2026-10-31T00:00:00")
        _gate(con, "G-a", task_id="T1", evidence="CAPTURE_BLOCKED", created_at="2026-10-31T00:00:00")

    assert _disclosures(monkeypatch, tmp_path, tasks, id_order_decides) == []


def test_evidence_status_is_the_reason_when_review_status_is_also_set(tmp_path, monkeypatch):
    lines = _disclosures(monkeypatch, tmp_path, [_task("T1", visual_spec={"url": "/a"})],
                         lambda con: _gate(con, "G1", task_id="T1", evidence="INVALID_COMPARISON",
                                           review_status="UNAVAILABLE"))
    assert lines == ["visual review skipped/degraded for T1: INVALID_COMPARISON"]
