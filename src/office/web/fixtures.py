"""Fixture mode for `office web --fixture small|large`.

Builds T1's synthetic workspace in a temp Office home and adds what the UI's
deliberate states need: a GitHub-visible repository that is not
execution-ready (named failing prerequisites), an archived and an
issues-disabled repository, a resumable run, command receipts in every state, and (large
scale) at least 2000 open issues. GitHub answers come from a fake transport
whose per-repository mode (`fresh`, `rate_limited`, `revoked`) the
fixture-only `POST /api/fixture/github` control switches. Nothing here touches
the real runs.db or GitHub.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from office import commands as receipts
from office import db
from office.util import dumps
from office.web import launcher as launch_mod
from office.web import synthetic
from office.web.capabilities import Resolver
from office.web.github import GitHubClient, Response
from office.web.service import FakeExecutor, Service

GITHUB_MODES = ("fresh", "rate_limited", "revoked")
RATE_RESET = 900  # seconds until a fixture rate limit resets
MIN_LARGE_ISSUES = 2000
NOT_READY = "synth-org-0/not-ready"
RESUMABLE_REPO = "synth-org-2/repo-02"
ARCHIVED = "synth-org-1/archived-repo"
NO_ISSUES = "synth-org-2/issues-disabled"
EXTRA_REPOS = {
    NOT_READY: {"archived": False, "has_issues": True},
    ARCHIVED: {"archived": True, "has_issues": True},
    NO_ISSUES: {"archived": False, "has_issues": False},
}
NOT_READY_FAILING = {"checkout_exists": "no local checkout found",
                     "git_repository": "checkout is not a git repository",
                     "origin_matches": "origin is missing"}
PREREQUISITES = ("checkout_exists", "git_repository", "origin_matches", "push_permission", "issues_enabled",
                 "not_archived")
# (id, final status, issue number on repo-00); `running` is seeded after start (startup recovery turns
# an earlier `running` into `unknown`).
RECEIPTS = (("fixture-cmd-accepted", "accepted", 1), ("fixture-cmd-completed", "completed", 2),
            ("fixture-cmd-failed", "failed", 3), ("fixture-cmd-unknown", "unknown", 4))
RUNNING_RECEIPT = ("fixture-cmd-running", "running", 5)


class FixtureGitHub:
    """The fake GitHub: synthetic data plus a per-repository mode the fixture control switches."""

    def __init__(self, data: dict):
        self.data = data
        self.meta = {r["slug"]: {"archived": False, "has_issues": True, **EXTRA_REPOS.get(r["slug"], {})}
                     for r in data["repos"]}
        self.modes: dict[str, str] = {}
        self.client: GitHubClient | None = None

    def transport(self, method, url, headers):
        parts = [p for p in urlparse(url).path.split("/") if p]
        data = self.data
        if parts == ["user", "repos"]:
            body = [{"id": i + 1, "node_id": f"R{i + 1}", "full_name": slug, "private": False,
                     "archived": m["archived"], "has_issues": m["has_issues"],
                     "permissions": {"pull": True, "push": True}}
                    for i, (slug, m) in enumerate(self.meta.items()) if self.modes.get(slug) != "revoked"]
            return Response(200, {}, body)
        if len(parts) < 4 or parts[0] != "repos":
            return Response(404, {}, None)
        slug = f"{parts[1]}/{parts[2]}"
        mode = self.modes.get(slug, "fresh")
        if slug not in self.meta or mode == "revoked":
            return Response(404, {}, None)
        if mode == "rate_limited":
            return Response(429, {"retry-after": str(RATE_RESET), "x-ratelimit-remaining": "0"}, None)
        if parts[3] == "issues":
            return Response(200, {}, [{"number": i["number"], "title": i["title"], "state": i["state"],
                                       "html_url": f"https://github.com/{slug}/issues/{i['number']}",
                                       "labels": [], "updated_at": None}
                                      for i in data["issues"] if i["repo"] == slug and i["state"] == "open"])
        if parts[3] == "pulls":
            view = [{"number": p["number"], "state": "open", "draft": False, "base": {"ref": p["base"]},
                     "head": {"ref": p["head"], "sha": f"sha{p['number']}"}, "merged_at": None,
                     "html_url": f"https://github.com/{slug}/pull/{p['number']}"}
                    for p in data["prs"] if p["repo"] == slug]
            if len(parts) == 5:
                one = [v for v in view if str(v["number"]) == parts[4]]
                return Response(200 if one else 404, {}, one[0] if one else None)
            return Response(200, {}, view)
        if parts[3] == "commits":
            return Response(200, {}, {"state": "success"})
        return Response(404, {}, None)

    def refresh(self, slug: str | None = None) -> None:
        client = self.client
        client.discover()
        for name in ([slug] if slug else list(self.meta)):
            client.refresh_issues(name)
            client.refresh_pulls(name)

    def set_mode(self, slug: str, mode: str) -> dict:
        """Switch one repository's GitHub answers, then refresh it so freshness reflects the mode."""
        if mode not in GITHUB_MODES:
            raise ValueError(f"state must be one of {', '.join(GITHUB_MODES)}")
        name = next((s for s in self.meta if s.lower() == slug.lower()), None)
        if name is None:
            raise KeyError(slug)
        self.modes[name] = mode
        self.client._cache.clear()  # a mode change must reach the transport, not an ETag cache
        self.refresh(name)
        return {"repo": name, "state": mode}


