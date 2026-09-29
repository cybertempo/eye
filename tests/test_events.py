"""Package 4c on PostGIS: the append-only event-claim ledger.

Every negative case has a sourced or ordinary control next to it:

* a lost flight signal, an AIS gap and a road slowdown never become an
  accident, casualty or collision (refused at capture, refused by the
  database, and never labelled so by the API), beside sourced reports that do;
* duplicate delivery adds a receipt, not a claim, beside a real correction;
* a corrected location keeps every version, beside an uncorrected case;
* conflicting claims stay unresolved until a later claim resolves them;
* replay re-derives every claim, and a stray row is caught;
* where no event source reports, events are unknown, never "none";
* links to tracks are conservative: ambiguous, distant or unobserved matches
  stay unlinked, beside one unique match.
"""

from __future__ import annotations

import copy
import json
import shutil

import jsonschema
import pytest
from conftest import AIS_DEMO, CAPTURES, REPO_ROOT
from eye.api import feed
from eye.ingest.capture import ingest, load_fixtures, verify_replay
from eye.storage.db import connect
from eye.storage.migrate import MIGRATIONS_DIR, migrate
from eye.wire import SCHEMA_PATH, validate_message
from pg8000.exceptions import DatabaseError
from synthetic_events import DAY, area, capture, claim, point, segment
from ws_client import WsClient, subscribe

EVENTS_DEMO = REPO_ROOT / "tests" / "fixtures" / "synthetic" / "events" / "demo"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VIEW = "/api/v0/snapshot?start=2026-02-01T12:00:00Z&end=2026-02-01T16:00:00Z"
AREA = (-0.5, -0.5, 0.5, 0.5)
HOURS = {"start": DAY + "12:00:00Z", "end": DAY + "16:00:00Z"}
ACCIDENT_KINDS = {"aviation_accident", "marine_casualty", "road_collision"}
SYNV20 = "track:synthetic-ais:vessel:SYNV-0020"
SYNV21 = "track:synthetic-ais:vessel:SYNV-0021"
FLT900 = "track:synthetic-fixture:flight:SYN-FLT-900"


def reference_valid(message: dict) -> bool:
    document = copy.deepcopy(SCHEMA)
    document["$ref"] = "#/$defs/ServerMessage"
    return jsonschema.Draft202012Validator(document).is_valid(message)


def snapshot(api, path: str = VIEW) -> dict:
    response, body = api.get(path)
    assert response.status == 200, body
    message = json.loads(body)
    assert validate_message(message, "ServerMessage") and reference_valid(message)
    return message


def case_of(message: dict, case: str) -> dict:
    (found,) = [e for e in message["events"] if e["case_id"] == case]
    return found


def batch_of(api) -> str:
    return api.conn.run(
        "SELECT batch_id::text FROM eye.capture_batch ORDER BY archived_at DESC LIMIT 1"
    )[0][0]


def event_coverage(message: dict, layer: str) -> list[tuple]:
    return [
        (c["interval"]["start"], c["interval"]["end"], c["state"], c["metric"]["value"])
        for c in message["coverage"]
        if c["layer"] == layer and c["metric"]["name"] == "event_cases_in_view"
    ]


# --- motion data never becomes an accident ------------------------------------------------

NEGATIVES = {
    # layer: (motion candidate kind, accident kind, sourced report control)
    "flight": ("signal_lost", "aviation_accident"),
    "vessel": ("ais_gap", "marine_casualty"),
    "road": ("traffic_slowdown", "road_collision"),
}


@pytest.mark.parametrize("layer", sorted(NEGATIVES))
def test_motion_inference_is_only_a_review_candidate(api_server, tmp_path, layer):
    candidate, accident = NEGATIVES[layer]
    api = api_server(ais_files=[])
    here = point(0.1, 0.1)
    promoted = [
        # Motion data claiming an accident, a final status, or citing a report.
        claim(
            "NEG-1", accident, DAY + "12:30:00Z", here, basis="motion_inference", status="candidate"
        ),
        claim(
            "NEG-2", candidate, DAY + "12:30:00Z", here, basis="motion_inference", status="final"
        ),
        claim(
            "NEG-3",
            candidate,
            DAY + "12:30:00Z",
            here,
            basis="motion_inference",
            status="candidate",
            evidence="synthetic-doc:x",
        ),
        # A sourced report of a motion kind, and a report citing no evidence.
        claim("NEG-4", candidate, DAY + "12:30:00Z", here),
        claim("NEG-5", accident, DAY + "12:30:00Z", here, evidence=None),
    ]
    good = [
        claim(
            "CAND-1",
            candidate,
            DAY + "12:30:00Z",
            here,
            basis="motion_inference",
            status="candidate",
        ),
        claim("REP-1", accident, DAY + "12:40:00Z", point(0.2, 0.1), status="final"),
    ]
    api.ingest(capture(tmp_path, "mixed", layer, promoted + good))
    accepted, rejected = api.conn.run(
        "SELECT accepted_count, rejected_count FROM eye.capture_batch ORDER BY archived_at DESC "
        "LIMIT 1"
    )[0]
    assert (accepted, rejected) == (2, 5)
    stored = api.conn.run("SELECT case_id, basis::text, kind FROM eye.event_claim ORDER BY 1")
    assert stored == [
        ["CAND-1", "motion_inference", candidate],
        ["REP-1", "official_report", accident],
    ]
    message = snapshot(api)
    cand, report = case_of(message, "CAND-1"), case_of(message, "REP-1")
    assert cand["standing"] == "review_candidate"
    assert {c["kind"] for c in cand["claims"]} == {candidate}
    assert {c["status"] for c in cand["claims"]} == {"candidate"}
    # Control: the sourced report is an accident, with its evidence.
    assert report["standing"] == "report"
    (current,) = report["claims"]
    assert current["kind"] == accident and current["status"] == "final"
    assert current["evidence_ref"].startswith("synthetic-doc:REP-1")
    # Nothing a candidate says reaches an accident kind anywhere in the message.
    for event in message["events"]:
        if event["standing"] == "review_candidate":
            assert not {c["kind"] for c in event["claims"]} & ACCIDENT_KINDS


