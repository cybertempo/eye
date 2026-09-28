"""Migrations, capture storage, coverage and replay against real PostGIS.

Every refusal here sits beside a positive control that uses the same path.
"""

from __future__ import annotations

import shutil

import pytest
from conftest import CAPTURES
from eye.ingest.capture import (
    CaptureRejected,
    archive,
    commit_batch,
    ingest,
    load_fixtures,
    replay_pending,
    verify_replay,
)
from eye.storage.db import connect
from eye.storage.migrate import MIGRATIONS_DIR, MigrationError, migrate, status
from pg8000.exceptions import DatabaseError


def fingerprint(conn) -> dict:
    """Everything replay must reproduce, excluding wall-clock bookkeeping."""
    return {
        "batches": conn.run(
            "SELECT batch_id::text, source_id, layer, status::text, accepted_count, "
            "rejected_count, evidence_sha256 FROM eye.capture_batch ORDER BY 1"
        ),
        "evidence": conn.run("SELECT evidence_id::text, sha256 FROM eye.raw_evidence ORDER BY 1"),
        "observations": conn.run(
            "SELECT observation_id::text, source_record_id, observed_time, received_time, "
            "ST_AsText(position), altitude_m, content_sha256, "
            "supersedes_observation_id::text FROM eye.observation ORDER BY 1"
        ),
        "coverage": conn.run(
            "SELECT coverage_id::text, layer, state::text, metric_value, reason "
            "FROM eye.coverage ORDER BY 1"
        ),
    }


def coverage_for(conn, fixture_name: str):
    return conn.run(
        "SELECT c.state::text, c.metric_value, c.reason FROM eye.coverage c "
        "JOIN eye.capture_batch b USING (batch_id) JOIN eye.raw_evidence e USING (batch_id) "
        "WHERE e.sha256 = encode(sha256(:raw), 'hex')",
        raw=(CAPTURES / fixture_name).read_bytes(),
    )[0]


# --- migrations -----------------------------------------------------------


def test_clean_database_migrates_and_rerun_is_a_no_op(make_db):
    conn = connect(make_db())
    assert migrate(conn) == [1]
    assert migrate(conn) == []
    assert status(conn) == {"applied": [1], "pending": []}
    assert conn.run("SELECT PostGIS_Lib_Version()")[0][0].startswith("3.")


def copy_migrations(tmp_path):
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target)
    return target


