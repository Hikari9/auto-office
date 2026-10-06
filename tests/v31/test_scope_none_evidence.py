"""Evidence ingestion for scope-none tasks: regular untracked files only, bounded reads."""
from __future__ import annotations

import itertools
import subprocess

from office import briefs, submit

_n = itertools.count()


def _save(env, monkeypatch, setup):
    """Run submit's evidence copy in a scratch repo; return the saved text or None."""
    wt = env.tmp / f"evwt{next(_n)}"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    setup(wt)
    dest = env.tmp / "evidence-out.md"
    dest.unlink(missing_ok=True)
    monkeypatch.setattr(briefs, "evidence_path", lambda run, did, rid: dest)
    _save.wt = wt
    d = {"id": "d"}
    with submit._evidence_commit() as staged:
        evidence = submit._read_evidence(wt)
        if evidence:
            submit._stage_evidence({"id": "r"}, d, "R1", evidence, staged)
    return dest.read_text() if dest.exists() else None


def test_evidence_symlink_is_not_followed(env, monkeypatch):
    secret = env.tmp / "secret.txt"
    secret.write_text("TOKEN=abc")
    assert _save(env, monkeypatch, lambda wt: (wt / briefs.EVIDENCE_FILE).symlink_to(secret)) is None


def test_tracked_evidence_file_is_not_fresh_evidence(env, monkeypatch):
    def setup(wt):
        (wt / briefs.EVIDENCE_FILE).write_text("old")
        subprocess.run(["git", "-C", str(wt), "add", briefs.EVIDENCE_FILE], check=True)
    assert _save(env, monkeypatch, setup) is None


def test_untracked_evidence_is_saved(env, monkeypatch):
    assert _save(env, monkeypatch, lambda wt: (wt / briefs.EVIDENCE_FILE).write_text("posted")) == "posted"


def test_oversized_evidence_is_truncated_and_marked(env, monkeypatch):
    big = "x" * (briefs.EVIDENCE_MAX_CHARS * 10)
    out = _save(env, monkeypatch, lambda wt: (wt / briefs.EVIDENCE_FILE).write_text(big))
    assert out.startswith("x" * briefs.EVIDENCE_MAX_CHARS) and "[evidence truncated" in out
    assert len(out) < briefs.EVIDENCE_MAX_CHARS + 100


def test_evidence_is_consumed_at_submit(env, monkeypatch):
    assert _save(env, monkeypatch, lambda wt: (wt / briefs.EVIDENCE_FILE).write_text("posted")) == "posted"
    assert not (_save.wt / briefs.EVIDENCE_FILE).exists()


def test_hard_linked_evidence_is_refused(env, monkeypatch):
    import os
    secret = env.tmp / "secret.txt"
    secret.write_text("TOKEN=abc")
    assert _save(env, monkeypatch, lambda wt: os.link(secret, wt / briefs.EVIDENCE_FILE)) is None


def test_rollback_keeps_the_worktree_file_and_discards_the_staged_copy(env, monkeypatch):
    import pytest
    wt = env.tmp / "evwt-rollback"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / briefs.EVIDENCE_FILE).write_text("posted")
    dest = env.tmp / "evidence-rollback.md"
    monkeypatch.setattr(briefs, "evidence_path", lambda run, did, rid: dest)
    with pytest.raises(RuntimeError):
        with submit._evidence_commit() as staged:
            submit._stage_evidence({"id": "r"}, {"id": "d"}, "R1", submit._read_evidence(wt), staged)
            assert dest.read_text() == "posted"
            raise RuntimeError("commit failed")
    assert (wt / briefs.EVIDENCE_FILE).read_text() == "posted" and not dest.exists()


EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
FINDING = ("VERDICT: RECHECK\nFINDING F1 | medium | blocking | issue #7 | the comment omits the summary | post it"
           " | owner: T1\nNEXT repost the comment")
APPROVED = "VERDICT: APPROVED\nNEXT proceed"


def _dispatched(env, tid="T1"):
    from pathlib import Path
    from conftest import task_row
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env, tid)["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor"}, Path(d["worktree"]), d


