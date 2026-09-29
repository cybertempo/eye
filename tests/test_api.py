"""Browser API: bounded REST and WebSocket, over a real PostGIS database.

Each refusal sits next to an accepted control. The WebSocket side is exercised
with a raw-socket client (tests/support/ws_client.py), not the server's own
framing code, and responses are checked by the reference JSON Schema library
as well as by EYE's validator.
"""

from __future__ import annotations

import copy
import json
import socket
import struct
import time

import jsonschema
import pytest
from conftest import AIS_DEMO, REPO_ROOT
from eye.api import feed
from eye.api.auth import Principal
from eye.wire import SCHEMA_PATH, validate_message
from pg8000.exceptions import DatabaseError
from ws_client import BINARY, TEXT, Closed, WsClient, subscribe

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
DEMO_FILES = sorted(AIS_DEMO.glob("*.json"))
AREA = (-0.5, -0.5, 0.5, 0.5)
DAY = {"start": "2026-02-01T12:00:00Z", "end": "2026-02-01T16:00:00Z"}


def reference_valid(message: dict) -> bool:
    document = copy.deepcopy(SCHEMA)
    document["$ref"] = "#/$defs/ServerMessage"
    return jsonschema.Draft202012Validator(document).is_valid(message)


def body(response_and_body) -> dict:
    return json.loads(response_and_body[1])


def ws(api, **kwargs) -> WsClient:
    return WsClient(api.host, api.port, **kwargs)


# --- REST: snapshots and transit counts --------------------------------------------------


def test_snapshot_is_database_backed_bounded_and_valid(api_server):
    api = api_server()
    response, raw = api.get("/api/v0/snapshot")
    message = json.loads(raw)
    assert response.status == 200
    assert validate_message(message, "ServerMessage") and reference_valid(message)
    assert message["kind"] == "snapshot" and message["synthetic"] is True
    # The default view is the latest hours with coverage: the four demo AIS hours.
    assert message["interval"] == DAY
    assert {t["source_record_id"] for t in message["tracks"]} == {
        f"SYNV-00{n}" for n in (20, 21, 22, 23, 24)
    }
    epoch, seq = feed.parse_cursor(message["cursor"])
    head = api.conn.run("SELECT max(change_seq) FROM eye.feed_change")[0][0]
    assert seq == head and epoch
    failed = [c for c in message["coverage"] if c["state"] == "failed"]
    assert failed and all(c["metric"]["value"] is None for c in failed)
    point = message["tracks"][0]["points"][0]
    assert point["observed_time"] != point["received_time"]  # separate fields, both present


def test_transit_counts_keep_unknown_partial_and_measured_zero_apart(api_server):
    api = api_server()
    message = body(api.get("/api/v0/transits"))
    assert validate_message(message, "ServerMessage") and reference_valid(message)
    by_hour = {c["interval"]["start"][11:13]: c for c in message["counts"]}
    assert (by_hour["12"]["state"], by_hour["12"]["total"]) == ("qualified", 1)
    assert (by_hour["13"]["state"], by_hour["13"]["total"]) == ("partial", 1)
    assert by_hour["13"]["ambiguous_crossings"] == 1 and by_hour["13"]["reason"]
    outage = by_hour["14"]
    assert outage["state"] == "unknown"
    assert (outage["inbound"], outage["outbound"], outage["total"]) == (None, None, None)
    assert "failed" in outage["reason"]
    # Control: a healthy hour with no crossing is a measured zero, not unknown.
    assert (by_hour["15"]["state"], by_hour["15"]["total"]) == ("qualified", 0)
    # Every cited crossing and coverage row is present with its evidence.
    cited = {c for count in message["counts"] for c in count["crossing_ids"]}
    assert cited == {c["id"] for c in message["crossings"]}
    assert all(c["evidence_batch_ids"] for c in message["crossings"])
    assert message["line"] == {
        "id": "synthetic-golden-gate",
        "version": 1,
        "name": "Synthetic Golden Gate count line (placeholder geometry)",
        "synthetic": True,
        "coords": [[0.3, -0.05], [0.3, 0.05]],
    }
    assert message["algorithm_version"] == "transit-counter/2" and message["run_id"]


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ("start=2026-02-01T12:00:00Z&end=2026-02-01T11:00:00Z", 400),
        ("start=2026-01-01T00:00:00Z&end=2026-02-01T00:00:00Z", 413),
        ("bbox=0,0,1", 400),
        ("bbox=10,0,5,1", 400),
        ("bbox=nan,0,1,1", 400),
        ("layers=flight,ships", 400),
        ("start=2026-02-30T00:00:00Z", 400),
        ("colour=red", 400),
        ("start=2026-02-01T12:00:00Z&start=2026-02-01T13:00:00Z", 400),
        ("&".join(f"p{n}=1" for n in range(9)), 400),
    ],
)
def test_snapshot_refuses_unbounded_or_malformed_queries(api_server, query, status):
    api = api_server()
    refused = api.get(f"/api/v0/snapshot?{query}")
    assert refused[0].status == status, refused[1]
    assert body(refused)["kind"] == "error"
    # Control: a bounded, well-formed query succeeds.
    ok = api.get(
        "/api/v0/snapshot?bbox=0,-0.2,0.6,0.2&start=2026-02-01T12:00:00Z"
        "&end=2026-02-01T14:00:00Z&layers=vessel"
    )
    assert ok[0].status == 200 and body(ok)["kind"] == "snapshot"


