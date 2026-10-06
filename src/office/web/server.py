"""`office web`: the loopback-only HTTP server and its daemon lifecycle.

`serve` runs in the foreground; `start` daemonizes `serve` and waits for its
pid file under `<state home>/web/`; `stop` and `status` read that file. The
server binds loopback only and refuses any other `--host`. Fixture mode
(`--fixture small|large`) serves T1's synthetic workspace from a temp Office
home with T4's client on a fake transport and fake launcher/executor: it never
touches the real runs.db or GitHub.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from functools import lru_cache
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from office import paths
from office.result import Result
from office.state import OfficeError
from office.web import api, launcher as launch_mod
from office.web.service import FakeExecutor, Service, _effective_config

log = logging.getLogger("office.web")
DEFAULT_PORT = 8765
GITHUB_REFRESH = 120.0


class WebError(OfficeError):
    exit_code = 2


def loopback_host(host: str) -> str:
    """The address to bind, or WebError for anything that is not loopback."""
    if host == "localhost":
        return "127.0.0.1"
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        raise WebError("non-loopback-host", f"--host {host!r} is not a loopback address",
                       next_step="use 127.0.0.1 (the default), localhost or ::1") from None
    if not addr.is_loopback:
        raise WebError("non-loopback-host", f"--host {host} is not loopback; the web service never listens off-host",
                       next_step="use 127.0.0.1 (the default), localhost or ::1")
    return str(addr)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    address_family = socket.AF_INET6


def make_server(service: Service, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    bind = loopback_host(host)
    cls = _Server6 if ":" in bind else _Server
    httpd = cls((bind, port), api.Handler)
    real_port = httpd.server_address[1]
    httpd.RequestHandlerClass = api.make_handler(service, bind, real_port)
    return httpd


# ------------------------------------------------------------------ fixture mode

def fixture_transport(github_json: Path):
    """A GitHub transport answering from a synthetic workspace's github.json."""
    from office.web.github import Response
    data = json.loads(Path(github_json).read_text(encoding="utf-8"))
    repos = {r["slug"]: r for r in data["repos"]}

    def transport(method, url, headers):
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if path == "/user/repos":
            body = [{"id": i + 1, "node_id": f"R{i + 1}", "full_name": slug, "private": False, "archived": False,
                     "has_issues": True, "permissions": {"pull": True, "push": True}}
                    for i, slug in enumerate(repos)]
            return Response(200, {"etag": '"repos"'}, body)
        if len(parts) >= 4 and parts[0] == "repos":
            slug = f"{parts[1]}/{parts[2]}"
            if slug not in repos:
                return Response(404, {}, None)
            if parts[3] == "issues":
                return Response(200, {}, [{"number": i["number"], "title": i["title"], "state": i["state"],
                                           "html_url": f"https://github.com/{slug}/issues/{i['number']}",
                                           "labels": [], "updated_at": None}
                                          for i in data["issues"] if i["repo"] == slug and i["state"] == "open"])
            if parts[3] == "pulls":
                prs = [p for p in data["prs"] if p["repo"] == slug]
                view = [{"number": p["number"], "state": "open", "draft": False, "base": {"ref": p["base"]},
                         "head": {"ref": p["head"], "sha": f"sha{p['number']}"}, "merged_at": None,
                         "html_url": f"https://github.com/{slug}/pull/{p['number']}"} for p in prs]
                if len(parts) == 5:
                    one = [v for v in view if str(v["number"]) == parts[4]]
                    return Response(200 if one else 404, {}, one[0] if one else None)
                return Response(200, {}, view)
            if parts[3] == "commits":
                return Response(200, {}, {"state": "success"})
        return Response(404, {}, None)
    return transport


