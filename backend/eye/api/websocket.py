"""Minimal RFC 6455 WebSocket framing for the API (standard library only).

Text, ping, pong and close frames only. Client frames must be masked and
unfragmented and are bounded in size; anything else is a protocol error that
closes the connection. Server frames are never masked.
"""

from __future__ import annotations

import base64
import hashlib
import struct

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_TRY_LATER = 1013


class ProtocolError(Exception):
    """The peer broke the protocol or a bound; close with ``code``."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def accept_key(key: str) -> str | None:
    """Sec-WebSocket-Accept for a client key, or None if the key is malformed."""
    try:
        raw = base64.b64decode(key.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    if len(raw) != 16:
        return None
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode("ascii")  # noqa: S324 - fixed by RFC 6455


def encode(opcode: int, payload: bytes = b"") -> bytes:
    """One unfragmented, unmasked server frame."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


def close_frame(code: int, reason: str = "") -> bytes:
    return encode(CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])


def _exact(stream, n: int) -> bytes:
    data = stream.read(n)
    if data is None or len(data) != n:
        raise EOFError("connection closed")
    return data


def read_frame(stream, max_payload: int) -> tuple[int, bytes]:
    """Read one client frame from a buffered binary stream: (opcode, payload)."""
    b0, b1 = _exact(stream, 2)
    fin, rsv, opcode = b0 & 0x80, b0 & 0x70, b0 & 0x0F
    if rsv:
        raise ProtocolError(CLOSE_PROTOCOL, "reserved bits set")
    if not b1 & 0x80:
        raise ProtocolError(CLOSE_PROTOCOL, "client frames must be masked")
    if not fin or opcode == 0x0:
        raise ProtocolError(CLOSE_UNSUPPORTED, "fragmented messages are not accepted")
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _exact(stream, 8))[0]
    if opcode >= 0x8 and length > 125:
        raise ProtocolError(CLOSE_PROTOCOL, "control frame too long")
    if length > max_payload:
        raise ProtocolError(CLOSE_TOO_BIG, f"message larger than {max_payload} bytes")
    mask = _exact(stream, 4)
    data = bytearray(_exact(stream, length))
    for i in range(length):
        data[i] ^= mask[i % 4]
    if opcode not in (TEXT, BINARY, CLOSE, PING, PONG):
        raise ProtocolError(CLOSE_PROTOCOL, "unknown opcode")
    return opcode, bytes(data)