def test_empty_result_is_still_marked_by_its_sources(api_server):
    api = api_server()
    empty = body(api.get("/api/v0/snapshot?bbox=-0.5,-0.5,-0.4,-0.4&layers=road"))
    assert empty["tracks"] == [] and empty["events"] == []
    # No event-report source covers it: events are unknown there, not absent.
    assert [(c["state"], c["metric"]) for c in empty["coverage"]] == [
        ("unknown", {"name": "event_cases_in_view", "value": None})
    ]
    assert empty["synthetic"] is True  # every source in this database is invented
    api.conn.run("ALTER TABLE eye.capture_batch DISABLE TRIGGER capture_batch_guard")
    api.conn.run("UPDATE eye.capture_batch SET source_id = 'not-synthetic' WHERE layer = 'road'")
    marked = body(api.get("/api/v0/snapshot?bbox=-0.5,-0.5,-0.4,-0.4&layers=road"))
    assert marked["synthetic"] is False and "notice" not in marked


def test_row_limits_refuse_rather_than_truncate(api_server):
    api = api_server(api={"max_tracks": 4})
    refused = api.get("/api/v0/snapshot")
    assert refused[0].status == 413 and "tracks" in body(refused)["error"]
    # Control: a narrower area that holds fewer tracks is served in full.
    narrow = api.get("/api/v0/snapshot?bbox=0.1,-0.02,0.5,0.02")
    assert narrow[0].status == 200 and 0 < len(body(narrow)["tracks"]) <= 4


def test_response_size_limit(api_server):
    api = api_server(server={"max_response_bytes": 2048}, api={"ws_max_buffer_bytes": 16384})
    too_big = api.get("/api/v0/snapshot")
    assert too_big[0].status == 503 and "limit" in body(too_big)["error"]
    health = api.get("/api/v0/health")
    assert health[0].status == 200  # control: a small response is served


def test_api_database_sessions_are_read_only(api_server):
    api = api_server()
    reader = feed.open_reader(api.database_url, require_loopback=True, query_timeout_ms=5000)
    try:
        assert reader.run("SELECT count(*) FROM eye.observation")[0][0] > 0  # control
        with pytest.raises(DatabaseError, match="read-only"):
            reader.run("DELETE FROM eye.feed_change")
    finally:
        reader.close()


def test_database_outage_is_reported_as_unknown_not_empty(api_server, admin_url):
    from eye.storage.db import connect

    api = api_server()
    assert api.get("/api/v0/transits")[0].status == 200  # control
    name = api.database_url.rsplit("/", 1)[1]
    admin = connect(admin_url)
    admin.run(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.close()
    for path in ("/api/v0/transits", "/api/v0/snapshot"):
        response, raw = api.get(path)
        message = json.loads(raw)
        assert response.status == 503
        assert message["kind"] == "error" and "unknown" in message["error"]
    assert api.get("/api/v0/health")[0].status == 200


# --- WebSocket: handshake and messages ------------------------------------------------


def test_websocket_origin_is_checked(api_server):
    api = api_server()
    for origin in ("http://evil.example", None, "http://localhost:1"):
        refused = ws(api, origin=origin)
        assert refused.status == 403, origin
    accepted = ws(api)
    assert accepted.status == 101
    accepted.close()


def test_websocket_handshake_requires_version_13(api_server):
    api = api_server()
    assert ws(api, version="8").status == 426
    client = ws(api)
    assert client.status == 101  # control
    client.close()


def test_valid_subscribe_gets_a_snapshot_and_invalid_is_refused(api_server):
    api = api_server()
    good = ws(api)
    good.send(subscribe(AREA, DAY))
    snapshot = good.recv()
    assert snapshot["kind"] == "snapshot" and reference_valid(snapshot)
    good.close()

    for bad in (
        subscribe((-0.5, -150, 0.5, 0.5), DAY),  # latitude out of range
        {**subscribe(AREA, DAY), "extra": 1},  # closed object
        "{not json",
    ):
        client = ws(api)
        client.send(bad)
        error = client.recv()
        assert error["kind"] == "error" and error["status"] == 400
        with pytest.raises(Closed) as closed:
            client.recv()
        assert closed.value.code == 1008


def test_subscribe_outside_bounds_is_refused_but_connection_stays(api_server):
    api = api_server()
    client = ws(api)
    client.send(subscribe(AREA, {"start": "2026-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"}))
    refused = client.recv()
    assert (refused["kind"], refused["status"]) == ("error", 413)
    client.send(subscribe(AREA, DAY))  # control on the same connection
    assert client.recv()["kind"] == "snapshot"
    client.close()


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        ("oversize", 1009),
        ("unmasked", 1002),
        ("binary", 1003),
        ("fragment", 1003),
    ],
)
def test_malformed_frames_close_the_connection(api_server, frame, code):
    api = api_server()
    client = ws(api)
    if frame == "oversize":
        client.send_frame(TEXT, b"x" * 20_000)
    elif frame == "unmasked":
        client.send_frame(TEXT, b"{}", mask=False)
    elif frame == "binary":
        client.send_frame(BINARY, b"\x00")
    else:
        client.send_frame(TEXT, b"{", fin=False)
    with pytest.raises(Closed) as closed:
        while True:
            client.recv()
    assert closed.value.code == code
    control = ws(api)
    control.send(subscribe(AREA, DAY))
    assert control.recv()["kind"] == "snapshot"
    control.close()


