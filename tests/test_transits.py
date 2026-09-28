"""Package 2: synthetic AIS tracks, line crossings and transit counts on PostGIS.

Every refusal, unknown or ambiguous outcome sits beside a positive control.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from eye.ingest.capture import archive, ingest, replay_pending, verify_replay
from eye.storage.db import connect
from eye.storage.migrate import migrate
from eye.worker import transits
from pg8000.exceptions import DatabaseError

AIS = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "ais"
LINE = transits.load_line(transits.LINES_DIR / "synthetic-golden-gate.v1.json")
NOON = datetime(2026, 2, 1, 12, tzinfo=UTC)
HOUR = [(NOON, NOON.replace(hour=13))]
HALF = (NOON, NOON.replace(minute=30))
SOURCE = transits.SOURCE_ID


def files(scenario: str, *, reverse: bool = False) -> list[Path]:
    return sorted((AIS / scenario).glob("*.json"), reverse=reverse)


def load(conn, scenario: str, *, reverse: bool = False) -> None:
    for path in files(scenario, reverse=reverse):
        ingest(conn, path.read_bytes())


def derive(conn, intervals=HOUR):
    return transits.store(conn, SOURCE, LINE, intervals)


def count(conn, start=NOON, end=None) -> dict:
    end = end or NOON.replace(hour=13)
    row = conn.run(
        "SELECT state::text, inbound, outbound, total, ambiguous_crossings, insufficient_gaps, "
        "reason, line_id, line_version, algorithm_version, cardinality(coverage_ids) "
        "FROM eye.transit_count WHERE interval_start = :a AND interval_end = :b",
        a=start,
        b=end,
    )[0]
    keys = (
        "state",
        "inbound",
        "outbound",
        "total",
        "ambiguous",
        "insufficient",
        "reason",
        "line_id",
        "line_version",
        "algorithm",
        "coverage_rows",
    )
    return dict(zip(keys, row, strict=True))


def crossings(conn) -> list[tuple]:
    return conn.run(
        "SELECT crossing_id::text, vessel_id, direction, status, reason FROM eye.line_crossing "
        "ORDER BY vessel_id, crossing_time"
    )


@pytest.fixture
def fresh(make_db):
    def _fresh():
        conn = connect(make_db())
        migrate(conn)
        return conn

    return _fresh


@pytest.fixture(scope="module")
def reference_crossing_id() -> str:
    """The crossing id the reference transit derives, computed from evidence alone."""
    from eye.ingest.capture import derive as capture_derive
    from eye.ingest.capture import parse_capture

    parsed = parse_capture(files("transit")[0].read_bytes())
    points = [
        transits.Point(
            oid, row[1], datetime.fromisoformat(row[4]), row[6], row[7], True, (parsed.batch_id,)
        )
        for oid, row in capture_derive(parsed).observations.items()
    ]
    d = transits.derive(transits.Inputs(points, [], "x"), LINE, [])
    (crossing_id,) = [k for k, c in d.crossings.items() if c[0] == "SYNV-0001"]
    return crossing_id


# --- the positive control --------------------------------------------------------


def test_valid_transit_is_counted_once_with_its_evidence(fresh, reference_crossing_id):
    conn = fresh()
    load(conn, "transit")
    derive(conn)
    assert count(conn) | {"reason": None} == {
        "state": "qualified",
        "inbound": 1,
        "outbound": 0,
        "total": 1,
        "ambiguous": 0,
        "insufficient": 0,
        "reason": None,
        "line_id": "synthetic-golden-gate",
        "line_version": 1,
        "algorithm": "transit-counter/1",
        "coverage_rows": 1,
    }
    assert crossings(conn) == [[reference_crossing_id, "SYNV-0001", "inbound", "definite", None]]
    # The crossing names the observations that bracket it, their batch, and an
    # interpolated (estimated) time between their observed times.
    row = conn.run(
        "SELECT eye.iso_utc(c.crossing_time), eye.iso_utc(b.observed_time), "
        "eye.iso_utc(a.observed_time), c.time_method, cardinality(c.evidence_batch_ids), "
        "c.line_version, c.algorithm_version FROM eye.line_crossing c "
        "JOIN eye.observation b ON b.observation_id = c.before_observation_id "
        "JOIN eye.observation a ON a.observation_id = c.after_observation_id"
    )[0]
    assert row[1] < row[0] < row[2]
    assert row[3:] == ["linear_interpolation", 1, 1, "transit-counter/1"]
    assert verify_replay(conn) == [] and transits.verify(conn, SOURCE, LINE) == []


def test_measured_zero_needs_coverage_of_the_line(fresh):
    quiet = fresh()
    load(quiet, "quiet")
    derive(quiet)
    assert count(quiet)["state"] == "qualified" and count(quiet)["total"] == 0
    elsewhere = fresh()
    load(elsewhere, "wrong-area")  # same data, but the capture area misses the line
    derive(elsewhere)
    result = count(elsewhere)
    assert (result["state"], result["total"]) == ("unknown", None)
    assert "missing" in result["reason"]


def test_crossings_agree_with_postgis_geometry(fresh):
    """Independent check: every definite crossing's bracketing segment meets the line."""
    conn = fresh()
    for scenario in ("transit", "reversal", "ambiguous"):
        load(conn, scenario)
    derive(conn)
    rows = conn.run(
        "SELECT c.status, ST_Intersects(l.geometry, ST_MakeLine(b.position, a.position)) "
        "FROM eye.line_crossing c JOIN eye.count_line l USING (line_id) "
        "JOIN eye.observation b ON b.observation_id = c.before_observation_id "
        "JOIN eye.observation a ON a.observation_id = c.after_observation_id "
        "WHERE l.version = c.line_version"
    )
    assert rows and all(meets for status, meets in rows if status == "definite")


