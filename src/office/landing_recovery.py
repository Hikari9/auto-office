"""Evidence for closing already-landed squash PRs, independent of commit ancestry.

Only the current accepted integration and its frozen checks can be used. A
task PR proves its own recorded head; exact tree equality is a separate proof
for work landed through another PR. Neither proof creates a merge parent or
an authorization, and both re-run checks on the actual merged commit.
"""
from __future__ import annotations

import contextlib
import subprocess
import uuid

from office import db, gates, integration, jobs, land, paths, prs, state
from office.state import Refused
from office.util import now_iso, sha256_obj


def _snapshot(con, run: dict) -> dict:
    current = state.get_run(con, run["id"])
    return {"integration": land._snapshot(con, current), "phase": current["phase"], "plan_version": current["plan_version"],
            "requirements_version": current["requirements_version"],
            "run_checks": (current.get("landing") or {}).get("run_checks") or [],
            "prs": (current.get("landing") or {}).get("prs") or {},
            "review_gates": [tuple(g) for g in con.execute(
                "SELECT id, kind, status, verdict FROM gates WHERE run_id=? AND subject<>'landing-recovery' ORDER BY id",
                (run["id"],))],
            "tasks": [{k: t.get(k) for k in ("id", "status", "accepted_revision_id", "checks", "pr")}
                      for t in state.tasks(con, run["id"])]}


def require_current(con, run: dict, snapshot: dict) -> None:
    """Recheck under the closing transaction as well as after external checks."""
    if _snapshot(con, run) != snapshot or gates.close_blockers(con, run):
        raise Refused("landing-proof-stale", "the accepted work, requirements, PRs or review gates changed during "
                      "landing verification", preserved="the open run and the merged PRs",
                      next_step="office status, then retry close after the current integration is accepted")