def test_database_refuses_motion_data_as_an_accident(db):
    """A second mechanism: the constraint, not the parser, refuses these rows."""
    columns = (
        "INSERT INTO eye.event_claim (claim_id, source_id, layer, case_id, basis, kind, status, "
        "source_published_time, location_type, location, precision_m, evidence_ref, "
        "content_sha256) VALUES (gen_random_uuid(), 'synthetic-events', :layer, 'X', "
        "CAST(:basis AS eye.claim_basis), :kind, CAST(:status AS eye.claim_status), now(), "
        "'point', ST_SetSRID(ST_MakePoint(0, 0), 4326), 10, :ref, repeat('a', 64))"
    )
    for layer, basis, kind, status, ref in (
        ("flight", "motion_inference", "aviation_accident", "candidate", None),
        ("vessel", "motion_inference", "ais_gap", "final", None),
        ("road", "motion_inference", "traffic_slowdown", "candidate", "doc"),
        ("road", "official_report", "traffic_slowdown", "reported", "doc"),
        ("road", "official_report", "road_collision", "reported", None),
    ):
        with pytest.raises(DatabaseError, match="motion_inference_is_only_a_candidate"):
            db.run(columns, layer=layer, basis=basis, kind=kind, status=status, ref=ref)
    # Controls: a candidate and a sourced report are accepted.
    db.run(
        columns,
        layer="road",
        basis="motion_inference",
        kind="traffic_slowdown",
        status="candidate",
        ref=None,
    )
    db.run(
        columns,
        layer="road",
        basis="official_report",
        kind="road_collision",
        status="final",
        ref="doc",
    )
    assert db.run("SELECT count(*) FROM eye.event_claim") == [[2]]


def test_signal_loss_gap_and_slowdown_alone_create_no_event(api_server, tmp_path):
    """Position data only: a flight whose reports stop, the demo AIS gap
    (SYNV-0021 is last seen at 12:29) and slow road observations. No event
    source reported, so events are unknown, not absent, and nothing is an
    accident. Control: sourced reports for the same places are served."""
    from synthetic_captures import capture as positions
    from synthetic_captures import record

    api = api_server()
    window = {"start": DAY + "12:00:00Z", "end": DAY + "13:00:00Z"}
    api.ingest(
        positions(
            tmp_path,
            "flight-stops",
            "flight",
            [
                record("SYN-FLT-800", DAY + f"12:0{m}:00Z", DAY + f"12:0{m}:05Z", 0.02 * m, 0.3)
                for m in range(5)
            ],
            **window,
            received=DAY + "13:00:05Z",
        )
    )
    api.ingest(
        positions(
            tmp_path,
            "road-slow",
            "road",
            [
                record(
                    "SYN-RD-800", DAY + f"12:{m}0:00Z", DAY + f"12:{m}0:05Z", -0.3 + 0.001 * m, -0.2
                )
                for m in range(5)
            ],
            **window,
            received=DAY + "13:00:06Z",
        )
    )
    before = snapshot(api)
    assert before["events"] == []
    for layer in ("flight", "vessel", "road"):
        assert event_coverage(before, layer) == [
            (DAY + "12:00:00Z", DAY + "16:00:00Z", "unknown", None)
        ]
    # Control: sourced reports for each layer are events, with their kinds.
    for layer, kind, where in (
        ("flight", "aviation_accident", point(0.09, 0.3)),
        ("vessel", "marine_casualty", point(0.15, 0.06)),
        ("road", "road_collision", segment([(-0.3, -0.2), (-0.29, -0.2)])),
    ):
        api.ingest(
            capture(
                tmp_path,
                f"report-{layer}",
                layer,
                [claim(f"POS-{layer}", kind, DAY + "12:50:00Z", where, status="final")],
            )
        )
    after = snapshot(api)
    kinds = {e["case_id"]: (e["standing"], e["claims"][0]["kind"]) for e in after["events"]}
    assert kinds == {
        "POS-flight": ("report", "aviation_accident"),
        "POS-vessel": ("report", "marine_casualty"),
        "POS-road": ("report", "road_collision"),
    }
    assert event_coverage(after, "road")[0] == (
        DAY + "12:00:00Z",
        DAY + "13:00:00Z",
        "qualified",
        1,
    )


# --- duplicate delivery, corrections, conflicts --------------------------------------------


def test_duplicate_delivery_adds_a_receipt_not_a_claim(api_server, tmp_path):
    api = api_server()
    report = claim(
        "DUP-1", "road_closure", DAY + "12:20:00Z", segment([(-0.2, -0.2), (-0.1, -0.2)])
    )
    api.ingest(capture(tmp_path, "first", "road", [report], received=DAY + "13:00:10Z"))
    first = batch_of(api)
    api.ingest(capture(tmp_path, "again", "road", [report], received=DAY + "13:00:20Z"))
    again = batch_of(api)
    assert first != again
    assert api.conn.run("SELECT count(*) FROM eye.event_claim") == [[1]]
    assert api.conn.run("SELECT count(*) FROM eye.event_claim_receipt") == [[2]]
    (only,) = case_of(snapshot(api), "DUP-1")["claims"]
    assert sorted(only["evidence_batch_ids"]) == sorted([first, again])
    assert only["received_time"] == DAY + "13:00:10Z"  # the first receipt
    # Control: changed content from the source is a new version, not a duplicate.
    moved = claim("DUP-1", "road_closure", DAY + "12:40:00Z", segment([(-0.2, -0.3), (-0.1, -0.3)]))
    api.ingest(capture(tmp_path, "moved", "road", [moved], received=DAY + "13:00:30Z"))
    assert [c["version"] for c in case_of(snapshot(api), "DUP-1")["claims"]] == [1, 2]