def test_live_socket_count_is_bounded(api_server):
    api = api_server(api={"max_websockets": 1})
    first = ws(api)
    assert first.status == 101
    second = ws(api)
    assert second.status == 503
    first.close()
    time.sleep(0.5)
    third = ws(api)
    assert third.status == 101  # control: capacity returns when a socket closes
    third.close()


# --- deltas, cursors, reconnect and gaps -----------------------------------------------


def _partial_demo(api_server, **kwargs):
    """A server with the first two demo hours loaded; the rest arrive during the test."""
    return api_server(ais_files=DEMO_FILES[:2], **kwargs)


def test_deltas_follow_the_snapshot_in_sequence(api_server):
    api = _partial_demo(api_server)
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    snapshot = client.recv()
    assert {t["source_record_id"] for t in snapshot["tracks"]} == {
        "SYNV-0020",
        "SYNV-0021",
        "SYNV-0022",
        "SYNV-0023",
    }
    api.ingest(DEMO_FILES[3])  # the quiet hour: SYNV-0024 appears
    api.derive()
    api.server.hub.tick()
    first, second = client.recv(), client.recv()
    for message in (first, second):
        assert message["kind"] == "delta" and reference_valid(message)
    assert (first["sequence"], second["sequence"]) == (1, 2)
    assert first["previous_cursor"] == snapshot["cursor"]
    assert second["previous_cursor"] == first["cursor"]
    assert [t["source_record_id"] for t in first["tracks_upserted"]] == ["SYNV-0024"]
    assert [c["interval"]["start"] for c in first["coverage_upserted"]] == ["2026-02-01T15:00:00Z"]
    # The derivation run changes counts only: an empty delta that advances the cursor.
    assert second["tracks_upserted"] == second["coverage_upserted"] == []
    # Control: with nothing new, a poll sends nothing.
    api.server.hub.tick()
    client.sock.settimeout(0.5)
    with pytest.raises(TimeoutError):
        client.recv()
    client.close()


def test_reconnect_with_a_valid_cursor_gets_a_fresh_snapshot(api_server):
    api = _partial_demo(api_server)
    first = ws(api)
    first.send(subscribe(AREA, DAY))
    cursor = first.recv()["cursor"]
    first.sock.close()  # connection lost
    api.ingest(DEMO_FILES[3])
    resumed = ws(api)
    resumed.send(subscribe(AREA, DAY, resume=cursor))
    notice = resumed.recv()
    assert notice == {
        "schema_version": "eye.wire/3",
        "kind": "resync_required",
        "reason": "reconnect",
        "last_cursor": cursor,
    }
    snapshot = resumed.recv()
    assert snapshot["kind"] == "snapshot"
    assert feed.parse_cursor(snapshot["cursor"])[1] == feed.parse_cursor(cursor)[1] + 1
    assert "SYNV-0024" in {t["source_record_id"] for t in snapshot["tracks"]}
    resumed.close()


