"""HTTP handler: security checks, JSON API and the SSE stream.

Every request must carry `Host: <bound loopback host>:<port>` (a DNS-rebinding
guard). A POST additionally needs `Content-Type: application/json`, an
`Origin` equal to this server's origin when one is sent, and an
`X-Office-Token` equal to the per-process token embedded in index.html.
"""
from __future__ import annotations

import hmac
import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from office.web.service import Command, CommandRefused, Service

STATIC = Path(__file__).with_name("static")
MAX_BODY = 64 * 1024
HEARTBEAT = 15.0


def allowed_hosts(host: str, port: int) -> set[str]:
    names = {host}
    if host == "127.0.0.1":
        names.add("localhost")
    if host == "::1":
        names = {"[::1]"}
    return {f"{n}:{port}" for n in names}


class Handler(BaseHTTPRequestHandler):
    server_version = "OfficeWeb/1"
    protocol_version = "HTTP/1.1"
    service: Service  # set by make_handler
    hosts: set[str]

    def log_message(self, fmt, *args):  # quiet: the service logs what matters
        pass

    # ------------------------------------------------------------------ responses

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body, sort_keys=True, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _refuse(self, code: int, reason: str, message: str) -> None:
        self._send(code, {"ok": False, "reason": reason, "message": message})

    # ------------------------------------------------------------------ guards

    def _host_ok(self) -> bool:
        if self.headers.get("Host") not in self.hosts:
            self._refuse(421, "bad-host", "the Host header does not name this loopback server")
            return False
        return True

    def _post_ok(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._refuse(415, "bad-content-type", "POST bodies must be application/json")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.hosts}:
            self._refuse(403, "bad-origin", "the Origin does not match this server")
            return False
        token = self.headers.get("X-Office-Token") or ""
        if not hmac.compare_digest(token.encode(), self.service.token.encode()):
            self._refuse(403, "bad-token", "missing or wrong X-Office-Token")
            return False
        return True

    # ------------------------------------------------------------------ GET

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        try:
            if url.path in ("/", "/index.html"):
                return self._index()
            if url.path == "/api/snapshot":
                return self._send(200, self.service.snapshot())
            if url.path == "/api/stream":
                return self._stream(self.headers.get("Last-Event-ID") or q.get("last_event_id"))
            if url.path == "/api/settings":
                return self._send(200, self.service.settings_view(run_id=q.get("run"), repo=q.get("repo")))
            if len(parts) == 3 and parts[:2] == ["api", "commands"]:
                found = self.service.command(parts[2])
                return self._send(200 if found else 404, found or {"ok": False, "reason": "unknown-command"})
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "activity":
                limit = int(q["limit"]) if q.get("limit", "").isdigit() else None
                before = int(q["before"]) if q.get("before", "").isdigit() else None
                return self._send(200, self.service.activity(parts[2], limit=limit, before_seq=before))
        except CommandRefused as exc:
            return self._send(exc.http, exc.body())
        except (sqlite3.Error, OSError):
            return self._refuse(503, "office-disconnected", "runs.db is not readable right now")
        self._refuse(404, "not-found", url.path)

    def _index(self) -> None:
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        fixture = self.service.fixture or ""
        page = page.replace("__OFFICE_TOKEN__", html.escape(self.service.token, quote=True))
        page = page.replace("__OFFICE_FIXTURE__", html.escape(fixture, quote=True))
        if fixture:  # the marker shows before (and without) the scripts
            page = page.replace('data-testid="fixture-marker" hidden></span>',
                                f'data-testid="fixture-marker">FIXTURE MODE ({html.escape(fixture)})</span>')
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _event(self, event: str, data: dict, event_id: str | None = None) -> None:
        out = f"event: {event}\n" + (f"id: {event_id}\n" if event_id else "") + \
            f"data: {json.dumps(data, sort_keys=True, default=str)}\n\n"
        self.wfile.write(out.encode())
        self.wfile.flush()

    def _stream(self, last_event_id: str | None) -> None:
        svc = self.service
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        mode, deltas = svc.events_since(last_event_id)
        try:
            if mode in ("snapshot", "resync"):
                snap = svc.snapshot()
                self._event(mode, snap, f"{snap['epoch']}:{snap['rev']}")
                rev = snap["rev"]
            else:
                rev = int(last_event_id.partition(":")[2])
                for d in deltas:
                    self._event("delta", d, f"{d['epoch']}:{d['rev']}")
                    rev = d["rev"]
            while not svc.stopping.is_set():
                pending = svc.wait_for(rev, HEARTBEAT)
                if not pending:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if pending[0]["base_rev"] != rev:  # fell out of the ring
                    snap = svc.snapshot()
                    self._event("resync", snap, f"{snap['epoch']}:{snap['rev']}")
                    rev = snap["rev"]
                    continue
                for d in pending:
                    self._event("delta", d, f"{d['epoch']}:{d['rev']}")
                    rev = d["rev"]
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ------------------------------------------------------------------ POST

    def do_POST(self):  # noqa: N802
        if not self._host_ok() or not self._post_ok():
            return
        if urlparse(self.path).path != "/api/commands":
            return self._refuse(404, "not-found", self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return self._refuse(413, "bad-length", f"body must be 0-{MAX_BODY} bytes")
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return self._refuse(400, "bad-json", "the body is not JSON")
        try:
            cmd = Command.parse(body)
            receipt = self.service.submit(cmd)
        except CommandRefused as exc:
            return self._send(exc.http, exc.body())
        except (sqlite3.Error, OSError):
            return self._refuse(503, "office-disconnected", "runs.db is not readable right now")
        self._send(200 if receipt.get("replayed") else 202, {"ok": True, "receipt": receipt})


def make_handler(service: Service, host: str, port: int) -> type[Handler]:
    return type("OfficeHandler", (Handler,), {"service": service, "hosts": allowed_hosts(host, port)})
