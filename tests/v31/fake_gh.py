"""A scripted `gh` for PR tests: PR state lives in $FAKE_GH_STATE (JSON)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_GH_STATE"])


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"repo": {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "mergeCommitAllowed": True,
                     "squashMergeAllowed": True, "rebaseMergeAllowed": True}, "prs": [], "calls": []}


def opt(args: list[str], name: str, default=None):
    return args[args.index(name) + 1] if name in args else default


def main(argv: list[str]) -> int:
    s = load()
    s["calls"].append(argv)
    out, code = "", 0
    if argv[:2] == ["repo", "view"]:
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
                pr["state"], pr["merged_with"] = "merged", next(a for a in argv if a in ("--merge", "--squash", "--rebase"))
        elif argv[1] == "view":
            out = json.dumps({"number": pr["number"], "state": pr["state"].upper(), "isDraft": pr["draft"],
                              "baseRefName": pr["base"], "mergeStateStatus": s.get("merge_state", "CLEAN"),
                              "statusCheckRollup": s.get("checks", [])})
        elif argv[1] == "checks":
            code = s.get("checks_exit", 0)
    STATE.write_text(json.dumps(s))
    if out:
        print(out, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
