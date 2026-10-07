"""Package 5 lifecycle: rollups, manifests, synthetic backup and drill, checked retention.

Every negative control sits beside a successful one. Everything here runs on
invented fixtures in disposable databases and directories; nothing prunes
outside a test-selected synthetic partition.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import CAPTURES, REPO_ROOT
from eye.ingest import capture as capture_module
from eye.ingest.capture import archive, ingest, load_fixtures, verify_replay
from eye.storage.db import connect
from eye.storage.migrate import migrate
from eye.worker import backup, retention, rollups, transits
from eye.worker.rollups import Partition
from pg8000.exceptions import DatabaseError
from synthetic_captures import capture, record

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "synthetic"
DEMO_DIRS = ("captures", "ais/demo", "events/demo", "media/demo")
LINES_DIR = REPO_ROOT / "reference" / "lines"
LINES = [transits.load_line(p) for p in sorted(LINES_DIR.glob("*.json"))]
NOW = datetime(2026, 9, 1, tzinfo=UTC)  # long after every fixture's lateness window
FLIGHT = Partition.parse("synthetic-fixture:flight:2026-01-01")
AIS = Partition.parse("synthetic-ais:vessel:2026-02-01")


def derive_transits(conn) -> None:
    for line in LINES:
        inputs = transits.read_inputs(conn, transits.SOURCE_ID, line)
        transits.store(conn, transits.SOURCE_ID, line, transits.hourly_intervals(inputs))


def prepare(conn, dirs=DEMO_DIRS, *, reverse=False) -> None:
    migrate(conn)
    for name in dirs:
        load_fixtures(conn, FIXTURES / name, reverse=reverse)
    derive_transits(conn)
    assert not [r for r in rollups.refresh_all(conn, LINES) if r.error]


@pytest.fixture
def demo(make_db):
    conn = connect(make_db())
    prepare(conn)
    yield conn
    conn.close()


def rollup(conn, partition: Partition, derivation: str, key: str, scope: str = ""):
    rows = conn.run(
        "SELECT state::text, value, reason FROM eye.current_rollup WHERE source_id = :s "
        "AND layer = :l AND day = :d AND derivation = :der AND scope = :scope AND row_key = :k",
        s=partition.source_id,
        l=partition.layer,
        d=partition.day,
        der=derivation,
        scope=scope,
        k=key,
    )
    assert len(rows) == 1, (partition, derivation, key, rows)
    state, value, reason = rows[0]
    return state, rollups.num(value), reason


def back_up(conn, directory: Path) -> backup.Proof:
    generation = backup.export(conn, directory, LINES_DIR)
    return backup.verify(conn, directory, generation)


def evaluate(conn, partition=FLIGHT, *, backup_dir=None, now=NOW, lateness=48):
    return retention.evaluate(
        conn, partition, now=now, lateness_hours=lateness, lines=LINES, backup_dir=backup_dir
    )


def execute(conn, partition=FLIGHT, *, backup_dir, allow=True, mode="demo", now=NOW):
    return retention.execute(
        conn,
        partition,
        allow_deletion=allow,
        mode=mode,
        lateness_hours=48,
        lines=LINES,
        backup_dir=backup_dir,
        now=now,
    )


def unpruned(conn, partition=FLIGHT) -> int:
    return conn.run(
        "SELECT count(*) FROM eye.raw_evidence e JOIN eye.capture_batch b USING (batch_id) "
        "WHERE b.source_id = :s AND b.layer = :l AND e.content IS NOT NULL "
        "AND (b.requested_start AT TIME ZONE 'UTC')::date = :d",
        s=partition.source_id,
        l=partition.layer,
        d=partition.day,
    )[0][0]


def ledger_counts(conn) -> list:
    return [
        conn.run(f"SELECT count(*) FROM eye.{t}")[0][0]  # noqa: S608
        for t in (
            "observation",
            "observation_receipt",
            "coverage",
            "event_claim",
            "media_item",
            "capture_batch",
            "raw_evidence",
            "derivation_manifest",
            "rollup_value",
        )
    ]


# --- rollups: zero versus unknown ---------------------------------------------------


def test_covered_zero_stays_zero_and_uncovered_or_failed_stays_unknown(demo):
    line = rollups.line_scope(LINES[0])
    # Positive controls: an exact hour and a covered hour with no transit.
    assert rollup(demo, AIS, "transit-daily", "hour:12", line) == ("qualified", 1, None)
    assert rollup(demo, AIS, "transit-daily", "hour:15", line) == ("qualified", 0, None)
    # A failed capture and an hour nobody captured are unknown, never 0.
    state, value, reason = rollup(demo, AIS, "transit-daily", "hour:14", line)
    assert (state, value) == ("unknown", None) and "failed" in reason
    assert rollup(demo, AIS, "transit-daily", "hour:16", line)[:2] == ("unknown", None)
    assert rollup(demo, AIS, "transit-daily", "day", line)[:2] == ("unknown", None)
    # A partial hour is a lower bound with its reason.
    state, value, reason = rollup(demo, AIS, "transit-daily", "hour:13", line)
    assert state == "partial" and value == 1 and "lower bound" in reason
    # Observed states follow the same rules.
    assert rollup(demo, AIS, "observations-hourly", "hour:15")[0] == "qualified"
    assert rollup(demo, AIS, "observations-hourly", "hour:14")[:2] == ("unknown", None)


def test_covered_hour_with_nothing_observed_is_a_genuine_zero(make_db, tmp_path):
    conn = connect(make_db())
    migrate(conn)
    day = "2026-01-02T"
    ingest(
        conn,
        capture(
            tmp_path,
            "zero",
            "flight",
            [],
            start=day + "00:00:00Z",
            end=day + "01:00:00Z",
            received=day + "01:00:05Z",
        ).read_bytes(),
    )
    seen = [record("SYNF-0001", day + "02:10:00Z", day + "02:10:01Z", 0.25)]
    ingest(
        conn,
        capture(
            tmp_path,
            "seen",
            "flight",
            seen,
            start=day + "02:00:00Z",
            end=day + "03:00:00Z",
            received=day + "03:00:05Z",
        ).read_bytes(),
    )
    rollups.refresh_all(conn, LINES)
    part = Partition.parse("synthetic-fixture:flight:2026-01-02")
    assert rollup(conn, part, "observations-hourly", "hour:00") == ("qualified", 0, None)
    assert rollup(conn, part, "observations-hourly", "hour:02") == ("qualified", 1, None)  # control
    assert rollup(conn, part, "observations-hourly", "hour:01")[:2] == ("unknown", None)
    assert rollup(conn, part, "observations-hourly", "day")[:2] == ("unknown", None)
    coverage = rollup(conn, part, "coverage-hourly", "hour:01")
    assert coverage == ("qualified", 0, None)  # EYE's own log: zero usable seconds
    conn.close()


def test_database_refuses_a_zero_for_unknown_and_a_position_in_a_rollup(demo):
    (manifest_id,) = demo.run(
        "SELECT manifest_id::text FROM eye.current_manifest WHERE derivation = 'cells-daily' "
        "AND source_id = 'synthetic-ais'"
    )[0]
    insert = (
        "INSERT INTO eye.rollup_value (manifest_id, row_key, interval_start, interval_end, "
        "metric, state, value, reason, detail) VALUES (:id, :k, '2026-02-01', '2026-02-02', "
        "'observed_states', CAST(:st AS eye.coverage_state), :v, :r, CAST(:d AS jsonb))"
    )
    demo.run("BEGIN")  # controls: a well-formed unknown row and a qualified zero
    demo.run(insert, id=manifest_id, k="test:a", st="unknown", v=None, r="none", d="{}")
    demo.run(insert, id=manifest_id, k="test:b", st="qualified", v=0, r=None, d="{}")
    demo.run("ROLLBACK")
    for state, value, reason, detail in (
        ("unknown", 0, "none", "{}"),
        ("failed", 0, "outage", "{}"),
        ("qualified", None, None, "{}"),
        ("partial", 1, None, "{}"),
        ("qualified", 1, None, '{"lon": 0.1, "lat": 0.2}'),
        ("qualified", 1, None, '{"centroid": [0.1, 0.2]}'),
    ):
        with pytest.raises(DatabaseError, match="rollup_"):
            demo.run(insert, id=manifest_id, k="test:c", st=state, v=value, r=reason, d=detail)


def test_cells_carry_bounds_never_a_coordinate(demo):
    rows = demo.run(
        "SELECT row_key, detail FROM eye.current_rollup WHERE derivation = 'cells-daily' "
        "AND source_id = 'synthetic-ais'"
    )
    assert rows  # control: there are occupied cells
    for key, detail in rows:
        assert key.startswith("cell:")
        assert set(detail) == {"bounds", "cell_deg", "coverage_ids", "records"}
        west, south, east, north = detail["bounds"]
        assert round(east - west, 6) == round(north - south, 6) == 0.1


def test_rollups_are_idempotent_and_independent_of_load_order(make_db, demo):
    first = sorted(demo.run("SELECT manifest_id::text, output_sha256 FROM eye.current_manifest"))
    again = rollups.refresh_all(demo, LINES)
    assert all(not r.created for r in again) and again
    other = connect(make_db())
    prepare(other, reverse=True)
    assert (
        sorted(other.run("SELECT manifest_id::text, output_sha256 FROM eye.current_manifest"))
        == first
    )
    other.close()


# --- manifests: late arrivals and corrections ---------------------------------------


def checks_by_derivation(conn, partition):
    return {c.derivation: c for c in rollups.validate(conn, partition, LINES)}


def test_late_arrival_makes_the_old_manifest_stale_and_keeps_history(make_db, tmp_path):
    conn = connect(make_db())
    prepare(conn, ("captures",))
    before = checks_by_derivation(conn, FLIGHT)
    assert {c.state for c in before.values()} == {"valid"}  # control
    assert rollup(conn, FLIGHT, "observations-hourly", "hour:00")[:2] == ("unknown", None)
    old_ids = {c.manifest_id for c in before.values()}
    # A late, healthy re-capture covers the failed 00:30 to 00:45 and the
    # uncaptured 00:45 to 01:00.
    late = [record("SYNF-0901", "2026-01-01T00:50:00Z", "2026-01-01T00:50:01Z", 0.2)]
    ingest(
        conn,
        capture(
            tmp_path,
            "late",
            "flight",
            late,
            start="2026-01-01T00:30:00Z",
            end="2026-01-01T01:00:00Z",
            received="2026-01-03T00:00:00Z",
        ).read_bytes(),
    )
    after = checks_by_derivation(conn, FLIGHT)
    for name in ("coverage-hourly", "observations-hourly", "cells-daily"):
        assert after[name].state == "stale" and "late arrival" in after[name].reason
    assert evaluate(conn).verdict == "blocked"
    # The outdated metric is not kept silently: the current value is the old
    # one until a new manifest exists, and the check says it is stale.
    rollups.refresh(conn, FLIGHT, LINES)
    renewed = checks_by_derivation(conn, FLIGHT)
    assert {c.state for c in renewed.values()} == {"valid"}
    assert not old_ids & {c.manifest_id for c in renewed.values()}
    stored = {m for (m,) in conn.run("SELECT manifest_id::text FROM eye.derivation_manifest")}
    assert old_ids <= stored  # history kept
    state, value, _ = rollup(conn, FLIGHT, "observations-hourly", "hour:00")
    assert state in ("qualified", "partial") and value >= 1
    conn.close()


def test_correction_invalidates_the_manifest_and_updates_the_metric(make_db, tmp_path):
    conn = connect(make_db())
    migrate(conn)
    day = "2026-01-05T"
    part = Partition.parse("synthetic-fixture:flight:2026-01-05")
    first = [record("SYNF-0100", day + "00:10:00Z", day + "00:10:01Z", 0.05)]
    ingest(
        conn,
        capture(
            tmp_path,
            "v1",
            "flight",
            first,
            start=day + "00:00:00Z",
            end=day + "01:00:00Z",
            received=day + "01:00:05Z",
        ).read_bytes(),
    )
    rollups.refresh(conn, part, LINES)
    cell_before = conn.run(
        "SELECT row_key FROM eye.current_rollup WHERE derivation = 'cells-daily' "
        "AND day = '2026-01-05'"
    )
    assert checks_by_derivation(conn, part)["cells-daily"].state == "valid"  # control
    # The source republishes the same record and time with a corrected position.
    fixed = [record("SYNF-0100", day + "00:10:00Z", day + "02:00:00Z", 0.35)]
    ingest(
        conn,
        capture(
            tmp_path,
            "v2",
            "flight",
            fixed,
            start=day + "00:00:00Z",
            end=day + "01:00:00Z",
            received=day + "02:00:05Z",
        ).read_bytes(),
    )
    assert checks_by_derivation(conn, part)["cells-daily"].state == "stale"
    rollups.refresh(conn, part, LINES)
    cell_after = conn.run(
        "SELECT row_key FROM eye.current_rollup WHERE derivation = 'cells-daily' "
        "AND day = '2026-01-05'"
    )
    assert cell_before != cell_after  # the corrected position moved the cell
    assert rollup(conn, part, "observations-hourly", "hour:00") == ("qualified", 1, None)
    versions = conn.run(
        "SELECT count(*) FROM eye.derivation_manifest WHERE derivation = 'cells-daily'"
    )[0][0]
    assert versions == 2
    conn.close()


def test_failed_rollup_cannot_pass_the_gate(demo, tmp_path, monkeypatch):
    assert back_up(demo, tmp_path).state == "verified"
    assert evaluate(demo, backup_dir=tmp_path).verdict == "eligible"  # control
    monkeypatch.setattr(rollups, "MAX_CELLS", 0)  # the cell rollup now refuses
    verdict = evaluate(demo, backup_dir=tmp_path)
    assert verdict.verdict == "blocked"
    assert any("cells-daily" in r and "failed" in r for r in verdict.reasons)


def test_missing_manifest_blocks_retention(make_db, tmp_path):
    conn = connect(make_db())
    migrate(conn)
    load_fixtures(conn, CAPTURES)
    verdict = evaluate(conn, backup_dir=tmp_path)
    assert verdict.verdict == "blocked"
    assert any("no manifest is recorded" in r for r in verdict.reasons)
    rollups.refresh_all(conn, LINES)
    back_up(conn, tmp_path)
    assert evaluate(conn, backup_dir=tmp_path).verdict == "eligible"  # control
    conn.close()


def test_open_lateness_window_blocks_retention(demo, tmp_path):
    back_up(demo, tmp_path)
    end = FLIGHT.end
    early = evaluate(demo, backup_dir=tmp_path, now=end + timedelta(hours=47, minutes=59))
    assert early.verdict == "blocked" and "lateness window open" in early.reasons[0]
    assert evaluate(demo, backup_dir=tmp_path, now=end + timedelta(hours=48)).verdict == "eligible"


# --- backups --------------------------------------------------------------------------


def blob(directory: Path, conn, partition=FLIGHT) -> Path:
    sha = conn.run(
        "SELECT e.sha256 FROM eye.raw_evidence e JOIN eye.capture_batch b USING (batch_id) "
        "WHERE b.source_id = :s AND b.layer = :l ORDER BY b.batch_id LIMIT 1",
        s=partition.source_id,
        l=partition.layer,
    )[0][0]
    return directory / "evidence" / f"{sha}.json"


def test_verified_backup_passes_and_each_backup_fault_blocks(demo, tmp_path):
    assert evaluate(demo, backup_dir=tmp_path).reasons[-1].startswith("no backup")
    assert back_up(demo, tmp_path).state == "verified"
    assert evaluate(demo, backup_dir=tmp_path).verdict == "eligible"  # control

    # No backup directory configured, or unreachable: UNVERIFIED, blocked.
    assert evaluate(demo, backup_dir=None).verdict == "unverified"
    gone = evaluate(demo, backup_dir=tmp_path / "unmounted")
    assert gone.verdict == "unverified" and "UNVERIFIED" in gone.reasons[0]

    # Corrupt copy.
    path = blob(tmp_path, demo)
    good = path.read_bytes()
    path.write_bytes(good[:-2] + b"X}")
    corrupt = evaluate(demo, backup_dir=tmp_path)
    assert corrupt.verdict == "blocked" and any("corrupt" in r for r in corrupt.reasons)
    path.write_bytes(good)
    assert evaluate(demo, backup_dir=tmp_path).verdict == "eligible"  # control

    # Missing copy.
    path.unlink()
    missing = evaluate(demo, backup_dir=tmp_path)
    assert missing.verdict == "blocked" and any("missing" in r for r in missing.reasons)
    path.write_bytes(good)

    # A failed verification is the most recent: blocked until a verified one follows.
    failed = backup.verify(demo, tmp_path, "0" * 64)
    assert failed.state == "failed"
    after_failure = evaluate(demo, backup_dir=tmp_path)
    assert after_failure.verdict == "blocked"
    assert "the most recent backup verification failed" in after_failure.reasons
    assert back_up(demo, tmp_path).state == "verified"
    assert evaluate(demo, backup_dir=tmp_path).verdict == "eligible"  # control


def test_backup_verification_fails_on_a_damaged_or_incomplete_store(demo, tmp_path):
    generation = backup.export(demo, tmp_path, LINES_DIR)
    assert backup.verify(demo, tmp_path, generation).state == "verified"  # control
    path = blob(tmp_path, demo)
    good = path.read_bytes()
    path.write_bytes(b"{}")
    damaged = backup.verify(demo, tmp_path, generation)
    assert damaged.state == "failed" and "corrupt" in damaged.reason
    path.unlink()
    incomplete = backup.verify(demo, tmp_path, generation)
    assert incomplete.state == "failed" and "missing" in incomplete.reason
    path.write_bytes(good)
    gen_file = tmp_path / "generations" / f"{generation}.json"
    gen_file.write_bytes(gen_file.read_bytes() + b" ")
    assert backup.verify(demo, tmp_path, generation).state == "failed"


def test_backup_store_refuses_to_overwrite_a_corrupt_copy(demo, tmp_path):
    backup.export(demo, tmp_path, LINES_DIR)
    blob(tmp_path, demo).write_bytes(b"{}")
    with pytest.raises(backup.BackupError, match="corrupt"):
        backup.export(demo, tmp_path, LINES_DIR)


# --- execution ----------------------------------------------------------------------


def one_flight_batch(conn) -> str:
    return conn.run(
        "SELECT min(batch_id::text) FROM eye.capture_batch "
        "WHERE source_id = 'synthetic-fixture' AND layer = 'flight'"
    )[0][0]


def test_execution_is_disabled_by_default_and_refused_outside_synthetic_demo(demo, tmp_path):
    back_up(demo, tmp_path)
    before = ledger_counts(demo)
    with pytest.raises(retention.RetentionRefused, match="disabled"):
        execute(demo, backup_dir=tmp_path, allow=False)
    with pytest.raises(retention.RetentionRefused, match="never production"):
        execute(demo, backup_dir=tmp_path, mode="production")
    with pytest.raises(retention.RetentionRefused, match="not an approved synthetic"):
        execute(demo, Partition("gdelt", "news", FLIGHT.day), backup_dir=tmp_path)
    assert ledger_counts(demo) == before and unpruned(demo) > 0
    assert demo.run("SELECT count(*) FROM eye.retention_decision")[0][0] == 0


def test_eligible_partition_is_pruned_and_history_survives(demo, tmp_path):
    back_up(demo, tmp_path)
    before = ledger_counts(demo)
    batches = unpruned(demo)
    assert batches > 0
    outcome = execute(demo, backup_dir=tmp_path)
    assert outcome.verdict.verdict == "eligible" and outcome.pruned == batches
    assert unpruned(demo) == 0
    assert ledger_counts(demo) == before  # only bytes went; every row stayed
    assert unpruned(demo, AIS) > 0  # other partitions untouched
    decision = demo.run(
        "SELECT verdict, backup_proof_id IS NOT NULL, cardinality(batch_ids), "
        "cardinality(manifest_ids) FROM eye.retention_decision"
    )
    assert decision == [["pruned", True, batches, 3]]
    assert demo.run("SELECT count(*) FROM eye.manifest_retention_proof")[0][0] == 3
    assert verify_replay(demo) == []
    again = execute(demo, backup_dir=tmp_path)
    assert again.pruned == 0 and "already pruned" in again.verdict.reasons[0]
    # Derivations still work after pruning (they read records, not raw bytes).
    assert {c.state for c in rollups.validate(demo, FLIGHT, LINES)} == {"valid"}


def test_blocked_execution_is_recorded_and_preserves_the_last_good_copy(demo, tmp_path):
    back_up(demo, tmp_path)
    blob(tmp_path, demo).write_bytes(b"{}")
    batches = unpruned(demo)
    outcome = execute(demo, backup_dir=tmp_path)
    assert outcome.pruned == 0 and outcome.verdict.verdict == "blocked"
    assert unpruned(demo) == batches
    recorded = demo.run("SELECT verdict, reasons FROM eye.retention_decision")
    assert recorded[0][0] == "blocked" and any("corrupt" in r for r in recorded[0][1])
    assert demo.run("SELECT count(*) FROM eye.manifest_validation")[0][0] > 0


def test_late_write_after_planning_is_caught_by_the_recheck(demo, tmp_path):
    back_up(demo, tmp_path)
    assert evaluate(demo, backup_dir=tmp_path).verdict == "eligible"  # the plan said yes
    late_dir = tmp_path / "late"
    late_dir.mkdir()
    late = [record("SYNF-0902", "2026-01-01T00:55:00Z", "2026-01-01T00:55:01Z", 0.2)]
    ingest(
        demo,
        capture(
            late_dir,
            "late",
            "flight",
            late,
            start="2026-01-01T00:45:00Z",
            end="2026-01-01T01:00:00Z",
            received="2026-01-03T00:00:00Z",
        ).read_bytes(),
    )
    outcome = execute(demo, backup_dir=tmp_path)
    assert outcome.pruned == 0 and outcome.verdict.verdict == "blocked"
    assert unpruned(demo) > 0


def test_concurrent_late_write_waits_for_the_lock_and_blocks_pruning(demo, tmp_path):
    back_up(demo, tmp_path)
    writer = connect(_same_database_url(demo))
    late_dir = tmp_path / "late"
    late_dir.mkdir()
    late = capture(
        late_dir,
        "late",
        "flight",
        [record("SYNF-0903", "2026-01-01T00:56:00Z", "2026-01-01T00:56:01Z", 0.2)],
        start="2026-01-01T00:45:00Z",
        end="2026-01-01T01:00:00Z",
        received="2026-01-03T00:00:00Z",
    ).read_bytes()
    writer.run("BEGIN")
    with _no_own_transaction():
        archive(writer, late)  # archived, uncommitted: holds a row lock on the batch table
    result: dict = {}

    def run() -> None:
        result["outcome"] = execute(demo, backup_dir=tmp_path)

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(1.0)
    assert worker.is_alive(), "retention did not wait for the in-flight write"
    writer.run("COMMIT")
    worker.join(timeout=60)
    outcome = result["outcome"]
    assert outcome.pruned == 0 and outcome.verdict.verdict == "blocked"
    assert any("pending" in r for r in outcome.verdict.reasons)
    assert unpruned(demo) > 0
    writer.close()


def _same_database_url(conn) -> str:
    import os
    from urllib.parse import urlsplit, urlunsplit

    name = conn.run("SELECT current_database()")[0][0]
    parts = urlsplit(os.environ["EYE_TEST_DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{name}"))


@contextlib.contextmanager
def _no_own_transaction():
    """Let archive() write inside the caller's open transaction (test only)."""
    original = capture_module.transaction
    capture_module.transaction = lambda conn: contextlib.nullcontext(conn)
    try:
        yield
    finally:
        capture_module.transaction = original