def test_reconnect_in_another_wire_version_is_refused_by_name(api_server):
    """A page speaking eye.wire/1 that reconnects with its cursor is told which
    version this server speaks and closed with 1003 (unsupported data); a
    resubscribe or reconnect would meet the same answer, so no snapshot, no
    subscription. Control: the same request in eye.wire/3 resumes normally."""
    api = _partial_demo(api_server)
    first = ws(api)
    first.send(subscribe(AREA, DAY))
    cursor = first.recv()["cursor"]
    first.sock.close()
    for version in ("eye.wire/1", "eye.wire/2", "eye.wire/4"):
        old = ws(api)
        old.send({**subscribe(AREA, DAY, resume=cursor), "schema_version": version})
        error = old.recv()
        assert reference_valid(error) and error["kind"] == "error" and error["status"] == 400
        assert error["schema_version"] == "eye.wire/3"
        assert error["error"] == (
            f"unsupported wire version {version}; this server speaks eye.wire/3 only. "
            "Reload the page"
        )
        with pytest.raises(Closed) as closed:
            old.recv()
        assert closed.value.code == 1003 and closed.value.reason == "unsupported wire version"
    resumed = ws(api)
    resumed.send(subscribe(AREA, DAY, resume=cursor))
    assert resumed.recv()["reason"] == "reconnect"
    assert resumed.recv()["kind"] == "snapshot"
    resumed.close()


@pytest.mark.parametrize(
    "cursor",
    [
        "e00000000-0000-4000-8000-000000000000:1",  # another database's epoch
        "not-a-cursor",
        None,  # filled in below: a cursor from this database's future
    ],
)
def test_expired_or_foreign_cursor_is_reported(api_server, cursor):
    api = api_server()
    if cursor is None:
        epoch, seq = feed.head(api.conn)
        cursor = feed.make_cursor(epoch, seq + 100)
    client = ws(api)
    client.send(subscribe(AREA, DAY, resume=cursor))
    notice = client.recv()
    assert (notice["kind"], notice["reason"], notice["last_cursor"]) == (
        "resync_required",
        "expired_cursor",
        cursor,
    )
    assert client.recv()["kind"] == "snapshot"
    # Control: no resume cursor means no resync notice, just the snapshot.
    client.send(subscribe(AREA, DAY))
    assert client.recv()["kind"] == "snapshot"
    client.close()


def test_subscriber_too_far_behind_gets_a_gap_resync(api_server):
    api = _partial_demo(api_server, api={"max_pending_changes": 1})
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    snapshot = client.recv()
    api.ingest(DEMO_FILES[2])
    api.ingest(DEMO_FILES[3])  # two changes pending, limit one
    api.server.hub.tick()
    notice = client.recv()
    assert (notice["kind"], notice["reason"], notice["last_cursor"]) == (
        "resync_required",
        "gap",
        snapshot["cursor"],
    )
    fresh = client.recv()
    assert fresh["kind"] == "snapshot"
    assert feed.parse_cursor(fresh["cursor"])[1] == feed.parse_cursor(snapshot["cursor"])[1] + 2
    # Control: one pending change is delivered as a delta.
    api.derive()
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and delta["previous_cursor"] == fresh["cursor"]
    client.close()


# --- slow clients and revoked access ---------------------------------------------------


SLOW = {"server": {"max_response_bytes": 65536}, "api": {"ws_max_buffer_bytes": 131072}}


def _flood(client: WsClient, count: int) -> None:
    for _ in range(count):
        try:
            client.send(subscribe(AREA, DAY))
        except OSError:
            return


def test_slow_client_is_disconnected_at_the_byte_cap(api_server):
    api = api_server(**SLOW)
    slow = ws(api, rcvbuf=4096)
    _flood(slow, 60)  # each request queues a ~25 KB snapshot; the client reads nothing
    reason = "slow client: outbound buffer cap exceeded"
    deadline = time.monotonic() + 20
    while reason not in api.server.hub.closed_reasons and time.monotonic() < deadline:
        time.sleep(0.1)
    assert list(api.server.hub.closed_reasons) == [reason]
    assert api.server.hub.subscribers() == []
    received = 0
    with pytest.raises((Closed, ConnectionResetError)) as closed:
        while True:
            if slow.recv()["kind"] == "snapshot":
                received += 1
    if closed.type is Closed:
        assert closed.value.code is None  # cut off, not a polite close frame
    assert received < 60


def test_client_that_keeps_up_is_not_disconnected(api_server):
    api = api_server(**SLOW)
    reader = ws(api, rcvbuf=4096)
    for _ in range(60):
        reader.send(subscribe(AREA, DAY))
        assert reader.recv()["kind"] == "snapshot"
    assert len(api.server.hub.subscribers()) == 1
    assert list(api.server.hub.closed_reasons) == []
    reader.close()


class SwitchableAuth:
    name = "test-switchable"

    def __init__(self) -> None:
        self.allowed = True

    def authenticate(self, headers):
        return Principal("synthetic-user", ("viewer",), True) if self.allowed else None


