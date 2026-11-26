"""`office route policy --effective` renders Rico's AA v5 table: each executor size's slate in rung order and each
reviewer role's preferred_seed order. Literal expectations, not derived from the YAML."""
import subprocess

import pytest

from office import adapters, routecmd

EXECUTOR_SLATES = {
    "XS": "1 claude/claude-haiku-5-5@high > 2 codex/gpt-6.1-terra@low > 3 agy/gemini-4-hydrogen@low (conditional, not "
          "dispatching without the Hydrogen gate (hydrogen_trials))",
    "S": "1 claude/claude-sonnet-5-5@medium > 2 codex/gpt-6.1-terra@medium > 3 agy/gemini-4-hydrogen@medium (conditional, not "
         "dispatching without the Hydrogen gate (hydrogen_trials))",
    "M": "1 claude/claude-sonnet-5-5@high > 2 agy/gemini-4-hydrogen@high (conditional, not dispatching without the Hydrogen "
         "gate (hydrogen_trials)) > 3 codex/gpt-6.1-terra@high",
    "L": "1 claude/claude-sonnet-5-5@high > 2 hermes/claude-sonnet-5-5@high > 3 agy/claude-sonnet-5-5@high > 4 codex/gpt-6.1-terra@high",
    "XL": "1 claude/claude-opus-5-5@high > 2 codex/gpt-6.2-sol@high > 3 agy/gemini-4-neon@high",
}
HYDROGEN_VISUAL = ("agy/gemini-4-hydrogen@low (conditional, not dispatching without the user's exact allow: "
                   "office route allow agy/gemini-4-hydrogen@low --role visual_reviewer)")


@pytest.fixture
def fresh_install(tmp_path, monkeypatch):
    """No user config: the shipped policy alone, as on a fresh install."""
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "my-config.yaml"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    real = adapters.route_version
    monkeypatch.setattr(adapters, "route_version", lambda h, a, s=None: real(h, a, s))


def lines_with(result, text):
    return [line for line in result.lines if text in line]


def test_every_executor_size_slate_renders_in_rung_order(fresh_install):
    result = routecmd.policy(role="executor", effective=True)
    for size, slate in EXECUTOR_SLATES.items():
        [line] = lines_with(result, f"executor slate {size}: ")
        assert line.startswith(f"  executor slate {size}: {slate}; ")
        assert "frontend-heavy task moves its agy gemini-* rung to rung 1" in line
    assert [row["rung"] for row in result.data["executor_slates"]["L"]] == [1, 2, 3, 4]
    assert [row["rung"] for row in result.data["executor_slates"]["XL"]] == [1, 2, 3]


def test_size_option_renders_only_that_slate(fresh_install):
    result = routecmd.policy(role="executor", effective=True, size="L")
    assert len(lines_with(result, "executor slate ")) == 1
    assert lines_with(result, "executor slate L: ")


def test_reviewer_seed_orders_match_the_table(fresh_install):
    result = routecmd.policy(effective=True)
    assert lines_with(result, "  code_reviewer preferred_seed: ") == [
        "  code_reviewer preferred_seed: codex/luna@high > claude/opus@low"]
    [visual] = lines_with(result, "  visual_reviewer preferred_seed: ")
    assert visual == ("  visual_reviewer preferred_seed: " + HYDROGEN_VISUAL
                      + " > codex/gpt-6.1-terra@low > claude/claude-opus-5-5@low")
    [plan] = lines_with(result, "  plan_reviewer preferred_seed: ")
    # Unchanged from main: Hydrogen and the other reviewers are not in the shipped plan_reviewer seed.
    assert plan == ("  plan_reviewer preferred_seed: codex/luna@high > opencode/deepseek-v4.1-flash-free@max"
                    " > claude/opus@low > agy/claude-opus-5-5@low")


def test_hydrogen_visual_rung_is_conditional_and_not_dispatching_without_the_user_allow(fresh_install):
    result = routecmd.policy(role="visual_reviewer", effective=True)
    [row] = [r for r in result.data["effective"] if r["route"] == "agy/gemini-4-hydrogen@low"]
    assert row["dispatches"] is False
    assert "user's exact allow of agy/gemini-4-hydrogen@low for visual_reviewer" in row["dispatch_reason"]
    assert result.data["preferred_seed"]["visual_reviewer"][0] == "agy/gemini-4-hydrogen@low"


def test_a_seed_entry_without_an_effort_renders_without_one(fresh_install):
    """office config roles.<role>.preferred_seed luna,opus stores entries with no effort."""
    cfg = {"roles": {"code_reviewer": {"preferred_seed": [{"model_id": "luna"}, {"harness": "claude", "model_id": "opus"}]}}}
    lines, data = routecmd._slate_view(("code_reviewer",), cfg, None)
    assert lines == ["  code_reviewer preferred_seed: */luna > claude/opus"]
    assert data["preferred_seed"]["code_reviewer"] == ["*/luna", "claude/opus"]


def test_the_slate_view_is_absent_without_the_executor_role_or_the_effective_flag(fresh_install):
    visual = routecmd.policy(role="visual_reviewer", effective=True)
    assert not lines_with(visual, "executor slate ") and "executor_slates" in visual.data
    assert visual.data["executor_slates"] == {}
    plain = routecmd.policy()
    assert not lines_with(plain, "executor slate ") and not lines_with(plain, "preferred_seed: ")
    assert "executor_slates" not in plain.data


def test_the_xs_slate_data_names_each_route_and_marks_only_the_hydrogen_rung_conditional(fresh_install):
    rows = routecmd.policy(role="executor", effective=True, size="XS").data["executor_slates"]["XS"]
    assert [(r["rung"], r["route"], r["conditional"]) for r in rows] == [
        (1, "claude/claude-haiku-5-5@high", False), (2, "codex/gpt-6.1-terra@low", False),
        (3, "agy/gemini-4-hydrogen@low", True)]

