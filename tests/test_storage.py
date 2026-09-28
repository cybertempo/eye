"""Migrations, capture storage, coverage and replay against real PostGIS.

Every refusal here sits beside a positive control that uses the same path.
"""

from __future__ import annotations

import json
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
from eye.storage.db import connect, transaction
from eye.storage.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    Statement,
    applied,
    execute_statements,
    migrate,
    status,
)
from pg8000.exceptions import DatabaseError


def fingerprint(conn) -> dict:
    """Everything replay must reproduce, excluding wall-clock bookkeeping."""
    return {
        "batches": conn.run(
            "SELECT batch_id::text, source_id, layer, status::text, accepted_count, "
            "rejected_count, failure_reason, evidence_sha256 FROM eye.capture_batch ORDER BY 1"
        ),
        "evidence": conn.run("SELECT evidence_id::text, sha256 FROM eye.raw_evidence ORDER BY 1"),
        "observations": conn.run(
            "SELECT observation_id::text, source_record_id, observed_time, source_published_time, "
            "ST_AsText(position), altitude_m, confidence, content_sha256 "
            "FROM eye.observation ORDER BY 1"
        ),
        "receipts": conn.run(
            "SELECT observation_id::text, batch_id::text, received_time "
            "FROM eye.observation_receipt ORDER BY 1, 2"
        ),
        "versions": conn.run(
            "SELECT observation_id::text, version, supersedes_observation_id::text, is_current, "
            "first_received_time, receipt_count, publication_conflict "
            "FROM eye.observation_version ORDER BY 1"
        ),
        "coverage": conn.run(
            "SELECT coverage_id::text, layer, state::text, metric_value, reason "
            "FROM eye.coverage ORDER BY 1"
        ),
    }


def raw(name: str) -> bytes:
    return (CAPTURES / name).read_bytes()


def edited(name: str, change) -> bytes:
    doc = json.loads(raw(name))
    change(doc)
    return json.dumps(doc).encode("utf-8")


def coverage_for(conn, capture: bytes):
    return conn.run(
        "SELECT c.state::text, c.metric_value, c.reason FROM eye.coverage c "
        "JOIN eye.raw_evidence e USING (batch_id) WHERE e.sha256 = encode(sha256(:raw), 'hex')",
        raw=capture,
    )[0]


def vessel_versions(conn):
    return conn.run(
        "SELECT eye.iso_utc(source_published_time), version, is_current, "
        "supersedes_observation_id IS NOT NULL FROM eye.observation_version "
        "WHERE source_record_id = 'SYN-VES-001' AND observed_time = '2026-01-01T00:20:00Z' "
        "ORDER BY version"
    )


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


PLPGSQL_MIGRATION = """
-- A function body full of semicolons, BEGIN and END must still apply.
CREATE FUNCTION eye.add_one(x integer) RETURNS integer LANGUAGE plpgsql AS $fn$
BEGIN
    -- COMMIT; inside a comment inside a body is only text
    RETURN x + 1;
END;
$fn$;
COMMENT ON FUNCTION eye.add_one(integer) IS 'text with ; COMMIT ; inside a string';
"""


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE eye.leak (a int); COMMIT; SELECT 1 / 0;\n",
        "CREATE TABLE eye.leak (a int); /* hidden */ commit ; SELECT 1 / 0;\n",
        "CREATE TABLE eye.leak (a int);\nEND;\n",
        "CREATE TABLE eye.leak (a int); ROLLBACK; CREATE TABLE eye.leak2 (a int);\n",
        "CREATE TABLE eye.leak (a int); START TRANSACTION;\n",
        "CREATE TABLE eye.leak (a int); SAVEPOINT s1;\n",
    ],
)
def test_commit_on_the_same_line_cannot_commit_partial_work(make_db, tmp_path, sql):
    migrations = copy_migrations(tmp_path)
    conn = connect(make_db())
    migrate(conn, migrations)
    (migrations / "0002_sneaky.sql").write_text(sql, encoding="utf-8")
    with pytest.raises(MigrationError, match="transaction control"):
        migrate(conn, migrations)
    assert conn.run("SELECT to_regclass('eye.leak'), to_regclass('eye.leak2')") == [[None, None]]
    assert sorted(applied(conn)) == [1]  # nothing from the refused file was recorded

    # Positive control: a valid PL/pgSQL function migration applies.
    (migrations / "0002_sneaky.sql").write_text(PLPGSQL_MIGRATION, encoding="utf-8")
    assert migrate(conn, migrations) == [2]
    assert conn.run("SELECT eye.add_one(41)") == [[42]]


