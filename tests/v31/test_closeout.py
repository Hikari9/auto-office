"""`office close` as a /cleanup closeout: base sync, run worktree removal, docs warning, report line."""
from __future__ import annotations

from pathlib import Path

from conftest import GOOD_ADD, approved_run
from test_land import _plan, _run
from test_task_prs import remote_head

DONE = "office close done — "


def _last(out: str) -> str:
    return out.rstrip().splitlines()[-1]


def _merged(env, monkeypatch):
    bare, _, _ = _run(env, monkeypatch, _plan("merge"))
    env.office("land", check=0)
    return bare


def _office_worktrees(env) -> list[str]:
    listing = env.git("worktree", "list", "--porcelain")
    return [l for l in listing.splitlines() if l.startswith("worktree ") and "/worktrees/" in l]


def test_merged_close_syncs_checked_out_clean_base_and_removes_run_worktrees(env, monkeypatch):
    bare = _merged(env, monkeypatch)
    other = env.tmp / "mine"
    env.git("worktree", "add", "-q", "-b", "mine", str(other))
    assert _office_worktrees(env)
    code, out = env.office("close")
    assert code == 0, out
    assert env.git("rev-parse", "main").strip() == remote_head(bare, "main"), out
    assert "local main == origin/main" in out or "already at origin/main" in out, out
    assert "worktree(s) removed" in _last(out) and _last(out).startswith(DONE + "merged, main synced"), out
    assert not [w for w in _office_worktrees(env) if "/mine" not in w], out
    assert other.is_dir() and env.git("branch", "--list", "mine").strip(), "a worktree Office did not create is kept"
    assert not env.git("branch", "--list", "office/*/T*").strip(), out  # merged task branches: git branch -d
    assert "kept branch(es) git branch -d refused" in out, out  # never -D


def test_base_not_checked_out_is_fast_forwarded_by_ref(env, monkeypatch):
    bare = _merged(env, monkeypatch)
    before = env.git("rev-parse", "main").strip()
    env.git("checkout", "-q", "-b", "side", before)
    code, out = env.office("close")
    assert code == 0, out
    assert env.git("rev-parse", "main").strip() == remote_head(bare, "main") != before, out
    assert "fast-forwarded" in out and "local main == origin/main" in out, out


def test_dirty_checked_out_base_is_left_alone_and_the_user_asked(env, monkeypatch):
    bare = _merged(env, monkeypatch)
    before = env.git("rev-parse", "main").strip()
    assert before != remote_head(bare, "main")
    (env.repo / "README.md").write_text("local edit\n")
    code, out = env.office("close")
    assert code == 0, out
    assert env.git("rev-parse", "main").strip() == before
    assert (env.repo / "README.md").read_text() == "local edit\n"
    assert "base sync skipped" in out and "ask the user (native question tool) whether to fast-forward main" in out, out
    assert _last(out).startswith(DONE) and "main sync skipped" in _last(out), out


def test_docs_warning_when_code_changes_without_the_kept_docs(env, monkeypatch):
    _merged(env, monkeypatch)
    code, out = env.office("close")
    assert code == 0 and "warning: the diff changes code but none of README.md" in out, out


def test_docs_warning_is_quiet_when_a_kept_doc_changed(env):
    from office import closeout
    base = env.git("rev-parse", "HEAD").strip()
    (env.repo / "calc.py").write_text(GOOD_ADD)
    env.git("commit", "-qam", "code")
    assert closeout.docs_warning(env.repo, base, env.git("rev-parse", "HEAD").strip())
    (env.repo / "README.md").write_text("documented\n")
    env.git("commit", "-qam", "docs")
    assert closeout.docs_warning(env.repo, base, env.git("rev-parse", "HEAD").strip()) is None


def test_handoff_keeps_worktrees_and_reports(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    kept = _office_worktrees(env)
    code, out = env.office("close", "--handoff", "https://example.test/pr/1")
    assert code == 0 and "handed off" in out, out
    assert _last(out) == DONE + "handed off https://example.test/pr/1, worktrees kept", out
    assert _office_worktrees(env) == kept
    code, out = env.office("close", "--handoff", "https://example.test/pr/1")
    assert code != 0 and _last(out).startswith(DONE + "stopped early (no-active-run)"), out


def test_refused_close_still_ends_with_the_report_line(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("close")
    assert code == 4 and "landing not recorded" in out, out
    assert _last(out) == DONE + "stopped early (close-blocked); nothing closed or cleaned up", out
    assert Path(env.repo).is_dir()
