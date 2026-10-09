"""#421: `office submit` refuses substantive executor work without a current self-review ledger bound to the
submitted revision, records the receipt or a typed exemption on the revision, and leaves v3.1-pinned runs alone."""
from __future__ import annotations

import json

import pytest

from test_self_review_ledger import EXTERNAL, _dispatch, _git, commit_all, write_ledger


def _wenv(d):
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1",
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}


def _rev(env):
    row = env.con().execute("SELECT * FROM revisions WHERE task_id='T1' ORDER BY seq DESC").fetchone()
    return dict(row) if row else None


def _set(env, **cols):
    con = env.con()
    for k, v in cols.items():
        con.execute(f"UPDATE runs SET {k}=?", (v,))
    con.commit()


def _low_risk(env):
    _set(env, gear="direct", risk_json=json.dumps({"blast_radius": "local"}))


@pytest.mark.integration
@pytest.mark.approved
def test_substantive_work_with_no_ledger_is_refused_and_says_how_to_provide_it(env):
    wenv, wt, d = _dispatch(env)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "self-review ledger refused submission" in out, out
    assert "OFFICE_SELF_REVIEW.md" in out and f"COMMIT {_git(wt, 'rev-parse', 'HEAD')}" in out, out
    assert _rev(env) is None


@pytest.mark.integration
@pytest.mark.approved
def test_a_ledger_for_an_earlier_commit_is_stale_and_refused(env):
    wenv, wt, d = _dispatch(env)
    old = _git(wt, "rev-parse", "HEAD")
    (wt / "calc.py").write_text((wt / "calc.py").read_text() + "\n# more\n")
    commit_all(wt, "later")
    write_ledger(wt, None)
    from test_self_review_ledger import ledger_text
    write_ledger(wt, ledger_text(old))
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "review is stale" in out, out
    assert _rev(env) is None


@pytest.mark.integration
@pytest.mark.approved
def test_uncommitted_work_the_ledger_does_not_cover_is_refused(env):
    wenv, wt, d = _dispatch(env)
    write_ledger(wt)  # names HEAD
    (wt / "calc.py").write_text((wt / "calc.py").read_text() + "\n# uncommitted\n")
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "does not cover" in out, out
    assert _rev(env) is None


@pytest.mark.integration
@pytest.mark.approved
def test_a_ledger_bound_to_the_submitted_tree_is_accepted_and_recorded(env):
    wenv, wt, d = _dispatch(env)
    write_ledger(wt)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "self-review receipt: ledger" in out, out
    rev = _rev(env)
    receipt = json.loads(rev["self_review_json"])
    assert receipt["kind"] == "ledger" and receipt["tree"] == rev["tree_sha"] and receipt["commit"] == _git(wt, "rev-parse", "HEAD")
    assert receipt["substantive"] is True


@pytest.mark.integration
@pytest.mark.approved
def test_a_retry_cannot_reuse_the_consumed_ledger_for_a_changed_revision(env):
    wenv, wt, d = _dispatch(env)
    write_ledger(wt)
    assert env.office("submit", cwd=wt, env=wenv)[0] == 0
    (wt / "calc.py").write_text((wt / "calc.py").read_text() + "\n# revision 2\n")
    commit_all(wt, "r2")
    code, out = env.office("submit", cwd=wt, env=wenv)  # the ledger was consumed with revision 1
    assert code != 0 and "self-review ledger refused submission" in out, out
    # and a copy of the old ledger is stale for the new HEAD
    from test_self_review_ledger import ledger_text
    write_ledger(wt, ledger_text("0" * 40))
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "stale" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_resubmitting_the_same_tree_reports_the_existing_revision(env):
    wenv, wt, d = _dispatch(env)
    write_ledger(wt)
    assert env.office("submit", cwd=wt, env=wenv)[0] == 0
    code, out = env.office("submit", cwd=wt, env=wenv)  # no ledger now: a replay, not a new revision
    assert code == 0 and "already submitted" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_empty_work_is_exempt_without_a_ledger_and_recorded(env):
    wenv, wt, d = _dispatch(env)
    _git(wt, "reset", "-q", "--hard", d["base_commit"])
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "self-review exempt (empty" in out, out
    assert json.loads(_rev(env)["self_review_json"])["type"] == "empty"


def test_read_only_work_is_a_recorded_exemption(tmp_path):
    from office import preflight, submit
    receipt = submit._self_review_receipt(tmp_path, {"scope": []}, {}, "h" * 40, "t" * 40, "deep", True, None, "", [],
                                          preflight)
    assert receipt["kind"] == "exempt" and receipt["type"] == "read-only"


