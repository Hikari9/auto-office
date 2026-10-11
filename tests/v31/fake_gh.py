"""A scripted `gh` for PR tests: PR state lives in $FAKE_GH_STATE (JSON)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

def state_path() -> Path:
    # Read per call: tests import this module in-process, so an import-time constant
    # would pin every later test to the first test's state file.
    return Path(os.environ["FAKE_GH_STATE"])


def load() -> dict:
    if state_path().exists():
        return json.loads(state_path().read_text())
    return {"repo": {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "mergeCommitAllowed": True,
                     "squashMergeAllowed": True, "rebaseMergeAllowed": True}, "prs": [], "calls": []}


def opt(args: list[str], name: str, default=None):
    return args[args.index(name) + 1] if name in args else default


def remote_head(pr: dict) -> str:
    out = subprocess.run(["git", "ls-remote", "origin", f"refs/heads/{pr['head']}"], capture_output=True, text=True).stdout
    return out.split()[0] if out.strip() else ""


def conflicting(s: dict, pr: dict) -> bool:
    """`conflicting: {"<n>": "<head sha>"}`: GitHub reports PR n unmergeable while its branch head is that sha."""
    return remote_head(pr) == (s.get("conflicting") or {}).get(str(pr["number"]))


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
    if argv[:2] == ["issue", "close"]:
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
        elif argv[1] == "edit" and (s.get("edit_failures") or {}).get(str(pr["number"]), 0) > 0:
            s["edit_failures"][str(pr["number"])] -= 1  # `edit_failures: {"<n>": times}`
            code, out = 1, "error connecting to api.github.com"
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
            elif (s.get("merge_error") or {}).get(str(pr["number"])):
                code, out = 1, s["merge_error"][str(pr["number"])]  # `merge_error: {"<n>": "<gh message>"}`
            elif "--match-head-commit" in argv and opt(argv, "--match-head-commit") != remote_head(pr):
                code, out = 1, "Head branch was modified. Review and try the merge again."
            elif conflicting(s, pr):
                code, out = 1, f"X Pull request #{pr['number']} is not mergeable: the merge commit cannot be cleanly created."
            else:
                method = next(a for a in argv if a in ("--merge", "--squash", "--rebase"))
                git_merge(pr, method)
                pr["state"], pr["merged_with"] = "merged", method
        elif argv[1] == "view":
            out = json.dumps({"number": pr["number"], "body": pr["body"], "state": pr["state"].upper(), "isDraft": pr["draft"],
                              "baseRefName": pr["base"], "headRefOid": remote_head(pr), "mergeStateStatus": s.get("merge_state", "CLEAN"),
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
