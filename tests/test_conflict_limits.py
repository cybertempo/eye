"""O52 and O54 on PostGIS: contested middle times and the claim limit.

O52 (server side): a contested time between two resolved positions leaves both
of them on the route and the conflict between them, so a client can break the
route there; a resolved middle time is an ordinary point (control). The
rendering half is in tests/test_browser.py.

O54: a conflict carries at most 20 claims (the wire limit). Twenty complete
claims are served whole with their evidence; a 21st makes the server refuse
the request by name, with a bounded 413, before any response is validated:
never a generic 500 and never a conflict cut short.
"""

from __future__ import annotations

import copy
import json

import jsonschema
from eye.api import feed
from eye.wire import SCHEMA_PATH, validate_message
from synthetic_captures import capture, record
from ws_client import WsClient, subscribe

AREA = (-0.5, -0.5, 0.5, 0.5)
HOUR3 = {"start": "2026-01-01T03:00:00Z", "end": "2026-01-01T04:00:00Z"}
SNAPSHOT_3 = "/api/v0/snapshot?start=2026-01-01T03:00:00Z&end=2026-01-01T04:00:00Z"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
REC = "SYN-FLT-920"
CONTESTED = "2026-01-01T03:05:00Z"
WINDOW = {"start": "2026-01-01T02:55:00Z", "end": "2026-01-01T03:15:00Z"}


def reference_valid(message: dict) -> bool:
    document = copy.deepcopy(SCHEMA)
    document["$ref"] = "#/$defs/ServerMessage"
    return jsonschema.Draft202012Validator(document).is_valid(message)


def track_of(message: dict) -> dict:
    (track,) = [t for t in message["tracks"] if t["source_record_id"] == REC]
    return track


def batch_of(api) -> str:
    return api.conn.run(
        "SELECT batch_id::text FROM eye.capture_batch ORDER BY archived_at DESC LIMIT 1"
    )[0][0]


def ingest_route(api, tmp_path) -> None:
    """Resolved fixes at 03:00 and 03:10 either side of the contested 03:05."""
    api.ingest(
        capture(
            tmp_path,
            "route",
            "flight",
            [
                record(REC, "2026-01-01T03:00:00Z", "2026-01-01T03:00:03Z", 0.0),
                record(REC, "2026-01-01T03:10:00Z", "2026-01-01T03:10:03Z", 0.3),
            ],
            **WINDOW,
            received="2026-01-01T03:15:01Z",
        )
    )


def ingest_claims(api, tmp_path, count: int, first: int = 0) -> dict[str, str]:
    """``count`` claims for 03:05, all published at the same time, one per batch."""
    batches = {}
    for index in range(first, first + count):
        lon = round(0.1 + 0.005 * index, 4)
        api.ingest(
            capture(
                tmp_path,
                f"claim-{index:02d}",
                "flight",
                [record(REC, CONTESTED, "2026-01-01T03:05:03Z", lon, 0.05)],
                **WINDOW,
                received=f"2026-01-01T03:15:{10 + index:02d}Z",
            )
        )
        batches[str(lon)] = batch_of(api)
    return batches


def resolve(api, tmp_path) -> None:
    """A later, unique publication for 03:05."""
    api.ingest(
        capture(
            tmp_path,
            "resolution",
            "flight",
            [record(REC, CONTESTED, "2026-01-01T03:30:00Z", 0.15)],
            **WINDOW,
            received="2026-01-01T03:30:05Z",
        )
    )


# --- O52: an intermediate contested time --------------------------------------------------


def test_contested_middle_time_sits_between_two_resolved_points(api_server, tmp_path):
    api = api_server(ais_files=[])
    ingest_route(api, tmp_path)
    ingest_claims(api, tmp_path, 2)
    message = json.loads(api.get(SNAPSHOT_3)[1])
    assert reference_valid(message)
    track = track_of(message)
    times = [p["observed_time"] for p in track["points"]]
    assert times == ["2026-01-01T03:00:00Z", "2026-01-01T03:10:00Z"]
    (conflict,) = track["conflicts"]
    assert times[0] < conflict["observed_time"] == CONTESTED < times[1]