# --- duplicates, crash/retry, late arrival --------------------------------------


def test_duplicate_deliveries_do_not_duplicate_the_count(fresh, reference_crossing_id):
    conn = fresh()
    load(conn, "duplicate")
    derive(conn)
    assert count(conn)["total"] == 1 and count(conn)["state"] == "qualified"
    (row,) = crossings(conn)
    assert row[0] == reference_crossing_id
    receipts = conn.run("SELECT cardinality(evidence_batch_ids) FROM eye.line_crossing")
    assert receipts == [[2]]  # both deliveries are cited as evidence, counted once


def test_crash_and_retry_neither_lose_nor_duplicate(fresh, reference_crossing_id):
    conn = fresh()
    timeout, retry = (p.read_bytes() for p in files("retry"))
    ingest(conn, timeout)
    derive(conn)
    assert count(conn)["state"] == "unknown" and count(conn)["total"] is None

    archive(conn, retry)  # crash: archived, never committed
    derive(conn)
    assert count(conn)["state"] == "unknown"

    replay_pending(conn)  # recovery
    derive(conn)
    assert (count(conn)["state"], count(conn)["total"]) == ("qualified", 1)
    assert [r[0] for r in crossings(conn)] == [reference_crossing_id]

    before = transits.read_stored(conn, SOURCE, LINE)
    ingest(conn, retry)  # the retry is delivered again
    derive(conn)
    assert transits.read_stored(conn, SOURCE, LINE) == before


def test_late_arrival_resolves_an_ambiguous_crossing_in_any_order(fresh, reference_crossing_id):
    conn = fresh()
    first, backfill = (p.read_bytes() for p in files("late"))
    ingest(conn, first)
    derive(conn)
    early = count(conn)
    assert (early["state"], early["total"], early["ambiguous"]) == ("partial", 0, 1)
    assert "lower bound" in early["reason"]
    ingest(conn, backfill)
    derive(conn)
    assert (count(conn)["state"], count(conn)["total"]) == ("qualified", 1)
    assert [r[0] for r in crossings(conn)] == [reference_crossing_id]
    # Both runs are kept, with the evidence each one used.
    runs = conn.run(
        "SELECT summary->'intervals'->0->>'state' FROM eye.derivation_run ORDER BY derived_at"
    )
    assert runs == [["partial"], ["qualified"]]

    reversed_order = fresh()
    load(reversed_order, "late", reverse=True)
    derive(reversed_order)
    assert transits.read_stored(reversed_order, SOURCE, LINE) == transits.read_stored(
        conn, SOURCE, LINE
    )


# --- reversals, gaps, ambiguity, identity and quality ----------------------------


def test_reversal_counts_both_directions_and_jitter_does_not(fresh, monkeypatch):
    conn = fresh()
    load(conn, "reversal")
    derive(conn)
    result = count(conn)
    assert (result["state"], result["inbound"], result["outbound"], result["total"]) == (
        "qualified",
        1,
        1,
        2,
    )
    assert {r[1] for r in crossings(conn)} == {"SYNV-0003"}
    # Control: without the 50 m band, SYNV-0004's jitter would count as two crossings.
    inputs = transits.read_inputs(conn, SOURCE, LINE)
    monkeypatch.setattr(transits, "BAND_M", 0.0)
    unbanded = transits.derive(inputs, LINE, HOUR)
    assert sum(c[0] == "SYNV-0004" for c in unbanded.crossings.values()) == 2