def test_corrected_location_keeps_every_version(api_server, tmp_path):
    api = api_server()
    window = (DAY + "12:30:00Z", DAY + "13:00:00Z")
    api.ingest(
        capture(
            tmp_path,
            "initial",
            "vessel",
            [
                claim(
                    "COR-1",
                    "marine_casualty",
                    DAY + "12:55:00Z",
                    area(0.40, 0.0),
                    event_time=DAY + "12:52:00Z",
                    identifiers=[SYNV20],
                    window=window,
                ),
                claim("STAY-1", "vessel_distress", DAY + "12:56:00Z", point(0.3, 0.05)),
            ],
        )
    )
    api.ingest(
        capture(
            tmp_path,
            "revised",
            "vessel",
            [
                claim(
                    "COR-1",
                    "marine_casualty",
                    DAY + "13:30:00Z",
                    area(0.41, 0.01, 0.005, 500),
                    status="preliminary",
                    event_time=DAY + "12:51:00Z",
                    identifiers=[SYNV20],
                    window=window,
                )
            ],
            start=DAY + "13:00:00Z",
            end=DAY + "14:00:00Z",
        )
    )
    corrected = case_of(snapshot(api), "COR-1")
    v1, v2 = corrected["claims"]
    assert (v1["version"], v1["is_current"], v2["version"], v2["is_current"]) == (1, False, 2, True)
    assert corrected["current_claim_id"] == v2["claim_id"] and corrected["standing"] == "report"
    assert v1["reported_event_location"] != v2["reported_event_location"]
    assert v2["reported_event_location"]["precision_m"] == 500
    assert (v1["status"], v2["status"]) == ("reported", "preliminary")
    # The last observed position comes from the track, not from either claim.
    last = corrected["last_observed_position"]
    assert corrected["link"]["state"] == "linked" and last["source_record_id"] == "SYNV-0020"
    assert last["observed_time"] <= DAY + "12:53:00Z"
    assert [last["lon"], last["lat"]] not in v2["reported_event_location"]["coords"]
    stored = api.conn.run(
        "SELECT o.observed_time <= :t FROM eye.observation o WHERE o.observation_id = "
        "CAST(:o AS uuid)",
        t=DAY + "12:53:00Z",
        o=last["observation_id"],
    )
    assert stored == [[True]]
    # Control: an uncorrected case has a single, current version.
    (only,) = case_of(snapshot(api), "STAY-1")["claims"]
    assert only["is_current"] is True and only["version"] == 1


def test_conflicting_claims_stay_unresolved_until_a_later_claim(api_server, tmp_path):
    api = api_server()
    same_time = DAY + "12:30:00Z"
    window = (DAY + "12:00:00Z", DAY + "12:50:00Z")
    api.ingest(
        capture(
            tmp_path,
            "conflict",
            "vessel",
            [
                claim(
                    "CON-1",
                    "vessel_distress",
                    same_time,
                    point(0.2, 0.0),
                    identifiers=[SYNV20],
                    window=window,
                ),
                claim(
                    "CON-1",
                    "vessel_distress",
                    same_time,
                    point(0.3, 0.05),
                    identifiers=[SYNV20],
                    window=window,
                ),
            ],
        )
    )
    unresolved = case_of(snapshot(api), "CON-1")
    assert unresolved["standing"] == "unresolved" and unresolved["current_claim_id"] is None
    assert [c["is_current"] for c in unresolved["claims"]] == [None, None]
    assert {tuple(c["reported_event_location"]["coords"]) for c in unresolved["claims"]} == {
        (0.2, 0.0),
        (0.3, 0.05),
    }
    assert unresolved["link"]["state"] == "unlinked"
    assert "conflict" in unresolved["link"]["reason"]
    assert unresolved["last_observed_position"] is None
    # Control: a later, unique claim resolves it.
    api.ingest(
        capture(
            tmp_path,
            "resolution",
            "vessel",
            [
                claim(
                    "CON-1",
                    "vessel_distress",
                    DAY + "12:45:00Z",
                    point(0.25, 0.02),
                    status="final",
                    identifiers=[SYNV20],
                    window=window,
                )
            ],
            received=DAY + "13:00:20Z",
        )
    )
    resolved = case_of(snapshot(api), "CON-1")
    assert resolved["standing"] == "report" and len(resolved["claims"]) == 3
    assert [c["is_current"] for c in resolved["claims"]] == [False, False, True]


# --- links to tracks -----------------------------------------------------------------------


def test_links_are_conservative(api_server, tmp_path):
    api = api_server()
    window = (DAY + "12:00:00Z", DAY + "13:00:00Z")
    near20 = point(0.39, 0.0)
    api.ingest(
        capture(
            tmp_path,
            "links",
            "vessel",
            [
                # One named track, observed in the window, near the site: linked.
                claim(
                    "LNK-ONE",
                    "vessel_distress",
                    DAY + "12:40:00Z",
                    near20,
                    identifiers=[SYNV20],
                    window=window,
                ),
                # Two named tracks, both observed: ambiguous, not linked.
                claim(
                    "LNK-TWO",
                    "vessel_distress",
                    DAY + "12:40:00Z",
                    near20,
                    identifiers=[SYNV20, SYNV21],
                    window=window,
                ),
                # Named track not observed in the window.
                claim(
                    "LNK-LATE",
                    "vessel_distress",
                    DAY + "12:40:00Z",
                    near20,
                    identifiers=["track:synthetic-ais:vessel:SYNV-0024"],
                    window=window,
                ),
                # Named track observed, but far from the reported site.
                claim(
                    "LNK-FAR",
                    "vessel_distress",
                    DAY + "12:40:00Z",
                    point(-0.45, -0.45, 100),
                    identifiers=[SYNV20],
                    window=window,
                ),
                # A flight identifier on a vessel case, and an unmatched scheme.
                claim(
                    "LNK-LAYER",
                    "vessel_distress",
                    DAY + "12:40:00Z",
                    near20,
                    identifiers=["track:synthetic-ais:flight:SYNV-0020", "mmsi:000000001"],
                    window=window,
                ),
            ],
        )
    )
    message = snapshot(api)
    one = case_of(message, "LNK-ONE")
    assert one["link"]["state"] == "linked"
    assert one["link"]["track_id"] == feed.track_id("synthetic-ais", "vessel", "SYNV-0020")
    assert one["last_observed_position"]["track_id"] == one["link"]["track_id"]
    two = case_of(message, "LNK-TWO")
    assert two["link"]["state"] == "ambiguous" and two["link"]["track_id"] is None
    assert len(two["link"]["candidate_track_ids"]) == 2 and two["last_observed_position"] is None
    for case, reason in (
        ("LNK-LATE", "no observed track"),
        ("LNK-FAR", "km from the reported location"),
        ("LNK-LAYER", "names no EYE track"),
    ):
        event = case_of(message, case)
        assert event["link"]["track_id"] is None and event["last_observed_position"] is None
        assert reason in event["link"]["reason"], (case, event["link"])


