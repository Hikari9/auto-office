#!/usr/bin/env python3
"""Out-of-band shadow code review: run one reviewer on a planted diff or a live PR (#1168).

Never part of an Office gate. Hydrogen stays vetoed as an Office reviewer; this lane only
records what a reviewer would have said so scorecard.py can compare it with Luna.

    python3 research/hydrogen-shadow/shadow_review.py --diff research/hydrogen-shadow/planted/p1-logic/change.diff --reviewer hydrogen
    python3 research/hydrogen-shadow/shadow_review.py --pr 1170 --reviewer hydrogen

Stdlib only. Writes results/<case-or-pr-N>/<reviewer>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GIT = shutil.which("git") or "/usr/bin/git"
DIFF_LIMIT = 120_000

REVIEWERS = {
    "luna": {"route": "codex/gpt-6-luna@high"},
    "hydrogen": {"route": "agy/gemini-4-hydrogen@high"},
}

PROMPT = """You are an independent code reviewer. Review ONLY the change below for real defects:
correctness and logic errors, security or access-control widening, off-by-one and boundary errors,
concurrency and transaction races, and missing or inadequate tests for changed behavior.
The repository at the current working directory is checked out with the change applied; read any
file you need for context. Do not modify files. Do not run the test suite.

Report a finding only when you can name the concrete failure. Do not report style, naming or
documentation nits. If the change has no real defect, approve it with no findings.

Reply with ONLY one JSON object, no prose and no code fence, in exactly this shape:
{{"verdict": "APPROVED" or "RECHECK",
  "findings": [{{"severity": "blocker" or "major" or "minor", "file": "<repo-relative path>",
                "line": <line number in the changed file>, "summary": "<one sentence: the defect and its failure>"}}]}}
Use RECHECK when there is at least one blocker or major finding.

Change ({label}):
```diff
{diff}
```
"""


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def repo_root() -> Path:
    out = run([GIT, "rev-parse", "--show-toplevel"], cwd=HERE, check=True).stdout.strip()
    return Path(out)


def prepare(args, root: Path) -> tuple[str, str, str]:
    """(case label, commit to check out, diff text)."""
    if args.diff:
        diff_path = Path(args.diff).resolve()
        diff = diff_path.read_text()
        base = args.base or (diff_path.parent.parent / "BASE").read_text().strip()
        case = args.case or diff_path.parent.name
        return case, base, diff
    pr = str(args.pr)
    info = json.loads(run(["gh", "pr", "view", pr, "--json", "headRefOid,baseRefName"], cwd=root, check=True).stdout)
    head = info["headRefOid"]
    run([GIT, "fetch", "-q", "origin", f"pull/{pr}/head", info["baseRefName"]], cwd=root, check=True)
    merge_base = run([GIT, "merge-base", f"origin/{info['baseRefName']}", head], cwd=root, check=True).stdout.strip()
    diff = run([GIT, "diff", f"{merge_base}..{head}"], cwd=root, check=True).stdout
    return args.case or f"pr-{pr}", head, diff


def reviewer_cmd(name: str, prompt: str, cwd: Path, last_msg: Path) -> list[str]:
    if name == "luna":
        return ["codex", "exec", "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="high"',
                "-s", "read-only", "-C", str(cwd), "--skip-git-repo-check", "-o", str(last_msg), prompt]
    if name == "hydrogen":
        # Plan mode is agy's read-only mode; no permission bypass is passed.
        return ["agy", "--model", "gemini-4-hydrogen-high", "--effort", "high", "--mode", "plan",
                "--add-dir", str(cwd), "--print-timeout", "30m",
                f"--print={prompt}"]
    raise SystemExit(f"unknown reviewer {name}")


def parse(text: str) -> dict | None:
    """The last JSON object in the reply that carries a verdict."""
    best = None
    for start in [m.start() for m in re.finditer(r"\{", text)]:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict) and "verdict" in obj:
                        best = obj
                    break
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--diff")
    src.add_argument("--pr", type=int)
    ap.add_argument("--reviewer", required=True, choices=sorted(REVIEWERS))
    ap.add_argument("--case")
    ap.add_argument("--base", help="commit the diff applies to (default planted/BASE)")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--timeout", type=int, default=2400)
    args = ap.parse_args(argv)

    root = repo_root()
    case, commit, diff = prepare(args, root)
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n[diff truncated; read the files in the working tree]\n"
    scratch = Path(tempfile.mkdtemp(prefix=f"shadow-{case}-{args.reviewer}-"))
    tree = scratch / "repo"
    out_dir = Path(args.out) / case
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {"case": case, "reviewer": args.reviewer, "route": REVIEWERS[args.reviewer]["route"],
              "commit": commit, "source": "diff" if args.diff else "pr"}
    try:
        run([GIT, "worktree", "add", "-q", "--detach", str(tree), commit], cwd=root, check=True)
        if args.diff:
            applied = run([GIT, "apply", "-"], cwd=tree, input=diff)
            if applied.returncode:
                raise SystemExit(f"diff does not apply at {commit}: {applied.stderr}")
        last_msg = scratch / "last.txt"
        prompt = PROMPT.format(label=case, diff=diff)
        started = time.monotonic()
        try:
            proc = run(reviewer_cmd(args.reviewer, prompt, tree, last_msg), cwd=tree, timeout=args.timeout,
                       stdin=subprocess.DEVNULL)
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, stdout, stderr = "timeout", exc.stdout or "", exc.stderr or ""
            stdout = stdout.decode() if isinstance(stdout, bytes) else stdout
            stderr = stderr.decode() if isinstance(stderr, bytes) else stderr
        record["wall_seconds"] = round(time.monotonic() - started, 1)
        reply = last_msg.read_text() if last_msg.exists() else stdout
        parsed = parse(reply) or parse(stdout)
        status = "ok" if parsed else ("quota-blocked" if re.search(r"quota|rate.?limit|usage limit|429",
                                                                   stdout + stderr, re.I) else "unparsed")
        record.update({"exit": rc, "status": status, "verdict": (parsed or {}).get("verdict"),
                       "findings": (parsed or {}).get("findings") or [],
                       "raw_reply": reply[-20000:], "stderr_tail": stderr[-4000:]})
    finally:
        run([GIT, "worktree", "remove", "--force", str(tree)], cwd=root)
        shutil.rmtree(scratch, ignore_errors=True)
    path = out_dir / f"{args.reviewer}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(f"{case} {args.reviewer}: {record['status']} verdict={record.get('verdict')} "
          f"findings={len(record['findings'])} {record.get('wall_seconds')}s -> {path.relative_to(root) if path.is_relative_to(root) else path}")
    return 0 if record["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
