"""#938: the revert-and-fail proof of a new run's self-review, run by Office, never by the executor.

Each medium or high fix in the ledger names one test. Office runs that test alone on two disposable
checkouts of the task's commit: with the task's non-test source reverted it must fail (a test
failure, not a collection error, a timeout or a missing command), and unreverted it must pass.
A task with no named test or no pytest command to run it with records an exemption: nothing
is left to prove, so nothing is blocked. The executor's worktree and index are never touched.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from office import briefs, contract, db, gates, paths, state, submit
from office.util import now_iso, sha256_obj

_SOURCE_SUFFIXES = frozenset((".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
                              ".c", ".cc", ".cpp", ".h", ".cs", ".rb", ".php", ".swift",
                              ".sh", ".bash", ".sql", ".vue", ".svelte", ".html", ".css", ".scss", ".sass",
                              ".astro", ".dart", ".graphql", ".proto"))
_PYTEST = re.compile(r"\bpytest\b")
_DROP_WITH_VALUE = ("-k", "-m", "-n")  # selectors and workers: the named test runs alone
_MAX_SECONDS = 120
_MAX_TESTS = 5
_PYTEST_TEST_FAILED = 1  # pytest's exit code for "tests ran and some failed"; 2 is a collection error
_VERSION = "main-proof-2"


def applicable(run: dict) -> bool:
    return contract.is_convergence(run)


def _sources(changed: list[str]) -> list[str]:
    from office.preflight import _is_test_path
    return sorted(p for p in changed
                  if not _is_test_path(p) and Path(p).suffix.lower() in _SOURCE_SUFFIXES)


def named_tests(ledger_text: str | None) -> list[str]:
    """The distinct tests the ledger's medium and high fixes name, in ledger order."""
    from office.preflight import parse_ledger
    if not ledger_text:
        return []
    led, _ = parse_ledger(ledger_text)
    tests = []
    for f in led["findings"]:
        if f["kind"] == "fixed" and f["severity"] in ("high", "medium") and f["arg"]:
            test = f["arg"].split()[0]
            if test not in tests:
                tests.append(test)
    return tests


def _runner(task: dict) -> list[str] | None:
    """The pytest invocation the task's own checks use, minus test selection, or None."""
    for check in task.get("checks") or []:
        if not isinstance(check, str) or "validate.sh" in check or not _PYTEST.search(check):
            continue
        try:
            words = shlex.split(check)
        except ValueError:
            continue
        if "pytest" not in words:
            continue
        start = words.index("pytest")
        kept, skip = words[:start + 1], False
        for word in words[start + 1:]:
            if skip:
                skip = False
            elif word in _DROP_WITH_VALUE:
                skip = True
            elif word.startswith("-") and not word.startswith(("-k", "-m", "-n")):
                kept.append(word)
            elif word.startswith("-"):
                continue  # -k<expr>, -m<expr> or -n<count> attached to its flag
            # a bare word after pytest is a path or node id: the ledger's test replaces it
        return kept
    return None


def _safe_test(checkout: Path, test: str) -> bool:
    file = test.split("::", 1)[0]
    if not file or file.startswith("-") or os.path.isabs(file) or ".." in Path(file).parts:
        return False
    try:
        (checkout / file).resolve().relative_to(checkout.resolve())
    except (OSError, ValueError, RuntimeError):
        return False
    return (checkout / file).is_file()


def _run(wt: Path, command: str, run: dict) -> int:
    return gates._run_check(command, wt, _MAX_SECONDS, gates._check_env(run))[0]