def test_database_guard_refuses_pruning_without_a_checked_decision(demo, tmp_path):
    proof = back_up(demo, tmp_path)
    one = one_flight_batch(demo)
    for statement in (
        "DELETE FROM eye.raw_evidence WHERE batch_id = CAST(:b AS uuid)",
        "UPDATE eye.raw_evidence SET content = NULL WHERE batch_id = CAST(:b AS uuid)",
        "UPDATE eye.raw_evidence SET sha256 = repeat('0', 64) WHERE batch_id = CAST(:b AS uuid)",
    ):
        with pytest.raises(DatabaseError, match="not permitted|needs a pruned"):
            demo.run(statement, b=one)
    # A decision recorded in another transaction does not authorise this one.
    decision = demo.run(
        "INSERT INTO eye.retention_decision (source_id, layer, partition_day, verdict, reasons, "
        "batch_ids, manifest_ids, backup_proof_id, evaluated_at) VALUES ('synthetic-fixture', "
        "'flight', '2026-01-01', 'pruned', '[]', ARRAY[CAST(:b AS uuid)], '{}', "
        "CAST(:p AS uuid), now()) RETURNING decision_id::text",
        p=proof.proof_id,
        b=one,
    )[0][0]
    with pytest.raises(DatabaseError, match="needs a pruned decision in this transaction"):
        demo.run(
            "UPDATE eye.raw_evidence SET content = NULL, pruned_by_decision = :d "
            "WHERE batch_id = CAST(:b AS uuid)",
            d=decision,
            b=one,
        )
    # Ledger history stays append-only.
    for statement in (
        "DELETE FROM eye.observation",
        "DELETE FROM eye.event_claim",
        "DELETE FROM eye.media_item",
        "DELETE FROM eye.rollup_value",
        "DELETE FROM eye.derivation_manifest",
        "DELETE FROM eye.retention_decision",
        "DELETE FROM eye.backup_proof",
    ):
        with pytest.raises(DatabaseError, match="not permitted"):
            demo.run(statement)
    assert execute(demo, backup_dir=tmp_path).pruned > 0  # control: the checked path works