def test_resolved_middle_time_is_an_ordinary_route_point(api_server, tmp_path):
    """Control: once resolved, 03:05 is a point and there is nothing to break at."""
    api = api_server(ais_files=[])
    ingest_route(api, tmp_path)
    ingest_claims(api, tmp_path, 2)
    resolve(api, tmp_path)
    track = track_of(json.loads(api.get(SNAPSHOT_3)[1]))
    assert [(p["observed_time"], p["lon"]) for p in track["points"]] == [
        ("2026-01-01T03:00:00Z", 0.0),
        (CONTESTED, 0.15),
        ("2026-01-01T03:10:00Z", 0.3),
    ]
    assert "conflicts" not in track


# --- O54: at most 20 claims, refused by name past that --------------------------------------


def test_twenty_complete_claims_are_served_whole(api_server, tmp_path):
    api = api_server(ais_files=[])
    ingest_route(api, tmp_path)
    batches = ingest_claims(api, tmp_path, feed.MAX_CONFLICT_CLAIMS)
    response, body = api.get(SNAPSHOT_3)
    assert response.status == 200
    message = json.loads(body)
    assert validate_message(message, "ServerMessage") and reference_valid(message)
    (conflict,) = track_of(message)["conflicts"]
    claims = conflict["claims"]
    assert len(claims) == 20
    # Complete: every claim, each with the batch that carried it.
    assert {str(c["lon"]): c["evidence_batch_ids"] for c in claims} == {
        lon: [batch] for lon, batch in batches.items()
    }
    stored = api.conn.run(
        "SELECT observation_id::text FROM eye.observation_version WHERE is_current IS NULL"
    )
    assert {c["observation_id"] for c in claims} == {r[0] for r in stored}
    # The same over WebSocket.
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOUR3, layers=("flight",)))
    snapshot = client.recv()
    assert snapshot["kind"] == "snapshot"
    assert len(track_of(snapshot)["conflicts"][0]["claims"]) == 20
    client.close()


def test_twenty_one_claims_are_refused_by_name_not_a_500(api_server, tmp_path):
    api = api_server(ais_files=[])
    ingest_route(api, tmp_path)
    ingest_claims(api, tmp_path, feed.MAX_CONFLICT_CLAIMS + 1)
    assert api.conn.run(
        "SELECT count(*) FROM eye.observation_version WHERE is_current IS NULL"
    ) == [[21]]
    response, body = api.get(SNAPSHOT_3)
    assert response.status == 413
    assert len(body) <= api.server.config.server.max_response_bytes
    error = validate_message(body, "ServerMessage")
    assert error["kind"] == "error" and error["status"] == 413
    assert "more than 20 conflicting claims" in error["error"]
    assert REC in error["error"] and CONTESTED in error["error"]
    assert "invalid outgoing message" not in error["error"]
    # WebSocket: the same refusal, and no subscription is left active.
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOUR3, layers=("flight",)))
    refused = client.recv()
    assert refused["kind"] == "error" and refused["status"] == 413
    assert "more than 20 conflicting claims" in refused["error"]
    assert "no subscription is active" in refused["error"]
    (sub,) = api.server.hub.subscribers()
    assert sub.query is None
    # A view that leaves the contested time out is served (control).
    narrow = "/api/v0/snapshot?start=2026-01-01T03:06:00Z&end=2026-01-01T04:00:00Z"
    response, body = api.get(narrow)
    assert response.status == 200
    assert [p["lon"] for p in track_of(json.loads(body))["points"]] == [0.3]
    # Once a unique publication resolves it, the full view is served again.
    resolve(api, tmp_path)
    response, body = api.get(SNAPSHOT_3)
    assert response.status == 200
    assert [p["lon"] for p in track_of(json.loads(body))["points"]] == [0.0, 0.15, 0.3]
    client.close()


def test_a_21st_claim_arriving_live_ends_the_subscription_by_name(api_server, tmp_path):
    api = api_server(ais_files=[])
    ingest_route(api, tmp_path)
    ingest_claims(api, tmp_path, feed.MAX_CONFLICT_CLAIMS)
    client = WsClient(api.host, api.port)
    client.send(subscribe(AREA, HOUR3, layers=("flight",)))
    assert len(track_of(client.recv())["conflicts"][0]["claims"]) == 20
    ingest_claims(api, tmp_path, 1, first=feed.MAX_CONFLICT_CLAIMS)
    api.server.hub.tick()
    resync = client.recv()
    assert resync["kind"] == "resync_required" and resync["reason"] == "overflow"
    refused = client.recv()
    assert refused["kind"] == "error" and refused["status"] == 413
    assert "more than 20 conflicting claims" in refused["error"]
    (sub,) = api.server.hub.subscribers()
    assert sub.query is None  # no delta can follow the refusal
    client.close()