def test_server_refuses_commit_even_if_the_parser_were_bypassed(db):
    """Second and third layers: the database itself refuses, and nothing is committed."""
    smuggled = Statement("CREATE TABLE eye.leak (a int); COMMIT", "CREATE")
    with pytest.raises(DatabaseError, match="multiple commands"), transaction(db):
        execute_statements(db, [smuggled])
    in_do_block = Statement("DO $$ BEGIN CREATE TABLE eye.leak (a int); COMMIT; END $$", "DO")
    with pytest.raises(DatabaseError, match="invalid transaction termination"), transaction(db):
        execute_statements(db, [in_do_block])
    assert db.run("SELECT to_regclass('eye.leak')") == [[None]]
    # Positive control through the same path.
    with transaction(db):
        execute_statements(db, [Statement("CREATE TABLE eye.fine (a int)", "CREATE")])
    assert db.run("SELECT to_regclass('eye.fine')::text") == [["eye.fine"]]


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
    (migrations / "0002_open_quote.sql").write_text("SELECT 'unterminated;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="unterminated"):
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


def test_clean_rebuild_in_any_load_order_yields_identical_state(make_db):
    fingerprints = []
    for reverse in (False, True, False):
        conn = connect(make_db())
        migrate(conn)
        load_fixtures(conn, CAPTURES, reverse=reverse)
        assert verify_replay(conn) == []
        fingerprints.append(fingerprint(conn))
        conn.close()
    assert fingerprints[0] == fingerprints[1] == fingerprints[2]
    assert len(fingerprints[0]["batches"]) == len(list(CAPTURES.glob("*.json")))


def test_crash_between_archive_and_commit_is_recovered(db, make_db):
    capture = raw("001-flight-ok.json")
    parsed = archive(db, capture)
    assert db.run("SELECT status::text FROM eye.capture_batch") == [["pending"]]
    assert db.run("SELECT count(*) FROM eye.observation") == [[0]]
    assert any("pending" in p for p in verify_replay(db))  # not clean while pending
    completed = replay_pending(db)
    assert [r.batch_id for r in completed] == [parsed.batch_id]
    assert replay_pending(db) == []
    assert verify_replay(db) == []

    control = connect(make_db())
    migrate(control)
    ingest(control, capture)
    assert fingerprint(db) == fingerprint(control)


# --- db-replay compares values, not just ids --------------------------------


TAMPERING = {
    "observation position": (
        "observation_append_only",
        "UPDATE eye.observation SET position = ST_SetSRID(ST_MakePoint(0.5, 0.5), 4326) "
        "WHERE observation_id = (SELECT min(observation_id::text)::uuid FROM eye.observation)",
        "observation",
    ),
    "observation confidence": (
        "observation_append_only",
        "UPDATE eye.observation SET confidence = 0.1 WHERE observation_id = "
        "(SELECT min(observation_id::text)::uuid FROM eye.observation)",
        "observation",
    ),
    "receipt time": (
        "observation_receipt_append_only",
        "ALTER TABLE eye.observation_receipt DISABLE TRIGGER observation_receipt_chronology; "
        "UPDATE eye.observation_receipt SET received_time = received_time + interval '1 hour' "
        "WHERE observation_id = (SELECT min(observation_id::text)::uuid "
        "FROM eye.observation_receipt)",
        "receipt",
    ),
    "coverage value": (
        "coverage_append_only",
        "UPDATE eye.coverage SET metric_value = 7 WHERE state = 'qualified' "
        "AND coverage_id = (SELECT min(coverage_id::text)::uuid FROM eye.coverage "
        "WHERE state = 'qualified')",
        "coverage",
    ),
    "coverage state": (
        "coverage_append_only",
        "UPDATE eye.coverage SET state = 'unknown', metric_value = NULL, reason = 'x' "
        "WHERE state = 'failed'",
        "coverage",
    ),
    "batch counts": (
        "capture_batch_guard",
        "UPDATE eye.capture_batch SET rejected_count = rejected_count + 1 "
        "WHERE batch_id = (SELECT min(batch_id::text)::uuid FROM eye.capture_batch)",
        "batch",
    ),
    "correction link (published time)": (
        "observation_append_only",
        "UPDATE eye.observation SET source_published_time = '2026-01-01T00:20:00Z' "
        "WHERE 'corrected_by_source' = ANY(quality_flags)",
        "version",
    ),
    "extra observation": (
        "observation_append_only",
        "INSERT INTO eye.observation (observation_id, source_id, source_record_id, layer, "
        "observed_time, source_published_time, position, content_sha256) VALUES "
        "(gen_random_uuid(), 'synthetic-fixture', 'SYN-GHOST', 'flight', "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
        "ST_SetSRID(ST_MakePoint(0, 0), 4326), repeat('a', 64))",
        "stored but not derivable",
    ),
    "capture provider status": (
        "capture_batch_guard",
        "UPDATE eye.capture_batch SET provider_status = 'error' WHERE provider_status = 'timeout'",
        "provider_status",
    ),
    "capture requested interval": (
        "capture_batch_guard",
        "UPDATE eye.capture_batch SET requested_end = requested_end - interval '1 minute' "
        "WHERE batch_id = (SELECT min(batch_id::text)::uuid FROM eye.capture_batch)",
        "requested_end",
    ),
    "capture requested area": (
        "capture_batch_guard",
        "UPDATE eye.capture_batch SET requested_area = ST_MakeEnvelope(-1, -1, 1, 1, 4326) "
        "WHERE batch_id = (SELECT min(batch_id::text)::uuid FROM eye.capture_batch)",
        "requested_area.west",
    ),
    "capture receipt time": (
        "capture_batch_guard",
        "UPDATE eye.capture_batch SET attempt_finished_at = attempt_finished_at "
        "+ interval '1 second' WHERE provider_status = 'indeterminate'",
        "attempt_finished_at",
    ),
    "capture quota and adapter": (
        "capture_batch_guard",
        "UPDATE eye.capture_batch SET quota_cost = 5, adapter_version = 'other' "
        "WHERE batch_id = (SELECT min(batch_id::text)::uuid FROM eye.capture_batch)",
        "adapter_version, quota_cost",
    ),
    "evidence checksum column": (
        "raw_evidence_append_only",
        "UPDATE eye.raw_evidence SET sha256 = repeat('0', 64) "
        "WHERE batch_id = (SELECT min(batch_id::text)::uuid FROM eye.capture_batch)",
        "stored metadata",
    ),
    "evidence bytes": (
        "raw_evidence_append_only",
        "UPDATE eye.raw_evidence SET content = overlay(content placing 'X' from 3 for 1) "
        "WHERE batch_id = (SELECT min(batch_id::text)::uuid FROM eye.capture_batch "
        "WHERE status = 'committed')",
        "checksum",
    ),
}


@pytest.mark.parametrize("name", sorted(TAMPERING))
def test_replay_verification_detects_changed_values(db, name):
    trigger, statement, expect = TAMPERING[name]
    load_fixtures(db, CAPTURES)
    assert verify_replay(db) == []  # control on the same database
    table = {
        "observation_append_only": "eye.observation",
        "observation_receipt_append_only": "eye.observation_receipt",
        "coverage_append_only": "eye.coverage",
        "capture_batch_guard": "eye.capture_batch",
        "raw_evidence_append_only": "eye.raw_evidence",
    }[trigger]
    db.run(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
    db.execute_simple(statement)
    problems = verify_replay(db)
    assert problems, f"{name}: tampering went undetected"
    assert any(expect in p for p in problems), problems


def test_tampered_evidence_before_commit_fails_the_batch(db):
    ok = ingest(db, raw("007-flight-measured-zero.json"))
    assert ok.status == "committed"  # control
    parsed = archive(db, raw("001-flight-ok.json"))
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
    problems = verify_replay(db)
    assert any("does not match its checksum" in p for p in problems)
    assert not any("stored" in p and "derived" in p for p in problems)


def test_unapproved_or_non_synthetic_capture_is_refused(db):
    capture = raw("001-flight-ok.json")
    assert ingest(db, capture).status == "committed"  # control
    for old, new in (
        (b'"source_id": "synthetic-fixture"', b'"source_id": "opensky"'),
        (b'"synthetic": true', b'"synthetic": false'),
    ):
        with pytest.raises(CaptureRejected):
            ingest(db, capture.replace(old, new))
    assert db.run("SELECT count(*) FROM eye.capture_batch") == [[1]]


# --- coverage: nothing usable is never zero --------------------------------


def test_outage_is_failed_or_unknown_with_null_metric(db):
    load_fixtures(db, CAPTURES)
    state, value, reason = coverage_for(db, raw("003-vessel-timeout.json"))
    assert (state, value) == ("failed", None) and "timeout" in reason
    state, value, _ = coverage_for(db, raw("006-road-indeterminate.json"))
    assert (state, value) == ("unknown", None)
    # Positive controls: a healthy empty response is a measured zero; data counts.
    assert coverage_for(db, raw("007-flight-measured-zero.json"))[:2] == ["qualified", 0]
    assert coverage_for(db, raw("001-flight-ok.json"))[:2] == ["qualified", 1]
    state, value, reason = coverage_for(db, raw("002-vessel-partial.json"))
    assert (state, value) == ("partial", 1) and "rejected" in reason


def test_ok_capture_with_every_record_rejected_is_not_a_zero(db):
    everything_bad = raw("008-flight-all-rejected.json")
    result = ingest(db, everything_bad)
    assert (result.accepted, result.rejected) == (0, 2)
    state, value, reason = coverage_for(db, everything_bad)
    assert (state, value) == ("failed", None) and "all 2 record(s) rejected" in reason
    # Positive control: the same healthy feed answering with no records at all.
    healthy_empty = raw("007-flight-measured-zero.json")
    ingest(db, healthy_empty)
    assert coverage_for(db, healthy_empty)[:2] == ["qualified", 0]
    # And one usable record among rejects is partial with a real count.
    one_good = edited(
        "008-flight-all-rejected.json",
        lambda d: d["provider_response"]["records"][1].update(
            source_published_time="2026-01-01T00:40:05.000000Z"
        ),
    )
    ingest(db, one_good)
    assert coverage_for(db, one_good)[:2] == ["partial", 1]


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


# --- time provenance and chronology ----------------------------------------


def test_observed_published_and_received_times_are_separate_and_ordered(db):
    load_fixtures(db, CAPTURES)
    rows = db.run(
        "SELECT eye.iso_utc(o.observed_time), eye.iso_utc(o.source_published_time), "
        "eye.iso_utc(r.received_time) FROM eye.observation o "
        "JOIN eye.observation_receipt r USING (observation_id) "
        "WHERE o.source_record_id = 'SYN-VES-001' AND o.observed_time = '2026-01-01T00:20:00Z' "
        "ORDER BY o.source_published_time"
    )
    assert rows == [
        [
            "2026-01-01T00:20:00.000000Z",
            "2026-01-01T00:20:30.000000Z",
            "2026-01-01T00:45:03.000000Z",
        ],
        [
            "2026-01-01T00:20:00.000000Z",
            "2026-01-01T00:51:30.000000Z",
            "2026-01-01T00:52:01.000000Z",
        ],
    ]
    # Every stored receipt is the batch's EYE receipt time, after publication.
    violations = db.run(
        "SELECT count(*) FROM eye.observation_receipt r JOIN eye.capture_batch b "
        "USING (batch_id) JOIN eye.observation o USING (observation_id) "
        "WHERE r.received_time <> b.attempt_finished_at "
        "OR r.received_time < o.source_published_time "
        "OR o.source_published_time < o.observed_time "
        "OR b.attempt_finished_at < b.requested_end"
    )
    assert violations == [[0]]


def test_provider_supplied_receipt_time_is_rejected(db):
    good = edited(
        "008-flight-all-rejected.json",
        lambda d: d["provider_response"]["records"].__setitem__(
            0,
            {k: v for k, v in d["provider_response"]["records"][0].items() if k != "received_time"},
        ),
    )
    with_receipt = raw("008-flight-all-rejected.json")
    assert b'"received_time"' in with_receipt and b'"received_time"' not in good
    assert ingest(db, good).accepted == 1  # control: same record without the field
    assert ingest(db, with_receipt).accepted == 0


def test_capture_claiming_receipt_before_the_interval_ended_is_refused(db):
    early = edited(
        "001-flight-ok.json",
        lambda d: d["attempt"].update(
            started_at="2026-01-01T00:14:00.000000Z", finished_at="2026-01-01T00:14:30.000000Z"
        ),
    )
    with pytest.raises(CaptureRejected, match="has not ended"):
        ingest(db, early)
    assert ingest(db, raw("001-flight-ok.json")).status == "committed"  # control


def test_database_refuses_an_invalid_receipt_and_accepts_a_genuine_one(db):
    load_fixtures(db, CAPTURES)
    # The correction was published by the source at 00:51:30.
    oid = db.run(
        "SELECT observation_id::text FROM eye.observation "
        "WHERE 'corrected_by_source' = ANY(quality_flags)"
    )[0][0]

    def batch_finished_at(moment: str) -> tuple[str, str]:
        return tuple(
            db.run(
                "SELECT b.batch_id::text, e.evidence_id::text FROM eye.capture_batch b "
                "JOIN eye.raw_evidence e USING (batch_id) "
                "WHERE b.attempt_finished_at = CAST(:t AS timestamptz)",
                t=moment,
            )[0]
        )

    insert = (
        "INSERT INTO eye.observation_receipt (observation_id, batch_id, evidence_id, "
        "received_time, schema_version, adapter_version) VALUES (CAST(:o AS uuid), "
        "CAST(:b AS uuid), CAST(:e AS uuid), CAST(:t AS timestamptz), 'eye.wire/1', "
        "'synthetic-adapter/2')"
    )
    timeout_batch, timeout_evidence = batch_finished_at("2026-01-01T01:00:30Z")
    early_batch, early_evidence = batch_finished_at("2026-01-01T00:10:31Z")
    # Evidence from a different batch.
    with pytest.raises(DatabaseError, match="receipt_evidence_of_batch"):
        db.run(insert, o=oid, b=timeout_batch, e=early_evidence, t="2026-01-01T01:00:30Z")
    # A receipt time that is not the batch's own receipt time.
    with pytest.raises(DatabaseError, match="not the batch receipt time"):
        db.run(insert, o=oid, b=timeout_batch, e=timeout_evidence, t="2026-01-01T01:00:29Z")
    # Received (00:10:31) before the source published it (00:51:30).
    with pytest.raises(DatabaseError, match="before the source published"):
        db.run(insert, o=oid, b=early_batch, e=early_evidence, t="2026-01-01T00:10:31Z")

    # Positive control: a batch that genuinely re-delivers the correction.
    redelivery = edited(
        "004-vessel-correction.json",
        lambda d: d["attempt"].update(
            started_at="2026-01-01T00:59:00Z", finished_at="2026-01-01T00:59:02Z"
        ),
    )
    parsed = archive(db, redelivery)  # pending: archived, not yet committed
    db.run(insert, o=oid, b=parsed.batch_id, e=parsed.evidence_id, t="2026-01-01T00:59:02Z")
    # Committing derives exactly that receipt, and replay finds nothing to report.
    assert commit_batch(db, parsed.batch_id).status == "committed"
    assert db.run(
        "SELECT count(*) FROM eye.observation_receipt WHERE batch_id = CAST(:b AS uuid)",
        b=parsed.batch_id,
    ) == [[1]]
    assert verify_replay(db) == []


# --- corrections, duplicates and append-only history -----------------------


def test_correction_meaning_is_independent_of_load_order(make_db):
    orders = {
        "original first": ["002-vessel-partial.json", "004-vessel-correction.json"],
        "correction first": ["004-vessel-correction.json", "002-vessel-partial.json"],
    }
    results = {}
    for label, names in orders.items():
        conn = connect(make_db())
        migrate(conn)
        for name in names:
            ingest(conn, raw(name))
        results[label] = vessel_versions(conn)
        assert verify_replay(conn) == []
        conn.close()
    expected = [
        ["2026-01-01T00:20:30.000000Z", 1, False, False],  # original, superseded
        ["2026-01-01T00:51:30.000000Z", 2, True, True],  # correction, current
    ]
    assert results["original first"] == expected  # positive control
    assert results["correction first"] == expected


def vessel_version_rows(conn):
    return conn.run(
        "SELECT eye.iso_utc(v.source_published_time), ST_X(o.position), v.version, "
        "v.is_current, s.source_published_time IS NOT NULL, v.publication_conflict "
        "FROM eye.observation_version v JOIN eye.observation o USING (observation_id) "
        "LEFT JOIN eye.observation s ON s.observation_id = v.supersedes_observation_id "
        "WHERE v.source_record_id = 'SYN-VES-001' AND v.observed_time = '2026-01-01T00:20:00Z' "
        "ORDER BY v.source_published_time, ST_X(o.position)"
    )


def test_conflicting_latest_publication_leaves_current_unknown(db):
    load_fixtures(db, CAPTURES)
    # Control: a unique later publication supersedes and is current.
    assert vessel_version_rows(db) == [
        ["2026-01-01T00:20:30.000000Z", 0.13, 1, False, False, False],
        ["2026-01-01T00:51:30.000000Z", 0.135, 2, True, True, False],
    ]
    rival = edited(
        "004-vessel-correction.json",
        lambda d: d["provider_response"]["records"][0].update(lon=0.14),
    )
    ingest(db, rival)
    # Two versions share the latest publication time: current is unknown,
    # neither supersedes anything, and they share a version number.
    assert vessel_version_rows(db) == [
        ["2026-01-01T00:20:30.000000Z", 0.13, 1, False, False, False],
        ["2026-01-01T00:51:30.000000Z", 0.135, 2, None, False, True],
        ["2026-01-01T00:51:30.000000Z", 0.14, 2, None, False, True],
    ]
    assert verify_replay(db) == []

    # A later unique publication becomes current, but does not claim to
    # correct either contested version.
    settled = edited(
        "004-vessel-correction.json",
        lambda d: (
            d["attempt"].update(
                started_at="2026-01-01T00:58:10Z", finished_at="2026-01-01T00:58:11Z"
            ),
            d["provider_response"]["records"][0].update(
                source_published_time="2026-01-01T00:58:00Z", lon=0.137
            ),
        ),
    )
    ingest(db, settled)
    assert vessel_version_rows(db) == [
        ["2026-01-01T00:20:30.000000Z", 0.13, 1, False, False, False],
        ["2026-01-01T00:51:30.000000Z", 0.135, 2, False, False, True],
        ["2026-01-01T00:51:30.000000Z", 0.14, 2, False, False, True],
        ["2026-01-01T00:58:00.000000Z", 0.137, 3, True, False, False],
    ]
    assert verify_replay(db) == []


def test_duplicate_delivery_adds_a_receipt_not_an_observation(db):
    load_fixtures(db, CAPTURES)
    rows = db.run(
        "SELECT receipt_count, eye.iso_utc(first_received_time) FROM eye.observation_version "
        "WHERE source_record_id = 'SYN-FLT-001' AND observed_time = '2026-01-01T00:05:00Z'"
    )
    assert rows == [[2, "2026-01-01T00:10:31.000000Z"]]
    flight = "SELECT count(*) FROM eye.observation WHERE source_record_id = 'SYN-FLT-001'"
    assert db.run(flight) == [[3]]


def test_history_is_append_only(db):
    load_fixtures(db, CAPTURES)
    assert db.run("SELECT count(*) FROM eye.observation")[0][0] > 0  # control: rows readable
    for statement in (
        "UPDATE eye.observation SET confidence = 0",
        "DELETE FROM eye.observation",
        "UPDATE eye.observation_receipt SET adapter_version = 'x'",
        "DELETE FROM eye.observation_receipt",
        "UPDATE eye.coverage SET metric_value = 0",
        "DELETE FROM eye.raw_evidence",
        "UPDATE eye.capture_batch SET status = 'pending'",
        "DELETE FROM eye.capture_batch",
    ):
        with pytest.raises(DatabaseError, match="not permitted|already|immutable"):
            db.run(statement)
