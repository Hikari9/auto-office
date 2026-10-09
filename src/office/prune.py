"""office prune: remove finished runs' local data, keep a tombstone.

`office prune` never mutates anything; it reports what `office prune -f`
would remove. `--run <id>` restricts both to that one run, and refuses when it
is unknown, not finished, or already pruned. With --force, each candidate is re-checked inside a runs.db
write transaction immediately before its data is deleted, so a run resumed or
reopened after the dry run is never touched. Unsafe or locked candidates are
skipped and reported. Running it twice is harmless.

Kept: one tombstone row in `runs` (id, terminal state, creating office_version,
requirements/plan/policy identity, terminal and prune timestamps, archive
receipt digest) plus the cross-run recorder tables that feed routing trust and
learning (dispatches, findings, validations, routing decisions, outcome labels).
Removed: worktrees, session bindings, the run directory (packets, briefs,
screenshots, traces, DOM extracts, logs, job files) and per-run lifecycle
detail rows.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from office import db, paths, state
from office.result import Result
from office.state import Refused, Usage
from office.util import now_iso, pid_alive, sha256_bytes, short

DETAIL_TABLES = ("tasks", "revisions", "gates", "amendments", "deliveries", "events", "cursors", "outbox", "evidence",
                 "plans", "requirements", "authorizations", "visual_refs", "deviations")


def _worktrees(run: dict) -> list[Path]:
    out = []
    root = paths.worktrees_dir() / run["id"][:8]
    if root.is_dir():
        out.extend(sorted(p for p in root.iterdir() if p.is_dir()))
    return out


def _bindings(run: dict) -> list[Path]:
    if not run.get("git_common_dir"):
        return []
    sessions = paths.primary_checkout(Path(run["git_common_dir"])) / ".office" / "sessions"
    out = []
    if sessions.is_dir():
        for p in sessions.glob("*.json"):
            try:
                if run["id"] in p.read_text(encoding="utf-8"):
                    out.append(p)
            except OSError:
                continue
    return out


def _eligibility(con, run: dict) -> str | None:
    """None if prunable, else the reason it is not."""
    if run.get("pruned_at") and run.get("prune_status") == "complete":
        return "already pruned"
    if run["phase"] not in state.TERMINAL_PHASES:
        return f"{run['phase']} (resumable)"
    if not run.get("terminal_at"):
        return "terminal state not recorded"
    live = con.execute("SELECT id, pid FROM dispatches WHERE run_id=? AND status IN ('launching','running')",
                       (run["id"],)).fetchall()
    if any(r["pid"] and pid_alive(r["pid"]) for r in live):
        return "an agent process is still running"
    busy = con.execute("SELECT 1 FROM outbox WHERE run_id=? AND status='claimed'", (run["id"],)).fetchall()
    if any(True for _ in busy):
        return "a job is still executing"
    for wt in _worktrees(run):
        if (wt / ".git").exists():
            dirty = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True)
            if dirty.returncode == 0 and dirty.stdout.strip() and not _recorded(con, run, wt):
                return f"worktree {wt.name} has changes no revision recorded"
    return None


def _recorded(con, run: dict, wt: Path) -> bool:
    """True when the worktree's full content equals the task's latest revision,
    so removing it loses nothing that was not captured at submit. Read-only."""
    from office.submit import worktree_equals_commit
    row = con.execute("SELECT commit_sha FROM revisions WHERE run_id=? AND task_id=? ORDER BY seq DESC LIMIT 1",
                      (run["id"], wt.name)).fetchone()
    if row is None:
        return False
    try:
        return worktree_equals_commit(wt, row["commit_sha"])
    except Exception:
        return False


def select_run(con, prefix: str) -> str:
    """`--run <id>`: the one finished, prunable run it names, else refuse.
    Naming a run must never widen to every finished run."""
    run = state.find_run(con, prefix)
    if run is None or not run.get("office_version"):
        raise Usage("unknown-run", f"no run {prefix!r}", next_step="office list --all shows run ids")
    reason = _eligibility(con, run)
    if reason == "already pruned":
        raise Refused("already-pruned", f"run {short(run['id'])} is already pruned", next_step="nothing to do")
    if run["phase"] not in state.TERMINAL_PHASES:
        raise Refused("run-not-finished", f"run {short(run['id'])} is {run['phase']}; only a closed or abandoned run "
                      "can be pruned", next_step=f"office close --run {short(run['id'])} first, or leave it")
    return run["id"]


def plan(con, *, cwd: Path | None = None, run_id: str | None = None) -> list[dict]:
    if run_id:
        rows = con.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchall()
    else:
        rows = con.execute("SELECT id FROM runs WHERE office_version IS NOT NULL ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        run = state.get_run(con, r["id"])
        reason = _eligibility(con, run)
        if reason == "already pruned":
            continue
        entry = {"run_id": run["id"], "phase": run["phase"], "goal": run["goal"], "eligible": reason is None,
                 "reason": reason, "worktrees": [str(p) for p in _worktrees(run)],
                 "bindings": [str(p) for p in _bindings(run)],
                 "run_dir": str(Path(run["state_dir"])) if run.get("state_dir") and Path(run["state_dir"]).exists() else None,
                 "bytes": _size(Path(run["state_dir"])) if run.get("state_dir") else 0}
        if reason is None or run["phase"] in state.TERMINAL_PHASES:
            out.append(entry)
    return out


def _size(p: Path) -> int:
    total = 0
    if p.exists():
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    return total


def dry_run(con, run_id: str | None = None) -> Result:
    candidates = plan(con, run_id=run_id) if run_id else plan(con)
    eligible = [c for c in candidates if c["eligible"]]
    skipped = [c for c in candidates if not c["eligible"]]
    res = Result()
    if not eligible:
        res.add("nothing to prune" + (f" | {len(skipped)} terminal run(s) not safe yet" if skipped else ""))
    else:
        mb = sum(c["bytes"] for c in eligible) / 1e6
        res.add(f"would prune {len(eligible)} finished run(s): {', '.join(short(c['run_id']) for c in eligible[:8])}"
                f"{' …' if len(eligible) > 8 else ''} | ~{mb:.1f} MB, {sum(len(c['worktrees']) for c in eligible)} worktree(s)")
        if skipped:
            res.add(f"would skip {len(skipped)}: " + "; ".join(f"{short(c['run_id'])} {c['reason']}" for c in skipped[:4]))
        res.add("active, paused, blocked, and resumable runs are never selected")
    only = f" --run {short(run_id)}" if run_id else ""
    res.next = f"office prune -f{only} to remove {'it' if run_id else 'them'} (tombstones stay in runs.db)" if eligible else None
    res.data = {"dry_run": True, "candidates": candidates}
    return res


def force(con, run_id: str | None = None) -> Result:
    candidates = plan(con, run_id=run_id) if run_id else plan(con)
    removed, skipped = [], []
    for c in candidates:
        if not c["eligible"]:
            skipped.append((c["run_id"], c["reason"]))
            continue
        outcome = _prune_one(con, c["run_id"])
        (removed if outcome is None else skipped).append(c["run_id"] if outcome is None else (c["run_id"], outcome))
    res = Result()
    res.add(f"pruned {len(removed)} run(s)" + (f": {', '.join(short(r) for r in removed[:8])}" if removed else "")
            + (f" | skipped {len(skipped)}" if skipped else ""))
    for rid, why in skipped[:5]:
        res.add(f"skipped {short(rid)}: {why}")
    res.data = {"dry_run": False, "removed": removed, "skipped": [{"run_id": r, "reason": w} for r, w in skipped]}
    return res


def _prune_one(con, run_id: str) -> str | None:
    """Delete one run's local data. Returns None on success, else a reason."""
    try:
        with db.transaction(con):
            run = state.get_run(con, run_id)
            reason = _eligibility(con, run)
            if reason is not None:
                return f"no longer eligible: {reason}"
            # Claim the prune in the same transaction that re-checked
            # eligibility; a later `resume` of a terminal run is refused.
            state.update_run(con, run_id, prune_status="in_progress", pruned_at=now_iso())
    except Exception as exc:  # pragma: no cover - lock contention
        return f"locked: {exc}"
    problems = []
    for wt in _worktrees(run):
        proc = subprocess.run(["git", "-C", run["repo_root"], "worktree", "remove", "--force", str(wt)],
                              capture_output=True, text=True)
        if proc.returncode != 0 and wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        if wt.exists():
            problems.append(f"worktree {wt.name} not removed")
    if run.get("repo_root") and Path(run["repo_root"]).exists():
        subprocess.run(["git", "-C", run["repo_root"], "worktree", "prune"], capture_output=True)
        refs = subprocess.run(["git", "-C", run["repo_root"], "for-each-ref", "--format=%(refname)",
                               f"refs/office/{run_id[:8]}/"], capture_output=True, text=True).stdout.split()
        for ref in refs:
            subprocess.run(["git", "-C", run["repo_root"], "update-ref", "-d", ref], capture_output=True)
    for b in _bindings(run):
        try:
            b.unlink()
        except OSError:
            problems.append(f"binding {b.name} not removed")
    wt_root = paths.worktrees_dir() / run_id[:8]
    shutil.rmtree(wt_root, ignore_errors=True)
    sdir = Path(run["state_dir"]) if run.get("state_dir") else None
    if sdir and sdir.exists():
        shutil.rmtree(sdir, ignore_errors=True)
        if sdir.exists():
            problems.append("run directory not fully removed")
    # Preserve bug evidence before the details (events/outbox/evidence) disappear.
    try:
        from office import bugwatch
        bugwatch.capture(con, run_id, force=True)
        bugwatch.start_reporter()
    except Exception:
        pass  # reporting never prevents legitimate cleanup
    with db.transaction(con):
        final_plan = state.current_plan(con, run_id)
        req = con.execute("SELECT frozen_json FROM requirements WHERE run_id=? ORDER BY version DESC LIMIT 1",
                          (run_id,)).fetchone()
        tombstone = {
            "terminal_state": run["phase"], "office_version": run["office_version"],
            "requirements_version": run["requirements_version"],
            "requirements_hash": sha256_bytes(req["frozen_json"].encode()) if req else None,
            "plan_version": run["plan_version"], "plan_hash": final_plan["content_hash"] if final_plan else None,
            "policy_hash": run["policy_hash"], "config_hash": run["config_hash"],
            "terminal_at": run["terminal_at"], "archive_digest": run["archive_digest"],
            "accepted": {r["id"]: r["commit_sha"] for r in con.execute(
                "SELECT t.id, v.commit_sha FROM tasks t JOIN revisions v ON v.id=t.accepted_revision_id WHERE t.run_id=?",
                (run_id,)).fetchall()},
        }
        landing = dict(run.get("landing") or {})
        landing["tombstone"] = tombstone
        state.update_run(con, run_id, landing=landing)
        for table in DETAIL_TABLES:
            con.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        con.execute("DELETE FROM session_bindings WHERE run_id=?", (run_id,))
        con.execute("UPDATE leases SET released_at=COALESCE(released_at, ?) WHERE run_id=?", (now_iso(), run_id))
        state.update_run(con, run_id, prune_status="complete" if not problems else "partial: " + "; ".join(problems)[:300],
                         pruned_at=now_iso())
    return None if not problems else "; ".join(problems)
