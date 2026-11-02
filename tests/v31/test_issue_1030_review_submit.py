"""#1030 (Rico, 2026-11-02): reviewers hand their verdict back through `office review submit`, like executors
use `office submit`. It validates the reply for its gate, records it at once (reply.txt plus a submitted
marker the watcher honours), and honours only the reviewer's own per-dispatch secret."""
from __future__ import annotations

import json
import os

import pytest

from conftest import approved_run

from office import dispatch, paths, review_submit, state
from office.state import OfficeError

APPROVED = "VERDICT: APPROVED\nREVIEWED_COMMIT {sha}\nNEXT proceed"


@pytest.fixture
def reviewer(env, monkeypatch):
    approved_run(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    sha = env.git("rev-parse", "HEAD").strip()
    with con:
        con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at, plan_version, scope) "
                    "VALUES('Gsub1', ?, 'lane', 'convergence_review', ?, 'running', '2026-11-02T00:00:00Z', 1, 'L-T1')",
                    (run["id"], f"L-T1:{sha}"))
        con.execute("INSERT INTO dispatches(id, run_id, role, holder_id, task_id, status, started_at, gate_id, harness, "
                    "office_version) VALUES('Dsub1', ?, 'code_reviewer', 'Dsub1', NULL, 'running', "
                    "'2026-11-02T00:00:00Z', 'Gsub1', 'claude', '3')", (run["id"],))
    d = state.get_dispatch(con, "Dsub1")
    ddir = paths.run_dir(run["id"]) / "dispatches" / "Dsub1"
    ddir.mkdir(parents=True, exist_ok=True)
    out = ddir / "reply.txt"
    (ddir / "launch.json").write_text(json.dumps({"output": str(out), "kind": "reviewer"}))
    ident = review_submit.token_env(run, d)
    for k, v in ident.items():
        monkeypatch.setenv(k, v)
    return con, run, sha, out, ident


def test_a_valid_review_is_recorded_at_once(reviewer):
    con, run, sha, out, _ = reviewer
    res = review_submit.submit(con, APPROVED.format(sha=sha))
    assert "APPROVED" in res.lines[0]
    assert out.read_text().startswith("VERDICT: APPROVED") and review_submit.submitted(out)
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='review.submitted'").fetchone()[0] == 1


@pytest.mark.parametrize("text, category", [
    ("VERDICT: PASS\nNEXT proceed", "review-invalid"),  # the v3.1 word on a convergence gate
    ("VERDICT: APPROVED\nREVIEWED_COMMIT deadbeefdeadbeef\nNEXT proceed", "review-wrong-commit"),
])
def test_an_invalid_review_is_refused_and_nothing_is_recorded(reviewer, text, category):
    con, run, sha, out, _ = reviewer
    with pytest.raises(OfficeError) as err:
        review_submit.submit(con, text)
    assert err.value.category == category, err.value.message
    assert not out.exists() and not review_submit.submitted(out)


def test_only_the_reviewers_own_secret_is_honoured(reviewer, monkeypatch):
    con, run, sha, out, _ = reviewer
    monkeypatch.setenv(review_submit.TOKEN_ENV, "0" * 48)
    with pytest.raises(OfficeError) as err:
        review_submit.submit(con, APPROVED.format(sha=sha))
    assert err.value.category == "review-identity"
    monkeypatch.delenv(review_submit.TOKEN_ENV)
    with pytest.raises(OfficeError) as err:
        review_submit.submit(con, APPROVED.format(sha=sha))
    assert err.value.category == "not-a-reviewer"


def test_a_closed_review_cannot_be_submitted(reviewer):
    con, run, sha, out, _ = reviewer
    with con:
        con.execute("UPDATE gates SET status='done' WHERE id='Gsub1'")
    with pytest.raises(OfficeError) as err:
        review_submit.submit(con, APPROVED.format(sha=sha))
    assert err.value.category == "review-closed"


def test_the_cli_hands_back_a_review_from_a_reviewer_session(env, reviewer, tmp_path):
    con, run, sha, out, ident = reviewer
    review = tmp_path / "r.txt"
    review.write_text(APPROVED.format(sha=sha))
    code, text = env.office("review", "submit", "--file", str(review),
                            env={"OFFICE_RUN_ID": run["id"], "OFFICE_ROLE": "reviewer", **ident})
    assert code == 0 and "review submitted" in text, text
    assert review_submit.submitted(out)


def test_reviewer_sessions_carry_only_their_own_submit_identity(reviewer, tmp_path):
    con, run, sha, out, ident = reviewer
    d = state.get_dispatch(con, "Dsub1")
    env_file = dispatch.write_agent_env(run, d, tmp_path, worker=False)
    text = env_file.read_text()
    assert f"{review_submit.TOKEN_ENV}={ident[review_submit.TOKEN_ENV]}" in text
    assert "OFFICE_DISPATCH_ID" not in text, "reviewers still get no Office dispatch authority"
    assert oct(os.stat(out.parent / review_submit.TOKEN_FILE).st_mode & 0o777) == "0o600"


def test_the_herdr_watcher_ends_at_once_on_a_submitted_review(reviewer, monkeypatch):
    con, run, sha, out, _ = reviewer
    review_submit.submit(con, APPROVED.format(sha=sha))
    monkeypatch.setattr(dispatch, "_herdr_agent_text", lambda *a, **k: pytest.fail("no pane read needed"))
    spec = {"kind": "reviewer", "herdr_agent": "office-dsub1", "output": str(out)}
    assert dispatch.watch_herdr_agent("Dsub1", spec, poll=0) == (0, "success")
