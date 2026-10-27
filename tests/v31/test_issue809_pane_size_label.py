"""R23: every task-scoped Herdr pane label carries the task size before the model tag."""
import pytest

from office import db, dispatch, pane_labels, state

RUN = {"id": "r1", "repo_root": "/work/auto-office"}
EXECUTOR = {"id": "D1", "role": "executor", "task_id": "T1", "harness": "claude",
            "model": "claude-sonnet-5-5", "effort": "high"}
REVIEWER = {**EXECUTOR, "id": "D2", "role": "code_reviewer"}


class _Con:
    def close(self):
        pass

    def execute(self, *args):
        raise AssertionError("a task-scoped label reads no gate rows")


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda *a, **k: _Con())


def _tasks(monkeypatch, size, *, pr=None):
    descriptor = {"task_size": size} if size else {}
    task = {"title": "#809 PR-A gears", "pr": pr or {}, "descriptor": descriptor}
    monkeypatch.setattr(state, "get_task", lambda con, rid, tid: task if tid == "T1" else None)


@pytest.mark.parametrize("size", ["XS", "S", "M", "L", "XL"])
def test_executor_label_shows_each_size_before_model_tag(monkeypatch, size):
    _tasks(monkeypatch, size)
    assert dispatch.pane_label(RUN, EXECUTOR) == f"auto-office T1 #809 PR-A gears [{size}] [sonnet high]"


@pytest.mark.parametrize("size", ["XS", "S", "M", "L", "XL"])
def test_reviewer_label_shows_each_size_after_role_and_before_model_tag(monkeypatch, size):
    _tasks(monkeypatch, size, pr={"number": 7})
    assert dispatch.pane_label(RUN, REVIEWER) == f"auto-office T1 #809 PR-A gears review [{size}] [sonnet high]"


@pytest.mark.parametrize("role", ["executor", "code_reviewer"])
def test_unsized_task_shows_no_size_tag(monkeypatch, role):
    _tasks(monkeypatch, None)
    label = dispatch.pane_label(RUN, {**EXECUTOR, "role": role})
    assert label.endswith("[sonnet high]") and not any(f"[{s}]" in label for s in pane_labels.SIZES)


def test_invalid_size_is_ignored_and_lowercase_is_normalised(monkeypatch):
    _tasks(monkeypatch, "huge")
    assert "[HUGE]" not in dispatch.pane_label(RUN, EXECUTOR)
    assert pane_labels.size_tag("xl") == "[XL]"
    assert pane_labels.size_tag(None) == ""


def test_plan_and_integration_panes_have_no_task_so_no_size_tag():
    label = dispatch.pane_label(RUN, {"id": "D3", "role": "plan_reviewer", "harness": "claude",
                                      "model": "claude-opus-5-5", "effort": "high"})
    assert label == "auto-office plan review [opus high]"


def test_size_survives_a_task_pr_known_after_launch(monkeypatch):
    _tasks(monkeypatch, "L", pr={"number": 12})
    assert dispatch.pane_label(RUN, EXECUTOR) == "auto-office T1 #809 PR-A gears [L] [sonnet high]"
