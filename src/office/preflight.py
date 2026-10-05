"""office preflight: read-only checks an executor runs before office submit.

Each check names what is wrong and the exact repair; preflight itself changes
nothing. The verdict line and exit code say what the caller does next:

  PREFLIGHT ready  exit 0   run office submit
  PREFLIGHT fix    exit 1   apply the listed repairs, then preflight again
  PREFLIGHT wait   exit 75  the task is paused but this session still holds it;
                            poll preflight until it is ready, then submit
  PREFLIGHT stop   exit 4   terminal for this session (lease lost, superseded,
                            cancelled, blocked on the orchestrator): emit the
                            status block and stop; never retry

A lost lease is never reacquired here. Only the orchestrator moves a task to a
new holder (office revoke / rerun); a worker that reclaims its own lease would
undo the takeover that revoked it.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from office import briefs, discovery, paths, planfile, state, submit
from office.result import Result

EXIT = {"ready": 0, "fix": 1, "stop": 4, "wait": 75}
# Pauses the orchestrator resolves while the worker keeps its lease; anything
# else that pauses or blocks a task ends this session's part in it.
WAITABLE = ("plan defect", "contract amendment", "brief defect", "stacked after")


def _git(wt: Path, *args: str) -> str:
    return paths.git(wt, *args, check=False)


def _packet(run: dict, d: dict) -> dict:
    try:
        return json.loads((paths.run_dir(run["id"]) / "dispatches" / d["id"] / "packet.json").read_text())
    except (OSError, ValueError):
        return {}


_SHA = re.compile(r"[0-9a-fA-F]{7,40}")
_ACCEPT_REF = re.compile(r"accept=([1-9][0-9]{0,5})")
_BLOCKERS = ("high", "medium")


def _one_line(text: str, limit: int = 160) -> str:
    text = "".join(c if c.isprintable() else " " for c in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _parse_disposition(raw: str) -> tuple[dict | None, str]:
    """(disposition, "") or (None, why it is malformed). `fixed` takes an optional test path,
    `rejected` a reason, `contract-conflict` the ACCEPT line it would break; the rest take nothing."""
    kind, _, arg = raw.strip().partition(" ")
    arg = arg.strip()
    if kind not in briefs.LEDGER_DISPOSITIONS:
        return None, f"disposition must be one of {', '.join(briefs.LEDGER_DISPOSITIONS)}"
    if kind == "fixed" and len(arg.split()) > 1:
        return None, "`fixed` takes one test path"
    if kind in ("open", "out-of-scope") and arg:
        return None, f"`{kind}` takes no argument"
    if kind == "rejected" and not arg:
        return None, "`rejected` needs a reason"
    if kind == "contract-conflict" and not _ACCEPT_REF.fullmatch(arg):
        return None, "`contract-conflict` needs accept=<n>, the ACCEPT line it would break"
    return {"kind": kind, "arg": arg}, ""


def parse_ledger(text: str) -> tuple[dict, list[str]]:
    """The ledger as {commit, round, lenses, findings} plus one error per line that is not in the
    format briefs.ledger_lines() states. A bad line is an error, never skipped; blank lines are the only
    lines that carry nothing."""
    led: dict = {"commit": None, "round": None, "lenses": {}, "findings": []}
    errors: list[str] = []
    for n, raw in enumerate(text.lstrip("\ufeff").split("\n"), 1):
        line = raw.strip()
        if not line:
            continue

        def bad(why: str) -> None:
            errors.append(f"line {n}: {why}: {_one_line(line, 80)}")

        word, _, rest = line.partition(" ")
        rest = rest.strip()
        if word == "COMMIT":
            if led["commit"] is not None:
                bad("second COMMIT line")
            elif not _SHA.fullmatch(rest):
                bad("COMMIT needs the sha of HEAD (7-40 hex digits)")
            else:
                led["commit"] = rest.lower()
        elif word == "ROUND":
            if led["round"] is not None:
                bad("second ROUND line")
            elif not re.fullmatch(r"[0-9]{1,2}", rest) or not 1 <= int(rest) <= briefs.MAX_REVIEW_ROUNDS:
                bad(f"ROUND must be 1-{briefs.MAX_REVIEW_ROUNDS}")
            else:
                led["round"] = int(rest)
        elif word == "LENS":
            name, _, status = rest.partition(" ")
            verb, _, reason = status.strip().partition(" ")
            if name not in briefs.LEDGER_LENSES:
                bad(f"LENS must be one of {', '.join(briefs.LEDGER_LENSES)}")
            elif name in led["lenses"]:
                bad(f"second LENS line for {name}")
            elif verb == "reviewed" and not reason.strip():
                led["lenses"][name] = None
            elif verb == "skipped" and reason.strip():
                led["lenses"][name] = reason.strip()
            else:
                bad("LENS needs `reviewed` or `skipped <reason>`")
        elif word == "FINDING":
            left, sep, disp_raw = rest.rpartition(" | ")
            head, sep2, summary = left.partition(" | ")
            sev, _, location = head.strip().partition(" ")
            location = location.strip()
            disp, why = _parse_disposition(disp_raw) if sep and sep2 else (None, "")
            if not (sep and sep2):
                bad("FINDING needs `<severity> <file:line> | <summary> | <disposition>`")
            elif sev not in briefs.LEDGER_SEVERITIES:
                bad(f"severity must be one of {', '.join(briefs.LEDGER_SEVERITIES)}")
            elif not location or " " in location:
                bad("location must be one word, file:line")
            elif not summary.strip():
                bad("FINDING needs a summary")
            elif disp is None:
                bad(why)
            else:
                led["findings"].append({"severity": sev, "location": location, "summary": summary.strip(),
                                        "line": n, **disp})
        else:
            bad("unknown line (expected COMMIT, ROUND, LENS or FINDING)")
    return led, errors


def _test_file_exists(wt: Path, rel: str) -> bool:
    rel = rel.split("::", 1)[0]  # a pytest node id names its file first
    if os.path.isabs(rel):
        return False
    try:
        target = (wt / rel).resolve()
        target.relative_to(wt.resolve())
    except (OSError, ValueError):
        return False
    return target.is_file()


def check_ledger(text: str, head: str, accept: list[str], wt: Path, tier: str = "deep") -> tuple[list[str], list[str]]:
    """(stop, fix) lines for a ledger's text. Every line of `fix` names its repair. Only the inline
    tier may skip a lens (the brief's other tiers cover all four)."""
    led, errors = parse_ledger(text)
    stop: list[str] = []
    fix = [f"ledger {e}" for e in errors]
    if led["commit"] is None:
        fix.append("ledger: no COMMIT line; name HEAD " + head[:12])
    elif not head.startswith(led["commit"]):
        fix.append(f"ledger: names commit {led['commit'][:12]} but HEAD is {head[:12]}; the review is stale: re-review "
                   "what changed, then update COMMIT")
    if led["round"] is None:
        fix.append("ledger: no ROUND line")
    for lens in briefs.LEDGER_LENSES:
        if lens not in led["lenses"]:
            fix.append(f"ledger: lens {lens} has no line: add `LENS {lens} reviewed` or `LENS {lens} skipped <reason>`")
        elif led["lenses"][lens] is not None and tier != "inline":
            fix.append(f"ledger: lens {lens} is skipped, but the {tier} tier reviews all four lenses: review it and "
                       f"write `LENS {lens} reviewed`")
    last_round = (led["round"] or 1) >= briefs.MAX_REVIEW_ROUNDS
    for f in led["findings"]:
        what = f"line {f['line']}: {f['severity']} {_one_line(f['location'], 80)} {_one_line(f['summary'])}"
        if f["kind"] == "contract-conflict":
            n = int(_ACCEPT_REF.fullmatch(f["arg"]).group(1))
            if n > len(accept):
                fix.append(f"ledger {what}: accept={n} but the brief has {len(accept)} ACCEPT lines")
            else:
                stop.append(f"contract-conflict: {what} would break ACCEPT {n}: \"{_one_line(accept[n - 1])}\"; "
                            "do not change the contract yourself: report it")
        elif f["kind"] == "open" and f["severity"] in _BLOCKERS and last_round:
            stop.append(f"self-review: round {briefs.MAX_REVIEW_ROUNDS} ended with a finding still open ({what}); "
                        "the round cap is spent: report it")
        elif f["kind"] == "open":
            fix.append(f"ledger {what} is open: fix it and mark it `fixed <test path>`, or mark it "
                       "`rejected <reason>` or `out-of-scope`")
        elif f["kind"] == "fixed" and f["severity"] in _BLOCKERS:
            if not f["arg"]:
                fix.append(f"ledger {what} is marked fixed without a test path: write the test, prove it fails "
                           "without the fix, then `fixed <test path>`")
            elif not _test_file_exists(wt, f["arg"]):
                fix.append(f"ledger {what} names test {_one_line(f['arg'], 80)}, which is not a file in this worktree")
    return stop, fix


def _in_scope_changes(wt: Path, task: dict, changed: list[str]) -> tuple[list[str], list[str]]:
    """(committed, pending) in-scope files: `changed` is the committed diff, pending is every uncommitted
    edit or new file. Neither lists the ledger. A task with no file scope has none."""
    if not task["scope"]:
        return [], []
    pending = _git(wt, "diff", "--name-only", "-z", "HEAD").split("\0") \
        + _git(wt, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    def mine(files):
        return sorted({f for f in files if f and f != briefs.LEDGER_FILE and planfile.path_in_scope(f, task["scope"])})
    return mine(changed), mine(pending)


def ledger_verdict(wt: Path, task: dict, changed: list[str], head: str, tier: str = "deep") -> tuple[list[str], list[str]]:
    """(stop, fix) for the task's self-review ledger; both empty when none is owed. The ledger names
    HEAD, so uncommitted in-scope work is a fix: it would ship without the review the ledger vouches for."""
    name = briefs.LEDGER_FILE
    if paths.git(wt, "ls-files", "--", name, check=False).strip() or paths.git(wt, "ls-tree", "HEAD", "--", name, check=False).strip():
        return [], [f"ledger: {name} is committed or staged; leave it untracked: git rm --cached {name} (commit "
                    "that removal), then update the ledger's COMMIT"]
    committed, pending = _in_scope_changes(wt, task, changed)
    if not committed and not pending:
        return [], []
    uncommitted = [f"ledger: uncommitted in-scope changes ({', '.join(pending[:6])}): commit them first, the "
                   "ledger names HEAD"] if pending else []
    text = submit._read_untracked_text(wt, name, briefs.LEDGER_MAX_CHARS)
    if text is None and os.path.lexists(wt / name):
        return [], uncommitted + [f"ledger: {name} must be a regular untracked file (not a symlink or hard link)"]
    if text is None:
        return [], uncommitted + [f"ledger: no {name}; run the SELF-REVIEW and write it in this worktree root in "
                                  "the format the brief gives"]
    if len(text) > briefs.LEDGER_MAX_CHARS:
        return [], uncommitted + [f"ledger: {name} is over {briefs.LEDGER_MAX_CHARS} characters; keep one short line "
                                  "per finding"]
    stop, fix = check_ledger(text, head, task["accept"] or [], wt, tier)
    return stop, uncommitted + fix


def _sed_backups(wt: Path) -> list[str]:
    """Untracked `<file>-e` next to a tracked `<file>`: BSD sed took `-e` as the
    -i backup suffix, so a GNU-style `sed -i -e` edit was not what it looked like."""
    tracked = set(_git(wt, "ls-files", "-z").split("\0"))
    others = _git(wt, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted(f for f in others if f.endswith("-e") and f[:-2] in tracked)


def preflight(con, run: dict, cwd: Path) -> Result:
    res = Result()
    stop: list[str] = []
    fix: list[str] = []
    wait: list[str] = []

    d = submit._worktree_dispatch(con, run, cwd)
    env_dispatch = os.environ.get("OFFICE_DISPATCH_ID")
    if d is None and env_dispatch:
        d = state.get_dispatch(con, env_dispatch)
    if d is None or d["role"] != "executor":
        found = discovery.task_worktree(con, cwd)
        res.lines = ["PREFLIGHT stop", "role: no executor dispatch owns this directory"]
        res.next = (f"cd {found[1]['worktree']} and run office preflight there" if found
                    else "run office preflight from your task worktree; if you are not an executor, do not submit")
        res.exit_code = EXIT["stop"]
        return res
    task = state.get_task(con, run["id"], d["task_id"])
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    wt = Path(d["worktree"]).resolve()
    res.data = {"task": task["id"], "dispatch": d["id"], "worktree": str(wt)}

    # 1. Role and identity. A shell env cannot be repaired from a child process,
    # so the repair is the line to prefix to office submit in the same shell.
    role = os.environ.get("OFFICE_ROLE")
    source = f". {ddir / 'agent.env'}"
    if role != "executor" or (env_dispatch and env_dispatch != d["id"]):
        fix.append(f"role: OFFICE_ROLE={role or 'unset'}, dispatch={env_dispatch or 'unset'}; "
                   f"submit in one command: {source} && office submit")
    res.data["source"] = source
    here = paths.repo_identity(cwd)
    if here is None or here[0] != wt:
        fix.append(f"worktree: submit refuses outside the task worktree; cd {wt}")

    # 2. Ownership. Superseded or lease-lost is terminal for this session.
    if task["current_dispatch_id"] != d["id"]:
        stop.append(f"superseded: {task['id']} now belongs to dispatch {task['current_dispatch_id']}")
    else:
        from office import dispatch as dispatch_mod
        if dispatch_mod.live_lease(con, run["id"], d["lease_id"]) is None:
            stop.append(f"lease-lost: {task['id']} lease {d['lease_id']} was revoked or taken over")

    # 3. Task state.
    status, reason = task["status"], task.get("pause_reason") or ""
    if status in ("paused", "blocked", "cancelled") and not submit.self_blocked(task):
        if status == "paused" and reason.startswith(WAITABLE) and not stop:
            wait.append(f"paused: {task['id']} {reason}")
        else:
            stop.append(f"{status}: {task['id']} {reason}".rstrip())

    # 4. A fix round must have findings to fix.
    packet = _packet(run, d)
    if packet.get("fix_of"):
        rows = con.execute("SELECT code, severity, location, summary FROM findings WHERE run_id=? AND task_id=? "
                           "AND state='open' ORDER BY created_at", (run["id"], task["id"])).fetchall()
        if rows:
            res.lines += [f"finding: {r['code']} [{r['severity']}] {r['location'] or ''} {r['summary']}" for r in rows]
        else:
            stop.append(f"findings: fix round for {packet['fix_of']} but no open findings are recorded; "
                        "the orchestrator must name them (office prompt) or rerun --fresh")

    # 5. Scope: tracked edits outside the contract are refused at submit.
    base = d["base_commit"]
    head = _git(wt, "rev-parse", "HEAD")
    changed = [f for f in _git(wt, "diff", "--name-only", "-z", base, head).split("\0") if f]
    dep_bases = [b for b in submit._dependency_bases(con, run, task, head) if b != base]
    for b in dep_bases:
        also = set(_git(wt, "diff", "--name-only", "-z", b, head).split("\0"))
        changed = [f for f in changed if f in also]
    outside = [f for f in changed if not planfile.path_in_scope(f, task["scope"]) and not submit._harness_path(f)]
    if outside:
        listed = " ".join(outside[:10])
        fix.append(f"scope: tracked edits outside SCOPE: {listed}; revert tool-stamped ones with "
                   f"git checkout {base[:12]} -- <file>, or ask: office submit --request-scope <file> -- \"<reason>\"")
    res.data["outside_scope"] = outside

    # 5b. Scope-none evidence: submit refuses a file it already ingested.
    if not task["scope"]:
        ev = submit._read_evidence(wt)
        if ev and ev[2] in submit._ingested_digests(con, run, task["id"]):
            fix.append(f"evidence: {briefs.EVIDENCE_FILE} repeats evidence already submitted for {task['id']}; "
                       "write it fresh for this submission")

    # 6. Silent BSD sed failures.
    backups = _sed_backups(wt)
    if backups:
        fix.append(f"sed: BSD sed wrote backup files {' '.join(backups[:10])}: a `sed -i -e` edit used `-e` as the "
                   "backup suffix; check git diff that each edit landed, delete the backups, and rebuild anything "
                   "generated from those files")
    res.data["sed_backups"] = backups

    # 7. Self-review ledger: the brief's SELF-REVIEW block says what it holds.
    ledger_stop, ledger_fix = ledger_verdict(wt, task, changed, head,
                                             briefs.self_review_tier(run.get("gear"), run.get("risk_json")))
    stop += ledger_stop
    fix += ledger_fix

    res.data["head"] = head
    verdict = "stop" if stop else "fix" if fix else "wait" if wait else "ready"
    res.lines = [f"PREFLIGHT {verdict}"] + [f"stop: {s}" for s in stop] + [f"fix: {f}" for f in fix] \
        + [f"wait: {w}" for w in wait] + res.lines
    res.data["verdict"] = verdict
    res.exit_code = EXIT[verdict]
    res.next = {
        "ready": f"{source} && office submit",
        "fix": "apply each fix above, then office preflight",
        "wait": "poll office preflight every 60s (Monitor or a sleep loop) until it prints ready, then submit; "
                "stop if it prints stop",
        "stop": "do not retry; end with the STATUS block (SUBMIT=refused: <the stop line>) and stop",
    }[verdict]
    return res