def test_bypassing_the_coordinator_still_meets_the_database_guard(demo, tmp_path, monkeypatch):
    """A disposable copy with the Python checks bypassed: the database refuses alone."""
    good = back_up(demo, tmp_path)
    failed = backup.verify(demo, tmp_path, "f" * 64)
    real = retention.evaluate

    def bypass(conn, partition, **kwargs):
        verdict = real(conn, partition, **kwargs)
        verdict.verdict, verdict.reasons, verdict.proof_id = "eligible", [], failed.proof_id
        return verdict

    monkeypatch.setattr(retention, "evaluate", bypass)
    batches = unpruned(demo)
    with pytest.raises(DatabaseError, match="needs a pruned decision"):
        execute(demo, backup_dir=tmp_path)
    assert unpruned(demo) == batches  # rolled back whole, decision included
    assert demo.run("SELECT count(*) FROM eye.retention_decision")[0][0] == 0
    # With the guard trigger also disabled, the negative control would fail:
    # the bytes go although the cited backup failed. This shows the assertion
    # above tests the guard, not an accident of the setup.
    demo.run("ALTER TABLE eye.raw_evidence DISABLE TRIGGER raw_evidence_append_only")
    assert execute(demo, backup_dir=tmp_path).pruned == batches
    assert unpruned(demo) == 0
    assert good.state == "verified"