def test_revoked_access_closes_open_sockets(api_server):
    auth = SwitchableAuth()
    api = api_server(auth=auth)
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    assert client.recv()["kind"] == "snapshot"
    api.server.hub.recheck_auth()  # control: still allowed, still open
    client.send(subscribe(AREA, DAY))
    assert client.recv()["kind"] == "snapshot"
    auth.allowed = False
    api.server.hub.recheck_auth()
    assert client.recv() == {
        "schema_version": "eye.wire/3",
        "kind": "error",
        "status": 403,
        "error": "not authorised",
    }
    with pytest.raises(Closed) as closed:
        client.recv()
    assert closed.value.code == 1008
    assert ws(api).status == 403
    assert api.get("/api/v0/transits")[0].status == 403
    assert api.get("/api/v0/health")[0].status == 200


def test_hub_rechecks_open_sockets_on_its_own(api_server, monkeypatch):
    from eye.api import stream

    monkeypatch.setattr(stream, "AUTH_RECHECK_SECONDS", 1)
    auth = SwitchableAuth()
    api = api_server(auth=auth)  # change polling is 60 s; rechecks must not wait for it
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    assert client.recv()["kind"] == "snapshot"
    time.sleep(2.5)
    client.send(subscribe(AREA, DAY))  # control: still open after automatic rechecks
    assert client.recv()["kind"] == "snapshot"
    auth.allowed = False
    started = time.monotonic()
    assert client.recv()["status"] == 403
    with pytest.raises(Closed):
        client.recv()
    assert time.monotonic() - started < 5


def test_auth_recheck_interval_is_within_the_sixty_second_rule():
    from eye.api import stream

    assert 0 < stream.AUTH_RECHECK_SECONDS <= 60


# --- production mode -------------------------------------------------------------------


def test_production_serves_only_through_the_private_adapter(api_server, example_raw):
    import eye_test_private_auth

    raw = copy.deepcopy(example_raw)
    raw["runtime"]["mode"] = "production"
    raw["auth"] = {"adapter": "private", "private_adapter_module": "eye_test_private_auth"}
    api = api_server(mode_raw=raw, auth=eye_test_private_auth.create_auth_port(None))
    health = body(api.get("/api/v0/health"))
    assert (health["mode"], health["synthetic"]) == ("production", False)
    for path in ("/api/v0/snapshot", "/api/v0/transits"):
        assert api.get(path)[0].status == 403  # the deny-all test adapter denies
    assert ws(api).status == 403


def test_kernel_send_buffer_is_small_on_live_sockets(api_server):
    api = api_server()
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    client.recv()
    (sub,) = api.server.hub.subscribers()
    size = sub.sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
    assert size <= 4 * 32_768  # Linux reports double the requested size
    client.close()


def test_close_frame_codes_are_standard():
    from eye.api import websocket

    frame = websocket.close_frame(1008, "invalid message")
    assert frame[0] == 0x88 and struct.unpack("!H", frame[2:4])[0] == 1008
    assert websocket.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert websocket.accept_key("short") is None


def test_repository_root_is_the_server_root():
    from eye.api import server

    assert server.REPO_ROOT == REPO_ROOT


# --- O44: a batch too large for one delta is never sent cut short ---------------------------


def _capture(tmp_path, name: str, doc: dict):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _three_vessel_hour(tmp_path):
    """The first demo hour plus SYNV-0099, a copy of SYNV-0021 moved 33 km east."""
    doc = json.loads(DEMO_FILES[0].read_text(encoding="utf-8"))
    doc["note"] = "Test capture: SYNV-0020, SYNV-0021 and SYNV-0099 (east of the others)."
    extra = [
        {**m, "mmsi": "SYNV-0099", "lon": round(m["lon"] + 0.3, 7)}
        for m in doc["provider_response"]["messages"]
        if m["mmsi"] == "SYNV-0021"
    ]
    doc["provider_response"]["messages"] += extra
    return _capture(tmp_path, "three-vessels", doc)


EAST = (0.42, -0.1, 0.48, 0.1)  # holds SYNV-0099 only


def test_delta_holds_every_changed_track_in_view_even_when_the_batch_is_larger(
    api_server, tmp_path
):
    # The batch changes three tracks but only one is in view, and the limit is
    # one. The in-view track must arrive; the others are not the view's business.
    api = api_server(ais_files=[], api={"max_tracks": 1})
    client = ws(api)
    client.send(subscribe(EAST, DAY))
    snapshot = client.recv()
    assert snapshot["kind"] == "snapshot" and snapshot["tracks"] == []
    api.ingest(_three_vessel_hour(tmp_path))
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and delta["previous_cursor"] == snapshot["cursor"]
    assert [t["source_record_id"] for t in delta["tracks_upserted"]] == ["SYNV-0099"]
    client.close()