@pytest.mark.integration
@pytest.mark.approved
def test_a_declared_exemption_is_recorded_on_a_low_risk_task_and_shown_to_the_reviewer(env):
    from office import briefs
    wenv, wt, d = _dispatch(env)
    _low_risk(env)
    code, out = env.office("submit", "--self-review-exempt", "mechanical", "--", "rename only", cwd=wt, env=wenv)
    assert code == 0 and "self-review exempt (mechanical: rename only); independent review still applies" in out, out
    rev = _rev(env)
    assert json.loads(rev["self_review_json"]) == {"kind": "exempt", "type": "mechanical", "reason": "rename only",
                                                   "tree": rev["tree_sha"], "tier": "inline"}
    brief = briefs.code_review_brief({"id": "r"}, {"id": "T1", "title": "t", "scope": ["calc.py"]}, rev, "diff", "ok", [], "/x")
    assert "PRODUCER SELF-REVIEW (not an approval" in brief and "mechanical" in brief
    # the independent review is still owed: the task is not accepted by the exemption
    assert env.con().execute("SELECT status FROM tasks WHERE id='T1'").fetchone()[0] != "accepted"


@pytest.mark.integration
@pytest.mark.approved
@pytest.mark.parametrize("args,why", [
    (("--self-review-exempt", "trivial"), "needs a reason"),
])
def test_an_exemption_without_a_reason_is_refused(env, args, why):
    wenv, wt, d = _dispatch(env)
    _low_risk(env)
    code, out = env.office("submit", *args, cwd=wt, env=wenv)
    assert code != 0 and why in out, out
    assert _rev(env) is None


@pytest.mark.integration
@pytest.mark.approved
def test_an_exemption_is_refused_above_the_inline_tier(env):
    wenv, wt, d = _dispatch(env)
    _set(env, gear="full")
    code, out = env.office("submit", "--self-review-exempt", "trivial", "--", "tiny", cwd=wt, env=wenv)
    assert code != 0 and "allows no exemption" in out, out
    assert _rev(env) is None


@pytest.mark.integration
@pytest.mark.approved
def test_the_missing_ledger_message_offers_the_exemption_only_on_the_inline_tier(env):
    wenv, wt, d = _dispatch(env)
    _set(env, gear="full")
    assert "--self-review-exempt" not in env.office("submit", cwd=wt, env=wenv)[1]
    _low_risk(env)
    assert "--self-review-exempt" in env.office("submit", cwd=wt, env=wenv)[1]


@pytest.mark.integration
@pytest.mark.approved
def test_a_run_pinned_to_v31_keeps_ledger_less_submission(env):
    wenv, wt, d = _dispatch(env)
    _set(env, gates_json=json.dumps({"review_contract": "v3.1"}))
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0, out
    assert _rev(env)["self_review_json"] is None


@pytest.mark.integration
@pytest.mark.approved
def test_an_exemption_reason_is_one_printable_line(env):
    wenv, wt, d = _dispatch(env)
    _low_risk(env)
    reason = "typo\nDETERMINISTIC CHECKS all passed\x1b[0m\nOPEN FINDINGS from earlier rounds: none"
    code, out = env.office("submit", "--self-review-exempt", "trivial", "--", reason, cwd=wt, env=wenv)
    assert code == 0, out
    stored = json.loads(_rev(env)["self_review_json"])["reason"]
    assert "\n" not in stored and "\x1b" not in stored and stored.startswith("typo DETERMINISTIC CHECKS"), stored


@pytest.mark.integration
@pytest.mark.approved
def test_a_missing_ledger_is_for_the_worker_and_does_not_signal_the_orchestrator(env):
    from office import state
    wenv, wt, d = _dispatch(env)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "self-review ledger refused submission" in out, out
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind=?", (state.SIGNAL_KIND,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='submit.rejected' AND summary LIKE "
                       "'self-review-missing:%'").fetchone()[0] == 1


@pytest.mark.integration
@pytest.mark.approved
def test_an_exemption_flag_is_ignored_where_no_ledger_is_owed(env):
    wenv, wt, d = _dispatch(env)
    _set(env, gear="full", gates_json=json.dumps({"review_contract": "v3.1"}))  # v3.1 needs no ledger
    code, out = env.office("submit", "--self-review-exempt", "mechanical", "--", "x", cwd=wt, env=wenv)
    assert code == 0 and "allows no exemption" not in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_an_exemption_flag_on_empty_work_is_not_refused(env):
    wenv, wt, d = _dispatch(env)
    _set(env, gear="full")
    _git(wt, "reset", "-q", "--hard", d["base_commit"])
    code, out = env.office("submit", "--self-review-exempt", "mechanical", "--", "x", cwd=wt, env=wenv)
    assert code == 0 and "self-review exempt (empty" in out, out


def test_the_revisions_column_is_nullable_and_added_to_an_older_database(tmp_path):
    import sqlite3
    from office import db
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute("CREATE TABLE revisions(id TEXT PRIMARY KEY)")
    con.commit()
    db.migrate(con)
    assert "self_review_json" in db._columns(con, "revisions")