def test_failed_migration_leaves_prior_data_intact_and_good_one_applies(make_db, tmp_path):
    migrations = copy_migrations(tmp_path)
    conn = connect(make_db())
    migrate(conn, migrations)
    load_fixtures(conn, CAPTURES)
    before = fingerprint(conn)
    assert before["observations"], "control: fixtures produced data"

    broken = migrations / "0002_add_note_column.sql"
    broken.write_text(
        "ALTER TABLE eye.observation ADD COLUMN note text;\n"
        "CREATE TABLE eye.half_done (x int);\n"
        "SELECT 1 / 0;\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationError, match="rolled back"):
        migrate(conn, migrations)
    assert fingerprint(conn) == before
    assert status(conn, migrations) == {"applied": [1], "pending": [2]}
    assert conn.run("SELECT to_regclass('eye.half_done')") == [[None]]
    columns = conn.run(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'eye' AND table_name = 'observation' AND column_name = 'note'"
    )
    assert columns == []

    # Positive control: the corrected migration applies over the same data.
    broken.write_text("ALTER TABLE eye.observation ADD COLUMN note text;\n", encoding="utf-8")
    assert migrate(conn, migrations) == [2]
    assert fingerprint(conn) == before
    assert status(conn, migrations) == {"applied": [1, 2], "pending": []}


def test_edited_applied_migration_is_refused(make_db, tmp_path):
    migrations = copy_migrations(tmp_path)
    conn = connect(make_db())
    migrate(conn, migrations)
    assert migrate(conn, migrations) == []  # control: unchanged files are accepted
    first = next(migrations.glob("0001_*.sql"))
    first.write_text(first.read_text() + "\n-- edited\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(conn, migrations)


def test_migration_files_are_checked_before_running(tmp_path):
    migrations = copy_migrations(tmp_path)
    (migrations / "0003_gap.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="without gaps"):
        migrate(None, migrations)
    (migrations / "0003_gap.sql").unlink()
    (migrations / "0002_commits.sql").write_text("SELECT 1;\nCOMMIT;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="transaction control"):
        migrate(None, migrations)


# --- fixtures, ids and replay ---------------------------------------------


def test_repeated_fixture_load_is_idempotent(db):
    first = load_fixtures(db, CAPTURES)
    assert all(result.created for result in first)
    before = fingerprint(db)
    second = load_fixtures(db, CAPTURES)
    assert not any(result.created for result in second)
    assert [r.batch_id for r in first] == [r.batch_id for r in second]
    assert fingerprint(db) == before


def test_clean_rebuild_and_replay_yield_identical_ids(make_db):
    fingerprints = []
    for _ in range(2):
        conn = connect(make_db())
        migrate(conn)
        load_fixtures(conn, CAPTURES)
        assert verify_replay(conn) == []
        fingerprints.append(fingerprint(conn))
        conn.close()
    assert fingerprints[0] == fingerprints[1]
    assert len(fingerprints[0]["batches"]) == len(list(CAPTURES.glob("*.json")))


def test_verify_replay_detects_a_missing_derived_row(db):
    load_fixtures(db, CAPTURES)
    assert verify_replay(db) == []  # control
    db.run("ALTER TABLE eye.observation DISABLE TRIGGER observation_append_only")
    db.run(
        "DELETE FROM eye.observation WHERE observation_id = "
        "(SELECT observation_id FROM eye.observation WHERE supersedes_observation_id IS NULL "
        "AND observation_id NOT IN (SELECT supersedes_observation_id FROM eye.observation "
        "WHERE supersedes_observation_id IS NOT NULL) ORDER BY 1 LIMIT 1)"
    )
    problems = verify_replay(db)
    assert problems and "missing" in problems[0]


def test_crash_between_archive_and_commit_is_recovered(db, make_db):
    raw = (CAPTURES / "001-flight-ok.json").read_bytes()
    parsed = archive(db, raw)
    assert db.run("SELECT status::text FROM eye.capture_batch") == [["pending"]]
    assert db.run("SELECT count(*) FROM eye.observation") == [[0]]
    completed = replay_pending(db)
    assert [r.batch_id for r in completed] == [parsed.batch_id]
    assert replay_pending(db) == []

    control = connect(make_db())
    migrate(control)
    ingest(control, raw)
    assert fingerprint(db) == fingerprint(control)


def test_tampered_evidence_fails_the_batch(db):
    ok = ingest(db, (CAPTURES / "007-flight-measured-zero.json").read_bytes())
    assert ok.status == "committed"  # control
    parsed = archive(db, (CAPTURES / "001-flight-ok.json").read_bytes())
    db.run("ALTER TABLE eye.raw_evidence DISABLE TRIGGER raw_evidence_append_only")
    db.run(
        "UPDATE eye.raw_evidence SET content = overlay(content placing 'X' from 3 for 1) "
        "WHERE batch_id = CAST(:id AS uuid)",
        id=parsed.batch_id,
    )
    result = commit_batch(db, parsed.batch_id)
    assert result.status == "failed"
    coverage = db.run(
        "SELECT state::text, metric_value, reason FROM eye.coverage "
        "WHERE batch_id = CAST(:id AS uuid)",
        id=parsed.batch_id,
    )
    assert coverage == [["failed", None, "evidence checksum mismatch"]]
    assert commit_batch(db, parsed.batch_id).created is False  # idempotent
    assert db.run(
        "SELECT count(*) FROM eye.observation WHERE batch_id = CAST(:id AS uuid)",
        id=parsed.batch_id,
    ) == [[0]]


def test_unapproved_or_non_synthetic_capture_is_refused(db):
    raw = (CAPTURES / "001-flight-ok.json").read_bytes()
    assert ingest(db, raw).status == "committed"  # control
    for old, new in (
        (b'"source_id": "synthetic-fixture"', b'"source_id": "opensky"'),
        (b'"synthetic": true', b'"synthetic": false'),
    ):
        with pytest.raises(CaptureRejected):
            ingest(db, raw.replace(old, new))
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[1]]


# --- coverage: an outage is never zero ------------------------------------


def test_outage_is_failed_or_unknown_with_null_metric(db):
    load_fixtures(db, CAPTURES)
    state, value, reason = coverage_for(db, "003-vessel-timeout.json")
    assert (state, value) == ("failed", None) and "timeout" in reason
    state, value, _ = coverage_for(db, "006-road-indeterminate.json")
    assert (state, value) == ("unknown", None)
    # Positive control: a healthy feed with nothing to report is a measured zero.
    assert coverage_for(db, "007-flight-measured-zero.json")[:2] == ["qualified", 0]
    assert coverage_for(db, "001-flight-ok.json")[:2] == ["qualified", 1]
    state, value, reason = coverage_for(db, "002-vessel-partial.json")
    assert (state, value) == ("partial", 1) and "rejected" in reason


def test_database_refuses_zero_for_an_outage(db):
    load_fixtures(db, CAPTURES)
    batch = db.run("SELECT batch_id::text FROM eye.capture_batch ORDER BY 1 LIMIT 1")[0][0]
    insert = (
        "INSERT INTO eye.coverage (coverage_id, batch_id, source_id, layer, interval_start, "
        "interval_end, state, reason, metric_name, metric_value, derivation_version) VALUES "
        "(gen_random_uuid(), CAST(:b AS uuid), 'synthetic-fixture', 'probe', "
        "'2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', CAST(:state AS eye.coverage_state), "
        ":reason, :metric, :value, 'test')"
    )
    db.run(insert, b=batch, state="failed", reason="probe outage", metric="m_null", value=None)
    for state, reason, value in (("failed", "probe outage", 0), ("unknown", None, None)):
        with pytest.raises(DatabaseError, match="coverage_missing_is_null"):
            db.run(insert, b=batch, state=state, reason=reason, metric="m_bad", value=value)
    db.run(insert, b=batch, state="qualified", reason=None, metric="m_zero", value=0)


# --- times, corrections, duplicates and append-only history --------------


def test_observed_and_received_times_are_separate(db):
    load_fixtures(db, CAPTURES)
    rows = db.run(
        "SELECT to_char(observed_time, 'HH24:MI'), to_char(received_time, 'HH24:MI') "
        "FROM eye.observation WHERE source_record_id = 'SYN-VES-001' "
        "AND observed_time = '2026-01-01T00:20:00Z' ORDER BY received_time"
    )
    assert rows == [["00:20", "00:20"], ["00:20", "00:52"]]
    columns = dict(
        db.run(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'eye' AND table_name = 'observation' "
            "AND column_name IN ('observed_time', 'received_time')"
        )
    )
    assert columns == {"observed_time": "NO", "received_time": "NO"}


def test_capture_without_received_time_rejects_that_record_only(db):
    raw = (CAPTURES / "001-flight-ok.json").read_bytes()
    bad = raw.replace(b'"received_time": "2026-01-01T00:00:04Z",', b"", 1)
    result = ingest(db, bad)
    assert (result.accepted, result.rejected) == (2, 1)
    assert db.run("SELECT count(*) FROM eye.observation") == [[2]]
    state, _, reason = db.run("SELECT state::text, metric_value, reason FROM eye.coverage")[0]
    assert state == "partial" and "rejected" in reason


def test_late_correction_supersedes_without_rewriting_history(db):
    load_fixtures(db, CAPTURES)
    rows = db.run(
        "SELECT observation_id::text, supersedes_observation_id::text, ST_AsText(position) "
        "FROM eye.observation WHERE source_record_id = 'SYN-VES-001' "
        "AND observed_time = '2026-01-01T00:20:00Z' ORDER BY received_time"
    )
    (original, none, first_pos), (correction, supersedes, corrected_pos) = rows
    assert none is None and supersedes == original
    assert first_pos == "POINT(0.13 0.27)" and corrected_pos == "POINT(0.135 0.268)"


def test_duplicate_delivery_is_not_double_counted(db):
    load_fixtures(db, CAPTURES)
    count = db.run(
        "SELECT count(*) FROM eye.observation WHERE source_record_id = 'SYN-FLT-001' "
        "AND observed_time = '2026-01-01T00:05:00Z'"
    )
    assert count == [[1]]
    assert db.run(
        "SELECT count(*) FROM eye.observation WHERE source_record_id = 'SYN-FLT-001'"
    ) == [[3]]


def test_history_is_append_only(db):
    load_fixtures(db, CAPTURES)
    assert db.run("SELECT count(*) FROM eye.observation")[0][0] > 0  # control: rows readable
    for statement in (
        "UPDATE eye.observation SET confidence = 0",
        "DELETE FROM eye.observation",
        "UPDATE eye.coverage SET metric_value = 0",
        "DELETE FROM eye.raw_evidence",
        "UPDATE eye.capture_batch SET status = 'pending'",
        "DELETE FROM eye.capture_batch",
    ):
        with pytest.raises(DatabaseError, match="not permitted|already|immutable"):
            db.run(statement)