def test_gaps_far_from_the_line_do_not_matter(fresh):
    conn = fresh()
    load(conn, "gap-far")
    derive(conn)
    assert (count(conn)["state"], count(conn)["total"]) == ("qualified", 1)
    assert conn.run("SELECT reason, effect FROM eye.track_gap") == [["time_gap", "none"]]


def test_gap_near_the_line_makes_the_count_unknown(fresh):
    conn = fresh()
    load(conn, "gap-near")
    derive(conn)
    result = count(conn)
    assert (result["state"], result["total"], result["insufficient"]) == ("unknown", None, 1)
    assert conn.run("SELECT reason, effect FROM eye.track_gap") == [
        ["time_gap", "insufficient_evidence"]
    ]


def test_gap_across_the_line_is_an_ambiguous_crossing(fresh):
    conn = fresh()
    load(conn, "gap-straddle")
    derive(conn)
    result = count(conn)
    assert (result["state"], result["total"], result["ambiguous"]) == ("partial", 0, 1)
    assert [r[2:] for r in crossings(conn)] == [
        ["inbound", "ambiguous", "time gap straddles the line"]
    ]


def test_crossing_near_a_line_end_is_ambiguous(fresh):
    conn = fresh()
    load(conn, "ambiguous")
    derive(conn)
    result = count(conn)
    assert (result["state"], result["total"], result["ambiguous"]) == ("partial", 1, 1)
    by_vessel = {r[1]: r[3] for r in crossings(conn)}
    assert by_vessel == {"SYNV-0009": "ambiguous", "SYNV-0010": "definite"}  # 0011 passes by


def test_identity_or_quality_jump_is_never_a_definite_crossing(fresh):
    conn = fresh()
    load(conn, "jump")
    derive(conn)
    statuses = sorted((r[1], r[2], r[3]) for r in crossings(conn))
    assert statuses == [
        ("SYNV-0001", "inbound", "definite"),
        ("SYNV-0012", "inbound", "ambiguous"),
        ("SYNV-0012", "outbound", "ambiguous"),
    ]
    assert (count(conn)["state"], count(conn)["total"]) == ("partial", 1)


def test_low_accuracy_positions_are_excluded(fresh):
    conn = fresh()
    raw = files("quality")[0].read_bytes()
    ingest(conn, raw)
    derive(conn)
    assert (count(conn)["state"], count(conn)["total"]) == ("qualified", 1)
    # Control: the same outlier reported with high accuracy would add two crossings.
    doc = json.loads(raw)
    for message in doc["provider_response"]["messages"]:
        message["accuracy"] = "high"
    other = fresh()
    ingest(other, json.dumps(doc).encode())
    transits.store(other, SOURCE, LINE, HOUR)
    assert count(other)["total"] == 3


# --- outage and unknown ------------------------------------------------------------


def test_outage_makes_the_count_unknown_never_zero(fresh):
    conn = fresh()
    load(conn, "outage")
    derive(conn, HOUR + [HALF])
    observed = count(conn, *HALF)
    assert (observed["state"], observed["total"]) == ("qualified", 1)  # control
    whole = count(conn)
    assert (whole["state"], whole["inbound"], whole["total"]) == ("unknown", None, None)
    assert "failed" in whole["reason"]


def test_database_refuses_a_zero_for_an_unknown_count(fresh):
    conn = fresh()
    load(conn, "quiet")
    run_id, _ = derive(conn)
    insert = (
        "INSERT INTO eye.transit_count (count_id, run_id, source_id, line_id, line_version, "
        "algorithm_version, interval_start, interval_end, state, inbound, outbound, total, "
        "ambiguous_crossings, insufficient_gaps, reason, coverage_ids, crossing_ids) VALUES "
        "(gen_random_uuid(), CAST(:run AS uuid), 'synthetic-ais', 'synthetic-golden-gate', 1, "
        "'probe', '2026-02-01T14:00:00Z', '2026-02-01T15:00:00Z', "
        "CAST(:state AS eye.coverage_state), :n, :n, :n, 0, 0, :reason, '{}', '{}')"
    )
    with pytest.raises(DatabaseError, match="transit_count_missing_is_null"):
        conn.run(insert, run=run_id, state="unknown", n=0, reason="outage")
    conn.run(insert, run=run_id, state="unknown", n=None, reason="outage")  # control


def test_interval_without_any_capture_is_unknown(fresh):
    conn = fresh()
    load(conn, "transit")
    later = (NOON.replace(hour=14), NOON.replace(hour=15))
    derive(conn, HOUR + [later])
    assert count(conn)["state"] == "qualified"  # control
    result = count(conn, *later)
    assert (result["state"], result["total"]) == ("unknown", None)


