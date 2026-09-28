"""Loopback HTTP server for the synthetic demo.

Standard library only. It serves one synthetic snapshot and the static demo
page, checks the Host header against loopback names, sets restrictive browser
headers and bounds concurrent connections, request time and response size.
"""

from __future__ import annotations

import json
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eye import __version__
from eye.api.auth import AuthPort
from eye.config import EyeConfig, is_loopback_host

API_PREFIX = "/api/v0"
REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = REPO_ROOT / "web"
STATIC_FILES = {
    "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
    "/static/demo.css": (WEB_ROOT / "demo.css", "text/css; charset=utf-8"),
    "/static/demo.js": (WEB_ROOT / "dist" / "demo.js", "text/javascript; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class StartupError(RuntimeError):
    """The server cannot start safely."""


def load_snapshot(config: EyeConfig) -> bytes:
    try:
        document = json.loads(config.fixture.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StartupError(f"cannot read synthetic fixture {config.fixture}: {exc}") from exc
    if document.get("synthetic") is not True:
        raise StartupError('the demo serves only fixtures marked "synthetic": true')
    body = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > config.server.max_response_bytes:
        raise StartupError("synthetic snapshot exceeds server.max_response_bytes")
    return body


def _host_name(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header[1 : host_header.find("]")]
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True  # permits restart over TIME_WAIT; not a shared bind

    def __init__(self, config: EyeConfig, auth: AuthPort, snapshot: bytes) -> None:
        self.config = config
        self.auth = auth
        self.snapshot = snapshot
        self._slots = threading.BoundedSemaphore(config.server.max_connections)
        if ":" in config.server.bind_host:
            self.address_family = socket.AF_INET6
        super().__init__((config.server.bind_host, config.server.port), DemoHandler)

    def process_request(self, request, client_address):  # noqa: ANN001
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):  # noqa: ANN001
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer
    server_version = f"eye-demo/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        self.timeout = self.server.config.server.request_timeout_seconds
        super().setup()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Request line and status only; never headers or client address.
        print(f"eye-demo: {format % args}", flush=True)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        if len(body) > self.server.config.server.max_response_bytes:
            status = HTTPStatus.SERVICE_UNAVAILABLE
            body = b'{"error":"response exceeds configured limit","status":503}'
            content_type = "application/json"
        self.send_response(status)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json")

    def _error(self, status: HTTPStatus, reason: str) -> None:
        self._json(status, {"error": reason, "status": status.value})

    def do_GET(self) -> None:
        if not is_loopback_host(_host_name(self.headers.get("Host", ""))):
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "host not allowed")
            return
        path = self.path.split("?", 1)[0]
        if path == f"{API_PREFIX}/health":
            self._json(
                HTTPStatus.OK,
                {"status": "ok", "mode": self.server.config.mode, "synthetic": True},
            )
            return
        if path == f"{API_PREFIX}/snapshot":
            principal = self.server.auth.authenticate(dict(self.headers.items()))
            if principal is None:
                self._error(HTTPStatus.FORBIDDEN, "not authorised")
                return
            self._send(HTTPStatus.OK, self.server.snapshot, "application/json")
            return
        static = STATIC_FILES.get(path)
        if static is not None and static[0].is_file():
            if static[0].stat().st_size > self.server.config.server.max_response_bytes:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "response exceeds configured limit")
                return
            self._send(HTTPStatus.OK, static[0].read_bytes(), static[1])
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_HEAD(self) -> None:
        self.do_GET()

    def _method_not_allowed(self) -> None:
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _method_not_allowed


def build_server(config: EyeConfig, auth: AuthPort) -> DemoServer:
    if config.mode != "demo":
        raise StartupError("the synthetic demo server runs in demo mode only")
    return DemoServer(config, auth, load_snapshot(config))
