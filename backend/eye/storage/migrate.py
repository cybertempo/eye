"""Forward-only migration runner.

Each ``migrations/NNNN_name.sql`` file runs in its own transaction together with
its version row, under an advisory lock. PostgreSQL DDL is transactional, so a
failing migration leaves schema and data exactly as they were. Applied files
are checksummed; editing one after it has run is refused.

A migration can never end the runner's transaction early. Four layers:

1. ``split_statements`` parses each file (quotes, identifiers, comments and
   dollar-quoted bodies) and refuses any top-level transaction-control
   statement before anything runs, wherever it sits on a line.
2. Each statement is sent unaltered in its own extended-protocol Parse message,
   and PostgreSQL itself refuses a Parse message containing more than one
   command, so a mis-split cannot smuggle a second statement through.
3. PostgreSQL refuses ``COMMIT`` inside ``DO`` or ``CALL`` within a transaction
   block ("invalid transaction termination").
4. The transaction id is checked after every statement; a change aborts.
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
# First keywords of statements that begin, end or split a transaction.
TRANSACTION_CONTROL = frozenset(
    {"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "PREPARE"}
)
DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
IDENT_CHAR = re.compile(r"[A-Za-z0-9_$]")

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
class Statement:
    text: str  # exactly as written, sent to the server
    first_word: str  # upper-case first keyword, comments removed


def split_statements(sql: str) -> list[Statement]:
    """Split SQL into top-level statements, skipping quoted text and comments."""
    statements: list[Statement] = []
    start = 0
    code: list[str] = []  # the current statement with comments and literals blanked
    i = 0
    n = len(sql)

    def finish(end: int) -> None:
        text = sql[start:end].strip()
        words = "".join(code).split()
        if words:
            statements.append(Statement(text, words[0].upper()))

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            i = n if end == -1 else end
            code.append(" ")
            continue
        if ch == "/" and nxt == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise MigrationError("unterminated block comment")
            code.append(" ")
            continue
        if ch == "'":
            escapes = i > 0 and sql[i - 1] in "eE" and (i < 2 or not IDENT_CHAR.match(sql[i - 2]))
            i += 1
            while True:
                if i >= n:
                    raise MigrationError("unterminated string literal")
                if escapes and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            code.append(" '' ")
            continue
        if ch == '"':
            i += 1
            while True:
                if i >= n:
                    raise MigrationError("unterminated quoted identifier")
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            code.append(' "x" ')
            continue
        if ch == "$" and (i == 0 or not IDENT_CHAR.match(sql[i - 1])):
            match = DOLLAR_TAG.match(sql, i)
            if match:
                tag = match.group(0)
                close = sql.find(tag, match.end())
                if close == -1:
                    raise MigrationError(f"unterminated dollar-quoted body {tag}")
                i = close + len(tag)
                code.append(" $body$ ")
                continue
        if ch == ";":
            finish(i)
            start, code = i + 1, []
            i += 1
            continue
        code.append(ch)
        i += 1
    finish(n)
    return statements


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sha256: str
    statements: tuple[Statement, ...]


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = FILE_PATTERN.match(path.name)
        if not match:
            raise MigrationError(f"{path.name}: expected NNNN_lower_snake_name.sql")
        raw = path.read_bytes()
        try:
            statements = split_statements(raw.decode("utf-8"))
        except MigrationError as exc:
            raise MigrationError(f"{path.name}: {exc}") from exc
        control = [s.first_word for s in statements if s.first_word in TRANSACTION_CONTROL]
        if control:
            raise MigrationError(
                f"{path.name}: must not contain transaction control statements ({control[0]})"
            )
        if not statements:
            raise MigrationError(f"{path.name}: contains no statements")
        digest = hashlib.sha256(raw).hexdigest()
        found.append(
            Migration(int(match.group(1)), match.group(2), path, digest, tuple(statements))
        )
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration versions must run 1..n without gaps, found {versions}")
    return found


def applied(conn: Connection) -> dict[int, tuple[str, str]]:
    conn.run(BOOKKEEPING)
    rows = conn.run("SELECT version, name, sha256 FROM public.eye_schema_migrations ORDER BY 1")
    return {version: (name, sha) for version, name, sha in rows}


def execute_statements(conn: Connection, statements: tuple[Statement, ...] | list) -> None:
    """Run statements one per prepared statement inside the caller's transaction."""
    xid = conn.run("SELECT txid_current()")[0][0]
    for statement in statements:
        # The extended-protocol Parse message carries the text byte for byte
        # (no client-side placeholder rewriting) and the server refuses more
        # than one command in it.
        conn.execute_unnamed(statement.text)
        if conn.run("SELECT txid_current()")[0][0] != xid:
            raise MigrationError("a statement ended the migration transaction")


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
                execute_statements(conn, migration.statements)
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