# --- rebuild, replay and tampering ---------------------------------------------------


ALL_SCENARIOS = sorted(p.name for p in AIS.iterdir() if p.is_dir())


def test_rebuild_and_replay_reproduce_ids_and_counts(fresh):
    stored = []
    for reverse in (False, True):
        conn = fresh()
        for scenario in ("transit", "reversal", "late", "ambiguous", "jump"):
            load(conn, scenario, reverse=reverse)
        derive(conn)
        derive(conn)  # re-running on the same evidence is a no-op
        assert conn.run("SELECT count(*) FROM eye.derivation_run") == [[1]]
        assert verify_replay(conn) == [] and transits.verify(conn, SOURCE, LINE) == []
        stored.append(transits.read_stored(conn, SOURCE, LINE))
    assert stored[0] == stored[1]
    assert stored[0].crossings and stored[0].counts


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_every_scenario_verifies_clean(fresh, scenario):
    conn = fresh()
    load(conn, scenario)
    derive(conn)
    assert verify_replay(conn) == []
    assert transits.verify(conn, SOURCE, LINE) == []


@pytest.mark.parametrize(
    ("statement", "label"),
    [
        (
            "UPDATE eye.line_crossing SET crossing_time = crossing_time + interval '1 minute'",
            "crossings",
        ),
        ("UPDATE eye.line_crossing SET status = 'ambiguous', reason = 'x'", "crossings"),
        ("UPDATE eye.transit_count SET inbound = 2, total = 2", "counts"),
        ("DELETE FROM eye.track_gap", "gaps"),
        (
            "UPDATE eye.vessel_track SET point_count = point_count - 1, "
            "observation_ids = observation_ids[1:point_count - 1]",
            "tracks",
        ),
    ],
)
def test_verification_detects_changed_derived_values(fresh, statement, label):
    conn = fresh()
    load(conn, "gap-far")
    derive(conn)
    assert transits.verify(conn, SOURCE, LINE) == []  # control
    conn.run(statement)
    problems = transits.verify(conn, SOURCE, LINE)
    assert any(p.startswith(label) for p in problems), problems


def test_verification_reports_results_made_stale_by_late_evidence(fresh):
    conn = fresh()
    first, backfill = (p.read_bytes() for p in files("late"))
    ingest(conn, first)
    derive(conn)
    assert transits.verify(conn, SOURCE, LINE) == []
    ingest(conn, backfill)  # new evidence, not yet derived
    assert transits.verify(conn, SOURCE, LINE)
    derive(conn)
    assert transits.verify(conn, SOURCE, LINE) == []


# --- the line is versioned and immutable ---------------------------------------------


def test_line_version_is_immutable(fresh, tmp_path):
    conn = fresh()
    transits.register_line(conn, LINE)
    transits.register_line(conn, LINE)  # control: same definition again
    doc = json.loads((transits.LINES_DIR / "synthetic-golden-gate.v1.json").read_text())
    doc["coordinates"][1][1] = 0.06
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(doc))
    with pytest.raises(transits.LineDefinitionError, match="new version"):
        transits.register_line(conn, transits.load_line(changed))
    doc["version"] = 2
    changed.write_text(json.dumps(doc))
    transits.register_line(conn, transits.load_line(changed))  # a new version is accepted
    assert conn.run("SELECT version FROM eye.count_line ORDER BY 1") == [[1], [2]]


def test_non_synthetic_line_is_refused(tmp_path):
    doc = json.loads((transits.LINES_DIR / "synthetic-golden-gate.v1.json").read_text())
    transits.load_line(transits.LINES_DIR / "synthetic-golden-gate.v1.json")  # control
    doc["synthetic"] = False
    path = tmp_path / "real.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(transits.LineDefinitionError, match="only synthetic"):
        transits.load_line(path)


# --- adapter ---------------------------------------------------------------------------


def test_ais_adapter_refuses_bad_messages_and_accepts_good_ones(fresh):
    conn = fresh()
    doc = json.loads(files("transit")[0].read_bytes())
    messages = doc["provider_response"]["messages"]
    messages[0]["received_time"] = messages[0]["sent_time"]  # provider-supplied receipt
    messages[1]["mmsi"] = "366123456"  # looks like a real MMSI, not synthetic
    messages[2]["sent_time"] = "2026-02-01T11:00:00Z"  # sent before measured
    result = ingest(conn, json.dumps(doc).encode())
    assert (result.accepted, result.rejected) == (len(messages) - 3, 3)


def test_generated_fixtures_are_current():
    result = subprocess.run(
        [sys.executable, "scripts/gen_ais_fixtures.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
