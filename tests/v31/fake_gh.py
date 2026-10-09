"""A scripted `gh` for PR tests: PR state lives in $FAKE_GH_STATE (JSON)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

def state_path() -> Path:
    return Path(os.environ["FAKE_GH_STATE"])  # read per call: tests import this module once and change the path


def load() -> dict:
    if state_path().exists():
        return json.loads(state_path().read_text())
    return {"repo": {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "mergeCommitAllowed": True,
                     "squashMergeAllowed": True, "rebaseMergeAllowed": True}, "prs": [], "calls": []}


def opt(args: list[str], name: str, default=None):
    return args[args.index(name) + 1] if name in args else default


def git_merge(pr: dict, method: str) -> None:
    """Really merge the PR head into its base on the bare origin."""
    origin = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        run = lambda *a: subprocess.run(["git", "-C", tmp, *a], check=True, capture_output=True, text=True)
        subprocess.run(["git", "clone", "-q", origin, tmp], check=True, capture_output=True)
        run("checkout", "-q", pr["base"])
        if method == "--merge":
            run("merge", "--no-ff", "-q", "-m", f"Merge pull request #{pr['number']}", f"origin/{pr['head']}")
        else:
            run("merge", "--squash", "-q", f"origin/{pr['head']}")
            run("commit", "-q", "-m", f"{pr['title']} (#{pr['number']})")
        run("push", "-q", "origin", pr["base"])


def main(argv: list[str]) -> int:
    s = load()
    s["calls"].append(argv)
    out, code = "", 0
    fail = s.get("fail") or {}  # {"pr ready": n}: the next n such calls exit 1 (-1: every call)
    key = " ".join(argv[:2])
    if fail.get(key):
        fail[key] -= fail[key] > 0
        code, out = 1, f"{key}: simulated gh failure"
    elif argv[:2] == ["issue", "close"]:
        s.setdefault("closed_issues", []).append(argv[2])
    elif argv[:2] == ["repo", "view"]:
        if s.get("repo_view_failures", 0) > 0:
            s["repo_view_failures"] -= 1
            code, out = 1, "error connecting to api.github.com: net/http: TLS handshake timeout"
        else:
            out = json.dumps(s["repo"])
    elif argv[:2] == ["pr", "list"]:
        head = opt(argv, "--head")
        out = json.dumps([{"number": p["number"], "url": p["url"], "baseRefName": p["base"], "isDraft": p["draft"]}
                          for p in s["prs"] if p["head"] == head and p["state"] == "open"])
    elif argv[:2] == ["pr", "create"]:
        n = len(s["prs"]) + 1
        url = f"https://github.com/o/r/pull/{n}"
        s["prs"].append({"number": n, "url": url, "head": opt(argv, "--head"), "base": opt(argv, "--base"),
                         "title": opt(argv, "--title"), "body": Path(opt(argv, "--body-file")).read_text(),
                         "draft": "--draft" in argv, "state": "open", "comments": []})
        out = url
    else:
        pr = next((p for p in s["prs"] if len(argv) > 2 and str(p["number"]) == argv[2]), None)
        if pr is None:
            code, out = 1, "no such pull request"
        elif argv[1] == "edit":
            if "--base" in argv:
                pr["base"] = opt(argv, "--base")
            if "--body-file" in argv:
                pr["body"] = Path(opt(argv, "--body-file")).read_text()
        elif argv[1] == "comment":
            pr["comments"].append(opt(argv, "--body"))
        elif argv[1] == "ready":
            pr["draft"] = False
        elif argv[1] == "merge":
            if s.get("fail_merge"):
                code, out = 1, "merge blocked"
            else:
                method = next(a for a in argv if a in ("--merge", "--squash", "--rebase"))
                git_merge(pr, method)
                pr["state"], pr["merged_with"] = "merged", method
        elif argv[1] == "view":
            out = json.dumps({"number": pr["number"], "body": pr["body"], "state": pr["state"].upper(), "isDraft": pr["draft"],
                              "baseRefName": pr["base"], "mergeStateStatus": s.get("merge_state", "CLEAN"),
                              "statusCheckRollup": s.get("checks", [])})
        elif argv[1] == "checks":
            code = s.get("checks_exit", 0)
            out = "no required checks reported" if code == 0 else "build  fail"
    state_path().write_text(json.dumps(s))
    if out:
        print(out, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