def test_bypassing_the_lateness_check_shows_its_control_is_real(demo, tmp_path, monkeypatch):
    back_up(demo, tmp_path)
    early = FLIGHT.end + timedelta(hours=1)
    assert execute(demo, backup_dir=tmp_path, now=early).pruned == 0
    monkeypatch.setattr(retention, "timedelta", lambda hours: timedelta(0))
    assert execute(demo, backup_dir=tmp_path, now=early).pruned > 0


def test_replay_reports_missing_bytes_without_a_checked_decision(demo, tmp_path):
    assert verify_replay(demo) == []  # control
    one = one_flight_batch(demo)
    decision = demo.run(
        "INSERT INTO eye.retention_decision (source_id, layer, partition_day, verdict, reasons, "
        "batch_ids, manifest_ids, evaluated_at) VALUES ('synthetic-fixture', 'flight', "
        "'2026-01-01', 'blocked', '[\"test\"]', '{}', '{}', now()) RETURNING decision_id::text"
    )[0][0]
    demo.run("ALTER TABLE eye.raw_evidence DISABLE TRIGGER raw_evidence_append_only")
    demo.run(
        "UPDATE eye.raw_evidence SET content = NULL, pruned_by_decision = :d "
        "WHERE batch_id = CAST(:b AS uuid)",
        d=decision,
        b=one,
    )
    problems = verify_replay(demo)
    assert any("missing without a checked retention decision" in p for p in problems)