def _git(run: dict, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", run["repo_root"], *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused("landing-unverified", f"cannot verify the landed Git history: {exc}", preserved="the open run")


def _ancestor(run: dict, older: str, newer: str) -> bool:
    return _git(run, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _task_prs(con, run: dict, selected: dict, tasks: list[dict], *, equal_tree: bool) -> dict | None:
    from office.lifecycle import _merged_pr
    owned = [t for t in tasks if prs.has_pr(t)]
    if len(owned) != len(tasks) or not owned or not any((t.get("pr") or {}).get("url") == selected["url"] for t in owned):
        return None
    proven = {}
    for task in owned:
        local = task.get("pr") or {}
        branch = f"office/{run['id'][:8]}/{task['id']}"
        if not local.get("number") or not local.get("url") or local.get("branch") != branch:
            return None
        try:
            remote = selected if local["url"] == selected["url"] else _merged_pr(run, local["url"])
        except Refused:
            return None
        if (remote["number"] != local["number"] or remote["url"] != local["url"]
                or remote.get("branch") != branch or remote.get("base") != selected["base"]
                or not _ancestor(run, remote["merge_commit"], selected["merge_commit"])):
            return None
        accepted = land._accepted_commit(con, task)
        source = "accepted" if remote.get("head_commit") == accepted else None
        # Historical restack/recovery records predate accepted-revision IDs on
        # those records. They can be used only with an independent final-tree
        # proof, so a stale recorded push can never vouch for a new acceptance.
        if source is None and equal_tree:
            source = next((k for k in ("restacked", "recovering", "recovered")
                           if remote.get("head_commit") and remote["head_commit"] == (local.get(k) or {}).get("commit")), None)
        if source is None:
            return None
        proven[task["id"]] = {"revision": task["accepted_revision_id"], "accepted_commit": accepted,
                              "head_source": source, "pr": remote}
    return proven


def _checks(con, run: dict, commit: str, commands: list[str], snapshot: dict) -> dict:
    token = uuid.uuid4().hex[:12]
    gid, rid, name = "G" + token, "L" + token, "landing-verify-" + token
    with db.transaction(con):
        con.execute("INSERT INTO gates(id, run_id, subject, revision_id, plan_version, kind, input_key, status, "
                    "created_at, started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (gid, run["id"], "landing-recovery", rid, run["plan_version"], "checks",
                     f"landing-recovery:{commit}", "queued", now_iso(), None))
        jid = state.enqueue(con, run, "landing_verify", {"gate_id": gid, "revision_id": rid, "checkout": name,
                            "commit": commit, "commands": commands, "proof_key": sha256_obj(snapshot)},
                            dedup_key=f"landing-verify:{run['id']}:{token}", max_attempts=1)
    # Keep close synchronous, with the same durable claim and inherited child
    # process lock as every other check job. A killed CLI cannot leave an
    # unowned check suite alongside the next recovery attempt.
    jobs.execute(con, jid)
    job = state.get_job(con, jid)
    if job["status"] in ("queued", "claimed"):
        raise Refused("landing-checks-running", "landing verification is still running or cannot yet acquire its "
                      "execution lock", preserved="the open run and the running checks", next_step="office wait, then retry close")
    evidence = {"job_id": jid, "gate_id": gid, **(job.get("result") or {
        "verdict": "UNAVAILABLE", "summary": job.get("error") or "landing checks did not complete", "results": []})}
    if job["status"] != "done" or evidence.get("verdict") != "PASS":
        raise Refused("landing-checks-failed", f"checks on merged {commit[:12]}: {evidence['summary']}",
                      preserved="the open run and the merged PRs",
                      next_step=f"inspect {paths.run_dir(run['id']) / 'evidence' / 'integration' / rid}; "
                                "fix forward or restore the checks, then retry close")
    return evidence


def job_verify(con, run: dict, job: dict) -> dict:
    """A fenced outbox attempt; only its first terminal gate result counts."""
    payload = job["payload"]
    gid, commit, rid, name = (payload[k] for k in ("gate_id", "commit", "revision_id", "checkout"))
    checkout = paths.run_dir(run["id"]) / "checkouts" / name
    outcome = {"verdict": "UNAVAILABLE", "summary": "landing verification interrupted", "results": []}
    with db.transaction(con):
        if sha256_obj(_snapshot(con, run)) != payload["proof_key"]:
            raise Refused("landing-proof-stale", "accepted work changed before the landing checks started")
        changed = con.execute("UPDATE gates SET status='running', started_at=? WHERE id=? AND status='queued'",
                              (now_iso(), gid))
        if not changed.rowcount:
            return {"verdict": "STALE", "summary": "the landing gate was already settled", "results": []}
    try:
        gates.detached_checkout(run, commit, name, purpose="check")
        outcome = gates.run_commands(con, run, payload["commands"], checkout,
                                     {"id": rid, "commit_sha": commit, "dispatch_id": None}, {"id": gid})
    finally:
        with db.transaction(con):
            con.execute("UPDATE gates SET status='done', verdict=?, summary=?, finished_at=? "
                        "WHERE id=? AND status='running'", (outcome["verdict"], outcome["summary"], now_iso(), gid))
        with contextlib.suppress(OSError):
            gates.remove_checkout(run, checkout)
    return {k: outcome.get(k) for k in ("verdict", "summary", "results")}


def _reclaim_checks(con, run: dict) -> None:
    with db.transaction(con):
        jobs.reclaim(con, run["id"])
        pending = [state.get_job(con, r["id"]) for r in con.execute(
            "SELECT id FROM outbox WHERE run_id=? AND kind='landing_verify' AND status IN ('queued','claimed')",
            (run["id"],))]
        for job in pending:
            if job["status"] == "queued":
                # No attempt owns it. A fresh proof gets fresh checks instead
                # of executing commands bound to an interrupted close's state.
                con.execute("UPDATE outbox SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                            (now_iso(), job["id"]))
                con.execute("UPDATE gates SET status='stale', stale_reason='superseded by fresh landing proof', finished_at=? "
                            "WHERE id=? AND status IN ('queued','running')", (now_iso(), job["payload"]["gate_id"]))
    if any(j["status"] == "claimed" for j in pending):
        raise Refused("landing-checks-running", "a previous landing verification still has a live or unknown owner",
                      preserved="the open run and the running checks", next_step="office wait, then retry close")


def prove(con, run: dict, selected: dict) -> tuple[dict, dict] | None:
    """Called while holding land's process lock; does not merge, deploy or close.

    A missing proof returns None for the existing landing-unproven diagnostic.
    A failed fetch or failed checks refuses rather than trusting stale evidence.
    """
    _reclaim_checks(con, run)
    with db.transaction(con):
        run = state.get_run(con, run["id"])
        snapshot = _snapshot(con, run)
        integrated = snapshot["integration"]["commit"]
        tasks = integration.accepted_set(con, run)
        if not integrated or not tasks or gates.close_blockers(con, run):
            return None
    base = snapshot["prs"].get("base_branch")
    if not base or selected.get("base") != base:
        return None
    # An isolated fetched ref cannot be replaced by an unrelated concurrent
    # fetch. Never infer landing from a stale origin/<base> after a failure.
    ref = f"refs/office/landing-proof/{run['id']}/{uuid.uuid4().hex}"
    try:
        fetched = _git(run, "fetch", "-q", "origin", f"+refs/heads/{base}:{ref}")
        if fetched.returncode:
            raise Refused("landing-fetch-failed", f"could not fetch origin/{base}: {fetched.stderr.strip()[:200]}",
                          preserved="the open run", next_step="restore Git access, then retry close")
        default = _git(run, "rev-parse", "--verify", ref).stdout.strip()
        if not default or not _ancestor(run, selected["merge_commit"], default):
            return None
        tree = _git(run, "rev-parse", "--verify", f"{selected['merge_commit']}^{{tree}}")
        reviewed = _git(run, "rev-parse", "--verify", f"{integrated}^{{tree}}")
        if tree.returncode or reviewed.returncode:
            return None
        equal = tree.stdout.strip() == reviewed.stdout.strip()
        proven = _task_prs(con, run, selected, tasks, equal_tree=equal)
        if proven is None and not equal:
            return None
        proof = {"kind": "office-task-prs" if proven is not None else "reviewed-tree",
                 "merge_commit": selected["merge_commit"], "default_commit": default, "base": base,
                 "integration_commit": integrated, "tree": tree.stdout.strip(), "reviewed_tree": reviewed.stdout.strip(),
                 "tree_equal": equal, "requirements_version": snapshot["requirements_version"],
                 "plan_version": snapshot["plan_version"],
                 "tasks": proven if proven is not None else {
                     t["id"]: {"revision": t["accepted_revision_id"], "accepted_commit": land._accepted_commit(con, t)}
                     for t in tasks}}
        require_current(con, run, snapshot)
        land._require_run_coverage(run, integrated, con=con)
        commands = list(dict.fromkeys([*snapshot["run_checks"], *(cmd for t in tasks for cmd in t["checks"])]))
        proof["checks"] = _checks(con, run, selected["merge_commit"], commands, snapshot)
        require_current(con, run, snapshot)
        proof["verified_at"] = now_iso()
        return proof, snapshot
    finally:
        with contextlib.suppress(Refused):
            _git(run, "update-ref", "-d", ref)