def build_fixture(scale: str, home: Path | None = None) -> Service:
    from office.web import synthetic
    from office.web.github import GitHubClient
    root = Path(home or tempfile.mkdtemp(prefix="office-web-fixture-"))
    ws = synthetic.build_workspace(root / "data", scale)
    client = GitHubClient(token="fixture-token", transport=fixture_transport(ws["github"]))
    client.discover()
    slugs = synthetic.repo_slugs(ws["github"])
    for slug in sorted(set(slugs.values())):
        client.refresh_issues(slug)
        client.refresh_pulls(slug)
    checkouts_root = root / "checkouts"

    def checkouts(full_name: str) -> Path | None:
        if not full_name:
            return None
        path = checkouts_root / full_name.replace("/", "__")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def readiness(repo: dict, checkout: Path | None) -> dict:
        return {"full_name": repo["full_name"], "attached": True, "checkout": "fixture", "ready": True,
                "failing": [], "prerequisites": []}

    return Service(ws["db"], root / "state", launcher=launch_mod.FakeLauncher(), executor=FakeExecutor(),
                   resolver=_fixture_resolver(), github=client, checkouts=checkouts, readiness=readiness,
                   host_probe=lambda: {"cpu": {"status": "unavailable", "value": None},
                                       "ram": {"status": "unavailable", "value": None}},
                   observer_ctx={"repo_slugs": slugs, "runs_dir": ws["runs_dir"], "home": str(root)},
                   fixture=scale)


def _fixture_resolver():
    from office.web import synthetic
    from office.web.capabilities import Resolver
    return Resolver(registry=lambda line: None, probe=lambda argv: False, current=synthetic.CURRENT_VERSION)


# ------------------------------------------------------------------ the real service

def build_real() -> Service:
    from office.web import github, repos
    db_path = paths.runs_db()

    @lru_cache(maxsize=256)
    def slug_of(git_common_dir: str) -> str | None:
        root = Path(git_common_dir).parent if git_common_dir.endswith(".git") else Path(git_common_dir)
        return repos.remote_full_name(repos.run_git(root, ["remote", "get-url", "origin"]))

    token = github.resolve_token()
    client = github.GitHubClient(token=token) if token else None
    conf = _web_config()
    configured = {k.lower(): Path(v).expanduser() for k, v in (conf.get("checkouts") or {}).items()}
    service: Service | None = None

    def checkouts(full_name: str) -> Path | None:
        if not full_name:
            return None
        if full_name.lower() in configured:
            return configured[full_name.lower()]
        rows = service.observer.read(lambda s: s.rows(
            "SELECT DISTINCT repo_root, git_common_dir FROM runs WHERE repo_root IS NOT NULL")) if service else []
        for r in rows:
            if r["git_common_dir"] and slug_of(r["git_common_dir"]) == full_name.lower() and Path(r["repo_root"]).is_dir():
                return Path(r["repo_root"])
        return None

    harness = str((_effective_config().get("scheduler") or {}).get("orchestrator_route") or "claude")
    service = Service(db_path, paths.state_home(), launcher=launch_mod.HerdrLauncher(harness), github=client,
                      checkouts=checkouts, observer_ctx={"repo_slugs": slug_of})
    return service


def _web_config() -> dict:
    return _effective_config().get("web") or {}


def github_refresher(service: Service, interval: float = GITHUB_REFRESH) -> threading.Thread | None:
    client = service.github
    if client is None:
        return None

    def loop():
        while not service.stopping.is_set():
            try:
                client.discover()
                snap = service.snapshot_state or {"entities": {"repos": {}, "prs": {}}}
                for name, repo in list(client.repos.items()):
                    if repo.get("access") == "revoked":
                        continue
                    key = f"repo:github.com/{name.lower()}"
                    local = snap["entities"]["repos"].get(key, {})
                    if not local.get("runs") and service.checkouts(name) is None:
                        continue  # only repositories this machine works on
                    client.refresh_issues(name)
                    linked = [p["number"] for p in snap["entities"]["prs"].values()
                              if p.get("number") and (p.get("ref") or "").startswith(f"pr:{key}#")]
                    client.refresh_pulls(name, linked)
            except Exception:  # noqa: BLE001 - freshness records the failure; keep refreshing
                log.exception("office web: GitHub refresh failed")
            service.stopping.wait(interval)
    t = threading.Thread(target=loop, name="office-web-github", daemon=True)
    t.start()
    return t


# ------------------------------------------------------------------ daemon lifecycle

def web_dir() -> Path:
    return paths.state_home() / "web"


def pid_file() -> Path:
    return web_dir() / "web.pid"