# --- replay, append-only, unknown coverage ------------------------------------------------


def test_replay_rederives_every_claim_and_catches_a_stray_row(make_db):
    ids = []
    for reverse in (False, True):
        conn = connect(make_db())
        migrate(conn)
        load_fixtures(conn, CAPTURES)
        load_fixtures(conn, AIS_DEMO)
        load_fixtures(conn, EVENTS_DEMO, reverse=reverse)
        assert verify_replay(conn) == []
        ids.append(
            conn.run(
                "SELECT claim_id::text, case_id, version, is_current FROM eye.event_claim_version "
                "ORDER BY claim_id"
            )
        )
        if reverse:
            conn.run(
                "INSERT INTO eye.event_claim (claim_id, source_id, layer, case_id, basis, kind, "
                "status, source_published_time, location_type, location, precision_m, "
                "evidence_ref, content_sha256) VALUES (gen_random_uuid(), 'synthetic-events', "
                "'road', 'STRAY', 'official_report', 'road_closure', 'reported', now(), 'point', "
                "ST_SetSRID(ST_MakePoint(0, 0), 4326), 10, 'doc', repeat('a', 64))"
            )
            problems = verify_replay(conn)
            assert len(problems) == 1 and "not derivable" in problems[0]
        conn.close()
    assert ids[0] == ids[1] and len(ids[0]) == 10  # same ids and versions in either order


def test_claims_and_receipts_are_append_only(db):
    load_fixtures(db, EVENTS_DEMO)
    assert db.run("SELECT count(*) FROM eye.event_claim") == [[10]]  # control: ingest wrote
    for statement in (
        "UPDATE eye.event_claim SET status = 'final'",
        "DELETE FROM eye.event_claim",
        "UPDATE eye.event_claim_receipt SET adapter_version = 'x'",
        "DELETE FROM eye.event_claim_receipt",
    ):
        with pytest.raises(DatabaseError, match="not permitted"):
            db.run(statement)


def test_missing_event_source_is_unknown_never_none(api_server, tmp_path):
    api = api_server()
    api.ingest(capture(tmp_path, "zero", "road", []))  # healthy and empty: measured zero
    api.ingest(
        capture(
            tmp_path,
            "timeout",
            "road",
            [],
            start=DAY + "13:00:00Z",
            end=DAY + "14:00:00Z",
            status="timeout",
        )
    )
    message = snapshot(api)
    assert event_coverage(message, "road") == [
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 0),
        (DAY + "13:00:00Z", DAY + "14:00:00Z", "failed", None),
        (DAY + "14:00:00Z", DAY + "16:00:00Z", "unknown", None),
    ]
    # A layer no event source ever covered is unknown for the whole view.
    assert event_coverage(message, "flight") == [
        (DAY + "12:00:00Z", DAY + "16:00:00Z", "unknown", None)
    ]
    # A view outside every event source's area is unknown too.
    outside = snapshot(api, VIEW + "&bbox=0.6,0.6,0.9,0.9&layers=road")
    assert event_coverage(outside, "road") == [
        (DAY + "12:00:00Z", DAY + "16:00:00Z", "unknown", None)
    ]


def table_digest(conn, name: str) -> list:
    sql = f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM eye.{name} t"  # noqa: S608
    return conn.run(sql)


def test_migration_0005_leaves_earlier_history_unchanged(make_db, tmp_path):
    before = tmp_path / "migrations"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("000[1-4]_*.sql")):
        shutil.copy(path, before / path.name)
    conn = connect(make_db())
    assert migrate(conn, before) == [1, 2, 3, 4]
    load_fixtures(conn, CAPTURES)
    load_fixtures(conn, AIS_DEMO)
    tables = (
        "capture_batch",
        "raw_evidence",
        "observation",
        "observation_receipt",
        "coverage",
        "feed_change",
    )
    prior = [table_digest(conn, name) for name in tables]
    assert migrate(conn) == [5]
    after = [table_digest(conn, name) for name in tables]
    assert after == prior
    assert verify_replay(conn) == []
    conn.close()


# --- live: event deltas ----------------------------------------------------------------------


def road_counts(message: dict) -> list:
    """Served road event coverage: (start, end, state, cases in view)."""
    return event_coverage(message, "road")