def _comment_only_run(env, reviews):
    from conftest import start_inline
    from test_scope_none import PLAN_ONLY_COMMENT
    env.trust()
    env.script(convergence_reviewer=[{"reply": r} for r in reviews])
    start_inline(env, plan=PLAN_ONLY_COMMENT, extra=("--no-prs",))
    env.office("approve", "plan", "--quote", "go", check=0)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    return _dispatched(env)


def _end_and_rerun(env, con, first):
    con.execute("UPDATE dispatches SET status='exited', ended_at='x' WHERE id=?", (first["id"],))  # the session ends
    con.commit()
    env.office("rerun", "T1", "--fresh", env=EXTERNAL, check=0)
    wenv, wt, second = _dispatched(env)
    assert second["id"] != first["id"]
    return wenv, wt, second


def test_unsubmitted_evidence_from_a_crashed_dispatch_is_moved_aside_at_launch(env):
    """A crashed dispatch leaves a fresh-looking, never-submitted file. The next
    launch moves it out of the worktree, so it cannot reach the reviewer."""
    import os
    from office import paths
    wenv, wt, first = _comment_only_run(env, [APPROVED])
    ev = wt / briefs.EVIDENCE_FILE
    ev.write_text("half-done: comment not posted yet\n")
    os.utime(ev)  # touched: newer than anything, so no timestamp could flag it
    con = env.con()
    wenv, wt2, second = _end_and_rerun(env, con, first)
    assert wt2 == wt and not ev.exists()
    aside = paths.run_dir(second["run_id"]) / "dispatches" / second["id"] / "stale-evidence.md"
    assert aside.read_text() == "half-done: comment not posted yet\n"
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out, out
    texts = [p.read_text(errors="replace") for p in env.state.rglob("*") if p.is_file() and p.suffix in (".md", ".txt")]
    reviewer = [t for t in texts if "EXECUTOR EVIDENCE" in t]
    assert reviewer and all("half-done" not in t and "none recorded" in t for t in reviewer), reviewer


def test_resubmitted_ingested_evidence_is_refused_and_fresh_content_passes(env):
    """Second guard: content already ingested for the task is refused even when
    the executor writes it again during the new dispatch."""
    from conftest import task_row
    wenv, wt, first = _comment_only_run(env, [FINDING, APPROVED])
    ev = wt / briefs.EVIDENCE_FILE
    ev.write_text("comment https://github.com/o/r/issues/7#c1: shipped\n")
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out, out
    assert not ev.exists() and task_row(env)["status"] == "changes_required"
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM evidence WHERE kind='executor_evidence' AND sha256 IS NOT NULL").fetchone()[0] == 1
    wenv, wt, second = _end_and_rerun(env, con, first)
    ev.write_text("comment https://github.com/o/r/issues/7#c1: shipped\n")
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code != 0 and "stale-evidence" in out and "redo or reconfirm" in out, out
    assert ev.exists()  # nothing consumed: the executor rewrites it
    assert con.execute("SELECT COUNT(*) FROM revisions WHERE task_id='T1'").fetchone()[0] == 1
    ev.write_text("comment https://github.com/o/r/issues/7#c2 (reposted with the summary): shipped\n")
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "captured" in out, out
    assert not ev.exists()
    assert con.execute("SELECT COUNT(*) FROM evidence WHERE kind='executor_evidence'").fetchone()[0] == 2
    assert task_row(env)["status"] == "accepted"


def test_evidence_saved_before_digests_were_recorded_still_counts(env):
    """A copy from an older build has no evidence row; its saved file is hashed instead."""
    from office import state
    wenv, wt, d = _comment_only_run(env, [APPROVED])
    (wt / briefs.EVIDENCE_FILE).write_text("posted\n")
    env.office("submit", cwd=wt, env=wenv, check=0)
    con = env.con()
    con.execute("DELETE FROM evidence WHERE kind='executor_evidence'")
    con.commit()
    run = state.get_run(con, d["run_id"])
    assert submit._read_evidence(wt) is None  # consumed
    (wt / briefs.EVIDENCE_FILE).write_text("posted\n")
    _, _, digest = submit._read_evidence(wt)
    assert digest in submit._ingested_digests(con, run, "T1")