# --- restore drill --------------------------------------------------------------------


def test_clean_restore_reproduces_named_metrics(make_db, demo, tmp_path):
    generation = backup.export(demo, tmp_path, LINES_DIR)
    assert backup.verify(demo, tmp_path, generation).state == "verified"
    target = connect(make_db())
    report = backup.restore_drill(tmp_path, generation, target)
    assert report.state == "verified", report.problems
    assert report.batches_restored == 23 and report.metrics_compared > 300
    assert report.manifests_compared == demo.run("SELECT count(*) FROM eye.current_manifest")[0][0]
    assert "SYNTHETIC CODE TEST ONLY" in report.label and "Google Drive" in report.label
    named = backup.named_metrics(target)
    line = f"transit/{LINES[0].line_id}/v{LINES[0].version}"
    assert named[f"{line}/2026-02-01T15:00:00.000000Z/2026-02-01T16:00:00.000000Z"] == [
        "qualified",
        0,
        0,
        0,
    ]
    assert named[f"{line}/2026-02-01T14:00:00.000000Z/2026-02-01T15:00:00.000000Z"][:2] == [
        "unknown",
        None,
    ]
    target.close()


def test_restore_after_pruning_comes_from_the_backup(make_db, demo, tmp_path):
    generation = backup.export(demo, tmp_path, LINES_DIR)
    backup.verify(demo, tmp_path, generation)
    assert execute(demo, backup_dir=tmp_path).pruned > 0
    later = backup.export(demo, tmp_path, LINES_DIR)  # pruned bytes are already in the store
    assert backup.verify(demo, tmp_path, later).state == "verified"
    report = backup.restore_drill(tmp_path, later, connect(make_db()))
    assert report.state == "verified", report.problems


