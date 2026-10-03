"""#259: a refused submit is a durable blocker even while the worker lives; an executor can
request scope; an amendment tells the live worker to resubmit; hints stay consistent."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, PLAN_ONE, approved_run, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
WIDER = PLAN_ONE.replace("scope: calc.py", "scope: calc.py, README.md")


import pytest as _pytest  # noqa: E402

pytestmark = _pytest.mark.integration


def _worker(env, tid="T1"):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}, Path(d["worktree"])


MANUAL = {"OFFICE_JOBS": "manual"}


def _notify(env, monkeypatch, *, alive: bool, landed: str, first: bool = False):
    """Run the queued notify_worker job as a herdr-hosted worker whose agent is `alive`
    or not and whose prompt delivery reports `landed`."""
    from office import dispatch, gates, state
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pane_id='p1', status='running' WHERE role='executor'")
    con.commit()
    monkeypatch.setattr(gates, "_agent_alive", lambda name: alive)
    monkeypatch.setattr(dispatch, "submit_prompt", lambda *a, **kw: landed)
    row = con.execute("SELECT id FROM outbox WHERE kind='notify_worker' ORDER BY created_at " + ("ASC" if first else "DESC")).fetchone()
    job = state.get_job(con, row["id"])
    run = state.get_run(con, job["run_id"])
    return dispatch.job_notify_worker(con, run, job)


def _live_refused(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    (wt / "README.md").write_text("changed\n")
    env.git("add", "-N", "README.md", cwd=wt)
    return wenv, wt


@pytest.mark.approved
def test_refused_submit_from_a_live_worker_blocks_and_wakes_wait(env):
    wenv, wt = _live_refused(env)
    env.office("status", check=0)  # consume earlier events
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "outside its scope" in out and "--request-scope" in out, out
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("submit refused"), row
    con = env.con()
    d = con.execute("SELECT status, override_json FROM dispatches WHERE id=?", (wenv["OFFICE_DISPATCH_ID"],)).fetchone()
    assert d["status"] in ("running", "launching") and "README.md" in json.loads(d["override_json"])["submit_refused"]["files"]
    code, out = env.office("wait", "--timeout", "3", "--poll", "0.2", env=EXTERNAL)
    assert code == 0 and "blocker: T1 submit refused" in out and "README.md" in out, out


@pytest.mark.approved
def test_request_scope_wakes_the_orchestrator_with_files_reason_and_diff(env):
    wenv, wt = _live_refused(env)
    env.office("status", check=0)
    code, out = env.office("submit", "--request-scope", "README.md", "--", "docs need the new flag", cwd=wt, env=wenv)
    assert code == 0 and "scope request recorded" in out and "office amend T1 --contract" in out, out
    assert task_row(env)["status"] == "blocked"
    code, out = env.office("wait", "--timeout", "3", "--poll", "0.2", env=EXTERNAL)
    assert code == 0 and "requests scope README.md: docs need the new flag" in out, out
    assert "office amend T1 --contract" in out, out
    row = env.con().execute("SELECT payload_json FROM events WHERE kind='task.scope_requested'").fetchone()
    payload = json.loads(row[0])
    assert payload["files"] == ["README.md"] and payload["reason"] == "docs need the new flag" and "README.md" in payload["diff"]


@pytest.mark.approved
def test_request_scope_needs_a_reason_and_an_executor(env):
    wenv, wt = _live_refused(env)
    code, out = env.office("submit", "--request-scope", "README.md", cwd=wt, env=wenv)
    assert code == 2 and "why the scope must grow" in out, out
    code, out = env.office("submit", "--request-scope", "README.md", "--", "why", env=EXTERNAL)
    assert code != 0 and "executor" in out, out


@pytest.mark.approved
def test_amendment_tells_the_live_blocked_worker_to_resubmit(env, monkeypatch):
    wenv, wt = _live_refused(env)
    env.office("submit", "--request-scope", "README.md", "--", "docs need the new flag", cwd=wt, env=wenv)
    env.write_plan(WIDER)
    code, out = env.office("amend", "T1", "--contract", "--", "add README.md", env=MANUAL, check=0)
    # Not unblocked by the amend itself: only a confirmed prompt delivery unblocks.
    assert task_row(env)["status"] == "blocked"
    texts = [json.loads(r[0]).get("text", "") for r in env.con().execute(
        "SELECT payload_json FROM outbox WHERE kind='notify_worker'")]
    assert any("office submit again" in t and "office ack" in t for t in texts), texts
    _notify(env, monkeypatch, alive=True, landed="landed")
    assert task_row(env)["status"] == "running" and task_row(env)["pause_reason"] is None
    code, out = env.office("ack", "A1", cwd=wt, env=wenv)
    assert code == 0, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out, out


@pytest.mark.approved
def test_blocked_worker_can_revert_and_resubmit(env):
    wenv, wt = _live_refused(env)
    env.office("submit", cwd=wt, env=wenv)
    assert task_row(env)["status"] == "blocked"
    env.git("checkout", "--", "README.md", cwd=wt)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out, out
    assert task_row(env)["status"] != "blocked" and task_row(env)["pause_reason"] is None


@pytest.mark.approved
def test_rerun_on_a_live_worker_does_not_point_at_itself(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    code, out = env.office("rerun", "T1", "--resume")
    assert code == 4 and "worker-live" in out or "live worker" in out, out
    assert "office prompt T1" in out and "office revoke T1" in out, out


@pytest.mark.approved
def test_request_scope_includes_an_unstaged_new_file(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "NEWDOC.md").write_text("brand new doc line\n" * 2000)
    code, out = env.office("submit", "--request-scope", "NEWDOC.md", "--", "new doc", cwd=wt, env=wenv)
    assert code == 0, out
    payload = json.loads(env.con().execute("SELECT payload_json FROM events WHERE kind='task.scope_requested'").fetchone()[0])
    assert "brand new doc line" in payload["diff"] and len(payload["diff"]) < 7000, payload["diff"][:200]


@pytest.mark.approved
def test_revoked_worker_cannot_replace_the_blocker_with_a_scope_request(env):
    wenv, wt = _live_refused(env)
    env.office("revoke", "T1", check=0)
    before = task_row(env)
    code, out = env.office("submit", "--request-scope", "README.md", "--", "late request", cwd=wt, env=wenv)
    assert code == 4, out
    assert task_row(env)["pause_reason"] == before["pause_reason"]
    assert "scope requested" not in (task_row(env)["pause_reason"] or "")
    assert env.con().execute("SELECT COUNT(*) FROM events WHERE kind='task.scope_requested'").fetchone()[0] == 0
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert task_row(env)["pause_reason"] == before["pause_reason"], out


@pytest.mark.approved
def test_revoke_between_the_check_and_the_write_changes_nothing(env, monkeypatch):
    from office import submit as submit_mod
    wenv, wt = _live_refused(env)
    real = submit_mod._scope_hunk

    def revoking(*a, **kw):
        out = real(*a, **kw)
        env.office("revoke", "T1", check=0)  # lands after the validation, before the write
        return out

    monkeypatch.setattr(submit_mod, "_scope_hunk", revoking)
    code, out = env.office("submit", "--request-scope", "README.md", "--", "racy", cwd=wt, env=wenv)
    assert code == 4, out
    row = task_row(env)
    assert "scope requested" not in (row["pause_reason"] or "")
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='task.scope_requested'").fetchone()[0] == 0
    ov = con.execute("SELECT override_json FROM dispatches WHERE id=?", (wenv["OFFICE_DISPATCH_ID"],)).fetchone()[0]
    assert "scope_request" not in (ov or "")


@pytest.mark.approved
def test_revoke_before_the_refusal_write_changes_nothing(env, monkeypatch):
    from office import submit as submit_mod
    wenv, wt = _live_refused(env)
    real = submit_mod._dependency_bases

    def revoking(*a, **kw):
        out = real(*a, **kw)
        env.office("revoke", "T1", check=0)  # after the status and lease checks, before the refusal write
        return out

    monkeypatch.setattr(submit_mod, "_dependency_bases", revoking)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4, out
    assert "submit refused" not in (task_row(env)["pause_reason"] or "")
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='submit.refused'").fetchone()[0] == 0
    ov = con.execute("SELECT override_json FROM dispatches WHERE id=?", (wenv["OFFICE_DISPATCH_ID"],)).fetchone()[0]
    assert "submit_refused" not in (ov or "")


def _submitted_then_refused(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out, out
    assert task_row(env)["status"] == "submitted"
    (wt / "README.md").write_text("changed\n")
    return wenv, wt


@pytest.mark.approved
def test_refusal_after_an_in_scope_submit_blocks_and_keeps_the_revision(env, monkeypatch):
    wenv, wt = _submitted_then_refused(env)
    con = env.con()
    rev_before = task_row(env)["current_revision_id"]
    gates_before = con.execute("SELECT id, status FROM gates ORDER BY id").fetchall()
    env.office("status", check=0)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4 and "outside its scope" in out, out
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("submit refused"), row
    assert row["current_revision_id"] == rev_before
    # The refusal neither supersedes the revision nor adds, cancels or restarts gates
    # (a dirty tree may mark a gate stale on its own; that is not this path's doing).
    assert [g[0] for g in env.con().execute("SELECT id FROM gates ORDER BY id")] == [g[0] for g in gates_before]
    assert env.con().execute("SELECT status FROM revisions WHERE id=?", (rev_before,)).fetchone()[0] == "current"
    code, out = env.office("wait", "--timeout", "3", "--poll", "0.2", env=EXTERNAL)
    assert code == 0 and "blocker: T1 submit refused" in out, out
    # An amendment lifts it back to submitted, the status it was blocked from.
    env.write_plan(WIDER)
    env.office("amend", "T1", "--contract", "--", "add README.md", env=MANUAL, check=0)
    assert task_row(env)["status"] == "blocked"
    _notify(env, monkeypatch, alive=True, landed="landed")
    assert task_row(env)["status"] == "submitted" and task_row(env)["pause_reason"] is None


@pytest.mark.approved
def test_reverting_after_a_refusal_unblocks_through_the_duplicate_submit(env):
    wenv, wt = _submitted_then_refused(env)
    env.office("submit", cwd=wt, env=wenv)
    assert task_row(env)["status"] == "blocked"
    env.git("checkout", "--", "README.md", cwd=wt)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "already submitted" in out, out
    row = task_row(env)
    assert row["status"] == "submitted" and row["pause_reason"] is None, row


@pytest.mark.approved
def test_a_scope_request_after_an_in_scope_submit_blocks_too(env):
    wenv, wt = _submitted_then_refused(env)
    code, out = env.office("submit", "--request-scope", "README.md", "--", "docs", cwd=wt, env=wenv)
    assert code == 0, out
    assert task_row(env)["status"] == "blocked"
    assert task_row(env)["pause_reason"].startswith("scope requested")


@pytest.mark.approved
@pytest.mark.parametrize("alive,landed", [(False, "landed"), (True, "held"), (True, "")])
def test_amendment_to_a_dead_or_unconfirmed_agent_keeps_the_blocker(env, monkeypatch, alive, landed):
    wenv, wt = _live_refused(env)
    env.office("submit", "--request-scope", "README.md", "--", "docs", cwd=wt, env=wenv)
    env.write_plan(WIDER)
    env.office("amend", "T1", "--contract", "--", "add README.md", env=MANUAL, check=0)
    _notify(env, monkeypatch, alive=alive, landed=landed)
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("scope requested"), row
    ev = env.con().execute("SELECT summary FROM events WHERE kind='task.amend_undelivered'").fetchone()
    assert ev and "office revoke T1, then office rerun T1 --resume|--fresh" in ev[0], ev


@pytest.mark.approved
def test_unblock_leaves_a_block_a_revoke_replaced(env, monkeypatch):
    wenv, wt = _live_refused(env)
    env.office("submit", "--request-scope", "README.md", "--", "docs", cwd=wt, env=wenv)
    env.write_plan(WIDER)
    env.office("amend", "T1", "--contract", "--", "add README.md", env=MANUAL, check=0)
    env.office("revoke", "T1", check=0)
    before = task_row(env)["pause_reason"]
    _notify(env, monkeypatch, alive=True, landed="landed")
    assert task_row(env)["status"] == "paused" and task_row(env)["pause_reason"] == before


@pytest.mark.approved
def test_amend_command_quotes_hostile_paths_and_reason(env):
    import shlex
    wenv, wt = _live_refused(env)
    nasty = "x$(touch pwned).md"
    (wt / nasty).write_text("n\n")
    code, out = env.office("submit", "--request-scope", nasty, "--", 'why "$(id)" `id` \'q\'', cwd=wt, env=wenv)
    assert code == 0, out
    nxt = json.loads(env.con().execute("SELECT payload_json FROM events WHERE kind='task.scope_requested'").fetchone()[0])["next"]
    parts = shlex.split(nxt)
    assert parts[:4] == ["office", "amend", "T1", "--contract"] and len(parts) == 6, parts
    assert nasty in parts[5] and "$(id)" in parts[5] and "`id`" in parts[5], parts


@pytest.mark.approved
@pytest.mark.parametrize("bad", ["", "  ", "/etc/passwd", "../outside", "a/../../b", ":(top)x", "."])
def test_request_scope_rejects_empty_absolute_and_outside_paths(env, bad):
    wenv, wt = _live_refused(env)
    code, out = env.office("submit", "--request-scope", bad, "--", "why", cwd=wt, env=wenv)
    assert code == 2, out
    assert task_row(env)["status"] == "running"
    assert env.con().execute("SELECT COUNT(*) FROM events WHERE kind='task.scope_requested'").fetchone()[0] == 0


@pytest.mark.approved
def test_stray_positional_text_is_still_a_usage_error_on_a_plain_submit(env):
    wenv, wt = _live_refused(env)
    code, out = env.office("submit", "README.md", cwd=wt, env=wenv)
    assert code == 2 and "unrecognized arguments" in out, out
    assert task_row(env)["status"] == "running" and not env.con().execute("SELECT 1 FROM revisions").fetchone()


@pytest.mark.approved
def test_a_huge_new_file_is_read_only_up_to_the_cap(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "BIG.md").write_text(("y" * 79 + "\n") * 100_000)
    code, out = env.office("submit", "--request-scope", "BIG.md", "--", "big", cwd=wt, env=wenv)
    assert code == 0, out
    diff = json.loads(env.con().execute("SELECT payload_json FROM events WHERE kind='task.scope_requested'").fetchone()[0])["diff"]
    assert diff.endswith("... (truncated)") and len(diff) < 6100


def test_bounded_reader_stops_an_endless_process():
    import sys
    from office import submit as submit_mod
    text, cut = submit_mod._bounded([sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('z'*1000)"], Path("."), 5000)
    assert cut and len(text) == 5000


@pytest.mark.approved
def test_stale_amendment_notice_leaves_a_newer_blocker(env, monkeypatch):
    wenv, wt = _live_refused(env)
    env.office("submit", "--request-scope", "README.md", "--", "first", cwd=wt, env=wenv)
    env.write_plan(WIDER)
    env.office("amend", "T1", "--contract", "--", "add README.md", env=MANUAL, check=0)  # A1 queued
    env.office("submit", "--request-scope", "README.md", "--", "second", cwd=wt, env=wenv)
    env.write_plan(WIDER.replace("README.md", "README.md, docs/x.md"))
    env.office("amend", "T1", "--contract", "--", "more", env=MANUAL, check=0)  # A2
    _notify(env, monkeypatch, alive=True, landed="landed", first=True)  # A1 lands late
    row = task_row(env)
    assert row["status"] == "blocked" and "second" in row["pause_reason"], row
    _notify(env, monkeypatch, alive=True, landed="landed")  # the current one lifts it
    assert task_row(env)["status"] == "running"


@pytest.mark.approved
def test_request_diff_covers_committed_staged_and_unstaged_changes(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "README.md").write_text("committed line\n")
    env.git("add", "README.md", cwd=wt)
    env.git("commit", "-qm", "wip", cwd=wt)
    code, out = env.office("submit", "--request-scope", "README.md", "--", "docs", cwd=wt, env=wenv)
    assert code == 0, out
    diff = json.loads(env.con().execute("SELECT payload_json FROM events WHERE kind='task.scope_requested'").fetchone()[0])["diff"]
    assert "committed line" in diff, diff


@pytest.mark.approved
def test_submit_does_not_clear_a_scope_request_recorded_in_between(env, monkeypatch):
    from office import submit as submit_mod
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    real = submit_mod._dependency_bases
    done = []

    def racing(*a, **kw):
        out = real(*a, **kw)
        if not done:
            done.append(1)
            # the same worker's scope request lands after this submit's first status check
            monkeypatch.setattr(submit_mod, "_dependency_bases", real)
            env.office("submit", "--request-scope", "README.md", "--", "race", cwd=wt, env=wenv, check=0)
        return out

    monkeypatch.setattr(submit_mod, "_dependency_bases", racing)
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4, out
    row = task_row(env)
    assert row["status"] == "blocked" and row["pause_reason"].startswith("scope requested"), row
    assert not env.con().execute("SELECT 1 FROM revisions").fetchone()


@pytest.mark.approved
def test_stale_undelivered_notice_recommends_nothing(env, monkeypatch):
    wenv, wt = _live_refused(env)
    env.office("submit", "--request-scope", "README.md", "--", "docs", cwd=wt, env=wenv)
    env.write_plan(WIDER)
    env.office("amend", "T1", "--contract", "--", "add README.md", env=MANUAL, check=0)
    con = env.con()
    con.execute("UPDATE tasks SET current_dispatch_id='Dnewowner' WHERE id='T1'")  # relaunched as another dispatch
    con.commit()
    _notify(env, monkeypatch, alive=False, landed="")
    assert env.con().execute("SELECT COUNT(*) FROM events WHERE kind='task.amend_undelivered'").fetchone()[0] == 0


@pytest.mark.approved
def test_request_diff_includes_every_new_file_or_marks_truncation(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt = _worker(env)
    names = [f"new{i:02d}.md" for i in range(30)]
    for n in names:
        (wt / n).write_text(f"body of {n}\n")
    code, out = env.office("submit", *[a for n in names for a in ("--request-scope", n)], "--", "many", cwd=wt, env=wenv)
    assert code == 0, out
    diff = json.loads(env.con().execute("SELECT payload_json FROM events WHERE kind='task.scope_requested'").fetchone()[0])["diff"]
    assert all(f"body of {n}" in diff for n in names), diff[-300:]


def test_plan_review_brief_asks_for_registries_in_scope():
    from office import briefs
    assert "Scope registries" in briefs.PLAN_REVIEW_FORMAT and "gate" in briefs.PLAN_REVIEW_FORMAT
