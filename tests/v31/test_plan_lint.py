"""T6: plan submit lints the done and accept criteria before any reviewer sees them (#363).

A criterion that needs a PR body when the run has no task PRs can never be met; a done criterion and an accept
item that put the same deliverable in different places contradict each other. The plan-review brief asks reviewers
to check each accept list against the done criteria it serves.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import PLAN_ONE, start_inline
from test_task_prs import github

PR_OFF = {"enabled": False, "reason": "no origin remote"}
PR_ON = {"enabled": True, "repo": "o/r"}


def _task(tid="T1", *accept):
    return {"id": tid, "accept": list(accept)}


@pytest.fixture
def settings(monkeypatch):
    """prs.settings answers `answer[0]` and records that it was asked."""
    from office import prs
    answer, asked = [PR_OFF], []

    def fake(con, run):
        asked.append(run)
        return answer[0]

    monkeypatch.setattr(prs, "settings", fake)
    return SimpleNamespace(set=lambda value: answer.__setitem__(0, value), asked=asked)


def lint(done, *tasks):
    from office import plans
    return plans.lint_plan(None, {"id": "r"}, list(tasks), list(done))


# ------------------------------------------------------------------ the patterns

@pytest.mark.parametrize("text", [
    "the PR body lists the test steps", "explain the change in the PR description", "post a comment on the PR",
    "each task's pull request body says what changed", "a summary in the pull-request description",
    "the write-up is in the PR", "PR comments name the reviewer", "the description of the PR names the issue",
    "the write-up is in the PR, with the test steps", "put the summary in the pull request and link the issue",
    "PR bodies list each change",
])
def test_criteria_that_need_pr_text(text):
    from office import briefs
    assert briefs.refers_to_pr_text(text), text


@pytest.mark.parametrize("text", [
    "add() returns the sum", "the app handles pricing", "the PR is merged by the user", "uses a prompt", "pr_number is an int",
    "commits are pushed to the PR branch", "CI checks on the PR pass", "reviewed in PR review", "each task opens a PR",
    "CI is green on the PR.", "changes are pushed to the PR", "do not put the summary in the PR body",
    "no PR description is required", "never write a comment on the PR",
    "Don't put the summary in the PR body", "we shouldn't add it in the PR description",
    "Do not paste the README.md diff in the PR body", "Never put the output of v1.2 in the PR description",
    "avoid a PR comment", "put it in the commit body instead of the PR body",
    "The summary cannot go in the PR body", "Don\u2019t put it in the PR body", "nothing in the PR body",
])
def test_criteria_that_do_not(text):
    from office import briefs
    assert not briefs.refers_to_pr_text(text), text


def test_locations_and_deliverables():
    from office import briefs
    assert briefs.locations("The write-up is in the PR body") == {"the PR body"}
    assert briefs.locations("the commit body contains the write-up") == {"the commit body"}
    assert briefs.locations("describe the cause in the commit message") == {"the commit body"}
    assert briefs.locations("a summary in docs/notes.md") == {"`docs/notes.md`"}
    assert briefs.locations("leave an issue comment with the report") == {"the issue"}
    assert briefs.locations("both the PR description and the commit body say why") == {"the PR body", "the commit body"}
    assert briefs.locations("calc.add(2, 3) == 5") == set()
    assert briefs.deliverables("A write-up of the root cause") == briefs.deliverables("the Writeup is complete") == {"writeup"}


# ------------------------------------------------------------------ task PRs off

def test_pr_text_with_task_prs_off_is_reported_for_done_and_accept(settings):
    problems = lint(["A write-up of the cause is in the PR body"],
                    _task("T1", "calc.add(2, 3) == 5", "a PR comment names the reviewer"))
    assert len(problems) == 2
    assert "done criterion 'A write-up of the cause is in the PR body'" in problems[0]
    assert "T1 accept criterion 'a PR comment names the reviewer'" in problems[1]
    assert all("this run has no task PRs (no origin remote)" in p for p in problems)


def test_pr_text_with_task_prs_on_is_fine(settings):
    settings.set(PR_ON)
    assert lint(["the PR body lists the test steps"], _task("T1", "the PR description names the issue")) == []


def test_a_transient_detection_failure_is_not_read_as_prs_off(settings):
    settings.set({"enabled": False, "transient": True, "reason": "gh repo view failed"})
    assert lint(["the PR body lists the steps"], _task("T1", "x")) == []


def test_github_is_not_asked_when_no_criterion_needs_a_pr(settings):
    assert lint(["add() returns the sum"], _task("T1", "calc.add(2, 3) == 5", "the PR is merged by the user")) == []
    assert settings.asked == []


# ------------------------------------------------------------------ the same deliverable in two places (#363)

def test_363_a_writeup_in_the_pr_body_for_done_and_the_commit_body_for_accept_conflicts(settings):
    settings.set(PR_ON)
    problems = lint(["A write-up of the root cause is in the PR body"],
                    _task("T1", "calc.add(2, 3) == 5", "The commit body contains the write-up of the root cause"))
    assert len(problems) == 1
    assert "puts the writeup in the PR body" in problems[0] and "T1 accept" in problems[0] and "the commit body" in problems[0]


def test_agreeing_or_unrelated_locations_do_not_conflict(settings):
    settings.set(PR_ON)
    assert lint(["a write-up is in the PR body"], _task("T1", "the write-up is in the PR description")) == []
    assert lint(["a write-up is in the PR body and the commit body"], _task("T1", "the write-up is in the commit body")) == []
    assert lint(["a write-up is in the PR body"], _task("T1", "the changelog is in the commit body")) == [], "another deliverable"
    assert lint(["a write-up is in the PR body"], _task("T1", "add() returns the sum")) == []
    assert lint(["the app builds"], _task("T1", "the write-up is in the commit body")) == []


def test_a_summary_line_or_a_forbidden_location_or_a_file_name_case_is_not_a_conflict(settings):
    settings.set(PR_ON)
    assert lint(["the PR body contains a summary of the change"], _task("T1", "the commit message has a summary line")) == []
    assert lint(["the PR body has the report"], _task("T1", "do not put the report in the commit body")) == []
    assert lint(["the report is written to docs/Report.md"], _task("T1", "the report is saved in docs/report.md")) == []


@pytest.mark.parametrize("text", [
    "the summary is not in the commit body but in the PR description",
    "the summary is in the PR body (no secrets)",
])
def test_a_negation_ends_with_its_clause(text):
    from office import briefs
    assert briefs.refers_to_pr_text(text), text


def test_a_file_is_a_location_only_where_something_is_placed_in_it():
    from office import briefs
    assert briefs.locations("the summary lists every change made") == set()
    assert briefs.locations("whether config.yml is valid") == set()
    assert briefs.locations("a summary in a.md.bak") == set() and briefs.locations("see v1.0.md") == set()
    assert briefs.locations("a summary in `docs/r.md`") == {"`docs/r.md`"}
    assert briefs.locations("Do not put the summary in README.md") == set(), "a forbidden file is no location"
    assert briefs.locations("Never write notes to README.md, put them in the commit body") == {"the commit body"}


def test_a_negation_early_in_a_long_clause_still_applies():
    from office import briefs
    text = "Do not " + "write about the thing and keep going with more words " * 6 + "and put the summary in the PR body"
    assert len(text) > 300 and not briefs.refers_to_pr_text(text)


def test_checking_a_long_criterion_stays_linear():
    import time

    from office import briefs
    text = "not in the PR body " * 20000
    start = time.monotonic()
    assert not briefs.refers_to_pr_text(text) and briefs.locations(text) == set()
    assert time.monotonic() - start < 2


def test_a_named_file_needs_no_preposition_and_paths_compare_normalised(settings):
    from office import briefs
    assert briefs.locations("the summary is in the commit body and README.md") == {"the commit body", "`readme.md`"}
    assert briefs.locations("a summary in ./docs/r.md") == briefs.locations("a summary in docs/r.md") == {"`docs/r.md`"}
    assert briefs.locations("a summary in config.jsonl") == set(), "an extension is not a prefix of a longer one"
    settings.set(PR_ON)
    assert lint(["the summary is in the commit body and README.md"], _task("T1", "summary in README.md")) == []
    assert lint(["the report is in ./docs/r.md"], _task("T1", "the report is saved in docs/r.md")) == []


def test_two_files_for_one_deliverable_conflict(settings):
    settings.set(PR_ON)
    problems = lint(["the report is written to docs/report.md"], _task("T1", "the report is saved in docs/out.md"))
    assert len(problems) == 1 and "`docs/report.md`" in problems[0] and "`docs/out.md`" in problems[0]


def test_every_conflicting_accept_item_is_named(settings):
    settings.set(PR_ON)
    problems = lint(["a summary in the PR body"], _task("T1", "summary in the commit body"), _task("T2", "summary in the issue comment"))
    assert [p.split(" accept ")[0].split()[-1] for p in problems] == ["T1", "T2"]


# ------------------------------------------------------------------ the plan-review brief

@pytest.mark.parametrize("review_contract", ["convergence-v1", "v3.1"])
def test_the_plan_review_brief_asks_for_each_accept_list_to_be_checked_against_its_done_criteria(review_contract):
    from office import briefs
    run = {"id": "r", "goal": "g", "gates": {"review_contract": review_contract}}
    brief = briefs.plan_review_brief(run, {"version": 1, "body": "PLAN BODY"},
                                     {"done_criteria": ["a"], "blast_radius": "repo"}, [], False)
    assert "Check each task's accept list against the done criteria it serves" in brief
    assert "the PR body vs the commit body" in brief and "naming both lines" in brief
    assert brief.index("Check each task's accept list") < brief.index("PLAN BODY")


# ------------------------------------------------------------------ plan submit

PR_DONE = PLAN_ONE.replace("- add() returns the sum", "- add() returns the sum\n- a write-up of the cause is in the PR body")
CONFLICT = PR_DONE.replace("- calc.add(2, 3) == 5\nvisual", "- calc.add(2, 3) == 5\n- the commit body contains the write-up of the cause\nvisual")


def _plan_version(env):
    con = env.con()
    try:
        return con.execute("SELECT plan_version FROM runs").fetchone()[0]
    finally:
        con.close()


def _start_run(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--gear", "direct+review", "--planner", "inline")
    assert code == 0, out


def test_submit_reports_a_pr_criterion_before_review_when_the_run_has_no_task_prs(env):
    _start_run(env)
    env.write_plan(PR_DONE)
    code, out = env.office("submit")
    assert code == 4 and "plan-lint" in out and "this run has no task PRs" in out, out
    assert "a write-up of the cause is in the PR body" in out
    assert _plan_version(env) in (None, 0), "the refused plan was not recorded, so no review was queued"
    env.write_plan(PLAN_ONE)
    env.office("submit", check=0)


def test_submit_reports_the_363_conflict_before_review(env, monkeypatch):
    github(env, monkeypatch)
    _start_run(env)
    env.write_plan(CONFLICT)
    code, out = env.office("submit")
    assert code == 4 and "plan-lint" in out and "puts the writeup in the PR body" in out, out
    assert "T1 accept" in out and "the commit body" in out
    assert _plan_version(env) in (None, 0)
    env.write_plan(CONFLICT.replace("the commit body contains", "the PR body contains"))
    env.office("submit", check=0)
    assert _plan_version(env) == 1


def test_pr_criteria_pass_when_task_prs_are_on(env, monkeypatch):
    github(env, monkeypatch)
    _start_run(env)
    env.write_plan(PR_DONE)
    env.office("submit", check=0)


@pytest.mark.review_contract("v3.1")
def test_a_v31_run_is_not_newly_refused_by_the_lint(env):
    """Runs pinned to v3.1 keep their semantics: a plan they would have accepted is still accepted."""
    _start_run(env)
    env.write_plan(PR_DONE)
    env.office("submit", check=0)
    assert _plan_version(env) == 1


def test_the_pr_check_does_not_pin_detection_for_a_blast_radius_the_plan_is_about_to_change(settings):
    from office import plans
    done = ["the PR body lists the test steps"]
    # intake froze local; the plan declares repo: PRs may exist once it is applied, so nothing is decided now.
    assert plans.lint_plan(None, {"id": "r"}, [], done, blast=("repo", "local")) == []
    # the plan goes local: PRs will be off, and the criterion can never be met.
    problems = plans.lint_plan(None, {"id": "r"}, [], done, blast=("local", "repo"))
    assert len(problems) == 1 and "blast radius is local" in problems[0]
    assert settings.asked == [], "neither answer asked (or pinned) the old blast radius's detection"
    settings.set(PR_OFF)
    assert plans.lint_plan(None, {"id": "r"}, [], done, blast=("repo", "repo")) and len(settings.asked) == 1


def test_an_already_pinned_pr_setting_decides_whatever_blast_radius_the_plan_declares(settings):
    from office import plans
    done = ["the PR body lists the test steps"]
    on = {"id": "r", "landing": {"prs": PR_ON}}
    settings.set(PR_ON)
    assert plans.lint_plan(None, on, [], done, blast=("local", "repo")) == [] and len(settings.asked) == 1