@pytest.mark.parametrize("damage", ["corrupt", "missing", "generation", "metric", "not_empty"])
def test_damaged_or_incomplete_restore_is_never_verified(make_db, demo, tmp_path, damage):
    generation = backup.export(demo, tmp_path, LINES_DIR)
    target = connect(make_db())
    path = blob(tmp_path, demo)
    if damage == "corrupt":
        path.write_bytes(path.read_bytes() + b" ")  # one extra byte
    elif damage == "missing":
        path.unlink()
    elif damage == "generation":
        gen = tmp_path / "generations" / f"{generation}.json"
        gen.write_bytes(gen.read_bytes()[:-1])
    elif damage == "metric":
        doc = json.loads((tmp_path / "generations" / f"{generation}.json").read_bytes())
        name = next(n for n in doc["metrics"] if n.startswith("transit/"))
        doc["metrics"][name] = ["qualified", 9, 9, 18]
        data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
        generation = backup._sha(data)
        (tmp_path / "generations" / f"{generation}.json").write_bytes(data)
    elif damage == "not_empty":
        migrate(target)
    report = backup.restore_drill(tmp_path, generation, target)
    assert report.state == "failed" and report.problems
    target.close()
    # Control: an undamaged generation restores into an empty database.
    fresh = tmp_path / "fresh"
    control = backup.restore_drill(fresh, backup.export(demo, fresh, LINES_DIR), connect(make_db()))
    assert control.state == "verified", control.problems