def _started(pid: int) -> str | None:
    """The process start time `ps` reports (macOS and Linux), to tell a reused pid apart."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _ours(info: dict | None) -> bool:
    """The pid file names a live process that is still the server that wrote it."""
    if not info or not _alive(info["pid"]):
        return False
    return info.get("started") is None or _started(info["pid"]) == info["started"]


def _alive(pid: int) -> bool:
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False  # our own child, now reaped
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid() -> dict | None:
    try:
        info = json.loads(pid_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and isinstance(info.get("pid"), int) else None


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, fixture: str | None = None,
          ready: threading.Event | None = None) -> int:
    """Foreground server. Writes the pid file once bound; removes it on exit."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    loopback_host(host)
    service = build_fixture(fixture) if fixture else build_real()
    service.start()
    httpd = make_server(service, host, port)
    real_port = httpd.server_address[1]
    url = f"http://{'[::1]' if ':' in httpd.server_address[0] else httpd.server_address[0]}:{real_port}/"
    try:
        service.run_poller()
        github_refresher(service)

        def stop(*_):
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, stop)
        web_dir().mkdir(parents=True, exist_ok=True)
        tmp = pid_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "started": _started(os.getpid()),
                                   "host": httpd.server_address[0], "port": real_port, "url": url,
                                   "fixture": fixture}), encoding="utf-8")
        os.replace(tmp, pid_file())  # written last and atomically: a reader sees a ready server or none
        log.info("office web: serving %s%s", url, f" (fixture {fixture})" if fixture else "")
        if ready:
            ready.set()
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        httpd.server_close()
        info = read_pid()
        if info and info["pid"] == os.getpid():
            pid_file().unlink(missing_ok=True)
    return 0


def start(host: str = "127.0.0.1", port: int = DEFAULT_PORT, fixture: str | None = None,
          wait: float = 20.0) -> Result:
    loopback_host(host)
    info = read_pid()
    if _ours(info):
        return Result(lines=[f"office web already running at {info['url']} (pid {info['pid']})"],
                      next="office web stop", data=info)
    web_dir().mkdir(parents=True, exist_ok=True)
    from office import frontdoor
    argv, extra = frontdoor.current_argv()
    cmd = [*argv, "web", "serve", "--host", host, "--port", str(port)] + (["--fixture", fixture] if fixture else [])
    env = {**os.environ, **extra}
    with open(web_dir() / "web.log", "ab") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True, close_fds=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        info = read_pid()
        if info and info["pid"] == proc.pid:
            return Result(lines=[f"office web running at {info['url']} (pid {proc.pid})"], next="office web stop",
                          data=info)
        if proc.poll() is not None:
            raise WebError("web-start-failed", f"office web exited with {proc.returncode}",
                           next_step=f"see {web_dir() / 'web.log'}")
        time.sleep(0.1)
    raise WebError("web-start-failed", f"office web did not report ready within {wait:g}s",
                   next_step=f"see {web_dir() / 'web.log'}")


def stop(wait: float = 10.0) -> Result:
    info = read_pid()
    if not _ours(info):
        pid_file().unlink(missing_ok=True)
        return Result(lines=["office web is not running"])
    os.kill(info["pid"], signal.SIGTERM)
    deadline = time.time() + wait
    while time.time() < deadline and _alive(info["pid"]):
        time.sleep(0.1)
    if _alive(info["pid"]):
        raise WebError("web-stop-failed", f"pid {info['pid']} did not stop within {wait:g}s",
                       next_step=f"kill {info['pid']}")
    pid_file().unlink(missing_ok=True)
    return Result(lines=[f"office web stopped (pid {info['pid']})"])


def status() -> Result:
    info = read_pid()
    if _ours(info):
        return Result(lines=[f"office web running at {info['url']} (pid {info['pid']})"
                             + (f" fixture {info['fixture']}" if info.get("fixture") else "")], data=info)
    return Result(lines=["office web is not running"], next="office web start", data={"running": False})


def main(args) -> Result | int:
    action = args.action
    if action == "serve":
        return serve(args.host, args.port, args.fixture)
    if action == "start":
        return start(args.host, args.port, args.fixture)
    if action == "stop":
        return stop()
    return status()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(serve())
