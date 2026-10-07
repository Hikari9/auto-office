"""GitHub discovery, issue/PR linkage and per-source freshness for the web UI.

The token lives only on the client instance. Snapshots, errors and repr never
carry it, and every message passes through `_scrub` as a second guard. All HTTP
goes through an injectable transport so tests never touch the network.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

API = "https://api.github.com"
PERMISSION_ORDER = ("pull", "triage", "push", "maintain", "admin")

FRESH, STALE, RATE_LIMITED = "fresh", "stale", "rate_limited"
UNAUTHENTICATED, REVOKED, DISABLED = "unauthenticated", "revoked", "disabled"


def resolve_token(env: Mapping[str, str] | None = None,
                  run: Callable[..., Any] = subprocess.run) -> str | None:
    """GH_TOKEN, then GITHUB_TOKEN, then `gh auth token`; None when absent."""
    env = os.environ if env is None else env
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if env.get(name, "").strip():
            return env[name].strip()
    try:
        out = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    token = (out.stdout or "").strip() if out.returncode == 0 else ""
    return token or None


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # lowercase keys
    body: Any = None  # parsed JSON


class TransportError(Exception):
    """Network-level failure (no HTTP status)."""


class Transport(Protocol):
    def __call__(self, method: str, url: str, headers: Mapping[str, str]) -> Response: ...


def urllib_transport(method: str, url: str, headers: Mapping[str, str]) -> Response:
    req = urllib.request.Request(url, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw, status, hdrs = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, e.headers
    except (urllib.error.URLError, OSError) as e:
        raise TransportError(type(e).__name__) from None
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    return Response(status, {k.lower(): v for k, v in hdrs.items()}, body)


class GitHubError(Exception):
    def __init__(self, state: str, message: str, reset_at: float | None = None):
        super().__init__(message)
        self.state, self.reset_at = state, reset_at


_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def next_link(link: str | None) -> str | None:
    m = _NEXT.search(link or "")
    return m.group(1) if m else None


def permission_level(perms: Mapping[str, bool] | None) -> str | None:
    for level in reversed(PERMISSION_ORDER):
        if (perms or {}).get(level):
            return level
    return None


def is_writable(level: str | None) -> bool:
    return level in PERMISSION_ORDER and PERMISSION_ORDER.index(level) >= PERMISSION_ORDER.index("push")


@dataclass
class Freshness:
    state: str
    checked_at: float
    fetched_at: float | None = None
    error: str | None = None
    reset_at: float | None = None

    def view(self, now: float) -> dict:
        age = None if self.fetched_at is None else round(now - self.fetched_at, 3)
        return {"state": self.state, "checked_at": self.checked_at, "fetched_at": self.fetched_at,
                "age": age, "error": self.error, "reset_at": self.reset_at}


@dataclass
class _Cached:
    etag: str | None
    data: Any


@dataclass
class GitHubClient:
    token: str = field(repr=False)
    transport: Transport = urllib_transport
    clock: Callable[[], float] = time.time
    api: str = API
    repos: dict[str, dict] = field(default_factory=dict)
    issues: dict[str, list[dict]] = field(default_factory=dict)
    pulls: dict[str, dict[int, dict]] = field(default_factory=dict)
    github_checks: dict[str, dict[int, str | None]] = field(default_factory=dict)
    freshness: dict[tuple[str, str | None], Freshness] = field(default_factory=dict)
    _cache: dict[str, _Cached] = field(default_factory=dict, repr=False)

    # -- HTTP -------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _request(self, url: str, etag: str | None = None) -> Response:
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "Authorization": f"Bearer {self.token}"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            resp = self.transport("GET", url, headers)
        except TransportError as e:
            raise GitHubError(STALE, self._scrub(f"network error: {e}")) from None
        except OSError as e:
            raise GitHubError(STALE, self._scrub(f"network error: {type(e).__name__}")) from None
        if resp.status in (200, 304):
            return resp
        raise self._classify(resp)

    def _classify(self, resp: Response) -> GitHubError:
        h, now = resp.headers, self.clock()
        if resp.status in (403, 429) and (h.get("x-ratelimit-remaining") == "0" or "retry-after" in h):
            reset = None
            if h.get("retry-after", "").isdigit():
                reset = now + int(h["retry-after"])
            elif h.get("x-ratelimit-reset", "").isdigit():
                reset = float(h["x-ratelimit-reset"])
            return GitHubError(RATE_LIMITED, f"rate limited (HTTP {resp.status})", reset)
        if resp.status == 401:
            return GitHubError(UNAUTHENTICATED, "authentication failed (HTTP 401)")
        if resp.status in (403, 404):
            return GitHubError(REVOKED, f"not accessible (HTTP {resp.status})")
        return GitHubError(STALE, f"GitHub error (HTTP {resp.status})")

    def _get_all(self, key: str, url: str) -> tuple[list, bool]:
        """Paginated GET with ETag on the first page. Returns (items, changed)."""
        cached = self._cache.get(key)
        first = self._request(url, cached.etag if cached else None)
        if first.status == 304 and cached:
            return cached.data, False
        items, resp = list(first.body or []), first
        while (url := next_link(resp.headers.get("link"))):
            if not url.startswith(self.api + "/"):
                raise GitHubError(STALE, "pagination link left the GitHub API host")
            resp = self._request(url)
            items.extend(resp.body or [])
        self._cache[key] = _Cached(first.headers.get("etag"), items)
        return items, True

    def _get_one(self, key: str, url: str) -> dict:
        cached = self._cache.get(key)
        resp = self._request(url, cached.etag if cached else None)
        if resp.status == 304 and cached:
            return cached.data
        self._cache[key] = _Cached(resp.headers.get("etag"), resp.body)
        return resp.body

    def _item(self, key: str, url: str) -> dict | None:
        """A single PR or status inside a visible repo; its own 404 is not a repo revocation."""
        try:
            return self._get_one(key, url)
        except GitHubError as e:
            if e.state == REVOKED:
                return None
            raise

    # -- freshness --------------------------------------------------------
    def _ok(self, source: str, repo: str | None) -> None:
        now = self.clock()
        self.freshness[(source, repo)] = Freshness(FRESH, now, now)

    def _fail(self, source: str, repo: str | None, err: GitHubError) -> None:
        prev = self.freshness.get((source, repo))
        self.freshness[(source, repo)] = Freshness(
            err.state, self.clock(), prev.fetched_at if prev else None,
            self._scrub(str(err)), err.reset_at)

    def _mark(self, source: str, repo: str, state: str, error: str | None = None) -> None:
        prev = self.freshness.get((source, repo))
        self.freshness[(source, repo)] = Freshness(
            state, self.clock(), prev.fetched_at if prev else None, error)

    # -- discovery --------------------------------------------------------
    def discover(self) -> None:
        try:
            raw, _ = self._get_all("repos", f"{self.api}/user/repos?per_page=100")
        except GitHubError as e:
            self._fail("discovery", None, e)
            return
        seen = set()
        for r in raw:
            level = permission_level(r.get("permissions"))
            name = r["full_name"]
            seen.add(name)
            self.repos[name] = {
                "id": r["id"], "node_id": r.get("node_id"), "full_name": name,
                "private": bool(r.get("private")), "archived": bool(r.get("archived")),
                "has_issues": bool(r.get("has_issues")), "permission": level,
                "visible": True, "writable": is_writable(level),
                "issues_enabled": bool(r.get("has_issues")), "access": "visible",
            }
        for name, repo in list(self.repos.items()):
            if name not in seen and repo["access"] != REVOKED:
                self.revoke(name, "dropped from discovery")
        self._ok("discovery", None)

    def revoke(self, name: str, reason: str) -> None:
        """Purge everything but identity for a repo that is no longer visible."""
        repo = self.repos.get(name, {"full_name": name, "id": None, "node_id": None})
        self.repos[name] = {"id": repo["id"], "node_id": repo["node_id"],
                            "full_name": name, "access": REVOKED}
        self.issues.pop(name, None)
        self.pulls.pop(name, None)
        self.github_checks.pop(name, None)
        prefix = f"{name}:"
        for key in [k for k in self._cache if k.startswith(prefix)]:
            del self._cache[key]
        for source in ("issues", "pulls"):
            self._mark(source, name, REVOKED, reason)
            self.freshness[(source, name)].fetched_at = None

    def _usable(self, name: str, source: str) -> bool:
        repo = self.repos.get(name)
        if repo is None or repo["access"] == REVOKED:
            return False
        if repo["archived"] or (source == "issues" and not repo["has_issues"]):
            why = "archived" if repo["archived"] else "issues disabled"
            self._mark(source, name, DISABLED, why)
            return False
        return True

    def _repo_error(self, source: str, name: str, err: GitHubError) -> None:
        if err.state == REVOKED:
            self.revoke(name, str(err))
        else:
            self._fail(source, name, err)

    # -- issues and PRs ---------------------------------------------------
    def refresh_issues(self, name: str) -> None:
        if not self._usable(name, "issues"):
            return
        try:
            raw, _ = self._get_all(f"{name}:issues",
                                   f"{self.api}/repos/{name}/issues?state=open&per_page=100")
        except GitHubError as e:
            self._repo_error("issues", name, e)
            return
        self.issues[name] = [
            {"repo": name, "number": i["number"], "title": i.get("title"), "state": i.get("state"),
             "url": i.get("html_url"), "labels": [lb.get("name") for lb in i.get("labels") or []],
             "updated_at": i.get("updated_at")}
            for i in raw if "pull_request" not in i]
        self._ok("issues", name)

    def refresh_pulls(self, name: str, linked: tuple[int, ...] | list[int] = ()) -> None:
        """Open PRs plus the PRs Office links, each with its combined check status."""
        if not self._usable(name, "pulls"):
            return
        try:
            raw, _ = self._get_all(f"{name}:pulls",
                                   f"{self.api}/repos/{name}/pulls?state=open&per_page=100")
            by_number = {p["number"]: p for p in raw}
            for n in linked:
                if n not in by_number:
                    by_number[n] = self._item(f"{name}:pull:{n}", f"{self.api}/repos/{name}/pulls/{n}")
            pulls, checks = {}, {}
            for n, p in by_number.items():
                if p is None:
                    continue
                pulls[n] = {
                    "repo": name, "number": n, "state": p.get("state"), "draft": bool(p.get("draft")),
                    "base": (p.get("base") or {}).get("ref"), "head": (p.get("head") or {}).get("ref"),
                    "head_sha": (p.get("head") or {}).get("sha"), "merged": bool(p.get("merged") or p.get("merged_at")),
                    "url": p.get("html_url")}
                sha = pulls[n]["head_sha"]
                status = self._item(f"{name}:status:{sha}",
                                    f"{self.api}/repos/{name}/commits/{sha}/status") if sha else None
                checks[n] = (status or {}).get("state")
        except GitHubError as e:
            self._repo_error("pulls", name, e)
            return
        self.pulls[name], self.github_checks[name] = pulls, checks
        self._ok("pulls", name)

    # -- output -----------------------------------------------------------
    def snapshot(self) -> dict:
        now = self.clock()
        fresh: dict = {"discovery": None, "repos": {}}
        for (source, repo), f in self.freshness.items():
            if repo is None:
                fresh[source] = f.view(now)
            else:
                fresh["repos"].setdefault(repo, {})[source] = f.view(now)
        return {
            "repos": [dict(r) for r in self.repos.values()],
            "issues": [dict(i) for items in self.issues.values() for i in items],
            "pulls": [dict(p) for prs in self.pulls.values() for p in prs.values()],
            "github_checks": [{"repo": r, "number": n, "state": s}
                              for r, cs in self.github_checks.items() for n, s in cs.items()],
            "freshness": fresh,
        }
