"""O50 and O51: record identity includes the layer; contested positions stay contested.

A source record is (source, layer, record id). The same record id reused as a
flight and a vessel is two records at every stage (capture ids, version
history, replay, API tracks, wire ids), next to a same-layer control that is
one record. Equal-time conflicting latest positions keep every claim and its
evidence, form no route and give no last position, next to a later unique
correction that resolves them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil

import jsonschema
import pytest
from conftest import CAPTURES
from eye.api import feed
from eye.ingest.capture import ingest, load_fixtures, verify_replay
from eye.storage.db import connect
from eye.storage.migrate import MIGRATIONS_DIR, MigrationError, migrate
from eye.wire import SCHEMA_PATH, validate_message
from synthetic_captures import capture, record
from ws_client import WsClient, subscribe

SOURCE = "synthetic-fixture"
AREA = (-0.5, -0.5, 0.5, 0.5)
HOUR3 = {"start": "2026-01-01T03:00:00Z", "end": "2026-01-01T04:00:00Z"}
SNAPSHOT_3 = "/api/v0/snapshot?start=2026-01-01T03:00:00Z&end=2026-01-01T04:00:00Z"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def reference_valid(message: dict) -> bool:
    document = copy.deepcopy(SCHEMA)
    document["$ref"] = "#/$defs/ServerMessage"
    return jsonschema.Draft202012Validator(document).is_valid(message)


def expected_track_id(source: str, layer: str, rec: str) -> str:
    digest = hashlib.sha256(json.dumps([source, layer, rec]).encode()).hexdigest()
    return f"trk-{digest[:32]}"


def window(**kw) -> dict:
    return {"start": "2026-01-01T03:25:00Z", "end": "2026-01-01T03:45:00Z", **kw}


def shared_id_captures(tmp_path):
    """SYN-SHARED-1 as a flight and as a vessel: same id, time, content."""
    same = record("SYN-SHARED-1", "2026-01-01T03:30:00Z", "2026-01-01T03:30:03Z", 0.1, 0.1)
    return [
        capture(
            tmp_path, "shared-flight", "flight", [same], **window(received="2026-01-01T03:45:02Z")
        ),
        capture(
            tmp_path, "shared-vessel", "vessel", [same], **window(received="2026-01-01T03:45:04Z")
        ),
    ]


def same_layer_captures(tmp_path):
    """Control: SYN-SAME-1 twice in one layer is one record with two positions."""
    return [
        capture(
            tmp_path,
            "same-1",
            "flight",
            [record("SYN-SAME-1", "2026-01-01T03:30:00Z", "2026-01-01T03:30:03Z", -0.1, -0.1)],
            **window(received="2026-01-01T03:45:02Z"),
        ),
        capture(
            tmp_path,
            "same-2",
            "flight",
            [record("SYN-SAME-1", "2026-01-01T03:40:00Z", "2026-01-01T03:40:03Z", -0.2, -0.1)],
            **window(received="2026-01-01T03:45:06Z"),
        ),
    ]


# --- O50: identity includes the layer ------------------------------------------------------


def _observations(conn, rec: str) -> list:
    return conn.run(
        "SELECT o.observation_id::text, o.layer, v.version, v.is_current, v.publication_conflict, "
        "(SELECT array_agg(r.batch_id::text) FROM eye.observation_receipt r "
        " WHERE r.observation_id = o.observation_id) "
        "FROM eye.observation o JOIN eye.observation_version v USING (observation_id) "
        "WHERE o.source_record_id = :r ORDER BY o.layer, o.observed_time",
        r=rec,
    )


def test_reused_record_id_across_layers_is_two_records_in_storage_and_replay(db, tmp_path):
    batches = [ingest(db, p.read_bytes()).batch_id for p in shared_id_captures(tmp_path)]
    rows = _observations(db, "SYN-SHARED-1")
    # Archive: two observations, one per layer, each received only in its own batch.
    assert [r[1] for r in rows] == ["flight", "vessel"]
    assert rows[0][0] != rows[1][0]
    assert [r[5] for r in rows] == [[batches[0]], [batches[1]]]
    # Version history: each is its own first, current, uncontested version.
    assert [(r[2], r[3], r[4]) for r in rows] == [(1, True, False), (1, True, False)]
    # Replay re-derives both from evidence and finds nothing to report.
    assert verify_replay(db) == []

    # Control: two deliveries in one layer are one record with two positions.
    for path in same_layer_captures(tmp_path):
        ingest(db, path.read_bytes())
    same = _observations(db, "SYN-SAME-1")
    assert [r[1] for r in same] == ["flight", "flight"]
    assert verify_replay(db) == []


def test_rebuilds_in_either_order_give_the_same_layered_ids(make_db, tmp_path):
    files = shared_id_captures(tmp_path) + same_layer_captures(tmp_path)
    ids = []
    for order in (files, list(reversed(files))):
        conn = connect(make_db())
        migrate(conn)
        for path in order:
            ingest(conn, path.read_bytes())
        ids.append(
            conn.run(
                "SELECT observation_id::text, layer, source_record_id FROM eye.observation "
                "ORDER BY 1"
            )
        )
        assert verify_replay(conn) == []
        conn.close()
    assert ids[0] == ids[1] and len(ids[0]) == 4


def test_api_groups_and_identifies_tracks_by_source_layer_and_record(api_server, tmp_path):
    api = api_server(ais_files=[])
    for path in shared_id_captures(tmp_path) + same_layer_captures(tmp_path):
        api.ingest(path)
    message = json.loads(api.get(SNAPSHOT_3)[1])
    assert reference_valid(message)
    by_record: dict[str, list] = {}
    for track in message["tracks"]:
        by_record.setdefault(track["source_record_id"], []).append(track)
    shared = sorted(by_record["SYN-SHARED-1"], key=lambda t: t["kind"])
    assert [t["kind"] for t in shared] == ["flight", "vessel"]
    assert [t["id"] for t in shared] == [
        expected_track_id(SOURCE, "flight", "SYN-SHARED-1"),
        expected_track_id(SOURCE, "vessel", "SYN-SHARED-1"),
    ]
    assert all(len(t["points"]) == 1 for t in shared)
    # Control: the same-layer record is one track with both positions.
    (same,) = by_record["SYN-SAME-1"]
    assert same["id"] == expected_track_id(SOURCE, "flight", "SYN-SAME-1")
    assert [p["lon"] for p in same["points"]] == [-0.1, -0.2]
    # The live feed uses the same ids.
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOUR3))
    live = {(t["source_record_id"], t["kind"]): t["id"] for t in client.recv()["tracks"]}
    client.close()
    assert live == {(t["source_record_id"], t["kind"]): t["id"] for t in message["tracks"]}


def test_migration_0004_refuses_a_database_with_old_identity_ids(make_db, tmp_path):
    before = tmp_path / "migrations"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("000[123]_*.sql")):
        shutil.copy(path, before / path.name)
    old = connect(make_db())
    assert migrate(old, before) == [1, 2, 3]
    old.run(
        "INSERT INTO eye.capture_batch (batch_id, source_id, layer, adapter_version, "
        "capture_format, requested_area, requested_start, requested_end, expected_interval_s, "
        "attempt_started_at, attempt_finished_at, provider_status, quota_cost, evidence_sha256) "
        "VALUES (gen_random_uuid(), 'synthetic-fixture', 'flight', 'a', 'f', "
        "ST_MakeEnvelope(0, 0, 1, 1, 4326), '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', "
        "60, '2026-01-01T01:00:00Z', '2026-01-01T01:00:00Z', 'ok', 0, repeat('a', 64))"
    )
    old.run(
        "INSERT INTO eye.observation (observation_id, source_id, source_record_id, layer, "
        "observed_time, source_published_time, position, content_sha256) VALUES "
        "(gen_random_uuid(), 'synthetic-fixture', 'SYN-OLD', 'flight', '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z', ST_SetSRID(ST_MakePoint(0, 0), 4326), repeat('b', 64))"
    )
    with pytest.raises(MigrationError, match="includes the layer|include the layer"):
        migrate(old)
    assert old.run("SELECT max(version) FROM public.eye_schema_migrations") == [[3]]
    old.close()
    # Control: an empty database takes 0004 and loads the fixtures under the new rule.
    fresh = connect(make_db())
    assert 4 in migrate(fresh)
    load_fixtures(fresh, CAPTURES)
    assert verify_replay(fresh) == []
    fresh.close()


# --- O51: equal-time conflicting latest positions ---------------------------------------------


def conflict_captures(tmp_path, *, with_route: bool = True):
    """SYN-FLT-910: a resolved 03:00 fix, then two claims for 03:05 published together."""
    paths = []
    if with_route:
        paths.append(
            capture(
                tmp_path,
                "route",
                "flight",
                [record("SYN-FLT-910", "2026-01-01T03:00:00Z", "2026-01-01T03:00:03Z", 0.0)],
                start="2026-01-01T02:55:00Z",
                end="2026-01-01T03:10:00Z",
                received="2026-01-01T03:10:02Z",
            )
        )
    for name, lon, received in (("claim-a", 0.1, "03:10:03"), ("claim-b", 0.2, "03:10:04")):
        paths.append(
            capture(
                tmp_path,
                name,
                "flight",
                [record("SYN-FLT-910", "2026-01-01T03:05:00Z", "2026-01-01T03:05:03Z", lon)],
                start="2026-01-01T02:55:00Z",
                end="2026-01-01T03:10:00Z",
                received=f"2026-01-01T{received}Z",
            )
        )
    return paths


def resolution_capture(tmp_path):
    """A later, unique publication for 03:05: it supersedes both claims."""
    return capture(
        tmp_path,
        "resolution",
        "flight",
        [record("SYN-FLT-910", "2026-01-01T03:05:00Z", "2026-01-01T03:30:00Z", 0.15)],
        start="2026-01-01T02:55:00Z",
        end="2026-01-01T03:10:00Z",
        received="2026-01-01T03:30:05Z",
    )


def _track(message: dict, rec: str = "SYN-FLT-910") -> dict:
    (track,) = [t for t in message["tracks"] if t["source_record_id"] == rec]
    return track


def test_equal_time_conflict_keeps_both_claims_off_the_route(api_server, tmp_path):
    api = api_server(ais_files=[])
    batches = {}
    for path in conflict_captures(tmp_path):
        api.ingest(path)
        batches[path.stem] = api.conn.run(
            "SELECT batch_id::text FROM eye.capture_batch ORDER BY archived_at DESC LIMIT 1"
        )[0][0]
    message = json.loads(api.get(SNAPSHOT_3)[1])
    assert validate_message(message, "ServerMessage") and reference_valid(message)
    track = _track(message)
    # The route holds only the resolved 03:00 position.
    assert [(p["observed_time"], p["lon"]) for p in track["points"]] == [
        ("2026-01-01T03:00:00Z", 0.0)
    ]
    (conflict,) = track["conflicts"]
    assert conflict["observed_time"] == "2026-01-01T03:05:00Z"
    claims = sorted(conflict["claims"], key=lambda c: c["lon"])
    assert [c["lon"] for c in claims] == [0.1, 0.2]
    assert [c["evidence_batch_ids"] for c in claims] == [
        [batches["claim-a"]],
        [batches["claim-b"]],
    ]
    assert {c["published_time"] for c in claims} == {"2026-01-01T03:05:03Z"}
    ids = {c["observation_id"] for c in claims}
    stored = api.conn.run(
        "SELECT observation_id::text FROM eye.observation_version WHERE is_current IS NULL"
    )
    assert ids == {r[0] for r in stored} and len(ids) == 2
    assert "publication_conflict" in track["quality_flags"]


def test_later_unique_correction_resolves_the_conflict(api_server, tmp_path):
    api = api_server(ais_files=[])
    for path in conflict_captures(tmp_path):
        api.ingest(path)
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOUR3, layers=("flight",)))
    before = _track(client.recv())
    assert len(before["conflicts"]) == 1
    api.ingest(resolution_capture(tmp_path))
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and reference_valid(delta)
    (after,) = delta["tracks_upserted"]
    assert after["id"] == before["id"]
    assert [(p["observed_time"], p["lon"]) for p in after["points"]] == [
        ("2026-01-01T03:00:00Z", 0.0),
        ("2026-01-01T03:05:00Z", 0.15),
    ]
    assert "conflicts" not in after
    rest = _track(json.loads(api.get(SNAPSHOT_3)[1]))
    assert rest["points"] == after["points"] and "conflicts" not in rest
    client.close()


def test_track_with_only_contested_positions_has_no_route(api_server, tmp_path):
    api = api_server(ais_files=[])
    for path in conflict_captures(tmp_path, with_route=False):
        api.ingest(path)
    message = json.loads(api.get(SNAPSHOT_3)[1])
    assert reference_valid(message)
    track = _track(message)
    assert track["points"] == [] and len(track["conflicts"][0]["claims"]) == 2
    # Control: resolved, it becomes an ordinary one-point track.
    api.ingest(resolution_capture(tmp_path))
    resolved = _track(json.loads(api.get(SNAPSHOT_3)[1]))
    assert [p["lon"] for p in resolved["points"]] == [0.15] and "conflicts" not in resolved


def test_claims_outside_the_view_are_still_shown_with_the_conflict(api_server, tmp_path):
    api = api_server(ais_files=[])
    for path in conflict_captures(tmp_path):
        api.ingest(path)
    narrow = SNAPSHOT_3 + "&bbox=-0.05,-0.05,0.15,0.05"
    track = _track(json.loads(api.get(narrow)[1]))
    assert sorted(c["lon"] for c in track["conflicts"][0]["claims"]) == [0.1, 0.2]


def test_feed_track_ids_use_layer():
    assert feed.track_id(SOURCE, "flight", "X") != feed.track_id(SOURCE, "vessel", "X")
    assert feed.track_id(SOURCE, "flight", "X") == expected_track_id(SOURCE, "flight", "X")
