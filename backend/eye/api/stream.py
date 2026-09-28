"""WebSocket subscriptions: snapshot, sequenced deltas and bounded delivery.

A client subscribes to an area, interval and layer set and receives one
snapshot carrying a cursor, then deltas in sequence. Each delta names the
cursor it follows (``previous_cursor``); a client that sees a different one has
missed an update and must subscribe again. A reconnecting client sends its last
cursor and is told whether it is still valid (``reconnect``) or not
(``expired_cursor``); either way it receives a fresh snapshot, so its state is
rebuilt from the database rather than patched across the gap.

Delivery is bounded. Every connection has a queue with a hard byte cap
(``api.ws_max_buffer_bytes``) that includes the frame being written, and the
kernel send buffer is set small. A client that cannot keep up is disconnected;
nothing is buffered without limit. A subscriber more than
``api.max_pending_changes`` behind gets a fresh snapshot instead of a long run
of deltas.
"""

from __future__ import annotations

import contextlib
import queue
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator

from eye.api import feed
from eye.api import websocket as ws
from eye.wire import SCHEMA_VERSION, WireValidationError, validate_message

KERNEL_SEND_BUFFER = 32_768  # bytes; the kernel buffer is part of the documented bound
AUTH_RECHECK_SECONDS = 30  # open sockets re-authenticate well within the 60 s rule


class PoolExhausted(RuntimeError):
    """Every database connection is busy; the request is refused, not queued forever."""


class Pool:
    """A small, bounded pool of read-only connections."""

    def __init__(self, factory: Callable[[], object], size: int, wait_seconds: float) -> None:
        self._factory = factory
        self._slots = threading.BoundedSemaphore(size)
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._wait = wait_seconds

    @contextlib.contextmanager
    def connection(self) -> Iterator[object]:
        if not self._slots.acquire(timeout=self._wait):
            raise PoolExhausted("database connections are all busy")
        conn = None
        try:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = self._factory()
            yield conn
        except feed.QueryRefused:
            raise
        except BaseException:
            # A failed connection or query may leave the session unusable.
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
                conn = None
            raise
        finally:
            if conn is not None:
                self._idle.put(conn)
            self._slots.release()

    def close(self) -> None:
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                return
            with contextlib.suppress(Exception):
                conn.close()


def encode_valid(message: dict) -> bytes | None:
    """The encoded message, or None if it breaks the wire schema."""
    try:
        validate_message(message, "ServerMessage")
    except WireValidationError:
        return None
    return feed.dumps(message)


def encode_message(message: dict) -> bytes:
    """Validate an outgoing message and encode it; a violation is a server bug."""
    try:
        validate_message(message, "ServerMessage")
    except WireValidationError:
        message = error_message(500, "internal error: invalid outgoing message")
    return feed.dumps(message)


def error_message(status: int, reason: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "kind": "error", "status": status, "error": reason}


def resync_message(reason: str, last_cursor: str | None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "resync_required",
        "reason": reason,
        "last_cursor": last_cursor,
    }


