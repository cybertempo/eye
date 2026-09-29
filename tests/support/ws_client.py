"""A small WebSocket test client over a raw socket.

Independent of the server's framing code and of the browser, so API tests
exercise the protocol boundary with a separate implementation.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import struct

TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class Closed(Exception):
    """The server closed the connection; ``code`` is None if it sent no close frame."""

    def __init__(self, code: int | None, reason: str = "") -> None:
        super().__init__(f"closed ({code}) {reason}")
        self.code = code
        self.reason = reason


class WsClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        origin: str | None = "auto",
        path: str = "/api/v0/stream",
        version: str = "13",
        rcvbuf: int | None = None,
        timeout: float = 10.0,
    ) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.settimeout(timeout)
        sock.connect((host, port))
        self.sock = sock
        self.buffer = b""
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            f"Sec-WebSocket-Version: {version}",
        ]
        if origin == "auto":
            origin = f"http://{host}:{port}"
        if origin is not None:
            lines.append(f"Origin: {origin}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buffer:
            chunk = sock.recv(4096)
            if not chunk:
                break
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        self.status = int(head.split(b" ", 2)[1]) if head else 0
        self.body = b""
        if self.status != 101:
            self.body = self.buffer + _drain(sock)

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise Closed(None, "connection ended without a close frame")
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def send_frame(self, opcode: int, payload: bytes, *, mask: bool = True, fin: bool = True):
        head = bytes([(0x80 if fin else 0) | opcode])
        n = len(payload)
        bit = 0x80 if mask else 0
        if n < 126:
            head += bytes([bit | n])
        elif n < 65536:
            head += bytes([bit | 126]) + struct.pack("!H", n)
        else:
            head += bytes([bit | 127]) + struct.pack("!Q", n)
        if mask:
            key = os.urandom(4)
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            head += key
        self.sock.sendall(head + payload)

    def send(self, message: dict | str) -> None:
        text = message if isinstance(message, str) else json.dumps(message)
        self.send_frame(TEXT, text.encode())

    def recv_frame(self) -> tuple[int, bytes]:
        b0, b1 = self._read(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        return b0 & 0x0F, self._read(n)

    def recv(self) -> dict:
        """The next text message as JSON; answers pings; raises Closed on close."""
        while True:
            opcode, payload = self.recv_frame()
            if opcode == PING:
                self.send_frame(PONG, payload)
            elif opcode == CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                raise Closed(code, payload[2:].decode(errors="replace"))
            elif opcode == TEXT:
                return json.loads(payload)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.send_frame(CLOSE, struct.pack("!H", 1000))
        self.sock.close()


def _drain(sock: socket.socket) -> bytes:
    data = b""
    try:
        while chunk := sock.recv(65536):
            data += chunk
    except OSError:
        pass
    return data


def subscribe(bbox, interval, layers=("flight", "vessel", "road"), resume=None) -> dict:
    return {
        "schema_version": "eye.wire/3",
        "kind": "subscribe",
        "bbox": list(bbox),
        "interval": interval,
        "layers": list(layers),
        "resume_cursor": resume,
    }