def _augment(data: dict, scale: str) -> dict:
    """The extra repositories and, at large scale, enough open issues for the >=2000 row target."""
    for slug in EXTRA_REPOS:
        data["repos"].append({"slug": slug, "git_common_dir": None})
        data["issues"] += [{"repo": slug, "number": n, "title": f"Issue {n} of {slug}", "state": "open"}
                           for n in (1, 2, 3)]
    if scale == "large":
        base = [r for r in data["repos"] if r["slug"] not in EXTRA_REPOS]
        missing = MIN_LARGE_ISSUES - sum(1 for i in data["issues"] if i["state"] == "open")
        per_repo = max(0, -(-missing // len(base))) + 5
        for r in base:
            top = max(i["number"] for i in data["issues"] if i["repo"] == r["slug"])
            data["issues"] += [{"repo": r["slug"], "number": n, "title": f"Issue {n} of {r['slug']}",
                                "state": "open"} for n in range(top + 1, top + 1 + per_repo)]
    return data


def _add_resumable_run(db_path: Path, data: dict) -> None:
    """One run with no session and no open dispatch on an otherwise run-free open issue: `resumable`."""
    repo = next(r for r in data["repos"] if r["slug"] == RESUMABLE_REPO)
    number = min(i["number"] for i in data["issues"]
                 if i["repo"] == RESUMABLE_REPO and i["state"] == "open" and i["number"] > 2)
    run_id = "0000fixture00001-resumable"
    con = db.connect(db_path)
    try:
        with db.transaction(con):
            synthetic.insert_run(con, run_id, git_common_dir=repo["git_common_dir"], goal="Fixture resumable run",
                                 landing={"issue": f"https://github.com/{RESUMABLE_REPO}/issues/{number}"},
                                 at=9000, end_state="merge")
            synthetic.insert_task(con, run_id, "T1", status="accepted", at=9000)
            synthetic.insert_task(con, run_id, "T2", status="queued", at=9001)
    finally:
        con.close()


def _readiness(repo: dict, checkout: Path | None) -> dict:
    name = repo["full_name"]
    failing = dict(NOT_READY_FAILING) if name == NOT_READY else {}
    if repo.get("access") == "revoked":
        failing.update(push_permission="access revoked", issues_enabled="access revoked",
                       not_archived="access revoked")
    if repo.get("archived"):
        failing["not_archived"] = "repository is archived"
    if repo.get("access") != "revoked" and not repo.get("has_issues", True):
        failing["issues_enabled"] = "issues are disabled"
    checks = [{"name": n, "ok": n not in failing, "detail": failing.get(n)} for n in PREREQUISITES]
    return {"full_name": name, "attached": "checkout_exists" not in failing,
            "checkout": None if "checkout_exists" in failing else "fixture", "ready": not failing,
            "failing": [c["name"] for c in checks if not c["ok"]], "prerequisites": checks}


def _receipt(con, cid: str, status: str, number: int) -> None:
    target = {"repo": "synth-org-0/repo-00", "issue": number}
    seeded = receipts.record(con, command_id=cid, kind="start_issue", target=dumps(target),
                             payload={"target": target, "expect": {}, "payload": {"end_state": "preview"}},
                             origin="fixture")
    if seeded["replayed"] or status == "accepted":
        return
    receipts.transition(con, cid, "running", pid=os.getpid())
    if status == "running":
        return
    outcome = {"completed": ({"pane": "fixture-pane", "command": None}, None),
               "failed": ({"refused": "repo-not-ready"}, "fixture: the launch was refused"),
               "unknown": ({}, "executor process ended before completion")}[status]
    receipts.transition(con, cid, status, result=outcome[0], error=outcome[1])


class FixtureService(Service):
    """A Service over the fixture workspace, optionally with a seeded `running` receipt."""

    fixture_github: FixtureGitHub
    seed_running = False

    def _load_launches(self) -> None:
        # start() calls this after startup recovery (which turns any earlier `running` receipt into
        # `unknown`) and before its first poll: the one point a `running` receipt survives.
        if self.seed_running:
            self.writer(_receipt, *RUNNING_RECEIPT)
        super()._load_launches()


def build(scale: str, home: Path | None = None, *, seed_receipts: bool = False) -> FixtureService:
    """The fixture service; `seed_receipts` adds one command receipt per state (`office web --fixture` does)."""
    root = Path(home or tempfile.mkdtemp(prefix="office-web-fixture-"))
    ws = synthetic.build_workspace(root / "data", scale)
    data = _augment(json.loads(Path(ws["github"]).read_text(encoding="utf-8")), scale)
    _add_resumable_run(ws["db"], data)
    gh = FixtureGitHub(data)
    gh.client = GitHubClient(token="fixture-token", transport=gh.transport)
    gh.refresh()
    if seed_receipts:
        con = db.connect(ws["db"])
        try:
            for receipt in RECEIPTS:
                _receipt(con, *receipt)
        finally:
            con.close()
    checkouts_root = root / "checkouts"

    def checkouts(full_name: str) -> Path | None:
        if not full_name or full_name == NOT_READY:
            return None
        path = checkouts_root / full_name.replace("/", "__")
        path.mkdir(parents=True, exist_ok=True)
        return path

    service = FixtureService(
        ws["db"], root / "state", launcher=launch_mod.FakeLauncher(), executor=FakeExecutor(),
        resolver=Resolver(registry=lambda line: None, probe=lambda argv: False, current=synthetic.CURRENT_VERSION),
        github=gh.client, checkouts=checkouts, readiness=_readiness,
        host_probe=lambda: {"cpu": {"status": "unavailable", "value": None},
                            "ram": {"status": "unavailable", "value": None}},
        observer_ctx={"repo_slugs": synthetic.repo_slugs(ws["github"]), "runs_dir": ws["runs_dir"],
                      "home": str(root)},
        fixture=scale)
    service.fixture_github, service.seed_running = gh, seed_receipts
    return service
