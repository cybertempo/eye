"""Forward-only migration runner.

Each ``migrations/NNNN_name.sql`` file runs in its own transaction together with
its version row, under an advisory lock. PostgreSQL DDL is transactional, so a
failing migration leaves schema and data exactly as they were. Applied files
are checksummed; editing one after it has run is refused.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from eye.storage.db import Connection, transaction

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
FILE_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
LOCK_KEY = 0x4559455F4D494752  # "EYE_MIGR"
# The runner owns the transaction; a file must not end or split it.
TRANSACTION_CONTROL = re.compile(
    r"^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION)\s*;", re.IGNORECASE | re.MULTILINE
)

BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS public.eye_schema_migrations (
    version    integer PRIMARY KEY CHECK (version > 0),
    name       text NOT NULL,
    sha256     text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """A migration could not be applied or the history does not match the files."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sha256: str

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = FILE_PATTERN.match(path.name)
        if not match:
            raise MigrationError(f"{path.name}: expected NNNN_lower_snake_name.sql")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if TRANSACTION_CONTROL.search(path.read_text(encoding="utf-8")):
            raise MigrationError(f"{path.name}: must not contain transaction control statements")
        found.append(Migration(int(match.group(1)), match.group(2), path, digest))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration versions must run 1..n without gaps, found {versions}")
    return found


def applied(conn: Connection) -> dict[int, tuple[str, str]]:
    conn.run(BOOKKEEPING)
    rows = conn.run("SELECT version, name, sha256 FROM public.eye_schema_migrations ORDER BY 1")
    return {version: (name, sha) for version, name, sha in rows}


def migrate(conn: Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied now."""
    migrations = discover(directory)
    done: list[int] = []
    for migration in migrations:
        with transaction(conn):
            conn.run("SELECT pg_advisory_xact_lock(:key)", key=LOCK_KEY)
            history = applied(conn)
            unknown = set(history) - {m.version for m in migrations}
            if unknown:
                raise MigrationError(f"database has migrations this code lacks: {sorted(unknown)}")
            if migration.version in history:
                name, digest = history[migration.version]
                if digest != migration.sha256 or name != migration.name:
                    raise MigrationError(
                        f"migration {migration.version:04d} was changed after it was applied"
                    )
                continue
            try:
                conn.execute_simple(migration.sql)
            except Exception as exc:
                raise MigrationError(
                    f"migration {migration.version:04d}_{migration.name} failed and was "
                    f"rolled back: {exc}"
                ) from exc
            conn.run(
                "INSERT INTO public.eye_schema_migrations (version, name, sha256) "
                "VALUES (:v, :n, :s)",
                v=migration.version,
                n=migration.name,
                s=migration.sha256,
            )
        done.append(migration.version)
    return done


def status(conn: Connection, directory: Path = MIGRATIONS_DIR) -> dict:
    migrations = discover(directory)
    history = applied(conn)
    return {
        "applied": sorted(history),
        "pending": [m.version for m in migrations if m.version not in history],
    }