def prove(wt: Path, base: str, head: str, changed: list[str], runner: list[str], tests: list[str],
          run: dict) -> dict:
    sources = _sources(changed)
    parent = Path(tempfile.mkdtemp(prefix="office-proof-"))
    red_checkout, green_checkout = parent / "reverted", parent / "restored"
    attached = []
    try:
        for checkout in (red_checkout, green_checkout):
            made = subprocess.run(["git", "-C", str(wt), "worktree", "add", "--detach", str(checkout), head],
                                  capture_output=True, text=True)
            if made.returncode:
                return {"ok": False, "status": "blocked",
                        "reason": "cannot isolate source for the proof: " + made.stderr[-300:]}
            attached.append(checkout)
        patch = subprocess.run(["git", "-C", str(red_checkout), "diff", "--binary", "--no-ext-diff",
                                "--no-renames", base, head, "--", *sources], capture_output=True)
        if patch.returncode or not patch.stdout:
            return {"ok": False, "status": "blocked", "reason": "cannot compute a reversible source-only diff"}
        undone = subprocess.run(["git", "-C", str(red_checkout), "apply", "--reverse", "--binary"],
                                input=patch.stdout, capture_output=True)
        if undone.returncode:
            return {"ok": False, "status": "blocked",
                    "reason": "source-only revert failed: " + undone.stderr.decode(errors="replace")[-300:]}
        for test in tests:
            if not _safe_test(green_checkout, test):
                return {"ok": False, "status": "blocked",
                        "reason": f"named test {test} is not a file of the submitted commit"}
            command = " ".join(shlex.quote(w) for w in [*runner, test])
            red, green = _run(red_checkout, command, run), _run(green_checkout, command, run)
            if red != _PYTEST_TEST_FAILED:
                why = ("passes without the fix" if red == 0 else
                       f"exited {red}, which is not a test failure (collection error, timeout or missing command)")
                return {"ok": False, "status": "blocked",
                        "reason": f"revert-and-fail proof: {test} with the source reverted {why}"}
            if green != 0:
                return {"ok": False, "status": "blocked",
                        "reason": f"revert-and-fail proof: {test} fails on the submitted commit (exit {green})"}
        return {"ok": True, "status": "passed",
                "reason": f"revert-and-fail proof: {', '.join(tests)} failed reverted and passed restored"}
    finally:
        for checkout in attached:
            subprocess.run(["git", "-C", str(wt), "worktree", "remove", "--force", str(checkout)],
                           capture_output=True, timeout=30)
        shutil.rmtree(parent, ignore_errors=True)


def gate(con, run: dict, task: dict, wt: Path, base: str, head: str, changed: list[str],
         *, execute: bool) -> dict:
    """Prove once per exact commit and test set. Submit only trusts a recorded pass or exemption."""
    if not applicable(run) or not _sources(changed) or not task.get("scope"):
        return {"ok": True, "status": "not-required", "reason": "no non-test source change to revert"}
    tests = named_tests(submit._read_untracked_text(wt, briefs.LEDGER_FILE, briefs.LEDGER_MAX_CHARS))
    if len(tests) > _MAX_TESTS:  # proving only some of them would let the rest pass unproven
        return {"ok": False, "status": "blocked",
                "reason": f"revert-and-fail proof: the ledger names {len(tests)} tests for medium or high fixes; "
                          f"the proof runs at most {_MAX_TESTS}, so name only the tests that prove each fix"}
    runner = _runner(task)
    if not tests or runner is None:
        why = ("the ledger names no test for a medium or high fix" if not tests
               else "the task's checks hold no pytest command to run a named test with")
        return {"ok": True, "status": "exempt", "reason": f"revert-and-fail proof exempt: {why}"}
    key = sha256_obj({"task": task["id"], "base": base, "head": head, "changed": sorted(changed),
                      "tests": tests, "runner": runner, "version": _VERSION})
    row = con.execute("SELECT * FROM gates WHERE run_id=? AND task_id=? AND kind='self_review_proof' "
                      "AND input_key=? AND status='done' AND verdict='PASS' ORDER BY created_at DESC LIMIT 1",
                      (run["id"], task["id"], key)).fetchone()
    if row:
        return {"ok": True, "status": "passed", "reason": row["summary"]}
    if not execute:
        return {"ok": False, "status": "blocked", "reason": "run office preflight for the revert-and-fail proof"}
    result = prove(wt, base, head, changed, runner, tests, run)
    if result["ok"]:
        with db.transaction(con):
            con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, verdict, summary,"
                        " created_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ("G" + uuid.uuid4().hex[:10], run["id"], task["id"], task["id"], "self_review_proof",
                         key, "done", "PASS", result["reason"], now_iso(), now_iso()))
            state.emit(con, run, "self_review.proof", result["reason"], task_id=task["id"],
                       payload={"commit": head, "passed": True, "tests": tests})
    return result