class Subscriber:
    """One WebSocket connection: a bounded outbound queue and its subscription."""

    def __init__(
        self, sock: socket.socket, *, cap: int, headers: dict[str, str], max_message: int
    ) -> None:
        self.sock = sock
        self.cap = cap
        self.headers = headers
        self.max_message = max_message
        self.lock = threading.RLock()  # held while the subscription or its deltas change
        self.query: feed.Query | None = None  # set only once a snapshot baseline is queued
        self.area_name = ""
        self.stale = False  # told the change feed failed; owed a fresh snapshot
        self.epoch: str | None = None
        self.seq = 0
        self.sequence = 0
        self.closed = False
        self.close_reason: str | None = None
        self._queue: deque[bytes] = deque()
        self._queued = 0
        self._cv = threading.Condition()
        self._closing = False
        self._writer = threading.Thread(target=self._write_loop, name="eye-ws-writer", daemon=True)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, KERNEL_SEND_BUFFER)
        self._writer.start()

    @property
    def queued_bytes(self) -> int:
        with self._cv:
            return self._queued

    def send_frame(self, frame: bytes) -> bool:
        with self._cv:
            if self.closed or self._closing:
                return False
            if self._queued + len(frame) > self.cap:
                self._abort_locked("slow client: outbound buffer cap exceeded")
                return False
            self._queue.append(frame)
            self._queued += len(frame)
            self._cv.notify()
            return True

    def send(self, message: dict) -> bool:
        body = encode_message(message)
        if len(body) > self.max_message:
            body = encode_message(
                error_message(413, "message exceeds the configured limit; narrow the subscription")
            )
        return self.send_frame(ws.encode(ws.TEXT, body))

    def close(self, code: int, reason: str) -> None:
        """Send a close frame after anything already queued, then shut down."""
        with self._cv:
            if self.closed or self._closing:
                return
            self.close_reason = reason
            frame = ws.close_frame(code, reason)
            self._queue.append(frame)
            self._queued += len(frame)
            self._closing = True
            self._cv.notify()

    def abort(self, reason: str) -> None:
        with self._cv:
            self._abort_locked(reason)

    def _abort_locked(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = self.close_reason or reason
        self._queue.clear()
        self._queued = 0
        self._cv.notify_all()
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)

    def _write_loop(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self.closed:
                    if self._closing:
                        self._abort_locked(self.close_reason or "closed")
                        return
                    self._cv.wait()
                if self.closed:
                    return
                frame = self._queue[0]
            try:
                self.sock.sendall(frame)  # socket timeout = api.ws_send_timeout_seconds
            except (OSError, TimeoutError):
                self.abort("slow client: send timed out")
                return
            with self._cv:
                if self._queue and self._queue[0] is frame:
                    self._queue.popleft()
                    self._queued -= len(frame)

    def join(self, timeout: float) -> None:
        self._writer.join(timeout)


class Hub:
    """Polls the change log and sends each subscriber its deltas, in order."""

    def __init__(
        self,
        *,
        connect: Callable[[], object],
        limits: feed.Limits,
        max_pending: int,
        poll_seconds: float,
        auth,
    ) -> None:
        self._connect = connect
        self.limits = limits
        self.max_pending = max_pending
        self.poll_seconds = poll_seconds
        self.auth = auth
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._conn = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_auth_check = time.monotonic()
        self.unavailable = False  # the last poll could not read the database
        self.closed_reasons: deque[str] = deque(maxlen=100)  # recent disconnects, for operators

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="eye-feed-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        for sub in self.subscribers():
            sub.close(ws.CLOSE_GOING_AWAY, "server stopping")
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()

    def _run(self) -> None:
        # Wake often enough for both duties: polling for changes, and
        # re-authorising open sockets every AUTH_RECHECK_SECONDS whatever the
        # poll interval is.
        next_poll = time.monotonic() + self.poll_seconds
        while not self._stop.wait(min(self.poll_seconds, 1.0)):
            self._recheck_auth()
            if time.monotonic() >= next_poll:
                self.tick()
                next_poll = time.monotonic() + self.poll_seconds

    def add(self, sub: Subscriber) -> None:
        with self._lock:
            self._subscribers.add(sub)

    def remove(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subscribers and sub.close_reason:
                self.closed_reasons.append(sub.close_reason)
            self._subscribers.discard(sub)

    def subscribers(self) -> list[Subscriber]:
        with self._lock:
            return list(self._subscribers)

    # -- one poll -----------------------------------------------------------------------

    def tick(self) -> None:
        """Deliver pending changes to every subscriber. Safe to call from tests.

        If the change feed cannot be read, every live subscriber is told once
        that its data may be stale; when the feed is readable again each of
        them gets a fresh snapshot before any further delta.
        """
        with self._tick_lock:
            try:
                if self._conn is None:
                    self._conn = self._connect()
                conn = self._conn
                epoch, head_seq = feed.head(conn)
                for sub in self.subscribers():
                    if sub.closed:
                        self.remove(sub)
                        continue
                    self._deliver(conn, sub, epoch, head_seq)
                self.unavailable = False
            except Exception:  # noqa: BLE001 - reported to clients as stale, then retried
                self.unavailable = True
                if self._conn is not None:
                    with contextlib.suppress(Exception):
                        self._conn.close()
                self._conn = None
                self._mark_stale()

    def _mark_stale(self) -> None:
        for sub in self.subscribers():
            with sub.lock:
                if sub.query is None or sub.stale or sub.closed:
                    continue
                sub.stale = True
                sub.send(
                    error_message(
                        503,
                        "live updates unavailable: the change feed cannot be read; "
                        "data shown may be stale",
                    )
                )

    def _recheck_auth(self) -> None:
        now = time.monotonic()
        if now - self._last_auth_check < AUTH_RECHECK_SECONDS:
            return
        self._last_auth_check = now
        self.recheck_auth()

    def recheck_auth(self) -> None:
        for sub in self.subscribers():
            try:
                principal = self.auth.authenticate(dict(sub.headers))
            except Exception:  # noqa: BLE001 - a failing adapter denies
                principal = None
            if principal is None:
                sub.send(error_message(403, "not authorised"))
                sub.close(ws.CLOSE_POLICY, "authorisation ended")

    def _deliver(self, conn, sub: Subscriber, epoch: str, head_seq: int) -> None:
        with sub.lock:
            if sub.query is None or sub.closed:
                return
            if sub.stale:
                self.resnapshot(conn, sub, "gap")  # updates may have been missed
                return
            if sub.seq >= head_seq:
                return
            if sub.epoch != epoch:
                self.resnapshot(conn, sub, "expired_cursor")
                return
            rows = feed.changes_after(conn, sub.seq, self.max_pending + 1)
            if len(rows) > self.max_pending:
                self.resnapshot(conn, sub, "gap")
                return
            for row in rows:
                # The cursor advances only after a complete, valid delta is queued.
                try:
                    message = feed.delta(
                        conn, sub.query, self.limits, epoch, row, sub.seq, sub.sequence + 1
                    )
                except feed.QueryRefused:
                    self.resnapshot(conn, sub, "overflow")
                    return
                body = encode_valid(message)
                if body is None or len(body) > sub.max_message:
                    self.resnapshot(conn, sub, "overflow")
                    return
                if not sub.send_frame(ws.encode(ws.TEXT, body)):
                    return
                sub.seq = row[0]
                sub.sequence += 1

    def resnapshot(self, conn, sub: Subscriber, reason: str) -> None:
        """Tell the client its state is stale, then send a fresh snapshot."""
        last = feed.make_cursor(sub.epoch, sub.seq) if sub.epoch else None
        sub.send(resync_message(reason, last))
        query = sub.query
        try:
            send_snapshot(conn, sub, query, self.limits, sub.area_name)
        except Exception:
            # The database failed mid-snapshot: keep the subscription so this
            # poll's failure marks it stale and a later poll retries the snapshot.
            sub.query = query
            raise


def send_snapshot(conn, sub: Subscriber, query: feed.Query, limits: feed.Limits, area: str) -> bool:
    """Send a snapshot and start the subscriber's delta sequence at its cursor.

    The subscription becomes active only once its snapshot (the client's
    baseline) is known to fit and has been queued. A snapshot that cannot be
    built or sent leaves no subscription: no delta will follow an error.
    """
    sub.query = None
    try:
        message = feed.snapshot(conn, query, limits, area)
    except feed.QueryRefused as exc:
        sub.send(error_message(exc.status, f"{exc}; no subscription is active"))
        return False
    body = encode_valid(message)
    if body is None or len(body) > sub.max_message:
        sub.send(
            error_message(413, "snapshot exceeds the configured limit; narrow the subscription")
        )
        return False
    if not sub.send_frame(ws.encode(ws.TEXT, body)):
        return False
    epoch, seq = feed.parse_cursor(message["cursor"])  # type: ignore[misc]
    sub.query, sub.epoch, sub.seq, sub.sequence = query, epoch, seq, 0
    sub.area_name, sub.stale = area, False
    return True
