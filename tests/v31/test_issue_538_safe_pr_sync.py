"""#538: restacked PR sync may rewrite only a proven Office-owned task revision."""
import sqlite3
import subprocess

from office import prs

RUN = {"id": "abcd1234-run"}
OLD = "a" * 40
NEW = "b" * 40
TASK = {"id": "T2", "current_revision_id": "R2"}
DISPATCH = {"branch": "office/abcd1234/T2", "worktree": "/tmp/worktree"}
REV = {"id": "R2", "commit_sha": NEW}


def db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE revisions (run_id TEXT, task_id TEXT, id TEXT, commit_sha TEXT)")
    con.executemany("INSERT INTO revisions VALUES (?,?,?,?)", [(RUN["id"], "T2", "R1", OLD),
                                                           (RUN["id"], "T2", "R2", NEW)])
    return con


def test_known_old_tip_updates_only_task_branch_with_explicit_lease(monkeypatch):
    calls = []
    monkeypatch.setattr(prs.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0,
                                                                                              OLD + "\trefs/heads/office/abcd1234/T2\n", ""))
    monkeypatch.setattr(prs, "push", lambda *a, **kw: (calls.append(kw) or True, ""))
    ok, _ = prs._safe_restack_push(db(), RUN, TASK, DISPATCH, REV, "rejected (non-fast-forward)")
    assert ok
    assert calls == [{"commit": NEW, "force": True, "expected": OLD}]


def test_unknown_or_shared_remote_tip_is_never_forced(monkeypatch):
    calls = []
    monkeypatch.setattr(prs.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0,
                                                                                              "c" * 40 + "\trefs/heads/office/abcd1234/T2\n", ""))
    monkeypatch.setattr(prs, "push", lambda *a, **kw: (calls.append(kw) or True, ""))
    for d, task, reason in [
        (DISPATCH, TASK, "non-fast-forward"),
        ({**DISPATCH, "branch": "shared-preview"}, TASK, "non-fast-forward"),
        (DISPATCH, {**TASK, "current_revision_id": "R3"}, "non-fast-forward"),
        (DISPATCH, TASK, "authentication failed"),
    ]:
        ok, detail = prs._safe_restack_push(db(), RUN, task, d, REV, reason)
        assert not ok and detail
    assert calls == []