def test_batch_over_the_delta_limit_resnapshots_before_the_cursor_moves(api_server, tmp_path):
    api = api_server(ais_files=[], api={"max_tracks": 2})
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    snapshot = client.recv()
    api.ingest(_three_vessel_hour(tmp_path))  # three tracks in view, limit two
    api.server.hub.tick()
    notice = client.recv()
    assert (notice["kind"], notice["reason"]) == ("resync_required", "overflow")
    assert notice["last_cursor"] == snapshot["cursor"]  # not advanced past the batch
    refused = client.recv()  # the fresh snapshot is over the limit too
    assert (refused["kind"], refused["status"]) == ("error", 413)
    (sub,) = api.server.hub.subscribers()
    assert sub.query is None and sub.seq == feed.parse_cursor(snapshot["cursor"])[1]
    # Control: the same batch within the limit arrives as one complete delta.
    ok = api_server(ais_files=[], api={"max_tracks": 3})
    control = ws(ok)
    control.send(subscribe(AREA, DAY))
    base = control.recv()
    ok.ingest(_three_vessel_hour(tmp_path))
    ok.server.hub.tick()
    delta = control.recv()
    assert delta["kind"] == "delta" and delta["previous_cursor"] == base["cursor"]
    assert len(delta["tracks_upserted"]) == 3
    client.close()
    control.close()


def test_delta_that_would_break_the_wire_schema_is_not_sent(api_server, monkeypatch):
    api = _partial_demo(api_server)
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    snapshot = client.recv()
    real = feed.delta

    def oversized(*args, **kwargs):
        message = real(*args, **kwargs)
        message["coverage_upserted"] = message["coverage_upserted"] * 501  # past maxItems
        return message

    monkeypatch.setattr(feed, "delta", oversized)
    api.ingest(DEMO_FILES[3])
    api.server.hub.tick()
    notice = client.recv()
    assert (notice["kind"], notice["reason"], notice["last_cursor"]) == (
        "resync_required",
        "overflow",
        snapshot["cursor"],
    )
    fresh = client.recv()
    assert fresh["kind"] == "snapshot"
    assert "SYNV-0024" in {t["source_record_id"] for t in fresh["tracks"]}
    monkeypatch.setattr(feed, "delta", real)  # control: a valid delta is sent as it is
    api.derive()
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and delta["previous_cursor"] == fresh["cursor"]
    client.close()


# --- O45: a change-feed failure makes clients stale, then recovers with a snapshot ---------