def test_event_batches_arrive_as_deltas_and_a_moved_case_resnapshots(api_server, tmp_path):
    api = api_server()
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOURS))
    base = client.recv()
    assert base["kind"] == "snapshot" and base["events"] == []
    closure = segment([(-0.2, -0.2), (-0.1, -0.2)])
    api.ingest(
        capture(
            tmp_path, "new", "road", [claim("LIVE-1", "road_closure", DAY + "12:20:00Z", closure)]
        )
    )
    api.server.hub.tick()
    # The first road event batch fills part of the road layer's unknown gap,
    # which a delta cannot narrow: a fresh snapshot replaces it (O58).
    resync = client.recv()
    assert resync["kind"] == "resync_required" and resync["reason"] == "overflow"
    first = client.recv()
    assert first["kind"] == "snapshot" and reference_valid(first)
    (event,) = first["events"]
    assert event["case_id"] == "LIVE-1" and event["standing"] == "report"
    assert road_counts(first)[0] == (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 1)
    # Control: a new case from a new batch, in an hour already covered, is an
    # ordinary upsert, and its row counts only its own case in view.
    api.ingest(
        capture(
            tmp_path,
            "second",
            "road",
            [claim("LIVE-2", "road_closure", DAY + "12:25:00Z", point(0.1, -0.1))],
            received=DAY + "13:00:15Z",
        )
    )
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and reference_valid(delta)
    assert [e["case_id"] for e in delta["events_upserted"]] == ["LIVE-2"]
    (row,) = delta["coverage_upserted"]
    assert (row["metric"]["name"], row["metric"]["value"]) == ("event_cases_in_view", 1)
    # A correction of LIVE-1 moves its current report to a new batch, which
    # changes the first batch's in-view count: a fresh snapshot, not a delta.
    moved_closure = segment([(-0.2, -0.25), (-0.1, -0.25)])
    api.ingest(
        capture(
            tmp_path,
            "inside",
            "road",
            [claim("LIVE-1", "road_closure", DAY + "12:30:00Z", moved_closure)],
            received=DAY + "13:00:20Z",
        )
    )
    api.server.hub.tick()
    resync = client.recv()
    assert resync["kind"] == "resync_required" and resync["reason"] == "overflow"
    corrected = client.recv()
    assert [c["version"] for c in case_of(corrected, "LIVE-1")["claims"]] == [1, 2]
    counts = sorted(c[3] for c in road_counts(corrected) if c[2] == "qualified")
    assert counts == [0, 1, 1]  # the superseded report's batch now counts no case in view
    assert coverage_rows(corrected) == coverage_rows(snapshot(api, VIEW))
    # A correction moving the case out of view cannot be an upsert: resnapshot.
    api.ingest(
        capture(
            tmp_path,
            "outside",
            "road",
            [claim("LIVE-1", "road_closure", DAY + "12:40:00Z", segment([(1.2, 1.2), (1.3, 1.2)]))],
            received=DAY + "13:00:30Z",
            bbox=(-2, -2, 2, 2),
        )
    )
    api.server.hub.tick()
    resync = client.recv()
    assert resync["kind"] == "resync_required" and resync["reason"] == "overflow"
    fresh = client.recv()
    assert fresh["kind"] == "snapshot"
    assert [e["case_id"] for e in fresh["events"]] == ["LIVE-2"]
    client.close()


def test_new_positions_refresh_a_linked_case(api_server, tmp_path):
    from synthetic_captures import capture as positions
    from synthetic_captures import record

    api = api_server(ais_files=[])
    window = (DAY + "12:00:00Z", DAY + "13:00:00Z")
    api.ingest(
        capture(
            tmp_path,
            "report",
            "flight",
            [
                claim(
                    "LNK-LIVE",
                    "emergency_declared",
                    DAY + "12:40:00Z",
                    point(0.1, 0.1),
                    event_time=DAY + "12:30:00Z",
                    identifiers=[FLT900],
                    window=window,
                )
            ],
        )
    )
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOURS))
    before = case_of(client.recv(), "LNK-LIVE")
    assert before["link"]["state"] == "unlinked"
    api.ingest(
        positions(
            tmp_path,
            "flight",
            "flight",
            [record("SYN-FLT-900", DAY + "12:20:00Z", DAY + "12:20:05Z", 0.1, 0.1)],
            start=DAY + "12:00:00Z",
            end=DAY + "13:00:00Z",
            received=DAY + "13:00:05Z",
        )
    )
    api.server.hub.tick()
    delta = client.recv()
    (after,) = delta["events_upserted"]
    assert after["link"]["state"] == "linked"
    assert after["last_observed_position"]["observed_time"] == DAY + "12:20:00Z"
    client.close()


def test_ingest_by_bytes_matches_ingest_by_file(db, tmp_path):
    """The claim pipeline takes the same path as positions: archive then commit."""
    path = capture(
        tmp_path, "bytes", "road", [claim("B-1", "road_closure", DAY + "12:20:00Z", point(0, 0))]
    )
    first = ingest(db, path.read_bytes())
    again = ingest(db, path.read_bytes())
    assert (first.created, again.created) == (True, False)
    assert db.run("SELECT count(*) FROM eye.event_claim_receipt") == [[1]]


def test_one_hundred_versions_are_served_and_a_hundred_and_first_is_refused(api_server, tmp_path):
    api = api_server(ais_files=[])

    def versions(n: int, case: str) -> list[dict]:
        return [
            claim(
                case,
                "road_closure",
                f"{DAY}12:{i // 60:02d}:{i % 60:02d}Z",
                point(0.1, 0.1 + i / 10_000),
            )
            for i in range(n)
        ]

    api.ingest(capture(tmp_path, "hundred", "road", versions(100, "V-100")))
    (event,) = snapshot(api)["events"]
    assert len(event["claims"]) == 100 and event["claims"][-1]["is_current"] is True
    api.ingest(
        capture(tmp_path, "one-more", "road", versions(101, "V-101"), received=DAY + "13:00:20Z")
    )
    response, body = api.get(VIEW)
    assert response.status == 413
    error = validate_message(body, "ServerMessage")
    assert "road case V-101 from synthetic-events has more than 100 versions" in error["error"]


# --- O56: a source area smaller than the view does not certify the view ---------------------

SMALL = (0.0, 0.0, 0.2, 0.2)  # a healthy event source's whole requested area
INSIDE = "&bbox=0.05,0.05,0.15,0.15"  # a view wholly inside it


def test_small_source_area_is_partial_for_a_larger_view(api_server, tmp_path):
    api = api_server()
    api.ingest(capture(tmp_path, "small", "road", [], bbox=SMALL))  # healthy, nothing reported
    wide = snapshot(api, VIEW + "&layers=road")
    rows = [c for c in wide["coverage"] if c["metric"]["name"] == "event_cases_in_view"]
    (partial,) = [c for c in rows if c["state"] == "partial"]
    assert partial["interval"] == {"start": DAY + "12:00:00Z", "end": DAY + "13:00:00Z"}
    assert partial["metric"]["value"] == 0  # a lower bound, not a measured zero
    assert "covers only part of this view" in partial["reason"]
    # The rest of the view, for the whole interval, is unknown.
    assert [
        (c["interval"]["start"], c["interval"]["end"]) for c in rows if c["state"] == "unknown"
    ] == [(DAY + "12:00:00Z", DAY + "16:00:00Z")]
    assert not [c for c in rows if c["state"] == "qualified"]
    # Control: a view wholly inside the source area is a measured zero there.
    inside = snapshot(api, VIEW + INSIDE + "&layers=road")
    assert event_coverage(inside, "road") == [
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 0),
        (DAY + "13:00:00Z", DAY + "16:00:00Z", "unknown", None),
    ]