# --- migration ----------------------------------------------------------------------


ORIGINAL_EVIDENCE = "evidence_id, batch_id, sha256, byte_size, media_type, content"


def test_migration_0007_leaves_earlier_history_unchanged(make_db, tmp_path):
    migrations = REPO_ROOT / "migrations"
    before = tmp_path / "migrations"
    before.mkdir()
    for path in sorted(migrations.glob("000[1-6]_*.sql")):
        shutil.copy(path, before / path.name)
    conn = connect(make_db())
    assert migrate(conn, before) == [1, 2, 3, 4, 5, 6]
    for name in DEMO_DIRS:
        load_fixtures(conn, FIXTURES / name)
    derive_transits(conn)
    tables = {
        "capture_batch": "*",
        "raw_evidence": ORIGINAL_EVIDENCE,
        "observation": "*",
        "observation_receipt": "*",
        "coverage": "*",
        "event_claim": "*",
        "media_item": "*",
        "derivation_run": "*",
        "run_count": "*",
        "feed_change": "*",
    }

    def digest():
        return [
            conn.run(
                f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) "  # noqa: S608
                f"FROM (SELECT {cols} FROM eye.{name}) t"
            )
            for name, cols in tables.items()
        ]

    prior = digest()
    assert migrate(conn) == [7]
    assert digest() == prior
    assert verify_replay(conn) == []
    conn.close()
