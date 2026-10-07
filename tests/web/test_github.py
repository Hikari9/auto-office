import json

import pytest

from office.web import github as gh
from office.web.github import API, GitHubClient, Response, TransportError

TOKEN = "ghp_SECRETtoken1234567890"


class FakeTransport:
    """url -> list of responses (consumed in order, last one repeats) or an exception."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def set(self, url, *responses):
        self.routes[url] = list(responses)

    def __call__(self, method, url, headers):
        self.calls.append((url, dict(headers)))
        if url not in self.routes:
            return Response(404, {}, {"message": "Not Found"})
        queue = self.routes[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(body, etag=None, link=None):
    h = {}
    if etag:
        h["etag"] = etag
    if link:
        h["link"] = link
    return Response(200, h, body)


def repo(name, rid, perms=("pull",), archived=False, has_issues=True, private=False):
    return {"id": rid, "node_id": f"N{rid}", "full_name": name, "private": private,
            "archived": archived, "has_issues": has_issues,
            "permissions": {p: True for p in perms}}


def issue(n, title="t", pr=False):
    d = {"number": n, "title": title, "state": "open", "html_url": f"u/{n}"}
    if pr:
        d["pull_request"] = {"url": "x"}
    return d


REPOS = f"{API}/user/repos?per_page=100"


def issues_url(name):
    return f"{API}/repos/{name}/issues?state=open&per_page=100"


def pulls_url(name):
    return f"{API}/repos/{name}/pulls?state=open&per_page=100"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(routes=None):
    t, c = FakeTransport(routes), Clock()
    return GitHubClient(TOKEN, transport=t, clock=c), t, c


def test_resolve_token_order():
    assert gh.resolve_token({"GH_TOKEN": "a", "GITHUB_TOKEN": "b"}) == "a"
    assert gh.resolve_token({"GITHUB_TOKEN": "b"}) == "b"

    class Out:
        returncode, stdout = 0, "c\n"
    assert gh.resolve_token({}, run=lambda *a, **k: Out()) == "c"

    def boom(*a, **k):
        raise FileNotFoundError
    assert gh.resolve_token({}, run=boom) is None


def test_discovery_paginates_and_records_fields():
    page2 = f"{API}/user/repos?per_page=100&page=2"
    c, t, _ = make()
    t.set(REPOS, ok([repo("o/a", 1, ("pull", "triage", "push"))], link=f'<{page2}>; rel="next", <{page2}>; rel="last"'))
    t.set(page2, ok([repo("o/b", 2, ("pull",), private=True), repo("o/c", 3, ("admin", "push"))]))
    c.discover()
    by = {r["full_name"]: r for r in c.snapshot()["repos"]}
    assert set(by) == {"o/a", "o/b", "o/c"}
    a = by["o/a"]
    assert (a["id"], a["node_id"], a["permission"], a["writable"], a["visible"]) == (1, "N1", "push", True, True)
    assert a["issues_enabled"] is True and a["archived"] is False
    assert by["o/b"]["private"] is True and by["o/b"]["permission"] == "pull" and by["o/b"]["writable"] is False
    assert by["o/c"]["permission"] == "admin"
    assert t.calls[0][1]["Authorization"] == f"Bearer {TOKEN}"
    assert c.snapshot()["freshness"]["discovery"]["state"] == "fresh"


def test_issues_paginate_exclude_prs_and_same_number_in_two_repos():
    c, t, _ = make()
    t.set(REPOS, ok([repo("o/a", 1), repo("o/b", 2)]))
    nxt = issues_url("o/a") + "&page=2"
    t.set(issues_url("o/a"), ok([issue(7, "a7"), issue(8, pr=True)], link=f'<{nxt}>; rel="next"'))
    t.set(nxt, ok([issue(9, "a9")]))
    t.set(issues_url("o/b"), ok([issue(7, "b7")]))
    c.discover()
    c.refresh_issues("o/a")
    c.refresh_issues("o/b")
    got = {(i["repo"], i["number"]): i["title"] for i in c.snapshot()["issues"]}
    assert got == {("o/a", 7): "a7", ("o/a", 9): "a9", ("o/b", 7): "b7"}


def test_pulls_open_and_linked_with_separate_checks():
    c, t, _ = make()
    t.set(REPOS, ok([repo("o/a", 1, ("push",))]))
    open_pr = {"number": 3, "state": "open", "draft": True, "base": {"ref": "main"},
               "head": {"ref": "f", "sha": "s3"}, "merged_at": None, "html_url": "p3"}
    merged = {"number": 2, "state": "closed", "draft": False, "base": {"ref": "main"},
              "head": {"ref": "g", "sha": "s2"}, "merged": True, "html_url": "p2"}
    t.set(pulls_url("o/a"), ok([open_pr]))
    t.set(f"{API}/repos/o/a/pulls/2", ok(merged))
    t.set(f"{API}/repos/o/a/commits/s3/status", ok({"state": "pending"}))
    t.set(f"{API}/repos/o/a/commits/s2/status", ok({"state": "success"}))
    c.discover()
    c.refresh_pulls("o/a", linked=[2, 3, 99])  # 99 is missing: skipped, not a revocation
    snap = c.snapshot()
    prs = {p["number"]: p for p in snap["pulls"]}
    assert prs[3] == {"repo": "o/a", "number": 3, "state": "open", "draft": True, "base": "main",
                      "head": "f", "head_sha": "s3", "merged": False, "url": "p3"}
    assert prs[2]["merged"] is True and set(prs) == {2, 3}
    assert "checks" not in prs[3] and "github_checks" not in prs[3]
    assert {(x["number"], x["state"]) for x in snap["github_checks"]} == {(3, "pending"), (2, "success")}
    assert snap["freshness"]["repos"]["o/a"]["pulls"]["state"] == "fresh"
    assert snap["repos"][0]["access"] == "visible"


def test_etag_304_keeps_data_and_refreshes_freshness():
    c, t, clock = make()
    t.set(REPOS, ok([repo("o/a", 1)]))
    t.set(issues_url("o/a"), ok([issue(1, "one")], etag='"e1"'), Response(304, {}, None))
    c.discover()
    c.refresh_issues("o/a")
    clock.t = 2000.0
    c.refresh_issues("o/a")
    assert t.calls[-1][1]["If-None-Match"] == '"e1"'
    snap = c.snapshot()
    assert [i["title"] for i in snap["issues"]] == ["one"]
    f = snap["freshness"]["repos"]["o/a"]["issues"]
    assert f["state"] == "fresh" and f["fetched_at"] == 2000.0 and f["age"] == 0


def test_rate_limit_keeps_stale_snapshot_with_reset():
    c, t, clock = make()
    t.set(REPOS, ok([repo("o/a", 1)]))
    t.set(issues_url("o/a"), ok([issue(1)]),
          Response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5000"}, {}),
          Response(429, {"retry-after": "60"}, {}))
    c.discover()
    c.refresh_issues("o/a")
    clock.t = 1100.0
    c.refresh_issues("o/a")
    f = c.snapshot()["freshness"]["repos"]["o/a"]["issues"]
    assert (f["state"], f["reset_at"], f["age"]) == ("rate_limited", 5000.0, 100.0)
    assert len(c.snapshot()["issues"]) == 1
    c.refresh_issues("o/a")
    assert c.snapshot()["freshness"]["repos"]["o/a"]["issues"]["reset_at"] == 1160.0
    assert c.repos["o/a"]["access"] == "visible"


@pytest.mark.parametrize("failure", [TransportError("timeout"), Response(502, {}, {})])
def test_transient_failure_marks_stale_with_error_and_age(failure):
    c, t, clock = make()
    t.set(REPOS, ok([repo("o/a", 1)]))
    t.set(issues_url("o/a"), ok([issue(1)]), failure)
    c.discover()
    c.refresh_issues("o/a")
    clock.t = 1030.0
    c.refresh_issues("o/a")
    f = c.snapshot()["freshness"]["repos"]["o/a"]["issues"]
    assert f["state"] == "stale" and f["error"] and f["age"] == 30.0
    assert len(c.snapshot()["issues"]) == 1


def test_unauthenticated():
    c, t, _ = make({REPOS: [Response(401, {}, {})]})
    c.discover()
    assert c.snapshot()["freshness"]["discovery"]["state"] == "unauthenticated"


@pytest.mark.parametrize("how", ["404", "403", "dropped"])
def test_revocation_purges_everything_but_identity(how):
    c, t, _ = make()
    t.set(REPOS, ok([repo("o/a", 1, ("push",)), repo("o/b", 2)]))
    t.set(issues_url("o/a"), ok([issue(1, "secret title")]))
    t.set(pulls_url("o/a"), ok([{"number": 5, "state": "open", "head": {"sha": None}}]))
    c.discover()
    c.refresh_issues("o/a")
    c.refresh_pulls("o/a")
    if how == "dropped":
        t.set(REPOS, ok([repo("o/b", 2)], etag='"x"'))
        c.discover()
    else:
        t.set(issues_url("o/a"), Response(int(how), {}, {}))
        c.refresh_issues("o/a")
    snap = c.snapshot()
    a = next(r for r in snap["repos"] if r["full_name"] == "o/a")
    assert a == {"id": 1, "node_id": "N1", "full_name": "o/a", "access": "revoked"}
    assert not [i for i in snap["issues"] if i["repo"] == "o/a"]
    assert not [p for p in snap["pulls"] if p["repo"] == "o/a"]
    assert "secret title" not in json.dumps(snap)
    assert snap["freshness"]["repos"]["o/a"]["issues"]["state"] == "revoked"
    assert not any(k.startswith("o/a:") for k in c._cache)


def test_archived_and_issues_disabled_are_disabled_without_fetching():
    c, t, _ = make()
    t.set(REPOS, ok([repo("o/arch", 1, archived=True), repo("o/noiss", 2, has_issues=False)]))
    c.discover()
    c.refresh_issues("o/arch")
    c.refresh_issues("o/noiss")
    c.refresh_pulls("o/arch")
    f = c.snapshot()["freshness"]["repos"]
    assert f["o/arch"]["issues"]["state"] == "disabled" and f["o/arch"]["pulls"]["state"] == "disabled"
    assert f["o/noiss"]["issues"]["state"] == "disabled"
    assert all("/repos/" not in url for url, _ in t.calls)
    r = {x["full_name"]: x for x in c.snapshot()["repos"]}
    assert r["o/noiss"]["issues_enabled"] is False and r["o/arch"]["archived"] is True


def test_token_never_serialized():
    c, t, _ = make()
    leak = f"boom {TOKEN}"
    t.set(REPOS, ok([repo("o/a", 1)]))
    t.set(issues_url("o/a"), TransportError(leak))
    t.set(pulls_url("o/a"), Response(500, {}, {"message": TOKEN}))
    c.discover()
    c.refresh_issues("o/a")
    c.refresh_pulls("o/a")
    outputs = [json.dumps(c.snapshot()), repr(c)]
    try:
        c._request(issues_url("o/a"))
    except gh.GitHubError as e:
        outputs.append(str(e))
    assert all(TOKEN not in o for o in outputs)
    assert c.snapshot()["freshness"]["repos"]["o/a"]["issues"]["state"] == "stale"


def test_pagination_never_sends_token_off_host():
    c, t, _ = make()
    evil = "https://api.github.com.evil.example/user/repos?page=2"
    t.set(REPOS, ok([repo("o/a", 1)], link=f'<{evil}>; rel="next"'))
    c.discover()
    assert all(url != evil for url, _ in t.calls)
    assert c.snapshot()["freshness"]["discovery"]["state"] == "stale"