# --- O57: select by the possible occurrence interval ----------------------------------------


def edge_cases(tmp_path) -> list:
    """Two captures: cases near the view's start (published 12:10) and near its
    end (published after they may have happened, 16:06)."""
    here = point(0.1, 0.1)
    early = capture(
        tmp_path,
        "edges-early",
        "road",
        [
            # Nominal time before the view, but it may have happened inside it.
            claim(
                "EDGE-IN-EARLY",
                "road_closure",
                DAY + "12:10:00Z",
                here,
                event_time=DAY + "11:59:30Z",
                uncertainty=60,
            ),
            # Wholly before the view, even with its uncertainty.
            claim(
                "EDGE-OUT-EARLY",
                "road_closure",
                DAY + "12:10:00Z",
                here,
                event_time=DAY + "11:50:00Z",
                uncertainty=60,
            ),
        ],
    )
    late = capture(
        tmp_path,
        "edges-late",
        "road",
        [
            # Nominal time just past the view's end, possibly inside it.
            claim(
                "EDGE-IN-LATE",
                "road_closure",
                DAY + "16:06:00Z",
                here,
                event_time=DAY + "16:00:30Z",
                uncertainty=60,
            ),
            # Wholly after the view.
            claim(
                "EDGE-OUT-LATE",
                "road_closure",
                DAY + "16:06:00Z",
                here,
                event_time=DAY + "16:05:00Z",
                uncertainty=60,
            ),
        ],
        start=DAY + "16:00:00Z",
        end=DAY + "17:00:00Z",
    )
    return [early, late]


def test_cases_are_selected_by_their_possible_occurrence_interval(api_server, tmp_path):
    api = api_server()
    for path in edge_cases(tmp_path):
        api.ingest(path)
    assert api.conn.run("SELECT count(*) FROM eye.event_claim") == [[4]]  # all four stored
    message = snapshot(api)
    assert {e["case_id"] for e in message["events"]} == {"EDGE-IN-EARLY", "EDGE-IN-LATE"}
    (early,) = case_of(message, "EDGE-IN-EARLY")["claims"]
    # Served as stated: the nominal time and its uncertainty are unchanged.
    assert (early["event_time"], early["event_time_uncertainty_s"]) == (DAY + "11:59:30Z", 60)
    (late,) = case_of(message, "EDGE-IN-LATE")["claims"]
    assert (late["event_time"], late["event_time_uncertainty_s"]) == (DAY + "16:00:30Z", 60)


def test_live_updates_select_by_the_possible_occurrence_interval(api_server, tmp_path):
    api = api_server()
    # Road event coverage for the whole view first, so the next batch is a delta.
    api.ingest(
        capture(tmp_path, "cover", "road", [], end=DAY + "16:00:00Z", received=DAY + "16:00:05Z")
    )
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOURS, layers=("road",)))
    assert client.recv()["kind"] == "snapshot"
    selected = set()
    for path in edge_cases(tmp_path):
        api.ingest(path)
        api.server.hub.tick()
        delta = client.recv()
        assert delta["kind"] == "delta" and reference_valid(delta)
        selected |= {e["case_id"] for e in delta["events_upserted"]}
    assert selected == {"EDGE-IN-EARLY", "EDGE-IN-LATE"}
    client.close()


# --- O58: live coverage narrows the unknown gaps ---------------------------------------------


def coverage_rows(message: dict) -> list:
    return sorted(json.dumps(c, sort_keys=True) for c in message["coverage"])


def test_a_capture_filling_part_of_a_gap_resnapshots_and_matches_rest(api_server, tmp_path):
    api = api_server()
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOURS))
    base = client.recv()
    assert event_coverage(base, "road") == [(DAY + "12:00:00Z", DAY + "16:00:00Z", "unknown", None)]
    # Control first: a capture whose source area lies outside the view changes
    # nothing in view; an ordinary (empty) delta advances the cursor.
    api.ingest(capture(tmp_path, "elsewhere", "road", [], bbox=(0.6, 0.6, 0.9, 0.9)))
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and delta["coverage_upserted"] == []
    rest = snapshot(api, VIEW)
    assert coverage_rows(rest) == coverage_rows(base)  # live state still matches REST
    # A capture filling 12:00-13:00 for the whole view narrows the road gap.
    api.ingest(capture(tmp_path, "fills", "road", [], received=DAY + "13:00:20Z"))
    api.server.hub.tick()
    resync = client.recv()
    assert resync["kind"] == "resync_required" and resync["reason"] == "overflow"
    live = client.recv()
    assert live["kind"] == "snapshot"
    assert event_coverage(live, "road") == [
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 0),
        (DAY + "13:00:00Z", DAY + "16:00:00Z", "unknown", None),
    ]
    assert coverage_rows(live) == coverage_rows(snapshot(api, VIEW))
    client.close()


# --- O59: counts are for the view, not the whole capture ------------------------------------

HOUR12 = "/api/v0/snapshot?start=2026-02-01T12:00:00Z&end=2026-02-01T13:00:00Z&layers=road"


def three_cases(tmp_path, name: str = "three", received: str | None = None):
    """One road batch over the whole demo area, 12:00-13:00: two cases near
    0.1,0.1 (12:15 and 12:45) and one far away at -0.4,-0.4 (12:15)."""
    return capture(
        tmp_path,
        name,
        "road",
        [
            claim(
                "NEAR-EARLY",
                "road_closure",
                DAY + "12:16:00Z",
                point(0.1, 0.1),
                event_time=DAY + "12:15:00Z",
                uncertainty=60,
            ),
            claim(
                "NEAR-LATE",
                "road_closure",
                DAY + "12:46:00Z",
                point(0.1, 0.1),
                event_time=DAY + "12:45:00Z",
                uncertainty=60,
            ),
            claim(
                "FAR-EARLY",
                "road_closure",
                DAY + "12:16:00Z",
                point(-0.4, -0.4),
                event_time=DAY + "12:15:00Z",
                uncertainty=60,
            ),
        ],
        received=received,
    )


