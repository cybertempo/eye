"""Database connections (pg8000, pure Python).

Connection details come from a URL held in an environment variable named by
configuration; nothing here hardcodes a host, port, user or password.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from urllib.parse import unquote, urlsplit

import pg8000.native

from eye.config import is_loopback_host

Connection = pg8000.native.Connection


class DatabaseConfigError(ValueError):
    """The database URL is missing, malformed or not allowed in this mode."""


def parse_url(url: str) -> dict:
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres"):
        raise DatabaseConfigError("database URL must start with postgresql://")
    if not parts.hostname or not parts.path.strip("/"):
        raise DatabaseConfigError("database URL needs a host and a database name")
    if parts.query or parts.fragment:
        raise DatabaseConfigError("database URL options are not supported")
    return {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password) if parts.password is not None else None,
        "database": unquote(parts.path.strip("/")),
    }


def connect(url: str, *, require_loopback: bool = False, timeout: float = 10.0) -> Connection:
    params = parse_url(url)
    if not params["user"]:
        raise DatabaseConfigError("database URL needs a user")
    if require_loopback and not is_loopback_host(params["host"]):
        raise DatabaseConfigError("demo mode connects only to a loopback database")
    conn = Connection(
        params["user"],
        host=params["host"],
        port=params["port"],
        database=params["database"],
        password=params["password"],
        timeout=timeout,
        application_name="eye",
    )
    conn.run("SET TIME ZONE 'UTC'")
    conn.run("SET statement_timeout = '30s'")
    return conn


@contextlib.contextmanager
def transaction(conn: Connection) -> Iterator[Connection]:
    """Run a block in one transaction; roll back on any exception."""
    conn.run("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.run("ROLLBACK")
        raise
    conn.run("COMMIT")
