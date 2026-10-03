"""Evidence ingestion for scope-none tasks: regular untracked files only, bounded reads."""
from __future__ import annotations

import itertools
import subprocess

from office import briefs, submit

_n = itertools.count()


def _save(env, monkeypatch, setup, started_at=None):
    """Run submit's evidence copy in a scratch repo; return the saved text or None."""
    wt = env.tmp / f"evwt{next(_n)}"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    setup(wt)
    dest = env.tmp / "evidence-out.md"
    dest.unlink(missing_ok=True)
    monkeypatch.setattr(briefs, "evidence_path", lambda run, did, rid: dest)
    submit._save_evidence({"id": "r"}, {"id": "d", "started_at": started_at}, wt, "R1")
    _save.wt = wt
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


def test_stale_evidence_from_an_earlier_dispatch_is_ignored(env, monkeypatch):
    from datetime import datetime, timedelta, timezone
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert _save(env, monkeypatch, lambda wt: (wt / briefs.EVIDENCE_FILE).write_text("stale"), started_at=later) is None
    earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert _save(env, monkeypatch, lambda wt: (wt / briefs.EVIDENCE_FILE).write_text("fresh"), started_at=earlier) == "fresh"
