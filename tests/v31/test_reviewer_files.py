"""Reviewers report through files; a bad reply is re-prompted, never discarded (R8, R11, R13, B9)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import GOOD_ADD, PLAN_ONE, start_inline


def _go(env, **script):
    env.trust()
    env.script(**script)
    start_inline(env, plan=PLAN_ONE, gear="direct+review")
    env.office("approve", "plan", "--quote", "approved", check=0)


def _task(env):
    return dict(env.con().execute("SELECT * FROM tasks WHERE id='T1'").fetchone())


def test_headless_invalid_reply_is_not_substituted_or_unavailable(env):
    # A headless reviewer has no live session to re-prompt: the gate goes to the
    # orchestrator as needs-attention, no other reviewer is launched, and the
    # route is not labelled an adapter failure.
    _go(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        code_reviewer=[{"reply": "looks fine to me"}, {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    reviewers = [c for c in env.calls() if c.get("role") == "code_reviewer"]
    assert len(reviewers) == 1, reviewers
    t = _task(env)
    assert t["status"] == "blocked" and "needs attention" in (t["pause_reason"] or ""), t
    con = env.con()
    assert not con.execute("SELECT 1 FROM gates WHERE verdict='UNAVAILABLE'").fetchone()
    assert not con.execute("SELECT 1 FROM dispatches WHERE role='code_reviewer' AND outcome='environment_failure'").fetchone()


def test_launch_failure_still_substitutes(env):
    _go(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
        **{"codex:code_reviewer": [{"stderr": "Not logged in · Please run /login", "exit": 1}],
           "claude:code_reviewer": [{"reply": "VERDICT: PASS"}], "gemini:code_reviewer": [{"reply": "VERDICT: PASS"}]})
    env.office("dispatch", "T1", check=0)
    assert _task(env)["status"] == "accepted"


def test_deliver_findings_never_launches(env):
    # With no live worker the findings wait for the orchestrator; nothing launches.
    cr = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py | wrong | fix it"
    _go(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": cr}])
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    con = env.con()
    d = con.execute("SELECT id, worktree FROM dispatches WHERE role='executor'").fetchone()
    (Path(d["worktree"]) / "calc.py").write_text(GOOD_ADD)
    wenv = {"OFFICE_DISPATCH_ID": d["id"], "OFFICE_WORKER_LAUNCHER": "external"}
    env.office("submit", cwd=d["worktree"], env=wenv, check=0)
    con.execute("UPDATE dispatches SET status='exited', ended_at='x' WHERE id=?", (d["id"],))
    con.commit()
    from office import db, gates, state
    c = db.connect()
    run = state.get_run(c, _task(env)["run_id"])
    gate = dict(c.execute("SELECT * FROM gates WHERE kind='code_review' LIMIT 1").fetchone())
    before = c.execute("SELECT COUNT(*) FROM dispatches WHERE role='executor'").fetchone()[0]
    with db.transaction(c):
        gates.deliver_findings(c, run, state.get_task(c, run["id"], "T1"), gate)
    after = c.execute("SELECT COUNT(*) FROM dispatches WHERE role='executor'").fetchone()[0]
    assert after == before
    assert c.execute("SELECT 1 FROM events WHERE kind='task.findings_queued'").fetchone()


def test_scoring_ignores_capture_bug_labels(env):
    from office import scoring
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE dispatches(id TEXT, attribution TEXT, triple TEXT, kind TEXT, outcome TEXT, "
                "terminal_classification TEXT)")
    con.execute("INSERT INTO dispatches VALUES('D1','adapter','codex@0/m@high','reviewer','environment_failure','success')")
    con.execute("CREATE TABLE outcome_labels(id TEXT, dispatch_id TEXT, label TEXT, primary_attribution TEXT, "
                "contributing_attributions TEXT, labeled_at TEXT, evidence_hash TEXT)")
    con.execute("INSERT INTO outcome_labels VALUES('L1','D1','recurrence_failure','adapter',NULL,'x','sha256:' || "
                "'" + "a" * 64 + "')")
    scoring.ensure_trust_schema(con)
    _, state = scoring.evaluate_trust_state(con, "codex@0/m@high")
    assert state != "quarantined", state


def test_briefs_require_the_reply_file():
    from office import briefs
    assert "Office reads only that file" in briefs.REVIEW_FORMAT
    assert "Office reads only that file" in briefs.PLAN_REVIEW_FORMAT


def test_reviewer_profiles_can_write_their_reply_file(tmp_path):
    # R12: no reviewer or vision profile bans writes, and each grants the reply
    # file's directory; a worker's argv carries no such grant.
    from office import adapters
    out = tmp_path / "D1" / "reply.txt"
    for aid in ("claude", "codex"):
        a = adapters.load_all()[aid]
        for kind in ("reviewer", "vision"):
            argv, _ = adapters.build_argv(a, kind, model="m", effort="high", cwd=tmp_path, output=out)
            joined = " ".join(argv)
            assert "read-only" not in joined and "Edit,Write" not in joined, (aid, kind, argv)
            assert str(out.parent) in argv, (aid, kind, argv)
            inter = adapters.interactive_argv(a, kind, model="m", effort="high", cwd=tmp_path, output=out)
            assert inter and str(out.parent) in inter[0] and "read-only" not in " ".join(inter[0]), (aid, kind, inter)
        wargv, _ = adapters.build_argv(a, "worker", model="m", effort="high", cwd=tmp_path)
        assert "{output_dir}" not in " ".join(wargv)


def test_worker_profiles_declare_native_resume(tmp_path):
    from office import adapters
    for aid, want in (("claude", ["--resume", "S1"]), ("codex", ["resume", "S1"])):
        got = adapters.resume_argv(adapters.load_all()[aid], "worker", session_id="S1", model="m", effort="high",
                                   cwd=tmp_path)
        assert got and got[0][-2:] == want, (aid, got)
