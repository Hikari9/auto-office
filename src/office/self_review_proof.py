"""#938 deterministic main-change red/green check on an isolated Git worktree.

Only the task's non-test changes are reversed; its tests remain at HEAD.
The executor worktree and git index are never modified.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from office import contract, db, gates, state
from office.util import now_iso, sha256_obj

_SOURCE_SUFFIXES = frozenset((".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
                             ".c", ".cc", ".cpp", ".h", ".cs", ".rb", ".php", ".swift",
                             ".sh", ".bash", ".sql", ".vue", ".svelte", ".html", ".css", ".scss", ".sass",
                             ".astro", ".dart", ".graphql", ".proto"))
_TEST_COMMAND = re.compile(r"\b(pytest|unittest|vitest|jest|mocha|cargo\s+test|go\s+test|dotnet\s+test|"
                           r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
                           r"python(?:3)?\s+-m\s+pytest)\b", re.I)
_MAX_SECONDS = 120


def applicable(run: dict) -> bool:
    return contract.is_convergence(run) and (run.get("gates") or {}).get("gear_model_version") == "run-gear-1"


def _sources(changed: list[str]) -> list[str]:
    from office.preflight import _is_test_path
    return sorted(p for p in changed
                  if not _is_test_path(p) and Path(p).suffix.lower() in _SOURCE_SUFFIXES)


def _test_command(task: dict) -> str | None:
    return next((c for c in (task.get("checks") or []) if isinstance(c, str)
                 and "validate.sh" not in c
                 and (_TEST_COMMAND.search(c) or
                      (re.search(r"\bpython(?:3)?\s+-c\b", c) and re.search(r"\bassert\b", c)))),
                None)


def _run(wt: Path, command: str, run: dict) -> tuple[int, str]:
    return gates._run_check(command, wt, _MAX_SECONDS, gates._check_env(run))


def prove(wt: Path, base: str, head: str, changed: list[str], command: str, run: dict) -> dict:
    """Require red on reversed non-test source and green on the original commit."""
    sources = _sources(changed)
    if not sources:
        return {"ok": True, "status": "not-required", "reason": "no non-test source change"}
    parent = Path(tempfile.mkdtemp(prefix="office-proof-"))
    red_checkout, green_checkout = parent / "reverted", parent / "restored"
    attached = []
    try:
        for checkout in (red_checkout, green_checkout):
            made = subprocess.run(["git", "-C", str(wt), "worktree", "add", "--detach", str(checkout), head],
                                  capture_output=True, text=True)
            if made.returncode:
                return {"ok": False, "status": "blocked",
                        "reason": "cannot isolate source for proof: " + made.stderr[-300:]}
            attached.append(checkout)
        patch = subprocess.run(["git", "-C", str(red_checkout), "diff", "--binary", "--no-ext-diff",
                                "--no-renames", base, head, "--", *sources], capture_output=True)
        if patch.returncode or not patch.stdout:
            return {"ok": False, "status": "blocked", "reason": "cannot compute a reversible source-only diff"}
        reversed_ = subprocess.run(["git", "-C", str(red_checkout), "apply", "--reverse", "--binary"],
                                   input=patch.stdout, capture_output=True)
        if reversed_.returncode:
            return {"ok": False, "status": "blocked", "reason": "source-only revert failed: "
                    + reversed_.stderr.decode(errors="replace")[-300:]}
        # Independent checkout for GREEN: red test artefacts can never contaminate it.
        red, _ = _run(red_checkout, command, run)
        green, _ = _run(green_checkout, command, run)
        good = red not in (0, 124, 127) and green == 0
        why = f"main-change red/green: reverted exit {red}; restored exit {green}"
        if red == 0:
            why += "; tests still pass without implementation"
        if red in (124, 127) or green in (124, 127):
            why += "; timeout or missing command is not proof"
        if green != 0:
            why += "; restored implementation did not pass"
        return {"ok": good, "status": "passed" if good else "blocked", "reason": why,
                "red_exit": red, "green_exit": green}
    finally:
        for checkout in attached:
            subprocess.run(["git", "-C", str(wt), "worktree", "remove", "--force", str(checkout)],
                           capture_output=True, timeout=30)
        shutil.rmtree(parent, ignore_errors=True)


def gate(con, run: dict, task: dict, wt: Path, base: str, head: str, changed: list[str],
         *, execute: bool) -> dict:
    """Cache proof by the exact source and test command; submit only trusts prior passed evidence."""
    if not applicable(run) or not _sources(changed) or not task.get("scope"):
        return {"ok": True, "status": "not-required", "reason": "not a new substantive source revision"}
    command = _test_command(task)
    if command is None:
        return {"ok": False, "status": "blocked",
                "reason": "main-change revert/fail gate needs a targeted test command in task checks"}
    key = sha256_obj({"task": task["id"], "base": base, "head": head,
                      "changed": sorted(changed), "test": command, "version": "main-proof-1"})
    row = con.execute("SELECT * FROM gates WHERE run_id=? AND task_id=? AND kind='self_review_proof' "
                      "AND input_key=? AND status='done' AND verdict='PASS' ORDER BY created_at DESC LIMIT 1",
                      (run["id"], task["id"], key)).fetchone()
    if row:
        return {"ok": row["verdict"] == "PASS", "status": "passed" if row["verdict"] == "PASS" else "blocked",
                "reason": row["summary"]}
    if not execute:
        return {"ok": False, "status": "blocked", "reason": "run office preflight for main-change red/green proof"}
    result = prove(wt, base, head, changed, command, run)
    with db.transaction(con):
        con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, verdict, summary,"
                    " created_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("G" + uuid.uuid4().hex[:10], run["id"], task["id"], task["id"], "self_review_proof",
                     key, "done", "PASS" if result["ok"] else "CHANGES_REQUIRED", result["reason"],
                     now_iso(), now_iso()))
        state.emit(con, run, "self_review.proof", result["reason"], task_id=task["id"],
                   payload={"commit": head, "passed": result["ok"], "test": command})
    return result
