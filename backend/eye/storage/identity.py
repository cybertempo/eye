"""Which kind of EYE database a connection reaches: the synthetic demo or private.

The configured mode and a loopback connection do not prove which database is
on the other end: a private installation can listen on the same host. So the
database records its own kind once (migration 0009), and the demo refuses a
database that is claimed ``private`` or holds evidence of a non-synthetic
source, before it serves or writes anything. Production refuses a database that
is not claimed ``private``. Every check here only reads, and it works on a
database that has not been migrated yet (so a refusal comes before migrating).
"""

from __future__ import annotations

from eye.storage.db import Connection, transaction

DEMO = "synthetic-demo"
PRIVATE = "private"
SYNTHETIC_PREFIX = "synthetic-"  # every synthetic source id; a real source must not use it


class IdentityRefused(Exception):
    """The database is not the kind this mode may use."""


def _exists(conn: Connection, table: str) -> bool:
    return conn.run("SELECT to_regclass(:t) IS NOT NULL", t=table)[0][0]


def kind(conn: Connection) -> str | None:
    """The claimed kind, or None when the database has not claimed one."""
    if not _exists(conn, "eye.database_identity"):
        return None
    rows = conn.run("SELECT kind FROM eye.database_identity")
    return rows[0][0] if rows else None


def real_sources(conn: Connection) -> list[str]:
    """Up to five non-synthetic sources with capture batches here."""
    if not _exists(conn, "eye.capture_batch"):
        return []
    rows = conn.run(
        "SELECT DISTINCT source_id FROM eye.capture_batch "
        "WHERE left(source_id, :n) <> :p ORDER BY 1 LIMIT 5",
        n=len(SYNTHETIC_PREFIX),
        p=SYNTHETIC_PREFIX,
    )
    return [r[0] for r in rows]


def check_demo_target(conn: Connection) -> None:
    """Refuse, before any write, a database the demo must not use."""
    claimed = kind(conn)
    if claimed == PRIVATE:
        raise IdentityRefused(
            "this database is claimed by a private installation; demo mode refuses it"
        )
    found = real_sources(conn)
    if found:
        raise IdentityRefused(
            f"this database holds captures of non-synthetic sources ({', '.join(found)}); "
            "demo mode refuses it"
        )


def check_private_target(conn: Connection) -> None:
    """Refuse a database that is not claimed by a private installation."""
    claimed = kind(conn)
    if claimed != PRIVATE:
        what = "the synthetic demo" if claimed == DEMO else "no installation"
        raise IdentityRefused(
            f"this database is claimed by {what}; production mode needs a database "
            "claimed by db-migrate in production mode"
        )


def claim(conn: Connection, wanted: str) -> str:
    """Claim the database for ``wanted`` once (after migrating); refuse the other kind."""
    if wanted not in (DEMO, PRIVATE):
        raise ValueError(f"unknown database kind {wanted!r}")
    with transaction(conn):
        conn.run("LOCK TABLE eye.database_identity, eye.capture_batch IN SHARE ROW EXCLUSIVE MODE")
        if wanted == DEMO:
            check_demo_target(conn)
        conn.run(
            "INSERT INTO eye.database_identity (kind) VALUES (:k) ON CONFLICT DO NOTHING",
            k=wanted,
        )
        claimed = kind(conn)
    if claimed != wanted:
        raise IdentityRefused(f"this database is already claimed as {claimed}; not {wanted}")
    return claimed
