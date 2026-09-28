"""Loopback HTTP and WebSocket server for THEATRE and DESK (standard library + pg8000).

Serves bounded, database-backed ``eye.wire/1`` messages:

- ``GET /api/v0/health``     process health (no database access)
- ``GET /api/v0/snapshot``   tracks and coverage for an area, interval and layer set
- ``GET /api/v0/transits``   observed transit counts for one versioned count line
- ``GET /api/v0/stream``     WebSocket: subscribe, snapshot, sequenced deltas

It checks the Host header against loopback names and the WebSocket Origin
against the request's own origin (or ``api.allowed_origin``), authenticates
every data request through the configured ``AuthPort``, sets restrictive
browser headers, and bounds connections, sockets, request time, query time,
response size and per-client buffers. Every JSON body is validated against the
wire schema before it is sent. The API's database sessions are read-only.
"""

from __future__ import annotations

import contextlib
import select
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from eye import __version__
from eye.api import feed, stream
from eye.api import websocket as ws
from eye.api.auth import AuthPort
from eye.config import EyeConfig, is_loopback_host
from eye.storage.db import DatabaseConfigError
from eye.wire import SCHEMA_VERSION, WireValidationError, validate_message

API_PREFIX = "/api/v0"
REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = REPO_ROOT / "web"
JS = "text/javascript; charset=utf-8"
# Browser modules built by `npm run --prefix web build`. A fixed allowlist;
# request paths never map onto the filesystem directly.
MODULES = (
    "app",
    "desk",
    "facts",
    "globe",
    "live",
    "wire-validate",
    "generated/wire-schema",
    "generated/wire-types",
)
STATIC_FILES = {
    "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
    "/static/app.css": (WEB_ROOT / "app.css", "text/css; charset=utf-8"),
    **{f"/static/{m}.js": (WEB_ROOT / "dist" / f"{m}.js", JS) for m in MODULES},
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


def _host_name(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header[1 : host_header.find("]")]
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def limits_for(config: EyeConfig) -> feed.Limits:
    api = config.api
    return feed.Limits(
        max_interval_hours=api.max_interval_hours,
        max_tracks=api.max_tracks,
        max_points=api.max_points,
        max_coverage=api.max_coverage,
        max_counts=api.max_counts,
        max_crossings=api.max_crossings,
        max_changes=api.max_pending_changes,
        query_timeout_ms=api.query_timeout_ms,
        default_view_hours=api.default_view_hours,
    )


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True  # permits restart over TIME_WAIT; not a shared bind

    def __init__(self, config: EyeConfig, auth: AuthPort, database_url: str) -> None:
        self.config = config
        self.auth = auth
        self.limits = limits_for(config)
        self.lines = feed.load_lines(config.count_lines)
        demo = config.mode == "demo"

        def reader():
            return feed.open_reader(
                database_url, require_loopback=demo, query_timeout_ms=config.api.query_timeout_ms
            )

        self.pool = stream.Pool(reader, config.api.db_pool_size, config.api.query_timeout_ms / 1000)
        self.hub = stream.Hub(
            connect=reader,
            limits=self.limits,
            max_pending=config.api.max_pending_changes,
            poll_seconds=config.api.poll_interval_ms / 1000,
            auth=auth,
        )
        self._slots = threading.BoundedSemaphore(config.server.max_connections)
        self._sockets = threading.BoundedSemaphore(config.api.max_websockets)
        if ":" in config.server.bind_host:
            self.address_family = socket.AF_INET6
        super().__init__((config.server.bind_host, config.server.port), ApiHandler)

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

    def server_close(self) -> None:
        self.hub.stop()
        super().server_close()
        self.pool.close()


class ApiHandler(BaseHTTPRequestHandler):
    server: ApiServer
    server_version = f"eye/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    rbufsize = 0  # unbuffered, so no WebSocket bytes are read ahead into the HTTP parser

    def setup(self) -> None:
        self.timeout = self.server.config.server.request_timeout_seconds
        super().setup()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Request line and status only; never headers, query values or client address.
        print(f"eye: {format % args}".split("?", 1)[0], flush=True)

    # -- responses ----------------------------------------------------------------------

    def _send(self, status: HTTPStatus | int, body: bytes, content_type: str) -> None:
        if len(body) > self.server.config.server.max_response_bytes:
            status = HTTPStatus.SERVICE_UNAVAILABLE
            body = feed.dumps(stream.error_message(503, "response exceeds configured limit"))
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

    def _message(self, status: HTTPStatus | int, message: dict) -> None:
        # Outgoing runtime validation: a schema violation is a server bug, so
        # the client receives a valid error message instead of the bad body.
        try:
            validate_message(message, "ServerMessage")
        except WireValidationError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            message = stream.error_message(500, "internal error: invalid outgoing message")
        self._send(status, feed.dumps(message), "application/json")

    def _error(self, status: HTTPStatus | int, reason: str) -> None:
        self._message(status, stream.error_message(int(status), reason))

    def _authorised(self) -> bool:
        try:
            principal = self.server.auth.authenticate(dict(self.headers.items()))
        except Exception:  # noqa: BLE001 - a failing adapter denies
            principal = None
        if principal is None:
            self._error(HTTPStatus.FORBIDDEN, "not authorised")
            return False
        return True

    # -- routing ------------------------------------------------------------------------

    def do_GET(self) -> None:
        if not is_loopback_host(_host_name(self.headers.get("Host", ""))):
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "host not allowed")
            return
        parts = urlsplit(self.path)
        path = parts.path
        if path == f"{API_PREFIX}/health":
            self._message(
                HTTPStatus.OK,
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "health",
                    "status": "ok",
                    "mode": self.server.config.mode,
                    "synthetic": self.server.config.mode == "demo",
                },
            )
            return
        if path in (f"{API_PREFIX}/snapshot", f"{API_PREFIX}/transits", f"{API_PREFIX}/stream"):
            if len(parts.query) > 1024:
                self._error(HTTPStatus.REQUEST_URI_TOO_LONG, "query string too long")
                return
            if not self._authorised():
                return
            try:
                params = parse_qs(parts.query, keep_blank_values=True, max_num_fields=8)
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "too many query parameters")
                return
            if path.endswith("/stream"):
                self._websocket()
            elif path.endswith("/snapshot"):
                self._database(lambda conn: self._snapshot(conn, params))
            else:
                self._database(lambda conn: self._transits(conn, params))
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

    # -- REST ---------------------------------------------------------------------------

    def _database(self, action) -> None:
        try:
            with self.server.pool.connection() as conn:
                status, message = action(conn)
        except feed.QueryRefused as exc:
            self._error(exc.status, str(exc))
            return
        except stream.PoolExhausted:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "database busy; try again")
            return
        except Exception:  # noqa: BLE001 - unreachable or failing database: unknown, not empty
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "database unavailable; data unknown")
            return
        self._message(status, message)

    def _default(self, conn) -> feed.Query:
        view = self.server.config.view
        return feed.default_query(conn, view.bbox, view.layers, self.server.limits)

    def _snapshot(self, conn, params) -> tuple[int, dict]:
        if "line" in params:
            raise feed.QueryRefused(400, "unknown query parameter 'line'")
        query = feed.query_from_params(params, self._default(conn), self.server.limits)
        area = self.server.config.view.area_name
        if query.bbox != self.server.config.view.bbox:
            area = "Requested area"
        return 200, feed.snapshot(conn, query, self.server.limits, area)

    def _transits(self, conn, params) -> tuple[int, dict]:
        for name in ("bbox", "layers"):
            if name in params:
                raise feed.QueryRefused(400, f"unknown query parameter {name!r}")
        lines = self.server.lines
        if not lines:
            raise feed.QueryRefused(404, "no count line is configured")
        line_id = params.get("line", [sorted(lines)[0]])
        if len(line_id) != 1 or line_id[0] not in lines:
            raise feed.QueryRefused(404, "unknown count line")
        rest = {k: v for k, v in params.items() if k != "line"}
        query = feed.query_from_params(rest, self._default(conn), self.server.limits)
        message = feed.transit_counts(
            conn, lines[line_id[0]], query.start, query.end, self.server.limits
        )
        return 200, message

    # -- WebSocket ----------------------------------------------------------------------

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return False
        allowed = self.server.config.api.allowed_origin
        if allowed:
            return origin == allowed
        return origin == f"http://{self.headers.get('Host', '')}"

    def _websocket(self) -> None:
        h = self.headers
        if (
            h.get("Upgrade", "").lower() != "websocket"
            or "upgrade" not in h.get("Connection", "").lower()
        ):
            self._error(HTTPStatus.BAD_REQUEST, "WebSocket upgrade required")
            return
        if h.get("Sec-WebSocket-Version") != "13":
            self._error(HTTPStatus.UPGRADE_REQUIRED, "WebSocket version 13 required")
            return
        accept = ws.accept_key(h.get("Sec-WebSocket-Key", ""))
        if accept is None:
            self._error(HTTPStatus.BAD_REQUEST, "invalid WebSocket key")
            return
        if not self._origin_allowed():
            self._error(HTTPStatus.FORBIDDEN, "origin not allowed")
            return
        if not self.server._sockets.acquire(blocking=False):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "too many live connections")
            return
        try:
            self._serve_socket(accept)
        finally:
            self.server._sockets.release()

    def _serve_socket(self, accept: str) -> None:
        api = self.server.config.api
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock: socket.socket = self.connection
        sock.settimeout(api.ws_send_timeout_seconds)
        sub = stream.Subscriber(
            sock,
            cap=api.ws_max_buffer_bytes,
            headers=dict(self.headers.items()),
            max_message=self.server.config.server.max_response_bytes,
        )
        self.server.hub.add(sub)
        try:
            self._read_loop(sub, _Reader(sock))
        finally:
            sub.close(ws.CLOSE_NORMAL, "closing")
            sub.join(api.ws_send_timeout_seconds + 1)
            sub.abort(sub.close_reason or "closed")
            self.server.hub.remove(sub)

    def _read_loop(self, sub: stream.Subscriber, reader: _Reader) -> None:
        api = self.server.config.api
        last_rx = last_ping = time.monotonic()
        while not sub.closed:
            if not reader.buffered:
                ready, _, _ = select.select([sub.sock], [], [], 1.0)
                now = time.monotonic()
                if not ready:
                    if now - last_rx > api.ws_idle_timeout_seconds:
                        sub.close(ws.CLOSE_GOING_AWAY, "idle timeout")
                        return
                    if now - last_ping > api.ws_ping_seconds:
                        sub.send_frame(ws.encode(ws.PING, b"eye"))
                        last_ping = now
                    continue
            try:
                opcode, payload = ws.read_frame(reader, api.ws_max_inbound_bytes)
            except ws.ProtocolError as exc:
                sub.close(exc.code, str(exc))
                return
            except (EOFError, OSError, TimeoutError):
                return
            last_rx = time.monotonic()
            if opcode == ws.CLOSE:
                sub.close(ws.CLOSE_NORMAL, "client closed")
                return
            if opcode == ws.PING:
                sub.send_frame(ws.encode(ws.PONG, payload))
                continue
            if opcode == ws.PONG:
                continue
            if opcode == ws.BINARY:
                sub.send(stream.error_message(400, "binary messages are not accepted"))
                sub.close(ws.CLOSE_UNSUPPORTED, "binary messages are not accepted")
                return
            if not self._client_message(sub, payload):
                return

    def _client_message(self, sub: stream.Subscriber, payload: bytes) -> bool:
        """Handle one text message; False closes the connection."""
        try:
            message = validate_message(
                payload, "ClientMessage", max_bytes=self.server.config.api.ws_max_inbound_bytes
            )
        except WireValidationError as exc:
            sub.send(stream.error_message(400, f"invalid message: {exc.errors[0]}"[:500]))
            sub.close(ws.CLOSE_POLICY, "invalid message")
            return False
        if message["kind"] == "unsubscribe":
            with sub.lock:
                sub.query = None
            return True
        with sub.lock:
            sub.query = None  # a new request replaces the old subscription, even if it fails
        west, south, east, north = message["bbox"]
        try:
            query = feed.check_query(
                feed.Query(
                    (west, south, east, north),
                    feed.parse_time(message["interval"]["start"], "interval.start"),
                    feed.parse_time(message["interval"]["end"], "interval.end"),
                    tuple(dict.fromkeys(message["layers"])),
                ),
                self.server.limits,
            )
        except feed.QueryRefused as exc:
            sub.send(stream.error_message(exc.status, str(exc)))
            return True
        area = self.server.config.view.area_name
        if query.bbox != self.server.config.view.bbox:
            area = "Requested area"
        try:
            with self.server.pool.connection() as conn, sub.lock:
                resume = message["resume_cursor"]
                if resume is not None:
                    epoch, seq = feed.head(conn)
                    parsed = feed.parse_cursor(resume)
                    valid = parsed is not None and parsed[0] == epoch and parsed[1] <= seq
                    reason = "reconnect" if valid else "expired_cursor"
                    sub.send(stream.resync_message(reason, resume))
                stream.send_snapshot(conn, sub, query, self.server.limits, area)
        except stream.PoolExhausted:
            sub.send(stream.error_message(503, "database busy; try again"))
        except Exception:  # noqa: BLE001 - unknown, not empty
            sub.send(stream.error_message(503, "database unavailable; data unknown"))
        return True


class _Reader:
    """Buffered reads straight from the socket; a timeout mid-frame is fatal."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buffer = bytearray()

    @property
    def buffered(self) -> bool:
        return bool(self._buffer)

    def read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buffer)))
            if not chunk:
                raise EOFError("connection closed")
            self._buffer += chunk
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out


def check_database(config: EyeConfig, database_url: str) -> None:
    """Refuse to start unless the database is reachable and migrated for the API."""
    try:
        conn = feed.open_reader(
            database_url,
            require_loopback=config.mode == "demo",
            query_timeout_ms=config.api.query_timeout_ms,
        )
    except DatabaseConfigError as exc:
        raise StartupError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise StartupError(f"database unreachable ({type(exc).__name__})") from exc
    try:
        conn.run("SELECT 1 FROM eye.feed_epoch")
    except Exception as exc:  # noqa: BLE001
        raise StartupError("database is not migrated; run db-migrate first") from exc
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def build_server(config: EyeConfig, auth: AuthPort, database_url: str) -> ApiServer:
    check_database(config, database_url)
    server = ApiServer(config, auth, database_url)
    server.hub.start()
    return server