def view_count(message: dict) -> list:
    return [(c[0], c[1], c[2], c[3]) for c in event_coverage(message, "road") if c[2] != "unknown"]


def test_counts_follow_a_smaller_area(api_server, tmp_path):
    api = api_server()
    api.ingest(three_cases(tmp_path))
    stored = api.conn.run(
        "SELECT metric_value FROM eye.coverage WHERE metric_name = 'event_reports'"
    )
    assert stored == [[3]]  # the stored batch metric is unchanged: the whole capture
    whole = snapshot(api, HOUR12)
    assert view_count(whole) == [(DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 3)]
    near = snapshot(api, HOUR12 + "&bbox=0.0,0.0,0.2,0.2")
    assert {e["case_id"] for e in near["events"]} == {"NEAR-EARLY", "NEAR-LATE"}
    assert view_count(near) == [(DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 2)]
    # Paired: an area inside the capture with no case in it is a measured zero.
    empty = snapshot(api, HOUR12 + "&bbox=0.3,0.3,0.45,0.45")
    assert empty["events"] == []
    assert view_count(empty) == [(DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 0)]


def test_counts_follow_a_shorter_window(api_server, tmp_path):
    api = api_server()
    api.ingest(three_cases(tmp_path))
    early = snapshot(
        api, "/api/v0/snapshot?start=2026-02-01T12:00:00Z&end=2026-02-01T12:30:00Z&layers=road"
    )
    assert {e["case_id"] for e in early["events"]} == {"NEAR-EARLY", "FAR-EARLY"}
    # The row keeps its capture's reporting window (O60) and counts only the
    # cases in this view.
    assert view_count(early) == [(DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 2)]
    late = snapshot(
        api, "/api/v0/snapshot?start=2026-02-01T12:30:00Z&end=2026-02-01T13:00:00Z&layers=road"
    )
    assert [e["case_id"] for e in late["events"]] == ["NEAR-LATE"]
    assert view_count(late) == [(DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 1)]


def test_live_counts_are_view_scoped_and_match_rest(api_server, tmp_path):
    api = api_server()
    api.ingest(capture(tmp_path, "cover", "road", []))  # the hour is covered: next is a delta
    small = (0.0, 0.0, 0.2, 0.2)
    window = {"start": DAY + "12:00:00Z", "end": DAY + "13:00:00Z"}
    client = WsClient(api.host, api.port)
    client.send(subscribe(small, window, layers=("road",)))
    base = client.recv()
    assert base["kind"] == "snapshot" and base["events"] == []
    api.ingest(three_cases(tmp_path, received=DAY + "13:00:20Z"))
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and reference_valid(delta)
    assert {e["case_id"] for e in delta["events_upserted"]} == {"NEAR-EARLY", "NEAR-LATE"}
    (row,) = delta["coverage_upserted"]
    assert (row["metric"]["name"], row["metric"]["value"]) == ("event_cases_in_view", 2)
    rest = snapshot(api, HOUR12 + "&bbox=0.0,0.0,0.2,0.2")
    live_rows = sorted(
        json.dumps(c, sort_keys=True) for c in base["coverage"] + delta["coverage_upserted"]
    )
    assert live_rows == coverage_rows(rest)
    client.close()


# --- O60: every event-coverage row is a reporting window ------------------------------------

LATE = DAY + "14:00:10Z"


def early_reports(tmp_path, name: str = "early", received: str | None = None):
    """Road reports published 12:00-13:00: an ordinary case and one that is
    corrected later."""
    return capture(
        tmp_path,
        name,
        "road",
        [
            claim(
                "EARLY-ONLY",
                "road_closure",
                DAY + "12:16:00Z",
                point(0.1, 0.1),
                event_time=DAY + "12:15:00Z",
            ),
            claim(
                "CORRECTED",
                "road_closure",
                DAY + "12:21:00Z",
                point(0.1, 0.1),
                event_time=DAY + "12:20:00Z",
            ),
        ],
        received=received,
    )


def later_reports(tmp_path, name: str = "later"):
    """Road reports published 13:00-14:00: a correction and a first report
    about events at 12:20 and 12:40, and an ordinary case at 13:40."""
    return capture(
        tmp_path,
        name,
        "road",
        [
            claim(
                "CORRECTED",
                "road_closure",
                DAY + "13:20:00Z",
                point(0.12, 0.1),
                event_time=DAY + "12:20:00Z",
            ),
            claim(
                "LATE-REPORT",
                "road_closure",
                DAY + "13:10:00Z",
                point(-0.1, 0.1),
                event_time=DAY + "12:40:00Z",
            ),
            claim(
                "LATE-ONLY",
                "road_closure",
                DAY + "13:40:00Z",
                point(-0.1, -0.1),
                event_time=DAY + "13:40:00Z",
            ),
        ],
        start=DAY + "13:00:00Z",
        end=DAY + "14:00:00Z",
        received=LATE,
    )


TWICE = claim(
    "TWICE", "road_closure", DAY + "12:40:00Z", point(0.1, 0.1), event_time=DAY + "12:39:00Z"
)
ONCE = claim(
    "ONCE", "road_closure", DAY + "12:20:00Z", point(0.1, 0.1), event_time=DAY + "12:19:00Z"
)


def two_deliveries(tmp_path) -> tuple:
    """The same claim delivered by two overlapping captures, beside a claim
    delivered once."""
    first = capture(tmp_path, "first", "road", [TWICE, ONCE])
    again = capture(
        tmp_path,
        "again",
        "road",
        [TWICE],
        start=DAY + "12:30:00Z",
        end=DAY + "13:30:00Z",
        received=DAY + "13:30:10Z",
    )
    return first, again


def windows(message: dict) -> list:
    """Event rows: (reporting window start, end, state, cases in view)."""
    rows = [c for c in message["coverage"] if c["metric"]["name"] == "event_cases_in_view"]
    assert all(c["interval_kind"] == "reporting_window" for c in rows)
    # Every row from a capture names it; only a generated gap has none.
    assert all(("batch_id" in c) == (c["state"] != "unknown") for c in rows)
    return sorted(event_coverage(message, "road"))


