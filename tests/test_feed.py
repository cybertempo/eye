"""Migration 0003: the feed change log behind API cursors."""

from __future__ import annotations

import shutil

import pytest
from conftest import AIS_DEMO, CAPTURES
from eye.api import feed
from eye.ingest.capture import archive, commit_batch, load_fixtures
from eye.storage.db import connect
from eye.storage.migrate import MIGRATIONS_DIR, migrate
from eye.worker import transits
from pg8000.exceptions import DatabaseError

DEMO_FILES = sorted(AIS_DEMO.glob("*.json"))
LINE = transits.load_line(transits.LINES_DIR / "synthetic-golden-gate.v1.json")


def derive(conn) -> str:
    inputs = transits.read_inputs(conn, transits.SOURCE_ID, LINE)
    return transits.store(conn, transits.SOURCE_ID, LINE, transits.hourly_intervals(inputs))[0]


def facts(conn) -> list:
    return [
        conn.run(f"SELECT count(*) FROM eye.{table}")[0][0]  # noqa: S608 - fixed names
        for table in ("capture_batch", "observation", "coverage", "transit_count", "derivation_run")
    ]


def test_migration_backfills_existing_history_in_order(make_db, tmp_path):
    before = tmp_path / "migrations"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("000[12]_*.sql")):
        shutil.copy(path, before / path.name)
    conn = connect(make_db())
    assert migrate(conn, before) == [1, 2]
    load_fixtures(conn, CAPTURES)
    load_fixtures(conn, AIS_DEMO)
    run_id = derive(conn)
    prior = facts(conn)
    # Up to 0003 only: 0004 refuses databases that already hold observations
    # (tests/test_identity.py covers that refusal).
    shutil.copy(next(MIGRATIONS_DIR.glob("0003_*.sql")), before)
    assert migrate(conn, before) == [3]
    assert facts(conn) == prior  # nothing earlier was rewritten
    rows = conn.run("SELECT kind, run_id::text FROM eye.feed_change ORDER BY change_seq")
    settled = conn.run("SELECT count(*) FROM eye.capture_batch WHERE status <> 'pending'")[0][0]
    assert [r[0] for r in rows] == ["capture_batch"] * settled + ["derivation_run"]
    assert rows[-1][1] == run_id  # the run came after the batches it used
    epoch, seq = feed.head(conn)
    assert seq == len(rows) and feed.parse_cursor(feed.make_cursor(epoch, seq)) == (epoch, seq)


def test_change_log_and_epoch_are_append_only(db):
    load_fixtures(db, AIS_DEMO)
    assert db.run("SELECT count(*) FROM eye.feed_change")[0][0] == 4  # control: triggers wrote
    for statement in (
        "DELETE FROM eye.feed_change",
        "UPDATE eye.feed_change SET layer = 'road'",
        "UPDATE eye.feed_epoch SET created_at = now()",
        "DELETE FROM eye.feed_epoch",
    ):
        with pytest.raises(DatabaseError, match="not permitted"):
            db.run(statement)
    with pytest.raises(DatabaseError):
        db.run("INSERT INTO eye.feed_epoch DEFAULT VALUES")  # only one epoch per database


def test_only_settled_batches_and_runs_are_changes(db):
    parsed = archive(db, DEMO_FILES[2].read_bytes())  # the outage hour
    assert db.run("SELECT count(*) FROM eye.feed_change")[0][0] == 0  # pending: not yet
    commit_batch(db, parsed.batch_id)
    rows = db.run("SELECT kind, batch_id::text FROM eye.feed_change")
    assert rows == [["capture_batch", parsed.batch_id]]  # a failed capture is a change too
    derive(db)
    assert db.run("SELECT kind FROM eye.feed_change ORDER BY change_seq")[-1] == ["derivation_run"]
    derive(db)  # re-running on the same evidence records nothing new
    assert db.run("SELECT count(*) FROM eye.feed_change")[0][0] == 2


def test_writers_serialise_so_changes_appear_in_commit_order(make_db):
    url = make_db()
    setup = connect(url)
    migrate(setup)
    first = archive(setup, DEMO_FILES[0].read_bytes()).batch_id
    second = archive(setup, DEMO_FILES[1].read_bytes()).batch_id
    a, b, reader = connect(url), connect(url), connect(url)
    settle = (
        "UPDATE eye.capture_batch SET status = 'failed', accepted_count = 0, "
        "rejected_count = 0, committed_at = now(), failure_reason = 'test' "
        "WHERE batch_id = CAST(:id AS uuid)"
    )
    try:
        a.run("BEGIN")
        a.run(settle, id=first)  # holds the change-log lock until commit
        b.run("SET lock_timeout = '300ms'")
        with pytest.raises(DatabaseError, match="lock"):
            b.run(settle, id=second)
        # Control: readers are never blocked by the writers' lock.
        assert reader.run("SELECT count(*) FROM eye.feed_change") == [[0]]
        a.run("COMMIT")
        b.run(settle, id=second)
        order = reader.run("SELECT batch_id::text FROM eye.feed_change ORDER BY change_seq")
        assert order == [[first], [second]]
    finally:
        for conn in (a, b, reader, setup):
            conn.close()