def _cut_database(api, admin_url, allow: bool) -> None:
    from eye.storage.db import connect

    name = api.database_url.rsplit("/", 1)[1]
    admin = connect(admin_url)
    try:
        admin.run(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS {"true" if allow else "false"}')
        if not allow:
            admin.run(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n",
                n=name,
            )
    finally:
        admin.close()


def test_feed_failure_marks_subscribers_stale_then_resnapshots(api_server, admin_url):
    from eye.storage.db import connect

    api = _partial_demo(api_server)
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    snapshot = client.recv()
    api.server.hub.tick()  # control: a healthy poll with nothing new sends nothing
    client.sock.settimeout(0.5)
    with pytest.raises(TimeoutError):
        client.recv()
    client.sock.settimeout(10)

    _cut_database(api, admin_url, allow=False)
    api.server.hub.tick()
    stale = client.recv()
    assert (stale["kind"], stale["status"]) == ("error", 503)
    assert "stale" in stale["error"]
    api.server.hub.tick()  # still down: told once, not repeatedly
    client.sock.settimeout(0.5)
    with pytest.raises(TimeoutError):
        client.recv()
    client.sock.settimeout(10)

    _cut_database(api, admin_url, allow=True)
    writer = connect(api.database_url)
    from eye.ingest.capture import ingest

    ingest(writer, DEMO_FILES[3].read_bytes())  # changed while the feed was down
    writer.close()
    api.server.hub.tick()
    notice = client.recv()
    assert (notice["kind"], notice["reason"], notice["last_cursor"]) == (
        "resync_required",
        "gap",
        snapshot["cursor"],
    )
    fresh = client.recv()
    assert fresh["kind"] == "snapshot"
    assert "SYNV-0024" in {t["source_record_id"] for t in fresh["tracks"]}
    (sub,) = api.server.hub.subscribers()
    assert not sub.stale and not api.server.hub.unavailable
    client.close()


# --- O46: a snapshot too large to send leaves no subscription -------------------------------


def test_oversized_websocket_snapshot_leaves_no_active_subscription(api_server):
    # 12 KiB: the full view (about 22 KiB) is refused; the narrow control fits.
    api = _partial_demo(
        api_server, server={"max_response_bytes": 12288}, api={"ws_max_buffer_bytes": 16384}
    )
    client = ws(api)
    client.send(subscribe(AREA, DAY))  # four vessel tracks: far more than 12 KiB
    refused = client.recv()
    assert (refused["kind"], refused["status"]) == ("error", 413)
    (sub,) = api.server.hub.subscribers()
    assert sub.query is None
    api.ingest(DEMO_FILES[3])
    api.derive()
    api.server.hub.tick()
    client.sock.settimeout(0.5)
    with pytest.raises(TimeoutError):  # no delta without a baseline
        client.recv()
    client.sock.settimeout(10)
    # Control: a narrow view fits, becomes active and receives deltas.
    client.send(subscribe((0.14, -0.07, 0.16, 0.07), DAY))  # the quiet vessels' column only
    snapshot = client.recv()
    assert snapshot["kind"] == "snapshot" and sub.query is not None
    api.ingest(_capture_quiet_again())
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and delta["previous_cursor"] == snapshot["cursor"]
    client.close()


def _capture_quiet_again():
    """A redelivery of the quiet hour, byte-identical except its receipt time."""
    import tempfile
    from pathlib import Path

    doc = json.loads(DEMO_FILES[3].read_text(encoding="utf-8"))
    doc["attempt"]["started_at"] = "2026-02-01T16:10:00Z"
    doc["attempt"]["finished_at"] = "2026-02-01T16:10:05Z"
    path = Path(tempfile.mkdtemp()) / "quiet-again.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_failed_resubscribe_ends_the_previous_subscription(api_server):
    api = _partial_demo(api_server)
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    assert client.recv()["kind"] == "snapshot"
    client.send(subscribe(AREA, {"start": "2026-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"}))
    assert client.recv()["status"] == 413
    (sub,) = api.server.hub.subscribers()
    assert sub.query is None  # the old view does not keep sending deltas
    api.ingest(DEMO_FILES[3])
    api.server.hub.tick()
    client.sock.settimeout(0.5)
    with pytest.raises(TimeoutError):
        client.recv()
    client.close()


# --- O47: stable, collision-resistant track ids ----------------------------------------------


def _long_record_flights(tmp_path):
    """Two invented flights whose record ids share their first 126 characters."""
    doc = json.loads(
        (REPO_ROOT / "tests/fixtures/synthetic/captures/001-flight-ok.json").read_text()
    )
    doc["note"] = "Test capture: two flights with long, nearly identical record ids."
    for key in ("start", "end"):
        doc["request"][key] = doc["request"][key].replace("T00:", "T02:")
    for key in ("started_at", "finished_at", "observed_start", "observed_end"):
        doc["attempt"][key] = doc["attempt"][key].replace("T00:", "T02:")
    base = doc["provider_response"]["records"]
    records = []
    for suffix, shift in (("A", 0.0), ("B", 0.1)):
        for r in base:
            records.append(
                {
                    **r,
                    "record_id": "SYN-" + "X" * 122 + suffix,
                    "observed_time": r["observed_time"].replace("T00:", "T02:"),
                    "source_published_time": r["source_published_time"].replace("T00:", "T02:"),
                    "lat": round(r["lat"] + shift, 6),
                }
            )
    doc["provider_response"]["records"] = records
    return _capture(tmp_path, "long-ids", doc)


def test_long_record_ids_get_distinct_stable_track_ids(api_server, tmp_path):
    import hashlib

    api = api_server()
    api.ingest(_long_record_flights(tmp_path))
    query = "/api/v0/snapshot?start=2026-01-01T02:00:00Z&end=2026-01-01T03:00:00Z&layers=flight"
    tracks = body(api.get(query))["tracks"]
    records = sorted(t["source_record_id"] for t in tracks)
    assert records == ["SYN-" + "X" * 122 + "A", "SYN-" + "X" * 122 + "B"]
    ids = {t["id"] for t in tracks}
    assert len(ids) == 2  # the old truncated ids were identical for these two
    # Independent computation of the documented rule, and stability across calls.
    for t in tracks:
        digest = hashlib.sha256(
            json.dumps([t["source"], t["kind"], t["source_record_id"]]).encode()
        ).hexdigest()
        assert t["id"] == f"trk-{digest[:32]}"
    assert {t["id"] for t in body(api.get(query))["tracks"]} == ids
    # Control: short records keep one stable id each, the same over REST and WebSocket.
    rest = {t["source_record_id"]: t["id"] for t in body(api.get("/api/v0/snapshot"))["tracks"]}
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    live = {t["source_record_id"]: t["id"] for t in client.recv()["tracks"]}
    assert live == rest and len(set(rest.values())) == len(rest) == 5
    client.close()


def test_failure_during_recovery_keeps_the_subscription_and_retries(
    api_server, admin_url, monkeypatch
):
    api = _partial_demo(api_server)
    client = ws(api)
    client.send(subscribe(AREA, DAY))
    snapshot = client.recv()
    _cut_database(api, admin_url, allow=False)
    api.server.hub.tick()
    assert client.recv()["status"] == 503
    _cut_database(api, admin_url, allow=True)

    real = feed.snapshot
    calls = []

    def fails_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("database lost mid-snapshot")
        return real(*args, **kwargs)

    monkeypatch.setattr(feed, "snapshot", fails_once)
    api.server.hub.tick()  # recovery starts, then fails
    assert client.recv()["reason"] == "gap"
    (sub,) = api.server.hub.subscribers()
    assert sub.query is not None and sub.stale  # not dropped: still owed a snapshot
    api.server.hub.tick()  # control: the retry succeeds
    assert client.recv()["reason"] == "gap"
    fresh = client.recv()
    assert fresh["kind"] == "snapshot" and fresh["cursor"] == snapshot["cursor"]
    assert not sub.stale
    client.close()


# --- O48: a correction that moves a track out of view -----------------------------------------

HOUR3 = {"start": "2026-01-01T03:00:00Z", "end": "2026-01-01T04:00:00Z"}


def flight_capture(tmp_path, name: str, lon: float, published: str, received: str):
    """One invented flight position at 03:00; a later publication is a correction."""
    doc = {
        "capture_format": "eye.synthetic-capture/2",
        "synthetic": True,
        "source_id": "synthetic-fixture",
        "adapter_version": "synthetic-adapter/2",
        "note": f"Test capture {name}: SYN-FLT-900 at 03:00, published {published}.",
        "layer": "flight",
        "request": {
            "bbox": [-1.0, -1.0, 1.0, 1.0],
            "start": "2026-01-01T02:55:00Z",
            "end": "2026-01-01T03:05:00Z",
            "expected_interval_s": 600,
        },
        "attempt": {
            "written_by": "EYE capture adapter; finished_at is the EYE receipt time",
            "started_at": received,
            "finished_at": received,
            "provider_status": "ok",
            "quota_cost": 0,
            "observed_start": "2026-01-01T02:55:00Z",
            "observed_end": "2026-01-01T03:05:00Z",
        },
        "provider_response": {
            "records": [
                {
                    "record_id": "SYN-FLT-900",
                    "observed_time": "2026-01-01T03:00:00Z",
                    "source_published_time": published,
                    "lon": lon,
                    "lat": 0.0,
                    "alt_m": 9000,
                    "confidence": 0.9,
                    "quality_flags": [],
                }
            ]
        },
    }
    return _capture(tmp_path, name, doc)


def _flight_in_view(api_server, tmp_path):
    api = api_server(ais_files=[])
    api.ingest(
        flight_capture(tmp_path, "original", 0.1, "2026-01-01T03:00:03Z", "2026-01-01T03:05:02Z")
    )
    client = ws(api)
    client.send(subscribe(AREA, HOUR3, layers=("flight",)))
    snapshot = client.recv()
    (track,) = snapshot["tracks"]
    assert (track["source_record_id"], track["points"][0]["lon"]) == ("SYN-FLT-900", 0.1)
    return api, client, snapshot


def test_correction_moving_the_only_point_out_of_view_forces_a_snapshot(api_server, tmp_path):
    api, client, snapshot = _flight_in_view(api_server, tmp_path)
    api.ingest(
        flight_capture(tmp_path, "moved-out", 0.8, "2026-01-01T03:20:00Z", "2026-01-01T03:20:05Z")
    )
    api.server.hub.tick()
    notice = client.recv()
    assert (notice["kind"], notice["reason"]) == ("resync_required", "overflow")
    assert notice["last_cursor"] == snapshot["cursor"]  # not advanced past the correction
    fresh = client.recv()
    assert fresh["kind"] == "snapshot" and fresh["tracks"] == []  # the track left the view
    assert feed.parse_cursor(fresh["cursor"])[1] == feed.parse_cursor(snapshot["cursor"])[1] + 1
    # REST agrees independently: the current position is outside the area.
    rest = body(
        api.get(
            "/api/v0/snapshot?start=2026-01-01T03:00:00Z&end=2026-01-01T04:00:00Z&layers=flight"
        )
    )
    assert rest["tracks"] == []
    client.close()


def test_correction_that_stays_in_view_updates_normally(api_server, tmp_path):
    api, client, snapshot = _flight_in_view(api_server, tmp_path)
    api.ingest(
        flight_capture(tmp_path, "moved-in", 0.2, "2026-01-01T03:20:00Z", "2026-01-01T03:20:05Z")
    )
    api.server.hub.tick()
    delta = client.recv()
    assert delta["kind"] == "delta" and delta["previous_cursor"] == snapshot["cursor"]
    (track,) = delta["tracks_upserted"]
    assert track["id"] == snapshot["tracks"][0]["id"]
    assert [p["lon"] for p in track["points"]] == [0.2]  # the correction replaced the point
    client.close()