def test_a_later_report_about_an_earlier_event_is_counted_in_its_own_window(api_server, tmp_path):
    api = api_server()
    api.ingest(early_reports(tmp_path))
    api.ingest(later_reports(tmp_path))
    stored = api.conn.run(
        "SELECT metric_value FROM eye.coverage WHERE metric_name = 'event_reports' "
        "ORDER BY interval_start"
    )
    assert stored == [[2], [3]]  # the stored batch metrics are unchanged: whole captures
    hour12 = snapshot(api, HOUR12)
    assert {e["case_id"] for e in hour12["events"]} == {"EARLY-ONLY", "CORRECTED", "LATE-REPORT"}
    # The 13:00-14:00 capture reported (and corrected) events of 12:20 and
    # 12:40: its row is listed with its own reporting window, not clipped to
    # the view and not read as an occurrence interval.
    assert windows(hour12) == [
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 1),
        (DAY + "13:00:00Z", DAY + "14:00:00Z", "qualified", 2),
    ]
    corrected = case_of(hour12, "CORRECTED")
    assert [c["is_current"] for c in corrected["claims"]] == [False, True]
    # Control: the later hour holds only the ordinary 13:40 case; the early
    # capture counts nothing there and is not listed.
    hour13 = snapshot(
        api, "/api/v0/snapshot?start=2026-02-01T13:00:00Z&end=2026-02-01T14:00:00Z&layers=road"
    )
    assert [e["case_id"] for e in hour13["events"]] == ["LATE-ONLY"]
    assert windows(hour13) == [(DAY + "13:00:00Z", DAY + "14:00:00Z", "qualified", 1)]
    # Track coverage rows carry no reporting-window label.
    whole = snapshot(api, VIEW)
    assert all(
        ("interval_kind" in c) == (c["metric"]["name"] == "event_cases_in_view")
        for c in whole["coverage"]
    )
    assert any(c["metric"]["name"] != "event_cases_in_view" for c in whole["coverage"])


def test_the_same_claim_in_two_batches_is_counted_by_both_rows(api_server, tmp_path):
    api = api_server()
    first, again = two_deliveries(tmp_path)
    api.ingest(first)
    api.ingest(again)
    view = snapshot(api, HOUR12)
    assert sorted(e["case_id"] for e in view["events"]) == ["ONCE", "TWICE"]
    (claim_twice,) = case_of(view, "TWICE")["claims"]
    assert len(claim_twice["evidence_batch_ids"]) == 2  # one claim, two receipts
    rows = windows(view)
    assert rows == [
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 2),
        (DAY + "12:30:00Z", DAY + "13:30:00Z", "qualified", 1),
    ]
    # Two rows, one per capture: each names its batch, and the one claim
    # cites both.
    ids = {c["batch_id"] for c in view["coverage"] if "batch_id" in c and c["layer"] == "road"}
    assert ids == set(claim_twice["evidence_batch_ids"])
    # The rows are not a total: TWICE is in both, so they add up to more
    # than the cases in view.
    assert sum(r[3] for r in rows) == 3 and len(view["events"]) == 2
    # Control: one delivery of each claim gives one row that equals the list.
    api2 = api_server()
    api2.ingest(first)
    single = snapshot(api2, HOUR12)
    assert windows(single) == [(DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 2)]
    assert len(single["events"]) == 2


def test_live_later_reports_and_repeats_match_rest(api_server, tmp_path):
    api = api_server()
    api.ingest(early_reports(tmp_path))
    road12 = {"start": DAY + "12:00:00Z", "end": DAY + "13:00:00Z"}
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, road12, layers=("road",)))
    base = client.recv()
    assert base["kind"] == "snapshot"
    assert windows(base) == [(DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 2)]
    # Control: a later capture with only a new report about a 12:50 event is
    # an ordinary delta, carrying its own reporting-window row.
    api.ingest(
        capture(
            tmp_path,
            "new-late",
            "road",
            [
                claim(
                    "LATE-NEW",
                    "road_closure",
                    DAY + "13:05:00Z",
                    point(0.2, 0.2),
                    event_time=DAY + "12:50:00Z",
                )
            ],
            start=DAY + "13:00:00Z",
            end=DAY + "14:00:00Z",
            received=DAY + "14:00:05Z",
        )
    )
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and reference_valid(delta)
    assert [e["case_id"] for e in delta["events_upserted"]] == ["LATE-NEW"]
    (row,) = delta["coverage_upserted"]
    assert row["interval_kind"] == "reporting_window"
    assert (row["interval"]["start"], row["metric"]["value"]) == (DAY + "13:00:00Z", 1)
    live = sorted(
        json.dumps(c, sort_keys=True) for c in base["coverage"] + delta["coverage_upserted"]
    )
    assert live == coverage_rows(snapshot(api, HOUR12))
    # A later correction of an earlier case changes the early row's count: a
    # fresh snapshot, and it matches REST.
    api.ingest(later_reports(tmp_path))
    api.server.hub.tick()
    resync = client.recv()
    assert resync["kind"] == "resync_required" and resync["reason"] == "overflow"
    corrected = client.recv()
    assert corrected["kind"] == "snapshot"
    assert windows(corrected) == [
        (DAY + "12:00:00Z", DAY + "13:00:00Z", "qualified", 1),
        (DAY + "13:00:00Z", DAY + "14:00:00Z", "qualified", 1),
        (DAY + "13:00:00Z", DAY + "14:00:00Z", "qualified", 2),
    ]
    assert coverage_rows(corrected) == coverage_rows(snapshot(api, HOUR12))
    # The same claim delivered again adds a second row that counts it: a
    # fresh snapshot too, and it matches REST.
    api.ingest(early_reports(tmp_path, "early-again", received=DAY + "13:00:40Z"))
    api.server.hub.tick()
    resync = client.recv()
    assert resync["kind"] == "resync_required" and resync["reason"] == "overflow"
    repeated = client.recv()
    assert sorted(r[3] for r in windows(repeated) if r[1] == DAY + "13:00:00Z") == [1, 1]
    assert coverage_rows(repeated) == coverage_rows(snapshot(api, HOUR12))
    client.close()
