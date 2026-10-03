"""`office land`: take accepted work as far as the user's end state says (3.2).

End states (plan requirements `end_state:`, the user's intake answer):
  ask      open PRs, then ask the user: merge, preview deploy, end to end, or stop.
  preview  deploy the verified integration to preview and verify it; PRs stay open.
  merge    merge the task PRs bottom-up.
  e2e      merge, deploy prod from the merged default branch, verify.

The approved plan carries the end state and deploy commands, so the plan
authorization is the authority for them. In `ask` mode the user's later answer
is recorded with `office land --merge|--preview|--e2e --quote "<words>"`.

Merging is bottom-up in the plan's order. A child PR is retargeted to the
default branch once its parent merged; when the repository forces squash or
rebase merges, the child is rebased onto the new base and force-pushed with
lease first. Required checks must pass before each merge, and a branch that
must be up to date is updated once. After the last merge the default branch's
tree is compared with the reviewed integration tree; if main moved meanwhile
the run checks re-run on it before any prod deploy.

`office land --rebase` moves a run onto a default branch that moved after
`office start`: when every accepted revision still merges cleanly onto the
new head, integration re-composes there and re-runs the run checks plus an
independent integration review. A conflict refuses with the by-hand steps.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from office import db, gates, integration, paths, prs, state
from office.result import Result
from office.state import Refused, Usage
from office.util import now_iso, short

MODES = ("preview", "merge", "e2e")
CHECKS_TIMEOUT = int(os.environ.get("OFFICE_PR_CHECKS_TIMEOUT", "1800"))


def end_state(con, run: dict) -> dict:
    frozen = state.current_requirements(con, run["id"])["frozen"]
    return {"mode": frozen.get("end_state") or "ask", "deploy": frozen.get("deploy") or {}}


def land(con, run: dict, *, mode: str | None = None, quote: str | None = None) -> Result:
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-land", "a worker cannot land the run")
    commit = integration.final_commit(con, run)
    if not commit:
        blockers = gates.close_blockers(con, run)
        raise Refused("not-ready", "nothing verified to land: " + ("; ".join(blockers[:3]) or "integration not accepted"),
                      next_step="office status")
    es = end_state(con, run)
    if mode:
        if mode not in MODES:
            raise Usage("unknown-mode", f"land mode must be one of {', '.join(MODES)}")
        if not quote or len(re.sub(r"\s+", "", quote)) < 2:
            raise Usage("user-quote-required", "landing beyond PRs records the user's own words",
                        next_step=f'office land --{mode} --quote "<user\'s exact words>"')
        _authorize(con, run, mode, quote)
    else:
        mode = es["mode"]
    if mode == "ask":
        return _ask(con, run)
    if mode in ("preview", "e2e") and not es["deploy"].get("preview" if mode == "preview" else "prod"):
        raise Refused("no-deploy-command", f"{mode} needs a confirmed deploy_{'preview' if mode == 'preview' else 'prod'} "
                      "command in the plan requirements", next_step='office amend requirements --quote "<words>" -- '
                      '"deploy_prod: <command>" (office land --detect proposes one)')
    res = Result()
    if mode == "preview":
        wt = paths.worktrees_dir() / run["id"][:8] / "_integration"
        _deploy(con, run, "preview", es["deploy"], wt, res)
        _record(con, run, delivered=f"preview deployed from {commit[:12]}; PRs left open for review")
        res.next = "office close (PRs stay open for the user to merge)"
        return res
    if not prs.enabled(run) and not any(prs.has_pr(t) for t in integration.accepted_set(con, run) or []):
        # PRs are off and no accepted task has file scope: nothing to merge or deploy.
        res.add("nothing to merge: no accepted task has a PR (all have no file scope)")
        _record(con, run, delivered="no task PRs to merge")
        res.next = "office close"
        return res
    before = _merge_all(con, run, res)
    main = _verify_main(con, run, commit, res)
    if mode == "e2e":
        checkout = gates.detached_checkout(run, main, "deploy-prod", purpose="deploy")
        try:
            _deploy(con, run, "prod", es["deploy"], checkout, res, rollback=before)
        finally:
            gates.remove_checkout(run, checkout)
    _close_issue(con, run, main, res)
    _record(con, run, merged={"commit": main, "rollback": before, "at": now_iso()})
    res.next = "office close"
    return res


# ------------------------------------------------------------------ rebase

COMPOSE_BY_HAND = ("compose by hand: a scratch worktree from origin/{base}, cherry-pick each accepted task's commits, "
                   "resolve the conflicts, run the full suite, have an independent reviewer check the resolution, open "
                   "one PR, close the task PRs as superseded, then office close --abandon \"landed as #<N>\"")


def rebase(con, run: dict) -> Result:
    if os.environ.get("OFFICE_DISPATCH_ID"):
        raise Refused("worker-cannot-land", "a worker cannot rebase the run")
    tasks = integration.accepted_set(con, run)
    if tasks is None:
        raise Refused("not-ready", "rebase needs every task accepted", next_step="office status")
    base = prs.settings(con, run).get("base_branch") or "main"
    if any((t.get("pr") or {}).get("merged") for t in tasks):
        raise Refused("already-merging", "some task PRs are merged; office land restacks the rest itself",
                      next_step="office land")
    repo = Path(run["repo_root"])
    if _git(repo, "fetch", "-q", "origin", base).returncode != 0:
        raise Refused("fetch-failed", f"could not fetch origin/{base}")
    new, old = paths.git(repo, "rev-parse", f"origin/{base}"), integration.compose_base(run)
    if _git(repo, "merge-base", "--is-ancestor", new, old).returncode == 0:
        return Result(lines=[f"already on origin/{base} {new[:12]}; nothing to rebase"], next="office land")
    if _git(repo, "merge-base", "--is-ancestor", old, new).returncode != 0:
        raise Refused("base-diverged", f"origin/{base} {new[:12]} does not contain the run base {old[:12]}",
                      next_step=COMPOSE_BY_HAND.format(base=base))
    checkout = gates.detached_checkout(run, new, "rebase-trial", purpose="rebase-trial")
    try:
        genv = dict(os.environ, **paths.commit_identity_env(repo))
        for t in tasks:
            commit = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (t["accepted_revision_id"],)).fetchone()[0]
            proc = subprocess.run(["git", "-C", str(checkout), "merge", "--no-ff", "--no-edit", commit],
                                  capture_output=True, text=True, env=genv)
            if proc.returncode != 0:
                files = paths.git(checkout, "diff", "--name-only", "--diff-filter=U", check=False).replace("\n", ", ")
                raise Refused("rebase-conflict", f"{t['id']} conflicts with origin/{base} {new[:12]} on {files or 'files'}",
                              preserved="the run, its PRs, and its accepted integration",
                              next_step=COMPOSE_BY_HAND.format(base=base))
    finally:
        gates.remove_checkout(run, checkout)
    with db.transaction(con):
        _record(con, run, rebase={"from": old, "onto": new, "at": now_iso()})
        run = state.get_run(con, run["id"])
        integration._set_integration(con, run, status="pending", detail=f"rebasing onto {new[:12]}")
        state.emit(con, run, "integration.rebase", f"rebased onto origin/{base} {new[:12]}; integration re-check queued")
        integration.retrigger(con, run)
    from office import jobs
    jobs.kick(con, run["id"])
    return Result(lines=[f"every accepted task merges cleanly onto origin/{base} {new[:12]}",
                         "integration re-composes there and re-runs the run checks and an integration review"],
                  next="office status (then office land once integration is accepted)")


# ------------------------------------------------------------------ ask

def _ask(con, run: dict) -> Result:
    lines = ["integration verified; the task PRs are ready:"]
    for t in integration._topo(integration.accepted_set(con, run) or []):
        if not prs.has_pr(t):
            continue
        pr = t.get("pr") or {}
        lines.append(f"  {t['id']} {pr.get('url') or '(no PR: ' + _pr_reason(run) + ')'}")
    return Result(lines=lines, next='ask the user (native question tool): merge | preview deploy | merge + prod | stop; '
                  'then office land --merge|--preview|--e2e --quote "<words>", or office close --handoff <pr-url>')


def _pr_reason(run: dict) -> str:
    return ((run.get("landing") or {}).get("prs") or {}).get("reason") or "PRs off"


def _authorize(con, run: dict, mode: str, quote: str) -> None:
    import uuid
    from office.util import dumps
    kinds = {"preview": ["deploy"], "merge": ["merge"], "e2e": ["merge", "deploy"]}[mode]
    with db.transaction(con):
        for kind in kinds:
            con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, envelope_json, "
                        "authorized_by, quote, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("Z" + uuid.uuid4().hex[:8], run["id"], kind, f"land:{mode}", run["requirements_version"],
                         dumps(run.get("envelope") or []), "user", quote.strip(), now_iso()))
        state.emit(con, run, "authority.land", f"user authorized land {mode}", audience="runtime")


# ------------------------------------------------------------------ merge

def _gh(run: dict, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=run["repo_root"], capture_output=True, text=True, timeout=timeout)


def _git(cwd, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _merge_all(con, run: dict, res: Result) -> str:
    """Merge every task PR bottom-up. Returns the default branch's commit
    before the first merge (the rollback target)."""
    run = state.get_run(con, run["id"])
    s = prs.settings(con, run)
    if not s.get("enabled"):
        raise Refused("prs-off", f"merging needs task PRs, which are off ({s.get('reason')})",
                      next_step="push the integration branch, open a PR, then office close --handoff <pr-url>")
    base, method = s["base_branch"], s["merge_method"]
    repo = Path(run["repo_root"])
    _git(repo, "fetch", "-q", "origin", base)
    before = paths.git(repo, "rev-parse", f"origin/{base}")
    merged: list[str] = []
    for t in integration._topo(integration.accepted_set(con, run) or []):
        t = state.get_task(con, run["id"], t["id"])
        if not prs.has_pr(t):
            res.add(f"{t['id']} has no file scope and no PR; skipped")
            continue
        pr = t.get("pr") or {}
        if not pr.get("number"):
            raise Refused("no-pr", f"{t['id']} has no PR", next_step="office status (a pr.error notice names why)")
        if pr.get("merged"):
            continue
        up = prs.parent(con, run, t)
        if up is not None and pr.get("base") != base:
            if method != "merge":
                _restack(con, run, t, up, base)
            if _gh(run, "pr", "edit", str(pr["number"]), "--base", base).returncode != 0:
                raise Refused("retarget-failed", f"could not retarget #{pr['number']} to {base}")
        _wait_checks(run, pr["number"])
        proc = _gh(run, "pr", "merge", str(pr["number"]), prs.MERGE_FLAGS[method])
        if proc.returncode != 0 and re.search(r"not up to date|behind|out of date", proc.stderr + proc.stdout, re.I):
            _gh(run, "pr", "update-branch", str(pr["number"]))
            _wait_checks(run, pr["number"])
            proc = _gh(run, "pr", "merge", str(pr["number"]), prs.MERGE_FLAGS[method])
        if proc.returncode != 0:
            raise Refused("merge-failed", f"#{pr['number']} ({t['id']}) did not merge: "
                          f"{(proc.stderr or proc.stdout).strip()[:200]}", preserved=f"merged so far: "
                          f"{', '.join(merged) or 'none'}; {base} was {before[:12]}",
                          next_step="resolve it on GitHub, then office land again (merged PRs are skipped)")
        with db.transaction(con):
            state.update_task(con, run["id"], t["id"], pr={**pr, "merged": True, "base": base, "method": method})
            state.emit(con, run, "pr.merged", f"{t['id']} #{pr['number']} merged into {base} ({method})",
                       task_id=t["id"])
        res.add(f"{t['id']} #{pr['number']} merged ({method})")
        merged.append(t["id"])
    return before


def _restack(con, run: dict, task: dict, up: dict, base: str) -> None:
    """Squash/rebase merges rewrite the parent, so rebase the child's own
    commits onto the new base and force-push with lease."""
    d = state.get_dispatch(con, task["current_dispatch_id"])
    wt = Path(d["worktree"])
    old_parent = con.execute("SELECT commit_sha FROM revisions WHERE id=?", (up["accepted_revision_id"],)).fetchone()[0]
    _git(wt, "fetch", "-q", "origin", base)
    proc = _git(wt, "rebase", "--onto", f"origin/{base}", old_parent)
    if proc.returncode != 0:
        _git(wt, "rebase", "--abort")
        raise Refused("restack-conflict", f"{task['id']} does not rebase cleanly onto {base} after {up['id']} merged",
                      preserved=f"{task['id']} branch unchanged", next_step="resolve the rebase by hand, push, then office land")
    ok, err = prs.push(run, d, force=True)
    if not ok:
        raise Refused("restack-push-failed", f"force-push of {d['branch']} failed: {err[:160]}")


def _wait_checks(run: dict, number: int) -> None:
    try:
        proc = _gh(run, "pr", "checks", str(number), "--required", "--watch", "--fail-fast", timeout=CHECKS_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Refused("checks-timeout", f"required checks on #{number} did not finish in {CHECKS_TIMEOUT}s",
                      next_step="office land again once they finish")
    out = (proc.stdout + proc.stderr).lower()
    if proc.returncode != 0 and "no required checks" not in out and "no checks reported" not in out:
        raise Refused("checks-failed", f"required checks failed on #{number}: {(proc.stdout or proc.stderr).strip()[:200]}",
                      next_step="fix it on the task branch (office rerun), then office land")


def _verify_main(con, run: dict, integrated: str, res: Result) -> str:
    s = prs.settings(con, run)
    repo = Path(run["repo_root"])
    _git(repo, "fetch", "-q", "origin", s["base_branch"])
    main = paths.git(repo, "rev-parse", f"origin/{s['base_branch']}")
    if paths.git(repo, "rev-parse", f"{main}^{{tree}}") == paths.git(repo, "rev-parse", f"{integrated}^{{tree}}"):
        res.add(f"{s['base_branch']} {main[:12]} matches the reviewed integration tree")
        return main
    checks = list((run.get("landing") or {}).get("run_checks") or [])
    res.add(f"{s['base_branch']} moved beyond the reviewed integration; "
            + ("re-running the run checks on it" if checks else "no run checks to re-run"))
    if checks:
        checkout = gates.detached_checkout(run, main, "land-verify", purpose="check")
        try:
            for cmd in checks:
                proc = subprocess.run(cmd, shell=True, cwd=checkout, capture_output=True, text=True, timeout=1800)
                if proc.returncode != 0:
                    raise Refused("main-checks-failed", f"`{cmd}` fails on merged {s['base_branch']} {main[:12]}",
                                  preserved="the merges; nothing was deployed",
                                  next_step="fix forward on a new task, or revert the merges")
        finally:
            gates.remove_checkout(run, checkout)
    return main


# ------------------------------------------------------------------ deploy

def _deploy(con, run: dict, target: str, deploy: dict, cwd: Path, res: Result, rollback: str | None = None) -> None:
    link = Path(run["repo_root"]) / ".vercel"
    if link.is_dir() and not (cwd / ".vercel").exists():
        import shutil
        shutil.copytree(link, cwd / ".vercel")  # the local project link is untracked
    log_dir = paths.run_dir(run["id"]) / "deploys"
    log_dir.mkdir(parents=True, exist_ok=True)
    for step, cmd in (("deploy", deploy.get(target)), ("verify", deploy.get("verify"))):
        if not cmd:
            continue
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=3600)
        (log_dir / f"{target}-{step}.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        with db.transaction(con):
            state.emit(con, run, f"land.{step}", f"{target} {step} `{cmd}` exit {proc.returncode}",
                       payload={"target": target, "exit": proc.returncode})
        if proc.returncode != 0:
            raise Refused(f"{step}-failed", f"{target} {step} failed (exit {proc.returncode}): `{cmd}`",
                          preserved=(f"merges are done; rollback target {rollback[:12]}" if rollback else "the PRs"),
                          next_step=f"see {log_dir / f'{target}-{step}.log'}; fix or roll back, then office land again")
        res.add(f"{target} {step} ok: `{cmd}`")


def _close_issue(con, run: dict, main: str, res: Result) -> None:
    issue = (run.get("landing") or {}).get("issue")
    if not issue:
        return
    number = str(issue).rstrip("/").rsplit("/", 1)[-1]
    proc = _gh(run, "issue", "close", number, "--comment", f"Landed by Office run {short(run['id'])} at {main[:12]}.")
    res.add(f"issue #{number} closed" if proc.returncode == 0 else f"issue #{number} not closed: {proc.stderr.strip()[:120]}")


def _record(con, run: dict, **fields) -> None:
    with db.transaction(con):
        landing = dict(state.get_run(con, run["id"]).get("landing") or {})
        landing.update(fields)
        state.update_run(con, run["id"], landing=landing)


# ------------------------------------------------------------------ detection

def detect_deploy(repo: Path) -> Result:
    """Propose deploy commands for the user to confirm at intake."""
    lines, proposals = [], {}
    if (repo / "vercel.json").exists() or (repo / ".vercel").exists():
        proposals = {"deploy_preview": "vercel deploy", "deploy_prod": "vercel deploy --prod"}
        lines.append("vercel project: preview and prod deploys through the vercel CLI")
    pkg = repo / "package.json"
    if pkg.exists():
        scripts = (json.loads(pkg.read_text() or "{}").get("scripts") or {})
        for name in scripts:
            if name in ("deploy:preview", "deploy-preview"):
                proposals["deploy_preview"] = f"pnpm run {name}"
            elif name in ("deploy", "deploy:prod", "deploy-prod"):
                proposals.setdefault("deploy_prod", f"pnpm run {name}")
        if any(n.startswith("deploy") for n in scripts):
            lines.append("package.json deploy scripts: " + ", ".join(n for n in scripts if n.startswith("deploy")))
    wf = repo / ".github" / "workflows"
    ci = sorted(p.name for p in wf.glob("*.y*ml") if re.search(r"deploy|release|publish", p.read_text(errors="ignore"), re.I)) \
        if wf.is_dir() else []
    if ci:
        lines.append(f"CI workflows that deploy: {', '.join(ci)} (merging may deploy by itself)")
    skills = sorted(p.parent.name for p in (repo / ".claude" / "skills").glob("*deploy*/SKILL.md")) \
        if (repo / ".claude" / "skills").is_dir() else []
    if skills:
        lines.append(f"repo deploy skills: {', '.join(skills)}")
    if not lines:
        lines.append("no deploy route detected; ask the user for the commands, or offer ask/merge only")
    if proposals:
        lines += ["", "proposed PLAN.md requirements (confirm with the user first):",
                  *[f"{k}: {v}" for k, v in proposals.items()], "deploy_verify: <command that exits 0 when healthy>"]
    return Result(lines=lines, data={"proposals": proposals, "ci": ci, "skills": skills})
